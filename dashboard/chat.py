"""Coach chat: a conversation about one session, streamed token by token.

The model (Qwen on the GX10, through Ollama) gets the same facts as the summary, plus
the analytics that workout/session.py computed, as its context. It narrates and
answers; it never supplies a number. Replies stream to the browser as Server-Sent
Events:

    event: meta    {"source": "llm" | "stub" | "offline", "model", "session", "notice"?}
    event: delta   {"text": "..."}
    event: done    {"chars": n}
    event: error   {"message": "..."}      (after it, the stream ends)

If Ollama can't be reached, the reply is built from the facts by the code and marked
"offline" with the reason, so the tab always answers.
"""
from __future__ import annotations

import json
import re
import time

import httpx

from .coach import _reason, facts, template_summary

MAX_TURNS = 12
MAX_CHARS = 2000

SYSTEM_PROMPT = (
    "You are Baymax's coach: a warm, plain-spoken assistant helping a therapist, caregiver or "
    "patient understand one physical therapy exercise session. The patient is an older adult. "
    "You are given measurements that software already computed, as JSON. Use only those facts; "
    "never invent, estimate or recalculate numbers, and say so when something wasn't measured. "
    "You are not a clinician: never diagnose, never name a medical condition, never guess at "
    "the cause of pain, and never give medical advice. If pain or a safety stop is in the "
    "facts, say the therapist should follow up. Give practical, encouraging feedback on form, "
    "pacing, range and what to try next time. Keep replies short: a few sentences, or a short "
    "list. Plain text only, no markdown headings or tables."
)


def context(session: dict | None, history: list) -> dict:
    """What the coach knows about the selected session."""
    if not session:
        return {"note": "No session is selected. Answer generally and suggest picking one."}
    f = facts(session, history)
    f["overall"] = session["scores"].get("overall")
    f["smoothness_pct"] = session["scores"].get("smoothness_pct")
    f["symmetry"] = session.get("symmetry")
    f["range_by_joint"] = session.get("rom_by_joint")
    f["insights"] = [i["text"] for i in session.get("insights") or []]
    f["synthetic_data"] = bool(session.get("synthetic"))
    return f


def clean_messages(messages) -> list:
    """The browser's turns, trimmed: user and assistant only, bounded in count and size."""
    out = []
    for m in messages if isinstance(messages, list) else []:
        if isinstance(m, dict) and m.get("role") in ("user", "assistant") and str(m.get("content") or "").strip():
            out.append({"role": m["role"], "content": str(m["content"]).strip()[:MAX_CHARS]})
    return out[-MAX_TURNS:]


# ---- the offline coach ------------------------------------------------------------


def offline_reply(question: str, ctx: dict) -> str:
    """An answer from the facts alone, shaped by what was asked."""
    if "scores" not in ctx:
        return ("Pick a session on the left and I can talk about it: the overall score, range of motion, "
                "pacing, smoothness, left and right balance, and what to try next time.")
    q = question.lower()
    sc, summary = ctx["scores"], template_summary(ctx)
    overall = ctx.get("overall") or {}
    lines = []

    def has(*words):
        return any(w in q for w in words)

    if has("score", "overall", "how did", "how was", "summary", "summar", "went"):
        if overall.get("score") is not None:
            lines.append(f"Overall this session scored {overall['score']} out of 100 ({overall['label'].lower()}).")
        lines.append(summary["headline"])
        lines += summary["highlights"][:2]
    if has("range", "motion", "rom", "high", "reach", "far"):
        if sc.get("accuracy_pct") is not None:
            lines.append(f"On average each rep reached {sc['accuracy_pct']}% of the target range.")
        for row in (ctx.get("range_by_joint") or [])[:4]:
            lines.append(f"{row['joint'].replace('_', ' ').capitalize()} moved through {round(row['rom_deg'])} degrees.")
    if has("pace", "pacing", "tempo", "speed", "fast", "slow", "rush"):
        if sc.get("pacing_pct") is not None:
            lines.append(f"Pacing scored {sc['pacing_pct']}%: that blends how even the reps were with how "
                         "close they were to the demonstrated tempo.")
    if has("smooth", "shaky", "jerky", "wobbl", "control"):
        lines.append(f"Smoothness scored {ctx['smoothness_pct']}%." if ctx.get("smoothness_pct") is not None
                     else "Smoothness wasn't measured for this session.")
    if has("symmetr", "balance", "left", "right", "even", "side"):
        sym = ctx.get("symmetry")
        lines.append("Left and right balance wasn't measured for this session." if not sym else
                     f"Left and right balance scored {sym['pct']}%: the left shoulder moved about "
                     f"{round(sym['left_rom_deg'])} degrees per rep and the right about {round(sym['right_rom_deg'])}.")
    if has("tired", "fatigue", "stamina", "tiring", "energy"):
        lines.append({"tiring": "Range or speed dropped toward the end, a sign of tiring.",
                      "warming_up": "Range grew as the session went on.",
                      "steady": "Effort held steady from the first reps to the last."}
                     .get(sc["fatigue"]["label"], "There were too few reps to judge stamina."))
    if has("pain", "hurt", "safe", "stop", "sore"):
        events = [e for e in ctx.get("events") or [] if e.get("type") in ("pain_word", "fall", "safety_stop")]
        lines.append("A pain report or safety stop was logged in this session. I can't say what caused it; "
                     "the therapist should follow up before the next session." if events
                     else "No pain words or safety stops were logged in this session.")
    if has("next", "improve", "better", "work on", "should", "advice", "recommend", "tip", "try", "focus"):
        lines.append(summary["work_on"])
        lines.append(summary["adjustment"]["text"])
    if has("trend", "progress", "last time", "previous", "before", "improving", "compare"):
        prev = [h for h in ctx.get("earlier_sessions") or [] if h.get("accuracy_pct") is not None]
        if prev and sc.get("accuracy_pct") is not None:
            lines.append(f"Range reached was {prev[-1]['accuracy_pct']}% in the session before and "
                         f"{sc['accuracy_pct']}% in this one.")
        else:
            lines.append("There is no earlier scored session to compare this one with.")
    if not lines:
        lines = [summary["headline"]] + (ctx.get("insights") or [])[:3] + [summary["adjustment"]["text"]]
    if summary["follow_up"] and not has("pain", "hurt"):
        lines.append("One more thing: " + (summary["follow_up_reason"] or "the therapist should follow up."))
    seen, out = set(), []
    for line in lines:
        if line and line not in seen:
            seen.add(line)
            out.append(line)
    return " ".join(out)


