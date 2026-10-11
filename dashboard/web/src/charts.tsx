// Session charts, hand-drawn in SVG. Every number shown here was computed by the server
// (workout/session.py); these components only place marks.
import { useEffect, useRef, useState } from "preact/hooks";
import { clock, Rep, SessionEvent, SetLog, words } from "./api";
import { Icon } from "./icons";

const MISS: Record<string, string> = {
  short: "stopped short", over: "went past the target", not_lowered: "didn't lower fully", fast: "rushed", slow: "slow",
};
export const missText = (r: Rep) => (r.misses.length ? r.misses.map((m) => MISS[m] ?? m).join(", ") : "on target and on pace");

/** Shared pattern fills: "off" is hatched and "lost" is dotted, so the shading reads without colour. */
export function ChartDefs() {
  return (
    <svg width="0" height="0" class="sr-only" aria-hidden="true">
      <defs>
        <pattern id="hatch-off" width="7" height="7" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
          <rect width="7" height="7" class="fill-off" /><line x1="0" y1="0" x2="0" y2="7" class="hatch-line" />
        </pattern>
        <pattern id="dots-lost" width="6" height="6" patternUnits="userSpaceOnUse">
          <rect width="6" height="6" class="fill-lost" /><circle cx="3" cy="3" r="1" class="dot-lost" />
        </pattern>
      </defs>
    </svg>
  );
}

