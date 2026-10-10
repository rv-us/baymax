#!/usr/bin/python3
# coding=utf8
"""Head tracking via MediaPipe TFLite — face or human pose.

Examples:
  python face_track.py --mode short   # faces within ~2m (faster)
  python face_track.py --mode full    # faces within ~5m
  python face_track.py --mode human   # head track + arm pose mirror (docs 5.12)
"""
import argparse
import copy
import logging
import math
import os
import time

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision as mp_vision

import hiwonder.PID as PID
import hiwonder.yaml_handle as yaml_handle
import hiwonder.ros_robot_controller_sdk as rrc
from hiwonder.Controller import Controller

from mp_helper import CameraWrapper, FPSMeter

LOG = logging.getLogger("face_track")

POSE_MODEL_DIR = "/home/pi/TonyPi/Functions/model"
POSE_MODELS = {
    "lite": os.path.join(POSE_MODEL_DIR, "pose_landmarker_lite.task"),
    "full": os.path.join(POSE_MODEL_DIR, "pose_landmarker_full.task"),
    "heavy": os.path.join(POSE_MODEL_DIR, "pose_landmarker_heavy.task"),
}


def setup_logging(log_file=None, verbose=False):
    level = logging.DEBUG if verbose else logging.INFO
    handlers = [logging.StreamHandler()]
    if log_file:
        handlers.append(logging.FileHandler(log_file, mode="w"))
    logging.basicConfig(
        level=level,
        format="%(asctime)s.%(msecs)03d %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )


class ServoHeadTracker:
    def __init__(
        self,
        p=0.10,
        dead_x=45,
        dead_y=55,
        update_interval=0.08,
        ema=0.35,
        max_delta_yaw=12,
        max_delta_pitch=15,
    ):
        servo_data = yaml_handle.get_yaml_data(yaml_handle.servo_file_path)
        self.pitch_center = 1500
        self.yaw_center = servo_data.get("servo2", 1500)
        self.pitch = self.pitch_center
        self.yaw = self.yaw_center
        self.pitch_limits = (500, 2500)
        self.yaw_limits = (500, 2500)
        self.dead_x = dead_x
        self.dead_y = dead_y
        self.max_delta_yaw = max_delta_yaw
        self.max_delta_pitch = max_delta_pitch

        self.board = rrc.Board()
        self.controller = Controller(self.board)
        self.controller.set_pwm_servo_pulse(1, 1500, 500)
        self.controller.set_pwm_servo_pulse(2, self.yaw, 500)

        time.sleep(1)

        self.pid_x = PID.PID(P=p, I=0.0, D=0.0005)
        self.pid_y = PID.PID(P=p, I=0.0, D=0.0005)
        self.p = p

        self.update_interval = update_interval
        self.last_update = time.time()
        self._smooth_x = None
        self._smooth_y = None
        self._ema = ema

        self.stats = {
            "updates": 0,
            "holds": 0,
            "moves": 0,
            "sum_abs_dyaw": 0,
            "sum_abs_dpitch": 0,
            "max_abs_err_x": 0.0,
            "max_abs_err_y": 0.0,
            "recenters": 0,
        }
        LOG.info(
            "head_init yaw0=%s pitch0=%s P=%.3f dead=(%d,%d) ema=%.2f interval=%.2f "
            "max_d=(%d,%d)",
            self.yaw,
            self.pitch,
            p,
            dead_x,
            dead_y,
            ema,
            update_interval,
            max_delta_yaw,
            max_delta_pitch,
        )

    @staticmethod
    def clamp(value, limits):
        return max(limits[0], min(limits[1], value))

    def move_to_center(self, duration=400):
        self.pitch = self.clamp(self.pitch_center, self.pitch_limits)
        self.yaw = self.clamp(self.yaw_center, self.yaw_limits)
        self.pid_x.clear()
        self.pid_y.clear()
        self._smooth_x = None
        self._smooth_y = None
        self.controller.set_pwm_servo_pulse(1, 1500, duration)
        self.controller.set_pwm_servo_pulse(2, self.yaw, duration)
        self.stats["recenters"] += 1
        LOG.info("head_recenter yaw=%s pitch=%s", self.yaw, self.pitch)

    def update(self, target_x, target_y, width, height):
        now = time.time()
        if now - self.last_update < self.update_interval:
            return None
        self.last_update = now
        self.stats["updates"] += 1

        raw_x, raw_y = target_x, target_y
        if self._smooth_x is None:
            self._smooth_x, self._smooth_y = target_x, target_y
        else:
            a = self._ema
            self._smooth_x = a * target_x + (1 - a) * self._smooth_x
            self._smooth_y = a * target_y + (1 - a) * self._smooth_y
        target_x, target_y = self._smooth_x, self._smooth_y

        center_x = width / 2.0
        center_y = height / 2.0
        error_x = target_x - center_x
        error_y = target_y - center_y
        self.stats["max_abs_err_x"] = max(self.stats["max_abs_err_x"], abs(error_x))
        self.stats["max_abs_err_y"] = max(self.stats["max_abs_err_y"], abs(error_y))

        self.pid_x.SetPoint = center_x
        self.pid_y.SetPoint = center_y
        self.pid_x.update(target_x)
        self.pid_y.update(target_y)

        delta_yaw = int(self.pid_x.output)
        delta_pitch = -int(self.pid_y.output)

        if abs(error_x) < self.dead_x:
            delta_yaw = 0
        if abs(error_y) < self.dead_y:
            delta_pitch = 0

        delta_yaw = max(-self.max_delta_yaw, min(self.max_delta_yaw, delta_yaw))
        delta_pitch = max(-self.max_delta_pitch, min(self.max_delta_pitch, delta_pitch))

        applied_pitch_delta = -delta_pitch

        if delta_yaw == 0 and applied_pitch_delta == 0:
            self.stats["holds"] += 1
            LOG.debug(
                "head_hold raw=(%.1f,%.1f) sm=(%.1f,%.1f) err=(%.1f,%.1f) yaw=%s pitch=%s",
                raw_x,
                raw_y,
                target_x,
                target_y,
                error_x,
                error_y,
                self.yaw,
                self.pitch,
            )
            return {"moved": False, "error_x": error_x, "error_y": error_y}

        move_time = float(max(abs(delta_yaw), abs(applied_pitch_delta)) * 0.00035)
        move_time = max(0.04, min(move_time, 0.25))
        duration = int(move_time * 1000)

        self.yaw = self.clamp(self.yaw + delta_yaw, self.yaw_limits)
        self.pitch = self.clamp(self.pitch + applied_pitch_delta, self.pitch_limits)
        self.controller.set_pwm_servo_pulse(2, self.yaw, duration)
        self.controller.set_pwm_servo_pulse(1, self.pitch, duration)
        self.stats["moves"] += 1
        self.stats["sum_abs_dyaw"] += abs(delta_yaw)
        self.stats["sum_abs_dpitch"] += abs(applied_pitch_delta)
        LOG.info(
            "head_move err=(%+.1f,%+.1f) d=(%+d,%+d) -> yaw=%d pitch=%d dur=%dms",
            error_x,
            error_y,
            delta_yaw,
            applied_pitch_delta,
            self.yaw,
            self.pitch,
            duration,
        )
        return {
            "moved": True,
            "error_x": error_x,
            "error_y": error_y,
            "delta_yaw": delta_yaw,
            "delta_pitch": applied_pitch_delta,
            "yaw": self.yaw,
            "pitch": self.pitch,
        }


class FaceDetector:
    """MediaPipe BlazeFace (TFLite): short (~2m) or full (~5m) range."""

    MODE_META = {
        "short": {"model_selection": 0, "min_score": 0.7, "label": "face-short (~2m)"},
        "full": {"model_selection": 1, "min_score": 0.55, "label": "face-full (~5m)"},
    }

    def __init__(self, mode="full", min_detection_confidence=0.5):
        if mode not in self.MODE_META:
            raise ValueError("face mode must be 'short' or 'full'")
        meta = self.MODE_META[mode]
        self.label = meta["label"]
        self.min_score = meta["min_score"]
        self._detector = mp.solutions.face_detection.FaceDetection(
            min_detection_confidence=min_detection_confidence,
            model_selection=meta["model_selection"],
        )

    def detect(self, frame_bgr):
        rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        results = self._detector.process(rgb)
        if not results.detections:
            return None

        best, score = None, -1.0
        for detection in results.detections:
            s = detection.score[0] if detection.score else 0.0
            if s > score:
                best, score = detection, s
        if best is None or score < self.min_score:
            return None

        h, w = frame_bgr.shape[:2]
        bbox = best.location_data.relative_bounding_box
        x = max(int(bbox.xmin * w), 0)
        y = max(int(bbox.ymin * h), 0)
        bw = max(int(bbox.width * w), 1)
        bh = max(int(bbox.height * h), 1)
        x = min(x, w - 1)
        y = min(y, h - 1)
        bw = min(bw, w - x)
        bh = min(bh, h - y)
        return {
            "x": x,
            "y": y,
            "w": bw,
            "h": bh,
            "cx": x + bw / 2.0,
            "cy": y + bh / 2.0,
            "score": float(score),
        }

    def close(self):
        self._detector.close()


def _val_map(x, in_min, in_max, out_min, out_max):
    return (x - in_min) * (out_max - out_min) / (in_max - in_min) + out_min


def _vector_2d_angle(v1, v2):
    # Exact copy of pose_control.vector_2d_angle
    d_v1_v2 = np.linalg.norm(v1) * np.linalg.norm(v2)
    if d_v1_v2 == 0:
        return None
    cos = np.dot(v1, v2) / d_v1_v2
    cos = np.clip(cos, -1.0, 1.0)
    sin = np.cross(v1, v2) / d_v1_v2
    sin = np.clip(sin, -1.0, 1.0)
    return int(np.degrees(np.arctan2(sin, cos)))


def _xyz(lm):
    return np.array([lm.x, lm.y, lm.z], dtype=np.float64)


def _angle_3d(u, v):
    """Unsigned degrees between two 3D vectors."""
    nu = np.linalg.norm(u)
    nv = np.linalg.norm(v)
    if nu < 1e-8 or nv < 1e-8:
        return None
    c = float(np.clip(np.dot(u, v) / (nu * nv), -1.0, 1.0))
    return float(np.degrees(np.arccos(c)))


def _norm(v):
    n = np.linalg.norm(v)
    if n < 1e-8:
        return None
    return v / n


def _torso_frame(world):
    """Orthonormal torso basis from MediaPipe world landmarks (Y≈up)."""
    l_sh, r_sh = _xyz(world[11]), _xyz(world[12])
    l_hip, r_hip = _xyz(world[23]), _xyz(world[24])
    mid_sh = 0.5 * (l_sh + r_sh)
    mid_hip = 0.5 * (l_hip + r_hip)
    up = _norm(mid_sh - mid_hip)
    # person-right axis (toward subject's right shoulder)
    right = _norm(r_sh - l_sh)
    if up is None or right is None:
        return None
    # re-orthogonalize
    forward = _norm(np.cross(up, right))
    if forward is None:
        return None
    right = _norm(np.cross(forward, up))
    if right is None:
        return None
    return up, right, forward


def _arm_rotations_3d(world, sh_i, el_i, wr_i, side):
    """Return (elev, abd, elbow) in ≈[-90, 90] for one arm.

    elev: hang≈-90 → forward/up≈+90 (vs torso down)
    abd:  across-body≈-90 → out-to-side≈+90
    elbow: straight≈0 → bent≈+90
    side: 'L' or 'R' (anatomical) for abduction sign
    """
    frame = _torso_frame(world)
    if frame is None:
        return None
    up, right, _forward = frame
    sh, el, wr = (_xyz(world[i]) for i in (sh_i, el_i, wr_i))
    upper = _norm(el - sh)
    if upper is None:
        return None

    torso_down = -up
    elev = _angle_3d(upper, torso_down)
    elbow_int = _angle_3d(sh - el, wr - el)
    if elev is None or elbow_int is None:
        return None

    # Abduction: component toward outward (+person-right for R, -person-right for L)
    outward = right if side == "R" else -right
    # project upper onto coronal plane (up × outward)
    abd = float(
        np.degrees(
            np.arctan2(np.dot(upper, outward), -np.dot(upper, up))
        )
    )
    abd = int(np.clip(abd, -90, 90))
    elev_cmd = int(np.clip(elev - 90.0, -90, 90))
    elbow_cmd = int(np.clip(180.0 - elbow_int, -90, 90))
    return elev_cmd, abd, elbow_cmd


# TonyPi Pro arm bus servos — from factory ActionGroups (not invented limits).
# Left:  6=elbow, 7=shoulder yaw, 8=raise (725 hang → 500 T → 330 up)
# Right: 14=elbow, 15=shoulder yaw, 16=raise (275 hang → 500 T → 787 up)
# stand / left_hand+right_hand / go_hand_up+wave
ARM_STAND = {6: 575, 7: 800, 8: 725, 14: 425, 15: 200, 16: 275}
ARM_TPOSE = {6: 575, 7: 800, 8: 500, 14: 425, 15: 200, 16: 500}
ARM_HANDS_UP = {6: 860, 7: 860, 8: 330, 14: 177, 15: 186, 16: 787}
ARM_LIMITS = {
    6: (150, 930),
    7: (150, 900),
    8: (150, 950),
    14: (150, 875),
    15: (150, 850),
    16: (150, 950),
}


def _clamp_pul(servo_id, pul):
    lo, hi = ARM_LIMITS[servo_id]
    return max(lo, min(hi, int(pul)))


def _lerp(a, b, t):
    return a + (b - a) * t


def _blend_pose(servo_id, abd_t, raise_t):
    """stand → T-pose via abd, then → hands-up via raise."""
    mid = _lerp(ARM_STAND[servo_id], ARM_TPOSE[servo_id], abd_t)
    return _clamp_pul(servo_id, _lerp(mid, ARM_HANDS_UP[servo_id], raise_t))


def _wrist_raise_t(image_lms, sh_i, wr_i):
    """0..1 from image wrist height above shoulder (hands-up cue)."""
    dy = float(image_lms[sh_i].y - image_lms[wr_i].y)
    # Cal frames: hands-up dy≈+0.30..0.46; hang dy≈-0.50; T-pose dy≈0
    return float(np.clip(dy / 0.28, 0.0, 1.0))


def _angles6_to_pulses(
    l_elev, l_abd, l_elb, r_elev, r_abd, r_elb, l_wrist_t=0.0, r_wrist_t=0.0
):
    """Map 6DOF → pulses: abd spreads to T-pose, raise lifts to hands-up.

    Factory ceiling is go_hand_up/wave (≈chest–head height), not overhead.
    """
    def elev_raise_t(elev):
        # Only count above horizontal so T-pose (elev≈0) stays on abd axis
        return float(np.clip(elev / 90.0, 0.0, 1.0))

    def elb_t(elb):
        return float(np.clip(elb / 90.0, 0.0, 1.0))

    def abd_t(abd):
        return float(np.clip(abd / 90.0, 0.0, 1.0))

    lt = max(elev_raise_t(l_elev), float(np.clip(l_wrist_t, 0.0, 1.0)))
    rt = max(elev_raise_t(r_elev), float(np.clip(r_wrist_t, 0.0, 1.0)))
    la, ra = abd_t(l_abd), abd_t(r_abd)
    l_elb_mix = float(np.clip(0.55 * elb_t(l_elb) + 0.45 * lt, 0.0, 1.0))
    r_elb_mix = float(np.clip(0.55 * elb_t(r_elb) + 0.45 * rt, 0.0, 1.0))

    return {
        6: _blend_pose(6, la, l_elb_mix),
        7: _blend_pose(7, la, lt),
        8: _blend_pose(8, la, lt),
        14: _blend_pose(14, ra, r_elb_mix),
        15: _blend_pose(15, ra, rt),
        16: _blend_pose(16, ra, rt),
    }


class HumanDetector:
    """Face locks the camera; arms mirror from pose (2D or 3D→6 servo DOF).

    Head: MediaPipe BlazeFace on the unflipped camera frame.
    Arms:
      - 2d: pose_control image-plane angles → servos 6/7 + 14/15
      - 3d: world-landmark elev/abd/elbow → servos 6/7/8 + 14/15/16 (6 DOF)
    """

    # MediaPipe indices
    L_SH, R_SH = 11, 12
    L_EL, R_EL = 13, 14
    L_WR, R_WR = 15, 16
    L_HIP, R_HIP = 23, 24

    L1 = 0.06
    L2 = 0.11
    # Low-lag follow: hands-up cal frames already map to 8≈330/16≈750;
    # delay was EMA + deadband + long servo move stacking.
    FILTER_ALPHA = 0.70
    MIN_DELTA = 6
    CONTROL_INTERVAL = 0.03
    ANGLE_MEDIAN_N = 1
    SERVO_MOVE_S = 0.04

    def __init__(
        self,
        board,
        model="lite",
        mirror_arms=True,
        face_mode="full",
        arm_space="3d",
    ):
        if model not in POSE_MODELS:
            raise ValueError("pose model must be one of: {}".format(list(POSE_MODELS)))
        if arm_space not in ("2d", "3d"):
            raise ValueError("arm_space must be '2d' or '3d'")
        model_path = POSE_MODELS[model]
        if not os.path.isfile(model_path):
            raise FileNotFoundError("Pose model not found: {}".format(model_path))

        self.board = board
        self.mirror_arms = mirror_arms
        self.arm_space = arm_space
        self.face = FaceDetector(mode=face_mode)
        self.min_score = self.face.min_score
        dof = "6dof" if arm_space == "3d" else "4dof"
        self.label = "face-{} + arms-{}({})".format(face_mode, arm_space, dof)

        options = mp_vision.PoseLandmarkerOptions(
            base_options=mp_python.BaseOptions(model_asset_path=model_path),
            output_segmentation_masks=False,
        )
        self._pose = mp_vision.PoseLandmarker.create_from_options(options)

        self.last_angle = []
        self.filtered_angles = None
        self.control_buffer = []
        self.last_control_time = 0.0
        self._angle_hist = []  # recent raw angle tuples for median filter
        self.stats = {
            "face_hits": 0,
            "face_misses": 0,
            "pose_hits": 0,
            "arm_cmds": 0,
            "arm_skips_delta": 0,
            "arm_skips_rate": 0,
        }
        LOG.info(
            "human_init face=%s pose=%s arms=%s space=%s",
            face_mode,
            model,
            mirror_arms,
            arm_space,
        )

    def _draw_arm_segments(self, frame_bgr, image_lms, width, height, mirrored_space):
        """Draw shoulder→elbow→wrist. If mirrored_space, x was computed on a flipped frame."""
        def pt(i):
            x = int(image_lms[i].x * width)
            y = int(image_lms[i].y * height)
            if mirrored_space:
                x = width - 1 - x
            return (x, y)

        pairs = (
            (self.L_SH, self.L_EL),
            (self.L_EL, self.L_WR),
            (self.R_SH, self.R_EL),
            (self.R_EL, self.R_WR),
        )
        for a, b in pairs:
            cv2.line(frame_bgr, pt(a), pt(b), (255, 255, 0), 4)
        for i in (self.L_SH, self.L_EL, self.L_WR, self.R_SH, self.R_EL, self.R_WR):
            cv2.circle(frame_bgr, pt(i), 8, (255, 255, 0), -1)

    def _angles_2d(self, image_lms, width, height):
        """Stock pose_control 2D angles (landmarks already in flipped image space)."""
        mark = []
        for landmark in image_lms[11:17]:
            mark.append([int(landmark.x * width), int(landmark.y * height)])

        left_p1, left_p2, left_p3 = mark[0], mark[2], mark[4]
        right_p1, right_p2, right_p3 = mark[1], mark[3], mark[5]
        left_p0 = copy.deepcopy(mark[0])
        left_p0[0] = width
        right_p0 = copy.deepcopy(mark[1])
        right_p0[0] = 0

        angle1 = _vector_2d_angle(
            np.array(left_p1) - np.array(left_p0),
            np.array(left_p1) - np.array(left_p2),
        )
        angle2 = _vector_2d_angle(
            np.array(left_p2) - np.array(left_p1),
            np.array(left_p3) - np.array(left_p2),
        )
        angle3 = _vector_2d_angle(
            np.array(right_p1) - np.array(right_p0),
            np.array(right_p1) - np.array(right_p2),
        )
        angle4 = _vector_2d_angle(
            np.array(right_p2) - np.array(right_p1),
            np.array(right_p3) - np.array(right_p2),
        )
        if None in (angle1, angle2, angle3, angle4):
            return None
        return angle1, angle2, angle3, angle4

    def _angles_3d(self, world_lms):
        """6 DOF: (L_elev, L_abd, L_elb, R_elev, R_abd, R_elb), mirrored.

        Robot left ← user right; robot right ← user left.
        """
        user_r = _arm_rotations_3d(
            world_lms, self.R_SH, self.R_EL, self.R_WR, "R"
        )
        user_l = _arm_rotations_3d(
            world_lms, self.L_SH, self.L_EL, self.L_WR, "L"
        )
        if user_r is None or user_l is None:
            return None
        # robot L gets user R, robot R gets user L
        return user_r + user_l  # 6-tuple

    def _apply_arm_2d(self, angle1, angle2, angle3, angle4):
        """Legacy 4-servo path (pose_control): 6/7 + 14/15."""
        self._angle_hist.append((angle1, angle2, angle3, angle4, 0, 0))
        if len(self._angle_hist) > self.ANGLE_MEDIAN_N:
            self._angle_hist.pop(0)
        med = np.median(np.array(self._angle_hist, dtype=np.float32)[:, :4], axis=0)
        angle1, angle2, angle3, angle4 = [int(v) for v in med]

        x1 = self.L1 * math.cos(math.radians(angle1)) + self.L2 * math.cos(
            math.radians(angle2) + math.radians(angle1)
        )
        x2 = self.L1 * math.cos(math.radians(angle3)) + self.L2 * math.cos(
            math.radians(angle4) + math.radians(angle3)
        )
        targets = np.array(
            [
                max(150, min(850, int(_val_map(angle2, -90, 90, 125, 875)))),
                max(150, min(850, int(_val_map(angle1, -90, 90, 125, 875)))),
                max(150, min(850, int(_val_map(angle4, -90, 90, 125, 875)))),
                max(150, min(850, int(_val_map(angle3, -90, 90, 125, 875)))),
            ],
            dtype=np.float32,
        )
        pulses = self._filter_and_gate(targets)
        if pulses is None:
            return
        s6, s7, s14, s15 = pulses
        cmds = []
        if x1 > 0:
            cmds.extend([[7, s7], [6, s6]])
        if x2 > 0:
            cmds.extend([[15, s15], [14, s14]])
        if not cmds:
            return
        self.board.bus_servo_set_position(self.SERVO_MOVE_S, cmds)
        self._log_arm_cmd(
            (angle1, angle2, 0, angle3, angle4, 0),
            {6: s6, 7: s7, 14: s14, 15: s15},
        )

    def _apply_arm_6dof(self, angles6, image_lms=None):
        """Map elev/abd/elbow ×2 → servos 6,7,8,14,15,16 (action-calibrated)."""
        self._angle_hist.append(tuple(angles6))
        if len(self._angle_hist) > self.ANGLE_MEDIAN_N:
            self._angle_hist.pop(0)
        med = np.median(np.array(self._angle_hist, dtype=np.float32), axis=0)
        l_elev, l_abd, l_elb, r_elev, r_abd, r_elb = [int(v) for v in med]

        # Robot L ← user R, robot R ← user L (same swap as _angles_3d)
        l_wrist_t = r_wrist_t = 0.0
        if image_lms is not None:
            l_wrist_t = _wrist_raise_t(image_lms, self.R_SH, self.R_WR)
            r_wrist_t = _wrist_raise_t(image_lms, self.L_SH, self.L_WR)

        pul = _angles6_to_pulses(
            l_elev,
            l_abd,
            l_elb,
            r_elev,
            r_abd,
            r_elb,
            l_wrist_t=l_wrist_t,
            r_wrist_t=r_wrist_t,
        )
        targets = np.array(
            [pul[6], pul[7], pul[8], pul[14], pul[15], pul[16]], dtype=np.float32
        )
        pulses = self._filter_and_gate(targets)
        if pulses is None:
            return
        s6, s7, s8, s14, s15, s16 = pulses
        self.board.bus_servo_set_position(
            self.SERVO_MOVE_S,
            [
                [6, s6],
                [7, s7],
                [8, s8],
                [14, s14],
                [15, s15],
                [16, s16],
            ],
        )
        self._log_arm_cmd(
            (l_elev, l_abd, l_elb, r_elev, r_abd, r_elb),
            {6: s6, 7: s7, 8: s8, 14: s14, 15: s15, 16: s16},
        )

    def _filter_and_gate(self, targets):
        """EMA + rate/delta gates. Returns int pulse list or None if skipped."""
        if self.filtered_angles is None or len(self.filtered_angles) != len(targets):
            self.filtered_angles = targets.copy()
            self.last_angle = []
        else:
            self.filtered_angles = (
                self.FILTER_ALPHA * targets
                + (1 - self.FILTER_ALPHA) * self.filtered_angles
            )

        now = time.time()
        if now - self.last_control_time < self.CONTROL_INTERVAL:
            self.stats["arm_skips_rate"] += 1
            return None

        # Latest EMA sample only (no multi-frame mean — that added lag)
        filtered = np.rint(self.filtered_angles).astype(int).tolist()
        self.control_buffer.clear()

        if self.last_angle and len(self.last_angle) == len(filtered):
            max_delta = max(abs(c - p) for c, p in zip(filtered, self.last_angle))
            if max_delta < self.MIN_DELTA:
                self.stats["arm_skips_delta"] += 1
                LOG.debug(
                    "arm_skip_delta maxΔ=%d < %d pul=%s",
                    max_delta,
                    self.MIN_DELTA,
                    filtered,
                )
                return None
            # Send full target vector when any joint moved — keeps arms coordinated
            filtered = list(filtered)

        self.last_control_time = now
        return filtered

    def _log_arm_cmd(self, angles6, pulses):
        prev = self.last_angle or [0] * len(pulses)
        ordered = [pulses[k] for k in sorted(pulses.keys())]
        self.last_angle = ordered
        self.stats["arm_cmds"] += 1
        l_elev, l_abd, l_elb, r_elev, r_abd, r_elb = angles6
        pul_s = " ".join("{}:{}".format(k, pulses[k]) for k in sorted(pulses))
        deltas = ",".join(
            str(ordered[i] - (prev[i] if i < len(prev) else 0))
            for i in range(len(ordered))
        )
        LOG.info(
            "arm_cmd space=%s L(elev,abd,elb)=(%d,%d,%d) R=(%d,%d,%d) pul=(%s) Δ=(%s)",
            self.arm_space,
            l_elev,
            l_abd,
            l_elb,
            r_elev,
            r_abd,
            r_elb,
            pul_s,
            deltas,
        )

    def detect(self, frame_bgr):
        face_hit = self.face.detect(frame_bgr)
        if face_hit is None:
            self.stats["face_misses"] += 1
            return None
        self.stats["face_hits"] += 1
        LOG.debug(
            "face_hit score=%.2f box=(%d,%d,%d,%d) c=(%.1f,%.1f)",
            face_hit["score"],
            face_hit["x"],
            face_hit["y"],
            face_hit["w"],
            face_hit["h"],
            face_hit["cx"],
            face_hit["cy"],
        )

        if not self.mirror_arms:
            return face_hit

        # 2d: flip for pose_control mirror UX. 3d: unflipped; swap L/R in angle math.
        if self.arm_space == "2d":
            rgb = cv2.flip(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB), 1)
            mirrored_space = True
        else:
            rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
            mirrored_space = False

        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        detection_result = self._pose.detect(mp_image)
        height, width, _ = rgb.shape

        if not detection_result.pose_landmarks:
            LOG.debug("pose_miss while face locked")
            return face_hit

        self.stats["pose_hits"] += 1
        # Pick pose whose nose is nearest the locked face (same image space as rgb)
        face_cx = (
            (width - 1 - face_hit["cx"]) if mirrored_space else face_hit["cx"]
        )
        best_i = 0
        best_dist = 1e9
        for i, pose_landmarks in enumerate(detection_result.pose_landmarks):
            nose = pose_landmarks[0]
            dist = abs(nose.x * width - face_cx) + abs(
                nose.y * height - face_hit["cy"]
            )
            if dist < best_dist:
                best_dist = dist
                best_i = i

        image_lms = detection_result.pose_landmarks[best_i]
        self._draw_arm_segments(
            frame_bgr, image_lms, width, height, mirrored_space
        )
        LOG.debug("pose_match dist=%.1f space=%s", best_dist, self.arm_space)

        if self.arm_space == "3d":
            if not detection_result.pose_world_landmarks:
                LOG.warning("no pose_world_landmarks — skip arm update")
            else:
                angles6 = self._angles_3d(
                    detection_result.pose_world_landmarks[best_i]
                )
                if angles6 is not None:
                    self._apply_arm_6dof(angles6, image_lms=image_lms)
        else:
            angles4 = self._angles_2d(image_lms, width, height)
            if angles4 is not None:
                self._apply_arm_2d(*angles4)

        return face_hit

    def close(self):
        LOG.info("human_stats %s", self.stats)
        self.face.close()
        self._pose.close()


