"""The scripted demo, as plain data: every line the robot says and every move it makes.

Nothing here listens, looks or asks a model. The patient "waves", "says ow" and
"answers" only in the sense that the presenter does it out loud and presses Enter, so
the robot always does the same thing in the same order. That is on purpose: a demo
room has bad Wi-Fi, loud noise and strangers. What it shows is the real robot API, the
real exercise animations and the real stop, driven by a fixed script instead of by
perception. Say so when you present it; the session log it writes is marked
"source": "scripted-demo".

A step is (kind, argument, note). `cue` steps wait for the presenter (or a fixed pause
with --auto); all others run on fixed timings.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Step:
    kind: str       # say, icon, text, head, arm, walk, turn, action, animate, count, pause, cue, freeze, mark
    arg: object = None
    note: str = ""  # shown to the presenter, never spoken


GREETING = "Hi, I'm Baymax. I saw you wave. I'm your physical therapy assistant."
APPROACH = "I'm going to walk over to you now."
FACE = "I'm going to turn to face you."
SAFETY_INTRO = "Before we start, let me show you what to do if something goes wrong."
SAFETY_WARNING = "I am going to fall down on purpose now, so please don't worry. Three, two, one."
SAFETY_HELP = "If at any time you get hurt or fall, please say help, or call your handler."
SAFETY_DONE = "I'm okay, that was just practice. Remember, if you ever need me, just say help."
TEMP_ASK = "First, a quick temperature check. Please rest the back of your hand on top of my right hand."
TEMP_DONE = "Thank you. That looks normal."
CHECK_IN = "How are you feeling today?"
CHECK_REPLY = "Good. Let's start with front arm raises. Watch me first."
YOUR_TURN = "Now you. Lift both arms to shoulder height, then lower them slowly. I'll count."
FORM = "Nice work. Next time, keep your elbows straight at the top."
TROUBLE = "Did you have any trouble with that set?"
TROUBLE_REPLY = "Okay, thanks. I've noted that."
SET_TWO = "Let's do one more set. Ready? Go."
PAIN_STOP = "Okay, I've stopped. Are you all right?"
PAIN_REPLY = "I'm glad you're okay. I've flagged that set for your therapist, and we'll stop here for today."
GOODBYE = "Great effort today. I'll send my notes to your therapist. Goodbye!"

REPS_SET_ONE = 5
REPS_SET_TWO = 2     # the pain stop lands after this many reps of the second set
REP_EVERY_S = 1.6
NUMBERS = ["one", "two", "three", "four", "five", "six", "seven", "eight"]


def build(walk: bool = False, fall: bool = False) -> list[Step]:
    """The whole demo, in order. walk: really walk to the patient (off by default, since
    the robot is usually on a table). fall: include the fall safety demo (needs 40 cm of
    clear, flat floor, and the robot gets up on its own)."""
    s: list[Step] = []
    add = lambda kind, arg=None, note="": s.append(Step(kind, arg, note))  # noqa: E731

    # 1. Looking for the patient: question mark, a fixed head sweep, then wait for the wave.
    add("mark", "start")
    add("action", "stand", "stand up")
    add("icon", "question")
    for pan in (1350, 1650, 1500):
        add("head", {"pan": pan, "tilt": 1500, "ms": 900})
    add("cue", "Wave at Baymax", "patient waves")

    # 2. Found them.
    add("icon", "smile")
    add("say", GREETING)
    add("say", APPROACH if walk else FACE)
    if walk:
        add("walk", 2)
    else:
        add("turn", 1)

    # 3. Safety demo, only if asked for.
    if fall:
        add("say", SAFETY_INTRO)
        add("say", SAFETY_WARNING)
        add("action", "lie_down", "falls on its back on purpose")
        add("say", SAFETY_HELP)
        add("action", "stand_up_back", "gets back up")
        add("action", "stand")
        add("say", SAFETY_DONE)
        add("mark", "safety_demo")

    # 4. Temperature check: the motion is real, the reading is not (see the README).
    add("say", TEMP_ASK)
    add("arm", ("right", "forward"))
    add("pause", 5)
    add("arm", ("right", "down"))
    add("say", TEMP_DONE)
    add("mark", "temperature_simulated")
    add("say", CHECK_IN)
    add("cue", "Answer: \"I'm doing fine\"", "patient answers")
    add("say", CHECK_REPLY)

    # 5. Set 1: demonstrate, count, feedback, question.
    add("animate", "front_arm_raise", "demonstrates front arm raises")
    add("say", YOUR_TURN)
    add("icon", "smile")
    add("count", REPS_SET_ONE, "patient does 5 reps")
    add("say", FORM)
    add("say", TROUBLE)
    add("cue", "Answer: \"No, it was fine\"", "patient answers")
    add("say", TROUBLE_REPLY)
    add("mark", "set_1_done")

    # 6. Set 2 is interrupted by pain.
    add("say", SET_TWO)
    add("count", REPS_SET_TWO, "patient starts set 2")
    add("cue", "Say \"ow, that hurts\" now", "patient says a pain word")
    add("freeze", None, "stops where it is")
    add("text", "STOP")
    add("say", PAIN_STOP)
    add("mark", "pain_stop")
    add("cue", "Answer: \"I'm okay\"", "patient answers")
    add("say", PAIN_REPLY)

    # 7. Wrap up.
    add("icon", "heart")
    add("say", GOODBYE)
    add("mark", "end")
    return s
