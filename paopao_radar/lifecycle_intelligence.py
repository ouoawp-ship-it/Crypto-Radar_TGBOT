from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .config import Settings
from .lifecycle_store import LifecycleStore, normalize_lifecycle_symbol


INTELLIGENCE_MODEL_VERSION = "lifecycle-intelligence-v1"
NOT_ADVICE = "仅用于信号整理和风险提示，不构成投资建议，不执行自动交易。"

LEVEL_RANK = {"unknown": 0, "15m": 1, "1h": 2, "4h": 3, "24h": 4}
UPGRADE_LEVEL = {
    "timeframe_upgrade_1h": "1h",
    "timeframe_upgrade_4h": "4h",
    "timeframe_upgrade_24h": "24h",
}
RISK_EVENTS = {
    "risk_warning",
    "oi_price_divergence",
    "cvd_divergence",
    "funding_crowded",
    "short_term_weakening",
    "major_timeframe_weakening",
    "launch_failed",
}

LIFECYCLE_SOURCE_PROJECTION = """
    l.id, l.symbol, l.first_signal_at, l.first_signal_level,
    l.current_state, l.highest_level, l.highest_level_rank,
    l.lifecycle_score, l.risk_score, l.latest_funding_rate,
    l.price_change_from_first_pct, l.oi_change_from_first_pct,
    l.futures_cvd_change_from_first, l.spot_cvd_change_from_first,
    l.metrics_json, l.is_active, l.updated_at, l.closed_at
"""
EVENT_SOURCE_PROJECTION = "id, lifecycle_id, event_time, event_type, event_score"


def _number(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    return {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
    return []


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, float(value)))


def quality_label(score: float) -> str:
    value = _clamp(score)
    if value >= 90:
        return "强趋势确认"
    if value >= 80:
        return "高质量启动"
    if value >= 70:
        return "启动有效"
    if value >= 60:
        return "启动观察"
    if value >= 40:
        return "动能不足"
    if value >= 20:
        return "风险升高"
    return "启动失败"


def build_upgrade_path(lifecycle: dict[str, Any], events: Iterable[dict[str, Any]]) -> str:
    first = str(lifecycle.get("first_signal_level") or "unknown")
    levels = [first]
    for event in sorted(events, key=lambda item: (_timestamp(item.get("event_time")) or 0, int(item.get("id") or 0))):
        level = UPGRADE_LEVEL.get(str(event.get("event_type") or ""))
        if level and level not in levels:
            levels.append(level)
    highest = str(lifecycle.get("highest_level") or "unknown")
    if highest not in {"", "unknown"} and highest not in levels:
        levels.append(highest)
    return " → ".join(levels or ["unknown"])


