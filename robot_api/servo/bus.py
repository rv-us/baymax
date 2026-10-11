"""Granular bus-servo control: set/read individual servos, coast, hold.

Every move is clamped to servo.limits, refused when a servo is too hot, and
slowed so no servo travels faster than config.MS_PER_PULSE.
"""
from __future__ import annotations

import time

from .. import config
from ..hardware import ctl, ensure_cool, motion_lock, reader
from ..errors import RobotError
from .joints import BODY_IDS, HAND_IDS, joint_name, resolve
from .limits import LIMITS, clamp


def _move_ms(targets: dict, requested_ms: int | None) -> int:
    r = reader()
    travel = 0
    for sid, pulse in targets.items():
        current = r.read(sid, "position") if sid not in HAND_IDS else None
        # Unknown start (hands never answer): assume the full allowed range.
        travel = max(travel, abs(pulse - current) if current is not None
                     else LIMITS[sid][1] - LIMITS[sid][0])
    return max(config.MIN_MOVE_MS, int(requested_ms or 0), int(travel * config.MS_PER_PULSE))


def set_servos(targets: dict, ms: int | None = None, wait: bool = True) -> dict:
    """Move several bus servos together.

    targets: {servo id or joint name: pulse 0-1000}. Pulses outside the
    servo's limits are clamped and reported under "clamped".
    """
    if not targets:
        raise RobotError("no servos given")
    applied, clamped = {}, {}
    for key, pulse in targets.items():
        sid = resolve(key)
        applied[sid] = clamp(sid, pulse)
        if applied[sid] != int(pulse):
            clamped[joint_name(sid)] = {"requested": int(pulse), "applied": applied[sid],
                                        "limits": LIMITS[sid]}
    with motion_lock:
        ensure_cool()
        move_ms = _move_ms(applied, ms)
        ctl().board.bus_servo_set_position(move_ms / 1000, [[i, p] for i, p in applied.items()])
        if wait:
            time.sleep(move_ms / 1000 + 0.05)
    result = {"moved": {joint_name(i): p for i, p in applied.items()}, "ms": move_ms}
    if clamped:
        result["clamped"] = clamped
    return result


def set_servo(servo: int | str, pulse: int, ms: int | None = None) -> dict:
    """Move one bus servo (ID 1-18 or joint name) to a pulse (0-1000)."""
    return set_servos({servo: pulse}, ms)


def read_servo(servo: int | str) -> dict:
    """Position, temperature, input voltage and torque state of one servo."""
    sid = resolve(servo)
    info = {"id": sid, "joint": joint_name(sid), "limits": LIMITS[sid]}
    if sid in HAND_IDS:
        info["note"] = "hand servos accept commands but never answer reads"
        return info
    r = reader()
    vin = r.read(sid, "vin")
    torque = r.read(sid, "torque")
    info.update(position=r.read(sid, "position"), temp_c=r.read(sid, "temp"),
                vin_v=round(vin / 1000, 2) if vin else None,
                torque_on=None if torque is None else bool(torque))
    return info


def status() -> dict:
    """read_servo for every body servo (1-16)."""
    servos = [read_servo(i) for i in BODY_IDS]
    temps = [s["temp_c"] for s in servos if s.get("temp_c") is not None]
    return {
        "servos": servos,
        "hottest_c": max(temps) if temps else None,
        "max_allowed_c": config.MAX_SERVO_TEMP_C,
        "limp": [s["joint"] for s in servos if s.get("torque_on") is False],
    }


def coast(servos: list | None = None) -> dict:
    """Turn torque off (servos go limp and cool down). The robot falls unless
    supported. Default: all body servos 1-16."""
    ids = [resolve(s) for s in servos] if servos else list(BODY_IDS)
    c = ctl()
    for sid in ids:
        c.unload_bus_servo(sid)
        time.sleep(0.02)
    return {"limp": [joint_name(i) for i in ids]}


def hold(servos: list | None = None) -> dict:
    """Turn torque back on, holding each servo where it is now."""
    ids = [resolve(s) for s in servos] if servos else list(BODY_IDS)
    r = reader()
    here = {sid: r.read(sid, "position") for sid in ids if sid not in HAND_IDS}
    unknown = [joint_name(i) for i, p in here.items() if p is None]
    if unknown:
        raise RobotError(f"could not read positions of {unknown}")
    return set_servos(here, config.MIN_MOVE_MS)


def stop() -> dict:
    """Stop all bus servos where they are (does not cancel action groups;
    see actions.groups.stop)."""
    ctl().board.bus_servo_stop(list(range(1, 19)))
    return {"stopped": True}
