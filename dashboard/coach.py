"""The session summary: Qwen on the GX10 (through Ollama) narrates, the code counts.

The model gets facts that workout/session.py already computed and returns structured
JSON (Ollama's `format` schema, as in coach_test.py). It never supplies a number the
dashboard shows. If Ollama can't be reached, times out or answers with something
unusable, a template summary built from the same facts is used instead, so the page
is never blank. Summaries run on a background thread and are cached in the session file.
"""
from __future__ import annotations

import json
import threading
import time

import httpx

ADJUSTMENTS = ("slower_tempo", "smaller_range", "extra_rest", "easier_variant", "keep_going")

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "highlights": {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 3},
        "work_on": {"type": "string"},
        "adjustment": {"type": "object",
                       "properties": {"kind": {"type": "string", "enum": list(ADJUSTMENTS)},
                                      "text": {"type": "string"}},
                       "required": ["kind", "text"]},
        "follow_up": {"type": "boolean"},
        "follow_up_reason": {"type": "string"},
    },
    "required": ["headline", "highlights", "work_on", "adjustment", "follow_up"],
}

SYSTEM_PROMPT = (
    "You write a short review of one physical therapy exercise session for the patient's "
    "therapist or caregiver. The patient is an older adult. You are given measurements that "
    "software already computed; use only those facts and never invent or recalculate numbers. "
    "Write in plain, warm, encouraging language, in sentence case, with no jargon. "
    "You are not a clinician: never diagnose, never name a medical condition, never guess at "
    "the cause of pain, and never give medical advice. If pain or a safety stop is reported, "
    "say that it happened and set follow_up to true. "
    "Reply with JSON only: headline (one short sentence), highlights (2 or 3 things that went "
    "well), work_on (one sentence on where they drifted), adjustment (kind: one of "
    f"{', '.join(ADJUSTMENTS)}; text: one concrete suggestion for next time), follow_up "
    "(true if the therapist should check in) and follow_up_reason."
)


# ---- facts ------------------------------------------------------------------------


def facts(session: dict, history: list) -> dict:
    """What the coach is told: the upgraded session boiled down, plus earlier sessions."""
    sets = []
    for s in session["sets"] + session["interrupted"]:
        misses: dict = {}
        for r in s["reps_detail"]:
            for m in r["misses"]:
                misses[m] = misses.get(m, 0) + 1
        sets.append({"exercise": s["exercise"], "set": s["set"], "reps": s.get("reps"),
                     "target_reps": s.get("target_reps"), "finished": s.get("finished", True),
                     "accuracy_pct": s["scores"]["accuracy_pct"], "pacing_pct": s["scores"]["pacing_pct"],
                     "fatigue": s["scores"]["fatigue"]["label"], "misses": misses,
                     "form_notes": s.get("deviations") or [], "patient_said": s.get("answer"),
                     "trouble": s.get("trouble")})
    return {
        "plan": session.get("plan"), "status": session.get("status"), "flagged": bool(session.get("flagged")),
        "scores": session["scores"], "sets": sets,
        "events": [{k: e[k] for k in ("type", "transcript", "detail") if e.get(k)} for e in session["events"]],
        "earlier_sessions": [{"days_ago": round((session.get("started", 0) - h["started"]) / 86400, 1),
                              "status": h["status"], "accuracy_pct": h["scores"]["accuracy_pct"],
                              "pacing_pct": h["scores"]["pacing_pct"]} for h in history],
    }


# ---- the template (also the fallback) ---------------------------------------------


