"""HTTP API over robot_api.api.ENDPOINTS.

Run on the robot (one process owns the servo bus):
    python -m robot_api.server [--host 0.0.0.0] [--port 8000]

POST /<path> with a JSON object of keyword arguments, e.g.
    curl -X POST :8000/actions/walk -H 'content-type: application/json' -d '{"steps": 2}'
Endpoints without required arguments also answer GET. GET /endpoints lists all.
GET /sensors/camera.jpg returns the camera view as an image, GET /sensors/camera.mjpg as a
live MJPEG stream (browsers and cv2.VideoCapture open it).
Set BAYMAX_API_TOKEN to require "Authorization: Bearer <token>" (or ?token=<token> for clients
that can't set headers, like cv2.VideoCapture on the stream).
"""
from __future__ import annotations

import argparse
import inspect

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from . import config
from .api import ENDPOINTS
from .errors import RobotError
from .hardware import check_exclusive


def _auth(authorization: str | None = Header(default=None), token: str | None = Query(default=None)):
    if config.API_TOKEN and authorization != f"Bearer {config.API_TOKEN}" and token != config.API_TOKEN:
        raise HTTPException(401, "missing or wrong bearer token")


app = FastAPI(title="Baymax robot API", dependencies=[Depends(_auth)])


def _invoke(path: str, fn, kwargs: dict):
    try:
        inspect.signature(fn).bind(**kwargs)
    except TypeError as e:
        raise HTTPException(422, f"{path}: {e}")
    try:
        return fn(**kwargs)
    except RobotError as e:
        raise HTTPException(409, str(e))


def _route(path: str, fn):
    def post(body: dict | None = Body(default=None)):
        return _invoke(path, fn, body or {})

    app.post("/" + path, name=path, description=fn.__doc__ or "")(post)
    required = [p for p in inspect.signature(fn).parameters.values()
                if p.default is inspect.Parameter.empty]
    if not required:
        app.get("/" + path, name=path + " (GET)", description=fn.__doc__ or "")(
            lambda: _invoke(path, fn, {}))


for _path, _fn in ENDPOINTS.items():
    _route(_path, _fn)


@app.get("/sensors/camera.jpg", response_class=Response)
def camera_jpg(width: int = 640, quality: int = 80):
    """The head camera's current view as a JPEG (open it in a browser)."""
    from .sensors import camera

    try:
        return Response(camera.snapshot_jpeg(width, quality), media_type="image/jpeg")
    except RobotError as e:
        raise HTTPException(409, str(e))


@app.get("/sensors/camera.mjpg")
def camera_mjpg(width: int = 640, quality: int = 70, fps: float = 30.0):
    """The head camera as a live MJPEG stream (multipart/x-mixed-replace), up to 30 fps."""
    from .sensors import camera

    frames = camera.stream_jpegs(width, quality, fps)
    try:
        first = next(frames)  # surface "camera not available" as a 409, not a broken stream
    except RobotError as e:
        raise HTTPException(409, str(e))

    async def parts():
        # Async, so a client disconnect cancels it here and the finally closes the frame generator
        # (which restores the camera controls). Starlette never closes a sync iterator.
        jpg = first
        try:
            while jpg is not None:
                yield b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n%s\r\n" % (len(jpg), jpg)
                jpg = await run_in_threadpool(next, frames, None)
        finally:
            frames.close()

    return StreamingResponse(parts(), media_type="multipart/x-mixed-replace; boundary=frame")


@app.get("/endpoints")
def endpoints():
    return {p: {"args": list(inspect.signature(f).parameters), "doc": (f.__doc__ or "").strip()}
            for p, f in ENDPOINTS.items()}


@app.get("/health")
def health():
    return {"ok": True, "dry_run": config.DRY_RUN}


def main():
    import uvicorn

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default=config.API_HOST)
    ap.add_argument("--port", type=int, default=config.API_PORT)
    args = ap.parse_args()
    check_exclusive()
    if config.API_TOKEN is None and args.host not in ("127.0.0.1", "localhost"):
        print("warning: BAYMAX_API_TOKEN is not set; anyone on the network can move the robot")
    uvicorn.run(app, host=args.host, port=args.port, workers=1)


if __name__ == "__main__":
    main()
