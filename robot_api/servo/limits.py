"""Per-servo pulse limits.

No servo has firmware angle limits set (all report 0-1000), so these are the
only bounds. They are the range each servo reaches across the 127 factory
action groups: poses the robot is known to survive. They do not prevent
collisions between joints (e.g. shoulder roll inward with the arm at the
side hits the leg).

Regenerate on the robot with: python -m robot_api.servo.limits
"""
from __future__ import annotations

import glob
import os
import sqlite3

LIMITS = {
    1: (395, 761), 2: (50, 819), 3: (0, 960), 4: (125, 857),
    5: (375, 750), 6: (125, 930), 7: (125, 900), 8: (0, 1000),
    9: (366, 570), 10: (140, 950), 11: (40, 1000), 12: (141, 875),
    13: (249, 624), 14: (33, 875), 15: (100, 875), 16: (31, 1000),
    17: (300, 760), 18: (240, 500),
}

# Symmetrical boundaries based on 4.167 ticks/deg
LIMITS.update({
    # Elbows: +-90 deg around neutral (500 +- 375)
    6: (125, 875),
    14: (125, 875),

    # Shoulder Roll: physical bracket protection (~ +-90 deg)
    7: (125, 875),
    15: (125, 875),

    # Shoulder Pitch (running swing): maximum mechanical range (0-1000)
    8: (0, 1000),
    16: (0, 1000),
})

# Head PWM servos (pulse width in us). Range used by Functions/Head_Control.py,
# with tilt extended down to the 984 the factory grab course looks at its feet with.
HEAD_LIMITS = {1: (950, 1800), 2: (1200, 1800)}
HEAD_CENTER = 1500


def clamp(servo_id: int, pulse: int, limits: dict = LIMITS) -> int:
    lo, hi = limits[servo_id]
    return max(lo, min(hi, int(pulse)))


def scan_action_groups(action_dir: str) -> dict:
    """Min/max pulse per servo across every .d6a file in action_dir."""
    lo, hi = {}, {}
    for path in glob.glob(os.path.join(action_dir, "*.d6a")):
        try:
            con = sqlite3.connect(path)
            cols = [r[1] for r in con.execute("pragma table_info(ActionGroup)")]
            for row in con.execute("select * from ActionGroup"):
                for i in range(1, 19):
                    key = f"Servo{i}"
                    if key in cols and row[cols.index(key)] is not None:
                        v = row[cols.index(key)]
                        lo[i] = min(lo.get(i, v), v)
                        hi[i] = max(hi.get(i, v), v)
            con.close()
        except sqlite3.Error:
            continue
    return {i: (lo[i], hi[i]) for i in sorted(lo)}


if __name__ == "__main__":
    from ..config import ACTION_DIR

    scanned = scan_action_groups(ACTION_DIR)
    for i, rng in scanned.items():
        flag = "" if LIMITS.get(i) == rng else f"   (table has {LIMITS.get(i)})"
        print(f"{i:2d}: {rng}{flag}")