def template_summary(f: dict) -> dict:
    """A deterministic summary from the facts. Used when the coach is offline."""
    sc, sets = f["scores"], f["sets"]
    names = list(dict.fromkeys(s["exercise"] for s in sets))
    pain = f["status"] == "stopped_pain" or any(e["type"] in ("pain_word", "fall") for e in f["events"])
    headline = {
        "completed": f"Finished all {len(names)} exercise{'s' if len(names) != 1 else ''}.",
        "stopped_pain": "The session stopped early after the patient reported pain.",
        "stopped_operator": "The session was stopped from the dashboard.",
        "no_response": "The session ended because the patient stopped answering.",
        "not_started": "The patient wasn't ready, so the workout didn't start.",
    }.get(f["status"], "Session recorded.")

    highlights = []
    if sc.get("reps"):
        highlights.append(f"Completed {sc['reps']} of {sc['target_reps']} reps.")
    scored = [s for s in sets if s["accuracy_pct"] is not None]
    if scored:
        best = max(scored, key=lambda s: s["accuracy_pct"])
        highlights.append(f"Best range of motion was on {best['exercise'].lower()}, set {best['set']} "
                          f"({best['accuracy_pct']}% of the target).")
    if sc.get("pacing_pct") is not None and sc["pacing_pct"] >= 70:
        highlights.append("Kept a steady, unhurried pace.")
    prev = [h for h in f["earlier_sessions"] if h["accuracy_pct"] is not None]
    if prev and sc.get("accuracy_pct") is not None and sc["accuracy_pct"] > prev[-1]["accuracy_pct"]:
        highlights.append(f"Range of motion is up from {prev[-1]['accuracy_pct']}% to {sc['accuracy_pct']}% "
                          "since the last session.")
    if len(highlights) < 2:
        highlights.append("Showed up and gave it a go, which is what counts.")
    highlights = highlights[:3]

    totals: dict = {}
    for s in sets:
        for m, n in s["misses"].items():
            totals[m] = totals.get(m, 0) + n
    worst = max(totals, key=totals.get) if totals else None
    tiring = [s for s in sets if s["fatigue"] == "tiring"]
    if pain:
        work_on = "Nothing to push on today. The pain report comes first."
        adjustment = {"kind": "extra_rest", "text": "Check in with the patient before the next session, "
                      "and start with a smaller range."}
    elif tiring:
        s = tiring[0]
        work_on = f"Range dropped off toward the end of {s['exercise'].lower()}, set {s['set']}."
        adjustment = {"kind": "extra_rest", "text": "Add a longer rest between sets, or do two fewer reps per set."}
    elif worst == "short":
        work_on = f"{totals['short']} reps stopped short of the target height."
        adjustment = {"kind": "smaller_range", "text": "Lower the target a little so every rep can reach it, "
                      "then build back up."}
    elif worst == "fast":
        work_on = f"{totals['fast']} reps were quicker than the demonstration."
        adjustment = {"kind": "slower_tempo", "text": "Slow the demonstration down and count out loud together."}
    elif worst in ("slow", "not_lowered", "over"):
        work_on = {"slow": "Some reps took much longer than the demonstration.",
                   "not_lowered": "The arms didn't come all the way back down between some reps.",
                   "over": "Some reps went past the target range."}[worst]
        adjustment = {"kind": "easier_variant", "text": "Try the movement one arm at a time next session."}
    else:
        work_on = "No clear weak spot this session."
        adjustment = {"kind": "keep_going", "text": "Keep the same plan next time."}

    hard = sum(1 for s in sets if s["trouble"] == "difficulty")
    follow = bool(f["flagged"] or pain or hard >= 2)
    reason = ("The patient reported pain and the session was stopped." if pain
              else "The session was flagged." if f["flagged"]
              else "The patient said more than one set was hard." if hard >= 2 else "")
    return {"headline": headline, "highlights": highlights, "work_on": work_on,
            "adjustment": adjustment, "follow_up": follow, "follow_up_reason": reason}


def clean(reply, fallback: dict) -> dict | None:
    """The model's reply as a summary, or None if it isn't usable. The follow-up flag
    can only be raised by the model, never lowered below what the code decided."""
    if not isinstance(reply, dict):
        return None
    try:
        headline = str(reply["headline"]).strip()[:200]
        highlights = [str(h).strip()[:240] for h in reply["highlights"] if str(h).strip()][:3]
        work_on = str(reply["work_on"]).strip()[:300]
        kind = reply["adjustment"]["kind"]
        text = str(reply["adjustment"]["text"]).strip()[:300]
    except (KeyError, TypeError):
        return None
    if not headline or len(highlights) < 2 or not work_on or not text:
        return None
    follow = bool(reply.get("follow_up")) or fallback["follow_up"]
    return {"headline": headline, "highlights": highlights, "work_on": work_on,
            "adjustment": {"kind": kind if kind in ADJUSTMENTS else "keep_going", "text": text},
            "follow_up": follow,
            "follow_up_reason": fallback["follow_up_reason"] or str(reply.get("follow_up_reason") or "").strip()[:300]}


