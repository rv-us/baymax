"""16x8 LED dot matrix (TM1640). Each display byte is one column, bit 0 = top."""
from __future__ import annotations

import threading
import time

from .. import config
from ..errors import RobotError

WIDTH, HEIGHT = 16, 8

# Classic 5x7 font, column bytes, bit 0 = top row. Lowercase maps to upper.
FONT = {
    " ": (0x00, 0x00, 0x00, 0x00, 0x00), "!": (0x00, 0x00, 0x5F, 0x00, 0x00),
    "'": (0x00, 0x05, 0x03, 0x00, 0x00), "(": (0x00, 0x1C, 0x22, 0x41, 0x00),
    ")": (0x00, 0x41, 0x22, 0x1C, 0x00), "+": (0x08, 0x08, 0x3E, 0x08, 0x08),
    ",": (0x00, 0x50, 0x30, 0x00, 0x00), "-": (0x08, 0x08, 0x08, 0x08, 0x08),
    ".": (0x00, 0x60, 0x60, 0x00, 0x00), "/": (0x20, 0x10, 0x08, 0x04, 0x02),
    "0": (0x3E, 0x51, 0x49, 0x45, 0x3E), "1": (0x00, 0x42, 0x7F, 0x40, 0x00),
    "2": (0x42, 0x61, 0x51, 0x49, 0x46), "3": (0x21, 0x41, 0x45, 0x4B, 0x31),
    "4": (0x18, 0x14, 0x12, 0x7F, 0x10), "5": (0x27, 0x45, 0x45, 0x45, 0x39),
    "6": (0x3C, 0x4A, 0x49, 0x49, 0x30), "7": (0x01, 0x71, 0x09, 0x05, 0x03),
    "8": (0x36, 0x49, 0x49, 0x49, 0x36), "9": (0x06, 0x49, 0x49, 0x29, 0x1E),
    ":": (0x00, 0x36, 0x36, 0x00, 0x00), "<": (0x08, 0x14, 0x22, 0x41, 0x00),
    "=": (0x14, 0x14, 0x14, 0x14, 0x14), ">": (0x00, 0x41, 0x22, 0x14, 0x08),
    "?": (0x02, 0x01, 0x51, 0x09, 0x06),
    "A": (0x7E, 0x11, 0x11, 0x11, 0x7E), "B": (0x7F, 0x49, 0x49, 0x49, 0x36),
    "C": (0x3E, 0x41, 0x41, 0x41, 0x22), "D": (0x7F, 0x41, 0x41, 0x22, 0x1C),
    "E": (0x7F, 0x49, 0x49, 0x49, 0x41), "F": (0x7F, 0x09, 0x09, 0x01, 0x01),
    "G": (0x3E, 0x41, 0x41, 0x51, 0x32), "H": (0x7F, 0x08, 0x08, 0x08, 0x7F),
    "I": (0x00, 0x41, 0x7F, 0x41, 0x00), "J": (0x20, 0x40, 0x41, 0x3F, 0x01),
    "K": (0x7F, 0x08, 0x14, 0x22, 0x41), "L": (0x7F, 0x40, 0x40, 0x40, 0x40),
    "M": (0x7F, 0x02, 0x04, 0x02, 0x7F), "N": (0x7F, 0x04, 0x08, 0x10, 0x7F),
    "O": (0x3E, 0x41, 0x41, 0x41, 0x3E), "P": (0x7F, 0x09, 0x09, 0x09, 0x06),
    "Q": (0x3E, 0x41, 0x51, 0x21, 0x5E), "R": (0x7F, 0x09, 0x19, 0x29, 0x46),
    "S": (0x46, 0x49, 0x49, 0x49, 0x31), "T": (0x01, 0x01, 0x7F, 0x01, 0x01),
    "U": (0x3F, 0x40, 0x40, 0x40, 0x3F), "V": (0x1F, 0x20, 0x40, 0x20, 0x1F),
    "W": (0x7F, 0x20, 0x18, 0x20, 0x7F), "X": (0x63, 0x14, 0x08, 0x14, 0x63),
    "Y": (0x03, 0x04, 0x78, 0x04, 0x03), "Z": (0x61, 0x51, 0x49, 0x45, 0x43),
}

