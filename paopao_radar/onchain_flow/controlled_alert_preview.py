from __future__ import annotations

from typing import Mapping


def evaluate_controlled_alert_preview(
    *,
    activity_complete: bool,
    analysis_complete: bool,
    existing_rule_gate_met: bool,
    historical_baseline: Mapping[str, object],
    market_convergence: Mapping[str, object],
    enforced: bool = False,
) -> dict[str, object]:
    """Evaluate the fail-closed controlled alert policy."""

    baseline_status = str(historical_baseline.get("status") or "")
    baseline_ready = baseline_status == "ready"
    historical_anomaly = bool(historical_baseline.get("anomaly"))
    multi_window_anomaly = bool(
        historical_baseline.get("multi_window_anomaly")
    )
    market_context_present = (
        int(market_convergence.get("market_signal_count") or 0) > 0
    )

    blockers: list[str] = []
    if not (activity_complete and analysis_complete):
        blockers.append("scan_incomplete")
    if not existing_rule_gate_met:
        blockers.append("existing_rule_gate_not_met")
    if not baseline_ready:
        blockers.append("historical_baseline_not_ready")
    elif not historical_anomaly:
        blockers.append("historical_anomaly_not_observed")
    if not market_context_present:
        blockers.append("market_context_not_present")

    eligible = not blockers
    preview_level = (
        "high"
        if eligible and multi_window_anomaly
        else ("medium" if eligible else "none")
    )
    return {
        "status": "eligible" if eligible else "blocked",
        "would_alert": eligible,
        "preview_level": preview_level,
        "existing_rule_gate_met": bool(existing_rule_gate_met),
        "historical_baseline_ready": baseline_ready,
        "historical_anomaly": historical_anomaly,
        "multi_window_anomaly": multi_window_anomaly,
        "market_context_present": market_context_present,
        "market_convergence_level": str(
            market_convergence.get("level") or "none"
        ),
        "block_reasons": blockers,
        "policy": "controlled_anomaly_v1",
        "enforced": bool(enforced),
        "dry_run_only": not bool(enforced),
        "notification_gate_changed": bool(enforced),
        "telegram_calls": 0,
        "persistent_messages": 0,
    }
