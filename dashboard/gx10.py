"""The dashboard's link to the GX10, made at startup: the coach's model and the pose service.

Unless it was told where Ollama is (--ollama-url, OLLAMA_HOST) or told not to (--gx10 off), the
dashboard looks for the GX10 at asus@192.168.1.149 as soon as it starts, in the background so
the page is usable straight away, and keeps trying until it gets through (the venue network may
not be up yet):

    1. Ollama answering on the GX10's own port 11434: use it directly.
    2. Otherwise an SSH tunnel to it (the same thing as dashboard/tunnel.py, opened for you with the
       password), on a free local port.
    3. Either way, if the GX10's pose service is up (dashboard/pose_service.py) the webcam starts
       sending it frames.

The password never goes in the code or the repo: it comes from BAYMAX_GX10_PASSWORD, or the first
line of a git-ignored `.gx10_password` file in the repo folder, or is left out and ssh keys are used.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import urllib.request

from .config import ROOT, ollama_url
from .tunnel import TunnelError

log = logging.getLogger(__name__)

DEFAULT_TARGET = "asus@192.168.1.149"
PASSWORD_FILE = os.path.join(ROOT, ".gx10_password")
RETRY_S = (5, 10, 20, 30)          # between attempts, then every 30 s


def parse_target(value: str | None) -> tuple[str, str]:
    """"user@host" or just "host" as (user, host)."""
    value = (value or DEFAULT_TARGET).strip()
    user, _, host = value.rpartition("@")
    return (user or "asus"), host


def find_password(path: str = PASSWORD_FILE) -> str | None:
    env = os.environ.get("BAYMAX_GX10_PASSWORD")
    if env:
        return env
    try:
        with open(path, encoding="utf-8") as f:
            return f.readline().strip() or None
    except OSError:
        return None


def _local_port_free(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return False
    except OSError:
        return True


class Gx10Link:
    def __init__(self, settings, tunnel, monitor, hub=None, get_webcam=lambda: None):
        self.settings, self.tunnel, self.monitor, self.hub = settings, tunnel, monitor, hub
        self.get_webcam = get_webcam
        self._stop = threading.Event()
        self.state = {"state": "off", "target": None, "route": None, "pose": False, "error": None}

    # ---- what the browser and the logs see ----------------------------------------

    def _set(self, **changes) -> None:
        self.state = {**self.state, **changes}
        if self.hub:
            self.hub.publish("gx10", self.state)

    def snapshot(self) -> dict:
        return self.state

    # ---- control ------------------------------------------------------------------

    def start(self) -> None:
        s = self.settings
        self._set(state="connecting", target=f"{s.gx10_user}@{s.gx10_host}")
        threading.Thread(target=self._run, name="dashboard-gx10", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            if self.connect():
                return
            wait = RETRY_S[min(attempt, len(RETRY_S) - 1)]
            attempt += 1
            self._stop.wait(wait)

    # ---- one attempt --------------------------------------------------------------

    def _ollama_answers(self, url: str) -> bool:
        try:
            with urllib.request.urlopen(f"{url}/api/tags", timeout=2.5) as r:
                json.load(r)
            return True
        except (OSError, ValueError):
            return False

    def _pose_service(self, host: str) -> str | None:
        from .pose_service import DEFAULT_PORT, SERVICE
        url = f"http://{host}:{DEFAULT_PORT}"
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=2) as r:
                return url if json.load(r).get("service") == SERVICE else None
        except (OSError, ValueError):
            return None

    def connect(self) -> bool:
        """One try at the whole link. True when the coach's model is reachable."""
        s = self.settings
        host, user = s.gx10_host, s.gx10_user
        route, error = None, None
        direct = f"http://{host}:11434"
        if self._ollama_answers(direct):
            route, url = "direct", direct
        else:
            port = next((p for p in (11434, 11435, 11436) if _local_port_free(p)), None)
            if port is None:
                error = "No free local port for the tunnel (11434 to 11436 are all in use)."
            else:
                try:
                    self.tunnel.open(host, user, s.gx10_password, port=port)
                    route, url = "tunnel", ollama_url(f"127.0.0.1:{port}")
                except TunnelError as e:
                    error = str(e)
        pose = self._pose_service(host)
        if pose and self.get_webcam() is not None:
            self.get_webcam().use_pose_service(pose)
        if route:
            s.ollama_url, s.stub_llm = url, False
            self.monitor.poll_llm()
            self.monitor.publish()
            log.info("GX10 at %s: coach model by %s%s", host, route, ", pose service found" if pose else "")
        self._set(state="ok" if route else "down", route=route, pose=bool(pose), error=error)
        return bool(route)
