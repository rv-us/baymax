// Calibrate: tune the digital twin against the real robot. Live per-servo readout, limits,
// offsets and speed caps per servo, saved as profiles. The active profile is enforced by
// the server on every command, so nothing played from any screen can leave its range.
import { useEffect, useRef, useState } from "preact/hooks";
import { api, attempt, calibration, cap, toast, words } from "./api";
import { CameraPanel } from "./camera";
import { Icon } from "./icons";
import { ChannelTwin } from "./motion";
import { blockedReason } from "./play";

interface Cal { min: number; max: number; offset: number; max_speed: number }
interface Row extends Cal {
  id: number; joint: string; group: "arm" | "leg" | "hand"; commanded: number | null; actual: number | null; raw: number | null;
  error: number | null; temp_c: number | null; vin_v: number | null; torque_on: boolean | null; note?: string;
  stand: number; hard_limits: [number, number];
}
interface Profile { id: string; name: string; notes: string; allow_action_groups: boolean; readonly?: boolean; unreadable?: boolean; servos: Record<string, Cal> }
interface Bounds { max_offset: number; min_speed: number; max_speed: number }

const GROUPS: [Row["group"], string, string][] = [
  ["arm", "Arms", "The joints animations move. These can be jogged from here."],
  ["leg", "Legs", "Read only here: moving one leg joint at a time makes the robot fall. Limits still apply to anything that commands them."],
  ["hand", "Hands", "The hand servos accept commands but never answer reads, so there is no position or temperature."],
];
const ERR_WARN = 40; // pulses, about 10 degrees

function Num({ value, min, max, label, disabled, onChange }: { value: number; min: number; max: number; label: string; disabled?: boolean; onChange: (v: number) => void }) {
  return <input type="number" inputMode="numeric" value={value} min={min} max={max} aria-label={label} disabled={disabled}
    onChange={(e) => { const v = Math.round(+e.currentTarget.value); onChange(Number.isFinite(v) ? Math.max(min, Math.min(max, v)) : value); }} />;
}

