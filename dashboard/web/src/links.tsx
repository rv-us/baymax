// The three links (robot, camera, coach): always-visible status, and a drawer to fix them.
import { signal } from "@preact/signals";
import { useEffect, useState } from "preact/hooks";
import { api, attempt, cap, setTheme, settings, status, theme, Theme, tunnel, words } from "./api";
import { Icon, StateMark } from "./icons";

export const drawerOpen = signal(false);

const STATE_TEXT: Record<string, string> = { ok: "Connected", down: "Not connected", unknown: "Checking", stub: "Stub" };
const NAMES: [keyof NonNullable<typeof status.value>["links"], string, string][] = [
  ["robot", "Robot", "robot"], ["camera", "Camera", "camera"], ["llm", "Coach", "coach"],
];

export function LinkChips() {
  const s = status.value;
  return (
    <button class="links" onClick={() => { drawerOpen.value = true; }} aria-label="Connections: open details">
      {NAMES.map(([key, name]) => {
        const state = s?.links[key].state ?? "unknown";
        return (
          <span key={key} class={`link link-${state}`} title={s?.links[key].detail}>
            <StateMark state={state} /><span class="link-name">{name}</span><span class="link-state">{STATE_TEXT[state]}</span>
          </span>
        );
      })}
    </button>
  );
}

export function Vitals() {
  const h = status.value?.health;
  if (!h) return null;
  const hot = h.hottest && h.max_allowed_c != null && h.hottest.temp_c >= h.max_allowed_c;
  return (
    <div class="vitals">
      <span class={h.needs_charge ? "is-bad" : ""} title="Battery">
        <Icon name="battery" size={18} />{h.battery_v != null ? `${h.battery_v.toFixed(1)} V` : "no reading"}{h.needs_charge && " low"}
      </span>
      {h.hottest && (
        <span class={hot ? "is-bad" : ""} title={`Hottest servo: ${words(h.hottest.joint)} (limit ${h.max_allowed_c} °C)`}>
          <Icon name="temp" size={18} />{h.hottest.temp_c} °C{hot && " too hot"}
        </span>
      )}
    </div>
  );
}

function CoachSettings() {
  const cfg = settings.value;
  const t = tunnel.value;
  const [url, setUrl] = useState(cfg?.ollama_url ?? "");
  const [model, setModel] = useState(cfg?.model ?? "");
  const [host, setHost] = useState(t?.host ?? "");
  const [user, setUser] = useState(t?.user ?? "asus");
  const [password, setPassword] = useState("");
  const [working, setWorking] = useState(false);
  useEffect(() => { if (cfg) { setUrl(cfg.ollama_url); setModel(cfg.model); } }, [cfg?.ollama_url, cfg?.model]);

  const saveUrl = async (e: Event) => {
    e.preventDefault();
    const out = await attempt(() => api("PUT", "/api/settings", { ollama_url: url, model }), "Coach settings saved.");
    if (out) settings.value = { ...cfg!, ollama_url: out.ollama_url, model: out.model, stub_llm: false };
  };
  const openTunnel = async (e: Event) => {
    e.preventDefault();
    setWorking(true);
    const out = await attempt(() => api("POST", "/api/tunnel", { host, user, password: password || undefined }), "Tunnel open.");
    setPassword("");
    setWorking(false);
    if (out) settings.value = { ...cfg!, ollama_url: `http://127.0.0.1:${out.port}`, stub_llm: false };
  };

  return (
    <>
      <form class="stack" onSubmit={saveUrl}>
        <label>Ollama address<input type="text" value={url} placeholder="http://gx10-ip:11434" onInput={(e) => setUrl(e.currentTarget.value)} /></label>
        <label>Model<input type="text" value={model} placeholder="qwen3.6:35b" onInput={(e) => setModel(e.currentTarget.value)} /></label>
        <button class="btn">Save and test</button>
        {cfg?.stub_llm && <p class="hint">Dry run uses a stub coach. Saving an address here switches to a real model.</p>}
      </form>

      <h3>SSH tunnel to the GX10</h3>
      <p class="hint">Ollama on the GX10 only listens on the GX10 itself. A tunnel makes it reachable from this computer.</p>
      {t?.open ? (
        <div class="stack">
          <p class="note"><Icon name="check" size={16} />Tunnel open to {t.user}@{t.host}, local port {t.port}.</p>
          <button class="btn" onClick={() => attempt(() => api("DELETE", "/api/tunnel"), "Tunnel closed.")}>Close the tunnel</button>
        </div>
      ) : (
        <form class="stack" onSubmit={openTunnel}>
          <label>GX10 address<input type="text" value={host} placeholder="192.168.1.50" autocomplete="off" onInput={(e) => setHost(e.currentTarget.value)} /></label>
          <label>User<input type="text" value={user} autocomplete="off" onInput={(e) => setUser(e.currentTarget.value)} /></label>
          <label>Password <small>(leave empty to use your SSH key)</small>
            <input type="password" value={password} autocomplete="off" onInput={(e) => setPassword(e.currentTarget.value)} /></label>
          <button class="btn" disabled={working || !host.trim()}>{working ? "Connecting…" : "Open tunnel"}</button>
          {t?.error && <p class="note bad"><Icon name="alert" size={16} />{t.error}</p>}
          <p class="hint">The password is passed straight to ssh and kept in memory only. Or run it yourself: <code>{t?.manual}</code></p>
        </form>
      )}
    </>
  );
}

