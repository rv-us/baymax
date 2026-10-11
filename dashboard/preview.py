"""What an exercise looks like as arm keyframes, for the twin preview and the live mirror.

The twin and the robot both play keyframes (workout/keyframes.py). A plan's demo may be
keyframes, a built-in `arm` raise, or a factory action group; the first two can be drawn
here. An action group is a recorded file on the robot with no known arm path, so it has
no preview and contributes nothing to the loop.
"""
from __future__ import annotations

from workout.keyframes import duration_ms, mirrored, pose_at

from . import poses

GAP_MS = 700  # a rest between exercises in the looping preview


def demo_frames(demo: dict) -> list:
    """One repetition of the demo as keyframes ([] if it can't be drawn)."""
    if "keyframes" in demo:
        return demo["keyframes"]
    if "arm" in demo:
        return poses.front_raise(reach=160 if demo["arm"] == "up" else 90)
    return []


def session_pose(exercises: list, t_ms: float) -> dict:
    """The pose `t_ms` into the whole session played end to end and looped, plus which
    exercise it is in. {"pose", "index", "total_ms"}; pose is None when nothing can be drawn."""
    parts = []
    for i, ex in enumerate(exercises):
        demo = ex.get("demo") or {}
        once = demo_frames(demo)
        if once and demo.get("alternate"):
            once = once + mirrored(once)
        if once:
            frames = once * max(1, int(demo.get("times", 1))) + [poses.frame(GAP_MS, poses.REST)]
            parts.append((i, frames, duration_ms(frames)))
    total = sum(d for _, _, d in parts)
    if not total:
        return {"pose": None, "index": None, "total_ms": 0}
    t = float(t_ms) % total
    for i, frames, d in parts:
        if t < d:
            return {"pose": pose_at(frames, t), "index": i, "total_ms": total}
        t -= d
    return {"pose": None, "index": None, "total_ms": total}
