"""
Automatic bone length calibration, stored in calibration.json next to this file.

Samples are collected continuously, but only on frames where the measurement is trustworthy:
a straight limb lying in the image plane, shoulders square to the camera, everything visible
and still. Standing still facing the camera with arms and legs straight calibrates everything,
a T-pose passes too. Arms and legs lock separately (legs are often out of frame), each at
LOCK_SAMPLES samples per side, and are then refined slowly from the rolling buffer.
"""
import json
import os
from collections import deque

import numpy as np

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calibration.json")
L_SHOULDER, R_SHOULDER = 11, 12
# Per limb: the bones (calibration.json keys) along a landmark chain that starts at the shoulder.
LIMBS = {
    "arm": {"bones": ("upper", "fore"), "chain": {"L": (11, 13, 15), "R": (12, 14, 16)}, "flag": "calibrated"},
    "leg": {"bones": ("torso", "thigh", "shin"), "chain": {"L": (11, 23, 25, 27), "R": (12, 24, 26, 28)},
            "flag": "legs_calibrated"},
}
# Typical bone length / shoulder width for MediaPipe's landmarks (used until calibrated).
# The depth recovery only depends on these ratios, the absolute shoulder width cancels out.
DEFAULT_SHOULDER_M = 0.36
RATIOS = {"upper": 0.80, "fore": 0.70, "torso": 1.40, "thigh": 1.15, "shin": 1.10}

LOCK_SAMPLES = 45        # valid samples per side before a limb's calibration locks
BUFFER = 150             # rolling buffer per side
MAX_STEP = 0.03          # after locking, one sample moves a length by at most 3 percent
MIN_JOINT_DEG = 160.0    # gate a: limb nearly straight (elbow or knee)
MAX_BONE_DZ = 0.25       # gate b: |dz| of each bone under 25 percent of its length
MAX_SHOULDER_DZ = 0.20   # gate c: shoulder line within about 11 degrees of the image plane
MIN_VIS = 0.6            # gate d: every landmark of the chain, and both shoulders
STILL_S, STILL_FRAC = 0.3, 0.05  # gate e: moved under 5 percent of the shoulder width in 0.3 s


def default_calibration(shoulder_m=DEFAULT_SHOULDER_M):
    cal = {"calibrated": False, "legs_calibrated": False, "shoulder_m": shoulder_m, "shoulder_px": None}
    cal.update({bone: {s: ratio * shoulder_m for s in "LR"} for bone, ratio in RATIOS.items()})
    return cal


def load(path=PATH):
    """Saved calibration, or the defaults. A file from the arms only version keeps its arm
    lengths and gets default leg lengths scaled to its shoulder width."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            saved = json.load(f)
        cal = default_calibration(float(saved["shoulder_m"]))
        cal["shoulder_px"] = saved.get("shoulder_px")
        for limb in LIMBS.values():
            vals = [saved.get(b, {}).get(s) for b in limb["bones"] for s in "LR"]
            if all(isinstance(v, (int, float)) and np.isfinite(v) and v > 0.05 for v in vals):
                cal.update({b: dict(saved[b]) for b in limb["bones"]})
                cal[limb["flag"]] = limb["flag"] == "calibrated" or bool(saved.get(limb["flag"]))
        if cal["calibrated"] and cal["shoulder_m"] > 0.05:
            return cal
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return default_calibration()


def save(cal, path=PATH):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cal, f, indent=2)


def new_state(cal=None):
    """Calibration state. Pass a loaded calibration to start locked, nothing to start from scratch."""
    cal = cal or default_calibration()
    return {"cal": cal, "hist": deque(), "locked": {name: bool(cal[limb["flag"]]) for name, limb in LIMBS.items()},
            "buf": {name: {s: deque(maxlen=BUFFER) for s in "LR"} for name in LIMBS}}


def progress(state, limb="arm"):
    return min(LOCK_SAMPLES, *(len(b) for b in state["buf"][limb].values()))


def matrix_rows(done, total=LOCK_SAMPLES, width=16, height=8):
    """Progress bar bitmap for robot_api sensors/matrix/rows (16x8 LED matrix)."""
    lit = int(np.clip(round(width * done / total), 0, width))
    return ["1" * lit + "0" * (width - lit)] * height


def _dz_frac(world, a, b):
    v = world[b] - world[a]
    return abs(float(v[2])) / max(float(np.linalg.norm(v)), 1e-6)


def pose_ok(side, world, vis, limb="arm"):
    """Gates a to d for one limb, from the world landmarks (33, 3) and visibility (33,)."""
    chain = list(LIMBS[limb]["chain"][side])
    used = chain + [L_SHOULDER, R_SHOULDER]
    if vis[used].min() <= MIN_VIS or not np.all(np.isfinite(world[used])):
        return False
    a, mid, b = chain[-3:]  # shoulder, elbow, wrist or hip, knee, ankle
    u, f = world[a] - world[mid], world[b] - world[mid]
    cos = float(u @ f) / max(float(np.linalg.norm(u) * np.linalg.norm(f)), 1e-9)
    return (np.degrees(np.arccos(np.clip(cos, -1.0, 1.0))) > MIN_JOINT_DEG
            and all(_dz_frac(world, p, q) < MAX_BONE_DZ for p, q in zip(chain, chain[1:]))
            and _dz_frac(world, R_SHOULDER, L_SHOULDER) < MAX_SHOULDER_DZ)


def _still(hist, chain, px, t):
    """Gate e: the limb's landmarks stayed put (in pixels) for the last STILL_S seconds."""
    if not hist or hist[0][0] > t - STILL_S:
        return False
    limit = STILL_FRAC * float(np.linalg.norm(px[L_SHOULDER] - px[R_SHOULDER]))
    return all(np.linalg.norm(old[list(chain)] - px[list(chain)], axis=1).max() < limit for _, old in hist)


