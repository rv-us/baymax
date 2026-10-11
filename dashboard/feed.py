"""The camera and tracker a session or Mirror reads the patient from.

With a robot it is the robot's own camera (H.264 over UDP, else MJPEG), with the frames sent to the
GX10's pose service for tracking (the same code runs here as the fallback). In a dry run the laptop's
webcam, which the dashboard already holds, stands in. Only one program can receive the robot's video,
so a feed the caller opened (`own`) must be stopped by the caller.
"""
from __future__ import annotations

from .robot import Refused


def open_feed(settings, gateway, what: str = "Mirror", on_pose=None):
    """(camera, own): a started, tracking camera, or Refused saying why there isn't one.
    `on_pose` is called about 5 times a second with the newest pose (only for a feed opened here)."""
    if settings.dry_run:
        cam = gateway.webcam
        if cam is None or not cam.tracking:
            raise Refused(f"{what} needs a camera that can track arms: start the dashboard on a computer "
                          "with a webcam, and either the GX10 pose service or MediaPipe.")
        return cam, False
    from .webcam import Webcam
    cam = Webcam(robot_url=settings.robot_url, pose_url=settings.pose_url, on_pose=on_pose)
    if not cam.start():
        raise Refused(cam.status["detail"])
    if not cam.tracking:
        cam.stop()
        raise Refused("Nothing can track arms: the GX10 pose service isn't answering and MediaPipe "
                      "isn't installed here.")
    return cam, True
