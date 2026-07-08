from __future__ import annotations

import sqlite3
import statistics
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .outcome_tracker import OUTCOME_WINDOWS
from .web_services.api_core import clamp, redact_api_payload


DECISION_LABELS = {
    "observe": "观察",
    "wait_pullback": "等待回踩",
    "probe": "可试仓",
    "avoid_chase": "禁止追高",
    "risk_alert": "风险警报",
    "unknown": "未识别",
}

CONFIDENCE_BUCKETS = (
    ("low", "低置信", 0, 39),
    ("mid_low", "中低置信", 40, 59),
    ("mid_high", "中高置信", 60, 74),
    ("high", "高置信", 75, 100),
)

VALID_BACKTEST_HORIZONS = {"all", *OUTCOME_WINDOWS.keys()}


def _iso(ts: int | float | None = None) -> str:
    value = int(time.time() if ts is None else ts)
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return int(default)
        return int(float(str(value)))
    except (TypeError, ValueError):
        return int(default)


def normalize_horizon(value: Any) -> str:
    horizon = str(value or "all").strip().lower() or "all"
    return horizon if horizon in VALID_BACKTEST_HORIZONS else "all"


def decision_label(code: str, fallback: str = "") -> str:
    normalized = str(code or "").strip() or "unknown"
    return DECISION_LABELS.get(normalized, fallback or normalized)


def confidence_bucket(value: Any) -> dict[str, str]:
    number = _safe_int(value, -1)
    if number < 0:
        return {"code": "unknown", "label": "未识别"}
    for code, label, minimum, maximum in CONFIDENCE_BUCKETS:
        if minimum <= number <= maximum:
            return {"code": code, "label": label}
    return {"code": "high", "label": "高置信"}


def sample_quality(success_count: int, coverage_ratio: float) -> str:
    if success_count < 10:
        return "样本不足"
    if success_count >= 50 and coverage_ratio >= 0.7:
        return "较可信"
    if success_count >= 20 and coverage_ratio >= 0.5:
        return "可参考"
    if success_count >= 10 and coverage_ratio < 0.4:
        return "观察中"
    return "观察中"


