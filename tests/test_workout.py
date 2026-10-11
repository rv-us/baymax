import json
import time
import types

import pytest

from robot_api.api import LocalBackend
from workout import loop
from workout.loop import Settings, Workout
from workout.parts import FakeTracker, ScriptedEars, TemplateCoach, classify_ready, \
    classify_trouble, is_pain
from workout.plan import PlanError, load_plan, parse_plan
from workout.robot import Robot
from workout.timing import Timings

FINE = "no, it was fine"


@pytest.fixture(autouse=True)
def instant_servo_moves(monkeypatch):
    """The fake robot waits out every move in real time; a workout makes dozens."""
    from robot_api.servo import bus

    monkeypatch.setattr(bus, "time", types.SimpleNamespace(
        sleep=lambda s: None, time=time.time, monotonic=time.monotonic))


def make(answers, tmp_path, tracker=None, deviations=None, timings=None):
    plan = load_plan("arms_basic")
    w = Workout(plan, Robot(LocalBackend(), timings=timings), ScriptedEars(answers),
                tracker or FakeTracker(deviations), TemplateCoach(),
                Settings(ready_wait_s=0, countdown=False, sessions_dir=str(tmp_path)),
                sleep=lambda s: None, timings=timings)
    return w


def said(state):
    return [entry[1] for entry in state.log if entry[0] == "say"]


def states(session):
    return [t["to"] for t in session.transitions]


def servo_stops(state):
    return sum(1 for entry in state.log if entry[0] == "bus_servo_stop")


# Plan

def test_example_plan_loads():
    plan = load_plan("arms_basic")
    assert [e.name for e in plan.exercises] == ["Front arm raises", "Overhead reaches"]
    assert plan.exercises[0].demo == {"arm": "forward", "times": 2}
    assert plan.exercises[1].targets["shoulder_elevation"]["top"] == 160


@pytest.mark.parametrize("change, message", [
    ({"sets": 0}, "sets"),
    ({"demo": {"action_group": "fall_down"}}, "safe list"),
    ({"demo": {"arm": "sideways"}}, "demo arm"),
    ({"demo": {"arm": "up", "action_group": "wave"}}, "exactly one"),
    ({"mirror": "legs"}, "mirror"),
    ({"track_joint": "toe"}, "track_joint"),
])
def test_bad_plans_are_rejected(change, message):
    ex = {"name": "X", "cue": "c", "demo": {"arm": "up"}, "sets": 1, "reps": 1,
          "track_joint": "elbow", **change}
    with pytest.raises(PlanError, match=message):
        parse_plan({"exercises": [ex]})


def test_missing_fields_are_named():
    with pytest.raises(PlanError, match="missing cue, demo"):
        parse_plan({"exercises": [{"name": "X", "sets": 1, "reps": 1, "track_joint": "elbow"}]})


# Whole sessions (fake robot API, scripted answers, fake tracker)

def test_full_workout(tmp_path, fake_robot):
    w = make(["Yes, I'm ready", FINE, FINE, "That was hard", FINE], tmp_path,
             deviations=[["keep your elbows straight"], [], [], []])
    s = w.run()
    assert s.status == "completed" and not s.flagged
    assert [(r["exercise"], r["set"]) for r in s.sets] == [
        ("Front arm raises", 1), ("Front arm raises", 2),
        ("Overhead reaches", 1), ("Overhead reaches", 2)]
    assert s.sets[2]["trouble"] == "difficulty"
    assert states(s) == ["INTRODUCE", "DEMONSTRATE", "PROMPT_START", "MIRROR", "SET_REVIEW",
                         "PROMPT_START", "MIRROR", "SET_REVIEW",
                         "INTRODUCE", "DEMONSTRATE", "PROMPT_START", "MIRROR", "SET_REVIEW",
                         "PROMPT_START", "MIRROR", "SET_REVIEW", "END"]
    words = said(fake_robot)
    assert "Nice work! One thing to watch: keep your elbows straight." in words
    assert any("You finished all 2 exercises, with great form on 3 of 4 sets." in w for w in words)
    # The demo moved both arms forward and back (shoulder servos 8 and 16).
    moves = [e for e in fake_robot.log if e[0] == "bus_servo_set_position"]
    assert any(sorted(p[0] for p in e[2]) == [8, 16] for e in moves)
    saved = json.load(open(w.saved_path))
    assert saved["status"] == "completed" and len(saved["sets"]) == 4


def test_not_ready_three_times_ends_without_starting(tmp_path, fake_robot):
    s = make(["not yet", "give me a minute", "no"], tmp_path).run()
    assert s.status == "not_started"
    assert states(s) == ["READY_CHECK", "READY_CHECK", "END"]
    assert "That's okay. We can do your workout another time." in said(fake_robot)


