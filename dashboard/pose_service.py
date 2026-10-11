"""The GX10's pose service: the dashboard sends camera frames, this sends back the patient's pose.

    python -m dashboard.pose_service                 # 0.0.0.0:9100
    python -m dashboard.pose_service --port 9200

It runs arm_mvp's whole tracker (dashboard/armtrack.py: MediaPipe landmarks, the 3D arm
solve, joint angles) so the dashboard machine only captures and draws. The robot's own
camera stream can feed it the same way, one POST per frame:

    GET  /health                       {"ok": true, "service": "baymax-arm-pose", "udp": 9100, ...}
    POST /track?session=ID&t=SECONDS   body: a JPEG  ->  {"pose": {...}, "overlay": {...}, "ms": n}
    UDP  the same port: JPEG frames in, poses out (below)

Over a busy Wi-Fi the UDP way is the one to use, for the same reasons the robot's own video is
UDP (docs/streaming-setup.md): nothing is retransmitted, so a lost packet costs one frame and
never stalls the ones behind it, and the sender doesn't wait for an answer, so the rate isn't
limited by the round trip. A frame is cut into datagrams of at most CHUNK bytes:

    frame:  "BPF1" | session (8 bytes) | seq u32 | t f64 | chunk u16 | chunks u16 | JPEG bytes
            chunk numbers 0..chunks-1 carry the picture; chunk number `chunks` is a parity datagram (the
            XOR of the others, zero-padded), so any ONE lost datagram of a frame is rebuilt on arrival
    reply:  "BPR1" | seq u32 | JSON {"pose", "overlay", "ms"}      (to the frame's sender)

Only the newest frame per session is ever worked on: a frame with a missing chunk is dropped when
a newer one starts, and a frame that arrives after a newer one has been answered is ignored.

With `--camera` the GX10 OWNS the robot's camera: nothing else has to open it. The first request for
the camera makes the service receive the robot's video (BAYMAX_API_URL / BAYMAX_API_TOKEN), track every
frame, and keep the newest answer; the dashboard just asks for it. After IDLE_CAMERA_S with nobody asking
the camera is let go again, so other tools can have it.

    GET  /camera/pose?after=SEQ&wait=S  {"pose", "overlay", "ms", "seq", "running", "mode", "fps", "age", "error"}:
                                         the newest answer, waiting up to S seconds for one newer than SEQ
    GET  /camera.jpg?width=W&quality=Q   the newest picture with the tracked person and arms drawn on it

Each `session` has its own tracker, because smoothing, calibration and left/right identity
are per patient. Idle sessions are dropped after a minute. Needs: opencv-python-headless,
mediapipe, numpy, fastapi, uvicorn. There is no login: run it on a trusted network only.
"""

import argparse
import json
import socket
import struct
import threading
import time

DEFAULT_PORT = 9100
SERVICE = "baymax-arm-pose"
FRAME_MAGIC, REPLY_MAGIC = b"BPF1", b"BPR1"
FRAME_HEADER = struct.Struct("!4s8sIdHH")
REPLY_HEADER = struct.Struct("!4sI")
CHUNK = 1200                 # JPEG bytes per datagram: under any Wi-Fi/VPN MTU, so nothing fragments
MAX_REPLY = 1400
IDLE_S = 60.0
MAX_SESSIONS = 8
IDLE_CAMERA_S = 20.0         # with nobody asking for the camera this long, it is let go


class Sessions:
    def __init__(self, make):
        self.make, self.items, self.frames = make, {}, 0
        self.lock = threading.Lock()

    def get(self, sid: str):
        now = time.monotonic()
        with self.lock:
            for key in [k for k, (_, seen, _) in self.items.items() if now - seen > IDLE_S]:
                self.items.pop(key)[0].close()
            if sid not in self.items:
                if len(self.items) >= MAX_SESSIONS:
                    oldest = min(self.items, key=lambda k: self.items[k][1])
                    self.items.pop(oldest)[0].close()
                self.items[sid] = (self.make(), now, threading.Lock())
            tracker, _, lock = self.items[sid]
            self.items[sid] = (tracker, now, lock)
            return tracker, lock


