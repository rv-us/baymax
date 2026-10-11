// Studio: make an exercise animation by hand. Dials set a pose, keyframes go on the tape,
// the twin previews it, and a separate, confirmed button plays it on the real robot.
import { signal } from "@preact/signals";
import { useEffect, useRef, useState } from "preact/hooks";
import { api, attempt, clamp, Keyframe, live, playback, status, toast } from "./api";
import { CameraPanel } from "./camera";
import { Dial } from "./dial";
import { Icon } from "./icons";
import { TwinPanel, twinState, twinView } from "./twin";

interface Joint { name: string; id: number; limits: [number, number]; stand: number }
interface Model {
  joints: Joint[]; head: Record<string, { limits: [number, number]; center: number }>;
  start: Pose; easings: string[]; min_ms: number; max_ms: number; max_keyframes: number; track_joints: string[];
}
interface Pose { servos: Record<string, number>; head: Record<string, number> }
interface Exercise { cue: string; sets: number; reps: number; mirror: string; track_joint: string; targets: Record<string, any> }
interface Anim { id: string | null; name: string; keyframes: Keyframe[]; exercise: Exercise; source?: string }

const JOINT_LABEL: Record<string, string> = { shoulder_pitch: "Swing forward", shoulder_roll: "Lift to the side", elbow: "Bend elbow" };
const EASE_LABEL: Record<string, string> = { inout: "Gentle start and stop", in: "Gentle start", out: "Gentle stop", linear: "Even speed" };
const blankExercise = (): Exercise => ({
  cue: "", sets: 2, reps: 8, mirror: "arms", track_joint: "shoulder_elevation",
  targets: { shoulder_elevation: { top: 90, bottom: 15, tolerance: 15 } },
});
const blank = (): Anim => ({ id: null, name: "", keyframes: [], exercise: blankExercise() });

// Module state, so the work in progress survives a trip to another screen.
const model = signal<Model | null>(null);
const anim = signal<Anim>(blank());
const pose = signal<Pose | null>(null);
const selected = signal<number | null>(null);
const playhead = signal(0);
const previewing = signal(false);
const dirty = signal(false);
const library = signal<any[] | null>(null);
const mirror = signal(true);
const loopOn = signal(false);
const speed = signal(1);
const moveS = signal(1.5);

const total = (frames: Keyframe[]) => frames.reduce((n, k) => n + k.ms + (k.hold_ms ?? 0), 0);
const sideOf = (joint: string) => joint.split("_")[0];
const partOf = (joint: string) => joint.split("_").slice(1).join("_");

// ---- talking to the twin (never the robot) --------------------------------------------

let busy = false;
let queued: any = null;
async function preview(body: any, adopt = false) {
  if (busy) { queued = { body, adopt }; return; }
  busy = true;
  try {
    const p = await api<Pose>("POST", "/api/preview/pose", body);
    if (adopt) pose.value = { servos: { ...pose.value!.servos, ...p.servos }, head: { ...pose.value!.head, ...(p.head ?? {}) } };
  } catch (e) {
    toast("error", e instanceof Error ? e.message : String(e));
    previewing.value = false;
  } finally {
    busy = false;
    if (queued) { const q = queued; queued = null; preview(q.body, q.adopt); }
  }
}
const showAt = (t: number) => {
  playhead.value = t;
  if (anim.value.keyframes.length) preview({ keyframes: anim.value.keyframes, t_ms: t }, true);
};
const showPose = (p: Pose) => { pose.value = p; preview({ servos: p.servos, head: p.head }); };

function setJoint(joint: string, pulse: number) {
  const m = model.value!, p = pose.value!;
  const servos = { ...p.servos, [joint]: pulse };
  if (mirror.value) {
    const other = `${sideOf(joint) === "left" ? "right" : "left"}_${partOf(joint)}`;
    const j = m.joints.find((x) => x.name === other);
    if (j) servos[other] = clamp(1000 - pulse, j.limits[0], j.limits[1]); // the two sides are mirror images
  }
  const next = { ...p, servos };
  showPose(next);
  const i = selected.value;
  if (i != null) {
    const frames = anim.value.keyframes.slice();
    frames[i] = { ...frames[i], servos: { ...next.servos } };
    setFrames(frames);
  }
}

