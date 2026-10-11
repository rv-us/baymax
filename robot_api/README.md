# robot_api

A layered Python API for Baymax (Hiwonder TonyPi Pro), served over HTTP, with LangChain tools that
let a Claude agent use it.

| Layer | What | Examples |
|---|---|---|
| `servo/` | Most granular control | Set or read any bus servo by ID or joint name, coast/hold, head pan and tilt |
| `actions/` | Preprogrammed moves | Walk, turn, side step, arm up/forward/down, grippers, stand, factory action groups |
| `sensors/` | Sensors and peripherals | Camera (snapshot, MJPEG stream, H.264 over UDP), ultrasonic distance and eye LEDs, temperature/humidity, light, dot matrix, voice box (speak, sounds, command words), microphone level, buzzer, battery and servo temperatures |

`api.py` registers every operation under a path such as `actions/walk`:
- `server.py` serves each operation as `POST /<path>`.
- `client.HttpBackend` calls that server.
- `api.LocalBackend` calls the hardware directly.
- `tools.make_tools(backend)` turns either backend into LangChain tools.
- `agent.py` is a chat command-line interface (CLI) on top of the tools.

## Safety (enforced in the library, for every caller)
- **Clamping:** pulses are clamped to each servo's range. That range is what the 127 factory action
  groups use (`servo/limits.py`), widened for the arms to the deepest contact poses captured by
  `scripts/arm_limit_capture.py`.
- **Arm self-collision:** a shoulder roll that would put the arm into the body, given that arm's
  shoulder pitch and elbow, is pulled out and reported under `arm_limited` (`servo/arm_limits.py`,
  map fitted from captured contact poses by `scripts/fit_arm_limits.py`; `BAYMAX_ARM_MARGIN` pulses
  of clearance, default 15). Only the target is checked, not the path.
  `python scripts/arm_limit_test.py verify` drives every captured contact past its limit on the
  robot and checks the arm stops clear (results in `scripts/arm_limits_verify.json`).
- **Speed limit:** at most 1 pulse/ms, and every move takes at least 300 ms.
- **Temperature:** off by default. With `BAYMAX_TEMP_CHECK=1`, motion is refused while any body servo is
  above 82.5 °C (reads 83 °C or more; cutoff 85 °C) (`BAYMAX_MAX_TEMP`), or when temperatures can't be read.
- **One move at a time:** a lock serializes motion. Walking, turning and repeat counts are capped at
  5 steps per call.
- **Risky action groups need opt-in:** groups that fall, kick or get up require
  `allow_risky=True`.
- **No conflicting programs:** the server or local agent won't start while `TonyPi.py` or
  `Joystick.py` is running (`sudo systemctl stop tonypi joystick`).
- **Arms kept off the body:** a shoulder roll that would put the arm into the torso is pulled out
  (`servo/arm_limits.py`, from the measured map in `servo/arm_limit_map.json`) and reported under
  `arm_limited`. Only the target pose is checked, not the path a move takes to get there, and
  other collisions (arm with arm, arm with head) are not checked.

## Setup (robot)
```bash
sudo apt install espeak-ng
cd ~/baymax && python3 -m venv --system-site-packages .venv   # keeps hiwonder/gpiod/smbus visible
.venv/bin/pip install langchain langchain-anthropic fastapi uvicorn httpx
```
The laptop only needs `pip install langchain langchain-anthropic httpx` for the HTTP backend.

## Run
```bash
# HTTP server on the robot (set a token; anyone on the network could move the robot otherwise).
# pulse: PipeWire holds the sound card; mic_shared: share the mic with the GX10 stream
# (streaming/setup_pi_audio.sh); TEMP_CHECK: refuse motion when a servo is too hot.
BAYMAX_API_TOKEN=secret BAYMAX_SPEAKER_PCM=pulse BAYMAX_MIC_PCM=mic_shared BAYMAX_TEMP_CHECK=1 \
  .venv/bin/python -m robot_api.server
curl -H 'Authorization: Bearer secret' :8000/sensors/distance
curl -H 'Authorization: Bearer secret' -X POST :8000/actions/walk -H 'content-type: application/json' -d '{"steps":2}'
curl :8000/endpoints        # every path and its arguments

# Agent on the robot, or on the laptop against the server
ANTHROPIC_API_KEY=... .venv/bin/python -m robot_api.agent --backend local
ANTHROPIC_API_KEY=... BAYMAX_API_TOKEN=secret python -m robot_api.agent --backend http --url http://169.233.72.93:8000

# Everything with fake hardware (no robot needed)
BAYMAX_DRY_RUN=1 python -m pytest tests
python -m robot_api.agent --dry-run -p "how far is the wall?"
```

