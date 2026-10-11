// The analysis charts for one session. Headline first, then one question per chart. Every
// number is computed by the server (workout/session.py); these components only draw.
// Colour jobs: blue and orange are left and right (or patient and demonstration) wherever
// they appear; green, amber and red are states and always come with an icon or a word.
import { ComponentChildren } from "preact";
import { useState } from "preact/hooks";
import { cap, Heatmap, Insight, JointRom, Overall, Rep, Scores, SetLog, Symmetry, Trend, when, words } from "./api";
import { Icon } from "./icons";

const JOINT: Record<string, string> = { left_shoulder: "Left shoulder", right_shoulder: "Right shoulder", left_elbow: "Left elbow", right_elbow: "Right elbow" };
const TONE_ICON: Record<string, string> = { good: "check", warn: "alert", bad: "alert", info: "info" };
const grade = (v: number | null) => (v == null ? "none" : v >= 80 ? "good" : v >= 60 ? "warn" : "bad");
const GRADE_WORD: Record<string, string> = { good: "On track", warn: "Needs attention", bad: "Well off target", none: "Not measured" };

/** A chart with its title, the unit it is drawn in, and one line on how to read it. */
export function ChartCard({ title, unit, means, children, aside, id }: { title: string; unit?: string; means: string; children: ComponentChildren; aside?: ComponentChildren; id?: string }) {
  return (
    <section class="panel chart-card" id={id}>
      <div class="panel-head">
        <h2>{title}</h2>
        {unit && <span class="chart-unit">{unit}</span>}
        {aside}
      </div>
      {children}
      <p class="means"><b>What this means:</b> {means}</p>
    </section>
  );
}

export function Unmeasured({ what, why }: { what: string; why: string }) {
  return <div class="chart-empty"><Icon name="chart" size={24} /><p><b>{what} wasn't measured.</b> {why}</p></div>;
}

// ---- the headline -----------------------------------------------------------------------

/** The overall score on a goniometer arc: 0 at the left stop, 100 at the right. */
export function ScoreArc({ score, size = 220 }: { score: number | null; size?: number }) {
  const R = 84, CX = 110, CY = 104, A0 = 180, A1 = 360; // degrees, clockwise from 3 o'clock: a half circle
  const pt = (a: number, r: number) => [CX + r * Math.cos((a * Math.PI) / 180), CY + r * Math.sin((a * Math.PI) / 180)];
  const arc = (a: number, b: number) => { const [x0, y0] = pt(a, R), [x1, y1] = pt(b, R); return `M${x0.toFixed(1)} ${y0.toFixed(1)}A${R} ${R} 0 ${b - a > 180 ? 1 : 0} 1 ${x1.toFixed(1)} ${y1.toFixed(1)}`; };
  const at = A0 + ((score ?? 0) / 100) * (A1 - A0);
  const [nx, ny] = pt(at, R);
  return (
    <svg class={`score-arc grade-${grade(score)}`} viewBox="0 0 220 124" width={size} role="img"
      aria-label={score == null ? "No overall score" : `Overall score ${score} out of 100`}>
      <path d={arc(A0, A1)} class="arc-track" />
      {score != null && <path d={arc(A0, Math.max(A0 + 0.5, at))} class="arc-fill" />}
      {Array.from({ length: 11 }, (_, i) => {
        const a = A0 + (i / 10) * (A1 - A0), [x0, y0] = pt(a, R + 6), [x1, y1] = pt(a, R + (i % 5 === 0 ? 14 : 10));
        return <line key={i} x1={x0} y1={y0} x2={x1} y2={y1} class={i % 5 === 0 ? "tick major" : "tick"} />;
      })}
      {score != null && <circle cx={nx} cy={ny} r="6" class="arc-knob" />}
      <text x={pt(A0, R)[0]} y={CY + 18} class="axis" text-anchor="middle">0</text>
      <text x={pt(A1, R)[0]} y={CY + 18} class="axis" text-anchor="middle">100</text>
    </svg>
  );
}