export function LinksDrawer() {
  const s = status.value;
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") drawerOpen.value = false; };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);
  if (!drawerOpen.value) return null;
  return (
    <div class="drawer-wrap" onClick={(e) => { if (e.target === e.currentTarget) drawerOpen.value = false; }}>
      <div class="drawer" role="dialog" aria-modal="true" aria-label="Connections">
        <header>
          <h2>Connections</h2>
          <button class="btn icon-only quiet" aria-label="Close" onClick={() => { drawerOpen.value = false; }}><Icon name="cross" /></button>
        </header>
        <section class="link-card">
          <h3>Appearance</h3>
          <div class="theme" role="group" aria-label="Colour theme">
            {(["system", "light", "dark"] as Theme[]).map((t) => (
              <button key={t} aria-pressed={theme.value === t} onClick={() => setTheme(t)}>{t === "system" ? "Match this device" : cap(t)}</button>
            ))}
          </div>
        </section>
        {!s ? <p>Waiting for the dashboard server…</p> : (
          <>
            {NAMES.map(([key, name, icon]) => {
              const link = s.links[key];
              return (
                <section key={key} class={`link-card link-${link.state}`}>
                  <h3><Icon name={icon} />{name}<span class="link-state"><StateMark state={link.state} />{STATE_TEXT[link.state]}</span></h3>
                  <p>{link.detail}</p>
                  {key === "robot" && link.latency_ms != null && <p class="hint">Last call took {link.latency_ms} ms.</p>}
                  {key === "robot" && !s.dry_run && <p class="hint">The address and token are set where the dashboard was started (<code>--robot-url</code>, <code>BAYMAX_API_TOKEN</code>). They never reach this browser.</p>}
                  {key === "llm" && <CoachSettings />}
                </section>
              );
            })}
            {s.health?.temps_c && (
              <section class="link-card">
                <h3><Icon name="temp" />Servo temperatures</h3>
                <ul class="temps">
                  {Object.entries(s.health.temps_c).map(([joint, t]) => (
                    <li key={joint} class={t != null && s.health!.max_allowed_c != null && t >= s.health!.max_allowed_c ? "is-bad" : ""}>
                      <span>{cap(words(joint))}</span><b>{t ?? "–"} °C</b>
                    </li>
                  ))}
                </ul>
                <p class="hint">The robot refuses to move at {s.health.max_allowed_c} °C or above.</p>
              </section>
            )}
          </>
        )}
      </div>
    </div>
  );
}
