"""Arm mimic unit tests: depth recovery, shoulder frame, One Euro filter, pulse mapping."""
import math

import numpy as np
import pytest

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


def test_calibration_roundtrip(tmp_path):
    assert calibration.load(str(tmp_path / "missing.json"))["calibrated"] is False
    px, world, vis = np.zeros((33, 2)), np.zeros((33, 3)), np.ones(33)
    for i, x in ((11, 0.18), (12, -0.18), (13, 0.47), (14, -0.47), (15, 0.72), (16, -0.72)):  # T-pose
        world[i] = (x, 0.0, 0.0)
        px[i] = (320 + 300 * x, 200)
    assert calibration.finish([calibration.sample(px, world, vis)] * 3) is None  # too few frames
    cal = calibration.finish([calibration.sample(px, world, vis)] * 30)
    assert cal["shoulder_m"] == pytest.approx(0.36) and cal["shoulder_px"] == pytest.approx(108.0)
    assert cal["upper"]["L"] == pytest.approx(0.29) and cal["fore"]["R"] == pytest.approx(0.25)
    vis[13] = 0.2
    assert calibration.sample(px, world, vis) is None
    calibration.save(cal, str(tmp_path / "c.json"))
    assert calibration.load(str(tmp_path / "c.json")) == cal
