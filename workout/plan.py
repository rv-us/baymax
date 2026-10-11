"""
Workout plans: a YAML file listing the exercises for one session (runs on the GX10).

    name: Arms, basic
    exercises:
      - name: Front arm raises
        cue: Lift both arms straight out in front of you to shoulder height, then lower them slowly.
        demo: { arm: forward, times: 2 }      # or { action_group: wave, times: 1 }
        sets: 2
        reps: 8
        mirror: arms                          # arms | none
        track_joint: shoulder_elevation       # the joint whose cycles count as reps
        targets:                              # form checks, in degrees
          shoulder_elevation: { top: 90, bottom: 15, tolerance: 15 }
          elbow: { min: 150 }

`demo` is one of: `arm` (down | forward | up, through the robot API's actions/arm,
moving both arms there and back), a factory `action_group` from the safe list, or
`keyframes` recorded in the dashboard (workout/keyframes.py):

        demo:
          times: 1
          keyframes:
            - { ms: 1500, ease: inout, servos: { left_shoulder_pitch: 350, right_shoulder_pitch: 650 } }
            - { ms: 1500, hold_ms: 500, servos: { left_shoulder_pitch: 725, right_shoulder_pitch: 275 } }

Add `alternate: true` to a keyframes demo when the keyframes are one side's rep: the demonstration does
that side, then the other, and the robot mirrors the patient with one side on one rep and the other on
the next (alternating reach). `ms` is the time to move into the keyframe, `hold_ms` a pause there. Only arm joints
and the head are allowed: moving one leg joint at a time makes the robot fall.
`tempo_s` (optional) is the seconds one rep should take; pacing is scored against it.
"""

import os
from dataclasses import dataclass, field

import yaml

PLANS_DIR = os.path.join(os.path.dirname(__file__), "plans")
ARM_POSITIONS = ("forward", "up")
MIRROR_MODES = ("arms", "none")
JOINTS = ("shoulder_elevation", "elbow", "knee", "trunk_lean", "hip_shift")
KEYFRAME_JOINTS = ("left_shoulder_pitch", "left_shoulder_roll", "left_elbow",
                   "right_shoulder_pitch", "right_shoulder_roll", "right_elbow")
EASINGS = ("linear", "in", "out", "inout")
MAX_KEYFRAMES = 60
MAX_KEYFRAME_MS = 10000
DEFAULT_TEMPO_S = 4.0


class PlanError(ValueError):
    pass


@dataclass
class Exercise:
    name: str
    cue: str
    demo: dict
    sets: int
    reps: int
    mirror: str
    track_joint: str
    targets: dict = field(default_factory=dict)
    tempo_s: float = DEFAULT_TEMPO_S


@dataclass
class Plan:
    name: str
    exercises: list


def load_plan(path: str) -> Plan:
    """Read and check a plan. A bare name loads workout/plans/<name>.yaml."""
    if os.sep not in path and not path.endswith((".yaml", ".yml")):
        path = os.path.join(PLANS_DIR, path + ".yaml")
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    return parse_plan(data, source=path)


def parse_plan(data: dict, source: str = "plan") -> Plan:
    if not isinstance(data, dict) or not data.get("exercises"):
        raise PlanError(f"{source}: needs a non-empty 'exercises' list")
    exercises = [_exercise(e, f"{source}: exercise {i + 1}")
                 for i, e in enumerate(data["exercises"])]
    return Plan(str(data.get("name") or os.path.basename(source)), exercises)


def _exercise(e: dict, where: str) -> Exercise:
    if not isinstance(e, dict):
        raise PlanError(f"{where}: must be a mapping")
    missing = [k for k in ("name", "cue", "demo", "sets", "reps", "track_joint") if k not in e]
    if missing:
        raise PlanError(f"{where}: missing {', '.join(missing)}")
    where = f"{where} ({e['name']})"
    for k in ("sets", "reps"):
        if not isinstance(e[k], int) or not 1 <= e[k] <= 50:
            raise PlanError(f"{where}: {k} must be a whole number from 1 to 50")
    mirror = e.get("mirror", "none")
    if mirror not in MIRROR_MODES:
        raise PlanError(f"{where}: mirror must be one of {MIRROR_MODES}")
    if e["track_joint"] not in JOINTS:
        raise PlanError(f"{where}: track_joint must be one of {JOINTS}")
    targets = e.get("targets") or {}
    unknown = [j for j in targets if j not in JOINTS]
    if unknown:
        raise PlanError(f"{where}: unknown target joints {unknown}")
    tempo = e.get("tempo_s", DEFAULT_TEMPO_S)
    if isinstance(tempo, bool) or not isinstance(tempo, (int, float)) or not 1 <= tempo <= 30:
        raise PlanError(f"{where}: tempo_s must be 1 to 30 seconds")
    _check_demo(e["demo"], where)
    return Exercise(e["name"], e["cue"], dict(e["demo"]), e["sets"], e["reps"], mirror,
                    e["track_joint"], targets, float(tempo))


