"""Shared hardware handles and the safety guards every motion goes through.

Writes: hiwonder.ActionGroupControl's own board/ctl. Action groups write
through it too, so reusing it keeps one writer on the servo bus.

Reads: a second Board with reception enabled. The SDK's receive thread closes
and reopens its port after 5 s without data; keeping reads on their own port
handle means that never interrupts movement commands.
"""
from __future__ import annotations

import os
import queue
import struct
import subprocess
import threading
import time

from . import config
from .errors import RobotError

# Held for the duration of every servo motion, so moves never interleave.
# Stop/coast deliberately do not take it.
motion_lock = threading.RLock()

_init_lock = threading.Lock()
_agc = None
_reader = None

# Bus servo read commands: (command byte, reply struct).
READ_CMDS = {
    "position": (0x05, "<BBbh"),
    "vin": (0x07, "<BBbH"),
    "temp": (0x09, "<BBbB"),
    "torque": (0x0D, "<BBbb"),
}


def agc():
    """The hiwonder.ActionGroupControl module (or its fake)."""
    global _agc
    with _init_lock:
        if _agc is None:
            if config.DRY_RUN:
                from .fake import FakeAGC

                _agc = FakeAGC()
            else:
                import hiwonder.ActionGroupControl as AGC

                _agc = AGC
    return _agc


def ctl():
    """hiwonder.Controller used for all servo writes."""
    return agc().ctl


class Reader:
    def __init__(self):
        import hiwonder.ros_robot_controller_sdk as rrc

        self._bus_func = rrc.PacketFunction.PACKET_FUNC_BUS_SERVO
        self.board = rrc.Board()
        self.board.enable_reception(True)
        self.lock = threading.Lock()
        time.sleep(0.3)

    def read(self, servo_id: int, what: str):
        """One bus servo value, or None if the servo did not answer."""
        cmd, fmt = READ_CMDS[what]
        with self.lock:
            q = self.board.bus_servo_queue
            while not q.empty():
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
            try:
                self.board.buf_write(self._bus_func, [cmd, servo_id])
                data = q.get(timeout=0.3)
            except Exception:
                return None
        try:
            reply_id, _, ok, value = struct.unpack(fmt, data)
        except struct.error:
            return None
        return value if ok == 0 and reply_id == servo_id else None

    def battery_mv(self, timeout: float = 2.0):
        """Battery voltage in mV from the controller's periodic report."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                value = self.board.get_battery()
            if value:
                return value
            time.sleep(0.1)
        return None

    def buzzer(self, freq: int, on_s: float, off_s: float, repeat: int) -> None:
        with self.lock:
            self.board.set_buzzer(freq, on_s, off_s, repeat)


def reader():
    """Board handle for reads (servo state, battery) and the buzzer."""
    global _reader
    with _init_lock:
        if _reader is None:
            if config.DRY_RUN:
                from .fake import FakeReader

                _reader = FakeReader()
            else:
                _reader = Reader()
    return _reader


# ---- guards -----------------------------------------------------------------

_last_cool_check = 0.0


def read_temps(ids=range(1, 17)) -> dict:
    r = reader()
    temps = {i: r.read(i, "temp") for i in ids}
    for i in [i for i, t in temps.items() if t is None]:
        temps[i] = r.read(i, "temp")  # one retry for dropped replies
    return temps


def ensure_cool(force: bool = False) -> None:
    """Raise RobotError if any body servo is too hot (or temps are unreadable).
    Does nothing unless config.TEMP_CHECK is on."""
    global _last_cool_check
    if not config.TEMP_CHECK:
        return
    if not force and time.monotonic() - _last_cool_check < config.TEMP_CHECK_TTL_S:
        return
    temps = read_temps()
    known = {i: t for i, t in temps.items() if t is not None}
    missing = sorted(set(temps) - set(known))
    if len(missing) > 2:
        raise RobotError(f"could not read temperatures of servos {missing}; refusing to move")
    sid = max(known, key=known.get)
    if known[sid] >= config.MAX_SERVO_TEMP_C:
        from .servo.joints import joint_name

        raise RobotError(
            f"servo {sid} ({joint_name(sid)}) is {known[sid]} C (limit "
            f"{config.MAX_SERVO_TEMP_C} C); let the servos cool. coast_servos makes "
            "them limp so they cool faster, but the robot will fall unless supported."
        )
    _last_cool_check = time.monotonic()


def other_servo_users() -> list:
    """Running programs that also drive the servo bus (they would fight us)."""
    if config.DRY_RUN:
        return []
    out = subprocess.run(["pgrep", "-af", "TonyPi.py|Joystick.py|robot_api.server"],
                         capture_output=True, text=True).stdout
    me = str(os.getpid())
    return [line for line in out.splitlines()
            if "pgrep" not in line and line.split()[0] != me]


def check_exclusive() -> None:
    users = other_servo_users()
    if users:
        raise RobotError(
            "other programs are using the servos; stop them first (sudo systemctl stop "
            "tonypi joystick; pkill -f robot_api.server), or drive the robot through the "
            "API server with --backend http:\n  " + "\n  ".join(users)
        )
