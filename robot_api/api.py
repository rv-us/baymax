"""One registry of every robot operation, keyed by its HTTP path.

The FastAPI server exposes each entry as POST /<path> (JSON body = keyword
arguments), and LocalBackend calls the same functions in-process, so both
backends behave identically.
"""
from __future__ import annotations

import inspect

from .actions import arms, groups, locomotion, poses
from .errors import RobotError
from .sensors import buzzer, climate, dot_matrix, health, light, microphone, sonar, voice_box
from .servo import bus, head
from .servo.joints import HEAD, JOINTS
from .servo.limits import HEAD_LIMITS, LIMITS


def stop_all() -> dict:
    """Emergency stop: cancel any action group and freeze every bus servo."""
    return {"action_group": groups.stop(), "servos": bus.stop()}


def joint_map() -> dict:
    return {"joints": {name: {"id": sid, "limits": LIMITS[sid]} for name, sid in JOINTS.items()},
            "head_pwm": {axis: {"id": sid, "limits": HEAD_LIMITS[sid]} for axis, sid in HEAD.items()}}


ENDPOINTS = {
    # servo: most granular
    "servo/joints": joint_map,
    "servo/status": bus.status,
    "servo/read": bus.read_servo,
    "servo/set": bus.set_servo,
    "servo/set_many": bus.set_servos,
    "servo/coast": bus.coast,
    "servo/hold": bus.hold,
    "servo/stop": stop_all,
    "servo/head": head.look,
    "servo/head/center": head.center,
    # actions: preprogrammed moves
    "actions/list": groups.list_groups,
    "actions/run": groups.run,
    "actions/stop": groups.stop,
    "actions/walk": locomotion.walk,
    "actions/turn": locomotion.turn,
    "actions/side_step": locomotion.side_step,
    "actions/arm": arms.arm,
    "actions/gripper": arms.gripper,
    "actions/wave": arms.wave,
    "actions/stand": poses.stand,
    "actions/rest": poses.rest,
    # sensors and peripherals
    "sensors/distance": sonar.distance,
    "sensors/eyes": sonar.set_eyes,
    "sensors/climate": climate.read,
    "sensors/health": health.report,
    "sensors/battery": health.battery,
    "sensors/servo_temps": health.servo_temps,
    "sensors/light": light.is_bright,
    "sensors/matrix/text": dot_matrix.show_text,
    "sensors/matrix/icon": dot_matrix.show_icon,
    "sensors/matrix/rows": dot_matrix.show_rows,
    "sensors/matrix/clear": dot_matrix.clear,
    "sensors/voice/say": voice_box.say,
    "sensors/voice/sounds": voice_box.list_sounds,
    "sensors/voice/play": voice_box.play_sound,
    "sensors/voice/volume": voice_box.set_volume,
    "sensors/voice/listen": voice_box.listen_command,
    "sensors/mic/level": microphone.level,
    "sensors/buzzer": buzzer.beep,
}


def call(path: str, **kwargs):
    fn = ENDPOINTS.get(path.strip("/"))
    if fn is None:
        raise RobotError(f"unknown endpoint {path!r}")
    try:
        inspect.signature(fn).bind(**kwargs)
    except TypeError as e:
        raise RobotError(f"{path}: {e}") from e
    return fn(**kwargs)


class LocalBackend:
    """Calls the hardware directly; only works on the robot (or in dry-run)."""

    def call(self, path: str, **kwargs):
        return call(path, **kwargs)
