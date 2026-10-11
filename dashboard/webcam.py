"""The laptop's webcam as the dashboard's camera, with arm pose tracking from arm_mvp.

In a dry run there is no robot camera, so this stands in for it: it reads the webcam on a
background thread, tracks the patient's arms on every frame, and keeps the newest annotated
JPEG and the newest pose. The dashboard reads from here exactly as it would read the robot's
camera and a pose tracker: frames from `jpeg()`, where the patient stands from `pose()`, and
the tracked joint angle a session mirrors from `pose()["angles"]`.

The tracking (MediaPipe landmarks, the 3D arm solve, joint angles) is the heavy part and has
two homes. With a `pose_url` frames are sent to the GX10's pose service
(dashboard/pose_service.py) and its answers are used; without one, or if the GX10 stops
answering, the same code (dashboard/armtrack.py) runs here when MediaPipe is installed.

Frames go as UDP datagrams, the way the robot's own video does (docs/streaming-setup.md), because
a busy venue Wi-Fi punishes TCP: it retransmits frames nobody wants any more and stalls everything
behind one lost packet. Here a lost datagram costs one frame, nothing waits for an answer, and the
frame size steps down when too few frames are being answered and back up when the link recovers.
HTTP (`transport="http"`) is the fallback for a service that has no UDP port.

OpenCV is needed either way. Without a webcam `start()` returns False and says why in
`status`; the dashboard falls back to its fake frame.
"""
from __future__ import annotations

import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

from .armtrack import EMPTY_POSE
from .pose_service import REPLY_HEADER, REPLY_MAGIC, SERVICE, encode_frame

COLORS = {"TRACKING": (80, 220, 80), "HOLD": (0, 210, 255), "LOST": (60, 60, 255)}  # BGR
REMOTE_TIMEOUT_S = 2.0
SEND_HZ = 15.0           # frames offered to the GX10 per second, at most
STALE_REPLY_S = 3.0      # no answer for this long: the GX10 is treated as down (a lossy link can be quiet a while)
TIERS = [(480, 65), (320, 60), (240, 50)]   # (frame width, JPEG quality), best first
WINDOW_S = 2.0           # how often the share of answered frames is checked
SLOW_RTT_S = 0.35        # beyond this smoothed round trip, frames go to the GX10 at half size
REMOTE_RETRY_S = 5.0     # after a failure, use the local tracker this long before asking the GX10 again


class RobotCameraSource:
    """The robot's own camera (H.264 over UDP if it can, else MJPEG: streaming/robot_camera.py), read
    like an OpenCV capture. Only one program can receive the robot's UDP video, so whoever opens
    this must release() it."""

    def __init__(self, api_url: str):
        from .armtrack import ROOT
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        from streaming.robot_camera import RobotCamera
        self.cam = RobotCamera(api_url).start()
        self.mode = self.cam.mode
        self._last = 0.0

    def read(self, wait_s: float = 3.0):
        end = time.monotonic() + wait_s
        while time.monotonic() < end:
            frame, stamp = self.cam.latest()
            if frame is not None and stamp != self._last:
                self._last = stamp
                return True, frame
            time.sleep(0.005)
        return False, None

    def release(self) -> None:
        self.cam.stop()


