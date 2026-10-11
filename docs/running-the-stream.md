# Running the TonyPi → GX10 Video and Audio Stream

Everything runs from the **GX10**. The camera comes from the robot API on request (H.264 over UDP), and the microphone from a sender the scripts start over SSH (host alias `tonypi`), so you never need to log into the Pi. For first-time network setup and troubleshooting, see [streaming-setup.md](streaming-setup.md).

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
../scripts/setup_gx10_venv.sh --models   # Python environment in ~/venvs/baymax (outside the repo), plus the Whisper model
./setup_pi_audio.sh   # makes a shared mic device on the Pi so the audio stream and the robot API can both record
```

## Every session

The robot API streams the camera when the GX10 asks for it, so the Pi only needs its API server running. Tell the GX10 where it is (once per shell, or in `~/.bashrc`):

```bash
export BAYMAX_API_URL=http://<robot>:8000 BAYMAX_API_TOKEN=<token>
```

```bash
cd ~/baymax/streaming
./network_setup.sh      # only at a new venue, or after the Wi-Fi reconnects
./start_pi_sender.sh    # starts the microphone stream on the Pi, aimed at the GX10's current IP
python3 receiver.py     # asks the robot for its camera and prints fps; add --show with a monitor
python3 audio_receiver.py   # prints mic loudness; talk to the robot and it should jump from about -60 dBFS
```

- `network_setup.sh` turns off Wi-Fi power saving and pins the GX10 to the Pi's access point. Once it's applied at a venue, it stays applied until you run `network_reset.sh`.
- Video: `receiver.py` (and every tool below) asks the API for the camera (`POST /sensors/camera/udp`) and the Pi sends H.264 over UDP back to whoever asked. The request is a lease, renewed every few seconds; if the program stops renewing (closed, crashed), the Pi stops streaming within 10 s. Status: `GET /sensors/camera/udp/status`.
- Only one machine gets the UDP video at a time. Tools that find it taken (or that run where GStreamer can't decode H.264, like most laptops) use the API's MJPEG stream instead, automatically.
- While the UDP stream runs, `/sensors/camera.jpg` and the MJPEG stream still work, from 15 fps snapshots.
- `start_pi_sender.sh` stops any microphone sender already running first, so there is only ever one. `VIDEO_ENABLED=1 ./start_pi_sender.sh` also pushes video the old way (without the API); don't mix the two, since it stops every camera pipeline on the Pi.

## Using the frames in your own code

```python
from streaming.robot_camera import RobotCamera   # run from ~/baymax

cam = RobotCamera().start()      # BAYMAX_API_URL / BAYMAX_API_TOKEN; UDP if it can, else MJPEG
frame, ts = cam.latest()         # newest 640x480 BGR frame (None until the first one arrives)
cam.mode                         # "udp" or "mjpeg"
cam.stop()                       # stops the robot's UDP stream too
```

`RobotCamera(mode="udp")` insists on UDP and says why if it can't. Underneath, the UDP path is `UdpVideoLease` (asks for and renews the stream) plus `streaming.receiver.FrameReceiver` (decodes it with `gst-launch-1.0` in a subprocess, needing only numpy, so it works in a venv with NumPy 2 next to pip's `opencv-python`). The MJPEG fallback needs OpenCV and sends the token in a header, never the URL.

Only one program per machine can receive the UDP stream; a second `RobotCamera` on the same GX10 uses MJPEG. If `receiver.py` prints `0.0 fps (no frames yet)`, check `GET /sensors/camera/udp/status` on the robot and look for a leftover receiver holding the port: `ss -ulpn | grep 5000`.

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
cd ~/baymax && python scripts/pose_server.py --stream --port 9000   # needs mediapipe; uses BAYMAX_API_URL
curl localhost:9000/latest     # newest pose: landmarks, joint angles, whether the body is in view
curl localhost:9000/stats      # fps, model time, latency
```

`/latest` includes `angles` (degrees; left/right are the patient's own): `left_shoulder_elevation` (0 = arm down, 90 = forward or sideways, 180 = overhead), `left_elbow` and `left_knee` (180 = straight), the `right_` versions, `trunk_lean`, and `hip_shift_cm` (+ = toward the patient's left). An angle is `null` when the joints it needs aren't visible. `in_view` says whether the `upper_body` and `full_body` are in the frame and lists `missing` parts.

In Python, without the server: `PoseStream(RobotCamera().start(), Pose()).start()` from `vision/stream.py`. MediaPipe tracks one person, the most prominent in the frame.

## Speech to text (`voice.Listener`)

Turns the mic stream into the patient's answers as text. Silero VAD finds where speech starts and stops, and Whisper (`small.en`) transcribes it on the GX10's GPU in about 50 ms. Everything runs on the GX10; nothing goes to an online API.

Install once, in the venv:

```bash
source ~/venvs/baymax/bin/activate
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

## The whole thing in one command

`scripts/start_gx10.sh` starts the pose service (which owns the robot's camera and tracks it), the voice service (which owns its microphone: Whisper on the GPU) and the dashboard (which gets the picture, the pose and what the patient says from those two); `scripts/stop_gx10.sh` stops them. Details: [dashboard/README.md](../dashboard/README.md#live-sessions-on-the-gx10s-tracking). Don't run `receiver.py`, `audio_receiver.py` or `pose_server.py --stream` at the same time: each wants the camera or the microphone for itself.

## When you're done

```bash
./stop_pi_sender.sh
./network_reset.sh      # only when leaving the venue; removes the access-point pin
```

## If the frame rate dips

The camera tops out at about 22 fps. Brief dips below that usually mean a weak GX10 Wi-Fi signal (check it with `iw dev wlP9s9 link`; better than about −65 dBm is good). Move the GX10 closer to the access point, or use the Ethernet fallback in [streaming-setup.md](streaming-setup.md#fallback-direct-ethernet-most-reliable).
