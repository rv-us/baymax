"""Runs a workout session from the dashboard, and streams it to the browser.

This drives the existing engine (workout.loop.Workout) with dashboard pieces plugged
into its sockets: ears that take the patient's answers from the therapist's keyboard
(or, in "simulate patient" mode, answer by themselves), and SimTracker, which replays
a synthetic patient in real time until the pose tracker exists. The session log is
the one the loop saves. A real tracker replaces SimTracker and nothing else changes.
"""
from __future__ import annotations

import json
import queue
import random
import threading
import time

from robot_api.errors import RobotError
from workout.loop import Settings as LoopSettings
from workout.loop import Workout
from workout.parts import SetResult, TemplateCoach, is_pain
from workout.plan import load_plan
from workout.robot import Robot
from workout.session import reps_from_trace, set_target

from . import synthetic

MAX_STEPS = 2  # steps per reposition request (the robot API allows 5)

# kind -> (event type, what the robot says, robot API call or None)
REPOSITION = {
    "closer": ("prompt_closer", "Can you come a little closer, please?", None),
    "back": ("prompt_back", "Can you take a small step back, please?", None),
    "robot_forward": ("prompt_closer", None, ("actions/walk", {"direction": "forward"})),
    "robot_back": ("prompt_back", None, ("actions/walk", {"direction": "back"})),
    "robot_left": ("side_step", None, ("actions/side_step", {"direction": "left"})),
    "robot_right": ("side_step", None, ("actions/side_step", {"direction": "right"})),
}


def frame_advice(frame: dict) -> dict | None:
    """Where the patient sits in the camera frame -> one suggestion, or None if fine.
    frame: {visible, x: -1 (image left) .. 1, size: share of frame height}."""
    if not frame or not frame.get("visible"):
        return None
    if frame["size"] < 0.35:
        return {"kind": "closer", "text": "The patient looks far away. Ask them to come closer."}
    if frame["size"] > 0.85:
        return {"kind": "back", "text": "The patient fills the frame. Ask them to step back."}
    if frame["x"] < -0.4:
        return {"kind": "robot_left", "text": "The patient is at the left edge. Side-step the robot left."}
    if frame["x"] > 0.4:
        return {"kind": "robot_right", "text": "The patient is at the right edge. Side-step the robot right."}
    return None


class Ears:
    """The workout loop's ears, fed from the dashboard."""

    def __init__(self, live):
        self.live = live
        self.answers: queue.Queue = queue.Queue()
        self.callback = lambda text: None

    def on_heard(self, callback):
        self.callback = callback

    def listen(self, timeout_s: float):
        live = self.live
        live.update(waiting=live.said)
        try:
            if live.simulate:
                live.sleep(1.2)
                text = live.sim_answer()
            else:
                text = self.answers.get(timeout=timeout_s)
        except queue.Empty:
            text = None
        live.update(waiting=None, heard=text)
        if text and is_pain(text):
            # Latch the dashboard's stop too; the loop then handles the pain itself.
            live.answering = True
            try:
                live.safety.trip("pain", "The patient's answer sounded like pain.", transcript=text)
            finally:
                live.answering = False
        return text


class TapBackend:
    """The gateway, plus a copy of what the robot says and shows for the browser."""

    def __init__(self, live):
        self.live = live

    def call(self, path: str, **kwargs):
        if path == "sensors/voice/say":
            self.live.update(said=kwargs.get("text"))
        elif path == "sensors/matrix/text":
            self.live.update(matrix=kwargs.get("text"))
        return self.live.gateway.call(path, **kwargs)


