// Server calls, the live event stream, and the handful of signals every screen reads.
import { signal } from "@preact/signals";

export type LinkState = "ok" | "down" | "unknown" | "stub";
export interface Link { state: LinkState; detail: string; latency_ms?: number | null; model?: string }
export interface Status {
  dry_run: boolean;
  links: { robot: Link; camera: Link; llm: Link };
  health: null | {
    battery_v: number | null; battery_pct: number | null; needs_charge: boolean | null;
    hottest: { joint: string; temp_c: number } | null; max_allowed_c: number | null;
    temps_c: Record<string, number | null> | null;
  };
  motion: { ok: boolean; reasons: string[] };
}
export interface SafetyStop { kind: "pain" | "fall"; title: string; detail: string; transcript: string | null; t: number; stop_errors: string[] }
export interface Safety { stop: SafetyStop | null; alerts: { kind: string; detail: string; t: number }[] }
export interface Frame { visible: boolean; x: number; size: number }
export interface Live {
  running: boolean; state?: string | null; plan?: string | null; simulate?: boolean; pace?: number;
  exercise?: { name: string; cue: string; index: number; count: number };
  set?: number; sets?: number; reps?: number; target_reps?: number;
  angle?: number | null; reference?: number; target?: Target;
  said?: string | null; waiting?: string | null; heard?: string | null; matrix?: string;
  frame: Frame; advice: { kind: string; text: string } | null;
  last?: { id: string | null; status: string; flagged: boolean } | null; error?: string | null;
}
export interface Playback { playing: boolean; t_ms: number; total_ms: number; error: string | null; name: string | null; stopped?: boolean }
export interface Tunnel { open: boolean; host: string | null; user: string | null; port: number | null; error: string | null; manual: string }
export interface Keyframe { ms: number; hold_ms?: number; ease?: string; servos: Record<string, number>; head?: Record<string, number> }
export interface Target { top?: number; bottom?: number; tolerance?: number; tempo_s?: number }
export interface Rep {
  n: number; t_start: number; t_end: number; peak_deg: number; trough_deg: number; duration_s: number;
  rom_deg: number; rom_ratio?: number; accuracy_pct?: number; pace_ratio?: number; misses: string[]; quality: "good" | "off";
}
export interface Scores {
  accuracy_pct: number | null; pacing_pct: number | null; consistency_pct: number | null; tempo_match_pct: number | null;
  fatigue: { label: string; rom_change_pct: number | null; duration_change_pct: number | null };
  reps?: number; target_reps?: number; active_s?: number; safety_events?: number;
}
export interface SetLog {
  exercise: string; set: number; reps: number; target_reps: number; deviations: string[]; answer?: string | null;
  trouble?: string; target: Target; started_t?: number; seconds?: number; finished?: boolean;
  trace?: { hz: number; patient: (number | null)[]; reference: number[] };
  reps_detail: Rep[]; scores: Scores; bands: { t0: number; t1: number; quality: "good" | "off" | "lost"; rep?: number }[];
}
export interface SessionEvent { t: number; type: string; set?: number; detail?: string; transcript?: string }
export interface Summary {
  headline: string; highlights: string[]; work_on: string; adjustment: { kind: string; text: string };
  follow_up: boolean; follow_up_reason: string; source: "llm" | "template" | "stub" | "error"; error?: string;
  generated: number | null; model: string | null;
}
export interface Session {
  id: string; plan: string; started: number; ended?: number; status: string; flagged: boolean; synthetic: boolean;
  sets: SetLog[]; interrupted: SetLog[]; events: SessionEvent[]; notes: string[]; scores: Scores;
  summary: Summary; summary_cached: boolean; summary_pending: boolean;
  calories?: { kcal: number; weight_kg: number };
}
export interface SessionRow {
  id: string; plan: string; started: number; status: string; flagged: boolean; synthetic: boolean; scores: Scores;
  exercises: string[]; sets: number; has_detail: boolean; headline: string | null; unreadable?: boolean;
}

export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); }
}

