"""
Synthetic 3D pose examples -> retarget -> TonyPi digital twin, compared limb by limb.

Each pose is built as 3D landmarks in the camera convention the estimator produces,
solved into joint angles, sent to the headless digital twin, and the direction every
simulated limb ends up pointing is compared with the direction of the human limb.
"""
import math

import numpy as np

from mediapose_est import JointFilter, LandmarkListWrapper, LandmarkPoint
from retarget import (ARM_IDX, LEG_IDX, L_HIP, L_SHOULDER, R_HIP, R_SHOULDER, ArmIK, arm_fk,
                      leg_angles, shoulder_pulses, elbow_pulse, pulses_to_angles, to_torso_frame)
from digital_twin import TonyPiDigitalTwin

S45 = math.sin(math.radians(45))
UPPER, FORE, THIGH, SHIN = 0.28, 0.25, 0.42, 0.40
DOWN, FWD, BACK = (0, 0, -1), (1, 0, 0), (-1, 0, 0)


def unit(v):
    v = np.asarray(v, dtype=np.float64)
    return v / np.linalg.norm(v)


def make_pose(arms, legs, yaw_deg=0.0):
    """
    arms/legs: {"L": (proximal_dir, distal_dir), "R": ...} as body-frame directions
    (x forward, y toward the subject's own left, z up). Returns camera-convention
    landmarks (x right, y down, z away) for a subject facing the camera, turned by yaw.
    """
    body = {L_SHOULDER: (0, 0.18, 0.25), R_SHOULDER: (0, -0.18, 0.25),
            L_HIP: (0, 0.10, -0.25), R_HIP: (0, -0.10, -0.25)}
    body = {k: np.array(v, dtype=np.float64) for k, v in body.items()}
    for side in "LR":
        for idx, dirs, lens in ((ARM_IDX[side], arms[side], (UPPER, FORE)),
                                (LEG_IDX[side], legs[side], (THIGH, SHIN))):
            body[idx[1]] = body[idx[0]] + lens[0] * unit(dirs[0])
            body[idx[2]] = body[idx[1]] + lens[1] * unit(dirs[1])
    c, s = math.cos(math.radians(yaw_deg)), math.sin(math.radians(yaw_deg))
    lms = [LandmarkPoint(0.0, 0.0, 0.0, 1.0) for _ in range(33)]
    for i, (f, l, u) in body.items():
        f, l = c * f - s * l, s * f + c * l
        lms[i] = LandmarkPoint(l, -u + 0.25, -f, 1.0)
    return LandmarkListWrapper(lms)


STAND = {"L": (DOWN, DOWN), "R": (DOWN, DOWN)}
ARMS_DOWN = {"L": (DOWN, DOWN), "R": (DOWN, DOWN)}
POSES = {
    "zombie": (
        {"L": (FWD, FWD), "R": (FWD, FWD)}, STAND),
    "t_pose": (
        {"L": ((0, 1, 0), (0, 1, 0)), "R": ((0, -1, 0), (0, -1, 0))}, STAND),
    # Pitch at rest (arms hang at the sides), roll 45 deg out from down, elbow bent to 90 deg.
    # With zero pitch the elbow flexes forward, so the forearms point straight ahead.
    "superhero": (
        {"L": ((0, S45, -S45), FWD), "R": ((0, -S45, -S45), FWD)}, STAND),
    # Standing on the right leg, left thigh lifted forward with the shin hanging down.
    "flamingo": (
        ARMS_DOWN, {"L": ((math.sin(math.radians(70)), 0, -math.cos(math.radians(70))), DOWN),
                    "R": (DOWN, DOWN)}),
}


def solve_pose(lms, solvers=None):
    pts = to_torso_frame(lms)
    solvers = solvers or {s: ArmIK(s) for s in "LR"}
    for _ in range(20):  # a held pose: let the frame-to-frame damping settle, as it does live
        arms = {s: solvers[s].solve(*(pts[i] for i in ARM_IDX[s])) for s in "LR"}
    legs = {s: leg_angles(pts, s) for s in "LR"}
    return pts, arms, legs


def angle_between(a, b):
    return math.degrees(math.acos(float(np.clip(np.dot(unit(a), unit(b)), -1.0, 1.0))))


