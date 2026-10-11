"""Arm mimic unit tests: depth recovery, shoulder frame, One Euro filter, pulse mapping."""
import math

import numpy as np
import pytest

import arm_mimic
import calibration
import solver
from filters import OneEuro, slew
from retarget import ARM_IDX, ArmIK, shoulder_frame

FOCAL, CENTER = 576.0, np.array([320.0, 240.0])
WIDTH, UPPER, FORE = 0.36, 0.29, 0.25
S = math.sqrt(0.5)
# (upper arm direction, forearm direction) for the LEFT arm in camera coords
# (x right = subject's left, y down, z away from the camera).
POSES = {
    "hanging": ((0, 1, 0), (0, 1, 0)),
    "t_pose": ((1, 0, 0), (1, 0, 0)),
    "pointing_at_camera": ((0, 0, -1), (0, 0, -1)),
    "elbow_out_forearm_at_camera": ((1, 0, 0), (0, 0, -1)),
    "diagonal_forward": ((S, 0, -S), (0, -S, -S)),
    "reaching_back": ((0, S, S), (0, S, S)),
    "overhead": ((0, -1, 0), (0.2, -1, -0.2)),
}


def project(p):
    return FOCAL * p[:2] / p[2] + CENTER


def synthetic_arm(dirs, distance=2.0, yaw_deg=0.0, offset=(0.0, 0.0)):
    """True camera-frame (l_shoulder, r_shoulder, elbow, wrist) for the left arm."""
    c, s = math.cos(math.radians(yaw_deg)), math.sin(math.radians(yaw_deg))
    mid = np.array([offset[0], offset[1], distance])
    half = 0.5 * WIDTH * np.array([c, 0.0, s])
    l_sh, r_sh = mid + half, mid - half
    up, fore = (np.asarray(d, dtype=float) / np.linalg.norm(d) for d in dirs)
    elbow = l_sh + UPPER * up
    return l_sh, r_sh, elbow, elbow + FORE * fore


def recover(l_sh, r_sh, elbow, wrist, noise_px=0.0, rng=None):
    px = [project(p) + (rng.normal(0, noise_px, 2) if rng is not None else 0.0) for p in (l_sh, r_sh, elbow, wrist)]
    world = np.array([l_sh, r_sh, elbow, wrist]) - 0.5 * (l_sh + r_sh)  # world landmarks: body centred
    sh = solver.shoulders_3d(px[0], px[1], world[0], world[1], WIDTH, FOCAL, CENTER)
    got_elbow, got_wrist, _ = solver.reconstruct_arm(
        sh[0], px[2], px[3], world[[0, 2, 3]], (1.0, 1.0), (UPPER, FORE), FOCAL, CENTER)
    return sh[0], sh[1], got_elbow, got_wrist


@pytest.mark.parametrize("name", POSES)
@pytest.mark.parametrize("distance,yaw,offset", [(2.0, 0, (0, 0)), (1.2, 0, (0.2, -0.1)), (2.5, 25, (-0.3, 0.1))])
def test_bone_length_depth_recovery(name, distance, yaw, offset):
    truth = synthetic_arm(POSES[name], distance, yaw, offset)
    got = recover(*truth)
    for want, have in zip(truth, got):
        assert np.linalg.norm(want - have) < 0.03, (name, want, have)
    assert abs(np.linalg.norm(got[2] - got[0]) - UPPER) < 1e-6
    assert abs(np.linalg.norm(got[3] - got[2]) - FORE) < 1e-6


def test_depth_recovery_keeps_bone_lengths_under_noise():
    rng = np.random.default_rng(0)
    for name, dirs in POSES.items():
        for _ in range(20):
            got = recover(*synthetic_arm(dirs), noise_px=4.0, rng=rng)
            assert abs(np.linalg.norm(got[2] - got[0]) - UPPER) < 1e-6, name
            assert abs(np.linalg.norm(got[3] - got[2]) - FORE) < 1e-6, name


def test_depth_sign_hysteresis():
    sign = 1.0
    for dz in (0.03, -0.03, 0.01, -0.039):  # noise around zero never flips
        sign = solver.depth_sign(sign, dz)
        assert sign == 1.0
    assert solver.depth_sign(sign, -0.05) == -1.0
    assert solver.depth_sign(-1.0, 0.03) == -1.0


def test_recovered_arm_solves_to_expected_angles():
    # Left arm straight forward (toward the camera): pitch -90, roll 0, elbow straight.
    l_sh, r_sh, elbow, wrist = recover(*synthetic_arm(POSES["pointing_at_camera"]))
    rot, origin = shoulder_frame(l_sh, r_sh)
    pts = (np.stack([l_sh, elbow, wrist]) - origin) @ rot.T
    ik = ArmIK("L")
    for _ in range(20):
        pitch, roll, interior = ik.solve(*pts)
    assert abs(pitch + 90) < 3 and abs(roll) < 3 and interior > 175, (pitch, roll, interior)


