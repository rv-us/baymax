"""The second dashboard pass: calibration limits, library play, recorder, generator, chat, analytics."""
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from dashboard import chat as chat_mod
from dashboard import generator as gen_mod
from dashboard import poses, starter, synthetic
from dashboard.app import create_app
from dashboard.calibration import CalibrationError, Limiter, Profiles, check_profile, factory_profile
from dashboard.config import Settings
from robot_api.servo.joints import JOINTS
from robot_api.servo.limits import LIMITS
from workout import session as S
from workout.plan import check_keyframes

RAISE = [{"ms": 300, "ease": "linear", "servos": {"left_shoulder_pitch": 200}}]


def make(tmp_path, **overrides):
    settings = Settings(dry_run=True, monitor=False, stub_llm=True, sessions_dir=str(tmp_path / "sessions"),
                        animations_dir=str(tmp_path / "animations"), plans_dir=str(tmp_path / "plans"),
                        calibration_dir=str(tmp_path / "calibration"), web_dir=str(tmp_path / "web"))
    for k, v in overrides.items():
        setattr(settings, k, v)
    app = create_app(settings)
    return TestClient(app), app.state.parts


@pytest.fixture
def dash(tmp_path):
    return make(tmp_path)


def wait_for(condition, seconds=6.0):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.02)
    return False


def moves(state):
    return [e for e in state.log if e[0] == "bus_servo_set_position"]


def sse(text: str) -> list:
    events = []
    for block in text.strip().split("\n\n"):
        kind = next(line[7:] for line in block.split("\n") if line.startswith("event: "))
        data = next(line[6:] for line in block.split("\n") if line.startswith("data: "))
        events.append((kind, json.loads(data)))
    return events


# Calibration profiles

def test_factory_profile_is_the_robot_apis_limits():
    f = factory_profile()
    assert f["servos"]["left_elbow"]["min"], f["servos"]["left_elbow"]["max"] == LIMITS[JOINTS["left_elbow"]]
    assert all(row["offset"] == 0 for row in f["servos"].values())


def test_profile_can_only_narrow_limits():
    lo, hi = LIMITS[JOINTS["left_shoulder_pitch"]]
    ok = check_profile({"name": "Tight", "servos": {"left_shoulder_pitch": {"min": lo + 100, "max": hi - 100}}})
    assert ok["servos"]["left_shoulder_pitch"]["min"] == lo + 100
    with pytest.raises(CalibrationError, match="max"):
        check_profile({"name": "Wide", "servos": {"left_elbow": {"max": 1000}}})
    with pytest.raises(CalibrationError, match="offset"):
        check_profile({"name": "Off", "servos": {"left_elbow": {"offset": 500}}})
    with pytest.raises(CalibrationError, match="speed"):
        check_profile({"name": "Fast", "servos": {"left_elbow": {"max_speed": 5000}}})
    with pytest.raises(CalibrationError, match="name"):
        check_profile({"servos": {}})
    with pytest.raises(CalibrationError, match="unknown joint"):
        check_profile({"name": "x", "servos": {"tail": {}}})


def test_profiles_persist_and_remember_the_active_one(tmp_path):
    p = Profiles(str(tmp_path))
    saved = p.save({"name": "Clinic A", "servos": {"left_elbow": {"offset": 12}}})
    assert saved["id"] == "clinic_a"
    p.set_active("clinic_a")
    again = Profiles(str(tmp_path))
    assert again.active()["servos"]["left_elbow"]["offset"] == 12
    assert [x["id"] for x in again.list()] == ["factory", "clinic_a"]
    again.delete("clinic_a")
    assert again.active_id() == "factory"
    with pytest.raises(CalibrationError):
        again.save({"name": "Factory"}, "factory")


def test_limiter_clamps_offsets_and_slows():
    profile = {"id": "t", "name": "t", "allow_action_groups": True, **check_profile({"name": "t", "servos": {
        "left_shoulder_pitch": {"min": 300, "max": 800, "offset": 20, "max_speed": 200}}})}
    lim = Limiter(profile)
    lim.positions[8] = 720
    raw, ms, report = lim.targets({"left_shoulder_pitch": 100}, 300)
    assert raw == {8: 320}                       # clamped to 300, then +20 offset
    assert report["clamped"]["left_shoulder_pitch"]["applied"] == 300
    assert ms == 2000 and report["slowed_ms"] == 2000   # 400 pulses at 200 a second
    status = lim.read({"servos": [{"id": 8, "position": 320}]})
    assert status["servos"][0]["position"] == 300 and status["servos"][0]["raw_position"] == 320


