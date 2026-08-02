from __future__ import annotations

from typing import Mapping, Sequence


ALLOWED_MARKET_MODULES = frozenset(
    {"launch", "flow", "funding", "announcement"}
)


def evaluate_market_convergence(
    linked_market_signals: Sequence[Mapping[str, object]],
    historical_baseline: Mapping[str, object],
    *,
    onchain_actionable: bool,
    behavior_score: int,
    max_wallet_group_score: int,
) -> dict[str, object]:
    safe_signals = [
        item
        for item in linked_market_signals[:10]
        if str(item.get("module") or "") in ALLOWED_MARKET_MODULES
    ]
    modules = sorted(
        {str(item.get("module") or "") for item in safe_signals}
    )
    scores = [
        int(item.get("score") or 0)
        for item in safe_signals
        if item.get("score") is not None
    ]
    newest_age_sec = min(
        (max(0, int(item.get("age_sec") or 0)) for item in safe_signals),
        default=None,
    )
    baseline_anomaly = bool(historical_baseline.get("anomaly"))
    multi_window_anomaly = bool(
        historical_baseline.get("multi_window_anomaly")
    )
    evidence: list[str] = []
    if safe_signals:
        evidence.append("linked_market_signal_present")
    if len(modules) >= 2:
        evidence.append("multiple_market_modules")
    if onchain_actionable:
        evidence.append("onchain_rule_gate_met")
    if baseline_anomaly:
        evidence.append("historical_scan_anomaly")
    if multi_window_anomaly:
        evidence.append("multi_window_scan_anomaly")

    if not safe_signals:
        status = "no_market_context"
        level = "none"
    elif multi_window_anomaly and onchain_actionable:
        status = "multi_window_anomaly_cooccurrence"
        level = "high"
    elif baseline_anomaly:
        status = "historical_anomaly_cooccurrence"
        level = "medium"
    elif onchain_actionable:
        status = "onchain_market_cooccurrence"
        level = "medium"
    else:
        status = "market_context_only"
        level = "low"

    rule_score = (
        min(
            100,
            20
            + (10 if len(modules) >= 2 else 0)
            + (30 if onchain_actionable else 0)
            + (15 if baseline_anomaly else 0)
            + (25 if multi_window_anomaly else 0),
        )
        if safe_signals
        else 0
    )
    return {
        "status": status,
        "level": level,
        "rule_score": rule_score,
        "score_semantics": "cooccurrence_rule_score_not_probability",
        "direction_alignment": "not_evaluated",
        "market_signal_count": len(safe_signals),
        "market_modules": modules,
        "distinct_market_module_count": len(modules),
        "max_market_signal_score": max(scores) if scores else None,
        "newest_market_signal_age_sec": newest_age_sec,
        "onchain_actionable": bool(onchain_actionable),
        "behavior_score": max(0, int(behavior_score)),
        "max_wallet_group_score": max(0, int(max_wallet_group_score)),
        "baseline_status": str(historical_baseline.get("status") or ""),
        "historical_scan_anomaly": baseline_anomaly,
        "multi_window_scan_anomaly": multi_window_anomaly,
        "evidence": evidence,
        "limitations": [
            "market_signal_direction_not_structured",
            "cooccurrence_not_causation",
        ],
        "notification_gate_changed": False,
    }