def test_ready_on_the_second_try(tmp_path):
    s = make(["hold on", "okay let's go", FINE, FINE, FINE, FINE], tmp_path).run()
    assert s.status == "completed" and s.ready_tries == 2


def test_pain_in_an_answer_stops_and_flags(tmp_path, fake_robot):
    s = make(["yes", "my shoulder hurts"], tmp_path).run()
    assert s.status == "stopped_pain" and s.flagged
    assert states(s)[-2:] == ["SAFETY_STOP", "END"]
    assert len(s.sets) == 1 and s.sets[0]["trouble"] == "pain"
    assert servo_stops(fake_robot) >= 1
    assert any("Let's stop here" in w for w in said(fake_robot))


def test_pain_heard_mid_set_stops_the_set(tmp_path, fake_robot):
    # The tracker raises "pain" halfway through set 2, as the listener would.
    w = make(["yes", FINE], tmp_path, tracker=FakeTracker(events={2: "pain"}))
    s = w.run()
    assert s.status == "stopped_pain"
    assert states(s)[-4:] == ["PROMPT_START", "MIRROR", "SAFETY_STOP", "END"]
    assert len(s.sets) == 1  # set 2 was never reviewed
    assert s.reps_done == 4  # stopped at half of 8
    assert servo_stops(fake_robot) >= 1


def test_pain_words_from_the_listener_thread(tmp_path, fake_robot):
    w = make(["yes"], tmp_path)
    w._heard("ow ow that really hurts")  # what ListenerEars.on_heard delivers
    s = w.run()
    assert s.status == "stopped_pain"
    assert servo_stops(fake_robot) >= 1  # stopped before the engine even ran a state


def test_no_answer_twice_ends_the_session(tmp_path, fake_robot):
    s = make(["yes", None, None], tmp_path).run()
    assert s.status == "no_response" and s.flagged
    assert states(s)[-1] == "END"
    assert "I haven't heard from you, so let's stop for today." in said(fake_robot)


def test_leaving_the_frame_pauses_and_resumes_the_set(tmp_path):
    w = make(["yes", FINE, FINE, FINE, FINE], tmp_path,
             tracker=FakeTracker(events={1: "left_frame"}))
    s = w.run()
    assert s.status == "completed"
    assert states(s)[3:6] == ["MIRROR", "PAUSED", "MIRROR"]
    assert s.sets[0]["reps"] == 8  # the set picked up where it stopped


def test_leaving_the_frame_outside_a_set_is_ignored(tmp_path):
    w = make(["yes", FINE, FINE, FINE, FINE], tmp_path)
    w.post("left_frame")  # arrives during READY_CHECK
    s = w.run()
    assert s.status == "completed" and "PAUSED" not in states(s)


def test_timing_report_accounts_for_every_state(tmp_path):
    timings = Timings()
    s = make(["yes", FINE, FINE, FINE, FINE], tmp_path, timings=timings).run()
    assert s.status == "completed"
    assert [v[0] for v in timings.visits] == [t["from"] for t in s.transitions] + ["END"]
    assert all("seconds" in t for t in s.transitions)
    report = timings.report()
    assert "sensors/voice/say" in report and "actions/arm" in report
    assert "unexplained" not in report  # the loop itself adds no hidden waits


def test_every_state_can_reach_end():
    graph = {}
    for (state, _), nxt in loop.TRANSITIONS.items():
        graph.setdefault(state, set()).add(nxt)
    for state in loop.INTERRUPTS.values():
        graph.setdefault(state, set())
    graph[loop.PAUSED].add(loop.MIRROR)  # RESUME goes back to MIRROR
    for start in graph:
        seen, todo = set(), [start]
        while todo:
            s = todo.pop()
            if s == loop.END or s in seen or s is loop.RESUME:
                continue
            seen.add(s)
            todo += graph.get(s, ())
        assert loop.END in {n for s in seen for n in graph.get(s, ())} or start == loop.END, start


# Keyword stand-ins for the classifier (#10 replaces these)

@pytest.mark.parametrize("text, pain", [
    ("ow", True), ("my shoulder hurts", True), ("stop", True), ("that's painful", True),
    ("nothing hurts", False), ("no pain at all", False), ("it was fine", False), (None, False),
])
def test_is_pain(text, pain):
    assert is_pain(text) == pain


@pytest.mark.parametrize("text, verdict", [
    ("yes", "ready"), ("Yeah, I'm ready.", "ready"), ("not yet", "not_ready"),
    ("give me a minute", "not_ready"), ("banana", "unclear"), (None, "unclear"),
])
def test_classify_ready(text, verdict):
    assert classify_ready(text) == verdict


def test_classify_trouble():
    assert classify_trouble("it was a bit hard") == "difficulty"
    assert classify_trouble("my elbow hurts") == "pain"
    assert classify_trouble("nope, all good") == "none"