def test_arm_action_is_rewritten_so_limits_apply():
    lim = Limiter({"id": "t", "name": "t", "allow_action_groups": True, **check_profile({"name": "t", "servos": {
        "left_shoulder_pitch": {"min": 400, "max": 800}}})})
    path, kwargs, report = lim.apply("actions/arm", {"side": "left", "position": "up"})
    assert path == "servo/set_many" and kwargs["targets"] == {"8": 400}
    assert "left_shoulder_pitch" in report["clamped"]


def test_no_animation_can_exceed_the_active_profile(dash, fake_robot):
    client, parts = dash
    r = client.post("/api/calibration", json={"name": "Narrow", "activate": True, "servos": {
        "left_shoulder_pitch": {"min": 500, "max": 800}}})
    assert r.status_code == 200 and client.get("/api/calibration").json()["active"] == "narrow"
    parts["monitor"].refresh()
    assert client.post("/api/playback/start", json={"keyframes": RAISE, "name": "raise"}).status_code == 200
    assert wait_for(lambda: not parts["player"].playing)
    sent = [pulse for e in moves(fake_robot) for sid, pulse in e[2] if sid == 8]
    assert sent and min(sent) >= 500             # asked for 200, never went below 500
    assert parts["player"].state["limited"] == ["left_shoulder_pitch"]
    # the proxy and the jog are limited too
    r = client.post("/api/robot/servo/set", json={"servo": "left_shoulder_pitch", "pulse": 0})
    assert r.json()["calibration"]["clamped"]["left_shoulder_pitch"]["applied"] == 500
    assert fake_robot.positions[8] == 500


def test_profile_can_refuse_action_groups(dash):
    client, parts = dash
    client.post("/api/calibration", json={"name": "Strict", "activate": True, "allow_action_groups": False})
    r = client.post("/api/robot/actions/run", json={"name": "wave"})
    assert r.status_code == 409 and "action groups" in r.json()["detail"]
    client.post("/api/calibration/active", json={"id": "factory"})
    assert client.post("/api/robot/actions/run", json={"name": "wave"}).status_code == 200


def test_calibration_errors_are_readable(dash):
    client, _ = dash
    assert client.post("/api/calibration/active", json={"id": "nope"}).status_code == 404
    r = client.post("/api/calibration", json={"name": "Bad", "servos": {"left_elbow": {"min": 800, "max": 200}}})
    assert r.status_code == 422 and "left_elbow" in r.json()["detail"]
    assert client.put("/api/calibration/factory", json={"name": "x"}).status_code == 422


def test_debug_table_shows_commanded_against_actual(dash, fake_robot):
    client, parts = dash
    parts["monitor"].refresh()
    assert client.post("/api/debug/jog", json={"servo": "left_elbow", "pulse": 640}).status_code == 200
    rows = {r["joint"]: r for r in client.get("/api/debug/servos").json()["servos"]}
    assert rows["left_elbow"]["commanded"] == 640 and rows["left_elbow"]["actual"] == 640
    assert rows["left_elbow"]["error"] == 0 and rows["left_elbow"]["temp_c"] == 40
    assert rows["left_elbow"]["load"] is None and rows["left_knee"]["group"] == "leg"
    assert client.post("/api/debug/jog", json={"servo": "left_knee", "pulse": 500}).status_code == 422
    assert client.post("/api/debug/jog", json={"servo": "left_elbow", "pulse": "x"}).status_code == 422


# Library and preview

def test_starters_are_valid_and_distinct(tmp_path):
    written = starter.write(str(tmp_path))
    assert len(written) >= 7
    exercises = [a for a in starter.STARTERS.values() if a["kind"] == "exercise"]
    assert len(exercises) >= 4
    for anim in starter.STARTERS.values():
        check_keyframes(anim["keyframes"])
    assert len({json.dumps(a["keyframes"], sort_keys=True) for a in starter.STARTERS.values()}) == len(starter.STARTERS)


