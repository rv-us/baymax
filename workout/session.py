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
      trace         { hz, patient: [deg | null], reference: [deg], joints?: {name: [deg | null]} }
                    All lists share one time axis starting at started_t; sample i is at
                    started_t + i / hz. null = no pose that frame. `reference` is the
                    demonstrated motion. Optional, and downsampled (10 Hz is plenty).
                    `joints` (optional) holds each side on its own: left_shoulder,
                    right_shoulder, left_elbow, right_elbow. Symmetry and per-joint range
                    come from it; without it they read "not measured".
      reps_detail   [rep]   optional. A tracker that counts reps itself writes it;
                    otherwise it is derived from `trace` by reps_from_trace().

    rep
      n, t_start, t_end     seconds since the set started
      peak_deg, trough_deg  extremes of the tracked joint

DERIVED (never stored; computed by upgrade() so there is one definition of each)

    rep     duration_s, rom_deg, rom_target_deg, rom_ratio, accuracy_pct, pace_ratio,
            up_s, down_s, smoothness_pct, symmetry_pct, left_rom_deg, right_rom_deg,
            misses: [short | over | not_lowered | fast | slow], quality: good | off
    set     scores { accuracy_pct, pacing_pct, consistency_pct, tempo_match_pct,
                     smoothness_pct, symmetry_pct, completion_pct,
                     fatigue { label, rom_change_pct, duration_change_pct },
                     overall { score, label, parts: [{ key, label, value, weight }] } }
            bands  [{ t0, t1, quality: good | off | lost, rep? }]   each rep's stretch of
                   the trace, and stretches with no pose
            rom_by_joint [{ joint, min_deg, max_deg, rom_deg, target_rom_deg?, pct? }]
            symmetry { pct, left_rom_deg, right_rom_deg, diff_deg, favours: left | right | even }
            tempo  { target_s, mean_s, up_s, down_s, spread_pct }
            heatmap { bins, rows: [{ rep, cells: [deg | null] }] }   patient minus the ideal
                   rep, by phase of the rep; positive = above the demonstration
    session scores { ...as a set, plus reps, target_reps, active_s, safety_events }
            kind (exercise | recording), rom_by_joint, symmetry,
            insights: [{ tone: good | warn | bad | info, text }]

smoothness_pct  how close each rep is to one clean rise and fall. The speed of a smooth
                rep goes up once and down once; stop-start or shaky reps change speed
                more. 100 x (ideal speed change / measured speed change), after a light
                3-sample smoothing. Tracker jitter still counts a little, so a clean
                rep from a 10 Hz tracker reads 50 to 70, not 100.
symmetry_pct    100 x (1 - |left range - right range| / the larger range), per rep.
overall.score   weighted mean of what was measured: range 35, pacing 20, smoothness 15,
                symmetry 15, reps completed 15. Parts that weren't measured drop out and
                the rest are re-weighted.

accuracy_pct  range of motion reached against the target range, capped at 100.
pacing_pct    half consistency (how even the rep durations are), half how close each
              rep's duration is to tempo_s.
fatigue       last third of the reps against the first third: "tiring" when range
              drops 10 % or reps slow 20 %, "warming_up" when range grows 10 %.

