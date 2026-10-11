// Record: move the robot's arms by hand, trim the take, name it, save it, play it back.
import { signal } from "@preact/signals";
import { useEffect, useState } from "preact/hooks";
import { api, attempt, Clip, Keyframe, recorder, status, words } from "./api";
import { Icon } from "./icons";
import { MotionTwin } from "./motion";
import { PlayButton, RobotBar } from "./play";
import { TwinPanel, twinView } from "./twin";

interface Take { id: string; duration_ms: number; simulated: boolean; hz: number; frames: { t_ms: number; servos: Record<string, number> }[]; joints: string[] }
interface Saved { animation: { id: string; name: string; keyframes: Keyframe[]; kind: string }; session: string }

// Kept across visits to other tabs, so a take isn't lost by looking something up.
const take = signal<Take | null>(null);
const trim = signal<[number, number]>([0, 0]);
const saved = signal<Saved | null>(null);
const name = signal("");
const kind = signal<"emote" | "exercise">("emote");

const JOINT_COLOR = ["var(--series-1)", "var(--series-2)", "var(--series-3)", "var(--series-4)", "var(--series-5)", "var(--series-6)"];

function TrimChart({ t, range, cursor }: { t: Take; range: [number, number]; cursor: number | null }) {
  const W = 720, H = 150, L = 6, R = 6, T = 8, B = 22;
  const x = (ms: number) => L + (ms / Math.max(1, t.duration_ms)) * (W - L - R);
  const y = (p: number) => T + (1 - p / 1000) * (H - T - B);
  const secs = Array.from({ length: Math.floor(t.duration_ms / 1000) + 1 }, (_, i) => i);
  const every = secs.length > 24 ? 5 : secs.length > 12 ? 2 : 1;
  return (
    <svg class="trim-chart" viewBox={`0 0 ${W} ${H}`} role="img" preserveAspectRatio="none"
      aria-label={`Recorded arm positions over ${(t.duration_ms / 1000).toFixed(1)} seconds. Keeping ${(range[0] / 1000).toFixed(1)} to ${(range[1] / 1000).toFixed(1)} seconds.`}>
      <rect x={x(range[0])} width={Math.max(1, x(range[1]) - x(range[0]))} y={T} height={H - T - B} class="trim-keep" />
      {t.joints.map((j, i) => (
        <path key={j} fill="none" stroke={JOINT_COLOR[i % 6]} stroke-width="2" vector-effect="non-scaling-stroke"
          d={t.frames.map((f, n) => `${n ? "L" : "M"}${x(f.t_ms).toFixed(1)} ${y(f.servos[j] ?? 500).toFixed(1)}`).join("")} />
      ))}
      <rect x={L} width={Math.max(0, x(range[0]) - L)} y={T} height={H - T - B} class="trim-cut" />
      <rect x={x(range[1])} width={Math.max(0, W - R - x(range[1]))} y={T} height={H - T - B} class="trim-cut" />
      <line x1={x(range[0])} x2={x(range[0])} y1={T - 4} y2={H - B + 4} class="trim-edge" />
      <line x1={x(range[1])} x2={x(range[1])} y1={T - 4} y2={H - B + 4} class="trim-edge" />
      {cursor != null && <line x1={x(range[0] + cursor)} x2={x(range[0] + cursor)} y1={T} y2={H - B} class="crosshair" />}
      {secs.filter((s) => s % every === 0).map((s) => <text key={s} x={x(s * 1000)} y={H - 6} class="axis" text-anchor={s === 0 ? "start" : "middle"}>{s}s</text>)}
    </svg>
  );
}

