# robot_api

A layered Python API for Baymax (Hiwonder TonyPi Pro), served over HTTP, with LangChain tools that
let a Claude agent use it.

| Layer | What | Examples |
|---|---|---|
| `servo/` | Most granular control | Set or read any bus servo by ID or joint name, coast/hold, head pan and tilt |
| `actions/` | Preprogrammed moves | Walk, turn, side step, arm up/forward/down, grippers, stand, factory action groups |
| `sensors/` | Sensors and peripherals | Ultrasonic distance and eye LEDs, temperature/humidity, light, dot matrix, voice box (speak, sounds, command words), microphone level, buzzer, battery and servo temperatures |

`api.py` registers every operation under a path such as `actions/walk`:
- `server.py` serves each operation as `POST /<path>`.
- `client.HttpBackend` calls that server.
- `api.LocalBackend` calls the hardware directly.
- `tools.make_tools(backend)` turns either backend into LangChain tools.
- `agent.py` is a chat command-line interface (CLI) on top of the tools.

## Safety (enforced in the library, for every caller)
- **Clamping:** pulses are clamped to each servo's range. That range is what the 127 factory action
  groups use (`servo/limits.py`).
- **Speed limit:** at most 1 pulse/ms, and every move takes at least 300 ms.
- **Temperature:** motion is refused while any body servo is at 70 °C or hotter
  (`BAYMAX_MAX_TEMP`), or when temperatures can't be read.
- **One move at a time:** a lock serializes motion. Walking, turning and repeat counts are capped at
  5 steps per call.
- **Risky action groups need opt-in:** groups that fall, kick or get up require
  `allow_risky=True`.
- **No conflicting programs:** the server or local agent won't start while `TonyPi.py` or
  `Joystick.py` is running (`sudo systemctl stop tonypi joystick`).
- **Collisions are not checked.** Don't roll a shoulder inward with that arm down.

## Setup (robot)
```bash
sudo apt install espeak-ng
cd ~/baymax && python3 -m venv --system-site-packages .venv   # keeps hiwonder/gpiod/smbus visible
.venv/bin/pip install langchain langchain-anthropic fastapi uvicorn httpx
```
The laptop only needs `pip install langchain langchain-anthropic httpx` for the HTTP backend.

## Run
```bash
# HTTP server on the robot (set a token; anyone on the network could move the robot otherwise)
BAYMAX_API_TOKEN=secret .venv/bin/python -m robot_api.server
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

## Configuration
Wiring and limits are in `config.py`:

| Device | Connection |
|---|---|
| Dot matrix | dio 7, clk 8 |
| Light sensor | gpiochip4 line 24 |
| AHT10 temperature/humidity | 0x38 |
| Ultrasonic | 0x77 |
| WonderEcho voice box | `/dev/ttyUSB0` |
| Audio | ALSA card `Device` |

Environment variables: `BAYMAX_DRY_RUN`, `BAYMAX_MAX_TEMP`, `BAYMAX_API_TOKEN`, `BAYMAX_API_URL`,
`BAYMAX_MODEL` (default `claude-opus-5-5`).