export function Headline({ overall, insights, scores }: { overall: Overall; insights: Insight[]; scores: Scores }) {
  const total = overall.parts.reduce((n, p) => n + p.weight, 0) || 1;
  return (
    <section class="panel headline">
      <div class="headline-score">
        <ScoreArc score={overall.score} />
        <div class="headline-number">
          <b>{overall.score ?? "–"}</b>
          <span>out of 100</span>
          <strong>{overall.label}</strong>
        </div>
      </div>
      <div class="headline-why">
        <h2>In plain words</h2>
        {insights.length === 0 ? <p class="hint">Nothing was measured in this session, so there is no score. Sessions logged before pose tracking only have rep counts.</p> : (
          <ul class="insights">
            {insights.map((i, n) => <li key={n} class={`tone-${i.tone}`}><Icon name={TONE_ICON[i.tone]} size={18} /><span>{i.text}</span></li>)}
          </ul>
        )}
      </div>
      <div class="headline-parts">
        <h2>What the score is made of</h2>
        {overall.parts.length === 0 ? <p class="hint">No parts were measured.</p> : (
          <ul class="parts">
            {overall.parts.map((p) => (
              <li key={p.key} class={`grade-${grade(p.value)}`}>
                <a href={`#chart-${p.key}`} onClick={(e) => { e.preventDefault(); document.getElementById(`chart-${p.key}`)?.scrollIntoView({ behavior: "smooth", block: "start" }); }}>
                  <span class="part-label">{p.label}<small>{Math.round((p.weight / total) * 100)}% of the score</small></span>
                  <span class="part-bar" aria-hidden="true"><i style={{ width: `${p.value}%` }} /></span>
                  <b>{p.value}%</b>
                  <span class="part-word"><Icon name={grade(p.value) === "good" ? "check" : "alert"} size={14} />{GRADE_WORD[grade(p.value)]}</span>
                </a>
              </li>
            ))}
          </ul>
        )}
        <p class="hint">Select a part to jump to its chart. {scores.fatigue.label === "not_enough_reps" ? "" : "Stamina is reported separately and doesn't change the score."}</p>
      </div>
    </section>
  );
}

// ---- range of motion per joint ----------------------------------------------------------

export function RomChart({ rows }: { rows: JointRom[] }) {
  const [hover, setHover] = useState<JointRom | null>(null);
  const hi = Math.max(180, ...rows.map((r) => r.max_deg));
  const pct = (deg: number) => `${Math.max(0, Math.min(100, (deg / hi) * 100))}%`;
  const ticks = [0, 45, 90, 135, 180].filter((t) => t <= hi);
  return (
    <figure class="chart rom">
      <div class="rom-rows" onPointerLeave={() => setHover(null)}>
        {rows.map((r) => (
          <div key={r.joint} class="rom-row" onPointerEnter={() => setHover(r)} tabIndex={0} onFocus={() => setHover(r)}
            aria-label={`${JOINT[r.joint] ?? words(r.joint)}: moved from ${Math.round(r.min_deg)} to ${Math.round(r.max_deg)} degrees, a range of ${Math.round(r.rom_deg)} degrees`}>
            <span class="rom-name"><i class={`swatch side-${r.joint.split("_")[0]}`} />{JOINT[r.joint] ?? cap(words(r.joint))}</span>
            <span class="rom-track">
              {ticks.map((t) => <i key={t} class="rom-grid" style={{ left: pct(t) }} />)}
              <i class={`rom-span side-${r.joint.split("_")[0]}`} style={{ left: pct(r.min_deg), width: `calc(${pct(r.max_deg)} - ${pct(r.min_deg)})` }} />
            </span>
            <b class="rom-value">{Math.round(r.rom_deg)}°{r.pct != null && <small>{r.pct}% of target</small>}</b>
          </div>
        ))}
        <div class="rom-row rom-axis" aria-hidden="true">
          <span /><span class="rom-track">{ticks.map((t) => <small key={t} style={{ left: pct(t) }}>{t}°</small>)}</span><span />
        </div>
      </div>
      <p class="chart-readout">
        {hover ? <><b>{JOINT[hover.joint] ?? words(hover.joint)}</b> moved between {Math.round(hover.min_deg)}° and {Math.round(hover.max_deg)}°, a range of {Math.round(hover.rom_deg)}°
          {hover.target_rom_deg != null && <> against a target range of {Math.round(hover.target_rom_deg)}°</>}.</>
          : "Point at a bar to read it. Each bar runs from the lowest to the highest angle that joint reached. Elbows read near 180° when the arm is straight."}
      </p>
    </figure>
  );
}

// ---- reps: count and quality ------------------------------------------------------------