def build_detector(
    mode,
    board=None,
    pose_model="lite",
    mirror_arms=True,
    face_mode="full",
    arm_space="3d",
):
    if mode in ("short", "full"):
        return FaceDetector(mode=mode)
    if mode == "human":
        if board is None:
            raise ValueError("human mode requires a board for arm servos")
        return HumanDetector(
            board=board,
            model=pose_model,
            mirror_arms=mirror_arms,
            face_mode=face_mode,
            arm_space=arm_space,
        )
    raise ValueError("unknown mode: {}".format(mode))


def parse_args():
    parser = argparse.ArgumentParser(
        description="TonyPi: face-locked head track + pose arm mirror (2d/3d)"
    )
    parser.add_argument(
        "--mode",
        choices=("short", "full", "human"),
        default="full",
        help="short/full=face only; human=face head-track + arm mirror",
    )
    parser.add_argument(
        "--pose-model",
        choices=("lite", "full", "heavy"),
        default="lite",
        help="human mode PoseLandmarker size (default: lite)",
    )
    parser.add_argument(
        "--face-mode",
        choices=("short", "full"),
        default="full",
        help="human mode face detector for camera aim (default: full)",
    )
    parser.add_argument(
        "--arm-space",
        choices=("2d", "3d"),
        default="3d",
        help="human arms: 3d→6DOF servos 6/7/8+14/15/16 (default) or 2d pose_control 4DOF",
    )
    parser.add_argument(
        "--no-arms",
        action="store_true",
        help="human mode: face head-track only",
    )
    parser.add_argument(
        "--conf",
        type=float,
        default=None,
        help="override face min confidence",
    )
    parser.add_argument(
        "--log-file",
        default="/home/pi/tonypro/scripts/face_track.log",
        help="path for motion log (default: scripts/face_track.log)",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0,
        help="auto-exit after N seconds (0 = run until ESC)",
    )
    parser.add_argument(
        "--no-gui",
        action="store_true",
        help="no OpenCV window (for unattended / SSH runs)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="DEBUG logging (face/pose per-frame)",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    setup_logging(log_file=args.log_file, verbose=args.verbose)
    LOG.info(
        "start mode=%s face_mode=%s pose=%s arms=%s space=%s duration=%s gui=%s",
        args.mode,
        args.face_mode,
        args.pose_model,
        not args.no_arms,
        args.arm_space,
        args.duration or "inf",
        not args.no_gui,
    )

    camera = CameraWrapper()
    fps_meter = FPSMeter()
    if args.mode == "human":
        # Tuned from first log: face locked 100%; yaw settled; pitch crept
        # with err_y≈55–77 — widen dead_y and soften pitch steps.
        tracker = ServoHeadTracker(
            p=0.05,
            dead_x=60,
            dead_y=80,
            update_interval=0.15,
            ema=0.22,
            max_delta_yaw=6,
            max_delta_pitch=6,
        )
    else:
        tracker = ServoHeadTracker()

    detector = build_detector(
        args.mode,
        board=tracker.board,
        pose_model=args.pose_model,
        mirror_arms=(not args.no_arms),
        face_mode=args.face_mode,
        arm_space=args.arm_space,
    )
    if args.conf is not None:
        detector.min_score = args.conf
        if isinstance(detector, HumanDetector):
            detector.face.min_score = args.conf

    window = "TonyPi Tracking ({})".format(detector.label)
    if not args.no_gui:
        cv2.namedWindow(window, cv2.WINDOW_NORMAL)
        cv2.resizeWindow(window, 960, 720)

    # Time-based lost timeout (frame counts race at 60+ fps when pose is skipped)
    LOST_TIMEOUT_S = 2.5
    RECENTER_COOLDOWN_S = 3.0
    last_face_t = time.time()
    last_recenter_t = 0.0
    box_color = (0, 255, 0)
    t0 = time.time()
    frames = 0
    face_lock_frames = 0

    try:
        while True:
            if args.duration and (time.time() - t0) >= args.duration:
                LOG.info("duration reached (%.1fs) — stopping", args.duration)
                break

            ret, frame = camera.read()
            if not ret or frame is None:
                time.sleep(0.01)
                continue
            frames += 1
            now = time.time()

            hit = detector.detect(frame)
            tracking_ok = False
            score = 0.0

            if hit is not None:
                x, y, w, h = hit["x"], hit["y"], hit["w"], hit["h"]
                score = hit["score"]
                face_lock_frames += 1
                last_face_t = now
                cv2.rectangle(frame, (x, y), (x + w, y + h), box_color, 2)
                cv2.circle(frame, (int(hit["cx"]), int(hit["cy"])), 5, (0, 255, 255), -1)
                tracker.update(hit["cx"], hit["cy"], frame.shape[1], frame.shape[0])
                tracking_ok = True
            elif (
                (now - last_face_t) > LOST_TIMEOUT_S
                and (now - last_recenter_t) > RECENTER_COOLDOWN_S
            ):
                tracker.move_to_center()
                last_recenter_t = now
                last_face_t = now  # avoid recenter spam while still searching
                LOG.info(
                    "face_lost for %.1fs — recenter (cooldown %.1fs)",
                    LOST_TIMEOUT_S,
                    RECENTER_COOLDOWN_S,
                )

            fps_meter.update()
            if frames % 30 == 0:
                LOG.info(
                    "heartbeat fps=%.1f frames=%d face_lock=%d/%.0f%% head=%s",
                    fps_meter.fps,
                    frames,
                    face_lock_frames,
                    100.0 * face_lock_frames / max(frames, 1),
                    {
                        "moves": tracker.stats["moves"],
                        "holds": tracker.stats["holds"],
                        "recenters": tracker.stats["recenters"],
                    },
                )

            if not args.no_gui:
                fps_meter.draw(frame)
                status = (
                    "Face locked {:.2f}".format(score)
                    if tracking_ok
                    else "Searching face..."
                )
                cv2.putText(
                    frame,
                    status,
                    (10, 54),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.7,
                    (0, 255, 0) if tracking_ok else (0, 0, 255),
                    2,
                )
                cv2.putText(
                    frame,
                    "{} | ESC to exit".format(detector.label),
                    (10, 84),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.7,
                    (0, 255, 255),
                    2,
                )
                cv2.imshow(window, frame)
                if cv2.waitKey(1) & 0xFF == 27:
                    break
    finally:
        elapsed = time.time() - t0
        LOG.info(
            "summary elapsed=%.1fs frames=%d face_lock=%d (%.0f%%) head_stats=%s",
            elapsed,
            frames,
            face_lock_frames,
            100.0 * face_lock_frames / max(frames, 1),
            tracker.stats,
        )
        camera.release()
        detector.close()
        if not args.no_gui:
            cv2.destroyAllWindows()
        LOG.info("stopped — log written to %s", args.log_file)


if __name__ == "__main__":
    main()
