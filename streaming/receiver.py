"""
Low-latency frame receiver for the TonyPi video stream (runs on the GX10).

The Pi sends H.264 over RTP/UDP (start_pi_sender.sh). This decodes it with
gst-launch-1.0 in a subprocess and reads raw BGR frames from its stdout, so it
only needs numpy: any OpenCV works alongside it (pip's opencv-python, which has
no GStreamer, is fine), and it runs in a venv with NumPy 2.

Usage in your own code:

    from receiver import FrameReceiver

    rx = FrameReceiver()
    rx.start()
    while True:
        frame, ts = rx.latest()      # newest BGR frame (numpy array) and its arrival time
        if frame is None:
            continue
        ...                          # run your model on `frame`

Standalone test (prints fps; add --show if the GX10 has a display and OpenCV):

    python3 receiver.py
    python3 receiver.py --show
"""

import argparse
import collections
import shutil
import subprocess
import threading
import time

import numpy as np


def build_command(port: int = 5000, latency_ms: int = 50,
                  width: int = 640, height: int = 480) -> list:
    # videoscale pins the output size, so every frame on stdout is exactly
    # width * height * 3 bytes even if the Pi's resolution changes.
    return [
        "gst-launch-1.0", "-q",
        "udpsrc", f"port={port}", "buffer-size=4194304",
        "caps=application/x-rtp,media=video,encoding-name=H264,payload=96,clock-rate=90000",
        "!", "rtpjitterbuffer", f"latency={latency_ms}", "drop-on-latency=true",
        "!", "rtph264depay", "!", "h264parse", "!", "avdec_h264", "max-threads=1",
        "!", "videoconvert", "!", "videoscale",
        "!", f"video/x-raw,format=BGR,width={width},height={height}",
        "!", "fdsink", "fd=1", "sync=false",
    ]


class FrameReceiver:
    """Reads the stream on a background process and always keeps only the newest frame."""

    def __init__(self, port: int = 5000, latency_ms: int = 50,
                 width: int = 640, height: int = 480):
        self.command = build_command(port, latency_ms, width, height)
        self.shape = (height, width, 3)
        self.frame_bytes = width * height * 3
        self._proc = None
        self._threads = []
        self._stderr = collections.deque(maxlen=20)
        self._lock = threading.Lock()
        self._frame = None
        self._ts = 0.0
        self.frame_count = 0

    def start(self, open_timeout: float = 1.0):
        """Start receiving in the background. Returns without waiting for the Pi to start sending."""
        if not shutil.which("gst-launch-1.0"):
            raise RuntimeError("gst-launch-1.0 not found. Run ./install.sh")
        self._proc = subprocess.Popen(self.command, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
        self._threads = [threading.Thread(target=self._read_frames, daemon=True),
                         threading.Thread(target=self._read_errors, daemon=True)]
        for t in self._threads:
            t.start()
        # A broken pipeline (missing plugin, port in use) exits right away.
        try:
            self._proc.wait(open_timeout)
        except subprocess.TimeoutExpired:
            return self
        self._threads[1].join(timeout=1)
        raise RuntimeError("Video pipeline exited: " + (" ".join(self._stderr) or "no error output")
                           + "\nIs the port already in use by another receiver?")

    def _read_frames(self):
        while True:
            buf = bytearray(self.frame_bytes)  # a fresh buffer each time, so handed-out frames never change
            view, got = memoryview(buf), 0
            while got < self.frame_bytes:
                n = self._proc.stdout.readinto(view[got:])
                if not n:  # the process exited
                    return
                got += n
            frame = np.frombuffer(buf, np.uint8).reshape(self.shape)
            with self._lock:
                self._frame = frame
                self._ts = time.time()
                self.frame_count += 1

    def _read_errors(self):
        for line in self._proc.stderr:
            self._stderr.append(line.decode(errors="replace").strip())

    def latest(self):
        """Return (frame, arrival_time). frame is None until the first frame arrives."""
        with self._lock:
            return self._frame, self._ts

    def seconds_since_last_frame(self) -> float:
        with self._lock:
            return float("inf") if self._ts == 0 else time.time() - self._ts

    def stop(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        for t in self._threads:
            t.join(timeout=1)


def main():
    parser = argparse.ArgumentParser(description="Receive the TonyPi video stream.")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--latency", type=int, default=50, help="jitter buffer in ms")
    parser.add_argument("--show", action="store_true", help="display frames in a window (needs OpenCV)")
    args = parser.parse_args()

    if args.show:
        import cv2

    rx = FrameReceiver(args.port, args.latency).start()
    print(f"Listening on UDP {args.port}... (Ctrl+C to stop)")
    last_count, last_time = 0, time.time()
    try:
        while True:
            if args.show:
                frame, _ = rx.latest()
                if frame is not None:
                    cv2.imshow("TonyPi", frame)
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break
            else:
                time.sleep(0.05)

            now = time.time()
            if now - last_time >= 1.0:
                fps = (rx.frame_count - last_count) / (now - last_time)
                stale = rx.seconds_since_last_frame()
                status = "no frames yet" if stale == float("inf") else f"last frame {stale*1000:.0f} ms ago"
                print(f"{fps:5.1f} fps  ({status})")
                last_count, last_time = rx.frame_count, now
    except KeyboardInterrupt:
        pass
    finally:
        rx.stop()
        if args.show:
            cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
