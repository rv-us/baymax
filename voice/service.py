"""The GX10 owns the robot's microphone: what the patient says, for whichever dashboard runs the session.

    python -m voice.service                 # 0.0.0.0:9200
    python -m voice.service --mute-tail 1.0

`voice.Listener` (Silero VAD, then Whisper on the GPU) turns the robot's microphone stream into text. The
dashboard, wherever it runs (this machine or a laptop), asks for it and tells this service when the robot is
talking, the same way it gets the camera from the pose service:

    GET  /health                       {"ok": true, "service": "baymax-voice", "seq": n, "muted": bool}
    GET  /heard?after=SEQ&wait=S       {"seq": n, "items": [{"seq", "utt", "final", "text", "age", "duration"}]}:
                                        what was said after SEQ (waits up to S seconds for something). Words arrive
                                        AS THEY ARE SPOKEN: every ~0.25 s the utterance so far (`final` false), then
                                        the whole of it once the speaker pauses (`final` true); `utt` ties them together.
                                        `age` is seconds since the speech (so far) ended
    POST /speaking {"on": true|false}  the robot starts or stops talking: the microphone is ignored while it does,
                                        and for --mute-tail seconds after (the sound takes a moment to come back)

The dashboard says "on" before the robot speaks and "off" when it has finished, so the robot never hears itself
and never takes its own words for the patient's answer. If the "off" never arrives (the dashboard crashed) the
microphone is un-muted after MAX_MUTE_S rather than staying deaf. There is no login: use it on a trusted network.
"""
from __future__ import annotations

import argparse
import collections
import contextlib
import threading
import time

DEFAULT_PORT = 9200
SERVICE = "baymax-voice"
MAX_MUTE_S = 90.0        # longer than any one thing the robot says
KEEP = 300               # transcripts remembered (partials too)


class VoiceHub:
    """The transcripts and the mute switch around a voice.Listener."""

    def __init__(self, listener):
        self.listener = listener
        self.cond = threading.Condition()
        self.items = collections.deque(maxlen=KEEP)
        self.seq = 0
        self._stack = contextlib.ExitStack()
        self._muted_at = None
        listener.on_transcript(self._add)
        if hasattr(listener, "on_partial"):
            listener.on_partial(self._add)

    def _add(self, t) -> None:
        with self.cond:
            self.seq += 1
            self.items.append({"seq": self.seq, "utt": getattr(t, "utt", 0), "final": getattr(t, "final", True),
                               "text": t.text, "end": t.end, "duration": round(t.end - t.start, 2)})
            self.cond.notify_all()

    def heard(self, after: int = -1, wait: float = 0.0) -> dict:
        end = time.monotonic() + wait
        with self.cond:
            while self.seq <= after and time.monotonic() < end:
                self.cond.wait(max(0.0, end - time.monotonic()))
            now = time.time()
            return {"seq": self.seq,
                    "items": [{"seq": i["seq"], "utt": i["utt"], "final": i["final"], "text": i["text"],
                               "age": round(now - i["end"], 2), "duration": i["duration"]}
                              for i in self.items if i["seq"] > after]}

    def speaking(self, on: bool) -> bool:
        """The robot starts (True) or stops (False) talking. Returns whether the microphone is now ignored."""
        with self.cond:
            self._expire()
            if on and self._muted_at is None:
                self._stack.enter_context(self.listener.robot_speaking())
                self._muted_at = time.monotonic()
            elif not on and self._muted_at is not None:
                self._stack.close()
                self._stack, self._muted_at = contextlib.ExitStack(), None
            return self._muted_at is not None

    def _expire(self) -> None:
        if self._muted_at is not None and time.monotonic() - self._muted_at > MAX_MUTE_S:
            self._stack.close()
            self._stack, self._muted_at = contextlib.ExitStack(), None

    def health(self) -> dict:
        with self.cond:
            self._expire()
            return {"ok": True, "service": SERVICE, "seq": self.seq, "muted": self._muted_at is not None}


def make_app(hub: VoiceHub):
    from fastapi import Body, FastAPI

    app = FastAPI(title="Baymax voice service")
    app.state.hub = hub

    @app.get("/health")
    def health():
        return hub.health()

    @app.get("/heard")
    def heard(after: int = -1, wait: float = 0.0):
        return hub.heard(after, max(0.0, min(5.0, wait)))

    @app.post("/speaking")
    def speaking(body: dict = Body(...)):
        return {"muted": hub.speaking(bool(body.get("on")))}

    return app


def main() -> None:
    ap = argparse.ArgumentParser(description="Baymax voice service (run on the GX10).")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--model", default=None, help="Whisper model on Hugging Face (default: voice.listener's)")
    ap.add_argument("--mute-tail", type=float, default=0.5, metavar="SECONDS",
                    help="keep ignoring the microphone this long after the robot stops talking (default 0.5)")
    ap.add_argument("--end-silence", type=float, default=0.6, metavar="SECONDS",
                    help="a pause this long ends an utterance (default 0.6); answers don't wait for it, they are "
                         "taken from the partial words as soon as they are complete")
    args = ap.parse_args()
    import uvicorn

    from voice.listener import MODEL, Listener
    print("loading Silero VAD and Whisper...", flush=True)
    listener = Listener(model=args.model or MODEL, mute_after_s=args.mute_tail,
                        end_silence_ms=args.end_silence * 1000).start()
    print(f"Baymax voice service on http://{args.host}:{args.port}", flush=True)
    try:
        uvicorn.run(make_app(VoiceHub(listener)), host=args.host, port=args.port, log_level="warning")
    finally:
        listener.stop()


if __name__ == "__main__":
    main()
