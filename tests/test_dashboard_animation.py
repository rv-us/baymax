"""Keyframe animations: validation in plan.py, playback, and the plan round trip."""
import pytest
import yaml

from dashboard import store
from robot_api.api import LocalBackend
from workout import keyframes
from workout.plan import PlanError, check_keyframes, load_plan, parse_plan
from workout.robot import Robot

RAISE = [
    {"ms": 1000, "ease": "linear", "servos": {"left_shoulder_pitch": 325, "right_shoulder_pitch": 675}},
    {"ms": 1000, "hold_ms": 600, "ease": "inout", "servos": {"left_shoulder_pitch": 725, "right_shoulder_pitch": 275}},
]
ANIM = {"name": "Slow arm raise", "keyframes": RAISE,
        "exercise": {"cue": "Lift both arms, then lower them.", "sets": 2, "reps": 5,
                     "targets": {"shoulder_elevation": {"top": 90, "bottom": 15, "tolerance": 15}}}}


@pytest.mark.parametrize("frames, message", [
    ([], "1 to 60"),
    ([{"ms": 1000, "servos": {"left_knee": 500}}], "not an arm joint"),
    ([{"ms": 1000, "servos": {"left_elbow": 990}}], "left_elbow must be a whole pulse from 125 to 875"),
    ([{"ms": 100, "servos": {"left_elbow": 500}}], "ms must be"),
    ([{"ms": 1000, "servos": {"left_elbow": 500}, "ease": "bounce"}], "ease"),
    ([{"ms": 1000}], "needs 'servos'"),
    ([{"ms": 1000, "head": {"pan": 5000}}], "head pan"),
    ([{"ms": 1000, "head": {"roll": 1500}}], "pan or tilt"),
])
def test_bad_keyframes_are_rejected(frames, message):
    with pytest.raises(PlanError, match=message):
        check_keyframes(frames)


def test_plan_accepts_a_keyframe_demo_and_only_one_demo_kind():
    ex = {"name": "X", "cue": "c", "sets": 1, "reps": 1, "track_joint": "elbow", "tempo_s": 3}
    plan = parse_plan({"exercises": [{**ex, "demo": {"keyframes": RAISE}}]})
    assert plan.exercises[0].demo["keyframes"] == RAISE and plan.exercises[0].tempo_s == 3.0
    with pytest.raises(PlanError, match="exactly one"):
        parse_plan({"exercises": [{**ex, "demo": {"keyframes": RAISE, "arm": "up"}}]})
    with pytest.raises(PlanError, match="tempo_s"):
        parse_plan({"exercises": [{**ex, "demo": {"arm": "up"}, "tempo_s": 0}]})


def test_pose_over_time():
    start = keyframes.start_pose()["servos"]
    assert start["left_shoulder_pitch"] == 725 and keyframes.duration_ms(RAISE) == 2600
    assert keyframes.pose_at(RAISE, 0)["servos"] == start
    assert keyframes.pose_at(RAISE, 500)["servos"]["left_shoulder_pitch"] == 525   # linear, half way
    assert keyframes.pose_at(RAISE, 1000)["servos"]["left_shoulder_pitch"] == 325
    assert keyframes.pose_at(RAISE, 1250)["servos"]["left_shoulder_pitch"] < 425   # eased: slow to start
    assert keyframes.pose_at(RAISE, 2300)["servos"] == start                       # holding
    assert keyframes.pose_at(RAISE, 9999)["servos"] == start
    assert keyframes.pose_at(RAISE, 500)["servos"]["left_elbow"] == start["left_elbow"]  # untouched joint


def test_steps_respect_speed_and_skip_holds():
    steps = keyframes.steps(RAISE)
    assert len(steps) == 9 and all(s["ms"] == 300 for s in steps)
    assert set(steps[0]["servos"]) == {"left_shoulder_pitch", "right_shoulder_pitch"}
    assert steps[-1]["servos"] == {} and steps[-1]["t_ms"] == 2600   # the hold sends nothing
    assert len(keyframes.steps(RAISE, speed=0.5)) == 18
    assert keyframes.steps(RAISE, speed=0.5)[-1]["t_ms"] == 2600