def test_library_lists_and_plays_over_the_api(dash, fake_robot):
    client, parts = dash
    starter.write(parts["settings"].animations_dir)
    lib = client.get("/api/library").json()
    assert {"wave_hello", "arm_circles"} <= {a["id"] for a in lib["animations"]}
    assert any(b["group"] == "wave" and b["available"] for b in lib["builtins"])
    parts["monitor"].refresh()
    r = client.post("/api/library/play", json={"id": "wave_hello", "speed": 1.5})
    assert r.status_code == 200 and r.json()["playing"] and r.json()["request"] == 1
    assert client.post("/api/library/play", json={"id": "cheer"}).status_code == 409   # one at a time
    client.post("/api/stop")
    assert wait_for(lambda: not parts["player"].playing)
    assert parts["player"].state["stopped"] is True
    r = client.post("/api/library/play", json={"builtin": "wave"})
    assert r.status_code == 200 and r.json()["kind"] == "builtin"
    assert wait_for(lambda: not parts["player"].playing)
    assert ("runActionGroup", "wave", 1, True) in fake_robot.log


def test_library_play_validates_what_it_is_sent(dash):
    client, parts = dash
    parts["monitor"].refresh()
    assert client.post("/api/library/play", json={"id": "../etc/passwd"}).status_code == 422
    assert client.post("/api/library/play", json={"id": "missing"}).status_code == 404
    assert client.post("/api/library/play", json={"builtin": "left_uppercut"}).status_code == 422
    assert client.post("/api/library/play", json={"builtin": "fall_down"}).status_code == 422
    starter.write(parts["settings"].animations_dir)
    assert client.post("/api/library/play", json={"id": "cheer", "speed": "fast"}).status_code == 422


def test_play_is_refused_while_a_safety_stop_is_latched(dash):
    client, parts = dash
    starter.write(parts["settings"].animations_dir)
    parts["monitor"].refresh()
    client.post("/api/safety/report", json={"kind": "pain"})
    r = client.post("/api/library/play", json={"id": "cheer"})
    assert r.status_code == 409 and "Safety stop" in r.json()["detail"]


def test_preview_solve_never_touches_the_robot(dash, fake_robot):
    client, parts = dash
    before = len(fake_robot.log)
    version = parts["twin"].state.version
    out = client.post("/api/preview/solve", json={"keyframes": poses.wave(), "hz": 10}).json()
    assert len(out["frames"]) >= 30 and out["total_ms"] == sum(k["ms"] + k.get("hold_ms", 0) for k in poses.wave())
    assert {"angles", "base"} <= set(out["frames"][0])
    assert len(fake_robot.log) == before and parts["twin"].state.version == version
    assert client.post("/api/preview/solve", json={"keyframes": [{"ms": 1}]}).status_code == 422


# Recorder

def test_record_trim_save_and_log_a_session(dash, fake_robot):
    client, parts = dash
    assert client.post("/api/recorder/start", json={}).json()["recording"] is True
    assert fake_robot.torque[8] == 0                       # arms limp, legs still holding
    assert fake_robot.torque[3] == 1
    assert wait_for(lambda: parts["recorder"].state["frames"] >= 12)
    state = client.post("/api/recorder/stop").json()
    assert state["recording"] is False and state["take"] and state["limp"] is False
    assert fake_robot.torque[8] == 1                       # holding again
    take = client.get(f"/api/recorder/takes/{state['take']['id']}").json()
    assert len(take["frames"]) >= 12 and take["simulated"] is True
    end = take["duration_ms"]
    draft = client.post(f"/api/recorder/takes/{take['id']}/draft", json={"start_ms": 200, "end_ms": end}).json()
    check_keyframes(draft["keyframes"])
    assert draft["keyframes"][-1]["servos"] == poses.frame(1200, poses.REST)["servos"]
    r = client.post(f"/api/recorder/takes/{take['id']}/save", json={"name": "My wave", "start_ms": 200, "end_ms": end})
    assert r.status_code == 200
    saved = r.json()
    assert saved["animation"]["id"] == "my_wave" and saved["animation"]["kind"] == "emote"
    rows = client.get("/api/sessions").json()
    row = next(x for x in rows if x["id"] == saved["session"])
    assert row["kind"] == "recording" and row["name"] == "My wave" and row["duration_s"] > 0
    detail = client.get(f"/api/sessions/{saved['session']}").json()
    assert detail["recording"]["animation_id"] == "my_wave" and detail["recording"]["samples"] >= 8
    replay = client.get(f"/api/sessions/{saved['session']}/replay").json()
    assert replay["kind"] == "recording" and len(replay["frames"]) > 5


