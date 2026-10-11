# Workout loop

Runs one workout session on the GX10: ready check, then for each exercise introduce, demonstrate, and for each set prompt, track and review. Design: [docs/workout-agent-loop.md](../docs/workout-agent-loop.md).

```bash
cd ~/baymax && source .venv/bin/activate
python -m workout --dry-run --keyboard   # no robot: fake robot API, type the patient's answers
python -m workout --keyboard             # real robot (BAYMAX_API_URL), typed answers
python -m workout                        # real robot and the robot's mic (voice.Listener)
```

Add `--fast` to skip waits and the countdown. Each session is saved to `sessions/<date>-<time>.json` with every state change, set result and answer, for the therapist.

| File | What it does |
|---|---|
| `loop.py` | The state table engine (`TRANSITIONS`, `INTERRUPTS`) and one handler per state |
| `plan.py`, `plans/` | Workout plan format (YAML) and loader with validation; `arms_basic` has two arm exercises |
| `robot.py` | The loop's robot calls; `say` always runs inside `listener.robot_speaking()` |
| `parts.py` | Plug-in pieces with stand-ins: ears, answer classifier, pose tracker, coach |

## Safety stops

Pain words ("hurts," "ow," "stop," ...) are checked on everything the mic hears, from the listener's thread. A match freezes the servos right away (`actions/stop` and `servo/stop`), without waiting for the current step, then the engine moves to `SAFETY_STOP`: the robot says "Let's stop here," the session ends and is flagged for the therapist. Leaving the camera view mid-set pauses the set and resumes it at the same rep.

## Stand-ins to replace

| Stand-in | Replaced by |
|---|---|
| `classify_ready`, `classify_trouble`, `is_pain` (keyword rules) | LLM classifier, #10 |
| `FakeTracker` (simulated reps) | Pose estimation, rep counting, mirroring, #11–#13 |
| `TemplateCoach` | LLM coach, #15 |
