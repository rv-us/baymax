"""The robot's camera as the GX10 owns it: the dashboard asks for the picture and the pose, nothing more.

    cam = Gx10Camera("http://gx10:9100", on_pose=live.apply_pose)
    cam.start()            # False, and says why in cam.status, if the service isn't there or doesn't own the camera
    cam.pose()             # the newest answer from the tracker (same shape as Webcam.pose())
    cam.jpeg(640)          # the newest picture with the tracked person and arms drawn on it

The pose service (`python -m dashboard.pose_service --camera`) receives the robot's video, tracks every frame
and answers these requests; this machine never opens the camera, so the GX10 keeps it. A request for the
newest answer waits on the GX10 until there is one newer than what we have, so the pose arrives as soon as it
exists and nothing is polled in a loop. If the GX10 stops answering the pose goes empty (nobody is "seen"),
so a session pauses rather than carrying on with an old answer, and this keeps trying.

It stands in wherever the dashboard held a `Webcam`: `tracking`, `source`, `transport`, `delivery`, `ms`,
`status`, `label`, `pose()`, `jpeg()` and `stop()` mean the same.
"""
from __future__ import annotations

import http.client
import json
import threading
import time
from urllib.parse import urlparse

from .armtrack import EMPTY_POSE
from .pose_service import SERVICE

TIMEOUT_S = 4.0
WAIT_S = 0.5            # how long the GX10 may hold a pose request open waiting for a newer answer
RETRY_S = 1.0
JPEG_CACHE_S = 0.08     # several browsers (and the status poll) share one picture this old


class Gx10Camera:
    gx10_camera = True   # app.py and feed.py tell this from a laptop Webcam by it

    def __init__(self, url: str, on_pose=None):
        self.pose_url = url.rstrip("/")
        self.on_pose = on_pose
        self.label = "Robot camera"
        self.source = "gx10"            # where the newest pose was computed
        self.transport = "http"
        self.delivery = None
        self.ms = None
        self.tracking = False           # the GX10 is answering with a camera
        self.status = {"state": "off", "detail": "Not started"}
        self._lock = threading.Lock()
        self._pose = dict(EMPTY_POSE, t=0.0)
        self._alive = False
        self._thread = None
        u = urlparse(self.pose_url)
        self._host, self._port = u.hostname, u.port or 80
        self._pic_conn = None
        self._pic = (0.0, None, None)   # when, (width, quality), jpeg
        self._pic_lock = threading.Lock()

    # ---- lifecycle ----------------------------------------------------------------

    def _get(self, conn, path: str):
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, resp.read()

    def check(self) -> tuple[bool, str]:
        """(is it a pose service that owns the camera, what to tell the person if not)."""
        try:
            conn = http.client.HTTPConnection(self._host, self._port, timeout=2.0)
            status, body = self._get(conn, "/health")
            conn.close()
            info = json.loads(body) if status == 200 else {}
        except (OSError, ValueError):
            return False, f"The GX10 pose service at {self.pose_url} isn't answering."
        if info.get("service") != SERVICE:
            return False, f"{self.pose_url} is not the Baymax pose service."
        if info.get("camera") is None:
            return False, "The GX10 pose service doesn't own the camera (start it with --camera)."
        return True, ""

    def start(self) -> bool:
        ok, why = self.check()
        if not ok:
            self.status = {"state": "down", "detail": why}
            return False
        self._alive = True
        self.tracking = True
        self.status = {"state": "ok", "detail": "Robot camera · owned and tracked by the GX10"}
        self._thread = threading.Thread(target=self._run, name="dashboard-gx10-camera", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._alive = False
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=3.0)

    # ---- the loop -----------------------------------------------------------------

    def _run(self) -> None:
        conn, seq, last_push = None, -1, 0.0
        while self._alive:
            try:
                if conn is None:
                    conn = http.client.HTTPConnection(self._host, self._port, timeout=TIMEOUT_S)
                status, body = self._get(conn, f"/camera/pose?after={seq}&wait={WAIT_S}")
                if status != 200:
                    raise OSError(f"the pose service answered {status}")
                out = json.loads(body)
                if out.get("pose") is None:
                    self._lost(out.get("error") or "waiting for the first picture from the robot's camera",
                               down=bool(out.get("error")))
                    time.sleep(0.2)
                    continue
                if out["seq"] == seq:
                    continue
                seq, self.ms = out["seq"], out.get("ms")
                pose = {**out["pose"], "t": time.time() - (out.get("age") or 0.0)}
                with self._lock:
                    self._pose = pose
                self.tracking = True
                if self.status["state"] != "ok":
                    self.status = {"state": "ok", "detail": "Robot camera · owned and tracked by the GX10"}
                now = time.monotonic()
                if self.on_pose and now - last_push > 0.2:
                    last_push = now
                    try:
                        self.on_pose(pose)
                    except Exception:
                        pass
            except (OSError, ValueError, KeyError, http.client.HTTPException) as e:
                if conn is not None:
                    conn.close()
                conn = None
                self._lost(f"The GX10 isn't answering: {e}")
                time.sleep(RETRY_S)
        if conn is not None:
            conn.close()

    def _lost(self, detail: str, down: bool = True) -> None:
        """Nothing is seen until the GX10 answers again: an old pose must never look current. `down` is
        False while the GX10 is simply starting the camera, so a session may begin and wait for it."""
        with self._lock:
            self._pose = dict(EMPTY_POSE, t=time.time())
        self.tracking = not down
        self.status = {"state": "down", "detail": detail}
        if self.on_pose:
            try:
                self.on_pose(self._pose)
            except Exception:
                pass

    # ---- what the dashboard reads -------------------------------------------------

    def pose(self) -> dict:
        with self._lock:
            return dict(self._pose)

    def jpeg(self, width: int = 640, quality: int = 70) -> bytes | None:
        with self._pic_lock:
            when, key, jpeg = self._pic
            if key == (width, quality) and time.monotonic() - when < JPEG_CACHE_S:
                return jpeg
            jpeg = None
            for attempt in (1, 2):   # the GX10 closes a connection that sat idle: the retry is on a fresh one
                try:
                    if self._pic_conn is None:
                        self._pic_conn = http.client.HTTPConnection(self._host, self._port, timeout=TIMEOUT_S)
                    status, body = self._get(self._pic_conn, f"/camera.jpg?width={width}&quality={quality}")
                    jpeg = body if status == 200 else None
                    if jpeg is None:
                        self.status = {"state": "down", "detail": _detail(body) or f"the pose service answered {status}"}
                    break
                except (OSError, http.client.HTTPException) as e:
                    self._pic_conn = None
                    if attempt == 2:
                        self.status = {"state": "down", "detail": f"The GX10 isn't answering: {e}"}
            self._pic = (time.monotonic(), (width, quality), jpeg)
            return jpeg


def _detail(body: bytes) -> str:
    try:
        return str(json.loads(body).get("detail") or "")
    except (ValueError, AttributeError):
        return ""
