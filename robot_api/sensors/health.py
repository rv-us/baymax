"""Battery, servo temperatures and Raspberry Pi health."""
from __future__ import annotations

import subprocess

from .. import config
from ..hardware import read_temps, reader
from ..servo.joints import joint_name

# Docs: battery range 9-12.6 V, recharge below 10 V.
BATTERY_FULL_MV, BATTERY_LOW_MV = 12600, 10000


def _vcgencmd(*args) -> str | None:
    if config.DRY_RUN:
        return None
    try:
        return subprocess.run(["vcgencmd", *args], capture_output=True, text=True,
                              timeout=2).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def battery() -> dict:
    mv = reader().battery_mv()
    if mv is None:
        return {"battery_v": None, "note": "controller did not report the battery"}
    pct = (mv - 9000) / (BATTERY_FULL_MV - 9000) * 100
    return {"battery_v": round(mv / 1000, 2),
            "approx_pct": max(0, min(100, round(pct))),
            "needs_charge": mv < BATTERY_LOW_MV}


def servo_temps() -> dict:
    temps = read_temps()
    known = {i: t for i, t in temps.items() if t is not None}
    hottest = max(known, key=known.get) if known else None
    return {
        "temps_c": {joint_name(i): t for i, t in temps.items()},
        "hottest": {"joint": joint_name(hottest), "temp_c": known[hottest]} if hottest else None,
        "max_allowed_c": config.MAX_SERVO_TEMP_C,
        "cutoff_c": 85,
    }


def report() -> dict:
    """Battery, servo temperatures and Pi CPU temperature/throttling."""
    cpu = _vcgencmd("measure_temp")  # temp=51.0'C
    throttled = _vcgencmd("get_throttled")  # throttled=0x0
    return {
        **battery(),
        **servo_temps(),
        "pi_cpu_c": float(cpu.split("=")[1].split("'")[0]) if cpu else None,
        "pi_throttled": throttled.split("=")[1] if throttled else None,
    }
