"""Buzzer on the controller board."""
from __future__ import annotations

from ..hardware import reader


def beep(freq_hz: int = 1900, ms: int = 100, repeat: int = 1) -> dict:
    freq_hz = max(100, min(10000, int(freq_hz)))
    on_s = max(10, min(3000, int(ms))) / 1000
    repeat = max(1, min(10, int(repeat)))
    reader().buzzer(freq_hz, on_s, on_s, repeat)
    return {"beeped": repeat, "freq_hz": freq_hz, "ms": int(on_s * 1000)}
