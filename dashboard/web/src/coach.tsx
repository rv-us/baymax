// Coach: a conversation about one session. The coach is given that session's measurements;
// replies stream in as they are written.
import { signal } from "@preact/signals";
import { useEffect, useRef, useState } from "preact/hooks";
import { api, SessionRow, sessionsChanged, status, streamChat, when } from "./api";
import { Icon } from "./icons";
import { drawerOpen } from "./links";
import { go } from "./route";

interface Msg { role: "user" | "assistant"; content: string; source?: string; model?: string | null; notice?: string; error?: string; pending?: boolean }

const threads = signal<Record<string, Msg[]>>({});     // kept while the app is open, per session
const GENERAL = "general";
const SUGGESTIONS = ["How did this session go overall?", "What should we work on next time?", "Was the left side weaker than the right?", "Did the patient tire toward the end?", "How does this compare with last time?"];

const pct = (v: number | null) => (v == null ? "not measured" : `${v}%`);
const setThread = (key: string, fn: (m: Msg[]) => Msg[]) => { threads.value = { ...threads.value, [key]: fn(threads.value[key] ?? []) }; };

export function Coach({ id }: { id?: string }) {
  const [rows, setRows] = useState<SessionRow[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [text, setText] = useState("");
  const [streaming, setStreaming] = useState(false);
  const cancel = useRef<(() => void) | null>(null);
  const end = useRef<HTMLDivElement>(null);
  const llm = status.value?.links.llm;

  useEffect(() => {
    api<SessionRow[]>("GET", "/api/sessions").then((r) => { setRows(r.filter((x) => !x.unreadable && x.kind === "exercise")); setLoadError(null); })
      .catch((e) => setLoadError(e.message));
  }, [sessionsChanged.value]);
  useEffect(() => () => cancel.current?.(), []);

  const session = rows?.find((r) => r.id === id) ?? null;
  // Open on the newest session, so the coach always has something to talk about.
  useEffect(() => { if (!id && rows && rows.length) go(`/coach/${rows[0].id}`); }, [id, rows]);
  const key = session?.id ?? GENERAL;
  const thread = threads.value[key] ?? [];
  useEffect(() => { end.current?.scrollIntoView({ block: "end" }); }, [thread.length, thread[thread.length - 1]?.content.length]);

  const send = (question: string) => {
    const q = question.trim();
    if (!q || streaming) return;
    const history = [...thread.filter((m) => !m.error && m.content), { role: "user" as const, content: q }];
    setThread(key, (m) => [...m, { role: "user", content: q }, { role: "assistant", content: "", pending: true }]);
    setText("");
    setStreaming(true);
    const patch = (change: Partial<Msg> | ((m: Msg) => Partial<Msg>)) => setThread(key, (m) => {
      const last = m[m.length - 1];
      return [...m.slice(0, -1), { ...last, ...(typeof change === "function" ? change(last) : change) }];
    });
    cancel.current = streamChat({ session_id: session?.id ?? null, messages: history.map(({ role, content }) => ({ role, content })) }, {
      onMeta: (meta) => patch({ source: meta.source, model: meta.model, notice: meta.notice }),
      onDelta: (t) => patch((m) => ({ content: m.content + t })),
      onDone: () => { patch({ pending: false }); setStreaming(false); },
      onError: (message) => { patch({ pending: false, error: message }); setStreaming(false); },
    });
  };
  const stop = () => { cancel.current?.(); setStreaming(false); setThread(key, (m) => [...m.slice(0, -1), { ...m[m.length - 1], pending: false, notice: "Stopped." }]); };
  const retry = () => {
    const lastUser = [...thread].reverse().find((m) => m.role === "user");
    if (!lastUser) return;
    setThread(key, (m) => m.slice(0, m.lastIndexOf(lastUser)));
    setTimeout(() => send(lastUser.content), 0);
  };

  const coachLine = llm?.state === "ok" ? `Answers come from ${llm.model} on the GX10.` : llm?.state === "stub" ? "Dry run: the offline coach answers from the session's numbers."
    : llm?.state === "down" ? "The model can't be reached. The offline coach answers from the session's numbers." : "Checking the coach…";

  return (
    <div class="coach">
      <header class="page-head">
        <h1>Coach</h1>
        <p>Ask about form, pacing, progress, or what to try next. The coach sees the selected session's measurements.</p>
      </header>
      <div class="coach-cols">
        <aside class="coach-side">
          <label for="coach-session">Talking about</label>
          {loadError ? <p class="note bad"><Icon name="alert" size={16} />{loadError}</p> : !rows ? <p class="hint">Reading the sessions…</p> : rows.length === 0 ? (
            <div class="list-empty"><p><b>No exercise sessions yet.</b> The coach can still answer general questions. Run a session from Live to get personal feedback.</p><a class="btn" href="#/live">Go to Live</a></div>
          ) : (
            <select id="coach-session" value={session?.id ?? ""} onChange={(e) => go(`/coach/${e.currentTarget.value}`)}>
              {rows.map((r) => <option key={r.id} value={r.id}>{when(r.started).date}, {when(r.started).time}{r.scores.overall.score != null ? ` (score ${r.scores.overall.score})` : ""}</option>)}
            </select>
          )}
          {session && (
            <div class="coach-context">
              <div class="score-dial small" style={{ "--v": session.scores.overall.score ?? 0 } as any}>
                <b>{session.scores.overall.score ?? "n/a"}</b><small>out of 100</small>
              </div>
              <dl>
                <div><dt>Range reached</dt><dd>{pct(session.scores.accuracy_pct)}</dd></div>
                <div><dt>Pacing</dt><dd>{pct(session.scores.pacing_pct)}</dd></div>
                <div><dt>Smoothness</dt><dd>{pct(session.scores.smoothness_pct)}</dd></div>
                <div><dt>Left and right</dt><dd>{pct(session.scores.symmetry_pct)}</dd></div>
                <div><dt>Reps</dt><dd>{session.scores.reps} of {session.scores.target_reps}</dd></div>
              </dl>
              <a class="btn" href={`#/sessions/${session.id}`}><Icon name="chart" size={18} />Open the full analysis</a>
              {session.synthetic && <span class="tag tag-syn">Synthetic data</span>}
            </div>
          )}
          <p class={`coach-link ${llm?.state === "down" ? "is-down" : ""}`}>
            <Icon name="coach" size={16} /><span>{coachLine}</span>
            {llm?.state !== "ok" && <button class="btn quiet" onClick={() => { drawerOpen.value = true; }}>Connect a model</button>}
          </p>
        </aside>

        <section class="chat" aria-label="Conversation with the coach">
          <div class="chat-log" role="log" aria-live="polite" aria-busy={streaming}>
            {thread.length === 0 && (
              <div class="chat-empty">
                <Icon name="coach" size={28} />
                <h2>{session ? "Ask about this session" : "Ask the coach"}</h2>
                <p>The coach explains the numbers in plain words and suggests what to try next. It never diagnoses and never invents a measurement.</p>
                <div class="suggest">
                  {SUGGESTIONS.map((s) => <button key={s} class="chip" onClick={() => send(s)}>{s}</button>)}
                </div>
              </div>
            )}
            {thread.map((m, i) => (
              <div key={i} class={`bubble bubble-${m.role} ${m.error ? "is-error" : ""}`}>
                {m.role === "assistant" && m.notice && <p class="bubble-notice"><Icon name="info" size={14} />{m.notice}</p>}
                {m.content ? <p class="bubble-text">{m.content}{m.pending && <span class="caret" aria-hidden="true" />}</p>
                  : m.pending ? <p class="bubble-text thinking"><i class="spinner" />The coach is thinking…</p> : null}
                {m.error && (
                  <p class="bubble-error" role="alert"><Icon name="alert" size={16} /><span>{m.error}</span>
                    <button class="btn" onClick={retry}><Icon name="refresh" size={16} />Try again</button></p>
                )}
                {m.role === "assistant" && !m.pending && m.content && (
                  <small class="bubble-source">{m.source === "llm" ? `Written by ${m.model}` : "Offline coach: built from the session's numbers, no model"}</small>
                )}
              </div>
            ))}
            <div ref={end} />
          </div>
          {thread.length > 0 && !streaming && (
            <div class="suggest inline">
              {SUGGESTIONS.filter((s) => !thread.some((m) => m.content === s)).slice(0, 3).map((s) => <button key={s} class="chip" onClick={() => send(s)}>{s}</button>)}
              <button class="btn quiet" onClick={() => setThread(key, () => [])}>Clear the conversation</button>
            </div>
          )}
          <form class="chat-input" onSubmit={(e) => { e.preventDefault(); send(text); }}>
            <label class="sr-only" for="chat-text">Message to the coach</label>
            <textarea id="chat-text" rows={1} maxLength={2000} value={text} placeholder={session ? "Ask about this session" : "Ask the coach"}
              onInput={(e) => setText(e.currentTarget.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(text); } }} />
            {streaming
              ? <button type="button" class="btn" onClick={stop}><Icon name="stop" size={18} />Stop</button>
              : <button class="btn primary" disabled={!text.trim()}><Icon name="send" size={18} />Send</button>}
          </form>
        </section>
      </div>
    </div>
  );
}
