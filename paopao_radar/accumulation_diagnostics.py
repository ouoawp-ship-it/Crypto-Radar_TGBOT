from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from .storage import JsonStore


SCHEMA_VERSION = 1
HISTORY_LIMIT = 48

REASON_TEXT = {
    "passed": "通过收筹质量门禁",
    "insufficient_history": "完整日线历史不足",
    "invalid_or_missing_candles": "日线数据缺失或包含无效K线",
    "range_exceeded": "横盘区间超过上限",
    "slope_exceeded": "趋势斜率超过上限",
    "baseline_volume_exceeded": "横盘基线平均成交额超过上限",
    "recent_price_gain_exceeded": "近期价格涨幅超过上限",
    "invalid_metrics": "质量指标缺失或无效",
    "evaluation_error": "质量评估发生异常",
}

_EXCLUSION_REASON_CODES = {
    "range_too_wide": "range_exceeded",
    "trend_too_strong": "slope_exceeded",
    "baseline_volume_too_high": "baseline_volume_exceeded",
    "recent_price_already_extended": "recent_price_gain_exceeded",
    "insufficient_baseline": "invalid_metrics",
    "invalid_metrics": "invalid_metrics",
    "evaluation_error": "evaluation_error",
}


def _finite_or_none(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _int_or_zero(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _schema_matches(value: Any) -> bool:
    try:
        return int(value) == SCHEMA_VERSION
    except (TypeError, ValueError):
        return False


def _reason_code(
    quality: dict[str, Any],
    *,
    input_row_count: int,
    evaluation_error: bool,
) -> str:
    if evaluation_error:
        return "evaluation_error"
    if bool(quality.get("eligible")):
        return "passed"
    exclusion = str(quality.get("exclusion_reason") or "").strip()
    if exclusion == "insufficient_history":
        available = _int_or_zero(quality.get("history_days"))
        required = _int_or_zero(quality.get("required_history_days"))
        if (
            input_row_count <= 0
            or available <= 0 < input_row_count
            or (required > 0 and input_row_count >= required > available)
        ):
            return "invalid_or_missing_candles"
        return "insufficient_history"
    return _EXCLUSION_REASON_CODES.get(exclusion, "invalid_metrics")


def build_evaluation_result(
    symbol: str,
    quality: dict[str, Any],
    *,
    input_row_count: int,
    dark_flow_candidate: bool,
    evaluated_at: int,
    evaluation_error: bool = False,
) -> dict[str, Any]:
    code = _reason_code(
        quality,
        input_row_count=max(0, int(input_row_count)),
        evaluation_error=bool(evaluation_error),
    )
    available = _int_or_zero(quality.get("history_days"))
    required = _int_or_zero(quality.get("required_history_days"))
    metrics_available = available >= required > 0 and code not in {
        "invalid_or_missing_candles",
        "invalid_metrics",
        "evaluation_error",
    }

    return {
        "symbol": str(symbol or "").strip().upper(),
        "eligible": bool(quality.get("eligible")) and not evaluation_error,
        "reason_code": code,
        "reason_text": REASON_TEXT[code],
        "evaluated_at": (
            _int_or_zero(quality.get("observed_at"))
            or _int_or_zero(evaluated_at)
        ),
        "available_history_days": available,
        "required_history_days": required,
        "sideways_days": (
            _int_or_zero(quality.get("sideways_days"))
            if metrics_available
            else None
        ),
        "range_pct": (
            _finite_or_none(quality.get("range_pct"))
            if metrics_available
            else None
        ),
        "slope_pct": (
            _finite_or_none(quality.get("slope_pct"))
            if metrics_available
            else None
        ),
        "baseline_avg_quote_volume": (
            _finite_or_none(quality.get("average_daily_quote_volume"))
            if metrics_available
            else None
        ),
        "recent_volume_ratio": (
            _finite_or_none(quality.get("recent_volume_ratio"))
            if metrics_available
            else None
        ),
        "recent_price_gain_pct": (
            _finite_or_none(quality.get("recent_price_gain_pct"))
            if metrics_available
            else None
        ),
        "dark_flow_candidate": bool(dark_flow_candidate),
        "data_source": str(quality.get("data_source") or "").strip() or None,
    }


def build_scan_summary(
    items: list[dict[str, Any]],
    *,
    scan_id: str,
    scan_started_at: int,
    scan_completed_at: int,
    duration_sec: float,
    feature_enabled: bool,
) -> dict[str, Any]:
    raw_results = [
        dict(result)
        for item in items
        for result in [item.get("accumulation_quality_diagnostic")]
        if isinstance(result, dict)
    ]
    results: list[dict[str, Any]] = []
    for result in raw_results:
        eligible = bool(result.get("eligible"))
        code = str(result.get("reason_code") or "")
        if eligible:
            code = "passed"
        elif code not in REASON_TEXT or code == "passed":
            code = "invalid_metrics"
        result["reason_code"] = code
        result["reason_text"] = REASON_TEXT[code]
        results.append(result)
    passed = [result for result in results if bool(result.get("eligible"))]
    rejected = [result for result in results if not bool(result.get("eligible"))]
    reason_counts: dict[str, int] = {}
    for result in rejected:
        code = str(result.get("reason_code") or "invalid_metrics")
        reason_counts[code] = reason_counts.get(code, 0) + 1
    dark_results = [
        result for result in results if bool(result.get("dark_flow_candidate"))
    ]
    dark_passed = [
        result for result in dark_results if bool(result.get("eligible"))
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "scan_id": str(scan_id),
        "scan_started_at": int(scan_started_at),
        "scan_completed_at": int(scan_completed_at),
        "duration_sec": round(max(0.0, float(duration_sec)), 3),
        "feature_enabled": bool(feature_enabled),
        "scanned_market_count": len(items),
        "evaluated_count": len(results),
        "passed_count": len(passed),
        "rejected_count": len(rejected),
        "reason_counts": dict(sorted(reason_counts.items())),
        "dark_flow_evaluated_count": len(dark_results),
        "dark_flow_passed_count": len(dark_passed),
        "dark_flow_rejected_count": len(dark_results) - len(dark_passed),
        "results": results,
    }


def persist_scan_summary(
    store: JsonStore,
    path: Path,
    summary: dict[str, Any],
    *,
    history_limit: int = HISTORY_LIMIT,
) -> dict[str, Any]:
    scan_id = str(summary.get("scan_id") or "")
    limit = max(1, int(history_limit))

    def update(current: Any) -> dict[str, Any]:
        scans = (
            list(current.get("scans") or [])
            if isinstance(current, dict)
            and _schema_matches(current.get("schema_version"))
            else []
        )
        scans = [
            scan for scan in scans
            if isinstance(scan, dict) and str(scan.get("scan_id") or "") != scan_id
        ]
        scans.append(dict(summary))
        return {
            "schema_version": SCHEMA_VERSION,
            "scans": scans[-limit:],
        }

    return store.update(path, update, {"schema_version": SCHEMA_VERSION, "scans": []})


def summary_log_line(summary: dict[str, Any]) -> str:
    reasons = json.dumps(
        summary.get("reason_counts") or {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        "accumulation_quality_summary "
        f"evaluated={int(summary.get('evaluated_count') or 0)} "
        f"passed={int(summary.get('passed_count') or 0)} "
        f"rejected={int(summary.get('rejected_count') or 0)} "
        f"reasons={reasons}"
    )


def scan_summary_without_results(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in summary.items()
        if key != "results"
    }


__all__ = [
    "HISTORY_LIMIT",
    "REASON_TEXT",
    "SCHEMA_VERSION",
    "build_evaluation_result",
    "build_scan_summary",
    "persist_scan_summary",
    "scan_summary_without_results",
    "summary_log_line",
]
