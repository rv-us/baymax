// Private twins: a robot model that plays a solved clip in this browser only. The shared
// twin (twin.tsx) shows what the robot was told and is doing; these show a preview, a
// replay, or one channel of the shared state, without changing what anyone else sees.
import { signal } from "@preact/signals";
import { useEffect, useRef, useState } from "preact/hooks";
import { api, Clip, Keyframe } from "./api";
import { Icon } from "./icons";

let twinModule: Promise<any> | null = null;
const loadTwin = () => (twinModule ??= import(/* @vite-ignore */ `${location.origin}/twin/web/twin.js`));

/** One BaymaxTwin in `slot`, never connected to the server stream. */
function useLocalTwin(slot: { current: HTMLDivElement | null }, camera = "front") {
  const twin = useRef<any>(null);
  const [state, setState] = useState<"loading" | "ready" | "failed">("loading");
  const [error, setError] = useState("");
  useEffect(() => {
    let dead = false;
    const host = document.createElement("div");
    host.className = "twin-canvas";
    slot.current!.appendChild(host);
    loadTwin().then(async (mod) => {
      if (dead) return;
      const t = new mod.BaymaxTwin(host, { baseUrl: "/twin", skeleton: false, ghost: false });
      twin.current = t;
      await t.init();
      if (dead) return;
      t.scene.traverse((o: any) => { if (o.geometry?.type === "CircleGeometry") o.material.color.set("#152b25"); });
      t.setCameraPreset(camera);
      setState("ready");
    }).catch((e) => { if (!dead) { setError(e instanceof Error ? e.message : String(e)); setState("failed"); } });
    return () => {
      dead = true;
      const t = twin.current;
      twin.current = null;
      try { t?.renderer.forceContextLoss(); t?.dispose(); } catch { /* already gone */ }
      host.remove();
    };
  }, []);
  return { twin, state, error };
}

const show = (twin: any, frame: any) => twin?.setState({
  display: "command", view: "command", landmarks: null, delta: null, robot: {},
  channels: { command: frame, actual: null },
});

function TwinStatus({ state, error }: { state: string; error: string }) {
  if (state === "failed") return <div class="screen-empty"><Icon name="robot" size={28} /><p><strong>The 3D preview couldn't start.</strong></p><p>{error || "WebGL may be turned off in this browser."}</p></div>;
  if (state === "loading") return <div class="screen-empty"><p>Loading the robot model…</p></div>;
  return null;
}

export interface MotionTwinProps {
  clip: Clip | null; playing?: boolean; loop?: boolean; speed?: number; time?: number;
  onTime?: (ms: number) => void; onEnd?: () => void;
  title?: string; note?: string; busy?: boolean; empty?: string; error?: string | null; compact?: boolean;
}