def test_play_sends_set_many_and_can_be_cancelled(fake_robot):
    calls = []

    def call(path, **kwargs):
        calls.append((path, kwargs))
        return LocalBackend().call(path, **kwargs)

    assert keyframes.play(call, RAISE[:1], sleep=lambda s: None) is True
    assert {c[0] for c in calls} == {"servo/set_many"} and len(calls) == 4
    assert calls[-1][1] == {"targets": {"left_shoulder_pitch": 325, "right_shoulder_pitch": 675},
                            "ms": 300, "wait": False}
    assert fake_robot.positions[8] == 325 and fake_robot.positions[16] == 675
    calls.clear()
    assert keyframes.play(call, RAISE, cancelled=lambda: len(calls) >= 2, sleep=lambda s: None) is False
    assert len(calls) == 2


def test_animation_to_plan_to_robot_round_trip(tmp_path, fake_robot, monkeypatch):
    monkeypatch.setattr(keyframes.time, "sleep", lambda s: None)
    animations = store.Animations(str(tmp_path / "animations"))
    plans = store.Plans(str(tmp_path / "plans"))

    saved = animations.save(ANIM)
    assert saved["id"] == "slow_arm_raise" and animations.get("slow_arm_raise")["keyframes"] == RAISE
    assert animations.list()[0]["duration_ms"] == 2600

    exercise = store.as_exercise(saved)
    assert exercise["demo"] == {"keyframes": RAISE, "times": 1} and exercise["tempo_s"] == 2.6
    plans.add_exercise("mornings", exercise)
    plans.add_exercise("mornings", exercise)  # saving again replaces, not duplicates
    on_disk = yaml.safe_load(open(tmp_path / "plans" / "mornings.yaml"))
    assert [e["name"] for e in on_disk["exercises"]] == ["Slow arm raise"]

    plan = load_plan(str(tmp_path / "plans" / "mornings.yaml"))   # plan.py is the judge
    ex = plan.exercises[0]
    assert (ex.sets, ex.reps, ex.track_joint, ex.tempo_s) == (2, 5, "shoulder_elevation", 2.6)
    Robot(LocalBackend()).demonstrate(ex.demo)
    moves = [e for e in fake_robot.log if e[0] == "bus_servo_set_position"]
    assert len(moves) == 7 and all(sorted(p[0] for p in m[2]) == [8, 16] for m in moves)
    assert fake_robot.positions[8] == 725  # back down at the end


def test_animations_refuse_legs_bad_names_and_bad_exercise_fields(tmp_path):
    animations = store.Animations(str(tmp_path))
    with pytest.raises(PlanError, match="not an arm joint"):
        animations.save({"name": "Squat", "keyframes": [{"ms": 1000, "servos": {"left_knee": 300}}]})
    with pytest.raises(PlanError, match="needs a name"):
        animations.save({"keyframes": RAISE})
    with pytest.raises(PlanError, match="reps"):
        animations.save({**ANIM, "exercise": {"reps": 500}})
    with pytest.raises(PlanError, match="letters, digits"):
        animations.get("../credentials")
    assert animations.list() == []


def test_import_contract_thins_frames_to_keyframes():
    frames = [{"t_ms": i * 33, "servos": {"8": 725 - i * 4, "16": 275 + i * 4, "3": 500}} for i in range(91)]
    anim = store.import_frames({"name": "From video", "frames": frames})
    assert anim["source"] == "imported" and 3 <= len(anim["keyframes"]) <= 12
    assert all(set(kf["servos"]) <= {"left_shoulder_pitch", "right_shoulder_pitch"} for kf in anim["keyframes"])
    assert anim["keyframes"][-1]["servos"]["left_shoulder_pitch"] == 365
    store.check_animation(anim)  # what the importer makes is a valid animation
    with pytest.raises(PlanError, match="no arm joints"):
        store.import_frames({"frames": [{"t_ms": 0, "servos": {"left_knee": 400}}]})
    with pytest.raises(PlanError, match="frames"):
        store.import_frames({"frames": []})
