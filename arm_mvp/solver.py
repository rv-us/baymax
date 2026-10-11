"""
Monocular body geometry: depth recovery from bone lengths, body frames, leg angles and the
balance layer. Servo mapping lives in joints.py.

Everything before the torso frame lives in metric camera coordinates (x right, y down, z away
from the camera, origin at the lens). A landmark at pixel (x, y) lies on the ray
r = ((x - cx) / f, (y - cy) / f, 1), so its 3D position is Z * r for some depth Z.
"""
import math

import numpy as np

import joints

# Balance layer constants (all UNVERIFIED on hardware, chosen conservative).
BALANCE_HIP_MARGIN = 10.0  # hip pitch may leave knee / 2 (hip over ankle) by this many degrees
LIFT_FRAC = 0.12           # ankle height difference, as a fraction of leg length, that counts as a lifted foot
LIFT_KNEE, LIFT_HIP = 15.0, 10.0  # most a lifted leg may differ from the stance leg, degrees
BLEND_START = 0.85         # in-plane fraction of a bone above which its depth leans on the world landmarks


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


def reconstruct_chain(parent, pxs, world, signs, lengths, focal, center, blend=False):
    """
    Joints of a bone chain in camera coordinates, each rebuilt as parent + bone vector of
    calibrated length. pxs = child pixels, world = world landmarks of parent and children,
    signs = previous depth signs, lengths = bone lengths in metres. Returns (points, signs).

    The sign of each bone is the side of the child's view ray the bone points along (world bone
    vector dotted with the ray). For a centred joint that is just the sign of the world z
    difference, off centre it stays correct where plain z would pick the wrong root.

    blend=True is for bones that spend their time in the image plane (torso, standing legs).
    There dz = sqrt(L^2 - d^2) is ill conditioned (1 cm of length error is about 10 cm of
    depth), so as the bone approaches the image plane its depth fades to the world landmark
    depth, which is small and steady there. Foreshortened bones keep the exact solution.
    """
    points, new_signs = [], []
    for i, px in enumerate(pxs):
        r = ray(px, focal, center)
        w = world[i + 1] - world[i]
        sign = depth_sign(signs[i], float(w @ r) / float(np.linalg.norm(r)))
        child = bone_end(parent, r, lengths[i], sign)
        if blend:
            v = child - parent
            k = float(np.clip((np.hypot(v[0], v[1]) / lengths[i] - BLEND_START) / (1.0 - BLEND_START), 0.0, 1.0))
            v[2] = (1.0 - k) * v[2] + k * lengths[i] * w[2] / max(float(np.linalg.norm(w)), 1e-6)
            child = parent + lengths[i] * v / max(float(np.linalg.norm(v)), 1e-9)
        points.append(child)
        new_signs.append(sign)
        parent = child
    return points, tuple(new_signs)


def reconstruct_arm(shoulder, px_elbow, px_wrist, world, signs, lengths, focal, center, blend=False):
    """Elbow and wrist from the shoulder (see reconstruct_chain). Returns (elbow, wrist, signs)."""
    (elbow, wrist), signs = reconstruct_chain(shoulder, (px_elbow, px_wrist), world, signs, lengths, focal, center, blend)
    return elbow, wrist, signs


def body_frame(l_shoulder, r_shoulder, l_hip, r_hip):
    """
    Full body frame for the legs: (rot, origin) with rot rows = (forward, left, up) in camera
    coords and origin at the hip midpoint. Up runs from mid hip to mid shoulder, left is the
    hip line re-orthogonalized against up. None when degenerate.
    """
    origin = 0.5 * (l_hip + r_hip)
    up = 0.5 * (l_shoulder + r_shoulder) - origin
    n = float(np.linalg.norm(up))
    if not np.isfinite(n) or n < 1e-6:
        return None
    up = up / n
    left = (l_hip - r_hip) - float((l_hip - r_hip) @ up) * up
    n = float(np.linalg.norm(left))
    if not np.isfinite(n) or n < 1e-6:
        return None
    left = left / n
    return np.stack([np.cross(left, up), left, up]), origin


def leg_angles(thigh, shin, side):
    """
    Body frame bone vectors (x forward, y left, z up) -> (hip_pitch, hip_roll, knee) in the
    joints.py conventions: + thigh forward, + abduction, + knee flexion. No IK, the robot chain
    (roll then pitch) is inverted directly: roll is the thigh's angle in the frontal plane,
    pitch its angle out of that plane toward the front.
    """
    t, s = thigh / max(float(np.linalg.norm(thigh)), 1e-9), shin / max(float(np.linalg.norm(shin)), 1e-9)
    roll = math.degrees(math.atan2(t[1], -t[2]))
    pitch = math.degrees(math.atan2(t[0], math.hypot(t[1], t[2])))
    knee = math.degrees(math.acos(float(np.clip(t @ s, -1.0, 1.0))))
    return pitch, roll if side == "L" else -roll, knee


def lifted_foot(ankle_z, leg_length):
    """"L" or "R" when that ankle is clearly higher than the other one (body frame z), else None."""
    diff = ankle_z["L"] - ankle_z["R"]
    if abs(diff) <= LIFT_FRAC * leg_length:
        return None
    return "L" if diff > 0 else "R"


def balance(angles, lifted=None):
    """
    Safety layer for {"L": (hip_pitch, hip_roll, knee), "R": ...}. Both feet stay planted:
      1. every angle is cut to the joint table's safe caps,
      2. a lifted foot is read as a single leg stance request: that leg may only differ from
         the stance leg by a small hip pitch and knee change, it is never raised,
      3. hip pitch stays near knee / 2. With the torso upright and the foot flat that keeps
         the hip above the ankle, so a bow at the waist (which the robot cannot do, it has no
         waist joint) does not throw the hips backward.
    """
    out = {s: [joints.safe_angle(i, a) for i, a in zip(joints.LEG_SERVOS[s], angles[s])] for s in "LR"}
    if lifted in out:
        hip, roll, knee = out[lifted]
        ref = out["R" if lifted == "L" else "L"]
        out[lifted] = [min(max(hip, ref[0] - LIFT_HIP), ref[0] + LIFT_HIP), min(roll, max(ref[1], 0.0)),
                       min(max(knee, ref[2]), ref[2] + LIFT_KNEE)]
    for leg in out.values():
        leg[0] = min(max(leg[0], 0.5 * leg[2] - BALANCE_HIP_MARGIN), 0.5 * leg[2] + BALANCE_HIP_MARGIN)
    return {s: tuple(v) for s, v in out.items()}
