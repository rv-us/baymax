// Sessions: the list of past sessions, and one session taken apart.
import { useEffect, useState } from "preact/hooks";
import { api, attempt, cap, clock, Session, SessionRow, sessionsChanged, SetLog, status, Summary, summaryEvent, when, words } from "./api";
import { ChartDefs, EventTimeline, Meter, OverlayChart, RepStrip } from "./charts";
import { Icon } from "./icons";
import { go } from "./route";

const STATUS: Record<string, { label: string; icon: string; tone: string }> = {
  completed: { label: "Completed", icon: "check", tone: "good" },
  stopped_pain: { label: "Stopped: pain reported", icon: "alert", tone: "bad" },
  stopped_operator: { label: "Stopped from the dashboard", icon: "stop", tone: "warn" },
  no_response: { label: "Ended: no answer", icon: "patient", tone: "warn" },
  not_started: { label: "Not started", icon: "pause", tone: "warn" },
  error: { label: "Ended with an error", icon: "alert", tone: "bad" },
  running: { label: "Unfinished", icon: "pause", tone: "warn" },
};
const FATIGUE: Record<string, string> = {
  steady: "Held steady from first reps to last", tiring: "Tired toward the end", warming_up: "Got stronger as it went",
  not_enough_reps: "Too few reps to tell",
};
const ADJUST: Record<string, string> = {
  slower_tempo: "Slower tempo", smaller_range: "Smaller range", extra_rest: "Extra rest", easier_variant: "Easier variant", keep_going: "Keep going",
};

function StatusMark({ value }: { value: string }) {
  const s = STATUS[value] ?? { label: cap(words(value ?? "unknown")), icon: "flag", tone: "warn" };
  return <span class={`state state-${s.tone}`}><Icon name={s.icon} size={16} />{s.label}</span>;
}

// ---- list -----------------------------------------------------------------------------

export function SessionList() {
  const [rows, setRows] = useState<SessionRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const load = () => api<SessionRow[]>("GET", "/api/sessions").then((r) => { setRows(r); setError(null); }).catch((e) => setError(e.message));
  useEffect(() => { load(); }, [sessionsChanged.value]);

  if (error) return <div class="page-empty"><Icon name="alert" size={28} /><h2>Couldn't read the sessions</h2><p>{error}</p><button class="btn" onClick={load}>Try again</button></div>;
  if (!rows) return <div class="page-empty"><p>Reading the session logs…</p></div>;
  const seed = () => attempt(async () => { await api("POST", "/api/sessions/synthetic", { count: 8 }); await load(); }, "Made 8 synthetic sessions.");
  if (!rows.length) {
    return (
      <div class="page-empty">
        <Icon name="sessions" size={28} />
        <h2>No sessions yet</h2>
        <p>Each workout the robot runs is saved here for review. Run one from Live, or make some synthetic sessions to see how the review works.</p>
        <div class="row"><button class="btn primary" onClick={() => go("/live")}>Go to Live</button><button class="btn" onClick={seed}>Make synthetic sessions</button></div>
      </div>
    );
  }
  const synthetic = rows.filter((r) => r.synthetic).length;
  return (
    <div class="sessions">
      <header class="page-head">
        <h1>Sessions</h1>
        <p>{rows.length} recorded{synthetic ? `, ${synthetic} of them synthetic (generated, not measured)` : ""}.</p>
        {synthetic > 0 && (
          <button class="btn quiet" onClick={() => { if (confirm(`Delete the ${synthetic} synthetic sessions?`)) attempt(async () => { await api("DELETE", "/api/sessions/synthetic"); await load(); }); }}>
            <Icon name="trash" size={18} />Remove synthetic sessions
          </button>
        )}
      </header>
      <div class="session-table" role="group" aria-label="Sessions, newest first">
        <div class="session-row session-head" aria-hidden="true">
          <span>When</span><span>Outcome</span>
          <span>Range reached</span><span>Pacing</span><span>Reps</span>
        </div>
        {rows.map((r) => r.unreadable ? (
          <div key={r.id} class="session-row is-broken"><span>{r.id}</span><span>This file couldn't be read.</span></div>
        ) : (
          <a key={r.id} class="session-row" href={`#/sessions/${r.id}`}>
            <span class="cell-when">
              <b>{when(r.started).date}</b><small>{when(r.started).time}, {when(r.started).ago}</small>
            </span>
            <span class="cell-outcome">
              <StatusMark value={r.status} />
              <small>{r.headline ?? r.plan}</small>
              <span class="tags">
                {r.flagged && <span class="tag tag-flag"><Icon name="flag" size={14} />Follow up</span>}
                {r.synthetic && <span class="tag tag-syn">Synthetic</span>}
              </span>
            </span>
            <span data-label="Range reached"><Meter value={r.scores.accuracy_pct} label="Range reached" /></span>
            <span data-label="Pacing"><Meter value={r.scores.pacing_pct} label="Pacing" /></span>
            <span data-label="Reps" class="cell-reps"><b>{r.scores.reps}</b> of {r.scores.target_reps}</span>
          </a>
        ))}
      </div>
    </div>
  );
}

