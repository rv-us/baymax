# Baymax dashboard

The control and review surface for whoever runs a session: play and make exercise animations, run and coach a session, see afterwards where the patient did well and where they drifted, and tune the digital twin against the real robot. One FastAPI app; the browser only ever talks to it.

```bash
pip install -r dashboard/requirements.txt

python -m dashboard --dry-run                               # no robot, GX10 or camera needed
BAYMAX_API_TOKEN=secret python -m dashboard --robot-url http://<robot>:8000 --ollama-url http://<gx10>:11434
python -m dashboard --dry-run --host 0.0.0.0                # also reachable from other machines on the LAN
python -m dashboard --dry-run --webcam off                    # keep the fake camera frame (--webcam 1 picks another camera)
python -m dashboard --dry-run --gx10 asus@169.233.72.183      # where the GX10 is (the default); --gx10 off skips looking for it

In a dry run the laptop's webcam is used as the robot camera when one can be opened (`--webcam auto`, the default with `--dry-run`; needs `opencv-python` and `mediapipe`). `arm_mvp`'s estimator tracks the patient's arms on every frame: the camera panel shows the skeleton, the patient-in-frame state comes from it, and a live session mirrors a real patient in real time (the log is marked `source: live-webcam`, not synthetic). Exercises that track a joint it can't read (knee, trunk) fall back to the simulated patient and the log says so. With several people in view the tracker follows the one closest to the centre of the frame (`arm_mvp/people.py`; it stays with the person it has unless someone else is clearly closer, and resets its smoothing when the person changes), for mirroring and for scoring a session alike. Only joints drawn green (`TRACKING`) count: an arm that is holding or lost contributes no pulses to the robot, and a workout is scored only on frames where **both** arms are green, so a rep that wasn't properly seen is never evaluated. The pulses are damped before they leave the tracker (a median of five, a dead band, smoothing that eases off for small differences: `arm_mvp/filters.py` `PulseDamper`), a straight elbow is eased to straight so camera noise doesn't make it twitch (`solver.soften_elbow`), and an arm at the person's side reads shoulder pitch 0 with the elbow slightly out instead of the equivalent half-turn answer the solver could land on (`solver.hang_prior`). 

**The GX10 at startup.** Unless `--ollama-url` or `OLLAMA_HOST` says where Ollama is, or `--gx10 off` is given, the dashboard looks for the GX10 at `asus@169.233.72.183` (or `--gx10 user@host`, or `BAYMAX_GX10`) as soon as it starts, in the background, and keeps trying every few seconds until it answers (`dashboard/gx10.py`). It uses Ollama on the GX10 directly if that answers, otherwise opens an SSH tunnel to it on a free local port, and starts sending frames to the GX10's pose service if that is running. The SSH password comes from `BAYMAX_GX10_PASSWORD` or the first line of a git-ignored `.gx10_password` file in the repo folder (never from the code, and a test checks that): put the GX10's password there once and the coach connects by itself; with neither it tries your ssh key. `GET /api/status` shows the link as `gx10`.

**Offloading the tracking to the GX10.** The tracking is the heavy part, so the GX10 can do it. On the GX10, `./dashboard/start_pose_service.sh` (use `scripts/start_gx10.sh`, below, for the whole setup) uses the GX10's venv (`~/venvs/baymax`) and starts `python -m dashboard.pose_service` on port 9100 (log: `~/pose_service.log`). On the dashboard machine, `--pose-url` points at it, and `--pose-url auto` (the default) uses the `--ollama-url` host on port 9100 if the service answers there:

```
python -m dashboard --dry-run --ollama-url http://<gx10>:11434      # frames go to http://<gx10>:9100
python -m dashboard --dry-run --pose-url http://<gx10>:9100 / off
```

The dashboard sends frames as **UDP datagrams** to the service's port (and the service answers with the pose the same way). That is the same choice the robot's own video makes ([docs/streaming-setup.md](../docs/streaming-setup.md)), for the same reason: on a busy venue Wi-Fi, TCP retransmits frames nobody wants any more and stalls everything behind one lost packet (measured here: 1.4 s per frame and 8 s stalls over TCP on a congested link, against 25 to 46 ms on a quiet one). With UDP a lost datagram costs one frame, the sender never waits for an answer (the rate isn't capped by the round trip), and only the newest frame is ever worked on. Each frame carries one XOR parity datagram, so any single lost datagram is rebuilt, and the frame size steps down (480, 320, 240 px wide) when fewer than 60% of frames are answered and back up above 90%. If nothing comes back for 3 s the dashboard tracks locally and retries every few seconds. HTTP `/track` stays for tools and for a service that has no UDP port (`Webcam(transport="http")`). The service has no login: use it on a trusted network. When the real robot is connected its camera already reaches the GX10 as H.264 over UDP, and `scripts/pose_server.py --stream` reads poses off it; the dashboard should take poses from that server's `/latest` (a tiny JSON poll, each one independent) rather than ship any frames itself.
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
| Train | Mirror | Start, and the robot copies your arms: the camera's frames go to the GX10, which runs `arm_mvp`'s tracker and answers with the servo pulses; only arms drawn green move the robot (see below). Shows which arms are followed, the calibration, how many people are in view, and where the tracking is running. |
| Train | Plans | The plans in `workout/plans`, as the loader reads them. |
| Review | Sessions | Every exercise session and recording, saved by themselves. One session: overall score, reasons in plain words, and a chart per reason. Replay on the twin, delete. |
| Review | Coach | Chat about a session. The coach gets that session's measurements; replies stream in. |
| Tune | Calibrate | Told against doing side by side, per-servo readout, limits, offsets, speed caps, saved profiles. |
| | Connections (the Robot / Camera / Coach chips) | Honest status of the three links, Ollama address, SSH tunnel, theme. |
| | `#/patient` | Large-type view for the patient: exercise, cue, rep count. |

