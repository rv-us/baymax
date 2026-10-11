import threading
import time

from voice.listener import Listener, WhisperTranscriber, _normalize, HALLUCINATIONS
from voice.segmenter import RATE, Segmenter

CHUNK_S = 0.02
LOUD = b"\x10\x27" * int(RATE * CHUNK_S)   # 20 ms at amplitude 10000
QUIET = b"\x00\x00" * int(RATE * CHUNK_S)


def fake_vad(window: bytes) -> float:
    return 1.0 if window[:2] == b"\x10\x27" else 0.0


def chunks(pattern, start):
    """pattern: [(seconds, LOUD or QUIET), ...] -> [(arrival_time, pcm), ...] from `start`."""
    out, t = [], start
    for seconds, pcm in pattern:
        for _ in range(round(seconds / CHUNK_S)):
            t += CHUNK_S
            out.append((t, pcm))
    return out


def run(seg, pattern, start=100.0):
    return [u for t, pcm in chunks(pattern, start) for u in seg.feed(t, pcm)]


# Segmenter

def test_speech_then_silence_is_one_utterance():
    utts = run(Segmenter(fake_vad), [(0.5, QUIET), (1.0, LOUD), (1.0, QUIET)])
    assert len(utts) == 1
    u = utts[0]
    assert abs(u.start - 100.5) < 0.05 and abs(u.end - 101.5) < 0.05
    seconds = len(u.pcm) / 2 / RATE
    assert 1.3 < seconds < 1.5  # 0.2 s pre-roll + 1 s speech + 0.2 s tail


def test_no_utterance_until_the_silence_is_long_enough():
    assert run(Segmenter(fake_vad), [(1.0, LOUD), (0.5, QUIET)]) == []


def test_short_noise_is_dropped():
    assert run(Segmenter(fake_vad), [(0.1, LOUD), (1.0, QUIET)]) == []


def test_reset_drops_the_utterance_in_progress():
    seg = Segmenter(fake_vad)
    run(seg, [(1.0, LOUD)])
    assert seg.in_utterance
    seg.reset()
    assert run(seg, [(1.0, QUIET)], start=200.0) == []


def test_long_speech_is_cut_into_pieces():
    utts = run(Segmenter(fake_vad), [(20.0, LOUD), (1.0, QUIET)])
    assert len(utts) == 2
    assert abs(len(utts[0].pcm) / 2 / RATE - 15.0) < 0.1


# Listener (fake receiver, VAD and transcriber; no models)

class FakeReceiver:
    def __init__(self):
        self._chunks, self._lock = [], threading.Lock()

    def push(self, pattern):
        with self._lock:
            self._chunks += chunks(pattern, time.time())

    def chunks_since(self, t):
        with self._lock:
            return [c for c in self._chunks if c[0] > t]

    def stop(self):
        pass


def make_listener():
    rx = FakeReceiver()
    listener = Listener(receiver=rx, vad=fake_vad, transcribe=lambda pcm: "yes").start()
    return listener, rx


def test_listen_returns_the_answer():
    listener, rx = make_listener()
    rx.push([(0.5, LOUD), (1.0, QUIET)])
    assert listener.listen(timeout_s=2) == "yes"
    listener.stop()


def test_listen_times_out_with_none():
    listener, _ = make_listener()
    started = time.time()
    assert listener.listen(timeout_s=0.3) is None
    assert time.time() - started < 1
    listener.stop()


def test_answers_from_before_listen_are_not_returned():
    listener, rx = make_listener()
    heard = []
    listener.on_transcript(heard.append)
    rx.push([(0.5, LOUD), (1.0, QUIET)])
    while not heard:
        time.sleep(0.01)
    # The fake chunks are stamped ahead of the clock; wait until the speech has really ended.
    time.sleep(max(0, heard[0].end - time.time()) + 0.05)
    assert listener.listen(timeout_s=0.3) is None
    listener.stop()


def test_robot_speech_is_ignored():
    listener, rx = make_listener()
    heard = []
    listener.on_transcript(heard.append)
    with listener.robot_speaking():
        rx.push([(0.5, LOUD)])
        time.sleep(0.2)
    rx.push([(1.0, QUIET)])  # arrives inside the 0.3 s after the robot stopped
    time.sleep(0.3)
    assert heard == []
    listener.stop()


def test_hallucination_list_matches_after_normalizing():
    assert _normalize(" Thank you. ") in HALLUCINATIONS
    assert _normalize("Yes, I'm ready.") not in HALLUCINATIONS
    assert WhisperTranscriber  # imported without faster-whisper installed