def test_recorder_refuses_bad_trims_and_names(dash):
    client, parts = dash
    client.post("/api/recorder/start", json={})
    assert client.post("/api/recorder/start", json={}).status_code == 409
    assert wait_for(lambda: parts["recorder"].state["frames"] >= 6)
    tid = client.post("/api/recorder/stop").json()["take"]["id"]
    assert client.post(f"/api/recorder/takes/{tid}/save", json={"name": ""}).status_code == 422
    assert client.post(f"/api/recorder/takes/{tid}/draft", json={"start_ms": 100, "end_ms": 150}).status_code == 422
    assert client.post(f"/api/recorder/takes/{tid}/draft", json={"start_ms": "a"}).status_code == 422
    assert client.get("/api/recorder/takes/nope").status_code == 404


# Generator

def test_mock_generator_reads_the_prompt():
    wave = gen_mod.mock("please wave hello slowly with the left arm, twice")
    assert wave["name"] == "Wave" and wave["matched"] and wave["kind"] == "emote"
    moved = {j for kf in wave["keyframes"] for j, p in kf["servos"].items() if p != poses.frame(300, poses.REST)["servos"][j]}
    assert moved and all(j.startswith("left") for j in moved)
    assert gen_mod.mock("arm circles")["name"] == "Arm circles"
    assert gen_mod.mock("zzzz")["matched"] is False


def test_generate_in_dry_run_uses_the_mock_and_says_so(dash):
    client, _ = dash
    out = client.post("/api/generate", json={"prompt": "reach both arms overhead"}).json()
    assert out["source"] == "mock" and "Dry run" in out["note"]
    assert out["animation"]["name"] == "Overhead reach" and out["animation"]["id"] is None
    check_keyframes(out["animation"]["keyframes"])
    saved = client.post("/api/animations", json=out["animation"]).json()
    assert saved["id"] == "overhead_reach" and saved["source"] == "generated" and saved["prompt"]
    again = client.post("/api/animations", json=out["animation"]).json()
    assert again["id"] == "overhead_reach_2"            # a new save never overwrites
    assert client.post("/api/generate", json={"prompt": ""}).status_code == 422


def test_model_degrees_become_safe_keyframes():
    wild = {"name": "Big", "cue": "Up!", "kind": "exercise", "keyframes": [
        {"seconds": 0.01, "left": {"forward": 900, "side": -50, "elbow": 400}, "right": {"forward": 90, "side": 0, "elbow": 0}},
        {"seconds": 99, "left": {"forward": 10, "side": 0, "elbow": 0}, "right": {}}]}
    draft = gen_mod.from_model(wild)
    check_keyframes(draft["keyframes"])                # every pulse inside robot_api's limits
    assert draft["keyframes"][0]["ms"] == 400 and draft["keyframes"][1]["ms"] == 6000
    assert draft["keyframes"][-1]["servos"] == poses.frame(1500, poses.REST)["servos"]
    for bad in ({}, {"keyframes": []}, {"keyframes": ["x", "y"]}, {"keyframes": [{"seconds": "a"}, {}]}):
        with pytest.raises(ValueError):
            gen_mod.from_model(bad)


def test_generator_uses_the_model_and_falls_back(tmp_path, monkeypatch):
    client, parts = make(tmp_path, stub_llm=False, ollama_url="http://gx10.invalid:11434")
    reply = {"name": "Slow reach", "cue": "Reach up.", "kind": "exercise", "keyframes": [
        {"seconds": 2, "left": {"forward": 120, "side": 0, "elbow": 0}, "right": {"forward": 120, "side": 0, "elbow": 0}},
        {"seconds": 2, "left": {"forward": 0, "side": 0, "elbow": 0}, "right": {"forward": 0, "side": 0, "elbow": 0}}]}
    monkeypatch.setattr(parts["generator"], "ask", lambda prompt: reply)
    out = client.post("/api/generate", json={"prompt": "reach up slowly"}).json()
    assert out["source"] == "gx10" and out["animation"]["name"] == "Slow reach" and out["note"] is None

    def down(prompt):
        raise httpx.ConnectError("no route")
    monkeypatch.setattr(parts["generator"], "ask", down)
    out = client.post("/api/generate", json={"prompt": "wave"}).json()
    assert out["source"] == "mock" and "isn't reachable" in out["note"] and out["animation"]["name"] == "Wave"
    monkeypatch.setattr(parts["generator"], "ask", lambda prompt: {"keyframes": "nope"})
    assert client.post("/api/generate", json={"prompt": "wave"}).json()["source"] == "mock"


