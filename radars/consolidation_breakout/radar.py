from __future__ import annotations

import copy
import math
import time
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from itertools import product
from pathlib import Path
from typing import Any, Iterable

from config import Settings
from shared.binance_data import BinanceDataSource
from shared.storage import JsonStore


TEMPLATE_ID = "TG_CONSOLIDATION_BREAKOUT"
STATE_SCHEMA_VERSION = 1
CST = timezone(timedelta(hours=8))

ATR_PERIOD = 14
TOUCH_TOLERANCE_ATR = 0.20
ENDPOINT_DRIFT_ATR = 0.35
BREAKOUT_BUFFER_ATR = 0.10
REENTRY_BUFFER_ATR = 0.05
FAKEOUT_BARS = 3
RETEST_BARS = 12
THREE_PUSH_PIVOT_LEFT = 2
THREE_PUSH_PIVOT_RIGHT = 2
THREE_PUSH_MACD_PIVOT_LEFT = 2
THREE_PUSH_MACD_PIVOT_RIGHT = 2
THREE_PUSH_MACD_ALIGNMENT_BARS = 2
THREE_PUSH_LOOKBACK_BARS = 96
THREE_PUSH_PULLBACK_ATR = 0.50
THREE_PUSH_PRICE_STEP_ATR = 0.10
THREE_PUSH_CONFIRM_BUFFER_ATR = 0.05
THREE_PUSH_INVALIDATION_BUFFER_ATR = 0.10
THREE_PUSH_MAX_CONFIRM_BARS = 12
THREE_PUSH_MIN_MACD_LEG_WEAKENING = 0.05
THREE_PUSH_RULE_VERSION = 2
THREE_PUSH_EDGE_TOLERANCE_ATR = 0.50
CHART_HISTORY_LIMIT = 264


@dataclass(frozen=True)
class HorizonSpec:
    name: str
    label: str
    length: int
    max_width_atr: float
    max_width_pct: float
    max_efficiency: float
    stability: int
    cooldown: int
    maximum_age: int
    rank: int


HORIZONS = (
    HorizonSpec("short", "短期", 24, 4.5, 8.0, 0.35, 3, 5, 120, 1),
    HorizonSpec("medium", "中期", 72, 9.0, 18.0, 0.30, 5, 8, 360, 2),
    HorizonSpec("long", "长期", 240, 18.0, 35.0, 0.25, 8, 12, 0, 3),
)

EVENT_PRIORITY = {
    "upper_sweep": 1,
    "lower_sweep": 1,
    "retest_up": 2,
    "retest_down": 2,
    "breakout_up": 3,
    "breakout_down": 3,
    "strong_breakout_up": 4,
    "strong_breakout_down": 4,
    "fake_breakout": 5,
    "fake_breakdown": 5,
    "three_push_top_forming": 6,
    "three_push_bottom_forming": 6,
    "three_push_top_confirmed": 7,
    "three_push_bottom_confirmed": 7,
}

EVENT_LABELS = {
    "breakout_up": ("🚀", "向上突破"),
    "breakout_down": ("📉", "向下跌破"),
    "strong_breakout_up": ("🔥", "放量向上突破"),
    "strong_breakout_down": ("🧊", "放量向下跌破"),
    "retest_up": ("✅", "突破后回踩确认"),
    "retest_down": ("✅", "跌破后反抽确认"),
    "fake_breakout": ("⚠️", "假突破"),
    "fake_breakdown": ("⚠️", "假跌破"),
    "upper_sweep": ("🧹", "上沿扫流动性"),
    "lower_sweep": ("🧹", "下沿扫流动性"),
    "three_push_top_forming": ("🔺", "三推顶背离形成中"),
    "three_push_bottom_forming": ("🔻", "三推底背离形成中"),
    "three_push_top_confirmed": ("🔴", "三推顶背离确认"),
    "three_push_bottom_confirmed": ("🟢", "三推底背离确认"),
}


@dataclass(frozen=True)
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int

    @classmethod
    def from_binance(cls, row: Any) -> Candle | None:
        if not isinstance(row, (list, tuple)) or len(row) < 7:
            return None
        try:
            candle = cls(
                open_time=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=max(0.0, float(row[5])),
                close_time=int(row[6]),
            )
        except (TypeError, ValueError, OverflowError):
            return None
        values = (candle.open, candle.high, candle.low, candle.close, candle.volume)
        if not all(math.isfinite(value) for value in values):
            return None
        if candle.close_time <= 0 or candle.high < candle.low or candle.close <= 0:
            return None
        return candle


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _timeframe_ms(timeframe: str) -> int:
    value = str(timeframe or "").strip().lower()
    if len(value) < 2:
        return 0
    try:
        amount = int(value[:-1])
    except ValueError:
        return 0
    unit = value[-1]
    multiplier = {
        "m": 60_000,
        "h": 3_600_000,
        "d": 86_400_000,
        "w": 7 * 86_400_000,
    }.get(unit, 0)
    return max(0, amount) * multiplier


def _cluster_count(flags: Iterable[bool]) -> int:
    """Count separated touch clusters; adjacent touch bars count only once."""

    count = 0
    touching = False
    for flag in flags:
        active = bool(flag)
        if active and not touching:
            count += 1
        touching = active
    return count


def count_touch_clusters(
    candles: list[Candle],
    *,
    upper: float,
    lower: float,
    tolerance: float,
) -> tuple[int, int]:
    tolerance = max(0.0, tolerance)
    upper_count = _cluster_count(
        candle.high >= upper - tolerance for candle in candles
    )
    lower_count = _cluster_count(
        candle.low <= lower + tolerance for candle in candles
    )
    return upper_count, lower_count


def _atr(candles: list[Candle], end_index: int, period: int = ATR_PERIOD) -> float:
    if end_index <= 0 or end_index >= len(candles):
        return 0.0
    start = max(1, end_index - max(1, period) + 1)
    values: list[float] = []
    for index in range(start, end_index + 1):
        candle = candles[index]
        previous_close = candles[index - 1].close
        values.append(max(
            candle.high - candle.low,
            abs(candle.high - previous_close),
            abs(candle.low - previous_close),
        ))
    return sum(values) / len(values) if values else 0.0


def _path_efficiency(candles: list[Candle]) -> float:
    if len(candles) < 2:
        return 1.0
    travelled = sum(
        abs(candles[index].close - candles[index - 1].close)
        for index in range(1, len(candles))
    )
    if travelled <= 0:
        return 0.0
    return abs(candles[-1].close - candles[0].close) / travelled


def _box_candidate(
    candles: list[Candle],
    current_index: int,
    spec: HorizonSpec,
) -> dict[str, Any] | None:
    """Build a confirmed range from bars strictly preceding ``current_index``."""

    previous_end = current_index - 1
    earliest_start = previous_end - spec.length + 1 - (spec.stability - 1)
    if earliest_start < 0 or previous_end <= 0:
        return None
    main_start = previous_end - spec.length + 1
    window = candles[main_start:previous_end + 1]
    if len(window) != spec.length:
        return None

    atr = _atr(candles, previous_end)
    if atr <= 0:
        return None
    upper = max(candle.high for candle in window)
    lower = min(candle.low for candle in window)
    width = upper - lower
    midpoint = (upper + lower) / 2.0
    if width <= 0 or midpoint <= 0:
        return None
    width_atr = width / atr
    width_pct = width / midpoint * 100.0
    efficiency = _path_efficiency(window)
    if (
        width_atr > spec.max_width_atr
        or width_pct > spec.max_width_pct
        or efficiency > spec.max_efficiency
    ):
        return None

    max_drift = ENDPOINT_DRIFT_ATR * atr
    for shift in range(1, spec.stability):
        shifted_end = previous_end - shift
        shifted_start = shifted_end - spec.length + 1
        shifted = candles[shifted_start:shifted_end + 1]
        if len(shifted) != spec.length:
            return None
        shifted_upper = max(candle.high for candle in shifted)
        shifted_lower = min(candle.low for candle in shifted)
        if abs(shifted_upper - upper) > max_drift:
            return None
        if abs(shifted_lower - lower) > max_drift:
            return None

    upper_touches, lower_touches = count_touch_clusters(
        window,
        upper=upper,
        lower=lower,
        tolerance=TOUCH_TOLERANCE_ATR * atr,
    )
    if upper_touches < 2 or lower_touches < 2:
        return None
    return {
        "upper": upper,
        "lower": lower,
        "atr": atr,
        "width_atr": width_atr,
        "width_pct": width_pct,
        "efficiency": efficiency,
        "upper_touches": upper_touches,
        "lower_touches": lower_touches,
        "formed_close_time": candles[current_index].close_time,
        "active_bars": 0,
        "base_bars": spec.length,
        "upper_sweep_sent": False,
        "lower_sweep_sent": False,
    }


def _volume_ratio(candles: list[Candle], current_index: int) -> float:
    start = max(0, current_index - 20)
    baseline = [candle.volume for candle in candles[start:current_index]]
    average = sum(baseline) / len(baseline) if baseline else 0.0
    if average <= 0:
        return 0.0
    return candles[current_index].volume / average


