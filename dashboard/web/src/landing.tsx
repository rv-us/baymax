// The front page: what Baymax is, shown with the product itself. The twin in the hero is
// the real viewer playing a real library animation, and the session below it is read from
// the dashboard this page is served by.
import { useEffect, useMemo, useState } from "preact/hooks";
import { api, Clip, Session, SessionRow, status, when } from "./api";
import { ScoreArc } from "./analytics";
import { Icon } from "./icons";
import { MotionTwin, solveClip } from "./motion";

const HERO_MOVE = "overhead_reach";

/** The joint that moves most in a clip, as degrees from where it started, per frame. */
function leadAngle(clip: Clip | null): number[] {
  if (!clip || !clip.frames.length) return [];
  const first = clip.frames[0].angles;
  let best = "", span = 0;
  for (const name of Object.keys(first)) {
    if (!name.startsWith("l_") && !name.includes("left")) continue;
    const vals = clip.frames.map((f) => f.angles[name]);
    const d = Math.max(...vals) - Math.min(...vals);
    if (d > span) { span = d; best = name; }
  }
  if (!best) for (const name of Object.keys(first)) {
    const vals = clip.frames.map((f) => f.angles[name]);
    const d = Math.max(...vals) - Math.min(...vals);
    if (d > span) { span = d; best = name; }
  }
  return clip.frames.map((f) => Math.abs(f.angles[best] - first[best]));
}

/** A protractor that reads the robot's shoulder as the hero animation plays. */
function Protractor({ deg }: { deg: number }) {
  const R = 120, CX = 150, CY = 150;
  const pt = (a: number, r: number) => [CX + r * Math.cos(((180 - a) * Math.PI) / 180), CY - r * Math.sin(((180 - a) * Math.PI) / 180)];
  const [nx, ny] = pt(Math.min(180, deg), R - 8);
  const [ax, ay] = pt(Math.min(180, deg), R);
  return (
    <svg class="protractor" viewBox="0 0 300 172" role="img" aria-label={`Shoulder angle ${Math.round(deg)} degrees`}>
      <path d={`M${CX - R} ${CY}A${R} ${R} 0 0 1 ${CX + R} ${CY}`} class="pro-arc" />
      <path d={`M${CX - R} ${CY}A${R} ${R} 0 0 1 ${ax.toFixed(1)} ${ay.toFixed(1)}`} class="pro-sweep" />
      {Array.from({ length: 37 }, (_, i) => {
        const a = i * 5, major = a % 45 === 0, [x0, y0] = pt(a, R + 3), [x1, y1] = pt(a, R + (major ? 16 : a % 15 === 0 ? 11 : 7));
        return <line key={i} x1={x0} y1={y0} x2={x1} y2={y1} class={major ? "pro-tick major" : "pro-tick"} />;
      })}
      {[0, 45, 90, 135, 180].map((a) => { const [x, y] = pt(a, R + 30); return <text key={a} x={x} y={y + 4} text-anchor="middle" class="pro-num">{a}</text>; })}
      <line x1={CX} y1={CY} x2={nx} y2={ny} class="pro-needle" />
      <circle cx={CX} cy={CY} r="5" class="pro-hub" />
    </svg>
  );
}

const STEPS: [string, string, string][] = [
  ["Show", "The robot says what the exercise is and does it first, slowly, so there is something to copy.", "robot"],
  ["Mirror", "The patient follows along. A camera on the robot tracks their shoulders and elbows as they move.", "mirror"],
  ["Measure", "Every rep is counted and compared with the demonstration: how high, how fast, how smooth, how even.", "studio"],
  ["Review", "The session is saved by itself. The therapist opens it later and sees where it went well and where it drifted.", "chart"],
];

const FEATURES: [string, string, string, string][] = [
  ["/library", "library", "A library the whole room can use", "Every exercise and emote in one list. Point to preview on the twin, click to play on the robot from any laptop on the network."],
  ["/record", "record", "Teach by hand", "Let the arms go limp, move them yourself, trim the take, name it. It is in the library a few seconds later."],
  ["/generate", "spark", "Or just describe it", "Type what the robot should do. A model on the GX10 writes the motion, the twin previews it, you keep it or bin it."],
  ["/sessions", "chart", "Analysis you can read", "One score first. Then range, reps, tempo, smoothness, left against right, and progress across visits, each with a line on what it means."],
  ["/coach", "coach", "A coach to ask", "Chat about any session. It sees that session's measurements, explains them in plain words, and never diagnoses."],
  ["/calibrate", "tune", "A twin you can trust", "Per-servo readout, limits, offsets and speed caps, saved as profiles and enforced on every command."],
];

