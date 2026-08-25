from __future__ import annotations

import binascii
import struct
import zlib
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .chart_font_zh import (
    GLYPH_HEIGHT,
    GLYPH_WIDTH,
    glyph_alpha,
)


CST = timezone(timedelta(hours=8))
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

CHART_STATUS_LABELS = {
    "breakout_15m": "15分钟突破",
    "confirmed_1h": "1小时确认",
    "confirmed_4h": "4小时确认",
    "sweep_high_15m": "上方插针",
    "sweep_low_15m": "下方插针",
    "false_breakout_15m": "15分钟假突破",
    "failed_breakout_15m": "15分钟失效",
    "false_breakout_1h": "1小时假突破",
    "failed_breakout_1h": "1小时失效",
    "false_breakout_4h": "4小时假突破",
    "failed_breakout_4h": "4小时失效",
}

CHART_CONFIRMATION_LABELS = {
    "15m": "15分钟突破",
    "1h": "1小时确认",
    "4h": "4小时确认",
}

CHART_COLORS = {
    "background": (19, 23, 34),
    "header": (19, 23, 34),
    "panel": (19, 23, 34),
    "grid": (28, 33, 44),
    "separator": (180, 185, 195),
    "ink": (214, 218, 226),
    "muted": (91, 98, 114),
    "rising": (38, 166, 154),
    "falling": (242, 54, 69),
    "price_rising": (38, 166, 154),
    "price_falling": (218, 222, 229),
    "accent": (38, 166, 154),
    "oi": (38, 166, 154),
    "extreme": (31, 64, 114),
    "price_badge": (238, 240, 245),
}


# Rendering limits are intentionally strict so candles remain readable on
# Telegram. The chart is presentation-only and never changes signal scoring.
DISPLAY_CANDLE_LIMIT = 120
MAX_EVENT_BADGES = 3

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

    def alpha_pixel(
        self,
        x: int,
        y: int,
        color: tuple[int, int, int],
        alpha: int,
    ) -> None:
        if not (0 <= x < self.width and 0 <= y < self.height):
            return
        safe_alpha = max(0, min(255, int(alpha)))
        if safe_alpha == 0:
            return
        if safe_alpha == 255:
            self.pixel(x, y, color)
            return
        offset = (y * self.width + x) * 3
        inverse = 255 - safe_alpha
        for channel, target in enumerate(color):
            current = self.pixels[offset + channel]
            self.pixels[offset + channel] = (
                current * inverse + int(target) * safe_alpha + 127
            ) // 255

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

    def alpha_rect(
        self,
        x0: int,
        y0: int,
        x1: int,
        y1: int,
        color: tuple[int, int, int],
        alpha: int,
    ) -> None:
        left, right = sorted((max(0, x0), min(self.width - 1, x1)))
        top, bottom = sorted((max(0, y0), min(self.height - 1, y1)))
        for y in range(top, bottom + 1):
            for x in range(left, right + 1):
                self.alpha_pixel(x, y, color, alpha)

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

    @staticmethod
    def ui_text_width(value: str) -> int:
        width = 0
        for character in str(value or ""):
            if character == " ":
                width += 4
                continue
            glyph = glyph_alpha(character)
            if glyph is not None:
                width += glyph[1] + 1
            elif character.upper() in FONT_5X7:
                width += 6
            else:
                width += 10
        return max(0, width - 1)

    def ui_text(
        self,
        x: int,
        y: int,
        value: str,
        color: tuple[int, int, int],
    ) -> None:
        cursor = int(x)
        for character in str(value or ""):
            if character == " ":
                cursor += 4
                continue
            glyph = glyph_alpha(character)
            if glyph is None:
                if character.upper() in FONT_5X7:
                    self.text(cursor, y + 4, character, color, scale=1)
                    cursor += 6
                    continue
                self.line(cursor, y + 2, cursor + 8, y + 2, color)
                self.line(cursor, y + 12, cursor + 8, y + 12, color)
                self.line(cursor, y + 2, cursor, y + 12, color)
                self.line(cursor + 8, y + 2, cursor + 8, y + 12, color)
                cursor += 10
                continue
            alpha, advance = glyph
            for row in range(GLYPH_HEIGHT):
                row_offset = row * GLYPH_WIDTH
                for column in range(GLYPH_WIDTH):
                    opacity = alpha[row_offset + column]
                    if opacity:
                        self.alpha_pixel(
                            cursor + column,
                            y + row,
                            color,
                            opacity,
                        )
            cursor += advance + 1


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


