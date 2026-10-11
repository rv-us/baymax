"""Stream a webcam to the pose server on the GX10 and draw the skeleton it sends back.

A background thread keeps sending the newest frame (frames captured while a request
is in flight are skipped, never queued), so the video stays smooth and the skeleton
is always from the most recent frame the server saw.

    python scripts/pose_stream.py                        # laptop webcam -> GX10 at :9000
    python scripts/pose_stream.py --url http://100.69.16.72:9000 --width 480
    python scripts/pose_stream.py --image person.jpg     # one image, print the result

The overlay shows pose fps (frames the server processed per second), the round trip
per frame, and how much of it was inference on the GX10. q quits.
"""

import argparse
import os
import threading
import time

import cv2
import requests

DEFAULT_URL = os.environ.get("BAYMAX_POSE_URL", "http://169.233.72.183:9000")

# MediaPipe pose landmark pairs to draw (face points left out except the nose).
CONNECTIONS = [(11, 12), (11, 13), (13, 15), (12, 14), (14, 16), (11, 23), (12, 24), (23, 24),
               (23, 25), (25, 27), (24, 26), (26, 28), (27, 31), (28, 32), (15, 19), (16, 20)]
POINTS = sorted({i for pair in CONNECTIONS for i in pair} | {0})


def encode(frame, width: int) -> bytes:
    h, w = frame.shape[:2]
    if w > width:
        frame = cv2.resize(frame, (width, int(h * width / w)), interpolation=cv2.INTER_AREA)
    ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
    if not ok:
        raise RuntimeError("JPEG encoding failed")
    return jpg.tobytes()


class Streamer:
    """Sends the newest frame whenever the previous request has finished."""

    def __init__(self, url: str, width: int):
        self.url, self.width = url.rstrip("/") + "/pose", width
        self.session = requests.Session()  # keep-alive: no new TCP connection per frame
        self.result, self.error = None, None
        self.round_trip_ms = 0.0
        self.done_times = []
        self._frame = None
        self._lock = threading.Condition()
        self._stop = False
        threading.Thread(target=self._loop, daemon=True).start()

    def offer(self, frame):
        with self._lock:
            self._frame = frame
            self._lock.notify()

    def stop(self):
        with self._lock:
            self._stop = True
            self._lock.notify()

    def fps(self) -> float:
        now = time.time()
        self.done_times = [t for t in self.done_times if now - t < 2.0]
        return len(self.done_times) / 2.0

    def _loop(self):
        while True:
            with self._lock:
                while self._frame is None and not self._stop:
                    self._lock.wait()
                if self._stop:
                    return
                frame, self._frame = self._frame, None
            started = time.perf_counter()
            try:
                r = self.session.post(self.url, data=encode(frame, self.width), timeout=5,
                                      headers={"content-type": "image/jpeg"})
                r.raise_for_status()
                self.result, self.error = r.json(), None
            except Exception as e:  # noqa: BLE001  (keep the video running; show the error)
                self.error = f"{type(e).__name__}: {e}"[:120]
                time.sleep(0.5)
                continue
            self.round_trip_ms = (time.perf_counter() - started) * 1000
            self.done_times.append(time.time())


def draw(frame, result: dict):
    if not result or not result.get("found"):
        return
    h, w = frame.shape[:2]
    pts = {i: (int(x * w), int(y * h), v) for i, (x, y, _, v) in enumerate(result["landmarks"])}
    for a, b in CONNECTIONS:
        if pts[a][2] > 0.5 and pts[b][2] > 0.5:
            cv2.line(frame, pts[a][:2], pts[b][:2], (0, 255, 0), 3, cv2.LINE_AA)
    for i in POINTS:
        if pts[i][2] > 0.5:
            cv2.circle(frame, pts[i][:2], 5, (0, 0, 255), -1, cv2.LINE_AA)


def status_text(s: Streamer) -> str:
    if s.error:
        return f"error: {s.error}"
    r = s.result
    if r is None:
        return "waiting for the server..."
    return (f"pose {s.fps():.1f} fps | round trip {s.round_trip_ms:.0f} ms "
            f"(GX10 inference {r['infer_ms']:.0f} ms) | {'person' if r['found'] else 'no person'}")


def main():
    ap = argparse.ArgumentParser(description="Webcam -> pose server on the GX10.")
    ap.add_argument("--url", default=DEFAULT_URL, help=f"pose server (default {DEFAULT_URL})")
    ap.add_argument("--cam", type=int, default=0)
    ap.add_argument("--width", type=int, default=640, help="resize frames to this width before sending")
    ap.add_argument("--image", help="send one image and print the result")
    args = ap.parse_args()
    if "://" not in args.url:
        args.url = f"http://{args.url}"

    if args.image:
        frame = cv2.imread(args.image)
        if frame is None:
            raise SystemExit(f"can't read {args.image}")
        started = time.perf_counter()
        r = requests.post(args.url.rstrip("/") + "/pose", data=encode(frame, args.width), timeout=10)
        r.raise_for_status()
        res = r.json()
        print(f"round trip {(time.perf_counter() - started) * 1000:.0f} ms, inference "
              f"{res['infer_ms']} ms, person found: {res['found']}")
        if res["found"]:
            names = {0: "nose", 11: "l_shoulder", 12: "r_shoulder", 15: "l_wrist", 16: "r_wrist"}
            for i, name in names.items():
                x, y, _, v = res["landmarks"][i]
                print(f"  {name:10s} x={x:.2f} y={y:.2f} visibility={v:.2f}")
        return

    cap = cv2.VideoCapture(args.cam)
    if not cap.isOpened():
        raise SystemExit(f"can't open camera {args.cam} (on macOS, allow camera access for your "
                         "terminal or VS Code in System Settings > Privacy & Security > Camera)")
    streamer = Streamer(args.url, args.width)
    print(f"streaming camera {args.cam} to {args.url}; q quits")
    try:
        while True:
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.01)
                continue
            streamer.offer(frame)
            shown = frame.copy()
            draw(shown, streamer.result)
            cv2.rectangle(shown, (0, 0), (shown.shape[1], 30), (0, 0, 0), -1)
            cv2.putText(shown, status_text(streamer), (8, 21), cv2.FONT_HERSHEY_SIMPLEX, 0.6,
                        (255, 255, 255), 1, cv2.LINE_AA)
            cv2.imshow("pose on GX10 (q quits)", shown)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break
    except KeyboardInterrupt:
        pass
    finally:
        streamer.stop()
        cap.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
