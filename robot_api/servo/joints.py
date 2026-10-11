"""Servo ID <-> joint names. See docs/servo_joint_map.md for sources."""
from __future__ import annotations

from ..errors import RobotError

_SIDE_ORDER = ["ankle_roll", "ankle_pitch", "knee", "hip_pitch", "hip_roll",
               "elbow", "shoulder_roll", "shoulder_pitch"]

JOINTS: dict[str, int] = {}
for _offset, _side in ((0, "left"), (8, "right")):
    for _n, _joint in enumerate(_SIDE_ORDER, start=1):
        JOINTS[f"{_side}_{_joint}"] = _offset + _n
# Hands. Which is left/right is unconfirmed (docs disagree); this follows the
# gripper example code and hw_dry_run.py.
JOINTS["right_hand"] = 17
JOINTS["left_hand"] = 18

NAMES = {v: k for k, v in JOINTS.items()}
BODY_IDS = range(1, 17)
HAND_IDS = (17, 18)

# Head PWM servos.
HEAD = {"tilt": 1, "pan": 2}

# Factory stand pose (stand.d6a), servos 1-18.
STAND_POSE = dict(zip(range(1, 19), (
    500, 390, 500, 600, 500, 575, 800, 725,
    500, 610, 500, 400, 500, 425, 200, 275,
    500, 500,
)))


def joint_name(servo_id: int) -> str:
    return NAMES.get(servo_id, f"servo_{servo_id}")


def resolve(servo: int | str) -> int:
    """Accept a bus servo ID (1-18), a numeric string, or a joint name."""
    if isinstance(servo, str):
        key = servo.strip().lower().replace(" ", "_").replace("-", "_")
        if key.isdigit():
            servo = int(key)
        elif key in JOINTS:
            return JOINTS[key]
        else:
            raise RobotError(f"unknown joint {servo!r}; known: {', '.join(JOINTS)}")
    if not 1 <= servo <= 18:
        raise RobotError(f"bus servo id must be 1-18, got {servo}")
    return servo
