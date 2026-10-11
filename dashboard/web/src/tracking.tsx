// What the pose tracker sees during a session: where it runs, who it follows, which arms count, and the
// angle the rep count and the robot's pace come from. The picture beside it has the same thing drawn on.
import { live } from "./api";
import { Icon } from "./icons";

const ARM_NAME: Record<string, string> = { L: "Left arm", R: "Right arm" };
const STATE_LABEL: Record<string, string> = { TRACKING: "Tracking", HOLD: "Holding", LOST: "Not seen" };
const STATE_TONE: Record<string, string> = { TRACKING: "good", HOLD: "warn", LOST: "bad" };

export function TrackingPanel() {
  const l = live.value;
  const t = l.tracking;
  if (l.synthetic_tracker) {
    return (
      <section class="panel tracking" aria-label="Pose tracking">
        <p class="note"><Icon name="alert" size={16} /><span>The patient is simulated, so there is no tracking to show.{l.tracker_note ? ` ${l.tracker_note}` : ""}</span></p>
      </section>
    );
  }
  if (!t) return <section class="panel tracking" aria-label="Pose tracking"><p class="mirror-where"><Icon name="alert" size={16} /><span>Waiting for the first answer from the tracker…</span></p></section>;
  const both = !!t.green.L && !!t.green.R;
  return (
    <section class="panel tracking" aria-label="Pose tracking">
      <h3>Tracking</h3>
      <p class="mirror-where">
        <Icon name={t.on === "gx10" ? "check" : "alert"} size={16} />
        <span>
          {t.on === "gx10" && <>Calculated on the GX10{t.transport === "udp" ? " over UDP" : ""}{t.delivery != null && <> · {Math.round(t.delivery * 100)}% of frames answered</>}{t.ms != null && <> · {t.ms} ms</>}</>}
          {t.on === "local" && "Calculated on this computer. The GX10 isn't answering."}
          {t.on === "none" && "Waiting for the first answer from the tracker…"}
        </span>
      </p>
      <p class={`mirror-people ${t.visible ? "" : "is-warn"}`}>
        <Icon name="patient" size={16} />
        <span>
          {!t.visible ? "No patient found in the picture." : t.people > 1 ? `${t.people} people in view. Following the one closest to the middle (boxed in the picture).` : "Following the patient (boxed in the picture)."}
        </span>
      </p>
      <p class={`mirror-cal ${t.calibrated ? "is-done" : ""}`}>
        <Icon name={t.calibrated ? "check" : "patient"} size={16} />
        <span>{t.calibrated ? "Calibrated to the patient." : "Calibrating: the patient should face the camera with their arms at their sides for a moment."}</span>
      </p>
      <ul class="arm-rows" aria-label="Which arms the tracker can see">
        {["L", "R"].map((side) => {
          const st = t.arms[side];
          return (
            <li key={side} class={`arm-row tone-${st ? STATE_TONE[st] : "none"}`}>
              <span class="arm-name">{ARM_NAME[side]}</span>
              <span class="arm-state"><i aria-hidden="true" />{st ? STATE_LABEL[st] : "—"}</span>
            </li>
          );
        })}
      </ul>
      <dl class="mirror-angles two">
        <div><dt>Shoulder lift</dt><dd>{both && t.angles.shoulder_elevation != null ? `${t.angles.shoulder_elevation}°` : "—"}</dd></div>
        <div><dt>Elbow bend</dt><dd>{both && t.angles.elbow != null ? `${t.angles.elbow}°` : "—"}</dd></div>
      </dl>
      <p class="hint">Reps are counted only while <b>both</b> arms are Tracking. If one is held or not seen, the robot waits where it is.</p>
    </section>
  );
}
