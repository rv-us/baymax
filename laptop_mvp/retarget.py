"""
Human pose -> TonyPi joint angles, solved against the robot's own joint chain.

Input is the fused 3D landmark set (pose estimator XY + depth-fused Z). Everything
is first expressed in the robot's torso frame (x forward, y left, z up, origin at
the torso centre), then turned into URDF joint angles:

Arms (shoulder_pitch about Y -> shoulder_roll about X -> elbow about Y):
  - Elbow flexion is the exact interior angle between upper arm and forearm.
  - Shoulder pitch/roll come from a tiny damped least-squares IK that matches the
    robot's upper-arm AND forearm *directions* to the human's. Directions (not
    positions) make it independent of human/robot limb proportions.
  - The chain's real pole is the T-pose (roll = +-90 deg): there the upper arm no
    longer constrains pitch, so the forearm term takes over, and if the arm is also
    straight the damping term simply holds the previous pitch. The two mirror
    solutions (p, r) / (p + 180, 180 - r) are both tried and the one closest to the
    previous frame wins, so the arm passes through the pole without flipping.

Legs (hip_pitch, knee, ankle - all about Y, sagittal plane only):
  - Closed-form: hip flexion from the thigh's forward/down components, knee from the
    interior angle, ankle chosen so the foot stays level.
"""

from __future__ import annotations

import math
from typing import Dict, Optional, Tuple

import numpy as np

L_SHOULDER, R_SHOULDER = 11, 12
L_ELBOW, R_ELBOW = 13, 14
L_WRIST, R_WRIST = 15, 16
L_HIP, R_HIP = 23, 24
L_KNEE, R_KNEE = 25, 26
L_ANKLE, R_ANKLE = 27, 28

ARM_IDX = {"L": (L_SHOULDER, L_ELBOW, L_WRIST), "R": (R_SHOULDER, R_ELBOW, R_WRIST)}
LEG_IDX = {"L": (L_HIP, L_KNEE, L_ANKLE), "R": (R_HIP, R_KNEE, R_ANKLE)}

# Joint limits in URDF degrees (pitch: 0 down, -90 forward, -180 overhead;
# roll: left arm positive outward, right arm negative outward).
PITCH_LIMITS = (-180.0, 80.0)
ROLL_LIMITS = {"L": (-30.0, 180.0), "R": (-180.0, 30.0)}
MAX_ELBOW_FLEX = 90.0  # servo 6/14 reach about 85-94 deg of flexion (pulse limits 930 / 33)


def _unit(v: np.ndarray) -> Optional[np.ndarray]:
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-6 else None


def to_torso_frame(world_lms) -> Optional[np.ndarray]:
    """
    (33, 3) landmark positions in the robot torso frame: x forward (out of the chest),
    y toward the left shoulder, z up the spine, origin at the torso centre.
    Input uses the MediaPipe camera convention (x right, y down, z away from camera).
    """
    pts = np.array([[l.x, l.y, l.z] for l in world_lms], dtype=np.float64)
    mid_sh = 0.5 * (pts[L_SHOULDER] + pts[R_SHOULDER])
    mid_hip = 0.5 * (pts[L_HIP] + pts[R_HIP])
    up = _unit(mid_sh - mid_hip)
    if up is None:
        return None
    left = pts[L_SHOULDER] - pts[R_SHOULDER]
    left = _unit(left - np.dot(left, up) * up)
    if left is None:
        return None
    fwd = np.cross(left, up)
    rot = np.stack([fwd, left, up])  # rows = torso axes in camera coords
    return (pts - 0.5 * (mid_sh + mid_hip)) @ rot.T


