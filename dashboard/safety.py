"""Safety state for the banner: one latched stop, plus conditions that come and go.

A pain word or a suspected fall freezes the robot first and only then raises the
alert, as the workout loop does. The stop stays latched (the gateway refuses every
motion call) until a therapist clears it. Hot servos, a low battery and a patient
out of frame are alerts: they show while true and clear themselves.
"""
from __future__ import annotations

import threading
import time

from workout.parts import is_pain

LATCHING = ("pain", "fall")
ALERTS = ("out_of_frame", "servo_hot", "battery_low", "temps_unknown")


class Safety:
    def __init__(self, gateway, hub):
        self.gateway, self.hub = gateway, hub
        self._lock = threading.Lock()
        self.stop: dict | None = None
        self.alerts: dict[str, dict] = {}
        self.history: list[dict] = []
        self.on_trip = []            # callbacks(kind, detail) run after the robot is frozen
        gateway.blocked = self.blocked
        self.publish()

    def blocked(self) -> str | None:
        stop = self.stop
        if stop:
            return f"Safety stop is active ({stop['title']}). A therapist must clear it first."
        return None

    def trip(self, kind: str, detail: str = "", transcript: str | None = None) -> dict:
        """Freeze the robot, then latch and announce. kind: pain | fall."""
        if kind not in LATCHING:
            raise ValueError(f"kind must be one of {LATCHING}")
        errors = self.gateway.stop_now()
        title = {"pain": "pain word heard", "fall": "fall suspected"}[kind]
        entry = {"kind": kind, "title": title, "detail": detail, "transcript": transcript,
                 "t": time.time(), "stop_errors": errors}
        with self._lock:
            first = self.stop is None
            if first:
                self.stop = entry
            self.history = (self.history + [entry])[-20:]
        self.publish()
        if first:
            for cb in self.on_trip:
                cb(kind, entry)
        return entry

    def heard(self, text: str) -> bool:
        """Anything the microphone (or a typed line) picked up. True if it tripped a stop."""
        if is_pain(text):
            self.trip("pain", "The patient said something that sounds like pain.", transcript=text)
            return True
        return False

    def clear(self, by: str = "therapist") -> None:
        with self._lock:
            if self.stop:
                self.history[-1] = {**self.history[-1], "cleared": time.time(), "cleared_by": by}
            self.stop = None
        self.publish()

    def alert(self, kind: str, active: bool, detail: str = "") -> None:
        with self._lock:
            had = self.alerts.get(kind)
            if active and (had is None or had["detail"] != detail):
                self.alerts[kind] = {"kind": kind, "detail": detail, "t": had["t"] if had else time.time()}
            elif not active and had is not None:
                del self.alerts[kind]
            else:
                return
        self.publish()

    def snapshot(self) -> dict:
        with self._lock:
            return {"stop": self.stop, "alerts": list(self.alerts.values()),
                    "history": list(self.history)}

    def publish(self) -> None:
        self.hub.publish("safety", self.snapshot())
