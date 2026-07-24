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
    price_action: Mapping[str, Any] | None = None,
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

    price_action_state = (
        dict(price_action)
        if isinstance(price_action, Mapping) and price_action.get("enabled")
        else {}
    )
    price_action_frames = price_action_state.get("timeframes")
    frame_15m = (
        price_action_frames.get("15m")
        if isinstance(price_action_frames, Mapping)
        and isinstance(price_action_frames.get("15m"), Mapping)
        else {}
    )
    box_high = _number(price_action_state.get("box_high"))
    box_low = _number(price_action_state.get("box_low"))
    if box_high <= 0:
        box_high = _number(frame_15m.get("box_high"))
    if box_low <= 0:
        box_low = _number(frame_15m.get("box_low"))
    level = _number(price_action_state.get("level"))
    valid_box = box_high > 0 and box_low > 0 and box_high >= box_low
    smc_state = price_action_state.get("smc")
    smc_state = dict(smc_state) if isinstance(smc_state, Mapping) else {}
    smc_snapshot = smc_state.get("snapshot")
    smc_snapshot = (
        dict(smc_snapshot) if isinstance(smc_snapshot, Mapping) else {}
    )
    smc_timeframes = smc_snapshot.get("timeframes")
    smc_timeframes = (
        smc_timeframes if isinstance(smc_timeframes, Mapping) else {}
    )
    smc_15m = smc_timeframes.get("15m")
    smc_15m = dict(smc_15m) if isinstance(smc_15m, Mapping) else {}

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
    status = str(price_action_state.get("status") or "")
    status_labels = {
        "breakout_15m": "15M BO",
        "confirmed_1h": "1H OK",
        "confirmed_4h": "4H OK",
        "sweep_high_15m": "SWEEP H",
        "sweep_low_15m": "SWEEP L",
        "false_breakout_15m": "15M FALSE",
        "failed_breakout_15m": "15M FAIL",
        "false_breakout_1h": "1H FALSE",
        "failed_breakout_1h": "1H FAIL",
        "false_breakout_4h": "4H FALSE",
        "failed_breakout_4h": "4H FAIL",
    }
    if status in status_labels:
        canvas.text(
            555,
            21,
            f"PA {status_labels[status]}",
            (86, 205, 220),
            scale=1,
        )
    smc_bias = smc_state.get("htf_bias")
    smc_bias = smc_bias if isinstance(smc_bias, Mapping) else {}
    smc_direction = str(smc_bias.get("direction") or "")
    if smc_state.get("enabled") and smc_direction in {"up", "down"}:
        canvas.text(
            635,
            21,
            "SMC UP" if smc_direction == "up" else "SMC DOWN",
            (53, 208, 127) if smc_direction == "up" else (255, 105, 120),
            scale=1,
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

    smc_overlay_prices: list[float] = []
    dealing_range = smc_15m.get("dealing_range")
    if isinstance(dealing_range, Mapping):
        smc_overlay_prices.extend([
            _number(dealing_range.get("high")),
            _number(dealing_range.get("low")),
            _number(dealing_range.get("equilibrium")),
        ])
    for collection in (
        "swings",
        "structures",
        "liquidity",
        "fvgs",
        "order_blocks",
        "breaker_blocks",
    ):
        for item in smc_15m.get(collection) or []:
            if not isinstance(item, Mapping):
                continue
            smc_overlay_prices.extend([
                _number(item.get("level")),
                _number(item.get("bottom")),
                _number(item.get("top")),
            ])
    overlay_prices = [
        value
        for value in (box_high, box_low, level, *smc_overlay_prices)
        if value > 0
    ]
    lowest = min(
        [item["low"] for item in normalized] + overlay_prices
    )
    highest = max(
        [item["high"] for item in normalized] + overlay_prices
    )
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
    x_positions = [
        plot_left + round((index + 0.5) * slot)
        for index in range(candle_count)
    ]
    first_close_ts = normalized[0]["close_ts"]
    last_close_ts = normalized[-1]["close_ts"]

    def nearest_index(timestamp: int) -> int:
        if timestamp <= first_close_ts:
            return 0
        if timestamp >= last_close_ts:
            return candle_count - 1
        return min(
            range(candle_count),
            key=lambda position: abs(
                normalized[position]["close_ts"] - timestamp
            ),
        )

    trigger_end_ts = int(
        _number(price_action_state.get("trigger_window_end_ts"))
    )
    box_start_ts = int(_number(price_action_state.get("box_start_ts")))
    box_end_ts = int(_number(price_action_state.get("box_end_ts")))
    if trigger_end_ts > 0:
        lookback = max(
            2,
            int(_number(price_action_state.get("lookback")) or 16),
        )
        if box_start_ts <= 0:
            box_start_ts = trigger_end_ts - lookback * 15 * 60
        if box_end_ts <= 0:
            box_end_ts = trigger_end_ts - 15 * 60

    def visible_interval(start_ts: int, end_ts: int = 0) -> tuple[int, int] | None:
        effective_end = end_ts if end_ts > 0 else last_close_ts
        if start_ts > last_close_ts or effective_end < first_close_ts:
            return None
        start_index = nearest_index(max(start_ts, first_close_ts))
        end_index = nearest_index(min(effective_end, last_close_ts))
        return x_positions[start_index], x_positions[end_index]

    def draw_smc_zone(
        item: Mapping[str, Any],
        *,
        label: str,
        border: tuple[int, int, int],
        fill: tuple[int, int, int],
        start_key: str = "formed_ts",
        end_ts: int = 0,
    ) -> None:
        bottom = _number(item.get("bottom"))
        top = _number(item.get("top"))
        start_ts = int(_number(item.get(start_key)))
        interval = visible_interval(start_ts, end_ts)
        if bottom <= 0 or top <= bottom or interval is None:
            return
        x0, x1 = interval
        y0 = price_y(top)
        y1 = price_y(bottom)
        for y in range(min(y0, y1) + 3, max(y0, y1), 8):
            canvas.line(x0, y, x1, y, fill)
        canvas.line(x0, y0, x1, y0, border)
        canvas.line(x0, y1, x1, y1, border)
        canvas.line(x0, y0, x0, y1, border)
        label_width = len(label) * 6 + 6
        label_x = min(max(plot_left, x0 + 2), plot_right - label_width)
        label_y = min(max(price_top, min(y0, y1) + 2), price_bottom - 11)
        canvas.rect(
            label_x,
            label_y,
            label_x + label_width,
            label_y + 10,
            (20, 27, 36),
        )
        canvas.text(label_x + 3, label_y + 2, label, border, scale=1)

    if isinstance(dealing_range, Mapping):
        range_high = _number(dealing_range.get("high"))
        range_low = _number(dealing_range.get("low"))
        equilibrium = _number(dealing_range.get("equilibrium"))
        range_interval = visible_interval(
            int(_number(dealing_range.get("start_ts"))),
            int(_number(dealing_range.get("end_ts"))),
        )
        if (
            range_high > equilibrium > range_low > 0
            and range_interval is not None
        ):
            range_x0, range_x1 = range_interval
            high_y = price_y(range_high)
            eq_y = price_y(equilibrium)
            low_y = price_y(range_low)
            for y in range(high_y + 5, eq_y, 14):
                canvas.line(range_x0, y, range_x1, y, (39, 21, 28))
            for y in range(eq_y + 5, low_y, 14):
                canvas.line(range_x0, y, range_x1, y, (16, 38, 31))
            for x in range(range_x0, range_x1 + 1, 9):
                canvas.line(
                    x,
                    eq_y,
                    min(x + 5, range_x1),
                    eq_y,
                    (168, 139, 84),
                )
            canvas.text(range_x0 + 3, high_y + 3, "PREM", (156, 83, 96), scale=1)
            canvas.text(range_x0 + 3, eq_y + 4, "DISC", (55, 137, 105), scale=1)

    for gap in (smc_15m.get("fvgs") or [])[-4:]:
        if not isinstance(gap, Mapping):
            continue
        direction = str(gap.get("direction") or "")
        end_ts = int(_number(gap.get("mitigated_ts")))
        draw_smc_zone(
            gap,
            label="FVG+" if direction == "up" else "FVG-",
            border=(44, 151, 119) if direction == "up" else (181, 77, 95),
            fill=(17, 47, 40) if direction == "up" else (49, 24, 31),
            end_ts=end_ts,
        )
    for block in (smc_15m.get("order_blocks") or [])[-4:]:
        if not isinstance(block, Mapping):
            continue
        direction = str(block.get("direction") or "")
        end_ts = int(_number(block.get("invalidated_ts")))
        draw_smc_zone(
            block,
            label="OB+" if direction == "up" else "OB-",
            border=(66, 124, 218) if direction == "up" else (174, 91, 190),
            fill=(20, 38, 68) if direction == "up" else (48, 26, 55),
            end_ts=end_ts,
        )
    for breaker in (smc_15m.get("breaker_blocks") or [])[-3:]:
        if not isinstance(breaker, Mapping):
            continue
        direction = str(breaker.get("direction") or "")
        draw_smc_zone(
            breaker,
            label="BRK+" if direction == "up" else "BRK-",
            border=(246, 189, 22),
            fill=(55, 44, 15),
        )

    if (
        valid_box
        and box_start_ts > 0
        and box_end_ts >= box_start_ts
        and box_start_ts <= last_close_ts
        and box_end_ts >= first_close_ts
    ):
        box_x0 = x_positions[nearest_index(max(box_start_ts, first_close_ts))]
        box_x1 = x_positions[nearest_index(min(box_end_ts, last_close_ts))]
        box_y0 = price_y(box_high)
        box_y1 = price_y(box_low)
        box_fill = (18, 34, 49)
        box_border = (60, 101, 139)
        for y in range(min(box_y0, box_y1) + 2, max(box_y0, box_y1), 4):
            canvas.line(box_x0 + 1, y, box_x1 - 1, y, box_fill)
        canvas.line(box_x0, box_y0, box_x1, box_y0, box_border)
        canvas.line(box_x0, box_y1, box_x1, box_y1, box_border)
        canvas.line(box_x0, box_y0, box_x0, box_y1, box_border)
        canvas.line(box_x1, box_y0, box_x1, box_y1, box_border)
        box_label_y = min(box_y0, box_y1) + 3
        canvas.rect(
            box_x0 + 3,
            box_label_y,
            box_x0 + 27,
            box_label_y + 11,
            (20, 27, 36),
        )
        canvas.text(
            box_x0 + 6,
            box_label_y + 2,
            "BOX",
            box_border,
            scale=1,
        )

    for index, candle in enumerate(normalized):
        x = x_positions[index]
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

    if level > 0:
        level_y = price_y(level)
        level_start_ts = box_start_ts or trigger_end_ts or first_close_ts
        level_x0 = x_positions[nearest_index(max(level_start_ts, first_close_ts))]
        level_color = (86, 205, 220)
        for x in range(level_x0, plot_right + 1, 9):
            canvas.line(x, level_y, min(x + 5, plot_right), level_y, level_color)
        label_x = max(level_x0 + 3, plot_right - 29)
        label_y = min(max(price_top, level_y - 14), price_bottom - 12)
        canvas.rect(label_x, label_y, label_x + 27, label_y + 11, (20, 27, 36))
        canvas.text(label_x + 3, label_y + 2, "LVL", level_color, scale=1)

    event_colors = {
        "primed": (73, 143, 255),
        "breakout": (246, 189, 22),
        "launched": (207, 106, 255),
        "cooling": (148, 163, 184),
        "failed": (255, 92, 92),
    }
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
            index = nearest_index(event_ts)
        x = x_positions[index]
        for y in range(price_top, volume_bottom + 1, 5):
            canvas.line(x, y, x, min(y + 2, volume_bottom), color)
        label = f"E{event_no}{'<' if clipped else ''}"
        label_width = len(label) * 12 + 10
        label_x = min(max(plot_left, x - label_width // 2), plot_right - label_width)
        label_y = price_top + 8 + ((event_no - 1) % 3) * 24
        canvas.rect(label_x, label_y, label_x + label_width, label_y + 19, (20, 27, 36))
        canvas.text(label_x + 5, label_y + 3, label, color, scale=2)

    def draw_smc_label(
        timestamp: int,
        price: float,
        label: str,
        color: tuple[int, int, int],
        *,
        above: bool,
    ) -> None:
        if (
            timestamp < first_close_ts
            or timestamp > last_close_ts
            or price <= 0
        ):
            return
        x = x_positions[nearest_index(timestamp)]
        anchor_y = price_y(price)
        label_width = len(label) * 6 + 6
        label_x = min(
            max(plot_left, x - label_width // 2),
            plot_right - label_width,
        )
        label_y = (
            max(price_top, anchor_y - 14)
            if above
            else min(price_bottom - 10, anchor_y + 4)
        )
        canvas.rect(
            label_x,
            label_y,
            label_x + label_width,
            label_y + 10,
            (20, 27, 36),
        )
        canvas.text(label_x + 3, label_y + 2, label, color, scale=1)

    for swing in (smc_15m.get("swings") or [])[-12:]:
        if not isinstance(swing, Mapping):
            continue
        kind = str(swing.get("kind") or "")
        draw_smc_label(
            int(_number(swing.get("ts"))),
            _number(swing.get("level")),
            str(swing.get("label") or ""),
            (196, 202, 211),
            above=kind == "high",
        )

    structure_colors = {
        "BOS": (53, 208, 127),
        "CHOCH": (255, 174, 66),
        "MSS": (214, 96, 255),
    }
    for structure in (smc_15m.get("structures") or [])[-8:]:
        if not isinstance(structure, Mapping):
            continue
        event_type = str(structure.get("type") or "BOS")
        color = structure_colors.get(event_type, (53, 208, 127))
        level_value = _number(structure.get("level"))
        interval = visible_interval(
            int(_number(structure.get("source_ts"))),
            int(_number(structure.get("event_ts"))),
        )
        if interval is not None and level_value > 0:
            x0, x1 = interval
            y = price_y(level_value)
            for x in range(x0, x1 + 1, 8):
                canvas.line(x, y, min(x + 4, x1), y, color)
        draw_smc_label(
            int(_number(structure.get("event_ts"))),
            level_value,
            event_type,
            color,
            above=str(structure.get("direction") or "") == "up",
        )

    for pool in (smc_15m.get("liquidity") or [])[-6:]:
        if not isinstance(pool, Mapping):
            continue
        level_value = _number(pool.get("level"))
        event_ts = int(_number(pool.get("event_ts")))
        interval = visible_interval(
            int(_number(pool.get("formed_ts"))),
            event_ts,
        )
        if interval is None or level_value <= 0:
            continue
        x0, x1 = interval
        y = price_y(level_value)
        color = (238, 196, 72)
        for x in range(x0, x1 + 1, 10):
            canvas.line(x, y, min(x + 5, x1), y, color)
        draw_smc_label(
            int(_number(pool.get("formed_ts"))),
            level_value,
            str(pool.get("type") or "LIQ"),
            color,
            above=str(pool.get("type") or "") == "BSL",
        )
        if pool.get("status") == "swept":
            draw_smc_label(
                event_ts,
                level_value,
                "SWP",
                (255, 126, 67),
                above=str(pool.get("type") or "") == "BSL",
            )

    for mitigation in (smc_15m.get("mitigation_blocks") or [])[-4:]:
        if not isinstance(mitigation, Mapping):
            continue
        direction = str(mitigation.get("direction") or "")
        price = (
            _number(mitigation.get("top"))
            if direction == "up"
            else _number(mitigation.get("bottom"))
        )
        draw_smc_label(
            int(_number(mitigation.get("event_ts"))),
            price,
            "MB",
            (105, 167, 255),
            above=direction == "up",
        )
    for gap in (smc_15m.get("fvgs") or [])[-4:]:
        if not isinstance(gap, Mapping):
            continue
        mitigated_ts = int(_number(gap.get("mitigated_ts")))
        if mitigated_ts <= 0:
            continue
        direction = str(gap.get("direction") or "")
        draw_smc_label(
            mitigated_ts,
            (
                _number(gap.get("top"))
                if direction == "up"
                else _number(gap.get("bottom"))
            ),
            "FVG R",
            (79, 185, 160) if direction == "up" else (206, 101, 119),
            above=direction == "up",
        )

    marker_specs: list[tuple[int, str, tuple[int, int, int]]] = []
    confirmation_ends = price_action_state.get("confirmation_ends")
    if isinstance(confirmation_ends, Mapping):
        for timeframe, label, color in (
            ("15m", "15M BO", (42, 204, 150)),
            ("1h", "1H OK", (79, 145, 255)),
            ("4h", "4H OK", (207, 106, 255)),
        ):
            timestamp = int(_number(confirmation_ends.get(timeframe)))
            if timestamp > 0:
                marker_specs.append((timestamp, label, color))

    terminal_label = ""
    terminal_color = (255, 159, 67)
    if status.startswith("sweep_high"):
        terminal_label = "SWEEP H"
    elif status.startswith("sweep_low"):
        terminal_label = "SWEEP L"
    elif status.startswith("false_breakout"):
        terminal_label = (
            "SWEEP H"
            if price_action_state.get("direction") == "up"
            else "SWEEP L"
        )
    elif status.startswith("failed_breakout"):
        terminal_label = "FAIL"
        terminal_color = (255, 92, 92)
    if terminal_label:
        terminal_ts = int(
            _number(price_action_state.get("event_window_end_ts"))
            or trigger_end_ts
        )
        if terminal_ts > 0:
            marker_specs.append((terminal_ts, terminal_label, terminal_color))

    for marker_no, (timestamp, label, color) in enumerate(marker_specs):
        index = nearest_index(timestamp)
        x = x_positions[index]
        anchor_y = price_y(normalized[index]["high"]) - 4
        label_width = len(label) * 6 + 8
        label_x = min(
            max(plot_left, x - label_width // 2),
            plot_right - label_width,
        )
        label_y = max(
            price_top + 2,
            anchor_y - 18 - (marker_no % 2) * 15,
        )
        canvas.line(x, anchor_y, x, label_y + 12, color)
        canvas.line(x - 3, anchor_y - 4, x, anchor_y, color)
        canvas.line(x + 3, anchor_y - 4, x, anchor_y, color)
        canvas.rect(
            label_x,
            label_y,
            label_x + label_width,
            label_y + 12,
            (20, 27, 36),
        )
        canvas.text(label_x + 4, label_y + 3, label, color, scale=1)

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
