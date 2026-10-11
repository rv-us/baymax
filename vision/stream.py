"""
Pose estimation on the robot's video stream (runs on the GX10).

A background thread takes the newest frame from streaming.receiver.FrameReceiver,
runs the pose model on it, and keeps the newest result with its joint angles. Frames
that arrive while the model is busy are skipped, never queued, so results never fall
behind the camera.

    from streaming.receiver import FrameReceiver
    from vision.pose import Pose
    from vision.stream import PoseStream

    poses = PoseStream(FrameReceiver().start(), Pose()).start()
    p = poses.latest()            # None until the first frame is processed
    p["angles"]["left_shoulder_elevation"], p["in_view"]["upper_body"]

scripts/pose_server.py --stream runs this and serves it over HTTP (GET /latest).
"""

import collections
import threading
import time

from vision.angles import in_view, joint_angles


class PoseStream:
    def __init__(self, receiver, detector):
        self.receiver = receiver    # anything with latest() -> (frame, arrival_time)
        self.detector = detector    # anything with detect(bgr) -> dict, like vision.pose.Pose
        self._latest = None
        self._lock = threading.Lock()
        self._done = collections.deque(maxlen=60)    # (finished_at, infer_ms, latency_ms)
        self._running = False
        self._thread = None
        self.processed = 0
        self.error = None

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def _loop(self):
        last_ts = 0.0
        while self._running:
            frame, ts = self.receiver.latest()
            if frame is None or ts == last_ts:
                time.sleep(0.002)
                continue
            last_ts = ts
            try:
                self._process(frame, ts)
            except Exception as e:  # keep serving; the error shows in stats()
                self.error = f"{type(e).__name__}: {e}"
                time.sleep(0.1)

    def _process(self, frame, frame_ts: float):
        result = self.detector.detect(frame)
        done = time.time()
        result["frame_time"] = frame_ts
        result["latency_ms"] = round((done - frame_ts) * 1000, 1)  # frame arrival -> pose ready
        if result.get("found"):
            result["angles"] = joint_angles(result["landmarks"], result["world"],
                                            result["width"], result["height"])
            result["in_view"] = in_view(result["landmarks"])
        else:
            result["angles"] = {}
            result["in_view"] = {"upper_body": False, "full_body": False, "missing": ["everything"]}
        with self._lock:
            self.processed += 1
            result["seq"] = self.processed
            self._latest = result
            self._done.append((done, result.get("infer_ms", 0.0), result["latency_ms"]))

    def latest(self):
        """The newest result, or None before the first frame."""
        with self._lock:
            return self._latest

    def stats(self) -> dict:
        """Frames per second processed and average times over the last 60 frames."""
        with self._lock:
            done = list(self._done)
            latest = self._latest
        out = {"processed": self.processed, "error": self.error,
               "seconds_since_last": round(time.time() - done[-1][0], 2) if done else None}
        if len(done) >= 2:
            span = done[-1][0] - done[0][0]
            out["fps"] = round((len(done) - 1) / span, 1) if span > 0 else None
            out["infer_ms"] = round(sum(d[1] for d in done) / len(done), 1)
            out["latency_ms"] = round(sum(d[2] for d in done) / len(done), 1)
        if latest is not None:
            out["person_found"] = latest.get("found", False)
        return out

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