@pytest.mark.parametrize("yaw", [0.0, 30.0, -30.0])
def test_shoulder_frame_orthonormal_without_hips(yaw):
    c, s = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
    l_sh = np.array([0.18 * c, 0.02, 2.0 + 0.18 * s])  # one shoulder slightly shrugged
    r_sh = np.array([-0.18 * c, -0.02, 2.0 - 0.18 * s])
    rot, origin = shoulder_frame(l_sh, r_sh)  # no hips anywhere in the input
    assert np.allclose(rot @ rot.T, np.eye(3), atol=1e-9)
    assert abs(np.linalg.det(rot) - 1.0) < 1e-9
    assert np.allclose(origin, 0.5 * (l_sh + r_sh))
    fwd, left, up = rot
    assert np.allclose(up, (0, -1, 0)) and fwd[2] < 0  # up is image up, forward faces the camera
    assert ((l_sh - origin) @ rot.T)[1] > 0  # left shoulder on +y
    assert shoulder_frame(l_sh, l_sh) is None
    assert shoulder_frame(l_sh, np.full(3, np.nan)) is None


def test_one_euro_reduces_jitter_without_lag():
    rng = np.random.default_rng(1)
    fps, n = 30.0, 600
    t = np.arange(n) / fps
    clean = 0.3 * np.sin(2 * math.pi * 0.5 * t)
    noisy = clean + rng.normal(0.0, 0.01, n)
    f = OneEuro()
    out = np.array([float(f(np.array([x]), ti)[0]) for x, ti in zip(noisy, t)])
    # Jitter: frame to frame noise once the motion itself is removed.
    assert np.std(np.diff(out - clean)) < 0.6 * np.std(np.diff(noisy - clean))
    assert np.sqrt(np.mean((out - clean)[30:] ** 2)) < 0.03
    # Lag: delay (in tenths of a frame) that best aligns the clean signal with the output.
    def err(k):
        return np.mean((out[40:] - 0.3 * np.sin(2 * math.pi * 0.5 * (t[40:] - k / fps))) ** 2)
    lag = min(np.arange(0, 60) / 10.0, key=err)
    assert lag <= 1.5, lag  # at most 1.5 frames (50 ms) at peak speed near 1 m/s

    still = OneEuro()
    held = np.array([float(still(np.array([x]), ti)[0]) for x, ti in zip(rng.normal(0.0, 0.01, n), t)])
    assert np.std(held[60:]) < 0.005  # a still joint: noise more than halved


def test_slew_limit():
    assert slew(0.0, 100.0, 12.0) == 12.0 and slew(0.0, -100.0, 12.0) == -12.0
    assert slew(0.0, 5.0, 12.0) == 5.0
    assert slew(0.0, 100.0, 12.0, dt=1.0 / 15.0) == pytest.approx(24.0)


def test_pulses_never_leave_servo_limits():
    angles = list(np.arange(-360.0, 361.0, 7.5)) + [float("nan"), float("inf"), float("-inf"), 1e12, -1e12]
    for side in "LR":
        assert solver.arm_pulses(side, *solver.REST) == {s: solver.STAND_PULSES[s] for s in solver.ARM_SERVOS[side]}
        for pitch in angles:
            for roll in angles:
                for elbow in (0.0, 90.0, 180.0, 400.0, float("nan"), float("inf"), float("-inf")):
                    for sid, pulse in solver.arm_pulses(side, pitch, roll, elbow).items():
                        lo, hi = solver.SERVO_LIMITS[sid]
                        assert isinstance(pulse, int) and lo <= pulse <= hi, (side, pitch, roll, elbow, sid, pulse)
    assert solver.arm_pulses("L", float("nan"), float("nan"), float("nan")) == solver.arm_pulses("L", *solver.REST)


def test_rate_limit_and_stand():
    stand = solver.stand_arm_pulses()
    assert set(stand) == {6, 7, 8, 14, 15, 16}
    target = {**stand, 8: 100, 16: 280}
    out = solver.rate_limit(stand, target, 30)
    assert out[8] == stand[8] - 30 and out[16] == 280 and out[6] == stand[6]


def test_calibration_file_roundtrip(tmp_path):
    assert calibration.load(str(tmp_path / "missing.json"))["calibrated"] is False
    assert calibration.new_state()["locked"] is False
    cal = {**calibration.default_calibration(0.4), "calibrated": True, "shoulder_px": 120.0}
    calibration.save(cal, str(tmp_path / "c.json"))
    assert calibration.load(str(tmp_path / "c.json")) == cal
    assert calibration.new_state(cal)["locked"] is True
    assert calibration.matrix_rows(0) == ["0" * 16] * 8 and calibration.matrix_rows(45) == ["1" * 16] * 8
    assert calibration.matrix_rows(23)[0] == "1" * 8 + "0" * 8


