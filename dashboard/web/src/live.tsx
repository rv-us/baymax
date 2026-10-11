// Live: run a session and coach it. Everything the robot does here goes through the server.
import { useEffect, useRef, useState } from "preact/hooks";
import { api, attempt, live, playback, robot, safety, status, voice, words } from "./api";
import { CameraPanel } from "./camera";
import { Icon } from "./icons";
import { go } from "./route";
import { TrackingPanel } from "./tracking";
import { TwinPanel, twinView } from "./twin";

const STEPS: [string, string][] = [
  ["READY_CHECK", "Ready?"], ["INTRODUCE", "Explain"], ["DEMONSTRATE", "Show"],
  ["PROMPT_START", "Count in"], ["MIRROR", "Patient's turn"], ["SET_REVIEW", "Check in"],
];
const QUICK: Record<string, string[]> = {
  ready: ["Yes, I'm ready", "Not yet"],
  trouble: ["No, it was fine", "That was hard", "My shoulder hurts"],
};
const MOODS: [string, [number, number, number]][] = [
  ["Calm blue", [0, 120, 255]], ["Warm amber", [255, 120, 0]], ["Go green", [0, 200, 80]], ["Soft white", [180, 180, 180]], ["Off", [0, 0, 0]],
];

/** The tracked joint against the demonstration, as it happens. */
function LiveTrace() {
  const l = live.value;
  const buf = useRef<{ a: number | null; r: number }[]>([]);
  const key = `${l.exercise?.index}-${l.set}`;
  const lastKey = useRef(key);
  if (lastKey.current !== key) { lastKey.current = key; buf.current = []; }
  if (l.state === "MIRROR" && l.angle !== undefined) {
    buf.current.push({ a: l.angle ?? null, r: l.reference ?? 0 });
    if (buf.current.length > 150) buf.current.shift();
  }
  const pts = buf.current;
  const top = l.target?.top ?? 90, bottom = l.target?.bottom ?? 0;
  const W = 600, H = 120, pad = 8;
  const y = (v: number) => H - pad - ((v - bottom + 10) / (top - bottom + 30)) * (H - 2 * pad);
  const x = (i: number) => (i / 149) * W;
  const line = (get: (p: { a: number | null; r: number }) => number | null) => {
    let d = "", pen = false;
    pts.forEach((p, i) => { const v = get(p); if (v == null) { pen = false; return; } d += `${pen ? "L" : "M"}${x(i).toFixed(1)} ${y(v).toFixed(1)}`; pen = true; });
    return d;
  };
  return (
    <figure class="live-trace">
      <svg viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img"
        aria-label={`Tracked joint angle, now ${l.angle != null ? Math.round(l.angle) : "unknown"} degrees`}>
        <line x1="0" x2={W} y1={y(top)} y2={y(top)} class="chart-target" />
        <line x1="0" x2={W} y1={y(bottom)} y2={y(bottom)} class="chart-target" />
        <path d={line((p) => p.r)} class="chart-ref" />
        <path d={line((p) => p.a)} class="chart-patient" />
      </svg>
      <figcaption>
        <span class="legend-item"><i class="swatch line-patient" />Patient</span>
        <span class="legend-item"><i class="swatch line-ref" />Robot, a little ahead</span>
        <span class="legend-item">Target {bottom}° to {top}°</span>
      </figcaption>
    </figure>
  );
}

const OUTCOME: Record<string, string> = {
  hearing: "hearing you…", answer: "taken as the answer", pain: "pain: the robot was stopped", echo: "the robot's own voice, ignored",
  unprompted: "nothing was asked, so it was ignored", stale: "too old, ignored", error: "couldn't be handled",
};

