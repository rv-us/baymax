"""
Arms-only mimic: one RGB camera -> TonyPi digital twin arms (servos 6, 7, 8, 14, 15, 16).

    python arm_mimic.py                  webcam, opens the twin viewer
    python arm_mimic.py --source clip.mp4 --headless-test 300
    python arm_mimic.py --api-url http://192.168.1.149:8000   robot's head camera in, arm pulses out

Keys: c = recalibrate from scratch, x = emergency stand and pause (press again to resume), q = quit.
Calibration is automatic: face the camera with straight arms (hanging or T-pose) and hold still.
Copy mode: your right arm drives the robot's right arm. The frame is mirrored for display only.
"""
import argparse
import os
import threading
import time

import cv2
import numpy as np

import calibration
import solver
from filters import OneEuro, slew
from mediapose_est import STAND_PULSES, GC0308HardwareSimulator
from retarget import ARM_IDX, ArmIK, shoulder_frame

MODEL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "laptop_mvp", "pose_landmarker_full.task")
COLORS = {"TRACKING": (80, 220, 80), "HOLD": (0, 210, 255), "LOST": (60, 60, 255)}
LOST_STEP_SCALE = 1.0 / 6.0  # a lost arm eases back to rest at a sixth of the tracking slew rate


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", default=None, help="camera index, stream URL, 'robot', or video file for offline "
                   "replay (default: 'robot' with --api-url, i.e. its camera over UDP or MJPEG, else webcam 0)")
    p.add_argument("--focal-px", type=float, default=None, help="focal length in pixels (default 0.9 * width)")
    p.add_argument("--min-cutoff", type=float, default=1.5, help="One Euro min cutoff in Hz (lower = smoother when still)")
    p.add_argument("--beta", type=float, default=4.0, help="One Euro beta (higher = less lag when moving)")
    p.add_argument("--min-vis", type=float, default=0.5, help="minimum landmark visibility to track an arm")
    p.add_argument("--hold-s", type=float, default=1.0, help="seconds an arm holds before easing to rest")
    p.add_argument("--max-jump", type=float, default=0.5, help="max wrist move per frame in metres (left/right swap guard)")
    p.add_argument("--max-step-deg", type=float, default=12.0, help="joint slew limit in degrees per frame at 30 fps")
    p.add_argument("--max-pulse-step", type=int, default=33, help="max servo pulse change per tick (33 = robot_api's 1 pulse/ms)")
    p.add_argument("--api-url", default=None, help="robot_api base URL. Only when given, arm pulses go to servo/set_many at 30 Hz, "
                   "calibration progress to the LED matrix, and frames come from the robot's camera")
    p.add_argument("--no-twin", action="store_true", help="do not start the digital twin")
    p.add_argument("--twin-port", type=int, default=8765)
    p.add_argument("--record-landmarks", default=None, metavar="OUT.npz", help="save raw landmarks of the run")
    p.add_argument("--sim-camera", action="store_true", help="degrade frames like the robot's GC0308 camera")
    p.add_argument("--headless-test", type=int, default=0, metavar="N", help="process N frames without a window, print stats, exit")
    args = p.parse_args(argv)
    if args.source is None:
        args.source = ROBOT if args.api_url else "0"
    return args


ROBOT = "robot"  # --source robot: the robot's camera via --api-url (UDP if possible, else MJPEG)


def is_live(source):
    return source == ROBOT or source.isdigit() or "://" in source


def robot_frames(api_url):
    """The robot's camera through streaming.robot_camera.RobotCamera, newest frames only."""
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    from streaming.robot_camera import RobotCamera

    cam = RobotCamera(api_url).start()
    print(f"robot camera: {cam.mode}")
    last, t0 = 0.0, time.monotonic()
    try:
        while True:
            frame, t = cam.latest()
            if frame is None or t == last:
                time.sleep(0.002)
                continue
            last = t
            yield frame, time.monotonic() - t0
    finally:
        cam.stop()


class LatestFrame:
    """Camera or stream reader thread that keeps only the newest frame, so inference never works on
    a stale one."""

    def __init__(self, source):
        self.cap = cv2.VideoCapture(int(source) if source.isdigit() else source)
        if source.isdigit():
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        if not self.cap.isOpened():
            raise SystemExit(f"could not open camera {source.split('token=')[0]}")
        self.cond, self.frame, self.stamp, self.seq, self.alive = threading.Condition(), None, 0.0, 0, True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while self.alive:
            ok, frame = self.cap.read()
            with self.cond:
                self.alive = self.alive and ok
                if ok:
                    self.frame, self.stamp, self.seq = frame, time.monotonic(), self.seq + 1
                self.cond.notify_all()
        self.cap.release()

    def read(self, last_seq):
        with self.cond:
            self.cond.wait_for(lambda: self.seq != last_seq or not self.alive, timeout=1.0)
            return None if self.seq == last_seq else (self.frame, self.stamp, self.seq)


