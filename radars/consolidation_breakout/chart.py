from __future__ import annotations

import binascii
import math
import struct
import zlib
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from radars.pulse.chart import Canvas, CHART_COLORS, PNG_SIGNATURE


CST = timezone(timedelta(hours=8))
RANGE_CANDLE_LIMIT = 264
THREE_PUSH_CANDLE_LIMIT = 120

ANNOTATION_COLORS = {
    "box": (60, 135, 181),
    "box_fill": (18, 48, 72),
    "event_up": (38, 166, 154),
    "event_down": (242, 54, 69),
    "event_neutral": (246, 189, 22),
    "price_push": (73, 143, 255),
    "macd": (42, 157, 244),
    "macd_push": (246, 189, 22),
    "neckline": (207, 106, 255),
    "invalidation": (255, 92, 92),
    "volume_push": (246, 189, 22),
}

EVENT_LABELS = {
    "breakout_up": "BRK UP",
    "breakout_down": "BRK DOWN",
    "strong_breakout_up": "STRONG BRK UP",
    "strong_breakout_down": "STRONG BRK DOWN",
    "retest_up": "RETEST UP",
    "retest_down": "RETEST DOWN",
    "fake_breakout": "FAKE BRK UP",
    "fake_breakdown": "FAKE BRK DOWN",
    "upper_sweep": "SWEEP H",
    "lower_sweep": "SWEEP L",
    "three_push_top_forming": "3 PUSH TOP FORMING",
    "three_push_bottom_forming": "3 PUSH BOTTOM FORMING",
    "three_push_top_confirmed": "3 PUSH TOP CONFIRMED",
    "three_push_bottom_confirmed": "3 PUSH BOTTOM CONFIRMED",
}