def shoulder_frame(l_shoulder, r_shoulder, up=(0.0, -1.0, 0.0)) -> Optional[Tuple[np.ndarray, np.ndarray]]:
    """
    Hip-free torso frame: (rot, origin) with rot rows = (forward, left, up) in camera coords and
    origin at the shoulder midpoint, so torso_pts = (pts - origin) @ rot.T. Left comes from the
    shoulder line, `up` is assumed (upright subject, camera convention y down), forward is their
    cross product. Left is re-orthogonalized against up, so a shrugged shoulder does not tilt the frame.
    """
    l_shoulder, r_shoulder = np.asarray(l_shoulder, dtype=np.float64), np.asarray(r_shoulder, dtype=np.float64)
    up = _unit(np.asarray(up, dtype=np.float64))
    if up is None or not (np.all(np.isfinite(l_shoulder)) and np.all(np.isfinite(r_shoulder))):
        return None
    left = l_shoulder - r_shoulder
    left = _unit(left - np.dot(left, up) * up)
    if left is None:
        return None
    return np.stack([np.cross(left, up), left, up]), 0.5 * (l_shoulder + r_shoulder)


def arm_fk(pitch: float, roll: float, flex: float) -> Tuple[np.ndarray, np.ndarray]:
    """Unit upper-arm and forearm directions in the torso frame (angles in radians)."""
    cp, sp, cr, sr = math.cos(pitch), math.sin(pitch), math.cos(roll), math.sin(roll)
    rot = np.array([[cp, sp * sr, sp * cr],
                    [0.0, cr, -sr],
                    [-sp, cp * sr, cp * cr]])  # Ry(pitch) @ Rx(roll)
    upper = rot @ np.array([0.0, 0.0, -1.0])
    fore = rot @ np.array([math.sin(flex), 0.0, -math.cos(flex)])  # elbow bends toward +x
    return upper, fore


class ArmIK:
    """Per-arm shoulder solver. Keeps the previous solution for continuity."""

    def __init__(self, side: str, fore_weight: float = 0.35, damping: float = 0.1):
        self.side = side
        self.fore_weight = fore_weight
        self.damping = damping
        self.lo = np.radians([PITCH_LIMITS[0], ROLL_LIMITS[side][0]])
        self.hi = np.radians([PITCH_LIMITS[1], ROLL_LIMITS[side][1]])
        self.q = np.zeros(2)  # [pitch, roll] in radians

    def _residual(self, q, d_h, f_h, flex, q_prev):
        d, f = arm_fk(q[0], q[1], flex)
        return np.concatenate([d - d_h,
                               math.sqrt(self.fore_weight) * (f - f_h),
                               math.sqrt(self.damping) * (q - q_prev)])

    def _clip(self, q):
        q = np.array(q, dtype=np.float64)
        q[0] = self.hi[0] - (self.hi[0] - q[0]) % (2.0 * math.pi)  # wrap pitch to (hi - 360, hi]
        q[1] = (q[1] + math.pi) % (2.0 * math.pi) - math.pi
        return np.clip(q, self.lo, self.hi)

    def _refine(self, q, d_h, f_h, flex, q_prev, iters=8):
        q = self._clip(q)
        for _ in range(iters):
            r = self._residual(q, d_h, f_h, flex, q_prev)
            jac = np.empty((r.size, 2))
            for k in range(2):
                dq = np.zeros(2)
                dq[k] = 1e-5
                jac[:, k] = (self._residual(q + dq, d_h, f_h, flex, q_prev) - r) / 1e-5
            step = np.linalg.solve(jac.T @ jac + 1e-4 * np.eye(2), -jac.T @ r)
            q = np.clip(q + np.clip(step, -0.5, 0.5), self.lo, self.hi)
        r = self._residual(q, d_h, f_h, flex, q_prev)
        return q, float(r @ r)

    def solve(self, shoulder, elbow, wrist, q_prev_deg=None) -> Optional[Tuple[float, float, float]]:
        """
        Torso-frame joint positions -> (pitch_deg, roll_deg, elbow_interior_deg) in URDF
        convention. `q_prev_deg` (pitch, roll) overrides the continuity reference, e.g.
        with the smoothed angles actually sent to the robot.
        """
        d_h, f_h = _unit(elbow - shoulder), _unit(wrist - elbow)
        if d_h is None or f_h is None:
            return None
        flex = min(math.acos(float(np.clip(np.dot(d_h, f_h), -1.0, 1.0))), math.radians(MAX_ELBOW_FLEX))
        q_prev = self._clip(np.radians(q_prev_deg)) if q_prev_deg is not None else self.q

        # Closed-form seeds from the upper arm alone: d = (-sp*cr, sr, -cp*cr)
        roll_a = math.asin(float(np.clip(d_h[1], -1.0, 1.0)))
        pitch_a = math.atan2(-d_h[0], -d_h[2]) if math.hypot(d_h[0], d_h[2]) > 1e-3 else q_prev[0]
        seeds = (q_prev, (pitch_a, roll_a), (pitch_a + math.pi, math.pi - roll_a))
        self.q = min((self._refine(s, d_h, f_h, flex, q_prev) for s in seeds), key=lambda qc: qc[1])[0]
        return math.degrees(self.q[0]), math.degrees(self.q[1]), 180.0 - math.degrees(flex)


