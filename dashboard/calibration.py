"""Calibration profiles: per-servo limits, offsets and speed caps, enforced in the gateway.

A profile tunes the real robot against the digital twin. For each bus servo it holds

    min, max     the range the dashboard may command, in twin pulses. It can only be
                 narrower than robot_api's own limits, never wider.
    offset       pulses added to a command on its way to the robot, and taken back off
                 its readback, so "500" means the same pose on the twin and the robot.
    max_speed    the fastest the servo may be asked to travel, in pulses per second
                 (robot_api's own cap is 1000).

Limiter.apply() rewrites every servo command the gateway sends, so no animation, jog
or demo can leave the active profile's range. Factory action groups run inside the
robot's controller and cannot be clamped; a profile can refuse them instead
(`allow_action_groups: false`).

Profiles are JSON files in calibration/. The active one is named in calibration/_active.json.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

from robot_api.config import MIN_MOVE_MS, MS_PER_PULSE
from robot_api.errors import RobotError
from robot_api.servo.joints import JOINTS, STAND_POSE, joint_name, resolve
from robot_api.servo.limits import LIMITS

FACTORY = "factory"
MAX_OFFSET = 100
SPEED_CAP = int(1000 / MS_PER_PULSE)   # pulses per second: robot_api's own ceiling
MIN_SPEED = 50
SERVO_IDS = tuple(range(1, 19))
ACTION_PATHS = {"actions/run", "actions/walk", "actions/turn", "actions/side_step", "actions/wave",
                "actions/stand", "actions/rest"}
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")


class CalibrationError(ValueError):
    pass


def factory_profile() -> dict:
    """robot_api's own limits, no offsets, full speed. Always available, never saved."""
    return {"id": FACTORY, "name": "Factory limits", "notes": "The robot API's own limits. Read only.",
            "allow_action_groups": True, "readonly": True, "updated": None,
            "servos": {joint_name(i): {"min": LIMITS[i][0], "max": LIMITS[i][1], "offset": 0,
                                       "max_speed": SPEED_CAP} for i in SERVO_IDS}}


def _whole(value, lo: int, hi: int, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
        raise CalibrationError(f"{what} must be a whole number")
    if not lo <= int(value) <= hi:
        raise CalibrationError(f"{what} must be from {lo} to {hi}, got {int(value)}")
    return int(value)


def check_profile(data: dict) -> dict:
    """A clean profile, or CalibrationError saying what is wrong."""
    if not isinstance(data, dict):
        raise CalibrationError("a profile must be a JSON object")
    name = str(data.get("name") or "").strip()[:60]
    if not name:
        raise CalibrationError("the profile needs a name")
    given = data.get("servos") or {}
    if not isinstance(given, dict):
        raise CalibrationError("servos must be an object of joint name to settings")
    servos = factory_profile()["servos"]
    for key, row in given.items():
        try:
            sid = resolve(key)
        except RobotError as e:
            raise CalibrationError(str(e)) from e
        joint, (lo, hi) = joint_name(sid), LIMITS[sid]
        if not isinstance(row, dict):
            raise CalibrationError(f"{joint}: settings must be an object")
        low = _whole(row.get("min", lo), lo, hi, f"{joint} min")
        high = _whole(row.get("max", hi), lo, hi, f"{joint} max")
        if high - low < 10:
            raise CalibrationError(f"{joint}: max must be at least 10 pulses above min")
        servos[joint] = {"min": low, "max": high,
                         "offset": _whole(row.get("offset", 0), -MAX_OFFSET, MAX_OFFSET, f"{joint} offset"),
                         "max_speed": _whole(row.get("max_speed", SPEED_CAP), MIN_SPEED, SPEED_CAP,
                                             f"{joint} speed limit")}
    return {"name": name, "notes": str(data.get("notes") or "")[:400],
            "allow_action_groups": bool(data.get("allow_action_groups", True)), "servos": servos}


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:48]
    return s or "profile"


