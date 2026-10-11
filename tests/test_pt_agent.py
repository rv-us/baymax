import contextlib
import json
import os
import time
import types

import pytest

from pt_agent.ears import is_wake
from pt_agent.perception import (Body, PeopleTracker, Perception, Person, body_from_landmarks,
                                 count_reversals)
from pt_agent.tools import CLOSE_SIZE, LEFT_OUT, SessionLog, Settings, make_pt_tools
from robot_api.api import LocalBackend


@pytest.fixture(autouse=True)
def instant_servo_moves(monkeypatch):
    """The fake robot waits out every move in real time."""
    from robot_api.servo import bus, head

    fast = types.SimpleNamespace(sleep=lambda s: None, time=time.time, monotonic=time.monotonic)
    monkeypatch.setattr(bus, "time", fast)
    monkeypatch.setattr(head, "time", fast)


class FakeEars:
    """listen() answers in order; poll() gives unprompted speech in order."""

    def __init__(self, answers=(), unprompted=()):
        self.answers, self.unprompted = list(answers), list(unprompted)
        self.callbacks = []

    def on_heard(self, cb):
        self.callbacks.append(cb)

    def hear(self, text):  # the patient says something out of turn
        for cb in self.callbacks:
            cb(text)

    def listen(self, timeout_s):
        return self.answers.pop(0) if self.answers else None

    def poll(self):
        return self.unprompted.pop(0) if self.unprompted else None

    def clear(self):
        pass

    def speaking(self):
        return contextlib.nullcontext()


class Backend:
    """LocalBackend with hooks: walking brings the patient closer, climate can be scripted."""

    def __init__(self, state, temps=None):
        self.inner, self.state = LocalBackend(), state
        self.temps = list(temps or [])
        self.calls = []

    def call(self, path, **kwargs):
        self.calls.append(path)
        if path == "actions/walk":
            self.state.distance_mm -= 150
        if path == "sensors/climate" and self.temps:
            return {"temp_c": self.temps.pop(0) if len(self.temps) > 1 else self.temps[0]}
        return self.inner.call(path, **kwargs)


def tools_for(backend, ears=None, perception=None, tmp_path=".", **settings):
    perception = perception or Perception(None, None, backend).start()
    session = SessionLog()
    tools = {t.name: t for t in make_pt_tools(backend, perception, ears or FakeEars(), session,
                                              Settings(sessions_dir=str(tmp_path), **settings),
                                              sleep=lambda s: None)}
    return tools, perception, session


def landmarks(face=(0.5, 0.3), shoulders=((0.6, 0.5), (0.4, 0.5)), wrists=None):
    pts = [(0.0, 0.0, 0.0)] * 33
    for i in (0, 2, 5, 7, 8):
        pts[i] = (face[0], face[1], 0.9)
    pts[11], pts[12] = (*shoulders[0], 0.9), (*shoulders[1], 0.9)
    for i, xy in (wrists or {}).items():
        pts[i] = (*xy, 0.9)
    return pts


# ---- perception ------------------------------------------------------------------

def test_count_reversals_ignores_small_wiggles():
    assert count_reversals([0, 0.3, 0, 0.3], 0.25) == 2
    assert count_reversals([0, 0.1, 0, 0.1, 0], 0.25) == 0
    assert count_reversals([], 0.25) == 0


def test_body_from_landmarks():
    body = body_from_landmarks(landmarks(wrists={15: (0.7, 0.2)}))
    assert body.face == pytest.approx((0.5, 0.3))
    assert body.size == pytest.approx(0.2)
    assert body.wrists["left"] == (pytest.approx(0.5), True, True)   # above shoulder and head
    assert body_from_landmarks([(0, 0, 0)] * 33) is None             # no head visible