function setHead(axis: string, pulse: number) {
  const next = { ...pose.value!, head: { ...pose.value!.head, [axis]: pulse } };
  showPose(next);
  const i = selected.value;
  if (i != null) {
    const frames = anim.value.keyframes.slice();
    frames[i] = { ...frames[i], head: { ...next.head } };
    setFrames(frames);
  }
}

function setFrames(keyframes: Keyframe[]) {
  anim.value = { ...anim.value, keyframes };
  dirty.value = true;
}

function startOf(i: number) { return total(anim.value.keyframes.slice(0, i)); }

function select(i: number | null) {
  selected.value = i;
  previewing.value = false;
  if (i == null) return;
  const kf = anim.value.keyframes[i];
  playhead.value = startOf(i) + kf.ms;
  showPose({ servos: { ...pose.value!.servos, ...kf.servos }, head: { ...model.value!.start.head, ...(kf.head ?? {}) } });
}

function capture(from?: Pose, ms?: number) {
  const m = model.value!;
  if (anim.value.keyframes.length >= m.max_keyframes) return toast("error", `An animation holds ${m.max_keyframes} keyframes at most.`);
  const p = from ?? pose.value!;
  const kf: Keyframe = { ms: clamp(Math.round(ms ?? moveS.value * 1000), m.min_ms, m.max_ms), ease: "inout", servos: { ...p.servos } };
  if (Object.entries(p.head).some(([axis, v]) => v !== m.head[axis].center)) kf.head = { ...p.head };
  const at = selected.value == null ? anim.value.keyframes.length : selected.value + 1;
  const frames = anim.value.keyframes.slice();
  frames.splice(at, 0, kf);
  setFrames(frames);
  selected.value = at;
  playhead.value = startOf(at) + kf.ms;
}

function editSelected(change: Partial<Keyframe>) {
  const i = selected.value!;
  const frames = anim.value.keyframes.slice();
  frames[i] = { ...frames[i], ...change };
  setFrames(frames);
}
function removeSelected() {
  const i = selected.value!;
  setFrames(anim.value.keyframes.filter((_, n) => n !== i));
  selected.value = null;
}
function moveSelected(by: number) {
  const i = selected.value!, j = i + by;
  const frames = anim.value.keyframes.slice();
  if (j < 0 || j >= frames.length) return;
  [frames[i], frames[j]] = [frames[j], frames[i]];
  setFrames(frames);
  selected.value = j;
}

async function refreshLibrary() {
  try { library.value = await api("GET", "/api/animations"); } catch { library.value = []; }
}
function open(a: any) {
  anim.value = { id: a.id ?? null, name: a.name, keyframes: a.keyframes, exercise: { ...blankExercise(), ...(a.exercise ?? {}) }, source: a.source };
  dirty.value = a.id == null;
  selected.value = null;
  previewing.value = false;
  pose.value = structuredClone(model.value!.start);
  showAt(0);
}
async function save(): Promise<string | null> {
  const a = anim.value;
  if (!a.name.trim()) { toast("error", "Give the animation a name first."); return null; }
  if (!a.keyframes.length) { toast("error", "Capture at least one keyframe first."); return null; }
  const body = { name: a.name, keyframes: a.keyframes, exercise: a.exercise, source: a.source };
  const saved = await attempt(() => (a.id ? api("PUT", `/api/animations/${a.id}`, body) : api("POST", "/api/animations", body)), `Saved "${a.name}".`);
  if (!saved) return null;
  anim.value = { ...a, id: saved.id };
  dirty.value = false;
  refreshLibrary();
  return saved.id;
}

// ---- the tape -------------------------------------------------------------------------

const PX_PER_S = 84;

