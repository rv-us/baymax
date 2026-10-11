// Sessions: every exercise session and recording, and one session taken apart.
import { useEffect, useState } from "preact/hooks";
import { api, attempt, cap, Clip, clock, Session, SessionRow, sessionsChanged, SetLog, status, Summary, summaryEvent, Trend, when, words } from "./api";
import { ChartCard, Headline, HeatmapChart, RepQuality, RomChart, SmoothnessChart, SymmetryChart, TempoChart, TrendChart, Unmeasured } from "./analytics";
import { ChartDefs, EventTimeline, Meter, OverlayChart, RepStrip } from "./charts";
import { Icon } from "./icons";
import { MotionTwin } from "./motion";
import { PlayButton } from "./play";
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

async function removeSession(id: string, label: string): Promise<boolean> {
  if (!confirm(`Delete the session from ${label}? Its log and analysis are removed for good.`)) return false;
  return !!(await attempt(() => api("DELETE", `/api/sessions/${id}`), "Session deleted."));
}

// ---- list -----------------------------------------------------------------------------

type Show = "all" | "exercise" | "recording";

export function SessionList() {
  const [rows, setRows] = useState<SessionRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [show, setShow] = useState<Show>("all");
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
        <p>Every workout the robot runs and every recording you save lands here by itself. Run one from Live, or make some synthetic sessions to see how the review works.</p>
        <div class="row"><button class="btn primary" onClick={() => go("/live")}>Go to Live</button><button class="btn" onClick={seed}>Make synthetic sessions</button></div>
      </div>
    );
  }
  const synthetic = rows.filter((r) => r.synthetic).length;
  const count = (s: Show) => (s === "all" ? rows.length : rows.filter((r) => !r.unreadable && r.kind === s).length);
  const list = rows.filter((r) => show === "all" || (!r.unreadable && r.kind === show));
  return (
    <div class="sessions">
      <header class="page-head">
        <h1>Sessions</h1>
        <p>{rows.length} saved{synthetic ? `, ${synthetic} of them synthetic (generated, not measured)` : ""}. New sessions and recordings appear here by themselves.</p>
      </header>
      <div class="list-tools">
        <div class="chips" role="group" aria-label="Show">
          {([["all", "All"], ["exercise", "Exercise sessions"], ["recording", "Recordings"]] as [Show, string][]).map(([s, label]) => (
            <button key={s} class="chip" aria-pressed={show === s} onClick={() => setShow(s)}>{label}<small>{count(s)}</small></button>
          ))}
        </div>
        {synthetic > 0 && (
          <button class="btn quiet" onClick={() => { if (confirm(`Delete the ${synthetic} synthetic sessions?`)) attempt(async () => { await api("DELETE", "/api/sessions/synthetic"); await load(); }); }}>
            <Icon name="trash" size={18} />Remove synthetic sessions
          </button>
        )}
      </div>
      {list.length === 0 ? (
        <div class="list-empty"><Icon name="sessions" size={24} /><p><b>{show === "recording" ? "No recordings yet." : "No exercise sessions yet."}</b> {show === "recording" ? "Record an emote and save it; it is logged here." : "Run a plan from Live."}</p>
          <a class="btn" href={show === "recording" ? "#/record" : "#/live"}>{show === "recording" ? "Go to Record" : "Go to Live"}</a></div>
      ) : (
        <div class="session-table" role="group" aria-label="Sessions, newest first">
          <div class="session-row session-head" aria-hidden="true">
            <span>When</span><span>What</span><span>Score</span><span>Range reached</span><span>Reps</span><span>Length</span><span />
          </div>
          {list.map((r) => r.unreadable ? (
            <div key={r.id} class="session-row is-broken">
              <span>{r.id}</span><span>This file couldn't be read.</span><span /><span /><span /><span />
              <button class="btn icon-only quiet" aria-label={`Delete ${r.id}`} onClick={async () => { if (await removeSession(r.id, r.id)) load(); }}><Icon name="trash" size={18} /></button>
            </div>
          ) : (
            <div key={r.id} class="session-row">
              <a class="session-link" href={`#/sessions/${r.id}`} aria-label={`Open the session from ${when(r.started).date}, ${when(r.started).time}`} />
              <span class="cell-when">
                <b>{when(r.started).date}</b><small>{when(r.started).time}, {when(r.started).ago}</small>
              </span>
              <span class="cell-outcome">
                {r.kind === "recording" ? <span class="state"><Icon name="record" size={16} />Recording: {r.name}</span> : <StatusMark value={r.status} />}
                <small>{r.kind === "recording" ? "Recorded by hand and saved to the library" : r.headline ?? r.exercises.join(", ") ?? r.plan}</small>
                <span class="tags">
                  {r.flagged && <span class="tag tag-flag"><Icon name="flag" size={14} />Follow up</span>}
                  {r.synthetic && <span class="tag tag-syn">{r.kind === "recording" ? "Simulated" : "Synthetic"}</span>}
                </span>
              </span>
              <span data-label="Score" class="cell-score">{r.scores.overall.score != null ? <><b>{r.scores.overall.score}</b><small>of 100</small></> : <small>{r.kind === "recording" ? "not scored" : "no score"}</small>}</span>
              <span data-label="Range reached">{r.kind === "recording" ? <small>–</small> : <Meter value={r.scores.accuracy_pct} label="Range reached" />}</span>
              <span data-label="Reps" class="cell-reps">{r.kind === "recording" ? <small>–</small> : <><b>{r.scores.reps}</b> of {r.scores.target_reps}</>}</span>
              <span data-label="Length">{clock(r.duration_s)}</span>
              <button class="btn icon-only quiet row-action" aria-label={`Delete the session from ${when(r.started).date}, ${when(r.started).time}`}
                onClick={async () => { if (await removeSession(r.id, `${when(r.started).date}, ${when(r.started).time}`)) load(); }}><Icon name="trash" size={18} /></button>
            </div>
          ))}
        </div>
      )}
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
          <a class="btn" href={`#/coach/${session.id}`}><Icon name="coach" size={18} />Ask the coach</a>
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

/** Replays a set (or a recording) on a private twin, and reports the playhead. */
function Replay({ url, onTime, label }: { url: string; onTime?: (s: number | null) => void; label: string }) {
  const [clip, setClip] = useState<Clip | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState(1);
  const [at, setAt] = useState(0);
  useEffect(() => {
    setClip(null); setError(null); setPlaying(false); setAt(0); onTime?.(null);
    api<Clip>("GET", url).then(setClip).catch((e) => setError(e.message));
  }, [url]);
  const total = clip?.total_ms ?? 0;
  return (
    <div class="replay">
      <MotionTwin clip={clip} playing={playing} speed={speed} time={at} busy={!clip && !error} error={error} title={label} compact
        onTime={(ms) => { setAt(ms); onTime?.(ms / 1000); }} onEnd={() => setPlaying(false)} />
      <div class="replay-bar">
        <button class="btn primary" disabled={!clip} onClick={() => { if (!playing && at >= total) setAt(0); setPlaying(!playing); }}>
          <Icon name={playing ? "pause" : "replay"} />{playing ? "Pause" : at > 0 && at < total ? "Resume" : "Replay"}
        </button>
        <label class="replay-scrub"><span class="sr-only">Position in the replay</span>
          <input type="range" min={0} max={Math.max(1, total)} step={50} value={at} disabled={!clip}
            onInput={(e) => { setPlaying(false); const ms = +e.currentTarget.value; setAt(ms); onTime?.(ms / 1000); }} /></label>
        <span class="time">{clock(at / 1000)} / {clock(total / 1000)}</span>
        <label class="replay-speed">Speed
          <select value={speed} onChange={(e) => setSpeed(+e.currentTarget.value)}>
            <option value={0.5}>0.5×</option><option value={1}>1×</option><option value={2}>2×</option><option value={4}>4×</option>
          </select></label>
      </div>
    </div>
  );
}

function RecordingDetail({ session }: { session: Session }) {
  const rec = session.recording!;
  const [missing, setMissing] = useState(false);
  useEffect(() => { api("GET", `/api/animations/${rec.animation_id}`).then(() => setMissing(false)).catch(() => setMissing(true)); }, [rec.animation_id]);
  return (
    <>
      <section class="panel">
        <div class="panel-head"><h2>Recording: {rec.name}</h2><span class="tag">{cap(rec.kind)}</span></div>
        <dl class="facts">
          <div><dt>Recorded for</dt><dd><b>{(rec.take_ms / 1000).toFixed(1)}</b> s</dd></div>
          <div><dt>Kept after trimming</dt><dd><b>{(rec.kept_ms / 1000).toFixed(1)}</b> s</dd></div>
          <div><dt>Saved animation</dt><dd><b>{(rec.animation_ms / 1000).toFixed(1)}</b> s, {rec.keyframes} keyframes</dd></div>
          <div><dt>Total arm travel</dt><dd><b>{rec.travel_deg}</b>°</dd></div>
        </dl>
        <p class="hint">Joints that moved: {rec.joints_moved.length ? rec.joints_moved.map(words).join(", ") : "none moved more than 10 degrees"}. {rec.samples} samples were kept.</p>
      </section>
      <section class="panel">
        <div class="panel-head"><h2>Replay</h2></div>
        {missing ? <div class="chart-empty"><Icon name="library" size={24} /><p><b>The saved animation was deleted from the library.</b> The log above is all that is left of this recording.</p></div> : (
          <div class="replay-wrap">
            <Replay url={`/api/sessions/${session.id}/replay`} label={rec.name} />
            <div class="stack">
              <p>This is the motion that was saved to the library, played on the twin. It can be played on the robot from here too.</p>
              <PlayButton target={{ key: rec.animation_id, name: rec.name, id: rec.animation_id }} />
              <a class="btn" href="#/library"><Icon name="library" size={18} />Find it in the library</a>
            </div>
          </div>
        )}
      </section>
    </>
  );
}

export function SessionDetail({ id }: { id: string }) {
  const [session, setSession] = useState<Session | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pick, setPick] = useState(0);
  const [trends, setTrends] = useState<Trend[] | null>(null);
  const [cursor, setCursor] = useState<number | null>(null);
  const [replaying, setReplaying] = useState(false);
  useEffect(() => {
    setSession(null); setError(null); setPick(0); setReplaying(false); setCursor(null);
    api<Session>("GET", `/api/sessions/${id}`).then(setSession).catch((e) => setError(e.message));
    api<Trend[]>("GET", "/api/analytics/trends").then(setTrends).catch(() => setTrends([]));
  }, [id]);

  if (error) return <div class="page-empty"><Icon name="alert" size={28} /><h2>This session couldn't be opened</h2><p>{error}</p><a class="btn" href="#/sessions">Back to sessions</a></div>;
  if (!session) return <div class="page-empty"><p>Opening the session…</p></div>;

  const sets = [...session.sets, ...session.interrupted];
  const set = sets[Math.min(pick, sets.length - 1)];
  const w = when(session.started);
  const sc = session.scores;
  const duration = (session.ended ?? session.started) - session.started;
  const choose = (i: number) => { setPick(i); setReplaying(false); setCursor(null); };
  const remove = async () => { if (await removeSession(session.id, `${w.date}, ${w.time}`)) go("/sessions"); };
  const recording = session.kind === "recording" && session.recording;

  return (
    <div class="session">
      <ChartDefs />
      <header class="page-head">
        <a class="back" href="#/sessions"><Icon name="left" size={18} />Sessions</a>
        <h1>{w.date}, {w.time}</h1>
        <p>{session.plan}, {clock(duration)} long. {!recording && <StatusMark value={session.status} />}</p>
        <div class="tags">
          {session.flagged && <span class="tag tag-flag"><Icon name="flag" size={14} />Follow up</span>}
          {session.synthetic && <span class="tag tag-syn">{recording ? "Simulated recording (dry run)" : "Synthetic data: generated, not measured from a patient"}</span>}
        </div>
        <div class="row head-actions">
          {!recording && <a class="btn" href={`#/coach/${session.id}`}><Icon name="coach" size={18} />Ask the coach about this</a>}
          <button class="btn quiet" onClick={remove}><Icon name="trash" size={18} />Delete this session</button>
        </div>
      </header>

      {recording ? <RecordingDetail session={session} /> : (
        <>
          <Headline overall={sc.overall} insights={session.insights} scores={sc} />
          <SummaryPanel session={session} onChange={(summary) => setSession({ ...session, summary, summary_cached: true })} />

          {sets.length === 0 ? (
            <section class="panel"><div class="chart-empty"><Icon name="sessions" size={24} /><p><b>No sets were recorded.</b> The session ended before the first set, so there is nothing to analyse.</p></div></section>
          ) : (
            <>
              <nav class="set-picker" aria-label="Which set the charts below show">
                <span>Looking at</span>
                <div class="chips">
                  {sets.map((s, i) => (
                    <button key={setKey(s, i)} class="chip" aria-pressed={i === pick} onClick={() => choose(i)}>
                      {s.exercise}, set {s.set}{s.finished === false ? " (cut short)" : ""}<small>{s.scores.overall.score ?? "–"}</small>
                    </button>
                  ))}
                </div>
              </nav>

              <ChartCard title={`Joint angle over time against the target: ${set.exercise}, set ${set.set}`} unit="degrees over time" id="chart-trace"
                means="When the patient's line reaches the dashed target line and follows the demonstration's shape, the rep was done as shown. Hatched blocks are reps that drifted."
                aside={set.trace && <button class="btn toggle" aria-pressed={replaying} onClick={() => { setReplaying(!replaying); setCursor(null); }}><Icon name="replay" size={18} />Replay on the twin</button>}>
                {set.trace ? (
                  <div class={replaying ? "replay-wrap" : ""}>
                    {replaying && <Replay url={`/api/sessions/${session.id}/replay?set=${pick}`} label="What the patient did" onTime={setCursor} />}
                    <OverlayChart key={setKey(set, pick)} set={set} cursor={replaying ? cursor : null} />
                  </div>
                ) : <Unmeasured what="The joint angle" why="Sessions logged before pose tracking only have rep counts." />}
                {set.deviations.length > 0 && <p class="devs"><b>Form notes:</b> {set.deviations.join("; ")}.</p>}
              </ChartCard>

              <ChartCard title="Range of motion per joint" unit="degrees" id="chart-accuracy_pct"
                means="Longer bars mean the joint moved through more of its range. A shoulder bar well short of its target means the arms aren't being lifted as high as shown.">
                {set.rom_by_joint.length ? <RomChart rows={set.rom_by_joint} /> : <Unmeasured what="Range per joint" why="It needs each side tracked on its own; this set only has one combined angle." />}
                <p class="chart-facts">Range reached this set: <b>{set.scores.accuracy_pct ?? "–"}%</b> of the target. {FATIGUE[set.scores.fatigue.label]}{set.scores.fatigue.rom_change_pct != null && ` (range ${set.scores.fatigue.rom_change_pct > 0 ? "+" : ""}${set.scores.fatigue.rom_change_pct}% from first reps to last)`}.</p>
              </ChartCard>

              <ChartCard title="Reps: how many, and how good" unit="reps, whole session" id="chart-completion_pct"
                means="A rep counts as good when it reached the target height, came back down, and took about as long as the demonstration. The bars below show each rep's height.">
                <RepQuality sets={sets} />
                <div class="set-table" role="table" aria-label="Each set">
                  <div class="set-row set-head" role="row">
                    <span role="columnheader">Set</span><span role="columnheader">Each rep</span>
                    <span role="columnheader">Range</span><span role="columnheader">Pacing</span><span role="columnheader">Patient said</span>
                  </div>
                  {sets.map((s, i) => (
                    <div key={setKey(s, i)} class={`set-row ${i === pick ? "is-picked" : ""}`} role="row">
                      <span role="cell">
                        <button class="set-pick" aria-pressed={i === pick} onClick={() => choose(i)}>
                          <b>{s.exercise}</b><small>Set {s.set}: {s.reps} of {s.target_reps} reps{s.finished === false ? ", cut short" : ""}</small>
                        </button>
                      </span>
                      <span role="cell" data-label="Each rep"><RepStrip set={s} onPick={() => choose(i)} /></span>
                      <span role="cell" data-label="Range"><Meter value={s.scores.accuracy_pct} label="Range reached" /></span>
                      <span role="cell" data-label="Pacing"><Meter value={s.scores.pacing_pct} label="Pacing" /></span>
                      <span role="cell" data-label="Patient said" class="cell-said">
                        {s.answer ? <>“{s.answer}”</> : <small>{s.finished === false ? "Not asked" : "No answer"}</small>}
                        {s.trouble && s.trouble !== "none" && <span class={`tag ${s.trouble === "pain" ? "tag-flag" : "tag-off"}`}>{cap(s.trouble)}</span>}
                      </span>
                    </div>
                  ))}
                </div>
                {set.reps_detail.length > 0 && (
                  <details class="fold">
                    <summary>Table of every rep in {set.exercise}, set {set.set}</summary>
                    <div class="table-scroll"><table class="rep-table">
                      <thead><tr><th>Rep</th><th>Peak</th><th>Range</th><th>Of target</th><th>Time</th><th>Against demo</th><th>Smooth</th><th>Balance</th><th>Notes</th></tr></thead>
                      <tbody>
                        {set.reps_detail.map((r) => (
                          <tr key={r.n}><td>{r.n}</td><td>{Math.round(r.peak_deg)}°</td><td>{Math.round(r.rom_deg)}°</td><td>{r.accuracy_pct ?? "–"}%</td>
                            <td>{r.duration_s.toFixed(1)} s</td><td>{r.pace_ratio != null ? `${r.pace_ratio.toFixed(2)}×` : "–"}</td>
                            <td>{r.smoothness_pct != null ? `${r.smoothness_pct}%` : "–"}</td><td>{r.symmetry_pct != null ? `${r.symmetry_pct}%` : "–"}</td>
                            <td>{r.misses.length ? r.misses.map(words).join(", ") : "on target"}</td></tr>
                        ))}
                      </tbody>
                    </table></div>
                  </details>
                )}
              </ChartCard>

              <div class="chart-pair">
                <ChartCard title="Tempo" unit="seconds per rep" id="chart-pacing_pct"
                  means="Bars near the demo line mean the patient kept the demonstrated pace. Bars getting taller toward the right mean the reps slowed, often a sign of tiring.">
                  {set.reps_detail.length ? <TempoChart set={set} /> : <Unmeasured what="Tempo" why="No reps were timed in this set." />}
                </ChartCard>
                <ChartCard title="Smoothness" unit="percent, per rep" id="chart-smoothness_pct"
                  means="Higher is steadier. A low bar means that rep stopped and started or shook. A clean rep through a camera tracker usually reads 50% to 70%, not 100%.">
                  <SmoothnessChart set={set} />
                </ChartCard>
              </div>

              <div class="chart-pair">
                <ChartCard title="Left against right" unit="degrees of range, per rep" id="chart-symmetry_pct"
                  means="Bars of equal length on both sides mean both arms did the same work. One side consistently shorter is worth mentioning to the therapist.">
                  {set.symmetry ? <SymmetryChart set={set} symmetry={set.symmetry} /> : <Unmeasured what="Left and right balance" why="It needs each arm tracked on its own; this set only has one combined angle." />}
                </ChartCard>
                <ChartCard title="Where each rep drifted" unit="degrees from the demonstration" id="chart-heat"
                  means="Blue cells are where the patient was below the demonstrated position, orange where they were above. A blue column in the middle means not lifting high enough at the top.">
                  {set.heatmap ? <HeatmapChart heat={set.heatmap} tolerance={set.target.tolerance} /> : <Unmeasured what="Deviation" why="It needs the joint-angle trace and a target range." />}
                </ChartCard>
              </div>
            </>
          )}

          <ChartCard title="Session over session" unit="score, 0 to 100" id="chart-trend"
            means="A line that climbs from left to right means the patient is improving between visits. The large dot is the session on this page.">
            {trends == null ? <p class="hint">Reading earlier sessions…</p> : <TrendChart trends={trends} current={session.id} />}
          </ChartCard>

          <section class="panel">
            <h2>Safety events, prompts and pauses</h2>
            <EventTimeline sets={sets} events={session.events} duration={duration} />
            {session.notes.length > 0 && <p class="devs"><b>Notes from the robot:</b> {session.notes.join("; ")}</p>}
          </section>

          <Calories session={session} />
        </>
      )}
    </div>
  );
}