def landmarks_for(angles, distance=2.0, size=(640, 480), yaw_deg=0.0):
    """MediaPipe style (image, world, vis) for a subject facing the camera whose arms are at the
    given robot angles {side: (pitch, roll, flex) in degrees}."""
    from retarget import arm_fk

    torso = np.zeros((33, 3))
    for side, y in (("L", 0.5 * WIDTH), ("R", -0.5 * WIDTH)):
        sh, el, wr = ARM_IDX[side]
        upper, fore = arm_fk(*np.radians(angles[side]), side=side)
        torso[sh] = (0.0, y, 0.0)
        torso[el] = torso[sh] + UPPER * upper
        torso[wr] = torso[el] + FORE * fore
    c, s = math.cos(math.radians(yaw_deg)), math.sin(math.radians(yaw_deg))
    torso = torso @ np.array([[c, s, 0.0], [-s, c, 0.0], [0.0, 0.0, 1.0]])  # turn about the vertical
    cam = np.stack([torso[:, 1], -torso[:, 2], -torso[:, 0]], axis=1)  # x right, y down, z away
    px = 0.9 * size[0] * (cam[:, :2] + (0.1, -0.05)) / (cam[:, 2:] + distance) + (size[0] / 2, size[1] / 2)
    return np.concatenate([px / size, cam[:, 2:]], axis=1), cam, np.ones(33)


def test_tracking_end_to_end_copy_mode():
    """Known robot arm angles -> synthetic camera landmarks -> full tracker -> the same angles,
    with the subject's left arm driving the robot's left servos (copy mode, not mirror)."""
    args = arm_mimic.parse_args([])
    cal = {"calibrated": True, "shoulder_m": WIDTH, "shoulder_px": None,
           "upper": {"L": UPPER, "R": UPPER}, "fore": {"L": FORE, "R": FORE}}
    cases = [
        {"L": (-90, 0, 0), "R": (0, 0, 0)},        # left arm forward, right hanging
        {"L": (0, 60, 0), "R": (-90, 0, 60)},      # left out to the side, right forward with bent elbow
        {"L": (-150, 20, 30), "R": (0, -80, 0)},   # left overhead, right near T-pose
        {"L": (-40, 30, 70), "R": (-120, -40, 45)},
    ]
    for want in cases:
        arms = {side: arm_mimic.new_arm(side, args) for side in "LR"}
        lm = landmarks_for(want)
        for i in range(60):
            arm_mimic.track_arms(arms, lm, cal, i / 30.0, 1 / 30.0, (640, 480), args)
        for side in "LR":
            pitch, roll, interior = arms[side]["angles"]
            assert arms[side]["state"] == "TRACKING"
            assert abs(pitch - want[side][0]) < 3 and abs(roll - want[side][1]) < 3, (side, want[side], arms[side]["angles"])
            assert abs(180 - interior - want[side][2]) < 3, (side, want[side], interior)
    # Left arm forward lowers S8 toward 350 and leaves the right arm servos at stand.
    arms = {side: arm_mimic.new_arm(side, args) for side in "LR"}
    for i in range(60):
        arm_mimic.track_arms(arms, landmarks_for(cases[0]), cal, i / 30.0, 1 / 30.0, (640, 480), args)
    assert abs(solver.arm_pulses("L", *arms["L"]["angles"])[8] - 350) <= 12
    assert abs(solver.arm_pulses("R", *arms["R"]["angles"])[16] - 275) <= 12


def test_tracking_gates_hold_then_lost():
    args = arm_mimic.parse_args([])
    cal = {**calibration.default_calibration(WIDTH), "upper": {"L": UPPER, "R": UPPER}, "fore": {"L": FORE, "R": FORE}}
    arms = {side: arm_mimic.new_arm(side, args) for side in "LR"}
    good = landmarks_for({"L": (-90, 0, 0), "R": (-90, 0, 0)})
    t = 0.0
    for _ in range(40):
        t += 1 / 30.0
        arm_mimic.track_arms(arms, good, cal, t, 1 / 30.0, (640, 480), args)
    held = list(arms["L"]["angles"])
    hidden = (good[0], good[1], good[2].copy())
    hidden[2][15] = 0.2  # left wrist occluded
    swapped = landmarks_for({"L": (-90, 0, 0), "R": (0, -90, 0)})  # right wrist teleports 0.7 m
    for _ in range(10):
        t += 1 / 30.0
        arm_mimic.track_arms(arms, (swapped[0], swapped[1], hidden[2]), cal, t, 1 / 30.0, (640, 480), args)
    assert arms["L"]["state"] == "HOLD" and arms["L"]["angles"] == held          # visibility gate
    assert arms["R"]["state"] == "HOLD" and abs(arms["R"]["angles"][0] + 90) < 3  # swap guard
    for _ in range(150):
        t += 1 / 30.0
        arm_mimic.track_arms(arms, None, cal, t, 1 / 30.0, (640, 480), args)
    for side in "LR":
        assert arms[side]["state"] == "LOST"
        assert np.allclose(arms[side]["angles"], solver.REST, atol=0.5)  # eased back to rest