def xor_parts(parts) -> bytes:
    out = bytearray(CHUNK)
    for part in parts:
        for i, byte in enumerate(part):
            out[i] ^= byte
    return bytes(out)


def encode_frame(session: bytes, seq: int, t: float, jpeg: bytes) -> list:
    """A frame as datagrams (the sender's half of the protocol above): its chunks, then parity."""
    parts = [jpeg[i:i + CHUNK] for i in range(0, len(jpeg), CHUNK)] or [b""]
    parts.append(xor_parts(parts))
    return [FRAME_HEADER.pack(FRAME_MAGIC, session, seq, t, i, len(parts) - 1) + part for i, part in enumerate(parts)]


def encode_reply(seq: int, pose: dict, overlay: dict, ms: float) -> bytes:
    body = json.dumps({"pose": pose, "overlay": overlay, "ms": ms}, separators=(",", ":"))
    if len(body) > MAX_REPLY:  # the picture can go without its skeleton; the pose can't
        body = json.dumps({"pose": pose, "overlay": {"shoulders": None, "arms": {}}, "ms": ms}, separators=(",", ":"))
    return REPLY_HEADER.pack(REPLY_MAGIC, seq) + body.encode()


class UdpServer:
    """Frames in over UDP, newest wins, poses out. One thread receives, one works."""

    def __init__(self, sessions: Sessions, port: int, host: str = "0.0.0.0"):
        self.sessions = sessions
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
        self.sock.bind((host, port))
        self.port = self.sock.getsockname()[1]
        self.partial = {}     # session -> {seq, t, count, parts, addr}: the frame being assembled
        self.ready = {}       # session -> (seq, t, jpeg, addr): the newest complete frame, not yet worked on
        self.answered = {}    # session -> the highest seq already answered
        self.dropped = 0
        self.cond = threading.Condition()
        self.alive = True

    def start(self):
        for target in (self._receive, self._work):
            threading.Thread(target=target, daemon=True).start()
        return self

    def close(self):
        self.alive = False
        with self.cond:
            self.cond.notify_all()
        self.sock.close()

    def _receive(self):
        while self.alive:
            try:
                data, addr = self.sock.recvfrom(65535)
            except OSError:
                if not self.alive:
                    return
                continue  # Windows reports an ICMP "port unreachable" as a reset: keep going
            if len(data) < FRAME_HEADER.size or data[:4] != FRAME_MAGIC:
                continue
            _, sid, seq, t, index, count = FRAME_HEADER.unpack_from(data)
            if seq <= self.answered.get(sid, -1) or index > count:
                continue
            cur = self.partial.get(sid)
            if cur is None or seq > cur["seq"]:
                if cur is not None:
                    self.dropped += 1       # a newer frame began before the older one was whole
                cur = self.partial[sid] = {"seq": seq, "t": t, "count": count, "parts": {}, "addr": addr}
            elif seq < cur["seq"] or count != cur["count"]:
                continue
            cur["parts"][index] = data[FRAME_HEADER.size:]
            have = [i for i in cur["parts"] if i < count]
            if len(have) == count - 1 and count in cur["parts"]:   # one chunk lost: rebuild it from the parity
                lost = next(i for i in range(count) if i not in cur["parts"])
                cur["parts"][lost] = xor_parts([v for k, v in cur["parts"].items() if k != lost])
                have = list(range(count))
            if len(have) == count:
                jpeg = b"".join(cur["parts"][i] for i in range(cur["count"]))
                del self.partial[sid]
                with self.cond:
                    self.ready[sid] = (seq, cur["t"], jpeg, cur["addr"])
                    self.cond.notify()

    def _work(self):
        import cv2
        import numpy as np
        while self.alive:
            with self.cond:
                while self.alive and not self.ready:
                    self.cond.wait(timeout=1.0)
                if not self.alive:
                    return
                sid = min(self.ready, key=lambda k: self.ready[k][0])   # the session waiting longest
                seq, t, jpeg, addr = self.ready.pop(sid)
            started = time.perf_counter()
            frame = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            if frame is None:
                continue
            try:
                tracker, lock = self.sessions.get(sid.hex())
                with lock:
                    pose, overlay = tracker.process(frame, t)
            except Exception:
                continue  # one bad frame must not stop the service
            self.sessions.frames += 1
            self.answered[sid] = seq
            try:
                self.sock.sendto(encode_reply(seq, pose, overlay, round((time.perf_counter() - started) * 1000, 1)), addr)
            except OSError:
                pass


