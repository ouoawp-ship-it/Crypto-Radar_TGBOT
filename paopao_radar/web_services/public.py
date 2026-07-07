from __future__ import annotations

from typing import Any

from ..config import Settings
from ..signal_store import SignalEventStore
from .api_core import api_error, api_ok, normalize_symbol_filter, redact_api_payload
from .coins import coin_detail_payload, coin_search_payload
from .decision import decision_for_symbol_payload, decisions_payload, decisions_stats_payload, enhance_signals_with_decisions
from .signals import enhance_signal_item, signal_display, signal_detail_view, signal_stats_display
from .timeline import timeline_payload


FORBIDDEN_PUBLIC_KEYS = {
    "dedup_key",
    "topic_id",
    "message_ids",
    "message_ids_json",
    "reply_to_message_id",
    "payload",
    "payload_json",
    "raw",
    "text_html",
    "config",
    "jobs",
    "logs",
    "audit",
    "service",
    "services",
    "token",
    "chat_id",
    "api_key",
    "secret",
    "authorization",
    "cookie",
    "telegram",
    "bot_token",
}


def _store(settings: Settings | None = None) -> SignalEventStore:
    loaded = settings or Settings.load()
    return SignalEventStore(loaded.signal_events_db_path)


def _short(value: Any, limit: int = 260) -> str:
    text = str(redact_api_payload(value if value is not None else ""))
    return text[: max(0, int(limit))]


def _strip_forbidden(value: Any) -> Any:
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text in FORBIDDEN_PUBLIC_KEYS:
                continue
            if any(secret in key_text.lower() for secret in ("token", "api_key", "apikey", "secret", "password")):
                continue
            clean[key_text] = _strip_forbidden(item)
        return clean
    if isinstance(value, list):
        return [_strip_forbidden(item) for item in value]
    redacted = redact_api_payload(value)
    if isinstance(redacted, str):
        for marker in ("WEB_ADMIN_TOKEN", "BOT_TOKEN", "TELEGRAM", "Telegram", "Authorization", "Cookie"):
            redacted = redacted.replace(marker, "<redacted>")
    return redacted


def public_signal_item(item: dict[str, Any]) -> dict[str, Any]:
    enhanced = enhance_signal_item(item)
    display = dict(enhanced.get("display") or signal_display(enhanced))
    display["summary"] = _short(display.get("summary") or enhanced.get("excerpt") or "", 220)
    display["badges"] = _strip_forbidden(display.get("badges") or [])
    public = {
        "id": enhanced.get("id"),
        "time": enhanced.get("time") or "",
        "module": enhanced.get("module") or "",
        "symbol": enhanced.get("symbol") or "",
        "status": enhanced.get("status") or "",
        "signal_type": enhanced.get("signal_type") or "",
        "score": enhanced.get("score"),
        "stage": enhanced.get("stage") or "",
        "excerpt": _short(enhanced.get("excerpt") or enhanced.get("title") or "", 260),
        "display": display,
    }
    if enhanced.get("decision"):
        public["decision"] = _strip_forbidden(enhanced.get("decision") or {})
    return _strip_forbidden(public)


def _public_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [public_signal_item(item) for item in items]


def public_signals_payload(
    *,
    limit: int = 50,
    cursor: int | None = None,
    module: str = "",
    symbol: str = "",
    status: str = "",
    q: str = "",
    window_sec: int = 86400,
    settings: Settings | None = None,
) -> dict[str, Any]:
    normalized = normalize_symbol_filter(symbol).get("symbol", "") if str(symbol or "").strip() else ""
    store = _store(settings)
    import time

    end_ts = int(time.time())
    start_ts = end_ts - max(1, min(int(window_sec or 86400), 2592000))
    result = store.list_signals(
        limit=max(1, min(int(limit or 50), 200)),
        cursor=cursor,
        module=str(module or "").strip().lower(),
        symbol=normalized,
        status=str(status or "").strip().lower(),
        start_ts=start_ts,
        end_ts=end_ts,
        q=str(q or "").strip()[:80],
    )
    raw_items = enhance_signals_with_decisions(result.get("items", []), window_sec=window_sec, settings=settings)
    items = _public_items(raw_items)
    return api_ok(
        {"items": items},
        items=items,
        count=len(items),
        next_cursor=result.get("next_cursor"),
        filters={
            "module": str(module or "").strip().lower(),
            "symbol": normalized,
            "status": str(status or "").strip().lower(),
            "q": str(q or "").strip()[:80],
            "window_sec": int(window_sec or 86400),
        },
        message="已读取公开信号",
    )


