# Workout loop

Runs one workout session on the GX10: ready check, then for each exercise introduce, demonstrate, and for each set prompt, track and review. Design: [docs/workout-agent-loop.md](../docs/workout-agent-loop.md). The dashboard runs this same loop for its live sessions (`dashboard/live.py`), and [pt_agent/](../pt_agent/) is the LLM-agent alternative.

```bash
cd ~/baymax && source ~/venvs/baymax/bin/activate   # scripts/setup_gx10_venv.sh builds it
python -m workout --dry-run --keyboard   # no robot: fake robot API, type the patient's answers
python -m workout --keyboard             # real robot (BAYMAX_API_URL), typed answers
python -m workout                        # real robot and the robot's mic (voice.Listener)
```

Flags: `--plan <name or path>` (default `arms_basic`), `--fast` (no waits or countdown), `--rep-seconds` (time per simulated rep), `--timing` (prints where the session's time went: per state, per robot call, unexplained gaps, and speech-to-text latency).

Each session is saved to `sessions/<date>-<time>.json`: every state change, event, set result and answer, for the therapist. The format (schema version 2) and the scores derived from it are described in `session.py`.

| File | What it does |
|---|---|
| `loop.py` | The state table engine (`TRANSITIONS`, `INTERRUPTS`) and one handler per state |
| `plan.py`, `plans/` | Workout plan format (YAML) and loader with validation; `arms_basic` has two arm exercises. Demos are `arm` moves, safe action groups, or dashboard `keyframes` |
| `keyframes.py` | Plays keyframe demos through the robot API (the dashboard's preview and "play on robot" use it too) |
| `pacing.py` | Pace leading: the robot demonstrates a quarter rep ahead of the patient, never further (used by the dashboard's live sessions) |
| `session.py` | The session log's schema and every score computed from it |
| `robot.py` | The loop's robot calls; `say` always runs inside `listener.robot_speaking()` |
| `parts.py` | Plug-in pieces with stand-ins: ears, answer classifier, pose tracker, coach |
| `timing.py` | The `--timing` report |

## Safety stops

Pain words ("hurts," "ow," "stop," ...) are checked on everything the mic hears, from the listener's thread. A match freezes the servos right away (`actions/stop` and `servo/stop`), without waiting for the current step, then the engine moves to `SAFETY_STOP`: the robot says "Let's stop here," the session ends and is flagged for the therapist. The robot then stays frozen where it stopped; it doesn't return to standing on its own. The dashboard's Stop button does the same (`operator_stop`, status `stopped_operator`). Leaving the camera view mid-set pauses the set and resumes it at the same rep.

## Stand-ins to replace

| Stand-in | Replaced by |
|---|---|
| `classify_ready`, `classify_trouble`, `is_pain` (keyword rules) | LLM classifier, #10. Misses seen on the real robot: a bystander's conversation was taken as the patient's answer, and "just a little bit" (to "any trouble?") counted as no trouble |
| `FakeTracker` (simulated reps; the dashboard uses its own `SimTracker`) | Pose-based rep counting and form checks, #12. Joint angles already exist: `vision/`, `scripts/pose_server.py --stream` |
| `TemplateCoach` | LLM coach, #15. The dashboard already summarizes sessions with an Ollama coach (`dashboard/coach.py`) |


`demo.alternate: true` (keyframes only) makes the keyframes one side's rep: the demonstration does that side, then the other (`workout/keyframes.py` `mirrored`), and in a session the robot mirrors the patient with one side on one rep and the other on the next. `workout/plans/arm_flow.yaml` uses it for alternating reach.
