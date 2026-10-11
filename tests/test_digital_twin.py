"""Digital twin: servo calibration against the recorded poses, kinematics, HTTP API, robot mirror."""
import json
import math
import threading
import urllib.request

import pytest

from digital_twin import TonyPiDigitalTwin, TwinServer, TwinState, mapping
from digital_twin.robot_link import RobotMirror, read_status
from digital_twin.urdf import Robot

POSES_PATH = mapping.default_poses_path()


@pytest.fixture(scope="module")
def state():
    return TwinState()


def hand_tip(links, side):
    """Fingertip-ish point: forearm origin (the elbow) plus ~16 cm down the forearm."""
    rot, elbow = links[f"{side}_forearm"]
    return tuple(elbow[i] + rot[i][2] * -0.16 for i in range(3))


def angles_of(state, pose):
    return mapping.pulses_to_angles(state.poses[pose])


# ---------------------------------------------------------------- calibration
def test_tables_match_robot_api():
    from robot_api.servo.joints import JOINTS, STAND_POSE
    from robot_api.servo.limits import LIMITS

    assert mapping.STAND_POSE == STAND_POSE
    assert mapping.LIMITS == LIMITS
    for label, sid in JOINTS.items():
        if sid <= 16:
            assert mapping.BY_LABEL[label].servo == sid


def test_pulse_angle_round_trip():
    for sj in mapping.SERVO_JOINTS.values():
        for pulse in (sj.zero, sj.zero + 100, sj.zero - 100):
            assert abs(sj.to_pulse(sj.to_angle(pulse)) - pulse) <= 1


def test_right_side_mirrors_left():
    for lname, rname in (("knee", "knee"), ("hip_pitch", "hip_pitch"), ("ankle_pitch", "ankle_pitch"),
                         ("shoulder_pitch", "shoulder_pitch"), ("shoulder_roll", "shoulder_roll"), ("elbow", "elbow")):
        left, right = mapping.BY_JOINT["l_" + lname], mapping.BY_JOINT["r_" + rname]
        # mirrored servos: pulse_R = 1000 - pulse_L is the same motion, so angle(L) = +-angle(R) by joint type
        mirror_pulse = 1000 - (left.zero + 100)
        a_l, a_r = left.to_angle(left.zero + 100), right.to_angle(mirror_pulse)
        expected = -a_l if "roll" in lname else a_l
        assert a_r == pytest.approx(expected, abs=0.5), (lname, a_l, a_r)


def test_recorded_poses_load_and_are_in_servo_limits(state):
    assert {"default_standing_pose", "t_pose", "zombie_arms_forward"} <= set(state.poses)
    for name, servos in state.poses.items():
        assert sorted(servos) == list(range(1, 19)), name
        for sid in mapping.BODY_IDS:
            lo, hi = mapping.LIMITS[sid]
            assert lo - 2 <= servos[sid] <= hi + 2, (name, sid, servos[sid])  # recorded readback jitters by ~2


def test_standing_pose_is_zero(state):
    for joint, angle in angles_of(state, "default_standing_pose").items():
        assert abs(angle) < 1.5, (joint, angle)


def test_t_pose_raises_both_arms_sideways(state):
    a = angles_of(state, "t_pose")
    assert a["l_shoulder_roll"] == pytest.approx(75, abs=2) and a["r_shoulder_roll"] == pytest.approx(-75, abs=2)
    links = state.robot.fk(a)
    for side, y_sign in (("l", 1), ("r", -1)):
        tip = hand_tip(links, side)
        assert tip[1] * y_sign > 0.2  # hand far out to its own side
        assert abs(tip[2] - 0.04) < 0.09  # about shoulder height (the recorded arms sag ~15 deg)


def test_zombie_arms_point_forward(state):
    links = state.robot.fk(angles_of(state, "zombie_arms_forward"))
    for side in "lr":
        x, _, z = hand_tip(links, side)
        assert x > 0.15 and abs(z - 0.04) < 0.06


def test_scorpion_leans_back_and_stands_on_the_floor(state):
    a = angles_of(state, "scorpion_pose_bending_backwards")
    links = state.robot.fk(a)
    base = state.robot.ground_contact(links)
    assert base["pitch_deg"] < -50  # torso leaning backwards
    assert base["pos"][2] > 0.05


