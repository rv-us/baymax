"""Test pt_agent's face tracking on the real robot, without the LLM. Run on the GX10.

Uses the same pieces as python -m pt_agent: the robot's camera (streaming.robot_camera),
YOLO pose and pt_agent.perception's head following. Follows the biggest person in view and
prints, twice a second, what it sees and what it sends:

    11:40:05  cam 24.8 fps  pose 11.9 fps  people 1  [id 1 face x=0.71 y=0.32 size 0.21]  head pan 1460 tilt 1650

and at the end a summary, including whether head moves brought the face toward the center
of the frame (if they mostly push it away, the pan or tilt direction is backwards: rerun
with --pan-sign / --tilt-sign flipped).

    python scripts/track_test.py                    # 60 s, head follows ($BAYMAX_API_URL)
    python scripts/track_test.py --no-head          # detection only: head commands logged, not sent
    python scripts/track_test.py --pan-sign 1       # try the other pan direction

Annotated frames go to /tmp/track_test/ every --save-every seconds (what YOLO saw).
"""
import argparse
import os
import sys
import threading
import time

import cv2

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))


class LoggingBackend:
    """Wraps the robot API backend: logs every head command, and with send=False doesn't send."""

    def __init__(self, inner, send: bool):
        self.inner, self.send = inner, send
        self.head_calls, self.errors, self.latencies = [], [], []
        self._lock = threading.Lock()

    def call(self, path, **kwargs):
        if path != "servo/head":
            return self.inner.call(path, **kwargs)
        started = time.time()
        if not self.send:
            out = {"head": {k: kwargs[k] for k in ("pan", "tilt") if k in kwargs}}
        else:
            try:
                out = self.inner.call(path, **kwargs)
            except Exception as e:  # noqa: BLE001  (record it; perception skips the move)
                with self._lock:
                    self.errors.append(str(e))
                raise
        with self._lock:
            self.head_calls.append((started, kwargs.get("pan"), kwargs.get("tilt")))
            self.latencies.append(time.time() - started)
        return out


