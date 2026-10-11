"""The laptop's webcam as the dashboard's camera, with arm pose tracking from arm_mvp.

In a dry run there is no robot camera, so this stands in for it: it reads the webcam on a
background thread, runs arm_mvp's estimator (MediaPipe landmarks -> calibrated 3D arm ->
joint angles) on every new frame, and keeps the newest annotated JPEG and the newest pose.
The dashboard reads from here exactly as it would read the robot's camera and a pose
tracker: frames from `jpeg()`, where the patient stands from `pose()`, and the tracked
joint angle a session mirrors from `pose()["angles"]`.

OpenCV and MediaPipe are optional. Without them, or without a webcam, `start()` returns
False and says why in `status`; the dashboard falls back to its fake frame.
"""
from __future__ import annotations

import math
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARM_MVP = os.path.join(ROOT, "arm_mvp")
BODY_TO_SHOULDERS = 4.5   # a standing body is about this many shoulder widths tall
HOLD_STATES = ("TRACKING", "HOLD")


def load_estimator():
    """arm_mvp's own pieces. They import each other by bare name, so its folder goes on the path."""
    if ARM_MVP not in sys.path:
        sys.path.insert(0, ARM_MVP)
    import arm_mimic  # noqa: PLC0415  (pulls in cv2, mediapipe, solver, calibration, filters)
    import calibration
    import solver
    return arm_mimic, calibration, solver


def elevation_deg(shoulder, elbow) -> float | None:
    """How far the upper arm is lifted from hanging, in the torso frame (forward, left, up)."""
    d = [e - s for e, s in zip(elbow, shoulder)]
    n = math.sqrt(sum(c * c for c in d))
    return None if n < 1e-6 else math.degrees(math.acos(max(-1.0, min(1.0, -d[2] / n))))


