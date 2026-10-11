// Dashboard chrome around the viewer (panel, pose buttons, servo table). Not needed when embedding twin.js.
import { BaymaxTwin } from "./twin.js";

const $ = (id) => document.getElementById(id);
const params = new URLSearchParams(location.search);
if (params.get("embed")) document.body.classList.add("embed");

let twin;
try {
  twin = new BaymaxTwin($("view"), { ghost: params.get("ghost") !== "0", ground: params.get("ground") !== "0" });
} catch (e) {
  // typically "WebGL not available" (blocked GPU, remote desktop, headless browser)
  $("t-stream").textContent = "3D view unavailable: " + e.message;
  document.getElementById("d-stream").className = "dot bad";
  throw e;
}
let model = null, rows = {}, latest = null, pending = false;

function setDot(id, cls) { $(id).className = "dot " + (cls || ""); }

twin.onConnection = (ok) => {
  setDot("d-stream", ok ? "ok" : "bad");
  $("t-stream").textContent = ok ? "live" : "reconnecting…";
};

twin.onState = (state, picked) => {
  latest = { state, picked };
  if (!pending) { pending = true; requestAnimationFrame(renderPanel); }
};

function buildServoTable() {
  const tb = document.querySelector("#servos tbody");
  for (const s of model.servos) {
    const tr = document.createElement("tr");
    const body = s.id <= 16;
    tr.innerHTML = `
      <td class="n">${s.id}</td>
      <td class="j" title="${s.joint || s.label}">${s.label.replace(/_/g, " ")}${s.id <= 16 && !s.verified ? ' <span class="unv" title="direction not verified against hardware">⚠</span>' : ""}</td>
      <td><div class="bar"><div class="track"></div><div class="mk stand"></div><div class="mk act" hidden></div><div class="mk cmd"></div>
        <input type="range" min="${s.limits[0]}" max="${s.limits[1]}" step="1" value="${s.stand}" aria-label="${s.label} pulse"></div></td>
      <td class="n p"></td><td class="n a"></td><td class="n d"></td><td class="n t"></td>`;
    tb.appendChild(tr);
    const bar = tr.querySelector(".bar");
    const rng = tr.querySelector("input");
    const place = (el, pulse) => { el.style.left = `${((pulse - s.limits[0]) / (s.limits[1] - s.limits[0])) * 100}%`; };
    place(bar.querySelector(".stand"), s.stand);
    let busy = false;
    rng.addEventListener("input", async () => {
      if (busy) return; busy = true;
      try { await twin.post("/api/command", { servos: { [s.id]: +rng.value }, source: "dashboard", label: "manual" }); } catch (e) { flash(e.message); }
      busy = false;
    });
    rows[s.id] = { tr, rng, cmd: bar.querySelector(".cmd"), act: bar.querySelector(".act"), p: tr.querySelector(".p"), a: tr.querySelector(".a"),
                   d: tr.querySelector(".d"), t: tr.querySelector(".t"), place, limits: s.limits, body };
  }
}

