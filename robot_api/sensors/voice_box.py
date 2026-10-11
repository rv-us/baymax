"""Voice box: speech out (espeak-ng / wav files) through the USB speaker, and
wake/command words from the WonderEcho Pro module over serial."""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time

from .. import config
from ..errors import RobotError

_speak_lock = threading.Lock()

# WonderEcho Pro 5-byte frames (from docs ch.6 voice_control_move.py). The
# module must hear its wake word ("hello hiwonder") before command words.
COMMANDS = {
    b"\xaa\x55\x03\x00\xfb": "wakeup",
    b"\xaa\x55\x02\x00\xfb": "sleep",
    b"\xaa\x55\x00\x01\xfb": "forward",
    b"\xaa\x55\x00\x02\xfb": "back",
    b"\xaa\x55\x00\x03\xfb": "turn_left",
    b"\xaa\x55\x00\x04\xfb": "turn_right",
}


def _play_wav_stream(cmd: list, timeout: float) -> None:
    aplay = ["aplay", "-q", "-D", config.SPEAKER_PCM]
    with _speak_lock:
        src = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        try:
            out = subprocess.run(aplay, stdin=src.stdout, capture_output=True, timeout=timeout)
        finally:
            src.stdout.close()
            src.wait(timeout=5)
    if out.returncode != 0:
        raise RobotError(f"audio playback failed: {out.stderr.decode(errors='replace').strip()}")


def say(text: str, speed: int = 150, voice: str = "en-us") -> dict:
    """Speak text out loud (offline TTS). Blocks until finished."""
    text = str(text).strip()[:500]
    if not text:
        raise RobotError("nothing to say")
    speed = max(80, min(300, int(speed)))
    if config.DRY_RUN:
        from ..fake import STATE

        STATE.log.append(("say", text))
        return {"said": text}
    if not shutil.which("espeak-ng"):
        raise RobotError("espeak-ng is not installed (sudo apt install espeak-ng)")
    _play_wav_stream(["espeak-ng", "-v", voice, "-s", str(speed), "--stdout", text],
                     timeout=60)
    return {"said": text}


def list_sounds() -> dict:
    if config.DRY_RUN or not os.path.isdir(config.AUDIO_DIR):
        return {"sounds": []}
    return {"sounds": sorted(f[:-4] for f in os.listdir(config.AUDIO_DIR) if f.endswith(".wav"))}


def play_sound(name: str) -> dict:
    """Play one of the robot's built-in wav files (see list_sounds)."""
    path = os.path.join(config.AUDIO_DIR, os.path.basename(name) + ".wav")
    if config.DRY_RUN:
        return {"played": name}
    if not os.path.isfile(path):
        raise RobotError(f"no sound named {name!r}; see list_sounds")
    _play_wav_stream(["cat", path], timeout=60)
    return {"played": name}


def set_volume(percent: int) -> dict:
    """Speaker volume 0-100%."""
    percent = max(0, min(100, int(percent)))
    if not config.DRY_RUN:
        out = subprocess.run(["amixer", "-q", "-c", config.AUDIO_CARD, "sset", "Speaker",
                              f"{percent}%"], capture_output=True, text=True)
        if out.returncode != 0:
            raise RobotError(f"could not set volume: {out.stderr.strip()}")
    return {"volume_pct": percent}


def listen_command(timeout_s: float = 10) -> dict:
    """Wait for a WonderEcho wake word or command word. Returns the first
    command heard (wake/sleep events are listed too), or none on timeout."""
    timeout_s = max(1.0, min(60.0, float(timeout_s)))
    if config.DRY_RUN:
        return {"command": None, "events": [], "timed_out": True}
    import serial

    try:
        port = serial.Serial(config.WONDERECHO_PORT, 115200, timeout=0.05)
    except serial.SerialException as e:
        raise RobotError(f"voice box not available ({e})") from e
    events, buf = [], b""
    deadline = time.monotonic() + timeout_s
    try:
        port.reset_input_buffer()
        while time.monotonic() < deadline:
            buf += port.read(16)
            while len(buf) >= 5:
                start = buf.find(b"\xaa\x55")
                if start < 0:
                    buf = buf[-1:]
                    break
                frame, buf = buf[start:start + 5], buf[start + 5:]
                if len(frame) < 5:
                    buf = frame + buf
                    break
                name = COMMANDS.get(frame, frame.hex())
                events.append(name)
                if name not in ("wakeup", "sleep"):
                    return {"command": name, "events": events, "timed_out": False}
    finally:
        port.close()
    return {"command": None, "events": events, "timed_out": True}
