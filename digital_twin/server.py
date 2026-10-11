"""Dependency-free HTTP server for the twin: REST + Server-Sent Events + the 3D viewer.

    GET  /                  viewer (add ?embed=1 to hide the chrome inside a dashboard iframe)
    GET  /api/model         URDF as JSON + servo calibration table
    GET  /api/poses         recorded poses (scripts/poses.json)
    GET  /api/state         one snapshot
    GET  /api/stream        Server-Sent Events: a snapshot per change (<= 30 Hz) + 1 s keep-alive
    POST /api/command       {"servos": {id|joint: pulse}, "head": {pan, tilt}, "source", "label", "replace"}
    POST /api/pose          {"name": "t_pose"}
    POST /api/view          {"view": "auto" | "command" | "actual"}
    POST /api/robot         {"url": "http://robot:8000", "hz": 4} | {"disconnect": true}
    POST /api/clone         copy the robot's current pose into the command channel

CORS is open so a dashboard on another origin can fetch/stream directly.
"""
from __future__ import annotations

import json
import mimetypes
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import urlparse

from .api import ApiError, TwinApi
from .state import TwinState

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
MIN_STREAM_INTERVAL = 1.0 / 30.0
MAX_BODY = 1 << 20

mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/javascript", ".mjs")


def _dumps(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":"), default=lambda o: o.item() if hasattr(o, "item") else str(o)).encode()


def _make_handler(api: TwinApi):
    state = api.state

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "BaymaxTwin/1"

        def log_message(self, *args):  # quiet: the estimator owns the console
            pass

        # ---------------------------------------------------------- helpers
        def _send(self, status: int, body: bytes, ctype: str = "application/json", extra: Optional[dict] = None):
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, status: int = 200):
            self._send(status, _dumps(obj))

        def _error(self, status: int, msg: str):
            self._json({"error": msg}, status)

        def _static(self, rel: str):
            rel = rel.lstrip("/") or "index.html"
            path = os.path.normpath(os.path.join(WEB_DIR, rel))
            if not path.startswith(WEB_DIR + os.sep) or not os.path.isfile(path):
                return self._error(404, "not found")
            with open(path, "rb") as f:
                data = f.read()
            ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self._send(200, data, ctype)

        # ---------------------------------------------------------- routes
        def do_OPTIONS(self):
            self._send(204, b"", extra={"Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                                        "Access-Control-Allow-Headers": "content-type, authorization"})

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/index.html", "/embed"):  # relative asset paths need the /web/ prefix
                query = urlparse(self.path).query
                if path == "/embed":
                    query = (query + "&" if query else "") + "embed=1"
                return self._send(302, b"", extra={"Location": "/web/index.html" + ("?" + query if query else "")})
            if path.startswith("/web/"):
                return self._static(path[len("/web/"):])
            if path == "/tonypi.urdf":
                return self._send(200, open(state.urdf_path, "rb").read(), "application/xml")
            if path == "/api/model":
                return self._json(api.model())
            if path == "/api/poses":
                return self._json(api.poses())
            if path == "/api/state":
                return self._json(api.snapshot())
            if path == "/api/health":
                return self._json({"ok": True, "version": state.version})
            if path == "/api/stream":
                return self._stream()
            self._error(404, "not found")

        def do_POST(self):
            path = urlparse(self.path).path
            handlers = {"/api/command": api.command, "/api/pose": api.pose, "/api/view": api.view,
                        "/api/robot": api.connect_robot, "/api/clone": api.clone}
            fn = handlers.get(path)
            if fn is None:
                return self._error(404, "not found")
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY:
                    return self._error(413, "body too large")
                raw = self.rfile.read(length) if length else b""
                body = json.loads(raw.decode() or "{}")
                if not isinstance(body, dict):
                    return self._error(400, "body must be a JSON object")
                self._json(fn(body))
            except ApiError as e:
                self._error(e.status, str(e))
            except ValueError as e:
                self._error(400, f"bad JSON: {e}")

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            last_version, last_sent = -1, 0.0
            try:
                while not getattr(self.server, "stopping", False):
                    version = state.wait_for_change(last_version, 1.0)
                    now = time.monotonic()
                    if version == last_version and now - last_sent < 1.0:
                        continue
                    if now - last_sent < MIN_STREAM_INTERVAL:
                        time.sleep(MIN_STREAM_INTERVAL - (now - last_sent))
                    snap = state.snapshot()
                    self.wfile.write(b"data: " + _dumps(snap) + b"\n\n")
                    self.wfile.flush()
                    last_version, last_sent = snap["version"], time.monotonic()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                pass

    return Handler


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"  # on Windows SO_REUSEADDR lets two servers share one port
    stopping = False


class TwinServer:
    """Runs the twin's HTTP server on a background thread."""

    def __init__(self, state: Optional[TwinState] = None, host: str = "127.0.0.1", port: int = 8765,
                 port_fallback: int = 20):
        self.state = state or TwinState()
        loopback = host in ("127.0.0.1", "localhost", "::1")
        self.api = TwinApi(self.state, allow_robot_config=loopback)
        self.host = host
        self._httpd = self._bind(host, port, port_fallback)
        self.port = self._httpd.server_address[1]
        self._thread: Optional[threading.Thread] = None

    def _bind(self, host: str, port: int, fallback: int) -> _Server:
        handler = _make_handler(self.api)
        last_err: Optional[OSError] = None
        for p in ([port] if port == 0 else range(port, port + fallback + 1)):
            try:
                return _Server((host, p), handler)
            except OSError as e:  # port taken (e.g. a second estimator running)
                last_err = e
        raise last_err  # type: ignore[misc]

    @property
    def url(self) -> str:
        shown = "127.0.0.1" if self.host in ("127.0.0.1", "0.0.0.0", "::1", "localhost") else self.host  # not "localhost": Windows tries ::1 first, ~2 s per request
        return f"http://{shown}:{self.port}"

    def start(self) -> "TwinServer":
        if self._thread is None:
            self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.2},
                                            name="twin-http", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        if self.api.mirror:
            self.api.mirror.stop()
        self._httpd.stopping = True
        with self.state._cond:
            self.state._cond.notify_all()
        if self._thread:
            self._httpd.shutdown()
        self._httpd.server_close()  # idempotent
        self._thread = None

    def serve_forever(self) -> None:
        self.start()
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()
