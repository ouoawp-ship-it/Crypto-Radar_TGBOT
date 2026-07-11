from __future__ import annotations

import json
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import Settings
from .lifecycle_intelligence_store import IntelligenceStore, source_signature
from .lifecycle_store import normalize_lifecycle_symbol, safe_float, safe_int


REPLAY_MODEL_VERSION = "lifecycle-replay-v1"
NOT_ADVICE = "仅用于信号整理和风险提示，不构成投资建议，不执行自动交易。"

LEVEL_RANK = {"unknown": 0, "15m": 1, "1h": 2, "4h": 3, "24h": 4}
UPGRADE_EVENT_LEVEL = {
    "timeframe_upgrade_1h": "1h",
    "timeframe_upgrade_4h": "4h",
    "timeframe_upgrade_24h": "24h",
}
EVENT_LABELS = {
    "first_signal": "首次信号",
    "same_level_confirm": "同级确认",
    "timeframe_upgrade_1h": "升级到 1H",
    "timeframe_upgrade_4h": "升级到 4H",
    "timeframe_upgrade_24h": "升级到 24H",
    "volume_expansion": "成交量放大",
    "oi_accumulation": "OI 增长确认",
    "oi_price_divergence": "OI 与价格背离",
    "futures_cvd_confirmed": "合约主动买盘确认",
    "spot_cvd_confirmed": "现货主动买盘确认",
    "cvd_divergence": "现货与合约 CVD 背离",
    "funding_crowded": "资金费率拥挤",
    "funding_cooling": "资金费率冷却",
    "short_term_weakening": "短周期走弱",
    "major_timeframe_weakening": "大周期走弱",
    "risk_warning": "风险升高",
    "launch_failed": "启动失败",
    "lifecycle_closed": "生命周期结束",
}
CONFIRMATION_EVENTS = {
    "same_level_confirm",
    "volume_expansion",
    "oi_accumulation",
    "futures_cvd_confirmed",
    "spot_cvd_confirmed",
}
RISK_EVENTS = {
    "oi_price_divergence",
    "cvd_divergence",
    "funding_crowded",
    "major_timeframe_weakening",
    "risk_warning",
    "launch_failed",
}
COOLING_EVENTS = {"short_term_weakening", "funding_cooling"}


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        try:
            parsed = datetime.fromtimestamp(float(value), timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: Any) -> str:
    parsed = _parse_time(value)
    return parsed.isoformat() if parsed else str(value or "")


