import importlib.util
import math
import os
import time

import numpy as np
import pytest

from vision import angles as A
from vision.stream import PoseStream


def body(arm="down", elbow_bend=0.0, knee_bend=0.0, lean_deg=0.0, hip_shift=0.0, vis=1.0):
    """A person facing the camera, in world meters (x = patient's left, y = up, z = toward
    the camera), plus matching image landmarks. Returns (landmarks, world)."""
    w = np.zeros((33, 3))
    w[A.L_HIP], w[A.R_HIP] = [0.1 + hip_shift, 0, 0], [-0.1 + hip_shift, 0, 0]
    w[A.L_KNEE], w[A.R_KNEE] = [0.1, -0.45, 0], [-0.1, -0.45, 0]
    w[A.L_ANKLE], w[A.R_ANKLE] = [0.1, -0.9, 0], [-0.1, -0.9, 0]
    if knee_bend:  # shins swing back
        b = math.radians(knee_bend)
        for k, a in ((A.L_KNEE, A.L_ANKLE), (A.R_KNEE, A.R_ANKLE)):
            w[a] = w[k] + [0, -0.45 * math.cos(b), -0.45 * math.sin(b)]
    w[A.L_SHOULDER], w[A.R_SHOULDER] = [0.18, 0.5, 0], [-0.18, 0.5, 0]
    upper = {"down": [0, -0.3, 0], "forward": [0, 0, 0.3], "up": [0, 0.3, 0],
             "side": [0.3, 0, 0]}[arm]
    for s, e, wr, sign in ((A.L_SHOULDER, A.L_ELBOW, A.L_WRIST, 1),
                           (A.R_SHOULDER, A.R_ELBOW, A.R_WRIST, -1)):
        u = np.array(upper) * [sign, 1, 1]
        w[e] = w[s] + u
        # Bend the forearm by elbow_bend degrees, toward the camera (or up for a forward arm).
        b = math.radians(elbow_bend)
        perp = np.array([0, 1.0, 0]) if arm == "forward" else np.array([0, 0, 1.0])
        w[wr] = w[e] + 0.25 * (math.cos(b) * u / np.linalg.norm(u) + math.sin(b) * perp)
    # Image landmarks: front view, 640x480, upright unless leaning.
    lm = np.zeros((33, 4))
    for i in range(33):
        lm[i] = [0.5 - w[i, 0] / 2.5, 0.45 - w[i, 1] / 2.5, 0, vis]
    if lean_deg:  # tilt the shoulders sideways around the hips, in pixels
        mid_hip = (lm[A.L_HIP, :2] + lm[A.R_HIP, :2]) / 2 * [640, 480]
        for i in (A.L_SHOULDER, A.R_SHOULDER):
            p = lm[i, :2] * [640, 480] - mid_hip
            r = math.radians(lean_deg)
            rot = np.array([[math.cos(r), -math.sin(r)], [math.sin(r), math.cos(r)]])
            lm[i, :2] = (mid_hip + rot @ p) / [640, 480]
    return lm.tolist(), w.tolist()


@pytest.mark.parametrize("arm, elevation", [("down", 0), ("forward", 90), ("side", 90), ("up", 180)])
def test_shoulder_elevation(arm, elevation):
    a = A.joint_angles(*body(arm=arm))
    assert a["left_shoulder_elevation"] == pytest.approx(elevation, abs=1)
    assert a["right_shoulder_elevation"] == pytest.approx(elevation, abs=1)


@pytest.mark.parametrize("bend", [0, 30, 90])
def test_elbow_is_interior_angle(bend):
    a = A.joint_angles(*body(arm="forward", elbow_bend=bend))
    assert a["left_elbow"] == pytest.approx(180 - bend, abs=1)


def test_knee_trunk_and_hips():
    assert A.joint_angles(*body(knee_bend=40))["left_knee"] == pytest.approx(140, abs=1)
    assert A.joint_angles(*body(lean_deg=15))["trunk_lean"] == pytest.approx(15, abs=1)
    a = A.joint_angles(*body(hip_shift=0.05))
    assert a["hip_shift_cm"] == pytest.approx(5, abs=0.5)  # + = toward the patient's left
    upright = A.joint_angles(*body())
    assert upright["trunk_lean"] == pytest.approx(0, abs=0.5)
    assert upright["hip_shift_cm"] == pytest.approx(0, abs=0.5)


