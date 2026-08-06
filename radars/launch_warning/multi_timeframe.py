from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


MULTI_TIMEFRAME_VERSION = 1

TIMEFRAME_INTERVAL_MS: dict[str, int] = {
    "5m": 5 * 60 * 1000,
    "15m": 15 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "2h": 2 * 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "8h": 8 * 60 * 60 * 1000,
    "12h": 12 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
    "1w": 7 * 24 * 60 * 60 * 1000,
}

ROLE_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("macro_direction", "大方向", ("1w", "1d")),
    ("main_structure", "主结构", ("12h", "8h", "4h")),
    ("confirmation", "确认", ("2h", "1h")),
    ("trigger", "触发", ("15m",)),
    ("entry", "入场", ("5m",)),
)

MINIMUM_CLOSED_CANDLES = 6
REFERENCE_CANDLES = 5
ANALYSIS_CANDLES = 32
WEEK_ALIGNMENT_MS = 4 * 24 * 60 * 60 * 1000  # Binance week starts Monday UTC.


@dataclass(frozen=True)
class ClosedCandle:
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    close_time_ms: int


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _integer(value: Any) -> int | None:
    number = _finite_number(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def _window_end(
    window_end_ms: int | None,
    clock: Callable[[], float] | None,
) -> int:
    if window_end_ms is None:
        if clock is None:
            raise ValueError("launch_multi_timeframe_window_end_required")
        window_end_ms = int(float(clock()) * 1000)
    boundary = _integer(window_end_ms)
    if boundary is None or boundary <= 0:
        raise ValueError("launch_multi_timeframe_window_end_invalid")
    return boundary


def _closed_candles(
    rows: Sequence[Sequence[Any]],
    *,
    interval_ms: int,
    window_end_ms: int,
) -> tuple[list[ClosedCandle], int, int]:
    candles: dict[int, ClosedCandle] = {}
    invalid_rows = 0
    unclosed_rows = 0
    duplicate_open_times: set[int] = set()
    for row in rows:
        if (
            not isinstance(row, Sequence)
            or isinstance(row, (str, bytes))
            or len(row) < 7
        ):
            invalid_rows += 1
            continue
        open_time_ms = _integer(row[0])
        close_time_ms = _integer(row[6])
        prices = [_finite_number(row[index]) for index in range(1, 5)]
        if open_time_ms is None or close_time_ms is None or any(
            price is None for price in prices
        ):
            invalid_rows += 1
            continue
        open_price, high, low, close = (float(price) for price in prices)
        if (
            open_time_ms < 0
            or close_time_ms != open_time_ms + interval_ms - 1
            or min(open_price, high, low, close) <= 0
            or high < max(open_price, close)
            or low > min(open_price, close)
        ):
            invalid_rows += 1
            continue
        if close_time_ms >= window_end_ms:
            unclosed_rows += 1
            continue
        if open_time_ms in candles:
            duplicate_open_times.add(open_time_ms)
            continue
        candles[open_time_ms] = ClosedCandle(
            open_time_ms=open_time_ms,
            open=open_price,
            high=high,
            low=low,
            close=close,
            close_time_ms=close_time_ms,
        )
    invalid_rows += len(duplicate_open_times)
    ordered = [candles[key] for key in sorted(candles)]
    return ordered[-ANALYSIS_CANDLES:], invalid_rows, unclosed_rows


def _confirmed_swings(
    candles: Sequence[ClosedCandle],
) -> tuple[list[ClosedCandle], list[ClosedCandle]]:
    highs: list[ClosedCandle] = []
    lows: list[ClosedCandle] = []
    for index in range(1, len(candles) - 1):
        previous = candles[index - 1]
        current = candles[index]
        following = candles[index + 1]
        if current.high > previous.high and current.high > following.high:
            highs.append(current)
        if current.low < previous.low and current.low < following.low:
            lows.append(current)
    return highs, lows


def _structure(candles: Sequence[ClosedCandle]) -> dict[str, str]:
    swing_highs, swing_lows = _confirmed_swings(candles)
    if len(swing_highs) >= 2:
        high_label = "HH" if swing_highs[-1].high > swing_highs[-2].high else "LH"
        high_source = "confirmed_swings"
    else:
        high_label = "HH" if candles[-1].high > candles[-2].high else "LH"
        high_source = "adjacent_candles"
    if len(swing_lows) >= 2:
        low_label = "HL" if swing_lows[-1].low > swing_lows[-2].low else "LL"
        low_source = "confirmed_swings"
    else:
        low_label = "HL" if candles[-1].low > candles[-2].low else "LL"
        low_source = "adjacent_candles"

    if high_label == "HH" and low_label == "HL":
        bias = "bullish"
    elif high_label == "LH" and low_label == "LL":
        bias = "bearish"
    else:
        bias = "mixed"
    return {
        "high": high_label,
        "low": low_label,
        "bias": bias,
        "source": (
            "confirmed_swings"
            if high_source == low_source == "confirmed_swings"
            else "adjacent_candles"
        ),
    }


def _prior_bias(candles: Sequence[ClosedCandle]) -> str:
    prior = candles[:-1]
    if len(prior) < 2:
        return "neutral"
    high_up = prior[-1].high > prior[-2].high
    low_up = prior[-1].low > prior[-2].low
    if high_up and low_up:
        return "bullish"
    if not high_up and not low_up:
        return "bearish"
    return "mixed"


def _break_or_change(
    candles: Sequence[ClosedCandle],
    *,
    reference_high: float,
    reference_low: float,
) -> str:
    close = candles[-1].close
    prior_bias = _prior_bias(candles)
    if close > reference_high:
        return "CHoCH_up" if prior_bias == "bearish" else "BOS_up"
    if close < reference_low:
        return "CHoCH_down" if prior_bias == "bullish" else "BOS_down"
    return "none"


def _liquidity_sweep(
    current: ClosedCandle,
    *,
    reference_high: float,
    reference_low: float,
) -> str:
    swept_high = current.high > reference_high and current.close <= reference_high
    swept_low = current.low < reference_low and current.close >= reference_low
    if swept_high and swept_low:
        return "both"
    if swept_high:
        return "high"
    if swept_low:
        return "low"
    return "none"


def _latest_fvg(candles: Sequence[ClosedCandle]) -> dict[str, Any]:
    for index in range(len(candles) - 1, 1, -1):
        first = candles[index - 2]
        third = candles[index]
        if third.low > first.high:
            return {
                "status": "bullish",
                "zone_low": first.high,
                "zone_high": third.low,
                "candle_end_ms": third.close_time_ms + 1,
            }
        if third.high < first.low:
            return {
                "status": "bearish",
                "zone_low": third.high,
                "zone_high": first.low,
                "candle_end_ms": third.close_time_ms + 1,
            }
    return {
        "status": "none",
        "zone_low": None,
        "zone_high": None,
        "candle_end_ms": None,
    }


def _average_true_range(candles: Sequence[ClosedCandle], period: int = 14) -> float | None:
    recent = list(candles[-(period + 1):])
    if len(recent) < 2:
        return None
    ranges = [
        max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        )
        for previous, current in zip(recent, recent[1:])
    ]
    return sum(ranges) / len(ranges) if ranges else None