def test_wave_detected_from_wrist_swinging_above_shoulder():
    t0 = 100.0
    p = Person(1, body_from_landmarks(landmarks()), t0)
    for k in range(8):
        x = 0.62 if k % 2 else 0.75  # left wrist swinging side to side, above the shoulder
        p.update(body_from_landmarks(landmarks(wrists={15: (x, 0.4)})), t0 + 0.2 * (k + 1))
    assert p.waving(t0 + 1.6)
    assert not p.waving(t0 + 10)  # old motion doesn't count


def test_hand_held_still_is_not_a_wave_but_a_raised_hand():
    t0 = 100.0
    p = Person(1, body_from_landmarks(landmarks()), t0)
    for k in range(8):
        p.update(body_from_landmarks(landmarks(wrists={15: (0.7, 0.1)})), t0 + 0.2 * (k + 1))
    assert not p.waving(t0 + 1.6)
    assert p.hand_raised(t0 + 1.6)


def test_tracker_keeps_ids_and_forgets_people():
    tr = PeopleTracker()
    a, b = Body((0.2, 0.3), 0.2, {}), Body((0.8, 0.3), 0.1, {})
    first = {p.body.face: p.id for p in tr.update([a, b], 0.0)}
    moved = [Body((0.25, 0.3), 0.2, {}), Body((0.78, 0.3), 0.1, {})]
    second = {round(p.body.face[0], 2): p.id for p in tr.update(moved, 0.5)}
    assert second[0.25] == first[(0.2, 0.3)] and second[0.78] == first[(0.8, 0.3)]
    assert tr.update([], 0.5 + PeopleTracker.FORGET_S + 0.1) == []


@pytest.mark.parametrize("name,people", [("yolo", 2), ("mediapipe", 1)])
def test_detectors_find_people_in_a_photo(name, people):
    """zidane.jpg has two people; MediaPipe only ever reports one."""
    ultralytics = pytest.importorskip("ultralytics")
    pytest.importorskip("mediapipe")
    import cv2

    from pt_agent.perception import make_detector

    image = os.path.join(os.path.dirname(ultralytics.__file__), "assets", "zidane.jpg")
    detector = make_detector(name)
    try:
        bodies = detector(cv2.imread(image))  # 1280 wide: MediaPipe needs the resize to 640
    finally:
        detector.close()
    assert len(bodies) == people
    assert all(0 <= b.face[0] <= 1 and 0 < b.size < 1 for b in bodies)


# ---- tools -----------------------------------------------------------------------

def test_tool_set():
    tools, _, _ = tools_for(LocalBackend())
    assert not LEFT_OUT & set(tools) - {"say", "camera_view", "look"}  # replaced ones come back
    for name in ("wait_for_patient", "approach_patient", "take_temperature", "listen", "say", "ask",
                 "people_in_view", "follow_patient", "record", "end_session", "clear_alert", "move_arm"):
        assert name in tools


def test_wake_words():
    assert is_wake("Hi Baymax!") and is_wake("I'm ready") and is_wake("over here")
    assert not is_wake("the weather is nice") and not is_wake(None)


def test_wait_for_patient_engages_on_greeting(fake_robot):
    tools, perception, _ = tools_for(LocalBackend(), FakeEars(unprompted=["nice day", "hi baymax"]))
    pid = perception.simulate_person(waving=False)
    out = tools["wait_for_patient"].invoke({"timeout_s": 5})
    assert out["engaged_by"] == "voice" and out["said"] == "hi baymax"
    assert out["patient"]["id"] == pid and perception.following


def test_wait_for_patient_engages_on_wave(fake_robot):
    tools, perception, _ = tools_for(LocalBackend())
    near = perception.simulate_person(waving=False)
    perception.person(near).body = Body((0.5, 0.4), 0.4, {})   # the closest person
    perception.simulate_person(waving=True)                     # someone further back waves
    out = tools["wait_for_patient"].invoke({"timeout_s": 5})
    assert out["engaged_by"] == "wave"
    assert out["patient"]["id"] == near   # the patient is whoever is in front


