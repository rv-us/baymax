"""
Body mimic: one RGB camera -> TonyPi digital twin, all 16 body servos (arms and legs).

    python arm_mimic.py                  webcam, opens the twin viewer
    python arm_mimic.py --source clip.mp4 --headless-test 300
    python arm_mimic.py --probe-sign     sweep every servo in the twin to check its direction

Keys: a = arms on/off, l = legs on/off, c = recalibrate from scratch,
      x = emergency stand for ALL servos and pause (press again to resume), q = quit.
Calibration is automatic: stand facing the camera with straight arms and legs and hold still.
Copy mode: your right arm drives the robot's right arm. The frame is mirrored for display only.
Legs follow only while hips, knees and ankles are all visible, otherwise they hold the stand pose.
"""
import argparse
import os
import sys
import threading
import time

import cv2
import numpy as np

_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in (os.path.join(_HERE, ".."), os.path.join(_HERE, "..", "laptop_mvp")):
    if os.path.abspath(_p) not in sys.path:
        sys.path.insert(0, os.path.abspath(_p))

import calibration  # noqa: E402
import joints  # noqa: E402
import solver  # noqa: E402
from filters import OneEuro, slew  # noqa: E402
from mediapose_est import GC0308HardwareSimulator  # noqa: E402
from retarget import ARM_IDX, ArmIK, shoulder_frame  # noqa: E402

MODEL = os.path.join(_HERE, "..", "laptop_mvp", "pose_landmarker_full.task")
COLORS = {"TRACKING": (80, 220, 80), "HOLD": (0, 210, 255), "LOST": (60, 60, 255), "OFF": (150, 150, 150)}
LOST_STEP_SCALE = 1.0 / 6.0  # a lost arm eases back to rest at a sixth of the tracking slew rate
LEG_IDX = {"L": (23, 25, 27), "R": (24, 26, 28)}  # hip, knee, ankle
LEG_VIS = 0.6        # legs are tracked only when hips, knees and ankles all clear this
LEG_SLEW_DEG = 1.4   # leg joint slew per frame at 30 fps. Slow on purpose: the ankle then stays inside its pulse step
EASE_S = 0.5         # a group fades between the stand pose and its target over this long
HAND_CLOSE, HAND_OPEN = 0.34, 0.42  # index to wrist span / upper arm length (hysteresis around mediapose_est's 0.38)


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", default="0", help="camera index or video file for offline replay (default 0)")
    p.add_argument("--focal-px", type=float, default=None, help="focal length in pixels (default 0.9 * width)")
    p.add_argument("--min-cutoff", type=float, default=1.5, help="One Euro min cutoff in Hz (lower = smoother when still)")
    p.add_argument("--beta", type=float, default=4.0, help="One Euro beta (higher = less lag when moving)")
    p.add_argument("--min-vis", type=float, default=0.5, help="minimum landmark visibility to track an arm")
    p.add_argument("--hold-s", type=float, default=1.0, help="seconds an arm holds before easing to rest")
    p.add_argument("--max-jump", type=float, default=0.5, help="max wrist move per frame in metres (left/right swap guard)")
    p.add_argument("--max-step-deg", type=float, default=12.0, help="arm joint slew limit in degrees per frame at 30 fps")
    p.add_argument("--api-url", default=None, help="robot_api base URL. Only when given, pulses of the enabled groups go to "
                   "servo/set_many at 30 Hz and calibration progress to the LED matrix. Legs start OFF")
    p.add_argument("--enable-legs", action="store_true", help="with --api-url: allow the legs to move on the real robot")
    p.add_argument("--hands", action="store_true", help="also drive the grippers (servos 17, 18) from an open/closed estimate")
    p.add_argument("--no-twin", action="store_true", help="do not start the digital twin")
    p.add_argument("--twin-port", type=int, default=8765)
    p.add_argument("--record-landmarks", default=None, metavar="OUT.npz", help="save raw landmarks of the run")
    p.add_argument("--sim-camera", action="store_true", help="degrade frames like the robot's GC0308 camera")
    p.add_argument("--headless-test", type=int, default=0, metavar="N", help="process N frames without a window, print stats, exit")
    p.add_argument("--probe-sign", action="store_true", help="twin only: sweep each servo by +/-20 degrees and print the expected motion")
    p.add_argument("--probe-hw", action="store_true", help="same on the real robot (needs --api-url): +/-5 degrees, one servo "
                   "at a time, after a typed confirmation. Legs only with --enable-legs")
    return p.parse_args(argv)


