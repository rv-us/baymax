import { render } from "preact";
import { useEffect, useState } from "preact/hooks";
import { api, attempt, connectEvents, dismiss, live, loadStatus, safety, serverUp, status, stopRobot, toasts } from "./api";
import { Icon } from "./icons";
import { drawerOpen, LinkChips, LinksDrawer, Vitals } from "./links";
import { LiveScreen } from "./live";
import { Plans } from "./plans";
import { route } from "./route";
import { SessionDetail, SessionList } from "./sessions";
import { Studio } from "./studio";
import "./styles.css";


const NAV: [string, string, string][] = [["/", "Studio", "studio"], ["/live", "Live", "live"], ["/sessions", "Sessions", "sessions"], ["/plans", "Plans", "plans"]];
const isAt = (path: string) => (path === "/" ? route.value === "/" : route.value.startsWith(path));

function StopButton() {
  const [sending, setSending] = useState(false);
  return (
    <button class="stop" disabled={sending} onClick={async () => { setSending(true); await stopRobot(); setSending(false); }}
      aria-label="Stop the robot now">
      <Icon name="stop" size={22} /><span>Stop</span>
    </button>
  );
}

function SafetyBanner() {
  const s = safety.value;
  const [confirming, setConfirming] = useState(false);
  useEffect(() => { if (!s.stop) setConfirming(false); }, [s.stop]);
  const clear = () => attempt(() => api("POST", "/api/safety/clear", { confirm: true, by: "therapist" }), "Safety stop cleared. The robot can move again.");
  return (
    <div class="banners" aria-live="assertive">
      {serverUp.value === false && (
        <div class="banner banner-warn" role="alert"><Icon name="alert" /><div><b>Lost contact with the dashboard server.</b> What you see may be out of date. Reconnecting…</div></div>
      )}
      {s.stop && (
        <div class="banner banner-stop" role="alert">
          <Icon name="alert" size={28} />
          <div>
            <b>Robot frozen: {s.stop.title}.</b>{" "}
            {s.stop.transcript ? <>Heard “{s.stop.transcript}” at {new Date(s.stop.t * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}. </> : s.stop.detail + " "}
            Check on the patient. Nothing will move until you clear this.
            {s.stop.stop_errors.length > 0 && <div class="banner-sub">The robot didn't confirm the freeze: {s.stop.stop_errors.join("; ")}</div>}
          </div>
          {confirming ? (
            <span class="banner-actions">
              <button class="btn on-stop" onClick={clear}>Yes, the patient is okay</button>
              <button class="btn on-stop quiet" onClick={() => setConfirming(false)}>Keep it stopped</button>
            </span>
          ) : <button class="btn on-stop" onClick={() => setConfirming(true)}>Clear the stop…</button>}
        </div>
      )}
      {s.alerts.map((a) => (
        <div key={a.kind} class="banner banner-warn" role="status"><Icon name="alert" /><div>{a.detail}</div></div>
      ))}
    </div>
  );
}

function Toasts() {
  return (
    <div class="toasts" role="region" aria-label="Messages" aria-live="polite">
      {toasts.value.map((t) => (
        <div key={t.id} class={`toast toast-${t.kind}`} role={t.kind === "error" ? "alert" : "status"}>
          <Icon name={t.kind === "error" ? "alert" : t.kind === "ok" ? "check" : "coach"} />
          <span>{t.text}</span>
          <button class="btn icon-only quiet" aria-label="Dismiss" onClick={() => dismiss(t.id)}><Icon name="cross" size={16} /></button>
        </div>
      ))}
    </div>
  );
}

/** Large type for the patient: what to do, and how far along they are. */
function PatientView() {
  const l = live.value;
  const resting = !l.running;
  return (
    <div class="patient">
      <a class="patient-exit" href="#/live"><Icon name="left" size={18} />Back to the dashboard</a>
      {resting ? (
        <div class="patient-idle"><h1>Hello!</h1><p>Baymax will show you each exercise. We'll start when you're ready.</p></div>
      ) : (
        <>
          <p class="patient-kicker">{l.state === "MIRROR" ? "Your turn" : l.state === "DEMONSTRATE" ? "Watch Baymax" : "Get ready"}</p>
          <h1>{l.exercise?.name}</h1>
          <p class="patient-cue">{l.exercise?.cue}</p>
          <div class="patient-reps" aria-live="polite"><b>{l.reps ?? 0}</b><span>of {l.target_reps ?? 0}</span></div>
          <div class="pips big" aria-hidden="true">
            {Array.from({ length: l.target_reps ?? 0 }, (_, i) => <i key={i} class={i < (l.reps ?? 0) ? "on" : ""} />)}
          </div>
          {l.said && <p class="patient-said">“{l.said}”</p>}
        </>
      )}
    </div>
  );
}

function Screen() {
  const r = route.value;
  if (r === "/") return <Studio />;
  if (r.startsWith("/live")) return <LiveScreen />;
  if (r.startsWith("/sessions/")) return <SessionDetail id={decodeURIComponent(r.slice("/sessions/".length))} />;
  if (r.startsWith("/sessions")) return <SessionList />;
  if (r.startsWith("/plans")) return <Plans />;
  return <div class="page-empty"><h2>There's no page here</h2><a class="btn" href="#/">Open Studio</a></div>;
}

function App() {
  useEffect(() => { loadStatus(); connectEvents(); }, []);
  const patient = route.value.startsWith("/patient");
  return (
    <div class={`app ${patient ? "is-patient" : ""}`}>
      <a class="skip" href="#main">Skip to the page</a>
      <div class="chrome">
      <header class="top">
        <a class="brand" href="#/" aria-label="Baymax, Studio">
          <svg viewBox="0 0 44 24" width="40" height="22" aria-hidden="true"><circle cx="9" cy="12" r="5" /><circle cx="35" cy="12" r="5" /><path d="M9 12h26" /></svg>
          <span>Baymax</span>
        </a>
        <nav class="nav" aria-label="Main">
          {NAV.map(([path, label]) => <a key={path} href={`#${path}`} aria-current={isAt(path) ? "page" : undefined}>{label}</a>)}
        </nav>
        {status.value?.dry_run && <span class="tag tag-dry" title="Fake hardware, synthetic sessions, stub coach">Dry run</span>}
        <LinkChips />
        <Vitals />
        <StopButton />
      </header>
      <SafetyBanner />
      </div>
      <main id="main" tabIndex={-1}>{patient ? <PatientView /> : <Screen />}</main>
      {!patient && (
        <nav class="tabbar" aria-label="Main">
          {NAV.map(([path, label, icon]) => (
            <a key={path} href={`#${path}`} aria-current={isAt(path) ? "page" : undefined}><Icon name={icon} size={24} /><span>{label}</span></a>
          ))}
          <button onClick={() => { drawerOpen.value = true; }}><Icon name="settings" size={24} /><span>Links</span></button>
        </nav>
      )}
      <LinksDrawer />
      <Toasts />
    </div>
  );
}

render(<App />, document.getElementById("app")!);