/** Call the dashboard server. A failure throws the server's own words (`detail`). */
export async function api<T = any>(method: string, path: string, body?: unknown): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, {
      method,
      headers: body === undefined ? undefined : { "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError("The dashboard server isn't answering. Is `python -m dashboard` still running?", 0);
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail ?? data);
    throw new ApiError(detail || `HTTP ${res.status}`, res.status);
  }
  return data as T;
}
export const robot = <T = any>(path: string, body: unknown = {}) => api<T>("POST", `/api/robot/${path}`, body);

// ---- shared state ---------------------------------------------------------------------

export const status = signal<Status | null>(null);
export const safety = signal<Safety>({ stop: null, alerts: [] });
export const live = signal<Live>({ running: false, frame: { visible: true, x: 0, size: 0.6 }, advice: null });
export const playback = signal<Playback>({ playing: false, t_ms: 0, total_ms: 0, error: null, name: null });
export const tunnel = signal<Tunnel | null>(null);
export const settings = signal<{ ollama_url: string; model: string; stub_llm: boolean; robot: string } | null>(null);
export const serverUp = signal<boolean | null>(null);
export const sessionsChanged = signal(0);
export const summaryEvent = signal<{ id: string; summary: Summary } | null>(null);

export interface Toast { id: number; kind: "error" | "ok" | "info"; text: string }
export const toasts = signal<Toast[]>([]);
let toastId = 0;
export function toast(kind: Toast["kind"], text: string) {
  const id = ++toastId;
  toasts.value = [...toasts.value.slice(-3), { id, kind, text }];
  setTimeout(() => dismiss(id), kind === "error" ? 9000 : 3500);
}
export const dismiss = (id: number) => { toasts.value = toasts.value.filter((t) => t.id !== id); };

/** Run an action; show the server's refusal (or our own failure) as a toast. */
export async function attempt<T>(fn: () => Promise<T>, done?: string): Promise<T | undefined> {
  try {
    const out = await fn();
    if (done) toast("ok", done);
    return out;
  } catch (e) {
    toast("error", e instanceof Error ? e.message : String(e));
    return undefined;
  }
}

export async function loadStatus() {
  try {
    const s = await api("GET", "/api/status");
    status.value = s; safety.value = s.safety; live.value = s.live; playback.value = s.playback;
    tunnel.value = s.tunnel; settings.value = s.settings; serverUp.value = true;
  } catch { serverUp.value = false; }
}

export function connectEvents() {
  const es = new EventSource("/api/events");
  const on = (kind: string, fn: (d: any) => void) => es.addEventListener(kind, (ev) => fn(JSON.parse((ev as MessageEvent).data)));
  on("status", (d) => { status.value = d; });
  on("safety", (d) => { safety.value = d; });
  on("live", (d) => { live.value = d; });
  on("playback", (d) => { playback.value = d; });
  on("tunnel", (d) => { tunnel.value = d; });
  on("sessions", () => { sessionsChanged.value++; });
  on("summary", (d) => { summaryEvent.value = d; sessionsChanged.value++; });
  es.onopen = () => { if (serverUp.value === false) loadStatus(); serverUp.value = true; };
  es.onerror = () => { serverUp.value = false; };  // EventSource retries by itself
}

export async function stopRobot() {
  try {
    await api("POST", "/api/stop");
    toast("ok", "Stop sent. The robot is frozen where it was.");
  } catch (e) {
    toast("error", e instanceof Error ? e.message : String(e));
  }
}

// ---- small helpers --------------------------------------------------------------------

export const words = (s: string) => s.replace(/_/g, " ");
export const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);
export const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v));
export function when(epoch: number) {
  const d = new Date(epoch * 1000);
  const days = Math.floor((Date.now() - d.getTime()) / 86400000);
  const time = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const date = d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
  return { date, time, ago: days <= 0 ? "today" : days === 1 ? "yesterday" : `${days} days ago` };
}
export function clock(seconds: number) {
  const s = Math.max(0, Math.round(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
