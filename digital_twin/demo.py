"""Cycles the recorded poses with smooth interpolation, so the viewer has motion with no inputs."""
from __future__ import annotations

import threading
import time
from typing import List, Optional

from .state import TwinState


def _ease(t: float) -> float:
    return t * t * (3 - 2 * t)


class PoseDemo:
    def __init__(self, state: TwinState, names: Optional[List[str]] = None, move_s: float = 1.8,
                 hold_s: float = 1.2, hz: float = 30.0):
        self.state, self.move_s, self.hold_s, self.hz = state, move_s, hold_s, hz
        self.names = names or list(state.poses)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> "PoseDemo":
        self._thread = threading.Thread(target=self._run, name="twin-demo", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        i = 0
        current = dict(self.state.command.servos)
        while not self._stop.is_set():
            name = self.names[i % len(self.names)]
            target = self.state.poses[name]
            t0 = time.monotonic()
            while not self._stop.is_set():
                f = min(1.0, (time.monotonic() - t0) / self.move_s)
                e = _ease(f)
                self.state.set_command({s: current[s] + (target[s] - current[s]) * e for s in target},
                                       source="demo", label=name)
                if f >= 1.0:
                    break
                self._stop.wait(1.0 / self.hz)
            current = dict(target)
            self._stop.wait(self.hold_s)
            i += 1
