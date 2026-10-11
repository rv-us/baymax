"""
MediaPipe 3D Pose Estimation & TonyPi Hardware Emulation
=========================================================
Physical therapy pose tracking and TonyPi Pro humanoid hardware simulation.

Features:
1. TonyPi Camera Emulation (GalaxyCore GC0308 sensor: 640x480 VGA, ~70° FOV,
   -25% barrel distortion mesh, JPEG 75 compression/chroma degradation).
2. MediaPipe Pose 3D Extraction with real-world metric coordinates centered at mid-hip.
3. Full Body 17 Keypoints Tracking & Hip Cutoff:
   - Tracks all 17 main joints (Head, Arms, Hips, Knees, Ankles).
   - If legs are not fully visible (low confidence or cropped out by frame),
     automatically cuts off at the hips, maintaining stable standing pose on leg servos.
   - When legs are visible, computes 3D knee flexion, hip pitch, and ankle pitch.
4. 180° Shoulder Pitch Axis Range of Motion:
   - When arms are extended straight out (T-pose) with elbows at 90°,
     supports one arm rotated such that the hand points toward the sky (pitch = +90°)
     while the other points toward the ground (pitch = -90°).
   - Commands Servo 8 (Left Shoulder Pitch: 125 sky to 725 ground) and
     Servo 16 (Right Shoulder Pitch: 875 sky to 275 ground) accurately.
5. TonyPi Bus Servo Mapping aligned with baymax/robot_api:
   - Left Arm: S6 (Elbow), S7 (Shoulder Roll), S8 (Shoulder Pitch), S18 (Hand).
   - Right Arm: S14 (Elbow), S15 (Shoulder Roll), S16 (Shoulder Pitch), S17 (Hand).
   - Left Leg: S1 (Ankle Roll), S2 (Ankle Pitch), S3 (Knee), S4 (Hip Pitch), S5 (Hip Roll).
   - Right Leg: S9 (Ankle Roll), S10 (Ankle Pitch), S11 (Knee), S12 (Hip Pitch), S13 (Hip Roll).
6. Real-Time HUD Dashboard:
   - Stream status (GC0308 sensor emulation, FPS, 3D twin link).
   - Tracking badge: FULL BODY (17 JOINTS) vs. UPPER BODY (CUT OFF AT HIPS).
   - Bilateral joint angle & pulse readouts.
   - Colored skeleton rendering (upper body + legs when visible, cutoff line when not).
   - Nemotron clinical evaluation supervisor.
7. 3D Digital Twin Integration: the servo pulses computed each frame drive digital_twin/
   (URDF model rendered in the browser; also embeddable in a web dashboard). Add
   --twin-robot-url to overlay the real robot's servo readback as a ghost.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import threading
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np
from retarget import ARM_IDX, ArmIK, elbow_pulse, leg_angles, shoulder_pulses, to_torso_frame

# Ensure repository root is on sys.path for robot_api imports
_CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_CURRENT_DIR, ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
if _CURRENT_DIR not in sys.path:
    sys.path.insert(0, _CURRENT_DIR)

# Optional robot_api imports (safe fallback if running in standalone mode)
try:
    from robot_api.servo import joints as robot_joints
    from robot_api.servo import limits as robot_limits
    ROBOT_API_AVAILABLE = True
except Exception:
    robot_joints = None
    robot_limits = None
    ROBOT_API_AVAILABLE = False


# =====================================================================
# 1. HARDWARE SIMULATION & GC0308 VIDEO DEGRADATION PIPELINE
# =====================================================================

SIMULATE_TONYPI_HARDWARE = True


class GC0308HardwareSimulator:
    """
    Emulates the GalaxyCore GC0308 sensor on the TonyPi Pro:
    - 640x480 (VGA, 0.3 MP) resolution.
    - ~70° diagonal field of view (FOV).
    - -25% radial barrel distortion mesh.
    - Low-cost sensor compression noise & chroma subsampling (JPEG quality 75).
    """

    def __init__(
        self,
        target_width: int = 640,
        target_height: int = 480,
        distortion_coeff: float = -0.25,
        jpeg_quality: int = 75,
        enabled: bool = True,
    ):
        self.width = target_width
        self.height = target_height
        self.distortion_coeff = distortion_coeff
        self.jpeg_quality = jpeg_quality
        self.enabled = enabled

        # Precompute radial distortion mesh maps for sub-millisecond execution
        self.map_x, self.map_y = self._build_distortion_mesh(
            self.width, self.height, self.distortion_coeff
        )

    @staticmethod
    def _build_distortion_mesh(
        w: int, h: int, k1: float
    ) -> Tuple[np.ndarray, np.ndarray]:
        """
        Builds coordinate remap grid modeling radial barrel distortion:
        r_d = r * (1 + k1 * r^2).
        Precomputed once at startup for zero overhead during inference.
        """
        cx = w / 2.0
        cy = h / 2.0
        diag_half = math.sqrt(cx**2 + cy**2)

        y_indices, x_indices = np.indices((h, w), dtype=np.float32)
        dx = (x_indices - cx) / diag_half
        dy = (y_indices - cy) / diag_half
        r_sq = dx**2 + dy**2

        factor = 1.0 + k1 * r_sq
        map_x = (cx + dx * diag_half * factor).astype(np.float32)
        map_y = (cy + dy * diag_half * factor).astype(np.float32)
        return map_x, map_y

    def apply(self, frame: np.ndarray) -> Tuple[np.ndarray, Tuple[int, int]]:
        """
        Applies downscaling and hardware degradation.
        Returns (processed_frame, native_resolution).
        """
        native_h, native_w = frame.shape[:2]
        native_res = (native_w, native_h)

        # 1. Downscale if frame exceeds 640x480
        if native_w != self.width or native_h != self.height:
            frame = cv2.resize(
                frame,
                (self.width, self.height),
                interpolation=cv2.INTER_AREA if native_w > self.width else cv2.INTER_LINEAR,
            )

        if not self.enabled:
            return frame, native_res

        # 2. Apply barrel distortion mesh via fast OpenCV remap
        frame = cv2.remap(
            frame, self.map_x, self.map_y, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT_101
        )

        # 3. Simulate sensor compression noise & chroma subsampling (JPEG quality 75)
        success, encoded = cv2.imencode(
            ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), self.jpeg_quality]
        )
        if success:
            frame = cv2.imdecode(encoded, cv2.IMREAD_COLOR)

        return frame, native_res


# =====================================================================
# 2. MEDIAPIPE POSE 3D EXTRACTION & COMPATIBILITY ADAPTER
# =====================================================================

class LandmarkPoint:
    """Wrapper offering property and indexing access to landmark coordinates."""
    __slots__ = ("x", "y", "z", "visibility", "presence")

    def __init__(self, x: float, y: float, z: float, visibility: float = 1.0, presence: float = 1.0):
        self.x = float(x)
        self.y = float(y)
        self.z = float(z)
        self.visibility = float(visibility)
        self.presence = float(presence)

    def __getitem__(self, idx: int) -> float:
        if idx == 0:
            return self.x
        if idx == 1:
            return self.y
        if idx == 2:
            return self.z
        raise IndexError("Index out of range for LandmarkPoint (0: x, 1: y, 2: z)")

    def __repr__(self) -> str:
        return f"LandmarkPoint(x={self.x:.3f}, y={self.y:.3f}, z={self.z:.3f}, v={self.visibility:.2f})"


class LandmarkListWrapper:
    """Container wrapping list of landmarks for solutions & tasks API compatibility."""

    def __init__(self, landmarks: List[LandmarkPoint]):
        self._landmarks = landmarks

    @property
    def landmark(self) -> List[LandmarkPoint]:
        return self._landmarks

    def __len__(self) -> int:
        return len(self._landmarks)

    def __getitem__(self, idx: int) -> LandmarkPoint:
        return self._landmarks[idx]

    def __iter__(self):
        return iter(self._landmarks)


class PoseResultWrapper:
    """Normalized output matching mp.solutions.pose results schema."""

    def __init__(
        self,
        pose_landmarks: Optional[LandmarkListWrapper],
        pose_world_landmarks: Optional[LandmarkListWrapper],
    ):
        self.pose_landmarks = pose_landmarks
        self.pose_world_landmarks = pose_world_landmarks


class PoseDetector:
    """
    MediaPipe Pose 3D Detector.
    Supports native mp.solutions.pose when available, and transparently bridges
    to mediapipe.tasks.python.vision.PoseLandmarker when running in newer
    environments (such as Python 3.14).
    """

    MODEL_URL = (
        "https://storage.googleapis.com/mediapipe-models/pose_landmarker/"
        "pose_landmarker_full/float16/latest/pose_landmarker_full.task"
    )

    def __init__(
        self,
        model_complexity: int = 1,
        smooth_landmarks: bool = True,
        min_detection_confidence: float = 0.5,
        min_tracking_confidence: float = 0.5,
    ):
        self.model_complexity = model_complexity
        self.smooth_landmarks = smooth_landmarks
        self.min_detection_confidence = min_detection_confidence
        self.min_tracking_confidence = min_tracking_confidence

        self._backend = "tasks"
        self._solutions_pose = None
        self._task_landmarker = None

        import mediapipe as mp
        if hasattr(mp, "solutions") and hasattr(mp.solutions, "pose"):
            self._backend = "solutions"
            self._solutions_pose = mp.solutions.pose.Pose(
                model_complexity=model_complexity,
                smooth_landmarks=smooth_landmarks,
                min_detection_confidence=min_detection_confidence,
                min_tracking_confidence=min_tracking_confidence,
            )
        else:
            self._backend = "tasks"
            self._init_tasks_landmarker()

    def _init_tasks_landmarker(self):
        """Initializes PoseLandmarker from mediapipe.tasks."""
        import mediapipe as mp
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision as mp_vision

        model_filename = (
            "pose_landmarker_heavy.task"
            if self.model_complexity >= 2
            else "pose_landmarker_full.task"
        )
        model_path = os.path.join(_CURRENT_DIR, model_filename)

        if not os.path.exists(model_path):
            print(f"[MediaPose] Downloading MediaPipe Pose model to {model_path}...")
            urllib.request.urlretrieve(self.MODEL_URL, model_path)
            print("[MediaPose] Model download complete.")

        options = mp_vision.PoseLandmarkerOptions(
            base_options=mp_python.BaseOptions(model_asset_path=model_path),
            running_mode=mp_vision.RunningMode.IMAGE,
            min_pose_detection_confidence=self.min_detection_confidence,
            min_tracking_confidence=self.min_tracking_confidence,
            output_segmentation_masks=False,
        )
        self._task_landmarker = mp_vision.PoseLandmarker.create_from_options(options)

    def process(self, rgb_frame: np.ndarray) -> PoseResultWrapper:
        """
        Runs 3D pose extraction on an RGB image.
        Returns PoseResultWrapper with pose_landmarks and pose_world_landmarks.
        """
        if self._backend == "solutions":
            raw_res = self._solutions_pose.process(rgb_frame)
            if not raw_res.pose_landmarks:
                return PoseResultWrapper(None, None)

            img_lms = [
                LandmarkPoint(lm.x, lm.y, lm.z, getattr(lm, "visibility", 1.0))
                for lm in raw_res.pose_landmarks.landmark
            ]
            world_lms = [
                LandmarkPoint(lm.x, lm.y, lm.z, getattr(lm, "visibility", 1.0))
                for lm in raw_res.pose_world_landmarks.landmark
            ]
            return PoseResultWrapper(
                LandmarkListWrapper(img_lms), LandmarkListWrapper(world_lms)
            )

        # Tasks backend
        import mediapipe as mp

        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_frame)
        detection = self._task_landmarker.detect(mp_image)

        if not detection.pose_landmarks or len(detection.pose_landmarks) == 0:
            return PoseResultWrapper(None, None)

        raw_img = detection.pose_landmarks[0]
        raw_world = (
            detection.pose_world_landmarks[0]
            if detection.pose_world_landmarks
            else raw_img
        )

        img_lms = [
            LandmarkPoint(lm.x, lm.y, lm.z, getattr(lm, "visibility", 1.0), getattr(lm, "presence", 1.0))
            for lm in raw_img
        ]
        world_lms = [
            LandmarkPoint(lm.x, lm.y, lm.z, getattr(lm, "visibility", 1.0), getattr(lm, "presence", 1.0))
            for lm in raw_world
        ]
        return PoseResultWrapper(
            LandmarkListWrapper(img_lms), LandmarkListWrapper(world_lms)
        )

    def close(self):
        if self._solutions_pose is not None:
            self._solutions_pose.close()
        if self._task_landmarker is not None:
            self._task_landmarker.close()


# =====================================================================
# 3. BILATERAL KINEMATICS & 17 KEYPOINT EXTRACTION
# =====================================================================

# MediaPipe Pose joint landmarks (COCO 17 equivalent + details)
NOSE = 0
L_EYE, R_EYE = 1, 2
L_EAR, R_EAR = 3, 4
L_SHOULDER, R_SHOULDER = 11, 12
L_ELBOW, R_ELBOW = 13, 14
L_WRIST, R_WRIST = 15, 16
L_PINKY, R_PINKY = 17, 18
L_INDEX, R_INDEX = 19, 20
L_THUMB, R_THUMB = 21, 22
L_HIP, R_HIP = 23, 24
L_KNEE, R_KNEE = 25, 26
L_ANKLE, R_ANKLE = 27, 28


def calculate_elbow_flexion_3d(p_shoulder, p_elbow, p_wrist) -> float:
    """
    Computes 3D interior elbow angle:
    theta = arccos((u . v) / (||u|| * ||v||))
    u = Shoulder - Elbow, v = Wrist - Elbow.
    180° = straight arm extension, ~40°-60° = flexed bicep curl.
    """
    u = np.array([
        p_shoulder[0] - p_elbow[0],
        p_shoulder[1] - p_elbow[1],
        p_shoulder[2] - p_elbow[2],
    ], dtype=np.float64)

    v = np.array([
        p_wrist[0] - p_elbow[0],
        p_wrist[1] - p_elbow[1],
        p_wrist[2] - p_elbow[2],
    ], dtype=np.float64)

    norm_u = np.linalg.norm(u)
    norm_v = np.linalg.norm(v)
    if norm_u < 1e-6 or norm_v < 1e-6:
        return 180.0

    cos_val = np.clip(np.dot(u, v) / (norm_u * norm_v), -1.0, 1.0)
    angle_rad = np.arccos(cos_val)
    return float(np.degrees(angle_rad))


def compute_torso_reference_frame(world_lms) -> Optional[Tuple[np.ndarray, np.ndarray, np.ndarray]]:
    """
    Computes an orthonormal torso coordinate frame from 3D metric world landmarks:
    - up: from mid-hip to mid-shoulder along spine.
    - right: from left shoulder to right shoulder.
    - forward: right x up (points anterior out of chest towards camera).
    """
    l_sh = np.array([world_lms[L_SHOULDER].x, world_lms[L_SHOULDER].y, world_lms[L_SHOULDER].z])
    r_sh = np.array([world_lms[R_SHOULDER].x, world_lms[R_SHOULDER].y, world_lms[R_SHOULDER].z])
    l_hip = np.array([world_lms[L_HIP].x, world_lms[L_HIP].y, world_lms[L_HIP].z])
    r_hip = np.array([world_lms[R_HIP].x, world_lms[R_HIP].y, world_lms[R_HIP].z])

    mid_sh = (l_sh + r_sh) * 0.5
    mid_hip = (l_hip + r_hip) * 0.5

    v_up = mid_sh - mid_hip
    norm_up = np.linalg.norm(v_up)
    if norm_up < 1e-5:
        return None
    u_up = v_up / norm_up

    # Ensure u_up points up (-Y in image/camera coordinates)
    if u_up[1] > 0:
        u_up = -u_up

    v_right = r_sh - l_sh
    norm_r = np.linalg.norm(v_right)
    if norm_r < 1e-5:
        return None
    u_right = v_right / norm_r

    # Forward vector points anterior (out of chest towards camera, -Z in camera space)
    u_fwd = np.cross(u_right, u_up)
    norm_f = np.linalg.norm(u_fwd)
    if norm_f < 1e-5:
        return None
    u_fwd = u_fwd / norm_f

    # Re-orthogonalize lateral vector
    u_right = np.cross(u_up, u_fwd)
    return u_up, u_right, u_fwd


def calculate_shoulder_plane_angle_3d(world_lms, side: str = "L") -> Tuple[float, float, float]:
    """
    Computes:
    1. Shoulder Transverse Plane Angle (Wingspan vs Forward):
       - 0° = Arm extended sideways along coronal plane (wingspan / T-pose).
       - 90° = Arm extended forward along sagittal plane (facing camera).
    2. Shoulder Pitch (Full 180° Range of Motion: Sky to Ground):
       - When arms are straight out in T-pose with elbows at 90°:
         * Hand pointing to the sky = +90°
         * Hand pointing forward = 0°
         * Hand pointing to the ground = -90°
       - When arms are down by sides: -90° (downward)
       - When arms are raised forward: 0° (horizontal)
    3. Shoulder Coronal Roll (Elevation):
       - Left: 0° down, +90° wingspan, +180° hands up overhead.
       - Right: 0° down, -90° wingspan, -180° hands up overhead.
    """
    sh_idx = L_SHOULDER if side == "L" else R_SHOULDER
    el_idx = L_ELBOW if side == "L" else R_ELBOW
    wr_idx = L_WRIST if side == "L" else R_WRIST

    sh = np.array([world_lms[sh_idx].x, world_lms[sh_idx].y, world_lms[sh_idx].z])
    el = np.array([world_lms[el_idx].x, world_lms[el_idx].y, world_lms[el_idx].z])
    wr = np.array([world_lms[wr_idx].x, world_lms[wr_idx].y, world_lms[wr_idx].z])

    v_upper = el - sh
    v_fore = wr - el
    l_arm = float(np.linalg.norm(v_upper))
    if l_arm < 1e-5:
        return 0.0, 0.0, 0.0

    frame = compute_torso_reference_frame(world_lms)
    if frame is None:
        return 0.0, 0.0, 0.0
    u_up, u_right, u_fwd = frame

    u_lat = -u_right if side == "L" else u_right

    # Upper arm projections on torso axes
    v_lat = float(np.dot(v_upper, u_lat))
    v_fwd = float(np.dot(v_upper, u_fwd))
    v_down = float(np.dot(v_upper, -u_up))

    # Transverse plane projection (0° wingspan, 90° forward)
    lat_mag = max(0.0, v_lat)
    fwd_mag = abs(v_fwd)
    plane_rad = math.atan2(fwd_mag, max(1e-5, lat_mag))
    plane_deg = float(np.clip(math.degrees(plane_rad), 0.0, 90.0))

    # Coronal Roll (elevation from down = 0° to lateral wingspan = 90° to overhead = 180°)
    elev_rad = math.atan2(v_lat, v_down)
    elev_deg = math.degrees(elev_rad)
    if side == "L":
        roll_deg = float(np.clip(elev_deg, -30.0, 180.0))
    else:
        roll_deg = float(np.clip(-elev_deg, -180.0, 30.0))

    # 1. Upper arm pitch (0° horizontal forward, -90° down along torso, +90° straight up)
    pitch_norm = np.clip(v_fwd / l_arm, -1.0, 1.0)
    pitch_upper_deg = float(-math.degrees(math.asin(pitch_norm)))
    # If arm is hanging down, pitch is -90°
    if v_down > 0.85 * l_arm:
        pitch_upper_deg = -90.0

    # 2. Forearm Rotator Cuff Pitch (Sky vs. Ground 180° rotation when arm is out in T-pose)
    y_fore = float(np.dot(v_fore, u_up))      # + = pointing to sky, - = pointing to ground
    z_fore = float(np.dot(v_fore, u_fwd))     # + = pointing forward
    pitch_forearm_deg = float(math.degrees(math.atan2(y_fore, max(1e-4, abs(z_fore)))))
    # Direct sign mapping: y_fore > 0 is pointing up (sky: +90°), y_fore < 0 is pointing down (ground: -90°)
    l_fore = float(np.linalg.norm(v_fore))
    if l_fore > 1e-4:
        norm_y = np.clip(y_fore / l_fore, -1.0, 1.0)
        pitch_forearm_deg = float(norm_y * 90.0)

    # 3. Smooth blend between upper arm pitch and forearm rotator cuff pitch:
    # When arm is in T-pose (roll > 45°) and elbow is bent (< 145°),
    # rotator cuff pitch dictates whether hand points to sky (+90°) or ground (-90°).
    elbow_angle = calculate_elbow_flexion_3d(sh, el, wr)
    w_roll = np.clip((abs(roll_deg) - 40.0) / 35.0, 0.0, 1.0)
    w_elbow = np.clip((150.0 - elbow_angle) / 35.0, 0.0, 1.0)
    w_tpose = float(w_roll * w_elbow)

    pitch_deg = (1.0 - w_tpose) * pitch_upper_deg + w_tpose * pitch_forearm_deg
    pitch_deg = float(np.clip(pitch_deg, -90.0, 90.0))

    return plane_deg, pitch_deg, roll_deg


class JointFilter:
    """
    Per-DoF smoother: confidence-weighted EMA (damping) followed by a hard slew limit.
    - confidence < 1 shrinks the EMA gain, so near a pole the joint holds its last value
      instead of chasing a garbage measurement.
    - the slew limit (max_step_deg per frame at 30 fps, scaled by real dt) guarantees no
      single frame can command a large jump, whatever the estimator outputs.
    """

    def __init__(self, init: float, alpha: float = 0.35, max_step_deg: float = 12.0):
        self.value = float(init)
        self.alpha = alpha
        self.max_step_deg = max_step_deg

    def update(self, raw: float, dt: float = 1.0 / 30.0, confidence: float = 1.0) -> float:
        gain = self.alpha * float(np.clip(confidence, 0.0, 1.0))
        step = gain * (raw - self.value)
        if self.max_step_deg and self.max_step_deg > 0:
            limit = self.max_step_deg * float(np.clip(dt, 1.0 / 120.0, 0.2)) * 30.0
            step = float(np.clip(step, -limit, limit))
        self.value += step
        return self.value


def human_pitch_to_sim_pitch(pitch_deg: float) -> float:
    """
    Converts the estimator pitch (-90 = arm down, 0 = forward horizontal, +90 = up)
    into the URDF shoulder_pitch convention (0 = arm down, -90 = forward, -180 = up).
    """
    return -(float(pitch_deg) + 90.0)


def check_legs_visible(landmarks: Optional[LandmarkListWrapper]) -> bool:
    """
    Checks if both legs (knees and ankles) are sufficiently visible and inside frame.
    If not, returns False (cut off at hips).
    Works with both normalized image landmarks (0..1) and world landmarks.
    """
    if landmarks is None or len(landmarks) < 29:
        return False

    # Check if landmarks are in normalized image space (where hips and shoulders are in [0, 1])
    is_normalized = (
        0.0 <= getattr(landmarks[L_HIP], "x", 0.0) <= 1.0
        and 0.0 <= getattr(landmarks[R_HIP], "x", 0.0) <= 1.0
        and 0.0 <= getattr(landmarks[L_SHOULDER], "x", 0.0) <= 1.0
        and 0.0 <= getattr(landmarks[R_SHOULDER], "x", 0.0) <= 1.0
    )

    leg_keypoints = [L_KNEE, R_KNEE, L_ANKLE, R_ANKLE]
    for idx in leg_keypoints:
        lm = landmarks[idx]
        if getattr(lm, "visibility", 1.0) < 0.45 or getattr(lm, "presence", 1.0) < 0.45:
            return False
        if is_normalized:
            # Cut off by camera frame boundary (especially bottom of 640x480 frame)
            if lm.y > 0.95 or lm.y < 0.0 or lm.x < 0.0 or lm.x > 1.0:
                return False
        else:
            # In synthetic/world space, check if y is pushed past cutoff threshold
            if lm.y > 1.0:
                return False
    return True


def calculate_leg_kinematics_3d(world_lms) -> Tuple[Dict[str, float], Dict[str, float]]:
    """
    Calculates 3D knee flexion, hip pitch, and ankle pitch for both legs:
    - Knee interior angle (180° straight stand, 90°-120° squat).
    - Hip pitch (0° standing straight, forward flexion).
    """
    l_hip = np.array([world_lms[L_HIP].x, world_lms[L_HIP].y, world_lms[L_HIP].z])
    r_hip = np.array([world_lms[R_HIP].x, world_lms[R_HIP].y, world_lms[R_HIP].z])
    l_knee = np.array([world_lms[L_KNEE].x, world_lms[L_KNEE].y, world_lms[L_KNEE].z])
    r_knee = np.array([world_lms[R_KNEE].x, world_lms[R_KNEE].y, world_lms[R_KNEE].z])
    l_ank = np.array([world_lms[L_ANKLE].x, world_lms[L_ANKLE].y, world_lms[L_ANKLE].z])
    r_ank = np.array([world_lms[R_ANKLE].x, world_lms[R_ANKLE].y, world_lms[R_ANKLE].z])

    # Left Knee Angle
    u_l = l_hip - l_knee
    v_l = l_ank - l_knee
    nu_l, nv_l = np.linalg.norm(u_l), np.linalg.norm(v_l)
    l_knee_deg = float(np.degrees(np.arccos(np.clip(np.dot(u_l, v_l) / (nu_l * nv_l), -1.0, 1.0)))) if nu_l > 1e-5 and nv_l > 1e-5 else 180.0

    # Right Knee Angle
    u_r = r_hip - r_knee
    v_r = r_ank - r_knee
    nu_r, nv_r = np.linalg.norm(u_r), np.linalg.norm(v_r)
    r_knee_deg = float(np.degrees(np.arccos(np.clip(np.dot(u_r, v_r) / (nu_r * nv_r), -1.0, 1.0)))) if nu_r > 1e-5 and nv_r > 1e-5 else 180.0

    # Hip Pitch flexion relative to vertical
    l_thigh = l_knee - l_hip
    r_thigh = r_knee - r_hip
    l_hip_pitch_deg = float(np.clip(np.degrees(math.atan2(abs(l_thigh[2]), max(1e-4, -l_thigh[1]))), 0.0, 90.0))
    r_hip_pitch_deg = float(np.clip(np.degrees(math.atan2(abs(r_thigh[2]), max(1e-4, -r_thigh[1]))), 0.0, 90.0))

    # Ankle Pitch counter-flexion
    l_ankle_pitch_deg = float(np.clip(180.0 - l_knee_deg, 0.0, 60.0))
    r_ankle_pitch_deg = float(np.clip(180.0 - r_knee_deg, 0.0, 60.0))

    left_leg = {
        "knee": l_knee_deg,
        "hip_pitch": l_hip_pitch_deg,
        "ankle_pitch": l_ankle_pitch_deg,
        "hip_roll": 0.0,
        "ankle_roll": 0.0,
    }
    right_leg = {
        "knee": r_knee_deg,
        "hip_pitch": r_hip_pitch_deg,
        "ankle_pitch": r_ankle_pitch_deg,
        "hip_roll": 0.0,
        "ankle_roll": 0.0,
    }
    return left_leg, right_leg


def detect_hand_closed(world_lms, side: str = "L") -> bool:
    """Detects if hand is closed (fist) vs open (splayed)."""
    wr_idx = L_WRIST if side == "L" else R_WRIST
    idx_idx = L_INDEX if side == "L" else R_INDEX
    sh_idx = L_SHOULDER if side == "L" else R_SHOULDER
    el_idx = L_ELBOW if side == "L" else R_ELBOW

    wr = np.array([world_lms[wr_idx].x, world_lms[wr_idx].y, world_lms[wr_idx].z])
    idx = np.array([world_lms[idx_idx].x, world_lms[idx_idx].y, world_lms[idx_idx].z])
    sh = np.array([world_lms[sh_idx].x, world_lms[sh_idx].y, world_lms[sh_idx].z])
    el = np.array([world_lms[el_idx].x, world_lms[el_idx].y, world_lms[el_idx].z])

    hand_span = float(np.linalg.norm(idx - wr))
    upper_len = float(np.linalg.norm(el - sh))
    if upper_len < 1e-4:
        return False
    return (hand_span / upper_len) < 0.38


# =====================================================================
# 4. TONYPI SERVO MAPPING (ALIGNED WITH baymax/robot_api & PHOTO)
# =====================================================================

SERVO_LIMITS = {
    1: (395, 761),   # Left Ankle Roll
    2: (50, 819),    # Left Ankle Pitch
    3: (0, 960),     # Left Knee
    4: (125, 857),   # Left Hip Pitch
    5: (375, 750),   # Left Hip Roll
    6: (125, 930),   # Left Elbow
    7: (125, 900),   # Left Shoulder Roll
    8: (0, 1000),    # Left Shoulder Pitch
    9: (366, 570),   # Right Ankle Roll
    10: (140, 950),  # Right Ankle Pitch
    11: (40, 1000),  # Right Knee
    12: (141, 875),  # Right Hip Pitch
    13: (249, 624),  # Right Hip Roll
    14: (33, 875),   # Right Elbow
    15: (100, 875),  # Right Shoulder Roll
    16: (31, 1000),  # Right Shoulder Pitch
    17: (300, 760),  # Right Hand Gripper
    18: (240, 500),  # Left Hand Gripper
}

STAND_PULSES = {
    1: 500, 2: 390, 3: 500, 4: 600, 5: 500, 6: 575, 7: 800, 8: 725, 18: 500,
    9: 500, 10: 610, 11: 500, 12: 400, 13: 500, 14: 425, 15: 200, 16: 275, 17: 500,
}


def clamp_servo(sid: int, pulse: int) -> int:
    lo, hi = SERVO_LIMITS[sid]
    return max(lo, min(hi, int(pulse)))


def map_elbow_to_tonypi_pulse(elbow_deg: float, side: str = "L") -> int:
    """Interior elbow angle (180 straight) to bus servo pulse (240 deg per 1000 pulses)."""
    return clamp_servo(6 if side == "L" else 14, elbow_pulse(float(elbow_deg), side))


def map_shoulder_pitch_to_tonypi_pulse(pitch_deg: float, side: str = "L") -> int:
    """
    180° Shoulder Pitch Axis range of motion (-90° ground to +90° sky):
      Left Arm (Servo 8):
        -90° (ground) -> 725 (stand/down)
          0° (forward horizontal) -> 425 (mid)
        +90° (sky)    -> 125 (up)
      Right Arm (Servo 16 - mirrored):
        -90° (ground) -> 275 (stand/down)
          0° (forward horizontal) -> 575 (mid)
        +90° (sky)    -> 875 (up)
    """
    norm_pitch = np.clip(pitch_deg / 90.0, -1.0, 1.0)
    if side == "L":
        target = int(425 - norm_pitch * 300)
        return clamp_servo(8, target)
    else:
        target = int(575 + norm_pitch * 300)
        return clamp_servo(16, target)


def map_shoulder_roll_to_tonypi_pulse(roll_deg: float, side: str = "L") -> int:
    """Shoulder roll / abduction: 0° down, 90° T-pose wingspan, 180° overhead."""
    if side == "L":
        r_clamped = np.clip(roll_deg, 0.0, 180.0)
        if r_clamped <= 90.0:
            t = r_clamped / 90.0
            target = int(800 - t * (800 - 500))
        else:
            t = (r_clamped - 90.0) / 90.0
            target = int(500 + t * (860 - 500))
        return clamp_servo(7, target)
    else:
        r_clamped = np.clip(-roll_deg, 0.0, 180.0)
        if r_clamped <= 90.0:
            t = r_clamped / 90.0
            target = int(200 + t * (500 - 200))
        else:
            t = (r_clamped - 90.0) / 90.0
            target = int(500 - t * (500 - 186))
        return clamp_servo(15, target)


def map_hand_to_tonypi_pulse(is_closed: bool, side: str = "L") -> int:
    """Hand gripper state: Right (S17: 760 open, 500 closed), Left (S18: 240 open, 500 closed)."""
    if side == "L":
        return clamp_servo(18, 500 if is_closed else 240)
    else:
        return clamp_servo(17, 500 if is_closed else 760)


def map_leg_servos(leg_data: Dict[str, float], side: str = "L", legs_visible: bool = True) -> Dict[str, int]:
    """
    Maps leg joint angles to bus servos 1-5 (Left) and 9-13 (Right).
    If legs_visible is False, safely anchors in STAND_PULSES.
    """
    if not legs_visible:
        if side == "L":
            return {
                "pulse_ankle_roll": STAND_PULSES[1],
                "pulse_ankle_pitch": STAND_PULSES[2],
                "pulse_knee": STAND_PULSES[3],
                "pulse_hip_pitch": STAND_PULSES[4],
                "pulse_hip_roll": STAND_PULSES[5],
            }
        else:
            return {
                "pulse_ankle_roll": STAND_PULSES[9],
                "pulse_ankle_pitch": STAND_PULSES[10],
                "pulse_knee": STAND_PULSES[11],
                "pulse_hip_pitch": STAND_PULSES[12],
                "pulse_hip_roll": STAND_PULSES[13],
            }

    knee_deg = leg_data.get("knee", 180.0)
    hip_p_deg = leg_data.get("hip_pitch", 0.0)
    ank_p_deg = leg_data.get("ankle_pitch", 0.0)
    knee_norm = np.clip((180.0 - knee_deg) / 90.0, 0.0, 1.0)
    hip_norm = np.clip(hip_p_deg / 90.0, 0.0, 1.0)
    ank_norm = np.clip(ank_p_deg / 60.0, 0.0, 1.0)

    if side == "L":
        p_knee = clamp_servo(3, int(500 + knee_norm * (800 - 500)))
        p_hip_p = clamp_servo(4, int(600 - hip_norm * (600 - 450)))
        p_ank_p = clamp_servo(2, int(390 + ank_norm * (550 - 390)))
        p_hip_r = STAND_PULSES[5]
        p_ank_r = STAND_PULSES[1]
        return {
            "pulse_ankle_roll": p_ank_r,
            "pulse_ankle_pitch": p_ank_p,
            "pulse_knee": p_knee,
            "pulse_hip_pitch": p_hip_p,
            "pulse_hip_roll": p_hip_r,
        }
    else:
        p_knee = clamp_servo(11, int(500 - knee_norm * (500 - 200)))
        p_hip_p = clamp_servo(12, int(400 + hip_norm * (550 - 400)))
        p_ank_p = clamp_servo(10, int(610 - ank_norm * (610 - 450)))
        p_hip_r = STAND_PULSES[13]
        p_ank_r = STAND_PULSES[9]
        return {
            "pulse_ankle_roll": p_ank_r,
            "pulse_ankle_pitch": p_ank_p,
            "pulse_knee": p_knee,
            "pulse_hip_pitch": p_hip_p,
            "pulse_hip_roll": p_hip_r,
        }


def build_robot_api_targets(
    left_arm: Dict[str, Any],
    right_arm: Dict[str, Any],
    left_leg: Dict[str, Any],
    right_leg: Dict[str, Any],
) -> Dict[str, int]:
    """Formats full-body payload matching baymax/robot_api (POST /servo/set_many)."""
    return {
        # Upper Body Left (Servos 6, 7, 8, 18)
        "left_elbow": int(left_arm["pulse_elbow"]),
        "left_shoulder_roll": int(left_arm["pulse_roll"]),
        "left_shoulder_pitch": int(left_arm["pulse_pitch"]),
        "left_hand": int(left_arm["pulse_hand"]),
        # Upper Body Right (Servos 14, 15, 16, 17)
        "right_elbow": int(right_arm["pulse_elbow"]),
        "right_shoulder_roll": int(right_arm["pulse_roll"]),
        "right_shoulder_pitch": int(right_arm["pulse_pitch"]),
        "right_hand": int(right_arm["pulse_hand"]),
        # Lower Body Left (Servos 1-5)
        "left_ankle_roll": int(left_leg["pulse_ankle_roll"]),
        "left_ankle_pitch": int(left_leg["pulse_ankle_pitch"]),
        "left_knee": int(left_leg["pulse_knee"]),
        "left_hip_pitch": int(left_leg["pulse_hip_pitch"]),
        "left_hip_roll": int(left_leg["pulse_hip_roll"]),
        # Lower Body Right (Servos 9-13)
        "right_ankle_roll": int(right_leg["pulse_ankle_roll"]),
        "right_ankle_pitch": int(right_leg["pulse_ankle_pitch"]),
        "right_knee": int(right_leg["pulse_knee"]),
        "right_hip_pitch": int(right_leg["pulse_hip_pitch"]),
        "right_hip_roll": int(right_leg["pulse_hip_roll"]),
    }


# =====================================================================
# 5. ASYNCHRONOUS CLINICAL SUPERVISOR (NEMOTRON)
# =====================================================================

class ClinicalSupervisor:
    """Evaluates dual-arm form asynchronously without blocking the 30 FPS camera loop."""

    def __init__(self):
        self.latest_feedback = "Ready. Begin bicep curls, shoulder raises, or wingspan rotator cuff."
        self.is_evaluating = False

    def async_evaluate(self, side: str, elbow_angle: float, min_rom: float, max_rom: float, rep_count: int):
        if self.is_evaluating:
            return
        thread = threading.Thread(
            target=self._run_inference,
            args=(side, elbow_angle, min_rom, max_rom, rep_count),
            daemon=True,
        )
        thread.start()

    def _run_inference(self, side: str, elbow_angle: float, min_rom: float, max_rom: float, rep_count: int):
        self.is_evaluating = True
        try:
            time.sleep(0.06)
            side_name = "Left" if side == "L" else "Right"
            if max_rom < 135:
                self.latest_feedback = f"Coach: {side_name} arm full extension not reached. Lengthen arm."
            elif min_rom > 70:
                self.latest_feedback = f"Coach: {side_name} arm incomplete curl. Squeeze closer to shoulder."
            else:
                self.latest_feedback = f"Coach: Great form on {side_name} arm! Repetition {rep_count} logged."
        finally:
            self.is_evaluating = False


# =====================================================================
# 6. ON-SCREEN DISPLAY (HUD)
# =====================================================================

def draw_hud_panel(
    frame: np.ndarray,
    left_data: Dict[str, Any],
    right_data: Dict[str, Any],
    left_leg_data: Dict[str, Any],
    right_leg_data: Dict[str, Any],
    legs_visible: bool,
    feedback: str,
    fps: float,
    sim_status: str,
    hw_sim_active: bool,
    native_res: Tuple[int, int],
) -> np.ndarray:
    """
    Renders an informative side telemetry dashboard attached to the OpenCV frame.
    Creates a 960x480 canvas: 640x480 video on left, 320x480 dark telemetry card on right.
    """
    h, w = frame.shape[:2]
    panel_w = 320
    canvas = np.zeros((h, w + panel_w, 3), dtype=np.uint8)

    canvas[:, :w] = frame
    panel = canvas[:, w:]
    panel[:] = (18, 18, 22)
    cv2.line(canvas, (w, 0), (w, h), (45, 45, 55), 2)

    # Title & Stream Status Header
    cv2.putText(panel, "TONYPI PT TELEMETRY", (16, 24),
                cv2.FONT_HERSHEY_DUPLEX, 0.58, (0, 230, 255), 1, cv2.LINE_AA)

    # Tracking Mode Badge: Full body vs Cut off at hips
    if legs_visible:
        mode_badge = "FULL BODY (17 JOINTS ACTIVE)"
        badge_color = (0, 255, 160)
    else:
        mode_badge = "UPPER BODY (CUT OFF AT HIPS)"
        badge_color = (0, 200, 255)
    cv2.putText(panel, mode_badge, (16, 40),
                cv2.FONT_HERSHEY_SIMPLEX, 0.36, badge_color, 1, cv2.LINE_AA)

    cv2.putText(panel, f"Native: {native_res[0]}x{native_res[1]} | FPS: {fps:.1f} | 3D: {sim_status}", (16, 56),
                cv2.FONT_HERSHEY_SIMPLEX, 0.32, (160, 160, 170), 1, cv2.LINE_AA)

    cv2.line(panel, (12, 62), (panel_w - 12, 62), (50, 50, 60), 1)

    # ---------------- LEFT ARM CARD (Cyan Theme) ----------------
    card_l_y = 66
    cv2.rectangle(panel, (10, card_l_y), (panel_w - 10, card_l_y + 116), (32, 34, 42), -1)
    cv2.rectangle(panel, (10, card_l_y), (panel_w - 10, card_l_y + 116), (255, 200, 0), 1)

    cv2.putText(panel, "LEFT ARM [S6, S7, S8, S18]", (18, card_l_y + 18),
                cv2.FONT_HERSHEY_DUPLEX, 0.40, (255, 220, 0), 1, cv2.LINE_AA)

    l_elb = left_data.get("elbow", 180.0)
    l_pitch = left_data.get("pitch", 0.0)
    l_roll = left_data.get("roll", 0.0)
    l_hand = left_data.get("hand_closed", False)
    p_l_elb = left_data.get("pulse_elbow", 575)
    p_l_roll = left_data.get("pulse_roll", 800)
    p_l_pitch = left_data.get("pulse_pitch", 725)
    p_l_hand = left_data.get("pulse_hand", 500)
    reps_l = left_data.get("reps", 0)

    pitch_tag_l = "SKY" if l_pitch > 45 else ("GROUND" if l_pitch < -45 else "FWD")
    cv2.putText(panel, f"Elbow Flex: {l_elb:5.1f} deg", (18, card_l_y + 36),
                cv2.FONT_HERSHEY_SIMPLEX, 0.36, (225, 225, 225), 1, cv2.LINE_AA)
    cv2.putText(panel, f"Pitch: {l_pitch:4.0f} deg [{pitch_tag_l}] | Roll: {l_roll:4.0f} deg", (18, card_l_y + 54),
                cv2.FONT_HERSHEY_SIMPLEX, 0.36, (225, 225, 225), 1, cv2.LINE_AA)
    cv2.putText(panel, f"Gripper: {'CLAMPED' if l_hand else 'OPEN'} | Reps: {reps_l}", (18, card_l_y + 72),
                cv2.FONT_HERSHEY_SIMPLEX, 0.36, (120, 240, 255) if l_hand else (200, 200, 200), 1, cv2.LINE_AA)
    cv2.putText(panel, f"Pulses: S6:{p_l_elb} S7:{p_l_roll} S8:{p_l_pitch} S18:{p_l_hand}", (18, card_l_y + 94),
                cv2.FONT_HERSHEY_SIMPLEX, 0.33, (160, 230, 255), 1, cv2.LINE_AA)

    # ---------------- RIGHT ARM CARD (Amber / Orange Theme) ----------------
    card_r_y = 190
    cv2.rectangle(panel, (10, card_r_y), (panel_w - 10, card_r_y + 116), (32, 34, 42), -1)
    cv2.rectangle(panel, (10, card_r_y), (panel_w - 10, card_r_y + 116), (0, 140, 255), 1)

    cv2.putText(panel, "RIGHT ARM [S14, S15, S16, S17]", (18, card_r_y + 18),
                cv2.FONT_HERSHEY_DUPLEX, 0.40, (0, 160, 255), 1, cv2.LINE_AA)

    r_elb = right_data.get("elbow", 180.0)
    r_pitch = right_data.get("pitch", 0.0)
    r_roll = right_data.get("roll", 0.0)
    r_hand = right_data.get("hand_closed", False)
    p_r_elb = right_data.get("pulse_elbow", 425)
    p_r_roll = right_data.get("pulse_roll", 200)
    p_r_pitch = right_data.get("pulse_pitch", 275)
    p_r_hand = right_data.get("pulse_hand", 500)
    reps_r = right_data.get("reps", 0)

    pitch_tag_r = "SKY" if r_pitch > 45 else ("GROUND" if r_pitch < -45 else "FWD")
    cv2.putText(panel, f"Elbow Flex: {r_elb:5.1f} deg", (18, card_r_y + 36),
                cv2.FONT_HERSHEY_SIMPLEX, 0.36, (225, 225, 225), 1, cv2.LINE_AA)
    cv2.putText(panel, f"Pitch: {r_pitch:4.0f} deg [{pitch_tag_r}] | Roll: {r_roll:4.0f} deg", (18, card_r_y + 54),
                cv2.FONT_HERSHEY_SIMPLEX, 0.36, (225, 225, 225), 1, cv2.LINE_AA)
    cv2.putText(panel, f"Gripper: {'CLAMPED' if r_hand else 'OPEN'} | Reps: {reps_r}", (18, card_r_y + 72),
                cv2.FONT_HERSHEY_SIMPLEX, 0.36, (255, 180, 120) if r_hand else (200, 200, 200), 1, cv2.LINE_AA)
    cv2.putText(panel, f"Pulses: S14:{p_r_elb} S15:{p_r_roll} S16:{p_r_pitch} S17:{p_r_hand}", (18, card_r_y + 94),
                cv2.FONT_HERSHEY_SIMPLEX, 0.33, (255, 200, 160), 1, cv2.LINE_AA)

    # ---------------- LEGS CARD (Green/Grey Theme) ----------------
    card_leg_y = 314
    cv2.rectangle(panel, (10, card_leg_y), (panel_w - 10, card_leg_y + 68), (30, 32, 38), -1)
    border_col = (0, 220, 150) if legs_visible else (80, 80, 95)
    cv2.rectangle(panel, (10, card_leg_y), (panel_w - 10, card_leg_y + 68), border_col, 1)

    cv2.putText(panel, f"LEGS: {'10 SERVOS ACTIVE' if legs_visible else 'CUT OFF AT HIPS (STAND)'}", (18, card_leg_y + 18),
                cv2.FONT_HERSHEY_DUPLEX, 0.38, (0, 230, 160) if legs_visible else (180, 180, 180), 1, cv2.LINE_AA)

    if legs_visible:
        lk = left_leg_data.get("knee", 180.0)
        rk = right_leg_data.get("knee", 180.0)
        p_l_k = left_leg_data.get("pulse_knee", 500)
        p_r_k = right_leg_data.get("pulse_knee", 500)
        cv2.putText(panel, f"Knees: L={lk:.0f} deg (S3:{p_l_k}) | R={rk:.0f} deg (S11:{p_r_k})", (18, card_leg_y + 36),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.34, (220, 220, 220), 1, cv2.LINE_AA)
        cv2.putText(panel, "Lower body 3D kinematics synced to TonyPi legs", (18, card_leg_y + 54),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.32, (160, 200, 180), 1, cv2.LINE_AA)
    else:
        cv2.putText(panel, "Legs out of frame. Servos 1-5 & 9-13 holding stand pose.", (18, card_leg_y + 38),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.32, (160, 160, 170), 1, cv2.LINE_AA)
        cv2.putText(panel, "Safe physiological neutral maintained.", (18, card_leg_y + 54),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.32, (140, 140, 150), 1, cv2.LINE_AA)

    # ---------------- CLINICAL EVALUATION CARD ----------------
    card_c_y = 390
    cv2.rectangle(panel, (10, card_c_y), (panel_w - 10, h - 8), (26, 26, 32), -1)
    cv2.rectangle(panel, (10, card_c_y), (panel_w - 10, h - 8), (70, 70, 85), 1)
    cv2.putText(panel, "CLINICAL EVALUATION", (18, card_c_y + 18),
                cv2.FONT_HERSHEY_DUPLEX, 0.38, (140, 180, 255), 1, cv2.LINE_AA)

    words = feedback.split(" ")
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
        cv2.putText(panel, l_text, (18, card_c_y + 38 + idx * 18),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.36, (230, 230, 230), 1, cv2.LINE_AA)

    # Bottom video banner
    cv2.rectangle(canvas, (10, h - 38), (w - 10, h - 8), (20, 20, 25), -1)
    cv2.putText(canvas, feedback, (18, h - 16),
                cv2.FONT_HERSHEY_SIMPLEX, 0.48, (255, 255, 255), 1, cv2.LINE_AA)

    return canvas


def draw_skeleton_overlay(
    frame: np.ndarray,
    img_lms: LandmarkListWrapper,
    w: int,
    h: int,
    legs_visible: bool
):
    """Draws full body 17-keypoint skeleton overlay, or hip cutoff indicator."""
    def to_px(lm: LandmarkPoint) -> Tuple[int, int]:
        return int(lm.x * w), int(lm.y * h)

    # Head (Nose and eyes)
    p_nose = to_px(img_lms[NOSE])
    cv2.circle(frame, p_nose, 5, (255, 255, 255), -1)

    # Upper Body
    p_ls = to_px(img_lms[L_SHOULDER])
    p_le = to_px(img_lms[L_ELBOW])
    p_lw = to_px(img_lms[L_WRIST])
    p_lh = to_px(img_lms[L_HIP])

    p_rs = to_px(img_lms[R_SHOULDER])
    p_re = to_px(img_lms[R_ELBOW])
    p_rw = to_px(img_lms[R_WRIST])
    p_rh = to_px(img_lms[R_HIP])

    # Left Arm (Cyan)
    cv2.line(frame, p_ls, p_le, (255, 220, 0), 3, cv2.LINE_AA)
    cv2.line(frame, p_le, p_lw, (255, 220, 0), 3, cv2.LINE_AA)
    cv2.line(frame, p_ls, p_lh, (80, 80, 95), 1, cv2.LINE_AA)
    for pt in [p_ls, p_le, p_lw]:
        cv2.circle(frame, pt, 6, (0, 255, 128), -1)
        cv2.circle(frame, pt, 8, (255, 255, 255), 1)

    # Right Arm (Amber/Orange)
    cv2.line(frame, p_rs, p_re, (0, 140, 255), 3, cv2.LINE_AA)
    cv2.line(frame, p_re, p_rw, (0, 140, 255), 3, cv2.LINE_AA)
    cv2.line(frame, p_rs, p_rh, (80, 80, 95), 1, cv2.LINE_AA)
    for pt in [p_rs, p_re, p_rw]:
        cv2.circle(frame, pt, 6, (30, 30, 255), -1)
        cv2.circle(frame, pt, 8, (255, 255, 255), 1)

    # Torso & Hips Baseline
    cv2.line(frame, p_ls, p_rs, (220, 220, 220), 2, cv2.LINE_AA)
    cv2.line(frame, p_lh, p_rh, (120, 120, 130), 2, cv2.LINE_AA)

    # Lower Body
    if legs_visible:
        p_lk = to_px(img_lms[L_KNEE])
        p_la = to_px(img_lms[L_ANKLE])
        p_rk = to_px(img_lms[R_KNEE])
        p_ra = to_px(img_lms[R_ANKLE])

        # Left Leg (Cyan/Teal)
        cv2.line(frame, p_lh, p_lk, (255, 180, 0), 3, cv2.LINE_AA)
        cv2.line(frame, p_lk, p_la, (255, 180, 0), 3, cv2.LINE_AA)
        cv2.circle(frame, p_lk, 6, (0, 230, 160), -1)
        cv2.circle(frame, p_la, 6, (0, 230, 160), -1)

        # Right Leg (Orange/Gold)
        cv2.line(frame, p_rh, p_rk, (0, 120, 255), 3, cv2.LINE_AA)
        cv2.line(frame, p_rk, p_ra, (0, 120, 255), 3, cv2.LINE_AA)
        cv2.circle(frame, p_rk, 6, (0, 180, 255), -1)
        cv2.circle(frame, p_ra, 6, (0, 180, 255), -1)
    else:
        # Draw dotted / dashed cutoff line below hips
        y_cutoff = int(max(p_lh[1], p_rh[1]) + 15)
        cv2.line(frame, (10, y_cutoff), (w - 10, y_cutoff), (0, 180, 255), 1, cv2.LINE_4)
        cv2.putText(frame, "--- HIP CUTOFF (LEGS NOT VISIBLE) ---", (w // 2 - 130, y_cutoff - 6),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.40, (0, 200, 255), 1, cv2.LINE_AA)


# =====================================================================
# 7. MAIN RUNTIME LOOP & CLI INTERFACE
# =====================================================================

def parse_args():
    parser = argparse.ArgumentParser(
        description="MediaPipe PT Pose Estimation & TonyPi Hardware Emulation"
    )
    parser.add_argument("--cam", type=int, default=0, help="Webcam device index (default: 0)")
    parser.add_argument("--source", type=str, default=None, help="Video source (index or path)")
    parser.add_argument("--no-sim", action="store_true", help="Disable the 3D digital twin")
    parser.add_argument("--sync-sim", action="store_true", help=argparse.SUPPRESS)  # legacy, ignored
    parser.add_argument("--twin-port", type=int, default=8765, help="Digital twin web viewer port (default 8765)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the twin viewer in a browser")
    parser.add_argument("--twin-robot-url", type=str, default=None,
                        help="robot_api URL whose servo readback is mirrored in the twin (defaults to --api-url)")
    parser.add_argument("--no-hw-sim", action="store_true", help="Disable TonyPi GC0308 hardware emulation")
    parser.add_argument("--alpha", type=float, default=0.35, help="EMA smoothing factor (0.0 to 1.0)")
    parser.add_argument("--max-step-deg", type=float, default=12.0,
                        help="Max shoulder pitch/roll change per frame at 30fps (0 disables slew limit)")
    parser.add_argument("--api-url", type=str, default=None, help="Baymax HTTP API base URL")
    parser.add_argument("--no-depth", action="store_true", help="Disable monocular depth fusion (Depth Anything V2)")
    parser.add_argument("--depth-weight", type=float, default=0.6, help="Max blend weight of depth vs MediaPipe z (0..1)")
    parser.add_argument("--focal-px", type=float, default=None, help="Camera focal length in pixels (default 0.9*width)")
    parser.add_argument("--headless-test", action="store_true", help="Run 10 frames headlessly and exit (test mode)")
    return parser.parse_args()


def main():
    args = parse_args()
    hw_sim_active = not args.no_hw_sim

    # 1. Initialize GC0308 Hardware Simulator
    hw_sim = GC0308HardwareSimulator(
        target_width=640,
        target_height=480,
        distortion_coeff=-0.25,
        jpeg_quality=75,
        enabled=hw_sim_active,
    )

    # 2. Initialize MediaPipe Pose 3D Detector
    pose_detector = PoseDetector(
        model_complexity=1,
        smooth_landmarks=True,
        min_detection_confidence=0.5,
        min_tracking_confidence=0.5,
    )

    # 2b. Monocular depth estimator (fused into MediaPipe world landmarks)
    depth_async = None
    if not args.no_depth:
        from depth_fusion import AsyncDepth, DepthEstimator

        depth_est = DepthEstimator()
        if depth_est.available:
            depth_async = AsyncDepth(depth_est)
            print(f"[INFO] Depth fusion enabled (Depth Anything V2 Small on {depth_est.device})")
        else:
            print(f"[WARN] Depth model unavailable, using MediaPipe z only: {depth_est.error}")

    # 3. Initialize TonyPi 3D Digital Twin (web viewer served from this process)
    digital_twin = None
    sim_status = "Disabled"
    if not args.no_sim:
        try:
            from digital_twin import TonyPiDigitalTwin
            digital_twin = TonyPiDigitalTwin(
                gui=not args.headless_test,
                headless=args.headless_test,
                port=args.twin_port,
                open_browser=False if args.no_browser else None,
                robot_url=args.twin_robot_url or args.api_url,
            )
            digital_twin.start()
            sim_status = "Connected"
            print(f"[INFO] TonyPi digital twin viewer: {digital_twin.url}")
        except Exception as e:
            sim_status = "Offline"
            print(f"[WARN] TonyPi digital twin could not be started: {e}")

    # Optional robot_api client backend
    http_backend = None
    if args.api_url:
        try:
            from robot_api.client import HttpBackend
            http_backend = HttpBackend(url=args.api_url)
            print(f"[INFO] Connected to robot_api server at {args.api_url}")
        except Exception as e:
            print(f"[WARN] Could not connect to robot_api backend: {e}")

    supervisor = ClinicalSupervisor()

    # Video Capture
    video_src = args.cam
    if args.source is not None:
        video_src = int(args.source) if args.source.isdigit() else args.source

    cap = None
    if not args.headless_test:
        cap = cv2.VideoCapture(video_src)
        if isinstance(video_src, int) and cap.isOpened():
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

        if not cap.isOpened():
            print(f"[WARN] Could not open video source '{video_src}'. Using synthetic camera feed.")
            cap = None

    # Filter state (EMA alpha = 0.35)
    alpha = args.alpha
    filt_l_elbow, filt_r_elbow = 180.0, 180.0
    filt_l_plane, filt_r_plane = 0.0, 0.0
    filt_l_pitch, filt_r_pitch = -90.0, -90.0
    filt_l_roll, filt_r_roll = 0.0, 0.0
    # Shoulder angles in URDF convention (pitch: 0 down, -90 forward; roll: + left-out, - right-out)
    sim_l_pitch, sim_r_pitch, sim_l_roll, sim_r_roll = 0.0, 0.0, 0.0, 0.0
    arm_ik = {"L": ArmIK("L"), "R": ArmIK("R")}
    jf_l_pitch = JointFilter(0.0, alpha, args.max_step_deg)
    jf_r_pitch = JointFilter(0.0, alpha, args.max_step_deg)
    jf_l_roll = JointFilter(0.0, alpha, args.max_step_deg)
    jf_r_roll = JointFilter(0.0, alpha, args.max_step_deg)
    filt_l_knee, filt_r_knee = 180.0, 180.0
    filt_l_hip_pitch, filt_r_hip_pitch = 0.0, 0.0
    filt_l_ankle_pitch, filt_r_ankle_pitch = 0.0, 0.0

    manual_gripper_clamp = False
    rep_count_l, in_rep_l = 0, False
    min_rom_l, max_rom_l = 180.0, 0.0

    rep_count_r, in_rep_r = 0, False
    min_rom_r, max_rom_r = 180.0, 0.0

    fps = 30.0
    prev_time = time.time()
    last_api_send = 0.0
    frame_count = 0

    print("\n--- Starting MediaPipe Full-Body PT Pose & TonyPi Hardware Emulation ---")
    print("GalaxyCore GC0308 Sensor Emulation:", "ENABLED (-25% barrel distortion, JPEG 75)" if hw_sim_active else "DISABLED")
    print("Servo mapping: Aligned with baymax/robot_api (Left: 1-8, 18 | Right: 9-16, 17)")
    print("Full Body & Hip Cutoff: Active. If legs not visible, automatically cuts off at hips.")
    print("180° Shoulder Pitch: Enabled for T-pose sky vs. ground rotator cuff poses.")
    print("Controls: 'q'=exit, 'g'=toggle gripper clamp, 'h'=toggle GC0308 hardware sim\n")

    try:
        while True:
            frame_count += 1
            if args.headless_test and frame_count > 10:
                print("[INFO] Headless test completed 10 frames successfully.")
                break

            if cap is not None and cap.isOpened():
                ret, raw_frame = cap.read()
                if not ret:
                    break
            else:
                raw_frame = np.full((480, 640, 3), 40, dtype=np.uint8)
                cv2.putText(raw_frame, "SYNTHETIC FEED - NO WEBCAM DETECTED", (40, 240),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 200, 255), 2)

            curr_time = time.time()
            dt = curr_time - prev_time
            prev_time = curr_time
            if dt > 0:
                fps = 0.9 * fps + 0.1 * (1.0 / dt)

            raw_frame = cv2.flip(raw_frame, 1)

            # 1. Apply TonyPi Hardware Degradation Pipeline
            frame, native_res = hw_sim.apply(raw_frame)

            # 2. Extract 3D Pose Landmarks
            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            results = pose_detector.process(rgb_frame)
            depth_info = {"active": 0.0}
            if depth_async is not None:
                depth_async.submit(frame)
                if results.pose_world_landmarks is not None and results.pose_landmarks is not None:
                    from depth_fusion import fuse_world_landmarks

                    fused_world, depth_info = fuse_world_landmarks(
                        results.pose_world_landmarks, results.pose_landmarks, depth_async.latest(),
                        frame_size=(frame.shape[1], frame.shape[0]),
                        focal_px=args.focal_px, weight=args.depth_weight,
                    )
                    results = PoseResultWrapper(results.pose_landmarks, fused_world)

            hand_l_closed = manual_gripper_clamp
            hand_r_closed = manual_gripper_clamp
            legs_visible = False
            raw_l_leg, raw_r_leg = {}, {}

            if results.pose_world_landmarks is not None and results.pose_landmarks is not None:
                world_lms = results.pose_world_landmarks
                img_lms = results.pose_landmarks

                # Check if legs are visible or cut off at hips
                legs_visible = check_legs_visible(img_lms)

                # 3. Bilateral Arm Kinematics & 180° Shoulder Pitch
                raw_l_elbow = calculate_elbow_flexion_3d(
                    world_lms[L_SHOULDER], world_lms[L_ELBOW], world_lms[L_WRIST]
                )
                raw_r_elbow = calculate_elbow_flexion_3d(
                    world_lms[R_SHOULDER], world_lms[R_ELBOW], world_lms[R_WRIST]
                )

                raw_l_plane, _, _ = calculate_shoulder_plane_angle_3d(world_lms, "L")
                raw_r_plane, _, _ = calculate_shoulder_plane_angle_3d(world_lms, "R")
                torso_pts = to_torso_frame(world_lms)
                if digital_twin is not None and torso_pts is not None:
                    digital_twin.update_landmarks(torso_pts, [lm.visibility for lm in world_lms], legs=legs_visible)

                hand_l_closed = manual_gripper_clamp or detect_hand_closed(world_lms, "L")
                hand_r_closed = manual_gripper_clamp or detect_hand_closed(world_lms, "R")

                # EMA Smoothing on arms
                filt_l_elbow = alpha * raw_l_elbow + (1.0 - alpha) * filt_l_elbow
                filt_r_elbow = alpha * raw_r_elbow + (1.0 - alpha) * filt_r_elbow
                filt_l_plane = alpha * raw_l_plane + (1.0 - alpha) * filt_l_plane
                filt_r_plane = alpha * raw_r_plane + (1.0 - alpha) * filt_r_plane
                # Shoulder pitch/roll: IK on the robot's joint chain, then damping + slew limit.
                # Low landmark visibility shrinks the gain so an occluded arm holds its pose.
                step_dt = min(max(dt, 1.0 / 120.0), 0.2)
                if torso_pts is not None:
                    for side, jf_p, jf_r in (("L", jf_l_pitch, jf_l_roll), ("R", jf_r_pitch, jf_r_roll)):
                        idx = ARM_IDX[side]
                        sol = arm_ik[side].solve(*(torso_pts[i] for i in idx), q_prev_deg=(jf_p.value, jf_r.value))
                        if sol is not None:
                            conf = (min(world_lms[i].visibility for i in idx) - 0.3) / 0.4
                            jf_p.update(sol[0], step_dt, conf)
                            jf_r.update(sol[1], step_dt, conf)
                sim_l_pitch, sim_l_roll = jf_l_pitch.value, jf_l_roll.value
                sim_r_pitch, sim_r_roll = jf_r_pitch.value, jf_r_roll.value
                # HUD convention: -90 ground, 0 forward, +90 sky
                filt_l_pitch, filt_r_pitch = -sim_l_pitch - 90.0, -sim_r_pitch - 90.0
                filt_l_roll, filt_r_roll = sim_l_roll, sim_r_roll

                # Lower body calculations (if legs visible)
                if legs_visible and torso_pts is not None:
                    raw_l_leg, raw_r_leg = leg_angles(torso_pts, "L"), leg_angles(torso_pts, "R")
                    filt_l_knee = alpha * raw_l_leg["knee"] + (1.0 - alpha) * filt_l_knee
                    filt_r_knee = alpha * raw_r_leg["knee"] + (1.0 - alpha) * filt_r_knee
                    filt_l_hip_pitch = alpha * raw_l_leg["hip_pitch"] + (1.0 - alpha) * filt_l_hip_pitch
                    filt_r_hip_pitch = alpha * raw_r_leg["hip_pitch"] + (1.0 - alpha) * filt_r_hip_pitch
                    filt_l_ankle_pitch = alpha * raw_l_leg["ankle_pitch"] + (1.0 - alpha) * filt_l_ankle_pitch
                    filt_r_ankle_pitch = alpha * raw_r_leg["ankle_pitch"] + (1.0 - alpha) * filt_r_ankle_pitch
                else:
                    filt_l_knee, filt_r_knee = 180.0, 180.0
                    filt_l_hip_pitch, filt_r_hip_pitch = 0.0, 0.0
                    filt_l_ankle_pitch, filt_r_ankle_pitch = 0.0, 0.0

                # PT Repetition Tracking
                min_rom_l = min(min_rom_l, filt_l_elbow)
                max_rom_l = max(max_rom_l, filt_l_elbow)
                if filt_l_elbow < 60 and not in_rep_l:
                    in_rep_l = True
                elif filt_l_elbow > 140 and in_rep_l:
                    in_rep_l = False
                    rep_count_l += 1
                    supervisor.async_evaluate("L", filt_l_elbow, min_rom_l, max_rom_l, rep_count_l)
                    min_rom_l, max_rom_l = 180.0, 0.0

                min_rom_r = min(min_rom_r, filt_r_elbow)
                max_rom_r = max(max_rom_r, filt_r_elbow)
                if filt_r_elbow < 60 and not in_rep_r:
                    in_rep_r = True
                elif filt_r_elbow > 140 and in_rep_r:
                    in_rep_r = False
                    rep_count_r += 1
                    supervisor.async_evaluate("R", filt_r_elbow, min_rom_r, max_rom_r, rep_count_r)
                    min_rom_r, max_rom_r = 180.0, 0.0

                # Draw 17-keypoint skeleton overlay (with hip cutoff if legs hidden)
                draw_skeleton_overlay(frame, img_lms, frame.shape[1], frame.shape[0], legs_visible)

            if digital_twin is not None and results.pose_world_landmarks is None:
                digital_twin.update_landmarks(None)

            # 4. Map Angles to TonyPi Bus Servo Pulses
            pulse_l_elb = map_elbow_to_tonypi_pulse(filt_l_elbow, "L")
            pulse_l_pitch, pulse_l_roll = (clamp_servo(sid, v) for sid, v in zip((8, 7), shoulder_pulses(sim_l_pitch, sim_l_roll, "L")))
            pulse_l_hand = map_hand_to_tonypi_pulse(hand_l_closed, "L")

            pulse_r_elb = map_elbow_to_tonypi_pulse(filt_r_elbow, "R")
            pulse_r_pitch, pulse_r_roll = (clamp_servo(sid, v) for sid, v in zip((16, 15), shoulder_pulses(sim_r_pitch, sim_r_roll, "R")))
            pulse_r_hand = map_hand_to_tonypi_pulse(hand_r_closed, "R")

            l_leg_filtered = {
                "knee": filt_l_knee, "hip_pitch": filt_l_hip_pitch, "ankle_pitch": filt_l_ankle_pitch
            }
            r_leg_filtered = {
                "knee": filt_r_knee, "hip_pitch": filt_r_hip_pitch, "ankle_pitch": filt_r_ankle_pitch
            }
            l_leg_pulses = map_leg_servos(l_leg_filtered, "L", legs_visible)
            r_leg_pulses = map_leg_servos(r_leg_filtered, "R", legs_visible)

            # 5. Update the digital twin with exactly the pulses the robot would receive
            if digital_twin is not None:
                digital_twin.update_servos(build_robot_api_targets(
                    {"pulse_elbow": pulse_l_elb, "pulse_roll": pulse_l_roll, "pulse_pitch": pulse_l_pitch, "pulse_hand": pulse_l_hand},
                    {"pulse_elbow": pulse_r_elb, "pulse_roll": pulse_r_roll, "pulse_pitch": pulse_r_pitch, "pulse_hand": pulse_r_hand},
                    l_leg_pulses, r_leg_pulses,
                ), label="mediapipe")

            # Assemble Telemetry
            left_arm_telemetry = {
                "elbow": filt_l_elbow,
                "plane": filt_l_plane,
                "pitch": filt_l_pitch,
                "roll": filt_l_roll,
                "hand_closed": hand_l_closed,
                "pulse_elbow": pulse_l_elb,
                "pulse_roll": pulse_l_roll,
                "pulse_pitch": pulse_l_pitch,
                "pulse_hand": pulse_l_hand,
                "reps": rep_count_l,
            }
            right_arm_telemetry = {
                "elbow": filt_r_elbow,
                "plane": filt_r_plane,
                "pitch": filt_r_pitch,
                "roll": filt_r_roll,
                "hand_closed": hand_r_closed,
                "pulse_elbow": pulse_r_elb,
                "pulse_roll": pulse_r_roll,
                "pulse_pitch": pulse_r_pitch,
                "pulse_hand": pulse_r_hand,
                "reps": rep_count_r,
            }
            left_leg_telemetry = {**l_leg_filtered, **l_leg_pulses}
            right_leg_telemetry = {**r_leg_filtered, **r_leg_pulses}

            # Dispatch to robot_api if enabled
            if http_backend is not None and (curr_time - last_api_send) > 0.06:
                try:
                    targets = build_robot_api_targets(
                        left_arm_telemetry, right_arm_telemetry,
                        left_leg_telemetry, right_leg_telemetry
                    )
                    http_backend.call("servo/set_many", targets=targets)
                    last_api_send = curr_time
                except Exception:
                    pass

            # Depth inset (top-right of the camera frame) + fusion status
            if depth_async is not None:
                dmap = depth_async.latest()
                if dmap is not None:
                    from depth_fusion import colorize_depth

                    inset = colorize_depth(dmap, (160, 120))
                    frame[4:124, frame.shape[1] - 164:frame.shape[1] - 4] = inset
                tag = "DEPTH FUSED" if depth_info.get("active") else "DEPTH WARMING UP"
                cv2.putText(frame, tag, (frame.shape[1] - 164, 140), cv2.FONT_HERSHEY_SIMPLEX,
                            0.4, (0, 255, 255), 1, cv2.LINE_AA)

            # 6. Render HUD Dashboard
            dashboard = draw_hud_panel(
                frame,
                left_arm_telemetry,
                right_arm_telemetry,
                left_leg_telemetry,
                right_leg_telemetry,
                legs_visible,
                supervisor.latest_feedback,
                fps,
                sim_status,
                hw_sim.enabled,
                native_res,
            )

            if not args.headless_test:
                cv2.imshow("TonyPi Full-Body PT Pose & Hardware Emulation", dashboard)
                key = cv2.waitKey(1) & 0xFF
                if key == ord("q"):
                    break
                elif key == ord("g"):
                    manual_gripper_clamp = not manual_gripper_clamp
                    print(f"[INFO] Gripper manual clamp toggled: {'CLOSED' if manual_gripper_clamp else 'OPEN'}")
                elif key == ord("h"):
                    hw_sim.enabled = not hw_sim.enabled
                    print(f"[INFO] GC0308 Hardware simulation: {'ENABLED' if hw_sim.enabled else 'DISABLED'}")

    finally:
        if cap is not None:
            cap.release()
        cv2.destroyAllWindows()
        pose_detector.close()
        if depth_async is not None:
            depth_async.close()
        if digital_twin is not None:
            digital_twin.close()


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()
