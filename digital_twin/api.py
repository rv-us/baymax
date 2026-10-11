"""Transport-independent twin operations, shared by the stdlib server and the FastAPI router."""
from __future__ import annotations

from typing import Optional

from . import mapping
from .robot_link import RobotMirror, make_backend
from .state import TwinState


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class TwinApi:
    def __init__(self, state: TwinState, allow_robot_config: bool = True):
        self.state = state
        self.allow_robot_config = allow_robot_config
        self.mirror: Optional[RobotMirror] = None

    # -------------------------------------------------------------- reads
    def model(self) -> dict:
        model = self.state.robot.to_model()
        model["servos"] = mapping.servo_table()
        model["head"] = {"center_us": mapping.HEAD_CENTER_US,
                         "limits_us": {k: list(v) for k, v in mapping.HEAD_LIMITS_US.items()}}
        model["pulse_per_deg"] = mapping.PULSE_PER_DEG
        return model

    def poses(self) -> dict:
        return {"poses": [{"name": n, "title": mapping.pretty_name(n), "servos": {str(k): v for k, v in s.items()}}
                          for n, s in self.state.poses.items()]}

    def snapshot(self) -> dict:
        return self.state.snapshot()

    # -------------------------------------------------------------- writes
    def command(self, body: dict) -> dict:
        servos = body.get("servos") or {}
        head = body.get("head")
        if not isinstance(servos, dict) or (not servos and not head):
            raise ApiError("body needs 'servos': {id or joint name: pulse} and/or 'head': {pan, tilt}")
        try:
            clamped = self.state.set_command(servos, source=str(body.get("source", "api")),
                                             label=str(body.get("label", "")), head=head,
                                             replace=bool(body.get("replace", False)))
        except (KeyError, ValueError, TypeError) as e:
            raise ApiError(str(e)) from e
        return {"ok": True, "clamped": clamped, "version": self.state.version}

    def pose(self, body: dict) -> dict:
        try:
            clamped = self.state.apply_pose(str(body.get("name", "")))
        except KeyError as e:
            raise ApiError(str(e.args[0]), 404) from e
        return {"ok": True, "clamped": clamped}

    def view(self, body: dict) -> dict:
        try:
            self.state.set_view(str(body.get("view", "")))
        except ValueError as e:
            raise ApiError(str(e)) from e
        return {"ok": True, "view": self.state.view}

    def connect_robot(self, body: dict) -> dict:
        """{"url": ..., "hz": 4} starts mirroring; {"disconnect": true} stops."""
        if not self.allow_robot_config:
            raise ApiError("robot connection is configured on the server only (it is not bound to localhost)", 403)
        if body.get("disconnect"):
            if self.mirror:
                self.mirror.stop()
                self.mirror = None
            return {"ok": True, "connected": False}
        try:
            backend = make_backend(body.get("url"), body.get("token"))
        except ValueError as e:
            raise ApiError(str(e)) from e
        self.attach_robot(backend, hz=float(body.get("hz", 4.0)))
        return {"ok": True, "connected": True}

    def attach_robot(self, backend, hz: float = 4.0, label: str = "") -> RobotMirror:
        if self.mirror:
            self.mirror.stop()
        self.mirror = RobotMirror(self.state, backend, hz=hz, label=label).start()
        return self.mirror

    def clone(self, body: Optional[dict] = None) -> dict:
        if self.mirror is None:
            raise ApiError("no robot connected; connect one first", 409)
        if not self.mirror.clone():
            raise ApiError(self.state.robot_link.get("error") or "could not read the robot", 502)
        return {"ok": True}
