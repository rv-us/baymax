"""HTTP backend: same call(path, **kwargs) interface as api.LocalBackend."""
from __future__ import annotations

import os

import httpx

from .errors import RobotError


class HttpBackend:
    def __init__(self, url: str | None = None, token: str | None = None, timeout: float = 180):
        self.url = (url or os.environ.get("BAYMAX_API_URL", "http://169.233.72.93:8000")).rstrip("/")
        token = token or os.environ.get("BAYMAX_API_TOKEN")
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._http = httpx.Client(base_url=self.url, headers=headers, timeout=timeout)

    def call(self, path: str, **kwargs):
        try:
            resp = self._http.post("/" + path.strip("/"), json=kwargs)
        except httpx.HTTPError as e:
            raise RobotError(f"robot API unreachable at {self.url}: {e}") from e
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail", resp.text)
            except ValueError:
                detail = resp.text
            raise RobotError(f"{detail} (HTTP {resp.status_code})")
        return resp.json()