def test_generated_motion_respects_the_calibration_profile(dash):
    client, _ = dash
    client.post("/api/calibration", json={"name": "Low ceiling", "activate": True, "servos": {
        "left_shoulder_pitch": {"min": 400, "max": 800}, "right_shoulder_pitch": {"min": 200, "max": 600}}})
    out = client.post("/api/generate", json={"prompt": "overhead reach"}).json()
    pulses = [kf["servos"]["left_shoulder_pitch"] for kf in out["animation"]["keyframes"]]
    assert min(pulses) == 400 and out["animation"]["limited_joints"] > 0


# Coach chat

def seeded(parts):
    return synthetic.seed_sessions(parts["sessions"], count=4, seed=3)


def test_chat_streams_with_the_sessions_numbers(dash):
    client, parts = dash
    sid = seeded(parts)[-1]
    score = parts["sessions"].get(sid)["scores"]["overall"]["score"]
    r = client.post("/api/chat", json={"session_id": sid, "messages": [{"role": "user", "content": "How did it go overall?"}]})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = sse(r.text)
    assert events[0][0] == "meta" and events[0][1]["source"] == "stub" and events[-1][0] == "done"
    text = "".join(d["text"] for k, d in events if k == "delta")
    assert sum(1 for k, _ in events if k == "delta") > 5 and f"{score} out of 100" in text


def test_chat_validates_and_404s(dash):
    client, _ = dash
    assert client.post("/api/chat", json={"messages": []}).status_code == 422
    assert client.post("/api/chat", json={"messages": [{"role": "assistant", "content": "hi"}]}).status_code == 422
    assert client.post("/api/chat", json={"session_id": "nope", "messages": [{"role": "user", "content": "hi"}]}).status_code == 404
    r = client.post("/api/chat", json={"messages": [{"role": "user", "content": "hello"}]})
    assert "Pick a session" in "".join(d["text"] for k, d in sse(r.text) if k == "delta")


def test_chat_falls_back_when_the_model_is_unreachable(tmp_path):
    client, parts = make(tmp_path, stub_llm=False, ollama_url="http://127.0.0.1:9", coach_timeout_s=2)
    sid = seeded(parts)[-1]
    r = client.post("/api/chat", json={"session_id": sid, "messages": [{"role": "user", "content": "what should we work on next?"}]})
    events = sse(r.text)
    assert events[0][1]["source"] == "offline" and "isn't reachable" in events[0][1]["notice"]
    assert events[-1][0] == "done" and "".join(d["text"] for k, d in events if k == "delta").strip()


def test_chat_streams_the_model_and_reports_a_broken_stream(tmp_path, monkeypatch):
    client, parts = make(tmp_path, stub_llm=False)

    class FakeStream:
        def __init__(self, lines):
            self.lines = lines

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def raise_for_status(self):
            pass

        def iter_lines(self):
            for line in self.lines:
                if isinstance(line, Exception):
                    raise line
                yield line

    good = [json.dumps({"message": {"content": "Nice "}}), json.dumps({"message": {"content": "work."}, "done": True})]
    monkeypatch.setattr(chat_mod.httpx, "stream", lambda *a, **k: FakeStream(good))
    events = sse(client.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}]}).text)
    assert [k for k, _ in events] == ["meta", "delta", "delta", "done"] and events[0][1]["source"] == "llm"
    broken = [json.dumps({"message": {"content": "Well "}}), httpx.ReadTimeout("slow")]
    monkeypatch.setattr(chat_mod.httpx, "stream", lambda *a, **k: FakeStream(broken))
    events = sse(client.post("/api/chat", json={"messages": [{"role": "user", "content": "hi"}]}).text)
    assert events[-1][0] == "error" and "didn't answer" in events[-1][1]["message"]


def test_offline_coach_never_diagnoses_and_flags_pain():
    session = S.upgrade(synthetic.generate_session(11, time.time(), 0.4, pain=True))
    text = chat_mod.offline_reply("did anything hurt?", chat_mod.context(session, []))
    assert "follow up" in text and "can't say what caused it" in text