class Webcam:
    def __init__(self, index: str | int | None = "auto", on_pose=None, pose_url: str | None = None,
                 transport: str = "auto", robot_url: str | None = None):
        self.robot_url = robot_url        # read the robot's camera instead of a webcam
        self.label = "Robot camera" if robot_url else "Laptop webcam"
        self.transport_choice = transport   # "auto": UDP if the service has a port for it; or "http"
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
        self._session_id = bytes.fromhex(self._session[:16].ljust(16, "0"))   # 8 bytes, as the datagram header wants
        self.transport = None             # "udp" or "http", chosen when the GX10 answers its health check
        self.delivery = None              # share of recent frames the GX10 answered (UDP)
        self._udp_port = None
        self._sock = None
        self._seq = 0
        self._sent = {}                   # seq -> (monotonic send time, overlay scale)
        self._reply = None                # the newest answer: {pose, overlay, mono, seq, wall}
        self._tier = 1
        self._window = [time.monotonic(), 0, 0]   # start, frames sent, frames answered
        self._last_send = 0.0
        self._started_sending = 0.0
        self._conn = None                 # the open HTTP connection, when that is the transport
        self._rtt = 0.0                   # smoothed seconds per frame to the GX10

    # ---- lifecycle ----------------------------------------------------------------

    def start(self) -> bool:
        try:
            import cv2
        except ImportError:
            self.status = {"state": "down", "detail": "OpenCV isn't installed (pip install opencv-python)."}
            return False
        self._cv2 = cv2
        try:
            cap = self._open_robot() if self.robot_url else self._open(cv2)
        except Exception as e:
            cap, self._open_error = None, str(e) or type(e).__name__
        if cap is None:
            self.status = {"state": "down", "detail": (f"The robot's camera isn't available: {getattr(self, '_open_error', '')}"
                                                       if self.robot_url else "No webcam found, or another program is using it.")}
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

    def use_pose_service(self, url: str) -> bool:
        """Start sending frames to a pose service that turned up after startup (the GX10 was not
        reachable yet when the dashboard started). Returns whether it answered."""
        self.pose_url = url.rstrip("/")
        if not self._remote_health():
            return False
        self._remote_down_until = 0.0
        self.tracking = True
        self._set_status()
        return True

    def _open_robot(self):
        return RobotCameraSource(self.robot_url)

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
            note = " · pose on the GX10" + (" over UDP" if self.transport == "udp" else "")
        elif self._local is not None:
            note = " · pose on this computer" + (" (the GX10 isn't answering)" if self.pose_url else "")
        else:
            note = f" (no pose tracking: {self._local_error[:70]})" if self._local_error else " (no pose tracking)"
        self.status = {"state": "ok", "detail": self.label + note}

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
            import json
            with urllib.request.urlopen(f"{self.pose_url}/health", timeout=REMOTE_TIMEOUT_S) as r:
                info = json.load(r)
            if info.get("service") != SERVICE:
                return False
            self._udp_port = info.get("udp") if self.transport_choice != "http" else None
            self.transport = "udp" if self._udp_port else "http"
            return True
        except (OSError, ValueError):
            return False

    def _open_socket(self):
        from urllib.parse import urlparse
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.settimeout(0.5)
        self._addr = (urlparse(self.pose_url).hostname, self._udp_port)
        self._started_sending = time.monotonic()
        threading.Thread(target=self._listen, args=(self._sock,), name="webcam-replies", daemon=True).start()

    def _listen(self, sock):
        """Replies from the GX10, as they come: the newest one wins, older ones are discarded."""
        import json
        while self._alive and sock is self._sock:
            try:
                data, _ = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                if sock is not self._sock:
                    return
                time.sleep(0.05)   # Windows reports an ICMP "port unreachable" as a reset
                continue
            if len(data) < REPLY_HEADER.size or data[:4] != REPLY_MAGIC:
                continue
            seq = REPLY_HEADER.unpack_from(data)[1]
            sent = self._sent.pop(seq, None)
            try:
                out = json.loads(data[REPLY_HEADER.size:])
            except ValueError:
                continue
            if sent is None or (self._reply and seq <= self._reply["seq"]):
                continue
            now = time.monotonic()
            overlay, k = out["overlay"], sent[1]
            if k != 1.0 and overlay.get("shoulders"):
                overlay = {"shoulders": [[x * k, y * k] for x, y in overlay["shoulders"]],
                           "arms": {a: {**v, "pts": [[x * k, y * k] for x, y in v["pts"]]}
                                    for a, v in overlay["arms"].items()}}
            self._window[2] += 1
            self._rtt = 0.7 * self._rtt + 0.3 * (now - sent[0])
            self._reply = {"pose": out["pose"], "overlay": overlay, "mono": now, "seq": seq, "wall": time.time()}
            for old in [q for q in list(self._sent) if q < seq - 64]:
                self._sent.pop(old, None)

    def _adapt(self, now: float):
        """Frame size down when the GX10 answers too few frames, up when it answers nearly all."""
        start, sent, got = self._window
        if now - start < WINDOW_S or sent < 8:
            return
        self.delivery = round(got / sent, 2)
        if self.delivery < 0.6 and self._tier < len(TIERS) - 1:
            self._tier += 1
        elif self.delivery > 0.9 and self._tier > 0:
            self._tier -= 1
        self._window = [now, 0, 0]

    def _remote_udp(self, frame, t: float):
        """Offer the newest frame (at most SEND_HZ a second, never waiting), and return the
        newest answer that has come back, whichever frame it was for."""
        import cv2
        now = time.monotonic()
        if self._sock is None:
            self._open_socket()
        if now - self._last_send >= 1.0 / SEND_HZ:
            self._last_send = now
            self._adapt(now)
            width, quality = TIERS[self._tier]
            k = max(frame.shape[1] / width, 1.0)
            send = cv2.resize(frame, (width, int(frame.shape[0] / k)), interpolation=cv2.INTER_AREA) if k > 1 else frame
            jpeg = cv2.imencode(".jpg", send, [cv2.IMWRITE_JPEG_QUALITY, quality])[1].tobytes()
            self._seq += 1
            self._sent[self._seq] = (now, k)
            self._window[1] += 1
            for datagram in encode_frame(self._session_id, self._seq, t, jpeg):
                try:
                    self._sock.sendto(datagram, self._addr)
                except OSError:
                    break
        reply = self._reply
        if now - (reply["mono"] if reply else self._started_sending) > STALE_REPLY_S:
            self._sock.close()
            self._sock, self._reply = None, None
            raise OSError("the pose service stopped answering")
        if reply is None:
            return dict(EMPTY_POSE), {"shoulders": None, "arms": {}}
        return {**reply["pose"], "t": reply["wall"]}, reply["overlay"]

    def _remote(self, frame, t: float):
        """One frame to the GX10 over a connection kept open between frames. On a slow link
        the frame is sent at half size (the arm solve doesn't depend on the scale) and the
        overlay is scaled back up to this picture."""
        import http.client
        import json
        from urllib.parse import urlparse

        import cv2
        small = self._rtt > SLOW_RTT_S
        send = cv2.resize(frame, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA) if small else frame
        jpeg = cv2.imencode(".jpg", send, [cv2.IMWRITE_JPEG_QUALITY, 70])[1].tobytes()
        started = time.monotonic()
        try:
            if self._conn is None:
                u = urlparse(self.pose_url)
                self._conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=REMOTE_TIMEOUT_S)
            self._conn.request("POST", f"/track?session={self._session}&t={t:.3f}", jpeg, {"Content-Type": "image/jpeg"})
            resp = self._conn.getresponse()
            body = resp.read()
            if resp.status != 200:
                raise OSError(f"pose service answered {resp.status}")
        except Exception:
            self._conn = None
            raise
        self._rtt = 0.7 * self._rtt + 0.3 * (time.monotonic() - started)
        out = json.loads(body)
        k = 2.0 if small else 1.0
        overlay = out["overlay"]
        if small and overlay.get("shoulders"):
            overlay = {"shoulders": [[x * k, y * k] for x, y in overlay["shoulders"]],
                       "arms": {s: {**a, "pts": [[x * k, y * k] for x, y in a["pts"]]} for s, a in overlay["arms"].items()}}
        return out["pose"], overlay

    def _compute(self, frame, t: float):
        """(pose, overlay, where) for one frame: the GX10 if it's answering, else here."""
        if self.pose_url and time.monotonic() >= self._remote_down_until:
            try:
                pose, overlay = self._remote_udp(frame, t) if self.transport == "udp" else self._remote(frame, t)
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
                try:
                    pose, overlay, where = self._compute(frame, now - t0)
                except Exception as e:  # one bad frame must not end tracking
                    self.status = {"state": "ok", "detail": f"{self.label} (pose error: {str(e)[:60]})"}
            pose.setdefault("t", time.time())
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
