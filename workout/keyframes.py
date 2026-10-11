"""
Keyframe demos: arm (and head) poses with a move time, played through the robot API.

The format is checked by workout.plan.check_keyframes. This file is the one place
that turns keyframes into poses over time, so the dashboard's twin preview, its
"play on robot" button and the workout loop's demonstration all move the same way.

A keyframe: { ms, hold_ms?, ease?, servos: {joint: pulse}, head?: {pan, tilt} }.
The animation starts from the stand pose's arms. A joint a keyframe doesn't mention
stays where the previous keyframe left it.
"""

import math
import time

from robot_api.config import MIN_MOVE_MS
from robot_api.servo.joints import HEAD, JOINTS, STAND_POSE
from robot_api.servo.limits import HEAD_CENTER, HEAD_LIMITS, LIMITS
from workout.plan import KEYFRAME_JOINTS

STEP_MS = MIN_MOVE_MS  # the robot API's shortest move; eased motion is sent in steps this long

EASE = {
    "linear": lambda u: u,
    "in": lambda u: u * u,
    "out": lambda u: 1 - (1 - u) ** 2,
    "inout": lambda u: 0.5 - 0.5 * math.cos(math.pi * u),
}


def start_pose() -> dict:
    """Arms hanging as in the stand pose, head centered."""
    return {"servos": {j: STAND_POSE[JOINTS[j]] for j in KEYFRAME_JOINTS},
            "head": {"pan": HEAD_CENTER, "tilt": HEAD_CENTER}}


def mirrored(frames) -> list:
    """The same animation done by the other arm: left and right swapped, and the head turned the other way.
    An exercise with `alternate: true` is one side on one rep and this on the next."""
    out = []
    for kf in frames:
        servos = {}
        for joint, pulse in (kf.get("servos") or {}).items():
            side, part = joint.split("_", 1)
            other = f"{'right' if side == 'left' else 'left'}_{part}"
            a, b = JOINTS[joint], JOINTS[other]
            lo, hi = LIMITS[b]
            servos[other] = max(lo, min(hi, int(round(STAND_POSE[b] - (pulse - STAND_POSE[a])))))
        new = {**kf, "servos": servos}
        if kf.get("head"):
            head = dict(kf["head"])
            if "pan" in head:
                lo, hi = HEAD_LIMITS[HEAD["pan"]]
                head["pan"] = max(lo, min(hi, 2 * HEAD_CENTER - head["pan"]))
            new["head"] = head
        out.append(new)
    return out


def duration_ms(frames) -> int:
    return sum(kf["ms"] + kf.get("hold_ms", 0) for kf in frames)


def pose_at(frames, t_ms: float) -> dict:
    """The pose t_ms into the animation: {"servos": {joint: pulse}, "head": {pan, tilt}}."""
    pose = start_pose()
    t = max(0.0, float(t_ms))
    for kf in frames:
        target = {"servos": {**pose["servos"], **(kf.get("servos") or {})},
                  "head": {**pose["head"], **(kf.get("head") or {})}}
        if t < kf["ms"]:
            u = EASE[kf.get("ease", "inout")](t / kf["ms"])
            return {part: {k: int(round(pose[part][k] + (target[part][k] - pose[part][k]) * u))
                           for k in pose[part]} for part in ("servos", "head")}
        pose = target
        t -= kf["ms"] + kf.get("hold_ms", 0)
        if t < 0:
            break  # holding at this keyframe
    return pose


def steps(frames, speed: float = 1.0, step_ms: int = STEP_MS) -> list:
    """The animation as robot moves: [{"t_ms", "ms", "servos", "head"}]. `t_ms` is the
    animation time the step ends at. Only joints that change are listed, so a hold
    sends nothing."""
    speed = max(0.1, min(2.0, float(speed)))
    total = duration_ms(frames)
    wall = total / speed
    count = max(1, math.ceil(wall / step_ms))
    out, last = [], start_pose()
    for k in range(1, count + 1):
        t_wall = min(wall, k * step_ms)
        pose = pose_at(frames, min(total, t_wall * speed))
        servos = {j: p for j, p in pose["servos"].items() if p != last["servos"][j]}
        head = pose["head"] if pose["head"] != last["head"] else None
        out.append({"t_ms": round(t_wall * speed), "ms": step_ms, "servos": servos, "head": head})
        last = pose
    return out


def play(call, frames, speed: float = 1.0, cancelled=lambda: False, on_step=None,
         sleep=time.sleep) -> bool:
    """Send the animation through `call(path, **kwargs)` (a robot API backend's call).
    Returns False if cancelled() became true part-way. Limits, the speed cap and the
    temperature check all stay in the robot API; a refusal raises its RobotError."""
    for step in steps(frames, speed):
        if cancelled():
            return False
        began = time.monotonic()
        if step["servos"]:
            call("servo/set_many", targets=step["servos"], ms=step["ms"], wait=False)
        if step["head"]:
            call("servo/head", ms=step["ms"], **step["head"])  # blocks for ms
        if on_step:
            on_step(step)
        left = step["ms"] / 1000 - (time.monotonic() - began)
        while left > 0:
            if cancelled():
                return False
            sleep(min(0.05, left))
            left -= 0.05
    return True
