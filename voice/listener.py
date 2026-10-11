"""
Speech-to-text for the workout loop (runs on the GX10).

Listens to the robot's microphone stream (streaming/audio_receiver.py), finds
utterances with Silero VAD, and transcribes them with Whisper (small.en on the
GPU through transformers, about 50 ms per answer). Everything runs on the GX10.

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

MODEL = "openai/whisper-small.en"
# Words Whisper should expect. Without it, a lone "Stop." came out as "DOP".
PROMPT = "Stop. Yes. No. Not yet. I'm ready. That hurts. Ow."
NO_SPEECH = 0.5      # drop audio Whisper thinks is more likely noise than speech
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
    """Whisper through Hugging Face transformers, in fp16 on the GPU (CPU if there is
    none). Returns the text, or "" for silence and noise."""

    def __init__(self, model: str = MODEL, device: str | None = None, prompt: str = PROMPT):
        import torch
        from transformers import WhisperForConditionalGeneration, WhisperProcessor

        self._torch = torch
        self.device = device or ("cuda" if torch.cuda.is_available() else "cpu")
        self.dtype = torch.float16 if self.device == "cuda" else torch.float32
        self.processor = WhisperProcessor.from_pretrained(model)
        self.model = WhisperForConditionalGeneration.from_pretrained(model, dtype=self.dtype)
        self.model.to(self.device).eval()
        self.model.generation_config.max_length = None  # max_new_tokens below is the limit
        tok = self.processor.tokenizer
        self._sot = tok.convert_tokens_to_ids("<|startoftranscript|>")
        # English-only models call the no-speech token <|nocaptions|>.
        self._nospeech = next(i for i in (tok.convert_tokens_to_ids(t) for t in
                                          ("<|nospeech|>", "<|nocaptions|>")) if i != tok.unk_token_id)
        self._prompt = (torch.tensor(self.processor.get_prompt_ids(prompt), device=self.device)
                        if prompt else None)
        self(b"\x00\x00" * RATE)  # warm up, so the first real answer isn't slow

    def no_speech_prob(self, features) -> float:
        """Whisper's own estimate that the audio has no speech (answers score under 0.1, noise over 0.8)."""
        sot = self._torch.tensor([[self._sot]], device=self.device)
        logits = self.model(features, decoder_input_ids=sot).logits[0, -1].float()
        return logits.softmax(-1)[self._nospeech].item()

    def __call__(self, pcm: bytes) -> str:
        audio = np.frombuffer(pcm, np.int16).astype(np.float32) / 32768
        features = self.processor(audio, sampling_rate=RATE, return_tensors="pt").input_features
        features = features.to(self.device, self.dtype)
        with self._torch.inference_mode():
            # Checked before decoding: with the prompt, Whisper turns noise into words like "No."
            if self.no_speech_prob(features) > NO_SPEECH:
                return ""
            kwargs = {"prompt_ids": self._prompt} if self._prompt is not None else {}
            ids = self.model.generate(features, max_new_tokens=64, **kwargs)
        text = self.processor.batch_decode(ids, skip_special_tokens=True)[0].strip()
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
    parser.add_argument("--model", default=MODEL, help="Whisper model on Hugging Face")
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
