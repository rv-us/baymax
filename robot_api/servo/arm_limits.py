"""Coupled arm limits: how far each shoulder may roll inward, given its shoulder pitch and elbow.

The per-servo ranges in servo.limits can't stop an arm hitting the body, because the safe roll
depends on the other two arm joints: hanging down with the elbow curled in, the forearm meets the
body long before the roll's own range ends; swung forward, the same roll is fine. The map in
arm_limit_map.json (fitted by scripts/fit_arm_limits.py from captured contact poses) holds, per
captured shoulder pitch, a boundary of (elbow bend inward, max roll inward) points. Between rows
it interpolates on pitch, between points on elbow bend; past the last point every extra pulse of
elbow bend costs a pulse of roll, past the first or last pitch the nearest row applies. The roll
is held MARGIN pulses clear of the boundary.

Only the commanded target is checked, not the path a move takes to get there.
The map is in the left arm's pulse frame; right arm pulses are mirrored (1000 - pulse).
"""
from __future__ import annotations

import json
import os

ARMS = {"L": (8, 7, 6), "R": (16, 15, 14)}  # shoulder pitch, shoulder roll, elbow
MAP_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "arm_limit_map.json")


def load(path: str = MAP_FILE) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None


MAP = load()
MARGIN = int(os.environ.get("BAYMAX_ARM_MARGIN", (MAP or {}).get("margin", 15)))


def _mirror(side: str, pulse: float) -> float:
    return pulse if side == "L" else 1000 - pulse


def _row_limit(points: list, elbow_in: float) -> float:
    if elbow_in <= points[0][0]:
        return points[0][1]
    for (e0, r0), (e1, r1) in zip(points, points[1:]):
        if elbow_in <= e1:
            return r0 if e1 == e0 else r0 + (r1 - r0) * (elbow_in - e0) / (e1 - e0)
    return points[-1][1] - (elbow_in - points[-1][0])


def roll_limit(side: str, pitch: int, elbow: int, limit_map: dict | None = None, margin: int | None = None):
    """Innermost safe shoulder roll pulse for this arm (left: the maximum, right: the minimum),
    or None when there is no map."""
    limit_map = MAP if limit_map is None else limit_map
    if not limit_map:
        return None
    rows = limit_map["rows"][side]
    p = _mirror(side, pitch)
    e = _mirror(side, elbow) - _mirror(side, limit_map["elbow_straight"][side])
    if p <= rows[0]["pitch"]:
        r = _row_limit(rows[0]["points"], e)
    elif p >= rows[-1]["pitch"]:
        r = _row_limit(rows[-1]["points"], e)
    else:
        for a, b in zip(rows, rows[1:]):
            if p <= b["pitch"]:
                t = (p - a["pitch"]) / (b["pitch"] - a["pitch"])
                r = (1 - t) * _row_limit(a["points"], e) + t * _row_limit(b["points"], e)
                break
    r -= MARGIN if margin is None else margin
    return int(_mirror(side, r))


def limit(targets: dict, current: dict, limit_map: dict | None = None) -> tuple[dict, dict]:
    """Apply the coupled limits to {servo id: pulse} targets.

    current: {servo id: pulse or None} for arm servos not in targets. When a move would leave an
    arm's roll past its limit (even if only the pitch or elbow was commanded), the roll is pulled
    out to the limit, added to the move if needed. Returns (targets, {roll id: (wanted, applied)}).
    """
    limit_map = MAP if limit_map is None else limit_map
    out, changed = dict(targets), {}
    if not limit_map:
        return out, changed
    for side, (pitch_id, roll_id, elbow_id) in ARMS.items():
        if not any(sid in targets for sid in ARMS[side]):
            continue
        pitch, roll, elbow = (out.get(sid, current.get(sid)) for sid in ARMS[side])
        if pitch is None or roll is None or elbow is None:
            continue  # unread servo: nothing to judge against
        lim = roll_limit(side, pitch, elbow, limit_map)
        safe = min(roll, lim) if side == "L" else max(roll, lim)
        if safe != roll:
            out[roll_id] = safe
            changed[roll_id] = (int(roll), safe)
    return out, changed
