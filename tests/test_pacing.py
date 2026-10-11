"""The robot stays about a quarter-rep ahead of the patient and never further."""
import random

from dashboard import preview, synthetic
from workout import pacing

TARGET = {"top": 90.0, "bottom": 10.0, "tempo_s": 4.0}
DT = 1 / synthetic.HZ


def run(angles, reps=4):
    est, leader = pacing.ProgressEstimator(TARGET), pacing.PaceLeader(4.0, reps)
    rows = []
    for a in angles:
        p = est.update(a)
        leader.step(p, DT)
        rows.append((p, leader.progress))
    return rows


def cosine_reps(reps, seconds, rng=None):
    import math
    n = int(seconds * synthetic.HZ)
    return [10 + 80 * (0.5 - 0.5 * math.cos(2 * math.pi * i / n)) for _ in range(reps) for i in range(n)]


def test_the_robot_leads_by_a_quarter_rep_and_never_more():
    rows = run(cosine_reps(4, 4.0))
    gaps = [r - p for p, r in rows]
    assert max(gaps) <= pacing.LEAD + 1e-9
    assert all(abs(g - pacing.LEAD) < 0.05 for g in gaps[len(gaps) // 4:len(gaps) - 10])


def test_a_patient_who_stops_gets_a_robot_that_waits():
    rows = run(cosine_reps(1, 4.0)[:20] + [cosine_reps(1, 4.0)[19]] * 200)  # stalls mid-lift
    p, r = rows[-1]
    assert r - p <= pacing.LEAD + 1e-9
    assert rows[-1][1] == rows[-50][1]          # stopped moving: it holds, it doesn't run off


def test_a_slow_patient_is_not_outrun_even_at_half_speed():
    rows = run(cosine_reps(3, 8.0), reps=3)
    assert max(r - p for p, r in rows) <= pacing.LEAD + 1e-9
    assert rows[-1][0] > 2.8


def test_lost_frames_and_noise_do_not_push_progress_backwards():
    rng = random.Random(3)
    angles = [a + rng.gauss(0, 1.5) if rng.random() > 0.1 else None for a in cosine_reps(3, 4.0)]
    ps = [p for p, _ in run(angles, reps=3)]
    assert ps == sorted(ps) and ps[-1] > 2.5


def test_the_robot_never_runs_past_the_last_rep():
    assert max(r for _, r in run(cosine_reps(2, 4.0), reps=2)) <= 2.0


def test_the_preview_loops_every_exercise_and_skips_those_it_cannot_draw():
    exs = [{"demo": {"arm": "forward", "times": 1}}, {"demo": {"action_group": "wave"}}, {"demo": {"arm": "up"}}]
    a = preview.session_pose(exs, 0)
    assert a["index"] == 0 and a["pose"]["servos"]
    total = a["total_ms"]
    assert preview.session_pose(exs, total + 500)["index"] == 0           # looped
    assert {preview.session_pose(exs, t)["index"] for t in range(0, total, 200)} == {0, 2}
    assert preview.session_pose([{"demo": {"action_group": "wave"}}], 0)["pose"] is None


def test_the_robot_follows_every_rep_of_a_simulated_patient():
    """The dashboard's own patient: a range that falls short of the target, noise, dropped frames."""
    for seed in range(40):
        rng = random.Random(seed)
        target = {"top": 90.0, "bottom": 15.0, "tempo_s": 4.0}
        trace = synthetic.patient_trace(rng, target, 6, synthetic.profile(rng, progress=0.8))
        est, leader = pacing.ProgressEstimator(target), pacing.PaceLeader(4.0, 6)
        for a in trace:
            leader.step(est.update(a), DT)
        assert est.progress > 5.5, (seed, est.progress)
        assert leader.progress >= 5.9, (seed, leader.progress)
        assert leader.progress - est.progress <= pacing.LEAD + 1e-9