class CameraOwner:
    """The GX10 holds the robot's camera and tracks it, for as long as someone is asking.

    `open_camera()` returns something with latest() -> (frame, stamp) and stop() (streaming/robot_camera.py's
    RobotCamera). One tracker, so the calibration and the choice of person carry on between the dashboard's
    requests; frames are worked on as they arrive, newest first, never queued."""

    def __init__(self, make_tracker, open_camera, idle_s: float = IDLE_CAMERA_S):
        self.make_tracker, self.open_camera, self.idle_s = make_tracker, open_camera, idle_s
        self.cond = threading.Condition()
        self.thread = None
        self.last_ask = 0.0
        self.answer = None        # {pose, overlay, jpeg, ms, seq, mono}
        self.seq = 0
        self.mode = None
        self.error = None
        self.fps = 0.0
        self._stamp = None

    def ask(self) -> None:
        """Someone wants the camera: start it if it isn't running."""
        with self.cond:
            self.last_ask = time.monotonic()
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._run, name="gx10-camera", daemon=True)
                self.thread.start()

    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive() and self.mode)

    def newest(self, after: int = -1, wait: float = 0.0):
        """The newest answer, once it is newer than `after` (waiting up to `wait` seconds); None if there is none."""
        self.ask()
        end = time.monotonic() + wait
        with self.cond:
            while (self.answer is None or self.answer["seq"] <= after) and time.monotonic() < end:
                self.cond.wait(max(0.0, end - time.monotonic()))
            return self.answer

    def _run(self) -> None:
        from .overlay import annotate
        cam = tracker = None
        try:
            while time.monotonic() - self.last_ask < self.idle_s:
                if cam is None:
                    try:
                        tracker = tracker or self.make_tracker()
                        cam = self.open_camera()
                        self.mode, self.error = getattr(cam, "mode", None) or "udp", None
                    except Exception as e:  # the robot isn't reachable yet: keep trying while someone asks
                        self.error, cam = str(e) or type(e).__name__, None
                        time.sleep(2.0)
                        continue
                frame, stamp = cam.latest()
                if frame is None or stamp == self._stamp:
                    time.sleep(0.005)
                    continue
                self._stamp = stamp
                started = time.perf_counter()
                try:
                    pose, overlay = tracker.process(frame, time.monotonic())
                    jpeg = annotate(frame, overlay, pose.get("people", 0))
                except Exception as e:  # one bad frame must not stop the camera
                    self.error = str(e) or type(e).__name__
                    continue
                now = time.monotonic()
                with self.cond:
                    prev = self.answer
                    if prev:
                        self.fps = 0.8 * self.fps + 0.2 / max(1e-3, now - prev["mono"])
                    self.seq += 1
                    self.answer = {"pose": pose, "overlay": overlay, "jpeg": jpeg, "seq": self.seq, "mono": now,
                                   "ms": round((time.perf_counter() - started) * 1000, 1)}
                    self.cond.notify_all()
        finally:
            with self.cond:
                self.mode, self.answer, self.fps, self._stamp = None, None, 0.0, None
                self.cond.notify_all()
            if cam is not None:
                cam.stop()
            if tracker is not None:
                tracker.close()

    def health(self) -> dict:
        a = self.answer
        return {"running": self.running, "mode": self.mode, "fps": round(self.fps, 1), "error": self.error,
                "age": None if a is None else round(time.monotonic() - a["mono"], 2)}


