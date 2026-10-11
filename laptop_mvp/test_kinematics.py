import numpy as np
import sys
import os

# Import directly from yolo_est
sys.path.insert(0, os.path.dirname(__file__))
from yolo_est import (
    calculate_elbow_angle,
    calculate_shoulder_plane_angle,
    calculate_arm_3d_angles,
    map_shoulder_pitch_to_tonypi_pulse,
    map_shoulder_roll_to_tonypi_pulse,
    map_elbow_to_tonypi_pulse
)

def test_legacy_plane_angles():
    print("\n--- 1. Testing Legacy 2D Plane Angles ---")
    s_right = np.array([200.0, 200.0])
    s_left = np.array([400.0, 200.0])
    w_sh = 200.0
    
    # 1. Left arm full sideways (T-pose): elbow at (570, 200)
    l_elbow_side = np.array([570.0, 200.0])
    deg_side, _ = calculate_shoulder_plane_angle(s_left, l_elbow_side, s_left, s_right, 'L', 0.85)
    print(f"Left arm sideways (T-pose): {deg_side:.2f}° (Expected: ~0°)")
    assert abs(deg_side - 0.0) < 1.0, f"Expected ~0°, got {deg_side}"

    # 2. Left arm straight forward at camera: elbow foreshortened at shoulder (400, 200)
    l_elbow_front = np.array([400.0, 200.0])
    deg_front, _ = calculate_shoulder_plane_angle(s_left, l_elbow_front, s_left, s_right, 'L', 0.85)
    print(f"Left arm straight forward: {deg_front:.2f}° (Expected: ~90°)")
    assert abs(deg_front - 90.0) < 1.0, f"Expected ~90°, got {deg_front}"

    # 3. Left arm halfway forward (45°):
    l_elbow_mid = np.array([400.0 + 170.0 * np.cos(np.radians(45)), 200.0])
    deg_mid, _ = calculate_shoulder_plane_angle(s_left, l_elbow_mid, s_left, s_right, 'L', 0.85)
    print(f"Left arm 45° forward: {deg_mid:.2f}° (Expected: ~45°)")
    assert abs(deg_mid - 45.0) < 1.5, f"Expected ~45°, got {deg_mid}"

    # 4. Right arm tests (mirrored)
    r_elbow_side = np.array([30.0, 200.0])
    r_deg_side, _ = calculate_shoulder_plane_angle(s_right, r_elbow_side, s_left, s_right, 'R', 0.85)
    print(f"Right arm sideways (T-pose): {r_deg_side:.2f}° (Expected: ~0°)")
    assert abs(r_deg_side - 0.0) < 1.0, f"Expected ~0°, got {r_deg_side}"

    r_elbow_front = np.array([200.0, 200.0])
    r_deg_front, _ = calculate_shoulder_plane_angle(s_right, r_elbow_front, s_left, s_right, 'R', 0.85)
    print(f"Right arm straight forward: {r_deg_front:.2f}° (Expected: ~90°)")
    assert abs(r_deg_front - 90.0) < 1.0, f"Expected ~90°, got {r_deg_front}"

    # 5. Elbow angles
    straight_elbow = calculate_elbow_angle(s_left, np.array([570.0, 200.0]), np.array([740.0, 200.0]))
    print(f"Elbow straight: {straight_elbow:.2f}° (Expected: ~180°)")
    assert abs(straight_elbow - 180.0) < 1.0

    flex90_elbow = calculate_elbow_angle(s_left, np.array([570.0, 200.0]), np.array([570.0, 100.0]))
    print(f"Elbow 90° flexed: {flex90_elbow:.2f}° (Expected: ~90°)")
    assert abs(flex90_elbow - 90.0) < 1.0
    print("[PASS] Legacy plane kinematics verified.")


