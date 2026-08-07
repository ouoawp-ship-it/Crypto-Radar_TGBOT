"""Deterministic 1h/4h SMC confirmation for launch-warning candidates.

The 15-minute discovery score remains authoritative.  This module only
rejects a new publication when closed higher-timeframe structure explicitly
opposes the candidate.  Missing or discontinuous data always fails open and
is reported as insufficient instead of being guessed.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from .smc_overlay import HOUR_SEC, build_smc_overlay


FILTER_VERSION = "launch_smc_filter_v1"
STATUS_SUPPORTIVE = "supportive"
STATUS_NEUTRAL = "neutral"
STATUS_CONFLICTING = "conflicting"
STATUS_INSUFFICIENT = "insufficient"

_TIMEFRAME_SETTINGS = {
    "1h": {"interval_sec": HOUR_SEC, "valuation_bars": 72, "max_age_bars": 72},
    "4h": {
        "interval_sec": 4 * HOUR_SEC,
        "valuation_bars": 72,
        "max_age_bars": 42,
    },
}


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def closed_candles_from_binance(
    rows: Sequence[Sequence[Any]],
    *,
    window_end_ms: int,
    interval_ms: int,
) -> list[dict[str, float | int]]:
    """Convert only fully closed Binance kline rows to domain candles."""

    candles: list[dict[str, float | int]] = []
    for row in rows:
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)):
            continue
        if len(row) < 7:
            continue
        open_time = _finite(row[0])
        close_time = _finite(row[6])
        prices = [_finite(row[index]) for index in (1, 2, 3, 4)]
        if (
            open_time is None
            or close_time is None
            or not open_time.is_integer()
            or not close_time.is_integer()
            or int(open_time) < 0
            or int(open_time) % int(interval_ms) != 0
            or int(close_time) != int(open_time) + int(interval_ms) - 1
            or any(value is None for value in prices)
        ):
            continue
        if int(close_time) >= int(window_end_ms):
            continue
        open_price, high, low, close = (float(value) for value in prices)
        candles.append({
            "close_ts": int(close_time) // 1000 + 1,
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
        })
    return candles


def _signal_side(signal: Mapping[str, Any]) -> str:
    direction = str(signal.get("direction") or "").strip().lower()
    if direction.startswith("bullish"):
        return "bullish"
    if direction.startswith("bearish"):
        return "bearish"
    return ""


def _latest_swing_direction(
    overlay: Mapping[str, Any],
    *,
    interval_sec: int,
    max_age_bars: int,
) -> str:
    last_ts = max(
        (
            int(event.get("broken_at_ts") or 0)
            for event in overlay.get("structure_events", [])
            if isinstance(event, Mapping)
        ),
        default=0,
    )
    valuation = overlay.get("valuation")
    valuation = valuation if isinstance(valuation, Mapping) else {}
    end_ts = int(valuation.get("end_ts") or 0)
    events = [
        event
        for event in overlay.get("structure_events", [])
        if isinstance(event, Mapping)
        and str(event.get("kind") or "") == "swing"
        and str(event.get("direction") or "") in {"bullish", "bearish"}
        and 0 < int(event.get("confirmed_at_ts") or 0)
        <= int(event.get("broken_at_ts") or 0)
        <= end_ts
    ]
    if not events:
        return "neutral"
    latest = max(events, key=lambda event: int(event.get("broken_at_ts") or 0))
    broken_at = int(latest.get("broken_at_ts") or 0)
    if not last_ts or end_ts - broken_at > interval_sec * max_age_bars:
        return "neutral"
    return str(latest.get("direction") or "neutral")


def _inside_opposing_swing_block(
    overlay: Mapping[str, Any],
    *,
    signal_side: str,
    current_price: float,
) -> bool:
    opposing_side = "supply" if signal_side == "bullish" else "demand"
    return any(
        str(block.get("kind") or "") == "swing"
        and str(block.get("side") or "") == opposing_side
        and float(block.get("zone_low") or 0.0)
        <= current_price
        <= float(block.get("zone_high") or 0.0)
        for block in overlay.get("active_order_blocks", [])
        if isinstance(block, Mapping)
    )


def _insufficient_result(
    *,
    signal_side: str,
    reason: str,
) -> dict[str, Any]:
    return {
        "version": FILTER_VERSION,
        "status": STATUS_INSUFFICIENT,
        "signal_direction": signal_side or "none",
        "one_hour_structure": "unavailable",
        "four_hour_structure": "unavailable",
        "opposing_zone_timeframes": [],
        "reasons": [reason],
        "data_complete": False,
        "blocks_publication": False,
        "ai_eligible": False,
        "score_adjustment": 0,
        "semantics": "higher_timeframe_filter_not_score_or_probability",
    }


def insufficient_smc_filter(
    *,
    signal: Mapping[str, Any],
    reason: str,
) -> dict[str, Any]:
    """Build a fixed, fail-open result without exposing exception text."""

    return _insufficient_result(
        signal_side=_signal_side(signal),
        reason=str(reason or "smc_filter_local_error"),
    )


def evaluate_smc_filter(
    *,
    signal: Mapping[str, Any],
    one_hour_candles: Sequence[Mapping[str, Any]],
    four_hour_candles: Sequence[Mapping[str, Any]],
    window_end_ms: int | None = None,
    allow_session_gaps: bool = False,
) -> dict[str, Any]:
    """Classify explicit higher-timeframe agreement without changing score."""

    signal_side = _signal_side(signal)
    if not signal_side:
        return _insufficient_result(
            signal_side="",
            reason="signal_direction_unavailable",
        )

    overlays: dict[str, Mapping[str, Any]] = {}
    for timeframe, candles in (
        ("1h", one_hour_candles),
        ("4h", four_hour_candles),
    ):
        settings = _TIMEFRAME_SETTINGS[timeframe]
        if window_end_ms is not None:
            interval_ms = int(settings["interval_sec"]) * 1000
            expected_last_close_ts = (
                int(window_end_ms) // interval_ms * interval_ms // 1000
            )
            actual_last_close_ts = (
                int(candles[-1].get("close_ts") or 0) if candles else 0
            )
            if actual_last_close_ts != expected_last_close_ts:
                return _insufficient_result(
                    signal_side=signal_side,
                    reason=f"{timeframe}_last_closed_window_missing",
                )
        try:
            overlay = build_smc_overlay(
                candles,
                allow_session_gaps=allow_session_gaps,
                timeframe=timeframe,
                interval_sec=int(settings["interval_sec"]),
                valuation_bars=int(settings["valuation_bars"]),
            )
        except (TypeError, ValueError):
            return _insufficient_result(
                signal_side=signal_side,
                reason=f"{timeframe}_structure_unavailable",
            )
        continuity = overlay.get("continuity")
        continuity = continuity if isinstance(continuity, Mapping) else {}
        if (
            overlay.get("status") != "ready"
            or int(continuity.get("session_gap_count") or 0) > 0
        ):
            return _insufficient_result(
                signal_side=signal_side,
                reason=f"{timeframe}_structure_incomplete",
            )
        overlays[timeframe] = overlay

    structures = {
        timeframe: _latest_swing_direction(
            overlays[timeframe],
            interval_sec=int(_TIMEFRAME_SETTINGS[timeframe]["interval_sec"]),
            max_age_bars=int(_TIMEFRAME_SETTINGS[timeframe]["max_age_bars"]),
        )
        for timeframe in ("1h", "4h")
    }
    current_price = _finite(one_hour_candles[-1].get("close")) if one_hour_candles else None
    if current_price is None or current_price <= 0:
        return _insufficient_result(
            signal_side=signal_side,
            reason="current_price_unavailable",
        )
    opposing_zones = [
        timeframe
        for timeframe in ("1h", "4h")
        if _inside_opposing_swing_block(
            overlays[timeframe],
            signal_side=signal_side,
            current_price=current_price,
        )
    ]
    opposite = "bearish" if signal_side == "bullish" else "bullish"
    opposing_structures = [
        timeframe
        for timeframe, direction in structures.items()
        if direction == opposite
    ]
    aligned = [
        timeframe
        for timeframe, direction in structures.items()
        if direction == signal_side
    ]

    reasons: list[str] = []
    # A publication is blocked only when both complete timeframes explicitly
    # oppose it.  A mixed structure or an opposing zone is counter-evidence,
    # not proof that the 15-minute discovery is false.
    if len(opposing_structures) == 2:
        status = STATUS_CONFLICTING
        reasons.extend(
            f"{timeframe}_structure_opposes_signal"
            for timeframe in opposing_structures
        )
        reasons.extend(
            f"price_inside_{timeframe}_opposing_zone"
            for timeframe in opposing_zones
        )
    elif aligned and not opposing_structures and not opposing_zones:
        status = STATUS_SUPPORTIVE
        reasons.extend(f"{timeframe}_structure_aligned" for timeframe in aligned)
    else:
        status = STATUS_NEUTRAL
        reasons.extend(f"{timeframe}_structure_aligned" for timeframe in aligned)
        reasons.extend(
            f"{timeframe}_structure_opposes_signal"
            for timeframe in opposing_structures
        )
        reasons.extend(
            f"price_inside_{timeframe}_opposing_zone"
            for timeframe in opposing_zones
        )
        reasons.extend(
            f"{timeframe}_structure_neutral"
            for timeframe, direction in structures.items()
            if direction == "neutral"
        )

    return {
        "version": FILTER_VERSION,
        "status": status,
        "signal_direction": signal_side,
        "one_hour_structure": structures["1h"],
        "four_hour_structure": structures["4h"],
        "opposing_zone_timeframes": opposing_zones,
        "reasons": reasons,
        "data_complete": True,
        "blocks_publication": status == STATUS_CONFLICTING,
        "ai_eligible": status == STATUS_SUPPORTIVE,
        "score_adjustment": 0,
        "semantics": "higher_timeframe_filter_not_score_or_probability",
    }


__all__ = [
    "FILTER_VERSION",
    "STATUS_CONFLICTING",
    "STATUS_INSUFFICIENT",
    "STATUS_NEUTRAL",
    "STATUS_SUPPORTIVE",
    "closed_candles_from_binance",
    "evaluate_smc_filter",
    "insufficient_smc_filter",
]
