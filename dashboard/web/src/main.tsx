import { render } from "preact";
import { useEffect, useState } from "preact/hooks";
import { api, attempt, calibration, connectEvents, dismiss, live, loadStatus, playback, recorder, safety, serverUp, status, stopRobot, toasts } from "./api";
import { Calibrate } from "./calibrate";
import { Coach } from "./coach";
import { Generate } from "./generate";
import { Icon } from "./icons";
import { Landing } from "./landing";
import { Library } from "./library";
import { drawerOpen, LinkChips, LinksDrawer, Vitals } from "./links";
import { LiveScreen } from "./live";
import { Plans } from "./plans";
import { Record as RecordScreen } from "./record";
import { route } from "./route";
import { SessionDetail, SessionList } from "./sessions";
import { Studio } from "./studio";
import "./styles.css";
import "./more.css";

// The dashboard's pages, grouped by the job they do. One list drives the side rail, the
// phone's bottom bar and its "More" sheet, so the three can't disagree.
interface Page { path: string; label: string; icon: string; about: string }
const GROUPS: { name: string; pages: Page[] }[] = [
  { name: "Move", pages: [
    { path: "/library", label: "Library", icon: "library", about: "Preview and play exercises and emotes" },
    { path: "/studio", label: "Studio", icon: "studio", about: "Build an animation keyframe by keyframe" },
    { path: "/record", label: "Record", icon: "record", about: "Record a motion by moving the arms" },
    { path: "/generate", label: "Generate", icon: "spark", about: "Describe a motion and let the model write it" },
  ] },
  { name: "Train", pages: [
    { path: "/live", label: "Live", icon: "live", about: "Run a session and coach it" },
    { path: "/plans", label: "Plans", icon: "plans", about: "What the robot teaches, in order" },
  ] },
  { name: "Review", pages: [
    { path: "/sessions", label: "Sessions", icon: "sessions", about: "Saved sessions and their analysis" },
    { path: "/coach", label: "Coach", icon: "coach", about: "Ask about a session" },
  ] },
  { name: "Tune", pages: [
    { path: "/calibrate", label: "Calibrate", icon: "tune", about: "Servo readout, limits and profiles" },
  ] },
];
const PAGES = GROUPS.flatMap((g) => g.pages);
const PHONE_TABS = ["/library", "/live", "/sessions", "/coach"];
const isAt = (path: string) => route.value === path || route.value.startsWith(path + "/");

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
  if (isAt("/library")) return <Library />;
  if (isAt("/studio")) return <Studio />;
  if (isAt("/record")) return <RecordScreen />;
  if (isAt("/generate")) return <Generate />;
  if (isAt("/live")) return <LiveScreen />;
  if (r.startsWith("/sessions/")) return <SessionDetail id={decodeURIComponent(r.slice("/sessions/".length))} />;
  if (isAt("/sessions")) return <SessionList />;
  if (r.startsWith("/coach/")) return <Coach id={decodeURIComponent(r.slice("/coach/".length))} />;
  if (isAt("/coach")) return <Coach />;
  if (isAt("/plans")) return <Plans />;
  if (isAt("/calibrate")) return <Calibrate />;
  return (
    <div class="page-empty">
      <Icon name="alert" size={28} />
      <h2>There's no page here</h2>
      <p>The address <code>#{r}</code> doesn't match anything in the dashboard.</p>
      <div class="row"><a class="btn primary" href="#/library">Open the library</a><a class="btn" href="#/">Front page</a></div>
    </div>
  );
}

/** A dot on a nav item when something is running there. */
function busyAt(path: string): string | null {
  if (path === "/live" && live.value.running) return "A session is running";
  if (path === "/record" && recorder.value.recording) return "Recording";
  if (path === "/library" && playback.value.playing) return "Playing on the robot";
  if (path === "/calibrate" && calibration.value && calibration.value.active !== "factory") return `Profile “${calibration.value.name}” is in force`;
  return null;
}

function NavLink({ page, onClick }: { page: Page; onClick?: () => void }) {
  const busy = busyAt(page.path);
  return (
    <a href={`#${page.path}`} aria-current={isAt(page.path) ? "page" : undefined} onClick={onClick} title={page.about}>
      <Icon name={page.icon} size={20} /><span>{page.label}</span>
      {busy && <i class={`nav-dot ${page.path === "/calibrate" ? "is-quiet" : ""}`} role="img" aria-label={busy} title={busy} />}
    </a>
  );
}

