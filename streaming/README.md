# robot-stream

GX10-side tools for getting the TonyPi camera and microphone onto the GX10.

Step-by-step usage: [docs/running-the-stream.md](../docs/running-the-stream.md). Network setup and troubleshooting: [docs/streaming-setup.md](../docs/streaming-setup.md).

| File | What it does |
|---|---|
| `config.env` | All settings (Pi SSH host, interface names, Wi-Fi connection, video and audio settings, `VIDEO_ENABLED`) |
| `install.sh` | One-time setup: installs packages and checks GStreamer, numpy, and SSH to the Pi |
| `setup_pi_audio.sh` | One-time setup: makes a shared mic device (`mic_shared`) on the Pi so the stream and the robot API can both record |
| `network_setup.sh` | Turns off Wi-Fi power saving on both and pins the GX10 to the Pi's access point |
| `network_reset.sh` | Removes the access-point pin (run before leaving a venue) |
| `start_pi_sender.sh` | Starts the microphone stream on the Pi, auto-pointed at the GX10's current IP. The camera comes from the robot API instead (`robot_camera.py`); `VIDEO_ENABLED=1` also pushes video the old way |
| `stop_pi_sender.sh` | Stops the microphone stream on the Pi (and its video, with `VIDEO_ENABLED=1`) |
| `start_receiver.sh` | Headless receiver that prints fps (quick test; needs video already arriving, e.g. `VIDEO_ENABLED=1`) |
| `robot_camera.py` | `RobotCamera`: the robot's camera, newest frame only. Asks the robot API for H.264 over UDP (`UdpVideoLease`) or falls back to its MJPEG stream |
| `receiver.py` | `FrameReceiver`: decodes the UDP video to numpy frames. Run it directly to check fps (asks the robot via `BAYMAX_API_URL`) |
| `audio_receiver.py` | `AudioReceiver` class: keeps the last 30 s of mic audio as 16 kHz mono PCM, stamped with GX10 time. Run it directly to check levels or save a `.wav` |

## First time
```bash
cd streaming
./install.sh
./setup_pi_audio.sh
```

## Every session
```bash
export BAYMAX_API_URL=http://<robot>:8000 BAYMAX_API_TOKEN=<token>
./network_setup.sh        # once per venue / after reconnecting
./start_pi_sender.sh      # microphone
python3 receiver.py       # camera, from the robot API; or RobotCamera in your own code
python3 audio_receiver.py # or use AudioReceiver in your own code
```

## Before leaving the venue
```bash
./stop_pi_sender.sh
./network_reset.sh
```

## Audio
The Pi streams the WonderEcho Pro's microphone as Opus over RTP/UDP on `AUDIO_PORT` (5002), with in-band FEC so a lost packet can be rebuilt from the next one. Set `AUDIO_ENABLED=0` in `config.env` to stream video only.

The sender records from `mic_shared`, a dsnoop device in the Pi user's `~/.asoundrc`, so other programs can still use the mic. Start the robot API with `BAYMAX_MIC_PCM=mic_shared` so its `mic_level` records from it too; otherwise it gets "Device or resource busy" while the stream runs.

The Pi also runs PipeWire, the default audio device for desktop programs. `mic_shared` works because PipeWire leaves the mic closed when nothing records through it. While the stream runs, a program that records through PipeWire (anything using the default device, such as Hiwonder's voice demos) can't open the mic, and if such a program is already recording, the stream can't start. Stop the other program, or set `AUDIO_ENABLED=0`.

The robot's own voice is loud enough to clip the mic (it sits a few centimeters from the speaker), so code that listens should ignore audio captured while the robot is talking.