def public_signal_detail_payload(signal_id: int, *, settings: Settings | None = None) -> dict[str, Any]:
    store = _store(settings)
    item = store.signal_detail(int(signal_id or 0))
    if not item:
        return api_error("信号不存在", code="not_found")
    related = []
    if item.get("symbol"):
        related = [
            related_item
            for related_item in store.symbol_timeline(str(item.get("symbol") or ""), limit=8)
            if int(related_item.get("id") or 0) != int(item.get("id") or 0)
        ][:6]
    detail = signal_detail_view(item, related)
    header = _strip_forbidden(detail.get("header") or {})
    public_sections = [
        {
            "title": "信号摘要",
            "rows": [
                {"label": "时间", "value": str(item.get("time") or ""), "code": False},
                {"label": "模块", "value": str(item.get("module") or ""), "code": False},
                {"label": "币种", "value": str(item.get("symbol") or "全局/无币种"), "code": False},
                {"label": "状态", "value": str(item.get("status") or ""), "code": False},
                {"label": "摘要", "value": _short(item.get("excerpt") or item.get("title") or "", 600), "code": False},
            ],
        },
        {
            "title": "结构字段",
            "rows": [
                {"label": "signal_type", "value": str(item.get("signal_type") or ""), "code": False},
                {"label": "score", "value": str(item.get("score") if item.get("score") is not None else "-"), "code": False},
                {"label": "stage", "value": str(item.get("stage") or "-"), "code": False},
            ],
        },
    ]
    payload = {
        "ok": True,
        "item": public_signal_item(item),
        "detail": {
            "header": header,
            "sections": _strip_forbidden(public_sections),
            "related": {"same_symbol": _public_items(related)},
        },
        "message": "已读取公开信号详情",
    }
    return _strip_forbidden(payload)


def public_signal_stats_payload(*, window_sec: int = 86400, settings: Settings | None = None) -> dict[str, Any]:
    store = _store(settings)
    safe_window = max(1, min(int(window_sec or 86400), 2592000))
    stats = store.stats(window_sec=safe_window)
    latest = _public_items(store.list_signals(limit=8, start_ts=None, end_ts=None).get("items", []))
    payload = {
        "ok": True,
        **stats,
        **signal_stats_display(stats),
        "latest": latest,
        "message": "已读取公开信号统计",
    }
    return _strip_forbidden(payload)


def public_decision_payload(
    symbol: str,
    *,
    window_sec: int = 86400,
    limit: int = 50,
    settings: Settings | None = None,
) -> dict[str, Any]:
    payload = decision_for_symbol_payload(symbol, window_sec=window_sec, limit=limit, settings=settings)
    return _strip_forbidden(payload)