# Sessions and analytics

def test_analytics_fields_are_computed():
    session = S.upgrade(synthetic.generate_session(5, time.time(), 0.6))
    sc = session["scores"]
    assert 0 <= sc["overall"]["score"] <= 100 and sc["overall"]["label"]
    assert {p["key"] for p in sc["overall"]["parts"]} == {"accuracy_pct", "pacing_pct", "smoothness_pct",
                                                          "symmetry_pct", "completion_pct"}
    assert sum(p["weight"] for p in sc["overall"]["parts"]) == 100
    assert 0 <= sc["smoothness_pct"] <= 100 and 0 <= sc["symmetry_pct"] <= 100
    assert [r["joint"] for r in session["rom_by_joint"]] == list(S.SIDE_JOINTS)
    assert session["symmetry"]["favours"] in ("left", "right", "even") and session["insights"]
    s = session["sets"][0]
    assert len(s["heatmap"]["rows"]) == len(s["reps_detail"]) and len(s["heatmap"]["rows"][0]["cells"]) == S.HEAT_BINS
    assert s["tempo"]["mean_s"] > 0 and s["tempo"]["up_s"] > 0
    rep = s["reps_detail"][0]
    assert rep["up_s"] + rep["down_s"] == pytest.approx(rep["duration_s"], abs=0.02)


def test_smoothness_tells_clean_from_shaky():
    import math
    clean = [10 + 70 * (0.5 - 0.5 * math.cos(2 * math.pi * i / 40)) for i in range(41)]
    shaky = [a + (6 if i % 2 else -6) for i, a in enumerate(clean)]
    assert S.smoothness(clean, 10) > 85 > 50 > S.smoothness(shaky, 10)
    assert S.smoothness([1, 2, 3], 10) is None


def test_symmetry_names_the_stronger_side():
    reps = [{"left_rom_deg": 80.0, "right_rom_deg": 60.0, "symmetry_pct": 75} for _ in range(4)]
    sym = S.symmetry(reps)
    assert sym["favours"] == "left" and sym["pct"] == 75 and sym["diff_deg"] == 20.0
    assert S.symmetry([{"n": 1}]) is None


def test_old_sessions_still_score_without_the_new_fields():
    old = {"plan": "p", "started": 1.0, "status": "completed",
           "sets": [{"exercise": "Front arm raises", "set": 1, "reps": 8, "target_reps": 8}]}
    up = S.upgrade(old)
    assert up["scores"]["overall"]["score"] is None and up["symmetry"] is None and up["rom_by_joint"] == []
    assert up["kind"] == "exercise" and up["sets"][0]["heatmap"] is None


def test_sessions_can_be_deleted_replayed_and_trended(dash):
    client, parts = dash
    ids = seeded(parts)
    trends = client.get("/api/analytics/trends").json()
    assert len(trends) == 4 and trends[0]["started"] < trends[-1]["started"] and trends[0]["overall"] is not None
    replay = client.get(f"/api/sessions/{ids[0]}/replay?set=0").json()
    trace = parts["sessions"].get(ids[0])["sets"][0]["trace"]
    assert len(replay["frames"]) == len(trace["patient"]) and replay["hz"] == trace["hz"]
    assert client.get(f"/api/sessions/{ids[0]}/replay?set=99").status_code == 404
    assert client.delete(f"/api/sessions/{ids[0]}").json() == {"deleted": ids[0]}
    assert client.get(f"/api/sessions/{ids[0]}").status_code == 404
    assert client.delete(f"/api/sessions/{ids[0]}").status_code == 404
    assert len(client.get("/api/sessions").json()) == 3


def test_stale_synthetic_sessions_are_remade(dash):
    _, parts = dash
    store = parts["sessions"]
    old = synthetic.generate_session(1, time.time() - 5000)
    old.pop("synthetic_version")
    old["sets"][0]["trace"].pop("joints")
    store.save("synthetic-old", old)
    store.save("real-one", {"plan": "p", "started": 5.0, "status": "completed", "sets": []})
    assert synthetic.refresh(store) is True
    ids = store.ids()
    assert "synthetic-old" not in ids and "real-one" in ids and len(ids) == 9
    assert synthetic.refresh(store) is False