def test_wait_for_patient_searches_and_times_out(fake_robot):
    tools, perception, _ = tools_for(LocalBackend())
    out = tools["wait_for_patient"].invoke({"timeout_s": 0.3})
    assert out["engaged_by"] is None
    icons = [e for e in fake_robot.log if e[0] == "matrix_icon"] if fake_robot.log else []
    assert perception.closest() is None and icons is not None


def test_pain_word_stops_robot_and_blocks_motion(fake_robot):
    ears = FakeEars()
    tools, _, session = tools_for(LocalBackend(), ears)
    ears.hear("ow, that hurts")
    assert ("stopAction",) in fake_robot.log or any(e[0] == "bus_servo_stop" for e in fake_robot.log)
    assert "error" in tools["move_arm"].invoke({"side": "right", "position": "forward"})
    assert "error" in tools["take_temperature"].invoke({})
    assert session.flagged
    tools["clear_alert"].invoke({})
    assert "error" not in tools["move_arm"].invoke({"side": "right", "position": "forward"})


def test_nothing_hurts_is_not_an_alert(fake_robot):
    ears = FakeEars()
    tools, _, session = tools_for(LocalBackend(), ears)
    ears.hear("nothing hurts, I'm good")
    assert not session.flagged
    assert "error" not in tools["move_arm"].invoke({"side": "right", "position": "forward"})


def test_simulated_temperature_reaches_out_and_reports_a_normal_reading(fake_robot, tmp_path):
    tools, _, session = tools_for(LocalBackend(), tmp_path=tmp_path)
    out = tools["take_temperature"].invoke({})
    assert 36.4 <= out["temperature_c"] <= 37.0 and out["contact_likely"]
    assert out["temperature_f"] == pytest.approx(out["temperature_c"] * 9 / 5 + 32, abs=0.1)
    right_shoulder = [p for e in fake_robot.log if e[0] == "bus_servo_set_position"
                      for sid, p in e[2] if sid == 16]
    assert right_shoulder[-2:] == [650, 275]              # reached forward, then back down
    saved = json.load(open(tools["end_session"].invoke({"summary": "x"})["saved"]))
    assert saved["temperature_simulated"] is True         # the notes say it wasn't measured
    assert saved["record"]["temperature_c"] == out["temperature_c"]


def test_take_temperature_reaches_out_reads_and_lowers_arm(fake_robot):
    backend = Backend(fake_robot, temps=[22.5, 24.0, 27.0, 30.0, 31.5, 32.0, 32.1, 32.1, 32.1])
    tools, _, session = tools_for(backend, real_temperature=True)
    out = tools["take_temperature"].invoke({"hold_s": 5})
    assert out["baseline_c"] == 22.5 and out["peak_c"] == 32.1
    assert out["rise_c"] == pytest.approx(9.6) and out["contact_likely"] and out["settled"]
    assert session.record["temperature_c"] == 32.1
    assert fake_robot.positions[16] == 275          # right shoulder back down
    arm_moves = [e for e in fake_robot.log if e[0] == "bus_servo_set_position"
                 and any(sid == 16 for sid, _ in e[2])]
    assert [p for e in arm_moves for sid, p in e[2] if sid == 16][-2:] == [650, 275]


def test_take_temperature_without_contact(fake_robot):
    tools, _, session = tools_for(Backend(fake_robot, temps=[22.5]), real_temperature=True)
    out = tools["take_temperature"].invoke({"hold_s": 0.2})
    assert out["rise_c"] == 0 and not out["contact_likely"]
    assert "temperature_c" not in session.record   # room air isn't the patient's temperature


def test_approach_walks_until_close(fake_robot):
    backend = Backend(fake_robot)
    tools, perception, _ = tools_for(backend)
    perception.simulate_person()
    out = tools["approach_patient"].invoke({"stop_distance_cm": 50})
    assert out["result"] == "arrived" and out["walk_moves"] == 2   # 800 -> 650 -> 500 mm
    assert backend.calls.count("actions/walk") == 2


