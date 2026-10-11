"""Robot constants: wiring, limits, and environment overrides."""
import os

# BAYMAX_DRY_RUN=1 swaps every hardware driver for an in-memory fake, so the
# whole API (and the agent) can run off the robot or without moving it.
DRY_RUN = os.environ.get("BAYMAX_DRY_RUN") == "1"

ACTION_DIR = "/home/pi/TonyPi/ActionGroups"
AUDIO_DIR = "/home/pi/TonyPi/Functions/voice_interaction/audio"

# Servo safety. Servos cut out at 85 C. They report whole degrees, so 82.5
# refuses new motion from 83 C, leaving 2 C before the cutoff. The refusal is off
# unless BAYMAX_TEMP_CHECK=1; temperatures are still reported either way.
TEMP_CHECK = os.environ.get("BAYMAX_TEMP_CHECK") == "1"
MAX_SERVO_TEMP_C = float(os.environ.get("BAYMAX_MAX_TEMP", "82.5"))
TEMP_CHECK_TTL_S = 5.0  # reuse a temperature check for this long
MIN_MOVE_MS = 300
MS_PER_PULSE = 1.0  # minimum move time grows with distance: 1000 pulses >= 1 s
MAX_REPEATS = 5  # walking steps / action-group repeats per call

# Camera controls (v4l2-ctl -c) applied while an MJPEG stream runs, restored after. Auto exposure
# drops the head camera to ~6 fps indoors, so the stream uses a fixed exposure (units of 100 us)
# with the camera's default brightness (0) and gamma (67). 12 ms suits a normally lit room (mean
# ~140/255, almost nothing clipped); in a dim room use e.g.
# BAYMAX_STREAM_CAMERA=auto_exposure=1,exposure_time_absolute=250,brightness=30,gamma=150.
# Empty = leave the camera alone.
CAMERA_DEVICE = "/dev/video0"
STREAM_CAMERA_CONTROLS = os.environ.get(
    "BAYMAX_STREAM_CAMERA", "auto_exposure=1,exposure_time_absolute=120,brightness=0,gamma=67")

# H.264 over UDP (sensors/camera_udp.py), the format streaming/receiver.py on the GX10 reads.
# While it runs, JPEG snapshots go to /dev/shm at UDP_SNAPSHOT_FPS for the API's other camera
# users (camera.jpg, the MJPEG stream, agents); 15 keeps those usable without loading the Pi.
UDP_VIDEO_WIDTH, UDP_VIDEO_HEIGHT, UDP_VIDEO_FPS = 640, 480, 30
UDP_VIDEO_KEYFRAME = 15
UDP_SNAPSHOT_FPS = int(os.environ.get("BAYMAX_UDP_SNAPSHOT_FPS", "15"))

# Expansion sensors (Pi 5 header is gpiochip4).
GPIO_CHIP = "gpiochip4"
DOT_MATRIX_DIO, DOT_MATRIX_CLK = 22, 24  # verified on this robot (docs ch.10.5 example uses 7, 8)
DOT_MATRIX_ROTATE_180 = True  # mounted upside down on this robot (verified)
LIGHT_SENSOR_LINE = 7  # tentative: lines 7 and 8 both reacted once; docs example uses 24 (matrix clk here)
I2C_BUS = 1
AHT10_ADDR = 0x38  # temperature/humidity; the ultrasonic sensor is 0x77 (fixed in hiwonder.Sonar)

# Voice box (WonderEcho Pro): CH340 serial for wake/command words.
WONDERECHO_PORT = "/dev/ttyUSB0"
# USB audio card (speaker + microphone); ALSA card name "Device".
AUDIO_CARD = "Device"
AUDIO_PCM = f"plughw:CARD={AUDIO_CARD},DEV=0"
# Recording device. Set BAYMAX_MIC_PCM=mic_shared (made by streaming/setup_pi_audio.sh)
# so mic_level can share the mic with the audio stream to the GX10.
MIC_PCM = os.environ.get("BAYMAX_MIC_PCM", AUDIO_PCM)
# Playback device. If PipeWire runs on the Pi it holds the card, and direct plughw
# playback stalls about 30 s on every other call; BAYMAX_SPEAKER_PCM=pulse plays
# through PipeWire instead.
SPEAKER_PCM = os.environ.get("BAYMAX_SPEAKER_PCM", AUDIO_PCM)

# HTTP server / client.
API_HOST = os.environ.get("BAYMAX_API_HOST", "0.0.0.0")
API_PORT = int(os.environ.get("BAYMAX_API_PORT", "8000"))
API_TOKEN = os.environ.get("BAYMAX_API_TOKEN") or None