def _json_loads(value: Any, default: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _first_number(*values: Any) -> float | None:
    for value in values:
        number = safe_float(value)
        if number is not None:
            return number
    return None


def _round(value: Any, digits: int = 4) -> float | None:
    number = safe_float(value)
    return round(number, digits) if number is not None else None


def _event_sort_key(event: dict[str, Any]) -> tuple[datetime, int]:
    return (
        _parse_time(event.get("event_time")) or datetime.max.replace(tzinfo=timezone.utc),
        safe_int(event.get("id")),
    )


def _snapshot_sort_key(snapshot: dict[str, Any]) -> tuple[datetime, int]:
    return (
        _parse_time(snapshot.get("snapshot_time")) or datetime.max.replace(tzinfo=timezone.utc),
        safe_int(snapshot.get("id")),
    )


def _event_metrics(event: dict[str, Any]) -> dict[str, Any]:
    metrics = event.get("metrics")
    if not isinstance(metrics, dict):
        metrics = _json_loads(event.get("metrics_json"), {})
    return metrics if isinstance(metrics, dict) else {}


def _normalize_level(value: Any) -> str:
    text = str(value or "unknown").strip().lower()
    aliases = {"1d": "24h", "1day": "24h", "day": "24h", "15min": "15m"}
    normalized = aliases.get(text, text)
    return normalized if normalized in LEVEL_RANK else "unknown"


def associate_outcomes(
    lifecycle: dict[str, Any],
    events: Iterable[dict[str, Any]],
    outcomes: Iterable[dict[str, Any]],
    *,
    tolerance_sec: int = 300,
) -> dict[str, Any]:
    """Associate all deterministic Outcome rows without symbol-only matching.

    Persisted v1.78.1 links are authoritative.  The in-memory compatibility path
    follows first signal -> event signals -> latest signal and only allows the
    legacy time fallback when the lifecycle has no signal IDs at all.
    """

    symbol = normalize_lifecycle_symbol(lifecycle.get("symbol"))
    lifecycle_id = safe_int(lifecycle.get("id") or lifecycle.get("lifecycle_id"))
    candidates = [
        dict(item)
        for item in outcomes
        if normalize_lifecycle_symbol(item.get("symbol")) == symbol
    ]
    persisted = [
        item
        for item in candidates
        if safe_int(item.get("_lifecycle_id") or item.get("link_lifecycle_id")) == lifecycle_id
    ]
    if persisted:
        primary = next((item for item in persisted if safe_int(item.get("_is_primary") or item.get("is_primary")) == 1), None)
        method = str((primary or persisted[0]).get("_link_method") or (primary or persisted[0]).get("link_method") or "persisted")
        return {
            "method": method,
            "items": _sort_outcomes(persisted),
            "count": len(persisted),
            "persisted": True,
            "primary_signal_id": safe_int((primary or {}).get("signal_id")) or None,
        }

    ordered_events = sorted((dict(item) for item in events), key=_event_sort_key)
    first_id = safe_int(lifecycle.get("first_signal_id"))
    latest_id = safe_int(lifecycle.get("latest_signal_id"))
    event_ids: list[int] = []
    for item in ordered_events:
        signal_id = safe_int(item.get("signal_id"))
        if signal_id > 0 and signal_id not in event_ids:
            event_ids.append(signal_id)
    ordered_ids: list[tuple[int, str]] = []
    if first_id > 0:
        ordered_ids.append((first_id, "first_signal_id"))
    ordered_ids.extend((value, "event_signal_id") for value in event_ids if value != first_id)
    if latest_id > 0 and latest_id not in {value for value, _ in ordered_ids}:
        ordered_ids.append((latest_id, "latest_signal_id"))
    if ordered_ids:
        allowed = {value for value, _ in ordered_ids}
        rows = [item for item in candidates if safe_int(item.get("signal_id")) in allowed]
        if rows:
            methods = {method for value, method in ordered_ids if any(safe_int(item.get("signal_id")) == value for item in rows)}
            primary_signal_id = next((value for value, _ in ordered_ids if any(safe_int(item.get("signal_id")) == value for item in rows)), 0)
            method = next((name for value, name in ordered_ids if value == primary_signal_id), "signal_ids")
            return {
                "method": method,
                "items": _sort_outcomes(rows),
                "count": len(rows),
                "primary_signal_id": primary_signal_id or None,
                "methods": sorted(methods),
            }
        # Signal-bearing lifecycles must wait for exact Outcome rows; a same-symbol
        # record from another moment must never be substituted.
        return {"method": "none", "items": [], "count": 0}

    sources: list[tuple[datetime, str, str]] = []
    first_time = _parse_time(lifecycle.get("first_signal_at") or lifecycle.get("created_at"))
    if first_time is not None:
        sources.append((first_time, str(lifecycle.get("first_signal_module") or ""), str(lifecycle.get("first_signal_template") or "")))
    for event in ordered_events:
        event_time = _parse_time(event.get("event_time"))
        if event_time is not None:
            sources.append((event_time, str(event.get("source_module") or ""), str(event.get("source_template") or "")))
    matched_signal_ids: set[int] = set()
    for source_time, module, template in sources:
        if not module and not template:
            continue
        distances: dict[int, float] = {}
        for item in candidates:
            signal_id = safe_int(item.get("signal_id"))
            signal_time = _parse_time(item.get("signal_time"))
            module_match = bool(module and module.lower() == str(item.get("module") or "").lower())
            template_match = bool(template and template.lower() == str(item.get("signal_type") or "").lower())
            if signal_id <= 0 or signal_time is None or not (module_match or template_match):
                continue
            distance = abs((signal_time - source_time).total_seconds())
            if distance <= max(0, int(tolerance_sec)):
                distances[signal_id] = min(distance, distances.get(signal_id, distance))
        if not distances:
            continue
        minimum = min(distances.values())
        winners = [signal_id for signal_id, distance in distances.items() if distance == minimum]
        if len(winners) != 1:
            return {"method": "ambiguous_match", "items": [], "count": 0, "ambiguous": True}
        matched_signal_ids.add(winners[0])
    if matched_signal_ids:
        rows = [item for item in candidates if safe_int(item.get("signal_id")) in matched_signal_ids]
        return {
            "method": "symbol_time_module",
            "items": _sort_outcomes(rows),
            "count": len(rows),
            "primary_signal_id": next(iter(sorted(matched_signal_ids))),
        }
    return {"method": "none", "items": [], "count": 0}


def _sort_outcomes(outcomes: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        (dict(item) for item in outcomes),
        key=lambda item: (
            safe_int(item.get("horizon_sec")),
            _parse_time(item.get("due_time")) or datetime.min.replace(tzinfo=timezone.utc),
            safe_int(item.get("id")),
        ),
    )


def lifecycle_result_label(
    *,
    final_return_pct: float | None,
    max_price_gain_pct: float | None,
    max_drawdown_pct: float | None,
    highest_level: str,
    final_state: str,
    risk_event_count: int,
    has_outcome: bool,
) -> str:
    # A lifecycle state or live price snapshot is not a matured Outcome.  Keep
    # result labels neutral until at least one success Outcome exists so
    # pending/not_due/unavailable records are never presented as a loss.
    if not has_outcome:
        return "insufficient_data"
    if str(final_state or "") == "failed":
        return "failed"
    final_return = safe_float(final_return_pct)
    max_gain = safe_float(max_price_gain_pct)
    drawdown = safe_float(max_drawdown_pct)
    if risk_event_count > 0 and final_return is not None and final_return <= -2.0:
        return "risk_avoided"
    if (final_return is not None and final_return <= -5.0) or (drawdown is not None and drawdown <= -10.0):
        return "failed"
    if (
        final_return is not None
        and final_return >= 5.0
        and (max_gain or final_return) >= 8.0
        and LEVEL_RANK.get(_normalize_level(highest_level), 0) >= 3
    ):
        return "strong_success"
    if (final_return is not None and final_return >= 3.0) or (max_gain is not None and max_gain >= 6.0):
        return "success"
    if (final_return is not None and final_return > 0.0) or (max_gain is not None and max_gain >= 3.0):
        return "partial_success"
    return "neutral" if has_outcome or final_return is not None else "insufficient_data"


def _outcome_summary(link: dict[str, Any]) -> dict[str, Any]:
    rows = list(link.get("items") or [])
    all_successful = [
        item
        for item in rows
        if str(item.get("data_status") or "").lower() == "success"
        and safe_float(item.get("final_return_pct")) is not None
    ]
    persisted_primary = next(
        (item for item in rows if safe_int(item.get("_is_primary") or item.get("is_primary")) == 1),
        None,
    )
    primary_signal_id = safe_int(link.get("primary_signal_id") or (persisted_primary or {}).get("signal_id"))
    successful = [
        item for item in all_successful
        if primary_signal_id <= 0 or safe_int(item.get("signal_id")) == primary_signal_id
    ]
    result_row = max(
        successful,
        key=lambda item: (safe_int(item.get("horizon_sec")), safe_int(item.get("id"))),
    ) if successful else None
    primary = persisted_primary or result_row
    gains = [safe_float(item.get("max_gain_pct")) for item in successful]
    drawdowns = [safe_float(item.get("max_drawdown_pct")) for item in successful]
    return {
        "primary": primary,
        "result_row": result_row,
        "status": "success" if result_row is not None else str((primary or {}).get("data_status") or (rows[-1].get("data_status") if rows else "insufficient_data")),
        "final_return_pct": _round((result_row or {}).get("final_return_pct")),
        "max_gain_pct": _round(max(value for value in gains if value is not None)) if any(value is not None for value in gains) else None,
        "max_drawdown_pct": _round(min(value for value in drawdowns if value is not None)) if any(value is not None for value in drawdowns) else None,
        "has_outcome": bool(successful),
    }


def _price_statistics(
    lifecycle: dict[str, Any],
    events: list[dict[str, Any]],
    snapshots: list[dict[str, Any]],
) -> dict[str, float | None]:
    first_price = _first_number(lifecycle.get("first_price"))
    timed_prices: list[tuple[datetime, float]] = []
    first_time = _parse_time(lifecycle.get("first_signal_at")) or datetime.min.replace(tzinfo=timezone.utc)
    if first_price is not None:
        timed_prices.append((first_time, first_price))
    for item in events:
        price = safe_float(item.get("price"))
        timestamp = _parse_time(item.get("event_time"))
        if price is not None and timestamp is not None:
            timed_prices.append((timestamp, price))
    for item in snapshots:
        price = safe_float(item.get("price"))
        timestamp = _parse_time(item.get("snapshot_time"))
        if price is not None and timestamp is not None:
            timed_prices.append((timestamp, price))
    timed_prices.sort(key=lambda pair: pair[0])
    if first_price is None and timed_prices:
        first_price = timed_prices[0][1]
    if first_price is None or first_price == 0 or not timed_prices:
        return {"max_price_gain_pct": None, "max_drawdown_pct": None, "final_return_pct": None}
    gains = [(price - first_price) / first_price * 100.0 for _, price in timed_prices]
    running_peak = timed_prices[0][1]
    drawdowns: list[float] = []
    for _, price in timed_prices:
        running_peak = max(running_peak, price)
        drawdowns.append((price - running_peak) / running_peak * 100.0 if running_peak else 0.0)
    return {
        "max_price_gain_pct": _round(max(gains)),
        "max_drawdown_pct": _round(min(drawdowns)),
        "final_return_pct": _round(gains[-1]),
    }


def _build_upgrade_path(lifecycle: dict[str, Any], events: list[dict[str, Any]]) -> tuple[str, dict[str, int | None]]:
    start_time = _parse_time(lifecycle.get("first_signal_at"))
    first_level = _normalize_level(lifecycle.get("first_signal_level"))
    path = [first_level]
    reached: dict[str, int | None] = {"1h": None, "4h": None, "24h": None}
    if first_level in reached:
        reached[first_level] = 0
    for event in events:
        level = UPGRADE_EVENT_LEVEL.get(str(event.get("event_type") or ""))
        if not level:
            continue
        if level not in path:
            path.append(level)
        event_time = _parse_time(event.get("event_time"))
        if reached[level] is None and start_time is not None and event_time is not None:
            reached[level] = max(0, int((event_time - start_time).total_seconds()))
    return " → ".join(path), reached


def _frame_summary(event: dict[str, Any], metrics: dict[str, Any]) -> str:
    event_type = str(event.get("event_type") or "unknown")
    label = EVENT_LABELS.get(event_type, event_type)
    parts = [label]
    oi_change = _first_number(metrics.get("oi_change_from_first_pct"), event.get("oi_change_pct"))
    spot_cvd = _first_number(metrics.get("spot_cvd_delta"), event.get("spot_cvd_delta"))
    futures_cvd = _first_number(metrics.get("futures_cvd_delta"), event.get("futures_cvd_delta"))
    if oi_change is not None and oi_change > 0:
        parts.append(f"OI +{oi_change:.2f}%")
    if spot_cvd is not None and spot_cvd > 0:
        parts.append("现货主动买盘增强")
    if futures_cvd is not None and futures_cvd > 0:
        parts.append("合约主动买盘增强")
    if event_type in RISK_EVENTS:
        parts.append("进入风险观察")
    return "，".join(parts) + "。"


def build_replay(
    lifecycle: dict[str, Any],
    events: Iterable[dict[str, Any]],
    snapshots: Iterable[dict[str, Any]],
    outcomes: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    lifecycle_id = safe_int(lifecycle.get("id") or lifecycle.get("lifecycle_id"))
    symbol = normalize_lifecycle_symbol(lifecycle.get("symbol"))
    if lifecycle_id <= 0 or not symbol:
        raise ValueError("valid lifecycle id and symbol are required")
    ordered_events = sorted((dict(item) for item in events), key=_event_sort_key)
    ordered_snapshots = sorted((dict(item) for item in snapshots), key=_snapshot_sort_key)
    coverage = lifecycle.get("outcome_coverage") if isinstance(lifecycle.get("outcome_coverage"), dict) else {}
    if not coverage:
        coverage = {
            key: lifecycle.get(key)
            for key in (
                "primary_outcome_id", "horizon_1h_status", "horizon_4h_status",
                "horizon_24h_status", "horizon_72h_status", "linked_horizon_count",
                "mature_horizon_count", "link_coverage_ratio", "maturity_ratio",
                "coverage_label", "maturity_label", "unlinked_reason",
            )
            if key in lifecycle
        }
    coverage_present = (
        bool(lifecycle.get("outcome_coverage_present"))
        if "outcome_coverage_present" in lifecycle
        else bool(coverage)
    )
    outcome_rows = [dict(item) for item in outcomes]
    if coverage_present:
        persisted_rows = [
            item for item in outcome_rows
            if safe_int(item.get("_lifecycle_id") or item.get("link_lifecycle_id")) == lifecycle_id
        ]
        outcome_link = (
            associate_outcomes(lifecycle, ordered_events, persisted_rows)
            if persisted_rows
            else {"method": "none", "items": [], "count": 0, "persisted": True}
        )
    else:
        outcome_link = associate_outcomes(lifecycle, ordered_events, outcome_rows)
    outcome_stats = _outcome_summary(outcome_link)
    upgrade_path, reached = _build_upgrade_path(lifecycle, ordered_events)
    intelligence_score = _first_number(
        lifecycle.get("intelligence_score"),
        (lifecycle.get("intelligence") or {}).get("intelligence_score")
        if isinstance(lifecycle.get("intelligence"), dict) else None,
    ) or 0.0

    frames: list[dict[str, Any]] = []
    for index, event in enumerate(ordered_events, 1):
        metrics = _event_metrics(event)
        frame = {
            "frame_index": index,
            "event_id": safe_int(event.get("id")) or None,
            "event_time": _iso(event.get("event_time")),
            "event_type": str(event.get("event_type") or "unknown"),
            "event_label": EVENT_LABELS.get(str(event.get("event_type") or ""), str(event.get("event_type") or "未知事件")),
            "state_before": str(event.get("previous_state") or ""),
            "state_after": str(event.get("new_state") or ""),
            "signal_level": _normalize_level(event.get("event_level")),
            "price": _round(_first_number(event.get("price"), metrics.get("price"))),
            "price_change_from_first_pct": _round(
                _first_number(event.get("price_change_from_first_pct"), metrics.get("price_change_from_first_pct"))
            ),
            "oi_change_from_first_pct": _round(
                _first_number(event.get("oi_change_pct"), metrics.get("oi_change_from_first_pct"))
            ),
            "spot_cvd_delta": _round(_first_number(event.get("spot_cvd_delta"), metrics.get("spot_cvd_delta"))),
            "futures_cvd_delta": _round(
                _first_number(event.get("futures_cvd_delta"), metrics.get("futures_cvd_delta"))
            ),
            "funding_rate": _round(_first_number(event.get("funding_rate"), metrics.get("funding_rate")), 8),
            "lifecycle_score": _round(_first_number(event.get("event_score"), lifecycle.get("lifecycle_score"))),
            "risk_score": _round(_first_number(event.get("risk_score"), lifecycle.get("risk_score"))),
            "intelligence_score": _round(intelligence_score),
            "summary": _frame_summary(event, metrics),
            "metrics": {
                key: metrics.get(key)
                for key in (
                    "volume_change_pct", "quote_volume_change_pct", "volume_multiplier",
                    "oi_value_change_from_first_pct", "data_source_status",
                )
                if key in metrics
            },
        }
        frames.append(frame)

    first_time = _parse_time(lifecycle.get("first_signal_at"))
    closed_at = _parse_time(lifecycle.get("closed_at"))
    if closed_at is not None:
        observed_times = [closed_at]
    else:
        observed_times = [_parse_time(item.get("event_time")) for item in ordered_events]
        observed_times.extend(_parse_time(item.get("snapshot_time")) for item in ordered_snapshots)
        observed_times.extend(
            value
            for value in (
                _parse_time(lifecycle.get("updated_at")),
                _parse_time(lifecycle.get("latest_signal_at")),
            )
            if value is not None
        )
    valid_times = [value for value in observed_times if value is not None]
    duration_sec = max(0, int((max(valid_times) - first_time).total_seconds())) if first_time and valid_times else 0
    price_stats = _price_statistics(lifecycle, ordered_events, ordered_snapshots)
    max_gain = _first_number(outcome_stats.get("max_gain_pct"))
    max_drawdown = _first_number(outcome_stats.get("max_drawdown_pct"))
    final_return = _first_number(outcome_stats.get("final_return_pct"))
    event_types = [str(item.get("event_type") or "") for item in ordered_events]
    final_state = str(lifecycle.get("current_state") or (ordered_events[-1].get("new_state") if ordered_events else ""))
    result_label = lifecycle_result_label(
        final_return_pct=final_return,
        max_price_gain_pct=max_gain,
        max_drawdown_pct=max_drawdown,
        highest_level=str(lifecycle.get("highest_level") or "unknown"),
        final_state=final_state,
        risk_event_count=sum(1 for value in event_types if value in RISK_EVENTS),
        has_outcome=bool(outcome_stats.get("has_outcome")),
    )
    horizon_statuses: dict[str, str] = {}
    for horizon in ("1h", "4h", "24h", "72h"):
        value = str(coverage.get(f"horizon_{horizon}_status") or "")
        if not value:
            rows = [item for item in outcome_link.get("items") or [] if str(item.get("horizon") or "") == horizon]
            value = str((rows[0] if rows else {}).get("data_status") or "missing")
        horizon_statuses[horizon] = value
    primary_row = outcome_stats.get("primary") if isinstance(outcome_stats.get("primary"), dict) else {}
    mature_horizons = [key for key, value in horizon_statuses.items() if value == "success"]
    pending_horizons = [key for key, value in horizon_statuses.items() if value in {"not_due", "pending", "ready", "missing"}]
    unavailable_horizons = [key for key, value in horizon_statuses.items() if value == "unavailable"]
    summary = {
        "lifecycle_id": lifecycle_id,
        "symbol": symbol,
        "replay_version": REPLAY_MODEL_VERSION,
        "duration_sec": duration_sec,
        "duration": duration_sec,
        "first_signal_level": _normalize_level(lifecycle.get("first_signal_level")),
        "highest_level": _normalize_level(lifecycle.get("highest_level")),
        "upgrade_path": upgrade_path,
        "event_count": len(ordered_events),
        "confirmation_count": sum(1 for value in event_types if value in CONFIRMATION_EVENTS),
        "risk_event_count": sum(1 for value in event_types if value in RISK_EVENTS),
        "cooling_count": sum(1 for value in event_types if value in COOLING_EVENTS),
        "time_to_1h_sec": reached["1h"],
        "time_to_4h_sec": reached["4h"],
        "time_to_24h_sec": reached["24h"],
        "max_price_gain_pct": _round(max_gain),
        "max_drawdown_pct": _round(max_drawdown),
        "final_return_pct": _round(final_return),
        "observed_max_price_gain_pct": _round(price_stats.get("max_price_gain_pct")),
        "observed_max_drawdown_pct": _round(price_stats.get("max_drawdown_pct")),
        "observed_final_return_pct": _round(price_stats.get("final_return_pct")),
        "final_state": final_state,
        "result_label": result_label,
        "outcome_status": outcome_stats.get("status"),
        "outcome_count": safe_int(outcome_link.get("count")),
        "outcome_link_method": str(outcome_link.get("method") or "none"),
        "outcome_link_status": str(coverage.get("coverage_label") or ("linked" if outcome_link.get("count") else "unlinked")),
        "primary_outcome_signal_id": safe_int(primary_row.get("signal_id")) or None,
        "primary_outcome": {
            "signal_id": safe_int(primary_row.get("signal_id")) or None,
            "horizon": str(primary_row.get("horizon") or ""),
            "status": str(primary_row.get("data_status") or "missing"),
            "link_method": str(primary_row.get("_link_method") or primary_row.get("link_method") or outcome_link.get("method") or "none"),
            "final_return_pct": _round(primary_row.get("final_return_pct")) if str(primary_row.get("data_status") or "") == "success" else None,
            "max_gain_pct": _round(primary_row.get("max_gain_pct")) if str(primary_row.get("data_status") or "") == "success" else None,
            "max_drawdown_pct": _round(primary_row.get("max_drawdown_pct")) if str(primary_row.get("data_status") or "") == "success" else None,
        } if primary_row else None,
        "mature_horizons": mature_horizons,
        "pending_horizons": pending_horizons,
        "unavailable_horizons": unavailable_horizons,
        "outcome_coverage_ratio": _round(coverage.get("link_coverage_ratio")),
        "outcome_maturity_ratio": _round(coverage.get("maturity_ratio")),
        "outcome_maturity_label": str(coverage.get("maturity_label") or "无数据"),
        "outcome_horizons": horizon_statuses,
        "not_advice": NOT_ADVICE,
    }
    fingerprint = source_signature(
        {
            "model": REPLAY_MODEL_VERSION,
            "lifecycle": {
                key: lifecycle.get(key)
                for key in (
                    "id", "symbol", "first_signal_id", "latest_signal_id", "first_signal_at",
                    "latest_signal_at", "updated_at", "closed_at", "first_signal_level", "highest_level",
                    "current_state", "first_price", "latest_price", "lifecycle_score", "risk_score",
                    "intelligence_score",
                )
            },
            "events": [
                {
                    key: item.get(key)
                    for key in (
                        "id", "event_time", "event_type", "event_level", "signal_id", "previous_state",
                        "new_state", "price", "price_change_from_first_pct", "oi_change_pct",
                        "futures_cvd_delta", "spot_cvd_delta", "funding_rate", "event_score", "risk_score",
                        "metrics_json",
                    )
                }
                for item in ordered_events
            ],
            "snapshots": [
                {
                    key: item.get(key)
                    for key in ("id", "snapshot_time", "timeframe", "price", "oi", "futures_cvd_delta", "spot_cvd_delta", "funding_rate")
                }
                for item in ordered_snapshots
            ],
            "outcomes": [
                {
                    key: item.get(key)
                    for key in (
                        "id", "signal_id", "signal_time", "horizon", "horizon_sec", "data_status",
                        "final_return_pct", "max_gain_pct", "max_drawdown_pct", "updated_at",
                    )
                }
                for item in outcome_link.get("items") or []
            ],
            "outcome_coverage": {
                key: coverage.get(key)
                for key in (
                    "primary_outcome_id", "candidate_signal_count", "linked_signal_count",
                    "linked_outcome_count", "horizon_1h_status", "horizon_4h_status",
                    "horizon_24h_status", "horizon_72h_status", "linked_horizon_count",
                    "mature_horizon_count", "link_coverage_ratio", "maturity_ratio",
                    "coverage_label", "maturity_label", "unlinked_reason",
                )
            },
        }
    )
    return {
        **summary,
        "model_version": REPLAY_MODEL_VERSION,
        "summary": summary,
        "frames": frames,
        "outcome_link": {"method": outcome_link.get("method"), "count": outcome_link.get("count")},
        "source_signature": fingerprint,
    }


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (str(name),)
    ).fetchone() is not None


def _rows_by_ids(
    conn: sqlite3.Connection,
    table: str,
    id_column: str,
    ids: list[int],
    *,
    projection: str = "*",
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for offset in range(0, len(ids), 800):
        chunk = ids[offset : offset + 800]
        if not chunk:
            continue
        placeholders = ",".join("?" for _ in chunk)
        rows.extend(dict(row) for row in conn.execute(
            f"SELECT {projection} FROM {table} WHERE {id_column} IN ({placeholders})", chunk
        ).fetchall())
    return rows


def _load_batch_sources(
    settings: Settings,
    *,
    symbol: str,
    lifecycle_id: int | None,
    limit: int,
    lifecycle_ids: Iterable[int] | None = None,
) -> tuple[list[dict[str, Any]], dict[int, list[dict[str, Any]]], dict[str, list[dict[str, Any]]], list[dict[str, Any]], dict[int, str]]:
    db_path = Path(getattr(settings, "lifecycle_db_path", settings.data_dir / "lifecycle.db"))
    if not db_path.exists():
        return [], {}, {}, [], {}
    conn = sqlite3.connect(str(db_path), timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        if not _table_exists(conn, "signal_lifecycles"):
            return [], {}, {}, [], {}
        clauses: list[str] = []
        params: dict[str, Any] = {"limit": max(1, min(safe_int(limit, 500), 500))}
        normalized_symbol = normalize_lifecycle_symbol(symbol)
        if normalized_symbol:
            clauses.append("l.symbol = :symbol")
            params["symbol"] = normalized_symbol
        if lifecycle_id:
            clauses.append("l.id = :lifecycle_id")
            params["lifecycle_id"] = safe_int(lifecycle_id)
        selected_ids = sorted({safe_int(value) for value in (lifecycle_ids or []) if safe_int(value) > 0})
        if selected_ids:
            names = []
            for index, value in enumerate(selected_ids):
                key = f"selected_id_{index}"
                names.append(f":{key}")
                params[key] = value
            clauses.append(f"l.id IN ({','.join(names)})")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        intelligence_join = ""
        intelligence_column = "0.0 AS intelligence_score"
        if _table_exists(conn, "lifecycle_intelligence"):
            intelligence_join = " LEFT JOIN lifecycle_intelligence i ON i.lifecycle_id = l.id"
            intelligence_column = "COALESCE(i.intelligence_score, 0) AS intelligence_score"
        has_replays = _table_exists(conn, "lifecycle_replays")
        replay_priority_join = " LEFT JOIN lifecycle_replays rp ON rp.lifecycle_id = l.id" if has_replays else ""
        replay_priority = (
            "CASE WHEN rp.lifecycle_id IS NULL OR rp.updated_at < l.updated_at THEN 0 ELSE 1 END, "
            if has_replays else ""
        )
        lifecycle_projection = (
            "l.id, l.symbol, l.first_signal_id, l.latest_signal_id, l.first_signal_at, "
            "l.latest_signal_at, l.first_signal_level, l.highest_level, l.first_price, "
            "l.latest_price, l.lifecycle_score, l.risk_score, l.current_state, l.is_active, "
            "l.first_signal_module, l.first_signal_template, l.first_signal_type, "
            "l.created_at, l.updated_at, l.closed_at"
        )
        lifecycles = [
            dict(row)
            for row in conn.execute(
                f"SELECT {lifecycle_projection}, {intelligence_column} "
                f"FROM signal_lifecycles l{intelligence_join}{replay_priority_join} {where} "
                f"ORDER BY l.is_active DESC, {replay_priority}l.updated_at DESC, l.id DESC LIMIT :limit",
                params,
            ).fetchall()
        ]
        ids = [safe_int(item.get("id")) for item in lifecycles if safe_int(item.get("id")) > 0]
        symbols = [normalize_lifecycle_symbol(item.get("symbol")) for item in lifecycles]
        events_by_id: dict[int, list[dict[str, Any]]] = defaultdict(list)
        if ids and _table_exists(conn, "lifecycle_events"):
            event_projection = (
                "id, lifecycle_id, symbol, event_time, event_type, event_level, signal_id, "
                "source_module, source_template, "
                "previous_state, new_state, price, price_change_from_first_pct, oi_change_pct, "
                "futures_cvd_delta, spot_cvd_delta, funding_rate, event_score, risk_score, metrics_json"
            )
            for row in _rows_by_ids(
                conn,
                "lifecycle_events",
                "lifecycle_id",
                ids,
                projection=event_projection,
            ):
                events_by_id[safe_int(row.get("lifecycle_id"))].append(row)
        snapshots_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
        if symbols and _table_exists(conn, "lifecycle_metric_snapshots"):
            unique_symbols = sorted(set(filter(None, symbols)))
            for offset in range(0, len(unique_symbols), 800):
                chunk = unique_symbols[offset : offset + 800]
                placeholders = ",".join("?" for _ in chunk)
                for row in conn.execute(
                    "SELECT id, symbol, timeframe, snapshot_time, price, oi, futures_cvd_delta, "
                    f"spot_cvd_delta, funding_rate FROM lifecycle_metric_snapshots WHERE symbol IN ({placeholders})",
                    chunk,
                ).fetchall():
                    item = dict(row)
                    snapshots_by_symbol[str(item.get("symbol") or "")].append(item)
        existing_signatures: dict[int, str] = {}
        if ids and _table_exists(conn, "lifecycle_replays"):
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(lifecycle_replays)").fetchall()}
            if "source_signature" in columns:
                for row in _rows_by_ids(
                    conn,
                    "lifecycle_replays",
                    "lifecycle_id",
                    ids,
                    projection="lifecycle_id, source_signature",
                ):
                    existing_signatures[safe_int(row.get("lifecycle_id"))] = str(row.get("source_signature") or "")
        persisted_links: list[dict[str, Any]] = []
        if ids and _table_exists(conn, "lifecycle_outcome_links"):
            persisted_links = _rows_by_ids(
                conn,
                "lifecycle_outcome_links",
                "lifecycle_id",
                ids,
                projection=(
                    "lifecycle_id, outcome_id, signal_id, lifecycle_event_id, horizon, outcome_status, "
                    "link_role, link_method, link_confidence, is_primary, signal_time, outcome_time"
                ),
            )
        if ids and _table_exists(conn, "lifecycle_outcome_coverage"):
            coverage_by_id = {
                safe_int(row.get("lifecycle_id")): row
                for row in _rows_by_ids(
                    conn,
                    "lifecycle_outcome_coverage",
                    "lifecycle_id",
                    ids,
                    projection=(
                        "lifecycle_id, candidate_signal_count, linked_signal_count, linked_outcome_count, "
                        "primary_outcome_id, horizon_1h_status, horizon_4h_status, horizon_24h_status, "
                        "horizon_72h_status, linked_horizon_count, mature_horizon_count, "
                        "link_coverage_ratio, maturity_ratio, coverage_label, maturity_label, "
                        "unlinked_reason, calculated_at, updated_at"
                    ),
                )
            }
            for lifecycle in lifecycles:
                current_id = safe_int(lifecycle.get("id"))
                lifecycle["outcome_coverage"] = coverage_by_id.get(current_id, {})
                lifecycle["outcome_coverage_present"] = current_id in coverage_by_id
    finally:
        conn.close()

    outcomes: list[dict[str, Any]] = []
    outcome_db = Path(getattr(settings, "outcome_db_path", settings.data_dir / "outcomes.db"))
    if symbols and outcome_db.exists():
        outcome_conn = sqlite3.connect(str(outcome_db), timeout=15)
        outcome_conn.row_factory = sqlite3.Row
        try:
            if _table_exists(outcome_conn, "signal_outcomes") and persisted_links:
                links_by_outcome = {safe_int(item.get("outcome_id")): item for item in persisted_links}
                outcome_ids = sorted(value for value in links_by_outcome if value > 0)
                projection = (
                    "id, signal_id, symbol, signal_time, horizon, horizon_sec, due_time, module, signal_type, "
                    "data_status, result_label, final_return_pct, max_gain_pct, max_drawdown_pct, updated_at"
                )
                for offset in range(0, len(outcome_ids), 800):
                    chunk = outcome_ids[offset : offset + 800]
                    placeholders = ",".join("?" for _ in chunk)
                    for row in outcome_conn.execute(
                        f"SELECT {projection} FROM signal_outcomes WHERE id IN ({placeholders})",
                        chunk,
                    ).fetchall():
                        item = dict(row)
                        link = links_by_outcome.get(safe_int(item.get("id"))) or {}
                        item.update({
                            "_lifecycle_id": safe_int(link.get("lifecycle_id")),
                            "_link_method": str(link.get("link_method") or ""),
                            "_link_role": str(link.get("link_role") or ""),
                            "_is_primary": safe_int(link.get("is_primary")),
                            "_lifecycle_event_id": safe_int(link.get("lifecycle_event_id")) or None,
                        })
                        outcomes.append(item)
            elif _table_exists(outcome_conn, "signal_outcomes"):
                unique_symbols = sorted(set(filter(None, symbols)))
                # One bounded batch read per <=500 lifecycle batch. Outcome
                # signal_time is the original signal time (not the horizon due
                # time), so the aggregate lifecycle window retains every exact
                # signal-id match while avoiding unbounded symbol history reads.
                placeholders = ",".join("?" for _ in unique_symbols)
                projection = (
                    "id, signal_id, symbol, signal_time, horizon, horizon_sec, due_time, module, signal_type, "
                    "data_status, result_label, final_return_pct, max_gain_pct, max_drawdown_pct, updated_at"
                )
                window_starts = [
                    _iso(item.get("first_signal_at") or item.get("created_at"))
                    for item in lifecycles
                    if _parse_time(item.get("first_signal_at") or item.get("created_at")) is not None
                ]
                window_ends = [
                    _iso(value)
                    for item in lifecycles
                    for value in (item.get("closed_at"), item.get("latest_signal_at"), item.get("updated_at"))
                    if _parse_time(value) is not None
                ]
                window_ends.extend(
                    _iso(event.get("event_time"))
                    for batch in events_by_id.values()
                    for event in batch
                    if _parse_time(event.get("event_time")) is not None
                )
                params: list[Any] = list(unique_symbols)
                window_clause = ""
                if window_starts and window_ends:
                    window_clause = " AND signal_time >= ? AND signal_time <= ?"
                    params.extend([min(window_starts), max(window_ends)])
                outcomes = [
                    dict(row)
                    for row in outcome_conn.execute(
                        f"SELECT {projection} FROM signal_outcomes "
                        f"WHERE symbol IN ({placeholders}){window_clause}",
                        params,
                    ).fetchall()
                ]
        finally:
            outcome_conn.close()
    return lifecycles, dict(events_by_id), dict(snapshots_by_symbol), outcomes, existing_signatures


def rebuild_replays(
    settings: Settings,
    symbol: str = "",
    lifecycle_id: int | None = None,
    limit: int = 500,
    dry_run: bool = False,
    force: bool = False,
    force_rebuild: bool | None = None,
    lifecycle_ids: Iterable[int] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    if force_rebuild is not None:
        force = bool(force_rebuild)
    if symbol:
        requested_symbol = str(symbol)
        symbol = normalize_lifecycle_symbol(requested_symbol)
        if not symbol:
            return {
                "ok": False,
                "model_version": REPLAY_MODEL_VERSION,
                "dry_run": bool(dry_run),
                "processed": 0,
                "skipped": 0,
                "failed": 1,
                "duration_sec": round(time.perf_counter() - started, 4),
                "counts": {"selected": 0, "processed": 0, "skipped": 0, "failed": 1},
                "items": [],
                "failures": [{"lifecycle_id": 0, "symbol": requested_symbol, "error": "invalid lifecycle symbol"}],
            }
    store = IntelligenceStore(settings)
    if not dry_run:
        store.ensure_schema()
    lifecycles, events_by_id, snapshots_by_symbol, outcomes, existing = _load_batch_sources(
        settings,
        symbol=symbol,
        lifecycle_id=lifecycle_id,
        lifecycle_ids=lifecycle_ids,
        limit=limit,
    )
    outcomes_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in outcomes:
        outcomes_by_symbol[normalize_lifecycle_symbol(item.get("symbol"))].append(item)

    built: list[dict[str, Any]] = []
    failed_items: list[dict[str, Any]] = []
    skipped = 0
    for lifecycle in lifecycles:
        current_id = safe_int(lifecycle.get("id"))
        current_symbol = normalize_lifecycle_symbol(lifecycle.get("symbol"))
        try:
            replay = build_replay(
                lifecycle,
                events_by_id.get(current_id, []),
                snapshots_by_symbol.get(current_symbol, []),
                outcomes_by_symbol.get(current_symbol, []),
            )
            if not force and existing.get(current_id) and existing[current_id] == replay["source_signature"]:
                skipped += 1
                continue
            built.append(replay)
        except Exception as exc:
            failed_items.append(
                {
                    "lifecycle_id": current_id,
                    "symbol": current_symbol,
                    "error": f"{type(exc).__name__}: {str(exc)[:240]}",
                }
            )

    write_failed = False
    if built and not dry_run:
        try:
            with store.transaction() as conn:
                for replay in built:
                    summary = dict(replay["summary"])
                    record = {
                        **summary,
                        "replay_version": REPLAY_MODEL_VERSION,
                        "frame_count": len(replay["frames"]),
                        "source_signature": replay["source_signature"],
                        "summary": summary,
                    }
                    store.upsert_replay(record, replay["frames"], conn=conn, fetch=False)
                store.invalidate_analytics_cache("lifecycle:", conn=conn)
        except Exception as exc:
            write_failed = True
            failed_items.append({"lifecycle_id": 0, "symbol": "", "error": f"{type(exc).__name__}: {str(exc)[:240]}"})

    processed = 0 if write_failed else len(built)
    duration = round(time.perf_counter() - started, 4)
    result = {
        "ok": not write_failed,
        "model_version": REPLAY_MODEL_VERSION,
        "dry_run": bool(dry_run),
        "processed": processed,
        "skipped": skipped,
        "failed": len(failed_items),
        "duration_sec": duration,
        "counts": {
            "selected": len(lifecycles),
            "processed": processed,
            "skipped": skipped,
            "failed": len(failed_items),
        },
        "items": [
            {
                "lifecycle_id": item["lifecycle_id"],
                "symbol": item["symbol"],
                "frame_count": len(item["frames"]),
                "upgrade_path": item["upgrade_path"],
                "result_label": item["result_label"],
            }
            for item in built[:50]
        ],
        "failures": failed_items[:20],
    }
    return result


def get_replay_payload(
    settings: Settings,
    symbol: str = "",
    lifecycle_id: int | None = None,
    frame_limit: int = 100,
    frame_offset: int = 0,
) -> dict[str, Any]:
    store = IntelligenceStore(settings)
    replay = store.get_replay(lifecycle_id, symbol)
    if not replay:
        return {
            "ok": True,
            "data": {
                "available": False,
                "replay": {},
                "frames": [],
                "pagination": {"limit": max(1, min(safe_int(frame_limit, 100), 500)), "offset": max(0, safe_int(frame_offset)), "total": 0},
                "model_version": REPLAY_MODEL_VERSION,
                "not_advice": NOT_ADVICE,
            },
        }
    replay_id = safe_int(replay.get("lifecycle_id"))
    frames = store.list_replay_frames(
        replay_id,
        limit=frame_limit,
        offset=frame_offset,
        include_metrics=False,
    )
    total = safe_int(replay.get("frame_count"))
    public_replay = {key: value for key, value in replay.items() if key not in {"source_signature"}}
    return {
        "ok": True,
        "data": {
            "available": True,
            "replay": public_replay,
            "frames": frames,
            "pagination": {
                "limit": max(1, min(safe_int(frame_limit, 100), 500)),
                "offset": max(0, safe_int(frame_offset)),
                "total": total,
            },
            "model_version": REPLAY_MODEL_VERSION,
            "not_advice": NOT_ADVICE,
        },
    }
