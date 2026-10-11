#!/usr/bin/env python3
"""Guided capture of the arm contact poses used to build the arm self-collision limits.

Runs on the laptop against the robot API (leave robot_api.server running on the robot). The body
servos go limp on startup, so support the robot. For each step the digital twin, open in your
browser, demonstrates the pose and the terminal shows the robot's live arm readings. Pose the
real arm by hand, press SPACE once the reading is steady, and the script stops and waits before
the next step. Every capture is saved immediately, so you can quit and rerun at any time: steps
already in the file are skipped (--redo captures them again).

    python scripts/arm_limit_capture.py --api-url http://192.168.1.149:8000

Keys while posing:  SPACE capture   s skip   b back to the previous step   q quit
Keys after capture: ENTER next step   r redo this step   q quit
"""
import argparse
import datetime
import json
import math
import os
import statistics
import sys
import threading
import time
from collections import deque

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from robot_api.client import HttpBackend  # noqa: E402
from robot_api.errors import RobotError  # noqa: E402

DEFAULT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "arm_limits.json")
ARM = {"L": {"pitch": 8, "roll": 7, "elbow": 6}, "R": {"pitch": 16, "roll": 15, "elbow": 14}}
SIDE_NAME = {"L": "LEFT", "R": "RIGHT"}
STEADY_S, STEADY_PULSES = 0.6, 4  # steady = every arm servo within 4 pulses (~1 deg) for 0.6 s
HANGING = (0.0, 0.0, 180.0)

# Demo poses for the twin as (pitch, outward roll, interior elbow) in URDF degrees: pitch 0 down,
# -90 forward, -180 overhead, + behind; outward roll is mirrored for the right arm; elbow 180 is
# straight, below 180 bends inward (toward the body), above 180 outward. TonyPi has no upper-arm
# rotation: the elbow axis is parallel to the roll axis, so the forearm only bends in the plane
# the roll swings the arm in, never forward like a bicep curl. A step animates from its first
# pose to its second; contact steps overshoot a little because the real stop is wherever the arm
# touches.
PITCHES = [
    ("back30", 30.0, "swung about 30 deg BEHIND the body"),
    ("p0", 0.0, "hanging straight DOWN"),
    ("p45", -45.0, "swung 45 deg FORWARD (halfway to horizontal)"),
    ("p90", -90.0, "straight FORWARD, horizontal"),
]
CONTACTS = [
    ("rollin_straight", lambda p: ((p, 15.0, 180.0), (p, -25.0, 180.0)),
     "Keep the elbow STRAIGHT. Swing the arm inward, across toward the body's midline,\n"
     "    until it JUST touches the body (no pressure)."),
    ("rollin_bent", lambda p: ((p, 15.0, 135.0), (p, -25.0, 135.0)),
     "Bend the elbow about 45 deg INWARD (it only bends sideways, in the same plane the\n"
     "    arm swings in). Then swing the arm inward until the forearm or hand JUST touches\n"
     "    the body (no pressure)."),
    ("elbow_max", lambda p: ((p, 0.0, 180.0), (p, 0.0, 90.0)),
     "Leave the shoulder roll alone. Bend the elbow INWARD (sideways, toward the body)\n"
     "    until the forearm touches the body or the joint stops."),
]
REFERENCES = [
    ("ref_tpose", (HANGING, (0.0, 90.0, 180.0)), "Arm straight out to the SIDE, horizontal (T-pose), elbow straight."),
    ("ref_forward", (HANGING, (-90.0, 0.0, 180.0)), "Arm straight FORWARD, horizontal, elbow straight."),
    ("ref_up", (HANGING, (-180.0, 0.0, 180.0)), "Arm straight UP overhead, elbow straight."),
    ("ref_forearm_up", ((0.0, 90.0, 180.0), (0.0, 90.0, 270.0)),
     "Arm straight out to the SIDE (T-pose), then bend the elbow so the forearm points straight UP."),
]