def make_app(make_tracker=None, udp_port: int | None = None, camera: CameraOwner | None = None):
    import cv2
    import numpy as np
    from fastapi import FastAPI, HTTPException, Request, Response

    if make_tracker is None:
        from .armtrack import ArmTracker as make_tracker  # noqa: N813
    app = FastAPI(title="Baymax pose service")
    sessions = Sessions(make_tracker)
    app.state.sessions = sessions
    app.state.camera = camera

    @app.get("/health")
    def health():
        return {"ok": True, "service": SERVICE, "udp": udp_port, "sessions": len(sessions.items),
                "frames": sessions.frames, "camera": camera.health() if camera else None}

    @app.get("/camera/pose")
    def camera_pose(after: int = -1, wait: float = 0.0):
        if camera is None:
            raise HTTPException(404, "this pose service doesn't own the camera (start it with --camera)")
        a = camera.newest(after, max(0.0, min(2.0, wait)))
        h = camera.health()
        if a is None:
            return {"pose": None, "overlay": None, "seq": -1, "ms": None, **h}
        return {"pose": a["pose"], "overlay": a["overlay"], "seq": a["seq"], "ms": a["ms"], **h}

    @app.get("/camera.jpg")
    def camera_jpg(width: int = 640, quality: int = 70):
        if camera is None:
            raise HTTPException(404, "this pose service doesn't own the camera (start it with --camera)")
        a = camera.newest(-1, 3.0)
        if a is None or a["jpeg"] is None:
            raise HTTPException(503, camera.error or "no picture from the robot's camera yet")
        jpeg, width = a["jpeg"], max(160, min(1280, width))
        if width < 640:
            arr = cv2.imdecode(np.frombuffer(jpeg, np.uint8), cv2.IMREAD_COLOR)
            ok, buf = cv2.imencode(".jpg", cv2.resize(arr, (width, int(arr.shape[0] * width / arr.shape[1]))),
                                   [cv2.IMWRITE_JPEG_QUALITY, max(20, min(95, quality))])
            jpeg = buf.tobytes() if ok else jpeg
        return Response(jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.post("/track")
    async def track(request: Request, session: str = "default", t: float | None = None):
        body = await request.body()
        frame = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise HTTPException(400, "the body is not a JPEG")
        started = time.perf_counter()
        tracker, lock = sessions.get(session[:40])
        with lock:  # a tracker is one patient's state: frames are handled in order
            pose, overlay = tracker.process(frame, time.monotonic() if t is None else t)
        sessions.frames += 1
        return {"pose": pose, "overlay": overlay, "ms": round((time.perf_counter() - started) * 1000, 1)}

    return app


def main():
    ap = argparse.ArgumentParser(description="Baymax pose service (run on the GX10).")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--camera", action="store_true",
                    help="own the robot's camera: receive its video (BAYMAX_API_URL, BAYMAX_API_TOKEN) and track "
                         "it for the dashboard, which then gets the picture and the pose from here")
    args = ap.parse_args()
    import uvicorn
    print(f"Baymax pose service on http://{args.host}:{args.port}")
    from .armtrack import ArmTracker, ROOT
    owner = None
    if args.camera:
        import sys
        if ROOT not in sys.path:
            sys.path.insert(0, ROOT)
        from streaming.robot_camera import RobotCamera
        owner = CameraOwner(ArmTracker, lambda: RobotCamera().start())
    app = make_app(ArmTracker, udp_port=args.port, camera=owner)
    udp = UdpServer(app.state.sessions, args.port, args.host).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
