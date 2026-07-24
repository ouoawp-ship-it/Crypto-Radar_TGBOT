from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .launch_smc import advance_smc_state, analyze_smc_frames


BASE_INTERVAL_MS = 15 * 60 * 1000
PRICE_ACTION_VERSION = 1
ACTIVE_SEQUENCE_STATUSES = {"breakout_15m", "confirmed_1h"}


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class Candle:
    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    close_time_ms: int


def required_15m_kline_limit(
    lookback: int,
    *,
    follow_up: bool,
    smc_history_bars: int = 0,
) -> int:
    """Keep the legacy 17-bar scan cheap; active cycles also need a full 1h box."""

    if not follow_up:
        return max(17, int(lookback) + 1)
    return max(
        17,
        (max(2, int(lookback)) + 2) * 4,
        max(0, int(smc_history_bars)),
    )


def _parse_closed_klines(
    klines: Sequence[Sequence[Any]],
    *,
    window_end_ms: int,
) -> list[Candle]:
    candles: dict[int, Candle] = {}
    for row in klines:
        if len(row) < 7:
            continue
        open_time_ms = int(_number(row[0]))
        close_time_ms = int(_number(row[6]))
        open_price = _number(row[1])
        high = _number(row[2])
        low = _number(row[3])
        close = _number(row[4])
        if (
            open_time_ms < 0
            or close_time_ms >= int(window_end_ms)
            or min(open_price, high, low, close) <= 0
            or high < max(open_price, close)
            or low > min(open_price, close)
        ):
            continue
        candles[open_time_ms] = Candle(
            open_time_ms=open_time_ms,
            open=open_price,
            high=high,
            low=low,
            close=close,
            close_time_ms=close_time_ms,
        )
    return [candles[key] for key in sorted(candles)]


def _aggregate_closed(
    candles: Sequence[Candle],
    *,
    factor: int,
    window_end_ms: int,
) -> list[Candle]:
    interval_ms = BASE_INTERVAL_MS * max(1, int(factor))
    groups: dict[int, dict[int, Candle]] = {}
    for candle in candles:
        bucket_start = candle.open_time_ms // interval_ms * interval_ms
        if bucket_start + interval_ms > int(window_end_ms):
            continue
        groups.setdefault(bucket_start, {})[candle.open_time_ms] = candle

    aggregated: list[Candle] = []
    for bucket_start in sorted(groups):
        group = groups[bucket_start]
        expected = [
            bucket_start + index * BASE_INTERVAL_MS
            for index in range(max(1, int(factor)))
        ]
        if any(open_time not in group for open_time in expected):
            continue
        ordered = [group[open_time] for open_time in expected]
        aggregated.append(Candle(
            open_time_ms=bucket_start,
            open=ordered[0].open,
            high=max(candle.high for candle in ordered),
            low=min(candle.low for candle in ordered),
            close=ordered[-1].close,
            close_time_ms=bucket_start + interval_ms - 1,
        ))
    return aggregated


def _wick_to_body(wick: float, body: float, candle_range: float) -> float:
    denominator = body if body > 0 else candle_range * 0.01
    if denominator <= 0:
        return 0.0
    return min(999.0, wick / denominator)


