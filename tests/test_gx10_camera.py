"""The GX10 owns the robot's camera; the dashboard asks it for the picture and the pose. And the microphone
bridge: what the patient says reaches the dashboard, and the robot never hears itself."""
import contextlib
import threading
import time

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")
uvicorn = pytest.importorskip("uvicorn")

from dashboard import pose_service  # noqa: E402
from dashboard.gx10_camera import Gx10Camera  # noqa: E402
from test_dashboard_api import make, tiny_plan, wait_for  # noqa: E402
from test_pose_service import free_port  # noqa: E402
from voice.bridge import Bridge  # noqa: E402


class RobotCam:
    """The robot's camera as streaming.robot_camera.RobotCamera gives it: new frames, a mode, stop()."""
    opened = []

    def __init__(self):
        self.mode, self.stopped, self.t0 = "udp", False, time.time()
        RobotCam.opened.append(self)

    def latest(self):
        stamp = int((time.time() - self.t0) * 30)
        return np.full((48, 64, 3), 90, np.uint8), stamp

    def stop(self):
        self.stopped = True


class Tracker:
    closed = 0

    def process(self, frame, t):
        pose = {"visible": True, "x": 0.1, "size": 0.5, "calibrated": True, "people": 2,
                "arms": {"L": "TRACKING", "R": "TRACKING"}, "green": {"L": True, "R": True}, "pulses": {},
                "angles": {"shoulder_elevation": 40.0, "elbow": 10.0}}
        return pose, {"shoulders": [[10, 10], [40, 10]], "arms": {}, "person": [5, 5, 50, 45]}

    def close(self):
        Tracker.closed += 1


def serve(app, port):
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    assert wait_for(lambda: server.started, 5)
    return server


@pytest.fixture
def gx10():
    """A pose service that owns the camera (and lets it go after 0.6 s of nobody asking)."""
    RobotCam.opened.clear()
    owner = pose_service.CameraOwner(Tracker, RobotCam, idle_s=0.6)
    port = free_port()
    server = serve(pose_service.make_app(Tracker, udp_port=port, camera=owner), port)
    yield f"http://127.0.0.1:{port}", owner, server
    server.should_exit = True


def test_the_camera_is_opened_by_the_gx10_only_when_asked_and_let_go_after(gx10):
    url, owner, _ = gx10
    assert not owner.running and not RobotCam.opened            # nothing asked, nothing opened
    cam = Gx10Camera(url)
    assert cam.start()
    assert wait_for(lambda: cam.pose()["visible"], 5)
    pose = cam.pose()
    assert pose["angles"]["shoulder_elevation"] == 40.0 and pose["people"] == 2 and pose["green"] == {"L": True, "R": True}
    assert time.time() - pose["t"] < 1.0                         # stamped on this machine's clock
    assert cam.source == "gx10" and cam.tracking and cam.ms is not None and len(RobotCam.opened) == 1
    assert cam.jpeg(640).startswith(b"\xff\xd8")                 # the picture, with the tracking drawn on it
    assert cam.jpeg(160).startswith(b"\xff\xd8")
    cam.stop()
    assert wait_for(lambda: RobotCam.opened[0].stopped, 5)       # nobody asking: the camera is let go
    assert not owner.running


def test_a_service_that_does_not_own_the_camera_is_not_one_to_use():
    port = free_port()
    server = serve(pose_service.make_app(Tracker, udp_port=port), port)
    try:
        cam = Gx10Camera(f"http://127.0.0.1:{port}")
        assert not cam.start() and "--camera" in cam.status["detail"]
        assert not Gx10Camera("http://127.0.0.1:9").start()      # nothing there at all
    finally:
        server.should_exit = True


def test_when_the_gx10_stops_answering_nobody_is_seen_and_it_is_not_tracking(gx10):
    url, _, server = gx10
    cam = Gx10Camera(url)
    cam.start()
    assert wait_for(lambda: cam.pose()["visible"], 5)
    server.should_exit = True
    assert wait_for(lambda: not cam.pose()["visible"] and not cam.tracking, 8)
    assert "isn't answering" in cam.status["detail"] or cam.status["state"] == "down"
    cam.stop()


def test_the_dashboard_takes_its_camera_from_the_gx10(gx10, tmp_path, fake_robot):
    url, _, _ = gx10
    client, parts = make(tmp_path)
    parts["gateway"].webcam = object()                           # a laptop webcam in use is never replaced
    assert parts["attach_gx10_camera"](url) is False
    parts["gateway"].webcam = None
    assert parts["attach_gx10_camera"](url) is True
    cam = parts["gateway"].webcam
    assert cam.gx10_camera and parts["settings"].pose_url == url
    assert parts["attach_gx10_camera"](url) is True and parts["gateway"].webcam is cam   # asking again changes nothing
    assert wait_for(lambda: cam.pose()["visible"], 5)
    r = client.get("/api/camera.jpg")
    assert r.status_code == 200 and r.content.startswith(b"\xff\xd8")                   # the GX10's annotated picture
    status = client.get("/api/status").json()["webcam"]
    assert status["owned_by_gx10"] and status["label"] == "Robot camera" and status["pose"] == "gx10"
    cam.stop()