/** Patient against the demonstrated motion for one set, with each rep's stretch shaded. */
export function OverlayChart({ set, cursor }: { set: SetLog; cursor?: number | null }) {
  const trace = set.trace!;
  const box = useRef<HTMLDivElement>(null);
  const [hover, setHover] = useState<number | null>(null);
  const n = trace.patient.length, hz = trace.hz, dur = n / hz;
  const [room, setRoom] = useState(900);
  useEffect(() => {
    const ro = new ResizeObserver((e) => setRoom(Math.floor(e[0].contentRect.width)));
    ro.observe(box.current!);
    return () => ro.disconnect();
  }, []);
  // Fill the panel; a long set gets at least 14 px a second and scrolls sideways instead of squashing.
  const W = Math.max(room, 300, Math.round(dur * 14)), H = 240, L = 44, R = 12, T = 14, B = 30;
  const known = trace.patient.filter((v): v is number => v != null);
  const top = set.target.top ?? Math.max(...known), bottom = set.target.bottom ?? Math.min(...known);
  const lo = Math.min(bottom, ...known) - 8, hi = Math.max(top, ...known) + 10;
  const x = (t: number) => L + (t / dur) * (W - L - R);
  const y = (v: number) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
  const path = (vals: (number | null)[]) => {
    let d = "", pen = false;
    vals.forEach((v, i) => { if (v == null) { pen = false; return; } d += `${pen ? "L" : "M"}${x(i / hz).toFixed(1)} ${y(v).toFixed(1)}`; pen = true; });
    return d;
  };
  const yTicks = [bottom, (top + bottom) / 2, top];
  const step = dur > 60 ? 10 : 5;
  const xTicks = Array.from({ length: Math.floor(dur / step) + 1 }, (_, i) => i * step);
  const onMove = (e: PointerEvent) => {
    const r = (e.currentTarget as SVGElement).getBoundingClientRect();
    const t = ((((e.clientX - r.left) / r.width) * W - L) / (W - L - R)) * dur;
    setHover(t >= 0 && t <= dur ? Math.round(t * hz) : null);
  };
  const hv = hover != null && hover < n ? { t: hover / hz, p: trace.patient[hover], r: trace.reference[hover] } : null;
  const hrep = hv ? set.reps_detail.find((r) => hv.t >= r.t_start && hv.t <= r.t_end) : undefined;

  return (
    <figure class="chart overlay">
      <figcaption class="chart-legend">
        <span class="legend-item"><i class="swatch line-patient" />Patient</span>
        <span class="legend-item"><i class="swatch line-ref" />Robot's demonstration</span>
        <span class="legend-item"><i class="swatch band-good" />Rep on target</span>
        <span class="legend-item"><i class="swatch band-off" />Rep drifted</span>
        {set.bands.some((b) => b.quality === "lost") && <span class="legend-item"><i class="swatch band-lost" />Not in view</span>}
      </figcaption>
      <div class="chart-scroll" ref={box}>
        <svg viewBox={`0 0 ${W} ${H}`} style={{ width: `${W}px` }} role="img"
          aria-label={`${words(set.exercise)} set ${set.set}: ${set.reps_detail.filter((r) => r.quality === "good").length} of ${set.reps_detail.length} reps on target`}
          onPointerMove={onMove} onPointerLeave={() => setHover(null)}>
          {set.bands.map((b, i) => (
            <rect key={i} x={x(b.t0)} width={Math.max(1, x(b.t1) - x(b.t0))} y={T} height={H - T - B}
              class={`band band-${b.quality}`} fill={b.quality === "off" ? "url(#hatch-off)" : b.quality === "lost" ? "url(#dots-lost)" : undefined} />
          ))}
          {yTicks.map((v) => (
            <g key={v}><line x1={L} x2={W - R} y1={y(v)} y2={y(v)} class="grid" /><text x={L - 8} y={y(v) + 4} class="axis" text-anchor="end">{Math.round(v)}°</text></g>
          ))}
          <line x1={L} x2={W - R} y1={y(top)} y2={y(top)} class="chart-target" />
          <text x={L + 4} y={y(top) - 5} class="axis" text-anchor="start">target</text>
          {xTicks.map((t) => <text key={t} x={x(t)} y={H - 10} class="axis" text-anchor="middle">{clock(t)}</text>)}
          <path d={path(trace.reference)} class="chart-ref" />
          <path d={path(trace.patient)} class="chart-patient" />
          {set.reps_detail.map((r) => (
            <text key={r.n} x={x((r.t_start + r.t_end) / 2)} y={T + 12} class="rep-no" text-anchor="middle">{r.n}</text>
          ))}
          {cursor != null && cursor <= dur && <line x1={x(cursor)} x2={x(cursor)} y1={T} y2={H - B} class="playhead" />}
          {hv && <line x1={x(hv.t)} x2={x(hv.t)} y1={T} y2={H - B} class="crosshair" />}
          {hv && hv.p != null && <circle cx={x(hv.t)} cy={y(hv.p)} r="4.5" class="hover-dot" />}
        </svg>
      </div>
      <p class="chart-readout" aria-live="off">
        {hv ? (
          <>
            <b>{clock(hv.t)}</b> Patient {hv.p != null ? `${Math.round(hv.p)}°` : "not in view"}, demonstration {Math.round(hv.r)}°
            {hrep && <>. Rep {hrep.n}: {missText(hrep)}</>}
          </>
        ) : "Point at the chart to read a moment. Shaded blocks are reps; the numbers along the top count them."}
      </p>
    </figure>
  );
}

/** One cell per rep. Height = range reached against the target; glyph and pattern = on or off. */
export function RepStrip({ set, onPick }: { set: SetLog; onPick?: (r: Rep) => void }) {
  const [open, setOpen] = useState<number | null>(null);
  if (!set.reps_detail.length) return <p class="hint">No rep detail was recorded for this set.</p>;
  const picked = set.reps_detail.find((r) => r.n === open);
  return (
    <div class="repstrip">
      <div class="repstrip-cells">
        {set.reps_detail.map((r) => (
          <button key={r.n} class={`rep rep-${r.quality} ${open === r.n ? "is-open" : ""}`} aria-pressed={open === r.n}
            aria-label={`Rep ${r.n}: ${r.accuracy_pct ?? "?"}% of target range, ${missText(r)}`}
            onClick={() => { setOpen(open === r.n ? null : r.n); onPick?.(r); }}>
            <span class="rep-bar"><i style={{ height: `${Math.min(100, r.accuracy_pct ?? 0)}%` }} /></span>
            <Icon name={r.quality === "good" ? "check" : "alert"} size={14} />
            <small>{r.n}</small>
          </button>
        ))}
      </div>
      {picked && (
        <p class="rep-detail" role="status">
          <b>Rep {picked.n}</b> reached {Math.round(picked.peak_deg)}° ({picked.accuracy_pct}% of the target range) in {picked.duration_s.toFixed(1)} s
          {picked.pace_ratio != null && <> ({picked.pace_ratio < 1 ? "quicker" : "slower"} than the demonstration by {Math.abs(Math.round((picked.pace_ratio - 1) * 100))}%)</>}.
          {" "}{cap(missText(picked))}.
        </p>
      )}
    </div>
  );
}
const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

