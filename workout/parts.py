"""
The pieces the workout loop plugs in. Each has a simple stand-in so the loop runs
end to end today; later issues replace them without changing the loop:

    Ears        hear the patient's answer       voice.Listener (#9) | keyboard | scripted
    understand  ready? trouble? pain?           keyword rules -> LLM classifier (#10)
    Tracker     run one set, count reps         fake results -> pose tracking (#11-#13)
    Coach       form feedback sentence          templates -> LLM coach (#15)
"""

import re
import select
import sys
import time
from dataclasses import dataclass, field

# ---- Ears -------------------------------------------------------------------


class ListenerEars:
    """The robot's microphone through voice.Listener."""

    def __init__(self, listener):
        self.listener = listener

    def listen(self, timeout_s: float):
        return self.listener.listen(timeout_s=timeout_s)

    def on_heard(self, callback):
        """callback(text) for everything heard, even between questions (pain words)."""
        self.listener.on_transcript(lambda t: callback(t.text))


class KeyboardEars:
    """Type the patient's answers in the terminal (testing without the robot's mic).
    An empty line, or no input before the timeout, counts as silence."""

    def listen(self, timeout_s: float):
        print(f"  [patient, {timeout_s:.0f} s] > ", end="", flush=True)
        ready, _, _ = select.select([sys.stdin], [], [], timeout_s)
        if not ready:
            print("(silence)")
            return None
        return sys.stdin.readline().strip() or None

    def on_heard(self, callback):
        pass  # typed answers only arrive through listen()


class ScriptedEars:
    """Answers from a list, in order (tests). None means silence."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.asked = 0

    def listen(self, timeout_s: float):
        self.asked += 1
        return self.answers.pop(0) if self.answers else None

    def on_heard(self, callback):
        pass


# ---- Understanding answers (keyword stand-in for #10) -------------------------

PAIN_WORDS = ("hurt", "hurts", "pain", "painful", "ow", "ouch", "stop", "sore", "sharp")
_YES = ("yes", "yeah", "yep", "ready", "sure", "ok", "okay", "let's go", "lets go", "go ahead")
_NO = ("no", "not yet", "wait", "hold on", "later", "not ready", "give me", "minute")
_HARD = ("hard", "difficult", "tough", "tired", "heavy", "couldn't", "could not", "too easy")


def _has(text: str, phrases) -> bool:
    t = " " + re.sub(r"[^a-z' ]", " ", text.lower()) + " "
    return any(f" {p} " in t for p in phrases)


def is_pain(text: str) -> bool:
    """Words that stop the workout right away, wherever they are heard.
    "Nothing hurts" / "no pain" don't count."""
    if not text or _has(text, ("nothing hurts", "no pain", "doesn't hurt", "does not hurt",
                               "didn't hurt", "not painful")):
        return False
    return _has(text, PAIN_WORDS)


def classify_ready(text) -> str:
    """ready | not_ready | unclear"""
    if not text:
        return "unclear"
    if _has(text, _NO) and not _has(text, ("i'm ready", "im ready", "i am ready")):
        return "not_ready"
    return "ready" if _has(text, _YES) else "unclear"


def classify_trouble(text) -> str:
    """none | difficulty | pain"""
    if is_pain(text):
        return "pain"
    if text and _has(text, _HARD):
        return "difficulty"
    return "none"  # including silence: no complaint


# ---- Tracker (stand-in for pose tracking, #11-#13) ----------------------------


@dataclass
class SetResult:
    reps: int
    target_reps: int
    deviations: list = field(default_factory=list)   # e.g. "left elbow bent 30° at the top"
    seconds: float = 0.0
    finished: bool = True                            # False if interrupted


class FakeTracker:
    """Pretends the patient does every set. `deviations` gives each set's form notes in
    order. `events` maps a set number (1-based, counted across the workout) to an
    interrupt the tracker raises during that set, e.g. {2: "pain"} (tests)."""

    def __init__(self, deviations=None, rep_seconds: float = 0.0, events=None):
        self.deviations = list(deviations or [])
        self.rep_seconds = rep_seconds
        self.events = dict(events or {})
        self.sets_run = 0

    def run_set(self, exercise, post_event, cancelled, reps_done: int = 0) -> SetResult:
        self.sets_run += 1
        event = self.events.pop(self.sets_run, None)
        started = time.time()
        reps = reps_done
        while reps < exercise.reps:
            if event and reps >= exercise.reps // 2:
                post_event(event)
                event = None
            if cancelled():
                return SetResult(reps, exercise.reps, [], time.time() - started, finished=False)
            time.sleep(self.rep_seconds)
            reps += 1
        notes = self.deviations.pop(0) if self.deviations else []
        return SetResult(reps, exercise.reps, notes, time.time() - started)

    def patient_visible(self) -> bool:
        return True


# ---- Coach (template stand-in for the LLM coach, #15) -------------------------


class TemplateCoach:
    def feedback(self, exercise, result: SetResult) -> str:
        if not result.deviations:
            return "Great form on that set!"
        return f"Nice work! One thing to watch: {result.deviations[0]}."
