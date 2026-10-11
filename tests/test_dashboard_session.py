"""Session schema (workout/session.py), scoring, and the synthetic generator."""
import json
import math
import time
import types

import pytest

from dashboard import store, synthetic
from robot_api.api import LocalBackend
from workout import session as S
from workout.loop import Settings, Workout
from workout.parts import ScriptedEars, SetResult, TemplateCoach
from workout.plan import load_plan
from workout.robot import Robot

TARGET = {"top": 90.0, "bottom": 10.0, "tolerance": 15.0, "tempo_s": 4.0}


def wave(reps, peak=90.0, trough=10.0, seconds=4.0, hz=10):
    n = int(seconds * hz)
    one = [trough + (peak - trough) * (0.5 - 0.5 * math.cos(2 * math.pi * i / n)) for i in range(n)]
    return [trough] * 5 + one * reps + [trough] * 5


def rep(n, peak=90.0, trough=10.0, start=0.0, seconds=4.0):
    return {"n": n, "t_start": start, "t_end": start + seconds, "peak_deg": peak, "trough_deg": trough}


# Schema

def test_a_version_1_log_still_loads():
    v1 = {"plan": "Arms, basic", "started": 1.0, "status": "completed", "flagged": False,
          "sets": [{"exercise": "Front arm raises", "set": 1, "reps": 8, "target_reps": 8,
                    "deviations": ["elbows bent"], "answer": "fine", "trouble": "none"}],
          "transitions": [], "notes": [], "ended": 60.0}
    s = S.upgrade(v1)
    assert s["schema"] == 2 and s["events"] == [] and s["synthetic"] is False
    assert s["sets"][0]["reps_detail"] == [] and s["sets"][0]["bands"] == []
    assert s["sets"][0]["scores"]["accuracy_pct"] is None
    assert s["scores"]["reps"] == 8 and s["scores"]["target_reps"] == 8
    assert "schema" not in v1  # the stored file is not touched


def test_reps_are_counted_from_a_trace():
    trace = {"hz": 10, "patient": wave(5)}
    reps = S.reps_from_trace(trace, TARGET)
    assert [r["n"] for r in reps] == [1, 2, 3, 4, 5]
    assert all(abs(r["peak_deg"] - 90) < 1 and r["trough_deg"] <= 12 for r in reps)
    assert all(2.5 < r["t_end"] - r["t_start"] < 4.0 for r in reps)


def test_dropped_frames_and_half_reps_do_not_count():
    samples = wave(3)
    samples[20:23] = [None, None, None]
    assert len(S.reps_from_trace({"hz": 10, "patient": samples}, TARGET)) == 3
    wiggle = wave(4, peak=30.0)  # never gets 40 % of the way up
    assert S.reps_from_trace({"hz": 10, "patient": wiggle}, TARGET) == []
    assert S.reps_from_trace({"hz": 10, "patient": [None] * 30}, TARGET) == []


# Scoring

@pytest.mark.parametrize("kwargs, misses, accuracy", [
    ({}, [], 100),
    ({"peak": 60.0}, ["short"], 62),
    ({"peak": 110.0}, ["over"], 100),
    ({"trough": 30.0}, ["not_lowered"], 75),
    ({"seconds": 2.0}, ["fast"], 100),
    ({"seconds": 6.5}, ["slow"], 100),
    ({"peak": 60.0, "seconds": 7.0}, ["short", "slow"], 62),
])
def test_rep_scoring(kwargs, misses, accuracy):
    r = S.score_rep(rep(1, **kwargs), TARGET)
    assert r["misses"] == misses and r["accuracy_pct"] == accuracy
    assert r["quality"] == ("off" if misses else "good")
    assert r["rom_target_deg"] == 80.0


def test_pacing_rewards_even_reps_at_the_demo_tempo():
    even = S.score_reps([S.score_rep(rep(i, seconds=4.0), TARGET) for i in range(1, 7)])
    uneven = S.score_reps([S.score_rep(rep(i, seconds=s), TARGET)
                           for i, s in enumerate((2.0, 6.0, 3.0, 7.0, 2.5, 5.0), start=1)])
    rushed = S.score_reps([S.score_rep(rep(i, seconds=2.0), TARGET) for i in range(1, 7)])
    assert even["pacing_pct"] == 100 and even["consistency_pct"] == 100
    assert uneven["pacing_pct"] < 60
    assert rushed["consistency_pct"] == 100 and rushed["tempo_match_pct"] == 0


def test_fatigue_trend():
    def label(peaks, seconds=None):
        seconds = seconds or [4.0] * len(peaks)
        return S.fatigue([S.score_rep(rep(i + 1, peak=p, seconds=s), TARGET)
                          for i, (p, s) in enumerate(zip(peaks, seconds))])["label"]

    assert label([90, 90, 88, 80, 72, 66]) == "tiring"
    assert label([90] * 6, [4, 4, 4, 5, 5.5, 6]) == "tiring"
    assert label([70, 72, 80, 88, 90, 90]) == "warming_up"
    assert label([90, 89, 91, 90, 88, 90]) == "steady"
    assert label([90, 60]) == "not_enough_reps"


