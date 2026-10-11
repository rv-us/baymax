"""
Ask the robot API for its camera as H.264 over UDP, and keep the stream alive (on the GX10).

The robot streams to whoever asks (POST /sensors/camera/udp) and stops by itself unless the
request is repeated, so this renews it every few seconds in the background. Receive the
video with FrameReceiver on the same port:

    from streaming.receiver import FrameReceiver
    from streaming.robot_camera import UdpVideoLease

    lease = UdpVideoLease("http://169.233.72.93:8000").start()   # token from BAYMAX_API_TOKEN
    rx = FrameReceiver(port=lease.port).start()
    ...
    lease.stop()

Standard library only, so it works in any venv.
"""

import json
import logging
import os
import threading
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
