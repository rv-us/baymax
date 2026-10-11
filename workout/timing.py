"""
Where a workout session's time goes, for finding hang-ups (python -m workout --timing).

Every robot API call, every wait for an answer and every deliberate pause is
recorded with the state it happened in. Time in a state that none of those explain
("other") is the loop's own overhead, and should be near zero.
"""

import collections
import threading
import time
from dataclasses import dataclass


@dataclass
class Span:
    kind: str       # call | listen | sleep
    name: str       # endpoint path, or what was waited for
    state: str
    start: float
    seconds: float


class Timings:
    def __init__(self):
        self.spans = []
        self.visits = []             # (state, start, seconds) for each state visit
        self.speech_latency = []     # seconds from end of speech to transcript
        self.current_state = lambda: "?"
        self._lock = threading.Lock()

    def record(self, kind: str, name: str, start: float):
        span = Span(kind, name, self.current_state(), start, time.time() - start)
        with self._lock:
            self.spans.append(span)
        return span

    def timed(self, kind: str, name: str, fn, *args, **kwargs):
        start = time.time()
        try:
            return fn(*args, **kwargs)
        finally:
            self.record(kind, name, start)

    def visit(self, state: str, start: float):
        self.visits.append((state, start, time.time() - start))

    def report(self) -> str:
        lines = ["", "== Time per state (each visit)",
                 f"   {'state':13} {'total':>6} {'robot':>6} {'listen':>6} {'pause':>6} {'other':>6}"]
        for state, start, seconds in self.visits:
            inside = [s for s in self.spans if start <= s.start < start + seconds
                      and s.state == state]
            by = collections.Counter()
            for s in inside:
                by[s.kind] += s.seconds
            other = seconds - sum(by.values())
            flag = "  <-- unexplained" if other > 0.5 else ""
            lines.append(f"   {state:13} {seconds:6.1f} {by['call']:6.1f} {by['listen']:6.1f} "
                         f"{by['sleep']:6.1f} {other:6.2f}{flag}")

        lines += ["", "== Robot API calls", f"   {'endpoint':24} {'count':>5} {'avg s':>6} {'max s':>6}"]
        calls = collections.defaultdict(list)
        for s in self.spans:
            if s.kind == "call":
                calls[s.name].append(s.seconds)
        for name, xs in sorted(calls.items(), key=lambda kv: -max(kv[1])):
            lines.append(f"   {name:24} {len(xs):5} {sum(xs) / len(xs):6.2f} {max(xs):6.2f}")

        lines += ["", "== Slowest single steps"]
        for s in sorted(self.spans, key=lambda s: -s.seconds)[:6]:
            lines.append(f"   {s.seconds:6.2f} s  {s.kind:6} {s.name:28} in {s.state}")

        if self.speech_latency:
            xs = self.speech_latency
            lines += ["", f"== Speech to text: {len(xs)} answers, text arrived "
                          f"{sum(xs) / len(xs):.2f} s (max {max(xs):.2f} s) after the speaker stopped"]
        return "\n".join(lines)
