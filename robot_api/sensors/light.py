"""Light sensor (digital; threshold set by its potentiometer)."""
from __future__ import annotations

import threading

from .. import config
from ..errors import RobotError

_lock = threading.Lock()
_line = None


def _get_line():
    global _line
    if _line is None:
        import gpiod  # v1 API, as in the factory examples

        chip = gpiod.Chip(config.GPIO_CHIP)
        line = chip.get_line(config.LIGHT_SENSOR_LINE)
        line.request(consumer="baymax_light", type=gpiod.LINE_REQ_DIR_IN,
                     flags=gpiod.LINE_REQ_FLAG_BIAS_PULL_UP)
        _line = line
    return _line


def is_bright() -> dict:
    """True when the light level is above the sensor's threshold."""
    if config.DRY_RUN:
        from ..fake import STATE

        return {"bright": STATE.bright}
    try:
        with _lock:
            value = _get_line().get_value()
    except OSError as e:
        raise RobotError(f"light sensor unavailable ({e})") from e
    return {"bright": value == 0}  # output goes LOW when bright