class Webcam:
    def __init__(self, index: str | int | None = "auto", on_pose=None):
        self.index = index
        self.on_pose = on_pose            # called about 5 times a second with pose()
        self.status = {"state": "off", "detail": "Not started"}
        self.tracking = False             # arm tracking is running (MediaPipe loaded)
        self._lock = threading.Lock()
        self._jpeg: bytes | None = None
        self._raw = None
        self._pose = {"visible": False, "x": 0.0, "size": 0.0, "angles": {}, "arms": {}, "t": 0.0}
        self._alive = False
        self._thread: threading.Thread | None = None

    # ---- lifecycle ----------------------------------------------------------------

    def start(self) -> bool:
        try:
            import cv2
        except ImportError:
            self.status = {"state": "down", "detail": "OpenCV isn't installed (pip install opencv-python)."}
            return False
        self._cv2 = cv2
        cap = self._open(cv2)
        if cap is None:
            self.status = {"state": "down", "detail": "No webcam found, or another program is using it."}
            return False
        self._cap = cap
        self._detect, note = None, ""
        try:
            self._arm_mimic, self._calibration, self._solver = load_estimator()
            self._detect, self._close = self._arm_mimic.make_landmarker()
            self.tracking = True
        except Exception as e:  # no mediapipe, no model file: still a camera
            note = f" (no pose tracking: {str(e)[:70]})"
        self._alive = True
        self.status = {"state": "ok", "detail": "Laptop webcam" + note}
        self._thread = threading.Thread(target=self._run, name="dashboard-webcam", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._alive = False

    def _open(self, cv2):
        choices = range(4) if self.index in (None, "auto") else [int(self.index)]
        backends = [getattr(cv2, "CAP_DSHOW", 0), 0] if sys.platform == "win32" else [0]
        for i in choices:
            for backend in backends:
                cap = cv2.VideoCapture(i, backend) if backend else cv2.VideoCapture(i)
                ok, frame = cap.read() if cap.isOpened() else (False, None)
                if ok and frame is not None:
                    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
                    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
                    return cap
                cap.release()
        return None

    # ---- the loop -----------------------------------------------------------------

    def _run(self) -> None:
        cv2, np = self._cv2, __import__("numpy")
        args = self._arm_mimic.parse_args([]) if self.tracking else None
        if self.tracking:
            calib = self._calibration.new_state(self._calibration.load())
            arms = {s: self._arm_mimic.new_arm(s, args) for s in "LR"}
        t0 = t_prev = time.monotonic()
        stamp, last_push = -1, 0.0
        while self._alive:
            ok, frame = self._cap.read()
            if not ok:
                self.status = {"state": "down", "detail": "The webcam stopped giving frames."}
                time.sleep(0.5)
                continue
            now = time.monotonic()
            t, dt, t_prev = now - t0, max(now - t_prev, 1e-3), now
            h, w = frame.shape[:2]
            lm, pose = None, {"visible": False, "x": 0.0, "size": 0.0, "angles": {}, "arms": {}}
            if self.tracking:
                stamp = max(stamp + 1, int(t * 1000))
                try:
                    lm = self._detect(frame, stamp)
                    pose = self._track(lm, calib, arms, args, t, dt, (w, h), np)
                except Exception as e:  # one bad frame must not end tracking
                    self.status = {"state": "ok", "detail": f"Laptop webcam (pose error: {str(e)[:60]})"}
            pose["t"] = time.time()
            jpeg = self._annotate(frame, lm, arms if self.tracking else {}, pose)
            with self._lock:
                self._raw, self._jpeg, self._pose = frame, jpeg, pose
            if self.on_pose and now - last_push > 0.2:
                last_push = now
                try:
                    self.on_pose(pose)
                except Exception:
                    pass
        self._cap.release()
        if self.tracking:
            self._close()

    def _track(self, lm, calib, arms, args, t, dt, size, np) -> dict:
        w, h = size
        cal_mod = self._calibration
        if lm is None:
            cal_mod.update(calib, None, None, None, t)
        else:
            cal_mod.update(calib, lm[0][:, :2] * (w, h), lm[1], lm[2], t)  # in memory only; never saved
        self._arm_mimic.track_arms(arms, lm, calib["cal"], t, dt, size, args)
        if lm is None:
            return {"visible": False, "x": 0.0, "size": 0.0, "angles": {}, "arms": {s: a["state"] for s, a in arms.items()}}
        px = lm[0][:, :2] * (w, h)
        l_sh, r_sh = px[11], px[12]
        seen = lm[2][11] >= args.min_vis and lm[2][12] >= args.min_vis
        width_px = float(np.linalg.norm(l_sh - r_sh))
        mid_x = (l_sh[0] + r_sh[0]) / 2
        elev, flex = [], []
        for side, arm in arms.items():
            if arm["state"] in HOLD_STATES and arm["pts"] is not None:
                e = elevation_deg(arm["pts"][0], arm["pts"][1])
                if e is not None:
                    elev.append(e)
                flex.append(180.0 - arm["angles"][2])
        return {"visible": bool(seen), "x": float(max(-1.0, min(1.0, mid_x / w * 2 - 1))),
                "size": float(max(0.0, min(1.0, BODY_TO_SHOULDERS * width_px / h))),
                "angles": {"shoulder_elevation": sum(elev) / len(elev) if elev else None,
                           "elbow": sum(flex) / len(flex) if flex else None},
                "arms": {s: a["state"] for s, a in arms.items()},
                "calibrated": bool(calib["locked"])}

    def _annotate(self, frame, lm, arms, pose) -> bytes | None:
        """The frame with the tracked arms drawn on it, as the dashboard shows it."""
        cv2 = self._cv2
        view = frame.copy()
        if lm is not None and arms:
            h, w = view.shape[:2]
            px = lm[0][:, :2] * (w, h)
            colors = self._arm_mimic.COLORS
            cv2.line(view, tuple(map(int, px[11])), tuple(map(int, px[12])), (220, 220, 220), 1, cv2.LINE_AA)
            for side, arm in arms.items():
                pts = [tuple(map(int, px[i])) for i in self._arm_mimic.ARM_IDX[side]]
                for a, b in zip(pts, pts[1:]):
                    cv2.line(view, a, b, colors[arm["state"]], 3, cv2.LINE_AA)
                for p in pts:
                    cv2.circle(view, p, 5, (255, 255, 255), -1)
        ok, buf = cv2.imencode(".jpg", view, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return buf.tobytes() if ok else None

    # ---- what the dashboard reads -------------------------------------------------

    def pose(self) -> dict:
        with self._lock:
            return dict(self._pose)

    def jpeg(self, width: int = 640, quality: int = 70) -> bytes | None:
        with self._lock:
            jpeg, raw = self._jpeg, self._raw
        if jpeg is None or width >= 640:
            return jpeg
        cv2 = self._cv2
        arr = cv2.imdecode(__import__("numpy").frombuffer(jpeg, "uint8"), cv2.IMREAD_COLOR)
        h = int(arr.shape[0] * width / arr.shape[1])
        ok, buf = cv2.imencode(".jpg", cv2.resize(arr, (width, h)), [cv2.IMWRITE_JPEG_QUALITY, quality])
        return buf.tobytes() if ok else jpeg
