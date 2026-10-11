"""Runs a workout session from the dashboard, and streams it to the browser.

This drives the existing engine (workout.loop.Workout) with dashboard pieces plugged
into its sockets: ears that take the patient's answers from the therapist's keyboard
(or, in "simulate patient" mode, answer by themselves), and SimTracker, which replays
a synthetic patient in real time until the pose tracker exists. The session log is
the one the loop saves. A real tracker replaces SimTracker and nothing else changes.
"""
from __future__ import annotations

import json
import logging
import queue
import random
import threading
import time

from robot_api.errors import RobotError
from workout.loop import Settings as LoopSettings
from workout.loop import Workout
from workout import pacing
from workout.keyframes import duration_ms, mirrored, pose_at
from workout.parts import SetResult, TemplateCoach, is_pain
from workout.plan import load_plan
from workout.robot import Robot
from workout.session import reps_from_trace, set_target

from . import preview, synthetic
from .feed import open_feed
from .robot import Refused

log = logging.getLogger(__name__)

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
        self.carry_ref: list = []   # the robot's own motion over the same samples
        self.sets_run = 0
        self.set_started = self.paused_at = 0.0

    def patient_visible(self) -> bool:
        return self.live.frame["visible"]

    def _move_robot(self, frames: list, t_ms: float, sent: dict) -> dict:
        """Put the arms where the demonstration is `t_ms` into one rep. Only joints that
        changed are sent; a refusal (a latched stop, a hot servo) just skips the move."""
        servos = pose_at(frames, t_ms)["servos"] if frames else {}
        changed = {j: p for j, p in servos.items() if sent.get(j) != p}
        if changed:
            try:
                self.live.gateway.call("servo/set_many", targets=changed, ms=200, wait=False)
                return {**sent, **changed}
            except RobotError as e:
                log.debug("mirror move skipped: %s", e)
        return sent

    def _begin_set(self, exercise, reps_done: int):
        """What a set is scored against, when it began, and the samples so far (a set that
        resumes after a pause continues the same trace, with a gap of no-pose frames)."""
        live = self.live
        target = {"tolerance": 15.0, **set_target(exercise)}
        if reps_done and self.carry:
            gap = int(min(60.0, (time.time() - self.paused_at) * live.pace) * synthetic.HZ)
            self.carry += [None] * gap
            self.carry_ref += [self.carry_ref[-1] if self.carry_ref else 0.0] * gap
        else:
            self.carry, self.carry_ref = [], []
            self.sets_run, self.set_started = self.sets_run + 1, time.time()
        return target, self.set_started, list(self.carry)

    def run_set(self, exercise, post_event, cancelled, reps_done: int = 0) -> SetResult:
        target, started, done = self._begin_set(exercise, reps_done)
        todo = synthetic.patient_trace(self.rng, target, exercise.reps - reps_done, self.profile,
                                       rep_offset=reps_done + (self.sets_run - 1) * exercise.reps)
        return self._mirror(exercise, post_event, cancelled, reps_done, target, started, done, iter(todo))

    def _mirror(self, exercise, post_event, cancelled, reps_done, target, started, done, angles,
                joints: bool = True) -> SetResult:
        """Follow the patient's tracked angle sample by sample (`angles` blocks or not as its
        source dictates) while the robot mirrors the exercise a quarter-rep ahead of them."""
        live = self.live
        est = pacing.ProgressEstimator(target, start=reps_done)
        leader = pacing.PaceLeader(target["tempo_s"], exercise.reps, start=reps_done)
        frames = preview.demo_frames(exercise.demo) if exercise.mirror == "arms" else []
        cycle_ms = duration_ms(frames)
        other_side = mirrored(frames) if frames and exercise.demo.get("alternate") else None   # every second rep
        ref = list(self.carry_ref)
        sent: dict = {}
        finished = True
        began = time.monotonic()
        for i, angle in enumerate(angles):
            if not live.frame["visible"]:
                post_event("left_frame")
            if cancelled():
                finished = False
                break
            done.append(angle)
            patient = est.update(angle)
            leader.step(patient, 1 / synthetic.HZ)
            ref.append(leader.angle(target))
            if i % 2 == 0:
                if frames:
                    now = other_side if other_side is not None and int(leader.progress) % 2 else frames
                    sent = self._move_robot(now, leader.phase() * cycle_ms, sent)
                reps = len(reps_from_trace({"hz": synthetic.HZ, "patient": done}, target))
                live.update(reps=reps, angle=angle, reference=ref[-1], target=target,
                            lead=round(leader.ahead_of(patient), 2))
            # A sample is 1/HZ of the exercise's time. Moving the robot and counting reps take time of
            # their own, so only the rest of the period is slept: the robot's lead and the trace's time
            # axis would otherwise run slow, and the robot would fall behind the patient it should lead.
            live.sleep(max(0.0, 1 / synthetic.HZ - (time.monotonic() - began) * live.pace))
            began = time.monotonic()
        if finished:
            self._move_robot(frames, 0, sent)  # back to the start pose
        self.carry, self.carry_ref, self.paused_at = (done, ref, time.time()) if not finished else ([], [], 0.0)
        trace = {"hz": synthetic.HZ, "patient": done, "reference": ref[:len(done)]}
        if joints:
            trace["joints"] = synthetic.joint_traces(done, target)
        reps = len(reps_from_trace(trace, target))
        return SetResult(reps, exercise.reps, [], len(done) / synthetic.HZ, finished=finished,
                         started=started, trace=trace)


