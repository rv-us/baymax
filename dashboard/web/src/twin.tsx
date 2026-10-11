// The digital twin as a panel. digital_twin/web/twin.js is loaded as-is from /twin (its
// documented embed path); one viewer instance is kept and moved between screens.
import { signal } from "@preact/signals";
import { useEffect, useRef } from "preact/hooks";
import { api, attempt, words } from "./api";
import { Icon } from "./icons";

const ARM_IDS = [8, 7, 6, 16, 15, 14];
const DRIFT_PULSES = 40; // about 10 degrees between command and readback

export const twinState = signal<any>(null);
export const twinStream = signal<"connecting" | "live" | "lost" | "failed">("connecting");
const twinError = signal<string | null>(null);
const twinModel = signal<any>(null);

const host = document.createElement("div");
host.className = "twin-canvas";
let twin: any = null;
let starting = false;
let lastPush = 0;

async function ensureTwin() {
  if (twin || starting) return;
  starting = true;
  try {
    const mod = await import(/* @vite-ignore */ `${location.origin}/twin/web/twin.js`);
    twin = new mod.BaymaxTwin(host, {
      baseUrl: "/twin", skeleton: false,
      onConnection: (ok: boolean) => { twinStream.value = ok ? "live" : "lost"; },
      onState: (state: any) => {
        const now = performance.now();
        if (now - lastPush > 180) { lastPush = now; twinState.value = state; }
      },
    });
    twinModel.value = await twin.init();
    // Tone the viewer's floor to this app's screen colour (scene objects, not twin.js itself).
    twin.scene.traverse((o: any) => { if (o.geometry?.type === "CircleGeometry") o.material.color.set("#152b25"); });
    twin.setCameraPreset("front");
    twin.connect();
  } catch (e) {
    twinStream.value = "failed";
    twinError.value = e instanceof Error ? e.message : String(e);
  } finally { starting = false; }
}

export function jointLabel(id: number): string {
  const row = twinModel.value?.servos?.find((s: any) => s.id === id);
  return row ? words(row.label) : `servo ${id}`;
}

export function TwinPanel({ compact = false }: { compact?: boolean }) {
  const slot = useRef<HTMLDivElement>(null);
  useEffect(() => {
    slot.current?.appendChild(host);
    ensureTwin();
  }, []);

  const s = twinState.value;
  const view: string = s?.view ?? "auto";
  const shown: string = s?.display ?? "command";
  const robotOn = !!s?.channels?.actual;
  const delta: Record<string, number> = s?.delta ?? {};
  const drift = ARM_IDS.map((id) => ({ id, d: delta[id] ?? 0 })).filter((r) => Math.abs(r.d) >= DRIFT_PULSES);
  const worst = Object.entries(delta).reduce((m, [, d]) => Math.max(m, Math.abs(d as number)), 0);
  // Only a pose that was really sent to the robot can be "not followed".
  const sent = s?.channels?.command?.label === "playing on robot";
  const setView = (v: string) => attempt(() => api("POST", "/twin/api/view", { view: v }));

  return (
    <section class={`screen twin ${compact ? "compact" : ""}`} aria-label="Digital twin">
      <div class="screen-body" ref={slot}>
        {twinStream.value === "failed" && (
          <div class="screen-empty">
            <Icon name="robot" size={28} />
            <p><strong>The 3D view couldn't start.</strong></p>
            <p>{twinError.value}</p>
            <p>The rest of the dashboard still works. This usually means WebGL is turned off in this browser.</p>
          </div>
        )}
        {twinStream.value === "connecting" && <div class="screen-empty"><p>Loading the robot model…</p></div>}
      </div>
      <header class="screen-head">
        <span class="screen-title">Twin</span>
        <div class="seg on-screen" role="group" aria-label="Which pose the twin shows">
          {[["auto", "Auto"], ["command", "Told"], ["actual", "Doing"]].map(([v, label]) => (
            <button key={v} aria-pressed={view === v} onClick={() => setView(v)}>{label}</button>
          ))}
        </div>
      </header>
      <footer class="screen-foot">
        <span class="legend-item"><i class="swatch solid" />{shown === "actual" ? "Robot is doing" : "Robot was told"}</span>
        {robotOn && <span class="legend-item"><i class="swatch ghost" />{shown === "actual" ? "Was told" : "Is doing"}</span>}
        {twinStream.value === "lost" && <span class="legend-item warn"><Icon name="alert" size={14} />Twin stream lost, retrying</span>}
        {!robotOn && twinStream.value === "live" && <span class="legend-item">No servo readback yet</span>}
        {robotOn && sent && (
          drift.length
            ? <span class="legend-item warn" title="Told and doing differ by more than about 10 degrees">
                <Icon name="alert" size={14} />Not following: {drift.map((r) => `${jointLabel(r.id)} ${r.d > 0 ? "+" : ""}${r.d}`).join(", ")}
              </span>
            : <span class="legend-item"><Icon name="check" size={14} />Following (largest gap {worst} pulses)</span>
        )}
        {robotOn && !sent && worst >= DRIFT_PULSES && <span class="legend-item">Preview only. Nothing was sent to the robot.</span>}
      </footer>
    </section>
  );
}

/** Studio previews what was told; Live follows the robot. */
export const twinView = (view: string) => { api("POST", "/twin/api/view", { view }).catch(() => {}); };
