"""arm_mvp's arm tracking as one object: a BGR frame in, the patient's pose out.

This is the computation the dashboard offloads. It runs in two places with the same code:
inside the dashboard process (dashboard/webcam.py, when there is no GX10) and on the GX10
behind dashboard/pose_service.py. One tracker holds one patient's state (smoothing,
calibration, which arm is which), so each client gets its own.

    tracker = ArmTracker()
    pose, overlay = tracker.process(frame_bgr, t_seconds)

With several people in view the tracker follows the one closest to the centre of the frame
(arm_mvp/people.py); `pose["people"]` is how many it saw. `pose` is what the dashboard reads: {visible, x, size, angles: {shoulder_elevation, elbow},
arms: {L: state, R: state}, calibrated, pulses}. `pulses` ({servo id as text: pulse}) is what the
robot's arms should do to copy the patient: arm_mvp's own solve, damping, rate limit and coupled arm
limits, so the machine that tracks decides it and the dashboard only forwards it.

Only joints drawn green (state TRACKING) count. An arm that is holding or lost contributes no pulses,
so nothing it guessed reaches the robot, and `angles` (what a workout is scored on) is only given while
BOTH arms are green: half a body's worth of angles would score a rep that wasn't seen. `green` says
which arms qualify. `overlay` is what is needed to draw the arms on the
picture, in pixels: {shoulders: [[x, y], [x, y]] | None, arms: {L: {pts, state}, R: ...}}.
"""
from __future__ import annotations

import math
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARM_MVP = os.path.join(ROOT, "arm_mvp")
BODY_TO_SHOULDERS = 4.5   # a standing body is about this many shoulder widths tall
GREEN = "TRACKING"        # the state arm_mvp draws green
EMPTY_POSE = {"visible": False, "x": 0.0, "size": 0.0, "angles": {}, "arms": {}, "calibrated": False, "pulses": {},
              "people": 0, "green": {}}


def load_estimator():
    """arm_mvp's own pieces. They import each other by bare name, so its folder goes on the path."""
    if ARM_MVP not in sys.path:
        sys.path.insert(0, ARM_MVP)
    import arm_mimic  # noqa: PLC0415  (pulls in cv2, mediapipe, solver, calibration, filters)
    import calibration
    return arm_mimic, calibration


def elevation_deg(shoulder, elbow) -> float | None:
    """How far the upper arm is lifted from hanging, in the torso frame (forward, left, up)."""
    d = [e - s for e, s in zip(elbow, shoulder)]
    n = math.sqrt(sum(c * c for c in d))
    return None if n < 1e-6 else math.degrees(math.acos(max(-1.0, min(1.0, -d[2] / n))))


class ArmTracker:
    def __init__(self):
        self.mimic, self.calibration = load_estimator()
        self.args = self.mimic.parse_args([])
        self.detect, self._close = self.mimic.make_landmarker()
        self.calib = self.calibration.new_state(self.calibration.load())  # in memory; never saved
        self.arms = {s: self.mimic.new_arm(s, self.args) for s in "LR"}
        self.stamp, self.t_prev = -1, None
        self.solver = self.mimic.solver
        self.pulses = self.solver.stand_arm_pulses()   # where the robot's arms are told to be, starting at rest
        self.damper = self.mimic.PulseDamper(self.pulses)

    def close(self) -> None:
        self._close()

    def process(self, frame, t: float) -> tuple[dict, dict]:
        h, w = frame.shape[:2]
        dt = 1.0 / 30.0 if self.t_prev is None else max(t - self.t_prev, 1e-3)
        self.t_prev = t
        self.stamp = max(self.stamp + 1, int(t * 1000))  # VIDEO mode needs strictly increasing times
        lm = self.detect(frame, self.stamp)
        if self.detect.switched:   # the person nearest the centre changed: start their arms fresh
            self.mimic.reset_arms(self.arms, self.args)
        people = self.detect.people
        if lm is None:
            self.calibration.update(self.calib, None, None, None, t)
        else:
            self.calibration.update(self.calib, lm[0][:, :2] * (w, h), lm[1], lm[2], t)
        self.mimic.track_arms(self.arms, lm, self.calib["cal"], t, dt, (w, h), self.args)
        states = {s: a["state"] for s, a in self.arms.items()}
        # Pulses for every arm are kept up to date (a lost arm's ease back to rest included) so the state is
        # right when the arm returns, but only green arms' pulses are handed out below.
        self.pulses = self.mimic.step_pulses(self.damper, self.pulses, self.arms, self.args)
        pulses = {str(sid): int(p) for sid, p in self.mimic.green_pulses(self.arms, self.pulses).items()}
        green = {s: a["state"] == GREEN for s, a in self.arms.items()}
        if lm is None:
            return ({**EMPTY_POSE, "arms": states, "calibrated": bool(self.calib["locked"]), "pulses": pulses,
                     "people": people, "green": green}, {"shoulders": None, "arms": {}})

        px = lm[0][:, :2] * (w, h)
        seen = bool(lm[2][11] >= self.args.min_vis and lm[2][12] >= self.args.min_vis)
        width_px = float(np.linalg.norm(px[11] - px[12]))
        elev, flex = [], []
        if all(green.values()):   # scored only when both arms are seen properly
            for arm in self.arms.values():
                e = elevation_deg(arm["pts"][0], arm["pts"][1])
                if e is not None:
                    elev.append(e)
                flex.append(180.0 - arm["angles"][2])
        pose = {"visible": seen,
                "x": float(max(-1.0, min(1.0, (px[11][0] + px[12][0]) / 2 / w * 2 - 1))),
                "size": float(max(0.0, min(1.0, BODY_TO_SHOULDERS * width_px / h))),
                "angles": {"shoulder_elevation": sum(elev) / len(elev) if elev else None,
                           "elbow": sum(flex) / len(flex) if flex else None},
                "arms": states, "calibrated": bool(self.calib["locked"]), "pulses": pulses, "people": people,
                "green": green}
        overlay = {"shoulders": [px[11].round(1).tolist(), px[12].round(1).tolist()],
                   "arms": {s: {"pts": [px[i].round(1).tolist() for i in self.mimic.ARM_IDX[s]],
                                "state": a["state"]} for s, a in self.arms.items()}}
        return pose, overlay