def frames(source, realtime, api_url=None):
    """Yields (bgr frame, seconds). Live cameras and streams drop stale frames, files replay every frame."""
    if source == ROBOT:
        if not api_url:
            raise SystemExit("--source robot needs --api-url")
        yield from robot_frames(api_url)
        return
    if is_live(source):
        cam, seq, t0 = LatestFrame(source), 0, time.monotonic()
        try:
            while cam.alive:
                got = cam.read(seq)
                if got is not None:
                    seq = got[2]
                    yield got[0], got[1] - t0
        finally:
            cam.alive = False
        return
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        raise SystemExit(f"could not open video {source}")
    fps, i, t0 = cap.get(cv2.CAP_PROP_FPS) or 30.0, 0, time.monotonic()
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if realtime:
            time.sleep(max(0.0, i / fps - (time.monotonic() - t0)))
        yield frame, i / fps
        i += 1
    cap.release()


def make_landmarker():
    import mediapipe as mp
    from mediapipe.tasks.python import BaseOptions, vision

    with open(MODEL, "rb") as f:
        options = vision.PoseLandmarkerOptions(base_options=BaseOptions(model_asset_buffer=f.read()),
                                               running_mode=vision.RunningMode.VIDEO, num_poses=1)
    landmarker = vision.PoseLandmarker.create_from_options(options)

    def detect(bgr, t_ms):
        """(image (33, 3) normalized, world (33, 3) metres, visibility (33,)) or None."""
        image = mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
        res = landmarker.detect_for_video(image, t_ms)
        if not res.pose_landmarks or not res.pose_world_landmarks:
            return None
        img = np.array([[l.x, l.y, l.z] for l in res.pose_landmarks[0]])
        world = np.array([[l.x, l.y, l.z] for l in res.pose_world_landmarks[0]])
        vis = np.array([l.visibility if l.visibility is not None else 1.0 for l in res.pose_landmarks[0]])
        return img, world, vis

    return detect, landmarker.close


def new_arm(side, args):
    return {"ik": ArmIK(side), "euro": OneEuro(args.min_cutoff, args.beta), "signs": (1.0, 1.0),
            "state": "LOST", "t_ok": -1e9, "wrist": None, "pts": None,
            "target": list(solver.REST), "angles": list(solver.REST)}


def track_arms(arms, lm, cal, t, dt, size, args):
    """One frame: landmarks -> per arm state and slew limited angles (pitch, roll, interior elbow)."""
    frame = px = None
    if lm is not None:
        img, world, vis = lm
        px = img[:, :2] * size
        focal, center = args.focal_px or 0.9 * size[0], 0.5 * np.asarray(size, dtype=np.float64)
        sh = solver.shoulders_3d(px[11], px[12], world[11], world[12], cal["shoulder_m"], focal, center)
        frame = shoulder_frame(*sh) if sh is not None else None
    for side, arm in arms.items():
        idx = list(ARM_IDX[side])
        sol = None
        if frame is not None and vis[idx].min() >= args.min_vis and np.all(np.isfinite(px[idx])):
            elbow, wrist, arm["signs"] = solver.reconstruct_arm(
                sh[side == "R"], px[idx[1]], px[idx[2]], world[idx], arm["signs"],
                (cal["upper"][side], cal["fore"][side]), focal, center)
            pts = (np.stack([sh[side == "R"], elbow, wrist]) - frame[1]) @ frame[0].T
            # Swap guard: a wrist cannot teleport. Reject the frame and hold instead.
            if arm["wrist"] is None or np.linalg.norm(pts[2] - arm["wrist"]) <= args.max_jump:
                arm["wrist"] = pts[2]
                arm["pts"] = arm["euro"](pts, t)
                sol = arm["ik"].solve(*arm["pts"], q_prev_deg=arm["angles"][:2])
        if sol is not None:
            arm["state"], arm["target"], arm["t_ok"] = "TRACKING", list(sol), t
        elif t - arm["t_ok"] < args.hold_s:
            arm["state"] = "HOLD"
        else:
            arm["state"], arm["target"], arm["wrist"], arm["pts"] = "LOST", list(solver.REST), None, None
            arm["euro"].reset()
        step = args.max_step_deg * (LOST_STEP_SCALE if arm["state"] == "LOST" else 1.0)
        arm["angles"] = [slew(a, g, step, dt) for a, g in zip(arm["angles"], arm["target"])]


