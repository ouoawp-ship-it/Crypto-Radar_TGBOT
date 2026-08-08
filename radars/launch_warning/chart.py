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
    missing_glyphs,
)


CST = timezone(timedelta(hours=8))
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

CHART_CATEGORY_LABELS = {
    "USD-M PERP": "永续合约",
    "GOLD": "黄金",
    "SILVER": "白银",
    "PLATINUM": "铂金",
    "PALLADIUM": "钯金",
    "CRUDE OIL": "原油",
    "NAT GAS": "天然气",
    "COPPER": "铜",
    "ETF INDEX": "指数基金",
    "LEVERAGED ETF": "杠杆基金",
    "EQUITY": "股票",
    "TOKENIZED STOCK": "股票代币",
    "STOCK TOKEN": "股票代币",
    "ETF": "指数基金",
    "COMMODITY": "大宗商品",
    "FOREX": "外汇",
    "CRYPTO INDEX": "加密指数",
    "CRYPTO CORE": "核心主流",
    "CRYPTO MAJOR": "主流加密",
    "CRYPTO ALT": "山寨币",
}

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
    "background": (9, 12, 16),
    "header": (15, 20, 27),
    "panel": (12, 17, 23),
    "grid": (31, 39, 49),
    "ink": (232, 237, 243),
    "muted": (132, 146, 166),
    "rising": (35, 196, 131),
    "falling": (239, 83, 80),
    "accent": (86, 205, 220),
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
            width += (glyph[1] if glyph is not None else 9) + 1
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


def _chart_category_label(value: Any) -> str:
    raw = str(value or "").strip()
    if raw and not missing_glyphs(raw):
        return raw
    return CHART_CATEGORY_LABELS.get(raw.upper(), "未分类")


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


def _footer_text(candle_count: int) -> str:
    return f"币安 · 最近{max(0, int(candle_count))}根1小时已收线K线"


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


