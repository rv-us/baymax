"""Whole-body poses."""
from __future__ import annotations

from ..servo import bus
from ..servo.joints import BODY_IDS, STAND_POSE


def stand(ms: int = 1500) -> dict:
    """Move every body servo to the factory stand pose. From limp (coast) the
    robot must be supported while this runs."""
    result = bus.set_servos({i: STAND_POSE[i] for i in BODY_IDS}, ms)
    return {"pose": "stand", "ms": result["ms"], **({"clamped": result["clamped"]} if "clamped" in result else {})}


def rest() -> dict:
    """Go limp so the servos cool. The robot falls unless supported."""
    return bus.coast()
