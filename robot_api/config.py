"""Robot constants: wiring, limits, and environment overrides."""
import os

# BAYMAX_DRY_RUN=1 swaps every hardware driver for an in-memory fake, so the
# whole API (and the agent) can run off the robot or without moving it.
DRY_RUN = os.environ.get("BAYMAX_DRY_RUN") == "1"

ACTION_DIR = "/home/pi/TonyPi/ActionGroups"
AUDIO_DIR = "/home/pi/TonyPi/Functions/voice_interaction/audio"

# Servo safety. Servos cut out at 85 C; holding the stand pose for a long time
# drove leg servos past 80 C, so refuse new motion well below that.
MAX_SERVO_TEMP_C = int(os.environ.get("BAYMAX_MAX_TEMP", "70"))
TEMP_CHECK_TTL_S = 5.0  # reuse a temperature check for this long
MIN_MOVE_MS = 300
MS_PER_PULSE = 1.0  # minimum move time grows with distance: 1000 pulses >= 1 s
MAX_REPEATS = 5  # walking steps / action-group repeats per call

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

# HTTP server / client.
API_HOST = os.environ.get("BAYMAX_API_HOST", "0.0.0.0")
API_PORT = int(os.environ.get("BAYMAX_API_PORT", "8000"))
API_TOKEN = os.environ.get("BAYMAX_API_TOKEN") or None
