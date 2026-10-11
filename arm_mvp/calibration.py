"""T-pose calibration of bone lengths, stored in calibration.json next to this file."""
import json
import os

import numpy as np

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calibration.json")
L_SHOULDER, R_SHOULDER = 11, 12
BONES = {"upper": {"L": (11, 13), "R": (12, 14)}, "fore": {"L": (13, 15), "R": (14, 16)}}
# Typical bone length / shoulder width for MediaPipe's landmarks (used until calibrated).
# The depth recovery only depends on these ratios, the absolute shoulder width cancels out.
DEFAULT_SHOULDER_M, UPPER_RATIO, FORE_RATIO = 0.36, 0.80, 0.70
MIN_SAMPLES = 10


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


def _in_plane(world, a, b):
    """Fraction of a world-landmark bone that lies in the image plane (1 = facing the camera)."""
    v = world[b] - world[a]
    return max(float(np.hypot(v[0], v[1]) / max(np.linalg.norm(v), 1e-6)), 0.8)


def sample(px, world, vis, min_vis=0.5):
    """
    One T-pose frame: world shoulder width (m), shoulder width (px) and each bone's length as a
    ratio of the shoulder width. px is (33, 2) pixels, world is (33, 3) metres, vis is (33,).

    The ratios are measured in the image (in a T-pose the bones lie in the image plane), with the
    world landmarks only correcting the small leftover foreshortening. The world landmarks' own
    bone lengths are not used: they are not consistent with the pixel scale the solver works in.
    """
    if vis[11:17].min() < min_vis:
        return None
    sh_px = float(np.linalg.norm(px[L_SHOULDER] - px[R_SHOULDER]))
    if sh_px < 10.0:
        return None
    width_px = sh_px / _in_plane(world, R_SHOULDER, L_SHOULDER)
    out = {"shoulder_m": float(np.linalg.norm(world[L_SHOULDER] - world[R_SHOULDER])), "shoulder_px": sh_px}
    for bone, sides in BONES.items():
        for side, (a, b) in sides.items():
            out[bone + side] = float(np.linalg.norm(px[b] - px[a])) / _in_plane(world, a, b) / width_px
    return out


def finish(samples):
    """Median over the hold. Returns a calibration dict, or None if the T-pose was not seen."""
    if len(samples) < MIN_SAMPLES:
        return None
    med = {k: float(np.median([s[k] for s in samples])) for k in samples[0]}
    width = med["shoulder_m"]
    return {"calibrated": True, "shoulder_m": width, "shoulder_px": med["shoulder_px"],
            "upper": {s: med["upper" + s] * width for s in "LR"},
            "fore": {s: med["fore" + s] * width for s in "LR"}}
