# robot-stream

GX10-side tools for streaming the TonyPi camera to the GX10.

Step-by-step usage: [docs/running-the-stream.md](../docs/running-the-stream.md). Network setup and troubleshooting: [docs/streaming-setup.md](../docs/streaming-setup.md).

| File | What it does |
|---|---|
| `config.env` | All settings (Pi SSH host, interface names, Wi-Fi connection, video settings) |
| `install.sh` | One-time setup: installs packages and checks GStreamer, OpenCV, and SSH to the Pi |
| `network_setup.sh` | Turns off Wi-Fi power saving on both and pins the GX10 to the Pi's access point |
| `network_reset.sh` | Removes the access-point pin (run before leaving a venue) |
| `start_pi_sender.sh` | Starts the camera stream on the Pi, auto-pointed at the GX10's current IP. Also writes JPEG snapshots on the Pi for the robot API's camera |
| `stop_pi_sender.sh` | Stops the camera stream on the Pi |
| `start_receiver.sh` | Headless receiver that prints fps (quick test) |
| `receiver.py` | `FrameReceiver` class: gives your code the newest frame as a numpy array |

## First time
```bash
cd streaming
./install.sh
```

## Every session
```bash
./network_setup.sh        # once per venue / after reconnecting
./start_pi_sender.sh
python3 receiver.py       # or use FrameReceiver in your own code
```

## Before leaving the venue
```bash
./stop_pi_sender.sh
./network_reset.sh
```