def _number(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def _integer(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0


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
    header = struct.pack(
        ">IIBBBBB",
        canvas.width,
        canvas.height,
        8,
        2,
        0,
        0,
        0,
    )
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


def _dotted_horizontal(
    canvas: Canvas,
    x0: int,
    x1: int,
    y: int,
    color: tuple[int, int, int],
    *,
    dot: int = 3,
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
    dot: int = 3,
    gap: int = 5,
) -> None:
    for y in range(y0, y1 + 1, max(1, dot + gap)):
        canvas.line(x, y, x, min(y + dot, y1), color)


def _event_color(event_name: str, direction: str) -> tuple[int, int, int]:
    if event_name in {"upper_sweep", "lower_sweep"}:
        return ANNOTATION_COLORS["event_neutral"]
    if direction == "up" or event_name.endswith("bottom_confirmed"):
        return ANNOTATION_COLORS["event_up"]
    if direction == "down" or event_name.endswith("top_confirmed"):
        return ANNOTATION_COLORS["event_down"]
    return ANNOTATION_COLORS["event_neutral"]


def _normalize_payload(
    event: Mapping[str, Any],
    chart_payload: Mapping[str, Any],
) -> tuple[list[dict[str, float | int]], list[float], int]:
    raw_candles = chart_payload.get("candles")
    raw_macd = chart_payload.get("macd")
    if not isinstance(raw_candles, (list, tuple)):
        raise ValueError("chart payload candles must be a sequence")
    if not isinstance(raw_macd, (list, tuple)) or len(raw_macd) != len(raw_candles):
        raise ValueError("chart payload macd must align with candles")

    event_close_time = _integer(event.get("close_time") or event.get("event_time"))
    if event_close_time <= 0:
        raise ValueError("event close_time is required")

    by_close_time: dict[int, tuple[dict[str, float | int], float]] = {}
    for raw, macd_value in zip(raw_candles, raw_macd):
        if not isinstance(raw, Mapping):
            continue
        close_time = _integer(raw.get("close_time"))
        open_time = _integer(raw.get("open_time"))
        open_price = _number(raw.get("open"))
        high_price = _number(raw.get("high"))
        low_price = _number(raw.get("low"))
        close_price = _number(raw.get("close"))
        volume = _number(raw.get("volume"))
        macd_number = _number(macd_value)
        if (
            close_time <= 0
            or min(open_price, high_price, low_price, close_price) <= 0
            or high_price < max(open_price, close_price)
            or low_price > min(open_price, close_price)
            or volume < 0
        ):
            continue
        by_close_time[close_time] = ({
            "open_time": open_time,
            "close_time": close_time,
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "volume": volume,
        }, macd_number)

    ordered = [
        (candle, macd_value)
        for close_time, (candle, macd_value) in sorted(by_close_time.items())
        if close_time <= event_close_time
    ]
    if len(ordered) < 5:
        raise ValueError("at least five valid closed candles are required")
    if not any(
        _integer(candle.get("close_time")) == event_close_time
        for candle, _macd in ordered
    ):
        raise ValueError("event close_time is absent from chart candles")
    return (
        [candle for candle, _macd in ordered],
        [macd_value for _candle, macd_value in ordered],
        event_close_time,
    )


def _visible_window(
    event: Mapping[str, Any],
    chart_payload: Mapping[str, Any],
    candles: list[dict[str, float | int]],
    macd: list[float],
) -> tuple[list[dict[str, float | int]], list[float]]:
    event_name = str(event.get("event") or "")
    if event_name.startswith("three_push_"):
        start = max(0, len(candles) - THREE_PUSH_CANDLE_LIMIT)
        return candles[start:], macd[start:]

    box_age = max(0, _integer(event.get("box_age")))
    desired = min(
        RANGE_CANDLE_LIMIT,
        max(80, box_age + 16 if box_age > 0 else 120),
    )
    start = max(0, len(candles) - desired)
    box_start_time = _integer(chart_payload.get("box_start_close_time"))
    if box_start_time > 0:
        box_index = next(
            (
                index
                for index, candle in enumerate(candles)
                if _integer(candle.get("close_time")) >= box_start_time
            ),
            len(candles) - 1,
        )
        start = max(0, box_index - 8)
        start = max(start, len(candles) - RANGE_CANDLE_LIMIT)
        if len(candles) - start < min(80, len(candles)):
            start = max(0, len(candles) - min(80, len(candles)))
    return candles[start:], macd[start:]


def _time_label(timestamp_ms: int, timeframe: str) -> str:
    stamp = datetime.fromtimestamp(timestamp_ms / 1000, CST)
    if str(timeframe or "").strip().lower().endswith("w"):
        return stamp.strftime("%Y-%m")
    return stamp.strftime("%m-%d")


def render_consolidation_chart_png(
    *,
    event: Mapping[str, Any],
    chart_payload: Mapping[str, Any],
    width: int = 1200,
    height: int = 760,
) -> bytes:
    """Render a closed-candle snapshot for one consolidation radar event."""

    if not isinstance(event, Mapping) or not isinstance(chart_payload, Mapping):
        raise ValueError("event and chart_payload must be mappings")
    if not (800 <= int(width) <= 1600 and 600 <= int(height) <= 1000):
        raise ValueError("unsupported chart dimensions")

    candles, macd, event_close_time = _normalize_payload(event, chart_payload)
    visible, visible_macd = _visible_window(
        event,
        chart_payload,
        candles,
        macd,
    )
    if len(visible) < 5:
        raise ValueError("visible chart window is too short")

    colors = CHART_COLORS
    canvas = Canvas(int(width), int(height), colors["background"])
    plot_left = 14
    plot_right = int(width) - 102
    price_top = 36
    time_axis_y = int(height) - 15
    macd_bottom = int(height) - 27
    macd_top = macd_bottom - round(int(height) * 0.19)
    volume_bottom = macd_top - 2
    volume_top = volume_bottom - round(int(height) * 0.13)
    price_bottom = volume_top - 2
    if price_bottom - price_top < 220:
        raise ValueError("chart height leaves no readable price panel")

    event_name = str(event.get("event") or "")
    direction = str(event.get("direction") or "")
    event_color = _event_color(event_name, direction)
    symbol = str(event.get("symbol") or "UNKNOWN").upper()[:18]
    timeframe = str(event.get("timeframe") or "").upper()[:6]
    event_label = EVENT_LABELS.get(event_name, "EVENT")
    canvas.text(
        plot_left + 4,
        8,
        f"{symbol}  {timeframe}  BINANCE  {event_label}",
        colors["ink"],
        scale=1,
    )
    close_stamp = datetime.fromtimestamp(event_close_time / 1000, CST).strftime(
        "%Y-%m-%d %H:%M CST"
    )
    close_header = f"CLOSE {close_stamp}"
    canvas.text(
        max(plot_left, plot_right - len(close_header) * 6),
        20,
        close_header,
        colors["muted"],
        scale=1,
    )

    event_close_index = len(visible) - 1
    candle_count = len(visible)
    slot = (plot_right - plot_left) / max(1, candle_count)
    x_positions = [
        plot_left + round((index + 0.5) * slot)
        for index in range(candle_count)
    ]
    body_half = max(1, min(4, round(slot * 0.32)))
    timestamp_to_index = {
        _integer(candle.get("close_time")): index
        for index, candle in enumerate(visible)
    }

    box_upper = _number(event.get("box_upper"))
    box_lower = _number(event.get("box_lower"))
    valid_box = box_upper > box_lower > 0
    push_prices = event.get("push_prices")
    price_points = (
        [_number(value) for value in push_prices]
        if isinstance(push_prices, (list, tuple)) and len(push_prices) == 3
        else []
    )
    neckline = _number(event.get("neckline"))
    invalidation = _number(event.get("invalidation"))
    price_references: list[float] = []
    if valid_box:
        price_references.extend([box_upper, box_lower])
    price_references.extend(value for value in price_points if value > 0)
    if neckline > 0:
        price_references.append(neckline)
    if invalidation > 0:
        price_references.append(invalidation)
    raw_low = min(
        [_number(candle.get("low")) for candle in visible] + price_references
    )
    raw_high = max(
        [_number(candle.get("high")) for candle in visible] + price_references
    )
    raw_span = max(raw_high - raw_low, raw_high * 0.001, 1e-9)
    price_low = raw_low - raw_span * 0.07
    price_high = raw_high + raw_span * 0.07
    price_span = price_high - price_low

    def price_y(value: float) -> int:
        ratio = (price_high - value) / price_span
        return price_top + round(ratio * (price_bottom - price_top))

    for grid_index in range(5):
        y = price_top + round((price_bottom - price_top) * grid_index / 4)
        canvas.line(plot_left, y, plot_right, y, colors["grid"])
        value = price_high - price_span * grid_index / 4
        canvas.text(
            plot_right + 7,
            y - 4,
            _format_price(value),
            colors["muted"],
            scale=1,
        )

    grid_columns = 6
    for grid_index in range(grid_columns):
        position = round((candle_count - 1) * grid_index / (grid_columns - 1))
        x = x_positions[position]
        canvas.line(x, price_top, x, macd_bottom, colors["grid"])
        label = _time_label(
            _integer(visible[position].get("close_time")),
            timeframe,
        )
        canvas.text(
            max(plot_left, min(plot_right - len(label) * 6, x - len(label) * 3)),
            time_axis_y,
            label,
            colors["muted"],
            scale=1,
        )

    box_start_index = 0
    box_start_time = _integer(chart_payload.get("box_start_close_time"))
    box_extent_known = box_start_time > 0 or _integer(event.get("box_age")) > 0
    if valid_box and box_extent_known:
        if box_start_time > 0:
            box_start_index = next(
                (
                    index
                    for index, candle in enumerate(visible)
                    if _integer(candle.get("close_time")) >= box_start_time
                ),
                0,
            )
        elif _integer(event.get("box_age")) > 0:
            box_start_index = max(
                0,
                event_close_index - _integer(event.get("box_age")),
            )
        box_x0 = x_positions[box_start_index]
        box_x1 = x_positions[event_close_index]
        box_y0 = price_y(box_upper)
        box_y1 = price_y(box_lower)
        canvas.alpha_rect(
            box_x0,
            min(box_y0, box_y1),
            box_x1,
            max(box_y0, box_y1),
            ANNOTATION_COLORS["box_fill"],
            105,
        )

    breakout_start_time = _integer(chart_payload.get("breakout_start_close_time"))
    if breakout_start_time in timestamp_to_index:
        breakout_x = x_positions[timestamp_to_index[breakout_start_time]]
        _dotted_vertical(
            canvas,
            breakout_x,
            price_top,
            macd_bottom,
            colors["muted"],
            dot=2,
            gap=6,
        )
        canvas.text(
            max(plot_left, breakout_x - 15),
            price_top + 4,
            "BREAK",
            colors["muted"],
            scale=1,
        )

    event_x = x_positions[event_close_index]
    _dotted_vertical(
        canvas,
        event_x,
        price_top,
        macd_bottom,
        event_color,
        dot=3,
        gap=5,
    )

    for index, candle in enumerate(visible):
        x = x_positions[index]
        open_price = _number(candle.get("open"))
        high_price = _number(candle.get("high"))
        low_price = _number(candle.get("low"))
        close_price = _number(candle.get("close"))
        rising = close_price >= open_price
        candle_color = (
            colors["price_rising"] if rising else colors["price_falling"]
        )
        canvas.line(x, price_y(high_price), x, price_y(low_price), candle_color)
        open_y = price_y(open_price)
        close_y = price_y(close_price)
        canvas.rect(
            x - body_half,
            min(open_y, close_y),
            x + body_half,
            max(min(open_y, close_y) + 1, max(open_y, close_y)),
            candle_color,
        )

    if valid_box:
        box_x0 = x_positions[box_start_index]
        box_x1 = x_positions[event_close_index]
        for label, value in (("BOX H", box_upper), ("BOX L", box_lower)):
            y = price_y(value)
            canvas.line(box_x0, y, box_x1, y, ANNOTATION_COLORS["box"])
            label_x = min(
                box_x1 - len(label) * 6 - 3,
                max(box_x0 + 3, plot_left + 3),
            )
            canvas.text(
                max(plot_left, label_x),
                max(price_top, y - 9),
                label,
                ANNOTATION_COLORS["box"],
                scale=1,
            )

    event_badge = "EVENT"
    event_badge_width = len(event_badge) * 6 + 8
    badge_x = max(
        plot_left,
        min(plot_right - event_badge_width, event_x - event_badge_width // 2),
    )
    canvas.rect(
        badge_x,
        price_top + 4,
        badge_x + event_badge_width,
        price_top + 17,
        event_color,
    )
    canvas.text(
        badge_x + 4,
        price_top + 7,
        event_badge,
        (255, 255, 255),
        scale=1,
    )

    push_times = event.get("push_close_times")
    push_indices: list[int] = []
    if (
        event_name.startswith("three_push_")
        and isinstance(push_times, (list, tuple))
        and len(push_times) == 3
        and len(price_points) == 3
    ):
        candidate_indices = [
            timestamp_to_index.get(_integer(timestamp), -1)
            for timestamp in push_times
        ]
        if all(index >= 0 for index in candidate_indices):
            push_indices = candidate_indices
            point_specs = [
                (
                    x_positions[index],
                    price_y(price_points[number]),
                )
                for number, index in enumerate(push_indices)
            ]
            for first, second in zip(point_specs, point_specs[1:]):
                canvas.line(
                    first[0],
                    first[1],
                    second[0],
                    second[1],
                    ANNOTATION_COLORS["price_push"],
                )
            structure = str(event.get("structure") or "")
            for number, (x, y) in enumerate(point_specs, start=1):
                canvas.rect(
                    x - 3,
                    y - 3,
                    x + 3,
                    y + 3,
                    ANNOTATION_COLORS["price_push"],
                )
                label_y = y - 14 if structure == "top" else y + 7
                label_y = max(price_top + 20, min(price_bottom - 10, label_y))
                canvas.text(
                    max(plot_left, min(plot_right - 12, x - 6)),
                    label_y,
                    f"P{number}",
                    ANNOTATION_COLORS["price_push"],
                    scale=1,
                )

            line_start_x = point_specs[0][0]
            if neckline > 0:
                neck_y = price_y(neckline)
                _dotted_horizontal(
                    canvas,
                    line_start_x,
                    event_x,
                    neck_y,
                    ANNOTATION_COLORS["neckline"],
                )
                canvas.text(
                    max(line_start_x, event_x - len("NECK") * 6 - 3),
                    max(price_top, neck_y - 9),
                    "NECK",
                    ANNOTATION_COLORS["neckline"],
                    scale=1,
                )
            if invalidation > 0:
                invalid_y = price_y(invalidation)
                _dotted_horizontal(
                    canvas,
                    point_specs[-1][0],
                    event_x,
                    invalid_y,
                    ANNOTATION_COLORS["invalidation"],
                )
                canvas.text(
                    max(
                        point_specs[-1][0],
                        event_x - len("INVALID") * 6 - 3,
                    ),
                    max(price_top, invalid_y - 9),
                    "INVALID",
                    ANNOTATION_COLORS["invalidation"],
                    scale=1,
                )

    canvas.line(0, price_bottom, int(width) - 1, price_bottom, colors["separator"])
    canvas.line(0, volume_bottom, int(width) - 1, volume_bottom, colors["separator"])
    canvas.line(0, macd_bottom, int(width) - 1, macd_bottom, colors["separator"])

    maximum_volume = max(_number(candle.get("volume")) for candle in visible)
    volume_span = max(maximum_volume, 1.0)
    volume_height = max(1, volume_bottom - volume_top - 18)
    canvas.text(
        plot_left + 4,
        volume_top + 5,
        f"VOL {_format_volume(_number(visible[-1].get('volume')))}",
        colors["muted"],
        scale=1,
    )
    for index, candle in enumerate(visible):
        x = x_positions[index]
        value = _number(candle.get("volume"))
        bar_top = volume_bottom - round(value / volume_span * volume_height)
        rising = _number(candle.get("close")) >= _number(candle.get("open"))
        volume_color = colors["rising"] if rising else colors["falling"]
        canvas.rect(
            x - body_half,
            max(volume_top + 15, bar_top),
            x + body_half,
            volume_bottom - 1,
            volume_color,
        )
        if index in push_indices:
            canvas.rect(
                x - 3,
                max(volume_top + 15, bar_top) - 3,
                x + 3,
                max(volume_top + 15, bar_top) + 2,
                ANNOTATION_COLORS["volume_push"],
            )

    macd_low = min(visible_macd + [0.0])
    macd_high = max(visible_macd + [0.0])
    macd_raw_span = max(
        macd_high - macd_low,
        max(abs(macd_high), abs(macd_low)) * 0.001,
        1e-9,
    )
    macd_low -= macd_raw_span * 0.10
    macd_high += macd_raw_span * 0.10
    macd_span = macd_high - macd_low

    def macd_y(value: float) -> int:
        ratio = (macd_high - value) / macd_span
        return macd_top + 18 + round(
            ratio * max(1, macd_bottom - macd_top - 22)
        )

    canvas.text(
        plot_left + 4,
        macd_top + 5,
        "MACD 12-26",
        colors["muted"],
        scale=1,
    )
    zero_y = macd_y(0.0)
    _dotted_horizontal(
        canvas,
        plot_left,
        plot_right,
        zero_y,
        colors["grid"],
        dot=2,
        gap=4,
    )
    macd_points = [
        (x_positions[index], macd_y(value))
        for index, value in enumerate(visible_macd)
    ]
    for first, second in zip(macd_points, macd_points[1:]):
        canvas.line(
            first[0],
            first[1],
            second[0],
            second[1],
            ANNOTATION_COLORS["macd"],
        )

    push_macd_times = event.get("push_macd_close_times")
    push_macd_values = event.get("push_macd")
    if (
        event_name.startswith("three_push_")
        and isinstance(push_macd_times, (list, tuple))
        and len(push_macd_times) == 3
        and isinstance(push_macd_values, (list, tuple))
        and len(push_macd_values) == 3
    ):
        marker_specs: list[tuple[int, int]] = []
        for timestamp, value in zip(push_macd_times, push_macd_values):
            index = timestamp_to_index.get(_integer(timestamp), -1)
            if index < 0:
                marker_specs = []
                break
            marker_specs.append((x_positions[index], macd_y(_number(value))))
        for first, second in zip(marker_specs, marker_specs[1:]):
            canvas.line(
                first[0],
                first[1],
                second[0],
                second[1],
                ANNOTATION_COLORS["macd_push"],
            )
        for number, (x, y) in enumerate(marker_specs, start=1):
            canvas.rect(
                x - 3,
                y - 3,
                x + 3,
                y + 3,
                ANNOTATION_COLORS["macd_push"],
            )
            canvas.text(
                max(plot_left, min(plot_right - 12, x - 6)),
                max(macd_top + 16, min(macd_bottom - 10, y - 13)),
                f"M{number}",
                ANNOTATION_COLORS["macd_push"],
                scale=1,
            )

    current_price = _number(visible[-1].get("close"))
    current_y = price_y(current_price)
    _dotted_horizontal(
        canvas,
        plot_left,
        plot_right,
        current_y,
        colors["separator"],
        dot=1,
        gap=3,
    )
    price_badge = f"C {_format_price(current_price)}"
    badge_width = len(price_badge) * 6 + 8
    canvas.rect(
        plot_right,
        current_y - 8,
        min(int(width) - 1, plot_right + badge_width),
        current_y + 8,
        colors["price_badge"],
    )
    canvas.text(
        plot_right + 4,
        current_y - 4,
        price_badge,
        colors["background"],
        scale=1,
    )
    return _encode_png(canvas)


__all__ = [
    "ANNOTATION_COLORS",
    "PNG_SIGNATURE",
    "RANGE_CANDLE_LIMIT",
    "THREE_PUSH_CANDLE_LIMIT",
    "render_consolidation_chart_png",
]