def twin_landmarks(arms):
    """Filtered arm points as a 33 landmark set for the twin's skeleton view (torso faded in)."""
    pts, vis = np.zeros((33, 3)), np.zeros(33)
    for side, arm in arms.items():
        if arm["pts"] is None:
            return None, None
        sh, hip = ARM_IDX[side][0], 23 if side == "L" else 24
        pts[list(ARM_IDX[side])], vis[list(ARM_IDX[side])] = arm["pts"], 1.0
        pts[hip] = arm["pts"][0] * (1.0, 0.6, 1.0) + (0.0, 0.0, -0.5)
        pts[sh + 6:sh + 11:2] = arm["pts"][2]  # hand landmarks collapse onto the wrist
    pts[0] = (0.0, 0.0, 0.2)
    return pts + (0.0, 0.0, 0.25), vis


def start_api_sender(url, hz=30.0):
    """Background sender to robot_api: servo/set_many for box["targets"], and box["matrix"] =
    (path, kwargs) for the LED matrix. Latest values only, never blocks the loop."""
    from robot_api.client import HttpBackend

    backend, box = HttpBackend(url=url, timeout=2.0), {"targets": None, "matrix": None, "error": None}

    def loop():
        while True:
            t0 = time.monotonic()
            targets, box["targets"] = box["targets"], None
            matrix, box["matrix"] = box["matrix"], None
            try:
                if targets:
                    backend.call("servo/set_many", targets=targets, wait=False)
                if matrix:
                    backend.call(matrix[0], **matrix[1])
                box["error"] = None if targets or matrix else box["error"]
            except Exception as e:  # keep tracking, show the error on the HUD
                box["error"] = str(e)[:60]
            time.sleep(max(0.0, 1.0 / hz - (time.monotonic() - t0)))

    threading.Thread(target=loop, daemon=True).start()
    return box


def draw_hud(frame, lm, arms, pulses, status):
    """Mirrored view with the arm skeleton, per arm state, angles, pulses and one status line."""
    view = cv2.flip(frame, 1)
    h, w = view.shape[:2]
    if lm is not None:
        px = lm[0][:, :2] * (w, h)
        px[:, 0] = w - 1 - px[:, 0]
        cv2.line(view, tuple(map(int, px[11])), tuple(map(int, px[12])), (220, 220, 220), 1, cv2.LINE_AA)
        for side, arm in arms.items():
            a, b, c = (tuple(map(int, px[i])) for i in ARM_IDX[side])
            for p, q in ((a, b), (b, c)):
                cv2.line(view, p, q, COLORS[arm["state"]], 3, cv2.LINE_AA)
            for p in (a, b, c):
                cv2.circle(view, p, 5, (255, 255, 255), -1)
    lines = [(status, (255, 255, 255))]
    for side, arm in arms.items():
        pitch, roll, elbow = arm["angles"]
        ids = solver.ARM_SERVOS[side]
        lines.append((f"{side} {arm['state']:8s} pitch {pitch:5.0f} roll {roll:5.0f} elbow {elbow:4.0f}   "
                      + " ".join(f"S{i}:{pulses[i]}" for i in ids), COLORS[arm["state"]]))
    view[:28] //= 3  # dark strips behind the text
    view[h - 48:] //= 3
    for i, (text, color) in enumerate(lines):
        y = 19 if i == 0 else h - 30 + 20 * (i - 1)
        cv2.putText(view, text, (8, y), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)
    return view


