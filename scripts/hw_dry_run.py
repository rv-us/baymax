#!/usr/bin/python3
"""Detect TonyPi peripherals, then dry-run a short test on each connected one.

Checks (docs ch.6 / ch.10 / ch.12):
  voice_box   WonderEcho Pro USB serial (+ USB audio when present)
  microphone  USB capture device (arecord)
  speaker     USB playback device (aplay)
  ultrasound  I2C Sonar @ 0x77
  light       GPIO light sensor (default BCM 24; not auto-detectable)
  temperature AHT10 I2C @ 0x38
  camera      /dev/video* via OpenCV
  matrix      TM1640 GPIO (default dio=7, clk=8; not auto-detectable)

GPIO modules cannot be probed for presence; pass --gpio to include them.
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

I2C_BUS = 1
ADDR_AHT10 = 0x38
ADDR_SONAR = 0x77
LIGHT_GPIO = 24
MATRIX_DIO = 7
MATRIX_CLK = 8
SAMPLE_WAV = "/home/pi/TonyPi/Functions/voice_interaction/audio/ready.wav"


@dataclass
class Device:
    name: str
    connected: bool
    detail: str = ""
    test: Optional[Callable[[], str]] = field(default=None, repr=False)


def _run(cmd: List[str], timeout: float = 10) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False
    )


def i2c_present(addr: int, bus: int = I2C_BUS) -> bool:
    try:
        from smbus2 import SMBus

        with SMBus(bus) as b:
            b.read_byte(addr)
        return True
    except Exception:
        return False


def find_usb_serial_ports() -> List[str]:
    ports = []
    for path in sorted(glob.glob("/dev/ttyUSB*") + glob.glob("/dev/ttyCH341*")):
        ports.append(path)
    by_id = sorted(glob.glob("/dev/serial/by-id/*"))
    # Prefer by-id names in detail, but open via /dev/ttyUSB*
    return ports or [os.path.realpath(p) for p in by_id]


def parse_alsa_cards(listing: str) -> List[tuple]:
    """Return [(card_index, name), ...] from arecord/aplay -l output."""
    cards = []
    for m in re.finditer(r"card\s+(\d+):\s+(\S+)\s+\[([^\]]+)\]", listing):
        cards.append((int(m.group(1)), m.group(3)))
    return cards


def usb_audio_cards(kind: str) -> List[tuple]:
    cmd = ["arecord", "-l"] if kind == "capture" else ["aplay", "-l"]
    try:
        out = _run(cmd, timeout=5).stdout or ""
    except Exception as e:
        return []
    return [
        (idx, name)
        for idx, name in parse_alsa_cards(out)
        if "usb" in name.lower() or "pnp" in name.lower()
    ]


def find_camera_nodes() -> List[str]:
    # Prefer low-numbered V4L2 capture nodes (TonyPi USB cam is usually video0)
    nodes = []
    for path in sorted(glob.glob("/dev/video*"), key=lambda p: int(re.sub(r"\D", "", p) or "999")):
        # Skip platform ISP metadata nodes by trying open briefly later; list first few
        nodes.append(path)
    return nodes[:8]


# --- tests -----------------------------------------------------------------

def test_voice_box(port: str) -> str:
    try:
        # speech package prints a banner on import
        import contextlib
        import io

        with contextlib.redirect_stdout(io.StringIO()):
            from speech.awake import WonderEchoPro
    except ImportError:
        # Fallback: serial open only
        import serial

        ser = serial.Serial(port, 115200, timeout=0.05)
        ser.close()
        return f"serial open ok on {port} (speech module missing)"

    kws = WonderEchoPro(port)
    try:
        kws.start()
        # Drain any pending bytes; do not wait for wake word
        _ = kws.detect()
        return f"WonderEcho serial ok on {port}"
    finally:
        kws.exit()


def test_microphone(card: int) -> str:
    device = f"plughw:{card},0"
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        path = f.name
    try:
        # 1s mono/stereo capture @ 16 kHz (docs ch.6 / ch.12)
        cp = _run(
            [
                "arecord",
                "-D",
                device,
                "-f",
                "S16_LE",
                "-r",
                "16000",
                "-c",
                "2",
                "-d",
                "1",
                "-q",
                path,
            ],
            timeout=8,
        )
        size = os.path.getsize(path) if os.path.exists(path) else 0
        if cp.returncode != 0 or size < 1000:
            err = (cp.stderr or cp.stdout or "").strip()
            raise RuntimeError(err or f"record failed (bytes={size})")
        return f"recorded {size} bytes via {device}"
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def test_speaker(card: int) -> str:
    device = f"plughw:{card},0"
    if os.path.isfile(SAMPLE_WAV):
        src = SAMPLE_WAV
    else:
        # Tiny silent wav if sample missing
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            src = f.name
        _run(
            [
                "arecord",
                "-D",
                "null",
                "-f",
                "S16_LE",
                "-r",
                "16000",
                "-c",
                "1",
                "-d",
                "1",
                "-q",
                src,
            ],
            timeout=5,
        )
    cp = _run(["aplay", "-D", device, "-q", src], timeout=15)
    if cp.returncode != 0:
        err = (cp.stderr or cp.stdout or "").strip()
        raise RuntimeError(err or "aplay failed")
    return f"played sample on {device}"


def test_ultrasound() -> str:
    import hiwonder.Sonar as Sonar

    s = Sonar.Sonar()
    s.setRGBMode(0)
    s.setRGB(0, (0, 64, 0))
    s.setRGB(1, (0, 64, 0))
    time.sleep(0.2)
    d = s.getDistance()
    s.setRGB(0, (0, 0, 0))
    s.setRGB(1, (0, 0, 0))
    if d == 99999:
        raise RuntimeError("no distance reading")
    return f"distance={d} mm, RGB blink ok"


def test_light(gpio: int = LIGHT_GPIO) -> str:
    import gpiod

    chip = gpiod.Chip("gpiochip4")
    line = chip.get_line(gpio)
    line.request(
        consumer="hw_dry_run_light",
        type=gpiod.LINE_REQ_DIR_IN,
        flags=gpiod.LINE_REQ_FLAG_BIAS_PULL_UP,
    )
    try:
        samples = [line.get_value() for _ in range(5)]
        time.sleep(0.05)
    finally:
        line.release()
    # 0 = bright (above threshold), 1 = dark — per docs ch.10.6
    label = "bright/low" if samples[-1] == 0 else "dark/high"
    return f"GPIO{gpio} samples={samples} ({label})"


def test_temperature() -> str:
    import smbus

    bus = smbus.SMBus(I2C_BUS)
    try:
        bus.write_i2c_block_data(ADDR_AHT10, 0xAC, [0x33, 0x00])
        time.sleep(0.1)
        data = bus.read_i2c_block_data(ADDR_AHT10, 0x00, 6)
    finally:
        bus.close()
    temp_raw = ((data[3] & 0x0F) << 16) | (data[4] << 8) | data[5]
    hum_raw = ((data[1] << 16) | (data[2] << 8) | data[3]) >> 4
    ctemp = ((temp_raw * 200) / 1048576) - 50
    chum = int(hum_raw * 100 / 1048576)
    return f"{ctemp:.1f} C, {chum}% RH"


def test_camera(node: Optional[str] = None) -> str:
    import cv2

    path = node or "/dev/video0"
    cap = cv2.VideoCapture(path if node else -1)
    try:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        ok, frame = False, None
        for _ in range(10):
            ok, frame = cap.read()
            if ok and frame is not None:
                break
            time.sleep(0.05)
        if not ok or frame is None:
            raise RuntimeError(f"no frame from {path}")
        h, w = frame.shape[:2]
        return f"frame {w}x{h} from {path}"
    finally:
        cap.release()


def test_matrix(dio: int = MATRIX_DIO, clk: int = MATRIX_CLK) -> str:
    from hiwonder import dot_matrix_sensor

    dms = dot_matrix_sensor.TM1640(dio=dio, clk=clk)
    # Brief "Hi" pattern, then clear
    dms.display_buf = (
        0x7F,
        0x08,
        0x7F,
        0x00,
        0x7C,
        0x54,
        0x5C,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
        0x00,
    )
    dms.update_display()
    time.sleep(0.8)
    dms.display_buf = [0] * 16
    dms.update_display()
    return f"TM1640 pattern on dio={dio} clk={clk}"


# --- detect ----------------------------------------------------------------

def detect_all(include_gpio: bool) -> List[Device]:
    devices: List[Device] = []

    serial_ports = find_usb_serial_ports()
    cap_cards = usb_audio_cards("capture")
    play_cards = usb_audio_cards("playback")
    voice_ok = bool(serial_ports)
    detail_vb = serial_ports[0] if serial_ports else "no USB serial"
    if cap_cards or play_cards:
        detail_vb += f"; usb-audio cards cap={cap_cards} play={play_cards}"
    devices.append(
        Device(
            "voice_box",
            voice_ok,
            detail_vb,
            (lambda p=serial_ports[0]: test_voice_box(p)) if voice_ok else None,
        )
    )

    mic_ok = bool(cap_cards)
    devices.append(
        Device(
            "microphone",
            mic_ok,
            f"card {cap_cards[0][0]} [{cap_cards[0][1]}]" if mic_ok else "no USB capture",
            (lambda c=cap_cards[0][0]: test_microphone(c)) if mic_ok else None,
        )
    )

    spk_ok = bool(play_cards)
    devices.append(
        Device(
            "speaker",
            spk_ok,
            f"card {play_cards[0][0]} [{play_cards[0][1]}]" if spk_ok else "no USB playback",
            (lambda c=play_cards[0][0]: test_speaker(c)) if spk_ok else None,
        )
    )

    sonar_ok = i2c_present(ADDR_SONAR)
    devices.append(
        Device(
            "ultrasound",
            sonar_ok,
            f"I2C 0x{ADDR_SONAR:02x}" if sonar_ok else f"I2C 0x{ADDR_SONAR:02x} missing",
            test_ultrasound if sonar_ok else None,
        )
    )

    if include_gpio:
        devices.append(
            Device(
                "light",
                True,
                f"GPIO{LIGHT_GPIO} (assumed; not auto-detectable)",
                lambda: test_light(LIGHT_GPIO),
            )
        )
    else:
        devices.append(
            Device("light", False, "skipped (GPIO; pass --gpio to test)", None)
        )

    temp_ok = i2c_present(ADDR_AHT10)
    devices.append(
        Device(
            "temperature",
            temp_ok,
            f"AHT10 I2C 0x{ADDR_AHT10:02x}" if temp_ok else f"I2C 0x{ADDR_AHT10:02x} missing",
            test_temperature if temp_ok else None,
        )
    )

    cam_nodes = find_camera_nodes()
    cam_ok = bool(cam_nodes)
    devices.append(
        Device(
            "camera",
            cam_ok,
            cam_nodes[0] if cam_ok else "no /dev/video*",
            (lambda n=cam_nodes[0]: test_camera(n)) if cam_ok else None,
        )
    )

    if include_gpio:
        devices.append(
            Device(
                "matrix",
                True,
                f"TM1640 dio={MATRIX_DIO} clk={MATRIX_CLK} (assumed)",
                lambda: test_matrix(MATRIX_DIO, MATRIX_CLK),
            )
        )
    else:
        devices.append(
            Device("matrix", False, "skipped (GPIO; pass --gpio to test)", None)
        )

    return devices


def main() -> int:
    ap = argparse.ArgumentParser(description="TonyPi hardware detect + dry-run")
    ap.add_argument(
        "--detect-only",
        action="store_true",
        help="only print connection status",
    )
    ap.add_argument(
        "--gpio",
        action="store_true",
        help="also dry-run light sensor + dot matrix (GPIO, not auto-detectable)",
    )
    ap.add_argument(
        "only",
        nargs="*",
        help="optional subset: voice_box microphone speaker ultrasound light temperature camera matrix",
    )
    args = ap.parse_args()

    devices = detect_all(include_gpio=args.gpio)
    if args.only:
        wanted = set(args.only)
        devices = [d for d in devices if d.name in wanted]
        unknown = wanted - {d.name for d in devices}
        if unknown:
            print(f"unknown device names: {', '.join(sorted(unknown))}", file=sys.stderr)
            return 2

    print("=== detect ===")
    for d in devices:
        flag = "YES" if d.connected else "no "
        print(f"  [{flag}] {d.name:12}  {d.detail}")

    if args.detect_only:
        return 0

    print("\n=== dry-run ===")
    failures = 0
    for d in devices:
        if not d.connected or d.test is None:
            print(f"  SKIP  {d.name}")
            continue
        try:
            msg = d.test()
            print(f"  OK    {d.name:12}  {msg}")
        except Exception as e:
            failures += 1
            print(f"  FAIL  {d.name:12}  {e}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
