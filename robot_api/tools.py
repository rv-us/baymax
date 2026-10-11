"""LangChain tools for the robot, over either backend.

    from robot_api.tools import make_tools
    tools = make_tools(LocalBackend())            # on the robot
    tools = make_tools(HttpBackend("http://...")) # anywhere, via server.py

Refusals (too hot, out of range, sensor missing...) come back to the model as
{"error": ...} instead of raising, so it can react.
"""
from __future__ import annotations

from typing import Literal, Optional

from langchain_core.tools import tool

from .errors import RobotError

Side = Literal["left", "right"]


def make_tools(backend) -> list:
    def call(path: str, **kwargs):
        try:
            return backend.call(path, **{k: v for k, v in kwargs.items() if v is not None})
        except RobotError as e:
            return {"error": str(e)}
        except Exception as e:  # noqa: BLE001  hardware hiccups shouldn't abort the task
            return {"error": f"{type(e).__name__}: {e}"}

    # ---- servo: most granular ------------------------------------------------

    @tool
    def joint_map() -> dict:
        """List every bus servo joint name with its ID and allowed pulse range,
        plus the head PWM servos."""
        return call("servo/joints")

    @tool
    def servo_status() -> dict:
        """Read position, temperature, voltage and torque of all 16 body servos."""
        return call("servo/status")

    @tool
    def read_servo(servo: str) -> dict:
        """Read one servo. servo: joint name (e.g. left_shoulder_pitch) or ID 1-18."""
        return call("servo/read", servo=servo)

    @tool
    def set_servo(servo: str, pulse: int, ms: Optional[int] = None) -> dict:
        """Move one bus servo to a pulse (0-1000, 500 = middle). servo: joint
        name or ID. Out-of-range pulses are clamped to the joint's safe limits
        (reported as "clamped"). Speed is capped; ms is a minimum duration.
        Check joint_map first. Collisions between joints are NOT prevented:
        e.g. moving shoulder roll inward with the arm down hits the leg."""
        return call("servo/set", servo=servo, pulse=pulse, ms=ms)

    @tool
    def set_servos(targets: dict[str, int], ms: Optional[int] = None) -> dict:
        """Move several bus servos together. targets: {joint name or ID: pulse}.
        Same clamping and speed limits as set_servo."""
        return call("servo/set_many", targets=targets, ms=ms)

    @tool
    def look(pan: Optional[int] = None, tilt: Optional[int] = None) -> dict:
        """Point the head. pan 1200-1800, tilt 950-1800; 1500 = straight ahead.
        Lower tilt looks down (about 1000 sees the feet). Omit an axis to leave it."""
        return call("servo/head", pan=pan, tilt=tilt)

    @tool
    def coast_servos(servos: Optional[list[str]] = None) -> dict:
        """Turn servo torque off (limp) so they cool down. Default: all body
        servos. THE ROBOT WILL FALL unless a human is holding it; ask first.
        Motion is refused once any servo reaches 83 C (cutoff 85 C)."""
        return call("servo/coast", servos=servos)

    @tool
    def hold_servos(servos: Optional[list[str]] = None) -> dict:
        """Turn torque back on, holding servos where they are now."""
        return call("servo/hold", servos=servos)

    @tool
    def stop_motion() -> dict:
        """Emergency stop: cancel the running action and freeze all servos."""
        return call("servo/stop")

    # ---- actions: preprogrammed moves ----------------------------------------

    @tool
    def stand() -> dict:
        """Return to the standing pose (arms down). Do this before walking."""
        return call("actions/stand")

    @tool
    def walk(direction: Literal["forward", "back"] = "forward", steps: int = 1) -> dict:
        """Walk forward or back 1-5 steps (a few cm each). Check read_distance
        before walking forward."""
        return call("actions/walk", direction=direction, steps=steps)

    @tool
    def turn(direction: Side = "left", steps: int = 1) -> dict:
        """Turn in place, 1-5 steps of roughly 20-30 degrees each."""
        return call("actions/turn", direction=direction, steps=steps)

    @tool
    def side_step(direction: Side = "left", steps: int = 1) -> dict:
        """Step sideways to the robot's own left or right, 1-5 steps."""
        return call("actions/side_step", direction=direction, steps=steps)

    @tool
    def move_arm(side: Literal["left", "right", "both"],
                 position: Literal["down", "forward", "up"]) -> dict:
        """Swing an arm at the shoulder: down (at the side), forward
        (horizontal), or up (raised high)."""
        return call("actions/arm", side=side, position=position)

    @tool
    def gripper(side: Literal["left", "right", "both"],
                action: Literal["open", "close"]) -> dict:
        """Open or close a hand."""
        return call("actions/gripper", side=side, action=action)

    @tool
    def list_action_groups() -> dict:
        """List the robot's factory action groups (safe and risky)."""
        return call("actions/list")

    @tool
    def run_action_group(name: str, times: int = 1, allow_risky: bool = False) -> dict:
        """Play a factory action group (e.g. wave, bow, chest, twist, squat,
        stepping, wing_chun), 1-5 times. Risky groups (falling, kicks, getting
        up, crawling) need allow_risky=True; only after a human confirms."""
        return call("actions/run", name=name, times=times, allow_risky=allow_risky)

    # ---- sensors ---------------------------------------------------------------

    @tool
    def camera_view(width: int = 640):
        """Take a photo with the camera on top of the head and look at it. Use
        this to find objects, check the path ahead, or see the result of a move.
        It sees where the head points: aim it first with look() (tilt ~1000
        sees the feet)."""
        out = call("sensors/camera", width=width)
        if "error" in out:
            return out
        return [{"type": "image", "base64": out["jpeg_base64"], "mime_type": "image/jpeg"}]

    @tool
    def read_distance() -> dict:
        """Distance in mm to the nearest object (ultrasonic, max 5000). The
        sensor is on the LEFT SHOULDER pointing forward, so it measures ahead
        of the left side of the body; keep the left arm down for a forward
        reading. Things only in front of the right side may be missed."""
        return call("sensors/distance")

    @tool
    def set_eye_color(r: int, g: int, b: int) -> dict:
        """Set the RGB 'eye' LEDs on the left-shoulder ultrasonic sensor (0-255)."""
        return call("sensors/eyes", r=r, g=g, b=b)

    @tool
    def read_climate() -> dict:
        """Air temperature (C) from the sensor on top of the RIGHT CLAW. To
        sense how warm something is, bring the right hand close to it first.
        Humidity is broken on this robot (reads unavailable)."""
        return call("sensors/climate")

    @tool
    def robot_health() -> dict:
        """Battery voltage/charge, every servo's temperature, Pi CPU temperature.
        Rest the robot if any servo is at 80 C or more (motion stops at 83 C)."""
        return call("sensors/health")

    @tool
    def is_bright() -> dict:
        """Whether the light sensor sees light above its threshold."""
        return call("sensors/light")

    @tool
    def show_on_matrix(text: Optional[str] = None,
                       icon: Optional[Literal["heart", "smile", "sad", "check", "cross", "up", "down",
                                             "left", "right", "dots", "question"]] = None,
                       rows: Optional[list[str]] = None) -> dict:
        """Show something on the 16x8 LED matrix. Give exactly one of: text
        (2 chars fit, longer scrolls), icon, or rows (up to 8 strings of '0'/'1',
        up to 16 wide, top row first). Pass text="" to clear."""
        if text == "":
            return call("sensors/matrix/clear")
        if text is not None:
            return call("sensors/matrix/text", text=text)
        if icon is not None:
            return call("sensors/matrix/icon", name=icon)
        if rows is not None:
            return call("sensors/matrix/rows", rows=rows)
        return {"error": "give text, icon or rows"}

    @tool
    def say(text: str) -> dict:
        """Speak text out loud through the robot's speaker."""
        return call("sensors/voice/say", text=text)

    @tool
    def play_sound(name: str) -> dict:
        """Play a built-in sound by name (see the robot's audio folder, e.g. ok, wakeup)."""
        return call("sensors/voice/play", name=name)

    @tool
    def listen_for_command(timeout_s: float = 10) -> dict:
        """Wait for a spoken command on the voice box. A human must first say
        the wake word "hello hiwonder", then forward/back/turn left/turn right."""
        return call("sensors/voice/listen", timeout_s=timeout_s)

    @tool
    def mic_level(seconds: float = 2) -> dict:
        """Measure how loud it is around the robot (dBFS; ~-60 quiet, 0 clipping)."""
        return call("sensors/mic/level", seconds=seconds)

    @tool
    def beep(freq_hz: int = 1900, ms: int = 100, repeat: int = 1) -> dict:
        """Sound the buzzer."""
        return call("sensors/buzzer", freq_hz=freq_hz, ms=ms, repeat=repeat)

    return [joint_map, servo_status, read_servo, set_servo, set_servos, look,
            coast_servos, hold_servos, stop_motion,
            stand, walk, turn, side_step, move_arm, gripper, list_action_groups,
            run_action_group,
            camera_view, read_distance, set_eye_color, read_climate, robot_health, is_bright,
            show_on_matrix, say, play_sound, listen_for_command, mic_level, beep]
