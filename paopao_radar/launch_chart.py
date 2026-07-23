from __future__ import annotations

import binascii
import struct
import zlib
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping


CST = timezone(timedelta(hours=8))
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

FONT_5X7 = {
    " ": ("00000",) * 7,
    "-": ("00000", "00000", "00000", "11111", "00000", "00000", "00000"),
    ".": ("00000", "00000", "00000", "00000", "00000", "01100", "01100"),
    ":": ("00000", "01100", "01100", "00000", "01100", "01100", "00000"),
    "/": ("00001", "00010", "00100", "01000", "10000", "00000", "00000"),
    "%": ("11001", "11010", "00100", "01000", "10110", "00110", "00000"),
    "+": ("00000", "00100", "00100", "11111", "00100", "00100", "00000"),
    "<": ("00010", "00100", "01000", "10000", "01000", "00100", "00010"),
    "0": ("01110", "10001", "10011", "10101", "11001", "10001", "01110"),
    "1": ("00100", "01100", "00100", "00100", "00100", "00100", "01110"),
    "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
    "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
    "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
    "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
    "6": ("01110", "10000", "10000", "11110", "10001", "10001", "01110"),
    "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
    "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
    "9": ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
    "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
    "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
    "C": ("01110", "10001", "10000", "10000", "10000", "10001", "01110"),
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
    "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
    "G": ("01110", "10001", "10000", "10111", "10001", "10001", "01110"),
    "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
    "I": ("01110", "00100", "00100", "00100", "00100", "00100", "01110"),
    "J": ("00111", "00010", "00010", "00010", "10010", "10010", "01100"),
    "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
    "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
    "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
    "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
    "O": ("01110", "10001", "10001", "10001", "10001", "10001", "01110"),
    "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
    "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
    "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
    "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
    "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
    "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
    "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
    "W": ("10001", "10001", "10001", "10101", "10101", "10101", "01010"),
    "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
    "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
    "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
}


class Canvas:
    def __init__(self, width: int, height: int, background: tuple[int, int, int]):
        self.width = int(width)
        self.height = int(height)
        self.pixels = bytearray(background * (self.width * self.height))

    def pixel(self, x: int, y: int, color: tuple[int, int, int]) -> None:
        if not (0 <= x < self.width and 0 <= y < self.height):
            return
        offset = (y * self.width + x) * 3
        self.pixels[offset:offset + 3] = bytes(color)

    def line(
        self,
        x0: int,
        y0: int,
        x1: int,
        y1: int,
        color: tuple[int, int, int],
    ) -> None:
        dx = abs(x1 - x0)
        sx = 1 if x0 < x1 else -1
        dy = -abs(y1 - y0)
        sy = 1 if y0 < y1 else -1
        error = dx + dy
        while True:
            self.pixel(x0, y0, color)
            if x0 == x1 and y0 == y1:
                break
            doubled = 2 * error
            if doubled >= dy:
                error += dy
                x0 += sx
            if doubled <= dx:
                error += dx
                y0 += sy

    def rect(
        self,
        x0: int,
        y0: int,
        x1: int,
        y1: int,
        color: tuple[int, int, int],
    ) -> None:
        left, right = sorted((max(0, x0), min(self.width - 1, x1)))
        top, bottom = sorted((max(0, y0), min(self.height - 1, y1)))
        row = bytes(color) * max(0, right - left + 1)
        for y in range(top, bottom + 1):
            offset = (y * self.width + left) * 3
            self.pixels[offset:offset + len(row)] = row

    def text(
        self,
        x: int,
        y: int,
        value: str,
        color: tuple[int, int, int],
        *,
        scale: int = 2,
    ) -> None:
        cursor = int(x)
        safe_scale = max(1, int(scale))
        for char in str(value).upper():
            glyph = FONT_5X7.get(char, FONT_5X7[" "])
            for row_index, row in enumerate(glyph):
                for column_index, enabled in enumerate(row):
                    if enabled != "1":
                        continue
                    self.rect(
                        cursor + column_index * safe_scale,
                        y + row_index * safe_scale,
                        cursor + (column_index + 1) * safe_scale - 1,
                        y + (row_index + 1) * safe_scale - 1,
                        color,
                    )
            cursor += 6 * safe_scale


