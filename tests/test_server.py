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
    fake_robot.temps[5] = 71
    with pytest.raises(RobotError, match="HTTP 409"):
        hb.call("actions/turn", direction="right")
