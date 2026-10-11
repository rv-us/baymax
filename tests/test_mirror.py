"""Mirror: the robot copies the patient's arms from tracked pulses, and stays safe doing it."""
import time

import pytest

from dashboard import mirror as mirror_module
from test_dashboard_api import make, tiny_plan, wait_for

REST = {"8": 725, "7": 800, "6": 575, "16": 275, "15": 200, "14": 425}


class Cam:
    """A tracking camera whose newest pose says where the arms should go."""
    tracking = True
    label = "Laptop webcam"
    source = "gx10"
    transport = "udp"
    delivery = 1.0
    status = {"state": "ok", "detail": "Laptop webcam"}

    def __init__(self):
        self.pulses = dict(REST)
        self.t = None           # None: the answers are fresh; a number: their age in seconds

    def pose(self):
        age = 0.0 if self.t is None else self.t
        return {"visible": True, "calibrated": True, "arms": {"L": "TRACKING", "R": "TRACKING"},
                "angles": {"shoulder_elevation": 40.0, "elbow": 10.0}, "pulses": dict(self.pulses),
                "t": time.time() - age}

    def jpeg(self, width=640, quality=70):
        return b"FRAME"

    def stop(self):
        pass


@pytest.fixture
def rig(tmp_path, fake_robot, monkeypatch):
    client, parts = make(tmp_path)
    cam = Cam()
    parts["gateway"].webcam = cam
    sent = []
    real = parts["gateway"].call

    def spy(path, **kwargs):
        if path == "servo/set_many":
            sent.append(dict(kwargs["targets"]))
        return real(path, **kwargs)

    monkeypatch.setattr(parts["gateway"], "call", spy)
    monkeypatch.setattr(mirror_module, "HZ", 100.0)
    return client, parts, cam, sent


def test_pulses_from_the_tracker_are_forwarded_and_only_when_they_change(rig):
    client, parts, cam, sent = rig
    assert client.post("/api/mirror/start").json()["running"]
    cam.pulses = {**REST, "8": 500, "16": 500}
    assert wait_for(lambda: any(s.get("8") == 500 for s in sent), 3)
    n = len(sent)
    time.sleep(0.3)
    assert len(sent) == n                                   # the same answer is not sent again
    assert client.get("/api/mirror").json()["arms"] == {"L": "TRACKING", "R": "TRACKING"}
    client.post("/api/mirror/stop")
    assert wait_for(lambda: not parts["mirror"].running, 3)
    assert sent[-1] == REST                                 # and it eases the arms back to rest at the end


def test_an_old_answer_is_never_replayed_and_nothing_is_sent_without_one(rig):
    client, parts, cam, sent = rig
    client.post("/api/mirror/start")
    cam.pulses = {**REST, "8": 500}
    assert wait_for(lambda: any(s.get("8") == 500 for s in sent), 3)
    cam.t = 5.0                                             # the GX10 has gone quiet
    cam.pulses = {**REST, "8": 300}
    count = len(sent)
    time.sleep(0.5)
    assert len(sent) == count                               # stale: it holds still, it doesn't move or relax
    client.post("/api/mirror/stop")
    assert wait_for(lambda: not parts["mirror"].running, 3)
    assert sent[-1] == REST                                 # Stop is what eases the arms back to rest


def test_only_the_arms_the_tracker_lists_are_sent(rig):
    """The tracker leaves a non-green arm out of `pulses`; mirror forwards exactly what it is given."""
    client, parts, cam, sent = rig
    cam.pulses = {"8": 500, "7": 810, "6": 600}             # only the left arm is green
    client.post("/api/mirror/start")
    assert wait_for(lambda: any(s.get("8") == 500 for s in sent), 3)
    time.sleep(0.2)
    moved = {k for s in sent for k in s}
    assert moved == {"8", "7", "6"}                         # nothing for the right arm
    client.post("/api/mirror/stop")
    assert wait_for(lambda: not parts["mirror"].running, 3)


def test_a_safety_stop_ends_mirroring_and_nothing_moves_afterwards(rig):
    client, parts, cam, sent = rig
    client.post("/api/mirror/start")
    cam.pulses = {**REST, "8": 500}
    assert wait_for(lambda: any(s.get("8") == 500 for s in sent), 3)
    client.post("/api/safety/heard", json={"text": "ouch, that hurts"})
    assert wait_for(lambda: not parts["mirror"].running, 3)
    count = len(sent)
    cam.pulses = {**REST, "8": 300}
    time.sleep(0.3)
    assert len(sent) == count and sent[-1].get("8") != 300  # frozen where it was; no rest move either
    assert client.post("/api/mirror/start").status_code == 409   # and it won't start while the stop is latched


def test_it_will_not_start_without_a_tracking_camera(tmp_path, fake_robot):
    client, parts = make(tmp_path)
    r = client.post("/api/mirror/start")
    assert r.status_code == 409 and "camera" in r.json()["detail"].lower()
    parts["gateway"].webcam = type("C", (), {"tracking": False, "label": "x"})()
    assert client.post("/api/mirror/start").status_code == 409


def test_mirror_and_a_session_never_run_together(rig):
    client, parts, cam, sent = rig
    tiny_plan(rig[1]["settings"].plans_dir and __import__("pathlib").Path(rig[1]["settings"].plans_dir).parent)
    client.post("/api/mirror/start")
    assert client.post("/api/live/start", json={"plan": "tiny"}).status_code == 409
    assert client.post("/api/mirror/start").status_code == 409              # already running
    client.post("/api/mirror/stop")
    assert wait_for(lambda: not parts["mirror"].running, 3)
    assert client.post("/api/live/start", json={"plan": "tiny", "pace": 8}).status_code == 200
    assert client.post("/api/mirror/start").status_code == 409              # a session has the robot
    client.post("/api/live/stop")
    assert wait_for(lambda: not parts["live"].running, 20)


def test_the_picture_comes_from_the_mirror_while_it_runs(rig):
    client, parts, cam, sent = rig
    assert client.get("/api/mirror.jpg").status_code == 404
    client.post("/api/mirror/start")
    r = client.get("/api/mirror.jpg")
    assert r.status_code == 200 and r.content == b"FRAME"
    client.post("/api/mirror/stop")
    assert wait_for(lambda: not parts["mirror"].running, 3)


def test_refused_moves_end_mirroring_with_the_reason(rig, monkeypatch):
    client, parts, cam, sent = rig
    from dashboard.robot import Refused
    real = parts["gateway"].call

    def hot(path, **kw):
        if path == "servo/set_many" and kw["targets"].get("8") == 500:
            raise Refused("The left shoulder is too hot to move.")
        return real(path, **kw)

    monkeypatch.setattr(parts["gateway"], "call", hot)
    client.post("/api/mirror/start")
    cam.pulses = {**REST, "8": 500}
    assert wait_for(lambda: not parts["mirror"].running, 3)
    assert "too hot" in client.get("/api/mirror").json()["error"]
