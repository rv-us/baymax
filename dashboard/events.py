"""One stream of live events for the browser (Server-Sent Events at /api/events).

Any thread calls hub.publish(kind, data). The latest event of each "sticky" kind is
replayed to a browser when it connects, so a page opened mid-session is correct at once.
"""
from __future__ import annotations

import asyncio
import json
import threading

STICKY = ("status", "safety", "live", "playback", "tunnel", "recorder", "calibration", "mirror", "gx10", "voice")


class Hub:
    def __init__(self):
        self._lock = threading.Lock()
        self._subs: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self.last: dict[str, dict] = {}

    def publish(self, kind: str, data: dict) -> None:
        with self._lock:
            if kind in STICKY:
                self.last[kind] = data
            subs = list(self._subs)
        for loop, q in subs:
            try:
                loop.call_soon_threadsafe(self._offer, q, (kind, data))
            except RuntimeError:  # that browser's event loop is gone
                pass

    @staticmethod
    def _offer(q: asyncio.Queue, item) -> None:
        if q.full():  # a stalled browser loses its oldest event, never blocks the robot
            q.get_nowait()
        q.put_nowait(item)

    async def stream(self):
        q: asyncio.Queue = asyncio.Queue(maxsize=400)
        entry = (asyncio.get_running_loop(), q)
        with self._lock:
            replay = list(self.last.items())
            self._subs.append(entry)
        try:
            for kind, data in replay:
                yield _frame(kind, data)
            while True:
                try:
                    kind, data = await asyncio.wait_for(q.get(), timeout=15)
                    yield _frame(kind, data)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            with self._lock:
                self._subs.remove(entry)


def _frame(kind: str, data: dict) -> str:
    return f"event: {kind}\ndata: {json.dumps(data)}\n\n"