HANG = {"L": (0, 0, 0), "R": (0, 0, 0)}
T_POSE = {"L": (0, 90, 0), "R": (0, -90, 0)}


def feed(state, angles, t, frames, vis=None, jitter=None, **kw):
    """Feeds `frames` frames of one pose at 30 fps. Returns (new time, samples added per side)."""
    before = {s: len(state["buf"][s]) for s in "LR"}
    for i in range(frames):
        img, world, v = landmarks_for(angles, **kw)
        px = img[:, :2] * (640, 480) + (jitter(i) if jitter else 0.0)
        calibration.update(state, px, world, v if vis is None else vis, t)
        t += 1 / 30.0
    return t, {s: len(state["buf"][s]) - before[s] for s in "LR"}


def test_auto_calibration_collects_only_good_frames():
    state, t = calibration.new_state(), 0.0
    # Bad frames first: nothing may be collected from any of them.
    bad = [
        {"L": (-90, 0, 0), "R": (-90, 0, 0)},     # arms pointing at the camera
        {"L": (0, 0, 60), "R": (0, 0, 60)},       # bent elbows
        {"L": (-35, 0, 0), "R": (-35, 0, 0)},     # straight but 35 degrees out of the image plane
    ]
    for pose in bad:
        t, added = feed(state, pose, t, 30)
        assert added == {"L": 0, "R": 0}, pose
    t, added = feed(state, HANG, t, 30, yaw_deg=30)  # shoulders turned away
    assert added == {"L": 0, "R": 0}
    low = np.ones(33)
    low[[13, 16]] = 0.55
    t, added = feed(state, HANG, t, 30, vis=low)     # visibility under 0.6 on both chains
    assert added == {"L": 0, "R": 0}
    t, added = feed(state, HANG, t, 30, jitter=lambda i: (12.0 * (i + 1), 0.0))  # walking sideways
    assert added == {"L": 0, "R": 0}
    assert not state["locked"] and calibration.progress(state) == 0

    # One good arm only: that side collects, the bent side does not, and nothing locks.
    t, added = feed(state, {"L": (0, 0, 0), "R": (0, 0, 70)}, t, 30)
    assert added["R"] == 0 and 15 <= added["L"] <= 21  # 30 frames minus the 0.3 s stillness wait
    assert not state["locked"]

    # Good frames: hanging arms, then a T-pose. Locks at 45 per side.
    t, added = feed(state, HANG, t, 40)
    assert 25 <= added["R"] <= 31 and not state["locked"] and calibration.progress(state) == added["R"]
    t, added = feed(state, T_POSE, t, 40)
    assert added["L"] > 25 and state["locked"]
    cal = state["cal"]
    assert cal["calibrated"] and abs(cal["shoulder_m"] - WIDTH) < 0.02
    for side in "LR":
        assert abs(cal["upper"][side] - UPPER) < 0.02 and abs(cal["fore"][side] - FORE) < 0.02, cal


def test_auto_calibration_median_rejects_outliers_and_refines_slowly():
    rng = np.random.default_rng(3)
    state, t = calibration.new_state(), 0.0
    t, _ = feed(state, HANG, t, 80, jitter=lambda i: rng.normal(0.0, 0.6, (33, 2)))  # pixel noise
    assert state["locked"]
    for side in "LR":
        assert abs(state["cal"]["upper"][side] - UPPER) < 0.02 and abs(state["cal"]["fore"][side] - FORE) < 0.02
    # After locking, a burst of wrong samples moves a length by at most 3 percent per sample.
    before = state["cal"]["upper"]["L"]
    state["buf"]["L"].extend([(2 * UPPER, 2 * FORE, WIDTH, 100.0)] * 100)
    t, _ = feed(state, HANG, t, 1)
    assert before < state["cal"]["upper"]["L"] <= before * 1.03 + 1e-9

    # A loaded calibration is not touched until the buffer holds enough fresh samples again.
    loaded = {**calibration.default_calibration(0.5), "calibrated": True}
    state = calibration.new_state(loaded)
    t, _ = feed(state, HANG, t, 30)
    assert state["cal"]["upper"]["L"] == 0.8 * 0.5
