from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import statistics
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable

from .atomic_json import atomic_write_text
from .config import BASE_DIR, Settings
from .decision_model import (
    DEFAULT_DECISION_THRESHOLDS,
    DEFAULT_DECISION_WEIGHTS,
    MODEL_FAMILY,
    MODEL_VERSION,
    MODULE_WEIGHTS,
)
from .lifecycle_calibration import (
    _lifecycle_sources,
    _outcome_rows,
    _readonly,
    _risk_statistics,
    _tables,
)
from .lifecycle_intelligence import INTELLIGENCE_MODEL_VERSION, NOT_ADVICE
from .lifecycle_intelligence_store import IntelligenceStore
from .lifecycle_store import normalize_lifecycle_symbol, safe_int


OPTIMIZATION_VERSION = "optimization-v1"
OPTIMIZATION_REPORT_JSON = BASE_DIR / "docs" / "generated" / "model_optimization_latest.json"
OPTIMIZATION_REPORT_MD = BASE_DIR / "docs" / "generated" / "model_optimization_latest.md"
HORIZONS = ("1h", "4h", "24h", "72h")
SCENARIO_KEYS = (
    "threshold_tuning",
    "risk_control",
    "lifecycle_quality",
    "module_rebalance",
)
SCENARIO_ALIASES = {
    "": "all",
    "all": "all",
    "threshold": "threshold_tuning",
    "risk": "risk_control",
    "lifecycle": "lifecycle_quality",
    "module": "module_rebalance",
    **{key: key for key in SCENARIO_KEYS},
}


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


PRODUCTION_MODEL = _freeze({
    "model_family": MODEL_FAMILY,
    "model_version": MODEL_VERSION,
    "decision_thresholds": dict(DEFAULT_DECISION_THRESHOLDS),
    "decision_weights": dict(DEFAULT_DECISION_WEIGHTS),
    "module_weights": dict(MODULE_WEIGHTS),
    "lifecycle_model_version": INTELLIGENCE_MODEL_VERSION,
    "immutable": True,
})


PARAMETER_RANGES: Mapping[str, tuple[float, float]] = MappingProxyType({
    "min_confidence": (0.0, 100.0),
    "min_probe_confidence": (0.0, 100.0),
    "probe_high_confidence": (0.0, 100.0),
    "max_lifecycle_risk_score": (0.0, 100.0),
    "min_lifecycle_score": (0.0, 100.0),
    "min_intelligence_score": (0.0, 100.0),
    "risk_penalty_multiplier": (0.5, 2.0),
    "oi_divergence_weight": (0.0, 50.0),
    "funding_weight": (0.0, 50.0),
    "cvd_divergence_weight": (0.0, 50.0),
    "spot_cvd_weight": (0.0, 25.0),
    "futures_cvd_weight": (0.0, 25.0),
    **{
        f"module_weight_{module}": (0.5, 1.5)
        for module in ("launch", "flow", "structure", "structure_review", "funding", "summary", "announcement", "ai")
    },
})

SCENARIO_PARAMETER_KEYS: Mapping[str, frozenset[str]] = MappingProxyType({
    "threshold_tuning": frozenset({"min_confidence", "min_probe_confidence", "probe_high_confidence"}),
    "risk_control": frozenset({
        "min_confidence", "max_lifecycle_risk_score", "risk_penalty_multiplier",
        "oi_divergence_weight", "funding_weight", "cvd_divergence_weight",
    }),
    "lifecycle_quality": frozenset({
        "min_confidence", "min_lifecycle_score", "min_intelligence_score",
        "max_lifecycle_risk_score", "spot_cvd_weight", "futures_cvd_weight",
    }),
    "module_rebalance": frozenset({
        "min_confidence",
        *(key for key in PARAMETER_RANGES if key.startswith("module_weight_")),
    }),
})