def _check_demo(demo, where: str):
    kinds = [k for k in ("arm", "action_group", "keyframes") if isinstance(demo, dict) and k in demo]
    if len(kinds) != 1:
        raise PlanError(f"{where}: demo needs exactly one of 'arm', 'action_group' or 'keyframes'")
    if "keyframes" in demo:
        check_keyframes(demo["keyframes"], where)
    if "arm" in demo and demo["arm"] not in ARM_POSITIONS:
        raise PlanError(f"{where}: demo arm must be one of {ARM_POSITIONS}")
    if "action_group" in demo:
        from robot_api.actions.groups import SAFE_GROUPS  # pure data; no hardware import

        if demo["action_group"] not in SAFE_GROUPS:
            raise PlanError(f"{where}: action group {demo['action_group']!r} is not on the safe "
                            "list (robot_api/actions/groups.py)")
    times = demo.get("times", 1)
    if not isinstance(times, int) or not 1 <= times <= 5:
        raise PlanError(f"{where}: demo times must be 1 to 5")
    if "alternate" in demo and (not isinstance(demo["alternate"], bool) or "keyframes" not in demo):
        raise PlanError(f"{where}: demo alternate must be true or false, and only goes with keyframes")


def _whole(value, lo: int, hi: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi


def check_keyframes(frames, where: str = "keyframes"):
    """Check a keyframe list (the demo format and the dashboard's animation files).
    Pulses outside the robot API's limits are rejected here, not clamped, so a saved
    animation is exactly what the robot will be asked to do."""
    from robot_api.config import MIN_MOVE_MS                    # pure data; no hardware import
    from robot_api.servo.joints import HEAD, JOINTS as SERVO_IDS
    from robot_api.servo.limits import HEAD_LIMITS, LIMITS

    if not isinstance(frames, list) or not 1 <= len(frames) <= MAX_KEYFRAMES:
        raise PlanError(f"{where}: keyframes must be a list of 1 to {MAX_KEYFRAMES}")
    for i, kf in enumerate(frames, start=1):
        at = f"{where}: keyframe {i}"
        if not isinstance(kf, dict):
            raise PlanError(f"{at}: must be a mapping")
        servos, head = kf.get("servos") or {}, kf.get("head") or {}
        if not isinstance(servos, dict) or not isinstance(head, dict) or not (servos or head):
            raise PlanError(f"{at}: needs 'servos' and/or 'head'")
        if not _whole(kf.get("ms"), MIN_MOVE_MS, MAX_KEYFRAME_MS):
            raise PlanError(f"{at}: ms must be a whole number from {MIN_MOVE_MS} to {MAX_KEYFRAME_MS}")
        if not _whole(kf.get("hold_ms", 0), 0, MAX_KEYFRAME_MS):
            raise PlanError(f"{at}: hold_ms must be a whole number from 0 to {MAX_KEYFRAME_MS}")
        if kf.get("ease", "inout") not in EASINGS:
            raise PlanError(f"{at}: ease must be one of {EASINGS}")
        for joint, pulse in servos.items():
            if joint not in KEYFRAME_JOINTS:
                raise PlanError(f"{at}: {joint!r} is not an arm joint; only {KEYFRAME_JOINTS} "
                                "can be keyframed (leg moves make the robot fall)")
            lo, hi = LIMITS[SERVO_IDS[joint]]
            if not _whole(pulse, lo, hi):
                raise PlanError(f"{at}: {joint} must be a whole pulse from {lo} to {hi}")
        for axis, pulse in head.items():
            if axis not in HEAD:
                raise PlanError(f"{at}: head axis must be pan or tilt, got {axis!r}")
            lo, hi = HEAD_LIMITS[HEAD[axis]]
            if not _whole(pulse, lo, hi):
                raise PlanError(f"{at}: head {axis} must be a whole pulse from {lo} to {hi}")
