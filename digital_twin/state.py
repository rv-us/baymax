"""Shared twin state: what the robot was *told* to do vs. what it *reports*.

Two channels feed the twin:

``command``  whatever last drove the model - the pose estimator, a recorded pose, or the
             dashboard sliders. This is the "simulated robot".
``actual``   servo positions read back from the real robot through ``robot_api``
             (``servo/status``). This is the "digital twin" proper.

The viewer shows one of them (``view``: auto / command / actual) and can overlay the other
as a ghost, so command-vs-reality drift is visible at a glance.
"""
from __future__ import annotations

import copy
import os
import threading
import time
from typing import Dict, List, Optional

from . import mapping
from .urdf import Robot

DEFAULT_URDF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tonypi.urdf")
ACTUAL_FRESH_S = 3.0  # "auto" view follows the robot while its readback is this recent


class Channel:
    def __init__(self, name: str, servos: Optional[Dict[int, int]] = None):
        self.name = name
        self.servos: Optional[Dict[int, int]] = dict(servos) if servos else None
        self.head = {"pan": mapping.HEAD_CENTER_US, "tilt": mapping.HEAD_CENTER_US}
        self.source = "init" if servos else "none"
        self.label = ""
        self.updated = time.monotonic() if servos else None
        self.extra: dict = {}
        self.clamped: List[int] = []


class TwinState:
    def __init__(self, urdf_path: Optional[str] = None, poses_path: Optional[str] = None,
                 enforce_limits: bool = True):
        self.enforce_limits = enforce_limits  # False: ideal kinematics (tests), pulses beyond robot_api limits allowed
        self.urdf_path = urdf_path or DEFAULT_URDF
        self.robot = Robot(self.urdf_path)
        self.poses = mapping.load_poses(poses_path)
        self._cond = threading.Condition()
        self._version = 0
        self.command = Channel("command", mapping.STAND_POSE)
        self.command.label = "default_standing_pose"
        self.actual = Channel("actual")
        self.view = "auto"
        self.robot_link: dict = {"enabled": False, "connected": False, "url": None,
                                 "error": None, "last_ok_age_s": None, "hz": 0.0}
        self._solve_cache: Dict[str, tuple] = {}

    # ------------------------------------------------------------------ writers
    def _bump(self):
        self._version += 1
        self._cond.notify_all()

    def set_command(self, servos: Dict, source: str = "api", label: str = "",
                    head: Optional[Dict] = None, replace: bool = False) -> List[int]:
        """Merge (or replace with) servo pulses into the command channel. Returns clamped ids."""
        raw = mapping.normalize_servos(servos, clamp=False)
        if self.enforce_limits:
            clamped = [s for s, p in raw.items() if mapping.clamp_pulse(s, p) != p]
            pulses = {s: mapping.clamp_pulse(s, p) for s, p in raw.items()}
        else:
            clamped, pulses = [], raw
        with self._cond:
            base = mapping.STAND_POSE if replace or self.command.servos is None else self.command.servos
            merged = dict(base)
            merged.update(pulses)
            self.command.servos = merged
            if head:
                for axis in ("pan", "tilt"):
                    if axis in head and head[axis] is not None:
                        lo, hi = mapping.HEAD_LIMITS_US[axis]
                        self.command.head[axis] = max(lo, min(hi, float(head[axis])))
            self.command.source = source
            self.command.label = label
            self.command.clamped = clamped
            self.command.updated = time.monotonic()
            self._bump()
        return clamped

    def set_actual(self, servos: Dict, temps: Optional[Dict] = None, limp: Optional[List] = None,
                   source: str = "robot") -> None:
        pulses = mapping.normalize_servos(servos, clamp=False)
        with self._cond:
            base = self.actual.servos or mapping.STAND_POSE
            merged = dict(base)
            merged.update(pulses)
            self.actual.servos = merged
            self.actual.source = source
            self.actual.updated = time.monotonic()
            self.actual.extra = {"temps": {int(k): v for k, v in (temps or {}).items()},
                                 "limp": list(limp or [])}
            self._bump()

    def set_view(self, view: str) -> None:
        if view not in ("auto", "command", "actual"):
            raise ValueError("view must be auto, command or actual")
        with self._cond:
            self.view = view
            self._bump()

    def set_robot_link(self, **kw) -> None:
        with self._cond:
            self.robot_link.update(kw)
            self._bump()

    def apply_pose(self, name: str, source: str = "pose") -> List[int]:
        if name not in self.poses:
            raise KeyError(f"unknown pose {name!r}; known: {', '.join(self.poses)}")
        return self.set_command(self.poses[name], source=source, label=name, replace=True)

    # ------------------------------------------------------------------ readers
    @property
    def version(self) -> int:
        return self._version

    def wait_for_change(self, since: int, timeout: float) -> int:
        with self._cond:
            if self._version == since:
                self._cond.wait(timeout)
            return self._version

    def _age(self, ch: Channel) -> Optional[float]:
        return None if ch.updated is None else round(time.monotonic() - ch.updated, 3)

    def display_channel(self) -> str:
        if self.view in ("command", "actual"):
            return self.view
        fresh = self.actual.updated is not None and time.monotonic() - self.actual.updated < ACTUAL_FRESH_S
        return "actual" if fresh else "command"

    def _solve(self, ch: Channel) -> dict:
        key = ch.name
        stamp = (ch.updated, tuple(sorted(ch.head.items())))
        cached = self._solve_cache.get(key)
        if cached and cached[0] == stamp:
            return cached[1]
        angles = mapping.pulses_to_angles(ch.servos)
        angles.update(mapping.head_to_angles(ch.head["pan"], ch.head["tilt"]))
        links = self.robot.fk(angles)
        base = self.robot.ground_contact(links)
        solved = {"angles": {k: round(v, 3) for k, v in angles.items()}, "base": base}
        self._solve_cache[key] = (stamp, solved)
        return solved

    def _channel_json(self, ch: Channel) -> Optional[dict]:
        if ch.servos is None:
            return None
        out = {
            "servos": {str(k): v for k, v in sorted(ch.servos.items())},
            "head": dict(ch.head),
            "source": ch.source,
            "label": ch.label,
            "age_s": self._age(ch),
            "clamped": list(ch.clamped),
            **self._solve(ch),
        }
        if ch.extra:
            out["temps"] = {str(k): v for k, v in ch.extra.get("temps", {}).items()}
            out["limp"] = ch.extra.get("limp", [])
        return out

    def snapshot(self) -> dict:
        with self._cond:
            cmd, act = self._channel_json(self.command), self._channel_json(self.actual)
            delta = None
            if cmd and act:
                delta = {str(s): act["servos"][str(s)] - cmd["servos"][str(s)] for s in mapping.BODY_IDS}
            link = copy.deepcopy(self.robot_link)
            if self.actual.updated is not None:
                link["last_ok_age_s"] = self._age(self.actual)
            return {
                "version": self._version,
                "t": time.time(),
                "view": self.view,
                "display": self.display_channel(),
                "channels": {"command": cmd, "actual": act},
                "delta": delta,
                "robot": link,
            }
