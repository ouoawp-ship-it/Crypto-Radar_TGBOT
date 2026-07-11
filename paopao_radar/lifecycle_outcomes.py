from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import BASE_DIR, Settings
from .lifecycle_intelligence_store import IntelligenceStore
from .lifecycle_store import normalize_lifecycle_symbol, safe_float, safe_int
from .outcome_tracker import OUTCOME_WINDOWS


HORIZONS = ("1h", "4h", "24h", "72h")
VALID_OUTCOME_STATUSES = {"not_due", "pending", "ready", "success", "unavailable", "error", "missing"}
LINK_ROLE_PRIORITY = {
    "first_signal": 0,
    "timeframe_upgrade": 1,
    "risk_event": 2,
    "weakening_event": 3,
    "same_level_confirm": 4,
    "latest_signal": 5,
    "fallback": 6,
}
RISK_EVENTS = {"risk_warning", "oi_price_divergence", "cvd_divergence", "funding_crowded"}
WEAKENING_EVENTS = {
    "short_term_weakening", "major_timeframe_weakening", "launch_failed", "lifecycle_closed",
}


def _scope_error(symbol: str, lifecycle_id: int | None) -> str:
    if str(symbol or "").strip() and not normalize_lifecycle_symbol(symbol):
        return "invalid_symbol"
    if lifecycle_id is not None and safe_int(lifecycle_id) <= 0:
        return "invalid_lifecycle_id"
    return ""


def _utc_now(now: datetime | int | float | None = None) -> datetime:
    if isinstance(now, datetime):
        value = now
    elif isinstance(now, (int, float)):
        value = datetime.fromtimestamp(float(now), timezone.utc)
    else:
        value = datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: Any) -> str:
    parsed = _parse_time(value)
    return parsed.isoformat() if parsed is not None else ""


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


def _readonly_connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (str(name),)
    ).fetchone() is not None


def _outcome_store_available(settings: Settings) -> bool:
    """Return True only when the authoritative Outcome table is readable."""
    path = Path(settings.outcome_db_path)
    if not path.exists():
        return False
    try:
        conn = _readonly_connection(path)
        try:
            return _table_exists(conn, "signal_outcomes")
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def _normalized_text(value: Any) -> str:
    return str(value or "").strip().casefold()


def _event_role(event_type: Any) -> str:
    value = str(event_type or "")
    if value == "first_signal":
        return "first_signal"
    if value == "same_level_confirm":
        return "same_level_confirm"
    if value.startswith("timeframe_upgrade_"):
        return "timeframe_upgrade"
    if value in RISK_EVENTS:
        return "risk_event"
    if value in WEAKENING_EVENTS:
        return "weakening_event"
    return "fallback"


def _candidate_key(item: dict[str, Any]) -> str:
    signal_id = safe_int(item.get("signal_id"))
    if signal_id > 0:
        return f"signal:{signal_id}"
    return "legacy:{time}:{module}:{template}".format(
        time=_iso(item.get("signal_time")),
        module=_normalized_text(item.get("module")),
        template=_normalized_text(item.get("template")),
    )


