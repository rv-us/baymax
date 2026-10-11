"""What the PT agent sees: people in the camera view, who is waving, and head tracking.

Runs on a background thread, separate from the LLM (a model step takes seconds; following
a face needs several updates a second). Each frame goes through a pose model: YOLO
(laptop_mvp/yolo11n-pose.pt, finds everyone in view) or MediaPipe's pose landmarker
(laptop_mvp/pose_landmarker_full.task, usually only one person). The head points give each
person's face position, the shoulders their size (a distance cue), the wrists whether they wave.

    source = RobotCamera("http://robot:8000")   # streaming.robot_camera; or FrameSource(0) for a webcam
    perception = Perception(source, PoseDetector(), backend).start()
    perception.people()          # [{"id": 1, "bearing_deg": 12, "waving": False, ...}]
    perception.follow(True)      # keep the head pointed at whoever is closest
"""
from __future__ import annotations

import base64
import collections
import logging
import os
import threading
import time
from dataclasses import dataclass, field

import cv2

log = logging.getLogger("pt_agent")

HERE = os.path.dirname(os.path.abspath(__file__))
POSE_MODEL = os.path.join(HERE, "..", "laptop_mvp", "pose_landmarker_full.task")
YOLO_MODEL = os.path.join(HERE, "..", "laptop_mvp", "yolo11n-pose.pt")

NOSE, L_EYE, R_EYE, L_EAR, R_EAR = 0, 2, 5, 7, 8
L_SHOULDER, R_SHOULDER, L_WRIST, R_WRIST = 11, 12, 15, 16
HEAD_POINTS = (NOSE, L_EYE, R_EYE, L_EAR, R_EAR)
VISIBLE = 0.5

# Head PWM servos (robot_api/servo/limits.py). Which way is which isn't verified on this
# robot; scripts/face_track.py lowers pan to follow a face on the right of the image and
# raises tilt to look up. Flip with BAYMAX_PAN_SIGN / BAYMAX_TILT_SIGN if the head runs away.
HEAD_CENTER = 1500
PAN_LIMITS = (1200, 1800)
TILT_LIMITS = (950, 1800)
PAN_SIGN = int(os.environ.get("BAYMAX_PAN_SIGN", "-1"))
TILT_SIGN = int(os.environ.get("BAYMAX_TILT_SIGN", "1"))
DEG_PER_PULSE = 0.09        # 180 degrees over 2000 us
CAMERA_FOV_DEG = 60         # horizontal, approximate
SCAN_PANS = (1500, 1700, 1500, 1300)
SCAN_TILT = 1650            # the robot is short: look up a little to find faces

WAVE_WINDOW_S = 2.0         # look at the last 2 s of wrist motion
WAVE_SWING = 0.25           # a swing must cover a quarter of the shoulder width
WAVE_REVERSALS = 2          # back and forth twice
RAISE_HOLD_S = 0.8          # a hand held above the head this long also counts


@dataclass
class Body:
    """One person in one frame, in normalized image coordinates (0-1)."""
    face: tuple             # (x, y) center of the visible head points
    size: float             # shoulder width as a fraction of the frame width
    wrists: dict            # side -> (x relative to shoulder in shoulder widths, above shoulder, above head)


def body_from_landmarks(points) -> Body | None:
    """points: 33 (x, y, visibility) tuples from the pose landmarker."""
    head = [points[i] for i in HEAD_POINTS if points[i][2] > VISIBLE]
    if not head:
        return None
    fx = sum(p[0] for p in head) / len(head)
    fy = sum(p[1] for p in head) / len(head)
    ls, rs = points[L_SHOULDER], points[R_SHOULDER]
    if ls[2] > VISIBLE and rs[2] > VISIBLE:
        size = abs(ls[0] - rs[0])
    else:  # shoulders out of view (close up): the head is about half a shoulder width
        size = 2 * (max(p[0] for p in head) - min(p[0] for p in head))
    size = max(size, 1e-3)
    wrists = {}
    for side, sh_i, wr_i in (("left", L_SHOULDER, L_WRIST), ("right", R_SHOULDER, R_WRIST)):
        sh, wr = points[sh_i], points[wr_i]
        if sh[2] > VISIBLE and wr[2] > VISIBLE:
            wrists[side] = ((wr[0] - sh[0]) / size, wr[1] < sh[1], wr[1] < fy)
    return Body((fx, fy), size, wrists)


