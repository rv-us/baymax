"""Full body tests: joint table, leg kinematics, flat foot, balance layer, gating, leg calibration."""
import math

import numpy as np
import pytest

import arm_mimic
import calibration
import joints
import solver
from retarget import arm_fk

SIZE = (640, 480)
HALF_SHOULDER, HALF_HIP, TORSO_DROP, THIGH, SHIN, UPPER, FORE = 0.18, 0.09, 0.50, 0.42, 0.40, 0.29, 0.25
TORSO = math.hypot(HALF_SHOULDER - HALF_HIP, TORSO_DROP)  # shoulder to hip on the same side
CAL = {"calibrated": True, "legs_calibrated": True, "shoulder_m": 2 * HALF_SHOULDER, "shoulder_px": None,
       **{bone: {"L": v, "R": v} for bone, v in (("upper", UPPER), ("fore", FORE), ("torso", TORSO),
                                                   ("thigh", THIGH), ("shin", SHIN))}}
ARMS_DOWN = {"L": (0, 0, 0), "R": (0, 0, 0)}
STRAIGHT = {"L": (0, 0, 0), "R": (0, 0, 0)}
ARGS = arm_mimic.parse_args([])
LEG_IDS = [i for s in "LR" for i in joints.LEG_SERVOS[s]]


def rot_x(deg):
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]])


def rot_y(deg):
    c, s = math.cos(math.radians(deg)), math.sin(math.radians(deg))
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


def body_landmarks(legs=STRAIGHT, arms=ARMS_DOWN, distance=3.0, lean_deg=0.0):
    """
    MediaPipe style (image, world, vis) of a subject facing the camera. legs = {side: (hip_pitch,
    hip_roll, knee)} in joints.py conventions, built with the robot's own chain (roll about x,
    then pitch and knee about y), arms = {side: (pitch, roll, flex)} as in retarget.arm_fk.
    lean_deg tips the torso forward about the hips (a bow), legs stay put in the world.
    """
    body = np.zeros((33, 3))  # body frame: x forward, y left, z up, origin at mid hip
    lean = rot_y(lean_deg)
    for side, y in (("L", 1.0), ("R", -1.0)):
        sh, el, wr = arm_mimic.ARM_IDX[side]
        hip, knee, ankle = arm_mimic.LEG_IDX[side]
        upper, fore = arm_fk(*np.radians(arms[side]))
        body[sh] = lean @ np.array([0.0, y * HALF_SHOULDER, TORSO_DROP])
        body[el] = body[sh] + UPPER * (lean @ upper)
        body[wr] = body[el] + FORE * (lean @ fore)
        pitch, roll, flex = legs[side]
        hip_rot = rot_x(y * roll) @ rot_y(-pitch)  # URDF: + roll swings left, - pitch swings forward
        body[hip] = (0.0, y * HALF_HIP, 0.0)
        body[knee] = body[hip] + THIGH * (hip_rot @ (0, 0, -1))
        body[ankle] = body[knee] + SHIN * (hip_rot @ rot_y(flex) @ (0, 0, -1))
    cam = np.stack([body[:, 1], -body[:, 2], -body[:, 0]], axis=1)  # x right, y down, z away
    px = 0.9 * SIZE[0] * (cam[:, :2] + (0.0, 0.1)) / (cam[:, 2:] + distance) + (SIZE[0] / 2, SIZE[1] / 2)
    return np.concatenate([px / SIZE, cam[:, 2:]], axis=1), cam, np.ones(33)


def run(lm, frames=90, body=None, enabled=None, pulses=None, t=0.0):
    """Feeds the same landmarks for `frames` frames through the real tracking and output path."""
    body = body or arm_mimic.new_body(ARGS)
    enabled = enabled or {"arm": True, "leg": True}
    pulses = pulses or joints.stand()
    biggest = 0
    for _ in range(frames):
        t += 1 / 30.0
        arm_mimic.track_arms(body["arms"], lm, CAL, t, 1 / 30.0, SIZE, ARGS)
        arm_mimic.track_legs(body["legs"], lm, CAL, t, 1 / 30.0, SIZE, ARGS)
        new = joints.rate_limit(pulses, arm_mimic.compose_pulses(body, enabled, 1 / 30.0))
        biggest = max(biggest, max(abs(new[i] - pulses[i]) for i in LEG_IDS))
        pulses = new
    return body, pulses, biggest, t


