"""Text to animation: the model on the GX10 writes a motion in degrees, the code makes it safe.

    "wave hello with the right arm, slowly"  ->  an unsaved animation for the previewer

The GX10 runs Qwen through Ollama (the same endpoint and model as the coach and as
`python -m robot_api --model qwen3.6:35b`). The model fills a small schema: a name, a
cue, and keyframes of arm angles in degrees. It never writes servo pulses. poses.py
turns the degrees into pulses inside robot_api's limits, the calibration profile
narrows them further, and workout.plan.check_keyframes has the last word, so nothing
the model says can become an out-of-range command.

When the GX10 can't be reached (or in dry run), a rule-based mock builds a motion from
the words in the prompt. The result is labelled "mock" so nobody mistakes it for the model.
"""
from __future__ import annotations

import re

import httpx

from workout.plan import MAX_KEYFRAMES, PlanError

from . import poses, store
from .coach import _reason, parse_json

ARM = {"type": "object",
       "properties": {"forward": {"type": "number"}, "side": {"type": "number"}, "elbow": {"type": "number"}},
       "required": ["forward", "side", "elbow"]}
SCHEMA = {
    "type": "object",
    "properties": {
        "name": {"type": "string"},
        "cue": {"type": "string"},
        "kind": {"type": "string", "enum": ["exercise", "emote"]},
        "keyframes": {"type": "array", "minItems": 2, "maxItems": 16, "items": {
            "type": "object",
            "properties": {"seconds": {"type": "number"}, "hold_seconds": {"type": "number"},
                           "left": ARM, "right": ARM,
                           "head_turn": {"type": "number"}, "head_nod": {"type": "number"}},
            "required": ["seconds", "left", "right"]}},
    },
    "required": ["name", "cue", "kind", "keyframes"],
}

SYSTEM_PROMPT = (
    "You design short upper-body motions for a small humanoid robot that demonstrates gentle "
    "physical therapy exercises and friendly gestures to older adults. Reply with JSON only.\n"
    "Each keyframe is a pose the robot moves into. Angles are in degrees for each arm:\n"
    "- forward: 0 = arm hanging down, 90 = straight out in front, 160 = overhead (0 to 170)\n"
    "- side: 0 = at the side, 90 = straight out sideways like a T (0 to 95)\n"
    "- elbow: 0 = straight, 70 = fully bent (0 to 70)\n"
    "Optional head_turn (-45 right to 45 left) and head_nod (-45 down to 45 up).\n"
    "seconds is the time to move into the pose (0.4 to 6; slow and gentle is 1.5 to 2.5), "
    "hold_seconds an optional pause there.\n"
    "Rules: 2 to 16 keyframes. The robot starts with both arms down, and the last keyframe must "
    "return both arms to forward 0, side 0, elbow 0. The legs never move. Keep it calm: no "
    "movement faster than 0.4 seconds. name is a short title, cue is one sentence the robot "
    "says to explain the move, kind is exercise or emote."
)

_SLOW = re.compile(r"\b(slow|slowly|gentle|gently|calm|relaxed|careful)\b")
_FAST = re.compile(r"\b(fast|quick|quickly|brisk|energetic|lively)\b")
_NUM = {"one": 1, "once": 1, "two": 2, "twice": 2, "three": 3, "four": 4, "five": 5}

