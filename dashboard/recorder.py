"""Emote recorder: move the robot's arms by hand, and keep the motion.

    start   let the arms go limp (legs keep holding) and sample their positions
    stop    hold the arms again; the samples become a "take"
    save    trim the take, thin it to keyframes, save it as an animation, and log the
            recording as a session so it shows under Sessions

Samples come from servo/status through the gateway, as fast as the robot answers, up
to 10 a second. A take is {id, frames: [{t_ms, servos: {joint: pulse}}]}: the same
shape as the keyframe import contract in store.import_frames, which does the thinning.

In dry run nobody can move the fake robot, so a scripted hand moves it while a
recording runs, and the take is marked simulated.
"""
from __future__ import annotations

import math
import threading
import time

from robot_api.errors import RobotError
from workout.keyframes import duration_ms
from workout.plan import KEYFRAME_JOINTS, PlanError

from . import poses, store

HZ = 10
MAX_SECONDS = 60
KEEP_TAKES = 6


def puppet_pose(t: float) -> dict:
    """Dry run only: where a pretend hand has the arms t seconds into a recording.
    A wave with the right arm while the left lifts to the side."""
    lift = min(1.0, t / 1.5)
    swing = math.sin(max(0.0, t - 1.5) * 3.2)
    return {**poses.arm("right", forward=150 * lift, side_lift=25 + 18 * swing * lift, elbow=30 + 15 * swing * lift),
            **poses.arm("left", side_lift=35 * lift * (0.5 + 0.5 * math.sin(t * 0.9)))}


