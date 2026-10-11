"""The GX10's pose service: the dashboard sends camera frames, this sends back the patient's pose.

    python -m dashboard.pose_service                 # 0.0.0.0:9000
    python -m dashboard.pose_service --port 9100

It runs arm_mvp's whole tracker (dashboard/armtrack.py: MediaPipe landmarks, the 3D arm
solve, joint angles) so the dashboard machine only captures and draws. The robot's own
camera stream can feed it the same way, one POST per frame:

    GET  /health                       {"ok": true, "sessions": n, "frames": n}
    POST /track?session=ID&t=SECONDS   body: a JPEG  ->  {"pose": {...}, "overlay": {...}, "ms": n}

Each `session` has its own tracker, because smoothing, calibration and left/right identity
are per patient. Idle sessions are dropped after a minute. Needs: opencv-python-headless,
mediapipe, numpy, fastapi, uvicorn. There is no login: run it on a trusted network only.
"""

import argparse
import threading
import time

IDLE_S = 60.0
MAX_SESSIONS = 8


class Sessions:
    def __init__(self, make):
        self.make, self.items, self.frames = make, {}, 0
        self.lock = threading.Lock()

    def get(self, sid: str):
        now = time.monotonic()
        with self.lock:
            for key in [k for k, (_, seen, _) in self.items.items() if now - seen > IDLE_S]:
                self.items.pop(key)[0].close()
            if sid not in self.items:
                if len(self.items) >= MAX_SESSIONS:
                    oldest = min(self.items, key=lambda k: self.items[k][1])
                    self.items.pop(oldest)[0].close()
                self.items[sid] = (self.make(), now, threading.Lock())
            tracker, _, lock = self.items[sid]
            self.items[sid] = (tracker, now, lock)
            return tracker, lock


def make_app(make_tracker=None):
    import cv2
    import numpy as np
    from fastapi import FastAPI, HTTPException, Request

    if make_tracker is None:
        from .armtrack import ArmTracker as make_tracker  # noqa: N813
    app = FastAPI(title="Baymax pose service")
    sessions = Sessions(make_tracker)

    @app.get("/health")
    def health():
        return {"ok": True, "sessions": len(sessions.items), "frames": sessions.frames}

    @app.post("/track")
    async def track(request: Request, session: str = "default", t: float | None = None):
        body = await request.body()
        frame = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise HTTPException(400, "the body is not a JPEG")
        started = time.perf_counter()
        tracker, lock = sessions.get(session[:40])
        with lock:  # a tracker is one patient's state: frames are handled in order
            pose, overlay = tracker.process(frame, time.monotonic() if t is None else t)
        sessions.frames += 1
        return {"pose": pose, "overlay": overlay, "ms": round((time.perf_counter() - started) * 1000, 1)}

    return app


def main():
    ap = argparse.ArgumentParser(description="Baymax pose service (run on the GX10).")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=9000)
    args = ap.parse_args()
    import uvicorn
    print(f"Baymax pose service on http://{args.host}:{args.port}")
    uvicorn.run(make_app(), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