/** What the GX10's microphone hears, as it is read: the words, and what the dashboard did with them. */
function Microphone() {
  const v = voice.value;
  const last = v.last;
  const [now, setNow] = useState(Date.now());
  useEffect(() => { const id = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(id); }, []);
  const fresh = last && now - last.t * 1000 < 20000;
  return (
    <div class={`mic mic-${v.state}`} role="status" aria-live="polite">
      <p class="mic-state">
        <Icon name={v.state === "listening" ? "speak" : "alert"} size={16} />
        <span>
          {v.state === "listening" && (v.muted ? "The robot is talking, so the microphone is off." : "Listening on the GX10. Just say your answer.")}
          {v.state === "connecting" && "Connecting to the GX10's microphone…"}
          {v.state === "down" && (v.error ?? "The GX10's microphone isn't answering.")}
          {v.state === "off" && "No microphone: type the patient's answers below."}
        </span>
      </p>
      {fresh && (
        <p class={`mic-caption outcome-${last!.outcome}`}>
          <Icon name="patient" size={18} />
          <span>“{last!.text}”<em>{OUTCOME[last!.outcome] ?? last!.outcome}</em></span>
        </p>
      )}
    </div>
  );
}

const plain = ({ kind, source, ...ex }: any) => ex;

function Stepper({ label, value, name, onChange }: { label: string; value: number; name: string; onChange: (v: number) => void }) {
  return (
    <div class="stepper" role="group" aria-label={`${label} for ${name}`}>
      <button type="button" aria-label={`Fewer ${label.toLowerCase()}`} disabled={value <= 1} onClick={() => onChange(value - 1)}>−</button>
      <output><b>{value}</b><span>{label}</span></output>
      <button type="button" aria-label={`More ${label.toLowerCase()}`} disabled={value >= 50} onClick={() => onChange(value + 1)}>+</button>
    </div>
  );
}

