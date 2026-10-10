#!/usr/bin/python3
"""Capture camera frames + 6DOF arm angles for calibration (no servo writes)."""
import os
import time

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision as mp_vision

from mp_helper import CameraWrapper

# Import math helpers without opening the serial board.
import importlib.util

spec = importlib.util.spec_from_file_location(
    "ft_helpers",
    "/home/pi/tonypro/scripts/face_track.py",
)
# Preload modules that face_track needs, but patch Controller import by setting
# env before load — instead duplicate the small helper set:

POSE_MODELS = {
    "lite": "/home/pi/TonyPi/Functions/model/pose_landmarker_lite.task",
}


def xyz(lm):
    return np.array([lm.x, lm.y, lm.z], dtype=np.float64)


def norm(v):
    n = np.linalg.norm(v)
    return None if n < 1e-8 else v / n


def angle_3d(u, v):
    nu, nv = np.linalg.norm(u), np.linalg.norm(v)
    if nu < 1e-8 or nv < 1e-8:
        return None
    c = float(np.clip(np.dot(u, v) / (nu * nv), -1.0, 1.0))
    return float(np.degrees(np.arccos(c)))


def torso_frame(world):
    l_sh, r_sh = xyz(world[11]), xyz(world[12])
    l_hip, r_hip = xyz(world[23]), xyz(world[24])
    up = norm(0.5 * (l_sh + r_sh) - 0.5 * (l_hip + r_hip))
    right = norm(r_sh - l_sh)
    if up is None or right is None:
        return None
    forward = norm(np.cross(up, right))
    if forward is None:
        return None
    right = norm(np.cross(forward, up))
    if right is None:
        return None
    return up, right, forward


def arm_rot(world, sh_i, el_i, wr_i, side):
    frame = torso_frame(world)
    if frame is None:
        return None
    up, right, _ = frame
    sh, el, wr = xyz(world[sh_i]), xyz(world[el_i]), xyz(world[wr_i])
    upper = norm(el - sh)
    if upper is None:
        return None
    elev = angle_3d(upper, -up)
    elbow_int = angle_3d(sh - el, wr - el)
    if elev is None or elbow_int is None:
        return None
    outward = right if side == "R" else -right
    abd = float(np.degrees(np.arctan2(np.dot(upper, outward), -np.dot(upper, up))))
    return (
        int(np.clip(elev - 90.0, -90, 90)),
        int(np.clip(abd, -90, 90)),
        int(np.clip(180.0 - elbow_int, -90, 90)),
    )


STAND = {6: 575, 7: 800, 8: 725, 14: 425, 15: 200, 16: 275}
SPAN = 320
LIMITS = {
    6: (150, 900),
    7: (150, 900),
    8: (150, 950),
    14: (150, 850),
    15: (150, 850),
    16: (150, 950),
}


def to_pulse(sid, angle, invert=False):
    a = -angle if invert else angle
    pul = int(STAND[sid] + (a / 90.0) * SPAN)
    lo, hi = LIMITS[sid]
    return max(lo, min(hi, pul))


def map6(l_elev, l_abd, l_elb, r_elev, r_abd, r_elb):
    return {
        6: to_pulse(6, l_elb),
        7: to_pulse(7, l_elev),
        8: to_pulse(8, l_abd, invert=True),
        14: to_pulse(14, r_elb, invert=True),
        15: to_pulse(15, r_elev, invert=True),
        16: to_pulse(16, r_abd),
    }


def main():
    out = "/home/pi/tonypro/scripts/debug_frames"
    os.makedirs(out, exist_ok=True)
    camera = CameraWrapper()
    pose = mp_vision.PoseLandmarker.create_from_options(
        mp_vision.PoseLandmarkerOptions(
            base_options=mp_python.BaseOptions(model_asset_path=POSE_MODELS["lite"])
        )
    )
    print("Raise BOTH hands straight UP and hold — capturing 6s…", flush=True)
    time.sleep(1.5)
    t0 = time.time()
    frame_i = 0
    saved = []
    while time.time() - t0 < 6.0:
        ret, frame = camera.read()
        if not ret or frame is None:
            continue
        frame_i += 1
        h, w = frame.shape[:2]
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        res = pose.detect(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb))
        if not res.pose_landmarks or not res.pose_world_landmarks:
            continue
        img_lms = res.pose_landmarks[0]
        world = res.pose_world_landmarks[0]
        # draw
        for a, b in ((11, 13), (13, 15), (12, 14), (14, 16)):
            pa = (int(img_lms[a].x * w), int(img_lms[a].y * h))
            pb = (int(img_lms[b].x * w), int(img_lms[b].y * h))
            cv2.line(frame, pa, pb, (0, 255, 255), 3)
            cv2.circle(frame, pa, 6, (0, 255, 0), -1)
            cv2.circle(frame, pb, 6, (0, 255, 0), -1)
        # wrist height relative (lower y = higher in image)
        lw_y = img_lms[15].y
        rw_y = img_lms[16].y
        nose_y = img_lms[0].y
        hands_up = lw_y < nose_y and rw_y < nose_y

        ur = arm_rot(world, 12, 14, 16, "R")
        ul = arm_rot(world, 11, 13, 15, "L")
        if ur is None or ul is None:
            continue
        # mirror: robot L <- user R
        angles = ur + ul
        pulses = map6(*angles)
        text = (
            f"hands_up={hands_up} lw_y={lw_y:.2f} rw_y={rw_y:.2f} nose_y={nose_y:.2f}\n"
            f"L elev,abd,elb={angles[0:3]}  R={angles[3:6]}\n"
            f"pul 6={pulses[6]} 7={pulses[7]} 8={pulses[8]} | "
            f"14={pulses[14]} 15={pulses[15]} 16={pulses[16]}"
        )
        for i, line in enumerate(text.split("\n")):
            cv2.putText(
                frame, line, (8, 24 + i * 22), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 0), 2
            )
        if frame_i % 4 == 0:
            path = os.path.join(out, f"cap_{frame_i:04d}.jpg")
            cv2.imwrite(path, frame)
            saved.append((path, hands_up, angles, pulses, lw_y, rw_y, nose_y))
            print(
                f"saved {path} hands_up={hands_up} L={angles[0:3]} R={angles[3:6]} "
                f"pul7/15={pulses[7]}/{pulses[15]}",
                flush=True,
            )

    camera.release()
    pose.close()
    # pick best hands-up frame
    ups = [s for s in saved if s[1]]
    print(f"\nCaptured {len(saved)} frames, hands_up={len(ups)}", flush=True)
    if ups:
        # lowest wrist y = highest hands
        best = min(ups, key=lambda s: 0.5 * (s[4] + s[5]))
        print("BEST hands-up:", best[0], "angles", best[2], "pulses", best[3], flush=True)
        # copy to fixed name for inspection
        import shutil

        shutil.copy(best[0], os.path.join(out, "hands_up_best.jpg"))
    else:
        print("No hands-up frames detected — check if arms were above nose.", flush=True)


if __name__ == "__main__":
    main()