class LatestFrame:
    """Camera reader thread that keeps only the newest frame, so inference never works on a stale one."""

    def __init__(self, index):
        self.cap = cv2.VideoCapture(index)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        if not self.cap.isOpened():
            raise SystemExit(f"could not open camera {index}")
        self.cond, self.frame, self.stamp, self.seq, self.alive = threading.Condition(), None, 0.0, 0, True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while self.alive:
            ok, frame = self.cap.read()
            with self.cond:
                self.alive = self.alive and ok
                if ok:
                    self.frame, self.stamp, self.seq = frame, time.monotonic(), self.seq + 1
                self.cond.notify_all()
        self.cap.release()

    def read(self, last_seq):
        with self.cond:
            self.cond.wait_for(lambda: self.seq != last_seq or not self.alive, timeout=1.0)
            return None if self.seq == last_seq else (self.frame, self.stamp, self.seq)


def frames(source, realtime):
    """Yields (bgr frame, seconds). Live cameras drop stale frames, files replay every frame."""
    if source.isdigit():
        cam, seq, t0 = LatestFrame(int(source)), 0, time.monotonic()
        try:
            while cam.alive:
                got = cam.read(seq)
                if got is not None:
                    seq = got[2]
                    yield got[0], got[1] - t0
        finally:
            cam.alive = False
        return
    cap = cv2.VideoCapture(source)
    if not cap.isOpened():
        raise SystemExit(f"could not open video {source}")
    fps, i, t0 = cap.get(cv2.CAP_PROP_FPS) or 30.0, 0, time.monotonic()
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if realtime:
            time.sleep(max(0.0, i / fps - (time.monotonic() - t0)))
        yield frame, i / fps
        i += 1
    cap.release()


