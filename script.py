"""Show the robot's camera (H.264 over UDP when this machine can decode it, else MJPEG).

    BAYMAX_API_URL=http://<robot>:8000 python script.py      # q quits
"""
import time

import cv2

from streaming.robot_camera import RobotCamera

cam = RobotCamera().start()
print(f"robot camera: {cam.mode}")
last = 0.0
try:
    while True:
        frame, t = cam.latest()
        if frame is not None and t != last:
            last = t
            cv2.imshow("Baymax camera", frame)
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break
        time.sleep(0.005)
finally:
    cam.stop()
    cv2.destroyAllWindows()
