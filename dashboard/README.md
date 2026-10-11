# Baymax dashboard

The control and review surface for whoever runs a session: make exercise animations, run and coach a session, and see afterwards where the patient did well and where they drifted. One FastAPI app; the browser only ever talks to it.

```bash
pip install -r dashboard/requirements.txt

python -m dashboard --dry-run                               # no robot, GX10 or camera needed
BAYMAX_API_TOKEN=secret python -m dashboard --robot-url http://<robot>:8000 --ollama-url http://localhost:11434
python -m dashboard --dry-run --host 0.0.0.0                # also reachable from a tablet or phone
```

Open http://127.0.0.1:8080. The built web app is committed (`web/dist`), so running needs no node and no internet.

| Mode | Robot | Sessions | Coach |
|---|---|---|---|
| `--dry-run` | `robot_api` fakes (`BAYMAX_DRY_RUN=1`) | 8 synthetic sessions are written to `sessions/` if it is empty | stub (canned text, labelled) unless `--ollama-url` is given |
| real | `--robot-url` or `BAYMAX_API_URL`; token from `BAYMAX_API_TOKEN` | whatever the workout loop saved | Ollama at `--ollama-url` or `OLLAMA_HOST`; model `--model` or `BAYMAX_COACH_MODEL` (default `qwen3.6:35b`); `BAYMAX_COACH_TIMEOUT` seconds (default 60) |

There is no login. On `--host 0.0.0.0` anyone on the network can move the robot through the dashboard.

## Screens

| Screen | What it is for |
|---|---|
| Studio | Set a pose on the dials, capture keyframes onto the tape, preview on the twin, then "Play on robot" (confirmed, and blocked while `sensors/health` says no). Record from the robot by hand, import frames, save as a plan exercise. |
| Live | Start a plan, see the session state, reps and tracked angle, answer for the patient, reposition, speak, show text, eye colour. Dry-run controls fake a hot servo, low battery, jammed servo, pain word, fall, and the patient's place in frame. |
| Sessions | List, then one session: coach summary, per-rep strips, patient-against-demonstration overlay, safety event timeline, calorie estimate. |
| Plans | The plans in `workout/plans`, as the loader reads them. |
| Connections (the Robot / Camera / Coach chips) | Honest status of the three links, Ollama address, SSH tunnel, theme. |
| `#/patient` | Large-type view for the patient: exercise, cue, rep count. |

Stop is on every screen. It freezes the robot and ends playback or the running session. It never latches. A pain word or a reported fall freezes the robot first, then latches: every motion call is refused (409, with the reason) until someone clears it in the banner.

## Layout

| File | What it does |
|---|---|
| `app.py` | Routes. Robot refusals reach the browser as `{"detail": <the robot's message>}` with the robot API's status |
| `robot.py` | `Gateway`: the only caller of `robot_api` (`LocalBackend` in dry run, `HttpBackend` otherwise). Adds the safety latch and refuses `allow_risky`; limits stay in `robot_api` |
| `safety.py`, `monitor.py` | Latched stop and alerts; background status of robot, camera and LLM, and `motion.ok` |
| `player.py` | Plays keyframes on the robot with `workout.keyframes.play` |
| `live.py` | Runs `workout.loop.Workout` with dashboard ears and `SimTracker` (replace it with the pose tracker) |
| `coach.py` | Summary facts, Ollama call with a `format` schema, template fallback, background jobs |
| `synthetic.py` | Seeded synthetic patient traces and sessions |
| `store.py` | JSON and YAML files: `sessions/`, `animations/`, `workout/plans/` |
| `tunnel.py` | `ssh -N -L 11434:127.0.0.1:11434 user@gx10` from the Connections drawer |
| `events.py` | Server-Sent Events at `/api/events`: `status`, `safety`, `live`, `playback`, `summary`, `sessions`, `tunnel` |
| `web/` | Vite + TypeScript + Preact. `cd dashboard/web && npm install && npm run build`; `npm run dev` proxies to a running dashboard on 8080 |