function Tape() {
  const frames = anim.value.keyframes;
  const ms = total(frames);
  const width = Math.max(480, (ms / 1000 + 1.5) * PX_PER_S);
  const x = (t: number) => (t / 1000) * PX_PER_S;
  const scrub = (e: PointerEvent) => {
    if (!frames.length) return;
    const el = e.currentTarget as HTMLElement;
    const t = clamp(((e.clientX - el.getBoundingClientRect().left) / PX_PER_S) * 1000, 0, ms);
    previewing.value = false;
    selected.value = null;
    showAt(Math.round(t));
  };
  const seconds = Array.from({ length: Math.ceil(width / PX_PER_S) + 1 }, (_, i) => i);
  let t0 = 0;
  return (
    <div class="tape" role="group" aria-label="Timeline">
      <div class="tape-track" style={{ width: `${width}px` }}>
        <div class="tape-ruler" onPointerDown={(e) => { (e.currentTarget as Element).setPointerCapture(e.pointerId); scrub(e); }}
          onPointerMove={(e) => { if (e.buttons) scrub(e); }} title="Drag to scrub">
          {seconds.map((s) => (
            <span key={s} class="tape-second" style={{ left: `${s * PX_PER_S}px` }}>{s}s</span>
          ))}
        </div>
        <div class="tape-frames">
          <span class="tape-rest" title="Every animation starts with the arms at rest">Rest</span>
          {frames.map((kf, i) => {
            const left = x(t0);
            const w = x(kf.ms), hold = x(kf.hold_ms ?? 0);
            t0 += kf.ms + (kf.hold_ms ?? 0);
            return (
              <button key={i} class={`tape-kf ${selected.value === i ? "is-selected" : ""}`}
                style={{ left: `${left}px`, width: `${w + hold}px` }} aria-pressed={selected.value === i}
                aria-label={`Keyframe ${i + 1}: ${(kf.ms / 1000).toFixed(1)} second move${kf.hold_ms ? `, ${(kf.hold_ms / 1000).toFixed(1)} second hold` : ""}`}
                onClick={() => select(selected.value === i ? null : i)}>
                <span class="tape-move" style={{ width: `${w}px` }}><b>{i + 1}</b><small>{(kf.ms / 1000).toFixed(1)}s</small></span>
                {hold > 0 && <span class="tape-hold" style={{ width: `${hold}px` }} />}
              </button>
            );
          })}
          {!frames.length && <p class="tape-empty">Set a pose with the dials, then press “Capture keyframe”.</p>}
        </div>
        {frames.length > 0 && <div class="tape-head" style={{ transform: `translateX(${x(playhead.value)}px)` }} />}
      </div>
    </div>
  );
}

function KeyframeEditor() {
  const m = model.value!;
  const i = selected.value;
  if (i == null) return null;
  const kf = anim.value.keyframes[i];
  const secs = (v: string, lo: number, hi: number) => clamp(Math.round(parseFloat(v || "0") * 10) * 100, lo, hi);
  return (
    <div class="kf-edit" aria-label={`Keyframe ${i + 1}`}>
      <strong>Keyframe {i + 1}</strong>
      <label>Move for
        <input type="number" inputMode="decimal" min={m.min_ms / 1000} max={m.max_ms / 1000} step="0.1" value={kf.ms / 1000}
          onChange={(e) => editSelected({ ms: secs(e.currentTarget.value, m.min_ms, m.max_ms) })} /> s
      </label>
      <label>then hold
        <input type="number" inputMode="decimal" min="0" max={m.max_ms / 1000} step="0.1" value={(kf.hold_ms ?? 0) / 1000}
          onChange={(e) => editSelected({ hold_ms: secs(e.currentTarget.value, 0, m.max_ms) })} /> s
      </label>
      <label>Motion
        <select value={kf.ease ?? "inout"} onChange={(e) => editSelected({ ease: e.currentTarget.value })}>
          {m.easings.map((e) => <option key={e} value={e}>{EASE_LABEL[e] ?? e}</option>)}
        </select>
      </label>
      <span class="kf-buttons">
        <button class="btn icon-only" aria-label="Move earlier" disabled={i === 0} onClick={() => moveSelected(-1)}><Icon name="left" /></button>
        <button class="btn icon-only" aria-label="Move later" disabled={i === anim.value.keyframes.length - 1} onClick={() => moveSelected(1)}><Icon name="right" /></button>
        <button class="btn icon-only" aria-label="Delete keyframe" onClick={removeSelected}><Icon name="trash" /></button>
        <button class="btn quiet" onClick={() => select(null)}>Done</button>
      </span>
    </div>
  );
}

