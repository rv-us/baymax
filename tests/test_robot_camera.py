import socket
import threading
import time

import pytest

from robot_api import config
from streaming import robot_camera
from streaming.robot_camera import MjpegReader, RobotCamera

cv2 = pytest.importorskip("cv2")


@pytest.fixture
def api(monkeypatch):
    """The robot API (dry run, fake camera) on a real port, with a token."""
    uvicorn = pytest.importorskip("uvicorn")
    from robot_api.server import app

    monkeypatch.setattr(config, "API_TOKEN", "tok")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while not server.started and time.time() < deadline:
        time.sleep(0.02)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(timeout=5)


def wait_frame(source, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        frame, t = source.latest()
        if frame is not None:
            return frame, t
        time.sleep(0.02)
    return None, 0.0


def test_mjpeg_reader_decodes_frames_with_header_token(api):
    reader = MjpegReader(api, token="tok").start()
    frame, t = wait_frame(reader)
    assert frame is not None and frame.shape == (48, 64, 3)  # the dry-run test image
    time.sleep(0.3)
    assert reader.latest()[1] > t  # keeps receiving
    reader.stop()


def test_mjpeg_reader_without_the_token_gets_nothing(api):
    reader = MjpegReader(api, token=None).start()
    assert wait_frame(reader, timeout=1.0)[0] is None
    reader.stop()


def test_auto_falls_back_to_mjpeg_without_gstreamer(api, monkeypatch):
    monkeypatch.setattr(robot_camera, "udp_receive_possible", lambda: False)
    cam = RobotCamera(api, token="tok").start()
    assert cam.mode == "mjpeg" and wait_frame(cam)[0] is not None
    cam.stop()


def test_auto_falls_back_when_the_port_is_taken_here(api, monkeypatch):
    monkeypatch.setattr(robot_camera, "udp_receive_possible", lambda: True)
    taken = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    taken.bind(("0.0.0.0", 0))
    try:
        cam = RobotCamera(api, token="tok", port=taken.getsockname()[1]).start()
        assert cam.mode == "mjpeg" and cam.lease is None  # never asked the robot for UDP
        cam.stop()
    finally:
        taken.close()


def test_auto_falls_back_when_the_robot_streams_elsewhere(api, monkeypatch):
    from robot_api.sensors import camera_udp

    monkeypatch.setattr(robot_camera, "udp_receive_possible", lambda: True)
    camera_udp.start("10.9.9.9", 5000)  # another machine already has the UDP stream
    try:
        cam = RobotCamera(api, token="tok", port=5077).start()
        assert cam.mode == "mjpeg"
        cam.stop()
    finally:
        camera_udp.stop()


def test_udp_mode_requests_the_stream_and_stops_it(api, monkeypatch):
    from robot_api.sensors import camera_udp

    class FakeReceiver:  # stands in for FrameReceiver, which needs gst-launch
        def __init__(self, port):
            self.port = port

        def start(self):
            return self

        def latest(self):
            return None, 0.0

        def stop(self):
            pass

    import streaming.receiver

    monkeypatch.setattr(robot_camera, "udp_receive_possible", lambda: True)
    monkeypatch.setattr(streaming.receiver, "FrameReceiver", FakeReceiver)
    cam = RobotCamera(api, token="tok", mode="udp", port=5078).start()
    assert cam.mode == "udp"
    st = camera_udp.status()
    assert st["running"] and st["host"] == "127.0.0.1" and st["port"] == 5078
    cam.stop()
    assert not camera_udp.status()["running"]


def test_udp_mode_says_why_it_cant(monkeypatch):
    monkeypatch.setattr(robot_camera, "udp_receive_possible", lambda: False)
    with pytest.raises(RuntimeError, match="no GStreamer"):
        RobotCamera("http://robot:8000", mode="udp").start()
