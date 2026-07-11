from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import mean
from typing import Any, Iterable
from urllib.parse import quote

from .config import Settings
from .model_registry import ModelRegistryStore, current_model, utc_now


PERIOD_DAYS: dict[str, int | None] = {"7d": 7, "30d": 30, "90d": 90, "all": None}


def _parse_time(value: Any) -> datetime | None:
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


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _avg(values: Iterable[Any]) -> float | None:
    numbers = [number for value in values if (number := _finite(value)) is not None]
    return round(mean(numbers), 6) if numbers else None


def _outcome_rows(settings: Settings) -> list[dict[str, Any]]:
    path = Path(settings.outcome_db_path)
    if not path.exists():
        return []
    uri = "file:" + quote(path.resolve().as_posix(), safe="/:\\") + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return []
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='signal_outcomes'"
        ).fetchone()
        if not exists:
            return []
        rows = conn.execute(
            "SELECT signal_time,horizon,data_status,final_return_pct,max_gain_pct,max_drawdown_pct "
            "FROM signal_outcomes ORDER BY signal_time"
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def _attribution_window(model: dict[str, Any]) -> tuple[datetime | None, datetime | None, str, list[str]]:
    metadata = dict(model.get("metadata") or {})
    if metadata.get("bootstrap"):
        return None, None, "bootstrap_current_runtime_assumption", [
            "Historical Outcome rows do not carry model_version; bootstrap attribution assumes the current runtime model."
        ]
    return (
        _parse_time(model.get("released_at")),
        _parse_time(model.get("deprecated_at")),
        "deployment_window",
        [],
    )


def calculate_performance_snapshot(
    rows: list[dict[str, Any]], *, model: dict[str, Any], period: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    if period not in PERIOD_DAYS:
        raise ValueError("invalid_performance_period")
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    released, deprecated, attribution_method, warnings = _attribution_window(model)
    period_start = current_time - timedelta(days=PERIOD_DAYS[period]) if PERIOD_DAYS[period] else None
    start = max((item for item in (period_start, released) if item is not None), default=None)
    end = min(current_time, deprecated) if deprecated else current_time
    selected: list[dict[str, Any]] = []
    invalid_signal_time = 0
    for row in rows:
        signal_time = _parse_time(row.get("signal_time"))
        if signal_time is None:
            invalid_signal_time += 1
            continue
        if start and signal_time < start:
            continue
        if signal_time > end:
            continue
        selected.append(row)
    statuses = Counter(str(row.get("data_status") or "missing").lower() for row in selected)
    # pending/unavailable/error never enter return or success denominators.
    mature = [row for row in selected if str(row.get("data_status") or "").lower() == "success"]
    usable = [row for row in mature if _finite(row.get("final_return_pct")) is not None]
    positive = sum(1 for row in usable if float(row["final_return_pct"]) > 0)
    success_ratio = round(positive / len(usable), 6) if usable else None
    avg_return = _avg(row.get("final_return_pct") for row in usable)
    avg_drawdown = _avg(row.get("max_drawdown_pct") for row in usable)
    drawdown_events = sum(1 for row in usable if (_finite(row.get("max_drawdown_pct")) or 0.0) <= -5.0)
    risk_score = round(abs(avg_drawdown or 0.0), 6) if usable else None
    return {
        "model_id": int(model["id"]), "model_key": model.get("model_key"),
        "model_version": model.get("model_version"), "period": period,
        "sample_count": len(usable), "mature_sample_count": len(mature),
        "observed_row_count": len(selected), "success_count": positive,
        "success_ratio": success_ratio, "avg_return": avg_return,
        "avg_drawdown": avg_drawdown, "risk_score": risk_score,
        "avg_max_gain": _avg(row.get("max_gain_pct") for row in usable),
        "drawdown_ratio": round(drawdown_events / len(usable), 6) if usable else None,
        "status_counts": dict(sorted(statuses.items())),
        "invalid_signal_time_count": invalid_signal_time,
        "window_start": start.isoformat() if start else None,
        "window_end": end.isoformat(),
        "attribution_method": attribution_method,
        "warnings": warnings,
        "unavailable_excluded_from_failure": True,
        "pending_excluded_from_maturity": True,
    }


def generate_model_performance(
    settings: Settings | None = None, *, model_key: str = "signal-decision", version: str = "",
    periods: Iterable[str] = PERIOD_DAYS, dry_run: bool = False,
) -> dict[str, Any]:
    loaded = settings or Settings.load()
    store = ModelRegistryStore(loaded)
    model = store.get(model_key, version) if version else current_model(loaded, model_key=model_key)
    if not model:
        return {"ok": False, "code": "model_not_found", "snapshots": []}
    selected_periods = list(dict.fromkeys(str(period) for period in periods))
    rows = _outcome_rows(loaded)
    snapshots = [calculate_performance_snapshot(rows, model=model, period=period) for period in selected_periods]
    if dry_run:
        return {"ok": True, "dry_run": True, "changed": False, "model": model, "snapshots": snapshots}
    created_at = utc_now()
    with store.transaction() as conn:
        for item in snapshots:
            conn.execute(
                "INSERT INTO model_performance_snapshots(model_id,period,sample_count,success_ratio,avg_return,avg_drawdown,risk_score,metrics_json,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (int(model["id"]), item["period"], item["sample_count"], item["success_ratio"],
                 item["avg_return"], item["avg_drawdown"], item["risk_score"],
                 json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")), created_at),
            )
    return {"ok": True, "dry_run": False, "changed": True, "model": model, "snapshots": snapshots, "created_at": created_at}


def model_performance(
    settings: Settings | None = None, *, model_key: str = "signal-decision", version: str = "",
    period: str = "", refresh: bool = False, dry_run: bool = False,
) -> dict[str, Any]:
    loaded = settings or Settings.load()
    if refresh:
        periods = [period] if period else list(PERIOD_DAYS)
        return generate_model_performance(
            loaded, model_key=model_key, version=version, periods=periods, dry_run=dry_run,
        )
    store = ModelRegistryStore(loaded)
    model = store.get(model_key, version) if version else current_model(loaded, model_key=model_key)
    if not model:
        return {"ok": False, "code": "model_not_found", "snapshots": []}
    with store.readonly() as conn:
        if conn is None:
            return {"ok": False, "code": "model_registry_unavailable", "model": model, "snapshots": []}
        clauses = ["p.model_id=?"]
        params: list[Any] = [int(model["id"])]
        if period:
            if period not in PERIOD_DAYS:
                raise ValueError("invalid_performance_period")
            clauses.append("p.period=?")
            params.append(period)
        rows = conn.execute(
            "SELECT p.* FROM model_performance_snapshots p JOIN ("
            "SELECT period,MAX(id) latest_id FROM model_performance_snapshots WHERE model_id=? GROUP BY period"
            ") latest ON latest.latest_id=p.id WHERE " + " AND ".join(clauses) + " ORDER BY CASE p.period WHEN '7d' THEN 1 WHEN '30d' THEN 2 WHEN '90d' THEN 3 ELSE 4 END",
            [int(model["id"]), *params],
        ).fetchall()
    snapshots = []
    for row in rows:
        item = dict(row)
        item["metrics"] = json.loads(str(item.pop("metrics_json") or "{}"))
        snapshots.append(item)
    return {"ok": True, "model": model, "snapshots": snapshots, "cached": True}


def evaluate_model_health(model: dict[str, Any], snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    if str(model.get("status")) == "deprecated":
        return {"status": "deprecated", "label": "deprecated", "decline_ratio": None, "warnings": []}
    by_period = {str(item.get("period")): item for item in snapshots}
    recent = by_period.get("30d") or by_period.get("7d")
    baseline = by_period.get("all") or by_period.get("90d")
    warnings: list[str] = []
    if not recent or not baseline or not recent.get("sample_count") or not baseline.get("sample_count"):
        return {"status": "warning", "label": "insufficient_performance_samples", "decline_ratio": None, "warnings": ["insufficient_samples"]}
    recent_ratio = _finite(recent.get("success_ratio"))
    baseline_ratio = _finite(baseline.get("success_ratio"))
    if recent_ratio is None or baseline_ratio is None or baseline_ratio <= 0:
        return {"status": "warning", "label": "insufficient_success_baseline", "decline_ratio": None, "warnings": ["insufficient_baseline"]}
    decline = max(0.0, (baseline_ratio - recent_ratio) / baseline_ratio)
    if decline > 0.30:
        status = "degraded"
        warnings.append("30d_success_ratio_declined_more_than_30_percent")
    elif decline > 0.15:
        status = "warning"
        warnings.append("30d_success_ratio_declined_more_than_15_percent")
    else:
        status = "healthy"
    return {
        "status": status, "label": status, "decline_ratio": round(decline, 6),
        "recent_success_ratio": recent_ratio, "baseline_success_ratio": baseline_ratio,
        "warnings": warnings, "auto_replace": False,
    }


def model_health(
    settings: Settings | None = None, *, model_key: str = "signal-decision", version: str = "",
    refresh: bool = False, dry_run: bool = False,
) -> dict[str, Any]:
    loaded = settings or Settings.load()
    if refresh:
        generated = generate_model_performance(loaded, model_key=model_key, version=version, dry_run=dry_run)
        if not generated.get("ok"):
            return generated
        model = generated["model"]
        snapshots = generated["snapshots"]
    else:
        cached = model_performance(loaded, model_key=model_key, version=version)
        if not cached.get("ok"):
            return cached
        model = cached["model"]
        snapshots = [dict(item.get("metrics") or item) for item in cached["snapshots"]]
        # A manual CLI health check may initialize the cached timeline. Public
        # API adapters pass dry_run=True and therefore never scan source data.
        if not snapshots and not dry_run:
            generated = generate_model_performance(
                loaded, model_key=model_key, version=version, dry_run=False,
            )
            if not generated.get("ok"):
                return generated
            model = generated["model"]
            snapshots = generated["snapshots"]
    health = evaluate_model_health(model, snapshots)
    if not dry_run and model and str(model.get("health_status")) != health["status"]:
        store = ModelRegistryStore(loaded)
        with store.transaction() as conn:
            conn.execute("UPDATE models SET health_status=?,updated_at=? WHERE id=?", (health["status"], utc_now(), int(model["id"])))
    return {
        "ok": True, "model": {key: model.get(key) for key in ("model_key", "model_version", "status", "health_status")},
        "health": health, "snapshots": snapshots, "dry_run": dry_run,
        "monitor_only": True, "auto_replace": False,
    }


__all__ = [
    "PERIOD_DAYS", "calculate_performance_snapshot", "evaluate_model_health",
    "generate_model_performance", "model_health", "model_performance",
]