def _direction(
    structure: Mapping[str, str],
    structure_event: str,
    liquidity_sweep: str,
) -> str:
    if structure_event.endswith("_up"):
        return "bullish"
    if structure_event.endswith("_down"):
        return "bearish"
    bias = str(structure.get("bias") or "mixed")
    sweep_direction = (
        "bearish" if liquidity_sweep == "high"
        else "bullish" if liquidity_sweep == "low"
        else "mixed"
    )
    if sweep_direction != "mixed" and bias not in {"mixed", sweep_direction}:
        return "mixed"
    if sweep_direction != "mixed":
        return sweep_direction
    return bias


def _frame(
    rows: Sequence[Sequence[Any]],
    *,
    timeframe: str,
    window_end_ms: int,
) -> dict[str, Any]:
    interval_ms = TIMEFRAME_INTERVAL_MS[timeframe]
    candles, invalid_rows, unclosed_rows = _closed_candles(
        rows,
        interval_ms=interval_ms,
        window_end_ms=window_end_ms,
    )
    common: dict[str, Any] = {
        "timeframe": timeframe,
        "interval_ms": interval_ms,
        "closed_candles": len(candles),
        "excluded_unclosed_candles": unclosed_rows,
        "invalid_rows": invalid_rows,
        "direction": "neutral",
        "vote": 0,
        "structure": {
            "high": "unknown",
            "low": "unknown",
            "bias": "neutral",
            "source": "insufficient_history",
        },
        "structure_event": "none",
        "liquidity_sweep": "none",
        "fvg": {
            "status": "none",
            "zone_low": None,
            "zone_high": None,
            "candle_end_ms": None,
        },
    }
    if len(candles) < MINIMUM_CLOSED_CANDLES:
        return {**common, "data_status": "insufficient_history"}
    recent = candles[-MINIMUM_CLOSED_CANDLES:]
    if any(
        recent[index].open_time_ms - recent[index - 1].open_time_ms != interval_ms
        for index in range(1, len(recent))
    ):
        return {**common, "data_status": "gap"}

    current = candles[-1]
    reference = candles[-(REFERENCE_CANDLES + 1):-1]
    reference_high = max(candle.high for candle in reference)
    reference_low = min(candle.low for candle in reference)
    structure = _structure(candles)
    structure_event = _break_or_change(
        candles,
        reference_high=reference_high,
        reference_low=reference_low,
    )
    liquidity_sweep = _liquidity_sweep(
        current,
        reference_high=reference_high,
        reference_low=reference_low,
    )
    direction = _direction(structure, structure_event, liquidity_sweep)
    return {
        **common,
        "data_status": "ready" if invalid_rows == 0 else "degraded",
        "last_closed_end_ms": current.close_time_ms + 1,
        "last_close": current.close,
        "atr": _average_true_range(candles),
        "reference_high": reference_high,
        "reference_low": reference_low,
        "structure": structure,
        "structure_event": structure_event,
        "liquidity_sweep": liquidity_sweep,
        "fvg": _latest_fvg(candles),
        "direction": direction,
        "vote": 1 if direction == "bullish" else -1 if direction == "bearish" else 0,
        "identity_inference": "not_performed",
    }


