"""Head camera.

If the camera stream is running (streaming/start_pi_sender.sh: one GStreamer
process feeding both the H.264 stream to the GX10 and SHARED_FRAME), frames
come from that file, since only one program can open the camera. Otherwise the
camera is opened directly like hiwonder.Camera (first device, YUYV), with the
flip from /boot/camera_setting.yaml applied either way."""
from __future__ import annotations

import base64
import os
import threading
import time

from .. import config
from ..errors import RobotError

_lock = threading.Lock()
_cap = None
_flip = None  # cv2.flip code, or None for no flip
STALE_FRAMES = 4  # the driver buffers a few frames; drop them for a fresh image
SHARED_FRAME = "/dev/shm/baymax_cam.jpg"
SHARED_MAX_AGE_S = 2.0


def _flip_setting():
    try:
        import yaml

        with open("/boot/camera_setting.yaml") as f:
            s = yaml.safe_load(f) or {}
        return s.get("flip_param") if s.get("flip") else None
    except (OSError, ImportError):
        return None


def _open():
    global _cap, _flip
    import cv2

    if _cap is None or not _cap.isOpened():
        cap = cv2.VideoCapture(-1)
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"YUYV"))
        cap.set(cv2.CAP_PROP_FPS, 30)
        if not cap.isOpened():
            raise RobotError("camera not available (is another program using it?)")
        _cap, _flip = cap, _flip_setting()
    return _cap


def _shared_frame():
    """Latest frame from the shared stream, or None if it isn't running."""
    import cv2
    import numpy as np

    for _ in range(5):
        try:
            if time.time() - os.path.getmtime(SHARED_FRAME) > SHARED_MAX_AGE_S:
                return None
            with open(SHARED_FRAME, "rb") as f:
                data = f.read()
        except OSError:
            return None
        if data.endswith(bytes([0xFF, 0xD9])):  # JPEG end marker; skip a frame caught mid-write
            frame = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
            if frame is not None:
                return frame
        time.sleep(0.05)
    return None


def _device_frame():
    with _lock:
        cap = _open()
        for _ in range(STALE_FRAMES):
            cap.grab()
        ok, frame = cap.read()
        if not ok:
            _release()
            raise RobotError("camera returned no frame")
    return frame


def snapshot_jpeg(width: int = 640, quality: int = 80) -> bytes:
    """A fresh frame as JPEG bytes, resized to `width` (keeps aspect ratio)."""
    if config.DRY_RUN:
        from ..fake import STATE

        STATE.log.append(("camera_snapshot", width))
        return b"\xff\xd8\xff\xd9"  # minimal JPEG markers
    import cv2

    global _flip
    width = max(160, min(1280, int(width)))
    frame = _shared_frame()
    if frame is None:
        frame = _device_frame()
    elif _flip is None:
        _flip = _flip_setting()
    if _flip is not None:
        frame = cv2.flip(frame, _flip)
    h, w = frame.shape[:2]
    if w != width:
        frame = cv2.resize(frame, (width, int(h * width / w)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, max(30, min(95, int(quality)))])
    if not ok:
        raise RobotError("could not encode the camera frame")
    return buf.tobytes()


def snapshot(width: int = 640, quality: int = 80) -> dict:
    """What the head camera sees now, as base64 JPEG."""
    data = snapshot_jpeg(width, quality)
    return {"mime_type": "image/jpeg", "jpeg_base64": base64.b64encode(data).decode(),
            "bytes": len(data)}


def _release() -> None:
    global _cap
    if _cap is not None:
        _cap.release()
        _cap = None


def close() -> dict:
    """Release the camera so other programs can use it."""
    with _lock:
        _release()
    return {"camera": "released"}
