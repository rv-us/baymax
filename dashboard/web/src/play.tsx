// Playing on the real robot, from any screen: one request at a time, an arm switch so a
// stray click can't move it, and a status line that says what happened to the request.
import { effect, signal } from "@preact/signals";
import { useEffect, useState } from "preact/hooks";
import { api, ApiError, calibration, Keyframe, live, playback, recorder, status, stopRobot, words } from "./api";
import { Icon } from "./icons";

export interface PlayTarget { key: string; name: string; id?: string; builtin?: string; keyframes?: Keyframe[]; speed?: number }
type Phase = "sending" | "playing" | "done" | "failed" | "stopped";
export interface PlayRequest { key: string; name: string; phase: Phase; message?: string; request?: number; at: number }

const ARM_MINUTES = 5;
const RESULT_SECONDS = 20;
export const armed = signal(false);
export const request = signal<PlayRequest | null>(null);
let disarmTimer = 0;

export function setArmed(on: boolean) {
  armed.value = on;
  clearTimeout(disarmTimer);
  if (on) disarmTimer = window.setTimeout(() => { armed.value = false; }, ARM_MINUTES * 60_000);
}

/** Why the robot can't be played right now, in words, or null if it can. */
export function blockedReason(): string | null {
  const motion = status.value?.motion;
  if (playback.value.playing) return `“${playback.value.name || "An animation"}” is playing. Wait for it to finish, or stop it.`;
  if (live.value.running) return "A session is running on the robot.";
  if (recorder.value.recording) return "A recording is running.";
  if (!motion) return "Still checking the robot.";
  if (!motion.ok) return motion.reasons.join(" ");
  return null;
}

export async function playOnRobot(target: PlayTarget) {
  request.value = { key: target.key, name: target.name, phase: "sending", at: Date.now() };
  try {
    const out = target.keyframes
      ? await api("POST", "/api/playback/start", { keyframes: target.keyframes, name: target.name, speed: target.speed ?? 1 })
      : await api("POST", "/api/library/play", target.builtin ? { builtin: target.builtin } : { id: target.id, speed: target.speed ?? 1 });
    if (request.value?.key === target.key) request.value = { ...request.value, phase: "playing", request: out.request };
    settle();  // a short move can finish before this reply arrives
  } catch (e) {
    const refused = e instanceof ApiError && e.status === 409;
    request.value = { key: target.key, name: target.name, phase: "failed", at: Date.now(),
      message: `${refused ? "The robot refused" : "The request failed"}: ${e instanceof Error ? e.message : e}` };
  }
}

// The server's playback state settles the request: finished, stopped, or stopped by the robot.
function settle() {
  const pb = playback.peek(), r = request.peek();
  if (!r || r.phase !== "playing" || pb.request !== r.request || pb.playing) return;
  request.value = pb.error ? { ...r, phase: "failed", message: `The robot stopped it: ${pb.error}`, at: Date.now() }
    : pb.stopped ? { ...r, phase: "stopped", at: Date.now() } : { ...r, phase: "done", at: Date.now() };
}
effect(() => { playback.value; settle(); });