def make_landmarker():
    import mediapipe as mp
    from mediapipe.tasks.python import BaseOptions, vision

    with open(MODEL, "rb") as f:
        options = vision.PoseLandmarkerOptions(base_options=BaseOptions(model_asset_buffer=f.read()),
                                               running_mode=vision.RunningMode.VIDEO, num_poses=1)
    landmarker = vision.PoseLandmarker.create_from_options(options)

    def detect(bgr, t_ms):
        """(image (33, 3) normalized, world (33, 3) metres, visibility (33,)) or None."""
        image = mp.Image(image_format=mp.ImageFormat.SRGB, data=cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
        res = landmarker.detect_for_video(image, t_ms)
        if not res.pose_landmarks or not res.pose_world_landmarks:
            return None
        img = np.array([[l.x, l.y, l.z] for l in res.pose_landmarks[0]])
        world = np.array([[l.x, l.y, l.z] for l in res.pose_world_landmarks[0]])
        vis = np.array([l.visibility if l.visibility is not None else 1.0 for l in res.pose_landmarks[0]])
        return img, world, vis

    return detect, landmarker.close


def new_arm(side, args):
    return {"ik": ArmIK(side), "euro": OneEuro(args.min_cutoff, args.beta), "signs": (1.0, 1.0),
            "state": "LOST", "t_ok": -1e9, "wrist": None, "pts": None,
            "target": list(joints.ARM_REST), "angles": list(joints.ARM_REST)}


def new_legs(args):
    """Leg state. angles / target are (hip_pitch, hip_roll, knee) per side in joints.py conventions."""
    return {"euro": OneEuro(args.min_cutoff, args.beta), "signs": {s: (1.0, 1.0, 1.0) for s in "LR"},
            "state": "HOLD", "lifted": None, "pts": None,
            "target": {s: (0.0, 0.0, 0.0) for s in "LR"}, "angles": {s: [0.0, 0.0, 0.0] for s in "LR"}}


def new_body(args):
    """Everything tracked. gain is each group's fade between the stand pose (0) and its target (1)."""
    return {"arms": {s: new_arm(s, args) for s in "LR"}, "legs": new_legs(args),
            "hands": {s: 0.0 for s in "LR"}, "gain": {"arm": 1.0, "leg": 0.0}}


def geometry(lm, cal, size, args):
    """Per frame basics shared by arms and legs: pixels, camera model, 3D shoulders, shoulder frame."""
    if lm is None:
        return None
    img, world, vis = lm
    px = img[:, :2] * size
    focal, center = args.focal_px or 0.9 * size[0], 0.5 * np.asarray(size, dtype=np.float64)
    sh = solver.shoulders_3d(px[11], px[12], world[11], world[12], cal["shoulder_m"], focal, center)
    frame = shoulder_frame(*sh) if sh is not None else None
    if frame is None:
        return None
    return {"img": img, "px": px, "world": world, "vis": vis, "focal": focal, "center": center,
            "sh": {"L": sh[0], "R": sh[1]}, "frame": frame}


def track_arms(arms, lm, cal, t, dt, size, args):
    """One frame: landmarks -> per arm state and slew limited angles (pitch, roll, interior elbow)."""
    geo = geometry(lm, cal, size, args)
    for side, arm in arms.items():
        idx = list(ARM_IDX[side])
        sol = None
        if geo is not None and geo["vis"][idx].min() >= args.min_vis and np.all(np.isfinite(geo["px"][idx])):
            px, sh = geo["px"], geo["sh"][side]
            elbow, wrist, arm["signs"] = solver.reconstruct_arm(
                sh, px[idx[1]], px[idx[2]], geo["world"][idx], arm["signs"],
                (cal["upper"][side], cal["fore"][side]), geo["focal"], geo["center"], blend=True)
            pts = (np.stack([sh, elbow, wrist]) - geo["frame"][1]) @ geo["frame"][0].T
            # Swap guard: a wrist cannot teleport. Reject the frame and hold instead.
            if arm["wrist"] is None or np.linalg.norm(pts[2] - arm["wrist"]) <= args.max_jump:
                arm["wrist"] = pts[2]
                arm["pts"] = arm["euro"](pts, t)
                sol = arm["ik"].solve(*arm["pts"], q_prev_deg=arm["angles"][:2])
        if sol is not None:
            arm["state"], arm["target"], arm["t_ok"] = "TRACKING", list(sol), t
        elif t - arm["t_ok"] < args.hold_s:
            arm["state"] = "HOLD"
        else:
            arm["state"], arm["target"], arm["wrist"], arm["pts"] = "LOST", list(joints.ARM_REST), None, None
            arm["euro"].reset()
        step = args.max_step_deg * (LOST_STEP_SCALE if arm["state"] == "LOST" else 1.0)
        arm["angles"] = [slew(a, g, step, dt) for a, g in zip(arm["angles"], arm["target"])]


def track_legs(legs, lm, cal, t, dt, size, args):
    """
    One frame of leg tracking. Legs are TRACKING only when hips, knees and ankles of both legs
    are visible and inside the image, otherwise HOLD (compose_pulses then fades them to the
    stand pose). Hips, knees and ankles are rebuilt from the shoulders down a chain of
    calibrated bones, expressed in the hip body frame, turned into bone direction angles and
    passed through the balance layer.
    """
    geo = geometry(lm, cal, size, args)
    idx = [i for s in "LR" for i in LEG_IDX[s]]
    frame = None
    if (geo is not None and geo["vis"][idx].min() > LEG_VIS
            and geo["img"][idx, :2].min() >= 0.0 and geo["img"][idx, :2].max() <= 1.0):
        cam = {}
        for s in "LR":
            chain = [ARM_IDX[s][0], *LEG_IDX[s]]
            cam[s], legs["signs"][s] = solver.reconstruct_chain(
                geo["sh"][s], geo["px"][chain[1:]], geo["world"][chain], legs["signs"][s],
                (cal["torso"][s], cal["thigh"][s], cal["shin"][s]), geo["focal"], geo["center"], blend=True)
        frame = solver.body_frame(geo["sh"]["L"], geo["sh"]["R"], cam["L"][0], cam["R"][0])
    if frame is None:
        legs["state"], legs["lifted"], legs["pts"] = "HOLD", None, None
        legs["euro"].reset()
        return
    cam = np.stack(cam["L"] + cam["R"])  # hip, knee, ankle for L then R
    pts = legs["euro"]((cam - frame[1]) @ frame[0].T, t)
    raw = {s: solver.leg_angles(pts[k + 1] - pts[k], pts[k + 2] - pts[k + 1], s) for s, k in (("L", 0), ("R", 3))}
    legs["lifted"] = solver.lifted_foot({"L": pts[2][2], "R": pts[5][2]},
                                        0.5 * sum(cal[b][s] for b in ("thigh", "shin") for s in "LR"))
    legs["target"] = solver.balance(raw, legs["lifted"])
    legs["state"] = "TRACKING"
    legs["pts"] = (cam - geo["frame"][1]) @ geo["frame"][0].T  # shoulder frame, for the twin's skeleton view
    for s in "LR":
        legs["angles"][s] = [slew(a, g, LEG_SLEW_DEG, dt) for a, g in zip(legs["angles"][s], legs["target"][s])]


def track_hands(hands, lm):
    """Open (1.0) / closed (0.0) per hand from the index to wrist span, with hysteresis."""
    if lm is None:
        return
    for s, (sh, el, wr, index) in (("L", (11, 13, 15, 19)), ("R", (12, 14, 16, 20))):
        if lm[2][[wr, index]].min() < 0.5:
            continue
        ratio = np.linalg.norm(lm[1][index] - lm[1][wr]) / max(np.linalg.norm(lm[1][el] - lm[1][sh]), 1e-4)
        hands[s] = 1.0 if ratio > HAND_OPEN else 0.0 if ratio < HAND_CLOSE else hands[s]


def compose_pulses(body, enabled, dt, hands=False):
    """
    Target pulses for every body servo (plus 17, 18 with hands). Each group has a gain that
    eases over EASE_S between 0 (stand pose) and 1 (tracked pose), so enabling a group, or
    legs dropping out of view, never jumps. Fading scales the joint angles, so the flat foot
    relation holds all the way.
    """
    active = {"arm": enabled["arm"], "leg": enabled["leg"] and body["legs"]["state"] == "TRACKING"}
    for group, on in active.items():
        step = dt / EASE_S
        body["gain"][group] = min(max(body["gain"][group] + (step if on else -step), 0.0), 1.0)
    g_arm, g_leg = body["gain"]["arm"], body["gain"]["leg"]
    target = {}
    for s in "LR":
        arm = [rest + g_arm * (a - rest) for a, rest in zip(body["arms"][s]["angles"], joints.ARM_REST)]
        target.update(joints.arm_pulses(s, *arm))
        target.update(joints.leg_pulses(s, *(g_leg * a for a in body["legs"]["angles"][s])))
        if hands:
            target[joints.sid(s, "hand")] = joints.pulse(joints.sid(s, "hand"), body["hands"][s])
    return target


def group_state(body, enabled, group):
    if not enabled[group]:
        return "OFF"
    if group == "leg":
        return body["legs"]["state"]
    states = {a["state"] for a in body["arms"].values()}
    return "TRACKING" if "TRACKING" in states else "HOLD" if "HOLD" in states else "LOST"


def twin_landmarks(body):
    """Filtered joints as a 33 landmark set (shoulder frame) for the twin's skeleton view."""
    pts, vis = np.zeros((33, 3)), np.zeros(33)
    for side, arm in body["arms"].items():
        if arm["pts"] is None:
            return None, None, False
        sh, hip = ARM_IDX[side][0], 23 if side == "L" else 24
        pts[list(ARM_IDX[side])], vis[list(ARM_IDX[side])] = arm["pts"], 1.0
        pts[hip] = arm["pts"][0] * (1.0, 0.6, 1.0) + (0.0, 0.0, -0.5)
        pts[sh + 6:sh + 11:2] = arm["pts"][2]  # hand landmarks collapse onto the wrist
    legs = body["legs"]["pts"]
    if legs is not None:
        for k, side in ((0, "L"), (3, "R")):
            pts[list(LEG_IDX[side])], vis[list(LEG_IDX[side])] = legs[k:k + 3], 1.0
            pts[LEG_IDX[side][2] + 2::2][:2] = legs[k + 2]  # heel and toe collapse onto the ankle
    pts[0] = (0.0, 0.0, 0.2)
    return pts + (0.0, 0.0, 0.25), vis, legs is not None


def start_api_sender(url, hz=30.0):
    """Background sender to robot_api: servo/set_many for box["targets"], and box["matrix"] =
    (path, kwargs) for the LED matrix. Latest values only, never blocks the loop."""
    from robot_api.client import HttpBackend

    backend, box = HttpBackend(url=url, timeout=2.0), {"targets": None, "matrix": None, "error": None}

    def loop():
        while True:
            t0 = time.monotonic()
            targets, box["targets"] = box["targets"], None
            matrix, box["matrix"] = box["matrix"], None
            try:
                if targets:
                    backend.call("servo/set_many", targets=targets, wait=False)
                if matrix:
                    backend.call(matrix[0], **matrix[1])
                box["error"] = None if targets or matrix else box["error"]
            except Exception as e:  # keep tracking, show the error on the HUD
                box["error"] = str(e)[:60]
            time.sleep(max(0.0, 1.0 / hz - (time.monotonic() - t0)))

    threading.Thread(target=loop, daemon=True).start()
    return box


def draw_hud(frame, lm, body, enabled, pulses, status):
    """Mirrored view with the skeleton, group states, and per limb angles and pulses."""
    view = cv2.flip(frame, 1)
    h, w = view.shape[:2]
    arms, legs = body["arms"], body["legs"]
    leg_state = group_state(body, enabled, "leg")
    if lm is not None:
        px = lm[0][:, :2] * (w, h)
        px[:, 0] = w - 1 - px[:, 0]
        pt = lambda i: (int(px[i][0]), int(px[i][1]))  # noqa: E731
        cv2.line(view, pt(11), pt(12), (220, 220, 220), 1, cv2.LINE_AA)
        limbs = [(ARM_IDX[s], COLORS[arms[s]["state"] if enabled["arm"] else "OFF"]) for s in "LR"]
        if legs["state"] == "TRACKING":
            limbs += [((ARM_IDX[s][0], *LEG_IDX[s]), COLORS[leg_state]) for s in "LR"]
        for chain, color in limbs:
            for a, b in zip(chain, chain[1:]):
                cv2.line(view, pt(a), pt(b), color, 1 if a < 13 and b > 22 else 3, cv2.LINE_AA)
            for i in chain[1:]:
                cv2.circle(view, pt(i), 4, (255, 255, 255), -1)
    lift = f"  {legs['lifted']} FOOT LIFT (clamped)" if legs["lifted"] else ""
    top = [(status, (255, 255, 255)),
           (f"ARMS: {group_state(body, enabled, 'arm')}   LEGS: {leg_state}{lift}   (a / l toggle, x stop)", (255, 255, 255))]
    bottom = []
    for s in "LR":
        pitch, roll, elbow = arms[s]["angles"]
        bottom.append((f"{s} arm {arms[s]['state']:8s} pitch {pitch:5.0f} roll {roll:5.0f} elbow {elbow:4.0f}  "
                       + " ".join(f"S{i}:{pulses[i]}" for i in joints.ARM_SERVOS[s]), COLORS[arms[s]["state"] if enabled["arm"] else "OFF"]))
    for s in "LR":
        hip, roll, knee = (body["gain"]["leg"] * a for a in legs["angles"][s])
        bottom.append((f"{s} leg hip {hip:4.0f} roll {roll:4.0f} knee {knee:4.0f}  "
                       + " ".join(f"S{i}:{pulses[i]}" for i in joints.LEG_SERVOS[s]), COLORS[leg_state]))
    view[:46] //= 3  # dark strips behind the text
    view[h - 84:] //= 3
    for i, (text, color) in enumerate(top):
        cv2.putText(view, text, (8, 18 + 20 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)
    for i, (text, color) in enumerate(bottom):
        cv2.putText(view, text, (8, h - 68 + 20 * i), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)
    return view


def probe(args):
    """
    Sign check. Moves one servo at a time away from the stand pose and back, printing what a
    correct sign looks like. Default: the twin, +/-20 degrees, every servo. --probe-hw: the real
    robot through robot_api, +/-5 degrees inside the safe caps, after a typed confirmation.
    """
    hw = args.probe_hw
    groups = ["arm"] + (["leg"] if not hw or args.enable_legs else []) + (["hand"] if args.hands else [])
    ids = sorted(i for g in groups for i in joints.GROUPS[g])
    if hw:
        if not args.api_url:
            raise SystemExit("--probe-hw needs --api-url")
        from robot_api.client import HttpBackend
        backend = HttpBackend(url=args.api_url, timeout=10.0)
        print(f"About to move servos {ids} on the REAL robot at {args.api_url}, one at a time, by 5 degrees.\n"
              "The robot must already be standing in the stand pose, with a hand ready to catch it.")
        if input("Type MOVE to continue: ").strip() != "MOVE":
            raise SystemExit("aborted, nothing was sent")
        send = lambda i, p: backend.call("servo/set", servo=i, pulse=p, ms=600)  # noqa: E731
    else:
        from digital_twin import TonyPiDigitalTwin
        twin = TonyPiDigitalTwin(port=args.twin_port).start()
        print(f"[twin] {twin.url}  (watch the model, each move is described below)")
        twin.update_servos(joints.stand(("leg", "arm", "hand")))
        time.sleep(3.0)
        send = lambda i, p: twin.update_servos({i: p}, label=f"probe S{i}")  # noqa: E731
    for i in ids:
        j = joints.JOINTS[i]
        for delta in ((1.0,) if j["group"] == "hand" else (5.0, -5.0) if hw else (20.0, -20.0)):
            target = joints.pulse(i, j["neutral"] + delta, safe=hw)
            if target == j["stand"]:
                continue  # capped (for example a knee cannot bend backward inside the safe caps)
            what = j["positive"] if delta > 0 else "the opposite of: " + j["positive"]
            flag = "" if j["verified"] else "   [sign UNVERIFIED]"
            print(f"S{i:<2d} {j['name']:21s} {delta:+5.0f}  pulse {j['stand']} -> {target}   expect: {what}{flag}")
            if hw:  # robot_api paces the move itself (600 ms each way)
                send(i, target)
                time.sleep(1.0)
                send(i, j["stand"])
                time.sleep(0.5)
                continue
            for k in list(range(1, 11)) + [10] * 12 + list(range(9, -1, -1)):  # ease out, hold, ease back
                send(i, int(round(j["stand"] + (target - j["stand"]) * k / 10.0)))
                time.sleep(0.05)
    print("probe done")


def main():
    args = parse_args()
    if args.probe_sign or args.probe_hw:
        return probe(args)
    headless = args.headless_test > 0
    calib = calibration.new_state(calibration.load())
    persist = args.source.isdigit()  # replaying a file never overwrites your calibration.json
    detect, close_landmarker = make_landmarker()
    sim = GC0308HardwareSimulator() if args.sim_camera else None
    twin = None
    if not args.no_twin:
        from digital_twin import TonyPiDigitalTwin
        twin = TonyPiDigitalTwin(headless=headless, port=0 if headless else args.twin_port).start()
        print(f"[twin] {twin.url}")
    api = start_api_sender(args.api_url) if args.api_url else None

    body = new_body(args)
    # Legs follow in the twin by default. On hardware they stay off unless --enable-legs is passed.
    enabled = {"arm": True, "leg": not args.api_url or args.enable_legs}
    all_groups = ("leg", "arm", "hand") if args.hands else ("leg", "arm")
    pulses = joints.stand(all_groups)
    paused, bar = False, None
    record = {"image": [], "world": [], "vis": [], "t": []}
    stats = {"min": dict(pulses), "max": dict(pulses), "L arm": {}, "R arm": {}, "legs": {}}
    n, fps, t_prev, ts_prev, wall_prev, wall_start = 0, 0.0, None, -1, time.monotonic(), time.monotonic()

    try:
        for frame, t in frames(args.source, realtime=not headless):
            if sim is not None:
                frame, _ = sim.apply(frame)
            h, w = frame.shape[:2]
            dt = 1.0 / 30.0 if t_prev is None else max(t - t_prev, 1e-3)
            ts_prev = max(ts_prev + 1, int(t * 1000))  # VIDEO mode needs strictly increasing timestamps
            lm = detect(frame, ts_prev)
            t_prev, n = t, n + 1
            if args.record_landmarks:
                blank = np.full((33, 3), np.nan)
                for key, val in zip(("image", "world", "vis", "t"), (*(lm or (blank, blank, blank[:, 0])), t)):
                    record[key].append(val)

            if lm is None:
                calibration.update(calib, None, None, None, t)
            elif calibration.update(calib, lm[0][:, :2] * (w, h), lm[1], lm[2], t) and persist:
                calibration.save(calib["cal"])
            cal, done = calib["cal"], calibration.progress(calib)
            if api is not None and not calib["locked"]["arm"] and bar != calibration.matrix_rows(done):
                bar = calibration.matrix_rows(done)  # LED matrix as a progress bar
                api["matrix"] = ("sensors/matrix/rows", {"rows": bar})
            elif api is not None and calib["locked"]["arm"] and bar != "check":
                bar, api["matrix"] = "check", ("sensors/matrix/icon", {"name": "check"})

            if paused:
                pulses = joints.stand(all_groups)  # emergency: every servo straight to the stand pose
            else:
                track_arms(body["arms"], lm, cal, t, dt, (w, h), args)
                track_legs(body["legs"], lm, cal, t, dt, (w, h), args)
                if args.hands:
                    track_hands(body["hands"], lm)
                pulses = joints.rate_limit(pulses, compose_pulses(body, enabled, dt, args.hands))
            if twin is not None:
                twin.update_servos(pulses, label="arm_mimic")
                pts, vis, has_legs = twin_landmarks(body) if not paused else (None, None, False)
                twin.update_landmarks(pts, vis, legs=has_legs)
            if api is not None:  # only the groups that are on (or still fading out). Paused sends everything to stand
                live = [g for g in ("arm", "leg") if paused or enabled[g] or body["gain"][g] > 0.0] + (["hand"] if args.hands else [])
                api["targets"] = {str(i): pulses[i] for g in live for i in joints.GROUPS[g]}

            for i, p in pulses.items():
                stats["min"][i], stats["max"][i] = min(stats["min"][i], p), max(stats["max"][i], p)
            for key, state in (("L arm", body["arms"]["L"]["state"]), ("R arm", body["arms"]["R"]["state"]),
                               ("legs", body["legs"]["state"])):
                stats[key][state] = stats[key].get(state, 0) + 1
            wall = time.monotonic()
            fps, wall_prev = 0.9 * fps + 0.1 / max(wall - wall_prev, 1e-6), wall
            if headless:
                if n >= args.headless_test:
                    break
                continue

            status = f"FPS {fps:4.1f}  " + ("CALIBRATED" if calib["locked"]["arm"] else f"CALIBRATING {done}/{calibration.LOCK_SAMPLES}"
                                            if done else "UNCALIBRATED (face camera, limbs straight, hold still)")
            status += "  legs cal " + ("ok" if calib["locked"]["leg"] else f"{calibration.progress(calib, 'leg')}/{calibration.LOCK_SAMPLES}")
            if cal.get("shoulder_px") and lm is not None:
                now_px = np.linalg.norm((lm[0][11, :2] - lm[0][12, :2]) * (w, h))
                status += f"  dist x{cal['shoulder_px'] / max(now_px, 1.0):.2f}"
            status += "  PAUSED (x resumes)" if paused else ""
            status += f"  API {api['error'] or 'on'}" if api is not None else ""
            cv2.imshow("body mimic", draw_hud(frame, lm, body, enabled, pulses, status))
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                break
            if key == ord("c"):  # discard everything and calibrate from scratch
                calib = calibration.new_state()
            if key == ord("a"):
                enabled["arm"] = not enabled["arm"]
            if key == ord("l"):
                enabled["leg"] = not enabled["leg"]
            if key == ord("x"):  # emergency: straight to the stand pose, tracking restarts from rest
                paused = not paused
                body = new_body(args)
    finally:
        close_landmarker()
        cv2.destroyAllWindows()
        if twin is not None:
            twin.close()
        if calib["locked"]["arm"] and persist:
            calibration.save(calib["cal"])  # keep the slow refinement
        if args.record_landmarks and record["t"]:
            np.savez_compressed(args.record_landmarks, **{k: np.array(v) for k, v in record.items()})
            print(f"[record] {len(record['t'])} frames -> {args.record_landmarks}")

    if headless:
        locked = calib["locked"]
        print(f"frames {n}  avg fps {n / max(time.monotonic() - wall_start, 1e-6):.1f}  "
              f"arms {'calibrated' if locked['arm'] else 'UNCALIBRATED'}  legs {'calibrated' if locked['leg'] else 'UNCALIBRATED'}")
        for key in ("L arm", "R arm", "legs"):
            print(f"{key:5s} frames per state: " + "  ".join(f"{k} {v}" for k, v in sorted(stats[key].items())))
        for i in sorted(pulses):
            j = joints.JOINTS[i]
            print(f"S{i:<2d} {j['name']:21s} stand {j['stand']:4d}  min {stats['min'][i]:4d}  max {stats['max'][i]:4d}")


if __name__ == "__main__":
    main()
