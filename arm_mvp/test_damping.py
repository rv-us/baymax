"""Calm output: damping against camera noise, arms-at-the-sides prior, and only green joints may move the robot."""
import math

import numpy as np
import pytest

import arm_mimic
import calibration
import solver
from filters import PulseDamper
from test_arm_mvp import FORE, UPPER, WIDTH, landmarks_for

REST = solver.stand_arm_pulses()


# ---------------------------------------------------------------- PulseDamper

def test_one_wild_frame_does_not_move_the_robot():
    d = PulseDamper(REST)
    for _ in range(5):
        d({8: 725})
    out = d({8: 400})                      # a single glitch frame
    assert out[8] == 725
    assert d({8: 725})[8] == 725


def test_camera_noise_around_a_still_arm_leaves_the_servo_still():
    d = PulseDamper(REST)
    rng = np.random.default_rng(1)
    outs = [d({8: int(725 + rng.integers(-5, 6))})[8] for _ in range(200)]
    assert len(set(outs)) == 1             # inside the dead band: not one buzz


def test_a_real_move_is_followed_without_overshoot_or_a_jump():
    d = PulseDamper(REST)
    last, steps = 725, []
    for _ in range(40):
        out = d({8: 425})[8]
        steps.append(abs(out - last))
        last = out
    assert max(steps) <= 33                # never lurches
    assert abs(last - 425) <= 8 + 1        # arrives, within the dead band
    assert last >= 425                     # and does not overshoot


def test_damping_cuts_the_jitter_of_a_noisy_sweep_but_keeps_up_with_it():
    rng = np.random.default_rng(2)
    truth = [725 - 4 * i for i in range(80)]            # a steady lift
    raw = [t + int(rng.normal(0, 8)) for t in truth]
    d = PulseDamper(REST)
    out = [d({8: r})[8] for r in raw]
    jerk = lambda xs: float(np.std(np.diff(xs, 2)))
    assert jerk(out) < 0.5 * jerk(raw)
    assert abs(out[-1] - truth[-1]) < 40   # lags a little, never loses the arm


# ---------------------------------------------------------------- the arms-at-the-sides prior

def pts_for(elev_deg, lateral_deg=0.0, side="L"):
    """Shoulder, elbow, wrist in the torso frame (forward, left, up) for an upper arm `elev_deg` from straight
    down, tilted toward the front by default and `lateral_deg` out to the side."""
    sign = 1.0 if side == "L" else -1.0
    e, l = math.radians(elev_deg), math.radians(lateral_deg)
    d = np.array([math.sin(e) * math.cos(l), sign * math.sin(e) * math.sin(l), -math.cos(e)])
    sh = np.array([0.0, sign * 0.18, 0.0])
    return np.stack([sh, sh + UPPER * d, sh + (UPPER + FORE) * d])


@pytest.mark.parametrize("side", "LR")
def test_an_arm_at_the_side_reads_pitch_zero_even_when_the_ik_picked_the_other_branch(side):
    sign = 1.0 if side == "L" else -1.0
    pts = pts_for(5.0, 0.0, side)
    for wrong in ((180.0, sign * 176.0, 170.0), (-180.0, sign * 170.0, 175.0), (3.0, 0.0, 180.0)):
        pitch, roll, elbow = solver.hang_prior(side, wrong, pts)
        assert pitch == pytest.approx(0.0, abs=1e-9)                       # not 180
        assert sign * roll >= solver.REST_OUT_DEG - 1e-9                   # elbow out, ever so slightly
        assert sign * roll < solver.REST_OUT_DEG + 6
        assert elbow == wrong[2]                                           # the elbow bend is not touched


@pytest.mark.parametrize("side", "LR")
def test_straight_down_gets_the_small_outward_roll_and_the_other_arm_mirrors_it(side):
    pitch, roll, _ = solver.hang_prior(side, (180.0, 180.0, 180.0), pts_for(0.0, 0.0, side))
    assert pitch == 0.0 and abs(roll) == pytest.approx(solver.REST_OUT_DEG)
    assert (roll > 0) == (side == "L")


def test_a_raised_arm_is_left_exactly_as_the_ik_solved_it():
    sol = (-90.0, 10.0, 150.0)
    for elev in (45.0, 60.0, 90.0, 150.0):
        assert solver.hang_prior("L", sol, pts_for(elev)) == sol


def test_the_prior_fades_in_smoothly_as_the_arm_comes_down():
    """Sweep the arm from 90 degrees to hanging: pitch and roll move in small steps, no jump at either edge."""
    side, prev = "L", None
    for elev in np.arange(90.0, -0.01, -0.5):
        e = math.radians(elev)
        pts = pts_for(float(elev), 20.0, side)
        # what a clean IK reports for this arm: pitch forward-negative, roll from the lateral component
        sol = (-math.degrees(math.atan2(pts[1, 0] - pts[0, 0], -(pts[1, 2] - pts[0, 2]))),
               math.degrees(math.asin((pts[1, 1] - pts[0, 1]) / UPPER)), 170.0)
        out = solver.hang_prior(side, sol, pts)
        if prev is not None:
            assert abs(out[0] - prev[0]) < 3.0 and abs(out[1] - prev[1]) < 3.0, (elev, prev, out)
        prev = out
    assert prev[0] == pytest.approx(0.0, abs=0.01)