def sim_limb_errors(twin, pts, arms, legs):
    """Drives the twin and returns {limb: degrees between simulated and human limb direction}."""
    twin.update_arms(l_pitch_deg=arms["L"][0], l_roll_deg=arms["L"][1], l_elbow_deg=arms["L"][2],
                     r_pitch_deg=arms["R"][0], r_roll_deg=arms["R"][1], r_elbow_deg=arms["R"][2])
    twin.update_legs(l_knee_deg=legs["L"]["knee"], l_hip_pitch_deg=legs["L"]["hip_pitch"],
                     l_ankle_deg=legs["L"]["ankle_pitch"], r_knee_deg=legs["R"]["knee"],
                     r_hip_pitch_deg=legs["R"]["hip_pitch"], r_ankle_deg=legs["R"]["ankle_pitch"])
    errors = {}
    for s in "LR":
        p = s.lower()
        for links, idx in (((f"{p}_upper_arm", f"{p}_forearm"), ARM_IDX[s]),
                           ((f"{p}_thigh", f"{p}_shin"), LEG_IDX[s])):
            for link, (a, b) in zip(links, ((idx[0], idx[1]), (idx[1], idx[2]))):
                sim_dir = np.array(twin.get_link_rotation(link)) @ np.array([0.0, 0.0, -1.0])
                errors[link] = angle_between(sim_dir, pts[b] - pts[a])
        foot_z = np.array(twin.get_link_rotation(f"{p}_foot")) @ np.array([0.0, 0.0, 1.0])
        errors[f"{p}_foot_tilt"] = angle_between(foot_z, (0, 0, 1))
    return errors


def test_fk_matches_simulator():
    twin = TonyPiDigitalTwin(gui=False, headless=True, port=0, enforce_limits=False)
    worst = 0.0
    for pitch, roll, flex in ((0, 0, 0), (-90, 0, 40), (-30, 70, 90), (-150, 110, 20), (40, -20, 80)):
        twin.update_arms(l_pitch_deg=pitch, l_roll_deg=roll, l_elbow_deg=180 - flex,
                         r_pitch_deg=pitch, r_roll_deg=-roll, r_elbow_deg=180 - flex)
        for side, sign in (("l", 1), ("r", -1)):
            d, f = arm_fk(math.radians(pitch), math.radians(sign * roll), math.radians(flex))
            down = np.array([0.0, 0.0, -1.0])
            worst = max(worst, angle_between(np.array(twin.get_link_rotation(f"{side}_upper_arm")) @ down, d),
                        angle_between(np.array(twin.get_link_rotation(f"{side}_forearm")) @ down, f))
    twin.close()
    assert worst < 0.3, worst  # servo pulses are integers: 0.24 deg per pulse
    print(f"[PASS] solver FK matches the URDF simulator (worst {worst:.4f} deg)")


def test_poses_against_simulator():
    twin = TonyPiDigitalTwin(gui=False, headless=True, port=0, enforce_limits=False)
    for yaw in (0.0, 25.0):
        print(f"\n  subject yaw {yaw:.0f} deg       L arm (pitch, roll, elbow)   R arm (pitch, roll, elbow)   L hip/knee   R hip/knee")
        for name, (arm_dirs, leg_dirs) in POSES.items():
            pts, arms, legs = solve_pose(make_pose(arm_dirs, leg_dirs, yaw))
            err = sim_limb_errors(twin, pts, arms, legs)
            print(f"  {name:10s} ang  ({arms['L'][0]:7.1f},{arms['L'][1]:7.1f},{arms['L'][2]:6.1f})"
                  f"   ({arms['R'][0]:7.1f},{arms['R'][1]:7.1f},{arms['R'][2]:6.1f})"
                  f"   {legs['L']['hip_pitch']:5.1f}/{legs['L']['knee']:5.1f}  {legs['R']['hip_pitch']:5.1f}/{legs['R']['knee']:5.1f}")
            print("             err  " + "  ".join(f"{k}={v:.1f}" for k, v in err.items()))
            for limb, e in err.items():
                assert e < 0.5, (name, yaw, limb, e)
            if name == "superhero":
                for side, sign in (("L", 1), ("R", -1)):
                    assert abs(arms[side][0]) < 0.5 and abs(arms[side][1] - sign * 45) < 0.5 and abs(arms[side][2] - 90) < 0.5, arms[side]
    twin.close()
    print("[PASS] zombie / T / superhero / flamingo reproduced by the simulator")