def _component_values(
    lifecycle: dict[str, Any],
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    metrics = _mapping(lifecycle.get("metrics") or lifecycle.get("metrics_json"))
    event_types = [str(item.get("event_type") or "") for item in events]
    price_change = _number(lifecycle.get("price_change_from_first_pct"), 0.0) or 0.0
    oi_change = _number(lifecycle.get("oi_change_from_first_pct"))
    if oi_change is None:
        oi_change = _number(metrics.get("oi_change_from_first_pct"), 0.0) or 0.0
    volume_multiplier = _number(metrics.get("volume_multiplier"))
    if volume_multiplier is None:
        volume_change = _number(metrics.get("volume_change_pct"))
        volume_multiplier = 1.0 + (volume_change / 100.0) if volume_change is not None else None
    spot_change = _number(lifecycle.get("spot_cvd_change_from_first"))
    if spot_change is None:
        spot_change = _number(metrics.get("spot_cvd_delta"))
    futures_change = _number(lifecycle.get("futures_cvd_change_from_first"))
    if futures_change is None:
        futures_change = _number(metrics.get("futures_cvd_delta"))
    funding = _number(lifecycle.get("latest_funding_rate"))
    if funding is None:
        funding = _number(metrics.get("funding_rate"))
    spot_confirmed = bool((spot_change is not None and spot_change > 0) or "spot_cvd_confirmed" in event_types)
    futures_confirmed = bool(
        (futures_change is not None and futures_change > 0) or "futures_cvd_confirmed" in event_types
    )
    oi_confirmed = bool(oi_change >= 8 or "oi_accumulation" in event_types)
    volume_confirmed = bool(
        (volume_multiplier is not None and volume_multiplier >= 2.0) or "volume_expansion" in event_types
    )
    return {
        "metrics": metrics,
        "event_types": event_types,
        "price_change_pct": price_change,
        "oi_change_pct": oi_change,
        "volume_multiplier": volume_multiplier,
        "spot_cvd_change": spot_change,
        "futures_cvd_change": futures_change,
        "funding_rate": funding,
        "spot_confirmed": spot_confirmed,
        "futures_confirmed": futures_confirmed,
        "oi_confirmed": oi_confirmed,
        "volume_confirmed": volume_confirmed,
    }


def identify_lifecycle_stage(
    lifecycle: dict[str, Any],
    events: list[dict[str, Any]],
    *,
    values: dict[str, Any] | None = None,
) -> tuple[str, str]:
    values = values or _component_values(lifecycle, events)
    state = str(lifecycle.get("current_state") or "")
    active = int(lifecycle.get("is_active", 1) or 0) == 1
    event_types = values["event_types"]
    rank = LEVEL_RANK.get(str(lifecycle.get("highest_level") or "unknown"), 0)
    risk_score = _number(lifecycle.get("risk_score"), 0.0) or 0.0
    price_change = values["price_change_pct"]
    max_drawdown = _number(values.get("max_drawdown_pct"))

    if state == "failed" or "launch_failed" in event_types:
        return "failure", "启动失败"
    if state == "closed" or not active:
        return "closed", "生命周期结束"
    if state == "risk_warning" or risk_score >= 60 or (max_drawdown is not None and max_drawdown <= -10) or any(
        item in event_types for item in ("oi_price_divergence", "cvd_divergence", "major_timeframe_weakening")
    ):
        return "distribution_risk", "派发风险"
    if state == "cooling" or "short_term_weakening" in event_types or (max_drawdown is not None and max_drawdown <= -5):
        return "cooling", "短线冷却"
    if rank >= 3 and price_change > 0 and (values["spot_confirmed"] or values["oi_confirmed"]):
        return "trend_expansion", "趋势扩张"
    if any(item.startswith("timeframe_upgrade_") for item in event_types):
        return "timeframe_upgrade", "周期升级"
    if rank >= 2 or "same_level_confirm" in event_types:
        return "confirmed_launch", "启动确认"
    if len(events) > 1 or price_change > 0 or values["volume_confirmed"]:
        return "early_launch", "早期启动"
    return "discovery", "首次发现"


def _capital_confirmation(values: dict[str, Any]) -> str:
    if values["spot_confirmed"] and values["futures_confirmed"]:
        return "现货与合约同步确认"
    if values["spot_confirmed"]:
        return "仅现货 CVD 确认"
    if values["futures_confirmed"]:
        return "仅合约 CVD 确认"
    if values["oi_confirmed"]:
        return "OI 确认但 CVD 未确认"
    if values["volume_confirmed"]:
        return "成交量确认但 OI 未确认"
    return "无资金确认"


def _source_signature(
    lifecycle: dict[str, Any],
    events: list[dict[str, Any]],
    replay: dict[str, Any] | None,
    outcome: dict[str, Any] | None,
) -> str:
    source = {
        "lifecycle_id": lifecycle.get("id"),
        "updated_at": lifecycle.get("updated_at"),
        "state": lifecycle.get("current_state"),
        "score": lifecycle.get("lifecycle_score"),
        "risk": lifecycle.get("risk_score"),
        "events": [
            (item.get("id"), item.get("event_time"), item.get("event_type"), item.get("event_score"))
            for item in events
        ],
        "replay": (replay or {}).get("updated_at") or (replay or {}).get("calculated_at"),
        "outcome": {
            key: (outcome or {}).get(key)
            for key in (
                "id", "coverage_label", "maturity_label", "linked_outcome_count",
                "mature_horizon_count", "link_coverage_ratio", "maturity_ratio",
                "horizon_1h_status", "horizon_4h_status", "horizon_24h_status", "horizon_72h_status",
            )
        },
    }
    payload = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def evaluate_lifecycle(
    lifecycle: dict[str, Any],
    events: Iterable[dict[str, Any]],
    replay: dict[str, Any] | None = None,
    outcome: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an independent research score without mutating lifecycle scoring.

    The calculation deliberately consumes only persisted lifecycle facts.  It
    never changes ``lifecycle_score`` / ``risk_score`` and never produces a
    trading instruction.
    """
    ordered_events = sorted(
        (dict(item) for item in events),
        key=lambda item: (_timestamp(item.get("event_time")) or 0, int(item.get("id") or 0)),
    )
    values = _component_values(lifecycle, ordered_events)
    values["max_drawdown_pct"] = _number((replay or {}).get("max_drawdown_pct"))
    event_types = values["event_types"]
    highest = str(lifecycle.get("highest_level") or "unknown")
    highest_rank = LEVEL_RANK.get(highest, int(lifecycle.get("highest_level_rank") or 0))
    lifecycle_score = _clamp(_number(lifecycle.get("lifecycle_score"), 0.0) or 0.0)

    strength = round(lifecycle_score * 0.25, 4)
    upgrade = {0: 0.0, 1: 4.0, 2: 10.0, 3: 16.0, 4: 20.0}.get(highest_rank, 0.0)
    price_change = values["price_change_pct"]
    if price_change >= 10:
        price = 15.0
    elif price_change >= 5:
        price = 12.0
    elif price_change >= 2:
        price = 9.0
    elif price_change > 0:
        price = 6.0
    elif price_change > -3:
        price = 3.0
    else:
        price = 0.0
    volume = 10.0 if values["volume_confirmed"] else 4.0 if (values["volume_multiplier"] or 0) > 1 else 0.0
    oi = 10.0 if values["oi_confirmed"] else 4.0 if values["oi_change_pct"] > 0 else 0.0
    spot = 10.0 if values["spot_confirmed"] else 0.0
    futures = 5.0 if values["futures_confirmed"] else 0.0
    funding = 5.0 if values["funding_rate"] is None or abs(values["funding_rate"]) < 0.0008 else 0.0

    penalties: dict[str, float] = {}
    if values["funding_rate"] is not None and abs(values["funding_rate"]) >= 0.0008:
        penalties["funding_overheated"] = 12.0
    if values["oi_change_pct"] >= 8 and price_change < 0:
        penalties["oi_up_price_down"] = 15.0
    if values["futures_confirmed"] and not values["spot_confirmed"]:
        penalties["futures_without_spot"] = 8.0
    if price_change >= 15 and highest_rank < 3:
        penalties["fast_rise_before_major_confirmation"] = 8.0
    false_breaks = sum(event_types.count(item) for item in ("short_term_weakening", "major_timeframe_weakening"))
    if false_breaks:
        penalties["repeated_weakening"] = min(12.0, 4.0 * false_breaks)
    if "launch_failed" in event_types or str(lifecycle.get("current_state")) == "failed":
        penalties["launch_failed"] = 20.0
    existing_risk = _number(lifecycle.get("risk_score"), 0.0) or 0.0
    if existing_risk > 0:
        penalties["persisted_risk"] = min(8.0, existing_risk * 0.08)
    started = _timestamp(lifecycle.get("first_signal_at"))
    ended = _timestamp(lifecycle.get("closed_at") or lifecycle.get("updated_at")) or time.time()
    duration_sec = max(0, int(ended - started)) if started is not None else None
    if duration_sec is not None and duration_sec >= 7 * 86400 and highest_rank <= 1:
        penalties["stale_without_upgrade"] = 8.0

    components = {
        "base_lifecycle_strength": strength,
        "timeframe_upgrade_quality": upgrade,
        "price_structure": price,
        "volume_confirmation": volume,
        "oi_confirmation": oi,
        "spot_cvd_confirmation": spot,
        "futures_cvd_confirmation": futures,
        "funding_health": funding,
    }
    base_score = sum(components.values())
    risk_penalty = sum(penalties.values())
    score = round(_clamp(base_score - risk_penalty), 2)
    stage, stage_label = identify_lifecycle_stage(lifecycle, ordered_events, values=values)
    capital_label = _capital_confirmation(values)

    if stage in {"failure", "distribution_risk", "cooling"} or price_change < -5:
        momentum_label = "动能走弱"
    elif price_change >= 5 and (values["spot_confirmed"] or values["oi_confirmed"]):
        momentum_label = "趋势增强"
    elif price_change > 0:
        momentum_label = "温和增强"
    else:
        momentum_label = "等待确认"
    risk_label = "高风险" if risk_penalty >= 20 or existing_risk >= 70 else "中风险" if risk_penalty >= 8 or existing_risk >= 40 else "低风险"
    maturity_label = {
        "24h": "24H 周期确认",
        "4h": "4H 周期确认",
        "1h": "1H 周期确认",
        "15m": "15M 启动观察",
    }.get(highest, "周期待识别")
    observed_metrics = sum(
        value is not None
        for value in (
            values["volume_multiplier"],
            values["oi_change_pct"],
            values["spot_cvd_change"],
            values["futures_cvd_change"],
            values["funding_rate"],
        )
    )
    mature_horizon_count = int((outcome or {}).get("mature_horizon_count") or 0)
    linked_outcome_count = int((outcome or {}).get("linked_outcome_count") or 0)
    has_coverage_record = bool(outcome)
    if len(ordered_events) >= 4 and observed_metrics >= 4 and (replay or outcome) and mature_horizon_count > 0:
        confidence_label = "高置信度"
    elif len(ordered_events) >= 2 and observed_metrics >= 2:
        confidence_label = "可参考"
    else:
        confidence_label = "样本积累中"
    if has_coverage_record and mature_horizon_count <= 0:
        confidence_label = "样本积累中" if linked_outcome_count <= 0 else "待 Outcome 成熟"

    strengths: list[str] = []
    risks: list[str] = []
    watch_points: list[str] = []
    if highest_rank >= 2:
        strengths.append(f"生命周期已确认至 {highest} 周期。")
    if values["spot_confirmed"] and values["futures_confirmed"]:
        strengths.append("现货与合约主动买盘同步确认。")
    elif values["spot_confirmed"]:
        strengths.append("现货主动买盘已确认。")
    if values["oi_confirmed"]:
        strengths.append("OI 增长达到资金确认条件。")
    if values["volume_confirmed"]:
        strengths.append("成交量相对首次阶段明显放大。")
    penalty_messages = {
        "funding_overheated": "资金费率处于过热区间。",
        "oi_up_price_down": "OI 增长但价格走弱，存在杠杆堆积风险。",
        "futures_without_spot": "合约 CVD 增强但现货 CVD 未同步。",
        "fast_rise_before_major_confirmation": "大周期确认前价格快速拉升。",
        "repeated_weakening": "生命周期内出现连续走弱或假突破事件。",
        "launch_failed": "生命周期已出现启动失败事件。",
        "stale_without_upgrade": "持续时间较长且尚未完成周期升级。",
    }
    risks.extend(message for key, message in penalty_messages.items() if key in penalties)
    if not values["spot_confirmed"]:
        watch_points.append("观察现货 CVD 是否出现持续确认。")
    if not values["oi_confirmed"]:
        watch_points.append("观察 OI 是否与价格同向增长。")
    if values["funding_rate"] is None:
        watch_points.append("资金费率数据不足，等待后续快照。")
    elif abs(values["funding_rate"]) < 0.0008:
        watch_points.append("持续观察资金费率是否进入拥挤区间。")
    if has_coverage_record and mature_horizon_count <= 0:
        watch_points.append("Outcome 尚未成熟；尚未到期、pending 或 unavailable 均不代表失败。")

    path = str((replay or {}).get("upgrade_path") or build_upgrade_path(lifecycle, ordered_events))
    funding_text = "资金费率数据待补充" if values["funding_rate"] is None else (
        "资金费率过热" if abs(values["funding_rate"]) >= 0.0008 else "资金费率尚未过热"
    )
    summary = (
        f"{str(lifecycle.get('first_signal_level') or 'unknown')} 首次信号，当前路径 {path}；"
        f"{capital_label}，{momentum_label}，{funding_text}。"
    )
    now = _utc_now()
    return {
        "lifecycle_id": int(lifecycle.get("id") or 0),
        "symbol": str(lifecycle.get("symbol") or "").upper(),
        "intelligence_score": score,
        "quality_label": quality_label(score),
        "stage": stage,
        "stage_label": stage_label,
        "momentum_label": momentum_label,
        "capital_confirmation_label": capital_label,
        "risk_label": risk_label,
        "maturity_label": maturity_label,
        "confidence_label": confidence_label,
        "summary": summary,
        "strengths": strengths,
        "risks": risks,
        "watch_points": watch_points,
        "factors": {
            "components": components,
            "base_score": round(base_score, 2),
            "risk_penalties": penalties,
            "risk_penalty": round(risk_penalty, 2),
            "upgrade_path": path,
            "duration_sec": duration_sec,
            "event_count": len(ordered_events),
            "source_lifecycle_score": lifecycle_score,
            "source_risk_score": existing_risk,
            "max_drawdown_pct": values["max_drawdown_pct"],
            "outcome_link_status": str((outcome or {}).get("coverage_label") or "unlinked"),
            "outcome_maturity_label": str((outcome or {}).get("maturity_label") or "无数据"),
            "outcome_mature_horizon_count": mature_horizon_count,
            "outcome_linked_outcome_count": linked_outcome_count,
            "outcome_coverage_ratio": _number((outcome or {}).get("link_coverage_ratio")),
            "outcome_maturity_ratio": _number((outcome or {}).get("maturity_ratio")),
        },
        "outcome_link_status": str((outcome or {}).get("coverage_label") or "unlinked"),
        "outcome_maturity_label": str((outcome or {}).get("maturity_label") or "无数据"),
        "mature_horizon_count": mature_horizon_count,
        "model_version": INTELLIGENCE_MODEL_VERSION,
        "source_signature": _source_signature(lifecycle, ordered_events, replay, outcome),
        "calculated_at": now,
        "updated_at": now,
        "not_advice": NOT_ADVICE,
    }


def _decode_row(row: sqlite3.Row) -> dict[str, Any]:
    result = dict(row)
    for key in ("metrics_json", "reasons_json", "exchange_context_json"):
        if key in result:
            public_key = key.removesuffix("_json")
            raw = result.pop(key)
            try:
                result[public_key] = json.loads(raw) if raw else ([] if key == "reasons_json" else {})
            except (TypeError, ValueError, json.JSONDecodeError):
                result[public_key] = [] if key == "reasons_json" else {}
    return result


def _read_sources(
    lifecycle_store: LifecycleStore,
    *,
    symbol: str,
    active_only: bool,
    limit: int,
    dry_run: bool,
    lifecycle_ids: Iterable[int] | None = None,
) -> tuple[list[dict[str, Any]], dict[int, list[dict[str, Any]]]]:
    db_path = Path(lifecycle_store.db_path)
    if dry_run and not db_path.exists():
        return [], {}
    if dry_run:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            clauses: list[str] = []
            params: list[Any] = []
            if symbol:
                clauses.append("l.symbol = ?")
                params.append(str(symbol).upper())
            if active_only:
                clauses.append("l.is_active = 1")
            selected_ids = sorted({int(value) for value in (lifecycle_ids or []) if int(value) > 0})
            if selected_ids:
                clauses.append(f"l.id IN ({','.join('?' for _ in selected_ids)})")
                params.extend(selected_ids)
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            has_intelligence = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='lifecycle_intelligence'"
            ).fetchone() is not None
            intelligence_join = " LEFT JOIN lifecycle_intelligence i ON i.lifecycle_id = l.id" if has_intelligence else ""
            priority = (
                "l.is_active DESC, CASE WHEN i.lifecycle_id IS NULL OR i.updated_at < l.updated_at THEN 0 ELSE 1 END, "
                if has_intelligence and not active_only else "l.is_active DESC, "
            )
            params.append(max(1, min(int(limit), 500)))
            rows = conn.execute(
                f"SELECT {LIFECYCLE_SOURCE_PROJECTION} FROM signal_lifecycles l{intelligence_join} {where} "
                f"ORDER BY {priority}l.updated_at DESC, l.id DESC LIMIT ?",
                params,
            ).fetchall()
            lifecycles = [_decode_row(row) for row in rows]
            ids = [int(item["id"]) for item in lifecycles]
            events_by_id: dict[int, list[dict[str, Any]]] = defaultdict(list)
            if ids:
                placeholders = ",".join("?" for _ in ids)
                event_rows = conn.execute(
                    f"SELECT {EVENT_SOURCE_PROJECTION} FROM lifecycle_events WHERE lifecycle_id IN ({placeholders}) "
                    "ORDER BY lifecycle_id, event_time, id",
                    ids,
                ).fetchall()
                for row in event_rows:
                    item = _decode_row(row)
                    events_by_id[int(item["lifecycle_id"])].append(item)
            return lifecycles, dict(events_by_id)
        finally:
            conn.close()

    with lifecycle_store.connect() as conn:
        clauses = []
        params: list[Any] = []
        if symbol:
            clauses.append("l.symbol = ?")
            params.append(str(symbol).upper())
        if active_only:
            clauses.append("l.is_active = 1")
        selected_ids = sorted({int(value) for value in (lifecycle_ids or []) if int(value) > 0})
        if selected_ids:
            clauses.append(f"l.id IN ({','.join('?' for _ in selected_ids)})")
            params.extend(selected_ids)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        has_intelligence = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='lifecycle_intelligence'"
        ).fetchone() is not None
        intelligence_join = " LEFT JOIN lifecycle_intelligence i ON i.lifecycle_id = l.id" if has_intelligence else ""
        priority = (
            "l.is_active DESC, CASE WHEN i.lifecycle_id IS NULL OR i.updated_at < l.updated_at THEN 0 ELSE 1 END, "
            if has_intelligence and not active_only else "l.is_active DESC, "
        )
        params.append(max(1, min(int(limit), 500)))
        rows = conn.execute(
            f"SELECT {LIFECYCLE_SOURCE_PROJECTION} FROM signal_lifecycles l{intelligence_join} {where} "
            f"ORDER BY {priority}l.updated_at DESC, l.id DESC LIMIT ?",
            params,
        ).fetchall()
        lifecycles = [_decode_row(row) for row in rows]
        ids = [int(item["id"]) for item in lifecycles]
        events_by_id = defaultdict(list)
        if ids:
            placeholders = ",".join("?" for _ in ids)
            event_rows = conn.execute(
                f"SELECT {EVENT_SOURCE_PROJECTION} FROM lifecycle_events WHERE lifecycle_id IN ({placeholders}) "
                "ORDER BY lifecycle_id, event_time, id",
                ids,
            ).fetchall()
            for row in event_rows:
                item = _decode_row(row)
                events_by_id[int(item["lifecycle_id"])].append(item)
        return lifecycles, dict(events_by_id)


def _items_from_result(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        items = value.get("items")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return []


def generate_intelligence(
    *,
    settings: Settings | None = None,
    store: Any | None = None,
    lifecycle_store: LifecycleStore | None = None,
    symbol: str = "",
    all_active: bool = False,
    dry_run: bool = False,
    force: bool = False,
    limit: int = 500,
    lifecycles: list[dict[str, Any]] | None = None,
    events_by_lifecycle: dict[int, list[dict[str, Any]]] | None = None,
    lifecycle_ids: Iterable[int] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    loaded = settings or Settings.load()
    lifecycle_store = lifecycle_store or LifecycleStore(loaded.lifecycle_db_path)
    if symbol:
        requested_symbol = str(symbol)
        symbol = normalize_lifecycle_symbol(requested_symbol)
        if not symbol:
            return {
                "ok": False,
                "model_version": INTELLIGENCE_MODEL_VERSION,
                "dry_run": bool(dry_run),
                "processed": 0,
                "skipped": 0,
                "failed": 1,
                "duration_sec": round(time.perf_counter() - started, 4),
                "items": [],
                "errors": [{"symbol": requested_symbol, "error": "invalid lifecycle symbol"}],
                "not_advice": NOT_ADVICE,
            }
    if lifecycles is None:
        lifecycles, loaded_events = _read_sources(
            lifecycle_store,
            symbol=symbol,
            active_only=bool(all_active),
            limit=limit,
            dry_run=dry_run,
            lifecycle_ids=lifecycle_ids,
        )
        events_by_lifecycle = events_by_lifecycle or loaded_events
    events_by_lifecycle = events_by_lifecycle or {}

    if store is None and not dry_run:
        from .lifecycle_intelligence_store import IntelligenceStore

        store = IntelligenceStore(loaded)
        store.ensure_schema()

    existing_by_id: dict[int, dict[str, Any]] = {}
    replay_by_id: dict[int, dict[str, Any]] = {}
    outcome_coverage_by_id: dict[int, dict[str, Any]] = {}
    if store is not None:
        try:
            source_ids = sorted({int(item.get("id") or 0) for item in lifecycles if int(item.get("id") or 0) > 0})
            with store.connect() as conn:
                for offset in range(0, len(source_ids), 800):
                    chunk = source_ids[offset : offset + 800]
                    if not chunk:
                        continue
                    placeholders = ",".join("?" for _ in chunk)
                    for row in conn.execute(
                        f"SELECT lifecycle_id, source_signature FROM lifecycle_intelligence "
                        f"WHERE lifecycle_id IN ({placeholders})",
                        chunk,
                    ).fetchall():
                        item = dict(row)
                        existing_by_id[int(item["lifecycle_id"])] = item
                    for row in conn.execute(
                        f"SELECT lifecycle_id, upgrade_path, max_drawdown_pct, outcome_status, "
                        f"outcome_count, calculated_at, updated_at FROM lifecycle_replays "
                        f"WHERE lifecycle_id IN ({placeholders})",
                        chunk,
                    ).fetchall():
                        item = dict(row)
                        replay_by_id[int(item["lifecycle_id"])] = item
                    has_coverage = conn.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='lifecycle_outcome_coverage'"
                    ).fetchone()
                    if has_coverage:
                        for row in conn.execute(
                            f"SELECT lifecycle_id, linked_outcome_count, mature_horizon_count, "
                            f"link_coverage_ratio, maturity_ratio, coverage_label, maturity_label, "
                            f"horizon_1h_status, horizon_4h_status, horizon_24h_status, horizon_72h_status, "
                            f"calculated_at, updated_at FROM lifecycle_outcome_coverage "
                            f"WHERE lifecycle_id IN ({placeholders})",
                            chunk,
                        ).fetchall():
                            item = dict(row)
                            outcome_coverage_by_id[int(item["lifecycle_id"])] = item
        except (AttributeError, TypeError, sqlite3.Error):
            # Small compatibility fallback for injected test stores.
            try:
                existing_by_id = {
                    int(item.get("lifecycle_id") or 0): item
                    for item in _items_from_result(store.list_intelligence(limit=max(1, min(limit, 500)), compact=False))
                }
                replay_by_id = {
                    int(item.get("lifecycle_id") or 0): item
                    for item in _items_from_result(store.list_replays(limit=max(1, min(limit, 500)), compact=False))
                }
                outcome_coverage_by_id = {}
            except (AttributeError, TypeError, sqlite3.Error):
                existing_by_id = {}
                replay_by_id = {}

    prepared: list[dict[str, Any]] = []
    skipped = 0
    failed = 0
    errors: list[dict[str, str]] = []
    for lifecycle in lifecycles:
        lifecycle_id = int(lifecycle.get("id") or 0)
        try:
            record = evaluate_lifecycle(
                lifecycle,
                events_by_lifecycle.get(lifecycle_id, []),
                replay=replay_by_id.get(lifecycle_id),
                outcome=outcome_coverage_by_id.get(lifecycle_id),
            )
            existing = existing_by_id.get(lifecycle_id) or {}
            if not force and existing.get("source_signature") == record.get("source_signature"):
                skipped += 1
                continue
            prepared.append(record)
        except Exception as exc:  # individual lifecycle failures must not stop the batch
            failed += 1
            errors.append({"symbol": str(lifecycle.get("symbol") or ""), "error": str(exc)[:240]})

    processed = 0
    if not dry_run and prepared and store is not None:
        with store.transaction() as conn:
            for record in prepared:
                try:
                    store.upsert_intelligence(record, conn=conn, fetch=False)
                    processed += 1
                except Exception as exc:
                    failed += 1
                    errors.append({"symbol": record["symbol"], "error": str(exc)[:240]})
            if processed:
                store.invalidate_analytics_cache("lifecycle:", conn=conn)
    elif dry_run:
        processed = len(prepared)

    return {
        "ok": failed == 0,
        "model_version": INTELLIGENCE_MODEL_VERSION,
        "dry_run": bool(dry_run),
        "processed": processed,
        "skipped": skipped,
        "failed": failed,
        "duration_sec": round(time.perf_counter() - started, 4),
        "items": prepared,
        "errors": errors,
        "not_advice": NOT_ADVICE,
    }


def intelligence_payload(result: dict[str, Any]) -> dict[str, Any]:
    """Small compatibility wrapper used by CLI/API adapters."""
    return {
        "ok": bool(result.get("ok", True)),
        "data": result,
        "model_version": INTELLIGENCE_MODEL_VERSION,
        "not_advice": NOT_ADVICE,
    }


__all__ = [
    "INTELLIGENCE_MODEL_VERSION",
    "NOT_ADVICE",
    "build_upgrade_path",
    "evaluate_lifecycle",
    "generate_intelligence",
    "identify_lifecycle_stage",
    "intelligence_payload",
    "quality_label",
]
