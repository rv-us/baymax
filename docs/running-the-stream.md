# Running the TonyPi → GX10 Video and Audio Stream

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
./install.sh          # installs packages (asks for your sudo password) and checks GStreamer, numpy, and SSH to the Pi
./setup_pi_audio.sh   # makes a shared mic device on the Pi so the audio stream and the robot API can both record
```

## Every session

```bash
cd ~/baymax/streaming
./network_setup.sh      # only at a new venue, or after the Wi-Fi reconnects
./start_pi_sender.sh    # starts the camera and mic streams on the Pi, aimed at the GX10's current IP
python3 receiver.py     # prints fps; add --show if a monitor is plugged into the GX10
python3 audio_receiver.py   # prints mic loudness; talk to the robot and it should jump from about -60 dBFS
```

- `network_setup.sh` turns off Wi-Fi power saving and pins the GX10 to the Pi's access point. Once it's applied at a venue, it stays applied until you run `network_reset.sh`.
- `start_pi_sender.sh` stops any sender already running on the Pi first, so there is only ever one.
- The sender also writes 5 fps JPEG snapshots to `/dev/shm/baymax_cam.jpg` on the Pi for the robot API's camera endpoint (`/sensors/camera.jpg`). Only one program can open the camera, so start the stream with this script, not a hand-typed `gst-launch` command, or the robot API loses its camera.
- You can start the sender and the receiver in either order.

## Video from the robot API instead (no sender script)

The robot API can stream the camera itself: the GX10 asks for it, and the Pi sends H.264 over UDP back to the GX10's address, the same format as above. The request is a lease: repeat it every few seconds, or the Pi stops streaming within `lease_s` (10 s), so a crashed program never leaves the camera running.

```bash
cd ~/baymax/streaming && VIDEO_ENABLED=0 ./start_pi_sender.sh   # microphone only; leaves the camera to the API
cd ~/baymax && BAYMAX_API_URL=http://<robot>:8000 python scripts/pose_server.py --stream   # asks for the video itself
```

In your own code, `UdpVideoLease` (in `streaming/robot_camera.py`) starts and renews it:

```python
from streaming.receiver import FrameReceiver
from streaming.robot_camera import UdpVideoLease

lease = UdpVideoLease("http://<robot>:8000").start()   # token from BAYMAX_API_TOKEN
rx = FrameReceiver(port=lease.port).start()
```

- Only one program at a time gets the UDP video; the API refuses a second target with a 409 until the first stops (`POST /sensors/camera/udp/stop`) or its lease runs out. Status: `GET /sensors/camera/udp/status`.
- While it runs, `/sensors/camera.jpg` and the MJPEG stream (`/sensors/camera.mjpg`) still work, from 15 fps snapshots.
- Don't run `start_pi_sender.sh` without `VIDEO_ENABLED=0` at the same time: it stops every camera pipeline on the Pi.

## Using the frames in your own code

```python
from streaming.receiver import FrameReceiver   # run from ~/baymax; or from inside streaming/: from receiver import FrameReceiver

rx = FrameReceiver().start()
frame, ts = rx.latest()        # newest 640x480 BGR frame (None until the first one arrives)
```

The receiver decodes with `gst-launch-1.0` in a subprocess and only needs numpy, so it works in a venv with NumPy 2 and alongside pip's `opencv-python` (which ultralytics installs). OpenCV is only needed for `--show`.

Only one program can receive the stream at a time, so stop `receiver.py` before running your own code. If it prints `0.0 fps (no frames yet)` while the sender is running, look for a leftover receiver holding the port: `ss -ulpn | grep 5000`.

## Using the audio in your own code

```python
from streaming.audio_receiver import AudioReceiver

rx = AudioReceiver().start()
pcm = rx.latest(2.0)           # the last 2 s as 16 kHz mono 16-bit PCM (bytes)
pcm, t0 = rx.since(t)          # everything that arrived after time.time() value t
```

To hear what the robot hears: `python3 audio_receiver.py --wav test.wav --seconds 5`, then play `test.wav`.

Like the video, only one program can receive the audio at a time. If the robot API's `mic_level` says "Device or resource busy," start the API with `BAYMAX_MIC_PCM=mic_shared`.

## Pose estimation on the stream

`scripts/pose_server.py --stream` reads the robot's video and runs MediaPipe on each frame it can (measured on the GX10: 28 fps from a 30 fps stream, 30 ms per frame, a pose ready 39 ms after its frame arrives). Because only one program can receive the video, other programs should read poses from it instead of opening the stream themselves.

```bash
cd ~/baymax && python scripts/pose_server.py --stream --port 9000   # needs mediapipe in the venv
# add --robot-url http://<robot>:8000 to have it ask the robot API for the video itself
curl localhost:9000/latest     # newest pose: landmarks, joint angles, whether the body is in view
curl localhost:9000/stats      # fps, model time, latency
```

`/latest` includes `angles` (degrees; left/right are the patient's own): `left_shoulder_elevation` (0 = arm down, 90 = forward or sideways, 180 = overhead), `left_elbow` and `left_knee` (180 = straight), the `right_` versions, `trunk_lean`, and `hip_shift_cm` (+ = toward the patient's left). An angle is `null` when the joints it needs aren't visible. `in_view` says whether the `upper_body` and `full_body` are in the frame and lists `missing` parts.

In Python, without the server: `PoseStream(FrameReceiver().start(), Pose()).start()` from `vision/stream.py`. MediaPipe tracks one person, the most prominent in the frame.

## Speech to text (`voice.Listener`)

Turns the mic stream into the patient's answers as text. Silero VAD finds where speech starts and stops, and Whisper (`small.en`) transcribes it on the GX10's GPU in about 50 ms. Everything runs on the GX10; nothing goes to an online API.

Install once, in the venv:

```bash
cd ~/baymax && source .venv/bin/activate
pip install silero-vad transformers   # Whisper's weights (~1 GB) download on first use
```

Check it with the robot's mic (talk to the robot; Ctrl+C to stop):

```bash
python -m voice.listener     # prints each transcript and how long after you stopped talking it arrived
```

In your own code:

```python
from voice import Listener

listener = Listener().start()                    # loads the models, about 3 s
with listener.robot_speaking():                  # wrap every say/play_sound, so the robot doesn't hear itself
    backend.call("sensors/voice/say", text="Are you ready?")
text = listener.listen(timeout_s=10)             # the next answer, or None if nobody spoke

listener.on_transcript(lambda t: print(t.text))  # every transcript, even between listen() calls
```

- `listen()` returns the first utterance that ends after you call it. An answer still being spoken at the timeout is waited for.
- An answer ends after 0.8 s of silence, so text arrives about 0.9 s after the patient stops talking. Most of that is the silence wait (`END_SILENCE_MS` in `voice/segmenter.py`).
- `Listener` starts its own `AudioReceiver`, so stop `audio_receiver.py` first: only one program can receive the audio.

## When you're done

```bash
./stop_pi_sender.sh
./network_reset.sh      # only when leaving the venue; removes the access-point pin
```

## If the frame rate dips

The camera tops out at about 22 fps. Brief dips below that usually mean a weak GX10 Wi-Fi signal (check it with `iw dev wlP9s9 link`; better than about −65 dBm is good). Move the GX10 closer to the access point, or use the Ethernet fallback in [streaming-setup.md](streaming-setup.md#fallback-direct-ethernet-most-reliable).
