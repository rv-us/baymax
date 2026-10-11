"""H.264 video over UDP to whoever asks for it (the GX10), started and stopped by the API.

POST /sensors/camera/udp starts the same pipeline streaming/start_pi_sender.sh used,
aimed at the caller's address, and returns a lease. Calling it again renews the lease;
if nobody renews it, the stream stops by itself (so a crashed client never leaves the
camera streaming). The GX10 receives it with streaming.receiver.FrameReceiver, and
streaming.robot_camera.UdpVideoLease keeps the lease alive.

While it runs, the pipeline also writes JPEG frames to camera.SHARED_FRAME, so
/sensors/camera.jpg, the MJPEG stream and the agent's camera keep working (the camera
can only be opened once).
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import threading
import time

from .. import config
from ..errors import RobotError
from . import camera

_lock = threading.Lock()
_proc = None
_target = None        # (host, port)
_expires = 0.0
_started = 0.0
_last_error = None
_watchdog = None


def build_command(host: str, port: int, bitrate_kbps: int) -> list:
    w, h, fps = config.UDP_VIDEO_WIDTH, config.UDP_VIDEO_HEIGHT, config.UDP_VIDEO_FPS
    return [
        "gst-launch-1.0", "-q", "v4l2src", f"device={config.CAMERA_DEVICE}",
        "!", f"video/x-raw,format=YUY2,width={w},height={h},framerate={fps}/1",
        "!", "tee", "name=t",
        # Snapshots for the API's own camera endpoints (MJPEG, camera.jpg, agents).
        "t.", "!", "queue", "leaky=downstream", "max-size-buffers=2", "!", "videorate",
        "!", f"video/x-raw,framerate={config.UDP_SNAPSHOT_FPS}/1", "!", "videoconvert",
        "!", "jpegenc", "quality=85", "!", "multifilesink", f"location={camera.SHARED_FRAME}",
        # H.264 to the client.
        "t.", "!", "queue", "!", "videoconvert", "!", "video/x-raw,format=I420",
        "!", "x264enc", "tune=zerolatency", "speed-preset=ultrafast", f"bitrate={bitrate_kbps}",
        f"key-int-max={config.UDP_VIDEO_KEYFRAME}", "threads=2",
        "!", "video/x-h264,profile=baseline", "!", "h264parse",
        "!", "rtph264pay", "config-interval=1", "pt=96",
        "!", "udpsink", f"host={host}", f"port={port}", "sync=false",
    ]


def _die_with_parent():
    ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, 15)  # PR_SET_PDEATHSIG, SIGTERM


def _other_senders() -> list:
    """Camera pipelines not started here, e.g. by streaming/start_pi_sender.sh."""
    out = subprocess.run(["pgrep", "-af", "gst-launch-1.0.* v4l2src"],
                         capture_output=True, text=True).stdout
    mine = str(_proc.pid) if _proc else None
    return [line for line in out.splitlines() if line.split()[0] != mine]


def _status_locked() -> dict:
    running = _proc is not None and _proc.poll() is None
    out = {"running": running, "last_error": _last_error}
    if running:
        out.update(host=_target[0], port=_target[1], seconds=round(time.time() - _started, 1),
                   lease_left_s=round(max(0.0, _expires - time.time()), 1))
    return out


START_TRIES = 2  # the camera occasionally refuses its format on open ("not-negotiated"); a retry works


def _launch(host: str, port: int, bitrate_kbps: int):
    """Start the pipeline (or its dry-run fake). Called with _lock held."""
    if config.DRY_RUN:
        from ..fake import STATE

        STATE.log.append(("camera_udp_start", host, port, bitrate_kbps))
        return _FakeProc()
    camera.close()  # release the API's own handle so the pipeline can open the device
    camera._stream_controls(start=True)
    kwargs = {"preexec_fn": _die_with_parent} if sys.platform.startswith("linux") else {}
    return subprocess.Popen(build_command(host, port, bitrate_kbps), stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **kwargs)


def start(host: str, port: int = 5000, bitrate_kbps: int = 1500, lease_s: float = 10.0) -> dict:
    """Stream H.264 over UDP to host:port. Repeat the call within lease_s seconds to keep it
    going. Refuses if the camera is already streaming somewhere else."""
    global _proc, _target, _expires, _started, _last_error, _watchdog
    port, bitrate_kbps = int(port), max(200, min(8000, int(bitrate_kbps)))
    lease_s = max(2.0, min(120.0, float(lease_s)))
    if not host:
        raise RobotError("no host to stream to")
    with _lock:
        running = _proc is not None and _proc.poll() is None
        if running and _target == (host, port):
            _expires = time.time() + lease_s
            return _status_locked()
        if running:
            raise RobotError(f"the camera is already streaming to {_target[0]}:{_target[1]}; "
                             "stop that first (sensors/camera/udp/stop)")
        if not config.DRY_RUN:
            others = _other_senders()
            if others:
                raise RobotError("another program is streaming the camera (start_pi_sender.sh?): "
                                 + others[0][:120])
    for attempt in range(1, START_TRIES + 1):
        with _lock:
            _proc = _launch(host, port, bitrate_kbps)
            _target, _started, _expires, _last_error = (host, port), time.time(), time.time() + lease_s, None
            proc = _proc
        # A pipeline that can't start (device busy, format refused, missing plugin) exits within a second.
        try:
            proc.wait(1.0)
        except subprocess.TimeoutExpired:
            break  # still running after a second: it started
        err =proc.stderr.read().decode(errors="replace").strip() if proc.stderr else ""
        _cleanup(f"pipeline exited: {err[-300:] or 'no error output'}")
        print(f"camera_udp: start attempt {attempt} failed: {err[-300:]}", file=sys.stderr, flush=True)
        if attempt == START_TRIES:
            raise RobotError(_last_error)
        time.sleep(0.5)
    with _lock:
        if _watchdog is None or not _watchdog.is_alive():
            _watchdog = threading.Thread(target=_watch, daemon=True)
            _watchdog.start()
        return _status_locked()


def stop() -> dict:
    """Stop the UDP video stream (it also stops by itself when its lease runs out)."""
    with _lock:
        was = _proc is not None and _proc.poll() is None
    _cleanup(None)
    return {"stopped": was}


def status() -> dict:
    """Whether the UDP video stream is running, where to, and how long its lease has left."""
    with _lock:
        return _status_locked()


def _cleanup(error):
    global _proc, _target, _last_error
    with _lock:
        proc, _proc, _target = _proc, None, None
        if error:
            _last_error = error
    if proc is None:
        return
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(2)
        except subprocess.TimeoutExpired:
            proc.kill()
    if not config.DRY_RUN:
        camera._stream_controls(start=False)


def _watch():
    while True:
        time.sleep(0.5)
        with _lock:
            if _proc is None:
                return
            died = _proc.poll() is not None
            expired = time.time() > _expires
        if died:
            _cleanup("pipeline stopped unexpectedly")
            return
        if expired:
            _cleanup("lease expired (the client stopped renewing it)")
            return


class _FakeProc:
    """Dry run: a pipeline that runs until stopped."""
    stderr = None

    def __init__(self):
        self._done = False
        self.pid = -1

    def poll(self):
        return 0 if self._done else None

    def wait(self, timeout=None):
        if self._done:
            return 0
        raise subprocess.TimeoutExpired("fake", timeout)

    def terminate(self):
        self._done = True

    kill = terminate
