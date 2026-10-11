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

`demo` is either `arm` (down | forward | up, through the robot API's actions/arm,
moving both arms there and back) or a factory `action_group` from the safe list.
"""

import os
from dataclasses import dataclass, field

import yaml

PLANS_DIR = os.path.join(os.path.dirname(__file__), "plans")
ARM_POSITIONS = ("forward", "up")
MIRROR_MODES = ("arms", "none")
JOINTS = ("shoulder_elevation", "elbow", "knee", "trunk_lean", "hip_shift")


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
    _check_demo(e["demo"], where)
    return Exercise(e["name"], e["cue"], dict(e["demo"]), e["sets"], e["reps"], mirror,
                    e["track_joint"], targets)


def _check_demo(demo, where: str):
    if not isinstance(demo, dict) or ("arm" in demo) == ("action_group" in demo):
        raise PlanError(f"{where}: demo needs exactly one of 'arm' or 'action_group'")
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
