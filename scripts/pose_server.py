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

With --stream it also reads the robot's video stream (streaming/receiver.py) and runs
the model on every frame it can, adding:

    GET  /latest   the newest pose from the robot's camera, with joint angles
                   (vision/angles.py) and whether the upper/full body is in view
    GET  /stats    frames per second, model time, and frame-to-pose latency

Only one program can receive the video stream, so with --stream other programs
(the workout loop, the digital twin) get poses from /latest instead.

Run on the GX10 (needs mediapipe, opencv-python-headless, fastapi, uvicorn):

    python scripts/pose_server.py                    # listens on 0.0.0.0:9000
    python scripts/pose_server.py --stream --robot-url http://<robot>:8000
                                                     # ...asks the robot for its video, runs on it
    python scripts/pose_server.py --bench            # time the model on a test frame, no server

Client: scripts/pose_stream.py streams a webcam to it.
"""

import argparse
import os
import sys
import time

import cv2
import numpy as np

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, ROOT)  # run as a script: make vision/ and streaming/ importable

from vision.pose import DEFAULT_MODEL, Pose  # noqa: E402


def make_app(pose: Pose, stream=None):
    from fastapi import FastAPI, HTTPException, Request

    app = FastAPI(title="Baymax pose server")

    @app.get("/health")
    def health():
        return {"ok": True, "model": pose.model_path, "frames": pose.frames,
                "stream": stream.stats() if stream else None}

    @app.get("/latest")
    def latest():
        if stream is None:
            raise HTTPException(404, "not reading the robot's video; start with --stream")
        result = stream.latest()
        return result if result is not None else {"found": False, "waiting": True}

    @app.get("/stats")
    def stats():
        if stream is None:
            raise HTTPException(404, "not reading the robot's video; start with --stream")
        return stream.stats()

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
    ap.add_argument("--stream", action="store_true",
                    help="also run on the robot's video stream (GET /latest, GET /stats)")
    ap.add_argument("--video-port", type=int, default=5000, help="robot video stream UDP port")
    ap.add_argument("--robot-url", default=os.environ.get("BAYMAX_API_URL"),
                    help="with --stream: ask this robot API for the video (POST /sensors/camera/udp) "
                         "and keep it going; without it, start the stream yourself "
                         "(streaming/start_pi_sender.sh). Default: BAYMAX_API_URL")
    args = ap.parse_args()

    started = time.time()
    pose = Pose(args.model)
    print(f"loaded {pose.model_path} in {time.time() - started:.1f} s")
    if args.bench:
        bench(pose, args.image)
        pose.landmarker.close()  # closing at interpreter exit prints a harmless traceback
        return

    stream = None
    if args.stream:
        from streaming.receiver import FrameReceiver
        from vision.stream import PoseStream

        if args.robot_url:
            from streaming.robot_camera import UdpVideoLease

            UdpVideoLease(args.robot_url, port=args.video_port).start()
            print(f"asked {args.robot_url} for its camera on UDP {args.video_port}")
        stream = PoseStream(FrameReceiver(port=args.video_port).start(), pose).start()
        print(f"reading the robot's video on UDP {args.video_port}; poses at /latest")

    import uvicorn

    uvicorn.run(make_app(pose, stream), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