def _frame(
    candles: Sequence[Candle],
    *,
    lookback: int,
    interval_ms: int,
    max_box_range_pct: float,
    min_body_ratio: float,
    wick_body_ratio: float,
) -> dict[str, Any]:
    if not candles:
        return {"data_status": "unavailable", "event": "unavailable"}

    current = candles[-1]
    candle_range = current.high - current.low
    body = abs(current.close - current.open)
    upper_wick = current.high - max(current.open, current.close)
    lower_wick = min(current.open, current.close) - current.low
    metrics: dict[str, Any] = {
        "data_status": "insufficient_history",
        "event": "insufficient_history",
        "candle_end_ts": int((current.close_time_ms + 1) / 1000),
        "open": current.open,
        "high": current.high,
        "low": current.low,
        "close": current.close,
        "body_ratio": round(body / candle_range, 6) if candle_range > 0 else 0.0,
        "upper_wick_body_ratio": round(
            _wick_to_body(upper_wick, body, candle_range),
            6,
        ),
        "lower_wick_body_ratio": round(
            _wick_to_body(lower_wick, body, candle_range),
            6,
        ),
    }
    required = max(2, int(lookback)) + 1
    if len(candles) < required:
        return metrics
    recent = list(candles[-required:])
    if any(
        recent[index].open_time_ms - recent[index - 1].open_time_ms != interval_ms
        for index in range(1, len(recent))
    ):
        metrics["data_status"] = "gap"
        metrics["event"] = "gap"
        return metrics

    reference = recent[:-1]
    box_high = max(candle.high for candle in reference)
    box_low = min(candle.low for candle in reference)
    midpoint = (box_high + box_low) / 2.0
    width_pct = (
        (box_high - box_low) / midpoint * 100.0
        if midpoint > 0
        else 0.0
    )
    consolidation = width_pct <= max(0.0, float(max_box_range_pct))
    metrics.update({
        "data_status": "ready",
        "box_high": box_high,
        "box_low": box_low,
        "box_width_pct": round(width_pct, 6),
        "consolidation": consolidation,
    })
    if not consolidation:
        metrics["event"] = "range_wide"
        return metrics

    body_confirmed_up = (
        current.close > current.open
        and metrics["body_ratio"] >= max(0.0, float(min_body_ratio))
    )
    body_confirmed_down = (
        current.close < current.open
        and metrics["body_ratio"] >= max(0.0, float(min_body_ratio))
    )
    if current.close > box_high:
        metrics["event"] = (
            "breakout_up" if body_confirmed_up else "close_outside_up"
        )
    elif current.close < box_low:
        metrics["event"] = (
            "breakout_down" if body_confirmed_down else "close_outside_down"
        )
    else:
        swept_high = (
            current.high > box_high
            and metrics["upper_wick_body_ratio"] >= float(wick_body_ratio)
        )
        swept_low = (
            current.low < box_low
            and metrics["lower_wick_body_ratio"] >= float(wick_body_ratio)
        )
        if swept_high and swept_low:
            metrics["event"] = (
                "sweep_high"
                if metrics["upper_wick_body_ratio"] >= metrics["lower_wick_body_ratio"]
                else "sweep_low"
            )
        elif swept_high:
            metrics["event"] = "sweep_high"
        elif swept_low:
            metrics["event"] = "sweep_low"
        else:
            metrics["event"] = "inside"
    return metrics


def analyze_launch_price_action(
    klines: Sequence[Sequence[Any]],
    *,
    window_end_ms: int,
    lookback: int = 16,
    max_box_range_pct: float = 12.0,
    min_body_ratio: float = 0.45,
    wick_body_ratio: float = 1.5,
    smc_enable: bool = False,
    smc_swing_length: int = 2,
    smc_equal_tolerance_atr: float = 0.15,
    smc_displacement_body_atr: float = 1.0,
    smc_max_zone_age_bars: int = 96,
) -> dict[str, Any]:
    """Analyze only Binance candles that closed before the requested boundary."""

    lookback = max(2, int(lookback))
    max_box_range_pct = max(0.0, float(max_box_range_pct))
    min_body_ratio = max(0.0, float(min_body_ratio))
    wick_body_ratio = max(0.0, float(wick_body_ratio))
    base = _parse_closed_klines(klines, window_end_ms=window_end_ms)
    timeframes: dict[str, dict[str, Any]] = {}
    smc_candles: dict[str, list[dict[str, Any]]] = {}
    for name, factor in (("15m", 1), ("1h", 4), ("4h", 16)):
        candles = (
            base
            if factor == 1
            else _aggregate_closed(
                base,
                factor=factor,
                window_end_ms=window_end_ms,
            )
        )
        smc_candles[name] = [
            {
                "open_ts": candle.open_time_ms // 1000,
                "close_ts": int((candle.close_time_ms + 1) / 1000),
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
            }
            for candle in candles
        ]
        timeframes[name] = _frame(
            candles,
            lookback=lookback,
            interval_ms=BASE_INTERVAL_MS * factor,
            max_box_range_pct=max_box_range_pct,
            min_body_ratio=min_body_ratio,
            wick_body_ratio=wick_body_ratio,
        )
    result = {
        "version": PRICE_ACTION_VERSION,
        "data_status": timeframes["15m"].get("data_status", "unavailable"),
        "lookback": lookback,
        "max_box_range_pct": max_box_range_pct,
        "min_body_ratio": min_body_ratio,
        "wick_body_ratio": wick_body_ratio,
        "timeframes": timeframes,
    }
    if smc_enable:
        result["smc_analysis"] = analyze_smc_frames(
            smc_candles,
            swing_length=smc_swing_length,
            equal_tolerance_atr=smc_equal_tolerance_atr,
            displacement_body_atr=smc_displacement_body_atr,
            max_zone_age_bars=smc_max_zone_age_bars,
        )
    return result


