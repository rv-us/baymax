// Library: every exercise and emote the robot knows. Point at one to preview it on a twin
// in this browser; click it to play it on the real robot over the LAN.
import { useEffect, useRef, useState } from "preact/hooks";
import { api, attempt, Clip, LibraryItem, playback } from "./api";
import { Icon } from "./icons";
import { forgetClips, MotionTwin, solveClip } from "./motion";
import { armed, blockedReason, playOnRobot, PlayTarget, request, RobotBar } from "./play";
import { go } from "./route";
import { pendingOpen } from "./studio";

type Filter = "all" | "exercise" | "emote" | "builtin";
const FILTERS: [Filter, string][] = [["all", "All"], ["exercise", "Exercises"], ["emote", "Emotes"], ["builtin", "Built into the robot"]];
const KIND: Record<string, string> = { exercise: "Exercise", emote: "Emote", builtin: "Built-in" };
const SOURCE: Record<string, string> = { generated: "Generated", recorded: "Recorded", imported: "Imported", starter: "Starter", manual: "Made in Studio" };

const targetOf = (item: LibraryItem): PlayTarget => item.kind === "builtin"
  ? { key: item.id, name: item.name, builtin: item.group }
  : { key: item.id, name: item.name, id: item.id };

export function Library() {
  const [items, setItems] = useState<LibraryItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
  const [query, setQuery] = useState("");
  const [shown, setShown] = useState<LibraryItem | null>(null);   // what the preview shows
  const [clip, setClip] = useState<Clip | null>(null);
  const [clipError, setClipError] = useState<string | null>(null);
  const [solving, setSolving] = useState(false);
  const [asking, setAsking] = useState<string | null>(null);      // row waiting for "Move the robot"
  const [tapped, setTapped] = useState<string | null>(null);      // row opened by a tap on a touch screen
  const want = useRef<string | null>(null);

  const load = () => api<{ animations: LibraryItem[]; builtins: LibraryItem[] }>("GET", "/api/library")
    .then((lib) => { setItems([...lib.animations, ...lib.builtins]); setError(null); })
    .catch((e) => setError(e.message));
  useEffect(() => { load(); }, []);

  const preview = (item: LibraryItem) => {
    if (want.current === item.id) return;
    want.current = item.id;
    setShown(item);
    setClipError(null);
    if (item.kind === "builtin" || item.unreadable) { setClip(null); setSolving(false); return; }
    setSolving(true);
    solveClip(item.id, async () => (await api("GET", `/api/animations/${item.id}`)).keyframes)
      .then((c) => { if (want.current === item.id) { setClip(c); setSolving(false); } })
      .catch((e) => { if (want.current === item.id) { setClip(null); setClipError(e.message); setSolving(false); } });
  };
  const touch = useRef(false);   // was the last press a finger or pen, not a mouse or key
  const click = (item: LibraryItem) => {
    preview(item);
    if (touch.current) { setAsking(null); setTapped(tapped === item.id ? null : item.id); return; }
    if (blockedReason() || item.unreadable || (item.kind === "builtin" && !item.available)) return setAsking(null);
    if (armed.value) { setAsking(null); playOnRobot(targetOf(item)); } else setAsking(item.id);
  };
  const remove = async (item: LibraryItem) => {
    if (!confirm(`Delete “${item.name}”? This can't be undone.`)) return;
    if (await attempt(() => api("DELETE", `/api/animations/${item.id}`), `Deleted “${item.name}”.`)) {
      forgetClips();
      if (shown?.id === item.id) { setShown(null); setClip(null); want.current = null; }
      load();
    }
  };

  if (error) return <div class="page-empty"><Icon name="alert" size={28} /><h2>The library couldn't be read</h2><p>{error}</p><button class="btn" onClick={load}>Try again</button></div>;
  if (!items) return <div class="page-empty"><p>Reading the library…</p></div>;

  const q = query.trim().toLowerCase();
  const list = items.filter((i) => (filter === "all" || i.kind === filter) && (!q || `${i.name} ${i.description ?? ""}`.toLowerCase().includes(q)));
  const count = (f: Filter) => (f === "all" ? items.length : items.filter((i) => i.kind === f).length);
  const blocked = blockedReason();
  const r = request.value;

  return (
    <div class="library">
      <header class="page-head">
        <h1>Library</h1>
        <p class="hover-copy">Point at a move to preview it. Click it to play it on the robot.</p>
        <p class="touch-copy">Tap a move to preview it, then press Play on robot.</p>
      </header>
      <RobotBar />
      <div class="library-cols">
        <div class="library-list">
          <div class="library-tools">
            <div class="chips" role="group" aria-label="Show">
              {FILTERS.map(([f, label]) => (
                <button key={f} class="chip" aria-pressed={filter === f} onClick={() => setFilter(f)}>{label}<small>{count(f)}</small></button>
              ))}
            </div>
            <input type="text" class="search" placeholder="Search moves" aria-label="Search moves" value={query} onInput={(e) => setQuery(e.currentTarget.value)} />
          </div>
          {list.length === 0 ? (
            <div class="list-empty">
              <Icon name="library" size={24} />
              {items.length === 0
                ? <><p><b>No animations yet.</b> Make one in Studio, record one by hand, or describe one for the generator.</p>
                    <div class="row"><a class="btn primary" href="#/studio">Open Studio</a><a class="btn" href="#/generate">Generate one</a></div></>
                : <><p><b>Nothing matches.</b> No move fits that filter and search.</p><button class="btn" onClick={() => { setFilter("all"); setQuery(""); }}>Show everything</button></>}
            </div>
          ) : (
            <ul class="moves">
              {list.map((item) => {
                const off = item.unreadable || (item.kind === "builtin" && !item.available);
                const mine = r?.key === item.id ? r : null;
                const live = mine?.phase === "playing" && playback.value.playing;
                return (
                  <li key={item.id} class={`move ${shown?.id === item.id ? "is-shown" : ""} ${off ? "is-off" : ""}`}>
                    <button class="move-main" onPointerEnter={(e) => { if (e.pointerType === "mouse") preview(item); }} onFocus={() => preview(item)}
                      onPointerDown={(e) => { touch.current = e.pointerType !== "mouse"; }} onKeyDown={() => { touch.current = false; }}
                      aria-expanded={tapped === item.id ? true : undefined}
                      onClick={() => click(item)} aria-describedby={`m-${item.id}`}>
                      <span class={`move-kind kind-${item.kind}`}><Icon name={item.kind === "exercise" ? "studio" : item.kind === "emote" ? "hand" : "robot"} size={18} /></span>
                      <span class="move-text">
                        <b>{item.name}</b>
                        <small id={`m-${item.id}`}>{item.unreadable ? "This file couldn't be read." : item.description || KIND[item.kind]}</small>
                      </span>
                      <span class="move-meta">
                        <span class="tag">{KIND[item.kind]}</span>
                        {item.source && SOURCE[item.source] && item.source !== "starter" && <span class="tag tag-accent">{SOURCE[item.source]}</span>}
                        {item.duration_ms != null && <small>{(item.duration_ms / 1000).toFixed(1)} s</small>}
                        {item.kind === "builtin" && <small>{item.available ? "no preview" : "not on this robot"}</small>}
                      </span>
                    </button>
                    {mine?.phase === "sending" && <p class="move-status is-busy" role="status"><i class="spinner" />Sending to the robot…</p>}
                    {live && <p class="move-status is-busy" role="status"><Icon name="robot" size={16} />Playing on the robot</p>}
                    {mine?.phase === "done" && <p class="move-status is-good" role="status"><Icon name="check" size={16} />Played on the robot</p>}
                    {mine?.phase === "stopped" && <p class="move-status" role="status"><Icon name="stop" size={16} />Stopped part way</p>}
                    {mine?.phase === "failed" && <p class="move-status is-bad" role="alert"><Icon name="alert" size={16} />{mine.message}</p>}
                    {tapped === item.id && asking !== item.id && (
                      <div class="move-actions">
                        <span class="hint">{item.kind === "builtin" ? "No preview for built-in moves." : "Previewing above."}</span>
                        <button class="btn robot" disabled={!!blocked || off}
                          onClick={() => { if (armed.value) playOnRobot(targetOf(item)); else setAsking(item.id); }}><Icon name="robot" size={18} />Play on robot</button>
                      </div>
                    )}
                    {asking === item.id && (
                      <div class="confirm" role="alertdialog" aria-label={`Play ${item.name} on the real robot?`}>
                        <span>Play “{item.name}” on the real robot? Check the space around it is clear.</span>
                        <button class="btn robot" autoFocus onClick={() => { setAsking(null); playOnRobot(targetOf(item)); }}>Move the robot</button>
                        <button class="btn quiet" onClick={() => setAsking(null)}>Cancel</button>
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          )}
          {blocked && list.length > 0 && <p class="hint"><Icon name="info" size={16} /> Clicking a move previews it. Playing on the robot is paused: {blocked}</p>}
        </div>

        <aside class="library-preview">
          <MotionTwin clip={clip} playing={!!clip} loop busy={solving} error={clipError}
            title={shown ? shown.name : "Preview"}
            empty={!shown ? "Point at a move in the list to see it here." : shown.kind === "builtin"
              ? "Built-in moves run from the robot's own controller, so there is nothing to preview. They can still be played." : undefined} />
          {shown && (
            <div class="preview-card">
              <h2>{shown.name}</h2>
              <p>{shown.description || KIND[shown.kind]}</p>
              <div class="row">
                <span class="tag">{KIND[shown.kind]}</span>
                {shown.keyframes != null && <span class="tag">{shown.keyframes} keyframes</span>}
                {shown.duration_ms != null && <span class="tag">{(shown.duration_ms / 1000).toFixed(1)} s</span>}
              </div>
              {shown.kind !== "builtin" && !shown.unreadable && (
                <div class="row">
                  <button class="btn" onClick={() => { pendingOpen.value = shown.id; go("/studio"); }}><Icon name="studio" size={18} />Edit in Studio</button>
                  <button class="btn quiet" onClick={() => remove(shown)}><Icon name="trash" size={18} />Delete</button>
                </div>
              )}
            </div>
          )}
        </aside>
      </div>
    </div>
  );
}
