import argparse
import cv2
import numpy as np
import time
import threading
from ultralytics import YOLO

# ==========================================
# 1. KINEMATIC & RETARGETING CALCULATIONS
# ==========================================

def calculate_elbow_angle(p_shoulder, p_elbow, p_wrist):
    """
    Computes the interior 2D angle (in degrees) at vertex p_elbow
    between vector (p_shoulder - p_elbow) and (p_wrist - p_elbow).
    180° = straight arm extension, ~40°-60° = flexed bicep curl.
    """
    v1 = np.array([p_shoulder[0] - p_elbow[0], p_shoulder[1] - p_elbow[1]], dtype=np.float32)
    v2 = np.array([p_wrist[0] - p_elbow[0], p_wrist[1] - p_elbow[1]], dtype=np.float32)

    norm1 = np.linalg.norm(v1)
    norm2 = np.linalg.norm(v2)
    if norm1 < 1e-4 or norm2 < 1e-4:
        return 180.0

    cosine = np.dot(v1, v2) / (norm1 * norm2)
    cosine = np.clip(cosine, -1.0, 1.0)
    angle_rad = np.arccos(cosine)
    return float(np.degrees(angle_rad))


def calculate_arm_3d_angles(shoulder, elbow, wrist, s_left, s_right,
                            hip_left=None, hip_right=None, side='L', max_ratio=0.85):
    """
    Computes 3D joint angles for human arm tracking in camera frame:
      - Pitch: sagittal rotation [0° to -90°], where -90° is forward extension.
      - Roll: coronal elevation / abduction:
          Left: 0° = arms down by sides, +90° = horizontal wingspan, +180° = hands overhead.
          Right: 0° = arms down by sides, -90° = horizontal wingspan, -180° = hands overhead.
      - Elbow: interior angle [180° = straight, ~40°-60° = flexed].
    Uses acromial baseline vector, torso vertical spine vector, and 2D monocular link
    foreshortening (Pythagorean residual) for continuous full-sphere 3D tracking.
    """
    # Acromial baseline vector S_LR from Right Shoulder to Left Shoulder
    s_lr = np.array([s_left[0] - s_right[0], s_left[1] - s_right[1]], dtype=np.float32)
    w_shoulder = float(np.linalg.norm(s_lr))
    if w_shoulder < 1e-4:
        return 0.0, 0.0, 180.0, max_ratio

    # Unit vector pointing laterally outward along coronal plane for this arm
    if side == 'L':
        u_lat = s_lr / w_shoulder  # points outward to patient's left
    else:
        u_lat = -s_lr / w_shoulder  # points outward to patient's right

    # Unit vertical upwards vector (along torso from mid-hip to mid-shoulder)
    if hip_left is not None and hip_right is not None:
        mid_sh = (np.array(s_left, dtype=np.float32) + np.array(s_right, dtype=np.float32)) * 0.5
        mid_hip = (np.array(hip_left, dtype=np.float32) + np.array(hip_right, dtype=np.float32)) * 0.5
        v_spine = mid_sh - mid_hip
        norm_spine = float(np.linalg.norm(v_spine))
        u_up = (v_spine / norm_spine) if norm_spine > 1e-4 else np.array([-u_lat[1], u_lat[0]], dtype=np.float32)
    else:
        u_up = np.array([-u_lat[1], u_lat[0]], dtype=np.float32)

    # In image coordinates, Y points downwards; ensure u_up points upwards (-Y in image)
    if u_up[1] > 0:
        u_up = -u_up

    # Upper arm vector (shoulder to elbow)
    v_upper = np.array([elbow[0] - shoulder[0], elbow[1] - shoulder[1]], dtype=np.float32)
    l_obs = float(np.linalg.norm(v_upper))

    # Adaptive reference upper-arm link length relative to shoulder width
    ratio = l_obs / w_shoulder
    current_max = max(max_ratio or 0.80, ratio, 0.75)
    l_ref = current_max * w_shoulder

    # Lateral and vertical projections on the coronal plane
    x_lat = float(np.dot(v_upper, u_lat))
    y_vert = float(np.dot(v_upper, u_up))

    # Sagittal depth from link length foreshortening (Pythagorean residual)
    z_sq = max(0.0, (l_ref ** 2) - (l_obs ** 2))
    z_depth = float(np.sqrt(z_sq))

    # Coronal elevation/abduction angle:
    # 0° = arms down along torso
    # 90° = horizontal wingspan
    # 180° = hands overhead
    if l_obs < 0.15 * l_ref:
        # Arm is pointing almost directly toward camera (pure forward sagittal raise)
        elevation_deg = 0.0
    else:
        # -y_vert is positive downward along torso
        elevation_rad = np.arctan2(x_lat, -y_vert)
        elevation_deg = float(np.degrees(elevation_rad))

    # Apply TonyPi URDF joint limits & conventions:
    # Left roll: positive outward [0° to 180°]
    # Right roll: negative outward [0° to -180°]
    if side == 'L':
        roll_deg = float(np.clip(elevation_deg, -45.0, 180.0))
    else:
        roll_deg = float(np.clip(-elevation_deg, -180.0, 45.0))

    # Forward sagittal pitch:
    # TonyPi URDF pitch convention: 0° = neutral down, -90° = forward horizontal
    norm_depth = float(np.clip(z_depth / max(1e-4, l_ref), 0.0, 1.0))
    if norm_depth < 0.05:  # Deadband to suppress coronal plane keypoint noise (< ~2.9°)
        norm_depth = 0.0
    pitch_deg = float(-np.degrees(np.arcsin(norm_depth)))

    # Elbow interior angle (180° = straight)
    elbow_deg = calculate_elbow_angle(shoulder, elbow, wrist)

    return pitch_deg, roll_deg, elbow_deg, current_max


