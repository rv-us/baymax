"""ArmTracker: what Mirror sends to the robot and what a workout is scored on come only from green joints."""
import importlib.util
import os

import numpy as np
import pytest

pytest.importorskip("cv2")
pytest.importorskip("mediapipe")

from dashboard import armtrack  # noqa: E402

FRAME = np.zeros((480, 640, 3), np.uint8)
ARM_MVP = os.path.join(os.path.dirname(__file__), "..", "arm_mvp")


def landmarks_for():
    spec = importlib.util.spec_from_file_location("arm_mvp_helpers", os.path.join(ARM_MVP, "test_arm_mvp.py"))
    armtrack.load_estimator()                       # puts arm_mvp on the path so its test module can import
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.landmarks_for


@pytest.fixture
def tracker(monkeypatch):
    mimic, _ = armtrack.load_estimator()
    state = {"lm": None, "switched": False, "people": 1}

    def fake_landmarker():
        def detect(frame, t_ms):
            detect.switched, detect.people = state["switched"], state["people"]
            return state["lm"]
        detect.switched, detect.people = False, 0
        return detect, lambda: None

    monkeypatch.setattr(mimic, "make_landmarker", fake_landmarker)
    return armtrack.ArmTracker(), state, landmarks_for()


def run(tracker, state, lm, frames, t0=0.0):
    state["lm"] = lm
    out = None
    for i in range(frames):
        out = tracker.process(FRAME, t0 + i / 30.0)
    return out, t0 + frames / 30.0


def test_with_both_arms_green_everything_is_sent_and_scored(tracker):
    t, state, landmarks_for = tracker
    pose, _ = run(t, state, landmarks_for({"L": (-90, 0, 0), "R": (-90, 0, 0)}), 60)[0]
    assert pose["green"] == {"L": True, "R": True}
    assert set(pose["pulses"]) == {"8", "7", "6", "16", "15", "14"}
    assert pose["angles"]["shoulder_elevation"] == pytest.approx(90.0, abs=8.0)
    assert pose["angles"]["elbow"] is not None


def test_an_arm_that_is_not_green_sends_nothing_and_the_workout_is_not_scored(tracker):
    t, state, landmarks_for = tracker
    good = landmarks_for({"L": (-90, 0, 0), "R": (-90, 0, 0)})
    _, now = run(t, state, good, 40)
    hidden = (good[0], good[1], good[2].copy())
    hidden[2][15] = 0.2                                          # the left wrist is occluded
    (pose, _), now = run(t, state, hidden, 15, now)              # it is holding, no longer green
    assert pose["arms"]["L"] == "HOLD" and pose["green"] == {"L": False, "R": True}
    assert set(pose["pulses"]) == {"16", "15", "14"}             # nothing for the left arm
    assert pose["angles"] == {"shoulder_elevation": None, "elbow": None}   # and no analysis from half a body
    (pose, _), now = run(t, state, hidden, 90, now)              # now it is lost
    assert pose["arms"]["L"] == "LOST" and set(pose["pulses"]) == {"16", "15", "14"}


def test_nobody_in_view_sends_nothing_and_scores_nothing(tracker):
    t, state, landmarks_for = tracker
    _, now = run(t, state, landmarks_for({"L": (-90, 0, 0), "R": (-90, 0, 0)}), 40)
    (pose, _), _ = run(t, state, None, 5, now)
    assert pose["visible"] is False
    assert pose["green"] == {"L": False, "R": False}             # holding, not green
    assert pose["pulses"] == {} and pose["angles"] == {}


def test_hanging_arms_read_pitch_zero_so_the_shoulder_servos_sit_at_rest(tracker):
    t, state, landmarks_for = tracker
    pose, _ = run(t, state, landmarks_for({"L": (0, 0, 0), "R": (0, 0, 0)}), 60)[0]
    assert pose["green"] == {"L": True, "R": True}
    assert abs(pose["pulses"]["8"] - 725) <= 8 and abs(pose["pulses"]["16"] - 275) <= 8     # pitch 0, not 180
    assert pose["angles"]["shoulder_elevation"] < 10.0


def test_a_change_of_person_starts_the_arms_fresh_and_is_counted(tracker):
    t, state, landmarks_for = tracker
    _, now = run(t, state, landmarks_for({"L": (-90, 0, 0), "R": (-90, 0, 0)}), 40)
    state.update(switched=True, people=2)
    (pose, _), _ = run(t, state, landmarks_for({"L": (0, 0, 0), "R": (0, 0, 0)}), 1, now)
    assert pose["people"] == 2
    # the new person's arms start from rest instead of carrying on from the old person's raised arms
    assert abs(t.arms["L"]["angles"][0]) < 13.0 and abs(t.arms["R"]["angles"][0]) < 13.0
