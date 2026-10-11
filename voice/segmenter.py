"""
Splits the incoming microphone audio into utterances (runs on the GX10).

Feed it timestamped 16 kHz mono 16-bit PCM chunks (from AudioReceiver.chunks_since).
A voice activity detector scores each 32 ms window; an utterance starts on speech
and ends after END_SILENCE_MS of silence. This file has no model code: the VAD is
passed in as a function, so the logic can be tested with fake scores.
"""

import collections
from dataclasses import dataclass

RATE = 16000
WINDOW = 512                 # samples per VAD window (32 ms), what Silero expects at 16 kHz
WINDOW_S = WINDOW / RATE

THRESHOLD = 0.5              # a window this likely to be speech starts an utterance...
NEG_THRESHOLD = 0.35         # ...and one below this counts as silence (hysteresis in between)
END_SILENCE_MS = 800         # this much silence ends an utterance
PRE_ROLL_MS = 200            # audio kept from before speech started, so the first sound isn't cut
TAIL_MS = 200                # silence kept after the last speech
MIN_SPEECH_MS = 250          # shorter bursts (a cough, a knock) are dropped
MAX_UTTERANCE_S = 15.0       # longer speech is cut here and returned in pieces


@dataclass
class Utterance:
    pcm: bytes               # 16 kHz mono 16-bit PCM, including pre-roll and tail
    start: float             # GX10 time the speech started arriving
    end: float               # GX10 time the speech stopped (before the trailing silence)


def _windows(ms: float) -> int:
    return max(1, round(ms / 1000 / WINDOW_S))


class Segmenter:
    """Turns a stream of (arrival_time, pcm) chunks into Utterances."""

    def __init__(self, vad, end_silence_ms: float = END_SILENCE_MS):
        self.vad = vad  # function(window_pcm: bytes) -> speech probability 0..1
        self.end_silence = _windows(end_silence_ms)
        self.pre_roll = collections.deque(maxlen=_windows(PRE_ROLL_MS))
        self.tail = _windows(TAIL_MS)
        self.min_speech = _windows(MIN_SPEECH_MS)
        self.max_windows = _windows(MAX_UTTERANCE_S * 1000)
        self._pending = b""   # samples left over that don't fill a window yet
        self.reset()

    def reset(self):
        """Forget any utterance in progress (used while the robot is talking)."""
        self._pending = b""
        self.pre_roll.clear()
        self._utt = None      # list of window pcm while speaking, else None
        self._voiced = 0      # windows above THRESHOLD in this utterance
        self._silence = 0     # windows of silence in a row at the end
        self._start = self._last_speech = 0.0
        reset_vad = getattr(self.vad, "reset", None)
        if reset_vad:
            reset_vad()

    @property
    def in_utterance(self) -> bool:
        return self._utt is not None

    def feed(self, arrival: float, pcm: bytes) -> list:
        """Add one chunk that finished arriving at `arrival`. Returns finished Utterances."""
        data = self._pending + pcm
        n = len(data) // (WINDOW * 2)
        self._pending = data[n * WINDOW * 2:]
        # Time each window ends: the chunk's arrival time minus the audio still after it.
        left_after = len(self._pending) // 2
        out = []
        for i in range(n):
            window = data[i * WINDOW * 2:(i + 1) * WINDOW * 2]
            t = arrival - ((n - 1 - i) * WINDOW + left_after) / RATE
            utt = self._step(window, t)
            if utt:
                out.append(utt)
        return out

    def _step(self, window: bytes, t: float):
        p = self.vad(window)
        if self._utt is None:
            if p < THRESHOLD:
                self.pre_roll.append(window)
                return None
            self._utt = list(self.pre_roll) + [window]
            self.pre_roll.clear()
            self._voiced, self._silence = 1, 0
            self._start, self._last_speech = t - WINDOW_S, t
            return None

        self._utt.append(window)
        if p >= THRESHOLD:
            self._voiced += 1
        if p >= NEG_THRESHOLD:
            self._silence = 0
            self._last_speech = t
        else:
            self._silence += 1
        if self._silence >= self.end_silence or len(self._utt) >= self.max_windows:
            return self._finish()
        return None

    def _finish(self):
        windows = self._utt[:len(self._utt) - max(0, self._silence - self.tail)]
        utt = Utterance(b"".join(windows), self._start, self._last_speech)
        voiced = self._voiced
        self._utt = None
        self._voiced = self._silence = 0
        return utt if voiced >= self.min_speech else None