def calculate_shoulder_plane_angle(shoulder, elbow, s_left, s_right, side='L', max_ratio=0.85):
    """
    Backward-compatible wrapper for horizontal shoulder plane angle estimation.
    """
    pitch_deg, roll_deg, elbow_deg, updated_ratio = calculate_arm_3d_angles(
        shoulder, elbow, elbow, s_left, s_right, side=side, max_ratio=max_ratio
    )
    plane_deg = float(np.clip(-pitch_deg, 0.0, 90.0))
    return plane_deg, updated_ratio


def map_elbow_to_tonypi_pulse(human_angle, side='L', min_angle=30.0, max_angle=180.0):
    """
    Maps human elbow angle to TonyPi bus servo pulse range (0 - 1000).
    Hiwonder LX-824HV servos:
      Left arm (Servo 6): 200 (flexed) to 800 (extended)
      Right arm (Servo 14): 800 (flexed) to 200 (extended - mirrored)
    """
    clamped = np.clip(human_angle, min_angle, max_angle)
    norm = (clamped - min_angle) / (max_angle - min_angle)  # 0 = flexed, 1 = extended
    if side == 'L':
        target_pulse = int(200 + norm * (800 - 200))
        return int(np.clip(target_pulse, 150, 930))
    else:
        target_pulse = int(800 - norm * (800 - 200))
        return int(np.clip(target_pulse, 150, 875))


def map_shoulder_pitch_to_tonypi_pulse(pitch_deg, side='L'):
    """
    Maps shoulder pitch angle (0° = vertical neutral, -90° = forward)
    to TonyPi bus servo pulse range (0 - 1000).
      Left arm (Servo 7): 800 (neutral down) down to 500 (forward)
      Right arm (Servo 15): 200 (neutral down) up to 500 (forward)
    """
    norm = np.clip(-pitch_deg / 90.0, 0.0, 1.0)
    if side == 'L':
        target_pulse = int(800 - norm * (800 - 500))
        return int(np.clip(target_pulse, 150, 900))
    else:
        target_pulse = int(200 + norm * (500 - 200))
        return int(np.clip(target_pulse, 150, 850))


