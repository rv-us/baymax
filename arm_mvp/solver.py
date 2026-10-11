"""
Monocular arm geometry and servo mapping.

Everything before the torso frame lives in metric camera coordinates (x right, y down, z away
from the camera, origin at the lens). A landmark at pixel (x, y) lies on the ray
r = ((x - cx) / f, (y - cy) / f, 1), so its 3D position is Z * r for some depth Z.
"""
import math
import os
import sys

import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(_HERE, ".."), os.path.join(_HERE, "..", "laptop_mvp")):
    if os.path.abspath(_p) not in sys.path:
        sys.path.insert(0, os.path.abspath(_p))

from mediapose_est import SERVO_LIMITS, STAND_PULSES  # noqa: E402
from retarget import elbow_pulse, shoulder_pulses  # noqa: E402

ARM_SERVOS = {"L": (8, 7, 6), "R": (16, 15, 14)}  # shoulder pitch, shoulder roll, elbow
REST = (0.0, 0.0, 180.0)  # pitch, roll, interior elbow: the stand pose
# Elbows and shoulder rolls tightened to robot_api/servo/limits.py (125..875, bracket protection).
ARM_LIMITS = {sid: (max(SERVO_LIMITS[sid][0], 125), min(SERVO_LIMITS[sid][1], 875)) if sid in (6, 7, 14, 15)
              else SERVO_LIMITS[sid] for side in "LR" for sid in ARM_SERVOS[side]}


def ray(px, focal, center):
    return np.array([(px[0] - center[0]) / focal, (px[1] - center[1]) / focal, 1.0])


def shoulders_3d(px_l, px_r, world_l, world_r, width_m, focal, center):
    """
    Left and right shoulder in camera coordinates. Depth comes from the shoulder width in
    pixels (known width in metres), the yaw of the shoulder line from the world landmarks.
    """
    w = np.asarray(world_l, dtype=np.float64) - np.asarray(world_r, dtype=np.float64)
    n = float(np.linalg.norm(w))
    r_l, r_r = ray(px_l, focal, center), ray(px_r, focal, center)
    sep = float(np.linalg.norm((r_l - r_r)[:2]))
    if n < 1e-6 or sep < 1e-6 or not np.isfinite(n + sep):
        return None
    dz = width_m * w[2] / n                       # depth difference left minus right
    flat = width_m * float(np.hypot(w[0], w[1])) / n  # width left in the image plane
    z = flat / sep
    for _ in range(3):  # the two depths differ when yawed, refine the mid depth
        p_l, p_r = (z + 0.5 * dz) * r_l, (z - 0.5 * dz) * r_r
        z *= flat / max(float(np.linalg.norm((p_l - p_r)[:2])), 1e-6)
    return (z + 0.5 * dz) * r_l, (z - 0.5 * dz) * r_r


def depth_sign(prev, world_dz, thresh=0.04):
    """Near (-1) or far (+1) solution for a bone, from the world landmark depth difference
    (child minus parent, metres). Only flips when |dz| clears the threshold, so it cannot
    flicker near 0."""
    if abs(world_dz) > thresh:
        return 1.0 if world_dz > 0 else -1.0
    return prev


def bone_end(parent, r, length, sign):
    """
    Child joint of a bone of known length: the point on ray r at distance `length` from
    `parent`. This is dz = sign * sqrt(L^2 - dx^2 - dy^2) with perspective handled exactly
    (it reduces to that formula far from the camera). When the 2D extent already exceeds the
    length (noise) there is no solution: take the closest point on the ray and renormalize.
    """
    a, b = float(r @ r), float(r @ parent)
    disc = b * b - a * (float(parent @ parent) - length * length)
    if disc <= 0.0:
        d = (b / a) * r - parent
        return parent + length * d / max(float(np.linalg.norm(d)), 1e-9)
    z = (b + sign * math.sqrt(disc)) / a
    if z <= 1e-3:  # near solution behind the lens
        z = (b + math.sqrt(disc)) / a
    return z * r


def reconstruct_arm(shoulder, px_elbow, px_wrist, world, signs, lengths, focal, center):
    """
    Elbow and wrist in camera coordinates, rebuilt as shoulder + bone vectors of calibrated
    length. world = (3, 3) world landmarks of shoulder, elbow, wrist, signs = previous (upper,
    forearm) depth signs, lengths = (upper, forearm) in metres. Returns (elbow, wrist, signs).

    The sign of each bone is the side of the child's view ray the bone points along (world bone
    vector dotted with the ray). For a centred joint that is just the sign of the world z
    difference, off centre it stays correct where plain z would pick the wrong root.
    """
    out, parent, new_signs = [], shoulder, []
    for i, px in enumerate((px_elbow, px_wrist)):
        r = ray(px, focal, center)
        sign = depth_sign(signs[i], float((world[i + 1] - world[i]) @ r) / float(np.linalg.norm(r)))
        parent = bone_end(parent, r, lengths[i], sign)
        out.append(parent)
        new_signs.append(sign)
    return out[0], out[1], tuple(new_signs)


def arm_pulses(side, pitch, roll, elbow):
    """Arm angles (retarget.py convention, degrees) -> {servo id: pulse}, always inside ARM_LIMITS."""
    pitch, roll, elbow = (float(np.clip(a, -360.0, 360.0)) if np.isfinite(a) else rest
                          for a, rest in zip((pitch, roll, elbow), REST))
    raw = (*shoulder_pulses(pitch, roll, side), elbow_pulse(elbow, side))
    return {sid: int(min(max(p, ARM_LIMITS[sid][0]), ARM_LIMITS[sid][1])) for sid, p in zip(ARM_SERVOS[side], raw)}


def rate_limit(prev, target, max_step):
    """Per tick pulse limit: each servo moves at most max_step pulses toward its target."""
    return {sid: int(prev[sid] + min(max(p - prev[sid], -max_step), max_step)) for sid, p in target.items()}


def stand_arm_pulses():
    return {sid: STAND_PULSES[sid] for side in "LR" for sid in ARM_SERVOS[side]}
