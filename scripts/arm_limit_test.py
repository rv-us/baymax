#!/usr/bin/env python3
"""Step through the captured arm limits on the real robot, a few percent at a time.

Each step in scripts/arm_limits.json gets a 0% and a 100% pose for its arm (the other arm stays
at stand):
  ref steps      0% = stand, 100% = the captured pose (all three arm servos interpolated)
  contact steps  0% = the captured shoulder pitch with roll and elbow at stand (arm swung to the
                 step's angle, straight, not rolled), 100% = the captured contact pose
The robot_api clamps anything outside the servo limits and the move result says so.

    python scripts/arm_limit_test.py twin              keep the digital twin looping the current step
    python scripts/arm_limit_test.py stand             robot to the stand pose
    python scripts/arm_limit_test.py move L_p0_rollin_straight 20
    python scripts/arm_limit_test.py list              steps with their 0% and 100% pulses
    python scripts/arm_limit_test.py mark L_ref_forearm_up recapture "why"   note a review result

`move` also tells a running `twin` which step to demonstrate (0% -> 100% and back, on loop).
"""
import argparse
import json
import math
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from robot_api.client import HttpBackend  # noqa: E402
from robot_api.servo.joints import STAND_POSE  # noqa: E402
from robot_api.servo.limits import LIMITS  # noqa: E402

LIMITS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "arm_limits.json")
REVIEW_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "arm_limits_review.json")
STATE_FILE = os.path.join(tempfile.gettempdir(), "baymax_arm_limit_test.json")
ARM = {"L": {"pitch": 8, "roll": 7, "elbow": 6}, "R": {"pitch": 16, "roll": 15, "elbow": 14}}
STAND = {sid: STAND_POSE[sid] for sid in range(1, 17)}
MIN_MS, MS_PER_PULSE = 1000, 3.0  # slow: at least 1 s, 3 ms per pulse of the largest change


# Hand-made steps for captures that came out wrong: name -> (0% arm pose, 100% arm pose).
# L_ref_forearm_up was captured with the shoulder flipped ~190 deg (pitch -64, unreachable), so
# this is the intended muscle pose from the T-pose capture: only the elbow bends, outward, up.
EXTRA_STEPS = {
    "L_ref_forearm_up_fixed": ({"pitch": 696, "roll": 519, "elbow": 575}, {"pitch": 696, "roll": 519, "elbow": 200}),
}


def endpoints(name, pose):
    """(0% pulses, 100% pulses) for the step's three arm servos, keyed by servo id."""
    side, arm = pose["side"], pose["arm"]
    if "start" in pose:
        return ({ARM[side][j]: v for j, v in pose["start"].items()}, {ARM[side][j]: v for j, v in arm.items()})
    ids = ARM[side]
    full = {ids[j]: arm[j] for j in ids}
    if "_ref_" in name:
        start = {sid: STAND[sid] for sid in full}
    else:
        start = {ids["pitch"]: arm["pitch"], ids["roll"]: STAND[ids["roll"]], ids["elbow"]: STAND[ids["elbow"]]}
    return start, full


def at(start, full, pct):
    f = max(0.0, min(300.0, pct)) / 100.0  # past 100% extrapolates; the API clamps to servo limits
    return {sid: int(round(start[sid] + f * (full[sid] - start[sid]))) for sid in start}


def out_of_range(pulses):
    return {sid: (p, LIMITS[sid]) for sid, p in pulses.items() if not LIMITS[sid][0] <= p <= LIMITS[sid][1]}


def load():
    with open(LIMITS_FILE) as f:
        poses = json.load(f)
    for name, (start, full) in EXTRA_STEPS.items():
        poses[name] = {"side": name[0], "start": start, "arm": full}
    return poses


def cmd_list(_):
    for name, pose in load().items():
        start, full = endpoints(name, pose)
        bad = out_of_range(full)
        flag = "  OUTSIDE LIMITS " + ", ".join(f"S{s}={p} (limits {lo}-{hi})" for s, (p, (lo, hi)) in bad.items()) if bad else ""
        print(f"{name:26s} 0%: {start}  100%: {full}{flag}")


