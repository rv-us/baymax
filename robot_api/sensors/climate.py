"""AHT10 temperature/humidity sensor (I2C 0x38)."""
from __future__ import annotations

import threading
import time

from .. import config
from ..errors import RobotError

_lock = threading.Lock()


def read() -> dict:
    """Ambient temperature (C) and relative humidity (%)."""
    if config.DRY_RUN:
        from ..fake import STATE

        t, h = STATE.climate
        return {"temp_c": t, "humidity_pct": h}
    from smbus2 import SMBus

    addr = config.AHT10_ADDR
    try:
        with _lock, SMBus(config.I2C_BUS) as bus:
            if not bus.read_byte(addr) & 0x08:  # not calibrated yet
                bus.write_i2c_block_data(addr, 0xE1, [0x08, 0x00])
                time.sleep(0.05)
            bus.write_i2c_block_data(addr, 0xAC, [0x33, 0x00])  # trigger measurement
            for _ in range(10):
                time.sleep(0.08)
                d = bus.read_i2c_block_data(addr, 0x00, 6)
                if not d[0] & 0x80:  # busy bit clear
                    break
            else:
                raise RobotError("AHT10 stayed busy")
    except OSError as e:
        raise RobotError(f"temperature/humidity sensor did not answer ({e})") from e
    raw_h = ((d[1] << 16) | (d[2] << 8) | d[3]) >> 4
    raw_t = ((d[3] & 0x0F) << 16) | (d[4] << 8) | d[5]
    result = {"temp_c": round(raw_t * 200 / 1048576 - 50, 1),
              "humidity_pct": round(raw_h * 100 / 1048576, 1)}
    if raw_h == 0:
        # This robot's sensor returns exactly 0 humidity on every read (even
        # after a soft reset) while temperature works.
        result.update(humidity_pct=None, note="humidity reading unavailable (sensor reports 0)")
    return result