function App() {
  const [more, setMore] = useState(false);
  useEffect(() => { loadStatus(); connectEvents(); }, []);
  useEffect(() => { setMore(false); }, [route.value]);
  useEffect(() => {
    if (!more) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setMore(false); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [more]);
  const r = route.value;
  const patient = r.startsWith("/patient");
  const here = PAGES.find((p) => isAt(p.path));
  useEffect(() => { if (r !== "/") document.title = here ? `${here.label}, Baymax dashboard` : "Baymax dashboard"; }, [here?.path, r === "/"]);
  if (r === "/") return <><Landing /><Toasts /></>;
  return (
    <div class={`app ${patient ? "is-patient" : ""}`}>
      <a class="skip" href="#main" onClick={(e) => { e.preventDefault(); document.getElementById("main")?.focus(); }}>Skip to the page</a>
      <div class="chrome">
        <header class="top">
          <a class="brand" href="#/" aria-label="Baymax, front page">
            <svg viewBox="0 0 44 24" width="40" height="22" aria-hidden="true"><circle cx="9" cy="12" r="5" /><circle cx="35" cy="12" r="5" /><path d="M9 12h26" /></svg>
            <span>Baymax</span>
          </a>
          {here && <span class="crumb" aria-hidden="true">{GROUPS.find((g) => g.pages.includes(here))!.name}<Icon name="right" size={14} /><b>{here.label}</b></span>}
          <span class="top-gap" />
          {status.value?.dry_run && <span class="tag tag-dry" title="Fake hardware, synthetic sessions, stub coach">Dry run</span>}
          <LinkChips />
          <Vitals />
          <StopButton />
        </header>
        <SafetyBanner />
      </div>
      <div class="body">
        {!patient && (
          <nav class="rail" aria-label="Main">
            {GROUPS.map((g) => (
              <div key={g.name} class="rail-group" role="group" aria-label={g.name}>
                <h2>{g.name}</h2>
                {g.pages.map((p) => <NavLink key={p.path} page={p} />)}
              </div>
            ))}
            <button class="rail-settings" onClick={() => { drawerOpen.value = true; }}><Icon name="settings" size={20} /><span>Connections</span></button>
          </nav>
        )}
        <main id="main" tabIndex={-1}>{patient ? <PatientView /> : <Screen />}</main>
      </div>
      {!patient && (
        <nav class="tabbar" aria-label="Main">
          {PHONE_TABS.map((path) => {
            const p = PAGES.find((x) => x.path === path)!;
            return <a key={path} href={`#${path}`} aria-current={isAt(path) ? "page" : undefined}><Icon name={p.icon} size={24} /><span>{p.label}</span>{busyAt(path) && <i class="nav-dot" />}</a>;
          })}
          <button aria-expanded={more} aria-controls="more-sheet" class={here && !PHONE_TABS.includes(here.path) ? "is-here" : ""} onClick={() => setMore(!more)}>
            <Icon name="more" size={24} /><span>{here && !PHONE_TABS.includes(here.path) ? here.label : "More"}</span>
          </button>
        </nav>
      )}
      {more && (
        <div class="sheet-wrap" onClick={(e) => { if (e.target === e.currentTarget) setMore(false); }}>
          <div class="sheet" id="more-sheet" role="dialog" aria-modal="true" aria-label="All pages">
            <header><h2>All pages</h2><button class="btn icon-only quiet" aria-label="Close" onClick={() => setMore(false)}><Icon name="cross" /></button></header>
            {GROUPS.map((g) => (
              <div key={g.name} class="sheet-group">
                <h3>{g.name}</h3>
                {g.pages.map((p) => (
                  <a key={p.path} href={`#${p.path}`} aria-current={isAt(p.path) ? "page" : undefined}>
                    <Icon name={p.icon} size={22} /><span><b>{p.label}</b><small>{p.about}</small></span>
                  </a>
                ))}
              </div>
            ))}
            <div class="sheet-group">
              <button onClick={() => { setMore(false); drawerOpen.value = true; }}><Icon name="settings" size={22} /><span><b>Connections</b><small>Robot, camera, coach and theme</small></span></button>
              <a href="#/"><Icon name="home" size={22} /><span><b>Front page</b><small>What Baymax is</small></span></a>
            </div>
          </div>
        </div>
      )}
      <LinksDrawer />
      <Toasts />
    </div>
  );
}

render(<App />, document.getElementById("app")!);
