"""Walking, turning and side-stepping, built on the factory action groups."""
from __future__ import annotations

from .. import config
from ..errors import RobotError
from . import groups


def _steps(steps) -> int:
    steps = int(steps)
    if not 1 <= steps <= config.MAX_REPEATS:
        raise RobotError(f"steps must be 1-{config.MAX_REPEATS}")
    return steps


def _check(direction: str, options: tuple) -> str:
    d = direction.strip().lower()
    if d not in options:
        raise RobotError(f"direction must be one of {options}, got {direction!r}")
    return d


def walk(direction: str = "forward", steps: int = 1) -> dict:
    """Walk forward or back. One step = one gait cycle (a few cm)."""
    d = _check(direction, ("forward", "back"))
    steps = _steps(steps)
    if d == "forward":
        # runActionGroup(go_forward, n) plays start + (n-1) cycles + end.
        result = groups.play("go_forward", steps + 1)
    else:
        # back plays back_start + n cycles + back_end.
        result = groups.play("back", steps)
    return {"walked": d, "steps": steps, **result}


def turn(direction: str = "left", steps: int = 1) -> dict:
    """Turn in place; each step is roughly 20-30 degrees."""
    d = _check(direction, ("left", "right"))
    return {"turned": d, **groups.play(f"turn_{d}", _steps(steps))}


def side_step(direction: str = "left", steps: int = 1) -> dict:
    """Step sideways (the robot's own left/right)."""
    d = _check(direction, ("left", "right"))
    return {"side_stepped": d, **groups.play(f"{d}_move", _steps(steps))}