def _aggregate_rows(
    rows: Sequence[Sequence[Any]],
    *,
    source_interval_ms: int,
    target_interval_ms: int,
    window_end_ms: int,
) -> list[list[Any]]:
    if target_interval_ms <= source_interval_ms or target_interval_ms % source_interval_ms:
        raise ValueError("launch_multi_timeframe_aggregate_interval_invalid")
    expected = target_interval_ms // source_interval_ms
    offset = WEEK_ALIGNMENT_MS if target_interval_ms == TIMEFRAME_INTERVAL_MS["1w"] else 0
    normalized: dict[int, list[Any]] = {}
    for row in rows:
        if (
            not isinstance(row, Sequence)
            or isinstance(row, (str, bytes))
            or len(row) < 7
        ):
            continue
        opened = _integer(row[0])
        closed = _integer(row[6])
        prices = [_finite_number(row[index]) for index in range(1, 5)]
        if (
            opened is None
            or closed != opened + source_interval_ms - 1
            or closed >= window_end_ms
            or any(value is None for value in prices)
        ):
            continue
        normalized[opened] = list(row)

    groups: dict[int, list[list[Any]]] = {}
    for opened in sorted(normalized):
        bucket = ((opened - offset) // target_interval_ms) * target_interval_ms + offset
        if bucket < 0 or bucket + target_interval_ms > window_end_ms:
            continue
        groups.setdefault(bucket, []).append(normalized[opened])

    aggregated: list[list[Any]] = []
    for bucket, members in sorted(groups.items()):
        if len(members) != expected:
            continue
        expected_opens = [bucket + index * source_interval_ms for index in range(expected)]
        if [_integer(row[0]) for row in members] != expected_opens:
            continue
        base_volume = sum(_finite_number(row[5]) or 0.0 for row in members if len(row) > 5)
        quote_volume = sum(_finite_number(row[7]) or 0.0 for row in members if len(row) > 7)
        trades = sum(_integer(row[8]) or 0 for row in members if len(row) > 8)
        taker_base = sum(_finite_number(row[9]) or 0.0 for row in members if len(row) > 9)
        taker_quote = sum(_finite_number(row[10]) or 0.0 for row in members if len(row) > 10)
        aggregated.append([
            bucket,
            members[0][1],
            max(float(row[2]) for row in members),
            min(float(row[3]) for row in members),
            members[-1][4],
            base_volume,
            bucket + target_interval_ms - 1,
            quote_volume,
            trades,
            taker_base,
            taker_quote,
            0,
        ])
    return aggregated


def expand_timeframe_klines(
    base_klines: Mapping[str, Sequence[Sequence[Any]]],
    *,
    window_end_ms: int,
) -> dict[str, list[list[Any]]]:
    """Build slower closed frames from five bounded Binance kline requests.

    Native 5m, 15m, 1h, 4h and 1d rows are preserved. 2h, 8h, 12h and
    1w are deterministically aggregated only when every source candle exists.
    """

    result = {
        timeframe: [list(row) for row in base_klines.get(timeframe, ())]
        for timeframe in ("5m", "15m", "1h", "4h", "1d")
    }
    derivations = {
        "2h": ("1h", TIMEFRAME_INTERVAL_MS["1h"]),
        "8h": ("4h", TIMEFRAME_INTERVAL_MS["4h"]),
        "12h": ("4h", TIMEFRAME_INTERVAL_MS["4h"]),
        "1w": ("1d", TIMEFRAME_INTERVAL_MS["1d"]),
    }
    for target, (source, source_interval_ms) in derivations.items():
        result[target] = _aggregate_rows(
            result[source],
            source_interval_ms=source_interval_ms,
            target_interval_ms=TIMEFRAME_INTERVAL_MS[target],
            window_end_ms=window_end_ms,
        )
    return result


def _role_group(
    key: str,
    label: str,
    members: Sequence[str],
    frames: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    ready_members = [
        timeframe
        for timeframe in members
        if frames[timeframe].get("data_status") in {"ready", "degraded"}
    ]
    directions = {
        str(frames[timeframe].get("direction"))
        for timeframe in ready_members
        if frames[timeframe].get("direction") in {"bullish", "bearish"}
    }
    if directions == {"bullish"}:
        direction = "bullish"
        vote = 1
    elif directions == {"bearish"}:
        direction = "bearish"
        vote = -1
    elif len(directions) > 1:
        direction = "mixed"
        vote = 0
    else:
        direction = "neutral"
        vote = 0
    if not ready_members:
        data_status = "unavailable"
    elif len(ready_members) < len(members) or any(
        frames[timeframe].get("data_status") != "ready"
        for timeframe in ready_members
    ):
        data_status = "degraded"
    else:
        data_status = "ready"
    return {
        "key": key,
        "label": label,
        "timeframes": list(members),
        "ready_timeframes": ready_members,
        "data_status": data_status,
        "direction": direction,
        "vote": vote,
        "max_vote_contribution": 1,
    }


def analyze_multi_timeframe(
    klines_by_timeframe: Mapping[str, Sequence[Sequence[Any]]],
    *,
    window_end_ms: int | None = None,
    clock: Callable[[], float] | None = None,
    rolling_24h: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Analyze supplied Binance klines without I/O or identity inference."""

    boundary = _window_end(window_end_ms, clock)
    frames = {
        timeframe: _frame(
            klines_by_timeframe.get(timeframe, ()),
            timeframe=timeframe,
            window_end_ms=boundary,
        )
        for timeframe in TIMEFRAME_INTERVAL_MS
    }
    role_groups = {
        key: _role_group(key, label, members, frames)
        for key, label, members in ROLE_GROUPS
    }
    group_votes = [int(group["vote"]) for group in role_groups.values()]
    ready_frames = [
        timeframe
        for timeframe, frame in frames.items()
        if frame["data_status"] in {"ready", "degraded"}
    ]
    if len(ready_frames) == len(TIMEFRAME_INTERVAL_MS) and all(
        frame["data_status"] == "ready" for frame in frames.values()
    ):
        status = "ok"
    elif ready_frames:
        status = "degraded"
    else:
        status = "unavailable"
    vote_total = sum(group_votes)
    return {
        "version": MULTI_TIMEFRAME_VERSION,
        "status": status,
        "window_end_ms": boundary,
        "timeframes": frames,
        "role_groups": role_groups,
        "vote_summary": {
            "bullish_groups": sum(vote > 0 for vote in group_votes),
            "bearish_groups": sum(vote < 0 for vote in group_votes),
            "neutral_or_mixed_groups": sum(vote == 0 for vote in group_votes),
            "net_group_vote": vote_total,
            "maximum_absolute_vote": len(ROLE_GROUPS),
            "direction": (
                "bullish" if vote_total > 0
                else "bearish" if vote_total < 0
                else "neutral"
            ),
            "semantics": "one_vote_per_role_group_not_probability",
        },
        "rolling_24h_background": {
            "data": dict(rolling_24h or {}),
            "counts_toward_vote": False,
            "semantics": "rolling_24h_background_only",
        },
        "identity_inference": "not_performed",
    }


__all__ = [
    "MULTI_TIMEFRAME_VERSION",
    "ROLE_GROUPS",
    "TIMEFRAME_INTERVAL_MS",
    "analyze_multi_timeframe",
    "expand_timeframe_klines",
]
