from __future__ import annotations

import sqlite3
import time
from typing import Any

from ..config import Settings
from ..decision_model import (
    DECISION_DISPLAY,
    DEFAULT_DECISION_THRESHOLDS,
    DEFAULT_DECISION_WEIGHTS,
    MODEL_FAMILY,
    MODEL_VERSION,
    enhance_signal_with_decision,
    evaluate_decision,
)
from ..signal_store import SignalEventStore
from .api_core import api_error, api_list_payload, api_ok, normalize_symbol_filter, redact_api_payload
from .signals import enhance_signal_items


def _store(settings: Settings | None = None) -> SignalEventStore:
    loaded = settings or Settings.load()
    return SignalEventStore(loaded.signal_events_db_path)


def _window_bounds(window_sec: int) -> tuple[int, int]:
    end_ts = int(time.time())
    safe_window = max(1, min(int(window_sec or 86400), 2592000))
    return max(0, end_ts - safe_window), end_ts


def _safe_limit(limit: int, default: int = 50, maximum: int = 200) -> int:
    try:
        value = int(limit or default)
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, maximum))


def _normalize_symbol(symbol: Any) -> dict[str, str]:
    return normalize_symbol_filter(str(symbol or "").strip())


def _decision_payload_from_items(
    *,
    symbol: str,
    items: list[dict[str, Any]],
    window_sec: int,
    limit: int,
) -> dict[str, Any]:
    result = evaluate_decision(items, symbol=symbol)
    related = enhance_signal_items(items[: min(len(items), 12)])
    data = {
        **result,
        "symbol": symbol,
        "coin": _normalize_symbol(symbol).get("coin", ""),
        "window_sec": int(window_sec or 86400),
        "limit": int(limit or 50),
        "related_signals": result.get("related_signals", []),
        "related_signal_items": related,
    }
    payload = api_ok(data, message="已读取信号决策模型")
    payload.update({
        **data,
        "message": "已读取信号决策模型",
    })
    return redact_api_payload(payload)


def decision_for_symbol_payload(
    symbol: str,
    *,
    window_sec: int = 86400,
    limit: int = 50,
    settings: Settings | None = None,
) -> dict[str, Any]:
    info = _normalize_symbol(symbol)
    normalized = info.get("symbol", "")
    if not normalized:
        return {
            "ok": False,
            "error": {"code": "missing_symbol", "message": "请提供币种，例如 BTCUSDT。"},
            "message": "请提供币种，例如 BTCUSDT。",
            "code": "missing_symbol",
        }
    start_ts, end_ts = _window_bounds(window_sec)
    safe_limit = _safe_limit(limit, 50, 200)
    store = _store(settings)
    listed = store.list_by_symbol(normalized, limit=safe_limit, start_ts=start_ts, end_ts=end_ts)
    items = listed.get("items", [])
    return _decision_payload_from_items(symbol=normalized, items=items, window_sec=window_sec, limit=safe_limit)


def decision_payload(
    *,
    symbol: str,
    window_sec: int = 86400,
    limit: int = 50,
    settings: Settings | None = None,
) -> dict[str, Any]:
    return decision_for_symbol_payload(symbol, window_sec=window_sec, limit=limit, settings=settings)


