from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from .directional_model import MIN_RISK_REWARD_RATIO


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def select_directional_candidates(
    items: Sequence[Mapping[str, Any]],
    *,
    limit: int,
) -> list[str]:
    """Choose a bounded deep-analysis set without changing 15m discovery.

    The caller supplies active lifecycle items from oldest ``last_window_end``
    to newest.  Preserve that order so an intense active symbol cannot occupy a
    deep-analysis slot forever while an older, quieter cycle is never checked.
    New candidates remain ordered by their current evidence strength.
    """

    active_symbols: list[str] = []
    new_candidates: list[tuple[float, str]] = []
    seen: set[str] = set()
    for item in items:
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        if item.get("launch_lifecycle_active"):
            active_symbols.append(symbol)
            continue
        score = _finite(item.get("discovery_score"))
        if score is None:
            score = _finite(item.get("score")) or 0.0
        price = abs(_finite(item.get("price_1h")) or 0.0)
        oi = abs(_finite(item.get("oi_1h")) or 0.0)
        spot = abs(_finite(item.get("spot_active_ratio")) or 0.0) * 100.0
        futures = abs(_finite(item.get("futures_active_ratio")) or 0.0) * 100.0
        priority = max(score, price * 8.0 + oi * 5.0 + spot + futures)
        new_candidates.append((priority, symbol))
    new_candidates.sort(key=lambda row: (-row[0], row[1]))
    ordered = [
        *active_symbols,
        *(symbol for _priority, symbol in new_candidates),
    ]
    return ordered[: max(0, int(limit))]


def active_flow_window(
    rows: Sequence[Sequence[Any]],
    *,
    interval_ms: int,
    window_end_ms: int,
    periods: int,
) -> dict[str, Any]:
    """Calculate closed-window CVD proxy from Binance taker-buy quote volume."""

    expected = max(1, int(periods))
    normalized: dict[int, Sequence[Any]] = {}
    for row in rows:
        if (
            not isinstance(row, Sequence)
            or isinstance(row, (str, bytes))
            or len(row) < 11
        ):
            continue
        opened = _finite(row[0])
        closed = _finite(row[6])
        gross = _finite(row[7])
        taker_buy = _finite(row[10])
        if (
            opened is None
            or closed is None
            or not opened.is_integer()
            or not closed.is_integer()
            or int(closed) != int(opened) + interval_ms - 1
            or gross is None
            or taker_buy is None
            or gross < 0
            or taker_buy < 0
            or taker_buy > gross
        ):
            continue
        normalized[int(opened)] = row
    start = int(window_end_ms) - expected * interval_ms
    opens = [start + index * interval_ms for index in range(expected)]
    selected = [normalized.get(opened) for opened in opens]
    if any(row is None for row in selected):
        return {
            "status": "window_incomplete",
            "net_usd": None,
            "gross_usd": None,
            "ratio": None,
            "periods": 0,
        }
    gross_total = sum(float(row[7]) for row in selected if row is not None)
    taker_total = sum(float(row[10]) for row in selected if row is not None)
    net = taker_total - (gross_total - taker_total)
    return {
        "status": "available" if gross_total > 0 else "no_trades",
        "net_usd": net,
        "gross_usd": gross_total,
        "ratio": max(-1.0, min(1.0, net / gross_total)) if gross_total else 0.0,
        "periods": expected,
        "semantics": "taker_trade_imbalance_not_wallet_capital_flow",
    }