def annotate(frame, people, perception):
    h, w = frame.shape[:2]
    out = frame.copy()
    cv2.line(out, (w // 2, 0), (w // 2, h), (255, 255, 255), 1)
    cv2.line(out, (0, h // 2), (w, h // 2), (255, 255, 255), 1)
    for p in people:
        x, y = int(p.body.face[0] * w), int(p.body.face[1] * h)
        color = (0, 0, 255) if p.id == perception.target_id else (0, 255, 0)
        cv2.circle(out, (x, y), 8, color, -1)
        cv2.putText(out, f"id {p.id} size {p.body.size:.2f}", (x + 10, y), cv2.FONT_HERSHEY_SIMPLEX,
                    0.5, color, 1)
    cv2.putText(out, f"pan {perception.head.pan} tilt {perception.head.tilt}", (8, 20),
                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
    return out


def main():
    ap = argparse.ArgumentParser(description="Face tracking test on the real robot (no LLM).")
    ap.add_argument("--url", default=os.environ.get("BAYMAX_API_URL", "http://169.233.72.93:8000"))
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--no-head", action="store_true", help="detect only; log head commands, don't send")
    ap.add_argument("--pan-sign", type=int, choices=[-1, 1], help="override BAYMAX_PAN_SIGN")
    ap.add_argument("--tilt-sign", type=int, choices=[-1, 1], help="override BAYMAX_TILT_SIGN")
    ap.add_argument("--detector", choices=["yolo", "mediapipe"], default="yolo")
    ap.add_argument("--camera", choices=["auto", "udp", "mjpeg"], default="auto")
    ap.add_argument("--save-every", type=float, default=3.0, help="seconds between saved frames (0: never)")
    ap.add_argument("--out", default="/tmp/track_test")
    args = ap.parse_args()

    # The signs are read when perception is imported.
    if args.pan_sign:
        os.environ["BAYMAX_PAN_SIGN"] = str(args.pan_sign)
    if args.tilt_sign:
        os.environ["BAYMAX_TILT_SIGN"] = str(args.tilt_sign)

    from pt_agent import perception as perc
    from robot_api.client import HttpBackend
    from streaming.robot_camera import RobotCamera

    backend = LoggingBackend(HttpBackend(args.url), send=not args.no_head)
    camera = RobotCamera(args.url, mode=args.camera)
    print(f"robot {args.url}; pan sign {perc.PAN_SIGN}, tilt sign {perc.TILT_SIGN}; "
          f"head {'NOT sent (--no-head)' if args.no_head else 'follows'}; loading {args.detector}...")
    perception = perc.Perception(camera, perc.make_detector(args.detector), backend).start()
    print(f"camera mode: {camera.mode}")
    os.makedirs(args.out, exist_ok=True)

    # Per-frame camera rate (separate from how fast pose detection keeps up).
    cam_times, last_t = [], 0.0
    stats = {"ticks": 0, "with_people": 0, "frames_saved": 0}
    moves = []  # (face offset before, face offset ~0.6 s after a head move)
    pending = []
    started = last_print = last_save = time.time()
    try:
        while time.time() - started < args.seconds:
            now = time.time()
            frame, t = camera.latest()
            if frame is not None and t != last_t:
                cam_times.append(t)
                last_t = t
            people = sorted(perception.persons(), key=lambda p: -p.body.size)
            if people and (perception.target_id is None or perception.person(perception.target_id) is None):
                perception.track(people[0].id)
            target = perception.person(perception.target_id) if perception.target_id else None

            # Did head moves bring the face toward the center?
            with backend._lock:
                calls = list(backend.head_calls)
            if target is not None and calls and (not pending or calls[-1][0] > pending[-1][0]):
                off = (target.body.face[0] - 0.5, target.body.face[1] - 0.5)
                pending.append((calls[-1][0], off))
            for item in list(pending):
                if now - item[0] > 0.6:
                    pending.remove(item)
                    if target is not None:
                        moves.append((item[1], (target.body.face[0] - 0.5, target.body.face[1] - 0.5)))

            if now - last_print >= 0.5:
                last_print = now
                stats["ticks"] += 1
                stats["with_people"] += bool(people)
                recent = [x for x in cam_times if now - x < 2]
                desc = "  ".join(f"[id {p.id} face x={p.body.face[0]:.2f} y={p.body.face[1]:.2f} "
                                 f"size {p.body.size:.2f}{' *' if p.id == perception.target_id else ''}]"
                                 for p in people[:3])
                print(f"{time.strftime('%H:%M:%S')}  cam {len(recent) / 2:4.1f} fps  pose {perception.fps:4.1f} fps  "
                      f"people {len(people)}  {desc or '-'}  head pan {perception.head.pan} tilt {perception.head.tilt}",
                      flush=True)
            if args.save_every and frame is not None and now - last_save >= args.save_every:
                last_save = now
                path = os.path.join(args.out, f"{time.strftime('%H%M%S')}.jpg")
                cv2.imwrite(path, annotate(frame, people, perception))
                stats["frames_saved"] += 1
            time.sleep(0.05)
    except KeyboardInterrupt:
        pass
    finally:
        perception.stop()
        camera.stop()

    took = time.time() - started
    print("\n=== summary ===")
    print(f"camera: {camera.mode}, {len(cam_times)} frames in {took:.0f} s ({len(cam_times) / took:.1f} fps)")
    print(f"someone in view: {stats['with_people']}/{stats['ticks']} checks")
    lat = sorted(backend.latencies)
    print(f"head commands: {len(backend.head_calls)} {'logged' if args.no_head else 'sent'}, "
          f"{len(backend.errors)} failed" + (f", median {lat[len(lat) // 2] * 1000:.0f} ms each" if lat else ""))
    if backend.errors:
        print("  first error:", backend.errors[0][:200])
    if moves:
        x_better = sum(abs(a[0]) > abs(b[0]) for a, b in moves if abs(a[0]) > 0.06)
        x_total = sum(1 for a, _ in moves if abs(a[0]) > 0.06)
        y_better = sum(abs(a[1]) > abs(b[1]) for a, b in moves if abs(a[1]) > 0.06)
        y_total = sum(1 for a, _ in moves if abs(a[1]) > 0.06)
        if x_total:
            print(f"pan: face got closer to center after {x_better}/{x_total} moves"
                  + ("  <- likely backwards, try --pan-sign %d" % -perc.PAN_SIGN if x_better < x_total / 2 else ""))
        if y_total:
            print(f"tilt: face got closer to center after {y_better}/{y_total} moves"
                  + ("  <- likely backwards, try --tilt-sign %d" % -perc.TILT_SIGN if y_better < y_total / 2 else ""))
    print(f"saved {stats['frames_saved']} annotated frames to {args.out}")


if __name__ == "__main__":
    main()