def measure(side, px, world, limb="arm"):
    """
    (bone lengths in metres ..., shoulder m, shoulder px) for one gated frame. The shoulder
    width comes from the world landmarks and sets the metres per pixel scale together with its
    pixel width. Bone lengths are pixel lengths in that scale (the gates put the bones in the
    image plane), with the world landmarks correcting the small leftover foreshortening. That
    keeps them consistent with the pixel scale the depth solver works in.
    """
    def flat_px(a, b):  # pixel length a bone would have if it lay exactly in the image plane
        return float(np.linalg.norm(px[b] - px[a])) / np.sqrt(1.0 - _dz_frac(world, a, b) ** 2)

    chain = LIMBS[limb]["chain"][side]
    shoulder_m = float(np.linalg.norm(world[L_SHOULDER] - world[R_SHOULDER]))
    scale = shoulder_m / max(flat_px(R_SHOULDER, L_SHOULDER), 1e-6)
    return (*(flat_px(a, b) * scale for a, b in zip(chain, chain[1:])), shoulder_m,
            float(np.linalg.norm(px[L_SHOULDER] - px[R_SHOULDER])))


def _toward(current, target):
    return float(np.clip(target, current * (1.0 - MAX_STEP), current * (1.0 + MAX_STEP)))


def update(state, px, world, vis, t):
    """
    Feed one frame (px (33, 2) pixels, world (33, 3) metres, vis (33,), or px=None when nobody
    is seen). Collects gated samples, locks each limb at LOCK_SAMPLES per side, then refines.
    Returns the list of limbs that locked on this frame.
    """
    hist, cal, locked_now = state["hist"], state["cal"], []
    if px is None:
        hist.clear()
        return locked_now
    while len(hist) > 1 and hist[1][0] <= t - STILL_S:  # keep one entry at least STILL_S old
        hist.popleft()
    for name, limb in LIMBS.items():
        buf, bones = state["buf"][name], limb["bones"]
        for side in "LR":
            if pose_ok(side, world, vis, name) and _still(hist, limb["chain"][side], px, t):
                buf[side].append(measure(side, px, world, name))
        full = [s for s in "LR" if len(buf[s]) >= LOCK_SAMPLES]
        if not full or (not state["locked"][name] and len(full) < 2):
            continue
        med = {s: np.median(np.array(buf[s]), axis=0) for s in full}
        width_m, width_px = (float(np.median(np.concatenate([np.array(buf[s])[:, k] for s in full])))
                             for k in (len(bones), len(bones) + 1))
        if not state["locked"][name]:
            if not any(state["locked"].values()):  # first lock: the other limb's defaults follow the new width
                for other in RATIOS:
                    cal[other] = {s: v * width_m / cal["shoulder_m"] for s, v in cal[other].items()}
                cal["shoulder_m"] = width_m
            for k, bone in enumerate(bones):
                cal[bone] = {s: float(med[s][k]) for s in "LR"}
            state["locked"][name] = cal[limb["flag"]] = True
            locked_now.append(name)
        else:
            for k, bone in enumerate(bones):
                for s in full:
                    cal[bone][s] = _toward(cal[bone][s], med[s][k])
        if name == "arm" or not state["locked"]["arm"]:  # the arm buffer owns the shoulder width once locked
            cal["shoulder_m"], cal["shoulder_px"] = _toward(cal["shoulder_m"], width_m), width_px
    hist.append((t, px.copy()))
    return locked_now