function SessionRunner() {
  const l = live.value;
  const [plans, setPlans] = useState<any[] | null>(null);
  const [plan, setPlan] = useState("");
  const [simulate, setSimulate] = useState(true);
  const [pace, setPace] = useState(1);
  const [answer, setAnswer] = useState("");
  // The session being put together: picking a plan copies its exercises here, then anything can change.
  const [draft, setDraft] = useState<any[]>([]);
  const [library, setLibrary] = useState<any[]>([]);
  const [saveName, setSaveName] = useState("");
  const pick = (id: string, list = plans) => {
    setPlan(id);
    setDraft(structuredClone(list?.find((p) => p.id === id)?.exercises ?? []));
  };
  useEffect(() => {
    api("GET", "/api/plans").then((p) => { setPlans(p); setPlan((v) => v || p[0]?.id || ""); pick(p[0]?.id ?? "", p); }).catch(() => setPlans([]));
    api("GET", "/api/exercises").then(setLibrary).catch(() => setLibrary([]));
  }, []);
  const edit = (i: number, patch: Record<string, number>) => setDraft((d) => d.map((e, j) => (j === i ? { ...e, ...patch } : e)));
  const count = (v: string) => Math.max(1, Math.min(50, Math.round(+v) || 1));
  const move = (i: number, by: number) => setDraft((d) => {
    const n = [...d]; const j = i + by;
    if (j < 0 || j >= n.length) return d;
    [n[i], n[j]] = [n[j], n[i]]; return n;
  });
  const addable = library.filter((x) => !draft.some((e) => e.name === x.name));
  const add = (name: string) => { const x = library.find((l) => l.name === name); if (x) setDraft((d) => [...d, structuredClone(x)]); };
  const startDraft = () => {
    const base = plans?.find((p) => p.id === plan);
    const slug = saveName.trim().toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "").slice(0, 48);
    attempt(() => api("POST", "/api/live/start", {
      exercises: draft.map(plain), name: saveName.trim() || (base ? `${base.name} (edited)` : "Custom session"),
      save_as: slug || undefined, simulate, pace,
    }), slug ? "Saved the plan and started." : undefined);
  };
  // Until Start is pressed, the twin loops the session as built (twin only; the robot isn't touched).
  const [loop, setLoop] = useState(true);
  const [showing, setShowing] = useState<number | null>(null);
  const draftNow = useRef(draft);
  draftNow.current = draft;
  const playing = playback.value.playing, hasDraft = draft.length > 0;
  useEffect(() => {
    setShowing(null);
    if (l.running || !loop || !hasDraft || playing) return;
    const t0 = performance.now();
    let alive = true, busy = false;
    const id = setInterval(async () => {
      if (busy) return;
      busy = true;
      try {
        const r = await api("POST", "/api/preview/session", { exercises: draftNow.current.map(plain), t_ms: performance.now() - t0 });
        if (alive && !r.skipped) setShowing(r.index ?? null);
      } catch { /* the loop just skips a beat */ }
      busy = false;
    }, 150);
    return () => {
      alive = false; clearInterval(id);
      if (!live.value.running) api("POST", "/twin/api/pose", { name: "default_standing_pose" }).catch(() => {});
    };
  }, [l.running, loop, hasDraft, playing]);
  const stopped = safety.value.stop;
  const send = (text: string) => { if (text.trim()) attempt(() => api("POST", "/api/live/answer", { text })); setAnswer(""); };

  if (!l.running) {
    const chosen = plans?.find((p) => p.id === plan);
    return (
      <section class="panel runner">
        <h2>Start a session</h2>
        {l.last && (
          <p class="note" role="status">
            <Icon name={l.last.status === "completed" ? "check" : "flag"} size={16} />
            <span>Last session: {words(l.last.status)}{l.last.flagged ? ", flagged for follow-up" : ""}.{" "}
              {l.last.id && <a href={`#/sessions/${l.last.id}`}>See how it went</a>}</span>
          </p>
        )}
        {l.error && <p class="note bad"><Icon name="alert" size={16} />{l.error}</p>}
        {plans && (
          <>
            <label>Start from
              <select value={plan} onChange={(e) => pick(e.currentTarget.value)}>
                <option value="">A blank session</option>
                {plans.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
              </select>
            </label>
            {chosen?.error && <p class="note bad"><Icon name="alert" size={16} />{chosen.error}</p>}
            <ol class="draft" aria-label="Exercises in this session">
              {draft.map((e, i) => (
                <li key={e.name} class={showing === i ? "is-showing" : ""}>
                  <span class="draft-num" aria-hidden="true">{i + 1}</span>
                  <div class="draft-name">
                    <b>{e.name}</b>
                    <span>{showing === i ? "Showing on the twin" : e.cue}</span>
                  </div>
                  <div class="draft-steps">
                    <Stepper label="Sets" name={e.name} value={e.sets} onChange={(v) => edit(i, { sets: count(String(v)) })} />
                  <Stepper label="Reps" name={e.name} value={e.reps} onChange={(v) => edit(i, { reps: count(String(v)) })} />
                  </div>
                  <span class="draft-tools">
                    <button class="btn icon-only quiet" disabled={i === 0} aria-label={`Move ${e.name} up`} onClick={() => move(i, -1)}><Icon name="up" size={16} /></button>
                    <button class="btn icon-only quiet" disabled={i === draft.length - 1} aria-label={`Move ${e.name} down`} onClick={() => move(i, 1)}><Icon name="down" size={16} /></button>
                    <button class="btn icon-only quiet" aria-label={`Remove ${e.name}`} onClick={() => setDraft((d) => d.filter((_, j) => j !== i))}><Icon name="trash" size={16} /></button>
                  </span>
                </li>
              ))}
            </ol>
            {!draft.length && <p class="draft-empty">Nothing in this session yet. Pick a plan above, or add exercises below.</p>}
            {!!addable.length && (
              <div class="adder">
                <span class="adder-label" id="adder-label">Add to this session</span>
                <div class="chips" role="group" aria-labelledby="adder-label">
                  {addable.map((x) => (
                    <button key={x.name} class="chip" onClick={() => add(x.name)}>
                      <span aria-hidden="true">+</span>{x.name}{x.kind === "emote" && <i>emote</i>}
                    </button>
                  ))}
                </div>
              </div>
            )}
            <div class="draft-foot">
              <button class="btn toggle" aria-pressed={loop} disabled={!hasDraft} onClick={() => setLoop(!loop)}>Loop on the twin</button>
              <details class="save-as">
                <summary>Keep this as a plan</summary>
                <input type="text" value={saveName} maxLength={60} aria-label="Plan name" placeholder="Name it to save it when you start" onInput={(e) => setSaveName(e.currentTarget.value)} />
              </details>
            </div>
            <label class="check"><input type="checkbox" checked={simulate} onChange={(e) => setSimulate(e.currentTarget.checked)} />
              Simulate the patient's answers</label>
            {status.value?.dry_run && !status.value?.webcam && <label>Pace
              <select value={pace} onChange={(e) => setPace(+e.currentTarget.value)}>
                <option value="1">Real time</option><option value="3">3× faster (demo)</option><option value="6">6× faster (demo)</option>
              </select>
            </label>}
            {status.value?.webcam?.owned_by_gx10
              ? <p class="note"><Icon name="camera" size={16} />The GX10 owns the robot's camera and tracks the patient's arms, so the session runs in real time on real movement and the robot keeps a quarter of a rep ahead. If the GX10 stops answering the session pauses.</p>
              : status.value?.webcam
              ? <p class="note"><Icon name="camera" size={16} />The laptop webcam tracks the patient's arms, so the session runs in real time on real movement. Face the camera with your arms in view.</p>
              : status.value?.dry_run
                ? <p class="note"><Icon name="alert" size={16} />No webcam, so the patient's movement in a session is simulated. The log is marked synthetic.</p>
                : <p class="note"><Icon name="camera" size={16} />The robot's own camera watches the patient and the GX10 tracks their arms, so the session runs in real time on real movement and the robot keeps a quarter of a rep ahead. If the camera or the tracker can't be reached, the patient is simulated instead and the log says so.</p>}
            <button class="btn primary big" disabled={!!stopped || !draft.length} onClick={startDraft}>
              <Icon name="play" />Start session
            </button>
            {stopped && <p class="note bad">A safety stop is active. Clear it in the banner above first.</p>}
          </>
        )}
      </section>
    );
  }

  const step = STEPS.findIndex(([s]) => s === l.state);
  const quick = l.waiting ? (l.waiting.toLowerCase().includes("ready") ? QUICK.ready : QUICK.trouble) : [];
  return (
    <section class="panel runner is-running">
      <div class="runner-head">
        <h2>{l.exercise?.name ?? l.plan}</h2>
        <span class="tag">Exercise {(l.exercise?.index ?? 0) + 1} of {l.exercise?.count ?? 1}</span>
        <button class="btn danger" onClick={() => attempt(() => api("POST", "/api/live/stop"))}><Icon name="stop" />End session</button>
      </div>
      <ol class="steps" aria-label="Where the session is">
        {STEPS.map(([s, label], i) => (
          <li key={s} class={i === step ? "is-now" : i < step ? "is-done" : ""} aria-current={i === step ? "step" : undefined}>{label}</li>
        ))}
        {l.state === "PAUSED" && <li class="is-now is-warn" aria-current="step">Paused: patient out of view</li>}
        {l.state === "SAFETY_STOP" && <li class="is-now is-warn" aria-current="step">Safety stop</li>}
      </ol>
      <div class="rep-row">
        <div class="rep-count" aria-live="polite">
          <b>{l.reps ?? 0}</b><span>of {l.target_reps ?? 0} reps</span>
        </div>
        <div class="rep-meta">
          <span>Set {l.set ?? 1} of {l.sets ?? 1}</span>
          {l.state === "MIRROR" && l.lead != null && (
            <span class="lead" title="The robot keeps about a quarter of a rep ahead and waits if the patient slows down">
              Robot {l.lead >= 0.05 ? `${Math.round(l.lead * 100)}% of a rep ahead` : "level with the patient"}
            </span>
          )}
          <div class="pips" aria-hidden="true">
            {Array.from({ length: l.target_reps ?? 0 }, (_, i) => <i key={i} class={i < (l.reps ?? 0) ? "on" : ""} />)}
          </div>
        </div>
      </div>
      <LiveTrace />
      <div class="said">
        <p><Icon name="speak" size={18} /><span>{l.said ?? "…"}</span></p>
        {l.simulate ? (l.heard && !l.waiting && <p class="heard"><Icon name="patient" size={18} /><span>{l.heard}</span></p>) : <Microphone />}
      </div>
      {l.waiting && (
        <div class="answer">
          <strong>{l.simulate ? "Waiting for the simulated patient…" : voice.value.state === "listening" ? "Listening for the patient's answer… or type it" : "What did the patient say?"}</strong>
          {!l.simulate && (
            <>
              <div class="row">{quick.map((q) => <button key={q} class="btn" onClick={() => send(q)}>{q}</button>)}</div>
              <form class="row" onSubmit={(e) => { e.preventDefault(); send(answer); }}>
                <input type="text" value={answer} placeholder="Or type their answer" aria-label="The patient's answer" onInput={(e) => setAnswer(e.currentTarget.value)} />
                <button class="btn primary">Send</button>
              </form>
            </>
          )}
        </div>
      )}
    </section>
  );
}

