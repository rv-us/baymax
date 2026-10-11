"""A session on the real robot: its camera is tracked on the GX10, the Live screen sees the tracker's
output, and the robot keeps a quarter of a rep ahead at the patient's own pace."""
import math
import time

import pytest

from dashboard import live as live_module
from dashboard import synthetic
from dashboard import webcam as webcam_module
from dashboard.armtrack import person_box
from dashboard.webcam import scale_overlay
from test_dashboard_api import make, tiny_plan, wait_for

import numpy as np


class RobotFeed:
    """Stands in for dashboard.webcam.Webcam on the robot's camera, tracked on the GX10: a patient
    doing one 1 s rep after another, with the people-in-view and green-arm answers the tracker gives."""
    opened = []
    fail = None            # a reason start() fails with

    def __init__(self, robot_url=None, pose_url=None, on_pose=None, **_):
        self.robot_url, self.pose_url, self.on_pose = robot_url, pose_url, on_pose
        self.label, self.source, self.transport, self.delivery, self.ms = "Robot camera", "gx10", "udp", 0.97, 31
        self.tracking = True
        self.status = {"state": "ok", "detail": "Robot camera · pose on the GX10"}
        self.t0, self.stopped, self.visible = time.time(), False, True
        RobotFeed.opened.append(self)

    def start(self):
        if RobotFeed.fail:
            self.status = {"state": "down", "detail": RobotFeed.fail}
            return False
        return True

    def pose(self):
        angle = 15 + 75 * (0.5 - 0.5 * math.cos(2 * math.pi * (time.time() - self.t0)))
        arms = {"L": "TRACKING", "R": "TRACKING"}
        pose = {"visible": self.visible, "x": 0.1, "size": 0.6, "t": time.time(), "arms": arms, "people": 2,
                "green": {"L": True, "R": True}, "calibrated": True, "pulses": {},
                "angles": {"shoulder_elevation": angle, "elbow": 12.0}}
        if self.on_pose:
            self.on_pose(pose)
        return pose

    def jpeg(self, width=640, quality=70):
        return b"ROBOTFEED"

    def stop(self):
        self.stopped = True


@pytest.fixture
def robot_dash(tmp_path, fake_robot, monkeypatch):
    """The dashboard as if it were on a robot (not a dry run), with the camera faked."""
    RobotFeed.opened, RobotFeed.fail = [], None
    monkeypatch.setattr(webcam_module, "Webcam", RobotFeed)
    client, parts = make(tmp_path)
    tiny_plan(tmp_path)
    parts["settings"].dry_run = False
    parts["settings"].pose_url = "http://gx10:9100"
    return client, parts


def test_a_session_on_the_robot_watches_through_its_camera_and_logs_real_data(robot_dash):
    client, parts = robot_dash
    r = client.post("/api/live/start", json={"plan": "tiny", "simulate": True, "pace": 8})
    assert r.status_code == 200 and r.json()["synthetic_tracker"] is False and r.json()["pace"] == 1.0
    (feed,) = RobotFeed.opened
    assert feed.robot_url == parts["settings"].robot_url and feed.pose_url == "http://gx10:9100"
    assert parts["gateway"].feed is feed
    assert client.get("/api/camera.jpg").content == b"ROBOTFEED"         # the picture has the tracking drawn on it
    assert wait_for(lambda: (parts["live"].state.get("tracking") or {}).get("on") == "gx10", 5)
    t = parts["live"].state["tracking"]
    assert t["people"] == 2 and t["green"] == {"L": True, "R": True} and t["angles"]["elbow"] == 12
    assert wait_for(lambda: not parts["live"].running, 40)
    assert parts["live"].state["last"]["status"] == "completed", parts["live"].state
    assert feed.stopped and parts["gateway"].feed is None                 # the camera is let go
    s = client.get(f"/api/sessions/{parts['live'].state['last']['id']}").json()
    assert s["synthetic"] is False and s["source"] == "live-webcam"


def test_without_a_camera_the_patient_is_simulated_and_the_reason_is_shown(robot_dash):
    client, parts = robot_dash
    RobotFeed.fail = "The robot's camera isn't available: nothing is streaming."
    r = client.post("/api/live/start", json={"plan": "tiny", "simulate": True, "pace": 8, "seed": 1})
    assert r.status_code == 200
    assert r.json()["synthetic_tracker"] is True and "isn't available" in r.json()["tracker_note"]
    assert parts["gateway"].feed is None
    assert wait_for(lambda: not parts["live"].running, 40)
    s = client.get(f"/api/sessions/{parts['live'].state['last']['id']}").json()
    assert s["synthetic"] is True


def test_the_camera_is_let_go_when_a_session_is_stopped(robot_dash):
    client, parts = robot_dash
    client.post("/api/live/start", json={"plan": "tiny", "simulate": True})
    (feed,) = RobotFeed.opened
    client.post("/api/live/stop")
    assert wait_for(lambda: not parts["live"].running, 20)
    assert feed.stopped and parts["gateway"].feed is None


def test_the_robots_lead_keeps_to_the_patients_pace_when_moving_the_robot_is_slow(robot_dash, monkeypatch):
    """Each move of the robot's arms takes time; the lead must still advance at one sample per 1/HZ."""
    client, parts = robot_dash
    real = parts["gateway"].call

    def slow(path, **kwargs):
        if path == "servo/set_many":
            time.sleep(0.09)
        return real(path, **kwargs)

    monkeypatch.setattr(parts["gateway"], "call", slow)
    ran = []
    original = live_module.WebcamTracker.run_set

    def timed(self, *args, **kwargs):
        began = time.monotonic()
        result = original(self, *args, **kwargs)
        ran.append((time.monotonic() - began, len(result.trace["patient"])))
        return result

    monkeypatch.setattr(live_module.WebcamTracker, "run_set", timed)
    client.post("/api/live/start", json={"plan": "tiny", "simulate": True})
    assert wait_for(lambda: not parts["live"].running, 40)
    seconds, samples = ran[0]
    assert abs(seconds - samples / synthetic.HZ) < 0.15 * seconds


def test_the_tracked_person_is_boxed_and_the_box_follows_a_resized_picture():
    px = np.array([[100.0, 50.0], [300.0, 400.0], [700.0, 10.0], [-5.0, 20.0]])
    vis = [0.9, 0.9, 0.1, 0.9]           # the third can't be seen; the fourth is partly out of the frame
    assert person_box(px, vis, (640, 480)) == [0.0, 20.0, 300.0, 400.0]
    assert person_box(px, [0.0] * 4, (640, 480)) is None
    overlay = {"shoulders": [[1, 2], [3, 4]], "arms": {"L": {"pts": [[1, 1]], "state": "TRACKING"}},
               "person": [10, 20, 30, 40]}
    assert scale_overlay(overlay, 2.0)["person"] == [20, 40, 60, 80]
    assert scale_overlay(overlay, 1.0) is overlay
    assert scale_overlay({"shoulders": None, "arms": {}}, 2.0) == {"shoulders": None, "arms": {}}
