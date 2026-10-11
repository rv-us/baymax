"""The starter animations, written from the pose helper so they stay inside the limits.

    python -m dashboard.starter            # (re)write animations/<id>.json for each starter

Squats and single-leg balance are left out on purpose: workout/plan.py refuses leg
keyframes because moving one leg joint at a time makes this robot fall. Everything
here is upper body.
"""
from __future__ import annotations

from . import poses, store
from .config import Settings


def _exercise(cue, track="shoulder_elevation", top=90, bottom=15, tolerance=15, sets=2, reps=8):
    return {"cue": cue, "sets": sets, "reps": reps, "mirror": "arms", "track_joint": track,
            "targets": {track: {"top": top, "bottom": bottom, "tolerance": tolerance}}}


# id -> animation. The first three ship as files already; they are not rewritten here.
STARTERS = {
    "arm_circles": {
        "name": "Arm circles", "kind": "exercise", "keyframes": poses.arm_circles(),
        "description": "Slow circles with both arms: forward, up, out to the side and down.",
        "exercise": _exercise("Draw a slow, wide circle with both arms: forward, up, out to the side, and down.",
                              top=100, bottom=10, tolerance=20, reps=6)},
    "elbow_curls": {
        "name": "Elbow curls", "kind": "exercise", "keyframes": poses.elbow_curls(),
        "description": "Bend both elbows up, then straighten them slowly.",
        "exercise": _exercise("Keep your upper arms still, bend your elbows up, then straighten them slowly.",
                              track="elbow", top=70, bottom=5, tolerance=15)},
    "t_hold": {
        "name": "T hold", "kind": "exercise", "keyframes": poses.t_hold(),
        "description": "Lift both arms out to the side and hold them level for four seconds.",
        "exercise": _exercise("Lift both arms out to the side like a letter T, hold them still, then lower.",
                              top=90, bottom=10, tolerance=15, reps=4)},
    "alternating_reach": {
        "name": "Alternating reach", "kind": "exercise", "keyframes": poses.alternating_reach(),
        "description": "Reach one arm up overhead, lower it, then the other.",
        "exercise": _exercise("Reach one arm up toward the ceiling, lower it, then reach with the other.",
                              top=150, bottom=15, tolerance=20, reps=6)},
    "goalpost_stretch": {
        "name": "Goalpost stretch", "kind": "exercise", "keyframes": poses.goalpost_stretch(),
        "description": "Arms out with elbows bent like goalposts, then bring them forward and open again.",
        "exercise": _exercise("Hold your arms like goalposts, bring them together in front, then open wide again.",
                              top=85, bottom=10, tolerance=20, reps=5)},
    "shoulder_press": {
        "name": "Shoulder press", "kind": "exercise", "keyframes": poses.shoulder_press(),
        "description": "From goalposts, press both hands up overhead and come back down.",
        "exercise": _exercise("Start with your hands by your shoulders, press them up overhead, then lower slowly.",
                              top=150, bottom=60, tolerance=20, reps=6)},
    "wave_hello": {
        "name": "Wave hello", "kind": "emote", "keyframes": poses.wave(),
        "description": "A friendly wave with the right hand."},
    "cheer": {
        "name": "Cheer", "kind": "emote", "keyframes": poses.cheer(),
        "description": "Both arms up for a job well done."},
    "gentle_hug": {
        "name": "Gentle hug", "kind": "emote", "keyframes": poses.hug(),
        "description": "Arms open wide, then close in a hug."},
}


def write(directory: str, overwrite: bool = True) -> list:
    animations = store.Animations(directory)
    have = {a["id"] for a in animations.list()}
    written = []
    for aid, anim in STARTERS.items():
        if aid in have and not overwrite:
            continue
        animations.save({**anim, "source": "starter"}, aid)
        written.append(aid)
    return written


if __name__ == "__main__":
    print("wrote", ", ".join(write(Settings().animations_dir)))