def _candidate_symbols(
    *,
    symbol: str = "",
    q: str = "",
    limit: int = 50,
    window_sec: int = 86400,
    settings: Settings | None = None,
    store: SignalEventStore | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    start_ts, end_ts = _window_bounds(window_sec)
    loaded_store = store or _store(settings)
    if str(symbol or "").strip():
        info = _normalize_symbol(symbol)
        normalized = info.get("symbol", "")
        return [{"symbol": normalized, "coin": info.get("coin", ""), "count": 0}] if normalized else []
    return loaded_store.search_symbols(
        q=str(q or "").strip()[:40],
        limit=_safe_limit(limit, 50, 100),
        start_ts=start_ts,
        end_ts=end_ts,
        conn=conn,
    )


def _decision_results_for_symbols(
    symbols: list[str],
    *,
    window_sec: int,
    limit_per_symbol: int = 50,
    settings: Settings | None = None,
    store: SignalEventStore | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict[str, dict[str, Any]]:
    normalized_symbols: list[str] = []
    seen: set[str] = set()
    for value in symbols:
        normalized = _normalize_symbol(value).get("symbol", "")
        if normalized and normalized not in seen:
            seen.add(normalized)
            normalized_symbols.append(normalized)
    if not normalized_symbols:
        return {}
    start_ts, end_ts = _window_bounds(window_sec)
    grouped = (store or _store(settings)).iter_by_symbols(
        normalized_symbols,
        limit_per_symbol=_safe_limit(limit_per_symbol, 50, 200),
        start_ts=start_ts,
        end_ts=end_ts,
        conn=conn,
    )
    results = {
        symbol: {
            **evaluate_decision(items, symbol=symbol),
            "coin": _normalize_symbol(symbol).get("coin", ""),
        }
        for symbol, items in grouped
    }
    for symbol in normalized_symbols:
        if symbol not in results:
            results[symbol] = {
                **evaluate_decision([], symbol=symbol),
                "coin": _normalize_symbol(symbol).get("coin", ""),
            }
    return results


def decisions_payload(
    *,
    limit: int = 50,
    cursor: int | None = None,
    q: str = "",
    symbol: str = "",
    decision: str = "",
    risk: str = "",
    window_sec: int = 86400,
    settings: Settings | None = None,
) -> dict[str, Any]:
    del cursor  # reserved for future pagination; list is currently ranked by active symbols.
    safe_limit = _safe_limit(limit, 50, 100)
    store = _store(settings)
    with store.connect() as conn:
        candidates = _candidate_symbols(
            symbol=symbol,
            q=q,
            limit=safe_limit,
            window_sec=window_sec,
            settings=settings,
            store=store,
            conn=conn,
        )
        results = _decision_results_for_symbols(
            [str(candidate.get("symbol") or "") for candidate in candidates[:safe_limit]],
            window_sec=window_sec,
            settings=settings,
            store=store,
            conn=conn,
        )
    items: list[dict[str, Any]] = []
    for candidate in candidates[:safe_limit]:
        normalized = str(candidate.get("symbol") or "")
        if not normalized:
            continue
        payload = results.get(normalized)
        if not payload:
            continue
        if decision and str(payload.get("decision", {}).get("code") or "") != str(decision):
            continue
        risk_level = str(payload.get("decision", {}).get("risk_level") or "")
        if risk and risk_level != str(risk):
            continue
        item = {
            "symbol": payload.get("symbol", normalized),
            "coin": payload.get("coin", ""),
            "decision": payload.get("decision", {}),
            "scores": payload.get("scores", {}),
            "reasons": (payload.get("reasons") or [])[:3],
            "risks": (payload.get("risks") or [])[:3],
            "watch_points": (payload.get("watch_points") or [])[:3],
            "factor_explanations": (payload.get("factor_explanations") or [])[:6],
            "calibration": payload.get("calibration", {}),
            "latest_signal": (payload.get("related_signals") or [None])[0],
            "model_version": MODEL_FAMILY,
            "model_runtime_version": MODEL_VERSION,
        }
        items.append(item)
    filters = {
        "q": str(q or "").strip()[:40],
        "symbol": _normalize_symbol(symbol).get("symbol", "") if str(symbol or "").strip() else "",
        "decision": str(decision or ""),
        "risk": str(risk or ""),
        "window_sec": int(window_sec or 86400),
    }
    distribution = decision_distribution(items)
    summary = decisions_summary(items, int(window_sec or 86400), distribution)
    pagination = {"limit": safe_limit, "next_cursor": None}
    data = {
        "items": items,
        "summary": summary,
        "distribution": distribution,
        "filters": filters,
        "pagination": pagination,
        "model_version": MODEL_FAMILY,
        "model_runtime_version": MODEL_VERSION,
    }
    payload = api_ok(data, message="已读取全市场信号决策列表")
    payload.update({
        "items": redact_api_payload(items),
        "decisions": redact_api_payload(items),
        "count": len(items),
        "summary": redact_api_payload(summary),
        "distribution": redact_api_payload(distribution),
        "filters": filters,
        "pagination": pagination,
        "model_version": MODEL_FAMILY,
        "model_runtime_version": MODEL_VERSION,
    })
    return redact_api_payload(payload)


def decision_distribution(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    total = max(0, len(items))
    counts: dict[str, int] = {code: 0 for code in DECISION_DISPLAY}
    for item in items:
        code = str((item.get("decision") or {}).get("code") or "observe")
        if code not in counts:
            code = "observe"
        counts[code] += 1
    return {
        code: {
            "label": DECISION_DISPLAY[code]["label"],
            "count": count,
            "ratio": round(count / total, 4) if total else 0.0,
        }
        for code, count in counts.items()
    }


def decisions_summary(items: list[dict[str, Any]], window_sec: int, distribution: dict[str, Any]) -> dict[str, Any]:
    dominant_code = "observe"
    if distribution:
        dominant_code = max(distribution.items(), key=lambda kv: int(kv[1].get("count", 0) or 0))[0]
    total = len(items)
    risk_alert_count = int((distribution.get("risk_alert") or {}).get("count", 0) or 0)
    probe_count = int((distribution.get("probe") or {}).get("count", 0) or 0)
    hours = max(1, int(window_sec or 86400) // 3600)
    label = DECISION_DISPLAY.get(dominant_code, DECISION_DISPLAY["observe"])["label"]
    return {
        "window_sec": int(window_sec or 86400),
        "total": total,
        "dominant_decision": label,
        "dominant_decision_code": dominant_code,
        "risk_alert_count": risk_alert_count,
        "probe_count": probe_count,
        "headline": f"最近 {hours} 小时共有 {total} 个币种进入决策模型，其中 {probe_count} 个为可试仓，{risk_alert_count} 个为风险警报。",
    }


def decisions_stats_payload(
    *,
    window_sec: int = 86400,
    limit: int = 100,
    settings: Settings | None = None,
    include_model_config: bool = False,
) -> dict[str, Any]:
    safe_limit = _safe_limit(limit, 100, 200)
    store = _store(settings)
    with store.connect() as conn:
        candidates = _candidate_symbols(
            limit=safe_limit,
            window_sec=window_sec,
            settings=settings,
            store=store,
            conn=conn,
        )
        results = _decision_results_for_symbols(
            [str(candidate.get("symbol") or "") for candidate in candidates[:safe_limit]],
            window_sec=window_sec,
            settings=settings,
            store=store,
            conn=conn,
        )
    items: list[dict[str, Any]] = []
    for candidate in candidates[:safe_limit]:
        normalized = str(candidate.get("symbol") or "")
        if not normalized:
            continue
        payload = results.get(normalized)
        if not payload:
            continue
        items.append({
            "symbol": payload.get("symbol", normalized),
            "coin": payload.get("coin", ""),
            "decision": payload.get("decision", {}),
            "scores": payload.get("scores", {}),
            "reasons": (payload.get("reasons") or [])[:3],
            "risks": (payload.get("risks") or [])[:3],
            "watch_points": (payload.get("watch_points") or [])[:3],
            "calibration": payload.get("calibration", {}),
        })
    distribution = decision_distribution(items)
    risk_distribution: dict[str, int] = {"低": 0, "中": 0, "高": 0}
    for item in items:
        risk_level = str((item.get("decision") or {}).get("risk_level") or "低")
        if risk_level not in risk_distribution:
            risk_distribution[risk_level] = 0
        risk_distribution[risk_level] += 1
    top_risk_symbols = [
        item for item in sorted(
            items,
            key=lambda x: int((x.get("scores") or {}).get("crowding_risk", 0) or 0),
            reverse=True,
        )
        if (item.get("decision") or {}).get("code") in {"risk_alert", "avoid_chase"}
    ][:10]
    top_probe_symbols = [
        item for item in sorted(
            items,
            key=lambda x: int((x.get("decision") or {}).get("confidence", 0) or 0),
            reverse=True,
        )
        if (item.get("decision") or {}).get("code") == "probe"
    ][:10]
    data: dict[str, Any] = {
        "window_sec": int(window_sec or 86400),
        "total_symbols": len(items),
        "distribution": distribution,
        "risk_distribution": risk_distribution,
        "top_risk_symbols": top_risk_symbols,
        "top_probe_symbols": top_probe_symbols,
        "generated_at": int(time.time()),
        "summary": decisions_summary(items, int(window_sec or 86400), distribution),
        "model_version": MODEL_FAMILY,
        "model_runtime_version": MODEL_VERSION,
    }
    if include_model_config:
        data.update({
            "weights": dict(DEFAULT_DECISION_WEIGHTS),
            "thresholds": dict(DEFAULT_DECISION_THRESHOLDS),
            "calibration_notes": [
                "高频币种不会仅因信号数量多就直接判为风险警报。",
                "风险警报需要明确风险因子，例如资金费率拥挤、假突破、破位或失败/阻止信号增加。",
                "可试仓要求多模块共振、结构确认、风险可控且不过热。",
            ],
        })
    payload = api_ok(data, message="已读取决策分布统计")
    payload.update(redact_api_payload(data))
    return redact_api_payload(payload)


def enhance_signals_with_decisions(
    items: list[dict[str, Any]],
    *,
    window_sec: int = 86400,
    settings: Settings | None = None,
    store: SignalEventStore | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict[str, Any]]:
    decisions_by_symbol = _decision_results_for_symbols(
        [str(item.get("symbol") or "") for item in items],
        window_sec=window_sec,
        settings=settings,
        store=store,
        conn=conn,
    )
    enhanced: list[dict[str, Any]] = []
    for item in items:
        symbol = str(item.get("symbol") or "").upper()
        decision_payload_for_symbol = decisions_by_symbol.get(symbol, {"decision": {"label": "观察", "code": "observe", "tone": "neutral", "confidence": 0, "risk_level": "低"}})
        enhanced.append(enhance_signal_with_decision(item, decision_payload_for_symbol))
    return enhanced