def _ema_series(values: list[float], period: int) -> list[float]:
    if not values:
        return []
    alpha = 2.0 / (max(1, period) + 1.0)
    result = [float(values[0])]
    for value in values[1:]:
        result.append(result[-1] + alpha * (float(value) - result[-1]))
    return result


def _macd_line(candles: list[Candle]) -> list[float]:
    closes = [candle.close for candle in candles]
    fast = _ema_series(closes, 12)
    slow = _ema_series(closes, 26)
    return [fast_value - slow_value for fast_value, slow_value in zip(fast, slow)]


def _is_price_pivot(
    candles: list[Candle],
    index: int,
    *,
    structure: str,
) -> bool:
    left = THREE_PUSH_PIVOT_LEFT
    right = THREE_PUSH_PIVOT_RIGHT
    if index - left < 0 or index + right >= len(candles):
        return False
    if structure == "top":
        value = candles[index].high
        return all(
            value > candles[neighbor].high
            for neighbor in range(index - left, index + right + 1)
            if neighbor != index
        )
    value = candles[index].low
    return all(
        value < candles[neighbor].low
        for neighbor in range(index - left, index + right + 1)
        if neighbor != index
    )


def _is_macd_pivot(
    values: list[float],
    index: int,
    *,
    structure: str,
) -> bool:
    left = THREE_PUSH_MACD_PIVOT_LEFT
    right = THREE_PUSH_MACD_PIVOT_RIGHT
    if index - left < 0 or index + right >= len(values):
        return False
    value = values[index]
    neighbors = (
        values[neighbor]
        for neighbor in range(index - left, index + right + 1)
        if neighbor != index
    )
    if structure == "top":
        return all(value > neighbor for neighbor in neighbors)
    return all(value < neighbor for neighbor in neighbors)


def _aligned_macd_pivots(
    macd: list[float],
    price_indices: list[int],
    current_index: int,
    *,
    structure: str,
) -> tuple[list[int], list[float], list[float]] | None:
    candidate_groups: list[list[int]] = []
    latest_confirmed = current_index - THREE_PUSH_MACD_PIVOT_RIGHT
    for price_index in price_indices:
        start = max(
            THREE_PUSH_MACD_PIVOT_LEFT,
            price_index - THREE_PUSH_MACD_ALIGNMENT_BARS,
        )
        end = min(
            latest_confirmed,
            price_index + THREE_PUSH_MACD_ALIGNMENT_BARS,
        )
        candidates = [
            index
            for index in range(start, end + 1)
            if _is_macd_pivot(macd, index, structure=structure)
        ]
        if not candidates:
            return None
        candidate_groups.append(candidates)

    matches: list[tuple[tuple[float, ...], list[int], list[float], list[float]]] = []
    for raw_indices in product(*candidate_groups):
        indices = [int(value) for value in raw_indices]
        if not (indices[0] < indices[1] < indices[2]):
            continue
        values = [macd[index] for index in indices]
        base = abs(values[0])
        if base <= 0:
            continue
        if structure == "top":
            if not all(value > 0 for value in values):
                continue
            weakening = [
                (values[0] - values[1]) / base,
                (values[1] - values[2]) / base,
            ]
        else:
            if not all(value < 0 for value in values):
                continue
            weakening = [
                (values[1] - values[0]) / base,
                (values[2] - values[1]) / base,
            ]
        if any(
            value < THREE_PUSH_MIN_MACD_LEG_WEAKENING
            for value in weakening
        ):
            continue
        offsets = [
            abs(index - price_index)
            for index, price_index in zip(indices, price_indices)
        ]
        rank = (
            float(sum(offsets)),
            float(max(offsets)),
            *[float(index) for index in indices],
        )
        matches.append((rank, indices, values, weakening))
    if not matches:
        return None
    _rank, indices, values, weakening = min(matches, key=lambda item: item[0])
    return indices, values, weakening


def _three_push_for_structure(
    candles: list[Candle],
    current_index: int,
    macd: list[float],
    *,
    structure: str,
) -> dict[str, Any] | None:
    latest_confirmed = current_index - THREE_PUSH_PIVOT_RIGHT
    if latest_confirmed <= THREE_PUSH_PIVOT_LEFT:
        return None
    first_search = max(
        THREE_PUSH_PIVOT_LEFT,
        current_index - THREE_PUSH_LOOKBACK_BARS + 1,
    )
    pivots = [
        index
        for index in range(first_search, latest_confirmed + 1)
        if _is_price_pivot(candles, index, structure=structure)
    ]
    if len(pivots) < 3:
        return None
    first_index, second_index, third_index = pivots[-3:]
    maximum_formation_lag = (
        THREE_PUSH_MACD_ALIGNMENT_BARS + THREE_PUSH_MACD_PIVOT_RIGHT
    )
    if current_index - third_index > maximum_formation_lag:
        return None
    if second_index - first_index <= THREE_PUSH_PIVOT_RIGHT:
        return None
    if third_index - second_index <= THREE_PUSH_PIVOT_RIGHT:
        return None

    atr = _atr(candles, third_index)
    if atr <= 0:
        return None
    if structure == "top":
        prices = [
            candles[first_index].high,
            candles[second_index].high,
            candles[third_index].high,
        ]
        price_steps = [
            (prices[1] - prices[0]) / atr,
            (prices[2] - prices[1]) / atr,
        ]
        if any(value < THREE_PUSH_PRICE_STEP_ATR for value in price_steps):
            return None
        if any(
            candle.high > prices[2]
            for candle in candles[third_index + 1:current_index + 1]
        ):
            return None
        first_pullback = min(
            candle.low for candle in candles[first_index + 1:second_index]
        )
        second_pullback = min(
            candle.low for candle in candles[second_index + 1:third_index]
        )
        minimum_pullback = THREE_PUSH_PULLBACK_ATR * atr
        if (
            min(prices[0], prices[1]) - first_pullback < minimum_pullback
            or min(prices[1], prices[2]) - second_pullback < minimum_pullback
        ):
            return None
        macd_match = _aligned_macd_pivots(
            macd,
            [first_index, second_index, third_index],
            current_index,
            structure=structure,
        )
        if macd_match is None:
            return None
        macd_indices, macd_values, macd_weakening = macd_match
        neckline = second_pullback
        invalidation = prices[2] + THREE_PUSH_INVALIDATION_BUFFER_ATR * atr
        direction = "down"
        weakening = sum(macd_weakening)
    else:
        prices = [
            candles[first_index].low,
            candles[second_index].low,
            candles[third_index].low,
        ]
        price_steps = [
            (prices[0] - prices[1]) / atr,
            (prices[1] - prices[2]) / atr,
        ]
        if any(value < THREE_PUSH_PRICE_STEP_ATR for value in price_steps):
            return None
        if any(
            candle.low < prices[2]
            for candle in candles[third_index + 1:current_index + 1]
        ):
            return None
        first_pullback = max(
            candle.high for candle in candles[first_index + 1:second_index]
        )
        second_pullback = max(
            candle.high for candle in candles[second_index + 1:third_index]
        )
        minimum_pullback = THREE_PUSH_PULLBACK_ATR * atr
        if (
            first_pullback - max(prices[0], prices[1]) < minimum_pullback
            or second_pullback - max(prices[1], prices[2]) < minimum_pullback
        ):
            return None
        macd_match = _aligned_macd_pivots(
            macd,
            [first_index, second_index, third_index],
            current_index,
            structure=structure,
        )
        if macd_match is None:
            return None
        macd_indices, macd_values, macd_weakening = macd_match
        neckline = second_pullback
        invalidation = prices[2] - THREE_PUSH_INVALIDATION_BUFFER_ATR * atr
        direction = "up"
        weakening = sum(macd_weakening)

    volumes = [
        candles[first_index].volume,
        candles[second_index].volume,
        candles[third_index].volume,
    ]
    push_close_times = [
        candles[first_index].close_time,
        candles[second_index].close_time,
        candles[third_index].close_time,
    ]
    push_macd_close_times = [
        candles[index].close_time for index in macd_indices
    ]
    return {
        "rule_version": THREE_PUSH_RULE_VERSION,
        "pattern_id": (
            f"v{THREE_PUSH_RULE_VERSION}:{structure}:p:"
            + ":".join(str(value) for value in push_close_times)
            + ":m:"
            + ":".join(str(value) for value in push_macd_close_times)
        ),
        "structure": structure,
        "direction": direction,
        "push_prices": prices,
        "push_macd": macd_values,
        "push_macd_close_times": push_macd_close_times,
        "push_volumes": volumes,
        "push_close_times": push_close_times,
        "third_pivot_close_time": candles[third_index].close_time,
        "neckline": neckline,
        "invalidation": invalidation,
        "atr": atr,
        "macd_weakening_pct": weakening * 100.0,
        "macd_step_weakening_pcts": [
            value * 100.0 for value in macd_weakening
        ],
        "price_step_atr_ratios": price_steps,
        "price_progress_pct": abs(prices[2] - prices[0]) / abs(prices[0]) * 100.0,
        "volume_progressive_weakening": volumes[0] > volumes[1] > volumes[2],
        "third_vs_first_volume_ratio": (
            volumes[2] / volumes[0] if volumes[0] > 0 else 0.0
        ),
    }