def cmd_stand(args):
    robot = HttpBackend(url=args.api_url, timeout=30)
    print(robot.call("actions/stand"))


def cmd_move(args):
    poses = load()
    if args.step not in poses:
        raise SystemExit(f"unknown step {args.step!r}; see `list`")
    start, full = endpoints(args.step, poses[args.step])
    target = at(start, full, args.percent)
    robot = HttpBackend(url=args.api_url, timeout=30)
    now = {int(k): v for k, v in robot.call("servo/positions", servos=list(target))["positions"].items()}
    ms = max(MIN_MS, int(MS_PER_PULSE * max(abs(target[s] - (now.get(s) or target[s])) for s in target)))
    with open(STATE_FILE, "w") as f:
        json.dump({"step": args.step, "start": start, "full": full, "percent": args.percent}, f)
    print(f"{args.step} at {args.percent:g}%: from {now} to {target} over {ms} ms")
    res = robot.call("servo/set_many", targets={str(s): p for s, p in target.items()}, ms=ms, wait=True)
    if res.get("clamped"):
        print("  CLAMPED by servo limits:", res["clamped"])
    time.sleep(0.3)
    back = robot.call("servo/positions", servos=list(target))["positions"]
    print("  now reads:", back)


def cmd_mark(args):
    review = {}
    if os.path.exists(REVIEW_FILE):
        with open(REVIEW_FILE) as f:
            review = json.load(f)
    review[args.step] = {"status": args.status, "note": args.note,
                         "time": time.strftime("%Y-%m-%dT%H:%M:%S")}
    with open(REVIEW_FILE, "w") as f:
        json.dump(review, f, indent=2)
    print(f"{args.step}: {args.status} ({len(review)} steps reviewed, {REVIEW_FILE})")


def cmd_twin(args):
    from digital_twin import TonyPiDigitalTwin

    twin = TonyPiDigitalTwin(port=args.twin_port, source="arm_limit_test").start()
    print(f"Digital twin: {twin.url}  (Ctrl+C to stop). Waiting for a `move` to pick the step.")
    move, hold, state, mtime, t0 = 1.6, 1.0, None, None, time.monotonic()
    try:
        while True:
            try:
                m = os.path.getmtime(STATE_FILE)
                if m != mtime:
                    with open(STATE_FILE) as f:
                        state = json.load(f)
                    mtime, t0 = m, time.monotonic()
                    print(f"  demonstrating {state['step']} (robot at {state['percent']:g}%)")
            except (OSError, ValueError):
                pass
            if state is not None:
                t = (time.monotonic() - t0) % (2 * (move + hold))
                f = min(t, move) / move if t < move + hold else 1.0 - min(t - move - hold, move) / move
                f = 0.5 - 0.5 * math.cos(math.pi * f)
                start = {int(k): v for k, v in state["start"].items()}
                full = {int(k): v for k, v in state["full"].items()}
                twin.update_servos({**STAND, **at(start, full, 100.0 * f)}, label=state["step"])
            time.sleep(1 / 30)
    except KeyboardInterrupt:
        pass
    finally:
        twin.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api-url", default=os.environ.get("BAYMAX_API_URL", "http://192.168.1.149:8000"))
    ap.add_argument("--twin-port", type=int, default=8765)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    sub.add_parser("stand").set_defaults(fn=cmd_stand)
    sub.add_parser("twin").set_defaults(fn=cmd_twin)
    mk = sub.add_parser("mark")
    mk.add_argument("step")
    mk.add_argument("status", choices=("ok", "recapture"))
    mk.add_argument("note", nargs="?", default="")
    mk.set_defaults(fn=cmd_mark)
    mv = sub.add_parser("move")
    mv.add_argument("step")
    mv.add_argument("percent", type=float)
    mv.set_defaults(fn=cmd_move)
    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