def _format_price(value: float) -> str:
    magnitude = abs(float(value))
    if magnitude >= 1_000:
        return f"{value:.1f}"
    if magnitude >= 100:
        return f"{value:.2f}"
    if magnitude >= 1:
        return f"{value:.4f}"
    if magnitude >= 0.01:
        return f"{value:.5f}"
    return f"{value:.7f}"


def _format_volume(value: float) -> str:
    amount = max(0.0, float(value))
    if amount >= 1_000_000_000:
        return f"{amount / 1_000_000_000:.2f}B"
    if amount >= 1_000_000:
        return f"{amount / 1_000_000:.2f}M"
    if amount >= 1_000:
        return f"{amount / 1_000:.1f}K"
    return f"{amount:.0f}"


def _format_signed_amount(value: float) -> str:
    amount = float(value)
    return f"{'+' if amount >= 0 else '-'}{_format_volume(abs(amount))}"


def _dotted_horizontal(
    canvas: Canvas,
    x0: int,
    x1: int,
    y: int,
    color: tuple[int, int, int],
    *,
    dot: int = 1,
    gap: int = 4,
) -> None:
    for x in range(x0, x1 + 1, max(1, dot + gap)):
        canvas.line(x, y, min(x + dot, x1), y, color)


def _dotted_vertical(
    canvas: Canvas,
    x: int,
    y0: int,
    y1: int,
    color: tuple[int, int, int],
    *,
    dot: int = 1,
    gap: int = 4,
) -> None:
    for y in range(y0, y1 + 1, max(1, dot + gap)):
        canvas.line(x, y, x, min(y + dot, y1), color)