A real pose tracker only has to fill SetResult.trace (or rep_details). Nothing in the
dashboard reads anything that this file does not define.
"""

import math
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
    reps, start, peak, peak_i, trough, up = [], None, None, 0, None, False
    for i, a in enumerate(samples):
        if a is None:
            continue
        if start is None:
            trough = a if trough is None else min(trough, a)
            if a > lo:
                start, peak, peak_i, up = i, a, i, a >= hi
            continue
        if a > peak:
            peak, peak_i = a, i
        up = up or a >= hi
        if a <= lo:
            if up:
                reps.append({"n": len(reps) + 1, "t_start": round(start / hz, 2),
                             "t_end": round(i / hz, 2), "t_peak": round(peak_i / hz, 2),
                             "window": "threshold",
                             "peak_deg": round(peak, 1), "trough_deg": round(trough, 1)})
            start, trough = None, a
    return reps


# ---- scores -----------------------------------------------------------------------


def _pct(x: float) -> int:
    return int(round(100 * max(0.0, min(1.0, x))))


def _slice(samples: list, hz: float, t0: float, t1: float) -> list:
    return samples[max(0, int(round(t0 * hz))):int(round(t1 * hz)) + 1]


def smoothness(samples: list, hz: float, windowed: bool = False):
    """How close a rep is to one clean rise and fall, 0 to 100. None if too short.
    `windowed`: the samples run between the rep's 20 % crossings, not rest to rest."""
    vals = [a for a in samples if a is not None]
    if len(vals) < 6:
        return None
    vals = [statistics.fmean(vals[max(0, i - 1):i + 2]) for i in range(len(vals))]
    speed = [(b - a) * hz for a, b in zip(vals, vals[1:])]
    change = sum(abs(b - a) for a, b in zip(speed, speed[1:]))
    # The speed change of one smooth (cosine) rise and fall through this range in this
    # time. Rest to rest the speed goes 0 -> peak -> -peak -> 0, which is 2 pi x the
    # mean speed. Between the 20 % crossings it starts and ends already moving.
    rise, fall = max(vals) - vals[0], max(vals) - vals[-1]
    scale = SMOOTH_WINDOWED if windowed else 2.0
    ideal = scale * math.pi * (rise + fall) * hz / (len(vals) - 1)
    if change <= 0 or ideal <= 0:
        return None
    return _pct(ideal / change)


def _range(samples: list):
    vals = [a for a in samples if a is not None]
    return round(max(vals) - min(vals), 1) if len(vals) >= 2 else None


def score_rep(rep: dict, target: dict, trace: dict | None = None) -> dict:
    out = dict(rep)
    out["duration_s"] = round(rep["t_end"] - rep["t_start"], 2)
    if rep.get("t_peak") is not None:
        out["up_s"] = round(rep["t_peak"] - rep["t_start"], 2)
        out["down_s"] = round(rep["t_end"] - rep["t_peak"], 2)
    if trace:
        hz = float(trace.get("hz") or 10)
        smooth = smoothness(_slice(trace.get("patient") or [], hz, rep["t_start"], rep["t_end"]), hz,
                            rep.get("window") == "threshold")
        if smooth is not None:
            out["smoothness_pct"] = smooth
        joints = trace.get("joints") or {}
        left = _range(_slice(joints.get("left_shoulder") or [], hz, rep["t_start"], rep["t_end"]))
        right = _range(_slice(joints.get("right_shoulder") or [], hz, rep["t_start"], rep["t_end"]))
        if left and right:
            out["left_rom_deg"], out["right_rom_deg"] = left, right
            out["symmetry_pct"] = _pct(1 - abs(left - right) / max(left, right))
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
        # A rep counted from a trace is timed between its 20 % crossings, which is
        # THRESHOLD_SHARE of a smooth cycle, so the tempo is scaled to match.
        expected = tempo * (THRESHOLD_SHARE if rep.get("window") == "threshold" else 1.0)
        out["pace_ratio"] = round(out["duration_s"] / expected, 2)
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
    for key in ("smoothness_pct", "symmetry_pct"):
        vals = [r[key] for r in reps if key in r]
        out[key] = round(statistics.fmean(vals)) if vals else None
    return out


OVERALL_PARTS = (("accuracy_pct", "Range reached", 35), ("pacing_pct", "Pacing", 20),
                 ("smoothness_pct", "Smoothness", 15), ("symmetry_pct", "Left and right balance", 15),
                 ("completion_pct", "Reps completed", 15))
OVERALL_LABELS = ((85, "Strong session"), (70, "Solid session"), (50, "Mixed session"), (0, "Tough session"))


def overall(scores: dict) -> dict:
    """One headline number from whatever was measured; missing parts drop out."""
    parts = [{"key": k, "label": label, "value": scores[k], "weight": w}
             for k, label, w in OVERALL_PARTS if scores.get(k) is not None]
    if not [p for p in parts if p["key"] != "completion_pct"]:
        return {"score": None, "label": "Not scored", "parts": []}
    score = round(sum(p["value"] * p["weight"] for p in parts) / sum(p["weight"] for p in parts))
    return {"score": score, "label": next(text for floor, text in OVERALL_LABELS if score >= floor),
            "parts": parts}