def test_bands_shade_each_rep_and_lost_stretches():
    samples = [None] * 10 + wave(2, peak=90.0) + wave(1, peak=55.0)
    s = S.upgrade_set({"exercise": "X", "set": 1, "reps": 3, "target_reps": 3, "target": TARGET,
                       "trace": {"hz": 10, "patient": samples, "reference": samples}})
    kinds = [b["quality"] for b in s["bands"]]
    assert kinds[0] == "lost" and kinds.count("good") == 2 and kinds[-1] == "off"


def test_calories_are_met_times_weight_times_hours():
    session = {"sets": [{"track_joint": "shoulder_elevation", "seconds": 1800}]}
    assert S.calories(session, 70) == {"kcal": 80.5, "weight_kg": 70, "estimate": True}


# The workout loop writes the new fields

class TraceTracker:
    def patient_visible(self):
        return True

    def run_set(self, exercise, post_event, cancelled, reps_done=0):
        target = S.set_target(exercise)
        samples = wave(exercise.reps, peak=target["top"], trough=target["bottom"], seconds=target["tempo_s"])
        return SetResult(exercise.reps, exercise.reps, [], len(samples) / 10, started=time.time(),
                         trace={"hz": 10, "patient": samples, "reference": samples})


def test_the_loop_saves_version_2_sets(tmp_path, monkeypatch):
    from robot_api.servo import bus

    monkeypatch.setattr(bus, "time", types.SimpleNamespace(
        sleep=lambda s: None, time=time.time, monotonic=time.monotonic))
    w = Workout(load_plan("arms_basic"), Robot(LocalBackend()),
                ScriptedEars(["yes"] + ["no, it was fine"] * 4), TraceTracker(), TemplateCoach(),
                Settings(ready_wait_s=0, countdown=False, sessions_dir=str(tmp_path)), sleep=lambda s: None)
    w.run()
    saved = json.load(open(w.saved_path))
    assert saved["schema"] == 2 and saved["events"] == [] and saved["interrupted"] == []
    first = saved["sets"][0]
    assert first["target"] == {"tempo_s": 4.0, "top": 90.0, "bottom": 15.0, "tolerance": 15.0}
    assert first["track_joint"] == "shoulder_elevation" and first["finished"] and "trace" in first
    scored = S.upgrade(saved)
    assert [len(s["reps_detail"]) for s in scored["sets"]] == [8, 8, 6, 6]
    assert scored["scores"]["accuracy_pct"] == 100 and scored["scores"]["fatigue"]["label"] == "steady"


# Synthetic sessions

def test_synthetic_sessions_are_seeded_and_marked():
    a = synthetic.generate_session(42, started=1000.0)
    assert a == synthetic.generate_session(42, started=1000.0)
    assert a != synthetic.generate_session(43, started=1000.0)
    assert a["synthetic"] is True and a["schema"] == 2 and a["status"] == "completed"
    s = S.upgrade(a)
    assert len(s["sets"]) == 4 and all(x["reps_detail"] for x in s["sets"])
    assert 50 <= s["scores"]["accuracy_pct"] <= 100
    assert all(len(x["trace"]["patient"]) == len(x["trace"]["reference"]) for x in s["sets"])


def test_synthetic_progress_pain_and_lost_frames():
    early = S.upgrade(synthetic.generate_session(5, 0.0, progress=0.0))
    late = S.upgrade(synthetic.generate_session(5, 0.0, progress=1.0))
    assert late["scores"]["accuracy_pct"] > early["scores"]["accuracy_pct"]

    hurt = S.upgrade(synthetic.generate_session(5, 0.0, pain=True))
    assert hurt["status"] == "stopped_pain" and hurt["flagged"]
    assert len(hurt["interrupted"]) == 1 and hurt["interrupted"][0]["finished"] is False
    assert [e["type"] for e in hurt["events"]][-2:] == ["pain_word", "safety_stop"]
    assert hurt["events"][-2]["transcript"] and hurt["scores"]["safety_events"] == 2

    away = S.upgrade(synthetic.generate_session(5, 0.0, left_frame=True))
    assert [e["type"] for e in away["events"] if e["type"] in ("left_frame", "returned")] == \
        ["left_frame", "returned"]
    assert any(b["quality"] == "lost" for b in away["sets"][0]["bands"])


def test_seeding_writes_files_the_store_lists(tmp_path):
    sessions = store.Sessions(str(tmp_path))
    ids = synthetic.seed_sessions(sessions, count=5, seed=3, now=1_800_000_000)
    rows = sessions.list()
    assert len(rows) == 5 and {r["id"] for r in rows} == set(ids)
    assert all(r["synthetic"] and r["has_detail"] for r in rows)
    assert rows == sorted(rows, key=lambda r: r["started"], reverse=True)
    assert "trace" not in json.dumps(rows)  # the list stays light
    assert sessions.delete_synthetic() == 5 and sessions.list() == []