const MISS_LABEL: Record<string, string> = { short: "Stopped short of the target", over: "Went past the target", not_lowered: "Didn't lower fully", fast: "Rushed", slow: "Too slow" };

export function RepQuality({ sets }: { sets: SetLog[] }) {
  const reps = sets.flatMap((s) => s.reps_detail);
  const good = reps.filter((r) => r.quality === "good").length;
  const done = sets.reduce((n, s) => n + (s.reps ?? 0), 0), target = sets.reduce((n, s) => n + (s.target_reps ?? 0), 0);
  const misses: Record<string, number> = {};
  reps.forEach((r) => r.misses.forEach((m) => { misses[m] = (misses[m] ?? 0) + 1; }));
  const worst = Object.entries(misses).sort((a, b) => b[1] - a[1]);
  const max = Math.max(1, ...worst.map(([, n]) => n));
  return (
    <div class="repq">
      <dl class="repq-counts">
        <div><dt>Reps counted</dt><dd><b>{done}</b> of {target}</dd></div>
        <div><dt>On target and on pace</dt><dd class="tone-good"><Icon name="check" size={18} /><b>{good}</b> of {reps.length}</dd></div>
        <div><dt>Drifted</dt><dd class={reps.length - good ? "tone-warn" : ""}>{reps.length - good > 0 && <Icon name="alert" size={18} />}<b>{reps.length - good}</b> of {reps.length}</dd></div>
      </dl>
      {reps.length > 0 && (
        <div class="repq-bar" role="img" aria-label={`${good} of ${reps.length} reps on target`}>
          <i class="is-good" style={{ flexGrow: good }} />{reps.length - good > 0 && <i class="is-off" style={{ flexGrow: reps.length - good }} />}
        </div>
      )}
      {worst.length > 0 ? (
        <ul class="miss-list" aria-label="Why reps drifted">
          {worst.map(([m, n]) => (
            <li key={m}><span>{MISS_LABEL[m] ?? words(m)}</span><span class="miss-bar"><i style={{ width: `${(n / max) * 100}%` }} /></span><b>{n} rep{n === 1 ? "" : "s"}</b></li>
          ))}
        </ul>
      ) : reps.length > 0 && <p class="hint">Every counted rep was on target and on pace.</p>}
    </div>
  );
}

// ---- tempo and smoothness ---------------------------------------------------------------