def test_hidden_landmarks_give_none():
    lm, w = body(arm="forward")
    lm[A.L_WRIST][3] = 0.1
    a = A.joint_angles(lm, w)
    assert a["left_elbow"] is None and a["right_elbow"] is not None
    assert a["left_shoulder_elevation"] is not None  # doesn't need the wrist


def test_in_view():
    lm, _ = body()
    assert A.in_view(lm) == {"upper_body": True, "full_body": True, "missing": []}
    lm[A.L_ANKLE][1] = 1.2  # below the bottom of the image
    lm[A.R_ANKLE][3] = 0.2  # not visible
    v = A.in_view(lm)
    assert v["upper_body"] and not v["full_body"]
    assert v["missing"] == ["left ankle", "right ankle"]
    lm[A.R_WRIST][0] = 0.0  # at the edge
    assert not A.in_view(lm)["upper_body"]


# PoseStream with a fake receiver and detector

class FakeReceiver:
    def __init__(self):
        self.frame, self.ts = None, 0.0

    def push(self):
        self.frame, self.ts = np.zeros((480, 640, 3), np.uint8), time.time()

    def latest(self):
        return self.frame, self.ts


class FakeDetector:
    def __init__(self, found=True):
        self.calls, self.found = 0, found

    def detect(self, frame):
        self.calls += 1
        if not self.found:
            return {"found": False, "width": 640, "height": 480, "infer_ms": 1.0}
        lm, w = body(arm="up")
        return {"found": True, "width": 640, "height": 480, "landmarks": lm, "world": w,
                "infer_ms": 1.0}


def wait_for(cond, timeout=2.0):
    end = time.time() + timeout
    while not cond() and time.time() < end:
        time.sleep(0.005)
    return cond()


def test_stream_processes_each_new_frame_once():
    rx, det = FakeReceiver(), FakeDetector()
    s = PoseStream(rx, det).start()
    assert s.latest() is None
    rx.push()
    assert wait_for(lambda: s.latest() is not None)
    time.sleep(0.05)
    assert det.calls == 1  # the same frame isn't processed twice
    p = s.latest()
    assert p["angles"]["left_shoulder_elevation"] == pytest.approx(180, abs=1)
    assert p["in_view"]["full_body"] and p["seq"] == 1 and p["latency_ms"] >= 0
    for _ in range(5):
        time.sleep(0.01)
        rx.push()
    assert wait_for(lambda: s.processed == 6)
    st = s.stats()
    assert st["fps"] > 0 and st["person_found"] and st["error"] is None
    s.stop()


def test_stream_without_a_person():
    rx = FakeReceiver()
    s = PoseStream(rx, FakeDetector(found=False)).start()
    rx.push()
    assert wait_for(lambda: s.latest() is not None)
    assert s.latest()["angles"] == {} and not s.latest()["in_view"]["upper_body"]
    s.stop()


def test_server_endpoints():
    pytest.importorskip("cv2")
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    path = os.path.join(os.path.dirname(__file__), "..", "scripts", "pose_server.py")
    spec = importlib.util.spec_from_file_location("pose_server", path)
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)

    class FakePose(FakeDetector):
        model_path, frames = "fake.task", 0

    rx = FakeReceiver()
    stream = PoseStream(rx, FakePose())
    client = TestClient(server.make_app(FakePose(), stream))
    assert client.get("/latest").json() == {"found": False, "waiting": True}
    stream.start()
    rx.push()
    assert wait_for(lambda: stream.latest() is not None)
    got = client.get("/latest").json()
    assert got["found"] and got["angles"]["right_shoulder_elevation"] == pytest.approx(180, abs=1)
    assert client.get("/stats").json()["processed"] == 1
    assert client.get("/health").json()["stream"]["processed"] == 1
    stream.stop()
    assert TestClient(server.make_app(FakePose())).get("/latest").status_code == 404