def _level_result(
    frame: Mapping[str, Any],
    *,
    direction: str,
    level: float,
    min_body_ratio: float,
    wick_body_ratio: float,
) -> str:
    open_price = _number(frame.get("open"))
    high = _number(frame.get("high"))
    low = _number(frame.get("low"))
    close = _number(frame.get("close"))
    body_ratio = _number(frame.get("body_ratio"))
    if direction == "up":
        if (
            high > level
            and close <= level
            and _number(frame.get("upper_wick_body_ratio")) >= wick_body_ratio
        ):
            return "sweep"
        if close <= level:
            return "failed"
        if close > open_price and body_ratio >= min_body_ratio:
            return "confirmed"
        return "pending"
    if (
        low < level
        and close >= level
        and _number(frame.get("lower_wick_body_ratio")) >= wick_body_ratio
    ):
        return "sweep"
    if close >= level:
        return "failed"
    if close < open_price and body_ratio >= min_body_ratio:
        return "confirmed"
    return "pending"


def advance_price_action_state(
    previous: Mapping[str, Any] | None,
    analysis: Mapping[str, Any] | None,
) -> dict[str, Any]:
    previous_state = dict(previous or {})
    if not isinstance(analysis, Mapping):
        if previous_state:
            previous_state["data_status"] = "unavailable"
            return previous_state
        return {
            "enabled": True,
            "version": PRICE_ACTION_VERSION,
            "data_status": "unavailable",
            "status": "watching",
            "event_key": "",
            "timeframes": {},
        }

    frames = analysis.get("timeframes")
    if not isinstance(frames, Mapping):
        frames = {}
    state = {
        **previous_state,
        "enabled": True,
        "version": PRICE_ACTION_VERSION,
        "data_status": str(analysis.get("data_status") or "unavailable"),
        "lookback": max(2, int(_number(analysis.get("lookback")) or 16)),
        "status": str(previous_state.get("status") or "watching"),
        "event_key": str(previous_state.get("event_key") or ""),
        "timeframes": dict(frames),
    }
    previous_smc = previous_state.get("smc")
    smc_analysis = analysis.get("smc_analysis")
    if isinstance(smc_analysis, Mapping) or isinstance(previous_smc, Mapping):
        smc_state = advance_smc_state(
            previous_smc if isinstance(previous_smc, Mapping) else None,
            smc_analysis if isinstance(smc_analysis, Mapping) else None,
        )
        state["smc"] = smc_state
        state["smc_event_key"] = str(smc_state.get("event_key") or "")
    frame_15m = frames.get("15m")
    if not isinstance(frame_15m, Mapping):
        return state

    status = str(state["status"])
    event = str(frame_15m.get("event") or "")
    candle_end_ts = int(_number(frame_15m.get("candle_end_ts")))
    min_body_ratio = _number(analysis.get("min_body_ratio"))
    wick_body_ratio = _number(analysis.get("wick_body_ratio"))

    if status not in ACTIVE_SEQUENCE_STATUSES:
        if event in {"breakout_up", "breakout_down"}:
            direction = "up" if event == "breakout_up" else "down"
            level_key = "box_high" if direction == "up" else "box_low"
            level = _number(frame_15m.get(level_key))
            if level > 0 and candle_end_ts > 0:
                lookback = int(state["lookback"])
                state.update({
                    "status": "breakout_15m",
                    "direction": direction,
                    "level": level,
                    "box_high": _number(frame_15m.get("box_high")),
                    "box_low": _number(frame_15m.get("box_low")),
                    "box_start_ts": candle_end_ts - lookback * 15 * 60,
                    "box_end_ts": candle_end_ts - 15 * 60,
                    "trigger_window_end_ts": candle_end_ts,
                    "event_window_end_ts": candle_end_ts,
                    "confirmed_timeframes": ["15m"],
                    "confirmation_ends": {"15m": candle_end_ts},
                    "last_checked": {},
                    "event_key": f"breakout_15m:{direction}:{candle_end_ts}:{level:.12g}",
                })
            return state
        if event in {"sweep_high", "sweep_low"} and candle_end_ts > 0:
            level_key = "box_high" if event == "sweep_high" else "box_low"
            level = _number(frame_15m.get(level_key))
            lookback = int(state["lookback"])
            state.update({
                "status": f"{event}_15m",
                "direction": "down" if event == "sweep_high" else "up",
                "level": level,
                "box_high": _number(frame_15m.get("box_high")),
                "box_low": _number(frame_15m.get("box_low")),
                "box_start_ts": candle_end_ts - lookback * 15 * 60,
                "box_end_ts": candle_end_ts - 15 * 60,
                "trigger_window_end_ts": candle_end_ts,
                "event_window_end_ts": candle_end_ts,
                "confirmed_timeframes": [],
                "confirmation_ends": {},
                "last_checked": {},
                "event_key": f"{event}_15m:{candle_end_ts}:{level:.12g}",
            })
        return state

    direction = str(state.get("direction") or "")
    level = _number(state.get("level"))
    trigger_end = int(_number(state.get("trigger_window_end_ts")))
    if direction not in {"up", "down"} or level <= 0 or trigger_end <= 0:
        state["status"] = "watching"
        state["event_key"] = ""
        return state

    if status == "breakout_15m" and candle_end_ts > trigger_end:
        result_15m = _level_result(
            frame_15m,
            direction=direction,
            level=level,
            min_body_ratio=min_body_ratio,
            wick_body_ratio=wick_body_ratio,
        )
        if result_15m in {"sweep", "failed"}:
            next_status = (
                "false_breakout_15m"
                if result_15m == "sweep"
                else "failed_breakout_15m"
            )
            state["status"] = next_status
            state["event_window_end_ts"] = candle_end_ts
            state["event_key"] = (
                f"{next_status}:{direction}:{candle_end_ts}:{level:.12g}"
            )
            return state

    next_timeframe = "1h" if status == "breakout_15m" else "4h"
    frame = frames.get(next_timeframe)
    if not isinstance(frame, Mapping):
        return state
    frame_end = int(_number(frame.get("candle_end_ts")))
    confirmation_ends = dict(state.get("confirmation_ends") or {})
    prior_timeframe = "15m" if next_timeframe == "1h" else "1h"
    minimum_end = int(_number(confirmation_ends.get(prior_timeframe)))
    last_checked = dict(state.get("last_checked") or {})
    if (
        frame_end <= minimum_end
        or frame_end <= int(_number(last_checked.get(next_timeframe)))
    ):
        return state
    last_checked[next_timeframe] = frame_end
    state["last_checked"] = last_checked

    result = _level_result(
        frame,
        direction=direction,
        level=level,
        min_body_ratio=min_body_ratio,
        wick_body_ratio=wick_body_ratio,
    )
    if result == "confirmed":
        next_status = f"confirmed_{next_timeframe}"
        confirmations = list(state.get("confirmed_timeframes") or [])
        if next_timeframe not in confirmations:
            confirmations.append(next_timeframe)
        confirmation_ends[next_timeframe] = frame_end
        state.update({
            "status": next_status,
            "confirmed_timeframes": confirmations,
            "confirmation_ends": confirmation_ends,
            "event_window_end_ts": frame_end,
            "event_key": (
                f"{next_status}:{direction}:{frame_end}:{level:.12g}"
            ),
        })
    elif result in {"sweep", "failed"}:
        next_status = (
            f"false_breakout_{next_timeframe}"
            if result == "sweep"
            else f"failed_breakout_{next_timeframe}"
        )
        state["status"] = next_status
        state["event_window_end_ts"] = frame_end
        state["event_key"] = (
            f"{next_status}:{direction}:{frame_end}:{level:.12g}"
        )
    return state


__all__ = [
    "PRICE_ACTION_VERSION",
    "advance_price_action_state",
    "analyze_launch_price_action",
    "required_15m_kline_limit",
]
