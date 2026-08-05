from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any


SELECTION_POLICY = "active_then_liquidity_rotation_v1"
_INTERVAL_SEC = 15 * 60
_TIER_SCHEDULE = (
    "high",
    "high",
    "medium",
    "high",
    "low",
    "high",
    "medium",
    "high",
    "medium",
    "low",
)
_TIER_ORDER = ("high", "medium", "low")


def _number(value: object) -> float:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _symbol(item: Mapping[str, Any]) -> str:
    return str(item.get("symbol") or "").strip().upper()


def _liquidity_bucket(item: Mapping[str, Any]) -> str:
    value = str(item.get("liquidity_tier") or "").strip().lower()
    if value in {"high", "high_liquidity"} or "高流动" in value:
        return "high"
    if value in {"medium", "mid", "medium_liquidity"} or "中流动" in value:
        return "medium"
    if value in {"low", "low_liquidity"} or "低流动" in value:
        return "low"

    quote_volume = _number(item.get("quote_volume"))
    if quote_volume >= 100_000_000:
        return "high"
    if quote_volume >= 20_000_000:
        return "medium"
    return "low"


def _deduplicate(
    candidates: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, dict[str, Any]], int, int]:
    by_symbol: dict[str, dict[str, Any]] = {}
    input_count = 0
    duplicates = 0
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        input_count += 1
        symbol = _symbol(candidate)
        if not symbol:
            continue
        normalized = dict(candidate)
        normalized["symbol"] = symbol
        previous = by_symbol.get(symbol)
        if previous is None:
            by_symbol[symbol] = normalized
            continue
        duplicates += 1
        if _number(normalized.get("quote_volume")) > _number(
            previous.get("quote_volume")
        ):
            by_symbol[symbol] = normalized
    return by_symbol, input_count, duplicates


def _scheduled_count_before(tier: str, position: int) -> int:
    cycles, remainder = divmod(max(0, position), len(_TIER_SCHEDULE))
    return cycles * _TIER_SCHEDULE.count(tier) + _TIER_SCHEDULE[:remainder].count(
        tier
    )


def _rotate(items: list[dict[str, Any]], offset: int) -> list[dict[str, Any]]:
    if not items:
        return []
    normalized_offset = offset % len(items)
    return items[normalized_offset:] + items[:normalized_offset]


def select_launch_candidates(
    candidates: Iterable[Mapping[str, Any]],
    *,
    active_symbols: Iterable[str],
    limit: int,
    closed_window_end_ts: int,
) -> dict[str, Any]:
    """Select a bounded launch scan set from an already eligible candidate pool.

    Eligibility thresholds deliberately remain the caller's responsibility. This
    function only applies active-lifecycle priority and deterministic liquidity
    rotation for one closed 15-minute window.
    """

    by_symbol, input_count, duplicates = _deduplicate(candidates)
    safe_limit = max(0, int(limit))
    window_index = max(0, int(closed_window_end_ts)) // _INTERVAL_SEC
    active_order = list(
        dict.fromkeys(
            str(symbol or "").strip().upper()
            for symbol in active_symbols
            if str(symbol or "").strip()
        )
    )

    pool_counts = {tier: 0 for tier in _TIER_ORDER}
    for candidate in by_symbol.values():
        pool_counts[_liquidity_bucket(candidate)] += 1

    stats: dict[str, Any] = {
        "status": "ok" if safe_limit > 0 else "disabled",
        "selection_policy": SELECTION_POLICY,
        "rotation_window": window_index,
        "limit": safe_limit,
        "input_count": input_count,
        "eligible_count": len(by_symbol),
        "duplicate_candidates_removed": duplicates,
        "active_requested": len(active_order),
        "active_selected": 0,
        "tier_weights": {"high": 5, "medium": 3, "low": 2},
        "tier_pool_counts": pool_counts,
        "tier_selected_counts": {tier: 0 for tier in _TIER_ORDER},
        "selected_count": 0,
    }
    if safe_limit == 0 or not by_symbol:
        return {"selected": [], "stats": stats}

    selected: list[dict[str, Any]] = []
    selected_symbols: set[str] = set()
    for symbol in active_order:
        candidate = by_symbol.get(symbol)
        if candidate is None or symbol in selected_symbols:
            continue
        selected.append(candidate)
        selected_symbols.add(symbol)
        stats["active_selected"] += 1
        if len(selected) >= safe_limit:
            break

    remaining_capacity = max(0, safe_limit - len(selected))
    schedule_position = window_index * max(1, remaining_capacity)
    tier_pools: dict[str, list[dict[str, Any]]] = {}
    for tier in _TIER_ORDER:
        items = [
            candidate
            for symbol, candidate in by_symbol.items()
            if symbol not in selected_symbols
            and _liquidity_bucket(candidate) == tier
        ]
        items.sort(
            key=lambda item: (-_number(item.get("quote_volume")), _symbol(item))
        )
        tier_pools[tier] = _rotate(
            items,
            _scheduled_count_before(tier, schedule_position),
        )

    slot = 0
    while len(selected) < safe_limit and any(tier_pools.values()):
        preferred = _TIER_SCHEDULE[
            (schedule_position + slot) % len(_TIER_SCHEDULE)
        ]
        fallback_order = (preferred,) + tuple(
            tier for tier in _TIER_ORDER if tier != preferred
        )
        chosen_tier = next(
            (tier for tier in fallback_order if tier_pools[tier]),
            None,
        )
        if chosen_tier is None:
            break
        candidate = tier_pools[chosen_tier].pop(0)
        selected.append(candidate)
        selected_symbols.add(_symbol(candidate))
        stats["tier_selected_counts"][chosen_tier] += 1
        slot += 1

    stats["selected_count"] = len(selected)
    return {"selected": selected, "stats": stats}


__all__ = ["SELECTION_POLICY", "select_launch_candidates"]
