# Baymax dashboard

The control and review surface for whoever runs a session: play and make exercise animations, run and coach a session, see afterwards where the patient did well and where they drifted, and tune the digital twin against the real robot. One FastAPI app; the browser only ever talks to it.

```bash
pip install -r dashboard/requirements.txt

python -m dashboard --dry-run                               # no robot, GX10 or camera needed
BAYMAX_API_TOKEN=secret python -m dashboard --robot-url http://<robot>:8000 --ollama-url http://<gx10>:11434
python -m dashboard --dry-run --host 0.0.0.0                # also reachable from other machines on the LAN
python -m dashboard --dry-run --webcam off                    # keep the fake camera frame (--webcam 1 picks another camera)

In a dry run the laptop's webcam is used as the robot camera when one can be opened (`--webcam auto`, the default with `--dry-run`; needs `opencv-python` and `mediapipe`). `arm_mvp`'s estimator tracks the patient's arms on every frame: the camera panel shows the skeleton, the patient-in-frame state comes from it, and a live session mirrors a real patient in real time (the log is marked `source: live-webcam`, not synthetic). Exercises that track a joint it can't read (knee, trunk) fall back to the simulated patient and the log says so. Frames are not sent to the GX10 yet.
```

Open http://127.0.0.1:8080. The built web app is committed (`web/dist`), so running needs no node and no internet.

| Mode | Robot | Sessions | Coach, chat and generator |
|---|---|---|---|
| `--dry-run` | `robot_api` fakes (`BAYMAX_DRY_RUN=1`) | 8 synthetic sessions are written to `sessions/` if it is empty | offline coach and mock generator, both labelled, unless `--ollama-url` is given |
| real | `--robot-url` or `BAYMAX_API_URL`; token from `BAYMAX_API_TOKEN` | whatever the workout loop and the recorder saved | Ollama at `--ollama-url` or `OLLAMA_HOST`; model `--model` or `BAYMAX_COACH_MODEL` (default `qwen3.6:35b`); `BAYMAX_COACH_TIMEOUT` seconds (default 60) |

There is no login. On `--host 0.0.0.0` anyone on the network can move the robot through the dashboard. That is the point of "play on LAN", and it is why every request is checked on the server.

## Screens

`#/` is the front page (the pitch). The dashboard is grouped by job:

| Group | Screen | What it is for |
|---|---|---|
| Move | Library | Every exercise and emote. Point at one to preview it on a twin in this browser; click to play it on the robot. Shows sending, playing, finished, stopped and refused. |
| Move | Studio | Set a pose on the dials, capture keyframes onto the tape, preview on the twin, play on the robot, save as a plan exercise. |
| Move | Record | Let the arms go limp, move them by hand, trim the take on a chart of the joints, name it, save it, play it back. |
| Move | Generate | Describe a motion. The model on the GX10 writes it, the twin previews it, Save or Discard. Mock mode when the GX10 is unreachable. |
| Train | Live | Start a plan, see the session state, reps and tracked angle, answer for the patient, reposition, speak, show text. |
| Train | Plans | The plans in `workout/plans`, as the loader reads them. |
| Review | Sessions | Every exercise session and recording, saved by themselves. One session: overall score, reasons in plain words, and a chart per reason. Replay on the twin, delete. |
| Review | Coach | Chat about a session. The coach gets that session's measurements; replies stream in. |
| Tune | Calibrate | Told against doing side by side, per-servo readout, limits, offsets, speed caps, saved profiles. |
| | Connections (the Robot / Camera / Coach chips) | Honest status of the three links, Ollama address, SSH tunnel, theme. |
| | `#/patient` | Large-type view for the patient: exercise, cue, rep count. |

## Safety