BUILTIN_SCENARIOS = _freeze({
    "threshold_tuning": {
        "scenario_key": "threshold_tuning",
        "scenario_type": "threshold",
        "name": "决策阈值保守化",
        "description": "提高历史重放中的最低置信度门槛，验证更严格筛选是否改善成熟 Outcome。",
        "candidate_params": {
            "min_confidence": 0.0,
            "min_probe_confidence": 75.0,
            "probe_high_confidence": 80.0,
        },
        "built_in": True,
    },
    "risk_control": {
        "scenario_key": "risk_control",
        "scenario_type": "risk",
        "name": "风险约束增强",
        "description": "降低高生命周期风险样本的候选权重，仅验证历史风险约束效果。",
        "candidate_params": {
            "min_confidence": 0.0,
            "max_lifecycle_risk_score": 55.0,
            "risk_penalty_multiplier": 1.0,
            "oi_divergence_weight": 35.0,
            "funding_weight": 10.0,
            "cvd_divergence_weight": 20.0,
        },
        "built_in": True,
    },
    "lifecycle_quality": {
        "scenario_key": "lifecycle_quality",
        "scenario_type": "lifecycle",
        "name": "生命周期质量过滤",
        "description": "使用事件时点的 Lifecycle 因子做离线候选重评分；不写回既有评分或生产权重。",
        "candidate_params": {
            "min_confidence": 45.0,
            "min_lifecycle_score": 60.0,
            "min_intelligence_score": 70.0,
            "max_lifecycle_risk_score": 60.0,
            "spot_cvd_weight": 15.0,
            "futures_cvd_weight": 5.0,
        },
        "built_in": True,
    },
    "module_rebalance": {
        "scenario_key": "module_rebalance",
        "scenario_type": "module",
        "name": "模块权重重放",
        "description": "仅在模拟层调整模块置信度倍率，生产 Module 权重保持不变。",
        "candidate_params": {
            "min_confidence": 52.0,
            "module_weight_launch": 1.05,
            "module_weight_flow": 1.10,
            "module_weight_structure": 1.10,
            "module_weight_structure_review": 1.00,
            "module_weight_funding": 0.85,
            "module_weight_summary": 0.75,
            "module_weight_announcement": 0.60,
            "module_weight_ai": 0.90,
        },
        "built_in": True,
    },
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _mean(values: Iterable[Any], digits: int = 6) -> float | None:
    numbers = [number for value in values if (number := _number(value)) is not None]
    return round(sum(numbers) / len(numbers), digits) if numbers else None


def _median(values: Iterable[Any], digits: int = 6) -> float | None:
    numbers = [number for value in values if (number := _number(value)) is not None]
    return round(float(statistics.median(numbers)), digits) if numbers else None


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator > 0 else None


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def production_model_snapshot() -> dict[str, Any]:
    """Return a detached copy; callers can never mutate the production constants."""

    return _thaw(PRODUCTION_MODEL)


def production_model_fingerprint() -> str:
    """Fingerprint the live production constants without modifying them."""

    payload = {
        "model_family": MODEL_FAMILY,
        "model_version": MODEL_VERSION,
        "decision_thresholds": dict(DEFAULT_DECISION_THRESHOLDS),
        "decision_weights": dict(DEFAULT_DECISION_WEIGHTS),
        "module_weights": dict(MODULE_WEIGHTS),
        "lifecycle_model_version": INTELLIGENCE_MODEL_VERSION,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


PRODUCTION_MODEL_FINGERPRINT = production_model_fingerprint()


def _normalize_scenario(value: str) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_")
    if normalized not in SCENARIO_ALIASES:
        raise ValueError("unknown optimization scenario")
    return SCENARIO_ALIASES[normalized]


def validate_candidate_params(
    params: Mapping[str, Any] | None,
    *,
    scenario: str,
) -> dict[str, float]:
    scenario_key = _normalize_scenario(scenario)
    if scenario_key == "all":
        if params:
            raise ValueError("candidate_params require one explicit scenario")
        return {}
    if params is None:
        return {}
    if not isinstance(params, Mapping):
        raise ValueError("candidate_params must be an object")
    allowed = SCENARIO_PARAMETER_KEYS[scenario_key]
    result: dict[str, float] = {}
    for raw_key, raw_value in params.items():
        key = str(raw_key or "").strip()
        if key not in PARAMETER_RANGES or key not in allowed:
            raise ValueError(f"candidate parameter is not allowed for {scenario_key}: {key}")
        if isinstance(raw_value, bool):
            raise ValueError(f"candidate parameter must be numeric: {key}")
        value = _number(raw_value)
        minimum, maximum = PARAMETER_RANGES[key]
        if value is None or value < minimum or value > maximum:
            raise ValueError(f"candidate parameter out of range: {key}")
        result[key] = round(value, 6)
    return result


def _scenario_definition(key: str, overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    definition = _thaw(BUILTIN_SCENARIOS[key])
    params = dict(definition.get("candidate_params") or {})
    params.update(validate_candidate_params(overrides, scenario=key))
    definition["candidate_params"] = params
    definition["optimization_version"] = OPTIMIZATION_VERSION
    definition["scenario_name"] = definition["name"]
    definition["base_model_version"] = MODEL_VERSION
    definition["scenario_version"] = OPTIMIZATION_VERSION
    definition["parameters"] = dict(params)
    definition["status"] = "draft"
    if key == "threshold_tuning":
        definition["factor_changes"] = [
            {
                "factor": "probe_min_total",
                "label": "Probe 阈值方案 75",
                "old_value": float(DEFAULT_DECISION_THRESHOLDS.get("probe_min_total", 70)),
                "new_value": float(params.get("min_probe_confidence", 75.0)),
                "delta": float(params.get("min_probe_confidence", 75.0)) - float(DEFAULT_DECISION_THRESHOLDS.get("probe_min_total", 70)),
                "variant": "75",
            },
            {
                "factor": "probe_min_total",
                "label": "Probe 阈值方案 80",
                "old_value": float(DEFAULT_DECISION_THRESHOLDS.get("probe_min_total", 70)),
                "new_value": float(params.get("probe_high_confidence", 80.0)),
                "delta": float(params.get("probe_high_confidence", 80.0)) - float(DEFAULT_DECISION_THRESHOLDS.get("probe_min_total", 70)),
                "variant": "80",
            },
        ]
    elif key == "risk_control":
        definition["factor_changes"] = [
            {
                "factor": factor,
                "label": label,
                "old_value": old,
                "new_value": float(params.get(parameter, old)),
                "delta": float(params.get(parameter, old)) - old,
            }
            for factor, label, parameter, old in (
                ("oi_divergence", "OI 背离风险权重", "oi_divergence_weight", 25.0),
                ("funding_crowding", "Funding 过热风险权重", "funding_weight", 20.0),
                ("cvd_divergence", "CVD 背离风险权重", "cvd_divergence_weight", 20.0),
            )
        ] + [{
            "factor": "maximum_lifecycle_risk_score",
            "label": "离线风险分数上限",
            "old_value": 100.0,
            "new_value": float(params.get("max_lifecycle_risk_score", 100.0)),
            "delta": float(params.get("max_lifecycle_risk_score", 100.0)) - 100.0,
        }]
    elif key == "lifecycle_quality":
        definition["factor_changes"] = [
            {
                "factor": "spot_cvd",
                "label": "Spot CVD 权重",
                "old_value": 10.0,
                "new_value": float(params.get("spot_cvd_weight", 10.0)),
                "delta": float(params.get("spot_cvd_weight", 10.0)) - 10.0,
            },
            {
                "factor": "futures_cvd",
                "label": "Futures CVD 权重",
                "old_value": 10.0,
                "new_value": float(params.get("futures_cvd_weight", 10.0)),
                "delta": float(params.get("futures_cvd_weight", 10.0)) - 10.0,
            },
            {
                "factor": "minimum_intelligence_score",
                "label": "生命周期离线质量门槛",
                "old_value": 0.0,
                "new_value": float(params.get("min_intelligence_score", 0.0)),
                "delta": float(params.get("min_intelligence_score", 0.0)),
            },
            {
                "factor": "maximum_lifecycle_risk_score",
                "label": "生命周期风险上限",
                "old_value": 100.0,
                "new_value": float(params.get("max_lifecycle_risk_score", 100.0)),
                "delta": float(params.get("max_lifecycle_risk_score", 100.0)) - 100.0,
            },
        ]
    else:
        definition["factor_changes"] = [
            {
                "factor": module,
                "label": f"{module} 模拟倍率",
                "old_value": 1.0,
                "new_value": float(params.get(f"module_weight_{module}", 1.0)),
                "delta": round(float(params.get(f"module_weight_{module}", 1.0)) - 1.0, 6),
                "production_module_weight": MODULE_WEIGHTS.get(module),
            }
            for module in ("launch", "flow", "structure", "structure_review", "funding", "summary", "announcement", "ai")
        ] + [{
            "factor": "minimum_confidence",
            "label": "模块倍率后的最低置信度",
            "old_value": 0.0,
            "new_value": float(params.get("min_confidence", 0.0)),
            "delta": float(params.get("min_confidence", 0.0)),
        }]
    definition["auto_apply"] = False
    return definition


def list_optimization_scenarios(settings: Settings | None = None) -> dict[str, Any]:
    del settings
    return {
        "ok": True,
        "optimization_version": OPTIMIZATION_VERSION,
        "production_model": production_model_snapshot(),
        "scenarios": [_scenario_definition(key) for key in SCENARIO_KEYS],
        "parameter_ranges": {key: list(value) for key, value in PARAMETER_RANGES.items()},
        "does_not_modify_model": True,
        "auto_apply": False,
    }


def _source_signature(
    samples: list[dict[str, Any]],
    *,
    risk_history: Mapping[str, Any] | None = None,
) -> str:
    projection = [
        {
            key: row.get(key)
            for key in (
                "id", "signal_id", "lifecycle_id", "symbol", "horizon", "data_status",
                "final_return_pct", "max_gain_pct", "max_drawdown_pct", "decision_code",
                "decision_confidence", "risk_level", "module", "lifecycle_score",
                "lifecycle_risk_score", "intelligence_score", "price_change_from_first_pct",
                "oi_change_from_first_pct", "spot_cvd_change_from_first",
                "futures_cvd_change_from_first", "latest_funding_rate", "volume_multiplier",
                "as_of_feature_status", "temporal_feature_mode", "lifecycle_event_id",
                "updated_at",
            )
        }
        for row in samples
    ]
    payload = json.dumps(
        {
            "optimization_version": OPTIMIZATION_VERSION,
            "model_version": MODEL_VERSION,
            "samples": projection,
            "risk_history": dict(risk_history or {}),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_as_of_events(path: Path, event_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    normalized = sorted({safe_int(value) for value in event_ids if safe_int(value) > 0})
    if not normalized:
        return {}
    conn = _readonly(path)
    if conn is None:
        return {}
    try:
        if "lifecycle_events" not in _tables(conn):
            return {}
        result: dict[int, dict[str, Any]] = {}
        for offset in range(0, len(normalized), 800):
            chunk = normalized[offset : offset + 800]
            placeholders = ",".join("?" for _ in chunk)
            for row in conn.execute(
                "SELECT id,lifecycle_id,event_time,price_change_from_first_pct,volume_change_pct,"
                "oi_change_pct,futures_cvd_delta,spot_cvd_delta,funding_rate,event_score,risk_score "
                f"FROM lifecycle_events WHERE id IN ({placeholders})",
                chunk,
            ).fetchall():
                result[safe_int(row["id"])] = dict(row)
        return result
    finally:
        conn.close()


def _load_mature_samples(
    settings: Settings,
    *,
    symbol: str = "",
    limit: int | None = None,
) -> dict[str, Any]:
    normalized_symbol = normalize_lifecycle_symbol(symbol)
    if symbol and not normalized_symbol:
        raise ValueError("invalid_symbol")
    sources = _lifecycle_sources(Path(settings.lifecycle_db_path))
    lifecycles = list(sources.get("lifecycles") or [])
    if normalized_symbol:
        lifecycles = [
            row for row in lifecycles
            if normalize_lifecycle_symbol(row.get("symbol")) == normalized_symbol
        ]
    if limit is not None:
        bounded = max(1, min(safe_int(limit, 10000), 10000))
        lifecycles = lifecycles[:bounded]
    lifecycle_ids = {safe_int(row.get("lifecycle_id")) for row in lifecycles}
    links = [
        row for row in (sources.get("links") or [])
        if safe_int(row.get("lifecycle_id")) in lifecycle_ids
    ]
    outcomes = _outcome_rows(Path(settings.outcome_db_path), links)
    by_lifecycle = {safe_int(row.get("lifecycle_id")): row for row in lifecycles}
    as_of_events = _read_as_of_events(
        Path(settings.lifecycle_db_path),
        (safe_int(outcome.get("lifecycle_event_id")) for outcome in outcomes),
    )
    samples: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    status_counts: Counter[str] = Counter()
    for outcome in outcomes:
        status = str(outcome.get("data_status") or "missing")
        status_counts[status] += 1
        lifecycle_id = safe_int(outcome.get("lifecycle_id"))
        outcome_id = safe_int(outcome.get("id"))
        identity = (lifecycle_id, outcome_id)
        if identity in seen:
            continue
        seen.add(identity)
        if status != "success" or _number(outcome.get("final_return_pct")) is None:
            continue
        lifecycle = by_lifecycle.get(lifecycle_id) or {}
        event = as_of_events.get(safe_int(outcome.get("lifecycle_event_id")))
        volume_change = _number((event or {}).get("volume_change_pct"))
        volume_multiplier = 1.0 + volume_change / 100.0 if volume_change is not None else None
        samples.append({
            **outcome,
            "symbol": normalize_lifecycle_symbol(outcome.get("symbol") or lifecycle.get("symbol")),
            "lifecycle_score": _number((event or {}).get("event_score")),
            "lifecycle_risk_score": _number((event or {}).get("risk_score")),
            "intelligence_score": None,
            "price_change_from_first_pct": _number((event or {}).get("price_change_from_first_pct")),
            "oi_change_from_first_pct": _number((event or {}).get("oi_change_pct")),
            "spot_cvd_change_from_first": _number((event or {}).get("spot_cvd_delta")),
            "futures_cvd_change_from_first": _number((event or {}).get("futures_cvd_delta")),
            "latest_funding_rate": _number((event or {}).get("funding_rate")),
            "volume_multiplier": volume_multiplier,
            "as_of_feature_status": "available" if event else "unavailable",
            "temporal_feature_mode": "lifecycle_event_exact" if event else "unavailable",
            "first_signal_level": str(lifecycle.get("first_signal_level") or "unknown"),
            "highest_level": str(lifecycle.get("highest_level") or "unknown"),
            "first_signal_module": str(lifecycle.get("first_signal_module") or ""),
        })
    risk_history = _risk_statistics(
        list(sources.get("events") or []),
        list(sources.get("frames") or []),
        outcomes,
    )
    return {
        "lifecycles": lifecycles,
        "samples": samples,
        "source_signature": _source_signature(samples, risk_history=risk_history),
        "status_counts": dict(sorted(status_counts.items())),
        "linked_outcome_count": len(outcomes),
        "risk_history": risk_history,
    }


def _decision_correct(row: dict[str, Any]) -> bool:
    decision = str(row.get("decision_code") or "observe")
    final_return = _number(row.get("final_return_pct")) or 0.0
    drawdown = _number(row.get("max_drawdown_pct")) or 0.0
    if decision in {"avoid_chase", "risk_alert"}:
        return final_return <= 0 or drawdown <= -3
    if decision == "wait_pullback":
        return final_return > 0 or drawdown <= -2
    return final_return > 0


def calculate_optimization_metrics(rows: Iterable[dict[str, Any]]) -> dict[str, Any]:
    samples = [
        dict(row) for row in rows
        if str(row.get("data_status") or "success") == "success"
        and _number(row.get("final_return_pct")) is not None
    ]
    returns = [_number(row.get("final_return_pct")) or 0.0 for row in samples]
    gains = [_number(row.get("max_gain_pct")) for row in samples]
    drawdowns = [_number(row.get("max_drawdown_pct")) for row in samples]
    positives = sum(value > 0 for value in returns)
    drawdown_events = sum(
        (_number(row.get("max_drawdown_pct")) or 0.0) <= -3
        or (_number(row.get("final_return_pct")) or 0.0) <= -2
        for row in samples
    )
    positive_sum = sum(value for value in returns if value > 0)
    negative_sum = abs(sum(value for value in returns if value < 0))
    symbols = Counter(str(row.get("symbol") or "") for row in samples if str(row.get("symbol") or ""))
    avg_return = _mean(returns)
    median_return = _median(returns)
    avg_gain = _mean(gains)
    avg_drawdown = _mean(drawdowns)
    risk_adjusted_score = (
        round(avg_return - abs(avg_drawdown) * 0.25, 6)
        if avg_return is not None and avg_drawdown is not None else None
    )
    horizons: dict[str, dict[str, Any]] = {}
    for horizon in HORIZONS:
        group = [row for row in samples if str(row.get("horizon") or "") == horizon]
        group_returns = [_number(row.get("final_return_pct")) or 0.0 for row in group]
        horizons[horizon] = {
            "sample_count": len(group),
            "positive_count": sum(value > 0 for value in group_returns),
            "success_ratio": _ratio(sum(value > 0 for value in group_returns), len(group)),
            "avg_return_pct": _mean(group_returns),
            "avg_max_drawdown_pct": _mean(row.get("max_drawdown_pct") for row in group),
        }
    return {
        "sample_count": len(samples),
        "mature_sample_count": len(samples),
        "unique_signal_count": len({safe_int(row.get("signal_id")) for row in samples if safe_int(row.get("signal_id")) > 0}),
        "unique_lifecycle_count": len({safe_int(row.get("lifecycle_id")) for row in samples if safe_int(row.get("lifecycle_id")) > 0}),
        "unique_symbol_count": len(symbols),
        "top_symbol": symbols.most_common(1)[0][0] if symbols else None,
        "top_symbol_sample_count": symbols.most_common(1)[0][1] if symbols else 0,
        "top_symbol_share": _ratio(symbols.most_common(1)[0][1], len(samples)) if symbols else None,
        "positive_count": positives,
        "success_ratio": _ratio(positives, len(samples)),
        "decision_correct_count": sum(_decision_correct(row) for row in samples),
        "decision_accuracy": _ratio(sum(_decision_correct(row) for row in samples), len(samples)),
        "avg_return_pct": avg_return,
        "avg_return": avg_return,
        "median_return_pct": median_return,
        "median_return": median_return,
        "return_stddev_pct": round(float(statistics.pstdev(returns)), 6) if len(returns) > 1 else (0.0 if returns else None),
        "avg_max_gain_pct": avg_gain,
        "avg_max_gain": avg_gain,
        "avg_max_drawdown_pct": avg_drawdown,
        "avg_drawdown": avg_drawdown,
        "worst_drawdown_pct": min((value for value in drawdowns if value is not None), default=None),
        "drawdown_event_count": drawdown_events,
        "drawdown_event_ratio": _ratio(drawdown_events, len(samples)),
        "drawdown_ratio": _ratio(drawdown_events, len(samples)),
        "expectancy_pct": _mean(returns),
        "expectancy_score": _mean(returns),
        "risk_adjusted_score": risk_adjusted_score,
        "risk_adjusted_score_formula": "expectancy_pct - abs(avg_max_drawdown_pct) * 0.25",
        "profit_factor": round(positive_sum / negative_sum, 6) if negative_sum > 0 else None,
        "avg_decision_confidence": _mean(row.get("decision_confidence") for row in samples),
        "horizons": horizons,
    }


def _risk_factor_contributions(row: Mapping[str, Any]) -> dict[str, float]:
    price = _number(row.get("price_change_from_first_pct"))
    oi = _number(row.get("oi_change_from_first_pct"))
    spot = _number(row.get("spot_cvd_change_from_first"))
    futures = _number(row.get("futures_cvd_change_from_first"))
    funding = _number(row.get("latest_funding_rate"))
    return {
        "oi_divergence": 25.0 if oi is not None and price is not None and oi > 0 and price < 0 else 0.0,
        "funding_crowding": 20.0 if funding is not None and abs(funding) >= 0.0008 else 0.0,
        "cvd_divergence": 20.0 if futures is not None and futures > 0 and (spot is None or spot <= 0) else 0.0,
    }


def _offline_risk_score(row: Mapping[str, Any], params: Mapping[str, float]) -> tuple[float | None, dict[str, float]]:
    base = _number(row.get("lifecycle_risk_score"))
    if base is None:
        return None, _risk_factor_contributions(row)
    contributions = _risk_factor_contributions(row)
    adjusted = base
    if contributions["oi_divergence"] > 0:
        adjusted += float(params.get("oi_divergence_weight", 25.0)) - 25.0
    if contributions["funding_crowding"] > 0:
        adjusted += float(params.get("funding_weight", 20.0)) - 20.0
    if contributions["cvd_divergence"] > 0:
        adjusted += float(params.get("cvd_divergence_weight", 20.0)) - 20.0
    return round(max(0.0, min(100.0, adjusted)), 6), contributions


def _offline_quality_score(row: Mapping[str, Any], params: Mapping[str, float]) -> tuple[float | None, dict[str, float]]:
    base = _number(row.get("intelligence_score"))
    if base is None:
        base = _number(row.get("lifecycle_score"))
    spot = _number(row.get("spot_cvd_change_from_first"))
    futures = _number(row.get("futures_cvd_change_from_first"))
    spot_direction = 1.0 if spot is not None and spot > 0 else -1.0 if spot is not None and spot < 0 else 0.0
    futures_direction = 1.0 if futures is not None and futures > 0 else -1.0 if futures is not None and futures < 0 else 0.0
    contributions = {
        "spot_cvd_base": 10.0 * spot_direction,
        "futures_cvd_base": 10.0 * futures_direction,
        "spot_cvd_candidate": float(params.get("spot_cvd_weight", 10.0)) * spot_direction,
        "futures_cvd_candidate": float(params.get("futures_cvd_weight", 10.0)) * futures_direction,
    }
    if base is None:
        return None, contributions
    adjusted = (
        base
        + contributions["spot_cvd_candidate"] - contributions["spot_cvd_base"]
        + contributions["futures_cvd_candidate"] - contributions["futures_cvd_base"]
    )
    return round(max(0.0, min(100.0, adjusted)), 6), contributions


def _candidate_evaluation(
    row: dict[str, Any],
    params: Mapping[str, float],
) -> tuple[bool, dict[str, Any]]:
    module = str(row.get("module") or row.get("first_signal_module") or "").lower()
    module_weight = float(params.get(f"module_weight_{module}", 1.0))
    confidence = (_number(row.get("decision_confidence")) or 0.0) * module_weight
    lifecycle_risk, risk_contributions = _offline_risk_score(row, params)
    quality_score, quality_contributions = _offline_quality_score(row, params)
    enriched = {
        **row,
        "simulated_confidence": round(max(0.0, min(100.0, confidence)), 6),
        "simulated_risk_score": lifecycle_risk,
        "simulated_quality_score": quality_score,
        "factor_contributions": {
            "risk": risk_contributions,
            "quality": quality_contributions,
            "module_weight": module_weight,
        },
    }
    as_of_required = any(
        key in params
        for key in (
            "max_lifecycle_risk_score", "oi_divergence_weight", "funding_weight",
            "cvd_divergence_weight", "min_lifecycle_score", "min_intelligence_score",
            "spot_cvd_weight", "futures_cvd_weight",
        )
    )
    if as_of_required and str(row.get("as_of_feature_status") or "unavailable") != "available":
        return False, enriched
    penalty_multiplier = float(params.get("risk_penalty_multiplier", 1.0))
    if lifecycle_risk is not None and penalty_multiplier > 1:
        confidence -= max(0.0, lifecycle_risk - 40.0) * (penalty_multiplier - 1.0) * 0.35
        enriched["simulated_confidence"] = round(max(0.0, min(100.0, confidence)), 6)
    if confidence < float(params.get("min_confidence", 0.0)):
        return False, enriched
    if str(row.get("decision_code") or "") == "probe" and confidence < float(params.get("min_probe_confidence", 0.0)):
        return False, enriched
    maximum_risk = params.get("max_lifecycle_risk_score")
    if maximum_risk is not None and (lifecycle_risk is None or lifecycle_risk > maximum_risk):
        return False, enriched
    lifecycle_score = _number(row.get("lifecycle_score"))
    minimum_lifecycle = params.get("min_lifecycle_score")
    if minimum_lifecycle is not None and (lifecycle_score is None or lifecycle_score < minimum_lifecycle):
        return False, enriched
    intelligence = quality_score
    minimum_intelligence = params.get("min_intelligence_score")
    if minimum_intelligence is not None and (intelligence is None or intelligence < minimum_intelligence):
        return False, enriched
    return True, enriched


def simulate_candidate(
    samples: Iterable[dict[str, Any]],
    candidate_params: Mapping[str, float],
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for raw in samples:
        accepted, enriched = _candidate_evaluation(dict(raw), candidate_params)
        if accepted:
            selected.append(enriched)
    return selected


def calculate_metric_delta(
    production: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    def difference(key: str) -> float | None:
        left, right = _number(production.get(key)), _number(candidate.get(key))
        return round(right - left, 6) if left is not None and right is not None else None

    horizons: dict[str, dict[str, Any]] = {}
    for horizon in HORIZONS:
        base = dict((production.get("horizons") or {}).get(horizon) or {})
        changed = dict((candidate.get("horizons") or {}).get(horizon) or {})
        horizons[horizon] = {
            "sample_count_delta": safe_int(changed.get("sample_count")) - safe_int(base.get("sample_count")),
            "success_ratio_delta": (
                round(float(changed["success_ratio"]) - float(base["success_ratio"]), 6)
                if changed.get("success_ratio") is not None and base.get("success_ratio") is not None else None
            ),
            "avg_return_delta_pct": (
                round(float(changed["avg_return_pct"]) - float(base["avg_return_pct"]), 6)
                if changed.get("avg_return_pct") is not None and base.get("avg_return_pct") is not None else None
            ),
        }
    sample_count = safe_int(production.get("sample_count"))
    return {
        "selected_sample_delta": safe_int(candidate.get("sample_count")) - sample_count,
        "selection_coverage_ratio": _ratio(safe_int(candidate.get("sample_count")), sample_count),
        "success_ratio_delta": difference("success_ratio"),
        "decision_accuracy_delta": difference("decision_accuracy"),
        "avg_return_delta_pct": difference("avg_return_pct"),
        "median_return_delta_pct": difference("median_return_pct"),
        "avg_max_gain_delta_pct": difference("avg_max_gain_pct"),
        "avg_drawdown_improvement_pct": difference("avg_max_drawdown_pct"),
        "drawdown_event_ratio_delta": difference("drawdown_event_ratio"),
        "expectancy_delta_pct": difference("expectancy_pct"),
        "risk_adjusted_score_delta": difference("risk_adjusted_score"),
        "horizons": horizons,
    }


def optimization_confidence(
    metrics: Mapping[str, Any],
    *,
    production: Mapping[str, Any] | None = None,
    delta: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    sample_count = safe_int(metrics.get("sample_count"))
    symbols = safe_int(metrics.get("unique_symbol_count"))
    top_symbol_share = _number(metrics.get("top_symbol_share"))
    horizons = dict(metrics.get("horizons") or {})
    sample_factor = min(1.0, sample_count / 100.0)
    symbol_factor = min(1.0, symbols / 10.0)
    horizon_factor = (
        min(1.0, safe_int((horizons.get("24h") or {}).get("sample_count")) / 100.0)
        + min(1.0, safe_int((horizons.get("72h") or {}).get("sample_count")) / 50.0)
    ) / 2.0
    changes = dict(delta or {})
    success_delta = _number(changes.get("success_ratio_delta")) or 0.0
    return_delta = _number(changes.get("avg_return_delta_pct")) or 0.0
    drawdown_delta = _number(changes.get("avg_drawdown_improvement_pct")) or 0.0
    performance_factor = max(0.0, min(1.0, 0.5 + success_delta * 2.5 + return_delta / 20.0))
    return_stddev = _number(metrics.get("return_stddev_pct"))
    variance_factor = 0.5 if return_stddev is None else 1.0 / (1.0 + max(0.0, return_stddev) / 10.0)
    drawdown_factor = max(0.0, min(1.0, 0.5 + drawdown_delta / 10.0))
    score = round(
        sample_factor * 0.25
        + symbol_factor * 0.15
        + horizon_factor * 0.20
        + performance_factor * 0.15
        + variance_factor * 0.10
        + drawdown_factor * 0.15,
        6,
    )
    if sample_count < 50:
        score = min(score, 0.49)
    if symbols <= 1:
        score = min(score, 0.49)
    elif top_symbol_share is not None and top_symbol_share > 0.80:
        score = min(score, 0.69)
    label = "high_confidence" if score >= 0.7 else "medium_confidence" if score >= 0.5 else "low_confidence"
    return {
        "score": score,
        "label": label,
        "sample_count": sample_count,
        "unique_symbol_count": symbols,
        "top_symbol_share": top_symbol_share,
        "low_sample": sample_count < 50,
        "components": {
            "sample_factor": round(sample_factor, 6),
            "symbol_factor": round(symbol_factor, 6),
            "horizon_factor": round(horizon_factor, 6),
            "performance_delta_factor": round(performance_factor, 6),
            "variance_factor": round(variance_factor, 6),
            "drawdown_change_factor": round(drawdown_factor, 6),
            "success_ratio_delta": success_delta,
            "avg_return_delta_pct": return_delta,
            "avg_drawdown_improvement_pct": drawdown_delta,
        },
    }


def evaluate_optimization_readiness(
    comparison: Mapping[str, Any],
    *,
    settings: Settings | Any | None = None,
) -> dict[str, Any]:
    loaded = settings or Settings.load()
    candidate = dict(comparison.get("candidate") or {})
    production = dict(comparison.get("production") or {})
    delta = dict(comparison.get("delta") or {})
    confidence = dict(
        comparison.get("confidence")
        or optimization_confidence(candidate, production=production, delta=delta)
    )
    horizons = dict(candidate.get("horizons") or {})
    current = {
        "mature_24h_samples": safe_int((horizons.get("24h") or {}).get("sample_count")),
        "mature_72h_samples": safe_int((horizons.get("72h") or {}).get("sample_count")),
        "success_ratio_delta": _number(delta.get("success_ratio_delta")),
        "avg_drawdown_improvement_pct": _number(delta.get("avg_drawdown_improvement_pct")),
        "unique_symbol_count": safe_int(candidate.get("unique_symbol_count")),
        "top_symbol_share": _number(candidate.get("top_symbol_share")),
        "confidence": _number(confidence.get("score")) or 0.0,
        "candidate_avg_max_drawdown_pct": _number(candidate.get("avg_max_drawdown_pct")),
        "production_avg_max_drawdown_pct": _number(production.get("avg_max_drawdown_pct")),
    }
    required = {
        "mature_24h_samples": safe_int(getattr(loaded, "model_optimization_min_24h_samples", 100), 100),
        "mature_72h_samples": safe_int(getattr(loaded, "model_optimization_min_72h_samples", 50), 50),
        "success_ratio_delta": float(getattr(loaded, "model_optimization_min_success_delta", 0.05)),
        "drawdown_not_worse": True,
        "unique_symbol_count": 2,
        "max_top_symbol_share": float(getattr(loaded, "model_optimization_max_top_symbol_share", 0.80)),
        "confidence": float(getattr(loaded, "model_optimization_min_confidence", 0.70)),
    }
    drawdown_not_worse = (
        current["candidate_avg_max_drawdown_pct"] is not None
        and current["production_avg_max_drawdown_pct"] is not None
        and current["candidate_avg_max_drawdown_pct"] >= current["production_avg_max_drawdown_pct"]
    )
    checks = {
        "24h_samples": current["mature_24h_samples"] >= required["mature_24h_samples"],
        "72h_samples": current["mature_72h_samples"] >= required["mature_72h_samples"],
        "success_delta": current["success_ratio_delta"] is not None and current["success_ratio_delta"] >= required["success_ratio_delta"],
        "drawdown_not_worse": drawdown_not_worse,
        "multi_symbol": current["unique_symbol_count"] >= required["unique_symbol_count"],
        "symbol_distribution": (
            current["top_symbol_share"] is not None
            and current["top_symbol_share"] <= required["max_top_symbol_share"]
        ),
        "confidence": current["confidence"] >= required["confidence"],
    }
    passed = [key for key, value in checks.items() if value]
    blocked = [key for key, value in checks.items() if not value]
    return {
        "ready": not blocked,
        "label": "候选场景达到人工验证准入条件" if not blocked else "候选场景暂未达到验证准入条件",
        "passed": passed,
        "blocked": blocked,
        "current": {**current, "drawdown_not_worse": drawdown_not_worse},
        "required": required,
        "does_not_modify_model": True,
        "auto_apply": False,
        "note": "准入仅代表具备进一步人工验证条件，不会自动替换生产模型。",
    }


def _scenario_recommendations(comparison: Mapping[str, Any]) -> list[dict[str, Any]]:
    readiness = dict(comparison.get("readiness") or {})
    delta = dict(comparison.get("delta") or {})
    confidence = dict(comparison.get("confidence") or {})
    confidence_score = _number(confidence.get("score")) or 0.0
    confidence_label = str(confidence.get("label") or "low_confidence")
    recommendations: list[dict[str, Any]] = []
    if confidence_score < 0.5:
        return [{
            "key": "observe_low_confidence",
            "type": "observation",
            "target": str(comparison.get("scenario_key") or "candidate"),
            "finding": "low_confidence_or_concentrated_sample",
            "priority": "medium",
            "recommendation": "成熟样本少于 50 或样本分布过于集中，仅继续观察并保持生产模型不变。",
            "evidence": {"confidence": confidence_score, "label": confidence_label},
            "confidence": confidence_score,
            "confidence_label": confidence_label,
            "action": "observe_only",
            "auto_apply": False,
        }]
    if readiness.get("ready"):
        recommendations.append({
            "key": "manual_shadow_validation",
            "type": "candidate_review",
            "target": str(comparison.get("scenario_key") or "candidate"),
            "finding": "historical_readiness_passed",
            "priority": "medium",
            "recommendation": "候选场景达到历史样本准入门槛，可进入人工审查或影子验证；禁止自动应用。",
            "evidence": {"passed": readiness.get("passed"), "success_ratio_delta": delta.get("success_ratio_delta")},
            "confidence": confidence_score,
            "confidence_label": confidence_label,
            "action": "review_only",
            "auto_apply": False,
        })
    else:
        recommendations.append({
            "key": "keep_production_model",
            "type": "keep_production",
            "target": "production_model",
            "finding": "readiness_blocked",
            "priority": "high",
            "recommendation": "当前证据不足，继续保持生产模型不变并积累成熟多币种样本。",
            "evidence": {"blocked": readiness.get("blocked")},
            "confidence": confidence_score,
            "confidence_label": confidence_label,
            "action": "review_only",
            "auto_apply": False,
        })
    if (_number(delta.get("avg_drawdown_improvement_pct")) or 0.0) < 0:
        recommendations.append({
            "key": "reject_drawdown_regression",
            "type": "reject_candidate",
            "target": str(comparison.get("scenario_key") or "candidate"),
            "finding": "drawdown_regression",
            "priority": "high",
            "recommendation": "候选场景平均回撤恶化，不建议进入下一阶段验证。",
            "evidence": {"avg_drawdown_improvement_pct": delta.get("avg_drawdown_improvement_pct")},
            "confidence": confidence_score,
            "confidence_label": confidence_label,
            "action": "review_only",
            "auto_apply": False,
        })
    return recommendations


def _sample_identity(row: Mapping[str, Any]) -> tuple[int, int]:
    return safe_int(row.get("lifecycle_id")), safe_int(row.get("id"))


def _scenario_specific_metrics(
    scenario: Mapping[str, Any],
    samples: list[dict[str, Any]],
    selected: list[dict[str, Any]],
    production: Mapping[str, Any],
    candidate: Mapping[str, Any],
    risk_history: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    key = str(scenario.get("scenario_key") or "")
    params = dict(scenario.get("candidate_params") or {})
    selected_ids = {_sample_identity(row) for row in selected}
    excluded = [row for row in samples if _sample_identity(row) not in selected_ids]
    metrics: dict[str, Any] = {
        "selected_count": len(selected),
        "excluded_count": len(excluded),
        "factor_contribution": {},
    }
    if key == "threshold_tuning":
        thresholds = sorted({
            float(params.get("min_probe_confidence", 75.0)),
            float(params.get("probe_high_confidence", 80.0)),
        })
        variants: list[dict[str, Any]] = []
        for threshold in thresholds:
            variant_params = dict(params)
            variant_params["min_probe_confidence"] = threshold
            variant_rows = simulate_candidate(samples, variant_params)
            variant_metrics = calculate_optimization_metrics(variant_rows)
            variants.append({
                "probe_min_confidence": threshold,
                "metrics": variant_metrics,
                "delta": calculate_metric_delta(production, variant_metrics),
            })
        metrics.update({
            "probe_threshold_variants": variants,
            "factor_contribution": {
                "production_probe_reference": DEFAULT_DECISION_THRESHOLDS.get("probe_min_total"),
                "candidate_probe_thresholds": thresholds,
            },
        })
    elif key == "risk_control":
        hits = [
            row for row in excluded
            if (_number(row.get("final_return_pct")) or 0.0) <= 0
            or (_number(row.get("max_drawdown_pct")) or 0.0) <= -3
        ]
        false_positives = [row for row in excluded if row not in hits]
        contribution_totals = Counter()
        adjusted_scores: list[float] = []
        for row in samples:
            score, contributions = _offline_risk_score(row, params)
            if score is not None:
                adjusted_scores.append(score)
            for factor, value in contributions.items():
                if value > 0:
                    contribution_totals[factor] += 1
        def risk_variant(
            label: str,
            oi_weight: float,
            funding_weight: float,
            cvd_weight: float,
        ) -> dict[str, Any]:
            variant_params = {
                **params,
                "oi_divergence_weight": oi_weight,
                "funding_weight": funding_weight,
                "cvd_divergence_weight": cvd_weight,
            }
            variant_rows = simulate_candidate(samples, variant_params)
            variant_ids = {_sample_identity(row) for row in variant_rows}
            variant_excluded = [row for row in samples if _sample_identity(row) not in variant_ids]
            variant_hits = [
                row for row in variant_excluded
                if (_number(row.get("final_return_pct")) or 0.0) <= 0
                or (_number(row.get("max_drawdown_pct")) or 0.0) <= -3
            ]
            variant_metrics = calculate_optimization_metrics(variant_rows)
            return {
                "variant": label,
                "weights": {
                    "oi_divergence": oi_weight,
                    "funding_crowding": funding_weight,
                    "cvd_divergence": cvd_weight,
                },
                "selected_count": len(variant_rows),
                "excluded_count": len(variant_excluded),
                "risk_hit_rate": _ratio(len(variant_hits), len(variant_excluded)),
                "false_positive_ratio": _ratio(len(variant_excluded) - len(variant_hits), len(variant_excluded)),
                "avoided_drawdown_pct": (
                    round(float(variant_metrics["avg_max_drawdown_pct"]) - float(production["avg_max_drawdown_pct"]), 6)
                    if variant_metrics.get("avg_max_drawdown_pct") is not None and production.get("avg_max_drawdown_pct") is not None else None
                ),
                "metrics": variant_metrics,
                "delta": calculate_metric_delta(production, variant_metrics),
            }
        risk_variants = [
            risk_variant("A", 25.0, 20.0, 20.0),
            risk_variant(
                "B",
                float(params.get("oi_divergence_weight", 35.0)),
                float(params.get("funding_weight", 10.0)),
                float(params.get("cvd_divergence_weight", 20.0)),
            ),
        ]
        metrics.update({
            "risk_hit_count": len(hits),
            "risk_hit_rate": _ratio(len(hits), len(excluded)),
            "false_positive_count": len(false_positives),
            "false_positive_ratio": _ratio(len(false_positives), len(excluded)),
            "avoided_drawdown_pct": (
                round(float(candidate["avg_max_drawdown_pct"]) - float(production["avg_max_drawdown_pct"]), 6)
                if candidate.get("avg_max_drawdown_pct") is not None and production.get("avg_max_drawdown_pct") is not None else None
            ),
            "excluded_avg_max_drawdown_pct": _mean(row.get("max_drawdown_pct") for row in excluded),
            "avg_simulated_risk_score": _mean(adjusted_scores),
            "risk_weight_variants": risk_variants,
            "warning_lead_time": {
                "avg_lead_time_sec": ((risk_history or {}).get("summary") or {}).get("avg_lead_time_sec"),
                "event_count": ((risk_history or {}).get("summary") or {}).get("event_count", 0),
                "source": "persisted_lifecycle_events_and_replay_frames",
                "historical_event_definition": True,
            },
            "factor_contribution": {
                "hit_counts": dict(sorted(contribution_totals.items())),
                "weights": {
                    "oi_divergence": params.get("oi_divergence_weight", 25.0),
                    "funding_crowding": params.get("funding_weight", 20.0),
                    "cvd_divergence": params.get("cvd_divergence_weight", 20.0),
                },
            },
        })
    elif key == "lifecycle_quality":
        base_scores: list[float] = []
        candidate_scores: list[float] = []
        spot_positive = futures_positive = 0
        for row in samples:
            base = _number(row.get("intelligence_score"))
            if base is None:
                base = _number(row.get("lifecycle_score"))
            adjusted, _contributions = _offline_quality_score(row, params)
            if base is not None:
                base_scores.append(base)
            if adjusted is not None:
                candidate_scores.append(adjusted)
            spot_positive += (_number(row.get("spot_cvd_change_from_first")) or 0.0) > 0
            futures_positive += (_number(row.get("futures_cvd_change_from_first")) or 0.0) > 0
        metrics.update({
            "avg_base_quality_score": _mean(base_scores),
            "avg_candidate_quality_score": _mean(candidate_scores),
            "avg_quality_score_delta": (
                round((_mean(candidate_scores) or 0.0) - (_mean(base_scores) or 0.0), 6)
                if base_scores and candidate_scores else None
            ),
            "factor_contribution": {
                "baseline_weights": {"spot_cvd": 10.0, "futures_cvd": 10.0},
                "candidate_weights": {
                    "spot_cvd": params.get("spot_cvd_weight", 10.0),
                    "futures_cvd": params.get("futures_cvd_weight", 10.0),
                },
                "spot_cvd_positive_count": spot_positive,
                "futures_cvd_positive_count": futures_positive,
            },
        })
    elif key == "module_rebalance":
        modules = sorted({str(row.get("module") or row.get("first_signal_module") or "unknown") for row in samples})
        module_metrics: list[dict[str, Any]] = []
        for module in modules:
            base_rows = [row for row in samples if str(row.get("module") or row.get("first_signal_module") or "unknown") == module]
            candidate_rows = [row for row in selected if str(row.get("module") or row.get("first_signal_module") or "unknown") == module]
            base_metric = calculate_optimization_metrics(base_rows)
            candidate_metric = calculate_optimization_metrics(candidate_rows)
            module_metrics.append({
                "module": module,
                "multiplier": params.get(f"module_weight_{module}", 1.0),
                "production": base_metric,
                "candidate": candidate_metric,
                "delta": calculate_metric_delta(base_metric, candidate_metric),
            })
        metrics.update({
            "module_metrics": module_metrics,
            "factor_contribution": {
                key.removeprefix("module_weight_"): value
                for key, value in params.items()
                if key.startswith("module_weight_")
            },
        })
    return metrics


def _simulate_one(
    scenario: dict[str, Any],
    samples: list[dict[str, Any]],
    *,
    settings: Settings,
    source_signature: str,
    scope: dict[str, Any],
    generated_at: str,
    risk_history: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    requires_as_of = scenario["scenario_type"] in {"risk", "lifecycle"}
    cohort = (
        [row for row in samples if str(row.get("as_of_feature_status") or "") == "available"]
        if requires_as_of else list(samples)
    )
    source_production = calculate_optimization_metrics(samples)
    production = calculate_optimization_metrics(cohort)
    selected = simulate_candidate(cohort, scenario["candidate_params"])
    candidate = calculate_optimization_metrics(selected)
    delta = calculate_metric_delta(production, candidate)
    confidence = optimization_confidence(candidate, production=production, delta=delta)
    source_feature_counts = Counter(str(row.get("as_of_feature_status") or "unavailable") for row in samples)
    selected_feature_counts = Counter(str(row.get("as_of_feature_status") or "unavailable") for row in selected)
    temporal_integrity = {
        "mode": "exact_lifecycle_event_only",
        "source_counts": dict(sorted(source_feature_counts.items())),
        "source_sample_count": len(samples),
        "cohort_sample_count": len(cohort),
        "cohort_coverage_ratio": _ratio(len(cohort), len(samples)),
        "selected_counts": dict(sorted(selected_feature_counts.items())),
        "selected_as_of_ratio": _ratio(selected_feature_counts.get("available", 0), len(selected)),
        "look_ahead_fallback_used": False,
    }
    if requires_as_of:
        minimum_coverage_for_confidence = float(
            getattr(settings, "model_optimization_min_asof_coverage_ratio", 0.50)
        )
        temporal_coverage = _number(temporal_integrity.get("cohort_coverage_ratio")) or 0.0
        if temporal_coverage < minimum_coverage_for_confidence:
            confidence["score"] = min(_number(confidence.get("score")) or 0.0, 0.49)
            confidence["label"] = "low_confidence"
            confidence.setdefault("components", {})["as_of_coverage_factor"] = temporal_coverage
    candidate.update({
        "confidence": confidence["score"],
        "confidence_label": confidence["label"],
    })
    scenario_metrics = _scenario_specific_metrics(
        scenario, cohort, selected, production, candidate, risk_history
    )
    scenario_metrics.update({
        "total_source_count": len(samples),
        "comparable_cohort_count": len(cohort),
        "as_of_coverage_ratio": _ratio(len(cohort), len(samples)),
        "excluded_unavailable_count": len(samples) - len(cohort),
    })
    comparison: dict[str, Any] = {
        "scenario_key": scenario["scenario_key"],
        "scenario_type": scenario["scenario_type"],
        "name": scenario["name"],
        "description": scenario["description"],
        "candidate_params": dict(scenario["candidate_params"]),
        "factor_changes": list(scenario.get("factor_changes") or []),
        "production": production,
        "source_production": source_production,
        "candidate": candidate,
        "delta": delta,
        "confidence": confidence,
        "temporal_integrity": temporal_integrity,
        "scenario_metrics": scenario_metrics,
        "source_signature": source_signature,
        "scope": dict(scope),
        "generated_at": generated_at,
        "does_not_modify_model": True,
        "auto_apply": False,
    }
    comparison["readiness"] = evaluate_optimization_readiness(comparison, settings=settings)
    if requires_as_of:
        minimum_coverage = float(getattr(settings, "model_optimization_min_asof_coverage_ratio", 0.50))
        coverage = _number(temporal_integrity.get("cohort_coverage_ratio")) or 0.0
        temporal_ok = (
            bool(selected)
            and selected_feature_counts.get("available", 0) == len(selected)
            and coverage >= minimum_coverage
        )
        comparison["readiness"]["current"]["exact_as_of_features"] = temporal_ok
        comparison["readiness"]["current"]["as_of_cohort_coverage_ratio"] = coverage
        comparison["readiness"]["required"]["exact_as_of_features"] = True
        comparison["readiness"]["required"]["as_of_cohort_coverage_ratio"] = minimum_coverage
        if temporal_ok:
            if "exact_as_of_features" not in comparison["readiness"]["passed"]:
                comparison["readiness"]["passed"].append("exact_as_of_features")
        else:
            comparison["readiness"]["ready"] = False
            comparison["readiness"]["label"] = "候选场景暂未达到验证准入条件"
            if "exact_as_of_features" not in comparison["readiness"]["blocked"]:
                comparison["readiness"]["blocked"].append("exact_as_of_features")
    comparison["status"] = "ready_for_manual_validation" if comparison["readiness"]["ready"] else "insufficient_evidence"
    comparison["recommendations"] = _scenario_recommendations(comparison)
    return comparison


def _aggregate_report(
    comparisons: list[dict[str, Any]],
    *,
    generated_at: str | None = None,
    scope: dict[str, Any] | None = None,
    source_signature: str = "",
) -> dict[str, Any]:
    ready = [item for item in comparisons if (item.get("readiness") or {}).get("ready")]
    ranked = sorted(
        comparisons,
        key=lambda item: (
            _number((item.get("delta") or {}).get("success_ratio_delta")) or -999,
            _number((item.get("delta") or {}).get("avg_drawdown_improvement_pct")) or -999,
        ),
        reverse=True,
    )
    recommendations = [
        {**recommendation, "scenario_key": item.get("scenario_key")}
        for item in comparisons
        for recommendation in item.get("recommendations") or []
    ]
    report = {
        "ok": True,
        "optimization_version": OPTIMIZATION_VERSION,
        "model_version": MODEL_VERSION,
        "generated_at": generated_at or _now(),
        "status": "candidate_ready_for_manual_validation" if ready else "production_model_unchanged",
        "source_signature": source_signature,
        "production_model_fingerprint": production_model_fingerprint(),
        "scope": dict(scope or {"global": True, "symbol": None, "limit": None}),
        "summary": {
            "scenario_count": len(comparisons),
            "ready_scenario_count": len(ready),
            "best_scenario_key": ranked[0].get("scenario_key") if ranked else None,
            "production_sample_count": safe_int((comparisons[0].get("production") or {}).get("sample_count")) if comparisons else 0,
            "candidate_selected_counts": {
                str(item.get("scenario_key")): safe_int((item.get("candidate") or {}).get("sample_count"))
                for item in comparisons
            },
        },
        "production_model": production_model_snapshot(),
        "base_model": production_model_snapshot(),
        "scenarios": [
            {
                key: item.get(key)
                for key in ("scenario_key", "scenario_type", "name", "description", "candidate_params", "factor_changes", "status", "confidence")
            }
            for item in comparisons
        ],
        "comparisons": comparisons,
        "runs": comparisons,
        "recommendations": recommendations,
        "readiness": {
            "ready": bool(ready),
            "ready_scenarios": [item.get("scenario_key") for item in ready],
            "blocked_scenarios": [item.get("scenario_key") for item in comparisons if item not in ready],
            "label": "存在候选场景达到人工验证条件" if ready else "暂无候选场景达到人工验证条件",
            "does_not_modify_model": True,
            "auto_apply": False,
        },
        "does_not_modify_model": True,
        "auto_apply": False,
        "not_advice": NOT_ADVICE,
    }
    return report


def _run_key(
    scenario: Mapping[str, Any],
    source_signature: str,
    *,
    nonce: str = "",
) -> str:
    payload = json.dumps(
        {
            "optimization_version": OPTIMIZATION_VERSION,
            "model_version": MODEL_VERSION,
            "scenario_key": scenario.get("scenario_key"),
            "candidate_params": scenario.get("candidate_params"),
            "source_signature": source_signature,
            "nonce": nonce,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _metric_rows(comparison: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    production = dict(comparison.get("production") or {})
    candidate = dict(comparison.get("candidate") or {})
    delta = dict(comparison.get("delta") or {})
    for metric_key, delta_key in (
        ("success_ratio", "success_ratio_delta"),
        ("decision_accuracy", "decision_accuracy_delta"),
        ("avg_return_pct", "avg_return_delta_pct"),
        ("median_return_pct", "median_return_delta_pct"),
        ("avg_max_gain_pct", "avg_max_gain_delta_pct"),
        ("avg_max_drawdown_pct", "avg_drawdown_improvement_pct"),
        ("drawdown_event_ratio", "drawdown_event_ratio_delta"),
        ("expectancy_pct", "expectancy_delta_pct"),
        ("risk_adjusted_score", "risk_adjusted_score_delta"),
    ):
        rows.append({
            "metric_scope": "comparison",
            "metric_type": "summary",
            "metric_key": metric_key,
            "sample_count": safe_int(candidate.get("sample_count")),
            "production_value": _number(production.get(metric_key)),
            "candidate_value": _number(candidate.get(metric_key)),
            "delta_value": _number(delta.get(delta_key)),
            "metric_value": _number(delta.get(delta_key)),
            "metrics": {
                "production": production.get(metric_key),
                "candidate": candidate.get(metric_key),
                "delta": delta.get(delta_key),
            },
        })
    for horizon in HORIZONS:
        base_values = dict((production.get("horizons") or {}).get(horizon) or {})
        candidate_values = dict((candidate.get("horizons") or {}).get(horizon) or {})
        delta_values = dict((delta.get("horizons") or {}).get(horizon) or {})
        for metric_key, delta_key in (
            ("success_ratio", "success_ratio_delta"),
            ("avg_return_pct", "avg_return_delta_pct"),
        ):
            rows.append({
                "metric_scope": "comparison",
                "metric_type": "horizon",
                "metric_key": f"{horizon}:{metric_key}",
                "sample_count": safe_int(candidate_values.get("sample_count")),
                "production_value": _number(base_values.get(metric_key)),
                "candidate_value": _number(candidate_values.get(metric_key)),
                "delta_value": _number(delta_values.get(delta_key)),
                "metric_value": _number(delta_values.get(delta_key)),
                "metrics": {
                    "horizon": horizon,
                    "production": base_values.get(metric_key),
                    "candidate": candidate_values.get(metric_key),
                    "delta": delta_values.get(delta_key),
                },
            })
    return rows


def _cached_comparisons(
    settings: Settings,
    scenarios: list[dict[str, Any]],
    source_signature: str,
) -> list[dict[str, Any]] | None:
    conn = _readonly(Path(settings.lifecycle_db_path))
    if conn is None:
        return None
    try:
        if not {"optimization_scenarios", "optimization_runs", "optimization_metrics"}.issubset(_tables(conn)):
            return None
        runs = IntelligenceStore(settings).latest_optimization_runs(
            scenario_keys=[str(item["scenario_key"]) for item in scenarios],
            optimization_version=OPTIMIZATION_VERSION,
            model_version=MODEL_VERSION,
            source_signature=source_signature,
            conn=conn,
        )
    finally:
        conn.close()
    by_key = {str(item.get("scenario_key")): item for item in runs}
    comparisons: list[dict[str, Any]] = []
    for scenario in scenarios:
        run = by_key.get(str(scenario["scenario_key"]))
        if not run:
            return None
        report = run.get("report")
        if not isinstance(report, dict):
            return None
        if dict(report.get("candidate_params") or {}) != dict(scenario.get("candidate_params") or {}):
            return None
        comparisons.append(dict(report))
    return comparisons


def run_optimization(
    settings: Settings | None = None,
    *,
    scenario: str = "",
    symbol: str = "",
    limit: int | None = None,
    dry_run: bool = False,
    force: bool = False,
    write_reports: bool = True,
    candidate_params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    loaded = settings or Settings.load()
    fingerprint_before = production_model_fingerprint()
    if fingerprint_before != PRODUCTION_MODEL_FINGERPRINT:
        return {
            "ok": False,
            "error": "production_model_fingerprint_mismatch",
            "optimization_version": OPTIMIZATION_VERSION,
            "does_not_modify_model": True,
            "auto_apply": False,
        }
    try:
        scenario_key = _normalize_scenario(scenario)
        normalized_symbol = normalize_lifecycle_symbol(symbol)
        if symbol and not normalized_symbol:
            raise ValueError("invalid_symbol")
        if normalized_symbol and not dry_run:
            raise ValueError("symbol_scope_requires_dry_run")
        selected_keys = list(SCENARIO_KEYS) if scenario_key == "all" else [scenario_key]
        if candidate_params and len(selected_keys) != 1:
            raise ValueError("candidate_params require one explicit scenario")
        scenarios = [
            _scenario_definition(key, candidate_params if len(selected_keys) == 1 else None)
            for key in selected_keys
        ]
    except ValueError as exc:
        return {
            "ok": False,
            "error": str(exc),
            "optimization_version": OPTIMIZATION_VERSION,
            "does_not_modify_model": True,
            "auto_apply": False,
        }
    source = _load_mature_samples(loaded, symbol=normalized_symbol, limit=limit)
    samples = list(source["samples"])
    signature = str(source["source_signature"])
    global_scope = not normalized_symbol and limit is None
    scope = {"global": global_scope, "symbol": normalized_symbol or None, "limit": limit}
    if global_scope and not dry_run and not force:
        cached = _cached_comparisons(loaded, scenarios, signature)
        if cached is not None:
            report = _aggregate_report(cached, scope=scope, source_signature=signature)
            report.update({"cached": True, "skipped": True, "persisted": True})
            if write_reports and len(selected_keys) == len(SCENARIO_KEYS):
                report["report_files"] = write_optimization_report_files(report)
            return report
    generated_at = _now()
    comparisons = [
        _simulate_one(
            definition,
            samples,
            settings=loaded,
            source_signature=signature,
            scope=scope,
            generated_at=generated_at,
            risk_history=source.get("risk_history") or {},
        )
        for definition in scenarios
    ]
    fingerprint_after = production_model_fingerprint()
    if fingerprint_after != fingerprint_before:
        return {
            "ok": False,
            "error": "production_model_changed_during_simulation",
            "optimization_version": OPTIMIZATION_VERSION,
            "does_not_modify_model": True,
            "auto_apply": False,
        }
    report = _aggregate_report(
        comparisons,
        generated_at=generated_at,
        scope=scope,
        source_signature=signature,
    )
    report.update({
        "dry_run": bool(dry_run),
        "cached": False,
        "skipped": False,
        "persisted": False,
        "source_status_counts": source["status_counts"],
        "linked_outcome_count": source["linked_outcome_count"],
    })
    if dry_run or not global_scope:
        return report
    nonce = generated_at if force else ""
    run_records = []
    for definition, comparison in zip(scenarios, comparisons):
        run_records.append({
            "scenario_key": definition["scenario_key"],
            "run_key": _run_key(definition, signature, nonce=nonce),
            "optimization_version": OPTIMIZATION_VERSION,
            "model_version": MODEL_VERSION,
            "source_signature": signature,
            "scope": scope,
            "sample_count": safe_int((comparison.get("production") or {}).get("sample_count")),
            "mature_sample_count": safe_int((comparison.get("production") or {}).get("sample_count")),
            "selected_sample_count": safe_int((comparison.get("candidate") or {}).get("sample_count")),
            "confidence": (comparison.get("confidence") or {}).get("score"),
            "confidence_label": (comparison.get("confidence") or {}).get("label"),
            "status": comparison.get("status"),
            "production": comparison.get("production"),
            "candidate": comparison.get("candidate"),
            "delta": comparison.get("delta"),
            "recommendations": comparison.get("recommendations"),
            "readiness": comparison.get("readiness"),
            "report": comparison,
            "metrics": _metric_rows(comparison),
            "result": comparison,
            "started_at": generated_at,
            "finished_at": generated_at,
            "generated_at": generated_at,
        })
    stored = IntelligenceStore(loaded).write_optimization_bundle(scenarios, run_records)
    report["run_ids"] = [safe_int(item.get("id")) for item in stored]
    report["persisted"] = True
    if write_reports and len(selected_keys) == len(SCENARIO_KEYS):
        report["report_files"] = write_optimization_report_files(report)
    return report


def _report_from_stored_runs(
    runs: list[dict[str, Any]],
    *,
    expected_scenarios: Iterable[str] = SCENARIO_KEYS,
) -> dict[str, Any]:
    expected = list(expected_scenarios)
    comparisons = [dict(run.get("report") or {}) for run in runs if isinstance(run.get("report"), dict)]
    generated_at = max((str(run.get("generated_at") or "") for run in runs), default="") or _now()
    signature = str(runs[0].get("source_signature") or "") if runs else ""
    report = _aggregate_report(comparisons, generated_at=generated_at, source_signature=signature)
    report.update({
        "available": bool(comparisons),
        "complete": len({str(run.get("scenario_key") or "") for run in runs}) == len(expected),
        "cached": True,
        "persisted": True,
        "run_ids": [safe_int(run.get("id")) for run in runs],
    })
    if not report["complete"]:
        report["status"] = "partial_optimization_report"
        report["summary"]["missing_scenarios"] = [
            key for key in expected
            if key not in {str(run.get("scenario_key") or "") for run in runs}
        ]
    return report


def get_optimization_report(
    settings: Settings | None = None,
    *,
    scenario: str = "",
) -> dict[str, Any]:
    loaded = settings or Settings.load()
    try:
        scenario_key = _normalize_scenario(scenario)
    except ValueError as exc:
        return {
            "ok": False,
            "available": False,
            "error": str(exc),
            "does_not_modify_model": True,
            "auto_apply": False,
        }
    selected_keys = list(SCENARIO_KEYS) if scenario_key == "all" else [scenario_key]
    conn = _readonly(Path(loaded.lifecycle_db_path))
    if conn is None:
        return {
            "ok": False,
            "available": False,
            "error": "optimization_report_unavailable",
            "does_not_modify_model": True,
            "auto_apply": False,
        }
    try:
        if not {"optimization_scenarios", "optimization_runs", "optimization_metrics"}.issubset(_tables(conn)):
            return {
                "ok": False,
                "available": False,
                "error": "optimization_report_unavailable",
                "does_not_modify_model": True,
                "auto_apply": False,
            }
        placeholders = ",".join("?" for _ in selected_keys)
        signature_row = conn.execute(
            "SELECT r.source_signature,COUNT(DISTINCT s.scenario_key) AS scenario_count,"
            "MAX(r.id) AS latest_id FROM optimization_runs r "
            "JOIN optimization_scenarios s ON s.id=r.scenario_id "
            "WHERE r.optimization_version=? AND r.model_version=? "
            f"AND s.scenario_key IN ({placeholders}) "
            "GROUP BY r.source_signature "
            "ORDER BY (COUNT(DISTINCT s.scenario_key) >= ?) DESC, MAX(r.id) DESC LIMIT 1",
            (OPTIMIZATION_VERSION, MODEL_VERSION, *selected_keys, len(selected_keys)),
        ).fetchone()
        selected_signature = str(signature_row[0] or "") if signature_row is not None else ""
        runs = IntelligenceStore(loaded).latest_optimization_runs(
            scenario_keys=selected_keys,
            optimization_version=OPTIMIZATION_VERSION,
            model_version=MODEL_VERSION,
            source_signature=selected_signature,
            conn=conn,
        )
    finally:
        conn.close()
    if not runs:
        return {
            "ok": False,
            "available": False,
            "error": "optimization_report_unavailable",
            "does_not_modify_model": True,
            "auto_apply": False,
        }
    return _report_from_stored_runs(runs, expected_scenarios=selected_keys)


def write_optimization_report_files(
    report: Mapping[str, Any],
    *,
    json_path: Path = OPTIMIZATION_REPORT_JSON,
    markdown_path: Path = OPTIMIZATION_REPORT_MD,
) -> dict[str, str]:
    safe = {
        key: report.get(key)
        for key in (
            "ok", "optimization_version", "model_version", "generated_at", "status",
            "summary", "production_model", "base_model", "scenarios", "comparisons",
            "recommendations", "readiness", "production_model_fingerprint",
            "does_not_modify_model", "auto_apply", "not_advice",
        )
    }
    atomic_write_text(json_path, json.dumps(safe, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    lines = [
        "# Model Optimization Simulation Report",
        "",
        f"- Optimization version: {report.get('optimization_version')}",
        f"- Production model: {report.get('model_version')}",
        f"- Generated: {report.get('generated_at')}",
        f"- Status: {report.get('status')}",
        "- Production model remains immutable; all candidate parameters are simulation-only.",
        "",
        "## Scenario Comparisons",
        "",
        "| Scenario | Production samples | Candidate samples | Success delta | Return delta | Drawdown improvement | Confidence | Ready |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for item in report.get("comparisons") or []:
        production = item.get("production") or {}
        candidate = item.get("candidate") or {}
        delta = item.get("delta") or {}
        confidence = item.get("confidence") or {}
        readiness = item.get("readiness") or {}
        lines.append(
            "| {name} | {base} | {candidate} | {success} | {returns} | {drawdown} | {confidence} | {ready} |".format(
                name=str(item.get("name") or item.get("scenario_key") or "-").replace("|", "/"),
                base=safe_int(production.get("sample_count")),
                candidate=safe_int(candidate.get("sample_count")),
                success="-" if delta.get("success_ratio_delta") is None else f"{float(delta['success_ratio_delta']):.2%}",
                returns="-" if delta.get("avg_return_delta_pct") is None else f"{float(delta['avg_return_delta_pct']):.4f}%",
                drawdown="-" if delta.get("avg_drawdown_improvement_pct") is None else f"{float(delta['avg_drawdown_improvement_pct']):.4f}%",
                confidence=f"{float(confidence.get('score') or 0):.2%}",
                ready="yes" if readiness.get("ready") else "no",
            )
        )
    lines.extend(["", "## Recommendations", ""])
    for item in report.get("recommendations") or []:
        lines.append(
            f"- `{item.get('scenario_key', '-')}/{item.get('key', 'review')}`: "
            f"{item.get('recommendation', '')} (auto_apply={bool(item.get('auto_apply'))})"
        )
    lines.extend([
        "",
        "## Boundaries",
        "",
        "- Only linked, mature, successful stored Outcome rows are replayed.",
        "- No exchange API is called and no historical Outcome is recomputed or rewritten.",
        "- Candidate recommendations require manual review and are never applied automatically.",
        f"- {report.get('not_advice') or NOT_ADVICE}",
    ])
    atomic_write_text(markdown_path, "\n".join(lines) + "\n")
    return {"json": str(json_path), "markdown": str(markdown_path)}


def generate_optimization_report(
    settings: Settings | None = None,
    *,
    scenario: str = "",
    write_reports: bool = True,
) -> dict[str, Any]:
    """Aggregate persisted runs only. This function never starts a simulation."""

    report = get_optimization_report(settings, scenario=scenario)
    if report.get("ok") and write_reports and _normalize_scenario(scenario) == "all":
        report["report_files"] = write_optimization_report_files(report)
    return report


def optimization_readiness(
    settings: Settings | None = None,
    *,
    report: Mapping[str, Any] | None = None,
    scenario: str = "",
) -> dict[str, Any]:
    current = dict(report or get_optimization_report(settings, scenario=scenario))
    if report is not None and _normalize_scenario(scenario) != "all":
        scenario_key = _normalize_scenario(scenario)
        matches = [
            dict(item) for item in current.get("comparisons") or []
            if str(item.get("scenario_key") or "") == scenario_key
        ]
        current = _aggregate_report(matches) if matches else {
            "ok": False,
            "error": "optimization_scenario_unavailable",
        }
    if not current.get("ok"):
        return {
            "ok": False,
            "ready": False,
            "label": "暂无持久化优化模拟报告",
            "blocked": ["optimization_report_unavailable"],
            "does_not_modify_model": True,
            "auto_apply": False,
        }
    normalized_scenario = _normalize_scenario(scenario)
    comparisons = [dict(item) for item in current.get("comparisons") or []]
    if normalized_scenario != "all" and comparisons:
        readiness = dict(comparisons[0].get("readiness") or {})
    else:
        readiness = dict(current.get("readiness") or {})
        readiness["scenarios"] = [
            {
                "scenario_key": item.get("scenario_key"),
                **dict(item.get("readiness") or {}),
            }
            for item in comparisons
        ]
    return {
        "ok": True,
        **readiness,
        "optimization_version": OPTIMIZATION_VERSION,
        "model_version": MODEL_VERSION,
        "does_not_modify_model": True,
        "auto_apply": False,
    }


# Compatibility aliases for CLI/job adapters.
run_model_optimization = run_optimization
get_model_optimization_report = get_optimization_report
model_optimization_readiness = optimization_readiness


__all__ = [
    "BUILTIN_SCENARIOS",
    "MODEL_VERSION",
    "OPTIMIZATION_VERSION",
    "PARAMETER_RANGES",
    "PRODUCTION_MODEL",
    "PRODUCTION_MODEL_FINGERPRINT",
    "SCENARIO_KEYS",
    "calculate_metric_delta",
    "calculate_optimization_metrics",
    "evaluate_optimization_readiness",
    "generate_optimization_report",
    "get_model_optimization_report",
    "get_optimization_report",
    "list_optimization_scenarios",
    "model_optimization_readiness",
    "optimization_confidence",
    "optimization_readiness",
    "production_model_snapshot",
    "production_model_fingerprint",
    "run_model_optimization",
    "run_optimization",
    "simulate_candidate",
    "validate_candidate_params",
    "write_optimization_report_files",
]