def leg_angles(pts: np.ndarray, side: str = "L") -> Dict[str, float]:
    """
    Torso-frame landmarks -> sagittal leg angles in the conventions `update_legs` and
    `map_leg_servos` expect: knee interior (180 straight), hip_pitch (+ = thigh forward),
    ankle_pitch (+ = dorsiflexion), with the ankle keeping the foot level.
    """
    hip, knee, ankle = (pts[i] for i in LEG_IDX[side])
    thigh, shin = _unit(knee - hip), _unit(ankle - knee)
    out = {"knee": 180.0, "hip_pitch": 0.0, "ankle_pitch": 0.0, "hip_roll": 0.0, "ankle_roll": 0.0}
    if thigh is None or shin is None:
        return out
    hip_deg = float(np.clip(math.degrees(math.atan2(thigh[0], -thigh[2])), -30.0, 90.0))
    knee_flex = float(np.clip(math.degrees(math.acos(float(np.clip(np.dot(thigh, shin), -1.0, 1.0)))), 0.0, 90.0))
    out.update(knee=180.0 - knee_flex, hip_pitch=hip_deg,
               ankle_pitch=float(np.clip(knee_flex - hip_deg, -45.0, 57.0)))
    return out


# Servo scale: 0-1000 pulses span 240 deg. Checked against robot_api/actions/arms.py, whose
# "forward" (horizontal) shoulder pulses are 350 (L) / 650 (R): 375 pulses = 90 deg from the
# stand pulses 725 / 275. The URDF zeros are the robot's stand pose (arm hanging, elbow
# straight, roll axis in the pitch plane), which is what STAND_POSE in servo/joints.py encodes.
PULSE_PER_DEG = 1000.0 / 240.0


def shoulder_pulses(pitch_deg: float, roll_deg: float, side: str = "L") -> Tuple[int, int]:
    """URDF shoulder angles -> raw (pitch_pulse, roll_pulse) for servos 8/7 (L) or 16/15 (R)."""
    k = PULSE_PER_DEG
    if side == "L":
        return int(round(725 + k * pitch_deg)), int(round(800 - k * roll_deg))
    return int(round(275 - k * pitch_deg)), int(round(200 - k * roll_deg))


def elbow_pulse(interior_deg: float, side: str = "L") -> int:
    """Interior elbow angle (180 = straight) -> raw pulse for servo 6 (L) or 14 (R)."""
    flex = 180.0 - interior_deg
    return int(round(575 + PULSE_PER_DEG * flex)) if side == "L" else int(round(425 - PULSE_PER_DEG * flex))


def pulses_to_angles(pitch_pulse: float, roll_pulse: float, elbow_pulse_: float, side: str = "L"):
    """Inverse of shoulder_pulses/elbow_pulse: (pitch_deg, roll_deg, interior_elbow_deg)."""
    k = PULSE_PER_DEG
    if side == "L":
        return (pitch_pulse - 725) / k, (800 - roll_pulse) / k, 180.0 - (elbow_pulse_ - 575) / k
    return (275 - pitch_pulse) / k, (200 - roll_pulse) / k, 180.0 - (425 - elbow_pulse_) / k
