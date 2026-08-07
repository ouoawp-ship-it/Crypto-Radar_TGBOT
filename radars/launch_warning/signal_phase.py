"""Deterministic launch-signal phase classification.

This module deliberately stays separate from directional scoring.  It decides
whether an already discovered direction is timely enough to publish or plan;
it never changes the legacy 15-minute discovery score or flips its direction.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from .directional_model import evaluate_directional_readiness


PHASE_VERSION = "launch_signal_phase_v1"
SCORE_EFFECT = "none"
MIN_CLOSED_ONE_HOUR_CANDLES = 72
HIGH_RANGE_POSITION = 0.90
LOW_RANGE_POSITION = 0.10
EXTENDED_ATR_MULTIPLE = 2.0
MIN_ONE_HOUR_VOLUME_RATIO = 1.0
MIN_ACTIVE_FLOW_GROSS_USD = 50_000.0
MIN_ACTIVE_FLOW_NET_USD = 10_000.0

_READY_SUMMARY_STATUSES = {"complete", "ok", "ready"}
_OI_RELEASE_MECHANISMS = {"short_covering", "long_liquidation"}


def _number(source: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = source.get(key)
        if isinstance(value, bool):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            return number
    return None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or not number.is_integer():
        return None
    return int(number)


def build_one_hour_phase_summary(
    rows: Any,
    *,
    window_end_ms: int,
    interval_ms: int = 3_600_000,
) -> dict[str, Any]:
    """Build a fail-closed summary from the latest 72 closed Binance rows."""

    result: dict[str, Any] = {
        "data_status": "invalid",
        "closed_only": True,
        "continuous": False,
        "closed_candles": 0,
        "range_low_72h": None,
        "range_high_72h": None,
        "last_close": None,
        "atr": None,
        "volume_ratio_1h": None,
        "excluded_unclosed_candles": 0,
        "invalid_rows": 0,
    }
    end_ms = _integer(window_end_ms)
    step_ms = _integer(interval_ms)
    if (
        not isinstance(rows, (list, tuple))
        or end_ms is None
        or step_ms is None
        or end_ms <= 0
        or step_ms <= 0
    ):
        return result

    boundary_ms = (end_ms // step_ms) * step_ms
    if boundary_ms <= 0:
        result["data_status"] = "boundary_missing"
        return result

    expected_opens = tuple(
        boundary_ms - (MIN_CLOSED_ONE_HOUR_CANDLES - offset) * step_ms
        for offset in range(MIN_CLOSED_ONE_HOUR_CANDLES)
    )
    expected_set = set(expected_opens)
    normalized: dict[int, tuple[float, float, float, float, float]] = {}
    invalid_expected_opens: set[int] = set()
    duplicate_expected_opens: set[int] = set()

    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 8:
            result["invalid_rows"] += 1
            continue
        opened = _integer(row[0])
        closed = _integer(row[6])
        if opened is None or closed is None:
            result["invalid_rows"] += 1
            continue
        if closed >= end_ms:
            result["excluded_unclosed_candles"] += 1
            continue
        if opened not in expected_set:
            continue
        try:
            open_price = float(row[1])
            high = float(row[2])
            low = float(row[3])
            close = float(row[4])
            quote_volume = float(row[7])
        except (TypeError, ValueError):
            result["invalid_rows"] += 1
            invalid_expected_opens.add(opened)
            continue
        values = (open_price, high, low, close, quote_volume)
        if (
            not all(math.isfinite(value) for value in values)
            or open_price <= 0
            or high <= 0
            or low <= 0
            or close <= 0
            or quote_volume < 0
            or high < max(open_price, close)
            or low > min(open_price, close)
            or closed != opened + step_ms - 1
        ):
            result["invalid_rows"] += 1
            invalid_expected_opens.add(opened)
            continue
        if opened in normalized:
            duplicate_expected_opens.add(opened)
            continue
        normalized[opened] = values

    result["closed_candles"] = sum(
        1 for opened in expected_opens if opened in normalized
    )
    if invalid_expected_opens or duplicate_expected_opens:
        result["data_status"] = "invalid"
        return result
    if result["closed_candles"] < MIN_CLOSED_ONE_HOUR_CANDLES:
        result["data_status"] = (
            "gap"
            if len(normalized) >= MIN_CLOSED_ONE_HOUR_CANDLES - 1
            else "insufficient_history"
        )
        return result

    candles = [normalized[opened] for opened in expected_opens]
    result["data_status"] = "complete"
    result["continuous"] = True
    result["range_low_72h"] = min(candle[2] for candle in candles)
    result["range_high_72h"] = max(candle[1] for candle in candles)
    result["last_close"] = candles[-1][3]

    true_ranges = [
        max(current[1] - current[2], abs(current[1] - previous[3]), abs(current[2] - previous[3]))
        for previous, current in zip(candles[-15:-1], candles[-14:])
    ]
    if true_ranges:
        result["atr"] = sum(true_ranges) / len(true_ranges)

    baseline = [candle[4] for candle in candles[-21:-1]]
    baseline_average = sum(baseline) / len(baseline) if baseline else 0.0
    if baseline_average > 0:
        result["volume_ratio_1h"] = candles[-1][4] / baseline_average
    return result


def _directional_bias(signal: Mapping[str, Any]) -> str:
    direction = str(signal.get("direction") or "").strip().lower()
    if direction.startswith("bullish"):
        return "bullish"
    if direction.startswith("bearish"):
        return "bearish"
    return "none"


def _flow_scale_status(facts: Mapping[str, Any]) -> str:
    explicit = str(facts.get("active_flow_scale_status") or "").strip().lower()
    if explicit in {"sufficient", "low", "insufficient"}:
        return explicit

    pairs = (
        (
            _number(facts, "spot_cvd_gross_usd", "spot_active_gross_usd"),
            _number(facts, "spot_cvd_net_usd", "spot_active_net_usd"),
        ),
        (
            _number(facts, "futures_cvd_gross_usd", "futures_active_gross_usd"),
            _number(facts, "futures_cvd_net_usd", "futures_active_net_usd"),
        ),
    )
    if any(gross is None or net is None for gross, net in pairs):
        return "insufficient"
    if all(
        gross >= MIN_ACTIVE_FLOW_GROSS_USD
        and abs(net) >= MIN_ACTIVE_FLOW_NET_USD
        for gross, net in pairs
        if gross is not None and net is not None
    ):
        return "sufficient"
    return "low"


def _position_context(summary: Mapping[str, Any], bias: str) -> dict[str, Any]:
    status = str(summary.get("data_status", summary.get("status", ""))).lower()
    closed_candles = _integer(summary.get("closed_candles"))
    low = _number(summary, "range_low_72h")
    high = _number(summary, "range_high_72h")
    close = _number(summary, "last_close")
    atr = _number(summary, "atr")
    context: dict[str, Any] = {
        "position_status": "insufficient",
        "range_position_72h": None,
        "extension_atr": None,
        "ready": False,
        "extended": False,
    }
    if (
        status not in _READY_SUMMARY_STATUSES
        or closed_candles is None
        or closed_candles < MIN_CLOSED_ONE_HOUR_CANDLES
        or summary.get("closed_only") is not True
        or summary.get("continuous") is not True
        or low is None
        or high is None
        or close is None
        or atr is None
        or high <= low
        or atr <= 0
    ):
        return context

    range_position = min(1.0, max(0.0, (close - low) / (high - low)))
    context["range_position_72h"] = range_position
    if bias == "bullish":
        reference = _number(
            summary, "bullish_reference_price", "structure_reference_price"
        )
        if reference is None or reference <= 0 or reference >= close:
            return context
        extension = (close - reference) / atr
        extended = (
            range_position >= HIGH_RANGE_POSITION
            and extension >= EXTENDED_ATR_MULTIPLE
        )
        position_status = (
            "high_extended"
            if extended
            else "high"
            if range_position >= HIGH_RANGE_POSITION
            else "low"
            if range_position <= LOW_RANGE_POSITION
            else "middle"
        )
    elif bias == "bearish":
        reference = _number(
            summary, "bearish_reference_price", "structure_reference_price"
        )
        if reference is None or reference <= close:
            return context
        extension = (reference - close) / atr
        extended = (
            range_position <= LOW_RANGE_POSITION
            and extension >= EXTENDED_ATR_MULTIPLE
        )
        position_status = (
            "low_extended"
            if extended
            else "high"
            if range_position >= HIGH_RANGE_POSITION
            else "low"
            if range_position <= LOW_RANGE_POSITION
            else "middle"
        )
    else:
        return context

    context.update(
        {
            "position_status": position_status,
            "extension_atr": extension,
            "ready": True,
            "extended": extended,
        }
    )
    return context


def classify_launch_phase(
    facts: Mapping[str, Any],
    one_hour_summary: Mapping[str, Any],
    *,
    directional_signal: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Classify timing/execution without modifying the directional score."""

    signal = dict(directional_signal or evaluate_directional_readiness(facts))
    bias = _directional_bias(signal)
    mechanism = str(
        signal.get("move_mechanism")
        or signal.get("participation_pattern")
        or "unknown"
    )
    position = _position_context(one_hour_summary, bias)

    volume_ratio = _number(one_hour_summary, "volume_ratio_1h")
    volume_status = (
        "insufficient"
        if volume_ratio is None
        else "sufficient"
        if volume_ratio >= MIN_ONE_HOUR_VOLUME_RATIO
        else "low"
    )
    flow_scale_status = _flow_scale_status(facts)
    crowding_state = str(signal.get("crowding_state") or "none")
    gates = signal.get("hard_gates")
    gates = gates if isinstance(gates, Mapping) else {}
    directional_gates_passed = gates.get(f"{bias}_passed") is True
    score = _number(
        signal,
        f"{bias}_evidence_score",
        f"{bias}_readiness",
    )
    if score is None:
        score = 0.0

    reasons: list[str] = []
    if signal.get("data_complete") is not True:
        reasons.append("directional_data_incomplete")
    if not position["ready"]:
        reasons.append("position_data_insufficient")
    if volume_status == "insufficient":
        reasons.append("volume_data_insufficient")
    elif volume_status == "low":
        reasons.append("volume_below_confirmation_floor")
    if flow_scale_status == "insufficient":
        reasons.append("active_flow_scale_insufficient")
    elif flow_scale_status == "low":
        reasons.append("active_flow_below_confirmation_floor")
    if position["extended"]:
        reasons.append(f"{bias}_72h_{'high' if bias == 'bullish' else 'low'}_extended")
    if crowding_state != "none":
        reasons.append(crowding_state)
    if mechanism == "short_covering":
        reasons.append("short_covering_not_new_long_positioning")
    elif mechanism == "long_liquidation":
        reasons.append("long_liquidation_not_new_short_positioning")
    if bias == "none":
        reasons.append("direction_not_resolved")
    if not directional_gates_passed:
        reasons.append("directional_hard_gates_incomplete")
    reasons = list(dict.fromkeys(reasons))

    data_blocked = (
        signal.get("data_complete") is not True
        or not position["ready"]
        or volume_status == "insufficient"
        or flow_scale_status == "insufficient"
    )
    if data_blocked:
        timing_stage = "insufficient"
        execution_status = "blocked_data"
    elif position["extended"]:
        timing_stage = "extended_no_chase"
        execution_status = "blocked_extension"
    elif volume_status == "low":
        timing_stage = "forming"
        execution_status = "blocked_volume"
    elif flow_scale_status == "low":
        timing_stage = "forming"
        execution_status = "blocked_flow_scale"
    elif crowding_state != "none":
        timing_stage = "crowding_watch"
        execution_status = "blocked_crowding"
    elif mechanism in _OI_RELEASE_MECHANISMS:
        timing_stage = "mechanism_watch"
        execution_status = "wait_new_positioning"
    elif bias == "none":
        timing_stage = "conflicting"
        execution_status = "wait_direction"
    elif directional_gates_passed:
        timing_stage = "confirmed"
        execution_status = "retest_ready"
    elif score >= 60:
        timing_stage = "forming"
        execution_status = "wait_confirmation"
    else:
        timing_stage = "discovered"
        execution_status = "wait_confirmation"

    initial_alert_eligible = (
        bias in {"bullish", "bearish"}
        and timing_stage in {"forming", "confirmed"}
        and execution_status in {"wait_confirmation", "retest_ready"}
    )
    plan_eligible = (
        timing_stage == "confirmed"
        and execution_status == "retest_ready"
        and directional_gates_passed
    )
    return {
        "version": PHASE_VERSION,
        "bias": bias,
        "mechanism": mechanism,
        "timing_stage": timing_stage,
        "execution_status": execution_status,
        "position_status": position["position_status"],
        "range_position_72h": position["range_position_72h"],
        "extension_atr": position["extension_atr"],
        "volume_status": volume_status,
        "volume_ratio_1h": volume_ratio,
        "active_flow_scale_status": flow_scale_status,
        "primary_block_reason": reasons[0] if reasons else "none",
        "initial_alert_eligible": initial_alert_eligible,
        "ai_eligible": initial_alert_eligible,
        "plan_eligible": plan_eligible,
        "reason_codes": reasons,
        "score_effect": SCORE_EFFECT,
        "directional_score": score,
        "directional_gates_passed": directional_gates_passed,
        "semantics": "phase_and_execution_do_not_change_directional_score",
    }


__all__ = [
    "PHASE_VERSION",
    "SCORE_EFFECT",
    "MIN_CLOSED_ONE_HOUR_CANDLES",
    "HIGH_RANGE_POSITION",
    "LOW_RANGE_POSITION",
    "EXTENDED_ATR_MULTIPLE",
    "MIN_ONE_HOUR_VOLUME_RATIO",
    "MIN_ACTIVE_FLOW_GROSS_USD",
    "MIN_ACTIVE_FLOW_NET_USD",
    "build_one_hour_phase_summary",
    "classify_launch_phase",
]