def public_decisions_payload(
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
    payload = decisions_payload(
        limit=limit,
        cursor=cursor,
        q=q,
        symbol=symbol,
        decision=decision,
        risk=risk,
        window_sec=window_sec,
        settings=settings,
    )
    return _strip_forbidden(payload)


def public_decisions_stats_payload(
    *,
    window_sec: int = 86400,
    limit: int = 100,
    settings: Settings | None = None,
) -> dict[str, Any]:
    payload = decisions_stats_payload(
        window_sec=window_sec,
        limit=limit,
        settings=settings,
        include_model_config=False,
    )
    return _strip_forbidden(payload)


def _public_timeline_payload(payload: dict[str, Any]) -> dict[str, Any]:
    items = [public_signal_item((item.get("signal") if isinstance(item, dict) else item) or item) for item in payload.get("items", []) if isinstance(item, dict)]
    groups = []
    for group in payload.get("groups", []) or []:
        if not isinstance(group, dict):
            continue
        groups.append({
            "date": group.get("date"),
            "label": group.get("label"),
            "count": group.get("count"),
            "modules": group.get("modules", []),
            "statuses": group.get("statuses", []),
            "items": [
                public_signal_item((event.get("signal") if isinstance(event, dict) else event) or event)
                for event in group.get("items", [])
                if isinstance(event, dict)
            ],
        })
    public = {
        "ok": bool(payload.get("ok", True)),
        "symbol": payload.get("symbol", ""),
        "coin": payload.get("coin", ""),
        "summary": payload.get("summary", {}),
        "groups": groups,
        "items": items,
        "count": len(items),
        "next_cursor": payload.get("next_cursor"),
        "filters": payload.get("filters", {}),
        "message": payload.get("message", "已读取公开时间线"),
    }
    return _strip_forbidden(public)


def public_timeline_payload(
    *,
    symbol: str = "",
    limit: int = 100,
    cursor: int | None = None,
    window_sec: int = 604800,
    module: str = "",
    status: str = "",
    q: str = "",
    settings: Settings | None = None,
) -> dict[str, Any]:
    payload = timeline_payload(
        symbol=symbol,
        limit=limit,
        cursor=cursor,
        window_sec=window_sec,
        module=module,
        status=status,
        q=q,
        settings=settings,
    )
    return _public_timeline_payload(payload)


def public_coin_search_payload(
    q: str = "",
    *,
    limit: int = 20,
    window_sec: int = 604800,
    settings: Settings | None = None,
) -> dict[str, Any]:
    return _strip_forbidden(coin_search_payload(q=q, limit=limit, window_sec=window_sec, settings=settings))


def public_coin_detail_payload(
    symbol_or_coin: str,
    *,
    limit: int = 100,
    window_sec: int = 604800,
    module: str = "",
    status: str = "",
    q: str = "",
    settings: Settings | None = None,
) -> dict[str, Any]:
    payload = coin_detail_payload(
        symbol_or_coin,
        limit=limit,
        window_sec=window_sec,
        module=module,
        status=status,
        q=q,
        settings=settings,
    )
    if not payload.get("ok", False):
        return payload
    public = {
        "ok": True,
        "coin": payload.get("coin", ""),
        "symbol": payload.get("symbol", ""),
        "generated_at": payload.get("generated_at", ""),
        "summary": payload.get("summary", {}),
        "module_counts": payload.get("module_counts", []),
        "status_counts": payload.get("status_counts", []),
        "timeline_summary": payload.get("timeline_summary", {}),
        "timeline_groups": _public_timeline_payload({"groups": payload.get("timeline_groups", []), "items": []}).get("groups", []),
        "timeline": _public_timeline_payload({"groups": payload.get("timeline", []), "items": []}).get("groups", []),
        "latest": [public_signal_item(item) for item in payload.get("latest", []) if isinstance(item, dict)],
        "related": {
            "latest_signal_id": payload.get("related", {}).get("latest_signal_id") if isinstance(payload.get("related"), dict) else None,
            "latest_sent": public_signal_item(payload["related"]["latest_sent"]) if isinstance(payload.get("related"), dict) and isinstance(payload["related"].get("latest_sent"), dict) else None,
            "latest_failed": public_signal_item(payload["related"]["latest_failed"]) if isinstance(payload.get("related"), dict) and isinstance(payload["related"].get("latest_failed"), dict) else None,
        },
        "filters": payload.get("filters", {}),
        "message": "已读取公开币种详情",
    }
    return _strip_forbidden(public)