def parse_json(text: str):
    """JSON, or JSON wrapped in other text (as coach_test.parse_instruction allows)."""
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        pass
    start, end = (text or "").find("{"), (text or "").rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except ValueError:
            pass
    return None


# ---- the coach --------------------------------------------------------------------


class Coach:
    def __init__(self, settings, sessions, hub):
        self.settings, self.sessions, self.hub = settings, sessions, hub
        self._lock = threading.Lock()
        self.pending: set = set()
        self.last_error: str | None = None

    # -- link status

    def probe(self) -> dict:
        s = self.settings
        if s.stub_llm:
            return {"state": "stub", "detail": "Stubbed coach (dry run): canned text, no model", "model": "stub"}
        try:
            r = httpx.get(f"{s.ollama_url}/api/tags", timeout=2.5)
            r.raise_for_status()
            models = [m.get("name") for m in r.json().get("models", [])]
        except (httpx.HTTPError, ValueError) as e:
            return {"state": "down", "model": s.model, "url": s.ollama_url,
                    "detail": f"Ollama isn't answering at {s.ollama_url} ({type(e).__name__})."}
        if s.model not in models:
            return {"state": "down", "model": s.model, "url": s.ollama_url,
                    "detail": f"Ollama is up but has no model {s.model!r} (ollama pull {s.model})."}
        return {"state": "ok", "model": s.model, "url": s.ollama_url, "detail": f"{s.model} at {s.ollama_url}"}

    # -- one summary

    def ask(self, f: dict) -> dict:
        """One blocking call to Ollama. Raises on any failure."""
        s = self.settings
        r = httpx.post(f"{s.ollama_url}/api/chat", timeout=s.coach_timeout_s, json={
            "model": s.model, "stream": False, "think": False, "keep_alive": -1,
            "format": SUMMARY_SCHEMA, "options": {"temperature": 0.3},
            "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                         {"role": "user", "content": json.dumps(f)}]})
        r.raise_for_status()
        return parse_json((r.json().get("message") or {}).get("content", ""))

    def summarize(self, session: dict, history: list) -> dict:
        """Never raises: an LLM summary, or the template with the reason it was used."""
        f = facts(session, history)
        fallback = template_summary(f)
        meta = {"generated": time.time(), "model": self.settings.model}
        if self.settings.stub_llm:
            time.sleep(0.8)  # long enough to see the "writing" state in the UI
            return {**fallback, **meta, "source": "stub", "model": "stub"}
        try:
            summary = clean(self.ask(f), fallback)
            if summary is None:
                raise ValueError("the model's reply didn't match the summary format")
        except Exception as e:  # unreachable, timeout, bad JSON: all the same to the reader
            self.last_error = _reason(e, self.settings)
            return {**fallback, **meta, "source": "template", "error": self.last_error}
        self.last_error = None
        return {**summary, **meta, "source": "llm"}

    def template(self, session: dict, history: list) -> dict:
        return {**template_summary(facts(session, history)), "source": "template",
                "generated": None, "model": None}

    # -- background jobs

    def request(self, sid: str) -> bool:
        """Start a summary job for a session unless one is running. Returns at once."""
        with self._lock:
            if sid in self.pending:
                return False
            self.pending.add(sid)
        threading.Thread(target=self._job, args=(sid,), name=f"coach-{sid}", daemon=True).start()
        return True

    def _job(self, sid: str) -> None:
        summary = None
        try:
            session = self.sessions.get(sid)
            summary = self.summarize(session, self.sessions.history(session.get("started") or 0))
            self.sessions.update(sid, summary=summary)
        except Exception as e:
            summary = {"source": "error", "error": str(e)}
        finally:
            with self._lock:
                self.pending.discard(sid)
            self.hub.publish("summary", {"id": sid, "summary": summary})


def _reason(e: Exception, settings) -> str:
    if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout)):
        return f"Ollama isn't reachable at {settings.ollama_url}."
    if isinstance(e, httpx.TimeoutException):
        return f"The coach didn't answer within {settings.coach_timeout_s:g} s."
    if isinstance(e, httpx.HTTPStatusError):
        return f"Ollama answered HTTP {e.response.status_code}."
    if isinstance(e, httpx.HTTPError):
        return f"Ollama isn't reachable at {settings.ollama_url}."
    return str(e)
