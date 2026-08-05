from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


THRESHOLD_PROFILE_VERSION = "launch_thresholds_v1"
SCORE_SEMANTICS = "rule_score_not_probability"

_ASSET_THRESHOLDS = {
    "core_crypto": (1.2, 2.0, 1.5, 0.08),
    "large_crypto": (1.8, 2.5, 1.7, 0.10),
    "altcoin": (3.0, 3.0, 2.0, 0.12),
    "tradfi": (4.0, 4.0, 2.5, 0.15),
    "unknown_conservative": (4.5, 4.5, 2.75, 0.18),
}
_TRADFI_SUBCLASSES = frozenset({
    "single_stock",
    "tokenized_stock",
    "broad_market_etf",
    "regional_etf",
    "leveraged_index_etf",
    "inverse_index_etf",
    "leveraged_sector_etf",
    "inverse_sector_etf",
    "precious_metal",
    "energy",
    "industrial_metal",
    "currency_pair",
})


def _optional_number(value: object) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _fact_number(facts: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key in facts:
            value = _optional_number(facts.get(key))
            if value is not None:
                return value
    return None


def _asset_profile(asset_subclass: str) -> str:
    normalized = str(asset_subclass or "").strip().lower()
    if normalized in {"core_crypto", "large_crypto", "altcoin"}:
        return normalized
    if normalized in _TRADFI_SUBCLASSES:
        return "tradfi"
    return "unknown_conservative"


def _liquidity_profile(liquidity_tier: str) -> tuple[str, float]:
    normalized = str(liquidity_tier or "").strip().lower()
    if normalized in {"high", "high_liquidity"} or "高流动" in normalized:
        return "high", 0.90
    if normalized in {"medium", "mid", "medium_liquidity"} or "中流动" in normalized:
        return "medium", 1.00
    if normalized in {"low", "low_liquidity"} or "低流动" in normalized:
        return "low", 1.20
    return "unknown", 1.30


def _volatility_profile(recent_volatility_pct: float | None) -> tuple[str, float]:
    if recent_volatility_pct is None or recent_volatility_pct < 0:
        return "unknown", 1.15
    if recent_volatility_pct < 0.75:
        return "quiet", 0.90
    if recent_volatility_pct <= 2.0:
        return "normal", 1.00
    if recent_volatility_pct <= 3.5:
        return "high", 1.20
    return "extreme", 1.40


def build_threshold_profile(
    *,
    asset_subclass: str,
    liquidity_tier: str,
    recent_volatility_pct: float | None,
) -> dict[str, Any]:
    asset_profile = _asset_profile(asset_subclass)
    liquidity_profile, liquidity_multiplier = _liquidity_profile(liquidity_tier)
    volatility_profile, volatility_multiplier = _volatility_profile(
        recent_volatility_pct
    )
    price, open_interest, volume, active_flow = _ASSET_THRESHOLDS[asset_profile]
    multiplier = liquidity_multiplier * volatility_multiplier
    return {
        "version": THRESHOLD_PROFILE_VERSION,
        "asset_profile": asset_profile,
        "liquidity_profile": liquidity_profile,
        "volatility_profile": volatility_profile,
        "price_trigger_pct": round(price * multiplier, 4),
        "price_1h_trigger_pct": round(price * 1.5 * multiplier, 4),
        "oi_trigger_pct": round(open_interest * multiplier, 4),
        "oi_1h_trigger_pct": round(open_interest * 1.5 * multiplier, 4),
        "volume_ratio_trigger": round(volume * multiplier, 4),
        "active_flow_ratio_trigger": round(active_flow * multiplier, 4),
        "historical_adjustment": "disabled",
    }


def _positive_group_score(
    values: tuple[tuple[float | None, float], ...],
    cap: int,
) -> int:
    strengths = [
        max(0.0, value) / threshold
        for value, threshold in values
        if value is not None and threshold > 0
    ]
    if not strengths:
        return 0
    strength = max(strengths)
    if strength < 0.5:
        return 0
    return min(cap, round(cap * min(1.0, strength)))


def _volume_group_score(value: float | None, threshold: float, cap: int) -> int:
    if value is None or value <= 1.0 or threshold <= 1.0:
        return 0
    strength = (value - 1.0) / (threshold - 1.0)
    return min(cap, max(0, round(cap * min(1.0, strength))))


def score_launch_signal(facts: Mapping[str, Any]) -> dict[str, Any]:
    """Return an explainable launch rule score without historical auto-tuning."""

    price_15m = _fact_number(facts, "price_15m", "price_15m_pct")
    price_1h = _fact_number(facts, "price_1h", "price_1h_pct")
    oi_15m = _fact_number(facts, "oi_15m", "oi_15m_pct")
    oi_1h = _fact_number(facts, "oi_1h", "oi_1h_pct")
    volume_ratio = _fact_number(facts, "volume_ratio", "volume_ratio_15m")
    spot_active = _optional_number(facts.get("spot_active_ratio"))
    futures_active = _optional_number(facts.get("futures_active_ratio"))
    volatility = _optional_number(facts.get("recent_volatility_pct"))
    profile = build_threshold_profile(
        asset_subclass=str(facts.get("asset_subclass") or ""),
        liquidity_tier=str(facts.get("liquidity_tier") or ""),
        recent_volatility_pct=volatility,
    )

    price_score = _positive_group_score(
        (
            (price_15m, profile["price_trigger_pct"]),
            (price_1h, profile["price_1h_trigger_pct"]),
        ),
        25,
    )
    oi_score = _positive_group_score(
        (
            (oi_15m, profile["oi_trigger_pct"]),
            (oi_1h, profile["oi_1h_trigger_pct"]),
        ),
        25,
    )
    volume_score = _volume_group_score(
        volume_ratio, profile["volume_ratio_trigger"], 20
    )
    structure_score = 15 if facts.get("breakout") is True else 0
    active_score = _positive_group_score(
        (
            (spot_active, profile["active_flow_ratio_trigger"]),
            (futures_active, profile["active_flow_ratio_trigger"]),
        ),
        15,
    )
    group_scores = {
        "price": price_score,
        "open_interest": oi_score,
        "volume": volume_score,
        "structure": structure_score,
        "active_funds": active_score,
    }

    price_met = any((
        price_15m is not None
        and price_15m >= profile["price_trigger_pct"],
        price_1h is not None
        and price_1h >= profile["price_1h_trigger_pct"],
    ))
    oi_met = any((
        oi_15m is not None and oi_15m >= profile["oi_trigger_pct"],
        oi_1h is not None and oi_1h >= profile["oi_1h_trigger_pct"],
    ))
    volume_met = (
        volume_ratio is not None
        and volume_ratio >= profile["volume_ratio_trigger"]
    )
    spot_met = (
        spot_active is not None
        and spot_active >= profile["active_flow_ratio_trigger"]
    )
    futures_met = (
        futures_active is not None
        and futures_active >= profile["active_flow_ratio_trigger"]
    )
    active_met = spot_met or futures_met

    supporting_evidence: list[str] = []
    if price_met:
        supporting_evidence.append("price_momentum_met")
    if oi_met:
        supporting_evidence.append("open_interest_growth_met")
    if volume_met:
        supporting_evidence.append("volume_expansion_met")
    if facts.get("breakout") is True:
        supporting_evidence.append("breakout_structure_met")
    if spot_met:
        supporting_evidence.append("spot_active_buying_met")
    if futures_met:
        supporting_evidence.append("futures_active_buying_met")

    counter_evidence: list[str] = []
    timeframe_pairs = (
        (
            price_15m,
            oi_15m,
            profile["price_trigger_pct"],
            profile["oi_trigger_pct"],
        ),
        (
            price_1h,
            oi_1h,
            profile["price_1h_trigger_pct"],
            profile["oi_1h_trigger_pct"],
        ),
    )
    if any(
        price is not None
        and oi is not None
        and price >= price_threshold
        and oi <= -(oi_threshold * 0.5)
        for price, oi, price_threshold, oi_threshold in timeframe_pairs
    ):
        counter_evidence.append("price_up_oi_down")
    if any(
        price is not None
        and oi is not None
        and price <= -price_threshold
        and oi >= oi_threshold
        for price, oi, price_threshold, oi_threshold in timeframe_pairs
    ):
        counter_evidence.append("price_down_oi_up")
    active_values = [
        value for value in (spot_active, futures_active) if value is not None
    ]
    if (
        price_met
        and active_values
        and min(active_values) <= -profile["active_flow_ratio_trigger"]
    ):
        counter_evidence.append("active_selling_against_move")
    if price_met and not oi_met and not volume_met and not active_met:
        counter_evidence.append("price_without_participation")

    hard_counter = bool(
        {
            "price_up_oi_down",
            "price_down_oi_up",
            "active_selling_against_move",
        }
        & set(counter_evidence)
    )
    if (
        price_met
        and (oi_met or active_met)
        and (volume_met or facts.get("breakout") is True)
        and not hard_counter
    ):
        trigger_path = "momentum"
    else:
        price_quiet = (
            price_15m is not None
            and price_1h is not None
            and abs(price_15m) < profile["price_trigger_pct"]
            and abs(price_1h) < profile["price_1h_trigger_pct"]
        )
        if price_quiet and oi_met and (volume_met or active_met):
            trigger_path = "dark_current"
            supporting_evidence.append("price_still_quiet")
        else:
            trigger_path = "none"

    return {
        "score": min(100, sum(group_scores.values())),
        "group_scores": group_scores,
        "threshold_profile": profile,
        "supporting_evidence": supporting_evidence,
        "counter_evidence": counter_evidence,
        "trigger_path": trigger_path,
        "score_semantics": SCORE_SEMANTICS,
        "historical_calibration": "report_only_not_applied",
        "data_availability": {
            "price": price_15m is not None or price_1h is not None,
            "open_interest": oi_15m is not None or oi_1h is not None,
            "volume": volume_ratio is not None,
            "structure": facts.get("breakout") is not None,
            "active_funds": spot_active is not None or futures_active is not None,
        },
    }


__all__ = [
    "SCORE_SEMANTICS",
    "THRESHOLD_PROFILE_VERSION",
    "build_threshold_profile",
    "score_launch_signal",
]
