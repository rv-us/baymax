"""``TonyPiDigitalTwin``: the drop-in the pose estimators use (replaces the old tonypi_sim window).

    twin = TonyPiDigitalTwin()      # starts the web viewer, prints/opens its URL
    twin.update_servos(pulses)      # {servo id or joint name: pulse}, e.g. what is sent to /servo/set_many
    twin.update_arms(...)           # angle-based convenience (same keywords the old sim had)
    twin.close()
"""
from __future__ import annotations

import math
import os
import threading
import webbrowser
from typing import Dict, Optional

from . import mapping
from .robot_link import make_backend
from .server import TwinServer
from .state import TwinState


class TonyPiDigitalTwin:
    def __init__(self, gui: bool = True, urdf_path: Optional[str] = None, headless: bool = False,
                 use_process: bool = True, host: str = "127.0.0.1", port: int = 8765,
                 open_browser: Optional[bool] = None, robot_url: Optional[str] = None,
                 robot_hz: float = 4.0, source: str = "estimator", state: Optional[TwinState] = None,
                 enforce_limits: bool = True):
        # ``use_process`` is accepted for compatibility with the old renderer; the viewer runs in
        # the browser, so there is nothing to put in a separate process.
        self.state = state or TwinState(urdf_path, enforce_limits=enforce_limits)
        self.server = TwinServer(self.state, host=host, port=port)
        self.source = source
        self.headless = headless
        if open_browser is None:
            open_browser = gui and not headless and os.environ.get("BAYMAX_TWIN_NO_BROWSER") != "1"
        self._open_browser = open_browser
        self._robot_url, self._robot_hz = robot_url, robot_hz
        self._started = False

    # ------------------------------------------------------------------ lifecycle
    @property
    def url(self) -> str:
        return self.server.url

    def start(self) -> "TonyPiDigitalTwin":
        if self._started:
            return self
        self.server.start()
        self._started = True
        if self._robot_url:
            self.connect_robot(self._robot_url, self._robot_hz)
        if self._open_browser:
            threading.Timer(0.4, lambda: webbrowser.open(self.url)).start()
        return self

    def close(self) -> None:
        self.server.stop()  # also releases the port when start() was never called
        self._started = False

    def connect_robot(self, url: Optional[str] = None, hz: float = 4.0, backend=None):
        return self.server.api.attach_robot(backend or make_backend(url), hz=hz, label=url or "")

    # compat no-ops / queries
    def step(self) -> None:
        pass

    def is_window_open(self) -> bool:
        return self._started

    def is_window_closed(self) -> bool:
        return not self._started

    # ------------------------------------------------------------------ writers
    def update_servos(self, servos: Dict, head: Optional[Dict] = None, label: str = "") -> None:
        """Drive the model with raw servo pulses (ids 1-18 or joint names)."""
        self.state.set_command(servos, source=self.source, label=label, head=head)

    def update_landmarks(self, points, visibility=None, legs: bool = True) -> None:
        """Raw estimator landmarks (N x 3, torso frame: x fwd, y left, z up, metres) drawn as a
        skeleton beside the robot, so estimator accuracy can be judged before it becomes servo pulses."""
        self.state.set_landmarks(points, visibility, source=self.source, legs=legs)

    def apply_pose(self, name: str) -> None:
        self.state.apply_pose(name, source=self.source)

    def set_angles(self, angles_deg: Dict[str, float]) -> None:
        """URDF joint angles in degrees (e.g. ``{"l_knee": 30}``), converted to servo pulses."""
        self.update_servos(mapping.angles_to_pulses(angles_deg, clamp=self.state.enforce_limits))

    def update_arms(self, l_pitch_deg=0.0, l_roll_deg=0.0, l_elbow_deg=180.0,
                    r_pitch_deg=0.0, r_roll_deg=0.0, r_elbow_deg=180.0, **_legacy) -> None:
        """Shoulder angles in URDF degrees, elbows as interior angles (180 = straight)."""
        self.set_angles({
            "l_shoulder_pitch": l_pitch_deg, "l_shoulder_roll": l_roll_deg, "l_elbow": -(180.0 - l_elbow_deg),
            "r_shoulder_pitch": r_pitch_deg, "r_shoulder_roll": r_roll_deg, "r_elbow": -(180.0 - r_elbow_deg),
        })

    def update_legs(self, l_knee_deg=180.0, l_hip_pitch_deg=0.0, l_ankle_deg=0.0,
                    r_knee_deg=180.0, r_hip_pitch_deg=0.0, r_ankle_deg=0.0) -> None:
        """Knee interior angle (180 = straight), hip pitch (+ thigh forward), ankle (+ dorsiflexion)."""
        self.set_angles({
            "l_knee": 180.0 - l_knee_deg, "l_hip_pitch": -l_hip_pitch_deg, "l_ankle_pitch": -l_ankle_deg,
            "r_knee": 180.0 - r_knee_deg, "r_hip_pitch": -r_hip_pitch_deg, "r_ankle_pitch": -r_ankle_deg,
        })

    def set_head(self, pitch_deg: float = 0.0, pan_deg: float = 0.0) -> None:
        self.state.set_command({}, source=self.source, head={
            "tilt": mapping.HEAD_CENTER_US + pitch_deg / mapping.HEAD_DEG_PER_US,
            "pan": mapping.HEAD_CENTER_US + pan_deg / mapping.HEAD_DEG_PER_US})

    # ------------------------------------------------------------------ readers
    def _command_angles(self) -> Dict[str, float]:
        ch = self.state.command
        angles = mapping.pulses_to_angles(ch.servos)
        angles.update(mapping.head_to_angles(ch.head["pan"], ch.head["tilt"]))
        return angles

    def get_joint_state(self, name: str) -> float:
        """Current command angle of a URDF joint, in radians."""
        return math.radians(self._command_angles().get(name, 0.0))

    def get_link_transforms(self):
        return self.state.robot.fk(self._command_angles())

    def get_link_rotation(self, link_name: str):
        """3x3 rotation (tuple of rows) of a link in the torso frame."""
        return self.get_link_transforms()[link_name][0]
