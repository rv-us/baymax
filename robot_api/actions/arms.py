"""Arm and gripper moves."""
from __future__ import annotations

from ..errors import RobotError
from ..servo import bus
from . import groups

# Shoulder pitch servo and its pulse for each named position. Mirrored sides:
# the left arm raises toward lower pulses, the right toward higher. "down" is
# the stand pose; "forward" is roughly horizontal; "up" is the raised limit
# used by keyboard_control.py.
ARM_POSITIONS = {
    "left": (8, {"down": 725, "forward": 350, "up": 125}),
    "right": (16, {"down": 275, "forward": 650, "up": 875}),
}

# Hand servo and its open pulse; both close at 500. Side follows the gripper
# example code (unconfirmed, see docs/servo_joint_map.md).
GRIPPERS = {"right": (17, 760), "left": (18, 240)}
GRIPPER_CLOSED = 500
GRIPPER_MS = 500


def _sides(side: str) -> list:
    s = side.strip().lower()
    if s == "both":
        return ["left", "right"]
    if s not in ("left", "right"):
        raise RobotError(f"side must be left, right or both, got {side!r}")
    return [s]


def arm(side: str, position: str) -> dict:
    """Raise or lower an arm at the shoulder: position down | forward | up."""
    pos = position.strip().lower()
    if pos not in ("down", "forward", "up"):
        raise RobotError(f"position must be down, forward or up, got {position!r}")
    targets = {ARM_POSITIONS[s][0]: ARM_POSITIONS[s][1][pos] for s in _sides(side)}
    return bus.set_servos(targets)


def gripper(side: str, action: str) -> dict:
    """Open or close a hand: action open | close."""
    act = action.strip().lower()
    if act not in ("open", "close"):
        raise RobotError(f"action must be open or close, got {action!r}")
    targets = {GRIPPERS[s][0]: GRIPPERS[s][1] if act == "open" else GRIPPER_CLOSED
               for s in _sides(side)}
    return bus.set_servos(targets, GRIPPER_MS)


def wave() -> dict:
    return groups.run("wave")
