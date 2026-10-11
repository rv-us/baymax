from fastapi.testclient import TestClient

from robot_api.server import app

client = TestClient(app)


def test_post_and_get():
    assert client.get("/sensors/distance").json()["distance_mm"] == 800
    r = client.post("/actions/arm", json={"side": "left", "position": "up"})
    assert r.status_code == 200 and r.json()["moved"] == {"left_shoulder_pitch": 125}


def test_refusal_is_409_and_bad_args_422(fake_robot):
    fake_robot.temps[2] = 90
    assert client.post("/actions/stand", json={}).status_code == 409
    assert client.post("/servo/set", json={"nope": 1}).status_code == 422


def test_http_backend_roundtrip(fake_robot, monkeypatch):
    from robot_api.client import HttpBackend
    from robot_api.errors import RobotError
    import pytest

    hb = HttpBackend("http://testserver")
    hb._http = client
    assert hb.call("sensors/climate")["humidity_pct"] == 45
    fake_robot.temps[5] = 85
    with pytest.raises(RobotError, match="HTTP 409"):
        hb.call("actions/turn", direction="right")


def test_camera_mjpg_stream(monkeypatch):
    import itertools

    from robot_api.sensors import camera

    real = camera.stream_jpegs  # TestClient can't cancel an endless stream, so cap it at 3 frames

    def capped(*a):
        yield from itertools.islice(real(*a), 3)

    monkeypatch.setattr(camera, "stream_jpegs", capped)
    with client.stream("GET", "/sensors/camera.mjpg?fps=30") as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("multipart/x-mixed-replace")
        data = b""
        for chunk in r.iter_bytes():
            data += chunk
            if data.count(b"--frame") >= 2:
                break
    assert b"Content-Type: image/jpeg" in data and b"\xff\xd8" in data


def test_token_by_header_or_query(monkeypatch):
    from robot_api import config

    monkeypatch.setattr(config, "API_TOKEN", "secret")
    assert client.get("/health").status_code == 401
    assert client.get("/health", headers={"Authorization": "Bearer secret"}).status_code == 200
    assert client.get("/health?token=secret").status_code == 200
    assert client.get("/health?token=wrong").status_code == 401


def test_stream_controls_applied_once_and_restored(monkeypatch):
    from robot_api import config
    from robot_api.sensors import camera

    calls = []
    monkeypatch.setattr(config, "DRY_RUN", False)
    monkeypatch.setattr(config, "STREAM_CAMERA_CONTROLS", "auto_exposure=1,gamma=150")
    monkeypatch.setattr(camera, "_v4l2", lambda *a: calls.append(a) or "auto_exposure: 3 (Aperture Priority Mode)\ngamma: 67\n")
    camera._stream_controls(True)
    camera._stream_controls(True)  # a second stream changes nothing
    camera._stream_controls(False)
    assert calls == [("-C", "auto_exposure,gamma"), ("-c", "auto_exposure=1,gamma=150")]
    camera._stream_controls(False)  # the last one restores, one control at a time
    assert calls[2:] == [("-c", "auto_exposure=3"), ("-c", "gamma=67")]


def test_servo_positions():
    out = client.post("/servo/positions", json={"servos": [6, "left_shoulder_roll", 8]}).json()["positions"]
    assert set(out) == {"6", "7", "8"} and all(isinstance(p, int) for p in out.values())
