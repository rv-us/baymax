"""Arm poses in degrees, turned into servo pulses.

People (and language models) think in degrees: "arms forward to shoulder height". The
robot takes pulses. This is the one place that converts, so the starter animations,
the animation generator and its mock all agree, and all stay inside robot_api's limits.

    arm("left", forward=90)            -> {"left_shoulder_pitch": 350, ...}
    frame(1500, both=dict(forward=90)) -> one keyframe

forward  degrees the arm swings forward and up from hanging (0 = down, 90 = straight
         ahead, 160 = overhead)
side     degrees the arm lifts out to the side (0 = at the side, 90 = a T pose)
elbow    degrees the elbow bends (0 = straight)
turn/nod head degrees left(+)/right(-) and up(+)/down(-)
"""
from __future__ import annotations

from robot_api.servo.joints import HEAD, JOINTS, STAND_POSE
from robot_api.servo.limits import HEAD_CENTER, HEAD_LIMITS, LIMITS

PULSE_PER_DEG = 1000 / 240          # the bus servos turn 240 degrees over 1000 pulses
HEAD_US_PER_DEG = 1 / 0.09          # as digital_twin.mapping (unverified on this robot)
MAX = {"forward": 170, "side": 95, "elbow": 70}
# Which way each left-arm joint's pulse moves as the angle grows. The right arm mirrors.
_LEFT_SIGN = {"shoulder_pitch": -1, "shoulder_roll": -1, "elbow": 1}
_PART = {"forward": "shoulder_pitch", "side": "shoulder_roll", "elbow": "elbow"}


def _clip(value, lo, hi) -> float:
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return 0.0


def arm(side: str, forward: float = 0, side_lift: float = 0, elbow: float = 0) -> dict:
    """One arm's three joints as pulses, clamped to robot_api's limits."""
    out = {}
    for key, deg in (("forward", forward), ("side", side_lift), ("elbow", elbow)):
        part = _PART[key]
        joint = f"{side}_{part}"
        sid = JOINTS[joint]
        sign = _LEFT_SIGN[part] * (1 if side == "left" else -1)
        pulse = STAND_POSE[sid] + sign * _clip(deg, 0, MAX[key]) * PULSE_PER_DEG
        lo, hi = LIMITS[sid]
        out[joint] = int(round(max(lo, min(hi, pulse))))
    return out


def head(turn: float = 0, nod: float = 0) -> dict:
    out = {}
    for axis, deg in (("pan", turn), ("tilt", nod)):
        lo, hi = HEAD_LIMITS[HEAD[axis]]
        out[axis] = int(round(max(lo, min(hi, HEAD_CENTER + _clip(deg, -45, 45) * HEAD_US_PER_DEG)) / 10) * 10)
    return out


def _arm_kwargs(spec: dict | None) -> dict:
    spec = spec or {}
    return {"forward": spec.get("forward", 0), "side_lift": spec.get("side", 0), "elbow": spec.get("elbow", 0)}


def frame(ms: int, both: dict | None = None, left: dict | None = None, right: dict | None = None,
          hold_ms: int = 0, ease: str = "inout", turn: float = 0, nod: float = 0) -> dict:
    """One keyframe. `both` sets the two arms alike; `left` / `right` override a side."""
    servos = {**arm("left", **_arm_kwargs(left if left is not None else both)),
              **arm("right", **_arm_kwargs(right if right is not None else both))}
    kf = {"ms": int(max(300, min(10000, round(ms)))), "ease": ease, "servos": servos}
    if hold_ms:
        kf["hold_ms"] = int(max(0, min(10000, round(hold_ms))))
    if turn or nod:
        kf["head"] = head(turn, nod)
    return kf


REST = dict(forward=0, side=0, elbow=0)


# ---- the move library: each returns keyframes for one repetition -------------------


def front_raise(s=1.0, reach=90):
    return [frame(2000 * s, dict(forward=reach), hold_ms=500), frame(2000 * s, REST, hold_ms=400)]


def side_raise(s=1.0, reach=80):
    return [frame(2000 * s, dict(side=reach), hold_ms=500), frame(2000 * s, REST, hold_ms=400)]


