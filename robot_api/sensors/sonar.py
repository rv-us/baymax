"""Ultrasonic distance sensor (I2C 0x77) and its two RGB 'eye' LEDs."""
from __future__ import annotations

import statistics
import threading
import time

from .. import config
from ..errors import RobotError

_lock = threading.Lock()
_sonar = None
MAX_RANGE_MM = 5000  # the SDK caps readings here


def _dev():
    global _sonar
    if _sonar is None:
        import hiwonder.Sonar as Sonar

        _sonar = Sonar.Sonar()
    return _sonar


def distance(samples: int = 5) -> dict:
    """Distance to the nearest object in front, median of `samples` readings."""
    samples = max(1, min(20, int(samples)))
    if config.DRY_RUN:
        from ..fake import STATE

        return {"distance_mm": STATE.distance_mm, "samples": samples}
    readings = []
    with _lock:
        for _ in range(samples):
            d = _dev().getDistance()
            if d != 99999:  # SDK's "sensor not connected / bad read" value
                readings.append(d)
            time.sleep(0.02)
    if not readings:
        raise RobotError("ultrasonic sensor did not answer (check the I2C cable)")
    mm = int(statistics.median(readings))
    return {"distance_mm": mm, "samples": len(readings),
            **({"note": "at or beyond max range"} if mm >= MAX_RANGE_MM else {})}


def set_eyes(r: int, g: int, b: int, which: str = "both") -> dict:
    """Set the sensor's RGB LEDs (0-255 each). which: both | 0 | 1."""
    rgb = tuple(max(0, min(255, int(v))) for v in (r, g, b))
    idx = {"both": (0, 1), "0": (0,), "1": (1,)}.get(str(which).lower())
    if idx is None:
        raise RobotError("which must be both, 0 or 1")
    if config.DRY_RUN:
        from ..fake import STATE

        STATE.log.append(("sonar_rgb", idx, rgb))
    else:
        with _lock:
            s = _dev()
            s.setRGBMode(0)  # solid colour
            for i in idx:
                s.setRGB(i, rgb)
    return {"eyes": list(idx), "rgb": rgb}