class SimTracker:
    """Stand-in for pose tracking: a seeded synthetic patient, replayed in real time."""

    def __init__(self, live, seed: int):
        self.live = live
        self.rng = random.Random(seed)
        self.profile = synthetic.profile(self.rng, progress=0.8)
        self.carry: list = []       # samples from before a pause, for the same set
        self.sets_run = 0
        self.set_started = self.paused_at = 0.0

    def patient_visible(self) -> bool:
        return self.live.frame["visible"]

    def run_set(self, exercise, post_event, cancelled, reps_done: int = 0) -> SetResult:
        live = self.live
        target = {"tolerance": 15.0, **set_target(exercise)}
        if reps_done and self.carry:  # back after a pause: the gap is frames with no pose
            gap = int(min(60.0, (time.time() - self.paused_at) * live.pace) * synthetic.HZ)
            self.carry += [None] * gap
        else:
            self.carry, self.sets_run, self.set_started = [], self.sets_run + 1, time.time()
        started = self.set_started
        todo = synthetic.patient_trace(self.rng, target, exercise.reps - reps_done, self.profile,
                                       rep_offset=reps_done + (self.sets_run - 1) * exercise.reps)
        done = list(self.carry)
        ref_all = synthetic.with_reference(done + todo, target, exercise.reps)["reference"]
        finished = True
        for i, angle in enumerate(todo):
            if not live.frame["visible"]:
                post_event("left_frame")
            if cancelled():
                finished = False
                break
            done.append(angle)
            if i % 2 == 0:
                reps = len(reps_from_trace({"hz": synthetic.HZ, "patient": done}, target))
                live.update(reps=reps, angle=angle,
                            reference=ref_all[len(done) - 1], target=target)
            live.sleep(1 / synthetic.HZ)
        self.carry, self.paused_at = (done, time.time()) if not finished else ([], 0.0)
        trace = {"hz": synthetic.HZ, "patient": done, "reference": ref_all[:len(done)],
                 "joints": synthetic.joint_traces(done, target)}
        reps = len(reps_from_trace(trace, target))
        return SetResult(reps, exercise.reps, [], len(done) / synthetic.HZ, finished=finished,
                         started=started, trace=trace)


