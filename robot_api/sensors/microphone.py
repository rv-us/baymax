"""Microphone loudness (USB audio card)."""
from __future__ import annotations

import array
import math
import subprocess

from .. import config
from ..errors import RobotError

RATE, CHANNELS = 16000, 2


def _dbfs(rms: float) -> float:
    return round(20 * math.log10(rms / 32768), 1) if rms else -120.0


def level(seconds: float = 2.0) -> dict:
    """Record for `seconds` and report loudness: average and loudest 0.25 s
    window in dBFS (0 = clipping, about -60 = quiet room)."""
    seconds = max(0.5, min(10.0, float(seconds)))
    if config.DRY_RUN:
        return {"avg_dbfs": -55.0, "loudest_dbfs": -40.0, "peak": 400, "clipped": False,
                "seconds": seconds}
    out = subprocess.run(
        ["arecord", "-D", config.AUDIO_PCM, "-f", "S16_LE", "-r", str(RATE),
         "-c", str(CHANNELS), "-d", str(math.ceil(seconds)), "-t", "raw", "-q"],
        capture_output=True, timeout=seconds + 10)
    samples = array.array("h", out.stdout)
    if not samples:
        raise RobotError("microphone recorded nothing: "
                         + out.stderr.decode(errors="replace").strip())
    window = RATE * CHANNELS // 4
    windows = [samples[i:i + window] for i in range(0, len(samples), window)]
    rms = [math.sqrt(sum(x * x for x in w) / len(w)) for w in windows if len(w)]
    total = math.sqrt(sum(x * x for x in samples) / len(samples))
    peak = max(abs(x) for x in samples)
    return {"avg_dbfs": _dbfs(total), "loudest_dbfs": _dbfs(max(rms)), "peak": peak,
            "clipped": peak >= 32767, "seconds": seconds}