def build_steps():
    """[(name, side, instructions, (demo start, demo end))] in capture order."""
    steps = []
    for side in "LR":
        for key, demo, text in REFERENCES:
            steps.append((f"{side}_{key}", side, text, demo))
        for pkey, pitch, ptext in PITCHES:
            for ckey, demo, ctext in CONTACTS:
                steps.append((f"{side}_{pkey}_{ckey}", side, f"Upper arm {ptext}.\n    {ctext}", demo(pitch)))
    return steps


class Keys:
    """Single keypresses without Enter, on Windows (msvcrt) and POSIX (cbreak terminal)."""

    def __enter__(self):
        if os.name != "nt":
            import termios
            import tty

            self._fd, self._termios = sys.stdin.fileno(), termios
            self._old = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        return self

    def __exit__(self, *exc):
        if os.name != "nt":
            self._termios.tcsetattr(self._fd, self._termios.TCSADRAIN, self._old)

    def get(self, timeout=None):
        """A key if one arrives within timeout seconds (None = wait forever), else None.
        Enter comes back as "\\n" on both platforms."""
        deadline = None if timeout is None else time.monotonic() + timeout
        if os.name == "nt":
            import msvcrt

            while True:
                if msvcrt.kbhit():
                    k = msvcrt.getwch()
                    return "\n" if k == "\r" else k.lower()
                if deadline is not None and time.monotonic() >= deadline:
                    return None
                time.sleep(0.01)
        import select

        if select.select([sys.stdin], [], [], timeout)[0]:
            k = sys.stdin.read(1)
            return "\n" if k == "\r" else k.lower()
        return None

    def wait(self, allowed):
        while (k := self.get()) not in allowed:
            pass
        return k


class TwinDemo:
    """Loops the current step's demonstration on the digital twin: ease to the demo pose, hold,
    ease back, hold. The other arm hangs."""

    def __init__(self, twin):
        self.twin, self.lock, self.demo = twin, threading.Lock(), None
        threading.Thread(target=self._run, daemon=True).start()

    def show(self, side, demo):
        with self.lock:
            self.demo = (side, demo, time.monotonic())

    def _arms(self, side, pose):
        pitch, roll_out, elbow = pose
        arms = {"L": HANGING, "R": HANGING, side: (pitch, roll_out if side == "L" else -roll_out, elbow)}
        self.twin.update_arms(*arms["L"], *arms["R"])

    def _run(self):
        move, hold = 1.6, 1.0
        while True:
            with self.lock:
                current = self.demo
            if current is not None:
                side, (a, b), t0 = current
                t = (time.monotonic() - t0) % (2 * (move + hold))
                if t < move:
                    f = t / move
                elif t < move + hold:
                    f = 1.0
                elif t < 2 * move + hold:
                    f = 1.0 - (t - move - hold) / move
                else:
                    f = 0.0
                f = 0.5 - 0.5 * math.cos(math.pi * f)  # ease in and out
                self._arms(side, tuple(x + (y - x) * f for x, y in zip(a, b)))
            time.sleep(1 / 30)


def read_arm(robot, side):
    got = robot.call("servo/positions", servos=list(ARM[side].values()))["positions"]
    vals = {j: got.get(str(sid)) for j, sid in ARM[side].items()}
    return None if any(v is None for v in vals.values()) else vals


def pose_and_capture(robot, keys, side):
    """Live readout until SPACE on a steady reading. Returns (arm values, spread) or a key: s, b, q."""
    hist = deque()
    while True:
        k = keys.get(0.0)
        if k in ("s", "b", "q"):
            print()
            return k
        try:
            vals = read_arm(robot, side)
        except RobotError as e:
            vals, err = None, str(e)[:50]
        else:
            err = "servo did not answer"
        now = time.monotonic()
        if vals is None:
            sys.stdout.write(f"\r    ({err}, retrying)" + " " * 30)
            sys.stdout.flush()
            time.sleep(0.2)
            continue
        hist.append((now, vals))
        while hist and hist[0][0] < now - STEADY_S:
            hist.popleft()
        spread = {j: max(v[j] for _, v in hist) - min(v[j] for _, v in hist) for j in vals}
        steady = now - hist[0][0] >= 0.8 * STEADY_S and max(spread.values()) <= STEADY_PULSES
        line = "  ".join(f"{j} {vals[j]:4d}" for j in ("pitch", "roll", "elbow"))
        sys.stdout.write(f"\r    {line}   {'STEADY - SPACE to capture' if steady else 'moving...':26s}")
        sys.stdout.flush()
        if k == " ":
            if not steady:
                sys.stdout.write("\r    hold still until it says STEADY" + " " * 40)
                sys.stdout.flush()
                time.sleep(0.6)
                continue
            print()
            return {j: int(statistics.median(v[j] for _, v in hist)) for j in vals}, spread
        time.sleep(0.03)


