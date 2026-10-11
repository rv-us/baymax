import cv2
import numpy as np
import time
import threading
from ultralytics import YOLO

# ==========================================
# 1. KINEMATIC & RETARGETING CALCULATIONS
# ==========================================

def calculate_angle(p1, p2, p3):
    """
    Computes the interior 2D angle (in degrees) at vertex p2 (the elbow)
    between vector (p1 - p2) (shoulder) and (p3 - p2) (wrist).
    """
    v1 = np.array([p1[0] - p2[0], p1[1] - p2[1]], dtype=np.float32)
    v2 = np.array([p3[0] - p2[0], p3[1] - p2[1]], dtype=np.float32)

    norm1 = np.linalg.norm(v1)
    norm2 = np.linalg.norm(v2)
    if norm1 < 1e-4 or norm2 < 1e-4:
        return 0.0

    cosine = np.dot(v1, v2) / (norm1 * norm2)
    cosine = np.clip(cosine, -1.0, 1.0)
    angle_rad = np.arccos(cosine)
    return float(np.degrees(angle_rad))

def map_angle_to_tonypi_pulse(human_angle, min_angle=30.0, max_angle=160.0):
    """
    Maps human elbow angle to TonyPi bus servo pulse range (0 - 1000).
    Hiwonder LX-824HV servos: 0 to 1000 corresponds to 0° to 240°.
    500 is neutral 120°.
    """
    # Clamp input to physical exercise boundaries
    clamped = np.clip(human_angle, min_angle, max_angle)
    norm = (clamped - min_angle) / (max_angle - min_angle)
    
    # Example mapping for TonyPi arm: adjust min/max pulses to your arm mounting
    target_pulse = int(200 + norm * (800 - 200))
    return int(np.clip(target_pulse, 0, 1000))

# ==========================================
# 2. ASYNCHRONOUS CLINICAL REASONING (Nemotron)
# ==========================================

class NemotronSupervisor:
    """
    Simulates / calls the Nemotron API asynchronously.
    Evaluates form without blocking the 30-60 FPS camera thread.
    """
    def __init__(self):
        self.latest_feedback = "Ready. Begin bicep curls."
        self.is_evaluating = False

    def async_evaluate(self, current_angle, min_rom, max_rom, rep_count):
        if self.is_evaluating:
            return
        thread = threading.Thread(
            target=self._run_inference,
            args=(current_angle, min_rom, max_rom, rep_count),
            daemon=True
        )
        thread.start()

    def _run_inference(self, current_angle, min_rom, max_rom, rep_count):
        self.is_evaluating = True
        try:
            # When deploying to GX10: Make an HTTP request to your local Nemotron NIM:
            # res = requests.post("http://localhost:8000/v1/chat/completions", json={...})
            time.sleep(0.1) # Simulate inference round-trip
            
            # Clinical heuristic simulation
            if max_rom < 130:
                self.latest_feedback = "Nemotron: Full extension not reached. Lengthen arm."
            elif min_rom > 60:
                self.latest_feedback = "Nemotron: Incomplete curl. Squeeze closer to shoulder."
            else:
                self.latest_feedback = f"Nemotron: Good form. Rep {rep_count} logged."
        finally:
            self.is_evaluating = False

# ==========================================
# 3. MAIN RUNTIME LOOP
# ==========================================

def main():
    # Load lightweight nano pose model (downloads automatically on first run)
    model = YOLO("yolo11n-pose.pt")
    
    cap = cv2.VideoCapture(0)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    supervisor = NemotronSupervisor()

    # Tracking state & filters
    filtered_angle = 180.0
    alpha = 0.35  # EMA smoothing factor (0 = static, 1 = no smoothing)
    
    rep_count = 0
    in_rep = False
    session_min_rom = 180.0
    session_max_rom = 0.0

    print("\n--- Starting PT Robot Pose MVP ---")
    print("Press 'q' in the window to quit.\n")

    while cap.isOpened():
        success, frame = cap.read()
        if not success:
            break

        # Mirror display for natural interaction
        frame = cv2.flip(frame, 1)
        h, w, _ = frame.shape

        results = model(frame, verbose=False, conf=0.5)

        if results and len(results[0].keypoints) > 0:
            # Extract first detected person
            keypoints = results[0].keypoints.data[0].cpu().numpy()
            
            # COCO Keypoint IDs for Right Arm: 6 (Shoulder), 8 (Elbow), 10 (Wrist)
            # Left Arm alternatives: 5 (Shoulder), 7 (Elbow), 9 (Wrist)
            shoulder = keypoints[6][:2]
            elbow = keypoints[8][:2]
            wrist = keypoints[10][:2]
            conf = min(keypoints[6][2], keypoints[8][2], keypoints[10][2])

            if conf > 0.5:
                raw_angle = calculate_angle(shoulder, elbow, wrist)
                
                # Apply EMA filter to eliminate camera jitter
                filtered_angle = alpha * raw_angle + (1 - alpha) * filtered_angle
                servo_pulse = map_angle_to_tonypi_pulse(filtered_angle)

                # Repetition detection & ROM tracking
                session_min_rom = min(session_min_rom, filtered_angle)
                session_max_rom = max(session_max_rom, filtered_angle)

                if filtered_angle < 55 and not in_rep:
                    in_rep = True
                elif filtered_angle > 140 and in_rep:
                    in_rep = False
                    rep_count += 1
                    # Trigger Nemotron evaluation on rep completion
                    supervisor.async_evaluate(filtered_angle, session_min_rom, session_max_rom, rep_count)
                    session_min_rom = 180.0
                    session_max_rom = 0.0

                # Render skeleton overlay
                s_pt = (int(shoulder[0]), int(shoulder[1]))
                e_pt = (int(elbow[0]), int(elbow[1]))
                w_pt = (int(wrist[0]), int(wrist[1]))
                cv2.line(frame, s_pt, e_pt, (0, 255, 0), 3)
                cv2.line(frame, e_pt, w_pt, (0, 255, 0), 3)
                for pt in [s_pt, e_pt, w_pt]:
                    cv2.circle(frame, pt, 6, (0, 0, 255), -1)

                # Render diagnostic metrics
                cv2.putText(frame, f"Elbow Angle: {filtered_angle:.1f} deg", (20, 40),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
                cv2.putText(frame, f"TonyPi Pulse: {servo_pulse} [ID:6]", (20, 75),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 200, 0), 2)
                cv2.putText(frame, f"Reps: {rep_count}", (20, 110),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

        # Render clinical reasoning feedback
        cv2.rectangle(frame, (10, h - 50), (w - 10, h - 10), (30, 30, 30), -1)
        cv2.putText(frame, supervisor.latest_feedback, (20, h - 22),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 2)

        cv2.imshow("PT Pose Mirroring MVP", frame)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

    cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()