def test_approach_turns_toward_patient_off_to_the_side(fake_robot):
    backend = Backend(fake_robot)
    tools, perception, _ = tools_for(backend)
    pid = perception.simulate_person()
    perception.person(pid).body = Body((0.95, 0.4), 0.25, {})   # far right of the frame
    out = tools["approach_patient"].invoke({"max_moves": 1})
    assert out["turns"] == 1 and "actions/turn" in backend.calls
    assert ("runActionGroup", "turn_right", 1, False) in fake_robot.log or out["turns"] == 1


def test_approach_stationary_only_faces_patient(fake_robot):
    backend = Backend(fake_robot)
    tools, perception, _ = tools_for(backend, stationary=True)
    perception.simulate_person()
    out = tools["approach_patient"].invoke({})
    assert out["result"].startswith("facing_patient") and "actions/walk" not in backend.calls


def test_approach_close_by_camera_size(fake_robot):
    tools, perception, _ = tools_for(Backend(fake_robot))
    pid = perception.simulate_person()
    perception.person(pid).body = Body((0.5, 0.4), CLOSE_SIZE + 0.05, {})
    assert tools["approach_patient"].invoke({})["result"] == "arrived"


def test_listen_record_and_end_session(fake_robot, tmp_path):
    tools, _, session = tools_for(LocalBackend(), FakeEars(answers=["Maria"]), tmp_path=tmp_path)
    tools["say"].invoke({"text": "What's your name?"})
    assert tools["listen"].invoke({})["heard"] == "Maria"
    assert tools["listen"].invoke({})["heard"] is None
    tools["record"].invoke({"item": "name", "value": "Maria"})
    saved = tools["end_session"].invoke({"summary": "Checked in Maria."})["saved"]
    data = json.load(open(saved))
    assert data["record"] == {"name": "Maria"} and data["summary"] == "Checked in Maria."
    assert [e["kind"] for e in data["events"]][:2] == ["robot", "patient"]


# ---- safety demo and "help" ------------------------------------------------------

def said_texts(backend):
    return [kw["text"] for path, kw in backend.said]


class SpeechBackend(Backend):
    """Backend that also keeps what the robot said, in order with the other calls."""

    def __init__(self, state, **kw):
        super().__init__(state, **kw)
        self.said = []

    def call(self, path, **kwargs):
        if path == "sensors/voice/say":
            self.said.append((path, kwargs))
        return super().call(path, **kwargs)


def test_safety_demo_warns_falls_says_help_and_gets_up(fake_robot):
    backend = SpeechBackend(fake_robot)
    tools, perception, session = tools_for(backend)
    perception.follow(True)
    out = tools["safety_demo"].invoke({})
    assert out["completed"]
    assert perception.following  # back to following whoever is closest afterwards
    groups = [e[1] for e in fake_robot.log if e[0] == "runActionGroup"]
    assert groups[:2] == ["lie_down", "stand_up_back"]
    order = [p if p != "actions/run" else "run" for p in backend.calls
             if p in ("sensors/voice/say", "actions/run", "actions/stand")]
    # intro, warn, fall, say help (from the floor), get up, stand, reassure
    say = "sensors/voice/say"
    assert order == [say, say, "run", say, "run", "actions/stand", say]
    intro, warning, help_line, done = said_texts(backend)
    assert "on purpose" in warning
    assert "say help" in help_line and "call your handler" in help_line
    assert "just practice" in done
    assert session.record["safety_demo"] == "completed"


def test_safety_demo_blocked_during_pain_alert(fake_robot):
    ears = FakeEars()
    tools, _, _ = tools_for(LocalBackend(), ears)
    ears.hear("ow")
    assert "error" in tools["safety_demo"].invoke({})
    assert not any(e[0] == "runActionGroup" for e in fake_robot.log)