def _round(value: float | None, digits: int = 4) -> float | None:
    return round(float(value), digits) if value is not None else None


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _median(values: list[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def _base_group(key: str, label: str) -> dict[str, Any]:
    return {
        "key": str(key or "unknown"),
        "label": str(label or key or "未识别"),
        "total_count": 0,
        "success_count": 0,
        "pending_count": 0,
        "unavailable_count": 0,
        "error_count": 0,
        "_final_returns": [],
        "_max_gains": [],
        "_max_drawdowns": [],
    }


def _append_row(group: dict[str, Any], row: dict[str, Any]) -> None:
    group["total_count"] += 1
    status = str(row.get("data_status") or "unknown")
    if status == "success":
        group["success_count"] += 1
        final_return = _safe_float(row.get("final_return_pct"))
        max_gain = _safe_float(row.get("max_gain_pct"))
        max_drawdown = _safe_float(row.get("max_drawdown_pct"))
        if final_return is not None:
            group["_final_returns"].append(final_return)
        if max_gain is not None:
            group["_max_gains"].append(max_gain)
        if max_drawdown is not None:
            group["_max_drawdowns"].append(max_drawdown)
    elif status == "pending":
        group["pending_count"] += 1
    elif status == "unavailable":
        group["unavailable_count"] += 1
    elif status == "error":
        group["error_count"] += 1


def finalize_group(group: dict[str, Any]) -> dict[str, Any]:
    final_returns = list(group.pop("_final_returns", []))
    max_gains = list(group.pop("_max_gains", []))
    max_drawdowns = list(group.pop("_max_drawdowns", []))
    total = max(0, int(group.get("total_count") or 0))
    success = max(0, int(group.get("success_count") or 0))
    coverage = (success / total) if total else 0.0
    positive_count = sum(1 for value in final_returns if value > 0)
    strong_count = sum(
        1
        for index, final_return in enumerate(final_returns)
        if final_return >= 2 or (index < len(max_gains) and max_gains[index] >= 3)
    )
    drawdown_count = sum(
        1
        for index, final_return in enumerate(final_returns)
        if final_return <= -2 or (index < len(max_drawdowns) and max_drawdowns[index] <= -3)
    )
    avg_final = _avg(final_returns)
    avg_gain = _avg(max_gains)
    avg_drawdown = _avg(max_drawdowns)
    positive_ratio = (positive_count / success) if success else 0.0
    strong_ratio = (strong_count / success) if success else 0.0
    drawdown_ratio = (drawdown_count / success) if success else 0.0
    gain_drawdown_ratio = None
    if avg_gain is not None and avg_drawdown not in (None, 0):
        gain_drawdown_ratio = avg_gain / abs(avg_drawdown)
    expectancy = (avg_final or 0.0) + strong_ratio * 2 - drawdown_ratio * 2
    group.update({
        "coverage_ratio": round(coverage, 4),
        "avg_final_return_pct": _round(avg_final),
        "median_final_return_pct": _round(_median(final_returns)),
        "avg_max_gain_pct": _round(avg_gain),
        "avg_max_drawdown_pct": _round(avg_drawdown),
        "positive_ratio": round(positive_ratio, 4),
        "strong_ratio": round(strong_ratio, 4),
        "drawdown_ratio": round(drawdown_ratio, 4),
        "avg_gain_drawdown_ratio": _round(gain_drawdown_ratio),
        "expectancy_score": round(expectancy, 2),
        "sample_quality": sample_quality(success, coverage),
    })
    return group


def _where_clause(
    *,
    horizon: str = "all",
    window_sec: int = 2592000,
    module: str = "",
    decision: str = "",
    risk_level: str = "",
) -> tuple[str, dict[str, Any]]:
    clauses: list[str] = []
    params: dict[str, Any] = {}
    normalized_horizon = normalize_horizon(horizon)
    if normalized_horizon != "all":
        clauses.append("horizon = :horizon")
        params["horizon"] = normalized_horizon
    if module:
        clauses.append("module = :module")
        params["module"] = str(module).strip().lower()
    if decision:
        clauses.append("COALESCE(decision_code, '') = :decision")
        params["decision"] = str(decision).strip()
    if risk_level:
        clauses.append("COALESCE(risk_level, '') = :risk_level")
        params["risk_level"] = str(risk_level).strip()
    safe_window = int(clamp(window_sec or 2592000, 3600, 31536000))
    if safe_window:
        clauses.append("signal_time >= :start_time")
        params["start_time"] = _iso(time.time() - safe_window)
    return (f"WHERE {' AND '.join(clauses)}" if clauses else "", params)


@dataclass
class DecisionBacktestDashboard:
    db_path: Path

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "DecisionBacktestDashboard":
        loaded = settings or Settings.load()
        return cls(Path(loaded.outcome_db_path))

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def _table_exists(self, conn: sqlite3.Connection) -> bool:
        row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='signal_outcomes'").fetchone()
        return bool(row)

    def rows(
        self,
        *,
        horizon: str = "all",
        window_sec: int = 2592000,
        module: str = "",
        decision: str = "",
        risk_level: str = "",
    ) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            return []
        where, params = _where_clause(
            horizon=horizon,
            window_sec=window_sec,
            module=module,
            decision=decision,
            risk_level=risk_level,
        )
        with closing(self._connect()) as conn:
            if not self._table_exists(conn):
                return []
            result = conn.execute(
                f"""
                SELECT id, signal_id, symbol, coin, signal_time, horizon, module, signal_type,
                       data_status, result_label, result_tone, final_return_pct, max_gain_pct,
                       max_drawdown_pct, decision_code, decision_label, decision_confidence,
                       risk_level, data_source, error
                FROM signal_outcomes
                {where}
                ORDER BY id DESC
                """,
                params,
            ).fetchall()
        return [dict(row) for row in result]

    def aggregate(self, rows: list[dict[str, Any]], dimension: str = "decision") -> list[dict[str, Any]]:
        groups: dict[str, dict[str, Any]] = {}
        for row in rows:
            if dimension == "decision":
                key = str(row.get("decision_code") or "unknown")
                label = decision_label(key, str(row.get("decision_label") or ""))
            elif dimension == "horizon":
                key = str(row.get("horizon") or "unknown")
                label = key
            elif dimension == "module":
                key = str(row.get("module") or "unknown")
                label = key
            elif dimension == "risk_level":
                key = str(row.get("risk_level") or "unknown")
                label = key
            elif dimension == "confidence_bucket":
                bucket = confidence_bucket(row.get("decision_confidence"))
                key = bucket["code"]
                label = bucket["label"]
            else:
                key = "all"
                label = "全部"
            group = groups.setdefault(key, _base_group(key, label))
            _append_row(group, row)
        return sorted((finalize_group(group) for group in groups.values()), key=lambda item: (-int(item["total_count"]), str(item["key"])))

    def matrix(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        matrix: dict[str, dict[str, Any]] = {}
        for decision_code in (*DECISION_LABELS.keys(),):
            if decision_code == "unknown":
                continue
            matrix[decision_code] = {"decision_code": decision_code, "decision_label": decision_label(decision_code), "horizons": {}}
        for decision_code in (*DECISION_LABELS.keys(),):
            for horizon in OUTCOME_WINDOWS:
                selected = [
                    row for row in rows
                    if str(row.get("horizon") or "") == horizon
                    and str(row.get("decision_code") or "unknown") == decision_code
                ]
                group = _base_group(horizon, horizon)
                for row in selected:
                    _append_row(group, row)
                if decision_code not in matrix:
                    matrix[decision_code] = {"decision_code": decision_code, "decision_label": decision_label(decision_code), "horizons": {}}
                matrix[decision_code]["horizons"][horizon] = finalize_group(group)
        return {"items": list(matrix.values()), "horizons": list(OUTCOME_WINDOWS.keys())}

    def detail(
        self,
        *,
        decision: str = "",
        horizon: str = "all",
        limit: int = 20,
        window_sec: int = 2592000,
        public: bool = False,
    ) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            return []
        where, params = _where_clause(horizon=horizon, window_sec=window_sec, decision=decision)
        params["limit"] = int(clamp(limit or 20, 1, 100))
        fields = """
            id, signal_id, symbol, horizon, signal_time, module, decision_code, decision_label,
            risk_level, decision_confidence, final_return_pct, max_gain_pct, max_drawdown_pct,
            result_label, result_tone, data_status
        """
        if not public:
            fields += ", data_source, error"
        with closing(self._connect()) as conn:
            if not self._table_exists(conn):
                return []
            rows = conn.execute(
                f"SELECT {fields} FROM signal_outcomes {where} ORDER BY id DESC LIMIT :limit",
                params,
            ).fetchall()
        return [redact_api_payload(dict(row)) for row in rows]


def summarize_coverage(all_group: dict[str, Any]) -> dict[str, Any]:
    total = int(all_group.get("total_count") or 0)
    return {
        "total_count": total,
        "success_count": int(all_group.get("success_count") or 0),
        "pending_count": int(all_group.get("pending_count") or 0),
        "unavailable_count": int(all_group.get("unavailable_count") or 0),
        "error_count": int(all_group.get("error_count") or 0),
        "coverage_ratio": all_group.get("coverage_ratio", 0),
    }


def model_diagnosis(decision_groups: list[dict[str, Any]], overall: dict[str, Any]) -> dict[str, Any]:
    groups = {str(item.get("key")): item for item in decision_groups}
    strengths: list[str] = []
    weaknesses: list[str] = []
    hints: list[str] = []
    warnings: list[str] = []
    overall_drawdown = float(overall.get("drawdown_ratio") or 0)
    overall_success = int(overall.get("success_count") or 0)

    probe = groups.get("probe", {})
    if int(probe.get("success_count") or 0) == 0:
        hints.append("可试仓样本不足，模型可能过于保守。")
    elif (
        int(probe.get("success_count") or 0) >= 10
        and float(probe.get("avg_final_return_pct") or 0) > 0
        and float(probe.get("positive_ratio") or 0) > 0.55
        and float(probe.get("drawdown_ratio") or 0) < 0.35
    ):
        strengths.append("可试仓后续表现有效，可继续观察该规则。")

    risk = groups.get("risk_alert", {})
    if int(risk.get("success_count") or 0) > 0:
        if float(risk.get("avg_final_return_pct") or 0) < 0 or float(risk.get("drawdown_ratio") or 0) > overall_drawdown:
            strengths.append("风险警报有一定过滤价值。")
        if float(risk.get("avg_final_return_pct") or 0) > 0 and float(risk.get("positive_ratio") or 0) > 0.55:
            weaknesses.append("风险警报后续仍明显走强，可能过度保守，需要降低风险权重。")

    avoid = groups.get("avoid_chase", {})
    if int(avoid.get("success_count") or 0) > 0:
        if float(avoid.get("drawdown_ratio") or 0) > overall_drawdown:
            strengths.append("禁止追高对高回撤样本有一定识别价值。")
        if float(avoid.get("avg_final_return_pct") or 0) > 1 and float(avoid.get("positive_ratio") or 0) > 0.6:
            weaknesses.append("禁止追高样本后续继续上涨，可能压制强趋势。")

    wait = groups.get("wait_pullback", {})
    if float(wait.get("avg_max_drawdown_pct") or 0) < -1 and float(wait.get("avg_final_return_pct") or 0) >= 0:
        strengths.append("等待回踩样本符合先回撤后转正特征。")

    observe = groups.get("observe", {})
    if int(observe.get("success_count") or 0) > 0 and abs(float(observe.get("avg_final_return_pct") or 0)) < 1 and float(observe.get("drawdown_ratio") or 0) < 0.5:
        strengths.append("观察分类收益和风险接近中性，分类基本正常。")

    if overall_success < 20:
        warnings.append("success 样本仍偏少，诊断只适合观察，不适合定论。")
    if float(overall.get("coverage_ratio") or 0) < 0.4 and int(overall.get("total_count") or 0) > 0:
        warnings.append("结果覆盖率偏低，pending 或 unavailable 较多。")
    if int(overall.get("error_count") or 0) > 0:
        warnings.append("存在 error 样本，需要优先排查 outcome 计算异常。")

    if not strengths and not weaknesses:
        overall_label = "观察中"
        overall_summary = "当前样本仍在积累，暂不建议据此大幅调整模型。"
    elif weaknesses:
        overall_label = "需要校准"
        overall_summary = "部分决策后续表现与预期不一致，建议继续校准阈值和风险权重。"
    else:
        overall_label = "初步有效"
        overall_summary = "部分决策表现与预期一致，可以继续扩大样本观察。"

    return {
        "overall_label": overall_label,
        "overall_summary": overall_summary,
        "strengths": strengths,
        "weaknesses": weaknesses,
        "calibration_hints": hints,
        "data_warnings": warnings,
    }


def build_backtest_payload(
    *,
    settings: Settings | None = None,
    horizon: str = "all",
    window_sec: int = 2592000,
    module: str = "",
    decision: str = "",
    risk_level: str = "",
    min_samples: int = 0,
) -> dict[str, Any]:
    dashboard = DecisionBacktestDashboard.from_settings(settings)
    rows = dashboard.rows(horizon=horizon, window_sec=window_sec, module=module, decision=decision, risk_level=risk_level)
    all_group = _base_group("all", "全部样本")
    for row in rows:
        _append_row(all_group, row)
    overall = finalize_group(all_group)
    decision_groups = [
        item for item in dashboard.aggregate(rows, "decision")
        if int(item.get("success_count") or 0) >= int(min_samples or 0)
    ]
    return {
        "summary": {
            **overall,
            "headline": (
                f"当前筛选下共有 {overall['total_count']} 条 outcome，"
                f"已计算 {overall['success_count']} 条，覆盖率 {round(float(overall['coverage_ratio']) * 100, 1)}%。"
            ),
        },
        "decision_groups": decision_groups,
        "module_groups": dashboard.aggregate(rows, "module"),
        "risk_groups": dashboard.aggregate(rows, "risk_level"),
        "confidence_groups": dashboard.aggregate(rows, "confidence_bucket"),
        "model_diagnosis": model_diagnosis(decision_groups, overall),
        "filters": {
            "horizon": normalize_horizon(horizon),
            "window_sec": int(clamp(window_sec or 2592000, 3600, 31536000)),
            "module": str(module or ""),
            "decision": str(decision or ""),
            "risk_level": str(risk_level or ""),
            "min_samples": int(min_samples or 0),
        },
        "coverage": summarize_coverage(overall),
    }


def build_backtest_matrix_payload(
    *,
    settings: Settings | None = None,
    window_sec: int = 2592000,
    module: str = "",
    risk_level: str = "",
) -> dict[str, Any]:
    dashboard = DecisionBacktestDashboard.from_settings(settings)
    rows = dashboard.rows(horizon="all", window_sec=window_sec, module=module, risk_level=risk_level)
    return {
        **dashboard.matrix(rows),
        "filters": {
            "window_sec": int(clamp(window_sec or 2592000, 3600, 31536000)),
            "module": str(module or ""),
            "risk_level": str(risk_level or ""),
        },
    }


def build_backtest_detail_payload(
    *,
    settings: Settings | None = None,
    decision: str = "",
    horizon: str = "all",
    limit: int = 20,
    window_sec: int = 2592000,
    public: bool = False,
) -> dict[str, Any]:
    dashboard = DecisionBacktestDashboard.from_settings(settings)
    items = dashboard.detail(decision=decision, horizon=horizon, limit=limit, window_sec=window_sec, public=public)
    return {
        "items": items,
        "count": len(items),
        "filters": {
            "decision": str(decision or ""),
            "horizon": normalize_horizon(horizon),
            "limit": int(clamp(limit or 20, 1, 100)),
            "window_sec": int(clamp(window_sec or 2592000, 3600, 31536000)),
        },
    }
