"""
Joint angles and "is the patient in view" from MediaPipe pose landmarks.

Angles use the 3D world landmarks (meters, from MediaPipe), so they don't depend on
how far the patient stands from the camera. Left and right are the patient's own.

    shoulder_elevation   angle between the upper arm and the torso's center line (mid-shoulder
                         to mid-hip, so shoulder width doesn't tilt it): 0 = arm hanging down,
                         90 = out in front or to the side, 180 = overhead
    elbow, knee          interior angle: 180 = straight
    trunk_lean           how far the torso tilts from upright in the image, degrees
    hip_shift_cm         sideways offset of the hips over the feet; + = toward the patient's left

An angle is None when a landmark it needs isn't visible enough.
"""

import math

import numpy as np

NOSE = 0
L_SHOULDER, R_SHOULDER, L_ELBOW, R_ELBOW, L_WRIST, R_WRIST = 11, 12, 13, 14, 15, 16
L_HIP, R_HIP, L_KNEE, R_KNEE, L_ANKLE, R_ANKLE = 23, 24, 25, 26, 27, 28

SIDES = {
    "left": {"shoulder": L_SHOULDER, "elbow": L_ELBOW, "wrist": L_WRIST,
             "hip": L_HIP, "knee": L_KNEE, "ankle": L_ANKLE},
    "right": {"shoulder": R_SHOULDER, "elbow": R_ELBOW, "wrist": R_WRIST,
              "hip": R_HIP, "knee": R_KNEE, "ankle": R_ANKLE},
}
UPPER_BODY = {"left shoulder": L_SHOULDER, "right shoulder": R_SHOULDER,
              "left elbow": L_ELBOW, "right elbow": R_ELBOW,
              "left wrist": L_WRIST, "right wrist": R_WRIST,
              "left hip": L_HIP, "right hip": R_HIP}
FULL_BODY = {**UPPER_BODY, "left knee": L_KNEE, "right knee": R_KNEE,
             "left ankle": L_ANKLE, "right ankle": R_ANKLE}

MIN_VISIBILITY = 0.5
EDGE = 0.01  # landmarks this close to the image edge count as cut off


def _angle(a, b) -> float:
    """Angle between two vectors, degrees."""
    na, nb = np.linalg.norm(a), np.linalg.norm(b)
    if na < 1e-9 or nb < 1e-9:
        return float("nan")
    return math.degrees(math.acos(float(np.clip(np.dot(a, b) / (na * nb), -1.0, 1.0))))


def joint_angles(landmarks, world, width: int = 640, height: int = 480,
                 min_vis: float = MIN_VISIBILITY) -> dict:
    """landmarks: 33 x [x, y, z, visibility] (x, y normalized to the image).
    world: 33 x [x, y, z] in meters. Returns {name: degrees or None}."""
    lm = np.asarray(landmarks, float)
    w = np.asarray(world, float)
    seen = lambda *ids: all(lm[i, 3] >= min_vis for i in ids)
    out = {}
    torso_ok = seen(L_SHOULDER, R_SHOULDER, L_HIP, R_HIP)
    down = ((w[L_HIP] + w[R_HIP]) - (w[L_SHOULDER] + w[R_SHOULDER])) / 2  # shoulders -> hips
    for side, j in SIDES.items():
        s, e, wr, h, k, a = (j[n] for n in ("shoulder", "elbow", "wrist", "hip", "knee", "ankle"))
        out[f"{side}_shoulder_elevation"] = (
            _angle(w[e] - w[s], down) if torso_ok and seen(e) else None)
        out[f"{side}_elbow"] = _angle(w[s] - w[e], w[wr] - w[e]) if seen(s, e, wr) else None
        out[f"{side}_knee"] = _angle(w[h] - w[k], w[a] - w[k]) if seen(h, k, a) else None

    if torso_ok:
        # In pixels, so a non-square image doesn't skew the angle. Image y points down.
        scale = np.array([width, height])
        mid_sh = (lm[L_SHOULDER, :2] + lm[R_SHOULDER, :2]) / 2 * scale
        mid_hip = (lm[L_HIP, :2] + lm[R_HIP, :2]) / 2 * scale
        out["trunk_lean"] = _angle(mid_sh - mid_hip, np.array([0.0, -1.0]))
    else:
        out["trunk_lean"] = None

    if seen(L_HIP, R_HIP, L_ANKLE, R_ANKLE):
        across = w[L_HIP] - w[R_HIP]  # toward the patient's left
        offset = (w[L_HIP] + w[R_HIP]) / 2 - (w[L_ANKLE] + w[R_ANKLE]) / 2
        out["hip_shift_cm"] = float(np.dot(offset, across / np.linalg.norm(across)) * 100)
    else:
        out["hip_shift_cm"] = None

    return {k: (None if v is None or math.isnan(v) else round(v, 1)) for k, v in out.items()}


def in_view(landmarks, min_vis: float = MIN_VISIBILITY) -> dict:
    """Whether the upper body (arm exercises) and the full body (leg exercises) are in view.
    A part counts when it's visible enough and not at the image edge."""
    lm = np.asarray(landmarks, float)

    def ok(i):
        x, y, _, vis = lm[i]
        return vis >= min_vis and EDGE <= x <= 1 - EDGE and EDGE <= y <= 1 - EDGE

    missing = [name for name, i in FULL_BODY.items() if not ok(i)]
    return {"upper_body": not any(n in missing for n in UPPER_BODY),
            "full_body": not missing, "missing": missing}