// ---- transport ------------------------------------------------------------------------

function usePreviewClock() {
  useEffect(() => {
    if (!previewing.value) return;
    let raf = 0, last = performance.now();
    const tick = (now: number) => {
      const end = total(anim.value.keyframes);
      let t = playhead.value + (now - last) * speed.value;
      last = now;
      if (t >= end) {
        if (loopOn.value) t = 0;
        else { showAt(end); previewing.value = false; return; }
      }
      showAt(t);
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [previewing.value]);
}

function Transport() {
  const frames = anim.value.keyframes;
  const pb = playback.value;
  const motion = status.value?.motion;
  const [confirming, setConfirming] = useState(false);
  const blocked = !frames.length ? "Capture a keyframe first."
    : live.value.running ? "A session is running on the robot."
    : motion && !motion.ok ? motion.reasons.join(" ")
    : !motion ? "Checking the robot…" : null;

  useEffect(() => { if (pb.playing) playhead.value = pb.t_ms; }, [pb.t_ms, pb.playing]);
  useEffect(() => { if (pb.error) toast("error", `The robot stopped the animation: ${pb.error}`); }, [pb.error]);

  const togglePreview = () => {
    if (!frames.length) return;
    if (!previewing.value && playhead.value >= total(frames)) playhead.value = 0;
    selected.value = null;
    previewing.value = !previewing.value;
  };
  const playOnRobot = async () => {
    setConfirming(false);
    previewing.value = false;
    await attempt(() => api("POST", "/api/playback/start", { keyframes: frames, speed: speed.value, loop: loopOn.value, name: anim.value.name }));
  };

  return (
    <div class="transport">
      <div class="transport-twin">
        <button class="btn primary" onClick={togglePreview} disabled={!frames.length || pb.playing}>
          <Icon name={previewing.value ? "pause" : "play"} />{previewing.value ? "Pause" : "Preview on twin"}
        </button>
        <button class="btn toggle" aria-pressed={loopOn.value} onClick={() => { loopOn.value = !loopOn.value; }}><Icon name="loop" />Loop</button>
        <label class="speed">Speed
          <input type="range" min="0.25" max="1.5" step="0.05" value={speed.value} onInput={(e) => { speed.value = +e.currentTarget.value; }} />
          <output>{speed.value.toFixed(2)}×</output>
        </label>
        <span class="time">{(playhead.value / 1000).toFixed(1)} / {(total(frames) / 1000).toFixed(1)} s</span>
      </div>
      <div class="transport-robot">
        {pb.playing ? (
          <button class="btn danger" onClick={() => attempt(() => api("POST", "/api/playback/stop"))}><Icon name="stop" />Stop playback</button>
        ) : confirming ? (
          <div class="confirm" role="alertdialog" aria-label="Move the real robot?">
            <span>The real robot will move its arms. Is the space around it clear?</span>
            <button class="btn robot" onClick={playOnRobot}>Move the robot</button>
            <button class="btn quiet" onClick={() => setConfirming(false)}>Cancel</button>
          </div>
        ) : (
          <>
            <button class="btn robot" disabled={!!blocked} onClick={() => setConfirming(true)}><Icon name="robot" />Play on robot</button>
            {blocked && frames.length > 0 && <span class="blocked"><Icon name="alert" size={16} />{blocked}</span>}
          </>
        )}
      </div>
    </div>
  );
}

// ---- record by hand, import, plan -----------------------------------------------------

const ARM_ID: Record<string, number> = {
  left_shoulder_pitch: 8, left_shoulder_roll: 7, left_elbow: 6, right_shoulder_pitch: 16, right_shoulder_roll: 15, right_elbow: 14,
};
function robotPose(): Pose | null {
  const actual = twinState.value?.channels?.actual?.servos;
  const m = model.value;
  if (!actual || !m) return null;
  const servos: Record<string, number> = {};
  for (const j of m.joints) servos[j.name] = clamp(actual[String(ARM_ID[j.name])], j.limits[0], j.limits[1]);
  return { servos, head: { ...m.start.head } };
}

function RecordPanel() {
  const [limp, setLimp] = useState(false);
  const [auto, setAuto] = useState(false);
  const has = !!twinState.value?.channels?.actual;
  useEffect(() => {
    if (!auto) return;
    const id = setInterval(() => { const p = robotPose(); if (p) { capture(p, 1000); pose.value = p; } }, 1000);
    return () => clearInterval(id);
  }, [auto]);
  const coast = async () => { if (await attempt(() => api("POST", "/api/record/coast"), "The arms are limp. Pose them by hand.")) setLimp(true); };
  const hold = async () => { setAuto(false); if (await attempt(() => api("POST", "/api/record/hold"), "The arms are holding again.")) setLimp(false); };
  return (
    <details class="fold">
      <summary><Icon name="hand" />Record from the robot</summary>
      <p>Let the robot's arms go limp, move them by hand, and capture each pose as a keyframe. The legs keep holding.</p>
      {!has && <p class="note"><Icon name="alert" size={16} />No servo readback from the robot yet, so there is nothing to capture.</p>}
      <div class="row">
        {!limp
          ? <button class="btn" disabled={!has} onClick={coast}>Let the arms go limp</button>
          : <button class="btn primary" onClick={hold}>Hold the arms again</button>}
        <button class="btn" disabled={!has} onClick={() => { const p = robotPose(); if (p) { capture(p); pose.value = p; } }}><Icon name="record" />Capture the robot's pose</button>
        <button class="btn toggle" aria-pressed={auto} disabled={!has} onClick={() => setAuto(!auto)}>Capture every second</button>
      </div>
    </details>
  );
}

function ImportPanel() {
  const [note, setNote] = useState<string | null>(null);
  const pick = async (e: Event) => {
    const file = (e.currentTarget as HTMLInputElement).files?.[0];
    (e.currentTarget as HTMLInputElement).value = "";
    if (!file) return;
    if (file.type.startsWith("video/")) {
      setNote(`“${file.name}” is a video. Turning video into robot motion isn't built yet: run arm_mvp on it and upload the frames it writes as JSON.`);
      return;
    }
    try {
      const data = JSON.parse(await file.text());
      const a = await api("POST", "/api/animations/import", { name: file.name.replace(/\.json$/i, ""), ...data });
      open({ ...a, id: null });
      setNote(`Imported ${a.keyframes.length} keyframes (${a.dropped_frames} in-between frames dropped). Preview it, then save.`);
    } catch (err) {
      setNote(`Couldn't import “${file.name}”: ${err instanceof Error ? err.message : err}`);
    }
  };
  return (
    <details class="fold">
      <summary><Icon name="film" />Import motion from a video</summary>
      <p>Upload the frames that pose retargeting produces and they become keyframes here. The format is a JSON file:
        {" "}<code>{`{ "frames": [ { "t_ms": 0, "servos": { "left_shoulder_pitch": 700 } } ] }`}</code></p>
      <label class="btn"><Icon name="upload" />Choose a file<input type="file" accept=".json,application/json,video/*" class="sr-only" onChange={pick} /></label>
      {note && <p class="note" role="status">{note}</p>}
    </details>
  );
}

function PlanPanel() {
  const m = model.value!;
  const ex = anim.value.exercise;
  const [plans, setPlans] = useState<any[]>([]);
  const [plan, setPlan] = useState("");
  useEffect(() => { api("GET", "/api/plans").then((p) => { setPlans(p); setPlan((v) => v || p[0]?.id || "my_plan"); }).catch(() => {}); }, []);
  const set = (change: Partial<Exercise>) => { anim.value = { ...anim.value, exercise: { ...ex, ...change } }; dirty.value = true; };
  const tgt = ex.targets[ex.track_joint] ?? {};
  const setTarget = (k: string, v: number) => set({ targets: { [ex.track_joint]: { ...tgt, [k]: v } } });
  const add = async () => {
    const id = await save();
    if (!id) return;
    const name = plan.trim().toLowerCase().replace(/[^a-z0-9_-]+/g, "_");
    await attempt(() => api("POST", `/api/animations/${id}/exercise`, { plan: name }), `Added to the plan “${name}”.`);
  };
  return (
    <details class="fold">
      <summary><Icon name="plans" />Use it as an exercise</summary>
      <div class="form-grid">
        <label class="wide">What the robot says to explain it
          <input type="text" value={ex.cue} placeholder="Lift both arms out in front of you, then lower them slowly."
            onInput={(e) => set({ cue: e.currentTarget.value })} /></label>
        <label>Sets<input type="number" min="1" max="50" value={ex.sets} onChange={(e) => set({ sets: +e.currentTarget.value })} /></label>
        <label>Reps<input type="number" min="1" max="50" value={ex.reps} onChange={(e) => set({ reps: +e.currentTarget.value })} /></label>
        <label>Joint to track
          <select value={ex.track_joint} onChange={(e) => set({ track_joint: e.currentTarget.value, targets: {} })}>
            {m.track_joints.map((j) => <option key={j} value={j}>{j.replace(/_/g, " ")}</option>)}
          </select></label>
        <label>Top of the move (°)<input type="number" value={tgt.top ?? ""} onChange={(e) => setTarget("top", +e.currentTarget.value)} /></label>
        <label>Bottom (°)<input type="number" value={tgt.bottom ?? ""} onChange={(e) => setTarget("bottom", +e.currentTarget.value)} /></label>
        <label>Leeway (°)<input type="number" value={tgt.tolerance ?? ""} onChange={(e) => setTarget("tolerance", +e.currentTarget.value)} /></label>
        <label>Plan
          <input type="text" list="plan-names" value={plan} onInput={(e) => setPlan(e.currentTarget.value)} />
          <datalist id="plan-names">{plans.map((p) => <option key={p.id} value={p.id} />)}</datalist></label>
      </div>
      <button class="btn primary" onClick={add}><Icon name="plus" />Save and add to plan</button>
    </details>
  );
}

// ---- the screen -----------------------------------------------------------------------

export function Studio() {
  const [loadError, setLoadError] = useState<string | null>(null);
  const nameRef = useRef<HTMLInputElement>(null);
  usePreviewClock();
  useEffect(() => {
    if (!model.value) {
      api<Model>("GET", "/api/animation-model").then((m) => { model.value = m; pose.value = structuredClone(m.start); })
        .catch((e) => setLoadError(e.message));
    }
    refreshLibrary();
    twinView("command");
  }, []);

  if (loadError) return <div class="page-empty"><Icon name="alert" size={28} /><h2>The editor couldn't load</h2><p>{loadError}</p></div>;
  const m = model.value, p = pose.value;
  if (!m || !p) return <div class="page-empty"><p>Loading the editor…</p></div>;
  const a = anim.value;
  const robotBusy = playback.value.playing;
  const sides = ["left", "right"].map((side) => ({ side, joints: m.joints.filter((j) => sideOf(j.name) === side) }));
  const newAnim = () => {
    if (dirty.value && a.keyframes.length && !confirm("Discard the unsaved changes to this animation?")) return;
    open(blank());
    nameRef.current?.focus();
  };
  const remove = async (item: any) => {
    if (!confirm(`Delete “${item.name}”? This can't be undone.`)) return;
    if (await attempt(() => api("DELETE", `/api/animations/${item.id}`), `Deleted “${item.name}”.`)) {
      if (a.id === item.id) open(blank());
      refreshLibrary();
    }
  };

  return (
    <div class="studio">
      <div class="studio-bar">
        <input ref={nameRef} class="name" type="text" value={a.name} placeholder="Name this animation" aria-label="Animation name"
          onInput={(e) => { anim.value = { ...a, name: e.currentTarget.value }; dirty.value = true; }} />
        <button class="btn primary" onClick={save} disabled={!dirty.value}><Icon name="save" />{dirty.value ? "Save" : "Saved"}</button>
        <button class="btn" onClick={newAnim}><Icon name="plus" />New</button>
        <details class="menu">
          <summary class="btn">Open<Icon name="down" size={16} /></summary>
          <div class="menu-body">
            {library.value == null && <p>Loading…</p>}
            {library.value?.length === 0 && <p>No saved animations yet. Capture a few keyframes and save.</p>}
            {library.value?.map((item) => (
              <div class="menu-row" key={item.id}>
                <button class="menu-open" onClick={(e) => {
                  (e.currentTarget.closest("details") as HTMLDetailsElement).open = false;
                  attempt(async () => open(await api("GET", `/api/animations/${item.id}`)));
                }}>
                  <b>{item.name}</b>
                  <small>{item.unreadable ? "unreadable file" : `${item.keyframes} keyframes, ${(item.duration_ms / 1000).toFixed(1)} s`}</small>
                </button>
                <button class="btn icon-only quiet" aria-label={`Delete ${item.name}`} onClick={() => remove(item)}><Icon name="trash" size={18} /></button>
              </div>
            ))}
          </div>
        </details>
      </div>

      <div class="studio-views">
        <TwinPanel />
        <CameraPanel compact />
      </div>

      <div class="studio-joints">
        <div class="joints-head">
          <h2>Pose</h2>
          <button class="btn toggle" aria-pressed={mirror.value} onClick={() => { mirror.value = !mirror.value; }}><Icon name="mirror" />Both arms together</button>
          <button class="btn quiet" onClick={() => { selected.value = null; showPose(structuredClone(m.start)); }}>Back to rest</button>
        </div>
        <div class="arms">
          {sides.map(({ side, joints }) => (
            <fieldset key={side} class="arm" disabled={robotBusy}>
              <legend>{side === "left" ? "Left arm" : "Right arm"}</legend>
              {joints.map((j) => (
                <Dial key={j.name} label={JOINT_LABEL[partOf(j.name)] ?? partOf(j.name)} value={p.servos[j.name]} stand={j.stand}
                  limits={j.limits} disabled={robotBusy || previewing.value} onChange={(v) => setJoint(j.name, v)} />
              ))}
            </fieldset>
          ))}
        </div>
        <div class="head-row">
          {Object.entries(m.head).map(([axis, h]) => (
            <label key={axis}>Head {axis === "pan" ? "turn" : "nod"}
              <input type="range" min={h.limits[0]} max={h.limits[1]} step="10" value={p.head[axis]} disabled={robotBusy || previewing.value}
                onInput={(e) => setHead(axis, +e.currentTarget.value)} />
            </label>
          ))}
          <span class="locked" title="Moving one leg joint at a time makes the robot fall">Legs are locked</span>
        </div>
        <div class="capture">
          <label>Take
            <input type="number" inputMode="decimal" min={m.min_ms / 1000} max={m.max_ms / 1000} step="0.1" value={moveS.value}
              onChange={(e) => { moveS.value = clamp(+e.currentTarget.value || 1.5, m.min_ms / 1000, m.max_ms / 1000); }} /> s to get here
          </label>
          <button class="btn primary" onClick={() => capture()} disabled={robotBusy}><Icon name="plus" />Capture keyframe</button>
        </div>
      </div>

      <div class="studio-time">
        <Transport />
        <Tape />
        <KeyframeEditor />
      </div>

      <div class="studio-more">
        <PlanPanel />
        <RecordPanel />
        <ImportPanel />
      </div>
    </div>
  );
}
