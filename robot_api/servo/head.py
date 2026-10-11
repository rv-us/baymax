"""Head pan/tilt (PWM servos 1 = tilt, 2 = pan)."""
from __future__ import annotations

import time

from ..hardware import ctl, motion_lock
from .joints import HEAD
from .limits import HEAD_CENTER, HEAD_LIMITS, clamp

# PWM servos can't be read back reliably, so remember what we last sent.
_last = {"tilt": None, "pan": None}


def look(pan: int | None = None, tilt: int | None = None, ms: int = 300) -> dict:
    """Point the head. Pulses are 1200-1800 us, 1500 = centered.
    Direction (which side is higher) is not yet verified on this robot."""
    ms = max(100, int(ms))
    result = {}
    with motion_lock:
        for axis, pulse in (("pan", pan), ("tilt", tilt)):
            if pulse is None:
                continue
            sid = HEAD[axis]
            applied = clamp(sid, pulse, HEAD_LIMITS)
            ctl().set_pwm_servo_pulse(sid, applied, ms)
            _last[axis] = applied
            result[axis] = applied
            if applied != int(pulse):
                result[f"{axis}_clamped_from"] = int(pulse)
        time.sleep(ms / 1000)
    return {"head": result, "ms": ms}


def center(ms: int = 300) -> dict:
    return look(HEAD_CENTER, HEAD_CENTER, ms)


def last() -> dict:
    return dict(_last)
