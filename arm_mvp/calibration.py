"""
Automatic bone length calibration, stored in calibration.json next to this file.

Samples are collected continuously, but only on frames where the measurement is trustworthy:
a straight arm lying in the image plane, shoulders square to the camera, everything visible
and still. Arms hanging at the sides while facing the camera is the main calibration pose, a
T-pose passes too. The calibration locks at LOCK_SAMPLES samples per side and is then refined
slowly from the rolling buffer.
"""
import json
import os
from collections import deque

import numpy as np

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calibration.json")
L_SHOULDER, R_SHOULDER = 11, 12
CHAIN = {"L": (11, 13, 15), "R": (12, 14, 16)}
# Typical bone length / shoulder width for MediaPipe's landmarks (used until calibrated).
# The depth recovery only depends on these ratios, the absolute shoulder width cancels out.
DEFAULT_SHOULDER_M, UPPER_RATIO, FORE_RATIO = 0.36, 0.80, 0.70

# The gates decide whether a frame shows someone standing square to the camera with their arms at their
# sides (or in a T), well enough to measure bones from. They are forgiving on purpose: landmark noise
# bends a straight arm by 10 to 25 degrees, tilts the shoulder line and makes a still person look like
# they are moving, and a gate that is too strict never lets the calibration lock.
LOCK_SAMPLES = 30        # valid samples per side before the calibration locks (the median shrugs off the odd bad one)
BUFFER = 150             # rolling buffer per side
MAX_STEP = 0.03          # after locking, one sample moves a length by at most 3 percent
MIN_ELBOW_DEG = 150.0    # gate a: arm nearly straight (within 30 degrees)
MAX_BONE_DZ = 0.35       # gate b: |dz| of each bone under 35 percent of its length
MAX_SHOULDER_DZ = 0.30   # gate c: shoulder line within about 17 degrees of the image plane
MIN_VIS = 0.5            # gate d: every landmark of the arm chain (the same floor the tracker uses)
STILL_S, STILL_FRAC = 0.3, 0.08  # gate e: moved under 8 percent of the shoulder width in 0.3 s


def default_calibration(shoulder_m=DEFAULT_SHOULDER_M):
    return {"calibrated": False, "shoulder_m": shoulder_m, "shoulder_px": None,
            "upper": {s: UPPER_RATIO * shoulder_m for s in "LR"},
            "fore": {s: FORE_RATIO * shoulder_m for s in "LR"}}


def load(path=PATH):
    try:
        with open(path, "r", encoding="utf-8") as f:
            cal = json.load(f)
        vals = [cal["shoulder_m"]] + [cal[b][s] for b in ("upper", "fore") for s in "LR"]
        if all(np.isfinite(v) and v > 0.05 for v in vals):
            return {**cal, "calibrated": True}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return default_calibration()


def save(cal, path=PATH):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cal, f, indent=2)


def new_state(cal=None):
    """Calibration state. Pass a loaded calibration to start locked, nothing to start from scratch."""
    cal = cal or default_calibration()
    return {"cal": cal, "locked": bool(cal["calibrated"]), "hist": deque(),
            "buf": {s: deque(maxlen=BUFFER) for s in "LR"}}


def progress(state):
    return min(LOCK_SAMPLES, *(len(b) for b in state["buf"].values()))


def matrix_rows(done, total=LOCK_SAMPLES, width=16, height=8):
    """Progress bar bitmap for robot_api sensors/matrix/rows (16x8 LED matrix)."""
    lit = int(np.clip(round(width * done / total), 0, width))
    return ["1" * lit + "0" * (width - lit)] * height


def _dz_frac(world, a, b):
    v = world[b] - world[a]
    return abs(float(v[2])) / max(float(np.linalg.norm(v)), 1e-6)


