// Generate: describe a motion, the model on the GX10 writes it, the twin previews it, and
// you decide whether it joins the library.
import { signal } from "@preact/signals";
import { useEffect, useState } from "preact/hooks";
import { api, attempt, Clip, Keyframe, status } from "./api";
import { Icon, StateMark } from "./icons";
import { drawerOpen } from "./links";
import { MotionTwin } from "./motion";
import { PlayButton, RobotBar } from "./play";

interface Draft { id: string | null; name: string; kind: string; keyframes: Keyframe[]; prompt?: string; exercise: any; limited_joints: number; source?: string; generator?: string }
interface Result { animation: Draft; source: "gx10" | "mock"; model: string | null; note: string | null }

const prompt = signal("");
const result = signal<Result | null>(null);
const savedId = signal<string | null>(null);
const useMock = signal(false);

const EXAMPLES = ["Wave hello with the right arm, slowly", "Reach both arms overhead, twice", "Slow arm circles", "Clap three times", "Open the arms like goalposts and stretch", "A big cheer"];

export function Generate() {
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [clip, setClip] = useState<Clip | null>(null);
  const [clipError, setClipError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const llm = status.value?.links.llm;
  const res = result.value;

  useEffect(() => {
    if (!res) { setClip(null); return; }
    let alive = true;
    api<Clip>("POST", "/api/preview/solve", { keyframes: res.animation.keyframes, hz: 20 })
      .then((c) => { if (alive) { setClip(c); setClipError(null); } }).catch((e) => { if (alive) { setClip(null); setClipError(e.message); } });
    return () => { alive = false; };
  }, [res]);

  const run = async (text = prompt.value) => {
    if (text.trim().length < 3) { setError("Describe the motion in a few words first."); return; }
    prompt.value = text;
    setWorking(true); setError(null);
    try {
      result.value = await api<Result>("POST", "/api/generate", { prompt: text, mock: useMock.value });
      savedId.value = null;
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
    setWorking(false);
  };
  const save = async () => {
    if (!res) return;
    setSaving(true);
    const out = await attempt(() => api("POST", "/api/animations", res.animation), `Saved “${res.animation.name}” to the library.`);
    setSaving(false);
    if (out) savedId.value = out.id;
  };
  const discard = () => { result.value = null; savedId.value = null; };
  const rename = (name: string) => { result.value = { ...res!, animation: { ...res!.animation, name } }; };

  const gx10 = llm?.state === "ok" ? "The model on the GX10 is connected." : llm?.state === "stub" ? "Dry run: no model is connected, so prompts go to the built-in mock generator."
    : llm?.state === "down" ? "The GX10 can't be reached. Prompts go to the built-in mock generator until it is back." : "Checking the GX10…";

  return (
    <div class="generate">
      <header class="page-head">
        <h1>Generate an animation</h1>
        <p>Describe a move in plain words. Preview it on the twin, then save it or throw it away.</p>
      </header>
      <div class="generate-cols">
        <div class="generate-main">
          <section class={`panel gx10 gx10-${llm?.state ?? "unknown"}`}>
            <div class="gx10-line">
              <StateMark state={llm?.state ?? "unknown"} />
              <div><b>{gx10}</b><small>{llm?.detail}</small></div>
              <button class="btn quiet" onClick={() => { drawerOpen.value = true; }}>Connection settings</button>
            </div>
            {llm?.state === "ok" && (
              <label class="check"><input type="checkbox" checked={useMock.value} onChange={(e) => { useMock.value = e.currentTarget.checked; }} />Use the mock generator instead (instant, no model)</label>
            )}
          </section>

          <form class="panel prompt-box" onSubmit={(e) => { e.preventDefault(); run(); }}>
            <label for="gen-prompt">What should the robot do?</label>
            <textarea id="gen-prompt" rows={3} maxLength={400} value={prompt.value} placeholder="Lift both arms out to the side, hold for two seconds, and lower them slowly"
              onInput={(e) => { prompt.value = e.currentTarget.value; }}
              onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); run(); } }} />
            <div class="row">
              <button class="btn primary big" disabled={working}><Icon name="spark" />{working ? "Generating…" : res ? "Generate again" : "Generate"}</button>
              <span class="hint">Arms and head only. Every generated pose is held inside the robot's limits.</span>
            </div>
            {error && <p class="note bad" role="alert"><Icon name="alert" size={16} />{error}</p>}
            <div class="examples">
              <span class="hint">Try one:</span>
              {EXAMPLES.map((ex) => <button type="button" key={ex} class="chip" disabled={working} onClick={() => run(ex)}>{ex}</button>)}
            </div>
          </form>

          {working && <section class="panel" role="status"><p class="note"><i class="spinner" />{llm?.state === "ok" && !useMock.value ? "Asking the model on the GX10. This can take up to a minute." : "Building the motion…"}</p></section>}

          {res && !working && (
            <section class="panel">
              <div class="panel-head">
                <h2>{savedId.value ? "Saved to the library" : "Keep it?"}</h2>
                <span class={`tag ${res.source === "mock" ? "tag-off" : "tag-accent"}`}>{res.source === "mock" ? "Made by the mock generator" : `Written by ${res.model}`}</span>
              </div>
              {res.note && <p class="note"><Icon name="info" size={16} /><span>{res.note}</span></p>}
              {res.animation.limited_joints > 0 && <p class="note"><Icon name="tune" size={16} /><span>{res.animation.limited_joints} joint positions were pulled back inside the active calibration limits.</span></p>}
              {savedId.value ? (
                <div class="stack">
                  <p class="note"><Icon name="check" size={16} /><span>“{res.animation.name}” is in the library.</span></p>
                  <PlayButton big target={{ key: savedId.value, name: res.animation.name, id: savedId.value }} />
                  <div class="row"><a class="btn" href="#/library"><Icon name="library" size={18} />See it in the library</a><button class="btn quiet" onClick={discard}>Make another</button></div>
                </div>
              ) : (
                <div class="stack">
                  <div class="form-grid">
                    <label class="wide">Name<input type="text" value={res.animation.name} maxLength={80} onInput={(e) => rename(e.currentTarget.value)} /></label>
                  </div>
                  <p class="hint">{res.animation.keyframes.length} keyframes, {clip ? (clip.total_ms / 1000).toFixed(1) : "…"} s. {res.animation.exercise?.cue && <>The robot would say: “{res.animation.exercise.cue}”</>}</p>
                  <div class="row">
                    <button class="btn primary" disabled={saving || !res.animation.name.trim()} onClick={save}><Icon name="save" />{saving ? "Saving…" : "Save to the library"}</button>
                    <button class="btn" onClick={discard}><Icon name="trash" size={18} />Discard</button>
                    <PlayButton label="Try on robot first" target={{ key: "generated-draft", name: res.animation.name || "Generated motion", keyframes: res.animation.keyframes }} />
                  </div>
                </div>
              )}
            </section>
          )}
        </div>
        <aside class="generate-side">
          <MotionTwin clip={clip} playing={!!clip} loop busy={working || (!!res && !clip && !clipError)} error={clipError}
            title={res ? res.animation.name || "Generated motion" : "Preview"} empty="The generated motion will play here." />
          <RobotBar stacked />
        </aside>
      </div>
    </div>
  );
}
