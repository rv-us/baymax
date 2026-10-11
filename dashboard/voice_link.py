"""The dashboard's link to the GX10's microphone (voice/service.py): captions in, "the robot is talking" out.

    link = VoiceLink(settings, hear, hub)       # hear(text, age_s, final, utt) -> what became of it (see app.py)
    link.start()                                # keeps trying until the voice service answers
    gateway.on_speaking = link.set_speaking     # told whenever the robot starts or stops talking

Everything the patient says is asked for with one waiting request (`/heard?after=SEQ&wait=S`), so words arrive
as they are spoken: the GX10 reads the utterance every quarter second while the patient is still talking and sends
those partial words (`final` false), then the whole utterance when they pause. `hear` can act on the first partial
that is already an answer, so a session moves on the moment the patient has said it, not after the silence. What was said before the link came up, or while it was down, is not replayed:
an old "ow" must not stop a session minutes after it was said. The other direction is a small sender that
makes the GX10 ignore the microphone exactly while the robot talks (it is told before the robot makes a sound).

The service's address is `settings.voice_url`, else the pose service's host on port 9200, so the dashboard
finds it wherever it runs. `state` is `off` until there is an address, then `connecting`, `listening` or `down`.
"""
from __future__ import annotations

import http.client
import json
import logging
import threading
import time
from urllib.parse import urlparse

log = logging.getLogger(__name__)

SERVICE = "baymax-voice"
DEFAULT_PORT = 9200
WAIT_S = 1.0
TIMEOUT_S = 5.0
STALE_S = 8.0            # a transcript older than this when it gets here is not acted on
RETRY_S = (1, 2, 5)


def voice_url(settings) -> str | None:
    """Where the voice service is: `settings.voice_url`, else beside the pose service."""
    if getattr(settings, "voice_url", None):
        return None if settings.voice_url == "off" else settings.voice_url.rstrip("/")
    host = urlparse(settings.pose_url).hostname if getattr(settings, "pose_url", None) else None
    return f"http://{host}:{DEFAULT_PORT}" if host else None


class VoiceLink:
    def __init__(self, settings, hear, hub=None):
        self.settings, self.hear, self.hub = settings, hear, hub
        self._stop = threading.Event()
        self._cond = threading.Condition()
        self._wanted = False          # the robot is talking
        self._told = None             # what the GX10 was last told (None: nothing yet)
        self.state = {"state": "off", "url": None, "muted": False, "last": None, "error": None}

    def snapshot(self) -> dict:
        return self.state

    def _set(self, **changes) -> None:
        new = {**self.state, **changes}
        if new != self.state:
            self.state = new
            if self.hub:
                self.hub.publish("voice", self.state)

    def start(self) -> None:
        threading.Thread(target=self._listen, name="dashboard-voice", daemon=True).start()
        threading.Thread(target=self._send, name="dashboard-voice-mute", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        with self._cond:
            self._cond.notify_all()

    # ---- the robot talking: dashboard -> GX10 -------------------------------------

    def set_speaking(self, on: bool) -> None:
        with self._cond:
            self._wanted = on
            self._cond.notify_all()

    def _send(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            with self._cond:
                while self._wanted == self._told and not self._stop.is_set():
                    self._cond.wait(1.0)
                want = self._wanted
            url = voice_url(self.settings)
            if self._stop.is_set():
                return
            if url is None or self.state["state"] != "listening":
                self._stop.wait(0.3)
                continue
            try:
                self._call(url, "POST", "/speaking", {"on": want})
                self._told, attempt = want, 0
                self._set(muted=want)
            except (OSError, ValueError, http.client.HTTPException):
                attempt += 1
                self._stop.wait(0.2 * min(attempt, 5))

    # ---- what was said: GX10 -> dashboard -------------------------------------------

    @staticmethod
    def _call(url: str, method: str, path: str, body=None, timeout: float = TIMEOUT_S):
        u = urlparse(url)
        conn = http.client.HTTPConnection(u.hostname, u.port or 80, timeout=timeout)
        try:
            conn.request(method, path, None if body is None else json.dumps(body),
                         {"Content-Type": "application/json"})
            resp = conn.getresponse()
            data = resp.read()
            if resp.status != 200:
                raise OSError(f"the voice service answered {resp.status}")
            return json.loads(data)
        finally:
            conn.close()

    def _listen(self) -> None:
        attempt, seq = 0, None
        while not self._stop.is_set():
            url = voice_url(self.settings)
            if url is None:
                self._set(state="off", url=None)
                self._stop.wait(2.0)
                continue
            try:
                if seq is None:
                    self._set(state="connecting", url=url)
                    health = self._call(url, "GET", "/health", timeout=2.0)
                    if health.get("service") != SERVICE:
                        raise OSError(f"{url} is not the Baymax voice service")
                    seq = health["seq"]            # from now on only: nothing said earlier is replayed
                    self._told = None              # a restarted service forgets whether the robot was talking
                    self._set(state="listening", url=url, error=None, muted=bool(health.get("muted")))
                    with self._cond:
                        self._cond.notify_all()
                out = self._call(url, "GET", f"/heard?after={seq}&wait={WAIT_S}", timeout=WAIT_S + TIMEOUT_S)
                attempt = 0
                for item in out["items"]:
                    self._heard(item)
                seq = out["seq"]
            except (OSError, ValueError, KeyError, http.client.HTTPException) as e:
                seq = None
                attempt += 1
                self._set(state="down", url=url, error=f"The GX10's microphone isn't answering: {e}")
                self._stop.wait(RETRY_S[min(attempt, len(RETRY_S)) - 1])

    def _heard(self, item: dict) -> None:
        text, age = str(item.get("text") or "").strip(), float(item.get("age") or 0)
        if not text:
            return
        if age > STALE_S:
            outcome = "stale"
        else:
            try:
                outcome = self.hear(text, age, bool(item.get("final", True)), item.get("utt"))
            except Exception:   # a bad line must not end listening
                log.exception("handling %r failed", text)
                outcome = "error"
        if outcome == "already":   # the rest of what was said after the answer was taken: caption it, keep the outcome
            outcome = (self.state["last"] or {}).get("outcome", "answer")
        self._set(last={"text": text, "outcome": outcome, "t": time.time()})
