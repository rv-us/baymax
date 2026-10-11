import os
import threading
import time

import pytest
from fastapi.testclient import TestClient

from robot_api import config
from robot_api.sensors import camera, camera_udp
from robot_api.server import app
from streaming.robot_camera import UdpVideoLease

client = TestClient(app)


@pytest.fixture(autouse=True)
def no_stream_left():
    yield
    camera_udp.stop()


def test_command_streams_h264_and_writes_snapshots():
    cmd = " ".join(camera_udp.build_command("10.0.0.5", 5000, 1500))
    assert "udpsink host=10.0.0.5 port=5000" in cmd and "bitrate=1500" in cmd
    assert f"multifilesink location={camera.SHARED_FRAME}" in cmd
    assert f"framerate={config.UDP_SNAPSHOT_FPS}/1" in cmd
    assert "rtph264pay config-interval=1 pt=96" in cmd  # what streaming/receiver.py expects


def test_streams_to_the_caller_and_renews(fake_robot):
    r = client.post("/sensors/camera/udp", json={"lease_s": 10})
    assert r.status_code == 200, r.text
    first = r.json()
    assert first["running"] and first["host"] == "testclient" and first["port"] == 5000
    time.sleep(0.2)
    again = client.post("/sensors/camera/udp", json={"lease_s": 10}).json()
    assert again["lease_left_s"] > first["lease_left_s"] - 0.2  # renewed, not restarted
    assert again["seconds"] >= 0.2
    assert ("camera_udp_start", "testclient", 5000, 1500) in fake_robot.log
    assert sum(1 for e in fake_robot.log if e[0] == "camera_udp_start") == 1


def test_explicit_host_and_one_target_at_a_time():
    assert client.post("/sensors/camera/udp", json={"host": "10.0.0.9", "port": 5100}).json()["host"] == "10.0.0.9"
    r = client.post("/sensors/camera/udp", json={"port": 5200})
    assert r.status_code == 409 and "already streaming to 10.0.0.9:5100" in r.json()["detail"]


def test_status_and_stop():
    client.post("/sensors/camera/udp", json={})
    assert client.get("/sensors/camera/udp/status").json()["running"]
    assert client.post("/sensors/camera/udp/stop").json() == {"stopped": True}
    assert not client.get("/sensors/camera/udp/status").json()["running"]
    assert client.post("/sensors/camera/udp/stop").json() == {"stopped": False}


def test_stops_by_itself_when_the_lease_runs_out():
    client.post("/sensors/camera/udp", json={"lease_s": 2})
    time.sleep(2.8)
    st = client.get("/sensors/camera/udp/status").json()
    assert not st["running"] and "lease expired" in st["last_error"]


def test_lease_client_keeps_it_alive_and_stops_it(monkeypatch):
    def post(self, path, body):
        r = client.post(path, json=body)
        assert r.status_code == 200, r.text
        return r.json()

    monkeypatch.setattr(UdpVideoLease, "_post", post)
    lease = UdpVideoLease("http://robot:8000", port=5300, lease_s=2, renew_every_s=0.3).start()
    time.sleep(2.8)  # past one lease: only renewals keep it running
    assert client.get("/sensors/camera/udp/status").json()["port"] == 5300
    assert lease.error is None
    lease.stop()
    assert not client.get("/sensors/camera/udp/status").json()["running"]


def test_mjpeg_sends_each_shared_frame_once(tmp_path, monkeypatch):
    """While the UDP pipeline owns the camera, the MJPEG stream reads its snapshot file:
    it should wait for a new file instead of resending the same frame."""
    cv2 = pytest.importorskip("cv2")
    import numpy as np

    shared = tmp_path / "cam.jpg"
    monkeypatch.setattr(camera, "SHARED_FRAME", str(shared))
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(camera, "_flip", None)
    ok, jpg = cv2.imencode(".jpg", np.zeros((48, 64, 3), np.uint8))
    shared.write_bytes(jpg.tobytes())
    frames = camera._stream(1 / 30, 160, 70)
    assert next(frames)
    got = threading.Event()
    threading.Thread(target=lambda: (next(frames), got.set()), daemon=True).start()
    assert not got.wait(0.2)  # same file: nothing new to send
    st = os.stat(shared)
    os.utime(shared, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000))  # a new frame arrives
    assert got.wait(1.0)