- **Stop** is in the top bar of every screen, and again in the robot bar wherever something can be played. It freezes every servo and ends playback, recording and any running session. It never latches.
- **A click on a move asks first** ("Move the robot"). One-click play is a switch that turns itself off after 5 minutes.
- **Pain word or reported fall**: the robot is frozen first, then a stop is latched. Every motion call is refused (409, with the reason) until someone clears it in the banner.
- **Limits are enforced in the command path.** `robot.py` passes every `servo/set`, `servo/set_many` and `actions/arm` through the active calibration profile (`calibration.py`), then `robot_api` clamps again with its own limits. A profile can only narrow `robot_api`'s limits. Factory action groups run inside the robot and cannot be clamped; a profile can refuse them (`allow_action_groups: false`).
- **Everything sent over the LAN is validated**: ids match a strict pattern, keyframes go through `workout.plan.check_keyframes`, built-in moves must be on the offered list, speeds and trims must be numbers in range, calibration values must be whole numbers inside bounds, risky action groups are refused from a browser.

## Layout

| File | What it does |
|---|---|
| `app.py` | Routes. Robot refusals reach the browser as `{"detail": <the robot's message>}` with the robot API's status |
| `robot.py` | `Gateway`: the only caller of `robot_api`. Adds the safety latch, the calibration limiter and refuses `allow_risky` |
| `calibration.py` | Profiles (`calibration/*.json`), `Limiter` (clamp, offset, speed cap, readback), the debug table |
| `safety.py`, `monitor.py` | Latched stop and alerts; background status of robot, camera and LLM, and `motion.ok` |
| `player.py` | Plays keyframes (or a built-in move) on the robot; publishes progress and which joints were limited |
| `recorder.py` | Records the arms by hand, trims, saves an animation and logs a `kind: "recording"` session |
| `poses.py`, `starter.py` | Arm poses in degrees to pulses; the starter animations (`python -m dashboard.starter` rewrites them) |
| `generator.py` | Prompt to animation through Ollama, in degrees, clamped in code; rule-based mock |
| `chat.py` | Streamed coach chat (SSE) with session facts as context; offline answer when Ollama is down |
| `coach.py` | Summary facts, Ollama call with a `format` schema, template fallback, background jobs |
| `live.py` | Runs `workout.loop.Workout` with dashboard ears and `SimTracker` (replace it with the pose tracker) |
| `synthetic.py` | Seeded synthetic patient traces (with left and right joints) and sessions |
| `store.py` | JSON and YAML files: `sessions/`, `animations/`, `workout/plans/` |
| `tunnel.py` | `ssh -N -L 11434:127.0.0.1:11434 user@gx10` from the Connections drawer |
| `events.py` | Server-Sent Events at `/api/events`: `status`, `safety`, `live`, `playback`, `recorder`, `calibration`, `summary`, `sessions`, `tunnel` |
| `web/` | Vite + TypeScript + Preact. `cd dashboard/web && npm install && npm run build`; `npm run dev` proxies to a running dashboard on 8080 |

Changes outside `dashboard/` are all in `workout/` (nothing in `robot_api/` or `digital_twin/` was touched): `plan.py` (keyframe demos, `tempo_s`), `keyframes.py`, `session.py` (the schema and every score), `parts.py`, `loop.py`.

## API, briefly

| Route | What it does |
|---|---|
| `GET /api/library`, `POST /api/library/play {id \| builtin, speed?}` | List what can be played; play it on the robot |
| `POST /api/preview/solve {keyframes}` | Solved joint angles for a whole animation, for the in-browser preview. Never touches the robot |
| `POST /api/recorder/start`, `/stop`, `GET /api/recorder/takes/{id}`, `POST .../draft`, `POST .../save` | The recorder |
| `POST /api/generate {prompt, mock?}` | `{animation, source: "gx10" \| "mock", note}`; save it with `POST /api/animations` |
| `POST /api/chat {session_id?, messages}` | SSE: `meta`, `delta`, `done`, `error` |
| `GET /api/sessions`, `GET/DELETE /api/sessions/{id}`, `GET /api/sessions/{id}/replay?set=n`, `GET /api/analytics/trends` | Sessions and analysis |
| `GET/POST /api/calibration`, `PUT/DELETE /api/calibration/{id}`, `POST /api/calibration/active`, `GET /api/debug/servos`, `POST /api/debug/jog` | Calibration and debug |
| `POST /api/stop` | Freeze everything |

## Session schema (version 2) and scores

Full description and all scoring: [workout/session.py](../workout/session.py). Version 1 files load unchanged.

