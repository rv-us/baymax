import pytest

from robot_api import api
from robot_api.errors import RobotError
from robot_api.servo.joints import STAND_POSE


def moves(state):
    return [e for e in state.log if e[0] == "bus_servo_set_position"]


def test_set_servo_by_joint_name(fake_robot):
    out = api.call("servo/set", servo="left_shoulder_pitch", pulse=600)
    assert out["moved"] == {"left_shoulder_pitch": 600}
    assert moves(fake_robot)[-1][2] == [[8, 600]]


def test_out_of_range_pulse_is_clamped(fake_robot):
    out = api.call("servo/set", servo=9, pulse=900)  # right ankle roll, limit 366-570
    assert out["moved"] == {"right_ankle_roll": 570}
    assert out["clamped"]["right_ankle_roll"]["requested"] == 900


def test_move_time_scales_with_distance(fake_robot):
    out = api.call("servo/set", servo=8, pulse=STAND_POSE[8] - 700, ms=100)
    assert out["ms"] >= 700


def test_hot_servo_refuses_motion(fake_robot):
    fake_robot.temps[3] = 75
    with pytest.raises(RobotError, match="servo 3"):
        api.call("actions/walk", direction="forward", steps=1)
    assert not any(e[0] == "runActionGroup" for e in fake_robot.log)


def test_unreadable_temps_refuse_motion(fake_robot):
    fake_robot.temps = {}
    with pytest.raises(RobotError, match="could not read"):
        api.call("actions/arm", side="left", position="up")


def test_coast_allowed_even_when_hot(fake_robot):
    fake_robot.temps[3] = 80
    api.call("servo/coast")
    assert fake_robot.torque[3] == 0


def test_walk_forward_plays_start_cycles_end(fake_robot):
    api.call("actions/walk", direction="forward", steps=2)
    assert ("runActionGroup", "go_forward", 3, True) in fake_robot.log


def test_step_cap(fake_robot):
    with pytest.raises(RobotError, match="1-5"):
        api.call("actions/turn", direction="left", steps=6)


def test_risky_group_needs_flag(fake_robot):
    with pytest.raises(RobotError):
        api.call("actions/run", name="lie_down")
    api.call("actions/run", name="wave")
    assert ("runActionGroup", "wave", 1, True) in fake_robot.log


def test_gripper_and_arm(fake_robot):
    api.call("actions/gripper", side="both", action="open")
    assert sorted(moves(fake_robot)[-1][2]) == [[17, 760], [18, 240]]
    api.call("actions/arm", side="right", position="forward")
    assert moves(fake_robot)[-1][2] == [[16, 650]]


def test_unknown_joint_and_bad_args():
    with pytest.raises(RobotError, match="unknown joint"):
        api.call("servo/set", servo="tail", pulse=500)
    with pytest.raises(RobotError, match="servo/set"):
        api.call("servo/set", servo=1)


def test_sensors_dry_run():
    assert api.call("sensors/distance")["distance_mm"] == 800
    assert api.call("sensors/climate")["temp_c"] == 22.5
    assert api.call("sensors/battery")["battery_v"] == 12.4
    assert api.call("sensors/matrix/text", text="HI")["scrolling"] is False
    assert api.call("sensors/matrix/icon", name="heart")


def test_dot_matrix_font_columns(fake_robot):
    api.call("sensors/matrix/text", text="I")
    cols = [e for e in fake_robot.log if e[0] == "dot_matrix"][-1][1]
    assert 0x7F in cols and len(cols) == 16
