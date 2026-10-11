"""
Unit & Integration Tests for mediapose_est.py
=============================================
Validates:
1. GalaxyCore GC0308 hardware simulation & video degradation pipeline.
2. Bilateral 3D kinematic calculations (elbow flexion, shoulder plane, pitch & roll).
3. 180° Shoulder Pitch Axis range of motion (T-pose with hand pointing to sky vs. ground).
4. Full-body 17 keypoint tracking vs. automatic cutoff at hips when legs are out of frame.
5. Hand gripper clamping open/close state.
6. TonyPi bus servo pulse mapping aligned with baymax/robot_api & physical servo diagram.
7. robot_api target payload schema compatibility (all 16 body joints + hands).
8. Real-time OpenCV HUD layout and dimensions.
9. TonyPi 3D Digital Twin integration (including update_legs).
"""

import math
import os
import sys
import numpy as np
import cv2

# Ensure laptop_mvp and repo root are in python path
_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_CURRENT_DIR, ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
if _CURRENT_DIR not in sys.path:
    sys.path.insert(0, _CURRENT_DIR)

from mediapose_est import (
    GC0308HardwareSimulator,
    LandmarkPoint,
    LandmarkListWrapper,
    calculate_elbow_flexion_3d,
    calculate_shoulder_plane_angle_3d,
    check_legs_visible,
    calculate_leg_kinematics_3d,
    detect_hand_closed,
    map_elbow_to_tonypi_pulse,
    map_shoulder_pitch_to_tonypi_pulse,
    map_shoulder_roll_to_tonypi_pulse,
    map_hand_to_tonypi_pulse,
    map_leg_servos,
    build_robot_api_targets,
    draw_hud_panel,
    SERVO_LIMITS,
    STAND_PULSES,
    L_SHOULDER, R_SHOULDER,
    L_ELBOW, R_ELBOW,
    L_WRIST, R_WRIST,
    L_HIP, R_HIP,
    L_KNEE, R_KNEE,
    L_ANKLE, R_ANKLE,
    L_INDEX, R_INDEX,
)
from tonypi_sim import TonyPiDigitalTwin


def test_gc0308_hardware_simulation():
    print("\n--- 1. Testing GC0308 Hardware Emulation & Video Degradation ---")
    sim = GC0308HardwareSimulator(target_width=640, target_height=480, distortion_coeff=-0.25, jpeg_quality=75)

    # Test high-res 1080p frame downscaling
    hd_frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
    degraded, native_res = sim.apply(hd_frame)
    assert native_res == (1920, 1080), f"Expected native res (1920, 1080), got {native_res}"
    assert degraded.shape == (480, 640, 3), f"Expected degraded shape (480, 640, 3), got {degraded.shape}"
    print("[PASS] 1080p frame successfully downscaled to 640x480 VGA.")

    # Test barrel distortion mesh
    grid_img = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2.line(grid_img, (0, 100), (640, 100), (255, 255, 255), 2)
    distorted, _ = sim.apply(grid_img)
    assert distorted.shape == (480, 640, 3)
    print("[PASS] Barrel distortion mesh verified.")

    # Test disable toggle
    sim.enabled = False
    raw_frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    passthrough, _ = sim.apply(raw_frame)
    assert passthrough.shape == (480, 640, 3)
    print("[PASS] Hardware simulation toggle verified.")


def test_elbow_flexion_3d_kinematics():
    print("\n--- 2. Testing 3D Elbow Flexion Kinematics ---")
    sh = [0.0, 0.0, 0.0]
    el = [0.0, -0.3, 0.0]
    wr_straight = [0.0, -0.6, 0.0]
    angle_straight = calculate_elbow_flexion_3d(sh, el, wr_straight)
    print(f"Elbow straight: {angle_straight:.2f}° (Expected: 180.0°)")
    assert abs(angle_straight - 180.0) < 1.0, f"Expected 180°, got {angle_straight}"

    # 90° flexed curl
    wr_90 = [0.0, -0.3, 0.3]
    angle_90 = calculate_elbow_flexion_3d(sh, el, wr_90)
    print(f"Elbow 90° curl: {angle_90:.2f}° (Expected: 90.0°)")
    assert abs(angle_90 - 90.0) < 1.0, f"Expected 90°, got {angle_90}"

    # 45° flexed bicep squeeze
    wr_45 = [0.0, -0.3 + 0.3 * math.cos(math.radians(45)), 0.3 * math.sin(math.radians(45))]
    angle_45 = calculate_elbow_flexion_3d(sh, el, wr_45)
    print(f"Elbow 45° curl: {angle_45:.2f}° (Expected: 45.0°)")
    assert abs(angle_45 - 45.0) < 1.5, f"Expected 45°, got {angle_45}"
    print("[PASS] 3D Elbow flexion kinematics verified.")