class WebcamTracker(SimTracker):
    """The same loop, with the patient's joint angle read from arm_mvp's arm tracking (computed
    on the GX10 when it is there) instead of a synthetic patient. A joint the tracker can't read
    (knees, trunk) falls back to the simulated patient for that exercise, and the log says so."""

    TRACKED = ("shoulder_elevation", "elbow")
    STALE_S = 1.0

    def __init__(self, live, seed: int, cam):
        super().__init__(live, seed)
        self.cam = cam
        self.synthetic = False

    def patient_visible(self) -> bool:
        return self.live.frame["visible"]

    def run_set(self, exercise, post_event, cancelled, reps_done: int = 0) -> SetResult:
        if exercise.track_joint not in self.TRACKED:
            self.synthetic = True
            return super().run_set(exercise, post_event, cancelled, reps_done)
        target, started, done = self._begin_set(exercise, reps_done)
        limit = time.time() + exercise.reps * target["tempo_s"] * 2.5 + 20  # a patient who can't finish

        def angles():
            while time.time() < limit:
                pose = self.cam.pose()
                fresh = time.time() - pose["t"] < self.STALE_S and pose["visible"]
                yield pose["angles"].get(exercise.track_joint) if fresh else None
                if len(reps_from_trace({"hz": synthetic.HZ, "patient": done}, target)) >= exercise.reps:
                    return

        return self._mirror(exercise, post_event, cancelled, reps_done, target, started, done, angles(), joints=False)


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
        self.feed = None            # the robot's camera, when this session opened it (and must let go of it)
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

    def start(self, plan_name: str, simulate: bool = True, pace: float = 1.0, seed: int | None = None,
              plan=None) -> dict:
        """Run `plan_name` from workout/plans, or `plan` (an already-checked Plan the
        dashboard assembled on the spot; `plan_name` is then only its label)."""
        with self._lock:
            if self.running:
                raise RobotError("A session is already running.")
            reason = self.safety.blocked()
            if reason:
                raise RobotError(reason)
            if plan is None:
                plan = load_plan(plan_name if plan_name.endswith((".yaml", ".yml")) else
                                 f"{self.settings.plans_dir}/{plan_name}.yaml")
            self.simulate, self.pace = bool(simulate), max(0.25, min(8.0, float(pace)))
            self.ears = Ears(self)
            self.frame.update(visible=True, x=0.0, size=0.6)
            cam, own, note = self._tracking_camera()
            try:
                seed = seed if seed is not None else int(time.time())
                tracker = WebcamTracker(self, seed, cam) if cam is not None else SimTracker(self, seed)
                if cam is not None:
                    self.pace = 1.0  # a real patient moves in real time
                loop_settings = LoopSettings(ready_wait_s=8, answer_timeout_s=30, pause_timeout_s=30,
                                             sessions_dir=self.settings.sessions_dir)
                self.workout = Workout(plan, Robot(TapBackend(self)), self.ears, tracker, TemplateCoach(),
                                       loop_settings, sleep=self.sleep)
            except BaseException:
                if own:
                    cam.stop()
                raise
            if own:
                self.feed = self.gateway.feed = cam
            self.state = {"running": True, "plan": plan.name, "plan_id": plan_name, "simulate": self.simulate,
                          "pace": self.pace, "frame": self.frame, "advice": None, "reps": 0,
                          "synthetic_tracker": cam is None, "tracker_note": note, "tracking": None,
                          "last": None, "error": None}
            self.update()
            self._thread = threading.Thread(target=self._run, name="dashboard-live", daemon=True)
            self._thread.start()
        return self.state

    def _tracking_camera(self):
        """(camera, opened here, why there's no camera). With a robot the session watches through the
        camera the GX10 owns (dashboard/gx10_camera.py), else through the robot's own camera opened here
        (dashboard/feed.py); in a dry run through the webcam the dashboard already holds. With neither the patient is simulated, and the reason is shown."""
        webcam = self.gateway.webcam
        if webcam is not None:
            if webcam.tracking:
                return webcam, False, None
            return None, False, (webcam.status["detail"] if getattr(webcam, "gx10_camera", False) else None)
        if self.settings.dry_run:
            return None, False, None
        try:
            cam, own = open_feed(self.settings, self.gateway, "A session", self.apply_pose)
        except Refused as e:
            return None, False, str(e)
        return cam, own, None

    def _release_feed(self) -> None:
        feed, self.feed = self.feed, None
        if feed is not None:
            self.gateway.feed = None
            feed.stop()
            self.frame["simulated"] = True   # nothing is supplying where the patient stands any more

    def _run(self) -> None:
        w, error, sid = self.workout, None, None
        watcher = threading.Thread(target=self._watch, daemon=True)
        watcher.start()
        try:
            w.run()
        except Exception as e:  # the loop already froze the robot and saved what it had
            error = str(e)
        self._release_feed()
        path = getattr(w, "saved_path", None)
        if path:
            sid = path.replace("\\", "/").rsplit("/", 1)[-1][:-5]
            try:  # a simulated patient is marked as such in the log; the webcam's is real data
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                fake = getattr(w.tracker, "synthetic", True)
                data.update(synthetic=fake, source="live-simulated-tracker" if fake else "live-webcam")
                self.sessions.save(sid, data)
            except (OSError, ValueError):
                pass
        self.state = {**self.state, "running": False, "state": "END", "waiting": None, "error": error, "tracking": None,
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

    def apply_pose(self, pose: dict) -> None:
        """Where the webcam sees the patient (called about 5 times a second). Same state the
        dry-run sliders set, so the camera panel, the advice and the pause-on-walk-out work as
        they will with the robot's own camera."""
        new = {"visible": bool(pose["visible"]), "x": round(pose["x"], 2), "size": round(pose["size"], 2)}
        first = self.frame.get("simulated", True)
        if first or any(self.frame.get(k) != v for k, v in new.items()):
            self.frame["simulated"] = False
            if self.running:
                self.set_frame(**new)
            else:
                self.frame.update(new)
                self.update(frame=dict(self.frame), advice=frame_advice(self.frame))
        if self.running:
            tracking = self._tracking(pose)
            if tracking != self.state.get("tracking"):
                self.update(tracking=tracking)

    def _tracking(self, pose: dict) -> dict:
        """What the tracker (on the GX10 when it can) says it sees, for the Live screen: who is
        followed, which arms are green, and the angles the session is scored on."""
        cam = self.feed or self.gateway.webcam
        angles = {k: None if v is None else round(v) for k, v in (pose.get("angles") or {}).items()}
        return {"on": getattr(cam, "source", "none"), "transport": getattr(cam, "transport", None),
                "delivery": getattr(cam, "delivery", None), "ms": getattr(cam, "ms", None),
                "camera": getattr(cam, "label", None), "visible": bool(pose.get("visible")),
                "calibrated": bool(pose.get("calibrated")), "people": pose.get("people", 0),
                "arms": pose.get("arms") or {}, "green": pose.get("green") or {}, "angles": angles}

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