def pose_ok(side, world, vis):
    """Gates a to d for one arm, from the world landmarks (33, 3) and visibility (33,)."""
    sh, el, wr = CHAIN[side]
    if vis[[sh, el, wr]].min() <= MIN_VIS or not np.all(np.isfinite(world[[11, 12, el, wr]])):
        return False
    u, f = world[sh] - world[el], world[wr] - world[el]
    cos = float(u @ f) / max(float(np.linalg.norm(u) * np.linalg.norm(f)), 1e-9)
    return (np.degrees(np.arccos(np.clip(cos, -1.0, 1.0))) > MIN_ELBOW_DEG
            and _dz_frac(world, sh, el) < MAX_BONE_DZ and _dz_frac(world, el, wr) < MAX_BONE_DZ
            and _dz_frac(world, R_SHOULDER, L_SHOULDER) < MAX_SHOULDER_DZ)


def _still(hist, side, px, t):
    """Gate e: the arm's landmarks stayed put (in pixels) for the last STILL_S seconds."""
    if not hist or hist[0][0] > t - STILL_S:
        return False
    idx = [i - 11 for i in CHAIN[side]]
    limit = STILL_FRAC * float(np.linalg.norm(px[L_SHOULDER] - px[R_SHOULDER]))
    return all(np.linalg.norm(old[idx] - px[11:17][idx], axis=1).max() < limit for _, old in hist)


def measure(side, px, world):
    """
    (upper m, forearm m, shoulder m, shoulder px) for one gated frame. The shoulder width comes
    from the world landmarks and sets the metres per pixel scale together with its pixel width.
    Bone lengths are pixel lengths in that scale (the gates put the bones in the image plane),
    with the world landmarks correcting the small leftover foreshortening. That keeps them
    consistent with the pixel scale the depth solver works in.
    """
    def flat_px(a, b):  # pixel length a bone would have if it lay exactly in the image plane
        return float(np.linalg.norm(px[b] - px[a])) / np.sqrt(1.0 - _dz_frac(world, a, b) ** 2)

    sh, el, wr = CHAIN[side]
    shoulder_m = float(np.linalg.norm(world[L_SHOULDER] - world[R_SHOULDER]))
    scale = shoulder_m / max(flat_px(R_SHOULDER, L_SHOULDER), 1e-6)
    return (flat_px(sh, el) * scale, flat_px(el, wr) * scale, shoulder_m,
            float(np.linalg.norm(px[L_SHOULDER] - px[R_SHOULDER])))


def _toward(current, target):
    return float(np.clip(target, current * (1.0 - MAX_STEP), current * (1.0 + MAX_STEP)))


def update(state, px, world, vis, t):
    """
    Feed one frame (px (33, 2) pixels, world (33, 3) metres, vis (33,), or px=None when nobody
    is seen). Collects gated samples, locks at LOCK_SAMPLES per side, then refines. Returns
    True on the frame the calibration locks.
    """
    hist, buf = state["hist"], state["buf"]
    if px is None:
        hist.clear()
        return False
    while len(hist) > 1 and hist[1][0] <= t - STILL_S:  # keep one entry at least STILL_S old
        hist.popleft()
    for side in "LR":
        if pose_ok(side, world, vis) and _still(hist, side, px, t):
            buf[side].append(measure(side, px, world))
    hist.append((t, px[11:17].copy()))

    full = [s for s in "LR" if len(buf[s]) >= LOCK_SAMPLES]
    if not full or (not state["locked"] and len(full) < 2):
        return False
    med = {s: np.median(np.array(buf[s]), axis=0) for s in full}
    width_m, width_px = (float(np.median(np.concatenate([np.array(buf[s])[:, k] for s in full]))) for k in (2, 3))
    cal, just_locked = state["cal"], not state["locked"]
    if just_locked:
        cal = state["cal"] = {"calibrated": True, "shoulder_m": width_m, "shoulder_px": width_px,
                              "upper": {s: float(med[s][0]) for s in "LR"}, "fore": {s: float(med[s][1]) for s in "LR"}}
        state["locked"] = True
    else:
        cal["shoulder_m"], cal["shoulder_px"] = _toward(cal["shoulder_m"], width_m), width_px
        for s in full:
            cal["upper"][s], cal["fore"][s] = _toward(cal["upper"][s], med[s][0]), _toward(cal["fore"][s], med[s][1])
    return just_locked
