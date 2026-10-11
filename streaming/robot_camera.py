"""
The robot's camera on another machine: RobotCamera picks H.264 over UDP or the MJPEG stream.

Most code wants RobotCamera(url).start() and its latest(). Underneath:
UdpVideoLease asks the robot API for its camera as H.264 over UDP and keeps the stream alive.

The robot streams to whoever asks (POST /sensors/camera/udp) and stops by itself unless the
request is repeated, so this renews it every few seconds in the background. Receive the
video with FrameReceiver on the same port:

    from streaming.receiver import FrameReceiver
    from streaming.robot_camera import UdpVideoLease

    lease = UdpVideoLease("http://169.233.72.93:8000").start()   # token from BAYMAX_API_TOKEN
    rx = FrameReceiver(port=lease.port).start()
    ...
    lease.stop()

UdpVideoLease is standard library only, so it works in any venv; MjpegReader needs OpenCV.
"""

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger("robot_camera")


class UdpVideoLease:
    def __init__(self, url: str = None, port: int = 5000, token: str = None,
                 bitrate_kbps: int = 1500, lease_s: float = 10.0, renew_every_s: float = 3.0):
        self.url = (url or os.environ.get("BAYMAX_API_URL", "")).rstrip("/")
        if not self.url:
            raise ValueError("no robot API url (pass one or set BAYMAX_API_URL)")
        self.token = token or os.environ.get("BAYMAX_API_TOKEN")
        self.port, self.bitrate_kbps, self.lease_s = port, bitrate_kbps, lease_s
        self.renew_every_s = renew_every_s
        self.status = None
        self.error = None
        self._stop = threading.Event()
        self._thread = None

    def _post(self, path: str, body: dict) -> dict:
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"content-type": "application/json"})
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")
            try:
                detail = json.loads(detail).get("detail", detail)
            except ValueError:
                pass
            raise RuntimeError(f"robot API {path}: {detail} (HTTP {e.code})") from None

    def _request(self) -> dict:
        self.status = self._post("/sensors/camera/udp", {
            "port": self.port, "bitrate_kbps": self.bitrate_kbps, "lease_s": self.lease_s})
        return self.status

    def start(self):
        """Start the stream (raises if the robot refuses) and keep renewing it."""
        self._request()
        self._thread = threading.Thread(target=self._renew, daemon=True)
        self._thread.start()
        return self

    def _renew(self):
        while not self._stop.wait(self.renew_every_s):
            try:
                self._request()
                if self.error:
                    log.info("robot camera stream back")
                self.error = None
            except Exception as e:  # keep trying: Wi-Fi drops are usually brief
                if not self.error:
                    log.warning("renewing the robot camera stream failed: %s", e)
                self.error = str(e)

    def stop(self):
        """Stop renewing and tell the robot to stop streaming."""
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        try:
            return self._post("/sensors/camera/udp/stop", {})
        except Exception as e:
            log.warning("stopping the robot camera stream failed: %s (it stops when the lease ends)", e)


def udp_receive_possible() -> bool:
    """Whether this machine can decode the UDP stream (FrameReceiver needs GStreamer's
    gst-launch-1.0 with the H.264 decoder; the GX10 has it, most laptops don't)."""
    import shutil
    import subprocess

    if not shutil.which("gst-launch-1.0"):
        return False
    try:
        return subprocess.run(["gst-inspect-1.0", "avdec_h264"], capture_output=True,
                              timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def udp_port_free(port: int) -> bool:
    """False if another program on this machine already receives on the port (two receivers
    on one port means one of them silently gets nothing)."""
    import socket

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


class MjpegReader:
    """The robot API's MJPEG stream (GET /sensors/camera.mjpg), newest frame only. The token
    goes in a header, so it never shows up in the robot's access log. Needs OpenCV."""

    def __init__(self, url: str, token: str = None, width: int = 640):
        self.url = f"{url.rstrip('/')}/sensors/camera.mjpg?width={width}"
        self.token = token
        self.frame, self.t = None, 0.0
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def latest(self):
        return self.frame, self.t

    def _loop(self):
        import cv2
        import numpy as np

        while not self._stop.is_set():
            req = urllib.request.Request(self.url)
            if self.token:
                req.add_header("Authorization", f"Bearer {self.token}")
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    while not self._stop.is_set():
                        length = None
                        while True:  # part headers, up to the blank line
                            line = r.readline()
                            if not line:
                                raise ConnectionError("stream ended")
                            line = line.strip()
                            if line.lower().startswith(b"content-length:"):
                                length = int(line.split(b":", 1)[1])
                            elif not line and length is not None:
                                break
                        jpg = r.read(length)
                        frame = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
                        if frame is not None:
                            self.frame, self.t = frame, time.time()
            except Exception as e:  # dropped Wi-Fi, robot restarting: reconnect
                if not self._stop.is_set():
                    log.warning("robot MJPEG stream: %s; reconnecting", e)
                    self._stop.wait(1.0)

    def stop(self):
        self._stop.set()


class RobotCamera:
    """The robot's camera as newest frames, the same latest() -> (frame, arrival_time) as
    FrameReceiver. mode="auto" uses H.264 over UDP (lower bandwidth, and a lost packet costs
    a frame instead of a growing delay) when this machine can decode it and nothing else
    here is receiving it, and the API's MJPEG stream otherwise.

        cam = RobotCamera("http://169.233.72.93:8000").start()   # token from BAYMAX_API_TOKEN
        frame, t = cam.latest()
        cam.mode   # "udp" or "mjpeg"
    """

    def __init__(self, url: str = None, token: str = None, mode: str = "auto", port: int = 5000,
                 width: int = 640):
        if mode not in ("auto", "udp", "mjpeg"):
            raise ValueError("mode must be auto, udp or mjpeg")
        self.url = (url or os.environ.get("BAYMAX_API_URL", "")).rstrip("/")
        if not self.url:
            raise ValueError("no robot API url (pass one or set BAYMAX_API_URL)")
        self.token = token or os.environ.get("BAYMAX_API_TOKEN")
        self.wanted, self.port, self.width = mode, port, width
        self.mode = None
        self.lease = self._source = None

    def start(self):
        if self.wanted in ("auto", "udp"):
            reason = None
            if not udp_receive_possible():
                reason = "no GStreamer H.264 decoder on this machine"
            elif not udp_port_free(self.port):
                reason = f"another program here already receives UDP port {self.port}"
            else:
                try:
                    self.lease = UdpVideoLease(self.url, port=self.port, token=self.token).start()
                except Exception as e:  # e.g. the robot already streams to another machine
                    reason = str(e)
            if reason is None:
                from streaming.receiver import FrameReceiver

                self._source, self.mode = FrameReceiver(port=self.port).start(), "udp"
                log.info("robot camera: H.264 over UDP on port %d", self.port)
                return self
            if self.wanted == "udp":
                raise RuntimeError(f"can't use the UDP stream: {reason}")
            log.info("robot camera: MJPEG (%s)", reason)
        self._source, self.mode = MjpegReader(self.url, self.token, self.width).start(), "mjpeg"
        return self

    def latest(self):
        return self._source.latest() if self._source else (None, 0.0)

    def stop(self):
        if self._source:
            self._source.stop()
        if self.lease:
            self.lease.stop()
