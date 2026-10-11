"""The microphone bridge: what the patient says, from the robot's microphone to the dashboard.

    python -m voice.bridge                          # the dashboard on this machine, port 8080
    python -m voice.bridge --dashboard http://gx10:8080

Runs on the GX10 next to the dashboard. `voice.Listener` (Silero VAD, then Whisper on the GPU) turns the
robot's microphone stream into text; each transcript is posted to the dashboard's `POST /api/voice/heard`,
which stops the robot on a pain word and otherwise hands it to the session as the answer to the question
it is waiting on. The dashboard also says when the robot is talking (`GET /api/voice`), and the listener
ignores the microphone for exactly that long so the robot never answers itself.

If the dashboard isn't up yet, or goes away, the bridge keeps trying; transcripts made while it is away
are dropped rather than delivered late (an old "ow" must not stop a session minutes after it was said).
"""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import threading
import time
import urllib.request

log = logging.getLogger("voice.bridge")

POLL_S = 0.05            # how often the dashboard is asked whether the robot is talking
STALE_S = 5.0            # a transcript older than this when the dashboard can be reached again is dropped


class Bridge:
    """`listener` is a voice.Listener (anything with on_transcript() and robot_speaking()); `get` and `post`
    talk to the dashboard (JSON in, JSON out) and raise OSError when it can't be reached."""

    def __init__(self, listener, get, post):
        self.listener, self.get, self.post = listener, get, post
        self._stop = threading.Event()
        self._muted = contextlib.ExitStack()
        self._is_muted = False
        self.delivered = 0
        listener.on_transcript(self._heard)

    def _heard(self, transcript) -> None:
        """From the listener's thread: one thing the patient said."""
        if time.time() - transcript.end > STALE_S:
            return
        try:
            out = self.post("/api/voice/heard", {"text": transcript.text})
        except (OSError, ValueError) as e:
            log.warning("couldn't tell the dashboard what was heard (%r): %s", transcript.text, e)
            return
        self.delivered += 1
        log.info("heard %r -> %s", transcript.text,
                 "PAIN, robot stopped" if out.get("pain") else "answer" if out.get("answered") else "no question waiting")

    def _mute(self, on: bool) -> None:
        if on and not self._is_muted:
            self._muted.enter_context(self.listener.robot_speaking())
            self._is_muted = True
        elif not on and self._is_muted:
            self._muted.close()
            self._muted = contextlib.ExitStack()
            self._is_muted = False

    def run(self) -> None:
        """Follow the dashboard's "the robot is talking" until stop()."""
        try:
            while not self._stop.is_set():
                try:
                    self._mute(bool(self.get("/api/voice").get("speaking")))
                except (OSError, ValueError):
                    pass          # keep the last state: not hearing the robot is safer than hearing it
                self._stop.wait(POLL_S)
        finally:
            self._mute(False)

    def stop(self) -> None:
        self._stop.set()


def http_json(base: str):
    """(get, post) for the dashboard at `base`."""
    def call(method: str, path: str, body=None):
        req = urllib.request.Request(base.rstrip("/") + path, method=method,
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.load(r)
    return (lambda path: call("GET", path)), (lambda path, body: call("POST", path, body))


def main() -> None:
    ap = argparse.ArgumentParser(description="Send what the robot's microphone hears to the dashboard.")
    ap.add_argument("--dashboard", default="http://127.0.0.1:8080", help="the dashboard's address")
    ap.add_argument("--model", default=None, help="Whisper model on Hugging Face (default: voice.listener's)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    from voice.listener import MODEL, Listener
    log.info("loading Silero VAD and Whisper...")
    listener = Listener(model=args.model or MODEL).start()
    get, post = http_json(args.dashboard)
    bridge = Bridge(listener, get, post)
    log.info("listening to the robot's microphone; telling the dashboard at %s", args.dashboard)
    try:
        bridge.run()
    except KeyboardInterrupt:
        pass
    finally:
        bridge.stop()
        listener.stop()


if __name__ == "__main__":
    main()
