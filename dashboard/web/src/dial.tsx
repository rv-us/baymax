// A joint as a goniometer: the arc is the servo's real range drawn to scale, the needle is
// the joint angle measured from the rest (stand) pose, which points straight down.
import { useRef } from "preact/hooks";
import { clamp } from "./api";

export const PULSE_PER_DEG = 1000 / 240;
const R = 44, CX = 60, CY = 60;

const polar = (deg: number, r: number) => {
  const a = ((90 + deg) * Math.PI) / 180; // 0 degrees = rest = pointing down
  return [CX + r * Math.cos(a), CY + r * Math.sin(a)] as const;
};

function arc(from: number, to: number, r: number) {
  const [x0, y0] = polar(from, r), [x1, y1] = polar(to, r);
  return `M${x0.toFixed(1)} ${y0.toFixed(1)}A${r} ${r} 0 ${to - from > 180 ? 1 : 0} 1 ${x1.toFixed(1)} ${y1.toFixed(1)}`;
}

export interface DialProps {
  label: string; value: number; stand: number; limits: [number, number];
  onChange: (pulse: number) => void; disabled?: boolean; ghost?: number | null;
}

export function Dial({ label, value, stand, limits, onChange, disabled, ghost }: DialProps) {
  const svg = useRef<SVGSVGElement>(null);
  const toDeg = (p: number) => (p - stand) / PULSE_PER_DEG;
  const lo = toDeg(limits[0]), hi = toDeg(limits[1]);
  const deg = toDeg(value);

  const fromPointer = (e: PointerEvent) => {
    const box = svg.current!.getBoundingClientRect();
    const x = ((e.clientX - box.left) / box.width) * 120 - CX;
    const y = ((e.clientY - box.top) / box.height) * 120 - CY;
    let a = (Math.atan2(y, x) * 180) / Math.PI - 90;      // degrees from straight down
    while (a <= -180) a += 360;
    while (a > 180) a -= 360;
    if (a < lo || a > hi) {                                // in the gap: snap to the nearer end
      const wrap = (d: number) => Math.min(Math.abs(d), 360 - Math.abs(d));
      a = wrap(a - lo) < wrap(a - hi) ? lo : hi;
    }
    onChange(clamp(Math.round(stand + a * PULSE_PER_DEG), limits[0], limits[1]));
  };
  const down = (e: PointerEvent) => {
    if (disabled) return;
    (e.currentTarget as Element).setPointerCapture(e.pointerId);
    fromPointer(e);
  };
  const move = (e: PointerEvent) => { if (e.buttons && !disabled) fromPointer(e); };
  const nudge = (by: number) => onChange(clamp(value + by, limits[0], limits[1]));
  const key = (e: KeyboardEvent) => {
    const step = e.shiftKey ? 42 : 8; // about 10 degrees, or 2
    if (e.key === "ArrowRight" || e.key === "ArrowUp") { nudge(step); e.preventDefault(); }
    if (e.key === "ArrowLeft" || e.key === "ArrowDown") { nudge(-step); e.preventDefault(); }
    if (e.key === "Home") { onChange(stand); e.preventDefault(); }
  };

  const ticks = [];
  for (let d = Math.ceil(lo / 15) * 15; d <= hi; d += 15) {
    const major = d % 45 === 0;
    const [x0, y0] = polar(d, R + 2), [x1, y1] = polar(d, R + (major ? 9 : 6));
    ticks.push(<line key={d} x1={x0} y1={y0} x2={x1} y2={y1} class={major ? "tick major" : "tick"} />);
  }
  const [nx, ny] = polar(deg, R - 4);
  const [gx, gy] = ghost != null ? polar(toDeg(ghost), R - 10) : [0, 0];
  const shown = Math.round(deg);

  return (
    <div class={`dial ${disabled ? "is-disabled" : ""}`}>
      <svg ref={svg} viewBox="0 0 120 120" role="slider" tabindex={disabled ? -1 : 0}
        aria-label={`${label}, degrees from rest`} aria-valuemin={Math.round(lo)} aria-valuemax={Math.round(hi)}
        aria-valuenow={shown} aria-valuetext={`${shown} degrees`} aria-disabled={disabled}
        onPointerDown={down} onPointerMove={move} onKeyDown={key}>
        <path d={arc(lo, hi, R)} class="dial-range" />
        <path d={deg >= 0 ? arc(0, Math.max(0.01, deg), R) : arc(deg, 0, R)} class="dial-sweep" />
        {ticks}
        <line x1={CX} y1={CY + R - 7} x2={CX} y2={CY + R + 9} class="dial-rest" />
        {ghost != null && <line x1={CX} y1={CY} x2={gx} y2={gy} class="dial-ghost" />}
        <line x1={CX} y1={CY} x2={nx} y2={ny} class="dial-needle" />
        <circle cx={nx} cy={ny} r="6.5" class="dial-grip" />
        <circle cx={CX} cy={CY} r="3" class="dial-hub" />
      </svg>
      <div class="dial-read">
        <button class="nudge" aria-label={`${label}: 2 degrees less`} disabled={disabled} onClick={() => nudge(-8)}>−</button>
        <output><b>{shown > 0 ? "+" : ""}{shown}°</b><small>{value}</small></output>
        <button class="nudge" aria-label={`${label}: 2 degrees more`} disabled={disabled} onClick={() => nudge(8)}>+</button>
      </div>
      <div class="dial-label">{label}</div>
    </div>
  );
}