def test_standing_base_height_matches_viewer_constant(state):
    base = state.robot.ground_contact(state.robot.fk(angles_of(state, "default_standing_pose")))
    assert base["pos"][2] == pytest.approx(0.251, abs=0.004)  # STAND_HEIGHT in web/twin.js
    assert abs(base["pitch_deg"]) < 1.5


# ---------------------------------------------------------------- kinematics
def test_urdf_structure():
    robot = Robot(TwinState().urdf_path)
    movable = set(robot.movable_joints)
    for sj in mapping.SERVO_JOINTS.values():
        assert sj.joint in movable, sj.joint
    assert {"neck_pan", "neck_pitch"} <= movable
    assert robot.root == "torso"


def test_fk_elbow_bends_forearm_forward(state):
    # left elbow servo up from stand = forearm swings forward (+x), like a bicep curl
    pulses = dict(mapping.STAND_POSE)
    pulses[6] += 250
    links = state.robot.fk(mapping.pulses_to_angles(pulses))
    assert hand_tip(links, "l")[0] > 0.05  # forearm points forward, not straight down


# ---------------------------------------------------------------- state / limits
def test_command_is_clamped_to_servo_limits():
    s = TwinState()
    clamped = s.set_command({6: 5000, "left_knee": -50})
    assert set(clamped) == {6, 3}
    assert s.command.servos[6] == mapping.LIMITS[6][1] and s.command.servos[3] == mapping.LIMITS[3][0]


def test_unknown_servo_rejected():
    with pytest.raises(KeyError):
        TwinState().set_command({"banana": 1})
    with pytest.raises(KeyError):
        TwinState().set_command({19: 1})


def test_auto_view_prefers_fresh_robot_readback():
    s = TwinState()
    assert s.snapshot()["display"] == "command"
    s.set_actual(dict(mapping.STAND_POSE), temps={1: 40})
    snap = s.snapshot()
    assert snap["display"] == "actual" and snap["delta"]["8"] == 0
    s.set_view("command")
    assert s.snapshot()["display"] == "command"


# ---------------------------------------------------------------- http server
@pytest.fixture()
def server():
    srv = TwinServer(TwinState(), port=0).start()
    yield srv
    srv.stop()