def test_a_session_on_the_robot_uses_the_camera_the_gx10_owns(gx10, tmp_path, fake_robot):
    url, _, _ = gx10
    client, parts = make(tmp_path)
    tiny_plan(tmp_path)
    parts["settings"].dry_run = False                            # as on a robot: the dashboard has no webcam of its own
    assert parts["attach_gx10_camera"](url)
    r = client.post("/api/live/start", json={"plan": "tiny", "simulate": True})
    assert r.status_code == 200 and r.json()["synthetic_tracker"] is False
    assert wait_for(lambda: (parts["live"].state.get("tracking") or {}).get("on") == "gx10", 5)
    t = parts["live"].state["tracking"]
    assert t["people"] == 2 and t["green"] == {"L": True, "R": True} and t["camera"] == "Robot camera"
    client.post("/api/live/stop")
    assert wait_for(lambda: not parts["live"].running, 20)
    parts["gateway"].webcam.stop()


# ---- the microphone ---------------------------------------------------------------------------

def test_the_robot_talking_is_known_while_a_speech_call_is_in_flight(tmp_path, fake_robot):
    client, parts = make(tmp_path)
    release, inside = threading.Event(), threading.Event()

    class Slow:
        def call(self, path, **kw):
            inside.set()
            release.wait(5)
            return {"said": kw.get("text")}

    parts["gateway"].backend = Slow()
    parts["gateway"].local = False
    assert client.get("/api/voice").json()["speaking"] is False
    t = threading.Thread(target=lambda: parts["gateway"].call("sensors/voice/say", text="Hello"))
    t.start()
    assert inside.wait(5)
    assert client.get("/api/voice").json()["speaking"] is True
    release.set()
    t.join(5)
    assert client.get("/api/voice").json()["speaking"] is False


def test_pain_stops_the_robot_and_an_answer_goes_only_to_a_question_that_is_waiting(tmp_path, fake_robot):
    client, parts = make(tmp_path)
    tiny_plan(tmp_path)
    assert client.post("/api/voice/heard", json={"text": "Yes I'm ready"}).json() == {"pain": False, "answered": False}
    client.post("/api/live/start", json={"plan": "tiny", "simulate": False})
    assert wait_for(lambda: parts["live"].state.get("waiting"), 10)
    assert client.get("/api/voice").json()["waiting"] is True
    assert client.post("/api/voice/heard", json={"text": "Yes I'm ready"}).json() == {"pain": False, "answered": True}
    assert wait_for(lambda: parts["live"].state.get("state") not in (None, "READY_CHECK"), 10)
    assert client.post("/api/voice/heard", json={"text": "ow, that hurts"}).json() == {"pain": True, "answered": False}
    assert client.get("/api/safety").json()["stop"]["kind"] == "pain"
    assert wait_for(lambda: not parts["live"].running, 20)


class FakeListener:
    def __init__(self):
        self.callbacks, self.events = [], []

    def on_transcript(self, cb):
        self.callbacks.append(cb)

    @contextlib.contextmanager
    def robot_speaking(self):
        self.events.append("mute")
        try:
            yield
        finally:
            self.events.append("unmute")


class Said:
    def __init__(self, text, age=0.0):
        self.text, self.end = text, time.time() - age


def test_the_bridge_sends_what_is_heard_and_mutes_while_the_robot_talks():
    listener, posted, speaking = FakeListener(), [], {"on": False}
    get = lambda path: {"speaking": speaking["on"]}                                  # noqa: E731
    post = lambda path, body: posted.append((path, body)) or {"pain": False, "answered": True}   # noqa: E731
    bridge = Bridge(listener, get, post)
    threading.Thread(target=bridge.run, daemon=True).start()
    listener.callbacks[0](Said("I'm ready"))
    listener.callbacks[0](Said("a long time ago", age=60))                           # too old to act on
    assert posted == [("/api/voice/heard", {"text": "I'm ready"})]
    speaking["on"] = True
    assert wait_for(lambda: listener.events == ["mute"], 2)
    speaking["on"] = False
    assert wait_for(lambda: listener.events == ["mute", "unmute"], 2)
    bridge.stop()


def test_the_bridge_survives_a_dashboard_that_is_not_there():
    listener = FakeListener()

    def down(*a):
        raise OSError("connection refused")

    bridge = Bridge(listener, down, down)
    listener.callbacks[0](Said("ow"))                                                # logged, not raised
    t = threading.Thread(target=bridge.run, daemon=True)
    t.start()
    time.sleep(0.2)
    assert t.is_alive() and listener.events == []                                    # keeps its last state, keeps trying
    bridge.stop()
    t.join(2)
    assert not t.is_alive()
