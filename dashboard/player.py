"""Plays a keyframe animation on the robot from a background thread.

The moves go through the gateway (so through robot_api's limits and the safety
latch) using workout.keyframes.play, the same code the workout loop demonstrates
with. Progress is published as "playback" events and mirrored into the twin's command
channel, so the twin shows what was asked while the readback shows what happened.
"""
from __future__ import annotations

import threading
import time

from robot_api.errors import RobotError
from workout import keyframes

MAX_LOOPS = 20


class Player:
    def __init__(self, gateway, twin_state, hub):
        self.gateway, self.twin, self.hub = gateway, twin_state, hub
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self.state = {"playing": False, "t_ms": 0, "total_ms": 0, "error": None, "name": None,
                      "request": 0, "finished": None}
        self._request = 0
        self._publish()

    def _publish(self, **changes) -> None:
        self.state = {**self.state, **changes}
        self.hub.publish("playback", self.state)

    @property
    def playing(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, frames: list, speed: float = 1.0, loop: bool = False, name: str = "") -> dict:
        with self._lock:
            if self.playing:
                raise RobotError("An animation is already playing on the robot.")
            self._cancel.clear()
            self._request += 1
            self._thread = threading.Thread(target=self._run, args=(frames, speed, loop),
                                            name="dashboard-player", daemon=True)
            self._publish(playing=True, t_ms=0, total_ms=keyframes.duration_ms(frames), error=None,
                          name=name, speed=speed, loop=loop, request=self._request, finished=None,
                          stopped=False, kind="animation", limited=[])
            self._thread.start()
        return self.state

    def start_group(self, group: str, label: str = "") -> dict:
        """Run one of the robot's built-in action groups (no twin preview: the motion
        lives in the robot's controller)."""
        with self._lock:
            if self.playing:
                raise RobotError("An animation is already playing on the robot.")
            self._cancel.clear()
            self._request += 1
            self._thread = threading.Thread(target=self._run_group, args=(group,),
                                            name="dashboard-player", daemon=True)
            self._publish(playing=True, t_ms=0, total_ms=0, error=None, name=label or group, speed=1.0,
                          loop=False, request=self._request, finished=None, stopped=False,
                          kind="builtin", limited=[])
            self._thread.start()
        return self.state

    def _run_group(self, group: str) -> None:
        error = None
        try:
            self.gateway.call("actions/run", name=group)
        except RobotError as e:
            error = str(e)
        except Exception as e:
            error = f"playback failed: {e}"
        self._publish(playing=False, error=error, stopped=self._cancel.is_set(), finished=time.time())

    def stop(self) -> None:
        self._cancel.set()

    def _run(self, frames: list, speed: float, loop: bool) -> None:
        error = None

        limited: set = set()

        def call(path, **kwargs):
            result = self.gateway.call(path, **kwargs)
            report = result.get("calibration") if isinstance(result, dict) else None
            if report and report.get("clamped") and not set(report["clamped"]) <= limited:
                limited.update(report["clamped"])
                self._publish(limited=sorted(limited))
            return result

        def on_step(step):
            pose = keyframes.pose_at(frames, step["t_ms"])
            # the twin shows what was really asked for: the pose inside the calibration limits
            self.twin.set_command(self.gateway.limiter.clamp_pose(pose["servos"]), source="dashboard",
                                  label="playing on robot", head=pose["head"])
            self._publish(t_ms=step["t_ms"])

        try:
            for _ in range(MAX_LOOPS if loop else 1):
                if not keyframes.play(call, frames, speed, self._cancel.is_set, on_step):
                    break
        except RobotError as e:  # the robot's own words: too hot, busy, unreachable...
            error = str(e)
        except Exception as e:
            error = f"playback failed: {e}"
        self._publish(playing=False, error=error, stopped=self._cancel.is_set(), finished=time.time())
