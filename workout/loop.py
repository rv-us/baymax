"""
The workout loop: a state table engine that runs one session (runs on the GX10).

Each state has a handler that does its work and returns an event. TRANSITIONS maps
(state, event) to the next state. INTERRUPTS can come from any state and win over
the handler's event; they arrive on a queue from other threads (the listener hearing
a pain word, the tracker losing the patient). Pain stops the servos right away from
the thread that heard it, then the engine moves to SAFETY_STOP.

Every transition is timestamped in the session log, which is saved as JSON for the
therapist.
"""

import json
import logging
import os
import queue
import threading
import time
from dataclasses import asdict, dataclass, field

from robot_api.errors import RobotError
from workout.parts import classify_ready, classify_trouble, is_pain

log = logging.getLogger("workout")

READY_CHECK = "READY_CHECK"
INTRODUCE = "INTRODUCE"
DEMONSTRATE = "DEMONSTRATE"
PROMPT_START = "PROMPT_START"
MIRROR = "MIRROR"
SET_REVIEW = "SET_REVIEW"
PAUSED = "PAUSED"
SAFETY_STOP = "SAFETY_STOP"
END = "END"

RESUME = object()  # "go back to the state PAUSED interrupted"

TRANSITIONS = {
    (READY_CHECK, "ready"): INTRODUCE,
    (READY_CHECK, "not_ready"): READY_CHECK,
    (READY_CHECK, "gave_up"): END,
    (INTRODUCE, "done"): DEMONSTRATE,
    (DEMONSTRATE, "done"): PROMPT_START,
    (PROMPT_START, "started"): MIRROR,
    (MIRROR, "set_done"): SET_REVIEW,
    (SET_REVIEW, "next_set"): PROMPT_START,
    (SET_REVIEW, "next_exercise"): INTRODUCE,
    (SET_REVIEW, "workout_done"): END,
    (PAUSED, "resume"): RESUME,
    (SAFETY_STOP, "done"): END,
}

INTERRUPTS = {
    "pain": SAFETY_STOP,
    "left_frame": PAUSED,         # only acted on during MIRROR
    "no_response": END,
}


@dataclass
class Settings:
    ready_tries: int = 3          # ask "are you ready?" this many times before giving up
    ready_wait_s: float = 30      # wait between asks after "not yet"
    answer_timeout_s: float = 10  # how long to wait for an answer
    pause_timeout_s: float = 30   # how long PAUSED waits for the patient to come back
    countdown: bool = True        # 3-2-1 beeps before each set
    sessions_dir: str = "sessions"


@dataclass
class Session:
    """Everything the handlers share, and what gets saved."""
    plan: str
    started: float = field(default_factory=time.time)
    status: str = "running"       # completed | not_started | stopped_pain | no_response
    flagged: bool = False         # the therapist should look at this session
    exercise: int = 0             # index into plan.exercises
    set_number: int = 1
    ready_tries: int = 0
    reps_done: int = 0            # in the current set, kept across a pause
    resume_state: str = ""
    transitions: list = field(default_factory=list)
    sets: list = field(default_factory=list)
    notes: list = field(default_factory=list)