# (words that pick the move, title, cue, kind, builder)
MOCK_MOVES = [
    (("wave", "hello", "greet", "goodbye", "bye", "hi "), "Wave", "Give a friendly wave.", "emote",
     lambda s, n, side: poses.wave(s, side or "right", n or 3)),
    (("clap", "applaud", "applause"), "Clap", "Clap your hands together.", "emote",
     lambda s, n, side: poses.clap(s, n or 3)),
    (("cheer", "celebrat", "hooray", "victory", "yay"), "Cheer", "Arms up, well done!", "emote",
     lambda s, n, side: poses.cheer(s)),
    (("hug", "embrace"), "Hug", "A big gentle hug.", "emote", lambda s, n, side: poses.hug(s)),
    (("shrug", "don't know", "dont know", "unsure"), "Shrug", "A little shrug.", "emote",
     lambda s, n, side: poses.shrug(s)),
    (("circle", "roll", "rotate", "windmill"), "Arm circles", "Draw slow circles with both arms.", "exercise",
     lambda s, n, side: poses.arm_circles(s) * (n or 1)),
    (("curl", "bicep", "elbow", "bend"), "Elbow curls", "Bend your elbows up, then straighten them slowly.",
     "exercise", lambda s, n, side: poses.elbow_curls(s) * (n or 1)),
    (("press", "push up", "push overhead"), "Shoulder press", "Press both hands up overhead, then lower them.",
     "exercise", lambda s, n, side: poses.shoulder_press(s) * (n or 1)),
    (("goalpost", "chest", "open", "stretch"), "Goalpost stretch",
     "Open your arms like goalposts and feel the stretch across your chest.", "exercise",
     lambda s, n, side: poses.goalpost_stretch(s) * (n or 1)),
    (("alternat", "one arm", "each arm", "one at a time"), "Alternating reach",
     "Reach one arm up, lower it, then the other.", "exercise",
     lambda s, n, side: poses.alternating_reach(s) * (n or 1)),
    (("overhead", "reach", "above", "ceiling", "sky", "up high"), "Overhead reach",
     "Reach both arms up over your head, then lower them slowly.", "exercise",
     lambda s, n, side: poses.overhead_reach(s) * (n or 1)),
    (("t pose", "t-pose", "balance", "hold", "airplane", "wings"), "T hold",
     "Lift both arms out to the side and hold them there.", "exercise",
     lambda s, n, side: poses.t_hold(s) * (n or 1)),
    (("side", "lateral", "out to"), "Side raise", "Lift both arms out to the side, then lower them slowly.",
     "exercise", lambda s, n, side: poses.side_raise(s) * (n or 1)),
    (("front", "forward", "raise", "lift"), "Front raise",
     "Lift both arms out in front of you, then lower them slowly.", "exercise",
     lambda s, n, side: poses.front_raise(s) * (n or 1)),
    (("sway", "dance", "wiggle", "groove"), "Sway", "Sway gently from side to side.", "emote",
     lambda s, n, side: poses.sway(s) * (n or 2)),
]


def mock(prompt: str) -> dict:
    """A motion from the words in the prompt. Deterministic; no model."""
    text = " " + prompt.lower() + " "
    speed = 1.35 if _SLOW.search(text) else 0.75 if _FAST.search(text) else 1.0
    times = next((v for k, v in _NUM.items() if re.search(rf"\b{k}\b", text)), None)
    digit = re.search(r"\b([1-5])\s*(?:x|times|reps?)\b", text)
    if digit:
        times = int(digit.group(1))
    side = "left" if re.search(r"\bleft\b", text) else "right" if re.search(r"\bright\b", text) else None
    for words, title, cue, kind, build in MOCK_MOVES:
        if any(w in text for w in words):
            frames, matched = build(speed, times, side), True
            break
    else:
        title, cue, kind, matched = "Gentle sway", "Sway gently from side to side.", "emote", False
        frames = poses.sway(speed) * 2
    return {"name": title, "cue": cue, "kind": kind, "keyframes": frames[:MAX_KEYFRAMES], "matched": matched}


