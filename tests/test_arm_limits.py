import json
import os

from robot_api import api
from robot_api.servo import arm_limits
from robot_api.servo.joints import STAND_POSE

MAP = {
    "margin": 10,
    "elbow_straight": {"L": 500, "R": 500},
    "rows": {side: [
        {"pitch": 300, "points": [[0, 950]]},                      # arm forward: roll far in
        {"pitch": 700, "points": [[0, 870], [300, 700]]},          # arm down: elbow costs roll
    ] for side in "LR"},
}


def test_roll_limit_interpolates_pitch_and_elbow():
    assert arm_limits.roll_limit("L", 700, 500, MAP) == 860
    assert arm_limits.roll_limit("L", 700, 650, MAP) == 775          # halfway along the boundary
    assert arm_limits.roll_limit("L", 700, 900, MAP) == 590          # past the last point: 1 for 1
    assert arm_limits.roll_limit("L", 500, 500, MAP) == 900          # between the rows
    assert arm_limits.roll_limit("L", 100, 500, MAP) == 940          # past the first row
    assert arm_limits.roll_limit("R", 300, 500, MAP) == 1000 - 860   # right arm is mirrored


def test_limit_pulls_roll_out_and_leaves_safe_poses():
    safe = {8: 700, 7: 800, 6: 500}
    assert arm_limits.limit(safe, {}, MAP) == (safe, {})
    out, changed = arm_limits.limit({8: 700, 7: 900, 6: 650}, {}, MAP)
    assert out[7] == 775 and changed == {7: (900, 775)}
    out, _ = arm_limits.limit({16: 300, 15: 100, 14: 350}, {}, MAP)
    assert out[15] == 1000 - 775


def test_elbow_alone_moves_the_roll_out_of_the_way():
    out, changed = arm_limits.limit({6: 800}, {8: 700, 7: 860}, MAP)
    assert out == {6: 800, 7: 690} and changed == {7: (860, 690)}


def test_set_many_reports_arm_limited(fake_robot, monkeypatch):
    monkeypatch.setattr(arm_limits, "MAP", MAP)
    out = api.call("servo/set_many", targets={"8": 700, "7": 900, "6": 650})
    assert out["moved"]["left_shoulder_roll"] == 775
    assert out["arm_limited"]["left_shoulder_roll"] == {"requested": 900, "applied": 775}
    out = api.call("servo/set_many", targets={"16": STAND_POSE[16]})
    assert "arm_limited" not in out


def test_shipped_map_keeps_stand_and_captures_clear():
    path = os.path.join(os.path.dirname(__file__), "..", "scripts", "arm_limits.json")
    with open(path) as f:
        captures = json.load(f)
    stand = {sid: STAND_POSE[sid] for sid in (6, 7, 8, 14, 15, 16)}
    assert arm_limits.limit(stand, {})[1] == {}
    for name, cap in captures.items():
        if "_ref_" in name:
            continue
        ids = arm_limits.ARMS[cap["side"]]
        pose = dict(zip(ids, (cap["arm"]["pitch"], cap["arm"]["roll"], cap["arm"]["elbow"])))
        _, changed = arm_limits.limit(pose, {})
        assert ids[1] in changed, f"{name}: a captured contact must be outside the safe roll"