def map_shoulder_roll_to_tonypi_pulse(roll_deg, side='L'):
    """
    Maps shoulder roll / elevation angle to TonyPi bus servo pulse range:
      Left arm (Servo 8): 0° (down) -> 500, +90° (wingspan) -> 800, +180° (up) -> 950
      Right arm (Servo 16): 0° (down) -> 500, -90° (wingspan) -> 200, -180° (up) -> 50
    """
    if side == 'L':
        norm = np.clip(roll_deg / 180.0, 0.0, 1.0)
        target_pulse = int(500 + norm * (950 - 500))
        return int(np.clip(target_pulse, 50, 980))
    else:
        norm = np.clip(-roll_deg / 180.0, 0.0, 1.0)
        target_pulse = int(500 - norm * (500 - 50))
        return int(np.clip(target_pulse, 50, 980))


def map_shoulder_plane_to_tonypi_pulse(plane_angle, side='L'):
    """
    Backward-compatible wrapper mapping plane angle to pitch pulse.
    """
    return map_shoulder_pitch_to_tonypi_pulse(-plane_angle, side=side)

# ==========================================
# 2. ASYNCHRONOUS CLINICAL REASONING (Nemotron)
# ==========================================

class NemotronSupervisor:
    """
    Simulates / calls the Nemotron API asynchronously.
    Evaluates dual-arm form without blocking the 30-60 FPS camera thread.
    """
    def __init__(self):
        self.latest_feedback = "Ready. Begin bicep curls or shoulder abduction."
        self.is_evaluating = False

    def async_evaluate(self, side, current_angle, min_rom, max_rom, rep_count):
        if self.is_evaluating:
            return
        thread = threading.Thread(
            target=self._run_inference,
            args=(side, current_angle, min_rom, max_rom, rep_count),
            daemon=True
        )
        thread.start()

    def _run_inference(self, side, current_angle, min_rom, max_rom, rep_count):
        self.is_evaluating = True
        try:
            # Simulate inference round-trip / local Nemotron NIM HTTP request
            time.sleep(0.08)
            side_name = "Left" if side == 'L' else "Right"
            if max_rom < 130:
                self.latest_feedback = f"Nemotron: {side_name} arm full extension not reached. Lengthen arm."
            elif min_rom > 65:
                self.latest_feedback = f"Nemotron: {side_name} arm incomplete curl. Squeeze closer to shoulder."
            else:
                self.latest_feedback = f"Nemotron: Good form on {side_name} arm! Rep {rep_count} logged."
        finally:
            self.is_evaluating = False

# ==========================================
# 3. HUD RENDERING HELPERS
# ==========================================