def test_full_range_3d_kinematics():
    print("\n--- 2. Testing 3D Full Range-of-Motion Kinematics ---")
    s_r = np.array([200.0, 200.0])
    s_l = np.array([400.0, 200.0])
    h_r = np.array([230.0, 450.0])
    h_l = np.array([370.0, 450.0])
    l_ref = 160.0  # 0.80 * 200

    # 1. Arms Down (| | |)
    l_p, l_r, l_e, _ = calculate_arm_3d_angles(s_l, [400.0, 360.0], [400.0, 520.0], s_l, s_r, h_l, h_r, 'L', 0.80)
    r_p, r_r, r_e, _ = calculate_arm_3d_angles(s_r, [200.0, 360.0], [200.0, 520.0], s_l, s_r, h_l, h_r, 'R', 0.80)
    print(f"Arms Down: L[pitch={l_p:.1f}°, roll={l_r:.1f}°, elb={l_e:.1f}°] | R[pitch={r_p:.1f}°, roll={r_r:.1f}°, elb={r_e:.1f}°]")
    assert abs(l_p) < 1.0 and abs(l_r) < 1.0 and abs(l_e - 180.0) < 1.0
    assert abs(r_p) < 1.0 and abs(r_r) < 1.0 and abs(r_e - 180.0) < 1.0

    # 2. T-Pose (- | -)
    l_p, l_r, l_e, _ = calculate_arm_3d_angles(s_l, [560.0, 200.0], [720.0, 200.0], s_l, s_r, h_l, h_r, 'L', 0.80)
    r_p, r_r, r_e, _ = calculate_arm_3d_angles(s_r, [40.0, 200.0], [-120.0, 200.0], s_l, s_r, h_l, h_r, 'R', 0.80)
    print(f"T-Pose: L[pitch={l_p:.1f}°, roll={l_r:.1f}°, elb={l_e:.1f}°] | R[pitch={r_p:.1f}°, roll={r_r:.1f}°, elb={r_e:.1f}°]")
    assert abs(l_p) < 1.0 and abs(l_r - 90.0) < 1.0 and abs(l_e - 180.0) < 1.0
    assert abs(r_p) < 1.0 and abs(r_r - (-90.0)) < 1.0 and abs(r_e - 180.0) < 1.0

    # 3. Hands Up (^ | ^)
    l_p, l_r, l_e, _ = calculate_arm_3d_angles(s_l, [400.0, 40.0], [400.0, -120.0], s_l, s_r, h_l, h_r, 'L', 0.80)
    r_p, r_r, r_e, _ = calculate_arm_3d_angles(s_r, [200.0, 40.0], [200.0, -120.0], s_l, s_r, h_l, h_r, 'R', 0.80)
    print(f"Hands Up: L[pitch={l_p:.1f}°, roll={l_r:.1f}°, elb={l_e:.1f}°] | R[pitch={r_p:.1f}°, roll={r_r:.1f}°, elb={r_e:.1f}°]")
    assert abs(l_p) < 1.0 and abs(l_r - 180.0) < 1.0 and abs(l_e - 180.0) < 1.0
    assert abs(r_p) < 1.0 and abs(r_r - (-180.0)) < 1.0 and abs(r_e - 180.0) < 1.0

    # 4. Zombie Pose (Forward Sagittal Extension)
    l_p, l_r, l_e, _ = calculate_arm_3d_angles(s_l, [400.0, 200.0], [400.0, 200.0], s_l, s_r, h_l, h_r, 'L', 0.80)
    r_p, r_r, r_e, _ = calculate_arm_3d_angles(s_r, [200.0, 200.0], [200.0, 200.0], s_l, s_r, h_l, h_r, 'R', 0.80)
    print(f"Zombie Pose: L[pitch={l_p:.1f}°, roll={l_r:.1f}°] | R[pitch={r_p:.1f}°, roll={r_r:.1f}°]")
    assert abs(l_p - (-90.0)) < 1.0 and abs(l_r) < 1.0
    assert abs(r_p - (-90.0)) < 1.0 and abs(r_r) < 1.0

    # 5. Flapping Wing Low (45°)
    l_p, l_r, l_e, _ = calculate_arm_3d_angles(
        s_l, [400.0 + 113.1, 200.0 + 113.1], [400.0 + 226.2, 200.0 + 226.2],
        s_l, s_r, h_l, h_r, 'L', 0.80
    )
    print(f"Wing Low: L[pitch={l_p:.1f}°, roll={l_r:.1f}°]")
    assert abs(l_p) < 1.0 and abs(l_r - 45.0) < 1.5

    # 6. Flapping Wing High (135°)
    l_p, l_r, l_e, _ = calculate_arm_3d_angles(
        s_l, [400.0 + 113.1, 200.0 - 113.1], [400.0 + 226.2, 200.0 - 226.2],
        s_l, s_r, h_l, h_r, 'L', 0.80
    )
    print(f"Wing High: L[pitch={l_p:.1f}°, roll={l_r:.1f}°]")
    assert abs(l_p) < 1.0 and abs(l_r - 135.0) < 1.5

    # 7. Superhero Pose (< | >) - Hands on Hips
    l_p, l_r, l_e, _ = calculate_arm_3d_angles(
        s_l, [513.1, 313.1], [380.0, 360.0],
        s_l, s_r, h_l, h_r, 'L', 0.80
    )
    r_p, r_r, r_e, _ = calculate_arm_3d_angles(
        s_r, [86.9, 313.1], [220.0, 360.0],
        s_l, s_r, h_l, h_r, 'R', 0.80
    )
    print(f"Superhero Pose (< | >): L[roll={l_r:.1f}°, elb={l_e:.1f}°] | R[roll={r_r:.1f}°, elb={r_e:.1f}°]")
    assert 40.0 <= l_r <= 50.0, f"Expected Left Roll ~45°, got {l_r}"
    assert -50.0 <= r_r <= -40.0, f"Expected Right Roll ~ -45°, got {r_r}"
    assert l_e < 100.0, f"Expected flexed elbow < 100°, got {l_e}"
    assert r_e < 100.0, f"Expected flexed elbow < 100°, got {r_e}"

    print("[PASS] 3D full range-of-motion poses verified successfully.")