def _detect_three_push_pattern(
    candles: list[Candle],
    current_index: int,
) -> dict[str, Any] | None:
    """Return a newly confirmed, non-repainting three-push setup."""

    if (
        current_index < 0
        or current_index >= len(candles)
        or current_index < 59
    ):
        return None
    visible = candles[:current_index + 1]
    macd = _macd_line(visible)
    top = _three_push_for_structure(
        visible,
        current_index,
        macd,
        structure="top",
    )
    bottom = _three_push_for_structure(
        visible,
        current_index,
        macd,
        structure="bottom",
    )
    if top is None:
        return bottom
    if bottom is None:
        return top
    return max(
        (top, bottom),
        key=lambda pattern: _to_float(pattern.get("macd_weakening_pct")),
    )


def _normalize_three_push_track(original: dict[str, Any]) -> dict[str, Any]:
    track = copy.deepcopy(original) if isinstance(original, dict) else {}
    if _to_int(track.get("rule_version")) == THREE_PUSH_RULE_VERSION:
        return track
    setup = track.get("setup")
    corrupted_setup = isinstance(setup, dict) and (
        _to_int(setup.get("bars_since"), -1) < 0
        or str(setup.get("structure") or "") not in {"top", "bottom"}
        or _to_float(setup.get("neckline")) <= 0
        or _to_float(setup.get("invalidation")) <= 0
        or _to_float(setup.get("atr")) <= 0
        or not isinstance(setup.get("push_prices"), list)
        or len(setup.get("push_prices")) != 3
    )
    track.update({
        "rule_version": THREE_PUSH_RULE_VERSION,
        "setup": None,
        "last_pattern_id": "",
        "last_third_pivot_close_time": 0,
        "pending_context": None,
    })
    if corrupted_setup:
        track["setup_recovery_count"] = max(
            0,
            _to_int(track.get("setup_recovery_count")),
        ) + 1
    return track