def draw_hud_panel(frame, left_data, right_data, feedback, fps, sim_status):
    """
    Renders an informative side telemetry dashboard attached to the OpenCV frame.
    Creates a 960x480 canvas: 640x480 video on left, 320x480 dark telemetry card on right.
    Continuous joint telemetry: Elbow flexion, Shoulder Roll (elevation), Shoulder Pitch.
    """
    h, w = frame.shape[:2]
    panel_w = 320
    canvas = np.zeros((h, w + panel_w, 3), dtype=np.uint8)
    
    # Copy video frame to left side
    canvas[:, :w] = frame
    
    # Dark panel styling
    panel = canvas[:, w:]
    panel[:] = (20, 20, 24)  # Sleek dark background
    
    # Subtle panel divider line
    cv2.line(canvas, (w, 0), (w, h), (45, 45, 55), 2)
    
    # Title & Header
    cv2.putText(panel, "TONYPI PT TELEMETRY", (18, 30),
                cv2.FONT_HERSHEY_DUPLEX, 0.65, (0, 230, 255), 1, cv2.LINE_AA)
    cv2.putText(panel, f"FPS: {fps:.1f} | 3D: {sim_status}", (18, 52),
                cv2.FONT_HERSHEY_SIMPLEX, 0.42, (160, 160, 170), 1, cv2.LINE_AA)
    
    cv2.line(panel, (15, 60), (panel_w - 15, 60), (50, 50, 60), 1)

    # ---------------- LEFT ARM CARD (Cyan Theme) ----------------
    card_l_y = 66
    cv2.rectangle(panel, (12, card_l_y), (panel_w - 12, card_l_y + 140), (35, 35, 45), -1)
    cv2.rectangle(panel, (12, card_l_y), (panel_w - 12, card_l_y + 140), (255, 200, 0), 1) # Cyan border
    
    cv2.putText(panel, "LEFT ARM [S6, S7, S8]", (20, card_l_y + 22),
                cv2.FONT_HERSHEY_DUPLEX, 0.46, (255, 220, 0), 1, cv2.LINE_AA)
    
    l_elb = left_data.get('elbow', 180.0)
    l_roll = left_data.get('roll', 0.0)
    l_pitch = left_data.get('pitch', -left_data.get('plane', 0.0))
    p_l_elb = left_data.get('pulse_elbow', 800)
    p_l_pitch = left_data.get('pulse_pitch', left_data.get('pulse_plane', 800))
    p_l_roll = left_data.get('pulse_roll', 500)
    reps_l = left_data.get('reps', 0)

    cv2.putText(panel, f"Elbow Flex:   {l_elb:5.1f} deg", (20, card_l_y + 44),
                cv2.FONT_HERSHEY_SIMPLEX, 0.42, (220, 220, 220), 1, cv2.LINE_AA)
    cv2.putText(panel, f"Shoulder Roll: {l_roll:5.1f} deg", (20, card_l_y + 64),
                cv2.FONT_HERSHEY_SIMPLEX, 0.42, (220, 220, 220), 1, cv2.LINE_AA)
    cv2.putText(panel, f"Shoulder Pitch:{l_pitch:5.1f} deg", (20, card_l_y + 84),
                cv2.FONT_HERSHEY_SIMPLEX, 0.42, (220, 220, 220), 1, cv2.LINE_AA)

    # Mini servo pulse readout & reps
    cv2.putText(panel, f"S6:{p_l_elb} | S7:{p_l_pitch} | S8:{p_l_roll}", (20, card_l_y + 108),
                cv2.FONT_HERSHEY_SIMPLEX, 0.38, (180, 240, 255), 1, cv2.LINE_AA)
    cv2.putText(panel, f"Reps: {reps_l}", (20, card_l_y + 128),
                cv2.FONT_HERSHEY_DUPLEX, 0.48, (0, 255, 180), 1, cv2.LINE_AA)

    # ---------------- RIGHT ARM CARD (Orange/Amber Theme) ----------------
    card_r_y = 214
    cv2.rectangle(panel, (12, card_r_y), (panel_w - 12, card_r_y + 140), (35, 35, 45), -1)
    cv2.rectangle(panel, (12, card_r_y), (panel_w - 12, card_r_y + 140), (0, 140, 255), 1) # Amber border
    
    cv2.putText(panel, "RIGHT ARM [S14, S15, S16]", (20, card_r_y + 22),
                cv2.FONT_HERSHEY_DUPLEX, 0.46, (0, 160, 255), 1, cv2.LINE_AA)
    
    r_elb = right_data.get('elbow', 180.0)
    r_roll = right_data.get('roll', 0.0)
    r_pitch = right_data.get('pitch', -right_data.get('plane', 0.0))
    p_r_elb = right_data.get('pulse_elbow', 200)
    p_r_pitch = right_data.get('pulse_pitch', right_data.get('pulse_plane', 200))
    p_r_roll = right_data.get('pulse_roll', 500)
    reps_r = right_data.get('reps', 0)

    cv2.putText(panel, f"Elbow Flex:   {r_elb:5.1f} deg", (20, card_r_y + 44),
                cv2.FONT_HERSHEY_SIMPLEX, 0.42, (220, 220, 220), 1, cv2.LINE_AA)
    cv2.putText(panel, f"Shoulder Roll: {r_roll:5.1f} deg", (20, card_r_y + 64),
                cv2.FONT_HERSHEY_SIMPLEX, 0.42, (220, 220, 220), 1, cv2.LINE_AA)
    cv2.putText(panel, f"Shoulder Pitch:{r_pitch:5.1f} deg", (20, card_r_y + 84),
                cv2.FONT_HERSHEY_SIMPLEX, 0.42, (220, 220, 220), 1, cv2.LINE_AA)

    # Mini servo pulse readout & reps
    cv2.putText(panel, f"S14:{p_r_elb} | S15:{p_r_pitch} | S16:{p_r_roll}", (20, card_r_y + 108),
                cv2.FONT_HERSHEY_SIMPLEX, 0.38, (255, 200, 150), 1, cv2.LINE_AA)
    cv2.putText(panel, f"Reps: {reps_r}", (20, card_r_y + 128),
                cv2.FONT_HERSHEY_DUPLEX, 0.48, (0, 255, 180), 1, cv2.LINE_AA)

    # ---------------- CLINICAL REASONING BOX ----------------
    feedback_y = 362
    cv2.rectangle(panel, (12, feedback_y), (panel_w - 12, h - 10), (28, 28, 36), -1)
    cv2.rectangle(panel, (12, feedback_y), (panel_w - 12, h - 10), (80, 80, 95), 1)
    cv2.putText(panel, "CLINICAL EVALUATION", (20, feedback_y + 20),
                cv2.FONT_HERSHEY_DUPLEX, 0.40, (140, 180, 255), 1, cv2.LINE_AA)
    
    # Word wrap feedback string
    words = feedback.split(' ')
    lines = []
    curr = ""
    for w_item in words:
        if len(curr + " " + w_item) < 32:
            curr = curr + (" " if curr else "") + w_item
        else:
            lines.append(curr)
            curr = w_item
    if curr:
        lines.append(curr)
        
    for idx, l_text in enumerate(lines[:3]):
        cv2.putText(panel, l_text, (20, feedback_y + 40 + idx * 18),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.38, (230, 230, 230), 1, cv2.LINE_AA)

    # Bottom video banner
    cv2.rectangle(canvas, (10, h - 45), (w - 10, h - 10), (20, 20, 25), -1)
    cv2.putText(canvas, feedback, (20, h - 22),
                cv2.FONT_HERSHEY_SIMPLEX, 0.52, (255, 255, 255), 1, cv2.LINE_AA)

    return canvas

