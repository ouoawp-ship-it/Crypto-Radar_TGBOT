from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .atomic_json import atomic_write_text
from .config import BASE_DIR, Settings
from .decision_model import MODEL_VERSION as DECISION_MODEL_VERSION
from .lifecycle_intelligence import NOT_ADVICE
from .lifecycle_intelligence_store import IntelligenceStore
from .lifecycle_outcome_quality import lifecycle_calibration_readiness
from .lifecycle_store import normalize_lifecycle_symbol, safe_int


CALIBRATION_VERSION = "calibration-v1"
CALIBRATION_MODEL_VERSION = DECISION_MODEL_VERSION
CALIBRATION_REPORT_JSON = BASE_DIR / "docs" / "generated" / "model_calibration_latest.json"
CALIBRATION_REPORT_MD = BASE_DIR / "docs" / "generated" / "model_calibration_latest.md"
DECISION_CODES = ("observe", "wait_pullback", "probe", "avoid_chase", "risk_alert")
DECISION_LABELS = {
    "observe": "观察",
    "wait_pullback": "等待回踩",
    "probe": "可试仓",
    "avoid_chase": "禁止追高",
    "risk_alert": "风险警报",
}
HORIZON_RANK = {"1h": 1, "4h": 2, "24h": 3, "72h": 4}
LEVEL_RANK = {"unknown": 0, "15m": 1, "1h": 2, "4h": 3, "24h": 4}
RISK_EVENT_TYPES = {
    "risk_warning",
    "cooling",
    "short_term_weakening",
    "major_timeframe_weakening",
    "launch_failed",
}
RISK_EVENT_LABELS = {
    "risk_warning": "风险警报",
    "cooling": "短线冷却",
    "short_term_weakening": "短周期走弱",
    "major_timeframe_weakening": "大周期走弱",
    "launch_failed": "启动失败",
}
FACTOR_LABELS: dict[str, dict[str, str]] = {
    "oi_quadrant": {
        "oi_up_price_up": "OI 增长 + 价格上涨",
        "oi_up_price_down": "OI 增长 + 价格下跌",
        "oi_down_price_up": "OI 下降 + 价格上涨",
        "oi_down_price_down": "OI 下降 + 价格下跌",
        "unknown": "OI / 价格数据不足",
    },
    "spot_cvd": {
        "confirmed": "Spot CVD 现货买盘确认",
        "unconfirmed": "Spot CVD 未确认",
        "declining": "Spot CVD 现货主动卖出增强",
        "unknown": "Spot CVD 数据不足",
    },
    "futures_cvd": {
        "active_buying": "Futures CVD 主动买入增强",
        "active_selling": "Futures CVD 主动卖出增强",
        "no_direction": "Futures CVD 无明显方向",
        "unknown": "Futures CVD 数据不足",
    },
    "cvd_confirmation": {
        "spot_futures_sync": "Spot / Futures CVD 同步确认",
        "spot_only": "仅 Spot CVD 确认",
        "futures_only": "仅 Futures CVD 确认",
        "cvd_unconfirmed": "Spot / Futures CVD 均未确认",
        "unknown": "CVD 数据不足",
    },
    "volume_confirmation": {
        "expanded": "成交量显著放大",
        "moderate": "成交量温和确认",
        "weak": "成交量未确认",
        "unknown": "成交量数据不足",
    },
    "funding_state": {
        "healthy": "资金费率健康",
        "overheated_positive": "正资金费率过热",
        "overheated_negative": "负资金费率极端",
        "unknown": "资金费率数据不足",
    },
    "factor_combination": {
        "price_oi_cvd_confirmed": "价格 / OI / CVD 同步确认",
        "leverage_divergence": "OI 增长但价格走弱",
        "futures_without_spot": "合约买盘缺少现货确认",
        "volume_oi_confirmed": "成交量与 OI 同步确认",
        "limited_confirmation": "资金确认有限",
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _number(value: Any) -> float | None:
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _mean(values: Iterable[Any], digits: int = 4) -> float | None:
    numbers = [number for value in values if (number := _number(value)) is not None]
    return round(sum(numbers) / len(numbers), digits) if numbers else None


def _median(values: Iterable[Any], digits: int = 4) -> float | None:
    numbers = [number for value in values if (number := _number(value)) is not None]
    return round(float(statistics.median(numbers)), digits) if numbers else None


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator > 0 else None


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str) or not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _parse_time(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _factor_label(metric_type: str, key: str) -> str:
    return FACTOR_LABELS.get(metric_type, {}).get(key, key)


def _readonly(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    return conn


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _lifecycle_sources(path: Path) -> dict[str, list[dict[str, Any]]]:
    conn = _readonly(path)
    if conn is None:
        return {"lifecycles": [], "links": [], "events": [], "frames": []}
    try:
        tables = _tables(conn)
        if "signal_lifecycles" not in tables:
            return {"lifecycles": [], "links": [], "events": [], "frames": []}
        intelligence_join = (
            "LEFT JOIN lifecycle_intelligence i ON i.lifecycle_id=l.id"
            if "lifecycle_intelligence" in tables else ""
        )
        replay_join = (
            "LEFT JOIN lifecycle_replays r ON r.lifecycle_id=l.id"
            if "lifecycle_replays" in tables else ""
        )
        intelligence_projection = (
            "i.intelligence_score,i.capital_confirmation_label,i.confidence_label,i.factors_json"
            if intelligence_join else
            "NULL AS intelligence_score,'' AS capital_confirmation_label,'' AS confidence_label,NULL AS factors_json"
        )
        replay_projection = (
            "r.upgrade_path,r.duration_sec,r.max_price_gain_pct AS replay_max_gain_pct,"
            "r.max_drawdown_pct AS replay_max_drawdown_pct,r.final_return_pct AS replay_final_return_pct,"
            "r.result_label AS replay_result_label,r.outcome_status AS replay_outcome_status"
            if replay_join else
            "'' AS upgrade_path,NULL AS duration_sec,NULL AS replay_max_gain_pct,"
            "NULL AS replay_max_drawdown_pct,NULL AS replay_final_return_pct,"
            "'' AS replay_result_label,'' AS replay_outcome_status"
        )
        lifecycles = [
            dict(row)
            for row in conn.execute(
                f"""
                SELECT l.id AS lifecycle_id,l.symbol,l.first_signal_id,l.first_signal_at,
                       l.first_signal_module,l.first_signal_type,l.first_signal_level,l.highest_level,
                       l.current_state,l.lifecycle_score,l.risk_score,l.price_change_from_first_pct,
                       l.oi_change_from_first_pct,l.futures_cvd_change_from_first,
                       l.spot_cvd_change_from_first,l.latest_funding_rate,l.metrics_json,
                       l.is_active,l.created_at,l.updated_at,l.closed_at,
                       {intelligence_projection},{replay_projection}
                FROM signal_lifecycles l
                {intelligence_join}
                {replay_join}
                ORDER BY l.id
                """
            ).fetchall()
        ]
        links = (
            [dict(row) for row in conn.execute(
                "SELECT lifecycle_id,lifecycle_event_id,signal_id,outcome_id,horizon,outcome_status,"
                "link_role,link_method,is_primary FROM lifecycle_outcome_links ORDER BY lifecycle_id,id"
            ).fetchall()]
            if "lifecycle_outcome_links" in tables else []
        )
        events = (
            [dict(row) for row in conn.execute(
                "SELECT id,lifecycle_id,signal_id,event_time,event_type,price FROM lifecycle_events "
                "WHERE event_type IN ('risk_warning','cooling','short_term_weakening',"
                "'major_timeframe_weakening','launch_failed') ORDER BY lifecycle_id,event_time,id"
            ).fetchall()]
            if "lifecycle_events" in tables else []
        )
        frames = (
            [dict(row) for row in conn.execute(
                "SELECT lifecycle_id,frame_index,event_time,price FROM lifecycle_replay_frames "
                "ORDER BY lifecycle_id,event_time,frame_index"
            ).fetchall()]
            if "lifecycle_replay_frames" in tables else []
        )
        return {"lifecycles": lifecycles, "links": links, "events": events, "frames": frames}
    finally:
        conn.close()


def _outcome_rows(path: Path, links: list[dict[str, Any]]) -> list[dict[str, Any]]:
    outcome_ids = sorted({safe_int(link.get("outcome_id")) for link in links if safe_int(link.get("outcome_id")) > 0})
    conn = _readonly(path)
    if conn is None or not outcome_ids:
        if conn is not None:
            conn.close()
        return []
    try:
        if "signal_outcomes" not in _tables(conn):
            return []
        by_id: dict[int, dict[str, Any]] = {}
        projection = (
            "id,signal_id,symbol,signal_time,horizon,data_status,final_return_pct,max_gain_pct,"
            "max_drawdown_pct,result_label,decision_code,decision_label,decision_confidence,"
            "risk_level,module,signal_type,updated_at"
        )
        for offset in range(0, len(outcome_ids), 800):
            chunk = outcome_ids[offset : offset + 800]
            placeholders = ",".join("?" for _ in chunk)
            for row in conn.execute(
                f"SELECT {projection} FROM signal_outcomes WHERE id IN ({placeholders})",
                chunk,
            ):
                by_id[safe_int(row["id"])] = dict(row)
    finally:
        conn.close()
    rows: list[dict[str, Any]] = []
    for link in links:
        outcome = by_id.get(safe_int(link.get("outcome_id")))
        if outcome is None:
            continue
        item = dict(outcome)
        item.update({
            "lifecycle_id": safe_int(link.get("lifecycle_id")),
            "lifecycle_event_id": safe_int(link.get("lifecycle_event_id")) or None,
            "link_role": str(link.get("link_role") or ""),
            "link_method": str(link.get("link_method") or ""),
            "is_primary": int(link.get("is_primary") or 0),
        })
        rows.append(item)
    return rows


def _decision_success(row: dict[str, Any]) -> bool:
    code = str(row.get("decision_code") or "observe")
    final_return = _number(row.get("final_return_pct")) or 0.0
    drawdown = _number(row.get("max_drawdown_pct")) or 0.0
    if code in {"avoid_chase", "risk_alert"}:
        return final_return <= 0 or drawdown <= -3
    if code == "wait_pullback":
        return final_return > 0 or drawdown <= -2
    return final_return > 0


def _lifecycle_success(row: dict[str, Any]) -> bool:
    label = str(row.get("replay_result_label") or "")
    if label in {"strong_success", "success", "partial_success", "risk_avoided"}:
        return True
    return (_number(row.get("final_return_pct")) or 0.0) > 0


def _metric(
    metric_type: str,
    metric_key: str,
    rows: Iterable[dict[str, Any]],
    *,
    label: str = "",
    success_fn: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    items = [dict(row) for row in rows]
    mature = [
        row for row in items
        if str(row.get("data_status") or "success") == "success"
        and _number(row.get("final_return_pct")) is not None
    ]
    evaluator = success_fn or (lambda row: (_number(row.get("final_return_pct")) or 0.0) > 0)
    successes = [row for row in mature if evaluator(row)]
    drawdowns = [
        row for row in mature
        if (_number(row.get("max_drawdown_pct")) or 0.0) <= -3
        or (_number(row.get("final_return_pct")) or 0.0) <= -2
    ]
    confidence_pairs = [
        (max(0.0, min(100.0, float(confidence))) / 100.0, 1.0 if evaluator(row) else 0.0)
        for row in mature
        if (confidence := _number(row.get("decision_confidence"))) is not None
    ]
    confidence_accuracy = (
        round(1.0 - sum(abs(probability - actual) for probability, actual in confidence_pairs) / len(confidence_pairs), 6)
        if confidence_pairs else None
    )
    mature_count = len(mature)
    return {
        "metric_type": metric_type,
        "metric_key": metric_key,
        "key": metric_key,
        "label": label or metric_key,
        "sample_count": len(items),
        "mature_sample_count": mature_count,
        "pending_count": sum(str(row.get("data_status") or "") in {"pending", "ready"} for row in items),
        "unavailable_count": sum(str(row.get("data_status") or "") == "unavailable" for row in items),
        "error_count": sum(str(row.get("data_status") or "") == "error" for row in items),
        "success_count": len(successes),
        "success_ratio": _ratio(len(successes), mature_count),
        "positive_ratio": _ratio(sum((_number(row.get("final_return_pct")) or 0.0) > 0 for row in mature), mature_count),
        "avg_return_pct": _mean(row.get("final_return_pct") for row in mature),
        "median_return_pct": _median(row.get("final_return_pct") for row in mature),
        "avg_max_gain_pct": _mean(row.get("max_gain_pct") for row in mature),
        "avg_max_drawdown_pct": _mean(row.get("max_drawdown_pct") for row in mature),
        "drawdown_ratio": _ratio(len(drawdowns), mature_count),
        "expectancy_pct": _mean(row.get("final_return_pct") for row in mature),
        "avg_confidence": _mean(
            (row.get("decision_confidence") for row in mature), digits=2
        ),
        "confidence_accuracy": confidence_accuracy,
        "sample_status": "insufficient_samples" if mature_count < 10 else "usable",
    }


def decision_label_statistics(outcomes: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [dict(row) for row in outcomes]
    return [
        {
            **_metric(
                "decision_label",
                code,
                [row for row in rows if str(row.get("decision_code") or "observe") == code],
                label=DECISION_LABELS[code],
                success_fn=_decision_success,
            ),
            "success_definition": (
                "风险或回撤实际出现" if code in {"avoid_chase", "risk_alert"}
                else "回踩风险出现或最终收益为正" if code == "wait_pullback"
                else "最终收益为正"
            ),
        }
        for code in DECISION_CODES
    ]


def _lifecycle_result_rows(
    lifecycles: Iterable[dict[str, Any]],
    outcomes: Iterable[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_lifecycle: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for outcome in outcomes:
        by_lifecycle[safe_int(outcome.get("lifecycle_id"))].append(dict(outcome))
    result: list[dict[str, Any]] = []
    for raw in lifecycles:
        item = dict(raw)
        linked = by_lifecycle.get(safe_int(item.get("lifecycle_id")), [])
        successful = [
            row for row in linked
            if str(row.get("data_status") or "") == "success"
            and _number(row.get("final_return_pct")) is not None
        ]
        first_signal = [row for row in successful if str(row.get("link_role") or "") == "first_signal"]
        selected = max(
            first_signal or successful,
            key=lambda row: (HORIZON_RANK.get(str(row.get("horizon") or ""), 0), safe_int(row.get("id"))),
            default=None,
        )
        if selected:
            item.update({
                "data_status": "success",
                "final_return_pct": selected.get("final_return_pct"),
                "max_gain_pct": selected.get("max_gain_pct"),
                "max_drawdown_pct": selected.get("max_drawdown_pct"),
                "result_horizon": selected.get("horizon"),
            })
        elif any(str(row.get("data_status") or "") == "unavailable" for row in linked):
            item["data_status"] = "unavailable"
            item["final_return_pct"] = None
            item["max_gain_pct"] = None
            item["max_drawdown_pct"] = None
        elif any(str(row.get("data_status") or "") == "error" for row in linked):
            item["data_status"] = "error"
        else:
            item["data_status"] = "pending"
        result.append(item)
    return result


def _first_level_statistics(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for level in ("15m", "1h", "4h", "24h", "unknown"):
        group = [row for row in rows if str(row.get("first_signal_level") or "unknown") == level]
        metric = _metric("first_level", level, group, label=level, success_fn=_lifecycle_success)
        rank = LEVEL_RANK.get(level, 0)
        mature = [row for row in group if str(row.get("data_status")) == "success"]
        failures = [row for row in mature if not _lifecycle_success(row)]
        metric.update({
            "upgrade_count": sum(LEVEL_RANK.get(str(row.get("highest_level") or "unknown"), 0) > rank for row in group),
            "upgrade_ratio": _ratio(
                sum(LEVEL_RANK.get(str(row.get("highest_level") or "unknown"), 0) > rank for row in group),
                len(group),
            ),
            "failure_count": len(failures),
            "failure_ratio": _ratio(len(failures), len(mature)),
            "avg_duration_sec": _mean(
                (row.get("duration_sec") for row in mature), digits=1
            ),
        })
        output.append(metric)
    return output


def _upgrade_path_statistics(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[str(row.get("upgrade_path") or row.get("first_signal_level") or "unknown")].append(row)
    output: list[dict[str, Any]] = []
    for path, group in groups.items():
        metric = _metric("upgrade_path", path, group, label=path, success_fn=_lifecycle_success)
        mature = [row for row in group if str(row.get("data_status")) == "success"]
        metric["avg_duration_sec"] = _mean(
            (row.get("duration_sec") for row in mature), digits=1
        )
        metric["risk_warning_count"] = sum(
            safe_int(row.get("risk_warning_count")) > 0 for row in group
        )
        metric["risk_warning_ratio"] = _ratio(
            safe_int(metric["risk_warning_count"]), len(group)
        )
        metric["risk_event_count"] = sum(
            safe_int(row.get("risk_event_count")) > 0 for row in group
        )
        metric["risk_event_ratio"] = _ratio(
            safe_int(metric["risk_event_count"]), len(group)
        )
        output.append(metric)
    return sorted(output, key=lambda item: (-safe_int(item.get("sample_count")), str(item.get("metric_key"))))


def _intelligence_bucket(value: Any) -> str:
    score = max(0.0, min(100.0, _number(value) or 0.0))
    if score < 20:
        return "0-20"
    if score < 40:
        return "20-40"
    if score < 60:
        return "40-60"
    if score < 80:
        return "60-80"
    return "80-100"


def _intelligence_statistics(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for bucket in ("0-20", "20-40", "40-60", "60-80", "80-100"):
        group = [
            row for row in rows
            if _intelligence_bucket(row.get("intelligence_score")) == bucket
        ]
        metric = _metric(
            "intelligence_bucket",
            bucket,
            group,
            label=f"智能评分 {bucket}",
            success_fn=_lifecycle_success,
        )
        mature = [row for row in group if str(row.get("data_status")) == "success"]
        strong_success_count = sum(
            str(row.get("replay_result_label") or "") == "strong_success"
            for row in mature
        )
        metric.update({
            "strong_success_count": strong_success_count,
            "strong_success_ratio": _ratio(strong_success_count, len(mature)),
        })
        output.append(metric)
    return output


def _factor_values(row: dict[str, Any]) -> dict[str, Any]:
    metrics = _json_dict(row.get("metrics_json"))
    price = _number(row.get("final_return_pct"))
    if price is None:
        price = _number(row.get("price_change_from_first_pct"))
    oi = _number(row.get("oi_change_from_first_pct"))
    spot = _number(row.get("spot_cvd_change_from_first"))
    futures = _number(row.get("futures_cvd_change_from_first"))
    funding = _number(row.get("latest_funding_rate"))
    volume_multiplier = _number(metrics.get("volume_multiplier"))
    if volume_multiplier is None and (volume_change := _number(metrics.get("volume_change_pct"))) is not None:
        volume_multiplier = 1.0 + volume_change / 100.0
    return {
        "price": price,
        "oi": oi,
        "spot": spot,
        "futures": futures,
        "funding": funding,
        "volume_multiplier": volume_multiplier,
    }


def _factor_statistics(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    enriched = [{**row, "_factor": _factor_values(row)} for row in rows]

    def oi_key(row: dict[str, Any]) -> str:
        values = row["_factor"]
        if values["oi"] is None or values["price"] is None:
            return "unknown"
        return f"oi_{'up' if values['oi'] >= 0 else 'down'}_price_{'up' if values['price'] >= 0 else 'down'}"

    def cvd_key(row: dict[str, Any]) -> str:
        values = row["_factor"]
        if values["spot"] is None and values["futures"] is None:
            return "unknown"
        spot_positive = values["spot"] is not None and values["spot"] > 0
        futures_positive = values["futures"] is not None and values["futures"] > 0
        if spot_positive and futures_positive:
            return "spot_futures_sync"
        if spot_positive:
            return "spot_only"
        if futures_positive:
            return "futures_only"
        return "cvd_unconfirmed"

    def spot_cvd_key(row: dict[str, Any]) -> str:
        value = row["_factor"]["spot"]
        if value is None:
            return "unknown"
        if value > 0:
            return "confirmed"
        if value < 0:
            return "declining"
        return "unconfirmed"

    def futures_cvd_key(row: dict[str, Any]) -> str:
        value = row["_factor"]["futures"]
        if value is None:
            return "unknown"
        if value > 0:
            return "active_buying"
        if value < 0:
            return "active_selling"
        return "no_direction"

    def volume_key(row: dict[str, Any]) -> str:
        value = row["_factor"]["volume_multiplier"]
        return "unknown" if value is None else "expanded" if value >= 2 else "moderate" if value >= 1 else "weak"

    def funding_key(row: dict[str, Any]) -> str:
        value = row["_factor"]["funding"]
        return "unknown" if value is None else "overheated_positive" if value >= 0.0008 else "overheated_negative" if value <= -0.0008 else "healthy"

    def combination_key(row: dict[str, Any]) -> str:
        oi, cvd, volume = oi_key(row), cvd_key(row), volume_key(row)
        if oi == "oi_up_price_up" and cvd == "spot_futures_sync":
            return "price_oi_cvd_confirmed"
        if oi == "oi_up_price_down":
            return "leverage_divergence"
        if cvd == "futures_only":
            return "futures_without_spot"
        if volume == "expanded" and oi.startswith("oi_up"):
            return "volume_oi_confirmed"
        return "limited_confirmation"

    definitions = {
        "oi_quadrant": oi_key,
        "spot_cvd": spot_cvd_key,
        "futures_cvd": futures_cvd_key,
        "cvd_confirmation": cvd_key,
        "volume_confirmation": volume_key,
        "funding_state": funding_key,
        "factor_combination": combination_key,
    }
    result: dict[str, list[dict[str, Any]]] = {}
    baseline = _metric("factor_baseline", "all", enriched, success_fn=_lifecycle_success)
    for metric_type, classifier in definitions.items():
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in enriched:
            groups[classifier(row)].append(row)
        metrics: list[dict[str, Any]] = []
        for key, group in sorted(groups.items()):
            metric = _metric(
                metric_type,
                key,
                group,
                label=_factor_label(metric_type, key),
                success_fn=_lifecycle_success,
            )
            if metric_type == "oi_quadrant":
                risk_count = sum(safe_int(row.get("risk_event_count")) > 0 for row in group)
                metric.update({
                    "risk_count": risk_count,
                    "risk_ratio": _ratio(risk_count, len(group)),
                })
            if metric_type == "factor_combination":
                success_ratio = _number(metric.get("success_ratio"))
                baseline_success = _number(baseline.get("success_ratio"))
                drawdown = _number(metric.get("avg_max_drawdown_pct"))
                baseline_drawdown = _number(baseline.get("avg_max_drawdown_pct"))
                metric.update({
                    "success_lift": (
                        round(success_ratio - baseline_success, 6)
                        if success_ratio is not None and baseline_success is not None else None
                    ),
                    "drawdown_improvement_pct": (
                        round(drawdown - baseline_drawdown, 4)
                        if drawdown is not None and baseline_drawdown is not None else None
                    ),
                    "baseline_success_ratio": baseline.get("success_ratio"),
                    "baseline_avg_max_drawdown_pct": baseline.get("avg_max_drawdown_pct"),
                })
            metrics.append(metric)
        result[metric_type] = metrics
    return result


def _risk_statistics(
    events: list[dict[str, Any]],
    frames: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
) -> dict[str, Any]:
    by_event: dict[int, list[dict[str, Any]]] = defaultdict(list)
    by_signal: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in outcomes:
        if safe_int(row.get("lifecycle_event_id")) > 0:
            by_event[safe_int(row.get("lifecycle_event_id"))].append(row)
        if safe_int(row.get("signal_id")) > 0:
            by_signal[safe_int(row.get("signal_id"))].append(row)
    frames_by_lifecycle: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for frame in frames:
        frames_by_lifecycle[safe_int(frame.get("lifecycle_id"))].append(frame)
    samples: list[dict[str, Any]] = []
    for event in events:
        linked = by_event.get(safe_int(event.get("id"))) or by_signal.get(safe_int(event.get("signal_id"))) or []
        event_time = _parse_time(event.get("event_time"))
        event_price = _number(event.get("price"))
        later_frames = [
            frame for frame in frames_by_lifecycle.get(safe_int(event.get("lifecycle_id")), [])
            if event_time is not None and (_parse_time(frame.get("event_time")) or 0) > event_time
            and _number(frame.get("price")) is not None
        ]
        lead_time: float | None = None
        frame_drawdown: float | None = None
        if event_price not in (None, 0.0) and later_frames:
            worst = min(later_frames, key=lambda frame: _number(frame.get("price")) or event_price)
            worst_price = _number(worst.get("price"))
            if worst_price is not None:
                frame_drawdown = round((worst_price / event_price - 1.0) * 100.0, 4)
                lead_time = max(0.0, (_parse_time(worst.get("event_time")) or event_time or 0) - (event_time or 0))
        for outcome in linked:
            if str(outcome.get("horizon") or "") not in {"1h", "4h", "24h"}:
                continue
            sample = dict(outcome)
            sample.update({
                "event_type": str(event.get("event_type") or "risk_warning"),
                "risk_event_id": safe_int(event.get("id")),
                "lead_time_sec": lead_time,
            })
            if _number(sample.get("max_drawdown_pct")) is None:
                sample["max_drawdown_pct"] = frame_drawdown
            samples.append(sample)
    items: list[dict[str, Any]] = []
    for event_type in sorted(RISK_EVENT_TYPES):
        group = [row for row in samples if str(row.get("event_type")) == event_type]
        metric = _metric(
            "risk_alert",
            event_type,
            group,
            label=RISK_EVENT_LABELS.get(event_type, event_type),
            success_fn=lambda row: (_number(row.get("final_return_pct")) or 0.0) <= 0
            or (_number(row.get("max_drawdown_pct")) or 0.0) <= -3,
        )
        metric.update({
            "event_count": len({safe_int(row.get("risk_event_id")) for row in group}),
            "avg_return_1h_pct": _mean(row.get("final_return_pct") for row in group if str(row.get("horizon")) == "1h" and str(row.get("data_status")) == "success"),
            "avg_return_4h_pct": _mean(row.get("final_return_pct") for row in group if str(row.get("horizon")) == "4h" and str(row.get("data_status")) == "success"),
            "avg_return_24h_pct": _mean(row.get("final_return_pct") for row in group if str(row.get("horizon")) == "24h" and str(row.get("data_status")) == "success"),
            "avoided_loss_ratio": metric["success_ratio"],
            "avg_lead_time_sec": _mean(
                (row.get("lead_time_sec") for row in group), digits=1
            ),
        })
        items.append(metric)
    all_metric = _metric(
        "risk_alert",
        "all",
        samples,
        label="全部风险事件",
        success_fn=lambda row: (_number(row.get("final_return_pct")) or 0.0) <= 0
        or (_number(row.get("max_drawdown_pct")) or 0.0) <= -3,
    )
    all_metric.update({
        "event_count": len({safe_int(row.get("id")) for row in events}),
        "avg_return_1h_pct": _mean(row.get("final_return_pct") for row in samples if str(row.get("horizon")) == "1h" and str(row.get("data_status")) == "success"),
        "avg_return_4h_pct": _mean(row.get("final_return_pct") for row in samples if str(row.get("horizon")) == "4h" and str(row.get("data_status")) == "success"),
        "avg_return_24h_pct": _mean(row.get("final_return_pct") for row in samples if str(row.get("horizon")) == "24h" and str(row.get("data_status")) == "success"),
        "avoided_loss_ratio": all_metric["success_ratio"],
        "avg_lead_time_sec": _mean(
            (row.get("lead_time_sec") for row in samples), digits=1
        ),
    })
    return {"summary": all_metric, "items": items}


def _source_signature(
    lifecycles: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    events: list[dict[str, Any]],
    frames: list[dict[str, Any]] | None = None,
) -> str:
    source = {
        # These projections are already bounded and contain no raw signal text or
        # secrets. Hash every analytical input so replay/intelligence changes
        # cannot accidentally reuse an older report.
        "lifecycles": lifecycles,
        "outcomes": outcomes,
        "events": events,
        "frames": list(frames or []),
    }
    encoded = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _all_metrics(report: dict[str, Any]) -> list[dict[str, Any]]:
    metrics: list[dict[str, Any]] = []
    metrics.extend(report.get("decision_labels") or [])
    metrics.extend(report.get("first_levels") or [])
    metrics.extend(report.get("upgrade_paths") or [])
    metrics.extend(report.get("intelligence_buckets") or [])
    for items in (report.get("factors") or {}).values():
        if isinstance(items, list):
            metrics.extend(items)
    risk = report.get("risk_alerts") or {}
    if isinstance(risk, dict):
        if isinstance(risk.get("summary"), dict):
            metrics.append(risk["summary"])
        metrics.extend(item for item in (risk.get("items") or []) if isinstance(item, dict))
    return [dict(item) for item in metrics if isinstance(item, dict) and item.get("metric_type")]


def evaluate_calibration_validation_readiness(
    report: dict[str, Any],
    *,
    settings: Settings | Any | None = None,
    base_readiness: dict[str, Any] | None = None,
) -> dict[str, Any]:
    loaded = settings or Settings.load()
    summary = dict(report.get("summary") or {})
    decisions = list(report.get("decision_labels") or [])
    minimum_mature = max(1, safe_int(getattr(loaded, "model_calibration_min_mature_samples", 50), 50))
    minimum_group = max(1, safe_int(getattr(loaded, "model_calibration_min_group_samples", 10), 10))
    minimum_stable_groups = max(1, safe_int(getattr(loaded, "model_calibration_min_stable_groups", 2), 2))
    stable_groups = sum(safe_int(item.get("mature_sample_count")) >= minimum_group for item in decisions)
    base = dict(base_readiness or {"ready": True, "blocked": []})
    base_current = dict(base.get("current") or {})
    base_required = dict(base.get("required") or {})
    checks = {
        "outcome_quality_gate": bool(base.get("ready")),
        "mature_samples": safe_int(summary.get("mature_sample_count")) >= minimum_mature,
        "stable_decision_groups": stable_groups >= minimum_stable_groups,
        "has_lifecycle_samples": safe_int(summary.get("mature_lifecycle_count")) > 0,
    }
    passed = [key for key, value in checks.items() if value]
    blocked = [key for key, value in checks.items() if not value]
    warnings: list[str] = []
    if safe_int(summary.get("unavailable_count")):
        warnings.append("unavailable 样本已单独统计，不进入成功率或收益分母。")
    if stable_groups < len(DECISION_CODES):
        warnings.append("部分决策标签的成熟样本仍不足，分组结论仅供研究。")
    return {
        "ready": not blocked,
        "label": "达到模型校准验证条件" if not blocked else "暂未达到模型校准验证条件",
        "passed": passed,
        "blocked": blocked,
        "warnings": warnings,
        "current": {
            **base_current,
            "mature_samples": safe_int(summary.get("mature_sample_count")),
            "stable_decision_groups": stable_groups,
            "mature_lifecycle_count": safe_int(summary.get("mature_lifecycle_count")),
            "base_gate_ready": bool(base.get("ready")),
        },
        "required": {
            **base_required,
            "mature_samples": minimum_mature,
            "decision_group_samples": minimum_group,
            "stable_decision_groups": minimum_stable_groups,
            "base_gate_ready": True,
        },
        "base_readiness": base,
        "does_not_modify_model": True,
        "note": "仅验证样本成熟度与稳定性，不会自动修改模型阈值或权重。",
    }


def _recommendations(report: dict[str, Any], readiness: dict[str, Any]) -> list[dict[str, Any]]:
    recommendations: list[dict[str, Any]] = []
    if not readiness.get("ready"):
        recommendations.append({
            "key": "accumulate_mature_samples",
            "priority": "high",
            "recommendation": "继续积累已关联、已到期且 success 的 Outcome，再进行模型校准验证。",
            "evidence": list(readiness.get("blocked") or []),
            "action": "review_only",
            "auto_apply": False,
        })
    for item in report.get("decision_labels") or []:
        if safe_int(item.get("mature_sample_count")) < 10:
            continue
        if (_number(item.get("success_ratio")) or 0.0) < 0.45 or (_number(item.get("expectancy_pct")) or 0.0) < 0:
            recommendations.append({
                "key": f"review_decision_{item.get('metric_key')}",
                "priority": "medium",
                "recommendation": f"建议人工复核“{item.get('label')}”历史表现；不自动调整阈值。",
                "evidence": {
                    "mature_sample_count": item.get("mature_sample_count"),
                    "success_ratio": item.get("success_ratio"),
                    "expectancy_pct": item.get("expectancy_pct"),
                },
                "action": "review_only",
                "auto_apply": False,
            })
    anomaly_recommendations = {
        "high_intelligence_bucket_not_better": (
            "review_intelligence_bucket_calibration",
            "高智能评分组未显著优于较低评分组，建议人工复核评分分层；不自动修改权重。",
        ),
        "low_risk_alert_effectiveness": (
            "review_risk_alert_definition",
            "风险提示后的损失规避有效率偏低，建议人工复核风险事件定义；不自动修改阈值。",
        ),
        "low_confidence_accuracy": (
            "review_decision_confidence",
            "部分决策置信度与实际结果一致性偏低，建议人工复核置信度口径。",
        ),
    }
    seen_keys = {str(item.get("key") or "") for item in recommendations}
    for anomaly in report.get("anomalies") or []:
        mapped = anomaly_recommendations.get(str(anomaly.get("key") or ""))
        if not mapped or mapped[0] in seen_keys:
            continue
        key, message = mapped
        recommendations.append({
            "key": key,
            "priority": "medium",
            "recommendation": message,
            "evidence": dict(anomaly),
            "action": "review_only",
            "auto_apply": False,
        })
        seen_keys.add(key)
    if not recommendations:
        recommendations.append({
            "key": "continue_validation",
            "priority": "low",
            "recommendation": "当前统计未发现需要立即人工复核的明显异常，继续观察样本稳定性。",
            "evidence": {"mature_sample_count": (report.get("summary") or {}).get("mature_sample_count")},
            "action": "review_only",
            "auto_apply": False,
        })
    return recommendations


def _findings_anomalies(report: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []
    anomalies: list[dict[str, Any]] = []
    usable_decisions = [
        item for item in report.get("decision_labels") or []
        if safe_int(item.get("mature_sample_count")) >= 10
        and _number(item.get("success_ratio")) is not None
    ]
    if usable_decisions:
        strongest = max(usable_decisions, key=lambda item: _number(item.get("success_ratio")) or 0.0)
        weakest = min(usable_decisions, key=lambda item: _number(item.get("success_ratio")) or 0.0)
        findings.append({
            "key": "strongest_decision_label",
            "label": strongest.get("label"),
            "success_ratio": strongest.get("success_ratio"),
            "mature_sample_count": strongest.get("mature_sample_count"),
        })
        findings.append({
            "key": "weakest_decision_label",
            "label": weakest.get("label"),
            "success_ratio": weakest.get("success_ratio"),
            "mature_sample_count": weakest.get("mature_sample_count"),
        })
    combinations = [
        item for item in ((report.get("factors") or {}).get("combinations") or [])
        if safe_int(item.get("mature_sample_count")) >= 10
        and _number(item.get("success_lift")) is not None
    ]
    if combinations:
        strongest_factor = max(combinations, key=lambda item: _number(item.get("success_lift")) or 0.0)
        weakest_factor = min(combinations, key=lambda item: _number(item.get("success_lift")) or 0.0)
        findings.extend([
            {
                "key": "strongest_factor_combination",
                "factor": strongest_factor.get("metric_key"),
                "success_lift": strongest_factor.get("success_lift"),
                "drawdown_improvement_pct": strongest_factor.get("drawdown_improvement_pct"),
                "mature_sample_count": strongest_factor.get("mature_sample_count"),
            },
            {
                "key": "weakest_factor_combination",
                "factor": weakest_factor.get("metric_key"),
                "success_lift": weakest_factor.get("success_lift"),
                "drawdown_improvement_pct": weakest_factor.get("drawdown_improvement_pct"),
                "mature_sample_count": weakest_factor.get("mature_sample_count"),
            },
        ])
    intelligence = {
        str(item.get("metric_key")): item
        for item in report.get("intelligence_buckets") or []
        if safe_int(item.get("mature_sample_count")) >= 10
        and _number(item.get("success_ratio")) is not None
    }
    high_bucket = intelligence.get("80-100")
    lower_candidates = [
        intelligence[key] for key in ("0-20", "20-40", "40-60", "60-80")
        if key in intelligence
    ]
    if high_bucket and lower_candidates:
        lower_weighted = sum(
            (_number(item.get("success_ratio")) or 0.0) * safe_int(item.get("mature_sample_count"))
            for item in lower_candidates
        ) / max(1, sum(safe_int(item.get("mature_sample_count")) for item in lower_candidates))
        high_ratio = _number(high_bucket.get("success_ratio")) or 0.0
        findings.append({
            "key": "intelligence_bucket_ordering",
            "high_bucket_success_ratio": high_ratio,
            "lower_bucket_weighted_success_ratio": round(lower_weighted, 6),
            "high_bucket_lift": round(high_ratio - lower_weighted, 6),
        })
        if high_ratio <= lower_weighted:
            anomalies.append({
                "key": "high_intelligence_bucket_not_better",
                "high_bucket_success_ratio": high_ratio,
                "lower_bucket_weighted_success_ratio": round(lower_weighted, 6),
                "severity": "review",
            })
    risk_summary = dict((report.get("risk_alerts") or {}).get("summary") or {})
    if safe_int(risk_summary.get("mature_sample_count")) >= 10:
        avoided = _number(risk_summary.get("avoided_loss_ratio"))
        findings.append({
            "key": "risk_alert_effectiveness",
            "avoided_loss_ratio": avoided,
            "event_count": safe_int(risk_summary.get("event_count")),
            "mature_sample_count": safe_int(risk_summary.get("mature_sample_count")),
            "avg_lead_time_sec": risk_summary.get("avg_lead_time_sec"),
        })
        if avoided is not None and avoided < 0.5:
            anomalies.append({
                "key": "low_risk_alert_effectiveness",
                "avoided_loss_ratio": avoided,
                "severity": "review",
            })
    for item in usable_decisions:
        accuracy = _number(item.get("confidence_accuracy"))
        if accuracy is not None and accuracy < 0.5:
            anomalies.append({
                "key": "low_confidence_accuracy",
                "metric_key": item.get("metric_key"),
                "confidence_accuracy": accuracy,
                "severity": "review",
            })
    unavailable = safe_int((report.get("summary") or {}).get("unavailable_count"))
    samples = safe_int((report.get("summary") or {}).get("sample_count"))
    if samples and unavailable / samples >= 0.1:
        anomalies.append({
            "key": "high_unavailable_ratio",
            "ratio": round(unavailable / samples, 6),
            "severity": "data_quality",
        })
    if not findings:
        findings.append({
            "key": "insufficient_group_samples",
            "message": "分组成熟样本仍不足，暂不比较标签优劣。",
        })
    return findings, anomalies


def build_calibration_report(
    lifecycles: Iterable[dict[str, Any]],
    outcomes: Iterable[dict[str, Any]],
    *,
    events: Iterable[dict[str, Any]] = (),
    frames: Iterable[dict[str, Any]] = (),
    settings: Settings | Any | None = None,
    base_readiness: dict[str, Any] | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    lifecycle_rows = [dict(row) for row in lifecycles]
    outcome_rows = [dict(row) for row in outcomes]
    event_rows = [dict(row) for row in events]
    frame_rows = [dict(row) for row in frames]
    risk_counts: dict[int, int] = defaultdict(int)
    risk_warning_counts: dict[int, int] = defaultdict(int)
    for event in event_rows:
        lifecycle_id = safe_int(event.get("lifecycle_id"))
        risk_counts[lifecycle_id] += 1
        if str(event.get("event_type") or "") == "risk_warning":
            risk_warning_counts[lifecycle_id] += 1
    lifecycle_rows = [
        {
            **row,
            "risk_event_count": risk_counts.get(safe_int(row.get("lifecycle_id")), 0),
            "risk_warning_count": risk_warning_counts.get(safe_int(row.get("lifecycle_id")), 0),
        }
        for row in lifecycle_rows
    ]
    lifecycle_results = _lifecycle_result_rows(lifecycle_rows, outcome_rows)
    mature_outcomes = [
        row for row in outcome_rows
        if str(row.get("data_status") or "") == "success" and _number(row.get("final_return_pct")) is not None
    ]
    mature_lifecycles = [row for row in lifecycle_results if str(row.get("data_status")) == "success"]
    decision = decision_label_statistics(outcome_rows)
    factors = _factor_statistics(lifecycle_results)
    report: dict[str, Any] = {
        "ok": True,
        "calibration_version": CALIBRATION_VERSION,
        "model_version": CALIBRATION_MODEL_VERSION,
        "generated_at": generated_at or _now(),
        "summary": {
            "sample_count": len(outcome_rows),
            "mature_sample_count": len(mature_outcomes),
            "unavailable_count": sum(str(row.get("data_status")) == "unavailable" for row in outcome_rows),
            "pending_count": sum(str(row.get("data_status")) in {"pending", "ready"} for row in outcome_rows),
            "error_count": sum(str(row.get("data_status")) == "error" for row in outcome_rows),
            "maturity_ratio": _ratio(len(mature_outcomes), len(outcome_rows)),
            "lifecycle_sample_count": len(lifecycle_results),
            "mature_lifecycle_count": len(mature_lifecycles),
            "lifecycle_maturity_ratio": _ratio(len(mature_lifecycles), len(lifecycle_results)),
            "decision_group_count": len(DECISION_CODES),
        },
        "decision_labels": decision,
        "decision": {"items": decision},
        "first_levels": _first_level_statistics(lifecycle_results),
        "upgrade_paths": _upgrade_path_statistics(lifecycle_results),
        "intelligence_buckets": _intelligence_statistics(lifecycle_results),
        "factors": {
            "oi_quadrants": factors["oi_quadrant"],
            "spot_cvd": factors["spot_cvd"],
            "futures_cvd": factors["futures_cvd"],
            "spot_futures_cvd": factors["cvd_confirmation"],
            "volume": factors["volume_confirmation"],
            "funding": factors["funding_state"],
            "combinations": factors["factor_combination"],
        },
        "risk_alerts": _risk_statistics(event_rows, frame_rows, outcome_rows),
        "not_advice": NOT_ADVICE,
        "does_not_modify_model": True,
    }
    report["lifecycle"] = {
        "first_levels": report["first_levels"],
        "upgrade_paths": report["upgrade_paths"],
        "intelligence_buckets": report["intelligence_buckets"],
    }
    report["risk"] = report["risk_alerts"]
    readiness = evaluate_calibration_validation_readiness(
        report,
        settings=settings,
        base_readiness=base_readiness,
    )
    report["readiness"] = readiness
    report["status"] = "ready_for_validation" if readiness["ready"] else "insufficient_samples"
    findings, anomalies = _findings_anomalies(report)
    report["findings"] = findings
    report["anomalies"] = anomalies
    report["recommendations"] = _recommendations(report, readiness)
    return report


def _stored_report_payload(stored: dict[str, Any]) -> dict[str, Any]:
    envelope = dict(stored.get("summary") or {})
    report = {
        "ok": True,
        "calibration_version": str(stored.get("report_version") or CALIBRATION_VERSION),
        "model_version": str(stored.get("model_version") or CALIBRATION_MODEL_VERSION),
        "generated_at": str(stored.get("generated_at") or ""),
        "source_signature": str(stored.get("source_signature") or ""),
        "status": str(envelope.get("status") or "insufficient_samples"),
        "summary": dict(envelope.get("summary") or {}),
        "readiness": dict(envelope.get("readiness") or {}),
        "findings": list(envelope.get("findings") or []),
        "anomalies": list(envelope.get("anomalies") or []),
        "recommendations": list(stored.get("recommendations") or []),
        "not_advice": NOT_ADVICE,
        "does_not_modify_model": True,
    }
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for metric in stored.get("metrics") or []:
        if isinstance(metric, dict):
            groups[str(metric.get("metric_type") or "")].append(metric)
    report["decision_labels"] = groups["decision_label"]
    report["decision"] = {"items": report["decision_labels"]}
    report["first_levels"] = groups["first_level"]
    report["upgrade_paths"] = groups["upgrade_path"]
    report["intelligence_buckets"] = groups["intelligence_bucket"]
    report["lifecycle"] = {
        "first_levels": report["first_levels"],
        "upgrade_paths": report["upgrade_paths"],
        "intelligence_buckets": report["intelligence_buckets"],
    }
    report["factors"] = {
        "oi_quadrants": groups["oi_quadrant"],
        "spot_cvd": groups["spot_cvd"],
        "futures_cvd": groups["futures_cvd"],
        "spot_futures_cvd": groups["cvd_confirmation"],
        "volume": groups["volume_confirmation"],
        "funding": groups["funding_state"],
        "combinations": groups["factor_combination"],
    }
    risk_items = groups["risk_alert"]
    report["risk_alerts"] = {
        "summary": next((item for item in risk_items if item.get("metric_key") == "all"), {}),
        "items": [item for item in risk_items if item.get("metric_key") != "all"],
    }
    report["risk"] = report["risk_alerts"]
    return report


def get_calibration_report(settings: Settings | None = None) -> dict[str, Any]:
    loaded = settings or Settings.load()
    path = Path(loaded.lifecycle_db_path)
    conn = _readonly(path)
    if conn is None:
        return {"ok": False, "available": False, "error": "calibration_report_unavailable"}
    try:
        tables = _tables(conn)
        if not {"calibration_reports", "calibration_metrics"}.issubset(tables):
            return {"ok": False, "available": False, "error": "calibration_report_unavailable"}
        store = IntelligenceStore(path)
        stored = store.latest_calibration_report(
            report_version=CALIBRATION_VERSION,
            model_version=CALIBRATION_MODEL_VERSION,
            conn=conn,
        )
    finally:
        conn.close()
    if not stored:
        return {"ok": False, "available": False, "error": "calibration_report_unavailable"}
    return {**_stored_report_payload(stored), "available": True, "cached": True}


def write_calibration_report_files(
    report: dict[str, Any],
    *,
    json_path: Path = CALIBRATION_REPORT_JSON,
    markdown_path: Path = CALIBRATION_REPORT_MD,
) -> dict[str, str]:
    safe_report = {
        key: report.get(key)
        for key in (
            "ok", "calibration_version", "model_version", "generated_at", "status", "summary",
            "decision_labels", "first_levels", "upgrade_paths", "intelligence_buckets",
            "factors", "risk_alerts", "recommendations", "readiness", "not_advice",
            "findings", "anomalies", "does_not_modify_model",
        )
    }
    atomic_write_text(json_path, json.dumps(safe_report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    summary = dict(report.get("summary") or {})
    readiness = dict(report.get("readiness") or {})
    lines = [
        "# Model Calibration Validation",
        "",
        f"- Calibration version: {report.get('calibration_version')}",
        f"- Model version: {report.get('model_version')}",
        f"- Generated: {report.get('generated_at')}",
        f"- Outcome samples: {summary.get('sample_count', 0)}",
        f"- Mature samples: {summary.get('mature_sample_count', 0)}",
        f"- Mature lifecycles: {summary.get('mature_lifecycle_count', 0)}",
        f"- Readiness: {readiness.get('label', '')}",
        "",
        "Only linked, due, successful Outcome samples enter return statistics.",
        "Unavailable, pending, and not-due records are not failures or losses.",
        "This report only proposes manual review and never changes model parameters.",
    ]

    def add_metric_section(title: str, items: Iterable[dict[str, Any]]) -> None:
        rows = [dict(item) for item in items if isinstance(item, dict)]
        lines.extend([
            "",
            f"## {title}",
            "",
            "| Group | Samples | Mature | Success ratio | Avg return | Avg drawdown |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ])
        if not rows:
            lines.append("| insufficient samples | 0 | 0 | - | - | - |")
            return
        for item in rows:
            ratio = item.get("success_ratio")
            avg_return = item.get("avg_return_pct")
            drawdown = item.get("avg_max_drawdown_pct")
            lines.append(
                "| {label} | {samples} | {mature} | {ratio} | {avg_return} | {drawdown} |".format(
                    label=str(item.get("label") or item.get("metric_key") or "-").replace("|", "/"),
                    samples=safe_int(item.get("sample_count")),
                    mature=safe_int(item.get("mature_sample_count")),
                    ratio="-" if ratio is None else f"{float(ratio):.2%}",
                    avg_return="-" if avg_return is None else f"{float(avg_return):.4f}%",
                    drawdown="-" if drawdown is None else f"{float(drawdown):.4f}%",
                )
            )

    add_metric_section("Decision Validation", report.get("decision_labels") or [])
    add_metric_section("First Signal Level", report.get("first_levels") or [])
    add_metric_section("Upgrade Path", report.get("upgrade_paths") or [])
    add_metric_section("Intelligence Buckets", report.get("intelligence_buckets") or [])
    for factor_name, factor_items in (report.get("factors") or {}).items():
        add_metric_section(f"Factor: {factor_name}", factor_items or [])
    risk = report.get("risk_alerts") or {}
    add_metric_section("Risk Alert Validation", risk.get("items") or [])
    lines.extend(["", "## Findings", ""])
    for item in report.get("findings") or []:
        lines.append(f"- `{item.get('key', 'finding')}`: {json.dumps(item, ensure_ascii=False, sort_keys=True)}")
    lines.extend(["", "## Anomalies", ""])
    if report.get("anomalies"):
        for item in report.get("anomalies") or []:
            lines.append(f"- `{item.get('key', 'anomaly')}`: {json.dumps(item, ensure_ascii=False, sort_keys=True)}")
    else:
        lines.append("- No material anomaly detected in mature samples.")
    lines.extend(["", "## Review-only Recommendations", ""])
    for item in report.get("recommendations") or []:
        lines.append(
            f"- **{item.get('priority', 'low')}** `{item.get('key', 'review')}`: "
            f"{item.get('recommendation', '')} (auto_apply={bool(item.get('auto_apply'))})"
        )
    lines.extend([
        "",
        "## Boundaries",
        "",
        "- Source databases are read-only inputs; this report does not request external market data.",
        "- Recommendations require manual review and are never applied automatically.",
        f"- {report.get('not_advice') or NOT_ADVICE}",
    ])
    atomic_write_text(markdown_path, "\n".join(lines) + "\n")
    return {"json": str(json_path), "markdown": str(markdown_path)}


def generate_calibration_report(
    settings: Settings | None = None,
    *,
    symbol: str = "",
    limit: int | None = None,
    dry_run: bool = False,
    force: bool = False,
    write_reports: bool = True,
) -> dict[str, Any]:
    loaded = settings or Settings.load()
    normalized_symbol = normalize_lifecycle_symbol(symbol)
    if symbol and not normalized_symbol:
        return {"ok": False, "error": "invalid_symbol", "dry_run": bool(dry_run)}
    sources = _lifecycle_sources(Path(loaded.lifecycle_db_path))
    lifecycles = list(sources["lifecycles"])
    if normalized_symbol:
        lifecycles = [row for row in lifecycles if normalize_lifecycle_symbol(row.get("symbol")) == normalized_symbol]
    if limit is not None:
        safe_limit = max(1, min(safe_int(limit, 10000), 10000))
        lifecycles = lifecycles[:safe_limit]
    lifecycle_ids = {safe_int(row.get("lifecycle_id")) for row in lifecycles}
    links = [row for row in sources["links"] if safe_int(row.get("lifecycle_id")) in lifecycle_ids]
    events = [row for row in sources["events"] if safe_int(row.get("lifecycle_id")) in lifecycle_ids]
    frames = [row for row in sources["frames"] if safe_int(row.get("lifecycle_id")) in lifecycle_ids]
    outcomes = _outcome_rows(Path(loaded.outcome_db_path), links)
    signature = _source_signature(lifecycles, outcomes, events, frames)
    global_scope = not normalized_symbol and limit is None
    if global_scope and not dry_run and not force:
        cached = get_calibration_report(loaded)
        if cached.get("ok") and str(cached.get("source_signature") or "") == signature:
            result = {**cached, "skipped": True, "scope": {"global": True}}
            if write_reports:
                result["report_files"] = write_calibration_report_files(result)
            return result
    try:
        base_readiness = lifecycle_calibration_readiness(loaded, write_reports=False)
    except Exception as exc:
        base_readiness = {
            "ready": False,
            "blocked": ["outcome_quality_gate_unavailable"],
            "warnings": [f"{type(exc).__name__}: {exc}"[:160]],
        }
    report = build_calibration_report(
        lifecycles,
        outcomes,
        events=events,
        frames=frames,
        settings=loaded,
        base_readiness=base_readiness,
    )
    report.update({
        "source_signature": signature,
        "dry_run": bool(dry_run),
        "scope": {"global": global_scope, "symbol": normalized_symbol or None, "limit": limit},
        "skipped": False,
    })
    if dry_run or not global_scope:
        report["persisted"] = False
        return report
    store = IntelligenceStore(loaded)
    stored = store.write_calibration_report(
        {
            "report_version": CALIBRATION_VERSION,
            "model_version": CALIBRATION_MODEL_VERSION,
            "generated_at": report["generated_at"],
            "sample_count": (report.get("summary") or {}).get("sample_count"),
            "mature_sample_count": (report.get("summary") or {}).get("mature_sample_count"),
            "summary": {
                "summary": report.get("summary"),
                "readiness": report.get("readiness"),
                "status": report.get("status"),
                "findings": report.get("findings"),
                "anomalies": report.get("anomalies"),
            },
            "recommendations": report.get("recommendations"),
            "source_signature": signature,
        },
        _all_metrics(report),
    )
    report["report_id"] = stored["id"]
    report["persisted"] = True
    if write_reports:
        report["report_files"] = write_calibration_report_files(report)
    return report


def calibration_validation_readiness(
    settings: Settings | None = None,
    *,
    report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    loaded = settings or Settings.load()
    current = report or get_calibration_report(loaded)
    if not current.get("ok"):
        current = generate_calibration_report(loaded, dry_run=True, write_reports=False)
    try:
        base = lifecycle_calibration_readiness(loaded, write_reports=False)
    except Exception:
        base = {"ready": False, "blocked": ["outcome_quality_gate_unavailable"]}
    return evaluate_calibration_validation_readiness(current, settings=loaded, base_readiness=base)


# Stable aliases used by CLI/web adapters.
generate_model_calibration = generate_calibration_report
get_model_calibration_report = get_calibration_report
model_calibration_readiness = calibration_validation_readiness


__all__ = [
    "CALIBRATION_MODEL_VERSION",
    "CALIBRATION_VERSION",
    "build_calibration_report",
    "calibration_validation_readiness",
    "decision_label_statistics",
    "evaluate_calibration_validation_readiness",
    "generate_calibration_report",
    "generate_model_calibration",
    "get_calibration_report",
    "get_model_calibration_report",
    "model_calibration_readiness",
    "write_calibration_report_files",
]