// ---- the coach's summary --------------------------------------------------------------

function SummaryPanel({ session, onChange }: { session: Session; onChange: (s: Summary, pending: boolean) => void }) {
  const [pending, setPending] = useState(session.summary_pending);
  const llm = status.value?.links.llm;
  const s = session.summary;
  const request = async () => {
    setPending(true);
    if (!(await attempt(() => api("POST", `/api/sessions/${session.id}/summary`)))) setPending(false);
  };
  // First visit: ask the coach once, if it is there. The template is on screen meanwhile.
  useEffect(() => {
    if (!session.summary_cached && !session.summary_pending && llm && (llm.state === "ok" || llm.state === "stub")) request();
  }, [session.id]);
  useEffect(() => {
    const ev = summaryEvent.value;
    if (!ev || ev.id !== session.id) return;
    setPending(false);
    if (ev.summary.source !== "error") onChange(ev.summary, false);
  }, [summaryEvent.value]);

  const offline = llm?.state === "down";
  const source = s.source === "llm" ? `Written by ${s.model}` : s.source === "stub" ? "Stub coach (dry run, no model)" : "Standard summary";
  return (
    <section class="panel summary" aria-busy={pending}>
      <div class="summary-head">
        <h2>{s.headline}</h2>
        <div class="summary-meta">
          {(offline || s.source === "template") && <span class="tag tag-off"><Icon name="coach" size={14} />Coach offline</span>}
          <span class="tag">{source}</span>
          <button class="btn quiet" disabled={pending} onClick={request}><Icon name="refresh" size={18} />{pending ? "Writing…" : "Regenerate"}</button>
        </div>
      </div>
      {s.source === "template" && s.error && <p class="note"><Icon name="alert" size={16} />{s.error} Showing the standard summary instead.</p>}
      {s.follow_up && (
        <p class="follow"><Icon name="flag" /><span><b>The therapist should follow up.</b> {s.follow_up_reason}</span></p>
      )}
      <div class="summary-body">
        <div>
          <h3>What went well</h3>
          <ul>{s.highlights.map((h, i) => <li key={i}>{h}</li>)}</ul>
        </div>
        <div>
          <h3>Where they drifted</h3>
          <p>{s.work_on}</p>
          <h3>For next time</h3>
          <p><span class="tag tag-accent">{ADJUST[s.adjustment.kind] ?? words(s.adjustment.kind)}</span> {s.adjustment.text}</p>
        </div>
      </div>
      <p class="hint">The words come from the coach. Every number on this page is measured by the tracker and computed by the dashboard, not by the model.</p>
    </section>
  );
}

// ---- detail ---------------------------------------------------------------------------

function Calories({ session }: { session: Session }) {
  const [kg, setKg] = useState(() => { try { return +(localStorage.getItem("baymax.weight_kg") ?? 70) || 70; } catch { return 70; } });
  const [kcal, setKcal] = useState<number | null>(null);
  useEffect(() => {
    try { localStorage.setItem("baymax.weight_kg", String(kg)); } catch { /* private mode */ }
    api<Session>("GET", `/api/sessions/${session.id}?weight_kg=${kg}`).then((s) => setKcal(s.calories?.kcal ?? null)).catch(() => setKcal(null));
  }, [kg, session.id]);
  return (
    <p class="calories">
      About <b>{kcal ?? "–"} kcal</b> of light exercise, estimated for a body weight of
      <input type="number" min="20" max="250" value={kg} aria-label="Body weight in kilograms" onChange={(e) => setKg(Math.min(250, Math.max(20, +e.currentTarget.value || 70)))} /> kg.
      <small> Estimate only: effort level × weight × active time.</small>
    </p>
  );
}

function setKey(s: SetLog, i: number) { return `${s.exercise}-${s.set}-${i}`; }