# ---- streaming --------------------------------------------------------------------


def _sse(kind: str, data: dict) -> str:
    return f"event: {kind}\ndata: {json.dumps(data)}\n\n"


def _words(text: str):
    for piece in re.findall(r"\S+\s*", text):
        yield piece


class Chat:
    def __init__(self, settings, sessions):
        self.settings, self.sessions = settings, sessions

    def _context(self, sid: str | None) -> tuple[dict, str | None]:
        if not sid:
            return context(None, []), None
        session = self.sessions.get(sid)  # NotFound reaches the browser as a 404
        return context(session, self.sessions.history(session.get("started") or 0)), sid

    def stream(self, sid: str | None, messages: list, pace: float = 0.02):
        """Yields SSE frames. Never raises once it has started."""
        s = self.settings
        ctx, sid = self._context(sid)
        question = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
        sent = 0

        def spoken(text: str):
            nonlocal sent
            for piece in _words(text):
                sent += len(piece)
                yield _sse("delta", {"text": piece})
                if pace:
                    time.sleep(pace)

        if s.stub_llm:
            yield _sse("meta", {"source": "stub", "model": None, "session": sid,
                                "notice": "Dry run: this is the offline coach answering from the session's numbers."})
            yield from spoken(offline_reply(question, ctx))
            yield _sse("done", {"chars": sent})
            return
        body = {"model": s.model, "stream": True, "think": False, "keep_alive": -1,
                "options": {"temperature": 0.4},
                "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                             {"role": "system", "content": "Session facts (JSON): " + json.dumps(ctx)},
                             *messages]}
        started = False
        try:
            with httpx.stream("POST", f"{s.ollama_url}/api/chat", json=body,
                              timeout=httpx.Timeout(s.coach_timeout_s, connect=4.0)) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line:
                        continue
                    chunk = json.loads(line)
                    if chunk.get("error"):
                        raise ValueError(str(chunk["error"]))
                    text = (chunk.get("message") or {}).get("content") or ""
                    if text:
                        if not started:
                            started = True
                            yield _sse("meta", {"source": "llm", "model": s.model, "session": sid})
                        sent += len(text)
                        yield _sse("delta", {"text": text})
                    if chunk.get("done"):
                        break
            if not started:
                raise ValueError("the model sent an empty reply")
            yield _sse("done", {"chars": sent})
        except Exception as e:  # unreachable, timeout, bad stream: all end the same way for the reader
            reason = _reason(e, s) if isinstance(e, httpx.HTTPError) else f"The coach's reply broke off ({e})."
            if started:  # part of an answer is already on screen: say it stopped, don't restart
                yield _sse("error", {"message": reason})
                return
            yield _sse("meta", {"source": "offline", "model": None, "session": sid,
                                "notice": f"{reason} Answering from the session's numbers instead."})
            yield from spoken(offline_reply(question, ctx))
            yield _sse("done", {"chars": sent})
