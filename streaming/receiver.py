"""
Low-latency frame receiver for the TonyPi video stream (runs on the GX10).

Usage in your own code:

    from receiver import FrameReceiver

    rx = FrameReceiver()
    rx.start()
    while True:
        frame, ts = rx.latest()      # newest BGR frame (numpy array) and its arrival time
        if frame is None:
            continue
        ...                          # run your model on `frame`

Standalone test (prints fps; add --show if the GX10 has a display):

    python3 receiver.py
    python3 receiver.py --show
"""

import argparse
import threading
import time

import cv2


def _check_gstreamer():
    info = cv2.getBuildInformation()
    for line in info.splitlines():
        if "GStreamer" in line:
            if "YES" in line:
                return
            break
    raise RuntimeError(
        "This OpenCV build has no GStreamer support (common with pip's opencv-python).\n"
        "Fix: pip uninstall opencv-python opencv-python-headless; sudo apt install python3-opencv"
    )


def build_pipeline(port: int = 5000, latency_ms: int = 50) -> str:
    return (
        f"udpsrc port={port} buffer-size=4194304 "
        'caps="application/x-rtp,media=video,encoding-name=H264,payload=96,clock-rate=90000" '
        f"! rtpjitterbuffer latency={latency_ms} drop-on-latency=true "
        "! rtph264depay ! h264parse ! avdec_h264 max-threads=1 "
        "! videoconvert ! video/x-raw,format=BGR "
        "! appsink drop=true max-buffers=1 sync=false"
    )


class FrameReceiver:
    """Reads the stream on a background thread and always keeps only the newest frame."""

    def __init__(self, port: int = 5000, latency_ms: int = 50):
        _check_gstreamer()
        self.pipeline = build_pipeline(port, latency_ms)
        self._cap = None
        self._thread = None
        self._lock = threading.Lock()
        self._frame = None
        self._ts = 0.0
        self._running = False
        self._opened = threading.Event()
        self._error = None
        self.frame_count = 0

    def start(self, open_timeout: float = 1.0):
        """Start receiving in the background. Returns without waiting for the Pi to start sending."""
        self._running = True
        self._opened.clear()
        self._error = None
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        # Opening blocks until the first packet arrives, so don't wait for it here.
        # A broken pipeline fails fast, though, so give it a moment to report that.
        self._opened.wait(open_timeout)
        if self._error:
            raise RuntimeError(self._error)
        return self

    def _loop(self):
        cap = cv2.VideoCapture(self.pipeline, cv2.CAP_GSTREAMER)
        if not cap.isOpened():
            self._error = "Could not open the GStreamer pipeline. Is the port already in use?"
            self._running = False
            self._opened.set()
            return
        self._cap = cap
        self._opened.set()
        if not self._running:  # stop() was called while we were waiting for the first packet
            cap.release()
            return
        while self._running:
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.005)
                continue
            with self._lock:
                self._frame = frame
                self._ts = time.time()
                self.frame_count += 1

    def latest(self):
        """Return (frame, arrival_time). frame is None until the first frame arrives."""
        with self._lock:
            return self._frame, self._ts

    def seconds_since_last_frame(self) -> float:
        with self._lock:
            return float("inf") if self._ts == 0 else time.time() - self._ts

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1)
        if self._cap:
            self._cap.release()


def main():
    parser = argparse.ArgumentParser(description="Receive the TonyPi video stream.")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--latency", type=int, default=50, help="jitter buffer in ms")
    parser.add_argument("--show", action="store_true", help="display frames in a window")
    args = parser.parse_args()

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
