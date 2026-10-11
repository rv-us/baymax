// Mirror: the robot copies the patient's arms, live. The camera's frames go to the GX10 (or are tracked here),
// the tracker answers with the servo pulses for every arm drawn green, and those go to the robot.
import { useEffect } from "preact/hooks";
import { api, attempt, mirror, safety, status } from "./api";
import { CameraPanel } from "./camera";
import { Icon } from "./icons";
import { TwinPanel, twinView } from "./twin";

const ARM_NAME: Record<string, string> = { L: "Left arm", R: "Right arm" };
const STATE_LABEL: Record<string, string> = { TRACKING: "Tracking", HOLD: "Holding", LOST: "Not seen" };
const STATE_TONE: Record<string, string> = { TRACKING: "good", HOLD: "warn", LOST: "bad" };

function Where() {
  const m = mirror.value;
  if (!m.running) return null;
  if (m.pose_on === "gx10") {
    return (
      <p class="mirror-where">
        <Icon name="check" size={16} />
        <span>Arms are tracked on the GX10{m.transport === "udp" ? " over UDP" : ""}
          {m.delivery != null && <> · {Math.round(m.delivery * 100)}% of frames answered</>}</span>
      </p>
    );
  }
  if (m.pose_on === "local") {
    return <p class="mirror-where"><Icon name="alert" size={16} /><span>Arms are tracked on this computer. The GX10 isn't answering.</span></p>;
  }
  return <p class="mirror-where"><Icon name="alert" size={16} /><span>Waiting for the first answer from the tracker…</span></p>;
}

function ArmRow({ side }: { side: string }) {
  const m = mirror.value;
  const st = m.arms[side];
  const green = !!m.green[side];
  return (
    <li class={`arm-row tone-${st ? STATE_TONE[st] : "none"}`}>
      <span class="arm-name">{ARM_NAME[side]}</span>
      <span class="arm-state"><i aria-hidden="true" />{st ? STATE_LABEL[st] : "—"}</span>
      <span class="arm-sends">{!m.running ? "" : green ? "Moving the robot" : "Robot's arm left alone"}</span>
    </li>
  );
}

export function MirrorScreen() {
  const m = mirror.value;
  const s = status.value;
  const stopped = !!safety.value.stop;
  const blocked = !stopped && s && !s.motion.ok;
  useEffect(() => { twinView("auto"); }, []);
  const both = m.green.L && m.green.R;

  return (
    <div class="mirror">
      <header class="page-head">
        <h1>Mirror</h1>
        <p>The robot copies your arms. Stand facing its camera with your arms at your sides, and it follows from there.</p>
      </header>

      <div class="mirror-views live-views">
        <TwinPanel />
        <CameraPanel path={m.running ? "/api/mirror.jpg" : "/api/camera.jpg"} />
      </div>

      <section class="panel mirror-side">
        <h2>{m.running ? "Mirroring" : "Start mirroring"}</h2>
        {m.error && <p class="note bad" role="alert"><Icon name="alert" size={16} />{m.error}</p>}

        {!m.running ? (
          <>
            <p class="hint">
              The robot's arms will copy yours as soon as it can see you properly. Keep clear of them. If several people are in
              view it follows the one closest to the middle of the picture.
            </p>
            {s?.dry_run && <p class="note"><Icon name="camera" size={16} />{s.webcam ? "This is a dry run, so the laptop's webcam stands in for the robot's camera." : "This is a dry run and there is no webcam, so there is nothing to mirror."}</p>}
            <button class="btn primary big" disabled={stopped || !!blocked || (!!s?.dry_run && !s?.webcam)}
              onClick={() => attempt(() => api("POST", "/api/mirror/start"))}>
              <Icon name="play" />Start
            </button>
            {stopped && <p class="note bad">A safety stop is active. Clear it in the banner above first.</p>}
            {blocked && <p class="note bad">{s!.motion.reasons.join(" ")}</p>}
          </>
        ) : (
          <>
            <Where />
            <p class={`mirror-cal ${m.calibrated ? "is-done" : ""}`}>
              <Icon name={m.calibrated ? "check" : "patient"} size={16} />
              <span>{m.calibrated ? "Calibrated to you." : "Calibrating: stand square to the camera with your arms at your sides and hold still for a moment."}</span>
            </p>
            {m.people > 1 && <p class="mirror-people"><Icon name="patient" size={16} /><span>{m.people} people in view. Following the one closest to the middle.</span></p>}
            <ul class="arm-rows" aria-label="Which arms are being followed">
              <ArmRow side="L" /><ArmRow side="R" />
            </ul>
            <p class="hint">Only an arm shown as <b>Tracking</b> moves the robot. If one is held or not seen, that arm of the robot stays where it is.</p>
            <dl class="mirror-angles">
              <div><dt>Shoulder lift</dt><dd>{both && m.angles.shoulder_elevation != null ? `${Math.round(m.angles.shoulder_elevation)}°` : "—"}</dd></div>
              <div><dt>Elbow bend</dt><dd>{both && m.angles.elbow != null ? `${Math.round(m.angles.elbow)}°` : "—"}</dd></div>
              <div><dt>Moves sent</dt><dd>{m.sent}</dd></div>
            </dl>
            <button class="btn danger big" onClick={() => attempt(() => api("POST", "/api/mirror/stop"))}>
              <Icon name="stop" />Stop mirroring
            </button>
            <p class="hint">Stopping eases the robot's arms back to rest.</p>
          </>
        )}
      </section>
    </div>
  );
}
