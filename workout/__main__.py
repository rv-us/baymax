"""
Run a workout session (on the GX10).

    python -m workout                        # real robot (BAYMAX_API_URL), robot's mic, fake pose
    python -m workout --keyboard             # type the patient's answers instead of speaking
    python -m workout --dry-run --keyboard   # no robot at all: fake robot API, typed answers

Pose tracking (#11-#13) isn't built yet, so every set is simulated: each rep takes
--rep-seconds and the first set of each exercise reports one form note.
"""

import argparse
import logging
import os


def main():
    parser = argparse.ArgumentParser(description="Run a Baymax workout session.")
    parser.add_argument("--plan", default="arms_basic", help="plan name in workout/plans, or a path")
    parser.add_argument("--dry-run", action="store_true", help="fake robot API, no hardware")
    parser.add_argument("--keyboard", action="store_true", help="type answers instead of the mic")
    parser.add_argument("--rep-seconds", type=float, default=1.0, help="simulated time per rep")
    parser.add_argument("--fast", action="store_true", help="no waits or countdown (testing)")
    args = parser.parse_args()

    if args.dry_run:
        os.environ["BAYMAX_DRY_RUN"] = "1"  # robot_api reads this on first import
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    from workout.loop import Settings, Workout
    from workout.parts import FakeTracker, KeyboardEars, ListenerEars, TemplateCoach
    from workout.plan import load_plan
    from workout.robot import Robot

    plan = load_plan(args.plan)
    if args.dry_run:
        from robot_api.api import LocalBackend
        backend = LocalBackend()
    else:
        from robot_api.client import HttpBackend
        backend = HttpBackend()

    listener = None
    if args.keyboard:
        ears = KeyboardEars()
    else:
        from voice import Listener
        print("Loading speech-to-text...")
        listener = Listener().start()
        ears = ListenerEars(listener)

    notes = [[f"keep your elbows straight at the top of {ex.name.lower()}"] if i == 0 else []
             for ex in plan.exercises for i in range(ex.sets)]
    settings = Settings()
    if args.fast:
        settings.ready_wait_s, settings.countdown = 0, False
    workout = Workout(plan, Robot(backend, muter=listener), ears,
                      FakeTracker(notes, rep_seconds=0 if args.fast else args.rep_seconds),
                      TemplateCoach(), settings)
    try:
        session = workout.run()
    finally:
        if listener:
            listener.stop()
    print(f"\nSession {session.status}{' (flagged for the therapist)' if session.flagged else ''}; "
          f"log saved to {workout.saved_path}")


if __name__ == "__main__":
    main()