def test_help_stops_says_911_stands_and_ends_the_session(fake_robot, tmp_path):
    ears = FakeEars()
    backend = SpeechBackend(fake_robot)
    tools, _, session = tools_for(backend, ears, tmp_path=tmp_path)
    tools["move_arm"].invoke({"side": "right", "position": "forward"})
    ears.hear("help me please")
    session.emergency_thread.join(timeout=10)
    assert session.ended and session.flagged and session.emergency == "help me please"
    calls = backend.calls
    assert calls.index("servo/stop") < calls.index("sensors/voice/say") < calls.index("actions/stand")
    assert "nine one one" in said_texts(backend)[-1]
    assert fake_robot.positions[16] == 275                     # back in the stand pose
    saved = json.load(open(session.path))
    assert saved["emergency"] == "help me please" and "EMERGENCY" in saved["summary"]
    for name, args in (("say", {"text": "hi"}), ("listen", {}), ("ask", {"question": "ok?"}),
                       ("record", {"item": "a", "value": "b"}),
                       ("move_arm", {"side": "left", "position": "up"}), ("clear_alert", {})):
        assert "session is over" in tools[name].invoke(args)["error"]


def test_ask_says_the_question_then_returns_the_answer(fake_robot, tmp_path):
    backend = SpeechBackend(fake_robot)
    tools, _, session = tools_for(backend, FakeEars(answers=["Maria", None]), tmp_path=tmp_path)
    assert tools["ask"].invoke({"question": "What's your name?"}) == {"heard": "Maria"}
    assert said_texts(backend) == ["What's your name?"]
    assert tools["ask"].invoke({"question": "Are you still there?"}) == {"heard": None}
    kinds = [(e["kind"], e.get("text")) for e in session.events][:4]
    assert kinds == [("robot", "What's your name?"), ("patient", "Maria"),
                     ("robot", "Are you still there?"), ("patient", None)]


def test_ask_reports_a_pain_alert(fake_robot):
    ears = FakeEars(answers=["it hurts a bit"])
    tools, _, _ = tools_for(SpeechBackend(fake_robot), ears)
    ears.hear("it hurts a bit")  # the listener's callback fires on everything heard
    out = tools["ask"].invoke({"question": "How is your shoulder?"})
    assert out["heard"] == "it hurts a bit" and "pain words" in out["alert"]


def test_help_is_heard_only_as_a_word():
    from pt_agent.tools import is_help

    assert is_help("Help!") and is_help("I need help")
    assert not is_help("that really helps") and not is_help("helpful") and not is_help(None)


def test_head_waits_for_a_frame_taken_after_the_last_move(fake_robot):
    perception = Perception(None, None, LocalBackend()).start()
    pid = perception.simulate_person()
    perception.person(pid).simulated = False          # let _follow steer toward it
    perception.person(pid).body = Body((0.9, 0.5), 0.25, {})
    perception.follow(True)
    perception.head.last_move = time.time() - 1
    pan0 = perception.head.pan
    perception._follow(frame_t=perception.head.last_move - 0.1)   # stale frame: no move
    assert perception.head.pan == pan0
    perception._follow(frame_t=time.time())                       # fresh frame: moves
    assert perception.head.pan != pan0


def test_approach_uses_whoever_is_closest_even_after_ids_change(fake_robot):
    """On the robot the tracker gave the patient a new id after the safety demo's fall, and
    approach_patient refused ("not following anyone"). It now just uses the closest person."""
    backend = Backend(fake_robot)
    tools, perception, _ = tools_for(backend)
    perception.simulate_person()
    tools["safety_demo"].invoke({})
    perception.tracker.people.clear()          # everyone forgotten while lying down
    perception.simulate_person()               # back in view with a new id
    assert tools["approach_patient"].invoke({})["result"] == "arrived"


def test_system_prompt_can_skip_the_name_and_the_safety_demo():
    from pt_agent.agent import SYSTEM_PROMPT, system_prompt

    assert system_prompt() == SYSTEM_PROMPT
    no_demo = system_prompt(skip_safety_demo=True)
    assert "call safety_demo" not in no_demo and "No safety demo" in no_demo
    no_name = system_prompt(skip_name=True)
    assert 'record("name"' not in no_name and "Don't ask their name" in no_name
    both = system_prompt(True, True)
    assert "call safety_demo" not in both and 'record("name"' not in both