def completion(reps: int, target_reps: int):
    return _pct(reps / target_reps) if target_reps else None


SIDE_JOINTS = ("left_shoulder", "right_shoulder", "left_elbow", "right_elbow")


def rom_by_joint(trace: dict, target: dict) -> list:
    """Range each measured joint moved through over the set."""
    rows = []
    span = (target["top"] - target["bottom"]) if target.get("top") is not None \
        and target.get("bottom") is not None else None
    for joint in SIDE_JOINTS:
        vals = [a for a in (trace.get("joints") or {}).get(joint) or [] if a is not None]
        if len(vals) < 2:
            continue
        row = {"joint": joint, "min_deg": round(min(vals), 1), "max_deg": round(max(vals), 1),
               "rom_deg": round(max(vals) - min(vals), 1)}
        if span and joint.endswith("shoulder"):
            row["target_rom_deg"] = round(span, 1)
            row["pct"] = _pct(row["rom_deg"] / span)
        rows.append(row)
    return rows


def symmetry(reps: list):
    """Left against right shoulder range over the scored reps."""
    pairs = [(r["left_rom_deg"], r["right_rom_deg"]) for r in reps if "symmetry_pct" in r]
    if not pairs:
        return None
    left, right = statistics.fmean(p[0] for p in pairs), statistics.fmean(p[1] for p in pairs)
    diff = left - right
    return {"pct": round(statistics.fmean(r["symmetry_pct"] for r in reps if "symmetry_pct" in r)),
            "left_rom_deg": round(left, 1), "right_rom_deg": round(right, 1), "diff_deg": round(diff, 1),
            "favours": "even" if abs(diff) < 0.05 * max(left, right) else "left" if diff > 0 else "right"}


def tempo(reps: list, target: dict):
    durs = [r["duration_s"] for r in reps if r.get("duration_s", 0) > 0]
    if not durs:
        return None
    ups, downs = [r["up_s"] for r in reps if "up_s" in r], [r["down_s"] for r in reps if "down_s" in r]
    mean = statistics.fmean(durs)
    return {"target_s": target.get("tempo_s"), "mean_s": round(mean, 2),
            "up_s": round(statistics.fmean(ups), 2) if ups else None,
            "down_s": round(statistics.fmean(downs), 2) if downs else None,
            "spread_pct": round(100 * statistics.pstdev(durs) / mean) if len(durs) > 1 else 0}


HEAT_BINS = 8
SMOOTH_GOOD = 50   # a clean rep read through ordinary 10 Hz tracker jitter lands a little above this
THRESHOLD_PHASE = math.acos(1 - 2 * 0.2) / (2 * math.pi)   # where a smooth cycle crosses 20 %
THRESHOLD_SHARE = 1 - 2 * THRESHOLD_PHASE                  # share of the cycle above it
SMOOTH_WINDOWED = (4 - 2 * math.sin(2 * math.pi * THRESHOLD_PHASE)) * THRESHOLD_SHARE / (2 * (1 - 0.2))


def heatmap(trace: dict, reps: list, target: dict):
    """Patient minus the ideal rep, in degrees, for each rep split into HEAT_BINS phases.
    The ideal rep is the demonstration stretched to that rep's own length, so a slow
    but well-shaped rep reads as on target here (pace has its own chart)."""
    top, bottom = target.get("top"), target.get("bottom")
    if top is None or bottom is None or not reps:
        return None
    hz, samples = float(trace.get("hz") or 10), trace.get("patient") or []
    rows = []
    for r in reps:
        i0, i1 = int(round(r["t_start"] * hz)), int(round(r["t_end"] * hz))
        cells: list = [[] for _ in range(HEAT_BINS)]
        for i in range(i0, min(i1, len(samples) - 1) + 1):
            a = samples[i]
            if a is None or i1 <= i0:
                continue
            u = (i - i0) / (i1 - i0)
            phase = THRESHOLD_PHASE + u * THRESHOLD_SHARE if r.get("window") == "threshold" else u
            ideal = bottom + (top - bottom) * (0.5 - 0.5 * math.cos(2 * math.pi * phase))
            cells[min(HEAT_BINS - 1, int(u * HEAT_BINS))].append(a - ideal)
        rows.append({"rep": r["n"], "cells": [round(statistics.fmean(c), 1) if c else None for c in cells]})
    return {"bins": HEAT_BINS, "rows": rows}