## Mirror

`camera -> GX10 tracker -> servo pulses -> robot arms`. With a robot the camera is the robot's own (H.264 over UDP, else MJPEG: `streaming/robot_camera.py`); in a dry run the laptop's webcam stands in. Frames go to the GX10 pose service (below), which answers with the pose and the pulses the arms should take; with no service, or if it goes quiet, the same tracker runs here. `dashboard/mirror.py` is only the loop that forwards the newest answer to the robot, at most 20 times a second, through the gateway, so the safety latch, the calibration profile and `robot_api`'s own limits all apply.

- **Only green arms move the robot.** An arm that is holding or lost is left alone, a stale answer is never replayed, and with nothing green the robot simply holds still. Stop eases the arms back to rest.
- **A safety stop ends mirroring at once** and leaves the robot frozen where it is. It won't start while a session, an animation or a recording is using the robot, and none of those start while it runs.
- Several people in view: it follows the one closest to the centre. Arms at the person's side read as pitch 0 with the elbows slightly out. Calibration is automatic (stand square to the camera, arms at your sides, hold still) and forgiving of camera noise.

## Live sessions on the GX10's tracking

**Running it on the GX10.** `./scripts/start_gx10.sh` starts three things (environment: `~/venvs/baymax`, see `scripts/setup_gx10_venv.sh`) against the robot at `BAYMAX_API_URL` / `BAYMAX_API_TOKEN` (else `streaming/config.env`), and prints where to open the dashboard (`http://<gx10-ip>:8080`, from any computer):

1. **The pose service with `--camera`** (port 9100). The GX10 *owns the robot's camera*: on the first request it receives the robot's video (H.264 over UDP), runs `arm_mvp`'s tracker on every frame and keeps the newest answer. After 20 s with nobody asking it lets the camera go, so other tools can have it. `GET /camera/pose?after=SEQ&wait=S` is the newest pose and overlay (it waits for one newer than `SEQ`), `GET /camera.jpg` the newest picture with the tracked person boxed and the arms drawn on it. `GET /health` says whether it owns a camera and how it is doing.
2. **The dashboard** (port 8080, on the network). It never opens the robot's camera: `dashboard/gx10_camera.py` (`Gx10Camera`) asks the pose service for the picture and the pose, and stands in wherever the dashboard held a webcam, so the camera panel, Live sessions and Mirror all get their picture and their tracking from the GX10. If the GX10 stops answering, nobody is "seen" (a session pauses), and it keeps trying. This works from a laptop too: `python -m dashboard --robot-url ... --pose-url http://<gx10>:9100` takes the camera from the GX10 as soon as that service owns it (and, with `--gx10`, when the GX10 turns up after the dashboard started). A laptop webcam in a dry run is left alone and still sends its own frames.
3. **The microphone bridge** (`voice/bridge.py`, below), unless `VOICE=0`.