# ------------------------------------------------------------------ joint table
def test_joint_table_matches_repo_constants():
    from digital_twin import mapping
    from mediapose_est import SERVO_LIMITS, STAND_PULSES

    assert sorted(joints.JOINTS) == list(range(1, 19))
    assert len(joints.BODY_IDS) == 16 and joints.GROUPS["hand"] == (17, 18)
    for i, j in joints.JOINTS.items():
        assert j["stand"] == STAND_PULSES[i] and j["side"] == ("L" if i <= 8 or i == 18 else "R")
        lo, hi = SERVO_LIMITS[i]
        assert lo <= j["min"] <= j["stand"] <= j["max"] <= hi
        if i <= 16:
            assert mapping.SERVO_JOINTS[i].label == j["name"]  # robot_api joint names
    for i in (6, 7, 14, 15):  # robot_api/servo/limits.py bracket protection
        assert (joints.JOINTS[i]["min"], joints.JOINTS[i]["max"]) == (125, 875)
    for kind in ("ankle_roll", "ankle_pitch", "knee", "hip_pitch", "hip_roll"):  # mirrored sides
        assert joints.JOINTS[joints.sid("L", kind)]["sign"] == -joints.JOINTS[joints.sid("R", kind)]["sign"]
    assert joints.LEG_STEP < joints.ARM_STEP and not joints.SAFE_CAPS_VERIFIED


def test_no_pulse_ever_leaves_its_limits():
    from mediapose_est import SERVO_LIMITS

    bad = [float("nan"), float("inf"), float("-inf"), 1e12, -1e12]
    for i, j in joints.JOINTS.items():
        for angle in list(np.arange(-400.0, 401.0, 2.5)) + bad:
            for safe in (True, False):
                p = joints.pulse(i, angle, safe)
                assert isinstance(p, int) and j["min"] <= p <= j["max"], (i, angle, p)
                assert SERVO_LIMITS[i][0] <= p <= SERVO_LIMITS[i][1]
            if j["safe_min"] is not None and j["group"] == "leg":  # safe caps are tighter than the hard limits
                assert abs(joints.pulse(i, angle) - j["stand"]) <= joints.PPD * max(abs(j["safe_min"]), abs(j["safe_max"])) + 1
    body = arm_mimic.new_body(ARGS)
    body["gain"]["leg"] = 1.0
    for junk in bad + [500.0, -500.0]:
        for s in "LR":
            body["arms"][s]["angles"] = [junk] * 3
            body["legs"]["angles"][s] = [junk] * 3
            body["hands"][s] = junk
        for i, p in arm_mimic.compose_pulses(body, {"arm": True, "leg": True}, 0.0, hands=True).items():
            assert joints.JOINTS[i]["min"] <= p <= joints.JOINTS[i]["max"], (junk, i, p)
    assert solver.balance({"L": (float("nan"),) * 3, "R": (float("inf"),) * 3})["L"] == (0.0, 0.0, 0.0)


# ------------------------------------------------------------------ leg kinematics
def test_body_frame_and_leg_angles():
    _, cam, _ = body_landmarks()
    rot, origin = solver.body_frame(cam[11], cam[12], cam[23], cam[24])
    assert np.allclose(rot @ rot.T, np.eye(3), atol=1e-9) and np.allclose(origin, 0.5 * (cam[23] + cam[24]))
    assert np.allclose(rot, [[0, 0, -1], [1, 0, 0], [0, -1, 0]])  # forward faces the camera, left is +x, up is -y
    assert solver.body_frame(cam[11], cam[12], cam[23], cam[23]) is None
    for side, y in (("L", 1.0), ("R", -1.0)):
        for want in ((0, 0, 0), (30, 0, 60), (-10, 12, 0), (25, 8, 40)):
            hip = rot_x(y * want[1]) @ rot_y(-want[0])
            got = solver.leg_angles(hip @ (0, 0, -1), hip @ rot_y(want[2]) @ (0, 0, -1), side)
            assert np.allclose(got, want, atol=1e-6), (side, want, got)
    # Sign convention in plain vectors (x forward, y left, z up):
    assert solver.leg_angles(np.array([0.5, 0, -1.0]), np.array([0.5, 0, -1.0]), "L")[0] > 0   # thigh forward: + hip pitch
    assert solver.leg_angles(np.array([0, 0.3, -1.0]), np.array([0, 0.3, -1.0]), "L")[1] > 0   # left thigh out to the left: + roll
    assert solver.leg_angles(np.array([0, -0.3, -1.0]), np.array([0, -0.3, -1.0]), "R")[1] > 0  # right thigh out to the right: + roll
    assert solver.leg_angles(np.array([0, 0, -1.0]), np.array([-0.5, 0, -1.0]), "L")[2] > 0    # shin swings back: + knee