function Coaching() {
  const l = live.value;
  const [line, setLine] = useState("");
  const [matrix, setMatrix] = useState("");
  const [heard, setHeard] = useState("");
  const [busy, setBusy] = useState(false);
  const stopped = !!safety.value.stop;
  const act = async (fn: () => Promise<unknown>, done?: string) => { setBusy(true); await attempt(fn, done); setBusy(false); };
  const repos = (kind: string) => act(() => api("POST", "/api/live/reposition", { kind }));
  const moving = busy || stopped || !(status.value?.motion.ok ?? false);
  const nudge = (side: string, position: string) => act(() => robot("actions/arm", { side, position }));

  return (
    <section class="panel coaching">
      <h2>Coaching</h2>

      {l.advice && (
        <div class="advice" role="status">
          <Icon name="eye" /><span>{l.advice.text}</span>
          <button class="btn" disabled={busy || (l.advice.kind.startsWith("robot") && moving)} onClick={() => repos(l.advice!.kind)}>Do it</button>
        </div>
      )}

      <h3>Where everyone stands</h3>
      <div class="row">
        <button class="btn" disabled={busy} onClick={() => repos("closer")}><Icon name="speak" />Ask to come closer</button>
        <button class="btn" disabled={busy} onClick={() => repos("back")}><Icon name="speak" />Ask to step back</button>
      </div>
      <div class="pad" role="group" aria-label="Move the robot one step">
        <button class="btn" style={{ gridArea: "f" }} disabled={moving} onClick={() => repos("robot_forward")} aria-label="Robot one step forward"><Icon name="up" /></button>
        <button class="btn" style={{ gridArea: "l" }} disabled={moving} onClick={() => repos("robot_left")} aria-label="Robot one side-step to its left"><Icon name="left" /></button>
        <button class="btn" style={{ gridArea: "s" }} disabled={moving} onClick={() => act(() => robot("actions/stand"))}>Stand</button>
        <button class="btn" style={{ gridArea: "r" }} disabled={moving} onClick={() => repos("robot_right")} aria-label="Robot one side-step to its right"><Icon name="right" /></button>
        <button class="btn" style={{ gridArea: "b" }} disabled={moving} onClick={() => repos("robot_back")} aria-label="Robot one step back"><Icon name="down" /></button>
      </div>
      <div class="row">
        <button class="btn" disabled={moving} onClick={() => act(() => robot("actions/turn", { direction: "left" }))}>Turn left</button>
        <button class="btn" disabled={moving} onClick={() => act(() => robot("actions/turn", { direction: "right" }))}>Turn right</button>
        <button class="btn" disabled={moving} onClick={() => nudge("both", "forward")}>Arms forward</button>
        <button class="btn" disabled={moving} onClick={() => nudge("both", "up")}>Arms up</button>
        <button class="btn" disabled={moving} onClick={() => nudge("both", "down")}>Arms down</button>
      </div>
      {!stopped && status.value && !status.value.motion.ok && <p class="note bad"><Icon name="alert" size={16} />{status.value.motion.reasons.join(" ")}</p>}

      <h3>Say and show</h3>
      <form class="row" onSubmit={(e) => { e.preventDefault(); if (line.trim()) act(() => robot("sensors/voice/say", { text: line }), "Said."); setLine(""); }}>
        <input type="text" value={line} placeholder="You're doing great. Nice and slow." aria-label="A line for the robot to say" onInput={(e) => setLine(e.currentTarget.value)} />
        <button class="btn" disabled={busy || !line.trim()}><Icon name="speak" />Say</button>
      </form>
      <form class="row" onSubmit={(e) => { e.preventDefault(); if (matrix.trim()) act(() => robot("sensors/matrix/text", { text: matrix }), "Showing."); }}>
        <input type="text" value={matrix} maxLength={60} placeholder="Text for the dot display" aria-label="Text for the dot display" onInput={(e) => setMatrix(e.currentTarget.value)} />
        <button class="btn" disabled={busy || !matrix.trim()}>Show</button>
        <button class="btn quiet" type="button" onClick={() => act(() => robot("sensors/matrix/clear"))}>Clear</button>
      </form>
      <div class="row moods" role="group" aria-label="Eye colour">
        {MOODS.map(([name, [r, g, b]]) => (
          <button key={name} class="btn mood" onClick={() => act(() => robot("sensors/eyes", { r, g, b }))}>
            <i style={{ background: `rgb(${r},${g},${b})` }} />{name}
          </button>
        ))}
      </div>

      <h3>If the patient says something worrying</h3>
      <form class="row" onSubmit={(e) => { e.preventDefault(); if (heard.trim()) act(() => api("POST", "/api/safety/heard", { text: heard })); setHeard(""); }}>
        <input type="text" value={heard} placeholder="Type what you heard, e.g. “ow, that hurts”" aria-label="What the patient said" onInput={(e) => setHeard(e.currentTarget.value)} />
        <button class="btn">Check</button>
      </form>
      <p class="hint">Pain words freeze the robot at once and need you to clear the stop. The microphone listener posts to the same place.</p>
    </section>
  );
}