# 8x8 icons as rows (top first), drawn centered.
ICONS = {
    "heart": ["01100110", "11111111", "11111111", "11111111",
              "01111110", "00111100", "00011000", "00000000"],
    "smile": ["00111100", "01000010", "10100101", "10000001",
              "10100101", "10011001", "01000010", "00111100"],
    "sad":   ["00111100", "01000010", "10100101", "10000001",
              "10011001", "10100101", "01000010", "00111100"],
    "check": ["00000000", "00000001", "00000011", "10000110",
              "11001100", "01111000", "00110000", "00000000"],
    "cross": ["10000001", "01000010", "00100100", "00011000",
              "00011000", "00100100", "01000010", "10000001"],
    "up":    ["00011000", "00111100", "01111110", "11011011",
              "00011000", "00011000", "00011000", "00011000"],
    "down":  ["00011000", "00011000", "00011000", "00011000",
              "11011011", "01111110", "00111100", "00011000"],
    "left":  ["00010000", "00110000", "01100000", "11111111",
              "11111111", "01100000", "00110000", "00010000"],
    "right": ["00001000", "00001100", "00000110", "11111111",
              "11111111", "00000110", "00001100", "00001000"],
    "dots":  ["00000000", "00000000", "00000000", "11011011",
              "11011011", "00000000", "00000000", "00000000"],
    "question": ["00111100", "01100110", "00000110", "00001100",
                 "00011000", "00011000", "00000000", "00011000"],
}

_lock = threading.Lock()
_dev = None
_scroll_stop = threading.Event()
_scroll_thread: threading.Thread | None = None


def _device():
    global _dev
    if _dev is None:
        from hiwonder.dot_matrix_sensor import TM1640

        _dev = TM1640(dio=config.DOT_MATRIX_DIO, clk=config.DOT_MATRIX_CLK)
    return _dev


def _rotate_180(cols: list) -> list:
    """Reverse column order and flip each column's bits (top <-> bottom)."""
    return [int(f"{c:08b}"[::-1], 2) for c in reversed(cols)]


def _write(columns: list) -> None:
    cols = (list(columns) + [0] * WIDTH)[:WIDTH]
    if config.DOT_MATRIX_ROTATE_180:
        cols = _rotate_180(cols)
    if config.DRY_RUN:
        from ..fake import STATE

        STATE.log.append(("dot_matrix", cols))
        return
    with _lock:
        dev = _device()
        dev.display_buf = cols
        dev.update_display()


def _render(text: str) -> list:
    cols = []
    for ch in text.upper():
        cols.extend(FONT.get(ch, FONT["?"]))
        cols.append(0)
    return cols[:-1] if cols else cols


def _cancel_scroll() -> None:
    global _scroll_thread
    if _scroll_thread and _scroll_thread.is_alive():
        _scroll_stop.set()
        _scroll_thread.join(timeout=2)
    _scroll_stop.clear()
    _scroll_thread = None


def show_text(text: str, scroll_ms: int = 80, loops: int = 1) -> dict:
    """Show text. Up to 2 characters fit; longer text scrolls `loops` times in
    the background (then stays on the last frame)."""
    global _scroll_thread
    text = str(text)[:60]
    _cancel_scroll()
    cols = _render(text)
    if len(cols) <= WIDTH:
        pad = (WIDTH - len(cols)) // 2
        _write([0] * pad + cols)
        return {"showing": text, "scrolling": False}
    frames = [0] * WIDTH + cols + [0] * WIDTH
    loops = max(1, min(10, int(loops)))
    delay = max(20, int(scroll_ms)) / 1000

    def run():
        for _ in range(loops):
            for start in range(len(frames) - WIDTH + 1):
                if _scroll_stop.is_set():
                    return
                _write(frames[start:start + WIDTH])
                time.sleep(delay)

    _scroll_thread = threading.Thread(target=run, daemon=True)
    _scroll_thread.start()
    return {"showing": text, "scrolling": True,
            "seconds": round(loops * (len(frames) - WIDTH + 1) * delay, 1)}


def show_icon(name: str) -> dict:
    """Show a built-in 8x8 icon in the middle of the display."""
    if name not in ICONS:
        raise RobotError(f"unknown icon {name!r}; choose from {sorted(ICONS)}")
    return show_rows(ICONS[name])


def show_rows(rows: list) -> dict:
    """Draw a bitmap: up to 8 strings (top row first) of '0'/'1', up to 16
    wide. Narrower bitmaps are centered."""
    rows = [str(r) for r in rows][:HEIGHT]
    width = min(WIDTH, max((len(r) for r in rows), default=0))
    if any(set(r) - {"0", "1"} for r in rows):
        raise RobotError("rows must contain only '0' and '1'")
    cols = [sum(1 << y for y, r in enumerate(rows) if x < len(r) and r[x] == "1")
            for x in range(width)]
    _cancel_scroll()
    _write([0] * ((WIDTH - width) // 2) + cols)
    return {"drawn": f"{width}x{len(rows)}"}


def clear() -> dict:
    _cancel_scroll()
    _write([0] * WIDTH)
    return {"cleared": True}
