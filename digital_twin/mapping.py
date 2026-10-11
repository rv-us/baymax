"""Servo pulse <-> URDF joint angle calibration for the TonyPi.

The bus servos report 0-1000 pulses spanning 240 deg (so 1 deg = 1000/240 pulses).
Every joint's URDF zero is the factory stand pose (``STAND_POSE``), which is what the
recorded poses in ``scripts/poses.json`` are measured against:

    angle_deg = sign * (pulse - STAND[servo]) / PULSE_PER_DEG

``sign`` is the only calibration freedom per joint. How each sign was settled:

* Arms (servos 6-8, 14-16): same convention as ``laptop_mvp/retarget.py``
  (``shoulder_pulses`` / ``elbow_pulse``), and checked against the recorded poses:
  ``t_pose`` gives roll ~75 deg / pitch ~-10 deg on both arms, ``zombie_arms_forward``
  gives pitch ~-97 deg (URDF: negative pitch = arm swings forward).
* Legs: right-side servos are mirrored (pulse_R = 1000 - pulse_L for the same motion), so
  every right sign is the negative of the left one for pitch joints. The left signs come
  from ``scorpion_pose_bending_backwards``: with these signs hip(+30) + knee(+88) +
  ankle(-47) leaves the sole tilted ~71 deg, i.e. the torso leans *back*, which is what
  the pose is called. The opposite ankle sign would give a 165 deg nonsense lean.
* Roll joints of the legs (servos 1, 5, 9, 13) barely move in any recorded pose
  (+-15 pulses), so their direction is unverified -> ``verified=False``. Same for the
  head PWM servos, which are not in the pose recordings at all.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Dict, Iterable, Optional

PULSE_PER_DEG = 1000.0 / 240.0

# Factory stand pose (robot_api.servo.joints.STAND_POSE), servos 1-18.
STAND_POSE: Dict[int, int] = dict(zip(range(1, 19), (
    500, 390, 500, 600, 500, 575, 800, 725,
    500, 610, 500, 400, 500, 425, 200, 275,
    500, 500,
)))

# Per-servo pulse limits (robot_api.servo.limits.LIMITS). tests/test_digital_twin.py
# asserts both tables still match robot_api so they cannot silently drift.
LIMITS: Dict[int, tuple] = {
    1: (395, 761), 2: (50, 819), 3: (0, 960), 4: (125, 857),
    5: (375, 750), 6: (125, 930), 7: (125, 900), 8: (0, 1000),
    9: (366, 570), 10: (140, 950), 11: (40, 1000), 12: (141, 875),
    13: (249, 624), 14: (33, 875), 15: (100, 875), 16: (31, 1000),
    17: (300, 760), 18: (240, 500),
}

HEAD_CENTER_US = 1500
HEAD_LIMITS_US = {"tilt": (950, 1800), "pan": (1200, 1800)}
HEAD_DEG_PER_US = 0.09  # hobby PWM servo: 500-2500 us across 180 deg (unverified on this robot)


@dataclass(frozen=True)
class ServoJoint:
    servo: int
    joint: str       # URDF joint name
    label: str       # robot_api joint name
    sign: int        # URDF angle = sign * (pulse - stand) / PULSE_PER_DEG
    verified: bool = True

    @property
    def zero(self) -> int:
        return STAND_POSE[self.servo]

    def to_angle(self, pulse: float) -> float:
        return self.sign * (pulse - self.zero) / PULSE_PER_DEG

    def to_pulse(self, angle_deg: float) -> int:
        return int(round(self.zero + self.sign * angle_deg * PULSE_PER_DEG))


def _table() -> Dict[int, ServoJoint]:
    rows = [
        # left leg + arm (servos 1-8), then right (9-16)
        (1, "l_ankle_roll", "left_ankle_roll", +1, False),
        (2, "l_ankle_pitch", "left_ankle_pitch", +1, True),
        (3, "l_knee", "left_knee", +1, True),
        (4, "l_hip_pitch", "left_hip_pitch", +1, True),
        (5, "l_hip_roll", "left_hip_roll", +1, False),
        (6, "l_elbow", "left_elbow", -1, True),
        (7, "l_shoulder_roll", "left_shoulder_roll", -1, True),
        (8, "l_shoulder_pitch", "left_shoulder_pitch", +1, True),
        (9, "r_ankle_roll", "right_ankle_roll", +1, False),
        (10, "r_ankle_pitch", "right_ankle_pitch", -1, True),
        (11, "r_knee", "right_knee", -1, True),
        (12, "r_hip_pitch", "right_hip_pitch", -1, True),
        (13, "r_hip_roll", "right_hip_roll", +1, False),
        (14, "r_elbow", "right_elbow", +1, True),
        (15, "r_shoulder_roll", "right_shoulder_roll", -1, True),
        (16, "r_shoulder_pitch", "right_shoulder_pitch", -1, True),
    ]
    return {r[0]: ServoJoint(*r) for r in rows}


SERVO_JOINTS: Dict[int, ServoJoint] = _table()
BY_JOINT: Dict[str, ServoJoint] = {sj.joint: sj for sj in SERVO_JOINTS.values()}
BY_LABEL: Dict[str, ServoJoint] = {sj.label: sj for sj in SERVO_JOINTS.values()}
BODY_IDS = tuple(range(1, 17))
HAND_IDS = (17, 18)


def clamp_pulse(servo: int, pulse: float) -> int:
    lo, hi = LIMITS[servo]
    return max(lo, min(hi, int(round(pulse))))


def normalize_servos(servos: Dict, clamp: bool = True) -> Dict[int, int]:
    """Accept {id | "id" | label: pulse}; return {id: pulse}, optionally clamped to LIMITS."""
    out: Dict[int, int] = {}
    for key, pulse in servos.items():
        if isinstance(key, str) and not key.strip().isdigit():
            name = key.strip().lower().replace(" ", "_").replace("-", "_")
            if name == "right_hand":
                sid = 17
            elif name == "left_hand":
                sid = 18
            elif name in BY_LABEL:
                sid = BY_LABEL[name].servo
            else:
                raise KeyError(f"unknown servo/joint {key!r}")
        else:
            sid = int(key)
        if not 1 <= sid <= 18:
            raise KeyError(f"servo id must be 1-18, got {sid}")
        out[sid] = clamp_pulse(sid, pulse) if clamp else int(round(pulse))
    return out


def full_servos(partial: Optional[Dict] = None, base: Optional[Dict[int, int]] = None) -> Dict[int, int]:
    """Stand pose (or ``base``) overlaid with ``partial``, always all 18 servos."""
    out = dict(base or STAND_POSE)
    if partial:
        out.update(normalize_servos(partial))
    return out


def pulses_to_angles(servos: Dict[int, int]) -> Dict[str, float]:
    """{servo: pulse} -> {urdf joint: degrees} for every body servo present."""
    return {SERVO_JOINTS[s].joint: SERVO_JOINTS[s].to_angle(p) for s, p in servos.items() if s in SERVO_JOINTS}


def angles_to_pulses(angles: Dict[str, float], clamp: bool = True) -> Dict[int, int]:
    """Inverse of :func:`pulses_to_angles`, clamped to servo limits unless ``clamp=False``."""
    return {BY_JOINT[j].servo: clamp_pulse(BY_JOINT[j].servo, BY_JOINT[j].to_pulse(a)) if clamp else BY_JOINT[j].to_pulse(a)
            for j, a in angles.items() if j in BY_JOINT}


def head_to_angles(pan_us: float, tilt_us: float) -> Dict[str, float]:
    """Head PWM pulses -> neck joint degrees. Direction is unverified (positive = left / down)."""
    return {"neck_pan": (pan_us - HEAD_CENTER_US) * HEAD_DEG_PER_US,
            "neck_pitch": (tilt_us - HEAD_CENTER_US) * HEAD_DEG_PER_US}


# ---------------------------------------------------------------------------
# Recorded poses (scripts/poses.json)
# ---------------------------------------------------------------------------

def default_poses_path() -> str:
    env = os.environ.get("BAYMAX_POSES_JSON")
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(here, "..", "scripts", "poses.json")


def load_poses(path: Optional[str] = None) -> Dict[str, Dict[int, int]]:
    """{pose name: {servo id: pulse}} from the recorded poses file (missing file -> just stand)."""
    path = path or default_poses_path()
    poses: Dict[str, Dict[int, int]] = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raw = {}
    for name, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("servos"), dict):
            servos = normalize_servos(entry["servos"], clamp=False)
        elif isinstance(entry.get("joints"), dict):
            servos = normalize_servos(entry["joints"], clamp=False)
        else:
            continue
        poses[str(name)] = full_servos(servos)
    poses.setdefault("default_standing_pose", dict(STAND_POSE))
    return poses


def pretty_name(key: str) -> str:
    return key.replace("_", " ").strip().capitalize()


def servo_table(ids: Iterable[int] = range(1, 19)) -> list:
    """Static description of each servo for the dashboard."""
    rows = []
    for sid in ids:
        sj = SERVO_JOINTS.get(sid)
        rows.append({
            "id": sid,
            "label": sj.label if sj else ("right_hand" if sid == 17 else "left_hand"),
            "joint": sj.joint if sj else None,
            "stand": STAND_POSE[sid],
            "limits": list(LIMITS[sid]),
            "sign": sj.sign if sj else 0,
            "verified": sj.verified if sj else False,
        })
    return rows
