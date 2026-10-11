"""Synthetic patients, until the pose tracker (#11-#13) is finished.

patient_trace() makes one set's joint-angle trace with plausible faults: range that
shrinks and reps that slow as the patient tires, sensor noise, a dropped frame now
and then. generate_session() builds a whole session log from such traces, in exactly
the shape the workout loop saves (workout/session.py), marked `synthetic: true`.
The live simulator (dashboard/live.py) replays the same traces in real time.

Everything is seeded, so the same seed always gives the same sessions.
"""
from __future__ import annotations

import math
import random
import time

from workout.plan import load_plan
from workout.session import reference_trace, set_target, upgrade_set

HZ = 10


def profile(rng: random.Random, progress: float = 1.0) -> dict:
    """How this patient moves today. `progress` 0..1 = how far along their therapy."""
    return {
        "rom": 0.74 + 0.2 * progress + rng.uniform(-0.04, 0.04),   # share of the target range reached
        "fatigue": rng.uniform(0.012, 0.03) * (1.3 - 0.5 * progress),  # range lost per rep
        "slowdown": rng.uniform(0.01, 0.035),                      # rep time gained per rep
        "pace": rng.uniform(0.9, 1.2),                             # rep time against the tempo
        "noise": rng.uniform(1.0, 2.5),                            # degrees of sensor jitter
        "drop": 0.004,                                             # chance per sample of a lost frame
    }


def patient_trace(rng: random.Random, target: dict, reps: int, prof: dict, rep_offset: int = 0) -> list:
    """Angles (degrees, HZ samples a second) for `reps` reps. None = no pose that frame."""
    top, bottom = target.get("top", 90.0), target.get("bottom", 10.0)
    span, tempo = top - bottom, target.get("tempo_s", 4.0)
    out, jitter = [], 0.0
    lead_in = int(HZ * rng.uniform(0.4, 1.0))
    out += [bottom + rng.uniform(0, 3)] * lead_in
    for k in range(reps):
        tired = k + 0.5 * rep_offset
        peak = bottom + span * prof["rom"] * (1 - prof["fatigue"] * tired) + rng.gauss(0, 2.5)
        trough = bottom + abs(rng.gauss(1.5, 2.0))
        seconds = tempo * prof["pace"] * (1 + prof["slowdown"] * tired) * (1 + rng.gauss(0, 0.06))
        n = max(8, int(round(seconds * HZ)))
        for i in range(n):
            out.append(trough + (peak - trough) * (0.5 - 0.5 * math.cos(2 * math.pi * i / n)))
        out += [trough] * rng.choice((0, 0, 1, 2, 4))  # a breath between reps
    out += [bottom + 1.0] * int(HZ * 0.6)
    noisy, lost = [], 0
    for a in out:
        jitter = 0.7 * jitter + rng.gauss(0, prof["noise"] * 0.5)
        if lost:
            lost -= 1
            noisy.append(None)
        elif rng.random() < prof["drop"]:
            lost = rng.choice((0, 1, 2))
            noisy.append(None)
        else:
            noisy.append(round(a + jitter, 1))
    return noisy


def with_reference(patient: list, target: dict, reps: int) -> dict:
    """A stored trace: the patient and the demonstrated motion on one time axis."""
    ref = reference_trace(target, reps, HZ)
    bottom = target.get("bottom", 0.0)
    ref = (ref + [bottom] * len(patient))[:len(patient)]
    return {"hz": HZ, "patient": patient, "reference": ref}


def deviations(scored_set: dict) -> list:
    """Free-text form notes like the tracker will write, from the scored reps."""
    reps = scored_set["reps_detail"]
    notes = []
    short = [r["n"] for r in reps if "short" in r["misses"]]
    if short:
        notes.append(f"arms stopped short of the target on rep{'s' if len(short) > 1 else ''} "
                     + ", ".join(map(str, short[:6])))
    if sum("slow" in r["misses"] for r in reps) >= 2:
        notes.append("the last reps slowed down")
    if sum("fast" in r["misses"] for r in reps) >= 2:
        notes.append("some reps were rushed")
    return notes


