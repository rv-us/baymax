"""
The session log's schema (version 2) and every score derived from it.

loop.py saves one JSON file per session. Version 1 had, per set, only `reps`,
`target_reps`, `deviations`, `answer` and `trouble`. Version 2 adds the fields below.
All of them are optional, so a version 1 file is a valid version 2 file with less in it.

STORED (written by the workout loop, the tracker, or the synthetic generator)

    session
      schema        2
      plan          plan name
      started/ended epoch seconds
      status        completed | not_started | stopped_pain | stopped_operator | no_response | error
      flagged       the therapist should look at this session
      synthetic     true when the data is generated, not measured
      patient       { name?, weight_kg? }
      transitions   [{ t, from, event, to, seconds }]            (unchanged from v1)
      notes         [text]                                       (unchanged from v1)
      events        [{ t, type, set?, detail?, transcript? }]    t = seconds since `started`
                    type: left_frame | returned | prompt_closer | prompt_back | side_step |
                          pain_word | fall | safety_stop | operator_stop | pause | resume
      sets          [set]
      interrupted   [set]   sets cut short by a stop (never reviewed, so not in `sets`)
      summary       cached coach summary (dashboard/coach.py)

    set
      exercise, set, reps, target_reps, deviations, answer, trouble   (v1)
      track_joint   the joint whose cycles count as reps
      target        { top, bottom, tolerance, tempo_s }   degrees, seconds per rep
      started_t     seconds since session start
      seconds       how long the set took
      finished      false if interrupted
      trace         { hz, patient: [deg | null], reference: [deg] }
                    Both lists share one time axis starting at started_t; sample i is at
                    started_t + i / hz. null = no pose that frame. `reference` is the
                    demonstrated motion. Optional, and downsampled (10 Hz is plenty).
      reps_detail   [rep]   optional. A tracker that counts reps itself writes it;
                    otherwise it is derived from `trace` by reps_from_trace().

    rep
      n, t_start, t_end     seconds since the set started
      peak_deg, trough_deg  extremes of the tracked joint

DERIVED (never stored; computed by upgrade() so there is one definition of each)

    rep     duration_s, rom_deg, rom_target_deg, rom_ratio, accuracy_pct, pace_ratio,
            misses: [short | over | not_lowered | fast | slow], quality: good | off
    set     scores { accuracy_pct, pacing_pct, consistency_pct, tempo_match_pct,
                     fatigue { label, rom_change_pct, duration_change_pct } }
            bands  [{ t0, t1, quality: good | off | lost, rep? }]   each rep's stretch of
                   the trace, and stretches with no pose
    session scores { accuracy_pct, pacing_pct, fatigue, reps, target_reps, active_s }

accuracy_pct  range of motion reached against the target range, capped at 100.
pacing_pct    half consistency (how even the rep durations are), half how close each
              rep's duration is to tempo_s.
fatigue       last third of the reps against the first third: "tiring" when range
              drops 10 % or reps slow 20 %, "warming_up" when range grows 10 %.

A real pose tracker only has to fill SetResult.trace (or rep_details). Nothing in the
dashboard reads anything that this file does not define.
"""

import statistics

SCHEMA = 2

EVENT_TYPES = ("left_frame", "returned", "prompt_closer", "prompt_back", "side_step",
               "pain_word", "fall", "safety_stop", "operator_stop", "pause", "resume")
SAFETY_EVENTS = ("pain_word", "fall", "safety_stop", "operator_stop")

# Light-effort MET values (Compendium of Physical Activities: 2.3 = light stretching /
# seated calisthenics). An estimate only.
MET = {"shoulder_elevation": 2.3, "elbow": 2.0, "knee": 2.8, "trunk_lean": 2.3, "hip_shift": 2.3}
MET_DEFAULT = 2.3
PACE_FAST, PACE_SLOW = 0.7, 1.4       # rep duration / tempo outside this is "fast" / "slow"
FATIGUE_ROM_DROP, FATIGUE_SLOWDOWN = 0.10, 0.20


def set_target(exercise) -> dict:
    """What a set is scored against, copied into the log so it reads without the plan."""
    t = (exercise.targets or {}).get(exercise.track_joint) or {}
    out = {"tempo_s": float(getattr(exercise, "tempo_s", 4.0))}
    for key in ("top", "bottom", "tolerance"):
        if isinstance(t.get(key), (int, float)):
            out[key] = float(t[key])
    return out


