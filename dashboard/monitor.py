"""Honest status for the three links (robot, camera, LLM), polled in the background.

Published as the "status" event and served at /api/status. `motion` says whether the
dashboard will start a move, with the reasons in words; it is what blocks "Play on
robot". The numbers come from sensors/health, so the limits are the robot API's own.
"""
from __future__ import annotations

import threading
import time

from robot_api.errors import RobotError

ROBOT_EVERY_S, CAMERA_EVERY_S, LLM_EVERY_S = 4.0, 6.0, 10.0
CAMERA_FRESH_S = 8.0


class Monitor:
    def __init__(self, settings, gateway, safety, coach, hub):
        self.settings, self.gateway, self.safety = settings, gateway, safety
        self.coach, self.hub = coach, hub
        self.health: dict | None = None
        self.robot = {"state": "unknown", "detail": "Checking…", "latency_ms": None}
        self.camera = {"state": "unknown", "detail": "Checking…", "last_frame": None}
        self.llm = {"state": "unknown", "detail": "Checking…"}
        self._stop = threading.Event()
        self._thread = None

    # ---- polling ------------------------------------------------------------------

    def poll_robot(self) -> None:
        try:
            self.health = self.gateway.call("sensors/health")
            self.robot = {"state": "ok", "latency_ms": self.gateway.link["latency_ms"],
                          "detail": "Fake hardware (dry run)" if self.settings.dry_run
                          else self.settings.robot_url}
        except RobotError as e:
            self.health = None
            self.robot = {"state": "down", "detail": str(e), "latency_ms": None}
        h = self.health or {}
        hottest, limit = h.get("hottest"), h.get("max_allowed_c")
        hot = bool(hottest and limit is not None and hottest["temp_c"] >= limit)
        self.safety.alert("servo_hot", hot, hot and
                          f"{_words(hottest['joint'])} is {hottest['temp_c']} °C (limit {limit} °C). "
                          "Let the servos cool before moving.")
        self.safety.alert("temps_unknown", bool(self.health) and hottest is None,
                          "The servos aren't reporting temperatures, so the robot will refuse to move.")
        low = bool(h.get("needs_charge"))
        self.safety.alert("battery_low", low, low and f"Battery is at {h.get('battery_v')} V. Recharge it.")

    def poll_camera(self) -> None:
        try:
            jpeg = self.gateway.camera_jpeg(width=160, quality=40)
            self.camera_ok(len(jpeg))
        except RobotError as e:
            self.camera = {"state": "down", "detail": str(e), "last_frame": self.camera.get("last_frame")}

    def camera_ok(self, size: int) -> None:
        self.camera = {"state": "ok", "last_frame": time.time(),
                       "detail": ("Laptop webcam" if self.gateway.webcam is not None else "Fake frame (dry run)")
                       if self.settings.dry_run else f"{size // 1024} kB frames"}

    def camera_failed(self, message: str) -> None:
        self.camera = {"state": "down", "detail": message, "last_frame": self.camera.get("last_frame")}

    def poll_llm(self) -> None:
        self.llm = self.coach.probe()

    # ---- what the browser gets ----------------------------------------------------

    def motion(self) -> dict:
        reasons = []
        blocked = self.safety.blocked()
        if blocked:
            reasons.append(blocked)
        if self.robot["state"] != "ok":
            reasons.append("The robot isn't reachable." if self.robot["state"] == "down"
                           else "Still checking the robot.")
        reasons += [a["detail"] for a in self.safety.snapshot()["alerts"]
                    if a["kind"] in ("servo_hot", "battery_low", "temps_unknown")]
        return {"ok": not reasons, "reasons": reasons}

    def snapshot(self) -> dict:
        h = self.health or {}
        return {
            "dry_run": self.settings.dry_run,
            "webcam": {"pose": self.gateway.webcam.source, "ms": self.gateway.webcam.ms,
                       "gx10": bool(self.gateway.webcam.pose_url), "transport": self.gateway.webcam.transport,
                       "delivery": self.gateway.webcam.delivery} if self.gateway.webcam is not None else None,
            "links": {"robot": self.robot, "camera": self.camera, "llm": self.llm},
            "health": {"battery_v": h.get("battery_v"), "battery_pct": h.get("approx_pct"),
                       "needs_charge": h.get("needs_charge"), "hottest": h.get("hottest"),
                       "max_allowed_c": h.get("max_allowed_c"), "cutoff_c": h.get("cutoff_c"),
                       "temps_c": h.get("temps_c")} if self.health else None,
            "motion": self.motion(),
        }

    def publish(self) -> None:
        self.hub.publish("status", self.snapshot())

    def refresh(self) -> dict:
        """Poll everything once, now (startup, tests, and after a settings change)."""
        self.poll_robot()
        self.poll_camera()
        self.poll_llm()
        self.publish()
        return self.snapshot()

    def start(self) -> None:
        if self._thread:
            return
        self._thread = threading.Thread(target=self._run, name="dashboard-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        due = {"robot": 0.0, "camera": 0.0, "llm": 0.0}
        every = {"robot": ROBOT_EVERY_S, "camera": CAMERA_EVERY_S, "llm": LLM_EVERY_S}
        while not self._stop.is_set():
            now = time.monotonic()
            # A browser showing the camera keeps the camera status fresh by itself.
            watched = (self.camera.get("last_frame") or 0) > time.time() - CAMERA_FRESH_S
            for name, poll in (("robot", self.poll_robot), ("camera", self.poll_camera), ("llm", self.poll_llm)):
                if now >= due[name] and not (name == "camera" and watched and self.camera["state"] == "ok"):
                    try:
                        poll()
                    except Exception as e:  # a poll must never kill the monitor
                        getattr(self, name).update(state="down", detail=f"status check failed: {e}")
                    due[name] = time.monotonic() + every[name]
            self.publish()
            self._stop.wait(1.0)


def _words(joint: str) -> str:
    return joint.replace("_", " ").capitalize()
