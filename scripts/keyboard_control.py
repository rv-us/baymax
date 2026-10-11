#!/usr/bin/python3
"""Drive TonyPi from the keyboard (run over SSH in a real terminal).

  W / S   walk forward / back          A / D   turn left / right
  Z / C   side-step left / right       space   return to stand pose
  Q / E   left arm up / down           T / G   right arm up / down
  X / Esc quit

Walking uses the factory action groups (docs ch.6 voice control uses the same
ones). Arms move the shoulder-pitch servos (left 8, right 16; see
docs/servo_joint_map.md) in steps between the stand pose (arm down) and a
raised-forward limit. Any walk resets the arms to the stand pose.

Safety:
  - Stop tonypi first (sudo systemctl stop tonypi); it shares the servo bus.
  - On start the body moves to the stand pose over 2 s. If the servos are limp
    (coast mode), support the robot before starting.
  - Refuses to move while any body servo is at or above --max-temp (default
    70 C; servos cut out at 85 C).
  - Holding the stand pose heats the leg servos. --coast-on-exit makes them
    limp on quit (support the robot first).
"""
from __future__ import annotations

import argparse
import os
import queue
import select
import sqlite3
import struct
import subprocess
import sys
import termios
import time
import tty

ACTION_DIR = "/home/pi/TonyPi/ActionGroups"
BODY_SERVOS = range(1, 17)

# key: (action group, times). go_forward/back need times=2 to take a real
# step (start + cycle + end), matching docs ch.6 voice_control_move.
WALK_KEYS = {
    "w": ("go_forward", 2),
    "s": ("back", 2),
    "a": ("turn_left", 1),
    "d": ("turn_right", 1),
    "z": ("left_move", 1),
    "c": ("right_move", 1),
}

# Shoulder pitch: (servo id, pulse with arm down at the side, pulse at the
# raised limit). Mirrored sides: left decreases to raise, right increases.
# Down values are the stand pose; docs ch.8/ch.11 raise them to 650 / 330.
ARMS = {
    "left": (8, 725, 125),
    "right": (16, 275, 875),
}
ARM_KEYS = {
    "q": ("left", +1),
    "e": ("left", -1),
    "t": ("right", +1),
    "g": ("right", -1),
}
ARM_STEP = 75  # pulses per key press (~18 deg)
ARM_MOVE_MS = 300
STANDUP_MS = 2000


def load_stand_pose() -> dict:
    con = sqlite3.connect(os.path.join(ACTION_DIR, "stand.d6a"))
    try:
        cols = [r[1] for r in con.execute("pragma table_info(ActionGroup)")]
        row = con.execute("select * from ActionGroup").fetchone()
    finally:
        con.close()
    return {i: row[cols.index(f"Servo{i}")] for i in BODY_SERVOS}


class TempMonitor:
    """Reads servo temperatures on its own Board instance.

    The SDK's receive thread closes and reopens its port after 5 s without
    data; keeping reads on a separate port handle means that never interrupts
    the movement commands sent through ActionGroupControl's board.
    """

    def __init__(self, rrc):
        self._pf = rrc.PacketFunction
        self.board = rrc.Board()
        self.board.enable_reception(True)
        time.sleep(0.3)

    def read(self, servo_id: int):
        q = self.board.bus_servo_queue
        while not q.empty():
            q.get_nowait()
        try:
            self.board.buf_write(self._pf.PACKET_FUNC_BUS_SERVO, [0x09, servo_id])
            data = q.get(timeout=0.3)
        except (queue.Empty, Exception):
            return None
        reply_id, _, ok, temp = struct.unpack("<BBbB", data)
        return temp if ok == 0 and reply_id == servo_id else None

    def hottest(self):
        temps = {i: self.read(i) for i in BODY_SERVOS}
        known = {i: t for i, t in temps.items() if t is not None}
        if not known:
            return None, None, len(temps)
        sid = max(known, key=known.get)
        return sid, known[sid], len(temps) - len(known)