def set_record(exercise, result, started: float) -> dict:
    """The version 2 fields for one set, from the tracker's SetResult."""
    rec = {"track_joint": exercise.track_joint, "target": set_target(exercise),
           "seconds": round(result.seconds, 2), "finished": result.finished}
    if result.started:
        rec["started_t"] = round(result.started - started, 2)
    if result.trace:
        rec["trace"] = result.trace
    if result.rep_details:
        rec["reps_detail"] = result.rep_details
    return rec


def reference_trace(target: dict, reps: int, hz: float) -> list:
    """The demonstrated motion for a set: smooth cycles between bottom and top."""
    import math

    top, bottom = target.get("top", 90.0), target.get("bottom", 0.0)
    n = int(round(reps * target.get("tempo_s", 4.0) * hz))
    return [round(bottom + (top - bottom) * (0.5 - 0.5 * math.cos(2 * math.pi * i / hz / target.get("tempo_s", 4.0))), 1)
            for i in range(n)]


# ---- reps from a trace ------------------------------------------------------------


def reps_from_trace(trace: dict, target: dict) -> list:
    """Count reps in an angle trace: one rep is a rise past 40 % of the target range
    and a return below 20 %. Missing samples (null) are skipped."""
    hz = float(trace.get("hz") or 10)
    samples = trace.get("patient") or []
    top, bottom = target.get("top"), target.get("bottom")
    known = [a for a in samples if a is not None]
    if not known:
        return []
    if top is None or bottom is None:
        bottom, top = min(known), max(known)
    span = top - bottom
    if span <= 0:
        return []
    lo, hi = bottom + 0.2 * span, bottom + 0.4 * span
    reps, start, peak, trough, up = [], None, None, None, False
    for i, a in enumerate(samples):
        if a is None:
            continue
        if start is None:
            trough = a if trough is None else min(trough, a)
            if a > lo:
                start, peak, up = i, a, a >= hi
            continue
        peak = max(peak, a)
        up = up or a >= hi
        if a <= lo:
            if up:
                reps.append({"n": len(reps) + 1, "t_start": round(start / hz, 2),
                             "t_end": round(i / hz, 2), "peak_deg": round(peak, 1),
                             "trough_deg": round(trough, 1)})
            start, trough = None, a
    return reps


# ---- scores -----------------------------------------------------------------------


def _pct(x: float) -> int:
    return int(round(100 * max(0.0, min(1.0, x))))


def score_rep(rep: dict, target: dict) -> dict:
    out = dict(rep)
    out["duration_s"] = round(rep["t_end"] - rep["t_start"], 2)
    out["rom_deg"] = round(rep["peak_deg"] - rep["trough_deg"], 1)
    misses = []
    top, bottom, tol = target.get("top"), target.get("bottom"), target.get("tolerance", 15.0)
    if top is not None and bottom is not None and top > bottom:
        out["rom_target_deg"] = round(top - bottom, 1)
        out["rom_ratio"] = round(out["rom_deg"] / (top - bottom), 3)
        out["accuracy_pct"] = _pct(out["rom_ratio"])
        if rep["peak_deg"] < top - tol:
            misses.append("short")
        elif rep["peak_deg"] > top + tol:
            misses.append("over")
        if rep["trough_deg"] > bottom + tol:
            misses.append("not_lowered")
    tempo = target.get("tempo_s")
    if tempo and out["duration_s"] > 0:
        out["pace_ratio"] = round(out["duration_s"] / tempo, 2)
        if out["pace_ratio"] < PACE_FAST:
            misses.append("fast")
        elif out["pace_ratio"] > PACE_SLOW:
            misses.append("slow")
    out["misses"] = misses
    out["quality"] = "off" if misses else "good"
    return out


