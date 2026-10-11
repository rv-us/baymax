"""SSH tunnel to Ollama on the GX10, for when it only listens on the GX10's localhost.

Same thing as:  ssh -N -L 11434:127.0.0.1:11434 asus@<gx10-ip>
Uses the system ssh client. With no password it relies on your SSH key. A password
typed into the dashboard is handed to ssh through SSH_ASKPASS: it lives in this
process's memory and the ssh child's environment only, never in a file or a log.

NOT yet tried against the real GX10 (see dashboard/README.md, known gaps).
"""
from __future__ import annotations

import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time

_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-:]{0,252}$")
_USER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.\-]{0,31}$")
_SECRET_ENV = "BAYMAX_SSH_ASKPASS_SECRET"


class TunnelError(Exception):
    pass


def manual_command(host: str = "<gx10-ip>", user: str = "asus", port: int = 11434) -> str:
    return f"ssh -f -N -L {port}:127.0.0.1:11434 {user}@{host}"


def _askpass_script() -> str:
    """A tiny program that prints the secret from the environment (ssh runs it)."""
    d = tempfile.mkdtemp(prefix="baymax-askpass-")
    if os.name == "nt":
        path = os.path.join(d, "askpass.cmd")
        body = f'@"{sys.executable}" -c "import os;print(os.environ.get(\'{_SECRET_ENV}\',\'\'))"\r\n'
    else:
        path = os.path.join(d, "askpass.sh")
        body = f'#!/bin/sh\nprintf \'%s\\n\' "${_SECRET_ENV}"\n'
    with open(path, "w") as f:
        f.write(body)
    os.chmod(path, stat.S_IRWXU)
    return path


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


class Tunnel:
    def __init__(self, hub=None):
        self.hub = hub
        self._lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.info = {"open": False, "host": None, "user": None, "port": None, "error": None}

    def status(self) -> dict:
        if self.proc and self.proc.poll() is not None and self.info["open"]:
            self.info.update(open=False, error="The tunnel closed.")
        return {**self.info, "manual": manual_command(self.info["host"] or "<gx10-ip>",
                                                       self.info["user"] or "asus")}

    def _publish(self) -> None:
        if self.hub:
            self.hub.publish("tunnel", self.status())

    def open(self, host: str, user: str = "asus", password: str | None = None,
             port: int = 11434, wait_s: float = 10.0) -> dict:
        host, user = (host or "").strip(), (user or "asus").strip()
        if not _HOST.match(host) or not _USER.match(user):
            raise TunnelError("Enter the GX10's IP address or host name, and a plain user name.")
        if not shutil.which("ssh"):
            raise TunnelError("No ssh client found on this computer. Run the tunnel by hand: "
                              + manual_command(host, user, port))
        with self._lock:
            self.close(publish=False)
            if _port_open(port):
                raise TunnelError(f"Port {port} on this computer is already in use. A tunnel "
                                  "(or a local Ollama) may already be running.")
            cmd = ["ssh", "-N", "-L", f"{port}:127.0.0.1:11434", "-o", "ExitOnForwardFailure=yes",
                   "-o", "StrictHostKeyChecking=accept-new", "-o", "ServerAliveInterval=15",
                   "-o", "ConnectTimeout=6"]
            env = dict(os.environ)
            if password:
                env.update({"SSH_ASKPASS": _askpass_script(), "SSH_ASKPASS_REQUIRE": "force",
                            "DISPLAY": env.get("DISPLAY", ":0"), _SECRET_ENV: password})
                cmd += ["-o", "PreferredAuthentications=password,keyboard-interactive",
                        "-o", "NumberOfPasswordPrompts=1"]
            else:
                cmd += ["-o", "BatchMode=yes"]
            self.proc = subprocess.Popen(cmd + [f"{user}@{host}"], env=env, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
            deadline = time.monotonic() + wait_s
            while time.monotonic() < deadline:
                if self.proc.poll() is not None:
                    err = (self.proc.stderr.read() or "").strip().splitlines()
                    self.proc = None
                    self.info.update(open=False, host=host, user=user, port=port,
                                     error=err[-1] if err else "ssh exited without saying why.")
                    self._publish()
                    raise TunnelError(self.info["error"])
                if _port_open(port):
                    self.info.update(open=True, host=host, user=user, port=port, error=None)
                    self._publish()
                    return self.status()
                time.sleep(0.2)
            self.close(publish=False)
            self.info.update(host=host, user=user, port=port,
                             error=f"No connection to {host} after {wait_s:g} s.")
            self._publish()
            raise TunnelError(self.info["error"])

    def close(self, publish: bool = True) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None
        self.info.update(open=False, error=None)
        if publish:
            self._publish()
