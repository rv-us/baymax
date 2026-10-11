// The robot's camera: JPEG frames pulled one after another through the dashboard server.
import { useEffect, useRef, useState } from "preact/hooks";
import { live, status } from "./api";
import { Icon } from "./icons";

const GAP_MS = 250;      // pause between frames (about 3 a second; servo/status shares the link)
const MIRROR_GAP_MS = 100;  // the Mirror feed is shown faster: you are watching your own arms
const RETRY_MS = 3000;

/** `path` is where the frames come from: the robot's camera by default, or the Mirror feed with the arms drawn on. */
export function CameraPanel({ compact = false, path = "/api/camera.jpg" }: { compact?: boolean; path?: string }) {
  const img = useRef<HTMLImageElement>(null);
  const [state, setState] = useState<"loading" | "ok" | "down">("loading");

  useEffect(() => {
    let alive = true;
    let timer = 0;
    const el = img.current!;
    const next = (delay: number) => {
      timer = window.setTimeout(() => {
        if (!alive) return;
        if (document.hidden) return next(1000);
        el.src = `${path}?width=${compact ? 320 : 640}&t=${Date.now()}`;
      }, delay);
    };
    el.onload = () => { setState("ok"); next(path === "/api/camera.jpg" ? GAP_MS : MIRROR_GAP_MS); };
    el.onerror = () => { setState("down"); next(RETRY_MS); };
    next(0);
    return () => { alive = false; clearTimeout(timer); el.onload = el.onerror = null; };
  }, [compact, path]);

  const s = status.value;
  const f = live.value.frame;
  const t = live.value.running ? live.value.tracking : null;
  const fake = s?.dry_run && !s?.webcam;
  return (
    <section class={`screen camera ${compact ? "compact" : ""}`} aria-label="Robot camera">
      <div class="screen-body">
        <img ref={img} alt="What the robot's camera sees" class={state === "ok" ? "" : "hidden"} />
        {state === "ok" && f.visible && path === "/api/camera.jpg" && (
          <div class="frame-box" aria-hidden="true"
            style={{ left: `${50 + f.x * 35}%`, height: `${f.size * 92}%`, width: `${f.size * 42}%` }} />
        )}
        {state === "down" && (
          <div class="screen-empty">
            <Icon name="camera" size={28} />
            <p><strong>No picture from the camera.</strong></p>
            <p>{s?.links.camera.detail ?? "Trying again every few seconds."}</p>
          </div>
        )}
        {state === "loading" && <div class="screen-empty"><p>Waiting for the first frame…</p></div>}
      </div>
      <header class="screen-head">
        <span class="screen-title">Camera</span>
        {fake && <span class="tag on-screen">Fake frame</span>}
        {s?.webcam && <span class="tag on-screen">Laptop webcam{s.webcam.pose === "gx10" ? ` · pose on GX10${s.webcam.delivery != null ? ` · ${Math.round(s.webcam.delivery * 100)}% of frames answered` : ""}` : ""}</span>}
        {!s?.webcam && t && <span class="tag on-screen">{t.camera ?? "Camera"}{t.on === "gx10" ? ` · pose on GX10${t.delivery != null ? ` · ${Math.round(t.delivery * 100)}% of frames answered` : ""}` : t.on === "local" ? " · pose on this computer" : ""}</span>}
      </header>
      <footer class="screen-foot">
        {!f.visible
          ? <span class="legend-item warn"><Icon name="alert" size={14} />Patient not in view</span>
          : <span class="legend-item"><i class="swatch box" />Where the patient is{fake ? " (simulated)" : ""}</span>}
      </footer>
    </section>
  );
}