/** The arm switch and the e-stop, with what the robot is doing with the last request. */
export function RobotBar({ stacked = false }: { stacked?: boolean }) {
  const r = request.value, pb = playback.value;
  const blocked = blockedReason();
  const cal = calibration.value;
  const pct = pb.playing && pb.total_ms ? Math.round((pb.t_ms / pb.total_ms) * 100) : 0;
  // "Finished" and "stopped" are news for a while, then the bar goes back to "ready".
  const [, tick] = useState(0);
  const recent = !!r && Date.now() - r.at < RESULT_SECONDS * 1000;
  useEffect(() => {
    if (!r || !recent) return;
    const id = setTimeout(() => tick((n) => n + 1), RESULT_SECONDS * 1000 - (Date.now() - r.at) + 50);
    return () => clearTimeout(id);
  }, [r?.at, r?.phase]);
  let line: preact.JSX.Element;
  if (r?.phase === "sending") line = <span class="rb-state is-busy"><i class="spinner" />Sending “{r.name}” to the robot…</span>;
  else if (pb.playing) {
    line = (
      <span class="rb-state is-busy">
        <Icon name="robot" size={18} />Playing “{pb.name || "an animation"}” on the robot
        {pb.kind === "builtin" ? <small>built-in move</small> : <span class="rb-meter" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100} aria-label="Playback progress"><i style={{ width: `${pct}%` }} /></span>}
      </span>
    );
  } else if (r?.phase === "failed") line = <span class="rb-state is-bad" role="alert"><Icon name="alert" size={18} /><span><b>“{r.name}” didn't play.</b> {r.message}</span></span>;
  else if (blocked) line = <span class="rb-state is-warn"><Icon name="alert" size={18} />The robot can't play right now: {blocked}</span>;
  else if (!recent) line = <span class="rb-state"><Icon name="check" size={18} />The robot is ready.</span>;
  else if (r?.phase === "done") line = <span class="rb-state is-good" role="status"><Icon name="check" size={18} />“{r.name}” finished on the robot.</span>;
  else if (r?.phase === "stopped") line = <span class="rb-state" role="status"><Icon name="stop" size={18} />“{r.name}” was stopped part way.</span>;
  else line = <span class="rb-state"><Icon name="check" size={18} />The robot is ready.</span>;
  return (
    <div class={`robotbar ${stacked ? "is-stacked" : ""}`} aria-live="polite">
      <button class={`arm-switch ${armed.value ? "is-armed" : ""}`} role="switch" aria-checked={armed.value} onClick={() => setArmed(!armed.value)}
        title={armed.value ? `One click plays on the robot. Turns itself off after ${ARM_MINUTES} minutes.` : "Each play asks for confirmation first."}>
        <Icon name={armed.value ? "unlock" : "lock"} size={18} />
        <span><b>{armed.value ? "One-click play is on" : "Confirm before each play"}</b><small>{armed.value ? "A click moves the robot at once" : "Turn on one-click play"}</small></span>
      </button>
      <div class="rb-line">
        {line}
        {pb.limited && pb.limited.length > 0 && (pb.playing || r?.phase === "done") && (
          <small class="rb-sub">Held inside the calibration limits: {pb.limited.map(words).join(", ")}.</small>
        )}
        {cal && cal.active !== "factory" && <small class="rb-sub">Calibration profile “{cal.name}” is active.</small>}
      </div>
      <button class="btn danger" onClick={stopRobot}><Icon name="stop" size={18} />Stop robot</button>
    </div>
  );
}

/** "Play on robot" for one thing. Asks first unless one-click play is on. */
export function PlayButton({ target, label = "Play on robot", big = false, disabledReason }: { target: PlayTarget; label?: string; big?: boolean; disabledReason?: string | null }) {
  const [confirming, setConfirming] = useState(false);
  const blocked = disabledReason ?? blockedReason();
  const mine = request.value?.key === target.key ? request.value : null;
  const go = () => { setConfirming(false); playOnRobot(target); };
  if (confirming) {
    return (
      <div class="confirm" role="alertdialog" aria-label={`Play ${target.name} on the real robot?`}>
        <span>The real robot will move. Is the space around it clear?</span>
        <button class="btn robot" autoFocus onClick={go}>Move the robot</button>
        <button class="btn quiet" onClick={() => setConfirming(false)}>Cancel</button>
      </div>
    );
  }
  return (
    <span class="play-wrap">
      <button class={`btn robot ${big ? "big" : ""}`} disabled={!!blocked || mine?.phase === "sending"} onClick={() => (armed.value ? go() : setConfirming(true))}>
        <Icon name="robot" />{mine?.phase === "sending" ? "Sending…" : label}
      </button>
      {blocked && <span class="blocked"><Icon name="alert" size={16} />{blocked}</span>}
    </span>
  );
}
