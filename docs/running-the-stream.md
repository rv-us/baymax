# Running the TonyPi → GX10 Video Stream

Everything runs from the **GX10**. The scripts control the Pi over SSH (host alias `tonypi`), so you never need to log into the Pi. For first-time network setup and troubleshooting, see [streaming-setup.md](streaming-setup.md).

## Get the code onto the GX10 (once)

```bash
cd ~/baymax && git pull                                  # if ~/baymax is already a clone of the repo
# or, if it isn't:
git clone https://github.com/rv-us/baymax.git ~/baymax
```

Use `~/baymax/streaming`. Older copies (such as `~/robot-stream` or files from a zip) don't have the latest fixes.

## First time only

```bash
cd ~/baymax/streaming
./install.sh       # installs packages (asks for your sudo password) and checks GStreamer, OpenCV, and SSH to the Pi
```

## Every session

```bash
cd ~/baymax/streaming
./network_setup.sh      # only at a new venue, or after the Wi-Fi reconnects
./start_pi_sender.sh    # starts the camera stream on the Pi, aimed at the GX10's current IP
python3 receiver.py     # prints fps; add --show if a monitor is plugged into the GX10
```

- `network_setup.sh` turns off Wi-Fi power saving and pins the GX10 to the Pi's access point. Once it's applied at a venue, it stays applied until you run `network_reset.sh`.
- `start_pi_sender.sh` stops any sender already running on the Pi first, so there is only ever one.
- The sender also writes 5 fps JPEG snapshots to `/dev/shm/baymax_cam.jpg` on the Pi for the robot API's camera endpoint (`/sensors/camera.jpg`). Only one program can open the camera, so start the stream with this script, not a hand-typed `gst-launch` command, or the robot API loses its camera.
- You can start the sender and the receiver in either order.

## Using the frames in your own code

```python
from streaming.receiver import FrameReceiver   # run from ~/baymax; or from inside streaming/: from receiver import FrameReceiver

rx = FrameReceiver().start()
frame, ts = rx.latest()        # newest 640x480 BGR frame (None until the first one arrives)
```

Only one program can receive the stream at a time, so stop `receiver.py` before running your own code.

## When you're done

```bash
./stop_pi_sender.sh
./network_reset.sh      # only when leaving the venue; removes the access-point pin
```

## If the frame rate dips

The camera tops out at about 22 fps. Brief dips below that usually mean a weak GX10 Wi-Fi signal (check it with `iw dev wlP9s9 link`; better than about −65 dBm is good). Move the GX10 closer to the access point, or use the Ethernet fallback in [streaming-setup.md](streaming-setup.md#fallback-direct-ethernet-most-reliable).