def http(srv, path, body=None):
    req = urllib.request.Request(srv.url + path, data=None if body is None else json.dumps(body).encode(),
                                 method="GET" if body is None else "POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_http_model_poses_state(server):
    code, raw = http(server, "/api/model")
    model = json.loads(raw)
    assert code == 200 and model["root"] == "torso" and len(model["servos"]) == 18
    assert len(json.loads(http(server, "/api/poses")[1])["poses"]) >= 6
    snap = json.loads(http(server, "/api/state")[1])
    assert snap["channels"]["command"]["servos"]["8"] == 725


def test_http_pose_command_and_errors(server):
    assert http(server, "/api/pose", {"name": "t_pose"})[0] == 200
    snap = json.loads(http(server, "/api/state")[1])
    assert snap["channels"]["command"]["label"] == "t_pose" and snap["channels"]["command"]["servos"]["7"] < 500
    code, raw = http(server, "/api/command", {"servos": {"left_elbow": 700}, "source": "test"})
    assert code == 200 and json.loads(http(server, "/api/state")[1])["channels"]["command"]["servos"]["6"] == 700
    assert http(server, "/api/pose", {"name": "nope"})[0] == 404
    assert http(server, "/api/command", {})[0] == 400
    assert http(server, "/api/command", {"servos": {"x": 1}})[0] == 400
    assert http(server, "/api/view", {"view": "sideways"})[0] == 400
    assert http(server, "/nothing")[0] == 404


def test_static_files_and_no_path_traversal(server):
    assert http(server, "/web/twin.js")[0] == 200
    assert http(server, "/web/index.html")[0] == 200
    assert http(server, "/web/../state.py")[0] == 404
    assert http(server, "/web/%2e%2e/state.py")[0] == 404


def test_sse_stream_pushes_updates(server):
    req = urllib.request.urlopen(server.url + "/api/stream", timeout=5)
    first = req.readline()
    assert first.startswith(b"data: ")
    req.readline()
    server.state.set_command({8: 400}, source="test")
    seen = False
    for _ in range(20):
        line = req.readline()
        if line.startswith(b"data: ") and json.loads(line[6:])["channels"]["command"]["servos"]["8"] == 400:
            seen = True
            break
    req.close()
    assert seen


def test_remote_bind_disables_robot_url_config():
    srv = TwinServer(TwinState(), host="0.0.0.0", port=0).start()
    try:
        port_url = f"http://127.0.0.1:{srv.port}"
        req = urllib.request.Request(port_url + "/api/robot", data=b'{"url": "http://example.invalid"}', method="POST")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        assert e.value.code == 403
    finally:
        srv.stop()


# ---------------------------------------------------------------- robot mirror
class FakeBackend:
    """Same call(path) interface as robot_api's backends, backed by robot_api's dry-run fake."""

    def __init__(self):
        self.url = "fake"

    def call(self, path, **kw):
        from robot_api import api
        return api.call(path, **kw)


def test_mirror_reads_servo_status_into_actual_channel(fake_robot):
    fake_robot.positions[8] = 310  # robot raised its left arm
    fake_robot.temps[3] = 71
    s = TwinState()
    mirror = RobotMirror(s, FakeBackend())
    assert mirror.poll_once()
    snap = s.snapshot()
    act = snap["channels"]["actual"]
    assert act["servos"]["8"] == 310 and act["temps"]["3"] == 71
    assert snap["robot"]["connected"] and snap["display"] == "actual"
    assert snap["delta"]["8"] == 310 - 725  # robot vs. still-standing command


def test_clone_copies_robot_pose_to_command(fake_robot):
    fake_robot.positions.update({7: 500, 8: 400})
    s = TwinState()
    assert RobotMirror(s, FakeBackend()).clone()
    assert s.command.servos[7] == 500 and s.command.source == "robot-clone"


def test_mirror_survives_unreachable_robot():
    class Down:
        url = "down"

        def call(self, path, **kw):
            raise RuntimeError("robot API unreachable")

    s = TwinState()
    assert RobotMirror(s, Down()).poll_once() is False
    link = s.snapshot()["robot"]
    assert link["enabled"] and not link["connected"] and "unreachable" in link["error"]


def test_read_status_skips_servos_that_do_not_answer():
    class Partial:
        def call(self, path):
            return {"servos": [{"id": 1, "position": 500, "temp_c": 40}, {"id": 2, "position": None}], "limp": []}

    out = read_status(Partial())
    assert out["servos"] == {1: 500} and out["temps"] == {1: 40}


# ---------------------------------------------------------------- estimator facade
def test_facade_arm_and_leg_updates_map_to_pulses():
    twin = TonyPiDigitalTwin(gui=False, headless=True, port=0)
    try:
        twin.start()
        twin.update_arms(l_pitch_deg=-90, l_roll_deg=0, l_elbow_deg=180, r_pitch_deg=-90, r_roll_deg=0, r_elbow_deg=180)
        assert twin.state.command.servos[8] == pytest.approx(350, abs=1) and twin.state.command.servos[16] == pytest.approx(650, abs=1)
        twin.update_legs(l_knee_deg=120, l_hip_pitch_deg=30, l_ankle_deg=30, r_knee_deg=120, r_hip_pitch_deg=30, r_ankle_deg=30)
        # symmetric squat: mirrored servos sum to ~1000
        for left, right in ((3, 11), (4, 12), (2, 10)):
            assert twin.state.command.servos[left] + twin.state.command.servos[right] == pytest.approx(1000, abs=3)
        rot = twin.get_link_rotation("l_thigh")
        assert rot[0][2] != 0  # thigh is tilted
        assert twin.is_window_open()
    finally:
        twin.close()
    assert twin.is_window_closed()


def test_two_twins_get_different_ports():
    a, b = TonyPiDigitalTwin(headless=True, port=8899), TonyPiDigitalTwin(headless=True, port=8899)
    try:
        assert a.server.port != b.server.port
    finally:
        a.close()
        b.close()


# ---------------------------------------------------------------- FastAPI mount (optional dependency)
def test_fastapi_mount():
    pytest.importorskip("fastapi")
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from digital_twin.fastapi_router import mount_twin

    app = FastAPI()
    api = mount_twin(app, prefix="/twin")
    client = TestClient(app)
    assert client.get("/twin/api/model").json()["root"] == "torso"
    assert client.post("/twin/api/pose", json={"name": "t_pose"}).status_code == 200
    assert client.get("/twin/api/state").json()["channels"]["command"]["label"] == "t_pose"
    assert client.post("/twin/api/pose", json={"name": "nope"}).status_code == 404
    assert client.post("/twin/api/robot", json={"url": "http://x"}).status_code == 403  # config is server-side only
    assert client.get("/twin/web/twin.js").status_code == 200
    assert api.state.command.label == "t_pose"
