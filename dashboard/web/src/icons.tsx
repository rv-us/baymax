// Line icons drawn for this app (24 px grid, 1.75 stroke). No icon font, no emoji.
const PATHS: Record<string, string> = {
  play: "M7 5l12 7-12 7z",
  pause: "M8 5v14M16 5v14",
  stop: "M6 6h12v12H6z",
  loop: "M17 3l3 3-3 3M20 6H8a4 4 0 0 0-4 4v1M7 21l-3-3 3-3M4 18h12a4 4 0 0 0 4-4v-1",
  plus: "M12 5v14M5 12h14",
  trash: "M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v6M14 11v6",
  left: "M15 5l-7 7 7 7",
  right: "M9 5l7 7-7 7",
  up: "M5 15l7-7 7 7",
  down: "M5 9l7 7 7-7",
  check: "M4 12.5l5 5L20 6.5",
  cross: "M6 6l12 12M18 6L6 18",
  alert: "M12 3l10 18H2zM12 10v5M12 18v.5",
  record: "M12 7a5 5 0 1 0 0 10 5 5 0 0 0 0-10z",
  robot: "M7 9h10a2 2 0 0 1 2 2v6a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2v-6a2 2 0 0 1 2-2zM12 9V5M12 5h.01M9.5 14h.01M14.5 14h.01M2.5 13v3M21.5 13v3",
  camera: "M4 8h3l2-3h6l2 3h3v11H4zM12 16.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7z",
  coach: "M4 5h16v11H9l-5 4zM8 9h8M8 12.5h5",
  studio: "M4 19h16M6 19V9M12 19V5M18 19v-7M4.5 9h3M10.5 5h3M16.5 12h3",
  live: "M12 12h.01M8.5 15.5a5 5 0 0 1 0-7M15.5 8.5a5 5 0 0 1 0 7M5.5 18.5a9 9 0 0 1 0-13M18.5 5.5a9 9 0 0 1 0 13",
  sessions: "M5 4h14v17H5zM9 9h6M9 13h6M9 17h3",
  plans: "M4 6h3M4 12h3M4 18h3M10 6h10M10 12h10M10 18h10",
  save: "M5 4h11l3 3v13H5zM8 4v5h7V4M8 20v-6h8v6",
  upload: "M12 16V4M7 9l5-5 5 5M4 20h16",
  speak: "M4 10v4h4l5 4V6L8 10zM16.5 9a4 4 0 0 1 0 6M19 6.5a7.5 7.5 0 0 1 0 11",
  walk: "M13 4.5a1.5 1.5 0 1 0 0 .01M9 21l2-6-2-3 1-4 3-1 3 3 3 1M11 15l3 2 1 4M10 8l-3 2-1 3",
  refresh: "M20 5v5h-5M4 19v-5h5M19 10a7.5 7.5 0 0 0-13.5-3M5 14a7.5 7.5 0 0 0 13.5 3",
  settings: "M4 7h10M18 7h2M4 17h2M10 17h10M16 4.5v5M8 14.5v5",
  battery: "M3 8h16v8H3zM21 11v2",
  temp: "M12 4a2 2 0 0 0-2 2v8.5a3.5 3.5 0 1 0 4 0V6a2 2 0 0 0-2-2zM12 10v6",
  eye: "M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z",
  patient: "M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 21a8 8 0 0 1 16 0",
  flag: "M5 21V4M5 5h12l-2 4 2 4H5",
  hand: "M8 12V6a1.5 1.5 0 0 1 3 0v5M11 11V4.5a1.5 1.5 0 0 1 3 0V11M14 11V6a1.5 1.5 0 0 1 3 0v8c0 4-2.5 7-6 7-3 0-4.5-1.5-6-4l-2-3.5a1.4 1.4 0 0 1 2.3-1.5L8 14",
  film: "M4 5h16v14H4zM8 5v14M16 5v14M4 9.5h4M4 14.5h4M16 9.5h4M16 14.5h4",
  mirror: "M12 3v18M8 7l-5 5 5 5zM16 7l5 5-5 5z",
};

export function Icon({ name, size = 20, label }: { name: string; size?: number; label?: string }) {
  return (
    <svg class="icon" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"
      role={label ? "img" : undefined} aria-label={label} aria-hidden={label ? undefined : "true"}>
      <path d={PATHS[name] ?? PATHS.alert} />
    </svg>
  );
}

/** Link status: the shape carries the state, so colour is never the only signal.
 *  ok = filled circle, stub = ring, down = cross, unknown = dash. */
export function StateMark({ state }: { state: string }) {
  const shape = state === "ok" ? <circle cx="8" cy="8" r="5" fill="currentColor" stroke="none" />
    : state === "stub" ? <circle cx="8" cy="8" r="4.25" />
    : state === "down" ? <path d="M4 4l8 8M12 4l-8 8" />
    : <path d="M4 8h8" />;
  return (
    <svg class={`mark mark-${state}`} width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor"
      stroke-width="2" stroke-linecap="round" aria-hidden="true">{shape}</svg>
  );
}