def build_trade_plans(multi_timeframe: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    frames = multi_timeframe.get("timeframes")
    frames = frames if isinstance(frames, Mapping) else {}
    frame_1h = frames.get("1h")
    frame_1h = frame_1h if isinstance(frame_1h, Mapping) else {}
    frame_4h = frames.get("4h")
    frame_4h = frame_4h if isinstance(frame_4h, Mapping) else {}
    current = _finite(frame_1h.get("last_close"))
    atr = _finite(frame_1h.get("atr"))
    unavailable = {
        "status": "unavailable",
        "entry_zone": None,
        "invalidation_price": None,
        "targets": [],
        "risk_reward_ratio": None,
    }
    if current is None or current <= 0 or atr is None or atr <= 0:
        return {"bullish": dict(unavailable), "bearish": dict(unavailable)}

    structure_lows: list[float] = []
    structure_highs: list[float] = []
    for frame in (frame_1h, frame_4h):
        reference_low = _finite(frame.get("reference_low"))
        reference_high = _finite(frame.get("reference_high"))
        if (
            reference_low is None
            or reference_high is None
            or reference_low <= 0
            or reference_high <= 0
            or reference_low >= reference_high
        ):
            continue
        structure_lows.append(reference_low)
        structure_highs.append(reference_high)
    if not structure_lows or not structure_highs:
        return {"bullish": dict(unavailable), "bearish": dict(unavailable)}

    bull_entry_low = current - atr * 0.15
    bull_entry_high = current + atr * 0.10
    bull_mid = (bull_entry_low + bull_entry_high) / 2.0
    bull_supports = [value for value in structure_lows if value < bull_entry_low]
    bull_resistances = [
        value for value in structure_highs if value > bull_entry_high
    ]
    bull_invalidation = max(bull_supports) if bull_supports else None
    bull_target = min(bull_resistances) if bull_resistances else None

    bear_entry_low = current - atr * 0.10
    bear_entry_high = current + atr * 0.15
    bear_mid = (bear_entry_low + bear_entry_high) / 2.0
    bear_supports = [value for value in structure_lows if value < bear_entry_low]
    bear_resistances = [
        value for value in structure_highs if value > bear_entry_high
    ]
    bear_target = max(bear_supports) if bear_supports else None
    bear_invalidation = min(bear_resistances) if bear_resistances else None

    def plan(
        direction: str,
        low: float,
        high: float,
        invalidation: float | None,
        target: float | None,
    ) -> dict[str, Any]:
        if invalidation is None or target is None:
            return dict(unavailable)
        midpoint = (low + high) / 2.0
        risk = (
            midpoint - invalidation
            if direction == "bullish"
            else invalidation - midpoint
        )
        reward = (
            target - midpoint
            if direction == "bullish"
            else midpoint - target
        )
        values = (low, high, midpoint, invalidation, target, risk, reward)
        if (
            any(not math.isfinite(value) or value <= 0 for value in values)
            or low > high
            or (direction == "bullish" and invalidation >= low)
            or (direction == "bearish" and invalidation <= high)
            or (direction == "bullish" and target <= high)
            or (direction == "bearish" and target >= low)
        ):
            return dict(unavailable)
        risk_reward_ratio = reward / risk
        if (
            not math.isfinite(risk_reward_ratio)
            or risk_reward_ratio < MIN_RISK_REWARD_RATIO
        ):
            return dict(unavailable)
        sign = 1.0 if direction == "bullish" else -1.0
        targets = [target]
        extension_target = target + sign * reward
        if math.isfinite(extension_target) and extension_target > 0:
            targets.append(extension_target)
        return {
            "status": "available",
            "direction": direction,
            "entry_zone": {"low": low, "high": high},
            "invalidation_price": invalidation,
            "targets": targets,
            "risk_reward_ratio": risk_reward_ratio,
            "risk_reward_target": target,
            "source": "closed_1h_4h_structure_space",
            "semantics": "observation_plan_not_order",
        }

    return {
        "bullish": plan(
            "bullish",
            bull_entry_low,
            bull_entry_high,
            bull_invalidation,
            bull_target,
        ),
        "bearish": plan(
            "bearish",
            bear_entry_low,
            bear_entry_high,
            bear_invalidation,
            bear_target,
        ),
    }


def build_directional_facts(
    item: Mapping[str, Any],
    multi_timeframe: Mapping[str, Any],
    *,
    spot_flow: Mapping[str, Any],
    futures_flow: Mapping[str, Any],
    trade_plans: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    frames = multi_timeframe.get("timeframes")
    frames = frames if isinstance(frames, Mapping) else {}
    groups = multi_timeframe.get("role_groups")
    groups = groups if isinstance(groups, Mapping) else {}
    macro = groups.get("macro_direction")
    macro = macro if isinstance(macro, Mapping) else {}
    main = groups.get("main_structure")
    main = main if isinstance(main, Mapping) else {}
    confirmation = groups.get("confirmation")
    confirmation = confirmation if isinstance(confirmation, Mapping) else {}
    trigger = groups.get("trigger")
    trigger = trigger if isinstance(trigger, Mapping) else {}
    entry = groups.get("entry")
    entry = entry if isinstance(entry, Mapping) else {}
    frame_2h = frames.get("2h")
    frame_2h = frame_2h if isinstance(frame_2h, Mapping) else {}
    frame_1h = frames.get("1h")
    frame_1h = frame_1h if isinstance(frame_1h, Mapping) else {}
    frame_4h = frames.get("4h")
    frame_4h = frame_4h if isinstance(frame_4h, Mapping) else {}
    frame_15m = frames.get("15m")
    frame_15m = frame_15m if isinstance(frame_15m, Mapping) else {}
    frame_5m = frames.get("5m")
    frame_5m = frame_5m if isinstance(frame_5m, Mapping) else {}
    bull_plan = trade_plans.get("bullish") or {}
    bear_plan = trade_plans.get("bearish") or {}
    spot_cvd_status = str(spot_flow.get("status") or "")
    futures_cvd_status = str(futures_flow.get("status") or "")
    complete_flow_ready = {
        spot_cvd_status,
        futures_cvd_status,
    } <= {"available", "no_trades"}
    base_ready = (
        str(multi_timeframe.get("status") or "") == "ok"
        and futures_cvd_status in {"available", "no_trades"}
        and item.get("funding_available") is True
        and item.get("basis_pct") is not None
    )
    return {
        "asset_category": item.get("asset_subclass") or item.get("asset_class"),
        "price_change_pct": item.get("price_1h"),
        "oi_change_pct": item.get("oi_1h"),
        "spot_cvd_ratio": spot_flow.get("ratio"),
        "futures_cvd_ratio": futures_flow.get("ratio"),
        "spot_cvd_status": spot_cvd_status,
        "futures_cvd_status": futures_cvd_status,
        "funding_rate_pct": item.get("funding_pct"),
        "basis_pct": item.get("basis_pct"),
        "structure": main.get("direction"),
        "macro_direction": macro.get("direction"),
        "main_structure": main.get("direction"),
        "confirmation": confirmation.get("direction"),
        "trigger": trigger.get("direction"),
        "entry": entry.get("direction"),
        "timeframe_2h": frame_2h.get("direction"),
        "timeframe_1h": frame_1h.get("direction"),
        "timeframe_4h": frame_4h.get("direction"),
        "timeframe_15m": frame_15m.get("direction"),
        "timeframe_5m": frame_5m.get("direction"),
        "liquidity_tier": item.get("liquidity_tier"),
        "bullish_risk_reward_ratio": (
            bull_plan.get("risk_reward_ratio")
            if bull_plan.get("status") == "available"
            else None
        ),
        "bearish_risk_reward_ratio": (
            bear_plan.get("risk_reward_ratio")
            if bear_plan.get("status") == "available"
            else None
        ),
        "data_complete": (
            base_ready and complete_flow_ready
        ),
        "observation_ready": (
            base_ready and spot_cvd_status == "spot_pair_not_listed"
        ),
    }


__all__ = [
    "active_flow_window",
    "build_directional_facts",
    "build_trade_plans",
    "select_directional_candidates",
]
