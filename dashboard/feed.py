"""The camera and tracker a session or Mirror reads the patient from.

Best first: the camera the GX10 owns (dashboard/gx10_camera.py: the dashboard asks the GX10 for the picture and
the pose and never opens the robot's camera); in a dry run, the laptop webcam the dashboard already holds; failing
both, the robot's own camera (H.264 over UDP, else MJPEG) opened here, with the frames sent to the GX10's pose service
for tracking (the same code runs here as the fallback). Only one program can receive the robot's video, so a feed
the caller opened (`own`) must be stopped by the caller.
"""
from __future__ import annotations

from .robot import Refused


def open_feed(settings, gateway, what: str = "Mirror", on_pose=None):
    """(camera, own): a started, tracking camera, or Refused saying why there isn't one.
    `on_pose` is called about 5 times a second with the newest pose (only for a feed opened here)."""
    cam = gateway.webcam
    if cam is not None and (settings.dry_run or getattr(cam, "gx10_camera", False)):
        if not cam.tracking:
            raise Refused(cam.status["detail"] if getattr(cam, "gx10_camera", False) else
                          f"{what} needs a camera that can track arms: start the dashboard on a computer "
                          "with a webcam, and either the GX10 pose service or MediaPipe.")
        return cam, False
    if settings.dry_run:
        raise Refused(f"{what} needs a camera that can track arms: start the dashboard on a computer "
                      "with a webcam, and either the GX10 pose service or MediaPipe.")
    from .webcam import Webcam
    cam = Webcam(robot_url=settings.robot_url, pose_url=settings.pose_url, on_pose=on_pose)
    if not cam.start():
        raise Refused(cam.status["detail"])
    if not cam.tracking:
        cam.stop()
        raise Refused("Nothing can track arms: the GX10 pose service isn't answering and MediaPipe "
                      "isn't installed here.")
    return cam, True