def test_standing_pose_maps_to_stand_pulses():
    body, pulses, _, _ = run(body_landmarks())
    assert body["legs"]["state"] == "TRACKING" and body["gain"]["leg"] == 1.0
    for i in joints.BODY_IDS:
        assert abs(pulses[i] - joints.JOINTS[i]["stand"]) <= 4, (joints.JOINTS[i]["name"], pulses[i])


def test_squat_pulses_and_flat_foot():
    """
    Squat with feet planted: thighs 30 degrees forward, knees flexed 60. Sign convention under
    test: ankle_pitch (+ toes up) = knee - hip_pitch = 30, ankle_roll = -hip_roll, which must
    leave the twin's feet exactly level.
    """
    k = joints.PPD
    assert joints.leg_angles_flat_foot("L", 30, 5, 60) == (30, 5, 60, 30, -5)
    want = {3: 500 + 60 * k, 11: 500 - 60 * k, 4: 600 - 30 * k, 12: 400 + 30 * k,
            2: 390 - 30 * k, 10: 610 + 30 * k, 5: 500, 13: 500, 1: 500, 9: 500}
    assert joints.leg_pulses("L", 30, 0, 60) == {i: round(want[i]) for i in joints.LEG_SERVOS["L"]}
    assert joints.leg_pulses("R", 30, 0, 60) == {i: round(want[i]) for i in joints.LEG_SERVOS["R"]}

    squat = {"L": (30, 0, 60), "R": (30, 0, 60)}
    body, pulses, _, _ = run(body_landmarks(squat), frames=150)
    for i in LEG_IDS:
        assert abs(pulses[i] - want[i]) <= 12, (joints.JOINTS[i]["name"], pulses[i], want[i])  # within 3 degrees
    for i in joints.GROUPS["arm"]:
        assert abs(pulses[i] - joints.JOINTS[i]["stand"]) <= 12

    # The digital twin's own kinematics agree: feet level, thighs forward, shins back.
    from digital_twin import TonyPiDigitalTwin
    twin = TonyPiDigitalTwin(headless=True, port=0)
    try:
        for legs in (squat, {"L": (20, 10, 35), "R": (5, 3, 50)}):
            twin.update_servos({**joints.leg_pulses("L", *legs["L"]), **joints.leg_pulses("R", *legs["R"])})
            for side, y in (("l", 1.0), ("r", -1.0)):
                hip_pitch, hip_roll, knee = legs[side.upper()]
                foot_up = np.array(twin.get_link_rotation(f"{side}_foot")) @ (0, 0, 1)
                assert math.degrees(math.acos(min(1.0, foot_up[2]))) < 0.5, (side, foot_up)  # sole flat
                thigh = np.array(twin.get_link_rotation(f"{side}_thigh")) @ (0, 0, -1)
                shin = np.array(twin.get_link_rotation(f"{side}_shin")) @ (0, 0, -1)
                assert np.allclose(solver.leg_angles(thigh, shin, side.upper()), (hip_pitch, hip_roll, knee), atol=0.5)
                assert thigh[0] > 0 and shin[0] < thigh[0] and (hip_roll == 0 or y * thigh[1] > 0)  # forward, back, outward
    finally:
        twin.close()


def test_mirror_symmetry_about_the_stand_pose():
    rng = np.random.default_rng(5)
    for _ in range(200):
        angles = rng.uniform((-10, -5, 0), (40, 15, 60))
        left, right = joints.leg_pulses("L", *angles), joints.leg_pulses("R", *angles)
        for a, b in zip(joints.LEG_SERVOS["L"], joints.LEG_SERVOS["R"]):
            assert (left[a] - joints.JOINTS[a]["stand"]) == -(right[b] - joints.JOINTS[b]["stand"]), angles
    # Through the whole pipeline: a left leg pose and its mirror image on the right.
    pose, rest = (20, 6, 40), (0, 0, 0)
    _, p_left, _, _ = run(body_landmarks({"L": pose, "R": rest}), frames=150)
    _, p_right, _, _ = run(body_landmarks({"L": rest, "R": pose}), frames=150)
    for a, b in zip(joints.LEG_SERVOS["L"], joints.LEG_SERVOS["R"]):
        d_left, d_right = p_left[a] - joints.JOINTS[a]["stand"], p_right[b] - joints.JOINTS[b]["stand"]
        assert abs(d_left + d_right) <= 3, (joints.JOINTS[a]["name"], d_left, d_right)
    assert abs(p_left[3] - 500) > 60 and abs(p_left[11] - 500) < 30  # only the posed leg moved