class Live:
    def __init__(self, settings, gateway, safety, sessions, hub):
        self.settings, self.gateway, self.safety = settings, gateway, safety
        self.sessions, self.hub = sessions, hub
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.workout: Workout | None = None
        self.ears: Ears | None = None
        self.simulate, self.pace, self.said = True, 1.0, None
        self.answering = False      # a pain answer is being handled by the loop itself
        self.frame = {"visible": True, "x": 0.0, "size": 0.6, "simulated": True}
        self.state = {"running": False, "state": None, "plan": None, "frame": self.frame,
                      "advice": None, "last": None}
        safety.on_trip.append(self._on_trip)
        self.publish()

    # ---- state for the browser ----------------------------------------------------

    def publish(self) -> None:
        self.hub.publish("live", self.state)

    def update(self, quiet: bool = False, **changes) -> None:
        if "said" in changes:
            self.said = changes["said"]
        w = self.workout
        if w and self.state["running"]:
            s = w.session
            ex = w.plan.exercises[min(s.exercise, len(w.plan.exercises) - 1)]
            changes.update(state=w.state, set=s.set_number, sets=ex.sets, target_reps=ex.reps,
                           exercise={"name": ex.name, "cue": ex.cue, "index": s.exercise,
                                     "count": len(w.plan.exercises)})
            if w.state != "MIRROR":
                changes.setdefault("reps", s.reps_done)
        self.state = {**self.state, **changes}
        if not quiet:
            self.publish()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds / self.pace)

    def sim_answer(self) -> str:
        said = (self.said or "").lower()
        return "Yes, I'm ready" if "ready" in said else "No, it was fine"

    # ---- control ------------------------------------------------------------------

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, plan_name: str, simulate: bool = True, pace: float = 1.0, seed: int | None = None) -> dict:
        with self._lock:
            if self.running:
                raise RobotError("A session is already running.")
            reason = self.safety.blocked()
            if reason:
                raise RobotError(reason)
            plan = load_plan(plan_name if plan_name.endswith((".yaml", ".yml")) else
                             f"{self.settings.plans_dir}/{plan_name}.yaml")
            self.simulate, self.pace = bool(simulate), max(0.25, min(8.0, float(pace)))
            self.ears = Ears(self)
            self.frame.update(visible=True, x=0.0, size=0.6)
            tracker = SimTracker(self, seed if seed is not None else int(time.time()))
            loop_settings = LoopSettings(ready_wait_s=8, answer_timeout_s=30, pause_timeout_s=30,
                                         sessions_dir=self.settings.sessions_dir)
            self.workout = Workout(plan, Robot(TapBackend(self)), self.ears, tracker, TemplateCoach(),
                                   loop_settings, sleep=self.sleep)
            self.state = {"running": True, "plan": plan.name, "plan_id": plan_name, "simulate": self.simulate,
                          "pace": self.pace, "frame": self.frame, "advice": None, "reps": 0,
                          "synthetic_tracker": True, "last": None, "error": None}
            self.update()
            self._thread = threading.Thread(target=self._run, name="dashboard-live", daemon=True)
            self._thread.start()
        return self.state

    def _run(self) -> None:
        w, error, sid = self.workout, None, None
        watcher = threading.Thread(target=self._watch, daemon=True)
        watcher.start()
        try:
            w.run()
        except Exception as e:  # the loop already froze the robot and saved what it had
            error = str(e)
        path = getattr(w, "saved_path", None)
        if path:
            sid = path.replace("\\", "/").rsplit("/", 1)[-1][:-5]
            try:  # the tracker was simulated, so say so in the log
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                data.update(synthetic=True, source="live-simulated-tracker")
                self.sessions.save(sid, data)
            except (OSError, ValueError):
                pass
        self.state = {**self.state, "running": False, "state": "END", "waiting": None, "error": error,
                      "last": {"id": sid, "status": w.session.status, "flagged": w.session.flagged}}
        self.publish()
        self.hub.publish("sessions", {"changed": sid})

    def _watch(self) -> None:
        """Publish state changes the loop makes between its own robot calls."""
        seen = None
        while self.running:
            w = self.workout
            now = (w.state, w.session.set_number, w.session.exercise)
            if now != seen:
                seen = now
                self.update()
            time.sleep(0.15)

    def answer(self, text: str) -> None:
        if not self.running or self.ears is None:
            raise RobotError("No session is running.")
        self.ears.answers.put(text)

    def stop(self) -> None:
        """The therapist's Stop: freeze, and end the session."""
        if self.running and self.workout:
            self.workout.post("operator_stop")

    def _on_trip(self, kind: str, entry: dict) -> None:
        w = self.workout
        if not (self.running and w) or self.answering or w.session.status != "running":
            return
        if kind == "fall":
            w.log_event("fall", detail=entry.get("detail"))
        elif entry.get("transcript"):
            w.log_event("pain_word", transcript=entry["transcript"])
        w.post("pain")  # SAFETY_STOP: the loop's own stop-and-flag path

    # ---- framing ------------------------------------------------------------------

    def set_frame(self, **changes) -> dict:
        self.frame.update({k: v for k, v in changes.items() if k in ("visible", "x", "size")})
        self.safety.alert("out_of_frame", not self.frame["visible"],
                          "The camera can't see the patient.")
        self.update(frame=dict(self.frame), advice=frame_advice(self.frame))
        return self.frame

    def reposition(self, kind: str, steps: int = 1) -> dict:
        if kind not in REPOSITION:
            raise RobotError(f"unknown reposition {kind!r}; choose from {sorted(REPOSITION)}")
        event, line, action = REPOSITION[kind]
        result = {}
        if line:
            result = self.gateway.call("sensors/voice/say", text=line)
            self.update(said=line)
        if action:
            path, kwargs = action
            result = self.gateway.call(path, steps=max(1, min(MAX_STEPS, int(steps))), **kwargs)
        if self.running and self.workout:
            self.workout.log_event(event, detail=kind)
        return result
