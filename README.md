# Baymax

A physical therapy robot: a Hiwonder TonyPi Pro (Raspberry Pi 5) demonstrates exercises, watches the patient do them, listens for answers and pain, and logs each session for a therapist. An ASUS GX10 does the heavy work (vision, speech-to-text, LLMs).

## Two machines

| | Runs | Talks to the other through |
|---|---|---|
| **TonyPi** (the robot) | `robot_api` server: servos, speaker, sensors, camera | HTTP API on port 8000 (token in `BAYMAX_API_TOKEN`) |
| **GX10** | Everything else: workout loop, agents, pose estimation, speech-to-text, Ollama models, dashboard | Calls the robot API; receives the camera (H.264 over UDP, on request) and microphone (Opus over UDP) |

Most GX10 tools need `BAYMAX_API_URL=http://<robot>:8000` and `BAYMAX_API_TOKEN` set, and the Python environment in `~/venvs/baymax` (`scripts/setup_gx10_venv.sh` builds it; keep it outside the repo). Almost everything has a dry run (`--dry-run` or `BAYMAX_DRY_RUN=1`) that fakes the robot.

## What's where

| Folder | What it is | Docs |
|---|---|---|
| `robot_api/` | The robot's HTTP API, its safety limits, and a chat agent CLI on top of it | [robot_api/README.md](robot_api/README.md) |
| `streaming/` | Getting the robot's camera (`RobotCamera`) and microphone (`AudioReceiver`) onto the GX10 | [docs/running-the-stream.md](docs/running-the-stream.md), [streaming/README.md](streaming/README.md) |
| `voice/` | Speech-to-text on the GX10: `Listener.listen()` (Silero VAD + Whisper on the GPU), and `voice.bridge`, which sends what the patient says to the dashboard | [docs/running-the-stream.md](docs/running-the-stream.md#speech-to-text-voicelistener) |
| `vision/` | Pose estimation on the robot's camera: joint angles, body-in-view (`scripts/pose_server.py --stream`) | [docs/running-the-stream.md](docs/running-the-stream.md#pose-estimation-on-the-stream) |
| `workout/` | A workout session as a state machine: plans, demos, sets, safety stops, session logs | [workout/README.md](workout/README.md), design: [docs/workout-agent-loop.md](docs/workout-agent-loop.md) |
| `pt_agent/` | A PT check-in run by an LLM agent: finds the patient, safety demo, temperature, notes | [pt_agent/README.md](pt_agent/README.md) |
| `demo/` | A fixed, scripted run of the whole story for presentations: same steps every time, no camera, mic or model | [demo/README.md](demo/README.md) |
| `dashboard/` | Web app for whoever runs a session: animations, live sessions, analytics, coach | [dashboard/README.md](dashboard/README.md) |
| `digital_twin/` | 3D model of the robot in the browser, next to the real robot's servo readings | [digital_twin/README.md](digital_twin/README.md) |
| `arm_mvp/` | Arm mimic: one camera to the robot's (or twin's) arms, with calibration | docstring of `arm_mvp/arm_mimic.py` |
| `laptop_mvp/` | Earlier pose-estimation experiments (MediaPipe, YOLO, depth fusion, retargeting) | docstrings |
| `scripts/` | `start_gx10.sh` / `stop_gx10.sh` (the GX10's pose service and dashboard, one command), then tools and experiments: pose server, calibration, teleop, arm limit capture, tests on hardware | docstrings |
| `animations/` | Keyframe animations made in the dashboard: exercises and gestures | [dashboard/README.md](dashboard/README.md) |
| `docs/` | Guides and design docs (`docs/site/` is the vendor's TonyPi manual) | below |
| `tests/` | `python -m pytest tests` (runs with the fake robot) | |

Planning records for the dashboard work: [PLAN.md](PLAN.md), [DECISIONS.md](DECISIONS.md), [FINAL_REPORT.md](FINAL_REPORT.md), and `reviews/`.

## Guides

- [docs/running-the-stream.md](docs/running-the-stream.md): every session on the GX10: camera, microphone, pose, speech-to-text.
- [docs/streaming-setup.md](docs/streaming-setup.md): first-time network setup and troubleshooting (Wi-Fi, access point pinning).
- [docs/workout-agent-loop.md](docs/workout-agent-loop.md): how a workout session is meant to work, and what's built.
- [docs/servo_joint_map.md](docs/servo_joint_map.md): which servo is which joint.

## Before running on the robot

- Start the robot API with `BAYMAX_SPEAKER_PCM=pulse` (PipeWire holds the sound card; without it, speech stalls about 30 s on every other sentence) and `BAYMAX_MIC_PCM=mic_shared` (after `streaming/setup_pi_audio.sh`).
- The servo temperature check is off unless `BAYMAX_TEMP_CHECK=1`. Standing for about 25 minutes took the servos to 74 °C; they cut out at 85 °C.
- Pain words stop the robot in both `workout` and `pt_agent`; test that on the real robot before a patient session.