def render_launch_chart_png(
    *,
    symbol: str,
    candles: list[Mapping[str, Any]],
    checkpoints: list[Mapping[str, Any]],
    cycle_no: int,
    price_action: Mapping[str, Any] | None = None,
    asset_category: str = "",
    width: int = 960,
    height: int = 540,
) -> bytes:
    """Render a compact closed-candle Binance 1h chart in memory."""

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

    # The image deliberately shows fewer bars so Telegram users can read the
    # candles instead of seeing a compressed line. Nothing here changes the
    # discovery score or directional evidence score.
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
    compact_header = width < 900 or height < 600
    spacious_layout = width >= 1000 and height >= 640
    header_bottom = 96 if spacious_layout else (82 if height >= 460 else 74)
    canvas.rect(0, 0, width - 1, header_bottom, colors["header"])
    event_top = header_bottom + 5
    event_bottom = event_top + (28 if spacious_layout else 22)
    canvas.rect(0, event_top, width - 1, event_bottom, colors["panel"])
    plot_left = 52 if spacious_layout else 44
    plot_right = width - (70 if spacious_layout else 62)
    candle_right = plot_right
    price_top = event_bottom + 8
    volume_bottom = height - 56
    volume_top = height - (112 if height >= 500 else 100)
    price_bottom = volume_top - 14
    time_axis_y = height - 40
    footer_y = height - 18
    if price_bottom - price_top < 70:
        raise ValueError("chart height leaves no readable price area")

    candle_change = (
        (latest["close"] / latest["open"] - 1.0) * 100.0
        if latest["open"] > 0
        else 0.0
    )
    change_color = (
        colors["rising"] if candle_change >= 0 else colors["falling"]
    )
    header_symbol = str(symbol or "UNKNOWN").upper()[:(12 if compact_header else 18)]
    status = str(price_action_state.get("status") or "")
    status_label = CHART_STATUS_LABELS.get(status)
    data_header_text = "1小时已收线 · 15分钟触发参考"
    current_text = f"{_format_price(latest['close'])} {candle_change:+.2f}%"
    ohlc_text = (
        f"开 {_format_price(latest['open'])}  高 {_format_price(latest['high'])}  "
        f"低 {_format_price(latest['low'])}  收 {_format_price(latest['close'])}"
    )
    category_label = _chart_category_label(asset_category or "USD-M PERP")
    checkpoint_count = sum(
        1
        for checkpoint in checkpoints
        if isinstance(checkpoint, Mapping)
        and 0 < int(_number(checkpoint.get("window_end_ts"))) <= last_close_ts
    )
    if spacious_layout:
        canvas.text(20, 14, header_symbol, colors["ink"], scale=3)
        symbol_width = len(header_symbol) * 18
        canvas.ui_text(
            min(width // 3, 30 + symbol_width),
            16,
            category_label,
            colors["accent"],
        )
        current_width = len(current_text) * 12
        canvas.text(
            width - current_width - 22,
            16,
            current_text,
            change_color,
            scale=2,
        )
        state_text = (
            f"第 {max(1, int(cycle_no))} 轮 · 事件 {checkpoint_count}"
            + (f" · {status_label}" if status_label else "")
        )
        canvas.ui_text(20, 48, state_text, colors["muted"])
        detail_text = f"{ohlc_text}  成交量 {_format_volume(latest['quote_volume'])}  · 已收线"
        canvas.ui_text(20, 72, detail_text, change_color)
        canvas.ui_text(
            width - canvas.ui_text_width(data_header_text) - 20,
            48,
            data_header_text,
            colors["accent"],
        )
    elif compact_header:
        canvas.text(16, 8, header_symbol, colors["ink"], scale=1)
        current_width = len(current_text) * 6
        current_group_x = max(
            16 + len(header_symbol) * 6 + 12,
            width - current_width - 12,
        )
        canvas.text(
            current_group_x,
            8,
            current_text,
            change_color,
            scale=1,
        )
        canvas.ui_text(16, 25, category_label, colors["accent"])
        cycle_x = max(102, 28 + canvas.ui_text_width(category_label))
        canvas.ui_text(cycle_x, 25, f"第 {max(1, int(cycle_no))} 轮", colors["muted"])
        event_x = cycle_x + 92
        canvas.ui_text(event_x, 25, f"事件 {checkpoint_count}", colors["muted"])
        canvas.ui_text(16, 44, data_header_text, colors["accent"])
        if status_label:
            status_text = f"形态 {status_label}"
            status_x = width - canvas.ui_text_width(status_text) - 12
            canvas.ui_text(max(238, status_x), 44, status_text, (86, 205, 220))
        compact_ohlc = (
            ohlc_text
            if width >= 640
            else f"开 {_format_price(latest['open'])}  收 {_format_price(latest['close'])}"
        )
        if header_bottom >= 82:
            canvas.ui_text(16, 63, compact_ohlc, change_color)
    else:
        canvas.text(24, 14, header_symbol, colors["ink"], scale=2)
        meta_x = max(190, min(250, width // 6))
        canvas.ui_text(meta_x, 7, category_label, colors["accent"])
        canvas.ui_text(meta_x, 27, data_header_text, colors["muted"])
        cycle_x = meta_x + 220
        canvas.ui_text(
            cycle_x,
            7,
            f"第 {max(1, int(cycle_no))} 轮",
            colors["muted"],
        )
        event_x = cycle_x + 100
        canvas.ui_text(event_x, 7, f"事件 {checkpoint_count}", colors["muted"])
        if status_label:
            canvas.ui_text(cycle_x, 27, f"形态 {status_label}", (86, 205, 220))
        current_width = len(current_text) * 12
        current_group_x = max(
            event_x + 90,
            width - current_width - 24,
        )
        canvas.text(
            current_group_x,
            17,
            current_text,
            change_color,
            scale=2,
        )
        canvas.ui_text(24, 47, "真实数据 · 仅使用已收线K线", colors["accent"])
        ohlc_x = max(300, width - canvas.ui_text_width(ohlc_text) - 20)
        canvas.ui_text(ohlc_x, 47, ohlc_text, change_color)

    for index in range(5):
        y = price_top + round((price_bottom - price_top) * index / 4)
        canvas.line(plot_left, y, plot_right, y, colors["grid"])
    grid_columns = 4
    for index in range(grid_columns):
        x = plot_left + round((candle_right - plot_left) * index / (grid_columns - 1))
        canvas.line(x, price_top, x, volume_bottom, colors["grid"])

    reference_prices: list[float] = []
    if trigger_visible:
        reference_prices.extend([box_low, box_high])
    if key_level_visible:
        reference_prices.append(key_level)
    reference_prices = [value for value in reference_prices if value > 0]
    lowest = min([item["low"] for item in visible] + reference_prices)
    highest = max([item["high"] for item in visible] + reference_prices)
    span = max(highest - lowest, highest * 0.001)
    lowest -= span * 0.07
    highest += span * 0.07
    span = highest - lowest

    def price_y(value: float) -> int:
        ratio = (highest - value) / span
        return price_top + round(ratio * (price_bottom - price_top))

    candle_count = len(visible)
    slot = (candle_right - plot_left) / max(1, candle_count)
    body_half = max(1, min(4, round(slot * 0.42)))
    max_volume = max(item["quote_volume"] for item in visible) or 1.0
    x_positions = [
        plot_left + round((index + 0.5) * slot)
        for index in range(candle_count)
    ]

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
        _dotted_vertical(canvas, x, price_top, volume_bottom, guide_color, gap=6)

    for index, candle in enumerate(visible):
        x = x_positions[index]
        up = candle["close"] >= candle["open"]
        color = colors["rising"] if up else colors["falling"]
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

    for index in range(grid_columns):
        candle_index = round((candle_count - 1) * index / (grid_columns - 1))
        timestamp = visible[candle_index]["close_ts"]
        label = datetime.fromtimestamp(timestamp, CST).strftime("%m-%d %H:%M")
        x = x_positions[candle_index]
        label_x = min(max(plot_left, x - len(label) * 3), candle_right - 67)
        canvas.text(label_x, time_axis_y, label, colors["muted"], scale=1)

    footer = _footer_text(candle_count)
    canvas.ui_text(
        width // 2 - canvas.ui_text_width(footer) // 2,
        footer_y - 2,
        footer,
        (82, 94, 112),
    )

    # The live price badge is always the final visual layer so axis labels or
    # other annotations cannot obscure it.
    current_price = latest["close"]
    current_y = price_y(current_price)
    _dotted_horizontal(canvas, plot_left, plot_right, current_y, change_color)
    price_label = _format_price(current_price)
    time_label = datetime.fromtimestamp(last_close_ts, CST).strftime("%H:%M")
    label_width = max(54, max(len(price_label), len(time_label)) * 6 + 8)
    canvas.rect(
        plot_right,
        current_y - 11,
        min(width - 1, plot_right + label_width),
        current_y + 12,
        change_color,
    )
    canvas.text(plot_right + 4, current_y - 8, price_label, (255, 255, 255), scale=1)
    canvas.text(plot_right + 4, current_y + 2, time_label, (255, 255, 255), scale=1)
    return _encode_png(canvas)


__all__ = [
    "CHART_COLORS",
    "DISPLAY_CANDLE_LIMIT",
    "MAX_EVENT_BADGES",
    "PNG_SIGNATURE",
    "render_launch_chart_png",
]
