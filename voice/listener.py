"""
Speech-to-text for the workout loop (runs on the GX10).

Listens to the robot's microphone stream (streaming/audio_receiver.py), finds
utterances with Silero VAD, and transcribes them with Whisper (faster-whisper,
small.en on the CPU).

Usage in your own code:

    from voice import Listener

    listener = Listener().start()                 # loads the models (a few seconds)
    with listener.robot_speaking():               # ignore the mic while the robot talks
        backend.call("sensors/voice/say", text="Are you ready?")
    text = listener.listen(timeout_s=10)          # the patient's answer, or None

    listener.on_transcript(lambda t: print(t.text))   # every transcript, e.g. for pain words

Standalone test (prints each transcript and how long it took):

    python3 -m voice.listener
"""

import argparse
import collections
import contextlib
import threading
import time
from dataclasses import dataclass

import numpy as np

from streaming.audio_receiver import AudioReceiver
from voice.segmenter import RATE, Segmenter, Utterance

MODEL = "small.en"
MUTE_AFTER_S = 0.3   # keep ignoring the mic this long after the robot stops talking
POLL_S = 0.05

# Whisper sometimes "hears" these in noise or silence; drop them when they are the whole answer.
HALLUCINATIONS = {"", "you", "thank you", "thanks for watching", "thank you for watching",
                  "bye", "the end"}


@dataclass
class Transcript:
    text: str
    start: float            # GX10 time the speech started
    end: float              # GX10 time the speech stopped
    transcribed_at: float   # GX10 time the text was ready

    @property
    def latency(self) -> float:
        """Seconds from the end of speech to the text, including the end-of-speech silence wait."""
        return self.transcribed_at - self.end


class SileroVad:
    """Silero VAD on the CPU: speech probability for one 512-sample window."""

    def __init__(self):
        import torch
        from silero_vad import load_silero_vad

        torch.set_num_threads(1)
        self._torch = torch
        self.model = load_silero_vad()

    def __call__(self, window: bytes) -> float:
        x = np.frombuffer(window, np.int16).astype(np.float32) / 32768
        return self.model(self._torch.from_numpy(x), RATE).item()

    def reset(self):
        self.model.reset_states()


class WhisperTranscriber:
    """faster-whisper on the CPU. Returns the text, or "" for silence and noise."""

    def __init__(self, model: str = MODEL, device: str = "cpu", compute_type: str = "int8",
                 threads: int = 8):
        from faster_whisper import WhisperModel

        self.model = WhisperModel(model, device=device, compute_type=compute_type,
                                  cpu_threads=threads)
        self(b"\x00\x00" * RATE)  # warm up, so the first real answer isn't slow

    def __call__(self, pcm: bytes) -> str:
        audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768
        segments, _ = self.model.transcribe(audio, language="en", beam_size=1,
                                            condition_on_previous_text=False)
        # Whisper's own no-speech rule: probably silence and not confidently words.
        text = " ".join(s.text.strip() for s in segments
                        if not (s.no_speech_prob > 0.6 and s.avg_logprob < -1.0)).strip()
        return "" if _normalize(text) in HALLUCINATIONS else text


def _normalize(text: str) -> str:
    return "".join(c for c in text.lower() if c.isalnum() or c == " ").strip()


class Listener:
    """Turns the robot's microphone into transcripts. One background thread does
    VAD and transcription, so every utterance is heard even between listen() calls."""

    def __init__(self, receiver=None, vad=None, transcribe=None, model: str = MODEL):
        self.receiver = receiver
        self._vad = vad
        self._transcribe = transcribe
        self._model = model
        self._mutes = []           # [start, end] GX10 times when the robot was talking
        self._transcripts = collections.deque(maxlen=50)
        self._callbacks = []
        self._cond = threading.Condition()
        self._running = False
        self._thread = None
        self._transcribing = False
        self.segmenter = None

    def start(self):
        """Load the models, start the audio receiver (if none was given) and listen."""
        if self._vad is None:
            self._vad = SileroVad()
        if self._transcribe is None:
            self._transcribe = WhisperTranscriber(self._model)
        if self.receiver is None:
            self.receiver = AudioReceiver().start()
        self.segmenter = Segmenter(self._vad)
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    @contextlib.contextmanager
    def robot_speaking(self):
        """Wrap the robot's say/play_sound calls: audio that arrives while they run,
        and for MUTE_AFTER_S after, is ignored so the robot doesn't hear itself."""
        window = [time.time(), float("inf")]
        with self._cond:
            self._mutes.append(window)
        try:
            yield
        finally:
            with self._cond:
                window[1] = time.time() + MUTE_AFTER_S

    def on_transcript(self, callback):
        """Call callback(Transcript) for every transcript, from the listener's thread."""
        self._callbacks.append(callback)

    def listen(self, timeout_s: float = 10) -> str | None:
        """Wait for the next utterance whose speech ends after this call. Returns its
        text, or None if nobody spoke before the timeout."""
        called = time.time()
        deadline = called + timeout_s
        with self._cond:
            while True:
                for t in self._transcripts:
                    if t.end > called:
                        return t.text
                # Speech in progress at the deadline still counts: wait for its text.
                left = deadline - time.time()
                if left <= 0 and not self._busy():
                    return None
                self._cond.wait(max(left, POLL_S))

    def _busy(self) -> bool:
        return self._transcribing or (self.segmenter is not None and self.segmenter.in_utterance)

    def _muted(self, t: float) -> bool:
        with self._cond:
            # Forget windows that ended long ago; keep open ones (end = inf).
            self._mutes = [w for w in self._mutes if w[1] > t - 60]
            return any(start <= t <= end for start, end in self._mutes)

    def _loop(self):
        last = time.time()  # only audio from now on
        was_muted = False
        while self._running:
            chunks = self.receiver.chunks_since(last)
            if not chunks:
                time.sleep(POLL_S)
                continue
            for arrival, pcm in chunks:
                last = arrival
                if self._muted(arrival):
                    if not was_muted:
                        self.segmenter.reset()
                    was_muted = True
                    continue
                if was_muted:
                    self.segmenter.reset()  # start clean after the robot stops talking
                    was_muted = False
                for utt in self.segmenter.feed(arrival, pcm):
                    self._handle(utt)

    def _handle(self, utt: Utterance):
        self._transcribing = True
        try:
            text = self._transcribe(utt.pcm)
        finally:
            self._transcribing = False
        t = Transcript(text, utt.start, utt.end, time.time())
        with self._cond:
            if text:
                self._transcripts.append(t)
            self._cond.notify_all()
        if not text:
            return
        for callback in self._callbacks:
            callback(t)

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        if self.receiver:
            self.receiver.stop()


def main():
    parser = argparse.ArgumentParser(description="Print what the robot's microphone hears.")
    parser.add_argument("--model", default=MODEL, help="Whisper model (small.en, base.en, ...)")
    args = parser.parse_args()

    print(f"Loading Silero VAD and Whisper {args.model}...")
    listener = Listener(model=args.model).start()
    print("Listening... talk to the robot (Ctrl+C to stop)")
    listener.on_transcript(lambda t: print(
        f"{t.text!r:50}  {t.end - t.start:4.1f} s of speech, text {t.latency:.2f} s after it ended"))
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        listener.stop()


if __name__ == "__main__":
    main()