```text
session   schema, kind (exercise | recording), plan, started, ended, status, flagged, synthetic,
          patient, transitions, notes, events[], sets[], interrupted[], summary, recording?
set       exercise, set, reps, target_reps, deviations, answer, trouble            (version 1)
          track_joint, target {top, bottom, tolerance, tempo_s}, started_t, seconds, finished,
          trace {hz, patient[], reference[], joints? {left_shoulder[], right_shoulder[], ...}}, reps_detail[]
rep       n, t_start, t_end, t_peak?, peak_deg, trough_deg
```

Derived when a session is read, never stored:

- per rep: `duration_s`, `up_s`, `down_s`, `rom_deg`, `accuracy_pct`, `pace_ratio`, `smoothness_pct`, `symmetry_pct`, `misses`, `quality`
- per set and per session: `accuracy_pct`, `pacing_pct`, `smoothness_pct`, `symmetry_pct`, `completion_pct`, `fatigue`, `rom_by_joint`, `symmetry`, `tempo`, `heatmap`, `insights`
- `overall.score`: range 35, pacing 20, smoothness 15, left and right balance 15, reps completed 15. Parts that were not measured drop out and the rest are re-weighted.

A pose tracker only has to return `SetResult.trace` (with `joints` if it tracks each side) or `rep_details`. The UI reads nothing else.

## Animations

`animations/<id>.json`: `{ name, kind: exercise | emote, description?, keyframes: [{ ms, hold_ms?, ease?, servos: {joint: pulse}, head? }], exercise: { cue, sets, reps, track_joint, targets } }`. `ms` is the time to move into the keyframe. Every animation starts from the stand pose's arms. Twelve ship: front arm raise, side arm raise, overhead reach, arm circles, elbow curls, T hold, alternating reach, goalpost stretch, shoulder press, wave hello, cheer, gentle hug.

Squats and single-leg balance are not offered: `workout/plan.py` refuses leg keyframes because moving one leg joint at a time makes the robot fall.

## Tests

```bash
BAYMAX_DRY_RUN=1 python -m pytest tests            # 196 pass
```

`tests/test_dashboard_features.py` covers calibration limits in the command path, library play and its validation, the recorder, the generator and its fallbacks, the chat stream and its fallbacks, the analytics and session delete, replay and trends. Two older tests fail before and after this work: `test_agent.py::test_api_key_from_env_file` and `test_digital_twin.py::test_tables_match_robot_api`.

Browser click-through (needs Chrome and `npm i playwright-core` somewhere on the module path), against a running dry-run dashboard:

```bash
python -m dashboard --dry-run --port 8090
BASE=http://127.0.0.1:8090 node dashboard/web/e2e/flows.mjs       # 15 flows, PASS or FAIL each
BASE=http://127.0.0.1:8090 node dashboard/web/e2e/touch.mjs       # 5 phone and touch checks
node dashboard/web/e2e/shots.mjs 390 home library sessions        # full-page screenshots, checks for sideways overflow
```

## Known gaps

- **Not run against the real robot or camera.** They were not reachable while this was built, so robot motion was checked in dry run only.
- **The GX10 was reachable.** The generator and the coach chat were run against `qwen3.6:35b` on it (a motion in about 12 s, a streamed answer in about 4 s). The model drifts from the keyframe schema, so `generator.from_model` accepts its common variants and rejects a motion that never moves.
- **Pose tracking is simulated.** Live sessions use `SimTracker`, so their logs are marked synthetic.
- **Servo load is not reported** by this robot's bus API. The debug table says so; the gap between told and doing is the stall signal.
- **Built-in moves have no preview** and cannot be limited by a calibration profile, only refused.
- **Head servos are not in calibration profiles.** They keep `robot_api`'s limits.
- **Fall detection has no source.** `POST /api/safety/report {"kind": "fall"}` works, but `robot_api` has no IMU endpoint.
- **The microphone isn't wired in.** `voice.Listener` should post what it hears to `POST /api/safety/heard {"text": ...}`.
- **SSH tunnel is untested**, especially password login through `SSH_ASKPASS` on Windows.
- **Easing on the robot is stepped**: moves are sent in 300 ms pieces (the robot API's minimum).
- **The recorder samples at up to 10 Hz**, less over a slow link; very fast hand movements are under-sampled.
