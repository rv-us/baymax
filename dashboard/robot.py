"""The dashboard's one door to the robot: robot_api, through LocalBackend or HttpBackend.

The browser never sees the robot's URL or bearer token. Clamping, the speed cap, the
move lock and the temperature cutoff stay in robot_api; nothing here repeats them.
This layer only adds refusals of its own: while a safety stop is latched nothing
moves, and risky action groups can't be requested from a browser.
"""
from __future__ import annotations

import base64
import inspect
import re
import threading
import time

from robot_api.errors import RobotError

from .calibration import Limiter
from .config import Settings

# Endpoints that move the robot. Refused while a safety stop is latched.
MOTION = {"servo/set", "servo/set_many", "servo/hold", "servo/head", "servo/head/center",
          "actions/run", "actions/walk", "actions/turn", "actions/side_step", "actions/arm",
          "actions/gripper", "actions/wave", "actions/stand"}
ARM_SERVOS = ("left_shoulder_pitch", "left_shoulder_roll", "left_elbow",
              "right_shoulder_pitch", "right_shoulder_roll", "right_elbow")


class Refused(RobotError):
    """A robot call that didn't happen, with the HTTP status the browser should get.
    409 = the robot (or the dashboard's safety latch) refused; 502 = unreachable."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


class Gateway:
    def __init__(self, settings: Settings, backend=None):
        self.settings = settings
        self.local = backend is None and settings.dry_run
        if backend is not None:
            self.backend = backend
        elif settings.dry_run:
            from robot_api.api import LocalBackend

            self.backend = LocalBackend()
        else:
            from robot_api.client import HttpBackend

            self.backend = HttpBackend(settings.robot_url, settings.robot_token, timeout=60)
        self.limiter = Limiter()         # the active calibration profile (app.py sets it)
        self.blocked = lambda: None      # set by Safety: returns a reason while latched
        self.webcam = None               # a dashboard.webcam.Webcam standing in for the robot's camera
        self.fake_stall: dict = {}       # dry-run only: {servo id: pulse it is stuck at}
        self._lock = threading.Lock()
        self.link = {"ok": None, "error": None, "last_ok": None, "latency_ms": None}

    # ---- calls --------------------------------------------------------------------

    def call(self, path: str, **kwargs):
        """Call a robot API endpoint. Raises Refused with the robot's own message."""
        path = path.strip("/")
        if path in MOTION:
            reason = self.blocked()
            if reason:
                raise Refused(reason, 409)
        if kwargs.get("allow_risky"):
            raise Refused("Risky action groups (falling, kicking, getting up) can't be run "
                          "from the dashboard.", 403)
        try:  # the calibration profile: clamp, offset and slow every servo command
            path, kwargs, report = self.limiter.apply(path, kwargs)
        except Refused:
            raise
        except RobotError as e:
            raise Refused(str(e), 409 if "profile" in str(e) else 422) from e
        self._check_local(path, kwargs)
        began = time.monotonic()
        try:
            result = self.backend.call(path, **kwargs)
        except Refused:
            raise
        except RobotError as e:
            status, message = classify(str(e))
            self._seen(status != 502, message if status == 502 else None, began)
            raise Refused(message, status) from e
        self._seen(True, None, began)
        if path == "servo/status":
            for s in result.get("servos", []) if self.fake_stall else []:
                if s["id"] in self.fake_stall:
                    s["position"] = self.fake_stall[s["id"]]
            result = self.limiter.read(result)
        if report and isinstance(result, dict):
            result["calibration"] = report
        return result

    def _check_local(self, path: str, kwargs: dict) -> None:
        """LocalBackend reports a wrong path or argument as a refusal; give them the
        statuses robot_api.server would."""
        if not self.local:
            return
        from robot_api.api import ENDPOINTS

        fn = ENDPOINTS.get(path)
        if fn is None:
            raise Refused(f"unknown endpoint {path!r}", 404)
        try:
            inspect.signature(fn).bind(**kwargs)
        except TypeError as e:
            raise Refused(f"{path}: {e}", 422) from e

    def _seen(self, ok: bool, error, began: float) -> None:
        with self._lock:
            self.link["ok"], self.link["error"] = ok, error
            if ok:
                self.link["last_ok"] = time.time()
                self.link["latency_ms"] = round((time.monotonic() - began) * 1000)

    def stop_now(self) -> list:
        """Freeze every servo and cancel any action group (as workout.robot.stop_now).
        Never raises: returns the messages of whatever failed."""
        errors = []
        for path in ("actions/stop", "servo/stop"):
            try:
                self.call(path)
            except RobotError as e:
                errors.append(f"{path}: {e}")
        return errors

    def camera_jpeg(self, width: int = 640, quality: int = 70) -> bytes:
        if self.webcam is not None:
            jpeg = self.webcam.jpeg(width, quality)
            if jpeg is None:
                raise Refused(self.webcam.status["detail"], 502)
            return jpeg
        frame = self.call("sensors/camera", width=width, quality=quality)
        return base64.b64decode(frame["jpeg_base64"])


_HTTP = re.compile(r"\s*\(HTTP (\d{3})\)$")


def classify(message: str) -> tuple[int, str]:
    """HttpBackend folds the status into the message; take it back out."""
    m = _HTTP.search(message)
    if m:
        status, text = int(m.group(1)), _HTTP.sub("", message)
        if status == 401:
            return 502, "The robot rejected the dashboard's token (check BAYMAX_API_TOKEN)."
        return (status if status in (404, 409, 422) else 502), text
    if "unreachable" in message:
        return 502, message
    return 409, message