def test_standing_legs_stay_straight():
    # Regression: full-body tracking used to send standing legs straight forward (hip 90 deg).
    for yaw in (0.0, 30.0, -30.0):
        _, _, legs = solve_pose(make_pose(ARMS_DOWN, STAND, yaw))
        for s in "LR":
            assert abs(legs[s]["hip_pitch"]) < 1.0 and legs[s]["knee"] > 179.0 and abs(legs[s]["ankle_pitch"]) < 1.0, legs[s]
    print("[PASS] standing legs stay straight (hip 0, knee 180, ankle 0)")


def sweep(dir_fn, steps=90, noise=0.005, seed=0):
    """
    Tracks the left arm along dir_fn(t) with landmark noise through the live pipeline
    (IK -> damping + slew limit). Returns per-frame commanded (pitch, roll) and the final
    angle between the commanded and the true upper-arm direction.
    """
    rng = np.random.default_rng(seed)
    solver = ArmIK("L")
    filters = [JointFilter(0.0, 0.35, 12.0), JointFilter(0.0, 0.35, 12.0)]
    out = []
    for t in list(np.linspace(0.0, 1.0, steps)) + [1.0] * 30:
        d = unit(dir_fn(t))
        lms = make_pose({"L": (d, d), "R": (DOWN, DOWN)}, STAND)
        for lm in lms:
            lm.x, lm.y, lm.z = (v + rng.normal(0.0, noise) for v in (lm.x, lm.y, lm.z))
        pts = to_torso_frame(lms)
        raw = solver.solve(*(pts[i] for i in ARM_IDX["L"]), q_prev_deg=[f.value for f in filters])
        out.append([f.update(v) for f, v in zip(filters, raw)])
    final = arm_fk(math.radians(out[-1][0]), math.radians(out[-1][1]), 0.0)[0]
    return np.array(out), angle_between(final, dir_fn(1.0))


def test_poles_are_smooth():
    cases = {
        # Raise sideways from hanging, through the T-pose pole, to overhead.
        "side raise through T-pose": lambda t: (0.0, math.sin(math.pi * t), -math.cos(math.pi * t)),
        # Hold the zombie pose (the old estimator's roll pole) with noise.
        "zombie hold": lambda t: FWD,
        # Hold a straight-arm T-pose: pitch is unobservable here and must not wander.
        "T-pose hold": lambda t: (0.0, 1.0, 0.0),
        # Swing forward-raise up to overhead.
        "forward raise to overhead": lambda t: (math.sin(math.pi * t), 0.0, -math.cos(math.pi * t)),
    }
    for name, fn in cases.items():
        traj, final_err = sweep(fn)
        jump = float(np.abs(np.diff(traj, axis=0)).max())
        wander = float(np.abs(np.diff(traj[-30:], axis=0)).max())
        print(f"  {name:28s} max step {jump:4.1f} deg/frame | settled jitter {wander:4.1f} | "
              f"end pitch {traj[-1][0]:7.1f} roll {traj[-1][1]:6.1f} | limb error {final_err:4.1f} deg")
        assert jump <= 12.0 + 1e-6 and wander < 6.0 and final_err < 6.0, (name, jump, wander, final_err)
    print("[PASS] arms pass through and hold at the poles without jumps")


def test_pulses_match_robot_api():
    # robot_api/actions/arms.py: down / forward / up shoulder-pitch pulses, and servo/joints.py stand pose.
    assert shoulder_pulses(0, 0, "L") == (725, 800) and shoulder_pulses(0, 0, "R") == (275, 200)
    assert shoulder_pulses(-90, 0, "L")[0] == 350 and shoulder_pulses(-90, 0, "R")[0] == 650
    assert abs(shoulder_pulses(-144, 0, "L")[0] - 125) <= 1 and abs(shoulder_pulses(-144, 0, "R")[0] - 875) <= 1
    assert elbow_pulse(180, "L") == 575 and elbow_pulse(180, "R") == 425
    for side in "LR":
        p, r = shoulder_pulses(-37.0, 52.0 if side == "L" else -52.0, side)
        got = pulses_to_angles(p, r, elbow_pulse(120.0, side), side)
        assert np.allclose(got, (-37.0, 52.0 if side == "L" else -52.0, 120.0), atol=0.3), got
    print("[PASS] pulse scale matches robot_api (350/650 forward, stand 725/800/575)")