export function SessionDetail({ id }: { id: string }) {
  const [session, setSession] = useState<Session | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pick, setPick] = useState(0);
  useEffect(() => {
    setSession(null); setError(null); setPick(0);
    api<Session>("GET", `/api/sessions/${id}`).then(setSession).catch((e) => setError(e.message));
  }, [id]);

  if (error) return <div class="page-empty"><Icon name="alert" size={28} /><h2>This session couldn't be opened</h2><p>{error}</p><a class="btn" href="#/sessions">Back to sessions</a></div>;
  if (!session) return <div class="page-empty"><p>Opening the session…</p></div>;

  const sets = [...session.sets, ...session.interrupted];
  const set = sets[Math.min(pick, sets.length - 1)];
  const w = when(session.started);
  const sc = session.scores;
  const duration = (session.ended ?? session.started) - session.started;
  return (
    <div class="session">
      <ChartDefs />
      <header class="page-head">
        <a class="back" href="#/sessions"><Icon name="left" size={18} />Sessions</a>
        <h1>{w.date}, {w.time}</h1>
        <p>{session.plan}, {clock(duration)} long. <StatusMark value={session.status} /></p>
        <div class="tags">
          {session.flagged && <span class="tag tag-flag"><Icon name="flag" size={14} />Follow up</span>}
          {session.synthetic && <span class="tag tag-syn">Synthetic data: generated, not measured from a patient</span>}
        </div>
      </header>

      <SummaryPanel session={session} onChange={(summary) => setSession({ ...session, summary, summary_cached: true })} />

      <section class="panel glance">
        <h2>At a glance</h2>
        <dl class="facts">
          <div><dt>Range reached</dt><dd><Meter value={sc.accuracy_pct} label="Range reached" /></dd></div>
          <div><dt>Pacing</dt><dd><Meter value={sc.pacing_pct} label="Pacing" /></dd></div>
          <div><dt>Reps done</dt><dd><b>{sc.reps}</b> of {sc.target_reps}</dd></div>
          <div><dt>Stamina</dt><dd>{FATIGUE[sc.fatigue.label] ?? words(sc.fatigue.label)}</dd></div>
        </dl>
        {sets.length === 0 ? <p class="hint">No sets were recorded in this session.</p> : (
          <div class="set-table" role="table" aria-label="Each set">
            <div class="set-row set-head" role="row">
              <span role="columnheader">Set</span><span role="columnheader">Each rep</span>
              <span role="columnheader">Range</span><span role="columnheader">Pacing</span><span role="columnheader">Patient said</span>
            </div>
            {sets.map((s, i) => (
              <div key={setKey(s, i)} class={`set-row ${i === pick ? "is-picked" : ""}`} role="row">
                <span role="cell">
                  <button class="set-pick" aria-pressed={i === pick} onClick={() => setPick(i)}>
                    <b>{s.exercise}</b><small>Set {s.set}: {s.reps} of {s.target_reps} reps{s.finished === false ? ", cut short" : ""}</small>
                  </button>
                </span>
                <span role="cell" data-label="Each rep"><RepStrip set={s} onPick={() => setPick(i)} /></span>
                <span role="cell" data-label="Range"><Meter value={s.scores.accuracy_pct} label="Range reached" /></span>
                <span role="cell" data-label="Pacing"><Meter value={s.scores.pacing_pct} label="Pacing" /></span>
                <span role="cell" data-label="Patient said" class="cell-said">
                  {s.answer ? <>“{s.answer}”</> : <small>{s.finished === false ? "Not asked" : "No answer"}</small>}
                  {s.trouble && s.trouble !== "none" && <span class={`tag ${s.trouble === "pain" ? "tag-flag" : "tag-off"}`}>{cap(s.trouble)}</span>}
                </span>
              </div>
            ))}
          </div>
        )}
        <p class="hint">Each bar is one rep: its height is how much of the target range was reached. A tick means on target and on pace; a warning mark means it drifted. Select a rep for detail.</p>
      </section>

      {set && (
        <section class="panel">
          <div class="panel-head">
            <h2>{set.exercise}, set {set.set}: patient against the demonstration</h2>
            <span class="hint">{FATIGUE[set.scores.fatigue.label]}{set.scores.fatigue.rom_change_pct != null && ` (range ${set.scores.fatigue.rom_change_pct > 0 ? "+" : ""}${set.scores.fatigue.rom_change_pct}% first reps to last)`}</span>
          </div>
          {set.trace ? <OverlayChart key={setKey(set, pick)} set={set} /> : (
            <div class="chart-empty"><Icon name="studio" size={24} /><p>No joint-angle trace was recorded for this set, so there is nothing to overlay. Sessions logged before pose tracking only have rep counts.</p></div>
          )}
          {set.deviations.length > 0 && <p class="devs"><b>Form notes:</b> {set.deviations.join("; ")}.</p>}
          {set.reps_detail.length > 0 && (
            <details class="fold">
              <summary>Rep table</summary>
              <table class="rep-table">
                <thead><tr><th>Rep</th><th>Peak</th><th>Range</th><th>Of target</th><th>Time</th><th>Against tempo</th><th>Notes</th></tr></thead>
                <tbody>
                  {set.reps_detail.map((r) => (
                    <tr key={r.n}><td>{r.n}</td><td>{Math.round(r.peak_deg)}°</td><td>{Math.round(r.rom_deg)}°</td><td>{r.accuracy_pct ?? "–"}%</td>
                      <td>{r.duration_s.toFixed(1)} s</td><td>{r.pace_ratio != null ? `${r.pace_ratio.toFixed(2)}×` : "–"}</td>
                      <td>{r.misses.length ? r.misses.map(words).join(", ") : "on target"}</td></tr>
                  ))}
                </tbody>
              </table>
            </details>
          )}
        </section>
      )}

      <section class="panel">
        <h2>Safety events, prompts and pauses</h2>
        <EventTimeline sets={sets} events={session.events} duration={duration} />
        {session.notes.length > 0 && <p class="devs"><b>Notes from the robot:</b> {session.notes.join("; ")}</p>}
      </section>

      <Calories session={session} />
    </div>
  );
}