export function Calibrate() {
  const [profiles, setProfiles] = useState<Profile[] | null>(null);
  const [bounds, setBounds] = useState<Bounds>({ max_offset: 100, min_speed: 50, max_speed: 1000 });
  const [editing, setEditing] = useState<Profile | null>(null);
  const [dirty, setDirty] = useState(false);
  const [rows, setRows] = useState<Row[] | null>(null);
  const [readError, setReadError] = useState<string | null>(null);
  const [limitC, setLimitC] = useState<number | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [paused, setPaused] = useState(false);
  const [newName, setNewName] = useState("");
  const [naming, setNaming] = useState(false);
  const nameRef = useRef<HTMLInputElement>(null);
  const active = calibration.value?.active ?? "factory";

  const load = async (pick?: string) => {
    try {
      const out = await api<{ profiles: Profile[]; active: string; bounds: Bounds }>("GET", "/api/calibration");
      setProfiles(out.profiles); setBounds(out.bounds); setLoadError(null);
      const want = pick ?? editing?.id ?? out.active;
      setEditing(structuredClone(out.profiles.find((p) => p.id === want && !p.unreadable) ?? out.profiles[0]));
      setDirty(false);
    } catch (e) { setLoadError(e instanceof Error ? e.message : String(e)); }
  };
  useEffect(() => { load(); }, []);

  // Live readout, twice a second while this screen is open and visible.
  useEffect(() => {
    if (paused) return;
    let alive = true, timer = 0;
    const tick = async () => {
      if (!document.hidden) {
        try {
          const out = await api<{ servos: Row[]; error: string | null; max_allowed_c: number | null }>("GET", "/api/debug/servos");
          if (!alive) return;
          setRows(out.servos); setReadError(out.error); setLimitC(out.max_allowed_c);
        } catch (e) { if (alive) setReadError(e instanceof Error ? e.message : String(e)); }
      }
      if (alive) timer = window.setTimeout(tick, 500);
    };
    tick();
    return () => { alive = false; clearTimeout(timer); };
  }, [paused]);

  if (loadError) return <div class="page-empty"><Icon name="alert" size={28} /><h2>Calibration couldn't load</h2><p>{loadError}</p><button class="btn" onClick={() => load()}>Try again</button></div>;
  if (!profiles || !editing) return <div class="page-empty"><p>Reading the calibration profiles…</p></div>;

  const readonly = !!editing.readonly;
  const isActive = editing.id === active;
  const change = (joint: string, patch: Partial<Cal>) => {
    setEditing({ ...editing, servos: { ...editing.servos, [joint]: { ...editing.servos[joint], ...patch } } });
    setDirty(true);
  };
  const pick = (id: string) => {
    if (dirty && !confirm("Discard the unsaved changes to this profile?")) return;
    setEditing(structuredClone(profiles.find((p) => p.id === id)!)); setDirty(false); setNaming(false);
  };
  const body = () => ({ name: editing.name, notes: editing.notes, allow_action_groups: editing.allow_action_groups, servos: editing.servos });
  const save = async () => {
    const out = await attempt(() => api<Profile>("PUT", `/api/calibration/${editing.id}`, body()),
      isActive ? "Saved. The new limits are in force now." : "Profile saved.");
    if (out) await load(out.id);
  };
  const saveAs = async (e: Event) => {
    e.preventDefault();
    const out = await attempt(() => api<Profile>("POST", "/api/calibration", { ...body(), name: newName.trim() }), `Saved the profile “${newName.trim()}”.`);
    if (out) { setNaming(false); setNewName(""); await load(out.id); }
  };
  const activate = async () => {
    if (dirty) return toast("error", "Save the changes first, then put the profile in force.");
    if (await attempt(() => api("POST", "/api/calibration/active", { id: editing.id }), `“${editing.name}” is now in force.`)) await load(editing.id);
  };
  const remove = async () => {
    if (!confirm(`Delete the profile “${editing.name}”? This can't be undone.`)) return;
    if (await attempt(() => api("DELETE", `/api/calibration/${editing.id}`), "Profile deleted.")) await load("factory");
  };
  const jog = (row: Row, by: number) => {
    const base = row.commanded ?? row.actual ?? row.stand;
    attempt(() => api("POST", "/api/debug/jog", { servo: row.joint, pulse: base + by }));
  };
  const blocked = blockedReason();
  const live = Object.fromEntries((rows ?? []).map((r) => [r.joint, r]));
  const narrowed = Object.entries(editing.servos).filter(([j, c]) => live[j] && (c.min !== live[j].hard_limits[0] || c.max !== live[j].hard_limits[1])).length;

  return (
    <div class="calibrate">
      <header class="page-head">
        <h1>Calibrate</h1>
        <p>Match the twin to the real robot, and set how far and how fast each servo may go.</p>
      </header>

      <section class="panel profile-bar">
        <div class="profile-pick">
          <label for="cal-profile">Profile</label>
          <select id="cal-profile" value={editing.id} onChange={(e) => pick(e.currentTarget.value)}>
            {profiles.map((p) => <option key={p.id} value={p.id} disabled={p.unreadable}>{p.name}{p.id === active ? " (in force)" : ""}{p.unreadable ? " (unreadable)" : ""}</option>)}
          </select>
          {isActive ? <span class="tag tag-accent"><Icon name="check" size={14} />In force on every command</span>
            : <button class="btn robot" onClick={activate}>Put this profile in force</button>}
          {dirty && <span class="tag tag-off">Unsaved changes</span>}
        </div>
        <div class="row">
          {!readonly && <button class="btn primary" disabled={!dirty} onClick={save}><Icon name="save" size={18} />Save changes</button>}
          <button class="btn" onClick={() => { setNaming(true); setNewName(readonly ? "" : `${editing.name} copy`); setTimeout(() => nameRef.current?.focus(), 0); }}><Icon name="plus" size={18} />Save as a new profile</button>
          {!readonly && <button class="btn quiet" onClick={remove}><Icon name="trash" size={18} />Delete</button>}
        </div>
        {naming && (
          <form class="row" onSubmit={saveAs}>
            <input ref={nameRef} type="text" value={newName} maxLength={60} placeholder="Clinic robot, October" aria-label="Name for the new profile" onInput={(e) => setNewName(e.currentTarget.value)} />
            <button class="btn primary" disabled={!newName.trim()}>Save profile</button>
            <button type="button" class="btn quiet" onClick={() => setNaming(false)}>Cancel</button>
          </form>
        )}
        {readonly
          ? <p class="hint">The factory profile is the robot API's own limits and can't be edited. Change a value below, then “Save as a new profile” to keep it.</p>
          : <p class="hint">{narrowed ? `${narrowed} servo${narrowed === 1 ? " has" : "s have"} a narrower range than the factory limits.` : "No range is narrower than the factory limits yet."} A profile can only narrow the factory limits, never widen them.</p>}
        <label class="check">
          <input type="checkbox" checked={editing.allow_action_groups} onChange={(e) => { setEditing({ ...editing, allow_action_groups: e.currentTarget.checked }); setDirty(true); }} />
          Allow the robot's built-in moves (walking, wave, bow). They run inside the robot and can't be limited by a profile.
        </label>
      </section>

      <div class="cal-views">
        <ChannelTwin channel="command" title="Twin: told" hint="The pose the dashboard last asked for" />
        <ChannelTwin channel="actual" title="Robot: doing" hint="Built from the servos' own readback" />
        <CameraPanel compact />
      </div>
      <p class="hint">What this shows: left is where the twin thinks each joint should be, the middle is where the servos say they are. If they disagree while the robot is still, set an offset below until they match.</p>

      <section class="panel">
        <div class="panel-head">
          <h2>Servos</h2>
          <span class="hint">{paused ? "Readout paused" : readError ? "" : "Reading twice a second"}</span>
          <button class="btn toggle" aria-pressed={paused} onClick={() => setPaused(!paused)}>Pause readout</button>
        </div>
        {readError && <p class="note bad" role="alert"><Icon name="alert" size={16} /><span>The robot isn't answering: {readError} Limits can still be edited and saved.</span></p>}
        {blocked && !readError && <p class="note"><Icon name="info" size={16} /><span>Jogging is paused: {blocked}</span></p>}
        {!rows && !readError && <p class="hint">Waiting for the first reading…</p>}
        {GROUPS.map(([group, title, about]) => {
          const list = (rows ?? []).filter((r) => r.group === group);
          if (!list.length) return null;
          return (
            <div key={group} class="servo-group">
              <h3>{title}</h3>
              <p class="hint">{about}</p>
              <div class="servo-scroll">
                <table class="servo-table">
                  <thead>
                    <tr>
                      <th scope="col">Servo</th>
                      <th scope="col" title="What the dashboard last asked for, in pulses (0 to 1000)">Told</th>
                      <th scope="col" title="What the servo reports, in pulses, after the offset">Doing</th>
                      <th scope="col" title="Doing minus told, in pulses. About 4 pulses is 1 degree.">Gap</th>
                      <th scope="col">Temp</th>
                      <th scope="col">Volts</th>
                      <th scope="col" title="This robot's servo bus doesn't report load. A growing gap is the sign of a stalled servo.">Load</th>
                      <th scope="col">Min</th><th scope="col">Max</th>
                      <th scope="col" title="Pulses added to every command, and taken off every reading">Offset</th>
                      <th scope="col" title="Fastest travel allowed, in pulses per second">Speed cap</th>
                      {group === "arm" && <th scope="col">Jog</th>}
                    </tr>
                  </thead>
                  <tbody>
                    {list.map((r) => {
                      const c = editing.servos[r.joint];
                      const hot = r.temp_c != null && limitC != null && r.temp_c >= limitC;
                      const off = r.error != null && Math.abs(r.error) >= ERR_WARN;
                      const span = r.hard_limits[1] - r.hard_limits[0];
                      const pos = (p: number) => `${Math.max(0, Math.min(100, ((p - r.hard_limits[0]) / span) * 100))}%`;
                      return (
                        <tr key={r.id} class={off || hot ? "is-flag" : ""}>
                          <th scope="row">
                            <b>{cap(words(r.joint))}</b><small>ID {r.id}{r.torque_on === false ? ", limp" : ""}</small>
                            <span class="range-bar" role="img" aria-label={`Allowed ${c.min} to ${c.max} of ${r.hard_limits[0]} to ${r.hard_limits[1]}`}>
                              <i class="range-allowed" style={{ left: pos(c.min), right: `calc(100% - ${pos(c.max)})` }} />
                              {r.commanded != null && <i class="range-told" style={{ left: pos(r.commanded) }} />}
                              {r.actual != null && <i class="range-doing" style={{ left: pos(r.actual) }} />}
                            </span>
                          </th>
                          <td>{r.commanded ?? "–"}</td>
                          <td>{r.actual ?? "–"}{r.raw != null && c.offset !== 0 && <small> raw {r.raw}</small>}</td>
                          <td class={off ? "is-bad" : ""}>{r.error == null ? "–" : <>{off && <Icon name="alert" size={14} />}{r.error > 0 ? "+" : ""}{r.error}</>}</td>
                          <td class={hot ? "is-bad" : ""}>{r.temp_c == null ? "–" : `${r.temp_c} °C`}</td>
                          <td>{r.vin_v == null ? "–" : `${r.vin_v.toFixed(1)} V`}</td>
                          <td><small>not reported</small></td>
                          <td><Num value={c.min} min={r.hard_limits[0]} max={c.max - 10} label={`${words(r.joint)} minimum`} onChange={(v) => change(r.joint, { min: v })} /></td>
                          <td><Num value={c.max} min={c.min + 10} max={r.hard_limits[1]} label={`${words(r.joint)} maximum`} onChange={(v) => change(r.joint, { max: v })} /></td>
                          <td class="cell-offset">
                            <Num value={c.offset} min={-bounds.max_offset} max={bounds.max_offset} label={`${words(r.joint)} offset`} onChange={(v) => change(r.joint, { offset: v })} />
                            {r.error != null && Math.abs(r.error) >= 8 && isActive && !dirty && (
                              <button class="btn quiet" title="Change the offset so this reading matches what was told" onClick={() => change(r.joint, { offset: Math.max(-bounds.max_offset, Math.min(bounds.max_offset, c.offset + r.error!)) })}>Absorb gap</button>
                            )}
                          </td>
                          <td><Num value={c.max_speed} min={bounds.min_speed} max={bounds.max_speed} label={`${words(r.joint)} speed cap`} onChange={(v) => change(r.joint, { max_speed: v })} /></td>
                          {group === "arm" && (
                            <td class="cell-jog">
                              <button class="nudge" disabled={!!blocked} aria-label={`${words(r.joint)}: 5 degrees down`} onClick={() => jog(r, -21)}>−</button>
                              <button class="nudge" disabled={!!blocked} aria-label={`${words(r.joint)}: 5 degrees up`} onClick={() => jog(r, 21)}>+</button>
                            </td>
                          )}
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          );
        })}
        <ul class="trim-legend">
          <li><i class="key-allowed" />Allowed range inside the factory range</li>
          <li><i class="key-told" />Told</li>
          <li><i class="key-doing" />Doing</li>
        </ul>
        <p class="hint">Positions are in servo pulses, 0 to 1000 over 240 degrees, so about 4 pulses is 1 degree. A gap of {ERR_WARN} pulses or more is flagged: the servo may be stalled, limp, or need an offset. Jog moves the real robot by 5 degrees, through every limit.</p>
      </section>
    </div>
  );
}