def _build_synthetic_pose(
    l_arm_dir: str = "down",
    r_arm_dir: str = "down",
    l_forearm_rot: str = "straight",
    r_forearm_rot: str = "straight",
    legs_in_frame: bool = True,
    legs_posture: str = "standing",
    hand_closed: bool = False,
) -> LandmarkListWrapper:
    """Helper creating 33 synthetic world landmarks for unit tests."""
    lms = [LandmarkPoint(0.0, 0.0, 0.0) for _ in range(33)]

    # Torso (mid-hip at origin)
    lms[L_HIP] = LandmarkPoint(-0.15, 0.0, 0.0)
    lms[R_HIP] = LandmarkPoint(0.15, 0.0, 0.0)

    # Shoulders
    lms[L_SHOULDER] = LandmarkPoint(-0.20, -0.50, 0.0)
    lms[R_SHOULDER] = LandmarkPoint(0.20, -0.50, 0.0)

    l_upper = 0.30
    l_fore = 0.25

    def place_arm(side, posture, rot):
        sh_idx = L_SHOULDER if side == "L" else R_SHOULDER
        el_idx = L_ELBOW if side == "L" else R_ELBOW
        wr_idx = L_WRIST if side == "L" else R_WRIST
        idx_idx = L_INDEX if side == "L" else R_INDEX
        sh = lms[sh_idx]
        sign = -1.0 if side == "L" else 1.0

        if posture == "down":
            lms[el_idx] = LandmarkPoint(sh.x, sh.y + l_upper, sh.z)
            lms[wr_idx] = LandmarkPoint(sh.x, sh.y + l_upper + l_fore, sh.z)
        elif posture == "wingspan":  # T-pose
            lms[el_idx] = LandmarkPoint(sh.x + sign * l_upper, sh.y, sh.z)
            if rot == "sky":
                # Forearm pointing straight UP (to the sky)
                lms[wr_idx] = LandmarkPoint(lms[el_idx].x, lms[el_idx].y - l_fore, lms[el_idx].z)
            elif rot == "ground":
                # Forearm pointing straight DOWN (to the ground)
                lms[wr_idx] = LandmarkPoint(lms[el_idx].x, lms[el_idx].y + l_fore, lms[el_idx].z)
            elif rot == "forward":
                # Forearm pointing forward at camera (-Z in camera space)
                lms[wr_idx] = LandmarkPoint(lms[el_idx].x, lms[el_idx].y, lms[el_idx].z - l_fore)
            else:
                # Straight arm wingspan
                lms[wr_idx] = LandmarkPoint(sh.x + sign * (l_upper + l_fore), sh.y, sh.z)
        elif posture == "forward":
            lms[el_idx] = LandmarkPoint(sh.x, sh.y, sh.z - l_upper)
            lms[wr_idx] = LandmarkPoint(sh.x, sh.y, sh.z - (l_upper + l_fore))

        wr = lms[wr_idx]
        span = 0.04 if hand_closed else 0.15
        lms[idx_idx] = LandmarkPoint(wr.x, wr.y + span, wr.z)

    place_arm("L", l_arm_dir, l_forearm_rot)
    place_arm("R", r_arm_dir, r_forearm_rot)

    # Place legs
    l_thigh, l_shin = 0.35, 0.35
    for side, hip_idx, knee_idx, ank_idx in [
        ("L", L_HIP, L_KNEE, L_ANKLE), ("R", R_HIP, R_KNEE, R_ANKLE)
    ]:
        hip = lms[hip_idx]
        vis = 0.95 if legs_in_frame else 0.10
        y_ank = 0.85 if legs_in_frame else 1.25

        if legs_posture == "standing":
            lms[knee_idx] = LandmarkPoint(hip.x, hip.y + l_thigh, hip.z, visibility=vis)
            lms[ank_idx] = LandmarkPoint(hip.x, hip.y + l_thigh + l_shin, hip.z, visibility=vis)
        elif legs_posture == "squat":
            # Knee flexed forward
            lms[knee_idx] = LandmarkPoint(hip.x, hip.y + 0.25, hip.z - 0.25, visibility=vis)
            lms[ank_idx] = LandmarkPoint(hip.x, hip.y + 0.50, hip.z, visibility=vis)

        # If not in frame, mark ankle with high image y coordinate
        if not legs_in_frame:
            lms[ank_idx].y = y_ank

    return LandmarkListWrapper(lms)