def load(path):
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def save(path, poses):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(poses, f, indent=2)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api-url", default=os.environ.get("BAYMAX_API_URL", "http://192.168.1.149:8000"))
    ap.add_argument("--file", default=DEFAULT_FILE, help="output JSON (default: %(default)s)")
    ap.add_argument("--redo", action="store_true", help="capture every step again, even ones already saved")
    ap.add_argument("--only", choices=("L", "R"), help="only this arm")
    ap.add_argument("--twin-port", type=int, default=8765)
    args = ap.parse_args()

    robot = HttpBackend(url=args.api_url, timeout=5.0)
    poses = load(args.file)
    steps = [s for s in build_steps() if args.only in (None, s[1])]
    todo = [i for i, s in enumerate(steps) if args.redo or s[0] not in poses]
    if not todo:
        print(f"All {len(steps)} steps are already in {args.file}. Use --redo to capture again.")
        return 0

    from digital_twin import TonyPiDigitalTwin

    twin = TonyPiDigitalTwin(port=args.twin_port, source="arm_limit_capture").start()
    demo = TwinDemo(twin)
    print(__doc__.split("\n\n")[0])
    print(f"\nDigital twin (demonstrates each step): {twin.url}")
    print(f"{len(todo)} of {len(steps)} steps to capture, saving to {args.file}")
    if input("All body servos go LIMP now. Is the robot supported? [y/N] ").strip().lower() != "y":
        twin.close()
        return 1
    robot.call("servo/coast")
    print("Servos are limp.")

    try:
        with Keys() as keys:
            pos = 0
            while pos < len(todo):
                name, side, text, demo_poses = steps[todo[pos]]
                other = SIDE_NAME["R" if side == "L" else "L"]
                demo.show(side, demo_poses)
                print(f"\n[{pos + 1}/{len(todo)}] {name}   ({SIDE_NAME[side]} arm, keep the {other} arm hanging)")
                print(f"    {text}")
                print("    The twin shows the motion; on the real robot stop where the arm touches.")
                if name in poses:
                    print("    (already captured; capturing again overwrites it)")
                got = pose_and_capture(robot, keys, side)
                if got == "q":
                    break
                if got == "s":
                    print("    skipped")
                    pos += 1
                    continue
                if got == "b":
                    pos = max(0, pos - 1)
                    continue

                arm, spread = got
                full = {int(k): v for k, v in robot.call("servo/positions")["positions"].items()}
                full.update({ARM[side][j]: v for j, v in arm.items()})
                poses[name] = {
                    "servos": {str(sid): p for sid, p in sorted(full.items())},
                    "side": side, "arm": arm, "spread": spread,
                    "time": datetime.datetime.now().isoformat(timespec="seconds"),
                }
                save(args.file, poses)
                print(f"    saved: pitch {arm['pitch']}  roll {arm['roll']}  elbow {arm['elbow']}")
                print("    ENTER = next step   r = redo this step   q = quit")
                k = keys.wait(("\n", "r", "q"))
                if k == "q":
                    break
                if k != "r":
                    pos += 1
    except KeyboardInterrupt:
        print()
    finally:
        twin.close()

    missing = [s[0] for s in steps if s[0] not in poses]
    print(f"\n{len(steps) - len(missing)}/{len(steps)} steps captured in {args.file}. Servos are still limp.")
    if missing:
        print("Still missing (rerun to capture them): " + ", ".join(missing))
    return 0


if __name__ == "__main__":
    sys.exit(main())
