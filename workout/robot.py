"""The workout loop's view of the robot: a few calls on top of the robot API backend."""

import contextlib
import logging

from robot_api.errors import RobotError

log = logging.getLogger("workout")


class Robot:
    """Wraps a robot API backend (client.HttpBackend, or api.LocalBackend in dry run).

    `muter` is anything with a robot_speaking() context manager (voice.Listener), so
    the robot never transcribes its own voice."""

    def __init__(self, backend, muter=None):
        self.backend = backend
        self.muter = muter

    def say(self, text: str):
        log.info("ROBOT: %s", text)
        mute = self.muter.robot_speaking() if self.muter else contextlib.nullcontext()
        with mute:
            self.backend.call("sensors/voice/say", text=text)

    def show(self, text: str):
        self._try("sensors/matrix/text", text=text)

    def eyes(self, r: int, g: int, b: int):
        self._try("sensors/eyes", r=r, g=g, b=b)

    def beep(self, ms: int = 100):
        self._try("sensors/buzzer", ms=ms)

    def stand(self):
        self.backend.call("actions/stand")

    def demonstrate(self, demo: dict, cancelled=lambda: False):
        """Play an exercise's demo motion, narrating it. Stops early if cancelled()."""
        for _ in range(demo.get("times", 1)):
            if cancelled():
                return
            if "arm" in demo:
                self.say("Up...")
                self.backend.call("actions/arm", side="both", position=demo["arm"])
                if cancelled():
                    return
                self.say("and down.")
                self.backend.call("actions/arm", side="both", position="down")
            else:
                self.backend.call("actions/run", name=demo["action_group"])

    def stop_now(self):
        """Freeze every servo and cancel any action group. Safe to call from any thread."""
        for path in ("actions/stop", "servo/stop"):
            try:
                self.backend.call(path)
            except RobotError as e:
                log.warning("stop: %s failed: %s", path, e)

    def _try(self, path: str, **kwargs):
        """Lights, matrix and buzzer are nice to have; a failure shouldn't end a workout."""
        try:
            self.backend.call(path, **kwargs)
        except RobotError as e:
            log.warning("%s failed: %s", path, e)
