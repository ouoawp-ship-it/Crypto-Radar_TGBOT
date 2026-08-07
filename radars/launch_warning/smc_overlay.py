"""Independent, closed-candle SMC facts used only by the launch chart.

This module does not participate in the 15-minute alert trigger or score.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


OVERLAY_VERSION = 1
HOUR_SEC = 60 * 60
INTERNAL_SIZE = 5
SWING_SIZE = 20
VALUATION_BARS = 72
ATR_PERIOD = 200
ATR_MIN_PERIODS = 30
HIGH_VOLATILITY_MULTIPLIER = 2.0
SESSION_GAP_MIN_HOURS = 8
SESSION_GAP_MAX_HOURS = 96


@dataclass(frozen=True)
class _Candle:
    close_ts: int
    open: float
    high: float
    low: float
    close: float


@dataclass
class _Pivot:
    kind: str
    side: str
    price: float
    origin_index: int
    confirmed_index: int
    broken_index: int | None = None


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _normalize(
    candles: Sequence[Mapping[str, Any]],
    *,
    allow_session_gaps: bool,
) -> list[_Candle]:
    normalized: list[_Candle] = []
    for item in candles:
        if not isinstance(item, Mapping):
            raise ValueError("smc_overlay_candle_invalid")
        timestamp = _finite(item.get("close_ts"))
        prices = [_finite(item.get(key)) for key in ("open", "high", "low", "close")]
        if (
            timestamp is None
            or not timestamp.is_integer()
            or timestamp <= 0
            or any(price is None or price <= 0 for price in prices)
        ):
            raise ValueError("smc_overlay_candle_invalid")
        open_price, high, low, close = (float(price) for price in prices)
        if high < max(open_price, close) or low > min(open_price, close):
            raise ValueError("smc_overlay_candle_invalid")
        normalized.append(_Candle(
            close_ts=int(timestamp),
            open=open_price,
            high=high,
            low=low,
            close=close,
        ))

    normalized.sort(key=lambda candle: candle.close_ts)
    for previous, current in zip(normalized, normalized[1:]):
        if current.close_ts == previous.close_ts:
            raise ValueError("smc_overlay_candle_duplicate")
        delta = current.close_ts - previous.close_ts
        if delta % HOUR_SEC:
            raise ValueError("smc_overlay_candle_cadence_invalid")
        delta_hours = delta // HOUR_SEC
        if delta_hours == 1:
            continue
        # Only explicitly identified session-based products may bridge a
        # plausible overnight/weekend closure. Small holes are treated as
        # missing data, and very long gaps cannot masquerade as current SMC.
        if (
            not allow_session_gaps
            or delta_hours < SESSION_GAP_MIN_HOURS
            or delta_hours > SESSION_GAP_MAX_HOURS
        ):
            raise ValueError("smc_overlay_candle_gap")
    return normalized


def _true_range_average(candles: Sequence[_Candle]) -> list[float | None]:
    true_ranges: list[float] = []
    averages: list[float | None] = []
    rolling_sum = 0.0
    for index, candle in enumerate(candles):
        previous_close = candles[index - 1].close if index else candle.close
        true_range = max(
            candle.high - candle.low,
            abs(candle.high - previous_close),
            abs(candle.low - previous_close),
        )
        true_ranges.append(true_range)
        rolling_sum += true_range
        if len(true_ranges) > ATR_PERIOD:
            rolling_sum -= true_ranges[-ATR_PERIOD - 1]
        sample_count = min(len(true_ranges), ATR_PERIOD)
        averages.append(
            rolling_sum / sample_count
            if sample_count >= ATR_MIN_PERIODS
            else None
        )
    return averages


def _confirmed_pivots(candles: Sequence[_Candle]) -> list[_Pivot]:
    pivots: list[_Pivot] = []
    for kind, size in (("internal", INTERNAL_SIZE), ("swing", SWING_SIZE)):
        leg = 0
        for confirmed_index in range(size, len(candles)):
            origin_index = confirmed_index - size
            origin = candles[origin_index]
            following = candles[origin_index + 1:confirmed_index + 1]
            high_candidate = origin.high > max(candle.high for candle in following)
            low_candidate = origin.low < min(candle.low for candle in following)
            next_leg = 0 if high_candidate else 1 if low_candidate else leg
            if next_leg == leg:
                continue
            side = "high" if next_leg == 0 else "low"
            pivots.append(_Pivot(
                kind=kind,
                side=side,
                price=origin.high if side == "high" else origin.low,
                origin_index=origin_index,
                confirmed_index=confirmed_index,
            ))
            leg = next_leg
    pivots.sort(key=lambda pivot: (
        pivot.confirmed_index,
        0 if pivot.kind == "internal" else 1,
        pivot.origin_index,
        pivot.side,
    ))
    return pivots


def _structure_events(
    candles: Sequence[_Candle],
    pivots: Sequence[_Pivot],
) -> list[dict[str, Any]]:
    by_confirmation: dict[int, list[_Pivot]] = {}
    for pivot in pivots:
        by_confirmation.setdefault(pivot.confirmed_index, []).append(pivot)

    latest: dict[tuple[str, str], _Pivot] = {}
    direction_by_kind: dict[str, str] = {}
    events: list[dict[str, Any]] = []
    for index, candle in enumerate(candles):
        for pivot in by_confirmation.get(index, []):
            latest[(pivot.kind, pivot.side)] = pivot
        for kind in ("internal", "swing"):
            for side, direction in (("high", "bullish"), ("low", "bearish")):
                pivot = latest.get((kind, side))
                if pivot is None or pivot.broken_index is not None:
                    continue
                broken = candle.close > pivot.price if side == "high" else candle.close < pivot.price
                if not broken:
                    continue
                pivot.broken_index = index
                previous_direction = direction_by_kind.get(kind)
                event = (
                    "structure_turn"
                    if previous_direction is not None and previous_direction != direction
                    else "continuation"
                )
                direction_by_kind[kind] = direction
                events.append({
                    "kind": kind,
                    "direction": direction,
                    "event": event,
                    "level": pivot.price,
                    "origin_ts": candles[pivot.origin_index].close_ts,
                    "confirmed_at_ts": candles[pivot.confirmed_index].close_ts,
                    "broken_at_ts": candle.close_ts,
                    "_origin_index": pivot.origin_index,
                    "_broken_index": index,
                })
    return events


def _active_order_blocks(
    candles: Sequence[_Candle],
    events: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    atr = _true_range_average(candles)
    parsed_high: list[float] = []
    parsed_low: list[float] = []
    for candle, average in zip(candles, atr):
        high_volatility = (
            average is not None
            and candle.high - candle.low >= HIGH_VOLATILITY_MULTIPLIER * average
        )
        parsed_high.append(candle.low if high_volatility else candle.high)
        parsed_low.append(candle.high if high_volatility else candle.low)

    active: dict[tuple[str, int], dict[str, Any]] = {}
    for event in events:
        origin_index = int(event["_origin_index"])
        broken_index = int(event["_broken_index"])
        if broken_index <= origin_index:
            continue
        direction = str(event["direction"])
        candidates = range(origin_index, broken_index)
        if direction == "bullish":
            source_index = min(candidates, key=lambda index: (parsed_low[index], index))
        else:
            source_index = max(candidates, key=lambda index: (parsed_high[index], -index))
        source = candles[source_index]
        invalidated = any(
            later.low < source.low
            if direction == "bullish"
            else later.high > source.high
            for later in candles[broken_index + 1:]
        )
        if invalidated:
            continue
        block = {
            "direction": direction,
            "side": "demand" if direction == "bullish" else "supply",
            "zone_low": source.low,
            "zone_high": source.high,
            "origin_ts": source.close_ts,
            "confirmed_at_ts": int(event["confirmed_at_ts"]),
            "broken_at_ts": int(event["broken_at_ts"]),
            "state": "active",
        }
        active[(direction, source.close_ts)] = block
    return sorted(
        active.values(),
        key=lambda block: (int(block["broken_at_ts"]), int(block["origin_ts"])),
    )


def _valuation(candles: Sequence[_Candle]) -> dict[str, Any]:
    if not candles:
        return {
            "data_status": "insufficient_history",
            "requested_bars": VALUATION_BARS,
            "window_bars": 0,
            "start_ts": None,
            "end_ts": None,
            "range_low": None,
            "range_high": None,
            "midpoint": None,
            "zones": {},
        }
    window = list(candles[-VALUATION_BARS:])
    range_low = min(candle.low for candle in window)
    range_high = max(candle.high for candle in window)
    span = range_high - range_low
    return {
        "data_status": "complete" if len(window) == VALUATION_BARS else "insufficient_history",
        "requested_bars": VALUATION_BARS,
        "window_bars": len(window),
        "start_ts": window[0].close_ts,
        "end_ts": window[-1].close_ts,
        "range_low": range_low,
        "range_high": range_high,
        "midpoint": (range_low + range_high) / 2.0,
        "zones": {
            "low": {"low": range_low, "high": range_low + span * 0.05},
            "mid": {
                "low": range_low + span * 0.475,
                "high": range_low + span * 0.525,
            },
            "high": {"low": range_low + span * 0.95, "high": range_high},
        },
    }


def build_smc_overlay(
    candles: Sequence[Mapping[str, Any]],
    *,
    allow_session_gaps: bool = False,
) -> dict[str, Any]:
    """Build deterministic SMC drawing facts from closed hourly candles."""

    normalized = _normalize(candles, allow_session_gaps=allow_session_gaps)
    gap_hours = [
        (current.close_ts - previous.close_ts) // HOUR_SEC - 1
        for previous, current in zip(normalized, normalized[1:])
        if current.close_ts - previous.close_ts > HOUR_SEC
    ]
    pivots = _confirmed_pivots(normalized)
    events = _structure_events(normalized, pivots)
    valuation = _valuation(normalized)
    public_events = [
        {key: value for key, value in event.items() if not key.startswith("_")}
        for event in events
    ]
    return {
        "version": OVERLAY_VERSION,
        "status": (
            "ready"
            if valuation["data_status"] == "complete"
            else "insufficient_history"
        ),
        "timeframe": "1h",
        "closed_candles": len(normalized),
        "continuity": {
            "session_gap_count": len(gap_hours),
            "missing_session_hours": sum(gap_hours),
            "largest_gap_hours": max(gap_hours, default=0),
        },
        "pivot_lengths": {"internal": INTERNAL_SIZE, "swing": SWING_SIZE},
        "pivots": [{
            "kind": pivot.kind,
            "side": pivot.side,
            "price": pivot.price,
            "origin_ts": normalized[pivot.origin_index].close_ts,
            "confirmed_at_ts": normalized[pivot.confirmed_index].close_ts,
            "broken_at_ts": (
                normalized[pivot.broken_index].close_ts
                if pivot.broken_index is not None
                else None
            ),
        } for pivot in pivots],
        "structure_events": public_events,
        "active_order_blocks": _active_order_blocks(normalized, events),
        "valuation": valuation,
    }


__all__ = ["build_smc_overlay"]