function renderPanel() {
  pending = false;
  if (!latest) return;
  const { state, picked } = latest;
  const shown = state.channels[picked.main];
  const cmd = state.channels.command, act = state.channels.actual;

  // top pills
  $("p-src").textContent = `showing: ${picked.main === "actual" ? "robot readback" : "command"}${shown.label ? " · " + shown.label.replace(/_/g, " ") : ""}`;
  const r = state.robot;
  if (!r.enabled) { setDot("d-robot", ""); $("t-robot").textContent = "robot: not linked"; }
  else if (r.connected) { setDot("d-robot", "ok"); $("t-robot").textContent = `robot: live${r.url ? " · " + r.url.replace(/^https?:\/\//, "") : ""}`; }
  else { setDot("d-robot", "bad"); $("t-robot").textContent = "robot: unreachable"; }
  $("robot-err").textContent = r.enabled && r.error ? r.error : "";
  $("robot-clone").disabled = !r.enabled || !r.connected;
  $("robot-off").disabled = !r.enabled;

  for (const b of document.querySelectorAll("#seg-view button")) b.setAttribute("aria-pressed", String(b.dataset.view === state.view));
  const lbl = state.channels.command?.label;
  for (const b of document.querySelectorAll("#poses button")) b.classList.toggle("on", b.dataset.name === lbl && cmd?.source !== "estimator");

  // source details
  const age = (c) => (c && c.age_s != null ? `${c.age_s.toFixed(1)} s ago` : "–");
  const lean = shown.base;
  $("kv").innerHTML = `
    <dt>command</dt><dd>${cmd ? `${cmd.source} · ${age(cmd)}` : "–"}</dd>
    <dt>robot</dt><dd>${act ? `${act.source} · ${age(act)}` : "no readback"}</dd>
    <dt>torso lean</dt><dd>${lean.pitch_deg.toFixed(0)}° pitch · ${lean.roll_deg.toFixed(0)}° roll</dd>
    ${cmd && cmd.clamped.length ? `<dt>clamped</dt><dd>servo ${cmd.clamped.join(", ")} (limit)</dd>` : ""}
    ${act && act.limp && act.limp.length ? `<dt>limp</dt><dd>${act.limp.join(", ")}</dd>` : ""}`;

  // servo table
  for (const s of model.servos) {
    const row = rows[s.id];
    const pc = shown.servos[s.id], c = cmd?.servos[s.id], a = act?.servos[s.id];
    row.p.textContent = pc;
    row.a.textContent = row.body ? (shown.angles[s.joint] ?? 0).toFixed(0) : "";
    if (document.activeElement !== row.rng) row.rng.value = c ?? pc;
    row.place(row.cmd, c ?? pc);
    row.act.hidden = a == null;
    if (a != null) row.place(row.act, a);
    const d = row.body && a != null && c != null ? a - c : null;
    row.d.textContent = d == null ? "" : (d > 0 ? "+" : "") + d;
    row.d.className = "n d" + (d != null && Math.abs(d) > 40 ? " warm" : "");
    const temp = act?.temps?.[s.id];
    row.t.textContent = temp != null ? temp : "";
    row.t.className = "n t" + (temp >= 75 ? " hot" : temp >= 65 ? " warm" : "");
  }
}

let flashTimer;
function flash(msg) {
  $("robot-err").textContent = msg;
  clearTimeout(flashTimer);
  flashTimer = setTimeout(() => { if ($("robot-err").textContent === msg) $("robot-err").textContent = ""; }, 6000);
}

async function main() {
  // camera presets
  for (const name of ["iso", "front", "side", "back", "top"]) {
    const b = document.createElement("button");
    b.textContent = name;
    b.onclick = () => twin.setCameraPreset(name);
    $("cams").appendChild(b);
  }
  try {
    model = await twin.init();
  } catch (e) {
    $("t-stream").textContent = "model failed: " + e.message;
    setDot("d-stream", "bad");
    return;
  }
  buildServoTable();

  const poses = await (await fetch(`${twin.baseUrl}/api/poses`)).json();
  for (const p of poses.poses) {
    const b = document.createElement("button");
    b.textContent = p.title;
    b.dataset.name = p.name;
    b.title = p.name;
    b.onclick = () => twin.post("/api/pose", { name: p.name }).catch((e) => flash(e.message));
    $("poses").appendChild(b);
  }

  for (const b of document.querySelectorAll("#seg-view button"))
    b.onclick = () => twin.post("/api/view", { view: b.dataset.view }).catch((e) => flash(e.message));
  if (params.get("view")) twin.post("/api/view", { view: params.get("view") }).catch(() => {});
  $("o-ghost").checked = twin.options.ghost;
  $("o-ground").checked = twin.options.ground;
  $("o-ghost").onchange = (e) => twin.setOptions({ ghost: e.target.checked });
  $("o-ground").onchange = (e) => twin.setOptions({ ground: e.target.checked });

  try { $("robot-url").value = localStorage.getItem("twin.robotUrl") || ""; } catch { /* storage may be blocked */ }
  $("robot-connect").onclick = () => {
    const url = $("robot-url").value.trim();
    if (!url) return flash("enter the robot_api URL, e.g. http://169.233.72.93:8000");
    try { localStorage.setItem("twin.robotUrl", url); } catch { /* ignore */ }
    twin.post("/api/robot", { url, hz: 4 }).catch((e) => flash(e.message));
  };
  $("robot-off").onclick = () => twin.post("/api/robot", { disconnect: true }).catch((e) => flash(e.message));
  $("robot-clone").onclick = () => twin.post("/api/clone").catch((e) => flash(e.message));

  twin.connect();
}
main();