/** Plays `clip` on a private twin. `time` (ms) positions it while not playing. */
export function MotionTwin({ clip, playing = false, loop = false, speed = 1, time, onTime, onEnd, title = "Preview", note, busy, empty, error, compact }: MotionTwinProps) {
  const slot = useRef<HTMLDivElement>(null);
  const { twin, state, error: twinError } = useLocalTwin(slot);
  const at = useRef(0);
  const cb = useRef({ onTime, onEnd });
  cb.current = { onTime, onEnd };

  const frameAt = (ms: number) => clip && clip.frames[Math.max(0, Math.min(clip.frames.length - 1, Math.round((ms / 1000) * clip.hz)))];
  useEffect(() => { at.current = 0; if (clip && state === "ready") show(twin.current, frameAt(time ?? 0)); }, [clip, state]);
  useEffect(() => {
    if (playing || time == null || !clip || state !== "ready") return;
    at.current = time;
    show(twin.current, frameAt(time));
  }, [time, playing]);
  useEffect(() => {
    if (!playing || !clip || state !== "ready") return;
    if (at.current >= clip.total_ms) at.current = 0;
    let raf = 0, last = performance.now();
    const tick = (now: number) => {
      at.current += (now - last) * speed;
      last = now;
      if (at.current >= clip.total_ms) {
        if (loop) at.current = 0;
        else { at.current = clip.total_ms; show(twin.current, frameAt(at.current)); cb.current.onTime?.(at.current); cb.current.onEnd?.(); return; }
      }
      show(twin.current, frameAt(at.current));
      cb.current.onTime?.(at.current);
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(raf);
  }, [playing, clip, state, loop, speed]);

  return (
    <section class={`screen twin ${compact ? "compact" : ""}`} aria-label={title}>
      <div class="screen-body" ref={slot}>
        <TwinStatus state={state} error={twinError} />
        {state === "ready" && error && <div class="screen-empty"><Icon name="alert" size={28} /><p><strong>No preview.</strong></p><p>{error}</p></div>}
        {state === "ready" && !error && !clip && !busy && empty && <div class="screen-note">{empty}</div>}
        {state === "ready" && busy && <div class="screen-note" role="status">Working out the motion…</div>}
      </div>
      <header class="screen-head">
        <span class="screen-title">{title}</span>
        {!compact && <span class="tag on-screen">Twin only. The robot doesn't move.</span>}
      </header>
      {note && <footer class="screen-foot"><span class="legend-item">{note}</span></footer>}
    </section>
  );
}

// ---- solved clips, cached ---------------------------------------------------------------

const clips = new Map<string, Promise<Clip>>();
/** Solve keyframes into a clip once per key. A failed solve isn't cached. */
export function solveClip(key: string, keyframes: () => Promise<Keyframe[]> | Keyframe[]): Promise<Clip> {
  let p = clips.get(key);
  if (!p) {
    p = (async () => api<Clip>("POST", "/api/preview/solve", { keyframes: await keyframes(), hz: 20 }))();
    clips.set(key, p);
    p.catch(() => clips.delete(key));
  }
  return p;
}
export const forgetClips = () => clips.clear();

// ---- one channel of the shared twin state, for the side by side view --------------------

const feed = signal<any>(null);
const feedState = signal<"connecting" | "live" | "lost">("connecting");
let feedUsers = 0;
let feedSource: EventSource | null = null;
function useFeed() {
  useEffect(() => {
    if (feedUsers++ === 0) {
      feedSource = new EventSource("/twin/api/stream");
      feedSource.onopen = () => { feedState.value = "live"; };
      feedSource.onerror = () => { feedState.value = "lost"; };
      feedSource.onmessage = (ev) => { try { feed.value = JSON.parse(ev.data); } catch { /* a bad frame is skipped */ } };
    }
    return () => { if (--feedUsers === 0) { feedSource?.close(); feedSource = null; feedState.value = "connecting"; } };
  }, []);
  return feed.value;
}

/** The twin showing only what the robot was told ("command") or only what it reports ("actual"). */
export function ChannelTwin({ channel, title, hint }: { channel: "command" | "actual"; title: string; hint: string }) {
  const slot = useRef<HTMLDivElement>(null);
  const { twin, state, error } = useLocalTwin(slot);
  const s = useFeed();
  const frame = s?.channels?.[channel];
  useEffect(() => { if (state === "ready" && frame) show(twin.current, frame); }, [s?.version, state]);
  const age = frame?.age_s;
  return (
    <section class="screen twin" aria-label={title}>
      <div class="screen-body" ref={slot}>
        <TwinStatus state={state} error={error} />
        {state === "ready" && !frame && (
          <div class="screen-note">{channel === "actual" ? "No servo readback from the robot yet." : "Nothing has been commanded yet."}</div>
        )}
      </div>
      <header class="screen-head"><span class="screen-title">{title}</span></header>
      <footer class="screen-foot">
        <span class="legend-item">{hint}</span>
        {feedState.value === "lost" && <span class="legend-item warn"><Icon name="alert" size={14} />Stream lost, retrying</span>}
        {channel === "actual" && age != null && age > 3 && <span class="legend-item warn"><Icon name="alert" size={14} />Last reading {Math.round(age)} s ago</span>}
      </footer>
    </section>
  );
}