def generate_session(seed: int, started: float, progress: float = 1.0, plan_name: str = "arms_basic",
                     pain: bool = False, left_frame: bool = False) -> dict:
    rng = random.Random(seed)
    plan = load_plan(plan_name)
    prof = profile(rng, progress)
    t = rng.uniform(8, 16)  # ready check
    transitions = [{"t": round(t, 2), "from": "READY_CHECK", "event": "ready", "to": "INTRODUCE",
                    "seconds": round(t, 2)}]
    session = {"schema": 2, "plan": plan.name, "started": started, "status": "completed", "flagged": False,
               "synthetic": True, "source": "synthetic", "seed": seed, "patient": {"name": "Demo patient"},
               "transitions": transitions, "sets": [], "interrupted": [], "events": [], "notes": []}
    if rng.random() < 0.5:
        session["events"].append({"t": round(t * 0.6, 2), "type": rng.choice(("prompt_closer", "prompt_back")),
                                  "detail": "asked before the first set"})
    stop_at = (rng.randrange(len(plan.exercises)), rng.choice((1, 2))) if pain else None
    lose_at = (0, 1) if left_frame else None
    stopped = False
    for ei, ex in enumerate(plan.exercises):
        target = {"tolerance": 15.0, **set_target(ex)}
        t += rng.uniform(14, 20)  # introduce and demonstrate
        for n in range(1, ex.sets + 1):
            t += rng.uniform(5, 8)  # prompt and countdown
            cut = stop_at == (ei, min(n, ex.sets))
            reps = max(2, ex.reps // 2 + rng.choice((0, 1))) if cut else ex.reps
            patient = patient_trace(rng, target, reps, prof, rep_offset=(n - 1) * ex.reps)
            if cut:
                patient = patient[:len(patient) - int(HZ * 1.5)]
            if lose_at == (ei, n):
                a = len(patient) // 2
                gap = int(HZ * rng.uniform(4, 7))
                patient = patient[:a] + [None] * gap + patient[a:]
                session["events"] += [
                    {"t": round(t + a / HZ, 2), "type": "left_frame", "set": len(session["sets"]) + 1},
                    {"t": round(t + (a + gap) / HZ, 2), "type": "returned", "set": len(session["sets"]) + 1}]
            seconds = len(patient) / HZ
            entry = {"exercise": ex.name, "set": n, "reps": 0, "target_reps": ex.reps, "deviations": [],
                     "track_joint": ex.track_joint, "target": target, "started_t": round(t, 2),
                     "seconds": round(seconds, 2), "finished": not cut,
                     "trace": with_reference(patient, target, ex.reps)}
            scored = upgrade_set(entry)
            entry["reps"] = len(scored["reps_detail"])
            entry["deviations"] = deviations(scored)
            t += seconds
            if cut:
                said = rng.choice(("ow, my shoulder hurts", "ouch, that's sore", "stop, that hurts"))
                session["interrupted"].append(entry)
                session["events"] += [{"t": round(t, 2), "type": "pain_word", "transcript": said,
                                       "set": len(session["sets"]) + 1},
                                      {"t": round(t + 0.1, 2), "type": "safety_stop"}]
                session.update(status="stopped_pain", flagged=True)
                stopped = True
                break
            tired = scored["scores"]["fatigue"]["label"] == "tiring"
            answer = rng.choice(("that was a bit hard", "my arms are getting tired")) if tired and rng.random() < 0.7 \
                else rng.choice(("no, it was fine", "nope, all good", "no trouble"))
            entry.update(answer=answer, trouble="difficulty" if tired and "fine" not in answer
                         and "good" not in answer and "no trouble" not in answer else "none")
            session["sets"].append(entry)
            t += rng.uniform(8, 14)  # review and the patient's answer
        if stopped:
            break
    session["ended"] = started + t + 4
    return session


def seed_sessions(store, count: int = 8, seed: int = 7, now: float | None = None) -> list:
    """Write `count` sessions spread over the last few weeks, improving over time, with
    one pain stop and one walk out of frame among them. Returns the new ids."""
    now = now or time.time()
    rng = random.Random(seed)
    ids = []
    for i in range(count):
        days_ago = (count - 1 - i) * 2.5 + rng.uniform(0, 0.4)
        started = now - days_ago * 86400 - rng.uniform(600, 7200)
        session = generate_session(seed * 1000 + i, started, progress=i / max(1, count - 1),
                                   pain=(i == count - 3), left_frame=(i == count - 1))
        sid = "synthetic-" + time.strftime("%Y%m%d-%H%M%S", time.localtime(started))
        store.save(sid, session)
        ids.append(sid)
    return ids