def main():
    args = parse_args()
    headless = args.headless_test > 0
    calib = calibration.new_state(calibration.load())
    persist = is_live(args.source)  # replaying a file never overwrites your calibration.json
    detect, close_landmarker = make_landmarker()
    sim = GC0308HardwareSimulator() if args.sim_camera else None
    twin = None
    if not args.no_twin:
        from digital_twin import TonyPiDigitalTwin
        twin = TonyPiDigitalTwin(headless=headless, port=0 if headless else args.twin_port).start()
        print(f"[twin] {twin.url}")
    api = start_api_sender(args.api_url) if args.api_url else None

    arms = {side: new_arm(side, args) for side in "LR"}
    pulses = solver.stand_arm_pulses()
    paused, bar = False, None
    record = {"image": [], "world": [], "vis": [], "t": []}
    stats = {s: {"min": [np.inf] * 3, "max": [-np.inf] * 3, "TRACKING": 0, "HOLD": 0, "LOST": 0} for s in "LR"}
    n, fps, t_prev, ts_prev, wall_prev, wall_start = 0, 0.0, None, -1, time.monotonic(), time.monotonic()

    try:
        for frame, t in frames(args.source, realtime=not headless, api_url=args.api_url):
            if sim is not None:
                frame, _ = sim.apply(frame)
            h, w = frame.shape[:2]
            dt = 1.0 / 30.0 if t_prev is None else max(t - t_prev, 1e-3)
            ts_prev = max(ts_prev + 1, int(t * 1000))  # VIDEO mode needs strictly increasing timestamps
            lm = detect(frame, ts_prev)
            t_prev, n = t, n + 1
            if args.record_landmarks:
                blank = np.full((33, 3), np.nan)
                for key, val in zip(("image", "world", "vis", "t"), (*(lm or (blank, blank, blank[:, 0])), t)):
                    record[key].append(val)

            if lm is None:
                calibration.update(calib, None, None, None, t)
            elif calibration.update(calib, lm[0][:, :2] * (w, h), lm[1], lm[2], t) and persist:
                calibration.save(calib["cal"])
            cal, done = calib["cal"], calibration.progress(calib)
            if api is not None and not calib["locked"] and bar != calibration.matrix_rows(done):
                bar = calibration.matrix_rows(done)  # LED matrix as a progress bar
                api["matrix"] = ("sensors/matrix/rows", {"rows": bar})
            elif api is not None and calib["locked"] and bar != "check":
                bar, api["matrix"] = "check", ("sensors/matrix/icon", {"name": "check"})

            if paused:
                pulses = solver.stand_arm_pulses()
            else:
                track_arms(arms, lm, cal, t, dt, (w, h), args)
                target = {}
                for side, arm in arms.items():
                    target.update(solver.arm_pulses(side, *arm["angles"]))
                pulses = solver.body_safe(solver.rate_limit(pulses, target, args.max_pulse_step))
            if twin is not None:
                twin.update_servos({**STAND_PULSES, **pulses}, label="arm_mimic")
                twin.update_landmarks(*(twin_landmarks(arms) if not paused else (None, None)), legs=False)
            if api is not None:
                api["targets"] = {str(sid): p for sid, p in pulses.items()}

            for side, arm in arms.items():
                st = stats[side]
                st[arm["state"]] += 1
                st["min"], st["max"] = np.minimum(st["min"], arm["angles"]), np.maximum(st["max"], arm["angles"])
            wall = time.monotonic()
            fps, wall_prev = 0.9 * fps + 0.1 / max(wall - wall_prev, 1e-6), wall
            if headless:
                if n >= args.headless_test:
                    break
                continue

            status = f"FPS {fps:4.1f}  " + ("CALIBRATED" if calib["locked"] else f"CALIBRATING {done}/{calibration.LOCK_SAMPLES}"
                                            if done else "UNCALIBRATED (face camera, arms straight, hold still)")
            if cal.get("shoulder_px") and lm is not None:
                now_px = np.linalg.norm((lm[0][11, :2] - lm[0][12, :2]) * (w, h))
                status += f"  dist x{cal['shoulder_px'] / max(now_px, 1.0):.2f}"
            status += "  PAUSED (x resumes)" if paused else ""
            status += f"  API {api['error'] or 'on'}" if api is not None else ""
            cv2.imshow("arm mimic", draw_hud(frame, lm, arms, pulses, status))
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                break
            if key == ord("c"):  # discard everything and calibrate from scratch
                calib = calibration.new_state()
            if key == ord("x"):  # emergency: straight to the stand pose, tracking restarts from rest
                paused = not paused
                arms = {side: new_arm(side, args) for side in "LR"}
    finally:
        close_landmarker()
        cv2.destroyAllWindows()
        if twin is not None:
            twin.close()
        if calib["locked"] and persist:
            calibration.save(calib["cal"])  # keep the slow refinement
        if args.record_landmarks and record["t"]:
            np.savez_compressed(args.record_landmarks, **{k: np.array(v) for k, v in record.items()})
            print(f"[record] {len(record['t'])} frames -> {args.record_landmarks}")

    if headless:
        print(f"frames {n}  avg fps {n / max(time.monotonic() - wall_start, 1e-6):.1f}  "
              + ("calibrated" if calib["locked"] else f"UNCALIBRATED (samples {calibration.progress(calib)}/{calibration.LOCK_SAMPLES})"))
        for side, st in stats.items():
            rng = "  ".join(f"{name} {lo:7.1f}..{hi:7.1f}" for name, lo, hi in zip(("pitch", "roll", "elbow"), st["min"], st["max"]))
            print(f"{side}: {rng}  tracking {st['TRACKING']}  hold {st['HOLD']}  lost {st['LOST']}")


if __name__ == "__main__":
    main()