const GUARDS: [string, string][] = [
  ["Limits live in the command path.", "Every servo command passes the calibration profile and then the robot's own limits. An animation that asks for more gets less, and says so."],
  ["Stop is on every screen.", "One press freezes every servo and ends whatever was running. A click on a move asks first unless one-click play is switched on."],
  ["Pain words freeze the robot.", "If the patient says it hurts, the robot stops first and tells you second. Nothing moves again until a person clears it."],
  ["The model only narrates.", "Every number on screen is computed from the measurements. The language model explains them and is never asked for one."],
];

const LIMITS = [
  "Pose tracking is still a simulation in this build. Live sessions replay a synthetic patient, and their logs are labelled synthetic.",
  "Exercises are upper body. Moving one leg joint at a time makes this robot fall, so leg keyframes are refused.",
  "The servo bus doesn't report load. A growing gap between told and doing is the stall signal instead.",
  "Baymax is not a medical device and gives no medical advice. It reports what it measured and flags what a therapist should look at.",
];

export function Landing() {
  const [clip, setClip] = useState<Clip | null>(null);
  const [clipError, setClipError] = useState<string | null>(null);
  const [ms, setMs] = useState(0);
  const [latest, setLatest] = useState<Session | null>(null);
  const [count, setCount] = useState<number | null>(null);
  const [moves, setMoves] = useState<number | null>(null);

  useEffect(() => {
    document.title = "Baymax: a physical therapy companion you can measure";
    solveClip(`hero:${HERO_MOVE}`, async () => (await api("GET", `/api/animations/${HERO_MOVE}`)).keyframes).then(setClip).catch((e) => setClipError(e.message));
    api<SessionRow[]>("GET", "/api/sessions").then(async (rows) => {
      const scored = rows.filter((r) => !r.unreadable && r.kind === "exercise" && r.scores.overall.score != null);
      setCount(scored.length);
      const best = scored.find((r) => r.status === "completed") ?? scored[0];
      if (best) setLatest(await api<Session>("GET", `/api/sessions/${best.id}`));
    }).catch(() => setCount(0));
    api<{ animations: unknown[] }>("GET", "/api/library").then((l) => setMoves(l.animations.length)).catch(() => {});
    return () => { document.title = "Baymax"; };
  }, []);

  const angles = useMemo(() => leadAngle(clip), [clip]);
  const deg = clip && angles.length ? angles[Math.min(angles.length - 1, Math.round((ms / 1000) * clip.hz))] : 0;
  const dry = status.value?.dry_run;

  return (
    <div class="landing">
      <header class="land-top">
        <a class="brand" href="#/" aria-label="Baymax">
          <svg viewBox="0 0 44 24" width="40" height="22" aria-hidden="true"><circle cx="9" cy="12" r="5" /><circle cx="35" cy="12" r="5" /><path d="M9 12h26" /></svg>
          <span>Baymax</span>
        </a>
        <nav aria-label="On this page">
          <a href="#/" onClick={(e) => { e.preventDefault(); document.getElementById("how")?.scrollIntoView({ behavior: "smooth" }); }}>How it works</a>
          <a href="#/" onClick={(e) => { e.preventDefault(); document.getElementById("session")?.scrollIntoView({ behavior: "smooth" }); }}>A real session</a>
          <a href="#/" onClick={(e) => { e.preventDefault(); document.getElementById("safety")?.scrollIntoView({ behavior: "smooth" }); }}>Safety</a>
        </nav>
        <a class="btn land-cta" href="#/library">Open the dashboard</a>
      </header>

      <section class="hero">
        <div class="hero-copy">
          <h1>The robot shows the exercise. The dashboard shows how it went.</h1>
          <p>Baymax is a small humanoid that leads older adults through their physical therapy exercises at home, and a dashboard that turns every session into something a therapist can read in a minute.</p>
          <div class="hero-actions">
            <a class="btn big land-cta" href="#/library">Open the dashboard</a>
            <a class="btn big land-ghost" href="#/sessions">See a session analysed</a>
          </div>
          <p class="hero-note">{dry ? "This copy is running in dry run: fake robot, synthetic patient, nothing to install." : "Connected to the robot on this network."}</p>
        </div>
        <div class="hero-stage">
          <MotionTwin clip={clip} playing={!!clip} loop onTime={setMs} title="Overhead reach" busy={!clip && !clipError} error={clipError} />
          <div class="hero-read" aria-hidden={!clip}>
            <Protractor deg={deg} />
            <p><b>{Math.round(deg)}°</b><span>shoulder, read from the twin as it moves</span></p>
          </div>
        </div>
      </section>

      <section class="land-band problem">
        <h2>Most of physical therapy happens where no therapist can see it.</h2>
        <div class="problem-cols">
          <p><b>Exercises get skipped.</b> A printed sheet of stretches is easy to put down. Doing them alone, with nobody counting along, is harder to keep up than the clinic visit that prescribed them.</p>
          <p><b>Form drifts quietly.</b> A reach that stops a little short, one arm doing more than the other, a pace that speeds up when it gets uncomfortable. Nobody notices until the next appointment.</p>
          <p><b>The check-up starts from memory.</b> "How have the exercises been going?" is answered with "fine". The therapist has twenty minutes and no record of the last three weeks.</p>
        </div>
      </section>

      <section class="land-band" id="how">
        <h2>One loop, four moves</h2>
        <ol class="loop">
          {STEPS.map(([title, text, icon], i) => (
            <li key={title}>
              <span class="loop-n">{i + 1}</span>
              <Icon name={icon} size={26} />
              <h3>{title}</h3>
              <p>{text}</p>
            </li>
          ))}
        </ol>
      </section>

      <section class="land-band session-demo" id="session">
        <div class="demo-copy">
          <h2>A session, as the therapist sees it</h2>
          <p>This is not a mock-up. It is the newest completed session in the dashboard behind this page{latest?.synthetic ? ", from the built-in synthetic patient" : ""}, scored the same way every session is.</p>
          <p>The number comes first. Under it are the reasons, in sentences. Under those, a chart for each reason, each with a line that says how to read it.</p>
          {latest && <a class="btn big land-cta" href={`#/sessions/${latest.id}`}>Open this session's full analysis</a>}
          {count === 0 && <a class="btn big land-cta" href="#/live">Run the first session</a>}
        </div>
        <div class="demo-card">
          {latest ? (
            <>
              <div class="demo-score">
                <ScoreArc score={latest.scores.overall.score} size={240} />
                <div class="headline-number"><b>{latest.scores.overall.score}</b><span>out of 100</span><strong>{latest.scores.overall.label}</strong></div>
              </div>
              <p class="demo-when">{when(latest.started).date}, {latest.scores.reps} of {latest.scores.target_reps} reps{latest.synthetic && <span class="tag tag-syn">Synthetic data</span>}</p>
              <ul class="insights">
                {latest.insights.slice(0, 4).map((i, n) => <li key={n} class={`tone-${i.tone}`}><Icon name={i.tone === "good" ? "check" : "alert"} size={18} /><span>{i.text}</span></li>)}
              </ul>
            </>
          ) : count === 0 ? (
            <div class="chart-empty"><Icon name="sessions" size={24} /><p><b>No sessions saved yet.</b> Run one from Live and it will appear here, scored.</p></div>
          ) : <p class="hint">Reading the newest session…</p>}
        </div>
      </section>

      <section class="land-band">
        <h2>What is in the dashboard</h2>
        <ul class="features">
          {FEATURES.map(([path, icon, title, text]) => (
            <li key={path}>
              <a href={`#${path}`}>
                <Icon name={icon} size={26} />
                <h3>{title}</h3>
                <p>{text}</p>
                <span class="feature-go">Open it<Icon name="right" size={16} /></span>
              </a>
            </li>
          ))}
        </ul>
        {moves != null && <p class="land-fact">{moves} exercises and emotes are in the library right now{count ? `, and ${count} scored sessions are saved` : ""}.</p>}
      </section>

      <section class="land-band guards" id="safety">
        <h2>What stops a bad move</h2>
        <dl>
          {GUARDS.map(([title, text]) => <div key={title}><dt>{title}</dt><dd>{text}</dd></div>)}
        </dl>
      </section>

      <section class="land-band limits">
        <h2>Limits, said plainly</h2>
        <ul>{LIMITS.map((l) => <li key={l}>{l}</li>)}</ul>
      </section>

      <section class="land-close">
        <h2>See it move.</h2>
        <p>Pick an exercise, watch the twin, press play. It takes about ten seconds.</p>
        <div class="hero-actions">
          <a class="btn big land-cta" href="#/library">Open the dashboard</a>
          <a class="btn big land-ghost" href="#/live">Run a session</a>
        </div>
      </section>
    </div>
  );
}
