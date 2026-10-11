#!/usr/bin/env python3
"""Fit the coupled arm limit map that robot_api enforces, from the contact captures.

    python scripts/fit_arm_limits.py            # scripts/arm_limits.json -> robot_api/servo/arm_limit_map.json

Each contact capture (scripts/arm_limit_capture.py) is a pose where the arm just touched the body.
At a given shoulder pitch, the captures form a boundary in (elbow bend inward, shoulder roll):
the more the elbow curls in, the less far the shoulder may roll in before the forearm hits.
The map keeps one row per captured pitch with its boundary points; robot_api.servo.arm_limits
interpolates between rows and points at run time.

Everything is stored in the LEFT arm's pulse frame. Right arm pulses are mirrored (1000 - pulse),
which is the same motion on the mirrored servo, so "bigger" always means inward / backward.
"""
import argparse
import json
import os
import statistics
from collections import defaultdict

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
DEFAULT_IN = os.path.join(ROOT, "scripts", "arm_limits.json")
DEFAULT_OUT = os.path.join(ROOT, "robot_api", "servo", "arm_limit_map.json")
CONTACTS = ("rollin_straight", "rollin_bent", "elbow_max")
# Captures known to be wrong: the forearm_up references were taken with the shoulder flipped.
# (They are references, not contacts, so they would be ignored anyway; listed for the record.)
EXCLUDE = {"L_ref_forearm_up", "R_ref_forearm_up"}
DEFAULT_MARGIN = 15  # pulses (~3.6 deg) of roll kept clear of every captured contact


def mirror(side, pulse):
    return pulse if side == "L" else 1000 - pulse


def fit(captures, margin=DEFAULT_MARGIN):
    straight = {}
    for side in "LR":
        ref = captures.get(f"{side}_ref_tpose")
        if ref is None:
            raise SystemExit(f"{side}_ref_tpose (elbow straight reference) is missing")
        straight[side] = ref["arm"]["elbow"]

    groups = defaultdict(list)  # (side, pitch key) -> [(name, pitch, elbow_in, roll)]
    for name, cap in captures.items():
        if name in EXCLUDE or "_ref_" in name:
            continue
        side, pitch_key, contact = name.split("_", 2)
        if contact not in CONTACTS:
            continue
        a = cap["arm"]
        groups[(side, pitch_key)].append((
            name,
            mirror(side, a["pitch"]),
            mirror(side, a["elbow"]) - mirror(side, straight[side]),
            mirror(side, a["roll"]),
        ))

    rows = {"L": [], "R": []}
    for (side, pitch_key), caps in groups.items():
        points = sorted((e, r) for _, _, e, r in caps)
        # A boundary point that allows MORE roll than a point with less elbow bend is inside the
        # safe region of that point, so the boundary is made non-increasing (conservative).
        mono = []
        for e, r in points:
            mono.append([e, min(r, mono[-1][1]) if mono else r])
        rows[side].append({
            "name": pitch_key,
            "pitch": round(statistics.mean(p for _, p, _, _ in caps)),
            "points": mono,
            "from": sorted(n for n, *_ in caps),
        })
    for side in rows:
        rows[side].sort(key=lambda row: row["pitch"])
    return {
        "about": "Coupled arm limits fitted by scripts/fit_arm_limits.py from scripts/arm_limits.json. "
                 "Left-arm pulse frame (right arm mirrored as 1000 - pulse). Each row: shoulder pitch, "
                 "then [elbow bend inward from straight, max shoulder roll inward] boundary points.",
        "margin": margin,
        "elbow_straight": straight,
        "rows": rows,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--captures", default=DEFAULT_IN)
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--margin", type=int, default=DEFAULT_MARGIN)
    args = ap.parse_args()
    with open(args.captures) as f:
        result = fit(json.load(f), args.margin)
    with open(args.out, "w") as f:
        json.dump(result, f, indent=1)
    for side, rows in result["rows"].items():
        for row in rows:
            print(f"{side} {row['name']:7s} pitch {row['pitch']:4d}  " + "  ".join(f"elbow+{e} roll<={r}" for e, r in row["points"]))
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
