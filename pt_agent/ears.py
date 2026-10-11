"""What the PT agent hears: the robot's microphone (voice.Listener on the GX10) or typed text.

Both give the same three calls:
    listen(timeout_s)   the patient's answer to a question, or None
    poll()              the next unprompted utterance, if any (used while looking for the patient)
    speaking()          context manager around the robot's own speech, so it isn't heard
and call on_heard callbacks with every utterance (pain words stop the robot from there).
"""
from __future__ import annotations

import collections
import contextlib
import queue
import sys
import threading

from workout.parts import _has, is_pain  # noqa: F401  (is_pain re-exported for the tools)

# Said to get the robot's attention before the session starts.
WAKE_WORDS = ("baymax", "hey robot", "hello", "hi", "hey", "over here", "i'm here", "im here",
              "i'm ready", "im ready", "ready", "that's me", "thats me", "i'm your patient")


def is_wake(text) -> bool:
    return bool(text) and _has(text, WAKE_WORDS)


class MicEars:
    """The robot's microphone, through a started voice.Listener."""

    def __init__(self, listener):
        self.listener = listener
        self._recent = collections.deque(maxlen=20)
        self._callbacks = []
        listener.on_transcript(self._heard)

    def _heard(self, transcript):
        self._recent.append(transcript.text)
        for cb in self._callbacks:
            cb(transcript.text)

    def on_heard(self, callback):
        self._callbacks.append(callback)

    def listen(self, timeout_s: float):
        return self.listener.listen(timeout_s=timeout_s)

    def poll(self):
        return self._recent.popleft() if self._recent else None

    def clear(self):
        self._recent.clear()

    def speaking(self):
        return self.listener.robot_speaking()


class KeyboardEars:
    """Type what the patient says. Lines starting with "/" are operator commands, e.g.
    /wave (pretend someone waved, for testing without a camera).

    Typed live, each line is heard (pain check) as soon as it is entered. Piped from a
    file, each line is heard when the agent gets to it, so a script plays out in order."""

    def __init__(self, commands: dict | None = None, stream=None):
        self.commands = commands or {}
        self.stream = stream or sys.stdin
        self.live = self.stream.isatty()
        self._lines = queue.Queue()
        self._callbacks = []
        threading.Thread(target=self._read, daemon=True).start()

    def on_heard(self, callback):
        self._callbacks.append(callback)

    def _read(self):
        for line in self.stream:
            line = line.strip()
            if not line:
                continue
            if self.live:
                if line.startswith("/"):
                    self._command(line)
                    continue
                self._notify(line)
            self._lines.put(line)

    def _command(self, line):
        fn = self.commands.get(line.split()[0])
        if fn:
            print(f"  [operator] {line}")
            fn()
        else:
            print(f"  unknown command {line}; known: {', '.join(self.commands) or 'none'}")

    def _notify(self, text):
        for cb in self._callbacks:
            cb(text)

    def _next(self, timeout):
        """Next patient line; in piped mode, commands and the pain check run here."""
        while True:
            try:
                line = self._lines.get(timeout=timeout) if timeout else self._lines.get_nowait()
            except queue.Empty:
                return None
            if self.live:
                return line
            if line.startswith("/"):
                self._command(line)
                if timeout is None:
                    return None
                continue
            print(f"  [patient] {line}")
            self._notify(line)
            return line

    def listen(self, timeout_s: float):
        if self.live:
            print(f"  (type the patient's reply, {timeout_s:.0f} s)")
        return self._next(timeout_s)

    def poll(self):
        return self._next(None)

    def clear(self):
        if self.live:
            while not self._lines.empty():
                self._lines.get_nowait()

    def speaking(self):
        return contextlib.nullcontext()
