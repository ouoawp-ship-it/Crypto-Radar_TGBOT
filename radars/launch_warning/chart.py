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
from .smc_overlay import build_smc_overlay


_SESSION_BASED_ASSET_CATEGORIES = frozenset({
    "EQUITY",
    "ETF",
    "ETF INDEX",
    "LEVERAGED ETF",
    "STOCK TOKEN",
    "TOKENIZED STOCK",
})


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
    "sweep_high_15m": "扫高",
    "sweep_low_15m": "扫低",
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


def _structure_label(event: Mapping[str, Any]) -> str:
    if str(event.get("event") or "") == "continuation":
        return "顺势突破"
    return (
        "结构转多"
        if str(event.get("direction") or "") == "bullish"
        else "结构转空"
    )


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
    """Render a closed-candle Binance 1h SMC reference chart in memory."""

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

    # Only the most recent 288 closed hourly bars are drawn. Extra prehistory
    # may still be supplied so delayed pivots are confirmed without repainting.
    try:
        overlay = build_smc_overlay(
            normalized,
            allow_session_gaps=(
                str(asset_category or "").strip().upper()
                in _SESSION_BASED_ASSET_CATEGORIES
            ),
        )
    except ValueError as exc:
        if str(exc) not in {
            "smc_overlay_candle_cadence_invalid",
            "smc_overlay_candle_gap",
        }:
            raise
        # A data hole must not suppress the whole launch update. Keep the raw
        # closed-candle chart, but fail closed on derived SMC layers.
        overlay = {
            "status": "degraded_discontinuous_input",
            "structure_events": [],
            "active_order_blocks": [],
            "valuation": {"data_status": "insufficient_history", "zones": {}},
        }
    visible = normalized[-288:]
    first_close_ts = visible[0]["close_ts"]
    last_close_ts = visible[-1]["close_ts"]
    canvas = Canvas(width, height, (255, 255, 255))
    ink = (35, 39, 45)
    muted = (91, 99, 110)
    grid = (202, 207, 214)
    plot_left = 10
    plot_right = width - 72
    context_width = max(112, round((plot_right - plot_left) * 0.16))
    candle_right = plot_right - context_width
    context_left = candle_right + 8
    price_top = 50
    price_bottom = height - 35

    # Header deliberately stays compact on Telegram mobile previews.
    header_symbol = str(symbol or "UNKNOWN").upper()
    canvas.text(10, 10, f"{header_symbol}.P", ink, scale=1)
    category_x = min(width - 280, 16 + len(header_symbol) * 6)
    canvas.ui_text(
        category_x,
        6,
        f"· {_chart_category_label(asset_category or 'USD-M PERP')} · 1小时 · 币安",
        ink,
    )
    latest = visible[-1]
    candle_change = (
        (latest["close"] / latest["open"] - 1.0) * 100.0
        if latest["open"] > 0
        else 0.0
    )
    change_color = (11, 158, 132) if candle_change >= 0 else (243, 55, 76)
    header_stats = (
        f"开 {_format_price(latest['open'])}  高 {_format_price(latest['high'])}  "
        f"低 {_format_price(latest['low'])}  收 {_format_price(latest['close'])}  "
        f"{candle_change:+.2f}%"
    )
    stats_x = min(max(310, width // 3), max(310, width - 360))
    canvas.ui_text(stats_x, 6, header_stats, change_color)
    canvas.ui_text(10, 25, "真实数据 · 仅使用已收线K线", muted)

    for index in range(6):
        y = price_top + round((price_bottom - price_top) * index / 5)
        _dotted_horizontal(canvas, plot_left, plot_right, y, grid)
    for index in range(9):
        x = plot_left + round((plot_right - plot_left) * index / 8)
        _dotted_vertical(canvas, x, price_top, price_bottom, grid)

    blocks = [
        item
        for item in overlay.get("active_order_blocks", [])
        if isinstance(item, Mapping)
        and first_close_ts <= int(_number(item.get("origin_ts"))) <= last_close_ts
        and _number(item.get("zone_high")) > _number(item.get("zone_low")) > 0
        and (
            _number(item.get("zone_high")) - _number(item.get("zone_low"))
        ) / (
            (_number(item.get("zone_high")) + _number(item.get("zone_low")))
            / 2.0
        ) <= 0.055
    ]
    valuation = overlay.get("valuation")
    valuation = valuation if isinstance(valuation, Mapping) else {}
    overlay_prices: list[float] = []
    for block in blocks:
        overlay_prices.extend([
            _number(block.get("zone_low")),
            _number(block.get("zone_high")),
        ])
    overlay_prices.extend([
        _number(valuation.get("range_low")),
        _number(valuation.get("range_high")),
    ])
    overlay_prices = [value for value in overlay_prices if value > 0]
    lowest = min([item["low"] for item in visible] + overlay_prices)
    highest = max([item["high"] for item in visible] + overlay_prices)
    span = max(highest - lowest, highest * 0.001)
    lowest -= span * 0.06
    highest += span * 0.06
    span = highest - lowest

    def price_y(value: float) -> int:
        ratio = (highest - value) / span
        return price_top + round(ratio * (price_bottom - price_top))

    candle_count = len(visible)
    slot = (candle_right - plot_left) / max(1, candle_count)
    body_half = max(1, min(4, int(slot * 0.34)))
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
            key=lambda position: abs(
                visible[position]["close_ts"] - timestamp
            ),
        )

    # The latest 72-hour valuation is shown in a reserved non-time context
    # panel to the right of the last candle. It never paints historical bars.
    zones = valuation.get("zones")
    if isinstance(zones, Mapping) and valuation.get("data_status") == "complete":
        zone_x0 = context_left
        for key, fill, text_color, label in (
            ("high", (255, 244, 184), (221, 177, 14), "高估"),
            ("mid", (229, 232, 236), (119, 126, 136), "中间价"),
            ("low", (207, 222, 253), (65, 126, 220), "低估"),
        ):
            zone = zones.get(key)
            if not isinstance(zone, Mapping):
                continue
            zone_low = _number(zone.get("low"))
            zone_high = _number(zone.get("high"))
            if zone_high <= zone_low:
                continue
            y0 = price_y(zone_high)
            y1 = price_y(zone_low)
            canvas.rect(zone_x0, min(y0, y1), plot_right, max(y0, y1), fill)
            label_x = zone_x0 + max(
                4,
                (plot_right - zone_x0 - canvas.ui_text_width(label)) // 2,
            )
            label_y = (y0 + y1 - GLYPH_HEIGHT) // 2
            canvas.ui_text(label_x, label_y, label, text_color)

    # Active order blocks extend to the current candle edge, but never enter
    # the independent valuation panel on the right.
    block_specs = sorted(
        blocks,
        key=lambda item: int(_number(item.get("origin_ts"))),
    )[-5:]
    for block in block_specs:
        origin_ts = int(_number(block.get("origin_ts")))
        x0 = x_positions[nearest_index(max(first_close_ts, origin_ts))]
        y0 = price_y(_number(block.get("zone_high")))
        y1 = price_y(_number(block.get("zone_low")))
        bearish = str(block.get("direction") or "") == "bearish"
        fill = (249, 209, 219) if bearish else (199, 235, 226)
        border = (237, 130, 157) if bearish else (80, 183, 160)
        canvas.rect(x0, min(y0, y1), candle_right, max(y0, y1), fill)
        canvas.line(x0, min(y0, y1), candle_right, min(y0, y1), border)

    for index, candle in enumerate(visible):
        x = x_positions[index]
        up = candle["close"] >= candle["open"]
        color = (24, 183, 164) if up else (255, 64, 82)
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

    # Show only major swing structure. Consecutive same-direction events are
    # compressed to their first/last member to avoid the old label clutter.
    raw_events = [
        item
        for item in overlay.get("structure_events", [])
        if isinstance(item, Mapping)
        and str(item.get("kind") or "") == "swing"
        and int(_number(item.get("origin_ts"))) >= first_close_ts
        and int(_number(item.get("broken_at_ts"))) >= first_close_ts
    ]
    compressed: list[Mapping[str, Any]] = []
    run: list[Mapping[str, Any]] = []
    for event in raw_events:
        if run and event.get("direction") != run[-1].get("direction"):
            compressed.append(run[0])
            if len(run) > 1:
                compressed.append(run[-1])
            run = []
        run.append(event)
    if run:
        compressed.append(run[0])
        if len(run) > 1:
            compressed.append(run[-1])
    for event in compressed[-4:]:
        origin_ts = int(_number(event.get("origin_ts")))
        broken_ts = int(_number(event.get("broken_at_ts")))
        x0 = x_positions[nearest_index(origin_ts)]
        x1 = x_positions[nearest_index(broken_ts)]
        y = price_y(_number(event.get("level")))
        bullish = str(event.get("direction") or "") == "bullish"
        color = (5, 151, 126) if bullish else (236, 70, 109)
        canvas.line(x0, y, x1, y, color)
        label = _structure_label(event)
        label_x = max(
            plot_left,
            min((x0 + x1 - canvas.ui_text_width(label)) // 2, plot_right - 60),
        )
        label_y = y - GLYPH_HEIGHT - 2 if bullish else y + 2
        canvas.ui_text(label_x, label_y, label, color)

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

    for index in range(6):
        value = highest - span * index / 5
        y = price_top + round((price_bottom - price_top) * index / 5) - 5
        canvas.text(plot_right + 5, y, _format_price(value), ink, scale=1)

    first_time = datetime.fromtimestamp(first_close_ts, CST)
    last_time = datetime.fromtimestamp(last_close_ts, CST)
    canvas.text(plot_left, height - 18, first_time.strftime("%m-%d %H:%M"), muted, scale=1)
    canvas.text(
        candle_right - 66,
        height - 18,
        last_time.strftime("%m-%d %H:%M"),
        muted,
        scale=1,
    )
    return _encode_png(canvas)


__all__ = [
    "PNG_SIGNATURE",
    "render_launch_chart_png",
]
