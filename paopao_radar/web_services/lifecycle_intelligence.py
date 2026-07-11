from __future__ import annotations

import re
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..config import Settings
from ..lifecycle_intelligence import INTELLIGENCE_MODEL_VERSION, NOT_ADVICE
from ..lifecycle_intelligence_store import IntelligenceStore
from ..lifecycle_store import normalize_lifecycle_symbol, public_lifecycle_redact
from ..runtime_cache import get_or_set as runtime_cache_get_or_set
from .api_core import api_error, api_ok


REPLAY_MODEL_VERSION = "lifecycle-replay-v1"
SIMILARITY_MODEL_VERSION = "lifecycle-similarity-v1"
SIMILARITY_DISCLAIMER = "历史相似样本仅用于研究，不代表未来结果。"
INSUFFICIENT_SAMPLES = "当前相似样本不足，暂不生成统计结论。"
SENSITIVE_KEY_RE = re.compile(
    r"(?i)(token|secret|password|cookie|authorization|chat_?id|topic_?id|message_?id|"
    r"dedup_?key|payload_?json|text_?html|database|api_?key|server_?path|db_?path|"
    r"internal_?job|exception_?stack|raw_?telegram|source_?signature)"
)


def _settings(settings: Settings | None = None) -> Settings:
    return settings or Settings.load()


@lru_cache(maxsize=8)
def _store_for_path(path: str) -> IntelligenceStore:
    """Reuse schema readiness while retaining per-request SQLite connections."""
    return IntelligenceStore(Path(path))


def _store(settings: Settings | None = None) -> IntelligenceStore:
    loaded = _settings(settings)
    return _store_for_path(str(Path(loaded.lifecycle_db_path).resolve()))


def _limit(value: Any, default: int = 50, maximum: int = 500) -> int:
    try:
        number = int(value if value not in {None, ""} else default)
    except (TypeError, ValueError):
        number = default
    return max(1, min(number, maximum))