def _number(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if result == result else 0.0


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(
        ">I",
        binascii.crc32(body) & 0xFFFFFFFF,
    )


def _encode_png(canvas: Canvas) -> bytes:
    scanlines = bytearray()
    stride = canvas.width * 3
    for y in range(canvas.height):
        scanlines.append(0)
        start = y * stride
        scanlines.extend(canvas.pixels[start:start + stride])
    header = struct.pack(">IIBBBBB", canvas.width, canvas.height, 8, 2, 0, 0, 0)
    return b"".join([
        PNG_SIGNATURE,
        _png_chunk(b"IHDR", header),
        _png_chunk(b"IDAT", zlib.compress(bytes(scanlines), level=9)),
        _png_chunk(b"IEND", b""),
    ])


def render_launch_chart_png(
    *,
    symbol: str,
    candles: list[Mapping[str, Any]],
    checkpoints: list[Mapping[str, Any]],
    cycle_no: int,
    width: int = 960,
    height: int = 540,
) -> bytes:
    """Render a deterministic Binance 15m lifecycle chart entirely in memory."""

    normalized = sorted(
        [
            {
                "close_ts": int(_number(item.get("close_ts"))),
                "open": _number(item.get("open")),
                "high": _number(item.get("high")),
                "low": _number(item.get("low")),
                "close": _number(item.get("close")),
                "quote_volume": max(0.0, _number(item.get("quote_volume"))),
            }
            for item in candles
            if isinstance(item, Mapping)
        ],
        key=lambda item: item["close_ts"],
    )
    normalized = [
        item
        for item in normalized
        if item["close_ts"] > 0
        and min(item["open"], item["high"], item["low"], item["close"]) > 0
        and item["high"] >= item["low"]
    ]
    if len(normalized) < 5:
        raise ValueError("at least five valid candles are required")
    if not (480 <= width <= 1600 and 320 <= height <= 1000):
        raise ValueError("unsupported chart dimensions")

    canvas = Canvas(width, height, (9, 12, 16))
    canvas.rect(0, 0, width - 1, 55, (15, 20, 27))
    canvas.text(24, 18, str(symbol or "UNKNOWN"), (232, 237, 243), scale=2)
    canvas.text(210, 18, "15M", (105, 167, 255), scale=2)
    canvas.text(286, 18, f"CYCLE {max(1, int(cycle_no))}", (148, 163, 184), scale=2)
    canvas.text(
        440,
        18,
        f"EVENTS {len(checkpoints)}",
        (148, 163, 184),
        scale=2,
    )

    plot_left = 54
    plot_right = width - 92
    price_top = 76
    price_bottom = height - 130
    volume_top = height - 106
    volume_bottom = height - 50
    grid = (31, 39, 49)
    muted = (116, 129, 148)
    for index in range(6):
        y = price_top + round((price_bottom - price_top) * index / 5)
        canvas.line(plot_left, y, plot_right, y, grid)
    for index in range(7):
        x = plot_left + round((plot_right - plot_left) * index / 6)
        canvas.line(x, price_top, x, volume_bottom, grid)

    lowest = min(item["low"] for item in normalized)
    highest = max(item["high"] for item in normalized)
    span = max(highest - lowest, highest * 0.001)
    lowest -= span * 0.08
    highest += span * 0.08
    span = highest - lowest

    def price_y(value: float) -> int:
        ratio = (highest - value) / span
        return price_top + round(ratio * (price_bottom - price_top))

    candle_count = len(normalized)
    slot = (plot_right - plot_left) / max(1, candle_count)
    body_half = max(1, min(4, int(slot * 0.32)))
    max_volume = max(item["quote_volume"] for item in normalized) or 1.0
    x_positions: list[int] = []
    for index, candle in enumerate(normalized):
        x = plot_left + round((index + 0.5) * slot)
        x_positions.append(x)
        up = candle["close"] >= candle["open"]
        color = (35, 196, 131) if up else (239, 83, 80)
        canvas.line(x, price_y(candle["high"]), x, price_y(candle["low"]), color)
        open_y = price_y(candle["open"])
        close_y = price_y(candle["close"])
        canvas.rect(
            x - body_half,
            min(open_y, close_y),
            x + body_half,
            max(open_y, close_y) or min(open_y, close_y) + 1,
            color,
        )
        volume_height = round(
            candle["quote_volume"] / max_volume * (volume_bottom - volume_top)
        )
        canvas.rect(
            x - body_half,
            volume_bottom - volume_height,
            x + body_half,
            volume_bottom,
            (28, 112, 82) if up else (116, 49, 53),
        )

    event_colors = {
        "primed": (73, 143, 255),
        "breakout": (246, 189, 22),
        "launched": (207, 106, 255),
        "cooling": (148, 163, 184),
        "failed": (255, 92, 92),
    }
    first_close_ts = normalized[0]["close_ts"]
    last_close_ts = normalized[-1]["close_ts"]
    for fallback_no, checkpoint in enumerate(checkpoints, start=1):
        event_no = int(_number(checkpoint.get("checkpoint_no"))) or fallback_no
        event_ts = int(_number(checkpoint.get("window_end_ts")))
        stage = str(checkpoint.get("stage") or "")
        color = event_colors.get(stage, (73, 143, 255))
        clipped = event_ts < first_close_ts
        if event_ts <= first_close_ts:
            index = 0
        elif event_ts >= last_close_ts:
            index = candle_count - 1
        else:
            index = min(
                range(candle_count),
                key=lambda position: abs(normalized[position]["close_ts"] - event_ts),
            )
        x = x_positions[index]
        for y in range(price_top, volume_bottom + 1, 5):
            canvas.line(x, y, x, min(y + 2, volume_bottom), color)
        label = f"E{event_no}{'<' if clipped else ''}"
        label_width = len(label) * 12 + 10
        label_x = min(max(plot_left, x - label_width // 2), plot_right - label_width)
        label_y = price_top + 8 + ((event_no - 1) % 3) * 24
        canvas.rect(label_x, label_y, label_x + label_width, label_y + 19, (20, 27, 36))
        canvas.text(label_x + 5, label_y + 3, label, color, scale=2)

    last = normalized[-1]["close"]
    first = normalized[0]["open"]
    change = (last / first - 1.0) * 100.0 if first > 0 else 0.0
    last_color = (35, 196, 131) if change >= 0 else (239, 83, 80)
    canvas.text(
        width - 265,
        18,
        f"{last:.6G} {change:+.2F}%",
        last_color,
        scale=2,
    )
    for index in range(6):
        value = highest - span * index / 5
        y = price_top + round((price_bottom - price_top) * index / 5) - 5
        canvas.text(plot_right + 10, y, f"{value:.5G}", muted, scale=1)

    first_time = datetime.fromtimestamp(first_close_ts, CST)
    last_time = datetime.fromtimestamp(last_close_ts, CST)
    canvas.text(plot_left, height - 30, first_time.strftime("%m-%d %H:%M"), muted, scale=1)
    canvas.text(
        plot_right - 66,
        height - 30,
        last_time.strftime("%m-%d %H:%M"),
        muted,
        scale=1,
    )
    canvas.text(
        width // 2 - 145,
        height - 30,
        "BINANCE USD-M FUTURES CLOSED 15M",
        (82, 94, 112),
        scale=1,
    )
    return _encode_png(canvas)


__all__ = ["PNG_SIGNATURE", "render_launch_chart_png"]
