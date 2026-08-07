from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any


MODEL_VERSION = "launch_directional_readiness_v1"
SCORE_SEMANTICS = "rule_readiness_not_probability"
MIN_RISK_REWARD_RATIO = 2.0

STATUS_BULLISH_CONFIRMED = "多头确认"
STATUS_BULLISH_CANDIDATE = "多头候选"
STATUS_LEVERAGE_OVERHEATED = "杠杆过热"
STATUS_SHORT_COVERING = "挤空反弹"
STATUS_ACCUMULATION = "潜伏积累"
STATUS_BEARISH_CONFIRMED = "空头确认"
STATUS_BEARISH_CANDIDATE = "空头候选"
STATUS_BEARISH_OVERHEATED = "空头拥挤"
STATUS_LONG_LIQUIDATION = "多头踩踏"
STATUS_DISTRIBUTION = "派发风险"
STATUS_CONFLICT = "冲突等待"
STATUS_INSUFFICIENT = "数据不足"
STATUS_FAKE_STRENGTH = "假强背离"
STATUS_FAKE_WEAKNESS = "假弱背离"

_GROUP_CAPS = {
    "price_oi_participation": 30,
    "active_funds": 25,
    "structure": 25,
    "execution_quality": 20,
}
_ASSET_THRESHOLDS = {
    "core_crypto": (1.0, 1.5, 0.05),
    "large_crypto": (1.5, 2.0, 0.07),
    "altcoin": (2.0, 2.5, 0.08),
    "tradfi": (2.0, 2.0, 0.08),
    "unknown_conservative": (3.0, 3.0, 0.10),
}
_TRADFI_CATEGORIES = frozenset({
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
    "tradfi",
})


