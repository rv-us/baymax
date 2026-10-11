import json
import time
import types

import pytest

from demo.__main__ import Demo
from demo.script import build
from robot_api.api import LocalBackend


@pytest.fixture(autouse=True)
def instant_servo_moves(monkeypatch):
    """The fake robot waits out every move in real time."""
    from robot_api.servo import bus, head

    fast = types.SimpleNamespace(sleep=lambda s: None, time=time.time, monotonic=time.monotonic)
    monkeypatch.setattr(bus, "time", fast)
    monkeypatch.setattr(head, "time", fast)


def run(tmp_path, steps=None, inputs=None, auto=True):
    prompts = []
    demo = Demo(LocalBackend(), steps or build(), auto=auto, sleep=lambda s: None,
                input_fn=lambda p: prompts.append(p) or "", sessions_dir=str(tmp_path))
    path = demo.run()
    return demo, path, prompts


def commands(state):
    """What the fake robot was told, without timestamps."""
    return [e for e in state.log]


def test_two_runs_do_exactly_the_same_thing(tmp_path, fake_robot):
    run(tmp_path)
    first = commands(fake_robot)
    fake_robot.reset()
    run(tmp_path)
    assert commands(fake_robot) == first
    assert any(e[0] == "say" for e in first)


def test_session_log_says_it_is_scripted(tmp_path):
    _, path, _ = run(tmp_path)
    log = json.load(open(path))
    assert log["source"] == "scripted-demo"
    assert log["flagged"] is True
    assert log["temperature_simulated"] is True


def test_pain_cue_freezes_the_servos_and_nothing_moves_after(tmp_path, fake_robot):
    run(tmp_path)
    kinds = [e[0] for e in fake_robot.log]
    stop = kinds.index("bus_servo_stop")
    assert not any(k in ("bus_servo_set_position", "runActionGroup") for k in kinds[stop:])


def test_presenter_is_asked_at_every_cue_unless_auto(tmp_path):
    cues = [s for s in build() if s.kind == "cue"]
    _, _, prompts = run(tmp_path, auto=False)
    assert len(prompts) == len(cues) == 5
    _, _, prompts = run(tmp_path, auto=True)
    assert prompts == []


def test_fall_and_walk_are_off_unless_asked(tmp_path, fake_robot):
    run(tmp_path)
    assert not any(e[0] == "runActionGroup" and e[1] in ("lie_down", "stand_up_back")
                   for e in fake_robot.log)
    fake_robot.reset()
    run(tmp_path, steps=build(walk=True, fall=True))
    names = [e[1] for e in fake_robot.log if e[0] == "runActionGroup"]
    assert "lie_down" in names and "stand_up_back" in names