def _step_three_push_track(
    original: dict[str, Any],
    candles: list[Candle],
    current_index: int,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    track = _normalize_three_push_track(original)
    candle = candles[current_index]
    track.setdefault("setup", None)
    track.setdefault("last_pattern_id", "")
    track.setdefault("last_third_pivot_close_time", 0)
    event: dict[str, Any] | None = None
    setup = track.get("setup")
    if isinstance(setup, dict):
        bars_since = _to_int(setup.get("bars_since"), -1)
        structure = str(setup.get("structure") or "")
        neckline = _to_float(setup.get("neckline"))
        invalidation = _to_float(setup.get("invalidation"))
        atr = _to_float(setup.get("atr"))
        push_prices = setup.get("push_prices")
        if (
            bars_since < 0
            or _to_int(setup.get("rule_version")) != THREE_PUSH_RULE_VERSION
            or structure not in {"top", "bottom"}
            or neckline <= 0
            or invalidation <= 0
            or atr <= 0
            or not isinstance(push_prices, list)
            or len(push_prices) != 3
        ):
            track["setup"] = None
            track["setup_recovery_count"] = max(
                0,
                _to_int(track.get("setup_recovery_count")),
            ) + 1
        else:
            bars_since += 1
            setup["bars_since"] = bars_since
            confirmation_buffer = THREE_PUSH_CONFIRM_BUFFER_ATR * atr
            third_price = _to_float(push_prices[2])
            superseded = (
                structure == "top" and candle.high > third_price
            ) or (
                structure == "bottom" and candle.low < third_price
            )
            invalidated = (
                structure == "top" and candle.close > invalidation
            ) or (
                structure == "bottom" and candle.close < invalidation
            )
            confirmed = (
                structure == "top"
                and candle.close < neckline - confirmation_buffer
            ) or (
                structure == "bottom"
                and candle.close > neckline + confirmation_buffer
            )
            if (
                superseded
                or invalidated
                or bars_since > THREE_PUSH_MAX_CONFIRM_BARS
            ):
                if superseded:
                    track["last_superseded_pattern_id"] = str(
                        setup.get("pattern_id") or ""
                    )
                    track["last_superseded_close_time"] = candle.close_time
                track["setup"] = None
            elif confirmed:
                event = copy.deepcopy(setup)
                event.update({
                    "event": f"three_push_{structure}_confirmed",
                    "close_time": candle.close_time,
                    "close": candle.close,
                    "bars_since_formation": bars_since,
                })
                track["setup"] = None
    else:
        track["setup"] = None

    if track.get("setup") is None and event is None:
        pattern = _detect_three_push_pattern(candles, current_index)
        if (
            pattern is not None
            and str(pattern.get("pattern_id") or "")
            != str(track.get("last_pattern_id") or "")
        ):
            setup = copy.deepcopy(pattern)
            setup.update({
                "formed_close_time": candle.close_time,
                "bars_since": 0,
            })
            track["last_pattern_id"] = str(setup["pattern_id"])
            track["last_third_pivot_close_time"] = _to_int(
                setup.get("third_pivot_close_time")
            )
            structure = str(setup["structure"])
            confirmation_buffer = (
                THREE_PUSH_CONFIRM_BUFFER_ATR * _to_float(setup.get("atr"))
            )
            already_confirmed = (
                structure == "top"
                and candle.close
                < _to_float(setup.get("neckline")) - confirmation_buffer
            ) or (
                structure == "bottom"
                and candle.close
                > _to_float(setup.get("neckline")) + confirmation_buffer
            )
            already_invalidated = (
                structure == "top"
                and candle.close > _to_float(setup.get("invalidation"))
            ) or (
                structure == "bottom"
                and candle.close < _to_float(setup.get("invalidation"))
            )
            if already_invalidated:
                track["setup"] = None
            else:
                track["setup"] = None if already_confirmed else setup
                event = copy.deepcopy(setup)
                event.update({
                    "event": (
                        f"three_push_{structure}_confirmed"
                        if already_confirmed
                        else f"three_push_{structure}_forming"
                    ),
                    "close_time": candle.close_time,
                    "close": candle.close,
                    "bars_since_formation": 0,
                })

    track["last_close_time"] = candle.close_time
    return track, event


def _reset_after_observation(
    track: dict[str, Any],
    candle: Candle,
    timeframe_ms: int,
    spec: HorizonSpec,
) -> None:
    track["box"] = None
    track["breakout"] = None
    track["cooldown_until"] = candle.close_time + timeframe_ms * spec.cooldown


def _step_track(
    original: dict[str, Any],
    candles: list[Candle],
    current_index: int,
    timeframe_ms: int,
    spec: HorizonSpec,
    strong_volume_ratio: float,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    track = copy.deepcopy(original) if isinstance(original, dict) else {}
    candle = candles[current_index]
    track.setdefault("box", None)
    track.setdefault("breakout", None)
    track.setdefault("cooldown_until", 0)

    box = track.get("box")
    if not isinstance(box, dict):
        track["box"] = None
        track["breakout"] = None
        if candle.close_time >= int(track.get("cooldown_until") or 0):
            candidate = _box_candidate(candles, current_index, spec)
            if candidate is not None:
                track["box"] = candidate
                box = candidate
    else:
        box["active_bars"] = max(0, int(box.get("active_bars") or 0)) + 1

    box = track.get("box")
    if not isinstance(box, dict):
        track["last_close_time"] = candle.close_time
        return track, None
    box.setdefault("upper_sweep_sent", False)
    box.setdefault("lower_sweep_sent", False)

    if (
        spec.maximum_age > 0
        and int(box.get("active_bars") or 0) > spec.maximum_age
        and not isinstance(track.get("breakout"), dict)
    ):
        _reset_after_observation(track, candle, timeframe_ms, spec)
        track["last_close_time"] = candle.close_time
        return track, None

    upper = _to_float(box.get("upper"))
    lower = _to_float(box.get("lower"))
    frozen_atr = _to_float(box.get("atr"))
    if upper <= lower or frozen_atr <= 0:
        _reset_after_observation(track, candle, timeframe_ms, spec)
        track["last_close_time"] = candle.close_time
        return track, None

    breakout_buffer = BREAKOUT_BUFFER_ATR * frozen_atr
    reentry_buffer = REENTRY_BUFFER_ATR * frozen_atr
    ratio = _volume_ratio(candles, current_index)
    breakout = track.get("breakout")
    event_name = ""
    direction = ""

    if isinstance(breakout, dict):
        breakout["bars_since"] = max(0, int(breakout.get("bars_since") or 0)) + 1
        bars_since = int(breakout["bars_since"])
        breakout_direction = str(breakout.get("direction") or "")
        if breakout_direction == "up":
            if candle.close < lower - breakout_buffer:
                event_name = (
                    "strong_breakout_down"
                    if ratio >= strong_volume_ratio
                    else "breakout_down"
                )
                direction = "down"
                track["breakout"] = {
                    "direction": "down",
                    "bars_since": 0,
                    "started_close_time": candle.close_time,
                    "retest_sent": False,
                }
            elif bars_since > RETEST_BARS:
                _reset_after_observation(track, candle, timeframe_ms, spec)
            elif bars_since <= FAKEOUT_BARS and candle.close < upper - reentry_buffer:
                event_name = "fake_breakout"
                direction = "down"
                _reset_after_observation(track, candle, timeframe_ms, spec)
            elif bars_since > FAKEOUT_BARS and candle.close < upper - reentry_buffer:
                _reset_after_observation(track, candle, timeframe_ms, spec)
            elif (
                bars_since <= RETEST_BARS
                and not bool(breakout.get("retest_sent"))
                and candle.low <= upper + reentry_buffer
                and candle.close > upper + reentry_buffer
            ):
                event_name = "retest_up"
                direction = "up"
                breakout["retest_sent"] = True
        elif breakout_direction == "down":
            if candle.close > upper + breakout_buffer:
                event_name = (
                    "strong_breakout_up"
                    if ratio >= strong_volume_ratio
                    else "breakout_up"
                )
                direction = "up"
                track["breakout"] = {
                    "direction": "up",
                    "bars_since": 0,
                    "started_close_time": candle.close_time,
                    "retest_sent": False,
                }
            elif bars_since > RETEST_BARS:
                _reset_after_observation(track, candle, timeframe_ms, spec)
            elif bars_since <= FAKEOUT_BARS and candle.close > lower + reentry_buffer:
                event_name = "fake_breakdown"
                direction = "up"
                _reset_after_observation(track, candle, timeframe_ms, spec)
            elif bars_since > FAKEOUT_BARS and candle.close > lower + reentry_buffer:
                _reset_after_observation(track, candle, timeframe_ms, spec)
            elif (
                bars_since <= RETEST_BARS
                and not bool(breakout.get("retest_sent"))
                and candle.high >= lower - reentry_buffer
                and candle.close < lower - reentry_buffer
            ):
                event_name = "retest_down"
                direction = "down"
                breakout["retest_sent"] = True
        else:
            track["breakout"] = None
    else:
        if candle.close > upper + breakout_buffer:
            event_name = (
                "strong_breakout_up"
                if ratio >= strong_volume_ratio
                else "breakout_up"
            )
            direction = "up"
            track["breakout"] = {
                "direction": "up",
                "bars_since": 0,
                "started_close_time": candle.close_time,
                "retest_sent": False,
            }
        elif candle.close < lower - breakout_buffer:
            event_name = (
                "strong_breakout_down"
                if ratio >= strong_volume_ratio
                else "breakout_down"
            )
            direction = "down"
            track["breakout"] = {
                "direction": "down",
                "bars_since": 0,
                "started_close_time": candle.close_time,
                "retest_sent": False,
            }
        else:
            swept_upper = candle.high > upper + breakout_buffer and candle.close <= upper
            swept_lower = candle.low < lower - breakout_buffer and candle.close >= lower
            if swept_upper and swept_lower:
                _reset_after_observation(track, candle, timeframe_ms, spec)
            elif swept_upper and not bool(box.get("upper_sweep_sent")):
                event_name = "upper_sweep"
                box["upper_sweep_sent"] = True
            elif swept_lower and not bool(box.get("lower_sweep_sent")):
                event_name = "lower_sweep"
                box["lower_sweep_sent"] = True
            if event_name:
                direction = "down" if event_name == "upper_sweep" else "up"

    track["last_close_time"] = candle.close_time
    if not event_name:
        return track, None
    event = {
        "event": event_name,
        "direction": direction,
        "close_time": candle.close_time,
        "close": candle.close,
        "box_upper": upper,
        "box_lower": lower,
        "box_age": spec.length + max(0, int(box.get("active_bars") or 0)),
        "box_width_atr": _to_float(box.get("width_atr")),
        "box_width_pct": _to_float(box.get("width_pct")),
        "width_pct": _to_float(box.get("width_pct")),
        "box_efficiency": _to_float(box.get("efficiency")),
        "upper_touches": max(0, int(box.get("upper_touches") or 0)),
        "lower_touches": max(0, int(box.get("lower_touches") or 0)),
        "volume_ratio": ratio,
        "bars_since_breakout": (
            0
            if event_name in {
                "breakout_up",
                "breakout_down",
                "strong_breakout_up",
                "strong_breakout_down",
            }
            else max(0, int(breakout.get("bars_since") or 0))
            if isinstance(breakout, dict)
            else 0
        ),
    }
    # Signed distance from the event's trigger edge: positive is beyond the
    # edge in breakout direction; negative has closed back inside the box.
    if event_name in {
        "breakout_up",
        "strong_breakout_up",
        "retest_up",
        "fake_breakout",
        "upper_sweep",
    }:
        event["breakout_distance_pct"] = (candle.close - upper) / upper * 100.0
    else:
        event["breakout_distance_pct"] = (lower - candle.close) / lower * 100.0
    event["breakout_distance_basis"] = "signed_directional_edge_pct"
    return track, event


def _price(value: float) -> str:
    amount = abs(value)
    if amount >= 1000:
        return f"{value:,.2f}"
    if amount >= 1:
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return f"{value:.8f}".rstrip("0").rstrip(".")


def _compact_number(value: float) -> str:
    amount = abs(value)
    for threshold, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if amount >= threshold:
            return f"{value / threshold:.2f}".rstrip("0").rstrip(".") + suffix
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _chart_payload(
    candles: list[Candle],
    current_index: int,
    event: dict[str, Any],
) -> dict[str, Any]:
    start_index = max(0, current_index - CHART_HISTORY_LIMIT + 1)
    visible = candles[start_index:current_index + 1]
    macd = _macd_line(candles[:current_index + 1])[start_index:]
    box_age = max(0, _to_int(event.get("box_age")))
    frozen_box_start = max(
        0,
        _to_int(event.get("box_start_close_time")),
    )
    bars_since_breakout = max(
        0,
        _to_int(event.get("bars_since_breakout")),
    )
    if frozen_box_start > 0:
        box_start_close_time = frozen_box_start
    elif box_age > 0:
        box_start_close_time = candles[
            max(0, current_index - box_age)
        ].close_time
    else:
        box_start_close_time = 0
    breakout_start_close_time = (
        candles[max(0, current_index - bars_since_breakout)].close_time
        if bars_since_breakout > 0
        else 0
    )
    return {
        "candles": [
            {
                "open_time": candle.open_time,
                "close_time": candle.close_time,
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": candle.volume,
            }
            for candle in visible
        ],
        "macd": list(macd),
        "box_start_close_time": box_start_close_time,
        "breakout_start_close_time": breakout_start_close_time,
    }


def _event_score(event: dict[str, Any], spec: HorizonSpec, strong_ratio: float) -> int:
    width_atr = max(0.0, _to_float(event.get("box_width_atr")))
    tightness = max(0.0, 1.0 - width_atr / max(spec.max_width_atr, 0.01))
    touch_bonus = min(
        10.0,
        max(0, int(event.get("upper_touches") or 0) + int(event.get("lower_touches") or 0) - 4) * 2.0 + 4.0,
    )
    ratio = max(0.0, _to_float(event.get("volume_ratio")))
    volume_bonus = min(20.0, ratio / max(strong_ratio, 0.01) * 10.0)
    kind_bonus = {
        "fake_breakout": 12,
        "fake_breakdown": 12,
        "strong_breakout_up": 10,
        "strong_breakout_down": 10,
        "retest_up": 7,
        "retest_down": 7,
    }.get(str(event.get("event") or ""), 0)
    return min(100, max(1, round(45 + tightness * 15 + touch_bonus + volume_bonus + kind_bonus)))


def _three_push_quality(event: dict[str, Any]) -> dict[str, Any]:
    existing_quality = str(event.get("structure_quality") or "")
    if existing_quality in {"strong", "normal", "weak"}:
        return {
            "structure_quality": existing_quality,
            "structure_quality_label": {
                "strong": "强",
                "normal": "一般",
                "weak": "弱",
            }[existing_quality],
            "quality_rank": {
                "strong": 2,
                "normal": 1,
                "weak": 0,
            }[existing_quality],
            "price_progression_pass": bool(
                event.get("price_progression_pass", True)
            ),
            "macd_three_pivots_pass": bool(
                event.get("macd_three_pivots_pass", True)
            ),
            "volume_confirmation_pass": bool(
                event.get("volume_confirmation_pass")
            ),
            "box_edge_confluence_pass": bool(
                event.get("box_edge_confluence_pass")
            ),
            "neckline_status": (
                "confirmed"
                if str(event.get("event") or "").endswith("_confirmed")
                else "forming"
            ),
        }
    volume_confirmation = bool(event.get("volume_progressive_weakening"))
    box_confirmation = bool(event.get("box_edge"))
    if volume_confirmation and box_confirmation:
        quality = "strong"
        label = "强"
        rank = 2
    elif volume_confirmation or box_confirmation:
        quality = "normal"
        label = "一般"
        rank = 1
    else:
        quality = "weak"
        label = "弱"
        rank = 0
    return {
        "structure_quality": quality,
        "structure_quality_label": label,
        "quality_rank": rank,
        "price_progression_pass": True,
        "macd_three_pivots_pass": True,
        "volume_confirmation_pass": volume_confirmation,
        "box_edge_confluence_pass": box_confirmation,
        "neckline_status": (
            "confirmed"
            if str(event.get("event") or "").endswith("_confirmed")
            else "forming"
        ),
    }


def _three_push_box_context(
    event: dict[str, Any],
    horizon_tracks: dict[str, dict[str, Any]],
    candles: list[Candle],
    current_index: int,
) -> dict[str, Any]:
    prices = event.get("push_prices")
    if not isinstance(prices, list) or len(prices) != 3:
        return {}
    third_price = _to_float(prices[-1])
    structure = str(event.get("structure") or "")
    candidates: list[tuple[float, int, HorizonSpec, dict[str, Any], float, float]] = []
    for spec in HORIZONS:
        track = horizon_tracks.get(spec.name)
        if (
            not isinstance(track, dict)
            or _to_int(track.get("last_close_time"))
            != _to_int(event.get("close_time"))
        ):
            continue
        box = track.get("box") if isinstance(track, dict) else None
        if not isinstance(box, dict):
            continue
        atr = _to_float(box.get("atr"))
        upper = _to_float(box.get("upper"))
        lower = _to_float(box.get("lower"))
        if atr <= 0 or upper <= lower:
            continue
        edge = upper if structure == "top" else lower
        signed_distance = (
            (third_price - edge) / atr
            if structure == "top"
            else (edge - third_price) / atr
        )
        distance = abs(signed_distance)
        if distance <= THREE_PUSH_EDGE_TOLERANCE_ATR:
            candidates.append((distance, -spec.rank, spec, box, edge, signed_distance))
    if not candidates:
        return {}
    _distance, _rank, spec, box, edge, signed_distance = min(
        candidates,
        key=lambda item: (item[0], item[1]),
    )
    box_age = spec.length + max(0, _to_int(box.get("active_bars")))
    return {
        "box_horizon": spec.name,
        "box_horizon_label": spec.label,
        "box_edge": edge,
        "box_upper": _to_float(box.get("upper")),
        "box_lower": _to_float(box.get("lower")),
        "box_age": box_age,
        "box_start_close_time": candles[
            max(0, current_index - box_age)
        ].close_time,
        "box_edge_signed_atr": signed_distance,
    }


def _observation_text(event_name: str) -> str:
    if event_name in {"breakout_up", "strong_breakout_up"}:
        return "未来3根K线若深度回到上沿内侧，升级为假突破；12根内关注回踩确认。"
    if event_name in {"breakout_down", "strong_breakout_down"}:
        return "未来3根K线若深度收回下沿上方，升级为假跌破；12根内关注反抽确认。"
    if event_name == "fake_breakout":
        return "突破后3根K线内深度重返箱体，原向上突破失效；等待冷却后重新识别箱体。"
    if event_name == "fake_breakdown":
        return "跌破后3根K线内深度收回箱体，原向下跌破失效；等待冷却后重新识别箱体。"
    if event_name == "retest_up":
        return "价格回踩原箱体上沿后重新收于其上，当前按突破确认观察。"
    if event_name == "retest_down":
        return "价格反抽原箱体下沿后重新收于其下，当前按跌破确认观察。"
    if event_name == "upper_sweep":
        return "影线越过上沿但收盘回到箱体，暂按扫流动性处理，不视为有效突破。"
    return "影线跌破下沿但收盘回到箱体，暂按扫流动性处理，不视为有效跌破。"


def _format_three_push_event(event: dict[str, Any]) -> str:
    event_name = str(event.get("event") or "")
    icon, label = EVENT_LABELS[event_name]
    symbol = escape(str(event.get("symbol") or ""), quote=False)
    timeframe = escape(str(event.get("timeframe") or "").upper(), quote=False)
    close_time = int(event.get("close_time") or 0)
    when = datetime.fromtimestamp(close_time / 1000, CST).strftime("%m-%d %H:%M CST")
    third_time = int(event.get("third_pivot_close_time") or 0)
    third_when = datetime.fromtimestamp(third_time / 1000, CST).strftime("%m-%d %H:%M")
    prices = [
        _to_float(value) for value in event.get("push_prices", [])
    ]
    macd = [
        _to_float(value) for value in event.get("push_macd", [])
    ]
    volumes = [
        max(0.0, _to_float(value)) for value in event.get("push_volumes", [])
    ]
    price_steps = [
        max(0.0, _to_float(value))
        for value in event.get("price_step_atr_ratios", [])
    ]
    macd_steps = [
        max(0.0, _to_float(value))
        for value in event.get("macd_step_weakening_pcts", [])
    ]
    if (
        len(prices) != 3
        or len(macd) != 3
        or len(volumes) != 3
        or len(price_steps) != 2
        or len(macd_steps) != 2
    ):
        raise ValueError("invalid three-push event payload")
    if bool(event.get("volume_progressive_weakening")):
        volume_note = "通过（逐次递减）"
    else:
        volume_ratio = _to_float(event.get("third_vs_first_volume_ratio"))
        volume_note = (
            f"未通过（第三推为第一推 {volume_ratio * 100:.0f}%）"
            if volume_ratio > 0
            else "未通过（量能不可用）"
        )
    structure = str(event.get("structure") or "")
    confirmed = event_name.endswith("_confirmed")
    observation = (
        "收盘已跌破第二、三推之间的回撤低点，三推顶背离升级为确认；失效位仍按第三推高点加ATR缓冲观察。"
        if confirmed and structure == "top"
        else "收盘已突破第二、三推之间的反弹高点，三推底背离升级为确认；失效位仍按第三推低点减ATR缓冲观察。"
        if confirmed
        else "价格高点连续抬高、MACD峰值连续降低；第三推已由右侧2根闭合K线确认，跌破颈线前只视为形成中。"
        if structure == "top"
        else "价格低点连续下移、MACD谷值连续抬高；第三推已由右侧2根闭合K线确认，突破颈线前只视为形成中。"
    )
    lines = [
        f"{icon} <b>盘整突破雷达 · {escape(label, quote=False)}</b>",
        f"<b>{symbol}</b> ｜ 周期 {timeframe} ｜ 三推结构",
        f"⏰ {when}（第三推 {third_when}）",
        "",
        (
            "结构质量｜<b>"
            f"{escape(str(event.get('structure_quality_label') or '一般'), quote=False)}"
            "</b>"
        ),
        (
            f"三推价格｜{_price(prices[0])} → {_price(prices[1])} → "
            f"<b>{_price(prices[2])}</b>"
        ),
        (
            f"价格推进｜通过（{price_steps[0]:.2f} / {price_steps[1]:.2f} ATR）"
        ),
        (
            f"MACD{'三峰' if structure == 'top' else '三谷'}｜通过（"
            f"{_price(macd[0])} → {_price(macd[1])} → "
            f"{_price(macd[2])}）"
        ),
        (
            f"MACD弱化｜通过（{macd_steps[0]:.1f}% / {macd_steps[1]:.1f}%）"
        ),
        (
            f"成交量｜{_compact_number(volumes[0])} → {_compact_number(volumes[1])} → "
            f"{_compact_number(volumes[2])}"
        ),
        f"量能确认｜{volume_note}",
        (
            f"结构｜颈线 {_price(_to_float(event.get('neckline')))} ｜ "
            f"失效位 {_price(_to_float(event.get('invalidation')))}"
        ),
    ]
    if event.get("box_edge"):
        relation = (
            "越过" if _to_float(event.get("box_edge_signed_atr")) > 0 else "贴近"
        )
        edge_name = "上沿" if structure == "top" else "下沿"
        lines.append(
            f"箱体位置｜通过：第三推{relation}{escape(str(event.get('box_horizon_label') or ''), quote=False)}"
            f"箱体{edge_name} {_price(_to_float(event.get('box_edge')))}"
        )
    else:
        lines.append("箱体位置｜未通过（无合格箱体边缘共振）")
    close_label = "确认收盘" if confirmed else "当前收盘"
    lines.extend([
        f"{close_label}｜<b>{_price(_to_float(event.get('close')))}</b>",
        (
            "颈线状态｜已确认"
            if confirmed
            else "颈线状态｜形成中（等待收盘确认）"
        ),
        "",
        f"🧭 观察：{escape(observation, quote=False)}",
    ])
    return "\n".join(lines)


def _format_event(event: dict[str, Any]) -> str:
    event_name = str(event.get("event") or "")
    if event_name.startswith("three_push_"):
        return _format_three_push_event(event)
    icon, label = EVENT_LABELS[event_name]
    symbol = escape(str(event.get("symbol") or ""), quote=False)
    timeframe = escape(str(event.get("timeframe") or "").upper(), quote=False)
    horizon = escape(str(event.get("horizon_label") or ""), quote=False)
    close_time = int(event.get("close_time") or 0)
    when = datetime.fromtimestamp(close_time / 1000, CST).strftime("%m-%d %H:%M CST")
    return "\n".join([
        f"{icon} <b>盘整突破雷达 · {escape(label, quote=False)}</b>",
        f"<b>{symbol}</b> ｜ 周期 {timeframe} ｜ {horizon}箱体（{int(event.get('box_age') or 0)}根）",
        f"⏰ {when}",
        "",
        f"收盘｜<b>{_price(_to_float(event.get('close')))}</b>",
        f"箱体｜上沿 {_price(_to_float(event.get('box_upper')))} ｜ 下沿 {_price(_to_float(event.get('box_lower')))}",
        (
            f"箱宽｜{_to_float(event.get('box_width_pct')):.2f}% ｜ "
            f"{_to_float(event.get('box_width_atr')):.2f} ATR"
        ),
        (
            f"触碰｜上沿 {int(event.get('upper_touches') or 0)} ｜ "
            f"下沿 {int(event.get('lower_touches') or 0)}（已去抖）"
        ),
        (
            f"量比｜{_to_float(event.get('volume_ratio')):.2f}x ｜ "
            f"评分 <b>{int(event.get('score') or 0)}/100</b>"
        ),
        "",
        f"🧭 观察：{escape(_observation_text(event_name), quote=False)}",
    ])


class ConsolidationBreakoutRadar:
    """Scan closed Binance USDT-perpetual candles for frozen-range events."""

    def __init__(self, settings: Settings, store: JsonStore | None = None):
        self.settings = settings
        self.store = store or JsonStore(settings.data_dir)

    @property
    def state_path(self) -> Path:
        value = getattr(
            self.settings,
            "consolidation_breakout_state_path",
            self.settings.data_dir / "consolidation_breakout_state.json",
        )
        return Path(value)

    def _empty_result(self, reason: str) -> dict[str, Any]:
        return {
            "template_id": TEMPLATE_ID,
            "events": [],
            "chart_payloads": {},
            "state_updates": [],
            "diagnostics": {
                "status": reason,
                "candidate_count": 0,
                "scanned_pairs": 0,
                "event_count": 0,
            },
        }

    def _load_state(self) -> dict[str, Any]:
        raw = self.store.load(self.state_path, {})
        if not isinstance(raw, dict) or raw.get("schema_version") != STATE_SCHEMA_VERSION:
            return {
                "schema_version": STATE_SCHEMA_VERSION,
                "tracks": {},
                "rotation": {"after_symbol": "", "round": 1},
            }
        tracks = raw.get("tracks")
        if not isinstance(tracks, dict):
            tracks = {}
        raw_rotation = raw.get("rotation")
        rotation = raw_rotation if isinstance(raw_rotation, dict) else {}
        after_symbol = str(rotation.get("after_symbol") or "").strip().upper()
        try:
            round_number = max(1, int(rotation.get("round") or 1))
        except (TypeError, ValueError, OverflowError):
            round_number = 1
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "tracks": tracks,
            "rotation": {
                "after_symbol": after_symbol,
                "round": round_number,
            },
        }

    def _universe(self, source: BinanceDataSource) -> list[str]:
        valid = {
            str(item.get("symbol") or "").upper()
            for item in source.usdt_perp_symbols()
            if isinstance(item, dict) and str(item.get("symbol") or "").upper().endswith("USDT")
        }
        excluded = {
            str(asset or "").upper()
            for asset in getattr(self.settings, "excluded_base_assets", ())
        }
        minimum = max(
            0.0,
            _to_float(getattr(self.settings, "consolidation_breakout_min_quote_volume", 0)),
        )
        eligible = {
            symbol
            for symbol in valid
            if symbol[:-4] not in excluded
        }
        if minimum <= 0:
            return sorted(eligible)

        volumes: dict[str, float] = {}
        for item in source.ticker_24h():
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").upper()
            if symbol not in eligible:
                continue
            quote_volume = _to_float(item.get("quoteVolume"))
            volumes[symbol] = max(volumes.get(symbol, 0.0), quote_volume)
        return sorted(
            symbol
            for symbol in eligible
            if volumes.get(symbol, 0.0) >= minimum
        )

    def _rotation_batch(
        self,
        universe: list[str],
        state: dict[str, Any],
        *,
        batch_limit: int,
    ) -> tuple[list[str], dict[str, Any] | None, dict[str, Any]]:
        if not universe:
            return [], None, {
                "coverage_mode": "full_market_rotation",
                "universe_count": 0,
                "rotation_round": 0,
                "remaining_in_round": 0,
                "round_completed": False,
            }

        limit = max(1, int(batch_limit))
        raw_rotation = state.get("rotation")
        rotation = raw_rotation if isinstance(raw_rotation, dict) else {}
        after_symbol = str(rotation.get("after_symbol") or "").strip().upper()
        try:
            round_number = max(1, int(rotation.get("round") or 1))
        except (TypeError, ValueError, OverflowError):
            round_number = 1

        start = bisect_right(universe, after_symbol) if after_symbol else 0
        if after_symbol and start >= len(universe):
            start = 0
            round_number += 1
        end = min(len(universe), start + limit)
        batch = universe[start:end]
        round_completed = bool(batch) and end >= len(universe)
        next_rotation = {
            "after_symbol": "" if round_completed else batch[-1],
            "round": round_number + 1 if round_completed else round_number,
        }
        diagnostics = {
            "coverage_mode": "full_market_rotation",
            "universe_count": len(universe),
            "rotation_round": round_number,
            "rotation_start_symbol": batch[0],
            "rotation_end_symbol": batch[-1],
            "remaining_in_round": max(0, len(universe) - end),
            "round_completed": round_completed,
        }
        return batch, next_rotation, diagnostics

    def build(
        self,
        source: BinanceDataSource,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        if not bool(getattr(self.settings, "consolidation_breakout_enable", False)):
            return self._empty_result("disabled")
        if int(getattr(self.settings, "consolidation_breakout_scan_limit", 40)) <= 0:
            return self._empty_result("scan_limit_zero")

        observed_now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        close_delay_ms = max(
            0,
            int(getattr(self.settings, "consolidation_breakout_close_delay_sec", 90)),
        ) * 1000
        cutoff_ms = observed_now_ms - close_delay_ms
        timeframes = tuple(dict.fromkeys(
            str(value or "").strip().lower()
            for value in getattr(self.settings, "consolidation_breakout_timeframes", ("4h", "1d", "1w"))
            if _timeframe_ms(str(value or "")) > 0
        ))
        if not timeframes:
            return self._empty_result("no_valid_timeframes")

        configured_batch_limit = max(
            1,
            int(getattr(self.settings, "consolidation_breakout_scan_limit", 40)),
        )
        kline_budget = max(0, int(getattr(self.settings, "kline_budget", 120)))
        budget_batch_limit = kline_budget // len(timeframes)
        if budget_batch_limit <= 0:
            result = self._empty_result("kline_budget_too_small")
            result["diagnostics"].update({
                "timeframes": list(timeframes),
                "kline_budget": kline_budget,
            })
            return result
        effective_batch_limit = min(configured_batch_limit, budget_batch_limit)

        state = self._load_state()
        try:
            universe = self._universe(source)
        except Exception as exc:
            result = self._empty_result("candidate_source_error")
            result["diagnostics"]["error"] = type(exc).__name__
            return result
        symbols, rotation_update, rotation_diagnostics = self._rotation_batch(
            universe,
            state,
            batch_limit=effective_batch_limit,
        )

        tracks = state.get("tracks", {})
        state_updates: list[dict[str, Any]] = []
        required_by_key: dict[str, list[str]] = {}
        events: list[dict[str, Any]] = []
        chart_contexts: dict[str, tuple[list[Candle], int]] = {}
        errors: list[dict[str, str]] = []
        scanned_pairs = 0
        successful_pairs_by_symbol: dict[str, int] = {}
        closed_candles = 0
        suppressed_horizon_events = 0
        three_push_state_recoveries = 0
        three_push_candidate_count = 0
        three_push_strong_count = 0
        three_push_normal_count = 0
        three_push_weak_suppressed_count = 0
        strong_ratio = max(
            0.01,
            _to_float(getattr(self.settings, "consolidation_breakout_strong_volume_ratio", 1.20), 1.20),
        )
        require_strong = bool(
            getattr(self.settings, "consolidation_breakout_require_strong_volume", False)
        )
        three_push_enabled = bool(
            getattr(
                self.settings,
                "consolidation_breakout_three_push_enable",
                False,
            )
        )
        kline_limit = max(spec.length + spec.stability for spec in HORIZONS) + RETEST_BARS + 4

        for symbol in symbols:
            for timeframe in timeframes:
                interval_ms = _timeframe_ms(timeframe)
                try:
                    raw_klines = source.klines(symbol, interval=timeframe, limit=kline_limit)
                except Exception as exc:
                    errors.append({
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "error": type(exc).__name__,
                    })
                    continue
                if not raw_klines:
                    errors.append({
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "error": "empty_klines",
                    })
                    continue
                parsed = [Candle.from_binance(row) for row in raw_klines]
                candles = sorted(
                    (candle for candle in parsed if candle is not None and candle.close_time <= cutoff_ms),
                    key=lambda candle: candle.close_time,
                )
                if not candles:
                    errors.append({
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "error": "no_closed_candles",
                    })
                    continue
                deduplicated: list[Candle] = []
                for candle in candles:
                    if deduplicated and deduplicated[-1].close_time == candle.close_time:
                        deduplicated[-1] = candle
                    else:
                        deduplicated.append(candle)
                candles = deduplicated
                scanned_pairs += 1
                successful_pairs_by_symbol[symbol] = (
                    successful_pairs_by_symbol.get(symbol, 0) + 1
                )
                closed_candles += len(candles)

                working: dict[str, dict[str, Any]] = {}
                pending_indices: set[int] = set()
                for spec in HORIZONS:
                    key = f"{symbol}|{timeframe}|{spec.name}"
                    existing = tracks.get(key, {}) if isinstance(tracks, dict) else {}
                    working[spec.name] = copy.deepcopy(existing) if isinstance(existing, dict) else {}
                    last_close = int(working[spec.name].get("last_close_time") or 0)
                    if last_close > 0:
                        pending_indices.update(
                            index
                            for index, candle in enumerate(candles)
                            if candle.close_time > last_close
                        )
                    else:
                        pending_indices.add(len(candles) - 1)
                three_push_key = f"{symbol}|{timeframe}|three_push"
                if three_push_enabled:
                    existing = (
                        tracks.get(three_push_key, {})
                        if isinstance(tracks, dict)
                        else {}
                    )
                    working["three_push"] = (
                        copy.deepcopy(existing) if isinstance(existing, dict) else {}
                    )
                    recovery_before_normalize = _to_int(
                        working["three_push"].get("setup_recovery_count")
                    )
                    working["three_push"] = _normalize_three_push_track(
                        working["three_push"]
                    )
                    three_push_state_recoveries += max(
                        0,
                        _to_int(
                            working["three_push"].get("setup_recovery_count")
                        ) - recovery_before_normalize,
                    )
                    last_close = _to_int(
                        working["three_push"].get("last_close_time") or 0
                    )
                    if last_close > 0:
                        pending_indices.update(
                            index
                            for index, candle in enumerate(candles)
                            if candle.close_time > last_close
                        )
                    else:
                        if len(candles) > 1:
                            baseline = copy.deepcopy(working["three_push"])
                            baseline["last_close_time"] = candles[-2].close_time
                            working["three_push"] = baseline
                            state_updates.append({
                                "key": three_push_key,
                                "state": copy.deepcopy(baseline),
                                "required_event_ids": [],
                            })
                        pending_indices.add(len(candles) - 1)

                for index in sorted(pending_indices):
                    bar_events: list[dict[str, Any]] = []
                    candidates_on_bar: list[tuple[HorizonSpec, dict[str, Any]]] = []
                    processed_on_bar: list[tuple[HorizonSpec, str]] = []
                    for spec in HORIZONS:
                        track = working[spec.name]
                        last_close = int(track.get("last_close_time") or 0)
                        if candles[index].close_time <= last_close:
                            continue
                        key = f"{symbol}|{timeframe}|{spec.name}"
                        updated, raw_event = _step_track(
                            track,
                            candles,
                            index,
                            interval_ms,
                            spec,
                            strong_ratio,
                        )
                        working[spec.name] = updated
                        processed_on_bar.append((spec, key))
                        if raw_event is not None:
                            raw_event.update({
                                "schema": "range_breakout.v1",
                                "symbol": symbol,
                                "timeframe": timeframe,
                                "horizon": spec.name,
                                "horizon_label": spec.label,
                                "horizon_length": spec.length,
                                "event_time": raw_event["close_time"],
                            })
                            event_id = (
                                f"range_breakout.v1:{symbol}:{timeframe}:{spec.name}:"
                                f"{raw_event['event']}:{raw_event['close_time']}"
                            )
                            raw_event["event_id"] = event_id
                            raw_event["dedup_key"] = event_id
                            raw_event["score"] = _event_score(raw_event, spec, strong_ratio)
                            candidates_on_bar.append((spec, raw_event))

                    eligible = [
                        (spec, event)
                        for spec, event in candidates_on_bar
                        if not (
                            require_strong
                            and event["event"] in {"breakout_up", "breakout_down"}
                        )
                    ]
                    if eligible:
                        winner_spec, winner = max(
                            eligible,
                            key=lambda item: (
                                EVENT_PRIORITY.get(str(item[1].get("event") or ""), 0),
                                item[0].rank,
                            ),
                        )
                        winner["priority"] = EVENT_PRIORITY[str(winner["event"])]
                        winner["text"] = _format_event(winner)
                        bar_events.append(winner)
                        key = f"{symbol}|{timeframe}|{winner_spec.name}"
                        required_by_key.setdefault(key, []).append(str(winner["event_id"]))
                        suppressed_horizon_events += max(0, len(candidates_on_bar) - 1)
                    else:
                        suppressed_horizon_events += len(candidates_on_bar)

                    three_push_processed = False
                    if three_push_enabled:
                        three_push_track = working["three_push"]
                        last_close = _to_int(
                            three_push_track.get("last_close_time") or 0
                        )
                        if candles[index].close_time > last_close:
                            track_before_step = copy.deepcopy(three_push_track)
                            updated, raw_event = _step_three_push_track(
                                three_push_track,
                                candles,
                                index,
                            )
                            recovery_delta = max(
                                0,
                                _to_int(updated.get("setup_recovery_count"))
                                - _to_int(
                                    track_before_step.get(
                                        "setup_recovery_count"
                                    )
                                ),
                            )
                            three_push_state_recoveries += recovery_delta
                            working["three_push"] = updated
                            three_push_processed = True
                            superseded_pattern_id = str(
                                updated.get("last_superseded_pattern_id") or ""
                            )
                            superseded_now = (
                                bool(superseded_pattern_id)
                                and superseded_pattern_id
                                != str(
                                    track_before_step.get(
                                        "last_superseded_pattern_id"
                                    )
                                    or ""
                                )
                            )
                            if superseded_now:
                                stale_event_ids = {
                                    str(event.get("event_id") or "")
                                    for event in events
                                    if str(event.get("pattern_id") or "")
                                    == superseded_pattern_id
                                    and str(event.get("event") or "").endswith(
                                        "_forming"
                                    )
                                }
                                if stale_event_ids:
                                    events[:] = [
                                        event
                                        for event in events
                                        if str(event.get("event_id") or "")
                                        not in stale_event_ids
                                    ]
                                    for event_id in stale_event_ids:
                                        chart_contexts.pop(event_id, None)
                                    required_by_key[three_push_key] = [
                                        event_id
                                        for event_id in required_by_key.get(
                                            three_push_key,
                                            [],
                                        )
                                        if event_id not in stale_event_ids
                                    ]
                                    for state_update in state_updates:
                                        if (
                                            state_update.get("key")
                                            != three_push_key
                                        ):
                                            continue
                                        state_update["required_event_ids"] = [
                                            event_id
                                            for event_id in state_update.get(
                                                "required_event_ids",
                                                [],
                                            )
                                            if event_id not in stale_event_ids
                                        ]
                            if raw_event is not None:
                                context_fields = (
                                    "box_horizon",
                                    "box_horizon_label",
                                    "box_edge",
                                    "box_upper",
                                    "box_lower",
                                    "box_age",
                                    "box_start_close_time",
                                    "box_edge_signed_atr",
                                )
                                quality_fields = (
                                    "structure_quality",
                                    "structure_quality_label",
                                    "quality_rank",
                                    "price_progression_pass",
                                    "macd_three_pivots_pass",
                                    "volume_confirmation_pass",
                                    "box_edge_confluence_pass",
                                )
                                context = {
                                    field: raw_event[field]
                                    for field in context_fields
                                    if field in raw_event
                                }
                                saved_quality: dict[str, Any] = {}
                                pattern_id = str(
                                    raw_event.get("pattern_id") or ""
                                )
                                saved_pending = track_before_step.get(
                                    "pending_context"
                                )
                                saved_matches = (
                                    isinstance(saved_pending, dict)
                                    and str(saved_pending.get("pattern_id") or "")
                                    == pattern_id
                                    and isinstance(saved_pending.get("context"), dict)
                                )
                                if saved_matches:
                                    saved_context = saved_pending["context"]
                                    saved_event = saved_pending.get("event")
                                    if isinstance(saved_event, dict):
                                        raw_event.update(
                                            copy.deepcopy(saved_event)
                                        )
                                        context = {
                                            field: raw_event[field]
                                            for field in context_fields
                                            if field in raw_event
                                        }
                                    if not context:
                                        context = {
                                            field: saved_context[field]
                                            for field in context_fields
                                            if field in saved_context
                                        }
                                    saved_quality = {
                                        field: saved_context[field]
                                        for field in quality_fields
                                        if field in saved_context
                                    }
                                new_pattern_event = (
                                    _to_int(
                                        raw_event.get("bars_since_formation"),
                                        -1,
                                    )
                                    == 0
                                )
                                if (
                                    not context
                                    and not saved_matches
                                    and new_pattern_event
                                ):
                                    context = _three_push_box_context(
                                        raw_event,
                                        working,
                                        candles,
                                        index,
                                    )
                                raw_event.update(context)
                                raw_event.update(saved_quality)
                                quality = _three_push_quality(raw_event)
                                raw_event.update(quality)
                                if raw_event["event"].endswith("_forming"):
                                    setup = updated.get("setup")
                                    if isinstance(setup, dict):
                                        setup.update(context)
                                        setup.update(quality)
                                        setup["box_context_frozen"] = True
                                updated["pending_context"] = None
                                three_push_candidate_count += 1
                                structure_quality = str(
                                    raw_event.get("structure_quality") or "weak"
                                )
                                if structure_quality == "weak":
                                    three_push_weak_suppressed_count += 1
                                else:
                                    if structure_quality == "strong":
                                        three_push_strong_count += 1
                                    else:
                                        three_push_normal_count += 1
                                    if new_pattern_event and not saved_matches:
                                        checkpoint = copy.deepcopy(
                                            track_before_step
                                        )
                                        if recovery_delta > 0:
                                            checkpoint["setup"] = None
                                            checkpoint[
                                                "setup_recovery_count"
                                            ] = _to_int(
                                                updated.get(
                                                    "setup_recovery_count"
                                                )
                                            )
                                        frozen_context = copy.deepcopy(context)
                                        frozen_context.update(quality)
                                        checkpoint["pending_context"] = {
                                            "pattern_id": pattern_id,
                                            "context": frozen_context,
                                            "event": copy.deepcopy(raw_event),
                                        }
                                        state_updates.append({
                                            "key": three_push_key,
                                            "state": checkpoint,
                                            "required_event_ids": list(
                                                required_by_key.get(
                                                    three_push_key,
                                                    [],
                                                )
                                            ),
                                        })
                                    raw_event.update({
                                        "schema": "three_push_divergence.v2",
                                        "divergence": "price_macd_three_push",
                                        "symbol": symbol,
                                        "timeframe": timeframe,
                                        "horizon": "three_push",
                                        "horizon_label": str(
                                            raw_event.get(
                                                "box_horizon_label"
                                            )
                                            or "独立"
                                        ),
                                        "event_time": raw_event["close_time"],
                                    })
                                    event_id = (
                                        f"three_push_divergence.v2:{symbol}:"
                                        f"{timeframe}:{raw_event['event']}:"
                                        f"{pattern_id}:{raw_event['close_time']}"
                                    )
                                    raw_event["event_id"] = event_id
                                    raw_event["dedup_key"] = event_id
                                    raw_event["priority"] = EVENT_PRIORITY[
                                        str(raw_event["event"])
                                    ]
                                    raw_event["text"] = _format_event(raw_event)
                                    bar_events.append(raw_event)
                                    required_by_key.setdefault(
                                        three_push_key,
                                        [],
                                    ).append(event_id)
                    for spec, key in processed_on_bar:
                        state_updates.append({
                            "key": key,
                            "state": copy.deepcopy(working[spec.name]),
                            "required_event_ids": list(required_by_key.get(key, [])),
                        })
                    if three_push_processed:
                        state_updates.append({
                            "key": three_push_key,
                            "state": copy.deepcopy(working["three_push"]),
                            "required_event_ids": list(
                                required_by_key.get(three_push_key, [])
                            ),
                        })
                    ordered_bar_events = sorted(
                        bar_events,
                        key=lambda event: (
                            int(event.get("priority") or 0),
                            int(
                                event.get("quality_rank")
                                or event.get("score")
                                or 0
                            ),
                        ),
                        reverse=True,
                    )
                    for event in ordered_bar_events:
                        event_id = str(event.get("event_id") or "")
                        if event_id:
                            chart_contexts[event_id] = (candles, index)
                    events.extend(ordered_bar_events)
        max_signals = max(
            0,
            int(getattr(self.settings, "consolidation_breakout_max_signals_per_scan", 8)),
        )
        outbound_events = events[:max_signals]
        outbound_chart_payloads: dict[str, dict[str, Any]] = {}
        for event in outbound_events:
            event_id = str(event.get("event_id") or "")
            context = chart_contexts.get(event_id)
            if not event_id or context is None:
                continue
            candles, event_index = context
            outbound_chart_payloads[event_id] = _chart_payload(
                candles,
                event_index,
                event,
            )
        withheld_event_ids = {
            str(event.get("event_id") or "") for event in events[max_signals:]
        }
        scanned_symbol_count = sum(
            count == len(timeframes)
            for count in successful_pairs_by_symbol.values()
        )
        partially_scanned_symbol_count = sum(
            0 < count < len(timeframes)
            for count in successful_pairs_by_symbol.values()
        )
        diagnostics: dict[str, Any] = {
            "status": (
                "no_candidates"
                if not symbols
                else "ok"
                if not errors and three_push_state_recoveries == 0
                else "degraded"
            ),
            "candidate_count": len(universe),
            "batch_count": len(symbols),
            "attempted_symbol_count": len(symbols),
            "scanned_symbol_count": scanned_symbol_count,
            "partially_scanned_symbol_count": partially_scanned_symbol_count,
            "configured_batch_size": configured_batch_limit,
            "effective_batch_size": effective_batch_limit,
            "kline_budget": kline_budget,
            "timeframes": list(timeframes),
            "expected_pairs": len(symbols) * len(timeframes),
            "scanned_pairs": scanned_pairs,
            "closed_candles": closed_candles,
            "event_count": len(outbound_events),
            "withheld_event_count": len(withheld_event_ids),
            "suppressed_horizon_events": suppressed_horizon_events,
            "three_push_enabled": three_push_enabled,
            "three_push_event_count": sum(
                str(event.get("event") or "").startswith("three_push_")
                for event in outbound_events
            ),
            "three_push_candidate_count": three_push_candidate_count,
            "three_push_strong_count": three_push_strong_count,
            "three_push_normal_count": three_push_normal_count,
            "three_push_weak_suppressed_count": (
                three_push_weak_suppressed_count
            ),
            "three_push_state_recoveries": three_push_state_recoveries,
            "state_update_count": len(state_updates),
            "cutoff_ms": cutoff_ms,
        }
        diagnostics.update(rotation_diagnostics)
        source_diagnostics = getattr(source, "diagnostics", None)
        if callable(source_diagnostics):
            diagnostics["binance"] = source_diagnostics()
        if errors:
            diagnostics["errors"] = errors[:20]
        return {
            "template_id": TEMPLATE_ID,
            "events": outbound_events,
            "chart_payloads": outbound_chart_payloads,
            "state_updates": state_updates,
            "rotation_update": rotation_update,
            "diagnostics": diagnostics,
        }

    def commit(
        self,
        result: dict[str, Any],
        accepted_event_ids: Iterable[str] | None,
    ) -> dict[str, Any]:
        """Commit safe updates; unaccepted outbound events remain replayable."""

        accepted = {
            str(value) for value in (accepted_event_ids or ()) if str(value)
        }
        updates = [
            update
            for update in result.get("state_updates", [])
            if isinstance(update, dict)
            and str(update.get("key") or "")
            and isinstance(update.get("state"), dict)
        ]
        applicable: list[dict[str, Any]] = []
        deferred = 0
        for update in updates:
            required = {
                str(value)
                for value in update.get("required_event_ids", [])
                if str(value)
            }
            if required.issubset(accepted):
                applicable.append(update)
            else:
                deferred += 1
        raw_rotation = result.get("rotation_update")
        rotation_update = raw_rotation if isinstance(raw_rotation, dict) else None
        if rotation_update is not None:
            after_symbol = str(rotation_update.get("after_symbol") or "").strip().upper()
            try:
                round_number = max(1, int(rotation_update.get("round") or 1))
            except (TypeError, ValueError, OverflowError):
                rotation_update = None
            else:
                rotation_update = {
                    "after_symbol": after_symbol,
                    "round": round_number,
                }
        if not applicable and rotation_update is None:
            return {
                "status": "deferred" if deferred else "no_changes",
                "applied": 0,
                "deferred": deferred,
                "rotation_advanced": False,
            }

        def apply(current: Any) -> dict[str, Any]:
            if not isinstance(current, dict) or current.get("schema_version") != STATE_SCHEMA_VERSION:
                payload: dict[str, Any] = {
                    "schema_version": STATE_SCHEMA_VERSION,
                    "tracks": {},
                }
            else:
                payload = copy.deepcopy(current)
                if not isinstance(payload.get("tracks"), dict):
                    payload["tracks"] = {}
            for update in applicable:
                payload["tracks"][str(update["key"])] = copy.deepcopy(update["state"])
            if rotation_update is not None:
                payload["rotation"] = copy.deepcopy(rotation_update)
            payload["updated_at"] = int(time.time())
            return payload

        self.store.update(self.state_path, apply, {})
        return {
            "status": "ok",
            "applied": len(applicable),
            "deferred": deferred,
            "rotation_advanced": rotation_update is not None,
        }


__all__ = [
    "Candle",
    "ConsolidationBreakoutRadar",
    "HORIZONS",
    "TEMPLATE_ID",
    "count_touch_clusters",
]