# ==========================================
# 4. MAIN RUNTIME LOOP
# ==========================================

def parse_args():
    parser = argparse.ArgumentParser(description="Dual-Arm PT Robot Pose Mirroring MVP")
    parser.add_argument("--cam", type=int, default=0, help="Webcam device index (default: 0)")
    parser.add_argument("--source", type=str, default=None, help="Video source (camera index or video file path)")
    parser.add_argument("--no-sim", action="store_true", help="Disable TonyPi 3D digital twin window")
    parser.add_argument("--sync-sim", action="store_true", help="Run 3D simulation in synchronous single-thread mode")
    parser.add_argument("--conf", type=float, default=0.45, help="Pose keypoint confidence threshold")
    parser.add_argument("--alpha", type=float, default=0.35, help="EMA smoothing factor (0 to 1)")
    return parser.parse_args()


def main():
    args = parse_args()

    # Load YOLOv11 Pose model
    model = YOLO("yolo11n-pose.pt")

    # Determine video source
    video_src = args.cam
    if args.source is not None:
        video_src = int(args.source) if args.source.isdigit() else args.source

    cap = cv2.VideoCapture(video_src)
    if isinstance(video_src, int):
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    if not cap.isOpened():
        print(f"[ERROR] Could not open video source '{video_src}'.")
        print("Please check camera connection/permissions or provide a video file path via --source <path>.")
        return

    supervisor = NemotronSupervisor()

    # 3D Digital Twin Simulation
    digital_twin = None
    sim_status = "Disabled"
    if not args.no_sim:
        try:
            from tonypi_sim import TonyPiDigitalTwin
            use_process = not args.sync_sim
            digital_twin = TonyPiDigitalTwin(use_process=use_process)
            digital_twin.start()
            sim_status = "Connected"
            mode_desc = "synchronous" if args.sync_sim else "multiprocess"
            print(f"[INFO] TonyPi 3D Digital Twin initialized successfully ({mode_desc} mode).")
        except Exception as e:
            sim_status = "Offline"
            print(f"[WARN] TonyPi 3D Digital Twin could not be started: {e}")

    # Tracking state & EMA filters (alpha = 0.35)
    alpha = args.alpha
    filt_l_elbow = 180.0
    filt_r_elbow = 180.0
    filt_l_pitch = 0.0
    filt_r_pitch = 0.0
    filt_l_roll = 0.0
    filt_r_roll = 0.0
    filt_l_plane = 0.0
    filt_r_plane = 0.0
    max_l_ratio = 0.85
    max_r_ratio = 0.85

    # Repetition detection & ROM tracking
    rep_count_l = 0
    in_rep_l = False
    min_rom_l = 180.0
    max_rom_l = 0.0

    rep_count_r = 0
    in_rep_r = False
    min_rom_r = 180.0
    max_rom_r = 0.0

    fps = 30.0
    prev_time = time.time()

    print("\n--- Starting PT Robot Pose MVP (Dual-Arm + 3D Simulation) ---")
    print("Tracking: Left Arm (5,7,9), Right Arm (6,8,10), Torso Anchors (11,12)")
    print("Press 'q' in the OpenCV window to exit.\n")

    try:
        while cap.isOpened():
            success, frame = cap.read()
            if not success:
                break

            # Calculate FPS
            curr_time = time.time()
            dt = curr_time - prev_time
            prev_time = curr_time
            if dt > 0:
                fps = 0.9 * fps + 0.1 * (1.0 / dt)

            # Mirror display for natural mirror interaction
            frame = cv2.flip(frame, 1)
            h, w, _ = frame.shape

            results = model(frame, verbose=False, conf=args.conf)

            # Default telemetry values
            pulse_l_elbow = map_elbow_to_tonypi_pulse(filt_l_elbow, side='L')
            pulse_l_pitch = map_shoulder_pitch_to_tonypi_pulse(filt_l_pitch, side='L')
            pulse_l_roll = map_shoulder_roll_to_tonypi_pulse(filt_l_roll, side='L')
            pulse_l_plane = pulse_l_pitch

            pulse_r_elbow = map_elbow_to_tonypi_pulse(filt_r_elbow, side='R')
            pulse_r_pitch = map_shoulder_pitch_to_tonypi_pulse(filt_r_pitch, side='R')
            pulse_r_roll = map_shoulder_roll_to_tonypi_pulse(filt_r_roll, side='R')
            pulse_r_plane = pulse_r_pitch

            if results and len(results[0].keypoints) > 0:
                # Extract first detected person (COCO 17 keypoints)
                keypoints = results[0].keypoints.data[0].cpu().numpy()

                # Keypoint indices:
                # Left Arm: Shoulder (5), Elbow (7), Wrist (9)
                # Right Arm: Shoulder (6), Elbow (8), Wrist (10)
                # Torso anchors: Left Hip (11), Right Hip (12)
                l_sh = keypoints[5][:2]
                l_el = keypoints[7][:2]
                l_wr = keypoints[9][:2]
                conf_l = min(keypoints[5][2], keypoints[7][2], keypoints[9][2])

                r_sh = keypoints[6][:2]
                r_el = keypoints[8][:2]
                r_wr = keypoints[10][:2]
                conf_r = min(keypoints[6][2], keypoints[8][2], keypoints[10][2])

                # Torso anchors
                l_hip = keypoints[11][:2]
                r_hip = keypoints[12][:2]
                conf_torso = min(keypoints[5][2], keypoints[6][2], keypoints[11][2], keypoints[12][2])

                # Draw Torso Baseline & Coronal Orientation
                if min(keypoints[5][2], keypoints[6][2]) > 0.4:
                    p_ls = (int(l_sh[0]), int(l_sh[1]))
                    p_rs = (int(r_sh[0]), int(r_sh[1]))
                    cv2.line(frame, p_ls, p_rs, (220, 220, 220), 2, cv2.LINE_AA)
                    cv2.circle(frame, p_ls, 5, (255, 220, 0), -1)
                    cv2.circle(frame, p_rs, 5, (0, 140, 255), -1)

                    # Draw outward baseline reference ticks
                    s_diff = l_sh - r_sh
                    w_sh = float(np.linalg.norm(s_diff))
                    if w_sh > 1e-4:
                        u_l = s_diff / w_sh
                        tick_l = (int(l_sh[0] + u_l[0] * 25), int(l_sh[1] + u_l[1] * 25))
                        tick_r = (int(r_sh[0] - u_l[0] * 25), int(r_sh[1] - u_l[1] * 25))
                        cv2.line(frame, p_ls, tick_l, (255, 220, 0), 1, cv2.LINE_AA)
                        cv2.line(frame, p_rs, tick_r, (0, 140, 255), 1, cv2.LINE_AA)

                # Torso hips anchor lines
                if conf_torso > 0.4:
                    cv2.line(frame, (int(l_sh[0]), int(l_sh[1])), (int(l_hip[0]), int(l_hip[1])), (80, 80, 90), 1)
                    cv2.line(frame, (int(r_sh[0]), int(r_sh[1])), (int(r_hip[0]), int(r_hip[1])), (80, 80, 90), 1)

                # ---------------- LEFT ARM PROCESSING ----------------
                if conf_l > 0.4:
                    raw_pitch_l, raw_roll_l, raw_elbow_l, max_l_ratio = calculate_arm_3d_angles(
                        l_sh, l_el, l_wr, l_sh, r_sh,
                        hip_left=l_hip if conf_torso > 0.4 else None,
                        hip_right=r_hip if conf_torso > 0.4 else None,
                        side='L', max_ratio=max_l_ratio
                    )

                    # EMA smoothing
                    filt_l_elbow = alpha * raw_elbow_l + (1.0 - alpha) * filt_l_elbow
                    filt_l_pitch = alpha * raw_pitch_l + (1.0 - alpha) * filt_l_pitch
                    filt_l_roll = alpha * raw_roll_l + (1.0 - alpha) * filt_l_roll
                    filt_l_plane = -filt_l_pitch

                    pulse_l_elbow = map_elbow_to_tonypi_pulse(filt_l_elbow, side='L')
                    pulse_l_pitch = map_shoulder_pitch_to_tonypi_pulse(filt_l_pitch, side='L')
                    pulse_l_roll = map_shoulder_roll_to_tonypi_pulse(filt_l_roll, side='L')
                    pulse_l_plane = pulse_l_pitch

                    # Left Rep & ROM Tracking
                    min_rom_l = min(min_rom_l, filt_l_elbow)
                    max_rom_l = max(max_rom_l, filt_l_elbow)
                    if filt_l_elbow < 55 and not in_rep_l:
                        in_rep_l = True
                    elif filt_l_elbow > 140 and in_rep_l:
                        in_rep_l = False
                        rep_count_l += 1
                        supervisor.async_evaluate('L', filt_l_elbow, min_rom_l, max_rom_l, rep_count_l)
                        min_rom_l, max_rom_l = 180.0, 0.0

                    # Render Left Skeleton (Cyan / Neon Green)
                    p_s = (int(l_sh[0]), int(l_sh[1]))
                    p_e = (int(l_el[0]), int(l_el[1]))
                    p_w = (int(l_wr[0]), int(l_wr[1]))
                    cv2.line(frame, p_s, p_e, (255, 220, 0), 3, cv2.LINE_AA)
                    cv2.line(frame, p_e, p_w, (255, 220, 0), 3, cv2.LINE_AA)
                    for pt in [p_s, p_e, p_w]:
                        cv2.circle(frame, pt, 6, (0, 255, 128), -1)
                        cv2.circle(frame, pt, 8, (255, 255, 255), 1)

                # ---------------- RIGHT ARM PROCESSING ----------------
                if conf_r > 0.4:
                    raw_pitch_r, raw_roll_r, raw_elbow_r, max_r_ratio = calculate_arm_3d_angles(
                        r_sh, r_el, r_wr, l_sh, r_sh,
                        hip_left=l_hip if conf_torso > 0.4 else None,
                        hip_right=r_hip if conf_torso > 0.4 else None,
                        side='R', max_ratio=max_r_ratio
                    )

                    # EMA smoothing
                    filt_r_elbow = alpha * raw_elbow_r + (1.0 - alpha) * filt_r_elbow
                    filt_r_pitch = alpha * raw_pitch_r + (1.0 - alpha) * filt_r_pitch
                    filt_r_roll = alpha * raw_roll_r + (1.0 - alpha) * filt_r_roll
                    filt_r_plane = -filt_r_pitch

                    pulse_r_elbow = map_elbow_to_tonypi_pulse(filt_r_elbow, side='R')
                    pulse_r_pitch = map_shoulder_pitch_to_tonypi_pulse(filt_r_pitch, side='R')
                    pulse_r_roll = map_shoulder_roll_to_tonypi_pulse(filt_r_roll, side='R')
                    pulse_r_plane = pulse_r_pitch

                    # Right Rep & ROM Tracking
                    min_rom_r = min(min_rom_r, filt_r_elbow)
                    max_rom_r = max(max_rom_r, filt_r_elbow)
                    if filt_r_elbow < 55 and not in_rep_r:
                        in_rep_r = True
                    elif filt_r_elbow > 140 and in_rep_r:
                        in_rep_r = False
                        rep_count_r += 1
                        supervisor.async_evaluate('R', filt_r_elbow, min_rom_r, max_rom_r, rep_count_r)
                        min_rom_r, max_rom_r = 180.0, 0.0

                    # Render Right Skeleton (Bright Orange / Vivid Red)
                    p_s = (int(r_sh[0]), int(r_sh[1]))
                    p_e = (int(r_el[0]), int(r_el[1]))
                    p_w = (int(r_wr[0]), int(r_wr[1]))
                    cv2.line(frame, p_s, p_e, (0, 140, 255), 3, cv2.LINE_AA)
                    cv2.line(frame, p_e, p_w, (0, 140, 255), 3, cv2.LINE_AA)
                    for pt in [p_s, p_e, p_w]:
                        cv2.circle(frame, pt, 6, (30, 30, 255), -1)
                        cv2.circle(frame, pt, 8, (255, 255, 255), 1)

            # Update 3D Digital Twin simulation
            if digital_twin is not None:
                if hasattr(digital_twin, "is_window_open") and not digital_twin.is_window_open():
                    sim_status = "Closed"
                else:
                    sim_status = "Connected"
                digital_twin.update_arms(
                    l_pitch_deg=filt_l_pitch,
                    l_roll_deg=filt_l_roll,
                    l_elbow_deg=filt_l_elbow,
                    r_pitch_deg=filt_r_pitch,
                    r_roll_deg=filt_r_roll,
                    r_elbow_deg=filt_r_elbow
                )

            # Assemble telemetry data dictionaries
            left_telemetry = {
                'elbow': filt_l_elbow,
                'pitch': filt_l_pitch,
                'roll': filt_l_roll,
                'plane': filt_l_plane,
                'pulse_elbow': pulse_l_elbow,
                'pulse_pitch': pulse_l_pitch,
                'pulse_roll': pulse_l_roll,
                'pulse_plane': pulse_l_plane,
                'reps': rep_count_l,
            }
            right_telemetry = {
                'elbow': filt_r_elbow,
                'pitch': filt_r_pitch,
                'roll': filt_r_roll,
                'plane': filt_r_plane,
                'pulse_elbow': pulse_r_elbow,
                'pulse_pitch': pulse_r_pitch,
                'pulse_roll': pulse_r_roll,
                'pulse_plane': pulse_r_plane,
                'reps': rep_count_r,
            }

            # Build and display dashboard frame
            display_frame = draw_hud_panel(
                frame, left_telemetry, right_telemetry,
                supervisor.latest_feedback, fps, sim_status
            )

            cv2.imshow("TonyPi PT Pose Telemetry & Mirroring", display_frame)
            if cv2.waitKey(1) & 0xFF == ord('q'):
                break

    finally:
        cap.release()
        cv2.destroyAllWindows()
        if digital_twin is not None:
            digital_twin.close()

if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()