"""
Run the scripted demo: the same steps in the same order every time, no camera, no
microphone, no model. See demo/README.md and demo/script.py.

    python -m demo --dry-run                  # fake robot, no waiting: check the script
    python -m demo --dry-run --auto           # fixed timings, no keypresses
    python -m demo                            # real robot (BAYMAX_API_URL), Enter at each cue
    python -m demo --fall --walk              # also the fall safety demo and walking to the patient

Press Ctrl-C at any time to freeze the robot where it is.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time

from demo.script import NUMBERS, REP_EVERY_S

log = logging.getLogger("demo")

AUTO_CUE_S = 2.5   # how long a cue waits with --auto


class Demo:
    def __init__(self, backend, steps, auto=False, sleep=time.sleep, input_fn=input,
                 sessions_dir="sessions"):
        from workout.robot import Robot

        self.backend, self.steps, self.auto = backend, steps, auto
        self.robot = Robot(backend)
        self.sleep, self.input_fn, self.sessions_dir = sleep, input_fn, sessions_dir
        self.events: list[dict] = []
        self.marks: dict = {}
        self.started = time.time()

    def event(self, kind, text=None):
        self.events.append({"t": round(time.time() - self.started, 1), "kind": kind, "text": text})

    def run(self) -> str:
        """Play every step. Returns the path of the session log."""
        try:
            for step in self.steps:
                self.do(step)
        except KeyboardInterrupt:
            log.warning("interrupted: freezing the robot")
            self.robot.stop_now()
            self.event("interrupted")
        return self.save()

    def do(self, step):
        k, a = step.kind, step.arg
        c = self.robot._call
        if step.note:
            log.info("[%s] %s", step.kind, step.note)
        if k == "say":
            self.event("robot", a)
            self.robot.say(a)
        elif k == "icon":
            self.robot._try("sensors/matrix/icon", name=a)
        elif k == "text":
            self.robot.show(a)
        elif k == "head":
            c("servo/head", **a)
        elif k == "arm":
            c("actions/arm", side=a[0], position=a[1])
        elif k == "walk":
            c("actions/walk", direction="forward", steps=a)
        elif k == "turn":
            c("actions/turn", direction="left", steps=a)
        elif k == "action":
            risky = a in ("lie_down", "stand_up_back")
            if a == "stand":
                c("actions/stand")
            else:
                c("actions/run", name=a, **({"allow_risky": True} if risky else {}))
        elif k == "animate":
            self.animate(a)
        elif k == "count":
            for i in range(a):
                self.sleep(REP_EVERY_S)
                self.robot.say(NUMBERS[i])
            self.event("reps", a)
        elif k == "pause":
            self.sleep(a)
        elif k == "cue":
            self.cue(a)
        elif k == "freeze":
            self.robot.stop_now()   # the same stop a pain word triggers in the real sessions
            self.event("pain_stop", "scripted")
        elif k == "mark":
            self.marks[a] = round(time.time() - self.started, 1)
            self.event(a)
        else:
            raise ValueError(f"unknown step {k!r}")

    def animate(self, name):
        from workout.keyframes import play

        path = os.path.join(os.path.dirname(__file__), "..", "animations", f"{name}.json")
        with open(path) as f:
            frames = json.load(f)["keyframes"]
        play(self.robot._call, frames, sleep=self.sleep)

    def cue(self, prompt):
        self.event("cue", prompt)
        if self.auto:
            self.sleep(AUTO_CUE_S)
            return
        self.input_fn(f"\n  >> {prompt}, then press Enter ")

    def save(self) -> str:
        os.makedirs(self.sessions_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self.started))
        path = os.path.join(self.sessions_dir, f"demo-{stamp}.json")
        flagged = "pain_stop" in self.marks
        with open(path, "w") as f:
            json.dump({"started": self.started, "ended": time.time(), "source": "scripted-demo",
                       "flagged": flagged, "temperature_simulated": True,
                       "summary": "Scripted demo: fixed script, no live perception. "
                                  "Pain stop in set 2; flagged for the therapist.",
                       "record": {"safety_demo": "completed" if "safety_demo" in self.marks else "skipped"},
                       "events": self.events}, f, indent=2)
        return path



def main():
    from demo.script import build

    parser = argparse.ArgumentParser(description="Run the scripted Baymax demo.")
    parser.add_argument("--dry-run", action="store_true", help="fake robot API, no hardware, no waiting")
    parser.add_argument("--auto", action="store_true", help="no keypresses: cues wait a fixed time")
    parser.add_argument("--fall", action="store_true", help="include the fall safety demo")
    parser.add_argument("--walk", action="store_true", help="really walk to the patient")
    args = parser.parse_args()

    if args.dry_run:
        os.environ["BAYMAX_DRY_RUN"] = "1"  # robot_api reads this on first import
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    if args.dry_run:
        from robot_api.api import LocalBackend
        backend = LocalBackend()
    else:
        from robot_api.client import HttpBackend
        backend = HttpBackend()

    demo = Demo(backend, build(walk=args.walk, fall=args.fall), auto=args.auto,
                sleep=(lambda s: None) if args.dry_run else time.sleep)
    print(f"Scripted demo ({'auto' if args.auto else 'Enter at each cue'}); Ctrl-C freezes the robot.")
    print(f"Session log: {demo.run()}")


if __name__ == "__main__":
    main()
