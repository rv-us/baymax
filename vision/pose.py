"""MediaPipe pose landmarker (IMAGE mode), shared by scripts/pose_server.py and vision.stream.

Same model and settings as laptop_mvp/mediapose_est.py.
"""

import os
import threading
import time

import cv2

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DEFAULT_MODEL = os.path.join(ROOT, "laptop_mvp", "pose_landmarker_full.task")


class Pose:
    """One MediaPipe PoseLandmarker in IMAGE mode, as in laptop_mvp/mediapose_est.py."""

    def __init__(self, model_path: str = DEFAULT_MODEL, min_conf: float = 0.5):
        import mediapipe as mp
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision as mp_vision

        self._mp = mp
        self.model_path = os.path.abspath(model_path)
        options = mp_vision.PoseLandmarkerOptions(
            base_options=mp_python.BaseOptions(model_asset_path=self.model_path),
            running_mode=mp_vision.RunningMode.IMAGE,
            min_pose_detection_confidence=min_conf,
            min_tracking_confidence=min_conf,
            output_segmentation_masks=False,
        )
        self.landmarker = mp_vision.PoseLandmarker.create_from_options(options)
        self.lock = threading.Lock()  # the landmarker isn't thread-safe
        self.frames = 0

    def detect(self, bgr) -> dict:
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        image = self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb)
        started = time.perf_counter()
        with self.lock:
            result = self.landmarker.detect(image)
            self.frames += 1
        out = {"found": bool(result.pose_landmarks), "width": bgr.shape[1], "height": bgr.shape[0],
               "infer_ms": round((time.perf_counter() - started) * 1000, 1)}
        if result.pose_landmarks:
            out["landmarks"] = [[round(p.x, 4), round(p.y, 4), round(p.z, 4), round(p.visibility, 3)]
                                for p in result.pose_landmarks[0]]
            out["world"] = [[round(p.x, 4), round(p.y, 4), round(p.z, 4)]
                            for p in result.pose_world_landmarks[0]]
        return out
