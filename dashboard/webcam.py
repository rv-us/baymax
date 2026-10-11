"""The laptop's webcam as the dashboard's camera, with arm pose tracking from arm_mvp.

In a dry run there is no robot camera, so this stands in for it: it reads the webcam on a
background thread, tracks the patient's arms on every frame, and keeps the newest annotated
JPEG and the newest pose. The dashboard reads from here exactly as it would read the robot's
camera and a pose tracker: frames from `jpeg()`, where the patient stands from `pose()`, and
the tracked joint angle a session mirrors from `pose()["angles"]`.

The tracking (MediaPipe landmarks, the 3D arm solve, joint angles) is the heavy part and has
two homes. With a `pose_url` each frame is sent to the GX10's pose service
(dashboard/pose_service.py) and its answer is used; without one, or if the GX10 stops
answering, the same code (dashboard/armtrack.py) runs here when MediaPipe is installed.

OpenCV is needed either way. Without a webcam `start()` returns False and says why in
`status`; the dashboard falls back to its fake frame.
"""
from __future__ import annotations

import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

from .armtrack import EMPTY_POSE

COLORS = {"TRACKING": (80, 220, 80), "HOLD": (0, 210, 255), "LOST": (60, 60, 255)}  # BGR
REMOTE_TIMEOUT_S = 2.0
REMOTE_RETRY_S = 5.0     # after a failure, use the local tracker this long before asking the GX10 again


class Webcam:
    def __init__(self, index: str | int | None = "auto", on_pose=None, pose_url: str | None = None):
        self.index = index
        self.on_pose = on_pose            # called about 5 times a second with pose()
        self.pose_url = pose_url.rstrip("/") if pose_url else None
        self.status = {"state": "off", "detail": "Not started"}
        self.tracking = False             # arm tracking is available (on the GX10 or here)
        self.source = "none"              # where the newest pose was computed: "gx10", "local" or "none"
        self.ms = None                    # how long the newest pose took, start to finish
        self._lock = threading.Lock()
        self._jpeg: bytes | None = None
        self._pose = dict(EMPTY_POSE, t=0.0)
        self._alive = False
        self._local = None                # an ArmTracker, made when first needed
        self._local_error = ""
        self._remote_down_until = 0.0
        self._session = uuid.uuid4().hex[:12]

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
        remote = self._remote_health() if self.pose_url else False
        if self.pose_url and not remote:
            print(f"webcam: the pose service at {self.pose_url} didn't answer; tracking here instead.")
        if not remote:
            self._get_local()
        self.tracking = remote or self._local is not None
        self._set_status()
        self._alive = True
        threading.Thread(target=self._run, name="dashboard-webcam", daemon=True).start()
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

    def _set_status(self) -> None:
        if self.source == "gx10" or (self.pose_url and self.source == "none" and not self._local):
            note = " · pose on the GX10"
        elif self._local is not None:
            note = " · pose on this computer" + (" (the GX10 isn't answering)" if self.pose_url else "")
        else:
            note = f" (no pose tracking: {self._local_error[:70]})" if self._local_error else " (no pose tracking)"
        self.status = {"state": "ok", "detail": "Laptop webcam" + note}

    # ---- where the pose is computed -----------------------------------------------

    def _get_local(self):
        if self._local is None and not self._local_error:
            try:
                from .armtrack import ArmTracker
                self._local = ArmTracker()
            except Exception as e:  # no mediapipe, no model file: still a camera
                self._local_error = str(e) or type(e).__name__
        return self._local

    def _remote_health(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.pose_url}/health", timeout=REMOTE_TIMEOUT_S) as r:
                return r.status == 200
        except (OSError, ValueError):
            return False

    def _remote(self, jpeg: bytes, t: float):
        import json
        req = urllib.request.Request(f"{self.pose_url}/track?session={self._session}&t={t:.3f}", data=jpeg,
                                     headers={"Content-Type": "image/jpeg"}, method="POST")
        with urllib.request.urlopen(req, timeout=REMOTE_TIMEOUT_S) as r:
            out = json.loads(r.read())
        return out["pose"], out["overlay"]

    def _compute(self, frame, jpeg: bytes, t: float):
        """(pose, overlay, where) for one frame: the GX10 if it's answering, else here."""
        if self.pose_url and time.monotonic() >= self._remote_down_until:
            try:
                pose, overlay = self._remote(jpeg, t)
                return pose, overlay, "gx10"
            except (OSError, ValueError, KeyError) as e:
                self._remote_down_until = time.monotonic() + REMOTE_RETRY_S
                self._get_local()
        local = self._get_local()
        if local is None:
            return dict(EMPTY_POSE), {"shoulders": None, "arms": {}}, "none"
        pose, overlay = local.process(frame, t)
        return pose, overlay, "local"

    # ---- the loop -----------------------------------------------------------------

    def _run(self) -> None:
        cv2 = self._cv2
        t0, last_push = time.monotonic(), 0.0
        while self._alive:
            ok, frame = self._cap.read()
            if not ok:
                self.status = {"state": "down", "detail": "The webcam stopped giving frames."}
                time.sleep(0.5)
                continue
            now = time.monotonic()
            pose, overlay, where = dict(EMPTY_POSE), {"shoulders": None, "arms": {}}, "none"
            if self.tracking:
                ok_enc, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
                try:
                    pose, overlay, where = self._compute(frame, buf.tobytes(), now - t0)
                except Exception as e:  # one bad frame must not end tracking
                    self.status = {"state": "ok", "detail": f"Laptop webcam (pose error: {str(e)[:60]})"}
            pose["t"] = time.time()
            self.ms = round((time.monotonic() - now) * 1000)
            if where != self.source:
                self.source = where
                self._set_status()
            jpeg = self._annotate(frame, overlay)
            with self._lock:
                self._jpeg, self._pose = jpeg, pose
            if self.on_pose and now - last_push > 0.2:
                last_push = now
                try:
                    self.on_pose(pose)
                except Exception:
                    pass
        self._cap.release()
        if self._local is not None:
            self._local.close()

    def _annotate(self, frame, overlay) -> bytes | None:
        """The frame with the tracked arms drawn on it, as the dashboard shows it."""
        cv2 = self._cv2
        view = frame.copy()
        if overlay.get("shoulders"):
            a, b = (tuple(int(v) for v in p) for p in overlay["shoulders"])
            cv2.line(view, a, b, (220, 220, 220), 1, cv2.LINE_AA)
            for arm in overlay["arms"].values():
                pts = [tuple(int(v) for v in p) for p in arm["pts"]]
                for p, q in zip(pts, pts[1:]):
                    cv2.line(view, p, q, COLORS.get(arm["state"], COLORS["LOST"]), 3, cv2.LINE_AA)
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
            jpeg = self._jpeg
        if jpeg is None or width >= 640:
            return jpeg
        import numpy as np
        cv2 = self._cv2
        arr = cv2.imdecode(np.frombuffer(jpeg, "uint8"), cv2.IMREAD_COLOR)
        h = int(arr.shape[0] * width / arr.shape[1])
        ok, buf = cv2.imencode(".jpg", cv2.resize(arr, (width, h)), [cv2.IMWRITE_JPEG_QUALITY, quality])
        return buf.tobytes() if ok else jpeg
