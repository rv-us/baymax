"""
Microphone receiver for the TonyPi audio stream (runs on the GX10).

The Pi sends its microphone as Opus over RTP/UDP (start_pi_sender.sh). This
decodes it with gst-launch-1.0 in a subprocess and keeps the last few seconds as
16 kHz mono 16-bit PCM. Each chunk is stamped with its arrival time on the GX10,
so it can be compared with other GX10 events (for example, when the robot spoke).

Usage in your own code:

    from audio_receiver import AudioReceiver

    rx = AudioReceiver().start()
    pcm, t0 = rx.since(t)        # audio that arrived after time t, and when its first sample was heard
    pcm = rx.latest(2.0)         # the last 2 seconds
    samples = numpy.frombuffer(pcm, numpy.int16)

Standalone test (prints loudness once a second; --wav saves a recording):

    python3 audio_receiver.py
    python3 audio_receiver.py --wav test.wav --seconds 5
"""

import argparse
import array
import collections
import math
import shutil
import subprocess
import threading
import time
import wave

RATE = 16000
CHUNK_MS = 20


def build_command(port: int = 5002, latency_ms: int = 60, rate: int = RATE) -> list:
    # do-lost makes the jitter buffer report lost packets, so opusdec can
    # rebuild them from the next packet's FEC data or conceal the gap (plc).
    return [
        "gst-launch-1.0", "-q",
        "udpsrc", f"port={port}", "buffer-size=1048576",
        "caps=application/x-rtp,media=audio,encoding-name=OPUS,clock-rate=48000,payload=97",
        "!", "rtpjitterbuffer", f"latency={latency_ms}", "do-lost=true",
        "!", "rtpopusdepay",
        "!", "opusdec", "plc=true", "use-inband-fec=true",
        "!", "audioconvert", "!", "audioresample",
        "!", f"audio/x-raw,format=S16LE,rate={rate},channels=1,layout=interleaved",
        "!", "fdsink", "fd=1", "sync=false",
    ]


class AudioReceiver:
    """Receives the stream on a background process and keeps the newest keep_s seconds."""

    def __init__(self, port: int = 5002, latency_ms: int = 60, rate: int = RATE,
                 keep_s: float = 30.0):
        self.command = build_command(port, latency_ms, rate)
        self.rate = rate
        self.chunk_bytes = rate * CHUNK_MS // 1000 * 2
        self.keep_bytes = int(keep_s * rate) * 2
        self._chunks = collections.deque()  # (arrival time of the chunk's end, pcm bytes)
        self._buffered = 0  # bytes; counted in bytes, not seconds, so rounding can't drift
        self._lock = threading.Lock()
        self._proc = None
        self._threads = []
        self._stderr = collections.deque(maxlen=20)
        self.chunk_count = 0

    def start(self, open_timeout: float = 1.0):
        """Start receiving in the background. Returns without waiting for the Pi to start sending."""
        if not shutil.which("gst-launch-1.0"):
            raise RuntimeError("gst-launch-1.0 not found. Run ./install.sh")
        self._proc = subprocess.Popen(self.command, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
        self._threads = [threading.Thread(target=self._read_audio, daemon=True),
                         threading.Thread(target=self._read_errors, daemon=True)]
        for t in self._threads:
            t.start()
        # A broken pipeline (missing plugin, port in use) exits right away.
        try:
            self._proc.wait(open_timeout)
        except subprocess.TimeoutExpired:
            return self
        self._threads[1].join(timeout=1)
        raise RuntimeError("Audio pipeline exited: " + (" ".join(self._stderr) or "no error output")
                           + "\nIs the port already in use by another receiver?")

    def _read_audio(self):
        while True:
            data = self._proc.stdout.read(self.chunk_bytes)
            if not data:  # the process exited
                return
            self._append(time.time(), data)

    def _read_errors(self):
        for line in self._proc.stderr:
            self._stderr.append(line.decode(errors="replace").strip())

    def _append(self, arrival: float, data: bytes):
        with self._lock:
            self._chunks.append((arrival, data))
            self._buffered += len(data)
            while self._buffered > self.keep_bytes and len(self._chunks) > 1:
                _, old = self._chunks.popleft()
                self._buffered -= len(old)
            self.chunk_count += 1

    def _duration(self, data: bytes) -> float:
        return len(data) / 2 / self.rate

    def since(self, t: float):
        """Return (pcm, start_time): the audio that arrived after time t, and when its
        first sample was heard (None if nothing arrived). Accurate to a chunk (20 ms)
        plus network jitter."""
        with self._lock:
            chunks = [(end, d) for end, d in self._chunks if end > t]
        if not chunks:
            return b"", None
        end, first = chunks[0]
        return b"".join(d for _, d in chunks), end - self._duration(first)

    def latest(self, seconds: float) -> bytes:
        """Return the newest `seconds` of audio (less if not that much has arrived)."""
        want = int(seconds * self.rate) * 2
        with self._lock:
            out, n = [], 0
            for _, d in reversed(self._chunks):
                if n >= want:
                    break
                out.append(d)
                n += len(d)
        pcm = b"".join(reversed(out))
        return pcm[-want:] if want else b""

    def seconds_since_last_audio(self) -> float:
        with self._lock:
            return time.time() - self._chunks[-1][0] if self._chunks else float("inf")

    def stop(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        for t in self._threads:
            t.join(timeout=1)


def dbfs(pcm: bytes) -> float:
    """Loudness of 16-bit PCM in dBFS (0 = clipping, about -60 = quiet room)."""
    samples = array.array("h", pcm)
    if not samples:
        return -120.0
    rms = math.sqrt(sum(x * x for x in samples) / len(samples))
    return round(20 * math.log10(rms / 32768), 1) if rms else -120.0


def main():
    parser = argparse.ArgumentParser(description="Receive the TonyPi microphone stream.")
    parser.add_argument("--port", type=int, default=5002)
    parser.add_argument("--latency", type=int, default=60, help="jitter buffer in ms")
    parser.add_argument("--wav", help="save the audio to this .wav file (up to the last 35 s without --seconds)")
    parser.add_argument("--seconds", type=float, default=0, help="stop after this long (0 = until Ctrl+C)")
    args = parser.parse_args()

    rx = AudioReceiver(args.port, args.latency, keep_s=max(args.seconds, 30) + 5).start()
    print(f"Listening on UDP {args.port}... (Ctrl+C to stop)")
    started = time.time()
    last_count, last_time = rx.chunk_count, started
    try:
        while not args.seconds or time.time() - started < args.seconds:
            time.sleep(0.05)
            now = time.time()
            if now - last_time >= 1.0:
                chunks = rx.chunk_count - last_count
                stale = rx.seconds_since_last_audio()
                status = "no audio yet" if stale == float("inf") else f"last chunk {stale*1000:.0f} ms ago"
                print(f"{chunks * CHUNK_MS / 1000 / (now - last_time):4.2f}x real time  "
                      f"{dbfs(rx.latest(1.0)):6.1f} dBFS  ({status})")
                last_count, last_time = rx.chunk_count, now
    except KeyboardInterrupt:
        pass
    finally:
        pcm, _ = rx.since(started)
        rx.stop()
    if args.wav:
        with wave.open(args.wav, "wb") as f:
            f.setnchannels(1)
            f.setsampwidth(2)
            f.setframerate(RATE)
            f.writeframes(pcm)
        print(f"Saved {len(pcm) / 2 / RATE:.1f} s to {args.wav}")


if __name__ == "__main__":
    main()