## Camera
Only one program can open the camera, so the API owns it and hands it out three ways:

| Endpoint | Gives | Used by |
|---|---|---|
| `GET /sensors/camera.jpg`, `sensors/camera` | One JPEG (base64 for the tool) | Agents' "what do you see", the dashboard |
| `GET /sensors/camera.mjpg` | Live MJPEG over HTTP, up to ~23 fps | Browsers; `RobotCamera`'s fallback |
| `POST /sensors/camera/udp` | H.264 over UDP to the caller, renewed like a lease; `/stop`, `/status` | `streaming/robot_camera.py` `RobotCamera` on the GX10 |

While the UDP stream runs, the other two read 15 fps snapshots from its pipeline. Details:
[docs/running-the-stream.md](../docs/running-the-stream.md).

## Agent CLI
Type a task, and Claude drives the robot with the tools until the task is done. It narrates through
the speaker and shows its state on the dot matrix.
```bash
cd ~/baymax && .venv/bin/python -m robot_api          # on the robot (stop robot_api.server first)
python -m robot_api --backend http --url http://169.233.72.93:8000   # from a laptop via the server
python -m robot_api --dry-run -p "how far is the wall?"              # fake hardware, one task
```
- **API key:** read from `robot_api/.env` (`claude_key = sk-ant-...`), a repo-root `.env`, or
  `ANTHROPIC_API_KEY`. `.env` is gitignored.
- **Defaults:** `--model claude-haiku-5-5` and `--thinking low` (1024-token budget; also
  `off`, `medium`, `high`).
- **Local model:** any `--model` not starting with `claude-` runs on Ollama, no API key needed:
  `python -m robot_api --model qwen3.6:35b --ollama-url http://<gx10-ip>:11434` (or set
  `OLLAMA_HOST`). Ollama on the GX10 listens on localhost only, so from a laptop tunnel it first:
  `ssh -f -N -L 11434:127.0.0.1:11434 asus@<gx10-ip>`, then use `--ollama-url localhost:11434`.
  Laptop dependency: `pip install langchain-ollama`.
- **Other flags:**
  - `--max-steps 40`: caps the agent loop for one task.
  - `--show-thinking`: prints the model's reasoning.
- **Commands inside the CLI:**

  | Command | What it does |
  |---|---|
  | `/stop` | Freeze all servos |
  | `/rest` | Go limp; hold the robot first |
  | `/status` | Battery, hottest servo and distance |
  | `/clear` | Forget the conversation |
  | `/help` | List these commands |
  | `/quit` | Exit |

- **Ctrl+C:** during a task, it stops the robot and returns to the prompt.
- **Pi venv dependencies:** `langchain`, `langchain-anthropic`, `python-dotenv`, and `brotli>=1.1`.
  The system's brotli 1.0.9 breaks the Anthropic client with "Connection error".

## Configuration
Wiring and limits are in `config.py`:

| Device | Connection |
|---|---|
| Dot matrix | gpiochip4 dio 22, clk 24 (mounted upside down; drawn rotated 180°) |
| Light sensor | gpiochip4 line 7 (tentative) |
| AHT10 temperature/humidity | 0x38 |
| Ultrasonic | 0x77 |
| WonderEcho voice box | `/dev/ttyUSB0` |
| Audio | ALSA card `Device` |

Environment variables: `BAYMAX_DRY_RUN`, `BAYMAX_TEMP_CHECK` (`1` turns the temperature refusal on),
`BAYMAX_MAX_TEMP`, `BAYMAX_API_TOKEN`, `BAYMAX_API_URL`, `BAYMAX_API_HOST`, `BAYMAX_API_PORT`,
`BAYMAX_MODEL` (default `claude-opus-5-5`), `BAYMAX_MIC_PCM` (recording device; set it to `mic_shared`
while the microphone streams to the GX10, see [streaming/README.md](../streaming/README.md)),
`BAYMAX_SPEAKER_PCM` (playback device; set it to `pulse` when PipeWire runs on the Pi, or `say` stalls
about 30 s on every other call while PipeWire holds the sound card), `BAYMAX_STREAM_CAMERA` (camera
controls while a stream runs; default a fixed 12 ms exposure for a normally lit room, see `config.py` for a dim-room setting), `BAYMAX_UDP_SNAPSHOT_FPS` (snapshot
rate for the API's own camera users while the UDP stream runs; default 15).
