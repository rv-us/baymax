"""Factory action groups (.d6a files in /home/pi/TonyPi/ActionGroups)."""
from __future__ import annotations

import os

from .. import config
from ..errors import RobotError
from ..hardware import agc, ensure_cool, motion_lock

# Groups that are known to be benign from standing: walking, turning,
# side-stepping and upper-body gestures. Everything else (falling, getting up,
# kicks, crawling, stairs, unlabeled test files...) needs allow_risky=True.
SAFE_GROUPS = {
    "stand", "stand_slow",
    "go_forward", "go_forward_fast", "go_forward_one_step", "go_forward_one_small_step",
    "back", "back_fast", "back_one_step",
    "turn_left", "turn_right", "turn_left_fast", "turn_right_fast",
    "turn_left_small_step", "turn_right_small_step",
    "left_move", "right_move", "left_move_fast", "right_move_fast",
    "left_move_10", "left_move_20", "left_move_30", "left_move_40",
    "right_move_10", "right_move_20", "right_move_30", "right_move_40",
    "wave", "bow", "bow2", "chest", "twist", "stepping", "squat", "wings_up",
    "left_hand", "right_hand", "go_hand_up", "lift_left_hand",
    "wing_chun", "left_uppercut", "right_uppercut",
}

_running = False


def list_groups() -> dict:
    if config.DRY_RUN or not os.path.isdir(config.ACTION_DIR):
        names = sorted(SAFE_GROUPS)
    else:
        names = sorted(f[:-4] for f in os.listdir(config.ACTION_DIR) if f.endswith(".d6a"))
    return {"safe": [n for n in names if n in SAFE_GROUPS],
            "risky": [n for n in names if n not in SAFE_GROUPS]}


def _exists(name: str) -> bool:
    if config.DRY_RUN:
        return name in SAFE_GROUPS
    return os.path.isfile(os.path.join(config.ACTION_DIR, name + ".d6a"))


def run(name: str, times: int = 1, allow_risky: bool = False) -> dict:
    """Run a factory action group `times` times, ending in the stand pose.
    Blocks until finished."""
    if not name or "/" in name or not _exists(name):
        raise RobotError(f"no action group named {name!r}; see list_action_groups")
    if name not in SAFE_GROUPS and not allow_risky:
        raise RobotError(f"{name!r} is not in the safe list (it may fall, kick or need "
                         "floor space); pass allow_risky=True after confirming with a human")
    times = int(times)
    if not 1 <= times <= config.MAX_REPEATS:
        raise RobotError(f"times must be 1-{config.MAX_REPEATS}")
    return play(name, times)


def play(name: str, times: int) -> dict:
    """run() without the name/count checks; for callers that validated."""
    global _running
    a = agc()
    with motion_lock:
        ensure_cool(force=True)
        _running = True
        try:
            a.runActionGroup(name, times, True)
        finally:
            _running = False
            # runActionGroup leaves its walk state mid-walk after with_stand,
            # and a stop request that arrived between frames would cancel the
            # next action; reset both so the next call starts cleanly.
            setattr(a, "__start", True)
            setattr(a, "__end", False)
            setattr(a, "stop_action", False)
            setattr(a, "stop_action_group", False)
    return {"ran": name, "times": times}


def stop() -> dict:
    """Interrupt a running action group after its current frame."""
    if not _running:
        return {"stopped": False, "note": "no action group was running"}
    a = agc()
    a.stopActionGroup()
    a.stopAction()
    return {"stopped": True}