class Workout:
    def __init__(self, plan, robot, ears, tracker, coach, settings: Settings = None,
                 sleep=time.sleep):
        self.plan, self.robot, self.ears = plan, robot, ears
        self.tracker, self.coach = tracker, coach
        self.settings = settings or Settings()
        self.sleep = sleep
        self.session = Session(plan.name)
        self.interrupts = queue.Queue()
        self._cancel = threading.Event()  # set when an interrupt arrives; long steps check it
        self.state = READY_CHECK
        self.handlers = {
            READY_CHECK: self.ready_check, INTRODUCE: self.introduce,
            DEMONSTRATE: self.demonstrate, PROMPT_START: self.prompt_start,
            MIRROR: self.mirror, SET_REVIEW: self.set_review, PAUSED: self.paused,
            SAFETY_STOP: self.safety_stop,
        }
        ears.on_heard(self._heard)

    # ---- engine --------------------------------------------------------------

    def post(self, event: str):
        """Raise an interrupt from any thread."""
        if event == "pain":
            self.robot.stop_now()  # don't wait for the engine: freeze the servos now
        self.interrupts.put(event)
        self._cancel.set()

    def run(self) -> Session:
        try:
            while self.state != END:
                event = self.handlers[self.state]()
                interrupt = self._next_interrupt()
                # Leaving the camera view only matters mid-set; resuming any other
                # state would redo its work (log a set twice, skip a set number).
                if interrupt == "left_frame" and self.state != MIRROR:
                    interrupt = None
                if interrupt:
                    self._go(interrupt, INTERRUPTS[interrupt])
                    continue
                key = (self.state, event)
                if key not in TRANSITIONS:
                    raise RuntimeError(f"no transition for {key}")
                nxt = TRANSITIONS[key]
                self._go(event, self.session.resume_state if nxt is RESUME else nxt)
            self.finish()
        except BaseException:
            self.robot.stop_now()
            if self.session.status == "running":
                self.session.status = "error"
            self.save()
            raise
        return self.session

    def _next_interrupt(self):
        try:
            event = self.interrupts.get_nowait()
        except queue.Empty:
            return None
        # Pain outranks anything else that arrived at the same time.
        pending = [event]
        while not self.interrupts.empty():
            pending.append(self.interrupts.get_nowait())
        for e in ("pain", "no_response", "left_frame"):
            if e in pending:
                return e
        return pending[0]

    def _go(self, event: str, nxt: str):
        if nxt == PAUSED:
            self.session.resume_state = self.state
        self.session.transitions.append(
            {"t": round(time.time() - self.session.started, 2), "from": self.state,
             "event": event, "to": nxt})
        log.info("%s --%s--> %s", self.state, event, nxt)
        self.state = nxt
        self._cancel.clear()

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    # ---- talking -------------------------------------------------------------

    def _heard(self, text: str):
        """Everything the mic hears, from the listener's thread (#10 adds the LLM check)."""
        if is_pain(text):
            log.warning("pain words heard: %r", text)
            self.post("pain")

    def ask(self, question: str, retry: str = None):
        """Say a question and return the answer text. No answer twice -> no_response."""
        for prompt in (question, retry or f"Sorry, I didn't hear you. {question}"):
            if self.cancelled():
                return None
            self.robot.say(prompt)
            answer = self.ears.listen(self.settings.answer_timeout_s)
            if answer:
                log.info("PATIENT: %s", answer)
                if is_pain(answer):
                    self.post("pain")
                return answer
        self.post("no_response")
        return None

    @property
    def exercise(self):
        return self.plan.exercises[self.session.exercise]

    # ---- states --------------------------------------------------------------

    def ready_check(self) -> str:
        s = self.session
        if s.ready_tries == 0:
            self.robot.stand()
            self.robot.eyes(0, 120, 255)
        s.ready_tries += 1
        self.robot.say("Hi! Are you ready to start your workout?" if s.ready_tries == 1
                       else "Are you ready now?")
        answer = self.ears.listen(self.settings.answer_timeout_s)
        if answer:
            log.info("PATIENT: %s", answer)
            if is_pain(answer):
                self.post("pain")
                return "not_ready"
        verdict = classify_ready(answer)
        if verdict == "ready":
            return "ready"
        if s.ready_tries >= self.settings.ready_tries:
            s.status = "not_started"
            self.robot.say("That's okay. We can do your workout another time.")
            return "gave_up"
        if verdict == "not_ready":
            self.robot.say("No problem, take your time.")
            self.sleep(self.settings.ready_wait_s)
        return "not_ready"

    def introduce(self) -> str:
        ex = self.exercise
        first = self.session.exercise == 0
        self.robot.show(ex.name)
        self.robot.say(f"{'Our first' if first else 'Next'} exercise is {ex.name.lower()}. {ex.cue}")
        return "done"

    def demonstrate(self) -> str:
        self.robot.say("Watch me first.")
        try:
            self.robot.demonstrate(self.exercise.demo, self.cancelled)
        except RobotError as e:  # e.g. a servo is too hot: carry on with words only
            log.warning("demo failed: %s", e)
            self.session.notes.append(f"demo skipped for {self.exercise.name}: {e}")
            self.robot.say("I need to rest my arms, so I'll just talk you through it.")
        return "done"

    def prompt_start(self) -> str:
        ex, s = self.exercise, self.session
        if s.set_number == 1:
            self.robot.say(f"Your turn! Let's do {ex.reps} reps.")
        else:
            self.robot.say(f"Set {s.set_number} of {ex.sets}. {ex.reps} reps.")
        if self.settings.countdown:
            for n in ("3", "2", "1"):
                self.robot.show(n)
                self.robot.beep()
                self.sleep(0.6)
        self.robot.say("Go!")
        s.reps_done = 0
        return "started"

    def mirror(self) -> str:
        result = self.tracker.run_set(self.exercise, self.post, self.cancelled,
                                      reps_done=self.session.reps_done)
        self.session.reps_done = result.reps
        self._result = result
        return "set_done"

    def set_review(self) -> str:
        ex, s, result = self.exercise, self.session, self._result
        self.robot.stand()
        self.robot.say(self.coach.feedback(ex, result))
        answer = self.ask("Did you have any trouble with that set?")
        trouble = classify_trouble(answer)
        s.sets.append({"exercise": ex.name, "set": s.set_number, "reps": result.reps,
                       "target_reps": result.target_reps, "deviations": result.deviations,
                       "answer": answer, "trouble": trouble})
        if trouble == "pain":
            self.post("pain")
            return "next_set"  # overridden by the interrupt
        if trouble == "difficulty":
            self.robot.say("Thanks for telling me. I'll let your therapist know.")
        if s.set_number < ex.sets:
            s.set_number += 1
            return "next_set"
        s.set_number = 1
        if s.exercise + 1 < len(self.plan.exercises):
            s.exercise += 1
            return "next_exercise"
        s.status = "completed"
        return "workout_done"

    def paused(self) -> str:
        self.robot.say("I can't see you. Are you okay?")
        waited = 0.0
        while not self.tracker.patient_visible():
            if waited >= self.settings.pause_timeout_s:
                self.post("no_response")
                return "resume"  # overridden by the interrupt
            self.sleep(1)
            waited += 1
        self.robot.say("There you are. Let's keep going.")
        return "resume"

    def safety_stop(self) -> str:
        s = self.session
        self.robot.stop_now()
        s.status, s.flagged = "stopped_pain", True
        self.robot.eyes(255, 120, 0)
        self.robot.say("Let's stop here. I'll let your therapist know how you're feeling.")
        return "done"

    # ---- end -----------------------------------------------------------------

    def finish(self):
        s = self.session
        if s.status == "running":
            s.status = "no_response"
            s.flagged = True
            self.robot.say("I haven't heard from you, so let's stop for today.")
        if s.status == "completed":
            self.robot.say("That's the whole workout, great job! " + self.summary())
        try:
            self.robot.stand()
        except RobotError as e:
            log.warning("final stand failed: %s", e)
        self.save()

    def summary(self) -> str:
        """One sentence about the session, spoken at the end."""
        names = list(dict.fromkeys(r["exercise"] for r in self.session.sets))
        clean = sum(1 for r in self.session.sets if not r["deviations"])
        return (f"You finished all {len(names)} exercises, "
                f"with great form on {clean} of {len(self.session.sets)} sets.")

    def save(self) -> str:
        s = self.session
        os.makedirs(self.settings.sessions_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(s.started))
        path = os.path.join(self.settings.sessions_dir, f"{stamp}.json")
        with open(path, "w") as f:
            json.dump({**asdict(s), "ended": time.time()}, f, indent=2)
        self.saved_path = path
        return path