def _number(facts: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        if key not in facts:
            continue
        try:
            number = float(facts.get(key))
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            return number
    return None


def _asset_profile(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"core_crypto", "large_crypto", "altcoin"}:
        return normalized
    if normalized in {"crypto", "crypto_index"}:
        return "altcoin"
    if normalized in _TRADFI_CATEGORIES:
        return "tradfi"
    return "unknown_conservative"


def _direction(value: object) -> str:
    if isinstance(value, Mapping):
        value = value.get("direction", value.get("status", ""))
    normalized = str(value or "").strip().lower()
    if normalized in {
        "bull",
        "bullish",
        "long",
        "up",
        "uptrend",
        "confirmed_up",
        "breakout_up",
        "看涨",
        "多头",
        "向上",
    }:
        return "bullish"
    if normalized in {
        "bear",
        "bearish",
        "short",
        "down",
        "downtrend",
        "confirmed_down",
        "breakdown_down",
        "看跌",
        "空头",
        "向下",
    }:
        return "bearish"
    if normalized in {
        "neutral",
        "range",
        "sideways",
        "flat",
        "中性",
        "震荡",
    }:
        return "neutral"
    if normalized in {"mixed", "conflict", "分歧", "冲突"}:
        return "mixed"
    return "unknown"


def _complete(facts: Mapping[str, Any]) -> bool:
    explicit = facts.get("data_complete")
    if isinstance(explicit, bool):
        return explicit
    value = str(
        facts.get("data_completeness", facts.get("completeness", "")) or ""
    ).strip().lower()
    return value in {"complete", "ok", "完整"}


def _liquidity_ok(facts: Mapping[str, Any]) -> bool:
    explicit = facts.get("liquidity_ok")
    if isinstance(explicit, bool):
        return explicit
    tier = str(facts.get("liquidity_tier") or "").strip().lower()
    return tier in {
        "high",
        "medium",
        "high_liquidity",
        "medium_liquidity",
        "高流动性",
        "中流动性",
    }


def _risk_reward(facts: Mapping[str, Any], direction: str) -> float | None:
    return _number(
        facts,
        f"{direction}_risk_reward_ratio",
        "risk_reward_ratio",
        "rr_ratio",
    )


def _crowding_deduction(
    funding_pct: float,
    basis_pct: float,
    *,
    direction: str,
) -> tuple[int, list[str]]:
    sign = 1.0 if direction == "bullish" else -1.0
    aligned_funding = funding_pct * sign
    aligned_basis = basis_pct * sign
    deduction = 0
    reasons: list[str] = []
    if aligned_funding >= 0.10:
        deduction += 12
        reasons.append("funding_extreme_in_direction")
    elif aligned_funding >= 0.03:
        deduction += 7
        reasons.append("funding_crowded_in_direction")
    if aligned_basis >= 0.50:
        deduction += 10
        reasons.append("basis_extreme_in_direction")
    elif aligned_basis >= 0.20:
        deduction += 5
        reasons.append("basis_crowded_in_direction")
    return min(20, deduction), reasons


def _participation_scores(
    price_change: float,
    oi_change: float,
    *,
    price_trigger: float,
    oi_trigger: float,
) -> tuple[int, int, str, list[str], list[str]]:
    bullish = 0
    bearish = 0
    pattern = "mixed"
    bullish_evidence: list[str] = []
    bearish_evidence: list[str] = []
    price_up = price_change >= price_trigger
    price_down = price_change <= -price_trigger
    price_quiet = abs(price_change) < price_trigger * 0.5
    oi_up = oi_change >= oi_trigger
    oi_down = oi_change <= -oi_trigger

    if price_up and oi_up:
        bullish = _GROUP_CAPS["price_oi_participation"]
        pattern = "price_up_oi_up"
        bullish_evidence.append(pattern)
    elif price_down and oi_up:
        bearish = _GROUP_CAPS["price_oi_participation"]
        pattern = "price_down_oi_up"
        bearish_evidence.append(pattern)
    elif price_up and oi_down:
        bullish = 12
        pattern = "short_covering"
        bullish_evidence.append(pattern)
    elif price_down and oi_down:
        bearish = 12
        pattern = "long_liquidation"
        bearish_evidence.append(pattern)
    elif price_quiet and oi_up:
        bullish = 8
        bearish = 8
        pattern = "quiet_price_oi_build"
        bullish_evidence.append(pattern)
        bearish_evidence.append(pattern)
    elif price_up:
        bullish = 10
        pattern = "price_up_without_oi_confirmation"
        bullish_evidence.append(pattern)
    elif price_down:
        bearish = 10
        pattern = "price_down_without_oi_confirmation"
        bearish_evidence.append(pattern)
    return bullish, bearish, pattern, bullish_evidence, bearish_evidence


def _flow_scores(
    spot_cvd_ratio: float | None,
    futures_cvd_ratio: float | None,
    *,
    trigger: float,
) -> tuple[int, int, list[str], list[str]]:
    bullish = 0
    bearish = 0
    bullish_evidence: list[str] = []
    bearish_evidence: list[str] = []
    if spot_cvd_ratio is not None and spot_cvd_ratio >= trigger:
        bullish += 16
        bullish_evidence.append("spot_cvd_buying")
    elif spot_cvd_ratio is not None and spot_cvd_ratio <= -trigger:
        bearish += 16
        bearish_evidence.append("spot_cvd_selling")
    if futures_cvd_ratio is not None and futures_cvd_ratio >= trigger:
        bullish += 9
        bullish_evidence.append("futures_cvd_buying")
    elif futures_cvd_ratio is not None and futures_cvd_ratio <= -trigger:
        bearish += 9
        bearish_evidence.append("futures_cvd_selling")
    return min(25, bullish), min(25, bearish), bullish_evidence, bearish_evidence


def evaluate_directional_readiness(facts: Mapping[str, Any]) -> dict[str, Any]:
    """Classify deterministic directional readiness, never trading probability.

    CVD inputs are normalized signed ratios. Multi-timeframe direction is used as
    a gate, not added repeatedly to the rule score. Funding and basis can only
    subtract for directional crowding; they never add bullish or bearish points.
    """

    if not isinstance(facts, Mapping):
        facts = {}
    asset_profile = _asset_profile(
        facts.get("asset_category", facts.get("asset_subclass"))
    )
    price_trigger, oi_trigger, cvd_trigger = _ASSET_THRESHOLDS[asset_profile]
    price_change = _number(facts, "price_change_pct", "price_15m_pct", "price_15m")
    oi_change = _number(facts, "oi_change_pct", "oi_15m_pct", "oi_15m")
    spot_cvd = _number(facts, "spot_cvd_ratio", "spot_active_ratio")
    futures_cvd = _number(facts, "futures_cvd_ratio", "futures_active_ratio")
    spot_cvd_status = str(facts.get("spot_cvd_status") or "").strip().lower()
    futures_cvd_status = str(
        facts.get("futures_cvd_status") or ""
    ).strip().lower()
    funding_pct = _number(facts, "funding_rate_pct", "funding_pct")
    basis_pct = _number(facts, "basis_pct", "futures_basis_pct")
    structure = _direction(facts.get("structure", facts.get("price_structure")))
    timeframe_1h = _direction(
        facts.get("timeframe_1h", facts.get("confirmation_1h"))
    )
    timeframe_2h = _direction(facts.get("timeframe_2h"))
    timeframe_4h = _direction(facts.get("timeframe_4h", facts.get("trend_4h")))
    timeframe_15m = _direction(facts.get("timeframe_15m"))
    timeframe_5m = _direction(facts.get("timeframe_5m"))
    macro_direction = _direction(facts.get("macro_direction"))
    main_structure = _direction(facts.get("main_structure"))
    confirmation = _direction(facts.get("confirmation"))
    trigger = _direction(facts.get("trigger"))
    entry = _direction(facts.get("entry"))
    bullish_rr = _risk_reward(facts, "bullish")
    bearish_rr = _risk_reward(facts, "bearish")
    liquidity_ok = _liquidity_ok(facts)

    required_values = {
        "price_change_pct": price_change,
        "oi_change_pct": oi_change,
        "spot_cvd_ratio": spot_cvd,
        "futures_cvd_ratio": futures_cvd,
        "funding_rate_pct": funding_pct,
        "basis_pct": basis_pct,
        "structure": None if structure == "unknown" else structure,
        "macro_direction": (
            None if macro_direction == "unknown" else macro_direction
        ),
        "main_structure": (
            None if main_structure == "unknown" else main_structure
        ),
        "confirmation": None if confirmation == "unknown" else confirmation,
        "trigger": None if trigger == "unknown" else trigger,
        "entry": None if entry == "unknown" else entry,
        "timeframe_2h": None if timeframe_2h == "unknown" else timeframe_2h,
        "timeframe_1h": None if timeframe_1h == "unknown" else timeframe_1h,
        "timeframe_4h": None if timeframe_4h == "unknown" else timeframe_4h,
        "timeframe_15m": (
            None if timeframe_15m == "unknown" else timeframe_15m
        ),
        "timeframe_5m": None if timeframe_5m == "unknown" else timeframe_5m,
        "liquidity": True if "liquidity_ok" in facts or facts.get("liquidity_tier") else None,
    }
    missing_fields = [key for key, value in required_values.items() if value is None]
    data_complete = _complete(facts) and not missing_fields
    observation_missing_fields = [
        key
        for key, value in required_values.items()
        if key != "spot_cvd_ratio" and value is None
    ]
    observation_ready = bool(facts.get("observation_ready")) and not (
        observation_missing_fields
    )
    observation_ready = bool(
        observation_ready
        and spot_cvd_status == "spot_pair_not_listed"
        and futures_cvd_status in {"available", "no_trades"}
    )
    observation_only = observation_ready and not data_complete

    if price_change is None:
        price_change = 0.0
    if oi_change is None:
        oi_change = 0.0
    if futures_cvd is None:
        futures_cvd = 0.0
    if funding_pct is None:
        funding_pct = 0.0
    if basis_pct is None:
        basis_pct = 0.0

    (
        participation_bull,
        participation_bear,
        participation_pattern,
        bullish_evidence,
        bearish_evidence,
    ) = _participation_scores(
        price_change,
        oi_change,
        price_trigger=price_trigger,
        oi_trigger=oi_trigger,
    )
    flow_bull, flow_bear, flow_bull_evidence, flow_bear_evidence = _flow_scores(
        spot_cvd,
        futures_cvd,
        trigger=cvd_trigger,
    )
    bullish_evidence.extend(flow_bull_evidence)
    bearish_evidence.extend(flow_bear_evidence)

    structure_bull = 25 if structure == "bullish" else 0
    structure_bear = 25 if structure == "bearish" else 0
    if structure_bull:
        bullish_evidence.append("bullish_structure")
    if structure_bear:
        bearish_evidence.append("bearish_structure")

    execution_bull = (
        (10 if liquidity_ok else 0)
        + (10 if bullish_rr is not None and bullish_rr >= MIN_RISK_REWARD_RATIO else 0)
    )
    execution_bear = (
        (10 if liquidity_ok else 0)
        + (10 if bearish_rr is not None and bearish_rr >= MIN_RISK_REWARD_RATIO else 0)
    )
    bullish_groups = {
        "price_oi_participation": participation_bull,
        "active_funds": flow_bull,
        "structure": structure_bull,
        "execution_quality": execution_bull,
    }
    bearish_groups = {
        "price_oi_participation": participation_bear,
        "active_funds": flow_bear,
        "structure": structure_bear,
        "execution_quality": execution_bear,
    }
    bullish_raw = min(100, sum(bullish_groups.values()))
    bearish_raw = min(100, sum(bearish_groups.values()))
    bullish_deduction, bullish_risks = _crowding_deduction(
        funding_pct, basis_pct, direction="bullish"
    )
    bearish_deduction, bearish_risks = _crowding_deduction(
        funding_pct, basis_pct, direction="bearish"
    )
    bullish_score = max(0, bullish_raw - bullish_deduction)
    bearish_score = max(0, bearish_raw - bearish_deduction)

    bullish_cvd_aligned = (
        spot_cvd is not None
        and spot_cvd >= cvd_trigger
        and futures_cvd >= cvd_trigger
    )
    bearish_cvd_aligned = (
        spot_cvd is not None
        and spot_cvd <= -cvd_trigger
        and futures_cvd <= -cvd_trigger
    )
    price_up = price_change >= price_trigger
    price_down = price_change <= -price_trigger
    oi_up = oi_change >= oi_trigger
    fake_strength = price_up and oi_up and bearish_cvd_aligned
    fake_weakness = price_down and oi_up and bullish_cvd_aligned
    divergence_status = (
        STATUS_FAKE_STRENGTH
        if fake_strength
        else STATUS_FAKE_WEAKNESS
        if fake_weakness
        else "none"
    )
    divergence_evidence: list[str] = []
    if fake_strength:
        participation_pattern = "fake_strength_divergence"
        divergence_evidence.append("spot_and_futures_cvd_oppose_price_rise")
    elif fake_weakness:
        participation_pattern = "fake_weakness_divergence"
        divergence_evidence.append("spot_and_futures_cvd_oppose_price_decline")
    if fake_strength or fake_weakness:
        bullish_score = min(bullish_score, 59)
        bearish_score = min(bearish_score, 59)

    timeframe_values = {
        "macro_direction": macro_direction,
        "main_structure": main_structure,
        "confirmation": confirmation,
        "trigger": trigger,
        "entry": entry,
        "timeframe_2h": timeframe_2h,
        "timeframe_1h": timeframe_1h,
        "timeframe_4h": timeframe_4h,
        "timeframe_15m": timeframe_15m,
        "timeframe_5m": timeframe_5m,
    }
    timeframe_conflicts = sorted(
        key for key, value in timeframe_values.items() if value == "mixed"
    )

    bullish_gates = {
        "complete_data": data_complete,
        "macro_direction_aligned": macro_direction == "bullish",
        "main_structure_aligned": main_structure == "bullish",
        "confirmation_group_aligned": confirmation == "bullish",
        "confirmed_2h": timeframe_2h == "bullish",
        "confirmed_1h": timeframe_1h == "bullish",
        "four_hour_not_opposed": timeframe_4h in {"bullish", "neutral"},
        "trigger_15m_aligned": (
            trigger == "bullish" and timeframe_15m == "bullish"
        ),
        "entry_5m_aligned": entry == "bullish" and timeframe_5m == "bullish",
        "spot_cvd_aligned": (
            spot_cvd is not None and spot_cvd >= cvd_trigger
        ),
        "futures_cvd_aligned": futures_cvd >= cvd_trigger,
        "liquidity": liquidity_ok,
        "risk_reward": (
            bullish_rr is not None and bullish_rr >= MIN_RISK_REWARD_RATIO
        ),
    }
    bearish_gates = {
        "complete_data": data_complete,
        "macro_direction_aligned": macro_direction == "bearish",
        "main_structure_aligned": main_structure == "bearish",
        "confirmation_group_aligned": confirmation == "bearish",
        "confirmed_2h": timeframe_2h == "bearish",
        "confirmed_1h": timeframe_1h == "bearish",
        "four_hour_not_opposed": timeframe_4h in {"bearish", "neutral"},
        "trigger_15m_aligned": (
            trigger == "bearish" and timeframe_15m == "bearish"
        ),
        "entry_5m_aligned": entry == "bearish" and timeframe_5m == "bearish",
        "spot_cvd_aligned": (
            spot_cvd is not None and spot_cvd <= -cvd_trigger
        ),
        "futures_cvd_aligned": futures_cvd <= -cvd_trigger,
        "liquidity": liquidity_ok,
        "risk_reward": (
            bearish_rr is not None and bearish_rr >= MIN_RISK_REWARD_RATIO
        ),
    }
    bullish_gate_passed = all(bullish_gates.values())
    bearish_gate_passed = all(bearish_gates.values())
    bullish_overheated = funding_pct >= 0.05 and basis_pct >= 0.30
    bearish_overheated = funding_pct <= -0.05 and basis_pct <= -0.30
    evidence_leader = (
        "bullish"
        if bullish_score > bearish_score
        else "bearish"
        if bearish_score > bullish_score
        else "none"
    )
    bullish_candidate_ready = (
        bullish_score >= 45 and bullish_score >= bearish_score + 10
    )
    bearish_candidate_ready = (
        bearish_score >= 45 and bearish_score >= bullish_score + 10
    )

    if not data_complete and not observation_ready:
        status = STATUS_INSUFFICIENT
        direction = "none"
    elif fake_strength:
        status = STATUS_FAKE_STRENGTH
        direction = "bearish_divergence_watch"
    elif fake_weakness:
        status = STATUS_FAKE_WEAKNESS
        direction = "bullish_divergence_watch"
    elif timeframe_conflicts:
        status = STATUS_CONFLICT
        direction = "none"
    elif observation_only:
        if (
            futures_cvd >= cvd_trigger
            and bullish_score >= 45
            and bullish_score >= bearish_score + 10
        ):
            status = STATUS_BULLISH_CANDIDATE
            direction = "bullish_candidate"
        elif (
            futures_cvd <= -cvd_trigger
            and bearish_score >= 45
            and bearish_score >= bullish_score + 10
        ):
            status = STATUS_BEARISH_CANDIDATE
            direction = "bearish_candidate"
        else:
            status = STATUS_CONFLICT
            direction = "none"
    elif (
        participation_pattern == "short_covering"
        and evidence_leader == "bullish"
    ):
        status = STATUS_SHORT_COVERING
        direction = "bullish_rebound_only"
    elif (
        participation_pattern == "long_liquidation"
        and evidence_leader == "bearish"
    ):
        status = STATUS_LONG_LIQUIDATION
        direction = "bearish_deleveraging_only"
    elif (
        participation_pattern == "quiet_price_oi_build"
        and spot_cvd is not None
        and spot_cvd >= cvd_trigger
        and futures_cvd > -cvd_trigger
        and structure != "bearish"
    ):
        status = STATUS_ACCUMULATION
        direction = "bullish_candidate"
    elif (
        price_change > -(price_trigger * 0.5)
        and oi_change >= oi_trigger
        and spot_cvd is not None
        and spot_cvd <= -cvd_trigger
        and futures_cvd < cvd_trigger
        and structure == "bearish"
    ):
        status = STATUS_DISTRIBUTION
        direction = "bearish_candidate"
    elif bullish_overheated and bullish_raw >= 60 and bullish_raw > bearish_raw:
        status = STATUS_LEVERAGE_OVERHEATED
        direction = "bullish_overheated"
    elif bearish_overheated and bearish_raw >= 60 and bearish_raw > bullish_raw:
        status = STATUS_BEARISH_OVERHEATED
        # Keep the public direction within the existing formatter contract.
        # The dedicated crowding_state below carries the symmetric risk
        # meaning until delivery adopts a specific short-crowding headline.
        direction = "bearish_candidate"
    elif bullish_gate_passed and bullish_score >= 70 and bullish_score >= bearish_score + 15:
        status = STATUS_BULLISH_CONFIRMED
        direction = "bullish"
    elif bearish_gate_passed and bearish_score >= 70 and bearish_score >= bullish_score + 15:
        status = STATUS_BEARISH_CONFIRMED
        direction = "bearish"
    elif bullish_candidate_ready:
        status = STATUS_BULLISH_CANDIDATE
        direction = "bullish_candidate"
    elif bearish_candidate_ready:
        status = STATUS_BEARISH_CANDIDATE
        direction = "bearish_candidate"
    else:
        status = STATUS_CONFLICT
        direction = "none"

    return {
        "version": MODEL_VERSION,
        "status": status,
        "direction": direction,
        "score_semantics": SCORE_SEMANTICS,
        "bullish_readiness": bullish_score,
        "bearish_readiness": bearish_score,
        # Compatibility: readiness keys remain for stored history and existing
        # formatters.  New phase-aware consumers should use evidence_score.
        "bullish_evidence_score": bullish_score,
        "bearish_evidence_score": bearish_score,
        "evidence_score_semantics": "rule_score_not_probability",
        "bullish_raw_score": bullish_raw,
        "bearish_raw_score": bearish_raw,
        "group_caps": dict(_GROUP_CAPS),
        "bullish_group_scores": bullish_groups,
        "bearish_group_scores": bearish_groups,
        "risk_adjustments": {
            "bullish": -bullish_deduction,
            "bearish": -bearish_deduction,
            "bullish_reasons": bullish_risks,
            "bearish_reasons": bearish_risks,
            "semantics": "funding_and_basis_can_only_subtract",
        },
        "hard_gates": {
            "bullish": bullish_gates,
            "bearish": bearish_gates,
            "bullish_passed": bullish_gate_passed,
            "bearish_passed": bearish_gate_passed,
            "minimum_risk_reward_ratio": MIN_RISK_REWARD_RATIO,
        },
        "evidence": {
            "bullish": bullish_evidence,
            "bearish": bearish_evidence,
        },
        "participation_pattern": participation_pattern,
        "move_mechanism": participation_pattern,
        "evidence_leader": evidence_leader,
        "crowding_state": (
            "long_side_overcrowded"
            if bullish_overheated and bullish_raw > bearish_raw
            else "short_side_overcrowded"
            if bearish_overheated and bearish_raw > bullish_raw
            else "none"
        ),
        "divergence_status": divergence_status,
        "divergence_evidence": divergence_evidence,
        "divergence_semantics": (
            "risk_watch_not_confirmed_reversal"
            if fake_strength or fake_weakness
            else "none"
        ),
        "timeframe_conflicts": timeframe_conflicts,
        "asset_profile": asset_profile,
        "thresholds": {
            "price_change_pct": price_trigger,
            "oi_change_pct": oi_trigger,
            "cvd_ratio": cvd_trigger,
        },
        "data_complete": data_complete,
        "observation_ready": observation_ready,
        "observation_mode": (
            "futures_only_spot_pair_not_listed"
            if observation_only
            else "full"
            if data_complete
            else "unavailable"
        ),
        "spot_cvd_status": spot_cvd_status or "unknown",
        "futures_cvd_status": futures_cvd_status or "unknown",
        "missing_fields": missing_fields,
        "observation_missing_fields": observation_missing_fields,
        "limitations": [
            "rule_readiness_not_probability",
            "open_interest_does_not_identify_long_or_short_by_itself",
            "cvd_is_aggressive_trade_imbalance_not_capital_inflow",
            "funding_and_basis_are_crowding_risk_not_direction_proof",
            "structure_labels_do_not_prove_institutional_activity",
            "divergence_watch_does_not_confirm_reversal",
            "futures_only_observation_cannot_confirm_direction",
        ],
    }


__all__ = [
    "MIN_RISK_REWARD_RATIO",
    "MODEL_VERSION",
    "SCORE_SEMANTICS",
    "evaluate_directional_readiness",
]