def overhead_reach(s=1.0):
    return [frame(2600 * s, dict(forward=160), hold_ms=600), frame(2600 * s, REST, hold_ms=400)]


def arm_circles(s=1.0):
    return [frame(1100 * s, dict(forward=70, side=10), ease="in"),
            frame(1000 * s, dict(forward=110, side=45), ease="linear"),
            frame(1000 * s, dict(forward=60, side=80), ease="linear"),
            frame(1000 * s, dict(forward=10, side=45), ease="linear"),
            frame(1100 * s, REST, ease="out", hold_ms=300)]


def elbow_curls(s=1.0):
    return [frame(900 * s, dict(forward=25)),
            frame(1500 * s, dict(forward=25, elbow=70), hold_ms=400),
            frame(1500 * s, dict(forward=25, elbow=0), hold_ms=200),
            frame(900 * s, REST)]


def t_hold(s=1.0, hold=4000):
    return [frame(2200 * s, dict(side=90), hold_ms=hold), frame(2200 * s, REST, hold_ms=400)]


def alternating_reach(s=1.0):
    return [frame(2000 * s, left=dict(forward=150), right=REST, hold_ms=400, turn=12),
            frame(1800 * s, REST, hold_ms=200),
            frame(2000 * s, left=REST, right=dict(forward=150), hold_ms=400, turn=-12),
            frame(1800 * s, REST, hold_ms=300)]


def goalpost_stretch(s=1.0):
    return [frame(2000 * s, dict(side=85, elbow=65), hold_ms=1500),
            frame(1600 * s, dict(forward=60, side=30, elbow=65), hold_ms=600),
            frame(1600 * s, dict(side=85, elbow=65), hold_ms=600),
            frame(2000 * s, REST, hold_ms=300)]


def shoulder_press(s=1.0):
    return [frame(1800 * s, dict(side=85, elbow=65), hold_ms=300),
            frame(1800 * s, dict(forward=150, side=20), hold_ms=500),
            frame(1800 * s, dict(side=85, elbow=65), hold_ms=300),
            frame(1600 * s, REST, hold_ms=300)]


def wave(s=1.0, side="right", times=3):
    up, out_, other = dict(forward=150, side=10, elbow=20), dict(forward=150, side=40, elbow=45), REST
    pick = (lambda a: dict(left=other, right=a)) if side == "right" else (lambda a: dict(left=a, right=other))
    frames = [frame(1400 * s, **pick(up), nod=6)]
    for _ in range(max(1, min(5, times))):
        frames += [frame(450 * s, ease="linear", **pick(out_), nod=6),
                   frame(450 * s, ease="linear", **pick(up), nod=6)]
    return frames + [frame(1400 * s, REST)]


def cheer(s=1.0):
    return [frame(1200 * s, dict(forward=155, side=25), nod=10),
            frame(400 * s, dict(forward=155, side=25, elbow=45), ease="linear", nod=10),
            frame(400 * s, dict(forward=155, side=25), ease="linear", nod=10),
            frame(400 * s, dict(forward=155, side=25, elbow=45), ease="linear", nod=10),
            frame(400 * s, dict(forward=155, side=25), ease="linear", hold_ms=400, nod=10),
            frame(1400 * s, REST)]


def hug(s=1.0):
    return [frame(1800 * s, dict(forward=80, side=60)), frame(1600 * s, dict(forward=85, side=5, elbow=55), hold_ms=1200),
            frame(1800 * s, REST)]


def shrug(s=1.0):
    return [frame(900 * s, dict(forward=20, side=25, elbow=60), hold_ms=600, nod=-6), frame(900 * s, REST)]


def clap(s=1.0, times=3):
    apart, together = dict(forward=85, side=25, elbow=30), dict(forward=85, side=0, elbow=45)
    frames = [frame(1300 * s, apart)]
    for _ in range(max(1, min(5, times))):
        frames += [frame(350 * s, together, ease="linear"), frame(350 * s, apart, ease="linear")]
    return frames + [frame(1300 * s, REST)]


def sway(s=1.0):
    return [frame(1500 * s, left=dict(side=45), right=dict(side=10), turn=10),
            frame(1700 * s, left=dict(side=10), right=dict(side=45), turn=-10),
            frame(1500 * s, REST)]