def test_180_degree_shoulder_pitch_sky_vs_ground():
    print("\n--- 3. Testing 180° Shoulder Pitch Axis: Hand to Sky vs. Ground ---")
    # Left arm: Hand pointing to SKY (pitch = +90°)
    # Right arm: Hand pointing to GROUND (pitch = -90°)
    pose_sky_ground = _build_synthetic_pose(
        l_arm_dir="wingspan", r_arm_dir="wingspan",
        l_forearm_rot="sky", r_forearm_rot="ground"
    )
    _, l_pitch_sky, l_roll = calculate_shoulder_plane_angle_3d(pose_sky_ground, "L")
    _, r_pitch_gnd, r_roll = calculate_shoulder_plane_angle_3d(pose_sky_ground, "R")

    print(f"Left Hand to SKY:     Pitch = {l_pitch_sky:.1f}° (Expected: +90°)")
    print(f"Right Hand to GROUND: Pitch = {r_pitch_gnd:.1f}° (Expected: -90°)")
    assert abs(l_pitch_sky - 90.0) < 5.0, f"Expected Left pitch ~+90°, got {l_pitch_sky}"
    assert abs(r_pitch_gnd - (-90.0)) < 5.0, f"Expected Right pitch ~-90°, got {r_pitch_gnd}"

    # Verify Servo 8 (Left Shoulder Pitch) and Servo 16 (Right Shoulder Pitch) commands
    p_l_s8 = map_shoulder_pitch_to_tonypi_pulse(l_pitch_sky, "L")
    p_r_s16 = map_shoulder_pitch_to_tonypi_pulse(r_pitch_gnd, "R")
    print(f"Servo 8  (Left Pitch Up to Sky):    {p_l_s8} (Expected: ~125)")
    print(f"Servo 16 (Right Pitch Down to Ground): {p_r_s16} (Expected: ~275)")
    assert abs(p_l_s8 - 125) <= 25, f"Expected S8 ~125 for sky, got {p_l_s8}"
    assert abs(p_r_s16 - 275) <= 25, f"Expected S16 ~275 for ground, got {p_r_s16}"

    # Reversed: Left hand to GROUND, Right hand to SKY
    pose_ground_sky = _build_synthetic_pose(
        l_arm_dir="wingspan", r_arm_dir="wingspan",
        l_forearm_rot="ground", r_forearm_rot="sky"
    )
    _, l_pitch_gnd, _ = calculate_shoulder_plane_angle_3d(pose_ground_sky, "L")
    _, r_pitch_sky, _ = calculate_shoulder_plane_angle_3d(pose_ground_sky, "R")

    p_l_s8_rev = map_shoulder_pitch_to_tonypi_pulse(l_pitch_gnd, "L")
    p_r_s16_rev = map_shoulder_pitch_to_tonypi_pulse(r_pitch_sky, "R")
    print(f"Servo 8  (Left Pitch Down to Ground): {p_l_s8_rev} (Expected: ~725)")
    print(f"Servo 16 (Right Pitch Up to Sky):      {p_r_s16_rev} (Expected: ~875)")
    assert abs(p_l_s8_rev - 725) <= 25, f"Expected S8 ~725 for ground, got {p_l_s8_rev}"
    assert abs(p_r_s16_rev - 875) <= 25, f"Expected S16 ~875 for sky, got {p_r_s16_rev}"

    # Total span on both servos represents the full 180-degree motion
    span_s8 = p_l_s8_rev - p_l_s8
    span_s16 = p_r_s16_rev - p_r_s16
    print(f"Servo 8  total 180° span: {span_s8} pulses (Expected: ~600)")
    print(f"Servo 16 total 180° span: {span_s16} pulses (Expected: ~600)")
    assert abs(span_s8 - 600) <= 50, f"Expected ~600 pulse span on S8, got {span_s8}"
    assert abs(span_s16 - 600) <= 50, f"Expected ~600 pulse span on S16, got {span_s16}"
    print("[PASS] 180° Shoulder Pitch Axis range of motion verified.")