def _offset(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _trim_text(value: Any, limit: int = 600) -> str:
    return public_lifecycle_redact(str(value or ""))[:limit]


def _safe_public(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if SENSITIVE_KEY_RE.search(name):
                continue
            result[name] = _safe_public(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_safe_public(item) for item in list(value)[:200]]
    if isinstance(value, str):
        return _trim_text(value, 1200)
    return value


def _envelope(data: dict[str, Any], message: str, *, public: bool) -> dict[str, Any]:
    safe = _safe_public(data) if public else data
    # These are new v1.78 endpoints, so use the canonical envelope once rather
    # than duplicating every item at both ``data`` and the response root.
    return api_ok(safe, message=message)


def _intelligence_list_query(
    store: IntelligenceStore,
    *,
    symbol: str = "",
    quality: str = "",
    stage: str = "",
    state: str = "",
    level: str = "",
    risk: str = "",
    limit: int = 50,
    offset: int = 0,
    conn: Any | None = None,
) -> tuple[list[dict[str, Any]], int]:
    if conn is None:
        with store.connect() as owned:
            return _intelligence_list_query(
                store,
                symbol=symbol,
                quality=quality,
                stage=stage,
                state=state,
                level=level,
                risk=risk,
                limit=limit,
                offset=offset,
                conn=owned,
            )
    where: list[str] = []
    params: dict[str, Any] = {"limit": _limit(limit), "offset": _offset(offset)}
    normalized = normalize_lifecycle_symbol(symbol)
    if normalized:
        where.append("i.symbol = :symbol")
        params["symbol"] = normalized
    elif str(symbol or "").strip():
        where.append("i.symbol LIKE :symbol_like")
        params["symbol_like"] = f"%{str(symbol).strip().upper()}%"
    if str(quality or "").strip():
        where.append("i.quality_label = :quality")
        params["quality"] = str(quality).strip()
    if str(stage or "").strip():
        where.append("(i.stage = :stage OR i.stage_label = :stage)")
        params["stage"] = str(stage).strip()
    has_base = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='signal_lifecycles'"
    ).fetchone() is not None
    if (str(state or "").strip() or str(level or "").strip()) and not has_base:
        return [], 0
    if str(state or "").strip():
        where.append("l.current_state = :state")
        params["state"] = str(state).strip()
    if str(level or "").strip():
        where.append("(l.first_signal_level = :level OR l.highest_level = :level)")
        params["level"] = str(level).strip()
    if str(risk or "").strip():
        where.append("i.risk_label LIKE :risk")
        params["risk"] = f"%{str(risk).strip()}%"
    clause = " WHERE " + " AND ".join(where) if where else ""
    base_projection = (
        "l.current_state, l.first_signal_level, l.highest_level, l.lifecycle_score, "
        "l.risk_score, l.price_change_from_first_pct, l.oi_change_from_first_pct, l.is_active"
        if has_base else
        "NULL AS current_state, NULL AS first_signal_level, NULL AS highest_level, "
        "NULL AS lifecycle_score, NULL AS risk_score, NULL AS price_change_from_first_pct, "
        "NULL AS oi_change_from_first_pct, NULL AS is_active"
    )
    projection = (
        "i.lifecycle_id, i.symbol, i.intelligence_score, i.quality_label, i.stage, i.stage_label, "
        "i.momentum_label, i.capital_confirmation_label, i.risk_label, i.maturity_label, "
        "i.confidence_label, i.model_version, i.calculated_at, i.updated_at, "
        f"{base_projection}, r.upgrade_path, r.result_label, r.final_return_pct, "
        "r.outcome_status, r.outcome_count"
    )
    base_join = " LEFT JOIN signal_lifecycles l ON l.id = i.lifecycle_id" if has_base else ""
    rows = conn.execute(
        f"SELECT {projection} FROM lifecycle_intelligence i "
        f"{base_join} LEFT JOIN lifecycle_replays r ON r.lifecycle_id = i.lifecycle_id"
        f"{clause} ORDER BY i.intelligence_score DESC, i.updated_at DESC "
        "LIMIT :limit OFFSET :offset",
        params,
    ).fetchall()
    total = int(conn.execute(
        "SELECT COUNT(*) FROM lifecycle_intelligence i" + base_join + clause,
        {key: value for key, value in params.items() if key not in {"limit", "offset"}},
    ).fetchone()[0])
    historical_count = 0
    if has_base:
        historical_count = int(conn.execute(
            """
            SELECT COUNT(*)
              FROM lifecycle_replays hr
              JOIN signal_lifecycles hl ON hl.id = hr.lifecycle_id
             WHERE hl.is_active = 0
                OR COALESCE(hr.outcome_count, 0) > 0
                OR hr.outcome_status IN ('linked', 'success')
            """
        ).fetchone()[0])
    items = [dict(row) for row in rows]
    for item in items:
        own_eligible = (
            int(item.get("is_active", 1) or 0) == 0
            or int(item.get("outcome_count") or 0) > 0
            or str(item.get("outcome_status") or "") in {"linked", "success"}
        )
        item["similar_count"] = max(0, historical_count - (1 if own_eligible else 0))
    return items, total


def lifecycle_intelligence_summary_payload(
    *, settings: Settings | None = None, public: bool = False,
) -> dict[str, Any]:
    store = _store(settings)
    with store.connect() as conn:
        has_base = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='signal_lifecycles'"
        ).fetchone() is not None
        base_join = " LEFT JOIN signal_lifecycles l ON l.id = i.lifecycle_id" if has_base else ""
        base_risk = " OR l.current_state = 'risk_warning'" if has_base else ""
        base_failed = " OR l.current_state = 'failed'" if has_base else ""
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS total_count,
                   SUM(CASE WHEN intelligence_score >= 90 THEN 1 ELSE 0 END) AS strong_trend_count,
                   SUM(CASE WHEN intelligence_score >= 80 AND intelligence_score < 90 THEN 1 ELSE 0 END) AS high_quality_count,
                   SUM(CASE WHEN i.risk_label = '高风险' OR i.stage_label = '派发风险'{base_risk} THEN 1 ELSE 0 END) AS risk_count,
                   SUM(CASE WHEN i.stage_label = '启动失败' OR r.result_label = 'failed'{base_failed} THEN 1 ELSE 0 END) AS failed_count,
                   AVG(intelligence_score) AS avg_intelligence_score
              FROM lifecycle_intelligence i
              LEFT JOIN lifecycle_replays r ON r.lifecycle_id = i.lifecycle_id
              {base_join}
            """
        ).fetchone()
        quality = [dict(item) for item in conn.execute(
            "SELECT quality_label AS label, COUNT(*) AS count FROM lifecycle_intelligence "
            "GROUP BY quality_label ORDER BY count DESC"
        ).fetchall()]
        stages = [dict(item) for item in conn.execute(
            "SELECT COALESCE(stage, stage_label) AS key, stage_label AS label, COUNT(*) AS count "
            "FROM lifecycle_intelligence GROUP BY stage, stage_label ORDER BY count DESC"
        ).fetchall()]
        items, _ = _intelligence_list_query(store, limit=5, conn=conn)
    summary = dict(row) if row else {"total_count": 0}
    data = {
        "summary": summary,
        "quality_distribution": quality,
        "stage_distribution": stages,
        "items": items,
        "model_version": INTELLIGENCE_MODEL_VERSION,
        "not_advice": NOT_ADVICE,
    }
    return _envelope(data, "已读取生命周期智能概览", public=public)


def lifecycle_intelligence_list_payload(
    *,
    symbol: str = "",
    quality: str = "",
    stage: str = "",
    state: str = "",
    level: str = "",
    risk: str = "",
    limit: int = 50,
    offset: int = 0,
    settings: Settings | None = None,
    public: bool = False,
) -> dict[str, Any]:
    items, total = _intelligence_list_query(
        _store(settings), symbol=symbol, quality=quality, stage=stage,
        state=state, level=level, risk=risk, limit=limit, offset=offset
    )
    data = {
        "items": items,
        "count": len(items),
        "total": total,
        "pagination": {"limit": _limit(limit), "offset": _offset(offset), "has_more": _offset(offset) + len(items) < total},
        "filters": {
            "symbol": str(symbol or ""), "quality": str(quality or ""),
            "stage": str(stage or ""), "state": str(state or ""),
            "level": str(level or ""), "risk": str(risk or ""),
        },
        "model_version": INTELLIGENCE_MODEL_VERSION,
        "not_advice": NOT_ADVICE,
    }
    return _envelope(data, "已读取生命周期智能排行", public=public)


def lifecycle_intelligence_detail_payload(
    symbol: str,
    *, settings: Settings | None = None, public: bool = False,
) -> dict[str, Any]:
    normalized = normalize_lifecycle_symbol(symbol)
    if not normalized:
        return api_error("请提供币种，例如 BTCUSDT。", code="bad_request")
    store = _store(settings)
    with store.connect() as conn:
        intelligence = store.get_intelligence(symbol=normalized, conn=conn)
        replay = store.get_replay(symbol=normalized, conn=conn)
    if not intelligence:
        return _envelope({
            "symbol": normalized,
            "intelligence": None,
            "model_version": INTELLIGENCE_MODEL_VERSION,
            "status": "insufficient_data",
            "message": "生命周期智能评价仍在生成",
            "not_advice": NOT_ADVICE,
        }, "生命周期智能评价仍在生成", public=public)
    intelligence["summary"] = _trim_text(intelligence.get("summary"), 800)
    for key in ("strengths", "risks", "watch_points"):
        intelligence[key] = [_trim_text(item, 300) for item in list(intelligence.get(key) or [])[:30]]
    data = {
        "symbol": normalized,
        "intelligence": intelligence,
        "replay": {key: value for key, value in (replay or {}).items() if key not in {"summary", "source_signature"}},
        "model_version": INTELLIGENCE_MODEL_VERSION,
        "not_advice": NOT_ADVICE,
    }
    return _envelope(data, "已读取生命周期智能评价", public=public)


def lifecycle_replay_payload(
    symbol: str = "",
    *, lifecycle_id: int | None = None, settings: Settings | None = None, public: bool = False,
) -> dict[str, Any]:
    normalized = normalize_lifecycle_symbol(symbol)
    if not normalized and not lifecycle_id:
        return api_error("请提供币种或 lifecycle_id。", code="bad_request")
    store = _store(settings)
    with store.connect() as conn:
        replay = store.get_replay(lifecycle_id=lifecycle_id, symbol=normalized, conn=conn)
    data = {
        "symbol": normalized or str((replay or {}).get("symbol") or ""),
        "replay": replay,
        "status": "ready" if replay else "insufficient_data",
        "message": "" if replay else "生命周期回放仍在生成",
        "model_version": REPLAY_MODEL_VERSION,
        "not_advice": NOT_ADVICE,
    }
    return _envelope(data, "已读取生命周期回放摘要", public=public)


def lifecycle_replay_frames_payload(
    symbol: str = "",
    *,
    lifecycle_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
    settings: Settings | None = None,
    public: bool = False,
) -> dict[str, Any]:
    normalized = normalize_lifecycle_symbol(symbol)
    if not normalized and not lifecycle_id:
        return api_error("请提供币种或 lifecycle_id。", code="bad_request")
    store = _store(settings)
    page_limit = _limit(limit, 100, 200)
    page_offset = _offset(offset)
    with store.connect() as conn:
        replay = store.get_replay(lifecycle_id=lifecycle_id, symbol=normalized, conn=conn)
        resolved_id = int((replay or {}).get("lifecycle_id") or lifecycle_id or 0)
        items = store.list_replay_frames(
            lifecycle_id=resolved_id or None,
            symbol=normalized,
            limit=page_limit,
            offset=page_offset,
            include_metrics=False,
            conn=conn,
        ) if replay else []
        total = store.count_replay_frames(resolved_id, conn=conn) if resolved_id else 0
    data = {
        "items": items,
        "count": len(items),
        "pagination": {"limit": page_limit, "offset": page_offset, "total": total, "has_more": page_offset + len(items) < total},
        "symbol": normalized or str((replay or {}).get("symbol") or ""),
        "model_version": REPLAY_MODEL_VERSION,
        "not_advice": NOT_ADVICE,
    }
    return _envelope(data, "已读取生命周期回放帧", public=public)


def _latest_analytics(store: IntelligenceStore) -> dict[str, Any]:
    from ..lifecycle_analytics import DEFAULT_ANALYTICS_CACHE_KEY

    data = store.get_analytics_cache(DEFAULT_ANALYTICS_CACHE_KEY) or {}
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        return dict(data["data"])
    return data if isinstance(data, dict) else {}


def lifecycle_analytics_payload(
    dimension: str,
    *, settings: Settings | None = None, public: bool = False,
) -> dict[str, Any]:
    key = str(dimension or "").strip().replace("-", "_")
    allowed = {"first_level", "upgrade_path", "module", "capital_confirmation"}
    if key not in allowed:
        return api_error("不支持的生命周期统计维度。", code="bad_request")
    analytics = _latest_analytics(_store(settings))
    items = analytics.get(key, [])
    if isinstance(items, dict):
        items = items.get("items", items)
    data = {
        "dimension": key,
        "items": items if isinstance(items, list) else [],
        "summary": analytics.get("summary", {}),
        "model_data_warnings": analytics.get("model_data_warnings", []),
        "model_version": str(analytics.get("model_version") or INTELLIGENCE_MODEL_VERSION),
        "status": "ready" if analytics else "insufficient_data",
        "message": "" if analytics else "历史样本仍在积累",
        "not_advice": NOT_ADVICE,
    }
    return _envelope(data, "已读取生命周期历史统计", public=public)


def lifecycle_similar_payload(
    symbol: str,
    *, limit: int = 10, settings: Settings | None = None, public: bool = False,
) -> dict[str, Any]:
    normalized = normalize_lifecycle_symbol(symbol)
    if not normalized:
        return api_error("请提供币种，例如 BTCUSDT。", code="bad_request")
    loaded = _settings(settings)

    def load() -> dict[str, Any]:
        from ..lifecycle_similarity import find_similar_for_symbol

        result = find_similar_for_symbol(
            settings=loaded,
            symbol=normalized,
            limit=_limit(limit, 10, 50),
            min_samples=max(1, int(loaded.lifecycle_similarity_min_samples or 5)),
        )
        return result if isinstance(result, dict) else {}

    try:
        result = runtime_cache_get_or_set(
            f"lifecycle:similar:{normalized}:{_limit(limit, 10, 50)}",
            30,
            load,
        )
    except Exception:
        # An empty pre-migration database is a normal rollout state. Public
        # callers receive an insufficient-sample result, never an exception.
        result = {"ok": True, "status": "insufficient_samples", "similar_count": 0}
    if not isinstance(result, dict):
        result = {}
    result.setdefault("symbol", normalized)
    result.setdefault("model_version", SIMILARITY_MODEL_VERSION)
    result.setdefault("disclaimer", SIMILARITY_DISCLAIMER)
    if int(result.get("similar_count") or 0) < int(loaded.lifecycle_similarity_min_samples or 5):
        result["status"] = "insufficient_samples"
        result.setdefault("message", INSUFFICIENT_SAMPLES)
    result.pop("ok", None)
    result.setdefault("not_advice", NOT_ADVICE)
    return _envelope(result, "已读取历史相似生命周期", public=public)


def public_lifecycle_intelligence_summary_payload(**kwargs: Any) -> dict[str, Any]:
    return lifecycle_intelligence_summary_payload(public=True, **kwargs)


def public_lifecycle_intelligence_list_payload(**kwargs: Any) -> dict[str, Any]:
    return lifecycle_intelligence_list_payload(public=True, **kwargs)


def public_lifecycle_intelligence_detail_payload(symbol: str, **kwargs: Any) -> dict[str, Any]:
    return lifecycle_intelligence_detail_payload(symbol, public=True, **kwargs)


def public_lifecycle_replay_payload(symbol: str = "", **kwargs: Any) -> dict[str, Any]:
    return lifecycle_replay_payload(symbol, public=True, **kwargs)


def public_lifecycle_replay_frames_payload(symbol: str = "", **kwargs: Any) -> dict[str, Any]:
    return lifecycle_replay_frames_payload(symbol, public=True, **kwargs)


def public_lifecycle_analytics_payload(dimension: str, **kwargs: Any) -> dict[str, Any]:
    return lifecycle_analytics_payload(dimension, public=True, **kwargs)


def public_lifecycle_similar_payload(symbol: str, **kwargs: Any) -> dict[str, Any]:
    return lifecycle_similar_payload(symbol, public=True, **kwargs)


__all__ = [name for name in globals() if name.endswith("_payload")]
