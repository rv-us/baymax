"""Is what the microphone heard just the robot's own voice coming back?

The microphone is ignored while the robot talks, but sound lags (the speaker, the room, the network), so the end
of a sentence can still arrive as the patient's "answer". The dashboard knows what the robot said, so a line that
matches it is dropped. A real answer ("yes", "stop", "that hurts") never matches a whole sentence of the robot's.
"""
from __future__ import annotations

import difflib
import time

WINDOW_S = 30.0       # only what the robot said this recently can be coming back
SIMILAR = 0.72        # how much alike (0 to 1) a line and a sentence of the robot's must be
MIN_WORDS = 3         # a fragment of what it said counts only if it is this long: "ready" alone is an answer


def _words(text: str) -> list:
    return "".join(c if c.isalnum() or c == " " else " " for c in text.lower()).split()


def is_echo(text: str, said: list, now: float | None = None) -> bool:
    """`said` is [(time, what the robot said)], as Gateway.said keeps it."""
    now = time.time() if now is None else now
    heard = _words(text)
    if not heard:
        return False
    for when, line in said:
        if now - when > WINDOW_S:
            continue
        spoken = _words(line)
        if not spoken:
            continue
        if difflib.SequenceMatcher(None, heard, spoken).ratio() >= SIMILAR:
            return True
        if len(heard) >= MIN_WORDS and all(w in spoken for w in heard):
            return True                                     # a stretch of it, or its start or end
    return False