def from_model(reply: dict) -> dict:
    """The model's degrees as keyframes. Raises PlanError if the reply isn't usable."""
    if not isinstance(reply, dict) or not isinstance(reply.get("keyframes"), list) or len(reply["keyframes"]) < 2:
        raise PlanError("the model's reply had no keyframes")
    frames = []
    for i, kf in enumerate(reply["keyframes"][:MAX_KEYFRAMES], start=1):
        if not isinstance(kf, dict):
            raise PlanError(f"keyframe {i} from the model isn't an object")
        try:
            seconds = float(kf.get("seconds") or 1.5)
            hold = float(kf.get("hold_seconds") or 0)
            turn, nod = float(kf.get("head_turn") or 0), float(kf.get("head_nod") or 0)
        except (TypeError, ValueError) as e:
            raise PlanError(f"keyframe {i} from the model has a value that isn't a number") from e
        left = kf.get("left") if isinstance(kf.get("left"), dict) else {}
        right = kf.get("right") if isinstance(kf.get("right"), dict) else {}
        frames.append(poses.frame(max(0.4, min(6.0, seconds)) * 1000, left=left, right=right,
                                  hold_ms=max(0.0, min(5.0, hold)) * 1000, turn=turn, nod=nod))
    frames.append(poses.frame(1500, poses.REST))  # always end at rest, whatever the model did
    if frames[-2]["servos"] == frames[-1]["servos"] and "head" not in frames[-2]:
        frames.pop()
    return {"name": str(reply.get("name") or "Generated motion").strip()[:60] or "Generated motion",
            "cue": str(reply.get("cue") or "").strip()[:200],
            "kind": reply.get("kind") if reply.get("kind") in ("exercise", "emote") else "emote",
            "keyframes": frames[:MAX_KEYFRAMES]}


class Generator:
    def __init__(self, settings, limiter):
        self.settings, self.limiter = settings, limiter

    def ask(self, prompt: str) -> dict:
        s = self.settings
        r = httpx.post(f"{s.ollama_url}/api/chat", timeout=s.coach_timeout_s, json={
            "model": s.model, "stream": False, "think": False, "keep_alive": -1,
            "format": SCHEMA, "options": {"temperature": 0.4},
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": prompt}]})
        r.raise_for_status()
        return parse_json((r.json().get("message") or {}).get("content", ""))

    def finish(self, draft: dict, prompt: str, source: str) -> dict:
        """Clamp to the calibration profile and run the animation checks."""
        limited = 0
        for kf in draft["keyframes"]:
            safe = self.limiter.clamp_pose(kf["servos"])
            limited += sum(1 for j in safe if safe[j] != kf["servos"][j])
            kf["servos"] = safe
        anim = store.check_animation({
            "name": draft["name"], "keyframes": draft["keyframes"], "source": "generated",
            "kind": draft["kind"], "prompt": prompt, "generator": source,
            "exercise": {"cue": draft["cue"]}})
        return {**anim, "id": None, "limited_joints": limited}

    def generate(self, prompt: str, force_mock: bool = False) -> dict:
        """Never raises for a model failure: {"animation", "source": "gx10" | "mock", "note"}."""
        prompt = " ".join(str(prompt or "").split())[:400]
        if len(prompt) < 3:
            raise PlanError("Describe the motion you want in a few words.")
        s, notes = self.settings, []
        if s.stub_llm:
            notes.append("Dry run: no model is connected, so the mock generator made this.")
        elif force_mock:
            notes.append("Mock mode was chosen, so the model on the GX10 wasn't asked.")
        else:
            try:
                draft = from_model(self.ask(prompt))
                return {"animation": self.finish(draft, prompt, f"gx10:{s.model}"), "source": "gx10",
                        "model": s.model, "note": None}
            except (httpx.HTTPError, PlanError, ValueError, KeyError, TypeError) as e:
                notes.append((_reason(e, s) if isinstance(e, httpx.HTTPError)
                              else f"The model's motion wasn't usable ({e}).")
                             + " The mock generator made this instead.")
        draft = mock(prompt)
        if not draft.pop("matched"):
            notes.append("The mock generator didn't recognise a move in that prompt, so it made a gentle "
                         "sway. Try words like wave, raise, reach, circle, curl, stretch or clap.")
        return {"animation": self.finish(draft, prompt, "mock"), "source": "mock", "model": None,
                "note": " ".join(notes)}