class Recorder:
    def __init__(self, settings, gateway, hub, animations, sessions):
        self.settings, self.gateway, self.hub = settings, gateway, hub
        self.animations, self.sessions = animations, sessions
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._frames: list = []
        self._began = 0.0
        self._wall = 0.0
        self.takes: dict[str, dict] = {}
        self.state = {"recording": False, "seconds": 0.0, "frames": 0, "error": None, "take": None,
                      "simulated": False, "limp": False}
        self._publish()

    def _publish(self, **changes) -> None:
        self.state = {**self.state, **changes}
        self.hub.publish("recorder", self.state)

    @property
    def recording(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ---- record -------------------------------------------------------------------

    def start(self, coast: bool = True) -> dict:
        with self._lock:
            if self.recording:
                raise RobotError("A recording is already running.")
            reason = self.gateway.blocked()
            if reason:
                raise RobotError(reason)
            if coast:
                self.gateway.call("servo/coast", servos=list(KEYFRAME_JOINTS))
            self._frames, self._began, self._wall = [], time.monotonic(), time.time()
            self._cancel.clear()
            self._thread = threading.Thread(target=self._run, name="dashboard-recorder", daemon=True)
            self._publish(recording=True, seconds=0.0, frames=0, error=None, take=None,
                          simulated=bool(self.gateway.local), limp=bool(coast))
            self._thread.start()
        return self.state

    def _run(self) -> None:
        error = None
        try:
            while not self._cancel.is_set():
                tick = time.monotonic()
                t = tick - self._began
                if t >= MAX_SECONDS:
                    break
                if self.gateway.local:
                    self._puppet(t)
                status = self.gateway.call("servo/status")
                servos = {s["joint"]: int(s["position"]) for s in status.get("servos", [])
                          if s.get("joint") in KEYFRAME_JOINTS and s.get("position") is not None}
                if servos:
                    self._frames.append({"t_ms": round(t * 1000), "servos": servos})
                if len(self._frames) % 3 == 0:
                    self._publish(seconds=round(t, 1), frames=len(self._frames))
                self._cancel.wait(max(0.0, 1 / HZ - (time.monotonic() - tick)))
        except RobotError as e:
            error = str(e)
        except Exception as e:  # a recorder bug must not leave the arms limp silently
            error = f"recording failed: {e}"
        self._finish(error)

    def _puppet(self, t: float) -> None:
        from robot_api.fake import STATE
        from robot_api.servo.joints import JOINTS

        for joint, pulse in puppet_pose(t).items():
            STATE.positions[JOINTS[joint]] = pulse

    def _finish(self, error: str | None) -> None:
        limp = self.state.get("limp")
        if limp:
            try:
                self.gateway.call("servo/hold", servos=list(KEYFRAME_JOINTS))
                limp = False
            except RobotError as e:
                error = error or f"The arms are still limp: {e}"
        frames = self._frames
        take = None
        if len(frames) >= 2:
            tid = time.strftime("take-%H%M%S", time.localtime(self._wall))
            take = {"id": tid, "started": self._wall, "frames": frames, "hz": HZ,
                    "duration_ms": frames[-1]["t_ms"], "simulated": bool(self.gateway.local)}
            self.takes[tid] = take
            for old in list(self.takes)[:-KEEP_TAKES]:
                del self.takes[old]
        elif not error:
            error = "Nothing was recorded. The robot didn't report any arm positions."
        self._publish(recording=False, error=error, limp=limp, frames=len(frames),
                      seconds=round(frames[-1]["t_ms"] / 1000, 1) if frames else 0.0,
                      take={k: take[k] for k in ("id", "duration_ms", "simulated")} if take else None)

    def stop(self) -> dict:
        self._cancel.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=5)
        return self.state

    def take(self, tid: str) -> dict:
        try:
            return self.takes[tid]
        except KeyError:
            raise store.NotFound(f"recording {tid}") from None

    # ---- trim and save ------------------------------------------------------------

    def keyframes(self, tid: str, start_ms: float = 0, end_ms: float | None = None, name: str = "Recording") -> dict:
        """The take between the trim points, as an unsaved animation."""
        take = self.take(tid)
        end = take["duration_ms"] if end_ms is None else float(end_ms)
        start = max(0.0, float(start_ms))
        if end - start < 300:
            raise PlanError("Keep at least 0.3 seconds of the recording.")
        cut = [{"t_ms": f["t_ms"] - start, "servos": f["servos"]} for f in take["frames"] if start <= f["t_ms"] <= end]
        if len(cut) < 2:
            raise PlanError("There are no samples between those trim points.")
        anim = store.import_frames({"name": name, "frames": cut})
        anim["keyframes"].append(poses.frame(1200, poses.REST))  # always finish with the arms down
        anim["keyframes"] = anim["keyframes"][-60:]
        return anim

    def save(self, tid: str, name: str, start_ms: float = 0, end_ms: float | None = None,
             kind: str = "emote", description: str = "") -> dict:
        take = self.take(tid)
        name = str(name or "").strip()[:80]
        if not name:
            raise PlanError("Give the recording a name first.")
        draft = self.keyframes(tid, start_ms, end_ms, name)
        saved = self.animations.save({"name": name, "keyframes": draft["keyframes"], "source": "recorded",
                                      "kind": kind, "description": description,
                                      "exercise": {"cue": description} if kind == "exercise" else {}})
        end = take["duration_ms"] if end_ms is None else float(end_ms)
        kept = [f for f in take["frames"] if max(0.0, float(start_ms)) <= f["t_ms"] <= end]
        travel = {j: sum(abs(b["servos"].get(j, 0) - a["servos"].get(j, 0)) for a, b in zip(kept, kept[1:])
                         if j in a["servos"] and j in b["servos"]) for j in KEYFRAME_JOINTS}
        sid = time.strftime("recording-%Y%m%d-%H%M%S", time.localtime(take["started"]))
        self.sessions.save(sid, {
            "schema": 2, "kind": "recording", "plan": f"Recording: {name}", "started": take["started"],
            "ended": take["started"] + take["duration_ms"] / 1000, "status": "completed", "flagged": False,
            "synthetic": bool(take["simulated"]), "sets": [], "interrupted": [], "events": [], "notes": [],
            "recording": {"name": name, "animation_id": saved["id"], "kind": saved["kind"],
                          "take_ms": take["duration_ms"], "kept_ms": round(end - max(0.0, float(start_ms))),
                          "animation_ms": duration_ms(saved["keyframes"]), "samples": len(kept),
                          "keyframes": len(saved["keyframes"]),
                          "joints_moved": sorted(j for j, d in travel.items() if d >= 40),
                          "travel_deg": round(sum(travel.values()) / poses.PULSE_PER_DEG)}})
        self.hub.publish("sessions", {"changed": sid})
        return {"animation": saved, "session": sid}