`./scripts/stop_gx10.sh` stops them. Logs: `~/pose_service.log`, `~/dashboard.log`, `~/voice_bridge.log`.

**The microphone bridge.** `python -m voice.bridge [--dashboard URL]` runs on the GX10: `voice.Listener` (Silero VAD, then Whisper `small.en` on the GPU) turns the robot's microphone stream into text and posts each transcript to `POST /api/voice/heard {text}`. A pain word there stops the robot at once (the safety latch, as typing it would); anything else is the patient's answer if a session is waiting for one (`GET /api/voice` says `waiting`), and is dropped otherwise, so a stray remark never answers a later question. `GET /api/voice` also says `speaking`: the dashboard knows when a `sensors/voice/say` or `play` call is in flight, and the bridge makes the listener ignore the microphone for exactly that long so the robot never answers itself. If the dashboard is away the bridge keeps trying and drops what it heard meanwhile. It needs the Pi's microphone sender (`streaming/start_pi_sender.sh`, which `start_gx10.sh` runs).

A session reads the patient the same way Mirror does: with a robot, from the robot's own camera (opened for the length of the session and let go at the end: `dashboard/feed.py`), with every frame tracked by `arm_mvp` on the GX10 pose service; in a dry run, from the laptop webcam. If there is no camera, or nothing can track (the GX10 isn't answering and MediaPipe isn't installed here), the patient is simulated instead, the log is marked synthetic, and the Live screen says why.

- **The Live screen shows the tracker's output.** The picture has the tracked person boxed (`Tracking`, or `Tracking 1 of N` when several are in view) and each arm drawn green, amber or red; the Tracking panel beside it says where the maths ran (GX10, over UDP, how many frames were answered, how long one took), who is followed, whether the patient is calibrated, which arms count, and the shoulder and elbow angles the session is scored on. It is the `tracking` field of `live` in `GET /api/status` and on the event stream. While a session owns the robot's camera, `GET /api/camera.jpg` is that annotated picture (the camera can't be opened twice).
- **The robot follows the patient's pace.** The GX10 answers with the tracked joint angle; `workout/pacing.py` turns it into progress in reps (2.25 is two reps and a quarter), and the robot's demonstration is played at that progress plus a quarter of a rep (`LEAD`), never further ahead and at most 1.5x the exercise's tempo. A patient who slows down gets a robot that waits a quarter-rep ahead of them; one who stops, the same. The Live screen shows how far ahead the robot is. The loop runs on a fixed clock of `synthetic.HZ` samples a second whatever the robot calls take, so the lead doesn't drift slow.
- Reps are counted only on frames where both arms are green. When the patient is out of view the session pauses, as before.

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
| `live.py` | Runs `workout.loop.Workout` with dashboard ears and a tracker: `WebcamTracker` (the patient read from the GX10's pose answers) or, with no camera, `SimTracker` (a synthetic patient) |
| `feed.py` | Opens the camera a session or Mirror reads from: the robot's own, tracked on the GX10, or the dry run's webcam |
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
| `POST /api/mirror/start`, `/stop`, `GET /api/mirror`, `GET /api/mirror.jpg` | Mirror: start and stop it, its state (also on the event stream), and the camera picture with the arms drawn on. Refused while a session, an animation, a recording or a safety stop has the robot |
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
- **The microphone is `voice/bridge.py`**, which runs on the GX10 (see above). Without it, answers and pain words have to be typed in the dashboard (`POST /api/safety/heard` still takes typed text).
- **SSH tunnel is untested**, especially password login through `SSH_ASKPASS` on Windows.
- **Easing on the robot is stepped**: moves are sent in 300 ms pieces (the robot API's minimum).
- **The recorder samples at up to 10 Hz**, less over a slow link; very fast hand movements are under-sampled.