function Bars<T>({ items, value, max, cls, label, tip, target, targetLabel, unit, split }: {
  items: T[]; value: (t: T) => number | null | undefined; max: number; cls?: (t: T) => string; label: (t: T) => string;
  tip: (t: T) => ComponentChildren; target?: number | null; targetLabel?: string; unit: string; split?: (t: T) => number | null | undefined; rest?: string;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const H = 150, top = Math.max(max, target ?? 0) * 1.12 || 1;
  const y = (v: number) => (1 - v / top) * H;
  const ticks = [0, top / 2 / 1.12 * 1, top / 1.12];
  return (
    <figure class="chart bars">
      <div class="bars-plot" style={{ height: `${H + 26}px` }} onPointerLeave={() => setHover(null)}>
        <div class="bars-axis" aria-hidden="true">{ticks.map((t, i) => <small key={i} style={{ top: `${y(t)}px` }}>{unit === "%" ? Math.round(t) : t.toFixed(1)}{unit}</small>)}</div>
        <div class="bars-area">
          {ticks.map((t, i) => <i key={i} class="bars-grid" style={{ top: `${y(t)}px` }} />)}
          {target != null && <i class="bars-target" style={{ top: `${y(target)}px` }}><small>{targetLabel}</small></i>}
          <div class="bars-cols">
            {items.map((it, i) => {
              const v = value(it), s = split?.(it);
              return (
                <button key={i} class={`bar-col ${hover === i ? "is-hover" : ""}`} onPointerEnter={() => setHover(i)} onFocus={() => setHover(i)}
                  aria-label={`${label(it)}: ${v == null ? "not measured" : `${unit === "%" ? Math.round(v) : v.toFixed(1)}${unit}`}`}>
                  <span class="bar-space" style={{ height: `${H}px` }}>
                    {v != null && (
                      <span class={`bar ${cls?.(it) ?? ""}`} style={{ height: `${Math.max(2, (v / top) * H)}px` }}>
                        {s != null && v > 0 && <i class="bar-split" style={{ height: `${Math.min(100, (s / v) * 100)}%` }} />}
                      </span>
                    )}
                  </span>
                  <small>{label(it)}</small>
                </button>
              );
            })}
          </div>
        </div>
      </div>
      <p class="chart-readout">{hover != null && items[hover] ? tip(items[hover]) : "Point at a bar to read that rep."}</p>
    </figure>
  );
}

export function TempoChart({ set }: { set: SetLog }) {
  const reps = set.reps_detail, t = set.tempo;
  // Reps counted from a trace are timed between their 20 % crossings, about 70 % of a full cycle.
  const expected = t?.target_s != null && reps[0]?.pace_ratio ? reps[0].duration_s / reps[0].pace_ratio : t?.target_s ?? null;
  return (
    <>
      <div class="chart-legend">
        <span class="legend-item"><i class="swatch bar-up" />Lifting</span>
        <span class="legend-item"><i class="swatch bar-down" />Lowering</span>
        {expected != null && <span class="legend-item"><i class="swatch line-target" />Demonstrated pace</span>}
      </div>
      <Bars items={reps} unit=" s" max={Math.max(...reps.map((r) => r.duration_s))} value={(r) => r.duration_s} split={(r) => r.down_s}
        label={(r) => `${r.n}`} target={expected} targetLabel="demo" cls={(r) => (r.misses.includes("fast") || r.misses.includes("slow") ? "is-off" : "")}
        tip={(r: Rep) => <><b>Rep {r.n}</b> took {r.duration_s.toFixed(1)} s{r.up_s != null && <>: {r.up_s.toFixed(1)} s lifting, {r.down_s!.toFixed(1)} s lowering</>}
          {r.pace_ratio != null && <>. That is {Math.abs(Math.round((r.pace_ratio - 1) * 100))}% {r.pace_ratio < 1 ? "quicker" : "slower"} than the demonstration{r.misses.includes("fast") ? " (rushed)" : r.misses.includes("slow") ? " (too slow)" : ""}</>}.</>} />
      {t && <p class="chart-facts">Average {t.mean_s.toFixed(1)} s a rep{t.up_s != null && <> ({t.up_s.toFixed(1)} s up, {t.down_s!.toFixed(1)} s down)</>}. Rep times varied by {t.spread_pct}%.</p>}
    </>
  );
}

export function SmoothnessChart({ set }: { set: SetLog }) {
  const reps = set.reps_detail.filter((r) => r.smoothness_pct != null);
  if (!reps.length) return <Unmeasured what="Smoothness" why="It needs the joint-angle trace, which this set doesn't have." />;
  return (
    <Bars items={reps} unit="%" max={100} value={(r) => r.smoothness_pct} label={(r) => `${r.n}`} target={50} targetLabel="steady"
      cls={(r) => ((r.smoothness_pct ?? 0) < 50 ? "is-off" : "")}
      tip={(r: Rep) => <><b>Rep {r.n}</b> smoothness {r.smoothness_pct}%. {(r.smoothness_pct ?? 0) >= 50 ? "One clean rise and return." : "The movement stopped and started, or shook, on the way."}</>} />
  );
}

// ---- symmetry ---------------------------------------------------------------------------

export function SymmetryChart({ set, symmetry }: { set: SetLog; symmetry: Symmetry }) {
  const [hover, setHover] = useState<Rep | null>(null);
  const reps = set.reps_detail.filter((r) => r.left_rom_deg != null);
  const max = Math.max(1, ...reps.flatMap((r) => [r.left_rom_deg!, r.right_rom_deg!]));
  return (
    <figure class="chart butterfly">
      <div class="chart-legend">
        <span class="legend-item"><i class="swatch side-left" />Left arm</span>
        <span class="legend-item"><i class="swatch side-right" />Right arm</span>
      </div>
      <div class="bf-rows" onPointerLeave={() => setHover(null)}>
        <div class="bf-row bf-head" aria-hidden="true"><span>Left range</span><span>Rep</span><span>Right range</span></div>
        {reps.map((r) => (
          <div key={r.n} class="bf-row" tabIndex={0} onPointerEnter={() => setHover(r)} onFocus={() => setHover(r)}
            aria-label={`Rep ${r.n}: left ${Math.round(r.left_rom_deg!)} degrees, right ${Math.round(r.right_rom_deg!)} degrees`}>
            <span class="bf-left"><small>{Math.round(r.left_rom_deg!)}°</small><i class="side-left" style={{ width: `${(r.left_rom_deg! / max) * 100}%` }} /></span>
            <span class="bf-n">{r.n}</span>
            <span class="bf-right"><i class="side-right" style={{ width: `${(r.right_rom_deg! / max) * 100}%` }} /><small>{Math.round(r.right_rom_deg!)}°</small></span>
          </div>
        ))}
      </div>
      <p class="chart-readout">
        {hover ? <><b>Rep {hover.n}</b> left arm {Math.round(hover.left_rom_deg!)}°, right arm {Math.round(hover.right_rom_deg!)}°: {hover.symmetry_pct}% balanced.</>
          : <>Across this set the left arm averaged {Math.round(symmetry.left_rom_deg)}° and the right {Math.round(symmetry.right_rom_deg)}°: {symmetry.pct}% balanced
            {symmetry.favours === "even" ? ", close to even." : `, with the ${symmetry.favours} arm doing more.`}</>}
      </p>
    </figure>
  );
}

// ---- deviation heatmap ------------------------------------------------------------------

const PHASES = ["Start of lift", "", "", "Top", "", "", "", "Back down"];

export function HeatmapChart({ heat, tolerance = 15 }: { heat: Heatmap; tolerance?: number }) {
  const [hover, setHover] = useState<{ rep: number; bin: number; v: number | null } | null>(null);
  const cap_ = Math.max(tolerance * 2, 20);
  const cell = (v: number | null) => {
    if (v == null) return { cls: "heat-none", style: {} };
    const k = Math.min(1, Math.abs(v) / cap_);
    return { cls: v >= 0 ? "heat-over" : "heat-under", style: { "--k": k.toFixed(2) } as any };
  };
  return (
    <figure class="chart heat">
      <div class="heat-scroll">
        <table class="heat-table" onPointerLeave={() => setHover(null)}>
          <caption class="sr-only">Degrees above or below the demonstration, by rep and by phase of the rep</caption>
          <thead><tr><th scope="col">Rep</th>{Array.from({ length: heat.bins }, (_, i) => <th key={i} scope="col">{PHASES[i] ?? ""}<span class="sr-only">phase {i + 1}</span></th>)}</tr></thead>
          <tbody>
            {heat.rows.map((row) => (
              <tr key={row.rep}>
                <th scope="row">{row.rep}</th>
                {row.cells.map((v, i) => {
                  const c = cell(v);
                  return (
                    <td key={i} class={c.cls} style={c.style} tabIndex={0} onPointerEnter={() => setHover({ rep: row.rep, bin: i, v })} onFocus={() => setHover({ rep: row.rep, bin: i, v })}>
                      <span>{v == null ? "" : `${v > 0 ? "+" : ""}${Math.round(v)}`}</span>
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div class="heat-key" aria-hidden="true">
        <small>{-Math.round(cap_)}° below</small><i class="heat-ramp" /><small>{Math.round(cap_)}° above</small>
        <span class="legend-item"><i class="swatch heat-none" />Not in view</span>
      </div>
      <p class="chart-readout">
        {hover ? hover.v == null ? <><b>Rep {hover.rep}</b>, part {hover.bin + 1} of {heat.bins}: the patient wasn't in view.</>
          : <><b>Rep {hover.rep}</b>, part {hover.bin + 1} of {heat.bins}: {Math.abs(Math.round(hover.v))}° {hover.v >= 0 ? "above" : "below"} the demonstration{Math.abs(hover.v) <= tolerance ? ", inside the leeway" : ""}.</>
          : `Each row is a rep, read left to right from the start of the lift to the return. Numbers are degrees above (+) or below (−) the demonstration; within ${Math.round(tolerance)}° counts as on target.`}
      </p>
    </figure>
  );
}

// ---- session over session ---------------------------------------------------------------

const TREND_KEYS: [keyof Trend, string][] = [["overall", "Overall score"], ["accuracy_pct", "Range reached"], ["pacing_pct", "Pacing"], ["smoothness_pct", "Smoothness"], ["symmetry_pct", "Left and right balance"]];

export function TrendChart({ trends, current }: { trends: Trend[]; current?: string }) {
  const [key, setKey] = useState<keyof Trend>("overall");
  const [hover, setHover] = useState<number | null>(null);
  const pts = trends.map((t, i) => ({ t, i, v: t[key] as number | null })).filter((p) => p.v != null) as { t: Trend; i: number; v: number }[];
  const W = 720, H = 220, L = 40, R = 16, T = 16, B = 34;
  const x = (i: number) => (trends.length <= 1 ? (L + W - R) / 2 : L + (i / (trends.length - 1)) * (W - L - R));
  const y = (v: number) => T + (1 - v / 100) * (H - T - B);
  const label = TREND_KEYS.find(([k]) => k === key)![1];
  const hv = hover != null ? pts.find((p) => p.i === hover) : undefined;
  const first = pts[0], last = pts[pts.length - 1];
  const change = first && last && first !== last ? last.v - first.v : null;
  const every = Math.ceil(trends.length / 8);
  return (
    <figure class="chart trend">
      <div class="chips" role="group" aria-label="Measure">
        {TREND_KEYS.map(([k, name]) => <button key={k} class="chip" aria-pressed={key === k} onClick={() => setKey(k)}>{name}</button>)}
      </div>
      {pts.length < 2 ? <div class="chart-empty"><Icon name="chart" size={24} /><p><b>Not enough sessions yet.</b> A trend needs at least two scored sessions. This chart fills in as sessions are saved.</p></div> : (
        <>
          <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label={`${label} over ${pts.length} sessions, from ${first.v} to ${last.v}`}
            onPointerLeave={() => setHover(null)}
            onPointerMove={(e) => {
              const r = (e.currentTarget as SVGElement).getBoundingClientRect();
              const px = ((e.clientX - r.left) / r.width) * W;
              setHover(pts.reduce((best, p) => (Math.abs(x(p.i) - px) < Math.abs(x(best.i) - px) ? p : best), pts[0]).i);
            }}>
            {[0, 25, 50, 75, 100].map((v) => <g key={v}><line x1={L} x2={W - R} y1={y(v)} y2={y(v)} class="grid" /><text x={L - 8} y={y(v) + 4} class="axis" text-anchor="end">{v}</text></g>)}
            {trends.map((t, i) => i % every === 0 && <text key={t.id} x={x(i)} y={H - 10} class="axis" text-anchor="middle">{when(t.started).date.replace(/^\w+, /, "")}</text>)}
            <path d={pts.map((p, n) => `${n ? "L" : "M"}${x(p.i).toFixed(1)} ${y(p.v).toFixed(1)}`).join("")} class="trend-line" />
            {hv && <line x1={x(hv.i)} x2={x(hv.i)} y1={T} y2={H - B} class="crosshair" />}
            {pts.map((p) => (
              <g key={p.t.id}>
                <circle cx={x(p.i)} cy={y(p.v)} r={p.t.id === current ? 7 : 4.5} class={`trend-dot ${p.t.id === current ? "is-current" : ""} ${p.t.status === "stopped_pain" ? "is-stopped" : ""}`} />
                {p.t.status === "stopped_pain" && <path d={`M${x(p.i) - 4} ${y(p.v) - 15}l4 -8 4 8z`} class="trend-flag" />}
              </g>
            ))}
            {last && <text x={Math.min(W - R - 2, x(last.i))} y={y(last.v) - 12} class="trend-label" text-anchor="end">{last.v}</text>}
          </svg>
          <div class="chart-legend">
            {current && <span class="legend-item"><i class="swatch dot-current" />This session</span>}
            {pts.some((p) => p.t.status === "stopped_pain") && <span class="legend-item"><i class="swatch tri-flag" />Stopped for pain</span>}
          </div>
          <p class="chart-readout">
            {hv ? <><b>{when(hv.t.started).date}</b> {label.toLowerCase()} {hv.v}{key === "overall" ? " out of 100" : "%"}, {hv.t.reps} of {hv.t.target_reps} reps{hv.t.status === "stopped_pain" ? ", stopped for pain" : ""}. <a href={`#/sessions/${hv.t.id}`}>Open it</a></>
              : change == null ? "Point at a dot to read a session."
              : <>{label} went from {first.v} to {last.v} over {pts.length} sessions: {change > 2 ? `up ${change} points.` : change < -2 ? `down ${Math.abs(change)} points.` : "about level."}</>}
          </p>
        </>
      )}
    </figure>
  );
}
