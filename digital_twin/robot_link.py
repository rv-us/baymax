"""Read-only link from the real robot (``robot_api``) into the twin's ``actual`` channel.

Polls ``servo/status`` (position, temperature and torque of servos 1-16) and mirrors it into
:class:`TwinState`. It never sends motion commands, so watching the robot is always safe.
Hand servos 17/18 do not answer reads, so the twin keeps showing their last command.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

from .state import TwinState


class UrlBackend:
    """Stdlib twin of ``robot_api.client.HttpBackend`` (no httpx needed on the laptop)."""

    def __init__(self, url: str, token: Optional[str] = None, timeout: float = 10.0):
        self.url = url.rstrip("/")
        self.token = token or os.environ.get("BAYMAX_API_TOKEN")
        self.timeout = timeout

    def call(self, path: str, **kwargs):
        req = urllib.request.Request(
            f"{self.url}/{path.strip('/')}", data=json.dumps(kwargs).encode(), method="POST",
            headers={"content-type": "application/json",
                     **({"Authorization": f"Bearer {self.token}"} if self.token else {})})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"HTTP {e.code}: {e.read().decode(errors='replace')[:200]}") from e
        except (urllib.error.URLError, OSError) as e:
            raise RuntimeError(f"robot API unreachable at {self.url}: {e}") from e


def make_backend(url: Optional[str] = None, token: Optional[str] = None, timeout: float = 10.0):
    """HttpBackend when httpx is installed (same code path as the agent), else the stdlib one."""
    url = url or os.environ.get("BAYMAX_API_URL")
    if not url:
        raise ValueError("no robot API url (pass one or set BAYMAX_API_URL)")
    try:
        from robot_api.client import HttpBackend  # type: ignore
        return HttpBackend(url=url, token=token, timeout=timeout)
    except Exception:
        return UrlBackend(url, token, timeout)


def read_status(backend) -> dict:
    """One ``servo/status`` call -> {"servos": {id: pulse}, "temps": {id: C}, "limp": [..]}."""
    status = backend.call("servo/status")
    servos, temps = {}, {}
    for s in status.get("servos", []):
        if s.get("position") is not None:
            servos[int(s["id"])] = int(s["position"])
        if s.get("temp_c") is not None:
            temps[int(s["id"])] = s["temp_c"]
    return {"servos": servos, "temps": temps, "limp": status.get("limp", [])}


class RobotMirror:
    def __init__(self, state: TwinState, backend, hz: float = 4.0, label: str = ""):
        self.state = state
        self.backend = backend
        self.hz = max(0.2, hz)
        self.label = label or getattr(backend, "url", "local")
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def poll_once(self) -> bool:
        try:
            data = read_status(self.backend)
            if not data["servos"]:
                raise RuntimeError("servo/status returned no positions")
            self.state.set_actual(data["servos"], data["temps"], data["limp"])
            self.state.set_robot_link(enabled=True, connected=True, url=self.label, error=None)
            return True
        except Exception as e:  # network / robot errors must never kill the twin
            self.state.set_robot_link(enabled=True, connected=False, url=self.label, error=str(e)[:200])
            return False

    def clone(self) -> bool:
        """Read the robot once and copy its pose into the command channel too."""
        if not self.poll_once():
            return False
        self.state.set_command(self.state.actual.servos, source="robot-clone", label="cloned from robot", replace=True)
        return True

    def start(self) -> "RobotMirror":
        if self._thread and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="twin-robot-mirror", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        self.state.set_robot_link(enabled=False, connected=False)

    def _run(self) -> None:
        period, fails = 1.0 / self.hz, 0
        while not self._stop.is_set():
            t0 = time.monotonic()
            fails = 0 if self.poll_once() else min(fails + 1, 5)
            wait = max(0.0, period - (time.monotonic() - t0)) + (fails * 0.5)  # back off while unreachable
            self._stop.wait(wait)