export function Record() {
  const rec = recorder.value;
  const t = take.value;
  const [busy, setBusy] = useState(false);
  const [draft, setDraft] = useState<{ clip: Clip; keyframes: number } | null>(null);
  const [draftError, setDraftError] = useState<string | null>(null);
  const [solving, setSolving] = useState(false);
  const [playing, setPlaying] = useState(true);
  const [cursor, setCursor] = useState<number | null>(null);
  const [saving, setSaving] = useState(false);
  const robotDown = status.value?.links.robot.state === "down";

  useEffect(() => { twinView("actual"); }, []);

  // A finished recording arrives as a take id; fetch its samples once.
  useEffect(() => {
    const id = rec.take?.id;
    if (!id || rec.recording || t?.id === id) return;
    api<Take>("GET", `/api/recorder/takes/${id}`).then((got) => {
      take.value = got; trim.value = [0, got.duration_ms]; saved.value = null;
      if (!name.value) name.value = "";
    }).catch(() => { /* the take was dropped by a server restart; record again */ });
  }, [rec.take?.id, rec.recording]);

  // Re-solve the preview shortly after the trim handles settle.
  const [a, b] = trim.value;
  useEffect(() => {
    if (!t) { setDraft(null); return; }
    setSolving(true);
    const timer = setTimeout(async () => {
      try {
        const d = await api("POST", `/api/recorder/takes/${t.id}/draft`, { start_ms: a, end_ms: b });
        const clip = await api<Clip>("POST", "/api/preview/solve", { keyframes: d.keyframes, hz: 20 });
        setDraft({ clip, keyframes: d.keyframes.length }); setDraftError(null);
      } catch (e) { setDraft(null); setDraftError(e instanceof Error ? e.message : String(e)); }
      setSolving(false);
    }, 350);
    return () => clearTimeout(timer);
  }, [t?.id, a, b]);

  const start = async () => { setBusy(true); saved.value = null; take.value = null; await attempt(() => api("POST", "/api/recorder/start", { coast: true })); setBusy(false); };
  const stop = async () => { setBusy(true); await attempt(() => api("POST", "/api/recorder/stop")); setBusy(false); };
  const discard = () => { if (!saved.value && !confirm("Discard this recording?")) return; take.value = null; saved.value = null; name.value = ""; };
  const save = async () => {
    if (!t) return;
    setSaving(true);
    const out = await attempt(() => api<Saved>("POST", `/api/recorder/takes/${t.id}/save`, { name: name.value, start_ms: a, end_ms: b, kind: kind.value }),
      `Saved “${name.value.trim()}” to the library.`);
    setSaving(false);
    if (out) saved.value = out;
  };

  const step = rec.recording ? 1 : !t ? 0 : saved.value ? 3 : 2;
  const STEPS = ["Record", "Recording", "Trim and name", "Play it back"];
  return (
    <div class="record">
      <header class="page-head">
        <h1>Record an emote</h1>
        <p>Let the arms go limp, move them by hand, and keep the motion.</p>
      </header>
      <ol class="wizard" aria-label="Progress">
        {["Record", "Trim and name", "Save and play"].map((label, i) => {
          const at = step <= 1 ? 0 : step === 2 ? 1 : 2;
          return <li key={label} class={i < at ? "is-done" : i === at ? "is-now" : ""} aria-current={i === at ? "step" : undefined}><span>{i + 1}</span>{label}</li>;
        })}
      </ol>
      <span class="sr-only" role="status">{STEPS[step]}</span>

      <div class="record-cols">
        <div class="record-main">
          <section class="panel">
            <div class="panel-head"><h2>{rec.recording ? "Recording…" : t ? "Record again" : "Record"}</h2>
              {rec.simulated && (rec.recording || t) && <span class="tag tag-syn">Dry run: a simulated hand moves the arms</span>}</div>
            {rec.recording ? (
              <div class="rec-live">
                <span class="rec-dot" aria-hidden="true" />
                <b class="rec-time" aria-live="off">{rec.seconds.toFixed(1)} s</b>
                <span class="hint">{rec.frames} samples. The arms are limp: move them now. Stops by itself at 60 seconds.</span>
                <button class="btn primary big" disabled={busy} onClick={stop}><Icon name="stop" />Stop recording</button>
              </div>
            ) : (
              <>
                <p class="hint">When you press record the robot's arms go limp so you can pose them. The legs keep holding. When you stop, the arms hold again.</p>
                <div class="row">
                  <button class="btn primary big" disabled={busy || robotDown} onClick={start}><Icon name="record" />{t ? "Record a new take" : "Start recording"}</button>
                  {t && <button class="btn quiet" onClick={discard}><Icon name="trash" size={18} />Discard this take</button>}
                </div>
                {robotDown && <p class="note bad"><Icon name="alert" size={16} />The robot isn't reachable, so there is nothing to record from. Check Connections.</p>}
                {rec.error && <p class="note bad" role="alert"><Icon name="alert" size={16} />{rec.error}</p>}
                {rec.limp && <p class="note bad" role="alert"><Icon name="alert" size={16} />The arms may still be limp. <button class="btn" onClick={() => attempt(() => api("POST", "/api/record/hold"), "The arms are holding again.")}>Hold the arms</button></p>}
              </>
            )}
          </section>

          {t && !rec.recording && (
            <section class="panel">
              <div class="panel-head"><h2>Trim</h2><span class="hint">Keeping {((b - a) / 1000).toFixed(1)} s of {(t.duration_ms / 1000).toFixed(1)} s{draft ? `, ${draft.keyframes} keyframes` : ""}</span></div>
              <TrimChart t={t} range={[a, b]} cursor={playing ? cursor : null} />
              <ul class="trim-legend" aria-label="Lines">
                {t.joints.map((j, i) => <li key={j}><i style={{ background: JOINT_COLOR[i % 6] }} />{words(j)}</li>)}
              </ul>
              <div class="trim-sliders">
                <label>Start at <output>{(a / 1000).toFixed(1)} s</output>
                  <input type="range" min={0} max={t.duration_ms} step={100} value={a} disabled={!!saved.value}
                    onInput={(e) => { trim.value = [Math.min(+e.currentTarget.value, b - 300), b]; }} /></label>
                <label>End at <output>{(b / 1000).toFixed(1)} s</output>
                  <input type="range" min={0} max={t.duration_ms} step={100} value={b} disabled={!!saved.value}
                    onInput={(e) => { trim.value = [a, Math.max(+e.currentTarget.value, a + 300)]; }} /></label>
              </div>
              <p class="hint">What this shows: each line is one arm joint's position over the take. The shaded part is kept; drag the sliders to cut the fumbling at the start and end. The saved motion always finishes with the arms down.</p>
              {draftError && <p class="note bad" role="alert"><Icon name="alert" size={16} />{draftError}</p>}
            </section>
          )}

          {t && !rec.recording && (
            <section class="panel">
              <div class="panel-head"><h2>{saved.value ? "Saved" : "Name and save"}</h2></div>
              {saved.value ? (
                <div class="stack">
                  <p class="note"><Icon name="check" size={16} /><span>“{saved.value.animation.name}” is in the library, and this recording is logged under Sessions.</span></p>
                  <PlayButton big target={{ key: saved.value.animation.id, name: saved.value.animation.name, id: saved.value.animation.id }} />
                  <div class="row">
                    <a class="btn" href="#/library"><Icon name="library" size={18} />See it in the library</a>
                    <a class="btn" href={`#/sessions/${saved.value.session}`}><Icon name="sessions" size={18} />Open the recording's session</a>
                    <button class="btn quiet" onClick={discard}>Record another</button>
                  </div>
                </div>
              ) : (
                <form class="stack" onSubmit={(e) => { e.preventDefault(); save(); }}>
                  <div class="form-grid">
                    <label class="wide">Name
                      <input type="text" value={name.value} maxLength={80} placeholder="Big wave" required onInput={(e) => { name.value = e.currentTarget.value; }} /></label>
                    <label>Save as
                      <select value={kind.value} onChange={(e) => { kind.value = e.currentTarget.value as "emote" | "exercise"; }}>
                        <option value="emote">Emote</option><option value="exercise">Exercise</option>
                      </select></label>
                  </div>
                  <div class="row">
                    <button class="btn primary" disabled={saving || !name.value.trim() || !draft}><Icon name="save" />{saving ? "Saving…" : "Save to the library"}</button>
                    {!name.value.trim() && <span class="hint">Give it a name to save it.</span>}
                  </div>
                </form>
              )}
            </section>
          )}
        </div>

        <aside class="record-side">
          {t && !rec.recording ? (
            <>
              <MotionTwin clip={draft?.clip ?? null} playing={playing && !!draft} loop busy={solving && !draft} error={draftError} title="Trimmed preview"
                onTime={setCursor} note={draft ? `${(draft.clip.total_ms / 1000).toFixed(1)} s with the return to rest` : undefined} />
              <div class="row">
                <button class="btn" onClick={() => setPlaying(!playing)} disabled={!draft}><Icon name={playing ? "pause" : "play"} />{playing ? "Pause preview" : "Play preview"}</button>
              </div>
            </>
          ) : <TwinPanel />}
          <RobotBar stacked />
        </aside>
      </div>
    </div>
  );
}