class Robot:
    def __init__(self, dry_run: bool):
        self.dry_run = dry_run
        self.stand = load_stand_pose()
        self.arm = {side: down for side, (_, down, _) in ARMS.items()}
        if dry_run:
            self.agc = self.ctl = None
            return
        import hiwonder.ActionGroupControl as AGC

        self.agc = AGC
        self.ctl = AGC.ctl  # reuse AGC's board so only one handle writes

    def set_servos(self, pulses: dict, ms: int) -> None:
        if self.dry_run:
            print(f"  [dry-run] servos {pulses} over {ms} ms")
            return
        self.ctl.board.bus_servo_set_position(ms / 1000, [[i, p] for i, p in pulses.items()])

    def stand_up(self, ms: int = STANDUP_MS) -> None:
        self.set_servos(self.stand, ms)
        time.sleep(ms / 1000 + 0.2)
        self.arm = {side: down for side, (_, down, _) in ARMS.items()}

    def walk(self, name: str, times: int) -> None:
        if self.dry_run:
            print(f"  [dry-run] runActionGroup({name!r}, {times}, with_stand=True)")
        else:
            self.agc.runActionGroup(name, times, True)
            # runActionGroup leaves its go/back state as "mid-walk" after
            # with_stand; reset it so the next call starts cleanly instead of
            # skipping go_forward_start or replaying go_forward_end.
            setattr(self.agc, "__start", True)
            setattr(self.agc, "__end", False)
        # Action groups drive the arms too and end in the stand pose.
        self.arm = {side: down for side, (_, down, _) in ARMS.items()}

    def move_arm(self, side: str, direction: int) -> int:
        servo_id, down, raised = ARMS[side]
        sign = 1 if raised > down else -1
        lo, hi = sorted((down, raised))
        target = self.arm[side] + direction * sign * ARM_STEP
        target = max(lo, min(hi, target))
        if target != self.arm[side]:
            self.set_servos({servo_id: target}, ARM_MOVE_MS)
            self.arm[side] = target
        return target

    def coast(self) -> None:
        if self.dry_run:
            print("  [dry-run] unload servos 1-16")
            return
        for i in BODY_SERVOS:
            self.ctl.unload_bus_servo(i)
            time.sleep(0.02)


def read_key(fd: int) -> str:
    ch = os.read(fd, 1).decode(errors="ignore")
    if ch == "\x1b":  # Esc, or the start of an arrow-key sequence
        while select.select([fd], [], [], 0.02)[0]:
            os.read(fd, 8)
        return "esc"
    return ch.lower()


def other_servo_users() -> list:
    out = subprocess.run(["pgrep", "-af", "TonyPi.py|Joystick.py"],
                         capture_output=True, text=True).stdout
    return [line for line in out.splitlines() if "pgrep" not in line]


def main() -> int:
    ap = argparse.ArgumentParser(description="TonyPi keyboard control")
    ap.add_argument("--dry-run", action="store_true",
                    help="print commands instead of moving (still reads temps)")
    ap.add_argument("--max-temp", type=int, default=70,
                    help="refuse to move at or above this servo temp in C (default 70)")
    ap.add_argument("--coast-on-exit", action="store_true",
                    help="make body servos limp on quit (support the robot first)")
    args = ap.parse_args()

    if not sys.stdin.isatty():
        print("needs an interactive terminal (ssh -t ...)", file=sys.stderr)
        return 2
    users = other_servo_users()
    if users and not args.dry_run:
        print("other programs are using the servos; stop them first:\n  "
              + "\n  ".join(users)
              + "\n  sudo systemctl stop tonypi joystick", file=sys.stderr)
        return 2

    import hiwonder.ros_robot_controller_sdk as rrc

    temps = TempMonitor(rrc)
    robot = Robot(args.dry_run)

    def too_hot() -> bool:
        sid, t, missing = temps.hottest()
        if t is None:
            print(f"  could not read servo temperatures; refusing to move")
            return True
        if t >= args.max_temp:
            print(f"  servo {sid} is {t} C (limit {args.max_temp} C); let it cool. "
                  f"Quit with X; --coast-on-exit makes the servos limp.")
            return True
        return False

    print(__doc__.split("Safety:")[0].strip())
    if too_hot():
        return 1
    input("\nSupport the robot, then press Enter to move to the stand pose... ")
    robot.stand_up()
    print("ready")

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while True:
            key = read_key(fd)
            if key in ("x", "esc", "\x03"):
                break
            if key in WALK_KEYS:
                if too_hot():
                    continue
                name, times = WALK_KEYS[key]
                print(f"{key}: {name}")
                robot.walk(name, times)
            elif key in ARM_KEYS:
                side, direction = ARM_KEYS[key]
                pulse = robot.move_arm(side, direction)
                print(f"{key}: {side} arm {'up' if direction > 0 else 'down'} -> {pulse}")
            elif key == " ":
                print("space: stand")
                robot.stand_up(1000)
            # Drop keys pressed (or auto-repeated) while the robot was moving,
            # so it stops when the key is released.
            termios.tcflush(fd, termios.TCIFLUSH)
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)

    print("returning to stand pose")
    robot.stand_up(1000)
    if args.coast_on_exit:
        print("making body servos limp")
        robot.coast()
    else:
        print("servos are holding the stand pose; they heat up if left like this")
    return 0


if __name__ == "__main__":
    sys.exit(main())