# ---------------------------------------------------------------- only green joints may drive the robot

def tracked(arms_angles, frames=60, vis=None, noise_px=0.0, seed=0):
    args = arm_mimic.parse_args([])
    cal = {"calibrated": True, "shoulder_m": WIDTH, "shoulder_px": None,
           "upper": {"L": UPPER, "R": UPPER}, "fore": {"L": FORE, "R": FORE}}
    arms = {s: arm_mimic.new_arm(s, args) for s in "LR"}
    damper, pulses = PulseDamper(REST), dict(REST)
    rng = np.random.default_rng(seed)
    history = []
    for i in range(frames):
        img, world, v = landmarks_for(arms_angles)
        if noise_px:
            img = img.copy()
            img[:, :2] += rng.normal(0, noise_px, (33, 2)) / (640, 480)
        arm_mimic.track_arms(arms, (img, world, v if vis is None else vis(i, v)), cal, i / 30.0, 1 / 30.0, (640, 480), args)
        pulses = arm_mimic.step_pulses(damper, pulses, arms, args)
        history.append((dict(pulses), arm_mimic.green_pulses(arms, pulses), {s: a["state"] for s, a in arms.items()}))
    return arms, history


def test_only_arms_drawn_green_are_sent():
    arms, history = tracked({"L": (-90, 0, 0), "R": (-90, 0, 0)}, frames=40)
    pulses, green, states = history[-1]
    assert states == {"L": "TRACKING", "R": "TRACKING"}
    assert set(green) == {8, 7, 6, 16, 15, 14}


def test_an_arm_that_stops_being_green_is_left_out_while_the_other_keeps_going():
    def hide_left_wrist(i, v):
        v = v.copy()
        if i >= 30:
            v[15] = 0.2                                      # the left wrist is occluded from frame 30
        return v

    arms, history = tracked({"L": (-90, 0, 0), "R": (-90, 0, 0)}, frames=50, vis=hide_left_wrist)
    _, green, states = history[-1]
    assert states["L"] == "HOLD" and states["R"] == "TRACKING"
    assert set(green) == {16, 15, 14}                       # nothing at all for the left arm
    assert all(set(g) == {8, 7, 6, 16, 15, 14} for _, g, _ in history[20:30])  # until it stopped being green


def test_noisy_hanging_arms_do_not_make_the_robot_jitter():
    """1 px of independent noise on every landmark (about what MediaPipe does to a still person)."""
    for seed in (4, 5, 6):
        arms, history = tracked({"L": (0, 0, 0), "R": (0, 0, 0)}, frames=120, noise_px=1.0, seed=seed)
        settled = [p for p, _, _ in history[40:]]
        for sid in (8, 7, 6, 16, 15, 14):
            series = [p[sid] for p in settled]
            assert max(series) - min(series) <= 40, (seed, sid, series)          # under 10 degrees, even at its worst
            assert int(np.sum(np.diff(series) != 0)) <= 0.3 * len(series), (seed, sid)   # mostly still, not a buzz
            assert max(abs(np.diff(series))) <= 20, (seed, sid)                  # and never a lurch
        for sid in (8, 7, 16, 15):                                              # shoulders are dead steady
            assert max(p[sid] for p in settled) - min(p[sid] for p in settled) <= 8
    # they hang with pitch 0: the pitch servos sit at the stand value, not a half turn away
    assert abs(history[-1][0][8] - REST[8]) <= 12 and abs(history[-1][0][16] - REST[16]) <= 12


# ---------------------------------------------------------------- the soft zone on a nearly straight elbow

def test_a_nearly_straight_elbow_is_eased_to_straight_and_a_real_bend_is_untouched():
    assert solver.soften_elbow(180.0) == 180.0
    assert 180.0 - solver.soften_elbow(175.0) < 1.0           # 5 degrees of "bend" is noise
    assert 180.0 - solver.soften_elbow(170.0) < 4.0
    for interior in (155.0, 140.0, 90.0, 30.0):
        assert solver.soften_elbow(interior) == interior      # real bends pass through exactly
    assert solver.soften_elbow(185.0) == 180.0                 # hyperextension can't go past straight


def test_the_soft_zone_is_continuous_and_never_bends_more_than_the_arm_does():
    prev = None
    for flex in np.arange(0.0, 60.0, 0.25):
        out = 180.0 - solver.soften_elbow(180.0 - flex)
        assert out <= flex + 1e-9
        if prev is not None:
            assert 0.0 <= out - prev <= 0.25 * 2.0 + 1e-9     # rising, no jump, and never steeper than 2x
        prev = out