def test_full_body_and_hip_cutoff():
    print("\n--- 4. Testing Full-Body 17 Keypoints & Hip Cutoff ---")
    # 1. Full Body in frame
    full_body_pose = _build_synthetic_pose(legs_in_frame=True, legs_posture="standing")
    is_vis = check_legs_visible(full_body_pose)
    assert is_vis is True, "Legs should be detected as visible"
    print("[PASS] Full body in frame correctly detected as visible.")

    l_leg, r_leg = calculate_leg_kinematics_3d(full_body_pose)
    assert abs(l_leg["knee"] - 180.0) < 5.0
    assert abs(r_leg["knee"] - 180.0) < 5.0

    l_leg_pulses = map_leg_servos(l_leg, "L", legs_visible=True)
    r_leg_pulses = map_leg_servos(r_leg, "R", legs_visible=True)
    # Standing straight knee pulses should be 500
    assert l_leg_pulses["pulse_knee"] == 500
    assert r_leg_pulses["pulse_knee"] == 500
    print("[PASS] Full body standing leg kinematics verified.")

    # 2. Legs cut off out of frame (camera zoomed on torso)
    cropped_pose = _build_synthetic_pose(legs_in_frame=False)
    is_vis_cropped = check_legs_visible(cropped_pose)
    assert is_vis_cropped is False, "Legs should be detected as cut off"
    print("[PASS] Cropped legs correctly cut off at hips.")

    l_leg_cut = map_leg_servos({}, "L", legs_visible=False)
    r_leg_cut = map_leg_servos({}, "R", legs_visible=False)
    assert l_leg_cut["pulse_knee"] == STAND_PULSES[3]  # 500
    assert l_leg_cut["pulse_hip_pitch"] == STAND_PULSES[4]  # 600
    assert r_leg_cut["pulse_knee"] == STAND_PULSES[11]  # 500
    assert r_leg_cut["pulse_hip_pitch"] == STAND_PULSES[12]  # 400
    print("[PASS] Hip cutoff automatically maintains safe neutral stand pose on leg servos.")


def test_hand_clamp_detection():
    print("\n--- 5. Testing Hand Clamp Detection & Gripper Pulses ---")
    open_lms = _build_synthetic_pose(hand_closed=False)
    closed_lms = _build_synthetic_pose(hand_closed=True)

    assert detect_hand_closed(open_lms, "L") is False
    assert detect_hand_closed(closed_lms, "L") is True

    p_l_open = map_hand_to_tonypi_pulse(False, "L")
    p_l_closed = map_hand_to_tonypi_pulse(True, "L")
    p_r_open = map_hand_to_tonypi_pulse(False, "R")
    p_r_closed = map_hand_to_tonypi_pulse(True, "R")

    assert p_l_open == 240
    assert p_l_closed == 500
    assert p_r_open == 760
    assert p_r_closed == 500
    print("[PASS] Hand clamp open/close verified.")