def render_pulse_chart_png(
    *,
    symbol: str,
    candles: list[Mapping[str, Any]],
    checkpoints: list[Mapping[str, Any]],
    cycle_no: int,
    price_action: Mapping[str, Any] | None = None,
    asset_category: str = "",
    signal_change_pct: float | None = None,
    signal_oi_change_pct: float | None = None,
    width: int = 960,
    height: int = 540,
) -> bytes:
    """Render closed Binance 1h context with an optional closed 15m tail."""

    normalized: list[dict[str, float | int | str | bool]] = []
    for item in candles:
        if not isinstance(item, Mapping):
            continue
        oi_close = max(
            0.0,
            _number(item.get("oi_close") or item.get("oi_value")),
        )
        oi_open = max(0.0, _number(item.get("oi_open") or oi_close))
        oi_high = max(
            oi_open,
            oi_close,
            _number(item.get("oi_high")),
        )
        raw_oi_low = max(0.0, _number(item.get("oi_low")))
        oi_low = (
            min(oi_open, oi_close, raw_oi_low)
            if raw_oi_low > 0
            else min(oi_open, oi_close)
        )
        cvd_delta = _number(item.get("cvd_delta"))
        cvd_has_ohlc = all(
            item.get(key) is not None
            for key in ("cvd_open", "cvd_high", "cvd_low", "cvd_close")
        )
        cvd_open = _number(item.get("cvd_open"))
        cvd_close = _number(item.get("cvd_close"))
        cvd_high = max(
            cvd_open,
            cvd_close,
            _number(item.get("cvd_high")),
        )
        cvd_low = min(
            cvd_open,
            cvd_close,
            _number(item.get("cvd_low")),
        )
        normalized.append({
            "close_ts": int(_number(item.get("close_ts"))),
            "open": _number(item.get("open")),
            "high": _number(item.get("high")),
            "low": _number(item.get("low")),
            "close": _number(item.get("close")),
            "quote_volume": max(0.0, _number(item.get("quote_volume"))),
            "oi_open": oi_open,
            "oi_high": oi_high,
            "oi_low": oi_low,
            "oi_close": oi_close,
            "oi_value": oi_close,
            "cvd_delta": cvd_delta,
            "cvd_has_ohlc": cvd_has_ohlc,
            "cvd_open": cvd_open,
            "cvd_high": cvd_high,
            "cvd_low": cvd_low,
            "cvd_close": cvd_close,
            "timeframe": (
                "15m" if str(item.get("timeframe") or "") == "15m" else "1h"
            ),
        })
    normalized.sort(key=lambda item: item["close_ts"])
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

    # The image deliberately shows fewer bars so Telegram users can read the
    # candles instead of seeing a compressed line. Nothing here changes the
    # pulse classification or delivery decision.
    visible = normalized[-DISPLAY_CANDLE_LIMIT:]
    first_close_ts = visible[0]["close_ts"]
    last_close_ts = visible[-1]["close_ts"]
    latest = visible[-1]
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
    key_level = _number(price_action_state.get("level"))
    valid_box = box_high > 0 and box_low > 0 and box_high >= box_low

    trigger_end_ts = int(_number(price_action_state.get("trigger_window_end_ts")))
    box_start_ts = int(_number(price_action_state.get("box_start_ts")))
    box_end_ts = int(_number(price_action_state.get("box_end_ts")))
    if trigger_end_ts > 0:
        lookback = max(2, int(_number(price_action_state.get("lookback")) or 16))
        if box_start_ts <= 0:
            box_start_ts = trigger_end_ts - lookback * 15 * 60
        if box_end_ts <= 0:
            box_end_ts = trigger_end_ts - 15 * 60
    trigger_visible = (
        valid_box
        and box_start_ts > 0
        and box_end_ts >= box_start_ts
        and box_start_ts <= last_close_ts
        and box_end_ts >= first_close_ts
    )
    key_level_visible = key_level > 0 and (
        trigger_visible
        or first_close_ts <= trigger_end_ts <= last_close_ts
    )

    colors = CHART_COLORS
    canvas = Canvas(width, height, colors["background"])
    wide_layout = width >= 1000 and height >= 600
    plot_left = 10 if wide_layout else 8
    plot_right = width - (126 if wide_layout else 84)
    candle_right = plot_left + round(
        (plot_right - plot_left) * (0.70 if wide_layout else 0.88)
    )
    price_top = 7
    indicator_bottom = height - (30 if wide_layout else 26)
    indicator_height = 94 if height >= 640 else (68 if height >= 500 else 48)
    indicator_gap = 1
    cvd_bottom = indicator_bottom
    cvd_top = cvd_bottom - indicator_height
    oi_bottom = cvd_top - indicator_gap
    oi_top = oi_bottom - indicator_height
    price_bottom = oi_top - indicator_gap
    time_axis_y = height - 17
    event_top = price_top + 17
    if price_bottom - price_top < 70:
        raise ValueError("chart height leaves no readable price area")

    candle_change = (
        (latest["close"] / latest["open"] - 1.0) * 100.0
        if latest["open"] > 0
        else 0.0
    )
    display_change = (
        candle_change
        if signal_change_pct is None
        else _number(signal_change_pct)
    )
    change_color = (
        colors["price_rising"]
        if display_change >= 0
        else colors["price_falling"]
    )
    header_symbol = str(symbol or "UNKNOWN").upper()[:18]
    status = str(price_action_state.get("status") or "")
    status_label = CHART_STATUS_LABELS.get(status)
    latest_time_label = datetime.fromtimestamp(last_close_ts, CST).strftime("%H:%M")
    ohlc_text = (
        f"开 {_format_price(latest['open'])}  高 {_format_price(latest['high'])}  "
        f"低 {_format_price(latest['low'])}  收 {_format_price(latest['close'])}"
    )
    checkpoint_count = sum(
        1
        for checkpoint in checkpoints
        if isinstance(checkpoint, Mapping)
        and 0 < int(_number(checkpoint.get("window_end_ts"))) <= last_close_ts
    )
    meta_text = f"{header_symbol}  1小时 / 15分钟  BINANCE"
    info_y = price_top + 3
    canvas.ui_text(plot_left + 6, info_y, meta_text, colors["muted"])
    ohlc_x = plot_left + 18 + canvas.ui_text_width(meta_text)
    if wide_layout:
        canvas.ui_text(ohlc_x, info_y, ohlc_text, colors["ink"])
        change_text = f"{display_change:+.2f}%"
        canvas.text(
            ohlc_x + canvas.ui_text_width(ohlc_text) + 12,
            info_y + 4,
            change_text,
            change_color,
            scale=1,
        )
    state_text = (
        f"第 {max(1, int(cycle_no))} 轮  事件 {checkpoint_count}  "
        f"已收线 {latest_time_label} UTC+8"
        + (f"  {status_label}" if status_label else "")
    )
    state_x = plot_right - canvas.ui_text_width(state_text) - 4
    if state_x > ohlc_x + 180:
        canvas.ui_text(state_x, info_y, state_text, colors["accent"])

    canvas.line(0, oi_top, width - 1, oi_top, colors["separator"])
    canvas.line(0, cvd_top, width - 1, cvd_top, colors["separator"])
    canvas.line(0, cvd_bottom, width - 1, cvd_bottom, colors["separator"])
    for index in range(5):
        y = price_top + round((price_bottom - price_top) * index / 4)
        canvas.line(plot_left, y, plot_right, y, colors["grid"])
    grid_columns = 6 if wide_layout else 4

    reference_prices: list[float] = []
    if trigger_visible:
        reference_prices.extend([box_low, box_high])
    if key_level_visible:
        reference_prices.append(key_level)
    reference_prices = [value for value in reference_prices if value > 0]
    raw_lowest = min([item["low"] for item in visible] + reference_prices)
    raw_highest = max([item["high"] for item in visible] + reference_prices)
    span = max(raw_highest - raw_lowest, raw_highest * 0.001)
    lowest = raw_lowest - span * 0.07
    highest = raw_highest + span * 0.07
    span = highest - lowest

    def price_y(value: float) -> int:
        ratio = (highest - value) / span
        return price_top + round(ratio * (price_bottom - price_top))

    candle_count = len(visible)
    slot = (candle_right - plot_left) / max(1, candle_count)
    body_half = max(1, min(3, round(slot * 0.34)))
    x_positions = [
        plot_left + round((index + 0.5) * slot)
        for index in range(candle_count)
    ]

    tail_start_index = next(
        (
            index
            for index, candle in enumerate(visible)
            if candle["timeframe"] == "15m"
        ),
        None,
    )
    if tail_start_index is not None and tail_start_index > 0:
        tail_boundary_x = round(
            (x_positions[tail_start_index - 1] + x_positions[tail_start_index]) / 2
        )
        _dotted_vertical(
            canvas,
            tail_boundary_x,
            price_top,
            indicator_bottom,
            (35, 92, 91),
            dot=3,
            gap=6,
        )

    def nearest_index(timestamp: int) -> int:
        if timestamp <= first_close_ts:
            return 0
        if timestamp >= last_close_ts:
            return candle_count - 1
        return min(
            range(candle_count),
            key=lambda position: abs(visible[position]["close_ts"] - timestamp),
        )

    trigger_label_spec: tuple[int, int, int, int] | None = None
    if trigger_visible:
        box_x0 = x_positions[nearest_index(max(box_start_ts, first_close_ts))]
        box_x1 = x_positions[nearest_index(min(box_end_ts, last_close_ts))]
        box_x1 = max(box_x0 + 2, box_x1)
        box_y0 = price_y(box_high)
        box_y1 = price_y(box_low)
        trigger_label_spec = (box_x0, box_x1, box_y0, box_y1)

    key_level_spec: tuple[int, int] | None = None
    if key_level_visible:
        level_y = price_y(key_level)
        level_x0 = x_positions[
            nearest_index(max(box_start_ts or trigger_end_ts, first_close_ts))
        ]
        key_level_spec = (level_x0, level_y)

    event_colors = {
        "primed": (73, 143, 255),
        "breakout": (246, 189, 22),
        "launched": (207, 106, 255),
        "cooling": colors["muted"],
        "failed": (255, 92, 92),
    }
    marker_specs: list[dict[str, Any]] = []
    for fallback_no, checkpoint in enumerate(checkpoints, start=1):
        if not isinstance(checkpoint, Mapping):
            continue
        timestamp = int(_number(checkpoint.get("window_end_ts")))
        if timestamp <= 0 or timestamp > last_close_ts:
            continue
        marker_specs.append({
            "number": int(_number(checkpoint.get("checkpoint_no"))) or fallback_no,
            "timestamp": timestamp,
            "stage": str(checkpoint.get("stage") or ""),
        })
    # Three recent numbered lifecycle events keep the chart legible on phones;
    # the header still shows the full event count.
    marker_specs = sorted(marker_specs, key=lambda item: item["timestamp"])[
        -MAX_EVENT_BADGES:
    ]

    if trigger_label_spec is not None:
        box_x0, box_x1, box_y0, box_y1 = trigger_label_spec
        canvas.alpha_rect(
            box_x0,
            min(box_y0, box_y1),
            box_x1,
            max(box_y0, box_y1),
            (18, 48, 72),
            125,
        )

    for marker in marker_specs:
        timestamp = int(marker["timestamp"])
        if timestamp < first_close_ts:
            continue
        x = x_positions[nearest_index(timestamp)]
        marker_color = event_colors.get(marker["stage"], (73, 143, 255))
        guide_color = tuple(
            round(background + (channel - background) * 0.42)
            for channel, background in zip(
                marker_color,
                colors["background"],
            )
        )
        _dotted_vertical(canvas, x, price_top, indicator_bottom, guide_color, gap=6)

    for index, candle in enumerate(visible):
        x = x_positions[index]
        up = candle["close"] >= candle["open"]
        color = colors["price_rising"] if up else colors["price_falling"]
        canvas.line(x, price_y(candle["high"]), x, price_y(candle["low"]), color)
        open_y = price_y(candle["open"])
        close_y = price_y(candle["close"])
        canvas.rect(
            x - body_half,
            min(open_y, close_y),
            x + body_half,
            max(min(open_y, close_y) + 1, max(open_y, close_y)),
            color,
        )

    oi_candles = [
        (
            index,
            candle["oi_open"],
            candle["oi_high"],
            candle["oi_low"],
            candle["oi_close"],
        )
        for index, candle in enumerate(visible)
        if min(
            candle["oi_open"],
            candle["oi_high"],
            candle["oi_low"],
            candle["oi_close"],
        ) > 0
        and candle["oi_high"] >= candle["oi_low"]
    ]
    latest_oi_y = oi_bottom
    oi_low = 0.0
    oi_high = 0.0
    if oi_candles:
        oi_low = min(low for _index, _open, _high, low, _close in oi_candles)
        oi_high = max(high for _index, _open, high, _low, _close in oi_candles)
        oi_span = max(oi_high - oi_low, oi_high * 0.001, 1.0)

        def oi_y(value: float) -> int:
            ratio = (oi_high - value) / oi_span
            return oi_top + 18 + round(
                ratio * max(1, oi_bottom - oi_top - 22)
            )

        oi_body_half = max(1, min(2, body_half))
        for index, open_value, high_value, low_value, close_value in oi_candles:
            x = x_positions[index]
            color = (
                colors["rising"]
                if close_value >= open_value
                else colors["falling"]
            )
            open_y = oi_y(open_value)
            high_y = oi_y(high_value)
            low_y = oi_y(low_value)
            close_y = oi_y(close_value)
            canvas.line(x, high_y, x, low_y, color)
            canvas.rect(
                x - oi_body_half,
                min(open_y, close_y),
                x + oi_body_half,
                max(min(open_y, close_y) + 1, max(open_y, close_y)),
                color,
            )
        latest_oi_y = oi_y(oi_candles[-1][4])

    cvd_candles: list[tuple[int, float, float, float, float]] = []
    running_cvd = 0.0
    for index, candle in enumerate(visible):
        if candle["cvd_has_ohlc"]:
            open_value = float(candle["cvd_open"])
            high_value = float(candle["cvd_high"])
            low_value = float(candle["cvd_low"])
            close_value = float(candle["cvd_close"])
        else:
            open_value = running_cvd
            close_value = open_value + float(candle["cvd_delta"])
            high_value = max(open_value, close_value)
            low_value = min(open_value, close_value)
        cvd_candles.append((
            index,
            open_value,
            high_value,
            low_value,
            close_value,
        ))
        running_cvd = close_value
    cvd_low = min([value[3] for value in cvd_candles] + [0.0])
    cvd_high = max([value[2] for value in cvd_candles] + [0.0])
    cvd_span = max(cvd_high - cvd_low, max(abs(cvd_high), abs(cvd_low)) * 0.001, 1.0)

    def cvd_y(value: float) -> int:
        ratio = (cvd_high - value) / cvd_span
        return cvd_top + 18 + round(
            ratio * max(1, cvd_bottom - cvd_top - 22)
        )

    cvd_zero_y = cvd_y(0.0)
    _dotted_horizontal(
        canvas,
        plot_left,
        plot_right,
        cvd_zero_y,
        colors["grid"],
        dot=2,
        gap=4,
    )
    cvd_body_half = max(1, min(2, body_half))
    for index, open_value, high_value, low_value, close_value in cvd_candles:
        x = x_positions[index]
        color = (
            colors["rising"]
            if close_value >= open_value
            else colors["falling"]
        )
        open_y = cvd_y(open_value)
        high_y = cvd_y(high_value)
        low_y = cvd_y(low_value)
        close_y = cvd_y(close_value)
        canvas.line(x, high_y, x, low_y, color)
        canvas.rect(
            x - cvd_body_half,
            min(open_y, close_y),
            x + cvd_body_half,
            max(min(open_y, close_y) + 1, max(open_y, close_y)),
            color,
        )

    latest_oi_open = oi_candles[-1][1] if oi_candles else 0.0
    latest_oi_high = oi_candles[-1][2] if oi_candles else 0.0
    latest_oi_low = oi_candles[-1][3] if oi_candles else 0.0
    latest_oi = oi_candles[-1][4] if oi_candles else 0.0
    oi_change = (
        0.0
        if signal_oi_change_pct is None
        else _number(signal_oi_change_pct)
    )
    oi_name = "OI KLINE"
    oi_label = (
        f"O {_format_volume(latest_oi_open)} "
        f"H {_format_volume(latest_oi_high)} "
        f"L {_format_volume(latest_oi_low)} "
        f"C {_format_volume(latest_oi)}"
    )
    oi_change_label = f"{oi_change:+.2f}%"
    latest_cvd_open = cvd_candles[-1][1]
    latest_cvd_high = cvd_candles[-1][2]
    latest_cvd_low = cvd_candles[-1][3]
    latest_cvd = cvd_candles[-1][4]
    cvd_name = "CVD KLINE"
    cvd_label = (
        f"O {_format_signed_amount(latest_cvd_open)} "
        f"H {_format_signed_amount(latest_cvd_high)} "
        f"L {_format_signed_amount(latest_cvd_low)} "
        f"C {_format_signed_amount(latest_cvd)}"
    )
    oi_label_x = plot_left + 8
    cvd_label_x = plot_left + 8
    label_y_offset = 7
    canvas.text(oi_label_x, oi_top + label_y_offset, oi_name, colors["muted"], scale=1)
    oi_values_x = oi_label_x + (len(oi_name) + 2) * 6
    canvas.text(
        oi_values_x,
        oi_top + label_y_offset,
        oi_label,
        colors["rising"] if latest_oi >= latest_oi_open else colors["falling"],
        scale=1,
    )
    canvas.text(
        oi_values_x + (len(oi_label) + 2) * 6,
        oi_top + label_y_offset,
        oi_change_label,
        colors["rising"] if oi_change >= 0 else colors["falling"],
        scale=1,
    )
    canvas.text(
        cvd_label_x,
        cvd_top + label_y_offset,
        cvd_name,
        colors["muted"],
        scale=1,
    )
    canvas.text(
        cvd_label_x + (len(cvd_name) + 2) * 6,
        cvd_top + label_y_offset,
        cvd_label,
        colors["rising"] if latest_cvd >= latest_cvd_open else colors["falling"],
        scale=1,
    )

    if oi_candles:
        oi_badge = f"OI {_format_volume(latest_oi)}"
        oi_badge_color = (
            colors["rising"] if latest_oi >= latest_oi_open else colors["falling"]
        )
        oi_badge_width = max(48, len(oi_badge) * 6 + 8)
        canvas.text(
            plot_right + 7,
            oi_top + 2,
            _format_volume(oi_high),
            colors["muted"],
            scale=1,
        )
        canvas.text(
            plot_right + 7,
            oi_bottom - 8,
            _format_volume(oi_low),
            colors["muted"],
            scale=1,
        )
        oi_badge_y = min(max(latest_oi_y, oi_top + 10), oi_bottom - 10)
        canvas.rect(
            plot_right,
            oi_badge_y - 8,
            min(width - 1, plot_right + oi_badge_width),
            oi_badge_y + 8,
            oi_badge_color,
        )
        canvas.text(
            plot_right + 4,
            oi_badge_y - 4,
            oi_badge,
            (255, 255, 255),
            scale=1,
        )

    latest_cvd_y = cvd_y(latest_cvd)
    latest_cvd_color = (
        colors["rising"] if latest_cvd >= latest_cvd_open else colors["falling"]
    )
    cvd_badge = f"CVD {_format_signed_amount(latest_cvd)}"
    cvd_badge_width = max(48, len(cvd_badge) * 6 + 8)
    canvas.text(
        plot_right + 7,
        cvd_top + 2,
        _format_signed_amount(cvd_high),
        colors["muted"],
        scale=1,
    )
    canvas.text(
        plot_right + 7,
        cvd_bottom - 8,
        _format_signed_amount(cvd_low),
        colors["muted"],
        scale=1,
    )
    cvd_badge_y = min(max(latest_cvd_y, cvd_top + 10), cvd_bottom - 10)
    canvas.rect(
        plot_right,
        cvd_badge_y - 8,
        min(width - 1, plot_right + cvd_badge_width),
        cvd_badge_y + 8,
        latest_cvd_color,
    )
    canvas.text(
        plot_right + 4,
        cvd_badge_y - 4,
        cvd_badge,
        (255, 255, 255),
        scale=1,
    )

    # Trigger facts are redrawn after candles so the original 15-minute
    # context stays readable.
    protected_x = x_positions[max(0, candle_count - 12)]
    # The original 15-minute trigger facts stay above the candles when both
    # share the same price.
    if trigger_label_spec is not None:
        box_x0, box_x1, box_y0, box_y1 = trigger_label_spec
        box_border = (60, 135, 181)
        canvas.line(box_x0, box_y0, box_x1, box_y0, box_border)
        canvas.line(box_x0, box_y1, box_x1, box_y1, box_border)
        canvas.line(box_x0, box_y0, box_x0, box_y1, box_border)
        canvas.line(box_x1, box_y0, box_x1, box_y1, box_border)
        label = "15分钟整理"
        label_x = min(
            max(plot_left, box_x0 + 4),
            protected_x - canvas.ui_text_width(label) - 4,
        )
        label_y = max(price_top, min(box_y0, box_y1) + 2)
        canvas.ui_text(label_x, label_y, label, (86, 205, 220))
    if key_level_spec is not None:
        level_x0, level_y = key_level_spec
        _dotted_horizontal(
            canvas,
            level_x0,
            candle_right,
            level_y,
            (86, 205, 220),
            dot=4,
            gap=5,
        )
        label = "关键位"
        label_x = protected_x - canvas.ui_text_width(label) - 5
        label_y = max(price_top, level_y - GLYPH_HEIGHT - 2)
        canvas.ui_text(label_x, label_y, label, (86, 205, 220))

    event_badge_right = candle_right
    marker_badges: list[
        tuple[Mapping[str, Any], int, int, int, str, tuple[int, int, int]]
    ] = []
    for marker in reversed(marker_specs):
        timestamp = int(marker["timestamp"])
        if timestamp <= 0:
            continue
        clipped = timestamp < first_close_ts
        x = x_positions[nearest_index(timestamp)]
        color = event_colors.get(marker["stage"], (73, 143, 255))
        label = f"{'<' if clipped else ''}{int(marker['number'])}"
        text_width = len(label) * 6 - 1
        label_width = max(15, text_width + 8)
        label_x = min(x - label_width // 2, event_badge_right - label_width)
        label_x = max(plot_left, label_x)
        if label_x + label_width > event_badge_right:
            continue
        marker_badges.append((marker, label_x, label_width, text_width, label, color))
        event_badge_right = label_x - 5
    for (
        _marker,
        label_x,
        label_width,
        text_width,
        label,
        color,
    ) in reversed(marker_badges):
        label_y = event_top + 6
        canvas.rect(label_x, label_y, label_x + label_width, label_y + 13, color)
        canvas.rect(
            label_x + 1,
            label_y + 1,
            label_x + label_width - 1,
            label_y + 12,
            colors["header"],
        )
        canvas.text(
            label_x + (label_width - text_width) // 2,
            label_y + 3,
            label,
            color,
            scale=1,
        )

    for index in range(5):
        value = highest - span * index / 4
        y = price_top + round((price_bottom - price_top) * index / 4) - 5
        canvas.text(plot_right + 7, y, _format_price(value), colors["muted"], scale=1)

    for label, value in (("最高", raw_highest), ("最低", raw_lowest)):
        badge_text = f"{label} {_format_price(value)}"
        badge_width = canvas.ui_text_width(badge_text) + 8
        badge_y = price_y(value)
        canvas.rect(
            plot_right,
            badge_y - 7,
            min(width - 1, plot_right + badge_width),
            badge_y + 7,
            colors["extreme"],
        )
        canvas.ui_text(
            plot_right + 4,
            badge_y - 7,
            badge_text,
            (235, 239, 247),
        )

    visible_time_span = max(1, last_close_ts - first_close_ts)
    data_pixel_span = max(1, candle_right - plot_left)
    for index in range(grid_columns):
        x = plot_left + round((plot_right - plot_left) * index / (grid_columns - 1))
        timestamp = first_close_ts + round(
            (x - plot_left) / data_pixel_span * visible_time_span
        )
        label = datetime.fromtimestamp(timestamp, CST).strftime("%m-%d")
        label_x = min(max(plot_left, x - len(label) * 3), plot_right - 31)
        canvas.text(label_x, time_axis_y, label, colors["muted"], scale=1)

    # The live price badge is always the final visual layer so axis labels or
    # other annotations cannot obscure it.
    current_price = latest["close"]
    current_y = price_y(current_price)
    _dotted_horizontal(
        canvas,
        plot_left,
        plot_right,
        current_y,
        (184, 190, 201),
        dot=1,
        gap=3,
    )
    price_label = f"{header_symbol}  {_format_price(current_price)}"
    time_label = latest_time_label
    label_width = max(54, max(len(price_label), len(time_label)) * 6 + 8)
    canvas.rect(
        plot_right,
        current_y - 11,
        min(width - 1, plot_right + label_width),
        current_y + 12,
        colors["price_badge"],
    )
    canvas.text(
        plot_right + 4,
        current_y - 8,
        price_label,
        colors["background"],
        scale=1,
    )
    canvas.text(
        plot_right + 4,
        current_y + 2,
        time_label,
        colors["background"],
        scale=1,
    )
    return _encode_png(canvas)


__all__ = [
    "CHART_COLORS",
    "DISPLAY_CANDLE_LIMIT",
    "MAX_EVENT_BADGES",
    "PNG_SIGNATURE",
    "render_pulse_chart_png",
]