def _merge_candidate(current: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    result = dict(current)
    current_role = str(result.get("link_role") or "fallback")
    incoming_role = str(incoming.get("link_role") or "fallback")
    if LINK_ROLE_PRIORITY.get(incoming_role, 99) < LINK_ROLE_PRIORITY.get(current_role, 99):
        for key in ("link_role", "link_method", "lifecycle_event_id", "priority"):
            result[key] = incoming.get(key)
    for key in ("signal_time", "module", "template", "signal_type", "lifecycle_event_id"):
        if not result.get(key) and incoming.get(key):
            result[key] = incoming.get(key)
    return result


def extract_lifecycle_signal_candidates(
    lifecycle: dict[str, Any],
    events: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return stable, de-duplicated first -> events -> latest signal candidates."""

    lifecycle_id = safe_int(lifecycle.get("id") or lifecycle.get("lifecycle_id"))
    symbol = normalize_lifecycle_symbol(lifecycle.get("symbol"))
    if lifecycle_id <= 0 or not symbol:
        return []
    ordered_events = sorted(
        (dict(item) for item in events),
        key=lambda item: (_parse_time(item.get("event_time")) or datetime.min.replace(tzinfo=timezone.utc), safe_int(item.get("id"))),
    )
    first_event = next((item for item in ordered_events if str(item.get("event_type")) == "first_signal"), None)
    stored_first_id = safe_int(lifecycle.get("first_signal_id"))
    event_first_id = safe_int((first_event or {}).get("signal_id"))
    effective_first_id = stored_first_id or event_first_id
    raw: list[dict[str, Any]] = [{
        "lifecycle_id": lifecycle_id,
        "symbol": symbol,
        "signal_id": effective_first_id or None,
        "lifecycle_event_id": safe_int((first_event or {}).get("id")) or None,
        "signal_time": lifecycle.get("first_signal_at"),
        "module": lifecycle.get("first_signal_module") or (first_event or {}).get("source_module"),
        "template": lifecycle.get("first_signal_template") or (first_event or {}).get("source_template"),
        "signal_type": lifecycle.get("first_signal_type"),
        "link_role": "first_signal",
        "link_method": "first_signal_id" if stored_first_id > 0 else "event_signal_id" if event_first_id > 0 else "symbol_time_module",
        "priority": 0,
    }]
    for index, event in enumerate(ordered_events, 1):
        signal_id = safe_int(event.get("signal_id"))
        source_module = str(event.get("source_module") or "")
        source_template = str(event.get("source_template") or "")
        if signal_id <= 0 and source_module == "lifecycle_metrics" and source_template == "LIFECYCLE_METRIC_REFRESH":
            continue
        raw.append({
            "lifecycle_id": lifecycle_id,
            "symbol": symbol,
            "signal_id": signal_id or None,
            "lifecycle_event_id": safe_int(event.get("id")) or None,
            "signal_time": event.get("event_time"),
            "module": source_module,
            "template": source_template,
            "signal_type": event.get("signal_type"),
            "link_role": _event_role(event.get("event_type")),
            "link_method": "event_signal_id" if signal_id > 0 else "symbol_time_module",
            "priority": index,
        })
    raw.append({
        "lifecycle_id": lifecycle_id,
        "symbol": symbol,
        "signal_id": safe_int(lifecycle.get("latest_signal_id")) or None,
        "lifecycle_event_id": None,
        "signal_time": lifecycle.get("latest_signal_at"),
        "module": "",
        "template": "",
        "signal_type": "",
        "link_role": "latest_signal",
        "link_method": "latest_signal_id" if safe_int(lifecycle.get("latest_signal_id")) > 0 else "symbol_time_module",
        "priority": len(ordered_events) + 1,
    })
    deduplicated: dict[str, dict[str, Any]] = {}
    for item in raw:
        if not _parse_time(item.get("signal_time")) and safe_int(item.get("signal_id")) <= 0:
            continue
        key = _candidate_key(item)
        if key.startswith("legacy:") and key.endswith("::"):
            continue
        deduplicated[key] = _merge_candidate(deduplicated[key], item) if key in deduplicated else item
    return sorted(
        deduplicated.values(),
        key=lambda item: (safe_int(item.get("priority"), 999999), _parse_time(item.get("signal_time")) or datetime.max.replace(tzinfo=timezone.utc)),
    )


def _read_lifecycle_sources(
    settings: Settings,
    *,
    symbol: str = "",
    lifecycle_id: int | None = None,
    lifecycle_ids: Iterable[int] | None = None,
    limit: int = 200,
    offset: int = 0,
    rotate: bool = True,
) -> tuple[list[dict[str, Any]], dict[int, list[dict[str, Any]]]]:
    path = Path(settings.lifecycle_db_path)
    if not path.exists():
        return [], {}
    conn = _readonly_connection(path)
    try:
        if not _table_exists(conn, "signal_lifecycles"):
            return [], {}
        clauses: list[str] = []
        params: list[Any] = []
        normalized_symbol = normalize_lifecycle_symbol(symbol)
        if symbol and not normalized_symbol:
            return [], {}
        if normalized_symbol:
            clauses.append("l.symbol = ?")
            params.append(normalized_symbol)
        if lifecycle_id:
            clauses.append("l.id = ?")
            params.append(safe_int(lifecycle_id))
        selected_ids = sorted({safe_int(value) for value in (lifecycle_ids or []) if safe_int(value) > 0})
        if selected_ids:
            clauses.append(f"l.id IN ({','.join('?' for _ in selected_ids)})")
            params.extend(selected_ids)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([
            max(1, min(safe_int(limit, 200), 1000)),
            max(0, safe_int(offset)),
        ])
        has_coverage = _table_exists(conn, "lifecycle_outcome_coverage")
        coverage_join = " LEFT JOIN lifecycle_outcome_coverage c ON c.lifecycle_id=l.id" if has_coverage else ""
        rotation_order = (
            "CASE WHEN c.lifecycle_id IS NULL THEN 0 ELSE 1 END, "
            "COALESCE(c.calculated_at, '') ASC, l.updated_at DESC, l.id DESC"
            if rotate and has_coverage and not normalized_symbol and not lifecycle_id
            else "l.updated_at DESC, l.id DESC"
        )
        lifecycles = [
            dict(row)
            for row in conn.execute(
                "SELECT l.id, l.symbol, l.first_signal_id, l.first_signal_at, l.first_signal_module, "
                "l.first_signal_template, l.first_signal_type, l.first_signal_level, l.latest_signal_id, "
                "l.latest_signal_at, l.highest_level, l.current_state, l.is_active, l.created_at, l.updated_at "
                f"FROM signal_lifecycles l{coverage_join} {where} ORDER BY {rotation_order} LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        ]
        ids = [safe_int(item.get("id")) for item in lifecycles]
        events: dict[int, list[dict[str, Any]]] = defaultdict(list)
        if ids and _table_exists(conn, "lifecycle_events"):
            for offset in range(0, len(ids), 800):
                chunk = ids[offset : offset + 800]
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    "SELECT id, lifecycle_id, symbol, event_time, event_type, event_level, signal_id, "
                    "source_module, source_template, previous_state, new_state "
                    f"FROM lifecycle_events WHERE lifecycle_id IN ({placeholders}) "
                    "ORDER BY lifecycle_id, event_time, id",
                    chunk,
                ).fetchall()
                for row in rows:
                    item = dict(row)
                    events[safe_int(item.get("lifecycle_id"))].append(item)
        return lifecycles, dict(events)
    finally:
        conn.close()


def _read_signal_rows(path: Path, signal_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    ids = sorted({safe_int(value) for value in signal_ids if safe_int(value) > 0})
    if not ids or not path.exists():
        return {}
    conn = _readonly_connection(path)
    try:
        if not _table_exists(conn, "signals"):
            return {}
        result: dict[int, dict[str, Any]] = {}
        for offset in range(0, len(ids), 800):
            chunk = ids[offset : offset + 800]
            placeholders = ",".join("?" for _ in chunk)
            for row in conn.execute(
                "SELECT id, ts, time, module, template_id, signal_type, symbol, status, score, stage "
                f"FROM signals WHERE id IN ({placeholders})",
                chunk,
            ).fetchall():
                item = dict(row)
                result[safe_int(item.get("id"))] = item
        return result
    finally:
        conn.close()


def _outcome_projection() -> str:
    return (
        "id, signal_id, symbol, signal_time, horizon, horizon_sec, due_time, data_status, "
        "module, signal_type, final_return_pct, max_gain_pct, max_drawdown_pct, result_label, "
        "data_source, updated_at"
    )


def _read_outcomes(
    settings: Settings,
    candidates: list[dict[str, Any]],
    *,
    horizon: str = "",
    tolerance_sec: int = 300,
) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    path = Path(settings.outcome_db_path)
    if not path.exists() or not candidates:
        return [], {}
    exact_ids = sorted({safe_int(item.get("signal_id")) for item in candidates if safe_int(item.get("signal_id")) > 0})
    legacy = [item for item in candidates if safe_int(item.get("signal_id")) <= 0]
    conn = _readonly_connection(path)
    found: dict[int, dict[str, Any]] = {}
    try:
        if not _table_exists(conn, "signal_outcomes"):
            return [], {}
        horizon_clause = " AND horizon = ?" if horizon else ""
        for offset in range(0, len(exact_ids), 800):
            chunk = exact_ids[offset : offset + 800]
            placeholders = ",".join("?" for _ in chunk)
            params: list[Any] = list(chunk)
            if horizon:
                params.append(str(horizon).lower())
            rows = conn.execute(
                f"SELECT {_outcome_projection()} FROM signal_outcomes "
                f"WHERE signal_id IN ({placeholders}){horizon_clause}",
                params,
            ).fetchall()
            for row in rows:
                item = dict(row)
                found[safe_int(item.get("id"))] = item
        times = [_parse_time(item.get("signal_time")) for item in legacy]
        valid_times = [item for item in times if item is not None]
        symbols = sorted({str(item.get("symbol") or "") for item in legacy if item.get("symbol")})
        if valid_times and symbols:
            placeholders = ",".join("?" for _ in symbols)
            start = min(valid_times).timestamp() - max(0, safe_int(tolerance_sec, 300))
            end = max(valid_times).timestamp() + max(0, safe_int(tolerance_sec, 300))
            params = [*symbols, datetime.fromtimestamp(start, timezone.utc).isoformat(), datetime.fromtimestamp(end, timezone.utc).isoformat()]
            if horizon:
                params.append(str(horizon).lower())
            rows = conn.execute(
                f"SELECT {_outcome_projection()} FROM signal_outcomes WHERE symbol IN ({placeholders}) "
                f"AND signal_time >= ? AND signal_time <= ?{horizon_clause}",
                params,
            ).fetchall()
            for row in rows:
                item = dict(row)
                found[safe_int(item.get("id"))] = item
    finally:
        conn.close()
    outcome_signal_rows = _read_signal_rows(
        Path(settings.signal_events_db_path),
        (safe_int(item.get("signal_id")) for item in found.values()),
    )
    return list(found.values()), outcome_signal_rows


def _effective_status(row: dict[str, Any], now: datetime) -> str:
    status = str(row.get("data_status") or "missing").strip().lower()
    if status not in VALID_OUTCOME_STATUSES:
        status = "error"
    due = _parse_time(row.get("due_time"))
    if status in {"pending", "ready"} and due is not None and due > now:
        return "not_due"
    return status


def _missing_horizon_status(signal_time: Any, horizon: str, now: datetime) -> str:
    started = _parse_time(signal_time)
    if started is None:
        return "missing"
    return "not_due" if started.timestamp() + OUTCOME_WINDOWS[horizon] > now.timestamp() else "missing"


def _outcome_link_row(candidate: dict[str, Any], outcome: dict[str, Any], now: datetime, confidence: float) -> dict[str, Any]:
    return {
        "lifecycle_id": safe_int(candidate.get("lifecycle_id")),
        "symbol": normalize_lifecycle_symbol(candidate.get("symbol")),
        "signal_id": safe_int(outcome.get("signal_id")) or safe_int(candidate.get("signal_id")) or None,
        "lifecycle_event_id": safe_int(candidate.get("lifecycle_event_id")) or None,
        "outcome_id": safe_int(outcome.get("id")),
        "horizon": str(outcome.get("horizon") or ""),
        "outcome_status": _effective_status(outcome, now),
        "link_role": str(candidate.get("link_role") or "fallback"),
        "link_method": str(candidate.get("link_method") or "symbol_time_module"),
        "link_confidence": confidence,
        "signal_time": str(outcome.get("signal_time") or candidate.get("signal_time") or ""),
        "outcome_time": str(outcome.get("due_time") or outcome.get("updated_at") or ""),
    }


def _strict_fallback_match(
    candidate: dict[str, Any],
    outcomes: list[dict[str, Any]],
    outcome_signals: dict[int, dict[str, Any]],
    *,
    tolerance_sec: int,
) -> tuple[list[dict[str, Any]], str]:
    signal_time = _parse_time(candidate.get("signal_time"))
    symbol = normalize_lifecycle_symbol(candidate.get("symbol"))
    if not symbol:
        return [], "invalid_symbol"
    if signal_time is None:
        return [], "invalid_signal_time"
    module = _normalized_text(candidate.get("module"))
    template = _normalized_text(candidate.get("template"))
    by_signal: dict[int, list[dict[str, Any]]] = defaultdict(list)
    observed_symbol = False
    observed_time = False
    for row in outcomes:
        if normalize_lifecycle_symbol(row.get("symbol")) != symbol:
            continue
        observed_symbol = True
        outcome_time = _parse_time(row.get("signal_time"))
        if outcome_time is None or abs((outcome_time - signal_time).total_seconds()) > tolerance_sec:
            continue
        observed_time = True
        source = outcome_signals.get(safe_int(row.get("signal_id"))) or {}
        row_module = _normalized_text(source.get("module") or row.get("module"))
        row_template = _normalized_text(source.get("template_id"))
        module_match = bool(module and row_module and module == row_module)
        template_match = bool(template and row_template and template == row_template)
        if not (module_match or template_match):
            continue
        by_signal[safe_int(row.get("signal_id"))].append(row)
    if not by_signal:
        return [], "module_mismatch" if observed_time else "time_mismatch" if observed_symbol else "no_outcome_row"
    distances: dict[int, float] = {}
    for signal_id, rows in by_signal.items():
        row_time = _parse_time(rows[0].get("signal_time"))
        distances[signal_id] = abs((row_time - signal_time).total_seconds()) if row_time else float("inf")
    smallest = min(distances.values())
    closest = [signal_id for signal_id, distance in distances.items() if distance == smallest]
    if len(closest) != 1:
        return [], "ambiguous_match"
    return by_signal[closest[0]], ""


def _primary_outcome_id(links: list[dict[str, Any]]) -> int | None:
    if not links:
        return None
    # Primary is a stable identity anchor.  Result statistics independently use
    # the longest mature success horizon, so changing status must not move it.
    selected = min(
        links,
        key=lambda item: (
            OUTCOME_WINDOWS.get(str(item.get("horizon") or ""), 10**9),
            safe_int(item.get("outcome_id")),
        ),
    )
    return safe_int(selected.get("outcome_id")) or None


def _maturity_label(statuses: dict[str, str], candidate_count: int) -> str:
    if candidate_count <= 0:
        return "无数据"
    if statuses and all(value == "not_due" for value in statuses.values()):
        return "等待到期"
    successes = {key for key, value in statuses.items() if value == "success"}
    if "72h" in successes:
        return "完整成熟"
    if "24h" in successes:
        return "基本成熟"
    if "4h" in successes:
        return "部分成熟"
    if "1h" in successes:
        return "初步成熟"
    due_values = [value for value in statuses.values() if value != "not_due"]
    if due_values and all(value == "unavailable" for value in due_values):
        return "数据不可用"
    if "error" in due_values and not successes:
        return "计算异常"
    return "等待计算"


def _coverage_plan(
    lifecycle: dict[str, Any],
    candidates: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    outcome_signals: dict[int, dict[str, Any]],
    source_signals: dict[int, dict[str, Any]],
    *,
    now: datetime,
    tolerance_sec: int,
) -> dict[str, Any]:
    symbol = normalize_lifecycle_symbol(lifecycle.get("symbol"))
    exact_by_signal: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for outcome in outcomes:
        exact_by_signal[safe_int(outcome.get("signal_id"))].append(outcome)
    candidate_links: dict[str, list[dict[str, Any]]] = defaultdict(list)
    candidate_reasons: dict[str, str] = {}
    all_links: list[dict[str, Any]] = []
    claimed_outcomes: set[int] = set()
    for candidate in candidates:
        key = _candidate_key(candidate)
        signal_id = safe_int(candidate.get("signal_id"))
        rows: list[dict[str, Any]] = []
        reason = ""
        confidence = 1.0
        if signal_id > 0:
            signal_rows = exact_by_signal.get(signal_id, [])
            rows = [row for row in signal_rows if normalize_lifecycle_symbol(row.get("symbol")) == symbol]
            if not rows:
                if signal_rows:
                    reason = "symbol_mismatch"
                elif signal_id not in source_signals:
                    reason = "signal_not_in_store"
                else:
                    reason = "no_outcome_row"
        else:
            rows, reason = _strict_fallback_match(
                candidate,
                outcomes,
                outcome_signals,
                tolerance_sec=tolerance_sec,
            )
            confidence = 0.8
        if rows and any(safe_int(row.get("id")) in claimed_outcomes for row in rows):
            rows = []
            reason = "ambiguous_match"
        for row in rows:
            link = _outcome_link_row(candidate, row, now, confidence)
            candidate_links[key].append(link)
            all_links.append(link)
            claimed_outcomes.add(safe_int(row.get("id")))
        if reason:
            candidate_reasons[key] = reason

    # A missing row is expected while every horizon for that signal is still
    # in the future.  Preserve that distinction in the reason distribution so
    # public diagnostics never misclassify normal maturation as missing data.
    for candidate in candidates:
        key = _candidate_key(candidate)
        if candidate_reasons.get(key) == "no_outcome_row" and all(
            _missing_horizon_status(candidate.get("signal_time"), horizon, now) == "not_due"
            for horizon in HORIZONS
        ):
            candidate_reasons[key] = "not_due"

    primary_candidate: dict[str, Any] | None = None
    primary_links: list[dict[str, Any]] = []
    for candidate in candidates:
        rows = candidate_links.get(_candidate_key(candidate), [])
        if rows and any(str(row.get("outcome_status") or "") in {"success", "pending", "ready", "not_due"} for row in rows):
            primary_candidate = candidate
            primary_links = rows
            break
    if primary_candidate is None:
        for candidate in candidates:
            rows = candidate_links.get(_candidate_key(candidate), [])
            if rows:
                primary_candidate = candidate
                primary_links = rows
                break
    baseline = primary_candidate or (candidates[0] if candidates else None)
    by_horizon = {str(item.get("horizon")): item for item in primary_links}
    horizon_statuses: dict[str, str] = {}
    for horizon in HORIZONS:
        row = by_horizon.get(horizon)
        horizon_statuses[horizon] = str(row.get("outcome_status")) if row else _missing_horizon_status(
            (baseline or {}).get("signal_time"), horizon, now
        )
    linked_signals = sum(bool(candidate_links.get(_candidate_key(item))) for item in candidates)
    candidate_count = len(candidates)
    linked_horizons = len(by_horizon)
    mature_horizons = sum(value == "success" for value in horizon_statuses.values())
    due_horizons = sum(value != "not_due" for value in horizon_statuses.values())
    reason_counts = Counter(candidate_reasons.values())
    primary_id = _primary_outcome_id(primary_links)
    if candidate_count <= 0:
        unlinked_reason = "no_signal_id"
    elif all_links:
        effective = [item["outcome_status"] for item in primary_links]
        if effective and all(value == "not_due" for value in effective):
            unlinked_reason = "not_due"
        elif effective and all(value == "unavailable" for value in effective):
            unlinked_reason = "outcome_unavailable"
        elif "error" in effective and "success" not in effective:
            unlinked_reason = "real_error"
        elif any(value in {"pending", "ready"} for value in effective):
            unlinked_reason = "pending_scan"
        else:
            unlinked_reason = ""
    elif horizon_statuses and all(value == "not_due" for value in horizon_statuses.values()):
        unlinked_reason = "not_due"
    elif reason_counts:
        unlinked_reason = reason_counts.most_common(1)[0][0]
    else:
        unlinked_reason = "no_outcome_row"
    ratio = linked_signals / candidate_count if candidate_count else 0.0
    coverage_label = "完整关联" if candidate_count and linked_signals == candidate_count else "部分关联" if linked_signals else "未关联" if candidate_count else "无数据"
    coverage = {
        "lifecycle_id": safe_int(lifecycle.get("id")),
        "symbol": symbol,
        "candidate_signal_count": candidate_count,
        "linked_signal_count": linked_signals,
        "linked_outcome_count": len(all_links),
        "primary_outcome_id": primary_id,
        **{f"horizon_{horizon}_status": horizon_statuses[horizon] for horizon in HORIZONS},
        "linked_horizon_count": linked_horizons,
        "mature_horizon_count": mature_horizons,
        "link_coverage_ratio": round(ratio, 6),
        "maturity_ratio": round(mature_horizons / due_horizons, 6) if due_horizons else 0.0,
        "coverage_label": coverage_label,
        "maturity_label": _maturity_label(horizon_statuses, candidate_count),
        "unlinked_reason": unlinked_reason,
        "reasons": {
            "reason_counts": dict(sorted(reason_counts.items())),
            "horizon_statuses": horizon_statuses,
            "mature_horizons": [key for key, value in horizon_statuses.items() if value == "success"],
            "pending_horizons": [key for key, value in horizon_statuses.items() if value in {"not_due", "pending", "ready", "missing"}],
            "unavailable_horizons": [key for key, value in horizon_statuses.items() if value == "unavailable"],
            "primary_outcome_signal_id": safe_int((primary_candidate or {}).get("signal_id")) or None,
            "primary_outcome_status": "linked" if primary_id else "no_primary_outcome",
            "primary_link_method": str((primary_candidate or {}).get("link_method") or "none"),
        },
    }
    return {"links": all_links, "coverage": coverage, "candidates": candidates}


def _prepare_plans(
    settings: Settings,
    *,
    symbol: str = "",
    lifecycle_id: int | None = None,
    lifecycle_ids: Iterable[int] | None = None,
    limit: int = 200,
    horizon: str = "",
    now: datetime | int | float | None = None,
    offset: int = 0,
    rotate: bool = True,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[int, dict[str, Any]]]:
    lifecycles, events = _read_lifecycle_sources(
        settings,
        symbol=symbol,
        lifecycle_id=lifecycle_id,
        lifecycle_ids=lifecycle_ids,
        limit=limit,
        offset=offset,
        rotate=rotate,
    )
    candidates_by_id = {
        safe_int(item.get("id")): extract_lifecycle_signal_candidates(item, events.get(safe_int(item.get("id")), []))
        for item in lifecycles
    }
    all_candidates = [item for rows in candidates_by_id.values() for item in rows]
    source_signals = _read_signal_rows(
        Path(settings.signal_events_db_path),
        (safe_int(item.get("signal_id")) for item in all_candidates),
    )
    for candidate in all_candidates:
        source = source_signals.get(safe_int(candidate.get("signal_id"))) or {}
        candidate["signal_time"] = candidate.get("signal_time") or source.get("time")
        candidate["module"] = candidate.get("module") or source.get("module")
        candidate["template"] = candidate.get("template") or source.get("template_id")
        candidate["signal_type"] = candidate.get("signal_type") or source.get("signal_type")
    tolerance = max(0, safe_int(getattr(settings, "lifecycle_outcome_link_time_tolerance_sec", 300), 300))
    outcomes, outcome_signals = _read_outcomes(
        settings,
        all_candidates,
        horizon=horizon,
        tolerance_sec=tolerance,
    )
    current_time = _utc_now(now)
    plans = [
        _coverage_plan(
            lifecycle,
            candidates_by_id.get(safe_int(lifecycle.get("id")), []),
            outcomes,
            outcome_signals,
            source_signals,
            now=current_time,
            tolerance_sec=tolerance,
        )
        for lifecycle in lifecycles
    ]
    return lifecycles, plans, source_signals


def link_lifecycle_outcomes(
    settings: Settings | None = None,
    *,
    symbol: str = "",
    lifecycle_id: int | None = None,
    limit: int = 200,
    horizon: str = "",
    dry_run: bool = False,
    force_relink: bool = False,
    now: datetime | int | float | None = None,
    _lifecycle_ids: Iterable[int] | None = None,
    _write_report: bool = True,
) -> dict[str, Any]:
    started = time.perf_counter()
    loaded = settings or Settings.load()
    scope_error = _scope_error(symbol, lifecycle_id)
    if scope_error:
        return {"ok": False, "error": scope_error, "processed": 0, "failed": 1}
    normalized_horizon = str(horizon or "").lower()
    if normalized_horizon and normalized_horizon not in HORIZONS:
        return {"ok": False, "error": "invalid_horizon", "processed": 0, "failed": 1}
    if not dry_run and not _outcome_store_available(loaded):
        return {
            "ok": False,
            "error": "outcome_store_unavailable",
            "processed": 0,
            "linked": 0,
            "failed": 1,
            "dry_run": False,
        }
    lifecycles, plans, _ = _prepare_plans(
        loaded,
        symbol=symbol,
        lifecycle_id=lifecycle_id,
        lifecycle_ids=_lifecycle_ids,
        limit=limit,
        # ``--horizon`` narrows backfill work, never the persisted lifecycle
        # coverage view.  Coverage always reflects all four research windows.
        horizon="",
        now=now,
    )
    store = IntelligenceStore(loaded)
    if not dry_run:
        store.ensure_schema()
        with store.transaction() as conn:
            store.write_outcome_plan_batch(
                plans,
                preserve_primary=not bool(force_relink),
                replace_links=True,
                conn=conn,
            )
            if plans:
                store.invalidate_analytics_cache("lifecycle:", conn=conn)
    linked = sum(safe_int(item["coverage"].get("linked_outcome_count")) for item in plans)
    linked_lifecycles = sum(safe_int(item["coverage"].get("linked_outcome_count")) > 0 for item in plans)
    report_written = False
    report_error = ""
    if not dry_run and plans and _write_report:
        try:
            lifecycle_path = Path(loaded.lifecycle_db_path).resolve()
            if lifecycle_path.is_relative_to(Path(BASE_DIR).resolve()):
                from .lifecycle_outcome_report import write_lifecycle_outcome_coverage_report

                write_lifecycle_outcome_coverage_report(loaded)
                report_written = True
        except Exception as exc:
            report_error = f"{type(exc).__name__}: {exc}"[:240]
    return {
        "ok": not bool(report_error),
        "dry_run": bool(dry_run),
        "processed": len(plans),
        "linked": linked,
        "linked_lifecycles": linked_lifecycles,
        "skipped": max(0, len(lifecycles) - len(plans)),
        "failed": 1 if report_error else 0,
        "report_written": report_written,
        "report_error": report_error,
        "duration_sec": round(time.perf_counter() - started, 4),
        "items": [item["coverage"] for item in plans],
    }


def _existing_outcome_keys(settings: Settings, signal_ids: list[int]) -> dict[tuple[int, str], str]:
    path = Path(settings.outcome_db_path)
    if not path.exists() or not signal_ids:
        return {}
    conn = _readonly_connection(path)
    result: dict[tuple[int, str], str] = {}
    try:
        if not _table_exists(conn, "signal_outcomes"):
            return {}
        for offset in range(0, len(signal_ids), 800):
            chunk = signal_ids[offset : offset + 800]
            placeholders = ",".join("?" for _ in chunk)
            for row in conn.execute(
                f"SELECT signal_id, horizon, data_status FROM signal_outcomes WHERE signal_id IN ({placeholders})",
                chunk,
            ).fetchall():
                result[(safe_int(row["signal_id"]), str(row["horizon"]))] = str(row["data_status"] or "missing")
        return result
    finally:
        conn.close()


def backfill_lifecycle_outcomes(
    settings: Settings | None = None,
    *,
    symbol: str = "",
    lifecycle_id: int | None = None,
    limit: int = 200,
    horizon: str = "",
    dry_run: bool = False,
    force_relink: bool = False,
    force_outcome_rebuild: bool = False,
    now: datetime | int | float | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    loaded = settings or Settings.load()
    scope_error = _scope_error(symbol, lifecycle_id)
    if scope_error:
        return {"ok": False, "error": scope_error, "processed": 0, "failed": 1, "dry_run": bool(dry_run)}
    normalized_horizon = str(horizon or "").lower()
    if normalized_horizon and normalized_horizon not in HORIZONS:
        return {"ok": False, "error": "invalid_horizon", "processed": 0, "failed": 1, "dry_run": bool(dry_run)}
    horizon = normalized_horizon
    if not dry_run and not _outcome_store_available(loaded):
        from .outcome_tracker import OutcomeStore

        OutcomeStore(loaded.outcome_db_path).ensure_schema()
    initial = link_lifecycle_outcomes(
        loaded,
        symbol=symbol,
        lifecycle_id=lifecycle_id,
        limit=limit,
        horizon=horizon,
        dry_run=dry_run,
        force_relink=force_relink,
        now=now,
        _write_report=False,
    )
    if not bool(initial.get("ok", True)):
        return {
            **initial,
            "failed": max(1, safe_int(initial.get("failed"))),
            "duration_sec": round(time.perf_counter() - started, 4),
        }
    selected_lifecycle_ids = [
        safe_int(item.get("lifecycle_id"))
        for item in list(initial.get("items") or [])
        if safe_int(item.get("lifecycle_id")) > 0
    ]
    if selected_lifecycle_ids:
        lifecycles, events = _read_lifecycle_sources(
            loaded,
            symbol=symbol,
            lifecycle_id=lifecycle_id,
            lifecycle_ids=selected_lifecycle_ids,
            limit=limit,
            rotate=False,
        )
    else:
        lifecycles, events = [], {}
    candidates = [
        candidate
        for lifecycle in lifecycles
        for candidate in extract_lifecycle_signal_candidates(lifecycle, events.get(safe_int(lifecycle.get("id")), []))
        if safe_int(candidate.get("signal_id")) > 0
    ]
    signal_ids = sorted({safe_int(item.get("signal_id")) for item in candidates})
    signals = _read_signal_rows(Path(loaded.signal_events_db_path), signal_ids)
    existing = _existing_outcome_keys(loaded, signal_ids)
    current = _utc_now(now)
    selected_horizons = (str(horizon).lower(),) if horizon else HORIZONS
    max_outcomes = max(1, safe_int(getattr(loaded, "lifecycle_outcome_backfill_max_outcomes", 1000), 1000))
    missing: dict[str, list[dict[str, Any]]] = defaultdict(list)
    retry_error_horizons: set[str] = set()
    seen_pairs: set[tuple[int, str]] = set()
    planned_pair_count = 0
    skipped = 0
    for candidate in candidates:
        signal_id = safe_int(candidate.get("signal_id"))
        source = signals.get(signal_id)
        if not source:
            skipped += len(selected_horizons)
            continue
        signal_time = _parse_time(source.get("time") or candidate.get("signal_time"))
        if signal_time is None:
            skipped += len(selected_horizons)
            continue
        for current_horizon in selected_horizons:
            pair = (signal_id, current_horizon)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            if signal_time.timestamp() + OUTCOME_WINDOWS[current_horizon] > current.timestamp():
                skipped += 1
                continue
            status = existing.get(pair, "missing")
            if not force_outcome_rebuild and status in {"success", "unavailable"}:
                skipped += 1
                continue
            if status == "error":
                retry_error_horizons.add(current_horizon)
            if planned_pair_count >= max_outcomes:
                skipped += 1
                continue
            missing[current_horizon].append(source)
            planned_pair_count += 1

    scan_results: list[dict[str, Any]] = []
    backfilled = unavailable = failed = 0
    changed_outcomes = 0
    if missing and dry_run:
        scan_results = [
            {"ok": True, "dry_run": True, "horizon": current_horizon, "planned": len(rows)}
            for current_horizon, rows in missing.items()
        ]
    elif missing:
        from . import outcome_tracker

        scanner = getattr(outcome_tracker, "scan_signal_outcomes", None)
        if not callable(scanner):
            return {
                **initial,
                "ok": False,
                "error": "targeted_outcome_scanner_unavailable",
                "failed": 1,
                "duration_sec": round(time.perf_counter() - started, 4),
            }
        for current_horizon, rows in missing.items():
            try:
                result = scanner(
                    rows,
                    settings=loaded,
                    limit=min(len(rows), max_outcomes),
                    horizon=current_horizon,
                    dry_run=dry_run,
                    force_rebuild=force_outcome_rebuild or current_horizon in retry_error_horizons,
                    now_ts=int(current.timestamp()),
                )
                scan_results.append(result if isinstance(result, dict) else {"ok": True})
                counts = result.get("counts", result) if isinstance(result, dict) else {}
                # A tracker can report both creation and completion for the
                # same signal/horizon. Count the completed outcome once.
                backfilled += safe_int(counts.get("success"))
                unavailable += safe_int(counts.get("unavailable"))
                completed_errors = safe_int(counts.get("error"))
                failed += completed_errors
                changed_outcomes += (
                    safe_int(counts.get("success"))
                    + safe_int(counts.get("unavailable"))
                    + completed_errors
                )
                if isinstance(result, dict) and result.get("ok") is False and safe_int(counts.get("error")) <= 0:
                    failed += len(rows)
            except Exception as exc:
                failed += len(rows)
                scan_results.append({"ok": False, "error": f"{type(exc).__name__}: {exc}"[:240]})
    final_link = initial if dry_run or not selected_lifecycle_ids else link_lifecycle_outcomes(
        loaded,
        symbol=symbol,
        lifecycle_id=lifecycle_id,
        limit=limit,
        horizon=horizon,
        force_relink=force_relink,
        now=now,
        _lifecycle_ids=selected_lifecycle_ids,
    )
    refresh: dict[str, Any] = {}
    if not dry_run and changed_outcomes > 0:
        try:
            from .lifecycle_replay import rebuild_replays

            refresh["replay"] = rebuild_replays(
                loaded,
                symbol=symbol,
                lifecycle_id=lifecycle_id,
                limit=min(max(1, safe_int(limit, 200)), 500),
                force=True,
                lifecycle_ids=selected_lifecycle_ids,
            )
        except Exception as exc:
            refresh["replay"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:240]}
        try:
            from .lifecycle_intelligence import generate_intelligence

            refresh["intelligence"] = generate_intelligence(
                settings=loaded,
                symbol=symbol,
                all_active=False,
                limit=min(max(1, safe_int(limit, 200)), 500),
                force=True,
                lifecycle_ids=selected_lifecycle_ids,
            )
        except Exception as exc:
            refresh["intelligence"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:240]}
        try:
            from .lifecycle_analytics import generate_lifecycle_analytics

            refresh["analytics"] = generate_lifecycle_analytics(settings=loaded, force=True)
        except Exception as exc:
            refresh["analytics"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:240]}
    refresh_failed = sum(
        1 for value in refresh.values()
        if isinstance(value, dict) and value.get("ok") is False
    )
    total_failed = failed + refresh_failed
    return {
        **final_link,
        "ok": bool(final_link.get("ok", True)) and total_failed == 0,
        "backfilled": backfilled,
        "changed_outcomes": changed_outcomes,
        "planned": sum(len(rows) for rows in missing.values()),
        "skipped": skipped,
        "unavailable": unavailable,
        "failed": total_failed,
        "refresh_failed": refresh_failed,
        "scan_results": scan_results,
        "refresh": refresh,
        "duration_sec": round(time.perf_counter() - started, 4),
    }


def lifecycle_outcome_coverage_list(
    settings: Settings | None = None,
    *,
    symbol: str = "",
    lifecycle_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
    coverage_label: str = "",
    maturity_label: str = "",
) -> dict[str, Any]:
    loaded = settings or Settings.load()
    scope_error = _scope_error(symbol, lifecycle_id)
    if scope_error:
        return {"ok": False, "error": scope_error, "data": {"items": [], "total": 0}}
    normalized_requested = normalize_lifecycle_symbol(symbol)
    if symbol and not normalized_requested:
        return {"ok": True, "data": {"items": [], "total": 0, "limit": limit, "offset": offset}}
    path = Path(loaded.lifecycle_db_path)
    if not path.exists():
        return {"ok": True, "data": {"items": [], "total": 0, "limit": limit, "offset": offset}}
    conn = _readonly_connection(path)
    try:
        if not _table_exists(conn, "lifecycle_outcome_coverage"):
            return {"ok": True, "data": {"items": [], "total": 0, "limit": limit, "offset": offset}}
        clauses: list[str] = []
        params: dict[str, Any] = {
            "limit": max(1, min(safe_int(limit, 50), 500)),
            "offset": max(0, safe_int(offset)),
        }
        normalized = normalized_requested
        if normalized:
            clauses.append("c.symbol = :symbol")
            params["symbol"] = normalized
        if lifecycle_id:
            clauses.append("c.lifecycle_id = :lifecycle_id")
            params["lifecycle_id"] = safe_int(lifecycle_id)
        if coverage_label:
            clauses.append("c.coverage_label = :coverage_label")
            params["coverage_label"] = str(coverage_label)
        if maturity_label:
            clauses.append("c.maturity_label = :maturity_label")
            params["maturity_label"] = str(maturity_label)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        total = safe_int(conn.execute(
            f"SELECT COUNT(*) FROM lifecycle_outcome_coverage c {where}", params
        ).fetchone()[0])
        has_base = _table_exists(conn, "signal_lifecycles")
        join = " LEFT JOIN signal_lifecycles l ON l.id = c.lifecycle_id" if has_base else ""
        base_columns = (
            ", l.first_signal_level, l.highest_level, l.current_state, l.is_active, l.updated_at AS lifecycle_updated_at"
            if has_base else ""
        )
        rows = conn.execute(
            "SELECT c.lifecycle_id, c.symbol, c.candidate_signal_count, c.linked_signal_count, "
            "c.linked_outcome_count, c.horizon_1h_status, c.horizon_4h_status, "
            "c.horizon_24h_status, c.horizon_72h_status, c.linked_horizon_count, "
            "c.mature_horizon_count, c.link_coverage_ratio, c.maturity_ratio, "
            "c.coverage_label, c.maturity_label, c.unlinked_reason, c.reasons_json, c.calculated_at, c.updated_at "
            f"{base_columns} FROM lifecycle_outcome_coverage c{join} {where} "
            "ORDER BY c.link_coverage_ratio ASC, c.maturity_ratio ASC, c.updated_at DESC "
            "LIMIT :limit OFFSET :offset",
            params,
        ).fetchall()
        return {
            "ok": True,
            "data": {
                "items": [dict(row) for row in rows],
                "total": total,
                "limit": params["limit"],
                "offset": params["offset"],
            },
        }
    finally:
        conn.close()


def lifecycle_outcome_detail(
    settings: Settings | None = None,
    *,
    symbol: str = "",
    lifecycle_id: int | None = None,
) -> dict[str, Any]:
    loaded = settings or Settings.load()
    scope_error = _scope_error(symbol, lifecycle_id)
    if scope_error or (not str(symbol or "").strip() and lifecycle_id is None):
        return {"ok": False, "error": scope_error or "missing_lifecycle_target", "data": {"available": False}}
    path = Path(loaded.lifecycle_db_path)
    if not path.exists():
        return {"ok": True, "data": {"available": False}}
    conn = _readonly_connection(path)
    try:
        if not _table_exists(conn, "lifecycle_outcome_coverage"):
            return {"ok": True, "data": {"available": False}}
        normalized = normalize_lifecycle_symbol(symbol)
        if lifecycle_id and normalized:
            coverage = conn.execute(
                "SELECT * FROM lifecycle_outcome_coverage WHERE lifecycle_id = ? AND symbol = ?",
                (safe_int(lifecycle_id), normalized),
            ).fetchone()
        elif lifecycle_id:
            coverage = conn.execute(
                "SELECT * FROM lifecycle_outcome_coverage WHERE lifecycle_id = ?", (safe_int(lifecycle_id),)
            ).fetchone()
        else:
            coverage = conn.execute(
                "SELECT * FROM lifecycle_outcome_coverage WHERE symbol = ?", (normalized,)
            ).fetchone()
        if coverage is None:
            return {"ok": True, "data": {"available": False, "symbol": normalized}}
        coverage_data = dict(coverage)
        reasons_raw = coverage_data.pop("reasons_json", "")
        try:
            import json

            coverage_data["reasons"] = json.loads(reasons_raw) if reasons_raw else {}
        except (TypeError, ValueError):
            coverage_data["reasons"] = {}
        links = [
            dict(row)
            for row in conn.execute(
                "SELECT id, lifecycle_id, symbol, signal_id, lifecycle_event_id, outcome_id, horizon, "
                "outcome_status, link_role, link_method, link_confidence, signal_time, outcome_time, is_primary "
                "FROM lifecycle_outcome_links WHERE lifecycle_id = ? "
                "ORDER BY is_primary DESC, signal_time, signal_id, horizon",
                (safe_int(coverage_data.get("lifecycle_id")),),
            ).fetchall()
        ] if _table_exists(conn, "lifecycle_outcome_links") else []
    finally:
        conn.close()
    outcome_ids = [safe_int(item.get("outcome_id")) for item in links]
    outcomes: dict[int, dict[str, Any]] = {}
    outcome_path = Path(loaded.outcome_db_path)
    if outcome_ids and outcome_path.exists():
        outcome_conn = _readonly_connection(outcome_path)
        try:
            if _table_exists(outcome_conn, "signal_outcomes"):
                for offset in range(0, len(outcome_ids), 800):
                    chunk = outcome_ids[offset : offset + 800]
                    placeholders = ",".join("?" for _ in chunk)
                    for row in outcome_conn.execute(
                        "SELECT id, signal_id, horizon, data_status, signal_time, due_time, "
                        "final_return_pct, max_gain_pct, max_drawdown_pct, result_label, updated_at "
                        f"FROM signal_outcomes WHERE id IN ({placeholders})",
                        chunk,
                    ).fetchall():
                        item = dict(row)
                        outcomes[safe_int(item.get("id"))] = item
        finally:
            outcome_conn.close()
    for link in links:
        link["outcome"] = outcomes.get(safe_int(link.get("outcome_id")))
    return {"ok": True, "data": {"available": True, "coverage": coverage_data, "links": links}}


def lifecycle_outcome_status(
    settings: Settings | None = None,
    *,
    symbol: str = "",
    lifecycle_id: int | None = None,
) -> dict[str, Any]:
    loaded = settings or Settings.load()
    scope_error = _scope_error(symbol, lifecycle_id)
    if scope_error:
        return {"ok": False, "error": scope_error, "data": {}}
    path = Path(loaded.lifecycle_db_path)
    lifecycle_count = 0
    rows: list[dict[str, Any]] = []
    if path.exists():
        conn = _readonly_connection(path)
        try:
            clauses: list[str] = []
            params: list[Any] = []
            normalized = normalize_lifecycle_symbol(symbol)
            if normalized:
                clauses.append("symbol = ?")
                params.append(normalized)
            if lifecycle_id:
                clauses.append("lifecycle_id = ?")
                params.append(safe_int(lifecycle_id))
            coverage_where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            if _table_exists(conn, "lifecycle_outcome_coverage"):
                rows = [
                    dict(row)
                    for row in conn.execute(
                        "SELECT lifecycle_id, symbol, candidate_signal_count, linked_signal_count, "
                        "linked_outcome_count, horizon_1h_status, horizon_4h_status, "
                        "horizon_24h_status, horizon_72h_status, mature_horizon_count, "
                        "unlinked_reason, reasons_json "
                        f"FROM lifecycle_outcome_coverage {coverage_where}",
                        params,
                    ).fetchall()
                ]
            if _table_exists(conn, "signal_lifecycles"):
                lifecycle_clauses: list[str] = []
                lifecycle_params: list[Any] = []
                if normalized:
                    lifecycle_clauses.append("symbol = ?")
                    lifecycle_params.append(normalized)
                if lifecycle_id:
                    lifecycle_clauses.append("id = ?")
                    lifecycle_params.append(safe_int(lifecycle_id))
                where = f"WHERE {' AND '.join(lifecycle_clauses)}" if lifecycle_clauses else ""
                lifecycle_count = safe_int(conn.execute(
                    f"SELECT COUNT(*) FROM signal_lifecycles {where}", lifecycle_params
                ).fetchone()[0])
        finally:
            conn.close()
    horizon_counts: dict[str, dict[str, int]] = {
        horizon: {status: 0 for status in VALID_OUTCOME_STATUSES} for horizon in HORIZONS
    }
    reasons: Counter[str] = Counter()
    lifecycle_primary_reasons: Counter[str] = Counter()
    for row in rows:
        if row.get("unlinked_reason"):
            lifecycle_primary_reasons[str(row["unlinked_reason"])] += 1
        try:
            reason_payload = json.loads(str(row.get("reasons_json") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            reason_payload = {}
        reason_counts = reason_payload.get("reason_counts") if isinstance(reason_payload, dict) else {}
        if isinstance(reason_counts, dict) and reason_counts:
            for reason, count in reason_counts.items():
                reasons[str(reason)] += max(0, safe_int(count))
        elif row.get("unlinked_reason"):
            reasons[str(row["unlinked_reason"])] += 1
        for horizon in HORIZONS:
            status = str(row.get(f"horizon_{horizon}_status") or "missing")
            horizon_counts[horizon][status if status in VALID_OUTCOME_STATUSES else "error"] += 1
    candidate_count = sum(safe_int(row.get("candidate_signal_count")) for row in rows)
    linked_signal_count = sum(safe_int(row.get("linked_signal_count")) for row in rows)
    mature_count = sum(safe_int(row.get("mature_horizon_count")) for row in rows)
    due_count = sum(
        1
        for row in rows
        for horizon in HORIZONS
        if str(row.get(f"horizon_{horizon}_status") or "missing") != "not_due"
    )
    status_totals = {
        status: sum(horizon_counts[horizon][status] for horizon in HORIZONS)
        for status in VALID_OUTCOME_STATUSES
    }
    data = {
        "lifecycle_count": lifecycle_count,
        "candidate_signal_count": candidate_count,
        "candidate_lifecycle_count": sum(safe_int(row.get("candidate_signal_count")) > 0 for row in rows),
        "linked_lifecycle_count": sum(safe_int(row.get("linked_outcome_count")) > 0 for row in rows),
        "linked_outcome_count": sum(safe_int(row.get("linked_outcome_count")) for row in rows),
        "link_coverage_ratio": round(linked_signal_count / candidate_count, 6) if candidate_count else 0.0,
        "mature_lifecycle_count": sum(safe_int(row.get("mature_horizon_count")) > 0 for row in rows),
        "maturity_ratio": round(mature_count / due_count, 6) if due_count else 0.0,
        "horizons": horizon_counts,
        "success_by_horizon": {horizon: horizon_counts[horizon]["success"] for horizon in HORIZONS},
        **status_totals,
        **{f"{status}_count": count for status, count in status_totals.items()},
        "unlinked_reasons": dict(sorted(reasons.items())),
        "lifecycle_primary_reasons": dict(sorted(lifecycle_primary_reasons.items())),
    }
    return {"ok": True, "data": data, **data}


def reconcile_lifecycle_outcomes(
    settings: Settings | None = None,
    *,
    symbol: str = "",
    lifecycle_id: int | None = None,
    limit: int = 200,
    repair: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    started = time.perf_counter()
    loaded = settings or Settings.load()
    scope_error = _scope_error(symbol, lifecycle_id)
    if scope_error:
        return {"ok": False, "error": scope_error, "dry_run": bool(dry_run), "repair": bool(repair), "issues": {}}
    path = Path(loaded.lifecycle_db_path)
    empty = {
        "duplicate_links": 0, "multiple_primary": 0, "orphan_links": 0,
        "symbol_mismatch": 0, "signal_id_mismatch": 0, "primary_mismatch": 0,
        "link_mismatch": 0, "coverage_mismatch": 0,
    }
    if not path.exists():
        return {"ok": True, "dry_run": bool(dry_run), "repair": bool(repair), "issues": empty}
    if not _outcome_store_available(loaded):
        return {
            "ok": False,
            "error": "outcome_store_unavailable",
            "dry_run": bool(dry_run),
            "repair": bool(repair),
            "repaired": 0,
            "issues": empty,
        }
    store = IntelligenceStore(loaded)
    if not dry_run:
        store.ensure_schema()
    conn = _readonly_connection(path)
    page_size = max(1, min(safe_int(limit, 200), 1000))
    audit_now = _utc_now()
    source_lifecycles: list[dict[str, Any]] = []
    source_events: dict[int, list[dict[str, Any]]] = defaultdict(list)
    expected_plans: list[dict[str, Any]] = []
    offset = 0
    while True:
        batch_lifecycles, batch_events = _read_lifecycle_sources(
            loaded,
            symbol=symbol,
            lifecycle_id=lifecycle_id,
            limit=page_size,
            offset=offset,
            rotate=False,
        )
        _, batch_plans, _ = _prepare_plans(
            loaded,
            symbol=symbol,
            lifecycle_id=lifecycle_id,
            limit=page_size,
            offset=offset,
            rotate=False,
            now=audit_now,
        )
        source_lifecycles.extend(batch_lifecycles)
        for key, values in batch_events.items():
            source_events[key].extend(values)
        expected_plans.extend(batch_plans)
        if symbol or lifecycle_id or len(batch_lifecycles) < page_size or len(source_lifecycles) >= 10000:
            break
        offset += page_size
    exact_candidate_ids = {
        safe_int(item.get("id")): {
            safe_int(candidate.get("signal_id"))
            for candidate in extract_lifecycle_signal_candidates(
                item, source_events.get(safe_int(item.get("id")), [])
            )
            if safe_int(candidate.get("signal_id")) > 0
        }
        for item in source_lifecycles
    }
    expected_coverage = {
        safe_int(plan.get("coverage", {}).get("lifecycle_id")): dict(plan.get("coverage") or {})
        for plan in expected_plans
    }
    expected_link_sets: dict[int, set[tuple[Any, ...]]] = defaultdict(set)
    for plan in expected_plans:
        coverage = dict(plan.get("coverage") or {})
        current_id = safe_int(coverage.get("lifecycle_id"))
        primary_id = safe_int(coverage.get("primary_outcome_id"))
        for link in list(plan.get("links") or []):
            expected_link_sets[current_id].add((
                safe_int(link.get("outcome_id")),
                safe_int(link.get("signal_id")),
                str(link.get("horizon") or ""),
                str(link.get("link_role") or ""),
                str(link.get("link_method") or ""),
                str(link.get("outcome_status") or ""),
                int(safe_int(link.get("outcome_id")) == primary_id),
            ))
    try:
        if not {_name for _name in ("signal_lifecycles", "lifecycle_outcome_links", "lifecycle_outcome_coverage") if _table_exists(conn, _name)} == {"signal_lifecycles", "lifecycle_outcome_links", "lifecycle_outcome_coverage"}:
            return {"ok": True, "dry_run": bool(dry_run), "repair": bool(repair), "issues": empty}
        normalized = normalize_lifecycle_symbol(symbol)
        lifecycle_rows = {
            safe_int(item.get("id")): {
                "id": safe_int(item.get("id")),
                "symbol": normalize_lifecycle_symbol(item.get("symbol")),
            }
            for item in source_lifecycles
        }
        selected_ids = sorted(lifecycle_rows)
        links: list[dict[str, Any]] = []
        coverage_rows: dict[int, dict[str, Any]] = {}
        duplicate = 0
        multiple_primary = 0
        for chunk_offset in range(0, len(selected_ids), 800):
            chunk = selected_ids[chunk_offset : chunk_offset + 800]
            placeholders = ",".join("?" for _ in chunk)
            links.extend(
                dict(row)
                for row in conn.execute(
                    f"SELECT * FROM lifecycle_outcome_links WHERE lifecycle_id IN ({placeholders}) "
                    "ORDER BY lifecycle_id, id",
                    chunk,
                ).fetchall()
            )
            duplicate += safe_int(conn.execute(
                "SELECT COUNT(*) FROM (SELECT lifecycle_id, outcome_id FROM lifecycle_outcome_links "
                f"WHERE lifecycle_id IN ({placeholders}) "
                "GROUP BY lifecycle_id, outcome_id HAVING COUNT(*) > 1)",
                chunk,
            ).fetchone()[0])
            multiple_primary += safe_int(conn.execute(
                "SELECT COUNT(*) FROM (SELECT lifecycle_id FROM lifecycle_outcome_links "
                f"WHERE is_primary=1 AND lifecycle_id IN ({placeholders}) "
                "GROUP BY lifecycle_id HAVING COUNT(*) > 1)",
                chunk,
            ).fetchone()[0])
            for row in conn.execute(
                f"SELECT * FROM lifecycle_outcome_coverage WHERE lifecycle_id IN ({placeholders})",
                chunk,
            ).fetchall():
                coverage_rows[safe_int(row["lifecycle_id"])] = dict(row)
        # A global reconcile also audits links whose lifecycle was removed.
        if not normalized and not lifecycle_id:
            links.extend(
                dict(row)
                for row in conn.execute(
                    "SELECT l.* FROM lifecycle_outcome_links l "
                    "LEFT JOIN signal_lifecycles b ON b.id=l.lifecycle_id "
                    "WHERE b.id IS NULL ORDER BY l.id"
                ).fetchall()
            )
    finally:
        conn.close()
    outcome_ids = sorted({safe_int(item.get("outcome_id")) for item in links})
    outcomes: dict[int, dict[str, Any]] = {}
    outcome_path = Path(loaded.outcome_db_path)
    if outcome_ids and outcome_path.exists():
        out_conn = _readonly_connection(outcome_path)
        try:
            if _table_exists(out_conn, "signal_outcomes"):
                for offset in range(0, len(outcome_ids), 800):
                    chunk = outcome_ids[offset : offset + 800]
                    placeholders = ",".join("?" for _ in chunk)
                    for row in out_conn.execute(
                        f"SELECT id, signal_id, symbol FROM signal_outcomes WHERE id IN ({placeholders})", chunk
                    ).fetchall():
                        outcomes[safe_int(row["id"])] = dict(row)
        finally:
            out_conn.close()
    invalid_link_ids: set[int] = set()
    orphan = symbol_mismatch = signal_mismatch = 0
    link_counts: Counter[int] = Counter()
    actual_primary_ids: dict[int, int] = {}
    actual_link_sets: dict[int, set[tuple[Any, ...]]] = defaultdict(set)
    for link in links:
        link_id = safe_int(link.get("id"))
        lifecycle = lifecycle_rows.get(safe_int(link.get("lifecycle_id")))
        outcome = outcomes.get(safe_int(link.get("outcome_id")))
        if lifecycle is None or outcome is None:
            orphan += 1
            invalid_link_ids.add(link_id)
            continue
        if normalize_lifecycle_symbol(link.get("symbol")) != normalize_lifecycle_symbol(lifecycle.get("symbol")) or normalize_lifecycle_symbol(link.get("symbol")) != normalize_lifecycle_symbol(outcome.get("symbol")):
            symbol_mismatch += 1
            invalid_link_ids.add(link_id)
            continue
        if safe_int(link.get("signal_id")) != safe_int(outcome.get("signal_id")):
            signal_mismatch += 1
            invalid_link_ids.add(link_id)
            continue
        lifecycle_key = safe_int(link.get("lifecycle_id"))
        if lifecycle_key in lifecycle_rows:
            actual_link_sets[lifecycle_key].add((
                safe_int(link.get("outcome_id")),
                safe_int(link.get("signal_id")),
                str(link.get("horizon") or ""),
                str(link.get("link_role") or ""),
                str(link.get("link_method") or ""),
                str(link.get("outcome_status") or ""),
                safe_int(link.get("is_primary")),
            ))
        if (
            str(link.get("link_method") or "") != "symbol_time_module"
            and safe_int(link.get("signal_id")) not in exact_candidate_ids.get(lifecycle_key, set())
        ):
            signal_mismatch += 1
            invalid_link_ids.add(link_id)
            continue
        link_counts[lifecycle_key] += 1
        if safe_int(link.get("is_primary")) == 1:
            actual_primary_ids[lifecycle_key] = safe_int(link.get("outcome_id"))
    coverage_mismatch = 0
    coverage_fields = (
        "candidate_signal_count", "linked_signal_count", "linked_outcome_count",
        "primary_outcome_id", "horizon_1h_status", "horizon_4h_status",
        "horizon_24h_status", "horizon_72h_status", "linked_horizon_count",
        "mature_horizon_count", "link_coverage_ratio", "maturity_ratio",
        "coverage_label", "maturity_label", "unlinked_reason",
        "reasons_json",
    )

    def coverage_value_equal(field: str, left: Any, right: Any) -> bool:
        if field == "reasons_json":
            def normalized_json(value: Any) -> str:
                parsed = value
                if isinstance(value, str):
                    try:
                        parsed = json.loads(value or "{}")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        return f"!invalid:{value}"
                return json.dumps(parsed or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

            return normalized_json(left) == normalized_json(right)
        if field in {"link_coverage_ratio", "maturity_ratio"}:
            return abs((safe_float(left) or 0.0) - (safe_float(right) or 0.0)) <= 0.000001
        if field in {
            "candidate_signal_count", "linked_signal_count", "linked_outcome_count",
            "primary_outcome_id", "linked_horizon_count", "mature_horizon_count",
        }:
            return safe_int(left) == safe_int(right)
        return str(left or "") == str(right or "")

    primary_mismatch = sum(
        safe_int(actual_primary_ids.get(current_id))
        != safe_int((expected_coverage.get(current_id) or {}).get("primary_outcome_id"))
        for current_id in lifecycle_rows
    )
    link_mismatch = sum(
        actual_link_sets.get(current_id, set()) != expected_link_sets.get(current_id, set())
        for current_id in lifecycle_rows
    )
    for current_id in lifecycle_rows:
        coverage = coverage_rows.get(current_id)
        expected = dict(expected_coverage.get(current_id) or {})
        expected["reasons_json"] = expected.get("reasons") or {}
        if (
            coverage is None
            or safe_int(coverage.get("linked_outcome_count")) != link_counts[current_id]
            or any(not coverage_value_equal(field, coverage.get(field), expected.get(field)) for field in coverage_fields)
        ):
            coverage_mismatch += 1
    issues = {
        "duplicate_links": safe_int(duplicate),
        "multiple_primary": safe_int(multiple_primary),
        "orphan_links": orphan,
        "symbol_mismatch": symbol_mismatch,
        "signal_id_mismatch": signal_mismatch,
        "primary_mismatch": primary_mismatch,
        "link_mismatch": link_mismatch,
        "coverage_mismatch": coverage_mismatch,
    }
    repaired = 0
    detected_issues = dict(issues)
    if repair and not dry_run and any(detected_issues.values()):
        if invalid_link_ids:
            with store.transaction() as write_conn:
                for offset in range(0, len(invalid_link_ids), 800):
                    chunk = list(invalid_link_ids)[offset : offset + 800]
                    placeholders = ",".join("?" for _ in chunk)
                    write_conn.execute(
                        f"DELETE FROM lifecycle_outcome_links WHERE id IN ({placeholders})", chunk
                    )
                    repaired += len(chunk)
        selected_source_ids = [safe_int(item.get("id")) for item in source_lifecycles if safe_int(item.get("id")) > 0]
        chunks = [
            selected_source_ids[index : index + page_size]
            for index in range(0, len(selected_source_ids), page_size)
        ]
        for index, selected_chunk in enumerate(chunks):
            relink = link_lifecycle_outcomes(
                loaded,
                symbol=symbol,
                lifecycle_id=lifecycle_id,
                limit=len(selected_chunk),
                force_relink=True,
                _lifecycle_ids=selected_chunk,
                _write_report=index == len(chunks) - 1,
            )
            repaired += safe_int(relink.get("processed"))
        post = reconcile_lifecycle_outcomes(
            loaded,
            symbol=symbol,
            lifecycle_id=lifecycle_id,
            limit=limit,
            repair=False,
            dry_run=True,
        )
        issues = dict(post.get("issues") or issues)
    return {
        "ok": not any(issues.values()),
        "dry_run": bool(dry_run),
        "repair": bool(repair),
        "repaired": repaired,
        "issues": issues,
        "detected_issues": detected_issues,
        "duration_sec": round(time.perf_counter() - started, 4),
    }


__all__ = [
    "HORIZONS",
    "VALID_OUTCOME_STATUSES",
    "backfill_lifecycle_outcomes",
    "extract_lifecycle_signal_candidates",
    "lifecycle_outcome_coverage_list",
    "lifecycle_outcome_detail",
    "lifecycle_outcome_status",
    "link_lifecycle_outcomes",
    "reconcile_lifecycle_outcomes",
]