def _thirds(values: list):
    """Mean of the first and last third (at least one value each)."""
    k = max(1, len(values) // 3)
    return statistics.fmean(values[:k]), statistics.fmean(values[-k:])


def fatigue(reps: list) -> dict:
    """Last third of the reps against the first third."""
    # Shares of the target, so reps of different exercises can be compared.
    roms = [r.get("rom_ratio") or r["rom_deg"] for r in reps if r.get("rom_deg")]
    durs = [r.get("pace_ratio") or r["duration_s"] for r in reps if r.get("duration_s")]
    if len(roms) < 4:
        return {"label": "not_enough_reps", "rom_change_pct": None, "duration_change_pct": None}
    r0, r1 = _thirds(roms)
    d0, d1 = _thirds(durs)
    rom, dur = (r1 - r0) / r0, (d1 - d0) / d0
    label = ("tiring" if rom <= -FATIGUE_ROM_DROP or dur >= FATIGUE_SLOWDOWN
             else "warming_up" if rom >= FATIGUE_ROM_DROP else "steady")
    return {"label": label, "rom_change_pct": round(100 * rom), "duration_change_pct": round(100 * dur)}


def score_reps(reps: list) -> dict:
    """Scores for scored reps (one set, or every rep of a session)."""
    acc = [r["accuracy_pct"] for r in reps if "accuracy_pct" in r]
    durs = [r["duration_s"] for r in reps if r["duration_s"] > 0]
    pace = [r["pace_ratio"] for r in reps if "pace_ratio" in r]
    out = {"accuracy_pct": round(statistics.fmean(acc)) if acc else None,
           "consistency_pct": None, "tempo_match_pct": None, "pacing_pct": None,
           "fatigue": fatigue(reps)}
    if len(durs) >= 2:
        cv = statistics.pstdev(durs) / statistics.fmean(durs)
        out["consistency_pct"] = _pct(1 - cv / 0.5)
    if pace:
        import math

        out["tempo_match_pct"] = _pct(statistics.fmean(1 - min(1.0, abs(math.log2(p))) for p in pace))
    parts = [p for p in (out["consistency_pct"], out["tempo_match_pct"]) if p is not None]
    if parts:
        out["pacing_pct"] = round(statistics.fmean(parts))
    return out


def trace_bands(trace: dict, reps: list) -> list:
    """Stretches of a set's time axis to shade: each scored rep ("good" or "off"), and
    any half second or more with no pose ("lost")."""
    hz = float(trace.get("hz") or 10)
    bands = [{"t0": r["t_start"], "t1": r["t_end"], "quality": r["quality"], "rep": r["n"]}
             for r in reps]
    run = None
    for i, a in enumerate((trace.get("patient") or []) + [0]):
        if a is None:
            run = i if run is None else run
        elif run is not None:
            if i - run >= hz / 2:
                bands.append({"t0": round(run / hz, 2), "t1": round(i / hz, 2), "quality": "lost"})
            run = None
    return sorted(bands, key=lambda b: b["t0"])


def upgrade_set(s: dict) -> dict:
    out = dict(s)
    target = dict(s.get("target") or {})
    out["target"] = target
    out.setdefault("deviations", [])
    raw = s.get("reps_detail")
    if raw is None and s.get("trace"):
        raw = reps_from_trace(s["trace"], target)
    out["reps_detail"] = [score_rep(r, target) for r in raw or []]
    out["scores"] = score_reps(out["reps_detail"])
    out["bands"] = trace_bands(s["trace"], out["reps_detail"]) if s.get("trace") else []
    return out


def upgrade(session: dict) -> dict:
    """A session file (version 1 or 2) with every optional field present and every
    derived field computed. The input is not changed."""
    out = dict(session)
    out["schema"] = SCHEMA
    for key in ("events", "notes", "transitions", "interrupted"):
        out[key] = list(session.get(key) or [])
    out["synthetic"] = bool(session.get("synthetic"))
    out["patient"] = dict(session.get("patient") or {})
    out["sets"] = [upgrade_set(s) for s in session.get("sets") or []]
    out["interrupted"] = [upgrade_set(s) for s in out["interrupted"]]
    reps = [r for s in out["sets"] + out["interrupted"] for r in s["reps_detail"]]
    scores = score_reps(reps)
    scores["reps"] = sum(s.get("reps", 0) for s in out["sets"] + out["interrupted"])
    scores["target_reps"] = sum(s.get("target_reps", 0) for s in out["sets"] + out["interrupted"])
    scores["active_s"] = round(sum(s.get("seconds") or 0 for s in out["sets"] + out["interrupted"]), 1)
    scores["safety_events"] = sum(1 for e in out["events"] if e.get("type") in SAFETY_EVENTS)
    out["scores"] = scores
    return out


def calories(session: dict, weight_kg: float) -> dict:
    """A rough estimate: MET x weight (kg) x active hours, with light-effort MET values."""
    kcal = sum(MET.get(s.get("track_joint"), MET_DEFAULT) * weight_kg * (s.get("seconds") or 0) / 3600
               for s in (session.get("sets") or []) + (session.get("interrupted") or []))
    return {"kcal": round(kcal, 1), "weight_kg": weight_kg, "estimate": True}