function DryRunPanel() {
  const l = live.value;
  const fake = (body: unknown) => attempt(() => api("POST", "/api/dev/fake", body));
  const frame = (body: unknown) => attempt(() => api("POST", "/api/live/frame", body));
  return (
    <details class="panel fold dry">
      <summary><Icon name="settings" />Dry-run controls (fake hardware)</summary>
      <p>These stand in for things that only happen with a real robot and patient.</p>
      <div class="row">
        <button class="btn" onClick={() => fake({ temps: { left_shoulder_pitch: 84 } })}>Overheat a shoulder servo</button>
        <button class="btn" onClick={() => fake({ battery_mv: 9600 })}>Drain the battery</button>
        <button class="btn" onClick={() => fake({ stall: { right_shoulder_pitch: 275 } })}>Jam the right shoulder</button>
        <button class="btn" onClick={() => fake({ reset: true })}>Reset the fake robot</button>
      </div>
      <div class="row">
        <button class="btn" onClick={() => attempt(() => api("POST", "/api/safety/heard", { text: "ow, my shoulder hurts" }))}>Patient says “ow, my shoulder hurts”</button>
        <button class="btn" onClick={() => attempt(() => api("POST", "/api/safety/report", { kind: "fall", detail: "Simulated from the dry-run panel." }))}>Report a fall</button>
        {l.frame.simulated !== false && <button class="btn toggle" aria-pressed={!l.frame.visible} onClick={() => frame({ visible: !l.frame.visible })}>Patient walks out of view</button>}
      </div>
      {l.frame.simulated === false
        ? <p class="hint">The webcam is supplying where the patient stands, so these sliders are off. Step out of view to test a pause.</p>
        : <>
      <label>Patient left to right in the camera frame
        <input type="range" min="-1" max="1" step="0.05" value={l.frame.x} onInput={(e) => frame({ x: +e.currentTarget.value })} /></label>
      <label>Patient distance (far to near)
        <input type="range" min="0.15" max="1" step="0.05" value={l.frame.size} onInput={(e) => frame({ size: +e.currentTarget.value })} /></label>
      </>}
    </details>
  );
}

export function LiveScreen() {
  useEffect(() => {
    twinView("auto");
    // Drop any pose left over from a Studio preview, so the ghost only shows real commands.
    if (!playback.value.playing) api("POST", "/twin/api/pose", { name: "default_standing_pose" }).catch(() => {});
  }, []);
  return (
    <div class="live">
      <header class="page-head live-head">
        <h1>Live</h1>
        <p>Run a workout plan with the patient, watch it happen, and step in when needed.</p>
      </header>
      <div class="live-views"><TwinPanel /><CameraPanel /></div>
      <div class="live-main"><SessionRunner /></div>
      <div class="live-side">
        {live.value.running && <TrackingPanel />}
        <Coaching />
        {status.value?.dry_run && <DryRunPanel />}
        <button class="btn quiet" onClick={() => go("/patient")}><Icon name="patient" />Open the large-type patient view</button>
      </div>
    </div>
  );
}
