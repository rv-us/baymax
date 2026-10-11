"""Dashboard settings: where the robot and the LLM are, and where files live."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
DEFAULT_MODEL = "qwen3.6:35b"


def ollama_url(value: str | None) -> str:
    """OLLAMA_HOST style values ("host", "host:11434", "http://host:11434") as a URL."""
    value = (value or "").strip().rstrip("/") or "127.0.0.1:11434"
    if "://" not in value:
        value = "http://" + value
    if value.count(":") == 1:  # scheme only, no port
        value += ":11434"
    return value


@dataclass
class Settings:
    dry_run: bool = False
    robot_url: str = field(default_factory=lambda: os.environ.get("BAYMAX_API_URL", "http://169.233.72.93:8000"))
    robot_token: str | None = field(default_factory=lambda: os.environ.get("BAYMAX_API_TOKEN") or None)
    ollama_url: str = field(default_factory=lambda: ollama_url(os.environ.get("OLLAMA_HOST")))
    model: str = field(default_factory=lambda: os.environ.get("BAYMAX_COACH_MODEL", DEFAULT_MODEL))
    coach_timeout_s: float = field(default_factory=lambda: float(os.environ.get("BAYMAX_COACH_TIMEOUT", "60")))
    stub_llm: bool = False          # dry-run without --ollama-url: a canned coach, labelled as such
    host: str = "127.0.0.1"
    port: int = 8080
    sessions_dir: str = os.path.join(ROOT, "sessions")
    animations_dir: str = os.path.join(ROOT, "animations")
    plans_dir: str = os.path.join(ROOT, "workout", "plans")
    calibration_dir: str = os.path.join(ROOT, "calibration")
    web_dir: str = os.path.join(ROOT, "dashboard", "web", "dist")
    monitor: bool = True            # background polling of robot, camera and LLM (off in tests)
