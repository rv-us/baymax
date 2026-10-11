# pt_agent

Baymax as an LLM agent running a physical therapy check-in. It looks around for its patient, follows faces (smile on the LED matrix), and starts when someone waves or greets it. It then shows a fall safety demo, walks over, checks their temperature with the sensor on its right hand, and asks how they're feeling. It talks like a PT throughout and saves notes for the therapist to `sessions/pt-<date>-<time>.json`.

This is the agent-loop version of [workout/](../workout/), which is a fixed state machine: here the model decides each step, guided by the session plan in its system prompt ([agent.py](agent.py)).

```bash
python -m pt_agent                                  # on the GX10: robot camera stream, robot mic, qwen
python -m pt_agent --ears keyboard                  # real robot, type what the patient says
python -m pt_agent --dry-run --camera webcam --ears keyboard   # laptop: webcam, fake robot
python -m pt_agent --dry-run --camera none --ears keyboard     # no camera: type /wave to "wave"
```

Thinking is off by default for Ollama models (replies take seconds instead of up to half a minute); `--thinking low` turns it on. Add `--ollama-url http://<gx10-ip>:11434` when not on the GX10, `--skip-safety-demo` and/or `--skip-name` for quicker test runs (the demo's tool is removed too), `--stationary` when the robot is on a table (it turns toward the patient but never walks), and `--detector mediapipe` to use MediaPipe instead of YOLO (it usually finds only one person).

## How it works

| File | What it does |
|---|---|
| [perception.py](perception.py) | Background thread: newest camera frame → YOLO pose (everyone in view) → people with stable ids, face position, size (distance cue), waving / hand raised. Points the head at the person being followed, about 4 times a second |
| [ears.py](ears.py) | `MicEars` (robot mic through `voice.Listener`, GX10) or `KeyboardEars`. Wake words ("hi", "Baymax", "I'm ready"...) and pain words |
| [tools.py](tools.py) | The robot API tools plus session tools (below). Motion tools are blocked after a pain word until the patient says they're okay |
| [agent.py](agent.py) | PT system prompt and session plan, CLI. Uses `robot_api.agent` to build the model (qwen on Ollama, or any `claude-*`) |

Session tools (each runs its own fast loop, so the model makes one call per step):

| Tool | Does |
|---|---|
| `wait_for_patient` | Sweeps the head when nobody's in view (question mark on the matrix), follows each person in turn when someone is (smile), returns when someone waves, raises a hand, or greets the robot |
| `approach_patient` | Turns to face the followed person, then walks 2 steps at a time until the ultrasonic reads about 50 cm or they fill the camera view (max 6 moves) |
| `safety_demo` | Warns the patient, falls on its back on purpose (`lie_down`), says from the floor "If at any time you get hurt or fall, please say help, or call your handler", gets up (`stand_up_back`) and stands. The only place risky action groups are used |
| `take_temperature` | Right arm forward, tells the patient where to put their hand, holds, arm down. The sensor doesn't measure people yet, so it reports a normal reading (36.4-37.0 C) and the notes say `"temperature_simulated": true`. `--real-temperature` reads the sensor instead (until it settles, max 20 s) |
| `listen`, `say` | Patient's answer / robot speech (the mic is muted while the robot talks) |
| `people_in_view`, `follow_person`, `camera_view`, `look` | What the camera sees, head control |
| `record`, `end_session`, `clear_alert` | Therapist notes, finish, resume after a pain alert |

## Safety

- **"Help" ends the session.** From the listening thread, right away: the servos stop, the matrix shows 911, the robot says "If you need help, please call nine one one, or ask someone nearby to call for you", stands up, and saves the notes with `"emergency"` set. Every tool refuses after that.
- A pain word ("ow", "stop", "that hurts"...) stops the servos right away from the listening thread, flags the session, and blocks every motion tool until the model checks on the patient and calls `clear_alert`.
- No going limp, no risky action groups. `approach_patient` caps walking at 6 moves.
- The model is told never to diagnose; concerning symptoms end the session with "tell your therapist or nurse".

## Know before testing on the robot

- **Temperature:** by default the reading is a placeholder (see `take_temperature`). The sensor (AHT10) measures air temperature at the claw; with a hand on it, it climbs toward skin temperature (30-35 C), not body temperature.
- **Safety demo:** needs about 40 cm of clear, flat floor around the robot. It always falls on its back. A patient repeating "say help" back to the robot ends the session, since any "help" does.
- **Head direction:** which way pan and tilt go isn't verified on this robot. If the head turns away from people instead of toward them, set `BAYMAX_PAN_SIGN=1` (or `BAYMAX_TILT_SIGN=-1`).
- **Distance:** the ultrasonic sensor is on the left shoulder, so it may miss a patient standing to the robot's right; the camera size cue (`CLOSE_SIZE` in tools.py) covers that. Both thresholds need tuning on the real robot.
- **GX10 setup:** the venv that runs this needs `langchain`, `langchain-ollama`, `ultralytics` (already there) and `httpx`. The robot needs the API server running (`python -m robot_api.server`): the camera comes from it through `streaming/robot_camera.py` (H.264 over UDP on the GX10; MJPEG if another program on the same machine already has the UDP stream, or from a laptop without GStreamer).