def insights(session: dict) -> list:
    """Plain sentences from the numbers, most important first. No model involved."""
    sc, out = session["scores"], []

    def add(tone, text):
        out.append({"tone": tone, "text": text})

    if session.get("status") == "stopped_pain":
        add("bad", "The session stopped early because the patient reported pain. Check in before the next one.")
    if sc.get("target_reps"):
        done = sc["reps"] >= sc["target_reps"]
        add("good" if done else "warn", f"{sc['reps']} of {sc['target_reps']} reps were completed"
            + ("." if done else ", so some sets were cut short or miscounted."))
    acc = sc.get("accuracy_pct")
    if acc is not None:
        add("good" if acc >= 85 else "warn" if acc >= 65 else "bad",
            f"On average each rep reached {acc}% of the target range"
            + (", right where it should be." if acc >= 85 else ", a little short of the demonstration."
               if acc >= 65 else ", well short of the demonstration. Try a smaller target range."))
    pace = sc.get("pacing_pct")
    if pace is not None:
        add("good" if pace >= 75 else "warn",
            "Pace was steady and close to the demonstration." if pace >= 75
            else "Pace wandered: some reps were rushed or much slower than the demonstration.")
    smooth = sc.get("smoothness_pct")
    if smooth is not None:
        add("good" if smooth >= SMOOTH_GOOD else "warn",
            "Movement was smooth, with clean rises and returns." if smooth >= SMOOTH_GOOD
            else "Movement was stop-start in places. Slower reps usually help.")
    sym = session.get("symmetry")
    if sym:
        even = sym["favours"] == "even" or sym["pct"] >= 90
        add("good" if even else "warn",
            "Left and right arms moved about the same amount." if even
            else f"The {sym['favours']} arm moved further than the other by about "
                 f"{abs(round(sym['diff_deg']))} degrees.")
    label = sc["fatigue"]["label"]
    if label == "tiring":
        add("warn", "Range or speed dropped toward the end, a sign of tiring. Consider fewer reps or more rest.")
    elif label == "warming_up":
        add("good", "Range grew as the session went on, so a longer warm-up may help.")
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
    trace = s.get("trace")
    out["reps_detail"] = [score_rep(r, target, trace) for r in raw or []]
    out["scores"] = score_reps(out["reps_detail"])
    out["scores"]["completion_pct"] = completion(s.get("reps") or 0, s.get("target_reps") or 0)
    out["scores"]["overall"] = overall(out["scores"])
    out["bands"] = trace_bands(trace, out["reps_detail"]) if trace else []
    out["rom_by_joint"] = rom_by_joint(trace, target) if trace else []
    out["symmetry"] = symmetry(out["reps_detail"])
    out["tempo"] = tempo(out["reps_detail"], target)
    out["heatmap"] = heatmap(trace, out["reps_detail"], target) if trace else None
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
    scores["completion_pct"] = completion(scores["reps"], scores["target_reps"])
    scores["overall"] = overall(scores)
    out["scores"] = scores
    out["kind"] = session.get("kind") or "exercise"
    out["symmetry"] = symmetry(reps)
    best: dict = {}
    for s in out["sets"] + out["interrupted"]:
        for row in s["rom_by_joint"]:
            if row["joint"] not in best or row["rom_deg"] > best[row["joint"]]["rom_deg"]:
                best[row["joint"]] = row
    out["rom_by_joint"] = [best[j] for j in SIDE_JOINTS if j in best]
    out["insights"] = insights(out)
    return out


def calories(session: dict, weight_kg: float) -> dict:
    """A rough estimate: MET x weight (kg) x active hours, with light-effort MET values."""
    kcal = sum(MET.get(s.get("track_joint"), MET_DEFAULT) * weight_kg * (s.get("seconds") or 0) / 3600
               for s in (session.get("sets") or []) + (session.get("interrupted") or []))
    return {"kcal": round(kcal, 1), "weight_kg": weight_kg, "estimate": True}