# ------------------------------------------------------------------ balance and safety
def test_balance_layer_clamps():
    # A bow at the waist reads as thighs far forward of the torso with straight knees. The robot
    # has no waist, so hip pitch is held near knee / 2 instead of throwing the hips back.
    out = solver.balance({"L": (70, 0, 0), "R": (70, 0, 0)})
    assert out["L"][0] == solver.BALANCE_HIP_MARGIN and out["L"][2] == 0
    # Safe caps: knee 60, hip roll 15.
    out = solver.balance({"L": (60, 40, 120), "R": (60, 40, 120)})
    assert out["R"] == (40.0, 15.0, 60.0)
    # A lifted left foot is a single leg stance request: small change only, never a raised leg.
    out = solver.balance({"L": (80, 20, 100), "R": (0, 0, 5)}, lifted="L")
    assert out["R"] == (0.0, 0.0, 5.0)
    assert out["L"][2] == 5 + solver.LIFT_KNEE and out["L"][0] <= solver.LIFT_HIP and out["L"][1] == 0
    assert solver.lifted_foot({"L": -0.60, "R": -0.80}, 0.8) == "L"
    assert solver.lifted_foot({"L": -0.80, "R": -0.60}, 0.8) == "R"
    assert solver.lifted_foot({"L": -0.80, "R": -0.78}, 0.8) is None


def test_lifted_foot_and_bow_through_the_pipeline():
    body, pulses, _, _ = run(body_landmarks({"L": (70, 0, 100), "R": (0, 0, 0)}), frames=150)
    assert body["legs"]["lifted"] == "L"
    assert abs(pulses[3] - 500) <= joints.PPD * solver.LIFT_KNEE + 2   # left knee: small change only
    assert abs(pulses[4] - 600) <= joints.PPD * (solver.LIFT_HIP + solver.BALANCE_HIP_MARGIN) + 2
    assert all(abs(pulses[i] - joints.JOINTS[i]["stand"]) <= 6 for i in joints.LEG_SERVOS["R"])
    body, pulses, _, _ = run(body_landmarks(lean_deg=50), frames=150)  # bowing with straight legs
    assert body["legs"]["state"] == "TRACKING"
    for i in (4, 12):
        assert abs(pulses[i] - joints.JOINTS[i]["stand"]) <= joints.PPD * solver.BALANCE_HIP_MARGIN + 2


def test_legs_hold_when_out_of_view_and_ease_without_jumps():
    squat = body_landmarks({"L": (30, 0, 60), "R": (30, 0, 60)})
    body, pulses, step_in, t = run(squat, frames=150)
    assert body["legs"]["state"] == "TRACKING" and abs(pulses[3] - 750) <= 12
    assert step_in <= joints.LEG_STEP

    cut = (squat[0].copy(), squat[1], squat[2])
    cut[0][[27, 28], 1] = 1.05  # ankles below the bottom edge of the image
    body, pulses, step_out, t = run(cut, frames=60, body=body, pulses=pulses, t=t)
    assert body["legs"]["state"] == "HOLD" and body["gain"]["leg"] == 0.0
    assert step_out <= joints.LEG_STEP and all(pulses[i] == joints.JOINTS[i]["stand"] for i in LEG_IDS)
    assert arm_mimic.group_state(body, {"arm": True, "leg": True}, "leg") == "HOLD"

    dim = (squat[0], squat[1], squat[2].copy())
    dim[2][25] = 0.5  # one knee under 0.6 visibility
    body, _, _, t = run(dim, frames=5, body=body, pulses=pulses, t=t)
    assert body["legs"]["state"] == "HOLD"

    # The gain takes EASE_S to fade, so nothing jumps even before the rate limit.
    body = arm_mimic.new_body(ARGS)
    body["legs"]["state"], body["legs"]["angles"] = "TRACKING", {"L": [30, 0, 60], "R": [30, 0, 60]}
    gains = []
    for _ in range(20):
        arm_mimic.compose_pulses(body, {"arm": True, "leg": True}, 1 / 30.0)
        gains.append(body["gain"]["leg"])
    assert gains[0] == pytest.approx(1 / 15) and gains[13] < 1.0 and gains[15] == 1.0

    # Legs disabled (the default with --api-url): tracked but never commanded, HUD says OFF.
    body, pulses, _, _ = run(squat, frames=90, enabled={"arm": True, "leg": False})
    assert all(pulses[i] == joints.JOINTS[i]["stand"] for i in LEG_IDS)
    assert arm_mimic.group_state(body, {"arm": True, "leg": False}, "leg") == "OFF"
    # Arms disabled: arms ease to stand.
    arms_up = body_landmarks(arms={"L": (-90, 0, 0), "R": (-90, 0, 0)})
    body, pulses, _, t = run(arms_up, frames=60)
    assert abs(pulses[8] - 350) <= 12
    body, pulses, _, _ = run(arms_up, frames=60, body=body, pulses=pulses, t=t, enabled={"arm": False, "leg": True})
    assert pulses[8] == 725 and pulses[16] == 275