def count_reversals(xs, swing: float) -> int:
    """Direction changes in xs, ignoring wiggles smaller than swing."""
    if not xs:
        return 0
    ref, direction, reversals = xs[0], 0, 0
    for x in xs[1:]:
        if direction == 0:
            if abs(x - ref) >= swing:
                direction, ref = (1 if x > ref else -1), x
        elif direction == 1:
            if x > ref:
                ref = x
            elif ref - x >= swing:
                reversals, direction, ref = reversals + 1, -1, x
        else:
            if x < ref:
                ref = x
            elif x - ref >= swing:
                reversals, direction, ref = reversals + 1, 1, x
    return reversals


class Person:
    """One tracked person across frames."""

    def __init__(self, pid: int, body: Body, now: float):
        self.id = pid
        self.first_seen = now
        self.simulated = False
        self._hands = {"left": collections.deque(maxlen=90), "right": collections.deque(maxlen=90)}
        self._raised_since = {"left": None, "right": None}
        self.update(body, now)

    def update(self, body: Body, now: float):
        self.body, self.last_seen = body, now
        for side in ("left", "right"):
            wrist = body.wrists.get(side)
            if wrist is None:
                self._raised_since[side] = None
                continue
            x_rel, above_shoulder, above_head = wrist
            self._hands[side].append((now, x_rel, above_shoulder))
            if above_head:
                self._raised_since[side] = self._raised_since[side] or now
            else:
                self._raised_since[side] = None

    def waving(self, now: float) -> bool:
        for samples in self._hands.values():
            xs = [x for t, x, up in samples if up and now - t <= WAVE_WINDOW_S]
            if len(xs) >= 4 and count_reversals(xs, WAVE_SWING) >= WAVE_REVERSALS:
                return True
        return False

    def hand_raised(self, now: float) -> bool:
        return any(t is not None and now - t >= RAISE_HOLD_S for t in self._raised_since.values())


class PeopleTracker:
    """Gives each person a stable id from frame to frame (nearest face wins)."""
    MATCH_DIST = 0.2
    FORGET_S = 1.5

    def __init__(self):
        self.people: dict[int, Person] = {}
        self._next_id = 1

    def update(self, bodies: list, now: float) -> list:
        unmatched = {pid: p for pid, p in self.people.items() if not p.simulated}
        for body in sorted(bodies, key=lambda b: -b.size):
            def dist(p):
                return ((p.body.face[0] - body.face[0]) ** 2 + (p.body.face[1] - body.face[1]) ** 2) ** 0.5
            best = min(unmatched.values(), key=dist, default=None)
            if best is not None and dist(best) < self.MATCH_DIST:
                best.update(body, now)
                del unmatched[best.id]
            else:
                self.people[self._next_id] = Person(self._next_id, body, now)
                self._next_id += 1
        for pid, p in list(self.people.items()):
            if not p.simulated and now - p.last_seen > self.FORGET_S:
                del self.people[pid]
        return list(self.people.values())

    def add_simulated(self, waving: bool = True) -> Person:
        """A pretend person for testing without a camera (KeyboardEars' /wave)."""
        body = Body((0.5, 0.4), 0.25, {})
        p = Person(self._next_id, body, time.time())
        p.simulated = True
        p.waving = lambda now, w=waving: w  # type: ignore[method-assign]
        self.people[p.id] = p
        self._next_id += 1
        return p


class PoseDetector:
    """MediaPipe pose landmarker in IMAGE mode, several people per frame. Frames are shrunk
    to max_width first: on a 1280-wide frame the model often finds nobody."""

    def __init__(self, model: str = POSE_MODEL, num_poses: int = 4, min_conf: float = 0.5,
                 max_width: int = 640):
        self.max_width = max_width
        import mediapipe as mp
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision as mp_vision

        self._mp = mp
        options = mp_vision.PoseLandmarkerOptions(
            base_options=mp_python.BaseOptions(model_asset_path=os.path.abspath(model)),
            running_mode=mp_vision.RunningMode.IMAGE,
            num_poses=num_poses,
            min_pose_detection_confidence=min_conf,
            min_tracking_confidence=min_conf,
            output_segmentation_masks=False,
        )
        self.landmarker = mp_vision.PoseLandmarker.create_from_options(options)

    def __call__(self, bgr) -> list:
        h, w = bgr.shape[:2]
        if w > self.max_width:  # landmarks are normalized, so resizing doesn't change them
            bgr = cv2.resize(bgr, (self.max_width, int(h * self.max_width / w)), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        result = self.landmarker.detect(self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=rgb))
        bodies = []
        for pose in result.pose_landmarks:
            body = body_from_landmarks([(p.x, p.y, p.visibility) for p in pose])
            if body:
                bodies.append(body)
        return bodies

    def close(self):
        self.landmarker.close()