/** A percentage as a ruled bar with its number: used for per-set accuracy and pacing. */
export function Meter({ value, label }: { value: number | null; label: string }) {
  return (
    <div class="meter" role="img" aria-label={`${label}: ${value == null ? "not measured" : value + "%"}`}>
      <span class="meter-track"><i style={{ width: `${value ?? 0}%` }} /></span>
      <b>{value == null ? "n/a" : `${value}%`}</b>
    </div>
  );
}

const EVENT: Record<string, { label: string; icon: string; safety?: boolean }> = {
  left_frame: { label: "Left the camera view", icon: "eye" },
  returned: { label: "Back in view", icon: "eye" },
  prompt_closer: { label: "Asked to come closer", icon: "speak" },
  prompt_back: { label: "Asked to step back", icon: "speak" },
  side_step: { label: "Robot side-stepped", icon: "walk" },
  pain_word: { label: "Pain word heard", icon: "alert", safety: true },
  fall: { label: "Fall suspected", icon: "alert", safety: true },
  safety_stop: { label: "Safety stop", icon: "stop", safety: true },
  operator_stop: { label: "Stopped from the dashboard", icon: "stop", safety: true },
  no_response: { label: "No answer from the patient", icon: "patient" },
  pause: { label: "Paused", icon: "pause" },
  resume: { label: "Resumed", icon: "play" },
};

/** The whole session on one line: when each set ran, and every event on top. */
export function EventTimeline({ sets, events, duration }: { sets: SetLog[]; events: SessionEvent[]; duration: number }) {
  const dur = Math.max(duration, 1);
  const pct = (t: number) => `${Math.min(100, Math.max(0, (t / dur) * 100))}%`;
  return (
    <div class="timeline">
      <div class="timeline-track" role="img" aria-label={`Session timeline, ${clock(dur)} long, ${events.length} events`}>
        {sets.filter((s) => s.started_t != null).map((s, i) => (
          <span key={i} class={`timeline-set ${s.finished === false ? "is-cut" : ""}`} title={`${s.exercise}, set ${s.set}`}
            style={{ left: pct(s.started_t!), width: pct(s.seconds ?? 0) }}><small>{s.exercise.split(" ")[0]} {s.set}</small></span>
        ))}
        {events.map((e, i) => (
          <i key={i} class={`timeline-mark ${EVENT[e.type]?.safety ? "is-safety" : ""}`} style={{ left: pct(e.t) }} />
        ))}
      </div>
      <div class="timeline-ends"><span>0:00</span><span>{clock(dur)}</span></div>
      {events.length === 0 ? <p class="hint">No safety events, prompts or pauses in this session.</p> : (
        <ol class="event-list">
          {events.map((e, i) => {
            const meta = EVENT[e.type] ?? { label: words(e.type), icon: "flag" };
            return (
              <li key={i} class={meta.safety ? "is-safety" : ""}>
                <time>{clock(e.t)}</time><Icon name={meta.icon} size={18} />
                <span><b>{meta.label}</b>{e.transcript && <> “{e.transcript}”</>}{e.set != null && <small> during set {e.set}</small>}</span>
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}
