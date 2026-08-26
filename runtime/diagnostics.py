from __future__ import annotations

from datetime import datetime
import time
from typing import Mapping

from config import Settings
from shared.storage import JsonStore


_RUNNING_STATUSES = {
    "running",
    "summary_failed",
    "announcement_risk_failed",
    "flow_failed",
    "consolidation_breakout_failed",
    "funding_alert_failed",
    "pulse_failed",
}

_RUNTIME_MODES = {"loop", "daemon", "live", "once", "pulse"}
_RUNTIME_TASKS = {"loop", "once", "pulse"}


def _as_mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _timestamp(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return max(0, int(value))
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        return max(0, int(float(text)))
    except ValueError:
        pass
    try:
        return max(0, int(datetime.fromisoformat(text).timestamp()))
    except (OSError, OverflowError, ValueError):
        return 0


def _timestamp_text(value: object) -> str:
    timestamp = _timestamp(value)
    if not timestamp:
        return ""
    try:
        return datetime.fromtimestamp(timestamp).isoformat(timespec="seconds")
    except (OSError, OverflowError, ValueError):
        return ""


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _safe_runtime_value(value: object, allowed: set[str]) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else "unknown"


def _safe_push_status(value: object) -> str:
    status = str(value or "").strip().lower()
    return status if status in {
        "sent",
        "dry_run",
        "skipped",
        "blocked",
        "failed",
        "partial",
        "not_requested",
        "no_new_alerts",
    } else "not_recorded"


def _safe_error_code(value: object) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 80:
        return ""
    return text if all(
        char.isalnum() or char in {"_", "-", "."} for char in text
    ) else ""


def _radar_state(
    *,
    enabled: bool,
    runtime_active: bool,
    runtime_status: str,
    failure_status: str,
    cycle_status: object,
    error_code: object,
    last_run_at: object,
    next_run_at: object,
    last_push_status: str,
    candidate_count: int | None,
    scanned_count: int | None,
    real_send: bool,
    current_ts: int,
    schedule_grace_sec: int,
    disabled_reason: str = "disabled_by_runtime_flag",
) -> dict[str, object]:
    last_run_ts = _timestamp(last_run_at)
    next_run_ts = _timestamp(next_run_at)
    schedule_overdue = bool(
        enabled
        and runtime_active
        and next_run_ts
        and current_ts > next_run_ts + schedule_grace_sec
    )
    safe_cycle_status = str(cycle_status or "unknown").lower()
    safe_error_code = _safe_error_code(error_code)
    if runtime_status == failure_status:
        safe_cycle_status = "failed"
    if not enabled:
        state = "disabled"
        reason = disabled_reason
    elif not runtime_active:
        state = "not_running"
        reason = "main_bot_runtime_not_active"
    elif safe_cycle_status == "failed":
        state = "degraded"
        reason = safe_error_code or failure_status
    elif schedule_overdue:
        state = "stale"
        reason = "scheduled_cycle_overdue"
    elif not last_run_ts:
        state = "waiting_first_cycle"
        reason = "scheduled_not_run_yet"
    else:
        state = "running"
        reason = "last_cycle_completed"
    if real_send:
        delivery_mode = "real"
        delivery_block_reason = ""
        telegram_http_calls = None
        persistent_messages = None
    else:
        delivery_mode = "dry_run"
        delivery_block_reason = "main_bot_dry_run"
        telegram_http_calls = 0
        persistent_messages = 0
    return {
        "enabled": bool(enabled),
        "state": state,
        "state_reason": reason,
        "last_cycle_status": safe_cycle_status,
        "last_error_code": safe_error_code,
        "last_run_at": _timestamp_text(last_run_at),
        "next_run_at": _timestamp_text(next_run_at),
        "schedule_overdue": schedule_overdue,
        "schedule_grace_sec": schedule_grace_sec,
        "last_push_status": last_push_status,
        "candidate_count": candidate_count,
        "scanned_count": scanned_count,
        "delivery_mode": delivery_mode,
        "delivery_block_reason": delivery_block_reason,
        "telegram_http_calls": telegram_http_calls,
        "persistent_messages": persistent_messages,
    }


def build_market_radar_runtime_status(
    settings: Settings,
    store: JsonStore,
    *,
    now: float | None = None,
) -> dict[str, object]:
    """Build local-only, credential-free status for the market radars."""

    current = int(time.time() if now is None else now)
    loaded = store.load(settings.runtime_status_path, {})
    runtime = dict(loaded) if isinstance(loaded, dict) else {}
    updated_at = runtime.get("updated_at", "")
    updated_ts = _timestamp(updated_at)
    heartbeat_age = max(0, current - updated_ts) if updated_ts else None
    task = _safe_runtime_value(runtime.get("task"), _RUNTIME_TASKS)
    mode = _safe_runtime_value(runtime.get("mode"), _RUNTIME_MODES)
    runtime_status = str(runtime.get("status") or "empty")
    heartbeat_fresh = bool(
        heartbeat_age is not None
        and heartbeat_age <= settings.health_runtime_max_age_sec
    )
    runtime_active = bool(
        task == "loop"
        and mode in {"loop", "daemon", "live"}
        and runtime_status in _RUNNING_STATUSES
        and heartbeat_fresh
    )
    real_send = bool(runtime.get("real_send"))
    schedule_grace_sec = max(
        30,
        min(300, settings.health_runtime_max_age_sec // 3),
    )

    pulse_state = _as_mapping(
        store.load(settings.data_dir / "simple_alert_state.json", {})
    )
    pulse_candidates = len(pulse_state)
    runtime_diagnostics = _as_mapping(runtime.get("diagnostics"))
    pulse_diagnostics = _as_mapping(runtime_diagnostics.get("pulse"))
    pulse_simple = _as_mapping(pulse_diagnostics.get("simple"))

    flow_state = _as_mapping(
        store.load(settings.flow_candidate_state_path, {})
    )
    flow_candidates = _nonnegative_int(flow_state.get("total_candidates"))
    flow_selected = _nonnegative_int(flow_state.get("selected_count"))

    funding_state = _as_mapping(
        store.load(settings.funding_alert_state_path, {})
    )
    funding_candidates = _nonnegative_int(
        funding_state.get("last_alert_count")
    )
    funding_scanned = _nonnegative_int(funding_state.get("last_scanned"))

    consolidation_state = _as_mapping(
        store.load(settings.consolidation_breakout_state_path, {})
    )
    consolidation_entries = _as_mapping(
        consolidation_state.get("tracks")
    )
    consolidation_diagnostics = _as_mapping(
        runtime_diagnostics.get("consolidation_breakout")
    )

    radars = {
        "launch_alert": _radar_state(
            enabled=bool(
                settings.pulse_radar_enable
                and not bool(runtime.get("no_launch"))
            ),
            disabled_reason=(
                "disabled_by_config"
                if not settings.pulse_radar_enable
                else "disabled_by_runtime_flag"
            ),
            runtime_active=runtime_active,
            runtime_status=runtime_status,
            failure_status="pulse_failed",
            cycle_status=runtime.get("pulse_cycle_status"),
            error_code=runtime.get("pulse_error_code"),
            last_run_at=runtime.get("last_launch_at"),
            next_run_at=runtime.get("next_launch_at"),
            last_push_status=runtime.get("pulse_cycle_status"),
            candidate_count=pulse_candidates,
            scanned_count=_nonnegative_int(pulse_simple.get("scanned")),
            real_send=real_send,
            current_ts=current,
            schedule_grace_sec=schedule_grace_sec,
        ),
        "radar_summary": _radar_state(
            enabled=bool(
                settings.radar_summary_enable
                and not bool(runtime.get("no_summary"))
            ),
            disabled_reason=(
                "disabled_by_config"
                if not settings.radar_summary_enable
                else "disabled_by_runtime_flag"
            ),
            runtime_active=runtime_active,
            runtime_status=runtime_status,
            failure_status="summary_failed",
            cycle_status=runtime.get("summary_cycle_status"),
            error_code=runtime.get("summary_error_code"),
            last_run_at=runtime.get("last_summary_at"),
            next_run_at=runtime.get("next_summary_at"),
            last_push_status=_safe_push_status(
                runtime.get("summary_push")
            ),
            candidate_count=None,
            scanned_count=_nonnegative_int(runtime.get("radar_scan_limit")),
            real_send=real_send,
            current_ts=current,
            schedule_grace_sec=schedule_grace_sec,
        ),
        "announcement_risk": _radar_state(
            enabled=bool(
                settings.announcement_risk_enable
                and not bool(runtime.get("no_announcements"))
            ),
            disabled_reason=(
                "disabled_by_config"
                if not settings.announcement_risk_enable
                else "disabled_by_runtime_flag"
            ),
            runtime_active=runtime_active,
            runtime_status=runtime_status,
            failure_status="announcement_risk_failed",
            cycle_status=runtime.get("announcement_risk_cycle_status"),
            error_code=runtime.get("announcement_risk_error_code"),
            last_run_at=runtime.get("last_announcement_at"),
            next_run_at=runtime.get("next_announcement_at"),
            last_push_status=_safe_push_status(
                runtime.get("announcement_risk_push")
            ),
            candidate_count=_nonnegative_int(
                runtime.get("announcement_risk_candidate_count")
            ),
            scanned_count=_nonnegative_int(
                runtime.get("announcement_risk_scanned_count")
            ),
            real_send=real_send,
            current_ts=current,
            schedule_grace_sec=schedule_grace_sec,
        ),
        "funding_alert": _radar_state(
            enabled=bool(
                settings.funding_alert_enable
                and not bool(runtime.get("no_funding_alert"))
            ),
            disabled_reason=(
                "disabled_by_config"
                if not settings.funding_alert_enable
                else "disabled_by_runtime_flag"
            ),
            runtime_active=runtime_active,
            runtime_status=runtime_status,
            failure_status="funding_alert_failed",
            cycle_status=runtime.get("funding_alert_cycle_status"),
            error_code=runtime.get("funding_alert_error_code"),
            last_run_at=(
                runtime.get("last_funding_alert_at")
                or funding_state.get("updated_at")
            ),
            next_run_at=runtime.get("next_funding_alert_at"),
            last_push_status=_safe_push_status(
                runtime.get("funding_alert_push")
            ),
            candidate_count=funding_candidates,
            scanned_count=funding_scanned,
            real_send=real_send,
            current_ts=current,
            schedule_grace_sec=schedule_grace_sec,
        ),
        "flow_radar": _radar_state(
            enabled=bool(
                settings.flow_radar_enable
                and not bool(runtime.get("no_flow"))
            ),
            disabled_reason=(
                "disabled_by_config"
                if not settings.flow_radar_enable
                else "disabled_by_runtime_flag"
            ),
            runtime_active=runtime_active,
            runtime_status=runtime_status,
            failure_status="flow_failed",
            cycle_status=runtime.get("flow_cycle_status"),
            error_code=runtime.get("flow_error_code"),
            last_run_at=(
                runtime.get("last_flow_at")
                or flow_state.get("updated_at")
                or flow_state.get("observed_at")
            ),
            next_run_at=runtime.get("next_flow_at"),
            last_push_status=_safe_push_status(runtime.get("flow_push")),
            candidate_count=flow_candidates,
            scanned_count=flow_selected,
            real_send=real_send,
            current_ts=current,
            schedule_grace_sec=schedule_grace_sec,
        ),
        "consolidation_breakout": _radar_state(
            enabled=bool(
                settings.consolidation_breakout_enable
                and not bool(runtime.get("no_consolidation_breakout"))
            ),
            disabled_reason=(
                "disabled_by_config"
                if not settings.consolidation_breakout_enable
                else "disabled_by_runtime_flag"
            ),
            runtime_active=runtime_active,
            runtime_status=runtime_status,
            failure_status="consolidation_breakout_failed",
            cycle_status=runtime.get(
                "consolidation_breakout_cycle_status"
            ),
            error_code=runtime.get("consolidation_breakout_error_code"),
            last_run_at=(
                runtime.get("last_consolidation_breakout_at")
                or consolidation_state.get("updated_at")
            ),
            next_run_at=runtime.get("next_consolidation_breakout_at"),
            last_push_status=_safe_push_status(
                runtime.get("consolidation_breakout_push")
            ),
            candidate_count=_nonnegative_int(
                consolidation_diagnostics.get("candidate_count")
            ),
            scanned_count=(
                _nonnegative_int(
                    consolidation_diagnostics.get("scanned_pairs")
                )
                or len(consolidation_entries)
            ),
            real_send=real_send,
            current_ts=current,
            schedule_grace_sec=schedule_grace_sec,
        ),
    }
    if not runtime:
        overall_status = "not_initialized"
        overall_reason = "runtime_status_missing"
    elif not heartbeat_fresh:
        overall_status = "stale"
        overall_reason = "runtime_heartbeat_stale"
    elif not runtime_active:
        overall_status = "not_running"
        overall_reason = "main_bot_loop_not_active"
    elif any(
        item.get("state") in {"degraded", "stale"}
        for item in radars.values()
    ):
        overall_status = "degraded"
        overall_reason = "one_or_more_radars_degraded"
    else:
        overall_status = "running"
        overall_reason = "runtime_heartbeat_fresh"
    return {
        "status": overall_status,
        "reason": overall_reason,
        "runtime_mode": mode,
        "runtime_task": task,
        "runtime_heartbeat_age_sec": heartbeat_age,
        "runtime_heartbeat_fresh": heartbeat_fresh,
        "delivery_mode": "real" if real_send else "dry_run",
        "real_send": real_send,
        "telegram_http_policy": (
            "real_send_gate_enabled" if real_send else "zero_by_dry_run"
        ),
        "radars": radars,
        "network_activity": False,
        "telegram_calls": 0,
        "credentials_exposed": False,
    }


__all__ = ["build_market_radar_runtime_status"]