def test_circular_motion():
    print("\n--- 3. Testing Continuous Circular Flapping Wing Motion ---")
    s_r = np.array([200.0, 200.0])
    s_l = np.array([400.0, 200.0])
    h_r = np.array([230.0, 450.0])
    h_l = np.array([370.0, 450.0])
    r = 160.0

    # Sweep angles from 0° (down) through 90° (T-pose) to 180° (up)
    prev_roll = -1.0
    for deg in range(0, 185, 15):
        rad = np.radians(deg)
        elbow_l = [400.0 + r * np.sin(rad), 200.0 + r * np.cos(rad)]
        wrist_l = [400.0 + 2 * r * np.sin(rad), 200.0 + 2 * r * np.cos(rad)]
        p, roll, elb, _ = calculate_arm_3d_angles(
            s_l, elbow_l, wrist_l, s_l, s_r, h_l, h_r, 'L', 0.80
        )
        assert roll >= prev_roll - 0.5, f"Monotonic elevation violated at {deg}°: roll={roll}"
        assert abs(roll - deg) < 2.0, f"Expected roll ~{deg}°, got {roll}"
        prev_roll = roll

    print("[PASS] Continuous circular sweep verified monotonically from 0° to 180°.")


def test_pulse_mappings():
    print("\n--- 4. Testing 3D Servo Pulse Mappings ---")
    # Pitch mapping
    assert map_shoulder_pitch_to_tonypi_pulse(0.0, 'L') == 800
    assert map_shoulder_pitch_to_tonypi_pulse(-90.0, 'L') == 500
    assert map_shoulder_pitch_to_tonypi_pulse(0.0, 'R') == 200
    assert map_shoulder_pitch_to_tonypi_pulse(-90.0, 'R') == 500

    # Roll mapping
    assert map_shoulder_roll_to_tonypi_pulse(0.0, 'L') == 500
    assert map_shoulder_roll_to_tonypi_pulse(90.0, 'L') == 725 or abs(map_shoulder_roll_to_tonypi_pulse(90.0, 'L') - 725) <= 100
    assert map_shoulder_roll_to_tonypi_pulse(180.0, 'L') == 950
    assert map_shoulder_roll_to_tonypi_pulse(0.0, 'R') == 500
    assert map_shoulder_roll_to_tonypi_pulse(-180.0, 'R') == 50

    print("[PASS] Servo pulse mappings verified.")

if __name__ == "__main__":
    test_legacy_plane_angles()
    test_full_range_3d_kinematics()
    test_circular_motion()
    test_pulse_mappings()
    print("\n=========================================")
    print("ALL 3D KINEMATICS UNIT TESTS PASSED (100%)")
    print("=========================================\n")