def test_hands_open_close_with_hysteresis():
    hands = {"L": 0.0, "R": 0.0}
    img, world, vis = body_landmarks()
    for span, want in ((0.45, 1.0), (0.38, 1.0), (0.30, 0.0), (0.38, 0.0), (0.45, 1.0)):
        world[19] = world[15] + (0, span * UPPER, 0)
        arm_mimic.track_hands(hands, (img, world, vis))
        assert hands["L"] == want, span
    assert joints.pulse(17, 1.0) == 760 and joints.pulse(18, 1.0) == 240 and joints.pulse(18, 0.0) == 500


# ------------------------------------------------------------------ leg calibration
def test_auto_calibration_of_leg_bones():
    state, t = calibration.new_state(), 0.0

    def feed(lm, frames):
        nonlocal t
        for _ in range(frames):
            calibration.update(state, lm[0][:, :2] * SIZE, lm[1], lm[2], t)
            t += 1 / 30.0
        return {s: len(state["buf"]["leg"][s]) for s in "LR"}

    assert feed(body_landmarks({"L": (30, 0, 60), "R": (30, 0, 60)}), 30) == {"L": 0, "R": 0}   # squat: bent, out of plane
    assert feed(body_landmarks({"L": (35, 0, 0), "R": (35, 0, 0)}), 30) == {"L": 0, "R": 0}     # legs toward the camera
    low = body_landmarks()
    low[2][[25, 28]] = 0.5
    assert feed(low, 30) == {"L": 0, "R": 0} and not state["locked"]["leg"]                     # knee, ankle not visible
    feed(body_landmarks(), 70)  # standing still, arms and legs straight: everything calibrates
    assert state["locked"] == {"arm": True, "leg": True} and state["cal"]["legs_calibrated"]
    cal = state["cal"]
    for bone, truth in (("torso", TORSO), ("thigh", THIGH), ("shin", SHIN), ("upper", UPPER), ("fore", FORE)):
        for side in "LR":
            assert abs(cal[bone][side] - truth) < 0.02, (bone, side, cal[bone][side], truth)
    assert abs(cal["shoulder_m"] - 2 * HALF_SHOULDER) < 0.02


def test_probe_directions_match_the_twin():
    """What --probe-sign prints as the expected motion is what the twin's kinematics do."""
    from digital_twin import TonyPiDigitalTwin

    down, fwd = np.array([0, 0, -1.0]), np.array([1.0, 0, 0])
    # kind -> (link, vector carried by the link, component, sign of that component for a + angle on the LEFT side)
    expect = {"ankle_roll": ("foot", down, 1, +1), "ankle_pitch": ("foot", fwd, 2, +1), "knee": ("shin", down, 0, -1),
              "hip_pitch": ("thigh", down, 0, +1), "hip_roll": ("thigh", down, 1, +1), "elbow": ("forearm", down, 0, -1),
              "shoulder_roll": ("upper_arm", down, 1, +1), "shoulder_pitch": ("upper_arm", down, 0, -1)}
    twin = TonyPiDigitalTwin(headless=True, port=0, enforce_limits=False)
    try:
        for i in joints.BODY_IDS:
            j = joints.JOINTS[i]
            side, kind = j["name"].split("_", 1)
            link, vec, axis, sign = expect[kind]
            if side == "right" and kind in ("ankle_roll", "hip_roll"):
                sign = -sign  # outward is the other way on the right. Shoulder roll keeps the URDF sign on both sides
            twin.update_servos({**joints.stand(), i: joints.pulse(i, j["neutral"] + 20.0, safe=False)})
            moved = np.array(twin.get_link_rotation(f"{side[0]}_{link}")) @ vec
            assert sign * moved[axis] > 0.2, (j["name"], moved)
    finally:
        twin.close()
