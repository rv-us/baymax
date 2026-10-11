"""The dashboard server: robot proxy errors, the safety latch, playback, the coach, live sessions."""
import time

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from dashboard import coach as coach_mod
from dashboard import synthetic
from dashboard.app import create_app
from dashboard.config import Settings, ollama_url
from dashboard.robot import classify
from robot_api.errors import RobotError

RAISE = [{"ms": 300, "ease": "linear", "servos": {"left_shoulder_pitch": 625}}]


def make(tmp_path, backend=None, **overrides):
    settings = Settings(dry_run=True, monitor=False, stub_llm=True, sessions_dir=str(tmp_path / "sessions"),
                        animations_dir=str(tmp_path / "animations"), plans_dir=str(tmp_path / "plans"),
                        web_dir=str(tmp_path / "web"))
    for k, v in overrides.items():
        setattr(settings, k, v)
    app = create_app(settings, backend)
    return TestClient(app), app.state.parts


@pytest.fixture
def dash(tmp_path):
    return make(tmp_path)


def stops(state):
    return [e for e in state.log if e[0] == "bus_servo_stop"]


def wait_for(condition, seconds=5.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


# Proxy and its errors

def test_proxy_passes_calls_through(dash, fake_robot):
    client, _ = dash
    r = client.post("/api/robot/actions/arm", json={"side": "left", "position": "up"})
    assert r.status_code == 200 and r.json()["moved"] == {"left_shoulder_pitch": 125}
    assert client.post("/api/robot/sensors/distance").json()["distance_mm"] == 800
    assert client.get("/api/camera.jpg").headers["content-type"] == "image/jpeg"


def test_a_safety_refusal_is_409_with_the_robots_own_message(dash, fake_robot):
    client, _ = dash
    fake_robot.temps[3] = 90
    r = client.post("/api/robot/actions/stand", json={})
    assert r.status_code == 409
    assert "servo 3 (left_knee) is 90 C" in r.json()["detail"]


def test_bad_arguments_unknown_paths_and_risky_groups(dash):
    client, _ = dash
    assert client.post("/api/robot/servo/set", json={"nope": 1}).status_code == 422
    assert client.post("/api/robot/servo/explode", json={}).status_code == 404
    r = client.post("/api/robot/actions/run", json={"name": "wave", "allow_risky": True})
    assert r.status_code == 403 and "can't be run from the dashboard" in r.json()["detail"]
    r = client.post("/api/robot/actions/walk", json={"steps": 9})
    assert r.status_code == 409 and r.json()["detail"] == "steps must be 1-5"


class DeadBackend:
    def call(self, path, **kwargs):
        raise RobotError("robot API unreachable at http://10.0.0.9:8000: timed out")


def test_an_unreachable_robot_is_502_and_shows_in_status(tmp_path):
    client, parts = make(tmp_path, backend=DeadBackend())
    r = client.post("/api/robot/actions/stand", json={})
    assert r.status_code == 502 and "unreachable at http://10.0.0.9:8000" in r.json()["detail"]
    assert client.get("/api/camera.jpg").status_code == 502
    status = parts["monitor"].refresh()
    assert status["links"]["robot"]["state"] == "down" and status["links"]["camera"]["state"] == "down"
    assert status["motion"]["ok"] is False and "isn't reachable" in status["motion"]["reasons"][0]
    # Stop still answers, and says it couldn't confirm.
    assert client.post("/api/stop").status_code == 502


def test_http_backend_messages_keep_their_status():
    assert classify("servo 2 is 90 C; let the servos cool (HTTP 409)") == (409, "servo 2 is 90 C; let the servos cool")
    assert classify("servo/set: missing a required argument: 'pulse' (HTTP 422)")[0] == 422
    assert classify("missing or wrong bearer token (HTTP 401)")[0] == 502
    assert classify("Internal Server Error (HTTP 500)")[0] == 502


# Status

def test_status_reports_all_three_links_honestly(dash, fake_robot):
    client, parts = dash
    status = parts["monitor"].refresh()
    assert {k: v["state"] for k, v in status["links"].items()} == {"robot": "ok", "camera": "ok", "llm": "stub"}
    assert status["health"]["battery_v"] == 12.4 and status["motion"] == {"ok": True, "reasons": []}

    fake_robot.temps[8] = 84
    fake_robot.battery_mv = 9500
    status = parts["monitor"].refresh()
    assert status["motion"]["ok"] is False
    assert any("Left shoulder pitch is 84 °C (limit 82.5 °C)" in r for r in status["motion"]["reasons"])
    assert {a["kind"] for a in client.get("/api/safety").json()["alerts"]} == {"servo_hot", "battery_low"}

    fake_robot.temps[8], fake_robot.battery_mv = 40, 12400
    assert parts["monitor"].refresh()["motion"]["ok"] is True
    assert client.get("/api/safety").json()["alerts"] == []


def test_llm_link_is_down_when_ollama_is_not_there(tmp_path):
    _, parts = make(tmp_path, stub_llm=False, ollama_url="http://127.0.0.1:9")
    llm = parts["coach"].probe()
    assert llm["state"] == "down" and "isn't answering" in llm["detail"]
    assert ollama_url("gx10") == "http://gx10:11434" and ollama_url("10.0.0.5:1234") == "http://10.0.0.5:1234"


# Safety latch

def test_pain_freezes_first_then_latches_until_a_therapist_clears_it(dash, fake_robot):
    client, _ = dash
    assert client.post("/api/safety/heard", json={"text": "nothing hurts"}).json()["pain"] is False
    assert not stops(fake_robot)

    r = client.post("/api/safety/heard", json={"text": "ow, my shoulder hurts"})
    assert r.json()["pain"] is True and len(stops(fake_robot)) == 1
    state = client.get("/api/safety").json()
    assert state["stop"]["kind"] == "pain" and state["stop"]["transcript"] == "ow, my shoulder hurts"

    r = client.post("/api/robot/actions/arm", json={"side": "both", "position": "up"})
    assert r.status_code == 409 and "Safety stop is active (pain word heard)" in r.json()["detail"]
    assert client.post("/api/playback/start", json={"keyframes": RAISE}).status_code == 409
    assert not [e for e in fake_robot.log if e[0] == "bus_servo_set_position"]
    # Stopping and speaking still work while latched.
    assert client.post("/api/stop").status_code == 200
    assert client.post("/api/robot/sensors/voice/say", json={"text": "Let's rest."}).status_code == 200

    assert client.post("/api/safety/clear", json={}).status_code == 422
    assert client.get("/api/safety").json()["stop"] is not None
    assert client.post("/api/safety/clear", json={"confirm": True}).json()["stop"] is None
    assert client.post("/api/robot/actions/arm", json={"side": "both", "position": "up"}).status_code == 200


def test_a_reported_fall_latches_too(dash, fake_robot):
    client, _ = dash
    assert client.post("/api/safety/report", json={"kind": "nap"}).status_code == 422
    assert client.post("/api/safety/report", json={"kind": "fall", "detail": "IMU pitch 170"}).status_code == 200
    assert len(stops(fake_robot)) == 1
    assert client.get("/api/safety").json()["stop"]["title"] == "fall suspected"


# Preview and playback

def test_preview_moves_the_twin_and_never_the_robot(dash, fake_robot):
    client, parts = dash
    pose = client.post("/api/preview/pose", json={"keyframes": RAISE, "t_ms": 150}).json()
    assert pose["servos"]["left_shoulder_pitch"] == 675
    assert parts["twin"].state.command.servos[8] == 675
    assert not [e for e in fake_robot.log if e[0] == "bus_servo_set_position"]
    r = client.post("/api/preview/pose", json={"servos": {"left_knee": 300}})
    assert r.status_code == 422 and "not an arm joint" in r.json()["detail"]


def test_play_on_robot_runs_and_is_blocked_when_health_says_no(dash, fake_robot):
    client, parts = dash
    assert client.post("/api/playback/start", json={"keyframes": RAISE, "name": "raise"}).json()["playing"]
    assert wait_for(lambda: not parts["player"].playing)
    assert fake_robot.positions[8] == 625 and parts["player"].state["error"] is None
    assert parts["twin"].state.command.servos[8] == 625

    fake_robot.temps[5] = 88
    parts["monitor"].refresh()
    r = client.post("/api/playback/start", json={"keyframes": RAISE})
    assert r.status_code == 409 and "Left hip roll is 88 °C" in r.json()["detail"]
    r = client.post("/api/playback/start", json={"keyframes": [{"ms": 300, "servos": {"left_knee": 1}}]})
    assert r.status_code == 422


def test_a_refusal_mid_playback_is_reported_not_swallowed(dash, fake_robot):
    client, parts = dash
    parts["monitor"].refresh()
    fake_robot.temps[2] = 90  # health was fine when Play was pressed; the robot refuses the move
    client.post("/api/playback/start", json={"keyframes": RAISE})
    assert wait_for(lambda: not parts["player"].playing)
    assert "servo 2 (left_ankle_pitch) is 90 C" in parts["player"].state["error"]


def test_animation_and_plan_endpoints(dash):
    client, _ = dash
    model = client.get("/api/animation-model").json()
    assert [j["name"] for j in model["joints"]][0] == "left_shoulder_pitch"
    assert model["joints"][0]["limits"] == [0, 1000] and model["min_ms"] == 300
    saved = client.post("/api/animations", json={"name": "Raise", "keyframes": RAISE}).json()
    assert saved["id"] == "raise" and client.get("/api/animations").json()[0]["keyframes"] == 1
    r = client.post("/api/animations/raise/exercise", json={"plan": "evenings"})
    assert r.json()["plan"]["exercises"][0]["demo"]["keyframes"] == RAISE
    assert [p["id"] for p in client.get("/api/plans").json()] == ["evenings"]
    assert client.post("/api/animations", json={"name": "Bad", "keyframes": []}).status_code == 422
    assert client.delete("/api/animations/raise").status_code == 200
    assert client.get("/api/animations/raise").status_code == 404


# The coach and its fallback

@pytest.fixture
def seeded(tmp_path):
    client, parts = make(tmp_path, stub_llm=False, ollama_url="http://127.0.0.1:9", coach_timeout_s=1)
    ids = synthetic.seed_sessions(parts["sessions"], count=4, seed=11, now=1_800_000_000)
    return client, parts, ids


GOOD_REPLY = {"headline": "A solid session.", "highlights": ["Every rep done.", "Steady pace."],
              "work_on": "The last few reps were lower.", "follow_up": False,
              "adjustment": {"kind": "extra_rest", "text": "Rest a little longer between sets."}}


def test_a_session_always_has_a_summary_even_before_the_coach_runs(seeded):
    client, _, ids = seeded
    s = client.get(f"/api/sessions/{ids[-1]}").json()
    assert s["summary"]["source"] == "template" and s["summary_cached"] is False
    assert s["summary"]["headline"] and 2 <= len(s["summary"]["highlights"]) <= 3
    assert s["summary"]["adjustment"]["kind"] in coach_mod.ADJUSTMENTS
    assert client.get(f"/api/sessions/{ids[-1]}?weight_kg=70").json()["calories"]["estimate"] is True
    assert client.get("/api/sessions/nope").status_code == 404


def test_coach_offline_falls_back_to_the_template_and_says_why(seeded):
    _, parts, ids = seeded
    session = parts["sessions"].get(ids[-1])
    summary = parts["coach"].summarize(session, parts["sessions"].history(session["started"]))
    assert summary["source"] == "template" and "isn't reachable" in summary["error"]
    assert summary["headline"] == "Finished all 2 exercises."


@pytest.mark.parametrize("reply, source", [
    (GOOD_REPLY, "llm"),
    ({"headline": "Only a headline"}, "template"),
    ({**GOOD_REPLY, "highlights": ["one"]}, "template"),
    (None, "template"),
    ("not json at all", "template"),
])
def test_bad_model_output_falls_back(seeded, monkeypatch, reply, source):
    _, parts, ids = seeded
    monkeypatch.setattr(parts["coach"], "ask", lambda facts: reply)
    session = parts["sessions"].get(ids[-1])
    assert parts["coach"].summarize(session, [])["source"] == source


def test_a_timeout_falls_back(seeded, monkeypatch):
    _, parts, ids = seeded

    def slow(facts):
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(parts["coach"], "ask", slow)
    summary = parts["coach"].summarize(parts["sessions"].get(ids[0]), [])
    assert summary["source"] == "template" and summary["error"] == "The coach didn't answer within 1 s."


def test_the_model_cannot_lower_the_follow_up_flag_or_see_made_up_numbers(seeded, monkeypatch):
    _, parts, ids = seeded
    hurt = next(parts["sessions"].get(i) for i in ids if parts["sessions"].get(i)["status"] == "stopped_pain")
    seen = {}
    monkeypatch.setattr(parts["coach"], "ask", lambda facts: seen.update(facts) or GOOD_REPLY)
    summary = parts["coach"].summarize(hurt, [])
    assert summary["source"] == "llm" and summary["follow_up"] is True
    assert summary["follow_up_reason"] == "The patient reported pain and the session was stopped."
    assert seen["scores"] == hurt["scores"] and "trace" not in str(seen)
    assert "never diagnose" in coach_mod.SYSTEM_PROMPT


def test_summary_job_runs_in_the_background_and_is_cached(seeded, monkeypatch):
    client, parts, ids = seeded
    monkeypatch.setattr(parts["coach"], "ask", lambda facts: GOOD_REPLY)
    assert client.post(f"/api/sessions/{ids[0]}/summary").json() == {"started": True, "pending": True}
    assert wait_for(lambda: ids[0] not in parts["coach"].pending)
    s = client.get(f"/api/sessions/{ids[0]}").json()
    assert s["summary_cached"] and s["summary"]["source"] == "llm" and s["summary"]["headline"] == "A solid session."
    assert client.get("/api/sessions").json()[-1]["headline"] == "A solid session."


def test_stub_coach_in_dry_run_is_labelled(dash):
    client, parts = dash
    ids = synthetic.seed_sessions(parts["sessions"], count=1)
    client.post(f"/api/sessions/{ids[0]}/summary")
    assert wait_for(lambda: not parts["coach"].pending)
    assert client.get(f"/api/sessions/{ids[0]}").json()["summary"]["source"] == "stub"


# Live session through the real workout loop

def tiny_plan(tmp_path):
    (tmp_path / "plans").mkdir(exist_ok=True)
    plan = {"name": "Tiny", "exercises": [{
        "name": "Quick raise", "cue": "Up and down.", "demo": {"keyframes": RAISE}, "sets": 1, "reps": 2,
        "mirror": "arms", "track_joint": "shoulder_elevation", "tempo_s": 1,
        "targets": {"shoulder_elevation": {"top": 90, "bottom": 15, "tolerance": 15}}}]}
    (tmp_path / "plans" / "tiny.yaml").write_text(yaml.safe_dump(plan))


def test_a_simulated_live_session_saves_a_scored_log(tmp_path, fake_robot):
    client, parts = make(tmp_path)
    tiny_plan(tmp_path)
    r = client.post("/api/live/start", json={"plan": "tiny", "simulate": True, "pace": 8, "seed": 1})
    assert r.status_code == 200 and r.json()["running"]
    assert client.post("/api/live/start", json={"plan": "tiny"}).status_code == 409
    assert wait_for(lambda: not parts["live"].running, 20)
    last = parts["live"].state["last"]
    assert last["status"] == "completed", parts["live"].state
    s = client.get(f"/api/sessions/{last['id']}").json()
    assert s["synthetic"] is True and s["sets"][0]["reps"] == 2 and len(s["sets"][0]["reps_detail"]) == 2
    assert s["sets"][0]["answer"] == "No, it was fine"
    assert client.post("/api/live/start", json={"plan": "missing"}).status_code == 404


def test_a_session_can_be_built_on_the_spot_and_saved(tmp_path, fake_robot):
    client, parts = make(tmp_path)
    tiny_plan(tmp_path)
    ex = client.get("/api/plans").json()[0]["exercises"][0]
    assert any(x["name"] == "Quick raise" for x in client.get("/api/exercises").json())
    bad = client.post("/api/live/start", json={"exercises": [{**ex, "reps": 99}]})
    assert bad.status_code == 422 or bad.status_code == 400
    r = client.post("/api/live/start", json={"exercises": [{**ex, "reps": 3}], "name": "My mix",
                                             "save_as": "my_mix", "simulate": True, "pace": 8, "seed": 1})
    assert r.status_code == 200 and r.json()["plan"] == "My mix"
    saved = next(p for p in client.get("/api/plans").json() if p["id"] == "my_mix")
    assert saved["exercises"][0]["reps"] == 3
    assert wait_for(lambda: not parts["live"].running, 20)


def test_the_robot_mirrors_every_rep_in_a_live_session(tmp_path, fake_robot):
    """Regression: the robot used to stall part-way through the first rep."""
    client, parts = make(tmp_path)
    tiny_plan(tmp_path)
    ex = client.get("/api/plans").json()[0]["exercises"][0]
    r = client.post("/api/live/start", json={"exercises": [{**ex, "reps": 6}], "simulate": True, "pace": 8, "seed": 5})
    assert r.status_code == 200
    assert wait_for(lambda: not parts["live"].running, 40)
    last = parts["live"].state["last"]
    s = client.get(f"/api/sessions/{last['id']}").json()
    ref = s["sets"][0]["trace"]["reference"]
    mid = (max(ref) + min(ref)) / 2
    peaks = sum(1 for a, b in zip(ref, ref[1:]) if a < mid <= b)
    assert peaks == 6, peaks
    assert abs(ref[-1] - min(ref)) < 5      # and it ends back at rest


def test_pain_during_a_live_session_stops_flags_and_latches(tmp_path, fake_robot):
    client, parts = make(tmp_path)
    tiny_plan(tmp_path)
    client.post("/api/live/start", json={"plan": "tiny", "simulate": True, "pace": 2, "seed": 1})
    assert wait_for(lambda: parts["live"].state.get("state") == "MIRROR", 20)
    client.post("/api/safety/heard", json={"text": "ouch that hurts"})
    assert wait_for(lambda: not parts["live"].running, 20)
    last = parts["live"].state["last"]
    assert last["status"] == "stopped_pain" and last["flagged"]
    s = client.get(f"/api/sessions/{last['id']}").json()
    assert [e["type"] for e in s["events"]] == ["pain_word", "safety_stop"]
    assert s["events"][0]["transcript"] == "ouch that hurts" and len(s["interrupted"]) == 1
    assert client.get("/api/safety").json()["stop"]["kind"] == "pain" and stops(fake_robot)


def test_stop_button_ends_a_live_session(tmp_path, fake_robot):
    client, parts = make(tmp_path)
    tiny_plan(tmp_path)
    client.post("/api/live/start", json={"plan": "tiny", "simulate": True, "pace": 2, "seed": 1})
    assert wait_for(lambda: parts["live"].state.get("state") == "MIRROR", 20)
    assert client.post("/api/stop").status_code == 200
    assert wait_for(lambda: not parts["live"].running, 20)
    assert parts["live"].state["last"]["status"] == "stopped_operator"
    assert client.get("/api/safety").json()["stop"] is None  # Stop itself never latches


def test_reposition_prompts_and_frame_advice(dash, fake_robot):
    client, _ = dash
    assert client.post("/api/live/reposition", json={"kind": "closer"}).status_code == 200
    assert ("say", "Can you come a little closer, please?") in fake_robot.log
    r = client.post("/api/live/reposition", json={"kind": "robot_left", "steps": 9}).json()
    assert r["side_stepped"] == "left" and r["times"] == 2          # capped at 2 steps
    assert client.post("/api/live/reposition", json={"kind": "dance"}).status_code == 409
    client.post("/api/live/frame", json={"visible": True, "x": 0.0, "size": 0.2})
    assert client.get("/api/status").json()["live"]["advice"]["kind"] == "closer"
    client.post("/api/live/frame", json={"visible": False})
    assert [a["kind"] for a in client.get("/api/safety").json()["alerts"]] == ["out_of_frame"]