Changes outside `dashboard/`, all in `workout/` (nothing in `robot_api/` or `digital_twin/` was touched):
- `plan.py`: `demo: { keyframes: [...] }`, optional `tempo_s`, `check_keyframes` (arm joints and head only, pulses inside the robot API's limits).
- `keyframes.py` (new): keyframes to poses over time; used by the twin preview, robot playback and `robot.demonstrate`.
- `session.py` (new): the session schema and every score.
- `parts.py`: `SetResult` gained `started`, `trace`, `rep_details`.
- `loop.py`: `events`, `interrupted`, `schema`; an `operator_stop` interrupt.

## Session schema (version 2)

Full description and all scoring: [workout/session.py](../workout/session.py). Version 1 files load unchanged.

```text
session   schema, plan, started, ended, status, flagged, synthetic, patient, transitions, notes,
          events[], sets[], interrupted[], summary
event     t, type (left_frame | returned | prompt_closer | prompt_back | side_step | pain_word |
          fall | safety_stop | operator_stop), set?, detail?, transcript?
set       exercise, set, reps, target_reps, deviations, answer, trouble            (version 1)
          track_joint, target {top, bottom, tolerance, tempo_s}, started_t, seconds, finished,
          trace {hz, patient[], reference[]}, reps_detail[]
rep       n, t_start, t_end, peak_deg, trough_deg
```

Derived when a session is read, never stored: per rep `duration_s`, `rom_deg`, `rom_ratio`, `accuracy_pct`, `pace_ratio`, `misses`, `quality`; per set and per session `accuracy_pct`, `pacing_pct` (half consistency, half match to `tempo_s`), `fatigue` (last third of reps against the first).

A pose tracker only has to return `SetResult.trace` (or `rep_details`). The UI reads nothing else.

## Animations

`animations/<id>.json`: `{ name, keyframes: [{ ms, hold_ms?, ease?, servos: {joint: pulse}, head? }], exercise: { cue, sets, reps, track_joint, targets } }`. `ms` is the time to move into the keyframe. Every animation starts from the stand pose's arms. Three starters are included.

Import contract (`POST /api/animations/import`, the "Import motion from a video" slot): `{ "name": ..., "frames": [{ "t_ms": 0, "servos": { "left_shoulder_pitch": 700 } }] }`, the same `servos` payload `arm_mvp` sends to `servo/set_many`. Frames are thinned to keyframes; legs and hands are dropped.

## Coach summary

The model receives the scores above plus the last five sessions, and returns JSON: `headline`, `highlights` (2 to 3), `work_on`, `adjustment {kind, text}`, `follow_up`, `follow_up_reason`. The system prompt forbids diagnoses. Numbers on screen come from `workout/session.py`, never from the model, and the model cannot lower the follow-up flag. Unreachable, timed out or malformed replies fall back to a template summary with a "Coach offline" tag. Summaries are cached in the session file; "Regenerate" replaces them.

## Tests

```bash
BAYMAX_DRY_RUN=1 python -m pytest tests
```

`tests/test_dashboard_session.py` (schema, scoring, synthetic), `test_dashboard_animation.py` (keyframes, plan round trip), `test_dashboard_api.py` (proxy errors, safety latch, playback, coach fallback, live sessions). Two older tests fail before and after this work: `test_agent.py::test_api_key_from_env_file` and `test_digital_twin.py::test_tables_match_robot_api`.

## Known gaps

- **Not run against the real robot, camera, or GX10.** Everything was checked in dry run only.
- **Pose tracking is simulated.** Live sessions use `SimTracker`, so their logs are marked synthetic. At a demo pace above real time, event times and trace times in the saved log don't line up.
- **Twin limits have drifted** from `robot_api/servo/limits.py` (the older failing test). The dials use the robot API's limits; the twin clamps a few joints slightly differently when drawing.
- **Fall detection has no source.** `POST /api/safety/report {"kind": "fall"}` works, but `robot_api` has no IMU endpoint (`scripts/fall_demo.py` reads the board directly).
- **The microphone isn't wired in.** `voice.Listener` should post what it hears to `POST /api/safety/heard {"text": ...}`; until then pain words are typed in Live.
- **Patient-in-frame is a stub.** `POST /api/live/frame {visible, x, size}` is the contract; only the dry-run panel sends it.
- **Video to animation is a slot.** JSON frames import; converting a video is not built.
- **SSH tunnel is untested**, especially password login through `SSH_ASKPASS` on Windows. The drawer shows the manual command.
- **Easing on the robot is stepped**: moves are sent in 300 ms pieces (the robot API's minimum), so eased motion is an approximation.
- **Record from the robot** samples the twin's 4 Hz readback; fast hand movements are under-sampled.
- **Removing an exercise in Plans rewrites the YAML** and drops its comments.
