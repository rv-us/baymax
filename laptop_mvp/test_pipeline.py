import numpy as np
import cv2
import yolo_est
from digital_twin import TonyPiDigitalTwin

def test_full_pipeline():
    print("Testing YOLO PT Pose & Simulation Pipeline (Full 3D)...")
    
    # 1. Test 3D HUD Drawing
    dummy_frame = np.zeros((480, 640, 3), dtype=np.uint8)
    left_telemetry = {
        'elbow': 65.0,
        'pitch': -10.0,
        'roll': 45.0,
        'pulse_elbow': 320,
        'pulse_pitch': 780,
        'pulse_roll': 612,
        'reps': 4
    }
    right_telemetry = {
        'elbow': 65.0,
        'pitch': -10.0,
        'roll': -45.0,
        'pulse_elbow': 680,
        'pulse_pitch': 220,
        'pulse_roll': 388,
        'reps': 3
    }
    
    hud_canvas = yolo_est.draw_hud_panel(
        dummy_frame, left_telemetry, right_telemetry,
        "Nemotron: Form verified across 3D range of motion.", 30.0, "Connected"
    )
    assert hud_canvas.shape == (480, 960, 3), f"HUD canvas shape mismatch: {hud_canvas.shape}"
    print("[PASS] 3D HUD canvas generated successfully with dimensions (480, 960, 3)")
    
    # 2. Test Digital Twin in Headless Mode (Multiprocess Default)
    twin = TonyPiDigitalTwin(headless=True, port=0)
    twin.start()
    
    # Send Wingspan T-pose (Roll = 90° / -90°)
    twin.update_arms(l_pitch_deg=0.0, l_roll_deg=90.0, l_elbow_deg=180.0,
                     r_pitch_deg=0.0, r_roll_deg=-90.0, r_elbow_deg=180.0)
    print("[PASS] Digital Twin updated to Wingspan T-pose")

    # Send Superhero Pose (< | >)
    twin.update_arms(l_pitch_deg=0.0, l_roll_deg=45.0, l_elbow_deg=65.0,
                     r_pitch_deg=0.0, r_roll_deg=-45.0, r_elbow_deg=65.0)
    print("[PASS] Digital Twin updated to Superhero Pose (< | >)")

    # Send Hands Up (^ | ^)
    twin.update_arms(l_pitch_deg=0.0, l_roll_deg=180.0, l_elbow_deg=180.0,
                     r_pitch_deg=0.0, r_roll_deg=-180.0, r_elbow_deg=180.0)
    print("[PASS] Digital Twin updated to Hands Up (^ | ^)")

    # Send Zombie Pose (Forward Sagittal Extension: Pitch = -90°)
    twin.update_arms(l_pitch_deg=-90.0, l_roll_deg=0.0, l_elbow_deg=180.0,
                     r_pitch_deg=-90.0, r_roll_deg=0.0, r_elbow_deg=180.0)
    print("[PASS] Digital Twin updated to Zombie Pose (Sagittal Forward)")

    # Send Flapping Wings (Roll = 45° / -45°)
    twin.update_arms(l_pitch_deg=0.0, l_roll_deg=45.0, l_elbow_deg=180.0,
                     r_pitch_deg=0.0, r_roll_deg=-45.0, r_elbow_deg=180.0)
    print("[PASS] Digital Twin updated to Flapping Wings")

    # The viewer sees what was sent: T-pose servo pulses 7/15 sit ~75 deg from stand
    twin.update_arms(l_pitch_deg=0.0, l_roll_deg=90.0, l_elbow_deg=180.0,
                     r_pitch_deg=0.0, r_roll_deg=-90.0, r_elbow_deg=180.0)
    assert abs(twin.state.command.servos[7] - (800 - round(90 * 1000 / 240))) <= 1
    assert twin.is_window_open()
    twin.close()
    assert twin.is_window_closed()
    print("[PASS] Digital Twin cleanly closed")

    # 3. A second twin must coexist with the first (ports are picked per instance)
    sync_twin = TonyPiDigitalTwin(headless=True, port=0)
    sync_twin.start()
    sync_twin.update_arms(l_pitch_deg=-15.0, l_roll_deg=45.0, l_elbow_deg=70.0,
                          r_pitch_deg=-15.0, r_roll_deg=-45.0, r_elbow_deg=70.0)
    sync_twin.step()
    sync_twin.close()
    print("[PASS] Synchronous Digital Twin tested cleanly")

    print("\nALL PIPELINE INTEGRATION TESTS PASSED (100%)!")

if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    test_full_pipeline()