# COCO keypoint (YOLO) -> MediaPipe landmark index, for the points body_from_landmarks uses.
COCO_TO_MP = {0: NOSE, 1: L_EYE, 2: R_EYE, 3: L_EAR, 4: R_EAR, 5: L_SHOULDER, 6: R_SHOULDER,
              9: L_WRIST, 10: R_WRIST}


class YoloDetector:
    """Ultralytics YOLO pose: every person in the frame (MediaPipe usually finds one).
    Runs on the GPU when there is one."""

    def __init__(self, model: str = YOLO_MODEL, min_conf: float = 0.5, imgsz: int = 640):
        from ultralytics import YOLO

        self.model = YOLO(model)
        self.min_conf, self.imgsz = min_conf, imgsz

    def __call__(self, bgr) -> list:
        result = self.model(bgr, imgsz=self.imgsz, conf=self.min_conf, verbose=False)[0]
        kp = result.keypoints
        if kp is None or kp.conf is None:
            return []
        bodies = []
        for xyn, conf in zip(kp.xyn.tolist(), kp.conf.tolist()):
            points = [(0.0, 0.0, 0.0)] * 33
            for coco, mp_i in COCO_TO_MP.items():
                points[mp_i] = (xyn[coco][0], xyn[coco][1], conf[coco])
            body = body_from_landmarks(points)
            if body:
                bodies.append(body)
        return bodies

    def close(self):
        pass


def make_detector(name: str = "yolo"):
    return YoloDetector() if name == "yolo" else PoseDetector()


class FrameSource:
    """The newest frame from a webcam index, a stream URL (the robot's MJPEG) or an image
    file. Read on its own thread so old frames are dropped, never queued."""

    def __init__(self, spec):
        self.spec = spec
        self.frame, self.t = None, 0.0
        self._running = False
        self._thread = None

    def start(self):
        if isinstance(self.spec, str) and os.path.isfile(self.spec):
            self.frame = cv2.imread(self.spec)
            if self.frame is None:
                raise RuntimeError(f"can't read image {self.spec}")
            self.t = time.time()
            return self
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def latest(self):
        """(frame, time it arrived); a still image counts as a new frame every call."""
        if self._thread is None and self.frame is not None:
            return self.frame, time.time()
        return self.frame, self.t

    def _loop(self):
        cap = None
        while self._running:
            if cap is None or not cap.isOpened():
                cap = cv2.VideoCapture(self.spec)
                if not cap.isOpened():
                    log.warning("camera %s not available, retrying", self.spec)
                    time.sleep(2)
                    continue
            ok, frame = cap.read()
            if not ok:
                cap.release()
                cap = None
                time.sleep(0.5)
                continue
            self.frame, self.t = frame, time.time()
        if cap is not None:
            cap.release()

    def stop(self):
        self._running = False


@dataclass
class HeadState:
    pan: int = HEAD_CENTER
    tilt: int = SCAN_TILT
    last_move: float = 0.0
    scan_i: int = 0
    paused: bool = False          # set while the body walks or turns
    extra: dict = field(default_factory=dict)