class Profiles:
    """The saved profiles and which one is active."""

    def __init__(self, directory: str):
        self.dir = directory
        self._lock = threading.Lock()

    def _path(self, pid: str) -> str:
        if not _ID.match(pid) or pid == FACTORY:
            raise CalibrationError("profile ids use lower-case letters, digits, - and _")
        return os.path.join(self.dir, pid + ".json")

    def list(self) -> list:
        rows = [factory_profile()]
        try:
            names = sorted(f[:-5] for f in os.listdir(self.dir) if f.endswith(".json") and _ID.match(f[:-5]))
        except FileNotFoundError:
            names = []
        for pid in names:
            try:
                rows.append(self.get(pid))
            except (CalibrationError, ValueError, OSError):
                rows.append({"id": pid, "name": pid, "unreadable": True})
        return rows

    def get(self, pid: str) -> dict:
        if pid == FACTORY:
            return factory_profile()
        try:
            with open(self._path(pid), encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            raise CalibrationError(f"no calibration profile named {pid!r}") from None
        return {"id": pid, **check_profile(raw), "updated": raw.get("updated"), "readonly": False}

    def save(self, data: dict, pid: str | None = None) -> dict:
        clean = check_profile(data)
        pid = pid or slug(clean["name"])
        if pid == FACTORY:
            raise CalibrationError("the factory profile can't be changed; save under another name")
        clean["updated"] = time.time()
        os.makedirs(self.dir, exist_ok=True)
        with self._lock:
            tmp = self._path(pid) + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(clean, f, indent=2)
            os.replace(tmp, self._path(pid))
        return {"id": pid, **clean, "readonly": False}

    def delete(self, pid: str) -> None:
        try:
            os.remove(self._path(pid))
        except FileNotFoundError:
            raise CalibrationError(f"no calibration profile named {pid!r}") from None
        if self.active_id() == pid:
            self.set_active(FACTORY)

    def active_id(self) -> str:
        try:
            with open(os.path.join(self.dir, "_active.json"), encoding="utf-8") as f:
                pid = json.load(f).get("id")
        except (OSError, ValueError):
            return FACTORY
        return pid if pid == FACTORY or (isinstance(pid, str) and os.path.isfile(self._path(pid))) else FACTORY

    def set_active(self, pid: str) -> dict:
        profile = self.get(pid)  # raises if it doesn't exist or doesn't validate
        os.makedirs(self.dir, exist_ok=True)
        with self._lock, open(os.path.join(self.dir, "_active.json"), "w", encoding="utf-8") as f:
            json.dump({"id": pid}, f)
        return profile

    def active(self) -> dict:
        try:
            return self.get(self.active_id())
        except CalibrationError:
            return factory_profile()


class Limiter:
    """Applies the active profile to outgoing servo commands and incoming readback."""

    def __init__(self, profile: dict | None = None):
        self.positions: dict[int, int] = {}   # last known raw pulse per servo (readback or command)
        self.commanded: dict[int, dict] = {}  # last command per servo, in twin pulses
        self.use(profile or factory_profile())

    def use(self, profile: dict) -> None:
        self.profile = profile
        self.rows = {JOINTS[j]: row for j, row in profile["servos"].items()}

    # ---- outgoing -----------------------------------------------------------------

    def clamp(self, servo, pulse) -> tuple[int, int]:
        """(servo id, twin pulse inside the profile's range)."""
        sid = resolve(servo)
        row = self.rows[sid]
        return sid, max(row["min"], min(row["max"], int(round(float(pulse)))))

    def clamp_pose(self, servos: dict) -> dict:
        """Joint-name pose with every pulse inside the profile (for the twin preview)."""
        return {joint_name(sid): p for sid, p in (self.clamp(k, v) for k, v in servos.items())}

    def targets(self, targets: dict, ms) -> tuple[dict, int, dict]:
        """Twin-pulse targets -> (raw targets for the robot, move time, report)."""
        if not isinstance(targets, dict) or not targets:
            raise RobotError("no servos given")
        raw, clamped, need_ms = {}, {}, 0.0
        for key, pulse in targets.items():
            if isinstance(pulse, bool) or not isinstance(pulse, (int, float)):
                raise RobotError(f"{key}: pulse must be a number")
            sid, applied = self.clamp(key, pulse)
            row = self.rows[sid]
            if applied != int(round(float(pulse))):
                clamped[joint_name(sid)] = {"requested": int(round(float(pulse))), "applied": applied,
                                            "limits": [row["min"], row["max"]]}
            lo, hi = LIMITS[sid]
            out = max(lo, min(hi, applied + row["offset"]))  # never past robot_api's own limits
            start = self.positions.get(sid)
            travel = abs(out - start) if start is not None else row["max"] - row["min"]
            need_ms = max(need_ms, travel / row["max_speed"] * 1000)
            raw[sid] = out
            self.commanded[sid] = {"pulse": applied, "t": time.time()}
        asked = int(ms) if isinstance(ms, (int, float)) and not isinstance(ms, bool) else 0
        move_ms = max(MIN_MOVE_MS, asked, int(round(need_ms)))
        report = {"profile": self.profile["id"]}
        if clamped:
            report["clamped"] = clamped
        if move_ms > max(MIN_MOVE_MS, asked):
            report["slowed_ms"] = move_ms
        return raw, move_ms, report

    def apply(self, path: str, kwargs: dict) -> tuple[str, dict, dict | None]:
        """Rewrite one robot call under the profile. Returns (path, kwargs, report)."""
        if path in ACTION_PATHS and not self.profile.get("allow_action_groups", True):
            raise RobotError(f"The calibration profile \"{self.profile['name']}\" doesn't allow factory "
                             "action groups, because their motion can't be limited. Switch profile in Calibrate.")
        if path == "servo/set":
            raw, ms, report = self.targets({kwargs.get("servo"): kwargs.get("pulse")}, kwargs.get("ms"))
            sid, pulse = next(iter(raw.items()))
            self._sent(raw)
            return path, {"servo": sid, "pulse": pulse, "ms": ms}, report
        if path == "servo/set_many":
            raw, ms, report = self.targets(kwargs.get("targets"), kwargs.get("ms"))
            self._sent(raw)
            return path, {**kwargs, "targets": {str(k): v for k, v in raw.items()}, "ms": ms}, report
        if path == "actions/arm":
            from robot_api.actions.arms import ARM_POSITIONS, _sides

            pos = str(kwargs.get("position", "")).strip().lower()
            if pos not in ("down", "forward", "up"):
                raise RobotError(f"position must be down, forward or up, got {kwargs.get('position')!r}")
            wanted = {ARM_POSITIONS[s][0]: ARM_POSITIONS[s][1][pos] for s in _sides(str(kwargs.get("side", "")))}
            raw, ms, report = self.targets(wanted, None)
            self._sent(raw)
            return "servo/set_many", {"targets": {str(k): v for k, v in raw.items()}, "ms": ms}, report
        return path, kwargs, None

    def _sent(self, raw: dict) -> None:
        self.positions.update(raw)

    # ---- incoming -----------------------------------------------------------------

    def read(self, status: dict) -> dict:
        """servo/status with positions back in twin pulses (`raw_position` keeps the robot's)."""
        for s in status.get("servos", []):
            row = self.rows.get(s.get("id"))
            if row is None or s.get("position") is None:
                continue
            self.positions[s["id"]] = s["position"]
            s["raw_position"] = s["position"]
            s["position"] = s["position"] - row["offset"]
        return status

    def table(self, status: dict | None, twin_command: dict | None = None) -> list:
        """One row per servo for the debug screen: commanded against actual, and health."""
        by_id = {s["id"]: s for s in (status or {}).get("servos", [])}
        rows = []
        for sid in SERVO_IDS:
            joint, cal, s = joint_name(sid), self.rows[sid], by_id.get(sid, {})
            cmd = self.commanded.get(sid)
            commanded = cmd["pulse"] if cmd else (twin_command or {}).get(str(sid))
            actual = s.get("position")
            rows.append({
                "id": sid, "joint": joint, "group": "arm" if "shoulder" in joint or "elbow" in joint
                else "hand" if "hand" in joint else "leg",
                "commanded": commanded, "commanded_age_s": round(time.time() - cmd["t"], 1) if cmd else None,
                "actual": actual, "raw": s.get("raw_position"),
                "error": None if commanded is None or actual is None else actual - commanded,
                "temp_c": s.get("temp_c"), "vin_v": s.get("vin_v"), "torque_on": s.get("torque_on"),
                "load": None,  # this bus API doesn't report load; position error is the stall signal
                "note": s.get("note"), "stand": STAND_POSE[sid], "hard_limits": list(LIMITS[sid]), **cal})
        return rows

    def narrowed(self) -> list:
        """Joints whose range the profile narrows below robot_api's own."""
        return [joint_name(sid) for sid, row in self.rows.items()
                if (row["min"], row["max"]) != tuple(LIMITS[sid])]