def twin_landmarks(twin):
    """Human-style landmarks (camera convention) read back from the simulator's own links."""
    tf = {}
    for name, (rot, pos) in twin.get_link_transforms().items():  # -> 4x4 homogeneous matrices
        m = np.eye(4)
        m[:3, :3], m[:3, 3] = np.array(rot), np.array(pos)
        tf[name] = m
    body = {}
    for s, sign in (("L", 1), ("R", -1)):
        p = s.lower()
        upper, fore = tf[f"{p}_upper_arm"], tf[f"{p}_forearm"]
        body[ARM_IDX[s][0]] = tf[f"{p}_upper_arm"][:3, 3]
        body[ARM_IDX[s][1]] = fore[:3, 3]
        body[ARM_IDX[s][2]] = fore[:3, 3] + fore[:3, :3] @ np.array([0.0, 0.0, -FORE])
        thigh, shin, foot = tf[f"{p}_thigh"], tf[f"{p}_shin"], tf[f"{p}_foot"]
        body[LEG_IDX[s][0]], body[LEG_IDX[s][1]], body[LEG_IDX[s][2]] = thigh[:3, 3], shin[:3, 3], foot[:3, 3]
    lms = [LandmarkPoint(0.0, 0.0, 0.0, 1.0) for _ in range(33)]
    for i, (f, l, u) in body.items():
        lms[i] = LandmarkPoint(l, -u, -f, 1.0)
    return LandmarkListWrapper(lms)


def test_round_trip_simulator_to_estimator():
    """sim joint angles -> sim limb positions -> retarget solver -> joint angles: must agree."""
    twin = TonyPiDigitalTwin(gui=False, headless=True, port=0, enforce_limits=False)
    worst = {"pitch": 0.0, "roll": 0.0, "elbow": 0.0}
    n = 0
    for pitch in range(-170, 61, 23):
        for roll in range(-20, 161, 20):
            for elbow in (180, 150, 120, 90):
                twin.update_arms(l_pitch_deg=pitch, l_roll_deg=roll, l_elbow_deg=elbow,
                                 r_pitch_deg=pitch, r_roll_deg=-roll, r_elbow_deg=elbow)
                twin.update_legs(l_knee_deg=180 - abs(roll) * 0.3, l_hip_pitch_deg=roll * 0.2,
                                 l_ankle_deg=0, r_knee_deg=180, r_hip_pitch_deg=0, r_ankle_deg=0)
                if abs(math.sin(math.radians(pitch)) * math.cos(math.radians(roll))) < 0.05 and                         abs(math.cos(math.radians(pitch)) * math.cos(math.radians(roll))) < 0.05:
                    continue  # arm along the lateral axis: pitch is not observable
                if elbow == 180 and abs(math.cos(math.radians(roll))) < 0.05:
                    continue
                solvers = {s: ArmIK(s) for s in "LR"}
                lms = twin_landmarks(twin)
                pts = to_torso_frame(lms)
                for s, sign in (("L", 1), ("R", -1)):
                    # Seed with the neighbouring solution as live tracking does; tests IK + FK consistency.
                    for _ in range(25):
                        got = solvers[s].solve(*(pts[i] for i in ARM_IDX[s]), q_prev_deg=(pitch, sign * roll))
                    d_got, f_got = arm_fk(*np.radians(got[:2]), math.radians(180 - got[2]))
                    d_ref, f_ref = arm_fk(math.radians(pitch), math.radians(sign * roll), math.radians(180 - elbow))
                    worst["pitch"] = max(worst["pitch"], angle_between(d_got, d_ref))
                    worst["roll"] = max(worst["roll"], angle_between(f_got, f_ref) if elbow < 175 else 0.0)
                    worst["elbow"] = max(worst["elbow"], abs(got[2] - elbow))
                n += 1
    twin.close()
    print(f"  {n} arm configurations, worst upper-arm dir err {worst['pitch']:.2f} deg, "
          f"forearm dir err {worst['roll']:.2f} deg, elbow err {worst['elbow']:.2f} deg")
    assert worst["pitch"] < 1.0 and worst["roll"] < 1.0 and worst["elbow"] < 1.0, worst
    print("[PASS] simulator -> estimator round trip recovers the commanded arm pose")


if __name__ == "__main__":
    test_fk_matches_simulator()
    test_poses_against_simulator()
    test_standing_legs_stay_straight()
    test_poles_are_smooth()
    test_pulses_match_robot_api()
    test_round_trip_simulator_to_estimator()
    print("\nALL RETARGET TESTS PASSED")
