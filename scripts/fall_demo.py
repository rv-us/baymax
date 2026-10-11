#!/usr/bin/python3
"""Fall-and-get-up demo: lie down, detect the fall with the IMU, stand back up.

Same detection as the factory Functions/Fall_and_Stand.py: the accelerometer's
pitch angle atan2(ax, az) is near 180 deg when lying on the back and near 0 deg
when lying on the front (about 90 deg when upright). Needs ~40 cm of clear,
flat floor. Stop robot_api.server first (both would read the controller board).
"""
import math
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from robot_api.actions import groups, poses  # noqa: E402
from robot_api.hardware import check_exclusive, reader  # noqa: E402
from robot_api.sensors import dot_matrix, voice_box  # noqa: E402


def pitch_deg():
    """IMU pitch from the accelerometer, or None if no reading arrived."""
    board = reader().board
    board.get_imu()  # the SDK queues one sample; drop it, it may be stale
    time.sleep(0.05)
    for _ in range(50):
        imu = board.get_imu()
        if imu:
            ax, _, az = imu[:3]
            return math.degrees(math.atan2(ax, az))
        time.sleep(0.02)
    return None


def posture(angle):
    if angle is None:
        return "unknown"
    if abs(angle) > 160:
        return "back"
    if abs(angle) < 10:
        return "front"
    return "upright"


def main():
    check_exclusive()
    poses.stand(2000)
    dot_matrix.show_icon("smile")
    print("standing, pitch", round(pitch_deg() or 0), "->", posture(pitch_deg()))
    voice_box.say("Watch me fall down. Three, two, one.")
    dot_matrix.show_text("!")
    groups.run("lie_down", allow_risky=True)
    time.sleep(1)

    # Confirm the fall the way Fall_and_Stand does: 5 consistent readings.
    seen = [posture(pitch_deg()) for _ in range(5)]
    print("after fall:", seen)
    side = max(set(seen), key=seen.count)
    if side not in ("back", "front"):
        voice_box.say("Hmm, I don't think I fell over.")
        print("no fall detected; not running a get-up routine")
        return 1
    dot_matrix.show_icon("up")
    voice_box.say(f"Oops, I fell on my {side}. Getting up.")
    groups.run(f"stand_up_{side}", allow_risky=True)
    poses.stand(1000)
    print("after get-up:", posture(pitch_deg()))
    dot_matrix.show_icon("check")
    voice_box.say("Ta da! I'm back up.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