class Perception:
    """Runs pose detection on the newest frame and, when tracking someone, keeps the head on them."""

    PAN_GAIN = 350      # pulses per unit of normalized face offset (0.5 = edge of the frame)
    TILT_GAIN = 250
    DEADBAND = 0.06
    MAX_STEP = 90
    HEAD_HZ = 4
    # Only correct from a frame taken after the last head move had finished: the camera runs
    # ~0.2 s behind, and correcting again from older frames stacks moves into big overshoots
    # (seen on the robot: the head swung between its 1200 and 1800 limits).
    SETTLE_S = 0.2

    def __init__(self, source: FrameSource | None, detector, backend):
        self.source, self.detector, self.backend = source, detector, backend
        self.tracker = PeopleTracker()
        self.head = HeadState()
        self.following = False    # head follows the closest person (the patient is whoever is in front)
        self.fps = 0.0
        self._lock = threading.Lock()
        self._running = False
        self._thread = None

    def start(self):
        self.point_head(HEAD_CENTER, SCAN_TILT)
        if self.source is not None:
            self.source.start()
            self._running = True
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        return self

    def stop(self):
        self._running = False
        if self.source is not None:
            self.source.stop()

    # ---- detection ---------------------------------------------------------------

    def _loop(self):
        last_t, times = 0.0, collections.deque(maxlen=20)
        while self._running:
            frame, t = self.source.latest()
            if frame is None or t == last_t:
                time.sleep(0.01)
                continue
            last_t = t
            try:
                bodies = self.detector(frame)
            except Exception as e:  # noqa: BLE001  (a bad frame shouldn't kill tracking)
                log.warning("pose detection failed: %s", e)
                continue
            now = time.time()
            with self._lock:
                self.tracker.update(bodies, now)
            times.append(now)
            if len(times) > 1:
                self.fps = (len(times) - 1) / (times[-1] - times[0])
            if self.following:
                self._follow(t)

    def persons(self) -> list:
        with self._lock:
            return list(self.tracker.people.values())

    def person(self, pid):
        with self._lock:
            return self.tracker.people.get(pid)

    def bearing(self, p: Person) -> float:
        """Degrees to the robot's left (negative: right) of where the body faces."""
        head = -PAN_SIGN * (self.head.pan - HEAD_CENTER) * DEG_PER_PULSE
        return head - (p.body.face[0] - 0.5) * CAMERA_FOV_DEG

    def describe(self, p: Person) -> dict:
        now = time.time()
        b = self.bearing(p)
        return {"id": p.id, "bearing_deg": round(b), "where": "left" if b > 15 else "right" if b < -15 else "ahead",
                "size": round(p.body.size, 3), "waving": p.waving(now), "hand_raised": p.hand_raised(now),
                "followed": self.following and p is self.closest(), "seen_s_ago": round(now - p.last_seen, 1)}

    def people(self) -> list:
        return [self.describe(p) for p in sorted(self.persons(), key=lambda p: -p.body.size)]

    def simulate_person(self, waving: bool = True) -> int:
        with self._lock:
            return self.tracker.add_simulated(waving).id

    def jpeg_b64(self, width: int = 640, max_age_s: float = 1.0) -> str | None:
        if self.source is None:
            return None
        frame, t = self.source.latest()
        if frame is None or time.time() - t > max_age_s:
            return None
        h, w = frame.shape[:2]
        if w > width:
            frame = cv2.resize(frame, (width, int(h * width / w)), interpolation=cv2.INTER_AREA)
        ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        return base64.b64encode(jpg.tobytes()).decode() if ok else None

    # ---- head --------------------------------------------------------------------

    def follow(self, on: bool = True):
        """Keep the head pointed at the closest person (off: leave the head where it is)."""
        self.following = on

    def closest(self):
        """The biggest (closest) person in view, or None. The patient is whoever is in front."""
        people = self.persons()
        return max(people, key=lambda p: p.body.size) if people else None

    def point_head(self, pan: int, tilt: int, ms: int = 150):
        pan = max(PAN_LIMITS[0], min(PAN_LIMITS[1], int(pan)))
        tilt = max(TILT_LIMITS[0], min(TILT_LIMITS[1], int(tilt)))
        try:
            self.backend.call("servo/head", pan=pan, tilt=tilt, ms=ms)
        except Exception as e:  # noqa: BLE001  (busy or too hot: try again next time)
            log.debug("head move failed: %s", e)
            return
        self.head.pan, self.head.tilt, self.head.last_move = pan, tilt, time.time()

    def scan_step(self):
        """Look at the next spot in the search sweep."""
        self.head.scan_i = (self.head.scan_i + 1) % len(SCAN_PANS)
        self.point_head(SCAN_PANS[self.head.scan_i], SCAN_TILT, ms=400)

    def _follow(self, frame_t: float | None = None):
        if self.head.paused or time.time() - self.head.last_move < 1 / self.HEAD_HZ:
            return
        if frame_t is not None and frame_t < self.head.last_move + self.SETTLE_S:
            return  # this frame may predate the last move: wait for one that shows its result
        p = self.closest()
        if p is None or p.simulated:
            return
        err_x, err_y = p.body.face[0] - 0.5, p.body.face[1] - 0.5
        d_pan = 0 if abs(err_x) < self.DEADBAND else PAN_SIGN * err_x * self.PAN_GAIN
        d_tilt = 0 if abs(err_y) < self.DEADBAND else -TILT_SIGN * err_y * self.TILT_GAIN
        if d_pan == 0 and d_tilt == 0:
            return
        clip = self.MAX_STEP
        self.point_head(self.head.pan + max(-clip, min(clip, d_pan)),
                        self.head.tilt + max(-clip, min(clip, d_tilt)))
