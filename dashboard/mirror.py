"""Mirror: the robot copies the patient's arms, live.

    camera  ->  GX10 (arm_mvp tracker)  ->  servo pulses  ->  robot arms

With a robot the camera is the robot's own (H.264 over UDP, else MJPEG), so the robot watches
the person in front of it and copies them. In a dry run the laptop's webcam stands in for it.
Every frame goes to the GX10's pose service (dashboard/pose_service.py), which runs arm_mvp's
tracker and answers with the pose and the pulses the arms should take (dashboard/armtrack.py);
with no service, or if it goes quiet, the same tracker runs on this computer. This module is only
the loop that forwards the newest answer to the robot.

What keeps it safe: every move goes through the Gateway, so the dashboard's safety latch,
the calibration profile and robot_api's own limits (speed cap, coupled arm limits, temperature
cutoff) all apply; the pulses are damped and rate limited at the tracker, and only for arms the tracker
draws green (an arm that is holding or lost is never sent anything, and a stale answer is never
replayed: with nothing green and fresh the robot simply holds still until Stop eases the arms back
to rest); a safety stop ends mirroring at once and leaves the robot frozen where it is; and it will not
start while a session, an animation or a recording is using the robot.
"""
from __future__ import annotations

import logging
import threading
import time

from robot_api.errors import RobotError

from .feed import open_feed
from .robot import Refused

log = logging.getLogger(__name__)

HZ = 20.0            # most pulse updates sent to the robot per second
MOVE_MS = 120        # each update is a move this long, so the arms glide between answers
STALE_S = 0.8        # an answer older than this is not acted on
REST_MS = 800
MAX_FAILURES = 5     # consecutive refused or failed moves before mirroring gives up


ARM_SERVO_IDS = (8, 7, 6, 16, 15, 14)   # shoulder pitch, shoulder roll, elbow: left, then right


def rest_pulses() -> dict:
    """The arms in the stand pose (the same values arm_mvp's tracker rests at)."""
    from robot_api.servo.joints import STAND_POSE
    return {str(sid): STAND_POSE[sid] for sid in ARM_SERVO_IDS}


class Mirror:
    def __init__(self, settings, gateway, hub, busy=lambda: None):
        self.settings, self.gateway, self.hub = settings, gateway, hub
        self.busy = busy              # () -> a reason the robot is in use by something else, or None
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._rest_on_stop = True
        self._feed = None             # the Webcam (laptop or robot camera) that tracks for us
        self._own_feed = False
        self.state = {"running": False, "source": None, "pose_on": "none", "transport": None, "delivery": None,
                      "visible": False, "calibrated": False, "arms": {}, "green": {}, "people": 0, "angles": {}, "sent": 0,
                      "error": None, "since": None}

    # ---- control ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> dict:
        with self._lock:
            if self.running:
                raise Refused("Mirror is already running.")
            reason = self.gateway.blocked() or self.busy()
            if reason:
                raise Refused(reason)
            self._feed, self._own_feed = self._open_feed()
            self._stop.clear()
            self._rest_on_stop = True
            self.state = {**self.state, "running": True, "error": None, "sent": 0, "since": time.time(),
                          "source": self._feed.label}
            self._thread = threading.Thread(target=self._run, name="dashboard-mirror", daemon=True)
            self._thread.start()
            self.publish()
        return self.state

    def _open_feed(self):
        """The camera and tracker to mirror from: the dry run's webcam, or the robot's own camera."""
        return open_feed(self.settings, self.gateway)

    def stop(self, rest: bool = True) -> None:
        """End mirroring. `rest` eases the arms back to the stand pose; a safety stop passes False
        because it has already frozen the robot and nothing may move it after that."""
        self._rest_on_stop = rest
        self._stop.set()

    def jpeg(self, width: int = 640):
        feed = self._feed
        return feed.jpeg(width) if feed is not None and self.running else None

    # ---- state for the browser ----------------------------------------------------

    def snapshot(self) -> dict:
        return self.state

    def publish(self) -> None:
        self.hub.publish("mirror", self.state)

    def _update(self, **changes) -> None:
        self.state = {**self.state, **changes}
        self.publish()

    # ---- the loop -----------------------------------------------------------------

    def _move(self, targets: dict, ms: int) -> None:
        self.gateway.call("servo/set_many", targets=targets, ms=ms, wait=False)

    def _run(self) -> None:
        feed, sent, failures, error = self._feed, {}, 0, None
        last_publish = 0.0
        tick = 1.0 / HZ
        try:
            while not self._stop.is_set():
                began = time.monotonic()
                pose = feed.pose()
                age = time.time() - pose.get("t", 0.0)
                pulses = pose.get("pulses") or {}
                try:
                    if pulses and age < STALE_S:   # the tracker lists only green arms; old answers are ignored
                        changed = {k: v for k, v in pulses.items() if sent.get(k) != v}
                        if changed:
                            self._move(changed, MOVE_MS)
                            sent.update(changed)
                            self.state["sent"] += 1
                    failures = 0
                except Refused as e:                 # a safety stop, a hot servo, another program moving it
                    error = str(e)
                    break
                except RobotError as e:
                    failures += 1
                    if failures >= MAX_FAILURES:
                        error = f"The robot stopped accepting moves: {e}"
                        break
                now = time.monotonic()
                if now - last_publish > 0.25:
                    last_publish = now
                    self._update(pose_on=feed.source, transport=feed.transport, delivery=feed.delivery,
                                 visible=bool(pose.get("visible")), calibrated=bool(pose.get("calibrated")),
                                 people=pose.get("people", 0), green=pose.get("green") or {},
                                 arms=pose.get("arms") or {}, angles=pose.get("angles") or {})
                time.sleep(max(0.0, tick - (now - began)))
        finally:
            if self._rest_on_stop and sent and error is None:
                try:
                    self._move(rest_pulses(), REST_MS)
                except RobotError as e:
                    log.warning("mirror: couldn't ease the arms back to rest: %s", e)
            if self._own_feed:
                feed.stop()                          # lets go of the robot's camera stream
            self._feed = None
            self._update(running=False, error=error, pose_on="none", arms={}, green={}, angles={}, visible=False)
