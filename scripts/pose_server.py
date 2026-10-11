"""Pose estimation server for the GX10: POST a JPEG, get MediaPipe pose landmarks back.

Loads laptop_mvp/pose_landmarker_full.task once (same model and settings as
laptop_mvp/mediapose_est.py) and serves:

    GET  /health   model path and how many frames it has served
    POST /pose     body: raw JPEG bytes -> 33 landmarks + timings

Response of /pose:
    {"found": true, "width": 640, "height": 480,
     "landmarks": [[x, y, z, visibility], ...],   # 33 points, x/y normalized 0-1
     "world":     [[x, y, z], ...],               # meters, origin at mid-hip
     "decode_ms": 1.2, "infer_ms": 18.5}

Run on the GX10 (needs mediapipe, opencv-python-headless, fastapi, uvicorn):

    python scripts/pose_server.py                    # listens on 0.0.0.0:9000
    python scripts/pose_server.py --bench            # time the model on a test frame, no server

Client: scripts/pose_stream.py streams a webcam to it.
"""

import argparse
import os
import threading
import time

import cv2
import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_MODEL = os.path.join(HERE, "..", "laptop_mvp", "pose_landmarker_full.task")


class Pose:
    """One MediaPipe PoseLandmarker in IMAGE mode, as in laptop_mvp/mediapose_est.py."""

    def __init__(self, model_path: str, min_conf: float = 0.5):
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


def make_app(pose: Pose):
    from fastapi import FastAPI, HTTPException, Request

    app = FastAPI(title="Baymax pose server")

    @app.get("/health")
    def health():
        return {"ok": True, "model": pose.model_path, "frames": pose.frames}

    @app.post("/pose")
    async def detect(request: Request):
        body = await request.body()
        started = time.perf_counter()
        frame = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise HTTPException(400, "body must be a JPEG or PNG image")
        decode_ms = round((time.perf_counter() - started) * 1000, 1)
        return {**pose.detect(frame), "decode_ms": decode_ms}

    return app


def bench(pose: Pose, image: str | None, n: int = 50):
    frame = cv2.imread(image) if image else np.full((480, 640, 3), 128, np.uint8)
    if frame is None:
        raise SystemExit(f"can't read {image}")
    pose.detect(frame)  # warm-up
    times = sorted(pose.detect(frame)["infer_ms"] for _ in range(n))
    found = pose.detect(frame)["found"]
    print(f"{n} frames {frame.shape[1]}x{frame.shape[0]}: median {times[n // 2]} ms, "
          f"p90 {times[int(n * 0.9)]} ms (~{1000 / times[n // 2]:.0f} fps); person found: {found}")


def main():
    ap = argparse.ArgumentParser(description="MediaPipe pose estimation over HTTP.")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--bench", action="store_true", help="time the model and exit")
    ap.add_argument("--image", help="test image for --bench (default: a blank frame)")
    args = ap.parse_args()

    started = time.time()
    pose = Pose(args.model)
    print(f"loaded {pose.model_path} in {time.time() - started:.1f} s")
    if args.bench:
        bench(pose, args.image)
        pose.landmarker.close()  # closing at interpreter exit prints a harmless traceback
        return

    import uvicorn

    uvicorn.run(make_app(pose), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