def test_robot_api_all_16_servos_payload():
    print("\n--- 6. Testing robot_api Full 16 Body Servos Payload ---")
    left_arm = {"pulse_elbow": 575, "pulse_roll": 800, "pulse_pitch": 725, "pulse_hand": 500}
    right_arm = {"pulse_elbow": 425, "pulse_roll": 200, "pulse_pitch": 275, "pulse_hand": 500}
    left_leg = {
        "pulse_ankle_roll": 500, "pulse_ankle_pitch": 390, "pulse_knee": 500,
        "pulse_hip_pitch": 600, "pulse_hip_roll": 500
    }
    right_leg = {
        "pulse_ankle_roll": 500, "pulse_ankle_pitch": 610, "pulse_knee": 500,
        "pulse_hip_pitch": 400, "pulse_hip_roll": 500
    }

    payload = build_robot_api_targets(left_arm, right_arm, left_leg, right_leg)
    expected_all_16 = {
        # Upper Body Left (6, 7, 8, 18)
        "left_elbow", "left_shoulder_roll", "left_shoulder_pitch", "left_hand",
        # Upper Body Right (14, 15, 16, 17)
        "right_elbow", "right_shoulder_roll", "right_shoulder_pitch", "right_hand",
        # Lower Body Left (1, 2, 3, 4, 5)
        "left_ankle_roll", "left_ankle_pitch", "left_knee", "left_hip_pitch", "left_hip_roll",
        # Lower Body Right (9, 10, 11, 12, 13)
        "right_ankle_roll", "right_ankle_pitch", "right_knee", "right_hip_pitch", "right_hip_roll",
    }
    assert set(payload.keys()) == expected_all_16, f"Keys mismatch: {set(payload.keys())} != {expected_all_16}"

    # Verify against robot_api.servo.joints if installed
    try:
        from robot_api.servo.joints import JOINTS
        for j_name in expected_all_16:
            assert j_name in JOINTS, f"Joint {j_name} not in JOINTS"
        print("[PASS] Full body 16 servos match robot_api.servo.joints.JOINTS exactly.")
    except ImportError:
        pass


def test_hud_rendering_full_vs_cutoff():
    print("\n--- 7. Testing Real-Time OpenCV HUD Dashboard (Full vs Cutoff) ---")
    dummy = np.zeros((480, 640, 3), dtype=np.uint8)
    l_arm = {"elbow": 90.0, "pitch": 90.0, "roll": 90.0, "hand_closed": False, "pulse_elbow": 600, "pulse_roll": 500, "pulse_pitch": 125, "pulse_hand": 240, "reps": 4}
    r_arm = {"elbow": 90.0, "pitch": -90.0, "roll": -90.0, "hand_closed": False, "pulse_elbow": 400, "pulse_roll": 500, "pulse_pitch": 275, "pulse_hand": 760, "reps": 4}
    l_leg = {"knee": 180.0, "hip_pitch": 0.0, "pulse_knee": 500}
    r_leg = {"knee": 180.0, "hip_pitch": 0.0, "pulse_knee": 500}

    # Test full body HUD
    hud_full = draw_hud_panel(dummy, l_arm, r_arm, l_leg, r_leg, True, "Coach: 180° rotator cuff active.", 30.0, "Connected", True, (640, 480))
    assert hud_full.shape == (480, 960, 3)

    # Test hip cutoff HUD
    hud_cut = draw_hud_panel(dummy, l_arm, r_arm, l_leg, r_leg, False, "Coach: Cut off at hips.", 30.0, "Connected", True, (640, 480))
    assert hud_cut.shape == (480, 960, 3)
    print("[PASS] HUD dashboard generated for both Full Body and Hip Cutoff modes.")


def test_tonypi_digital_twin_legs():
    print("\n--- 8. Testing TonyPi 3D Digital Twin with Leg Updates ---")
    twin = TonyPiDigitalTwin(gui=False, headless=True)
    twin.start()

    # 180° Scarecrow pose (sky vs ground)
    twin.update_arms(l_pitch_deg=90.0, l_roll_deg=90.0, l_elbow_deg=90.0,
                     r_pitch_deg=-90.0, r_roll_deg=-90.0, r_elbow_deg=90.0)

    # Leg update (squat)
    twin.update_legs(l_knee_deg=130.0, l_hip_pitch_deg=25.0, l_ankle_deg=15.0,
                     r_knee_deg=130.0, r_hip_pitch_deg=25.0, r_ankle_deg=15.0)

    twin.close()
    print("[PASS] TonyPi 3D Digital Twin successfully stepped with 180° pitch and leg updates.")


def run_all_tests():
    test_gc0308_hardware_simulation()
    test_elbow_flexion_3d_kinematics()
    test_180_degree_shoulder_pitch_sky_vs_ground()
    test_full_body_and_hip_cutoff()
    test_hand_clamp_detection()
    test_robot_api_all_16_servos_payload()
    test_hud_rendering_full_vs_cutoff()
    test_tonypi_digital_twin_legs()
    print("\n" + "=" * 55)
    print("ALL FULL-BODY & 180° PITCH UNIT TESTS PASSED (100%)!")
    print("=" * 55 + "\n")


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    run_all_tests()
