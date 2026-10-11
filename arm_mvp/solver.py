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
from robot_api.servo import arm_limits  # noqa: E402
from robot_api.servo.limits import LIMITS as ROBOT_LIMITS  # noqa: E402

ARM_SERVOS = {"L": (8, 7, 6), "R": (16, 15, 14)}  # shoulder pitch, shoulder roll, elbow
REST = (0.0, 0.0, 180.0)  # pitch, roll, interior elbow: the stand pose
HANG_FULL_DEG, HANG_NONE_DEG = 20.0, 40.0  # upper arm this far from straight down: surely at the side / not
REST_OUT_DEG = 4.0                         # at the side the elbows point out, ever so slightly
ELBOW_SOFT_DEG = 25.0                      # elbow bends smaller than this are eased toward straight (see soften_elbow)
# Same per-servo ranges robot_api clamps to; arm_limits keeps the rolls off the body on top of that.
ARM_LIMITS = {sid: ROBOT_LIMITS[sid] for side in "LR" for sid in ARM_SERVOS[side]}


def body_safe(pulses):
    """robot_api's coupled arm limits applied locally (the robot applies them too), so the twin
    and HUD show what the robot will do. pulses: {servo id: pulse} for both arms."""
    return arm_limits.limit(pulses, {})[0]


def hang_prior(side, sol, pts):
    """
    A person's arm at their side points down with the elbow a little out, so there its shoulder pitch
    is 0, not a half turn away. Straight down, the arm's direction is the same whether the shoulder is
    at pitch 0 or at pitch 180 with the roll flipped, and the IK can land on either (and flip between
    them as the camera noise nudges it), which swings the robot's arm round. Within HANG_FULL_DEG of
    straight down this returns pitch 0 and an outward roll of at least REST_OUT_DEG; between that and
    HANG_NONE_DEG it fades into the arm's own forward and sideways lift (read from the upper arm alone,
    on the branch where pitch is small); beyond, the IK's answer is returned untouched.

    side: "L" or "R"; sol: (pitch, roll, interior elbow) from ArmIK.solve; pts: shoulder, elbow, wrist
    in the torso frame (forward, left, up). Roll is outward-positive for the left arm, negative for the right.
    """
    pitch, roll, elbow = sol
    d = np.asarray(pts[1], dtype=np.float64) - np.asarray(pts[0], dtype=np.float64)
    n = float(np.linalg.norm(d))
    if n < 1e-6 or not np.isfinite(n):
        return sol
    elevation = math.degrees(math.acos(float(np.clip(-d[2] / n, -1.0, 1.0))))
    if elevation >= HANG_NONE_DEG:
        return sol
    pitch_geo = math.degrees(math.atan2(-d[0], -d[2])) if math.hypot(d[0], d[2]) > 1e-3 else 0.0
    roll_geo = math.degrees(math.asin(float(np.clip(d[1] / n, -1.0, 1.0))))
    sign = 1.0 if side == "L" else -1.0
    hang = float(np.clip((HANG_NONE_DEG - elevation) / (HANG_NONE_DEG - HANG_FULL_DEG), 0.0, 1.0))
    at_side_roll = sign * max(sign * roll_geo, REST_OUT_DEG)
    return ((1.0 - hang) * pitch_geo, (1.0 - hang) * roll_geo + hang * at_side_roll, elbow)


def soften_elbow(interior):
    """
    Keep a straight arm from twitching. The elbow bend is the angle between two noisy directions, so
    when the arm is nearly straight the noise cannot go negative: it piles up as a few degrees of bend
    that flicker from frame to frame (measured: 6 to 17 degrees of wander on a perfectly still, straight
    arm). Bends under ELBOW_SOFT_DEG are scaled down by a smoothstep (3t^2 - 2t^3), so 5 degrees reads as
    about 0.5 and 10 as 3.5, while the curve meets the real angle smoothly at ELBOW_SOFT_DEG (no kink)
    and everything beyond it is untouched. `interior` is the interior elbow angle (180 = straight).
    """
    flex = 180.0 - float(interior)
    if flex >= ELBOW_SOFT_DEG:
        return float(interior)
    t = max(flex, 0.0) / ELBOW_SOFT_DEG
    return 180.0 - flex * (3.0 * t * t - 2.0 * t ** 3)


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
