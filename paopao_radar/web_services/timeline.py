from __future__ import annotations

import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from ..config import Settings
from ..signal_store import SignalEventStore
from .api_core import api_ok, normalize_symbol_filter, redact_api_payload
from .signals import enhance_signal_item, signal_display


def _store(settings: Settings | None = None) -> SignalEventStore:
    loaded = settings or Settings.load()
    return SignalEventStore(loaded.signal_events_db_path)


def _window_bounds(window_sec: int) -> tuple[int, int]:
    end_ts = int(time.time())
    safe_window = max(1, min(int(window_sec or 604800), 2592000))
    return max(0, end_ts - safe_window), end_ts


def _parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def _date_label(item: dict[str, Any]) -> str:
    parsed = _parse_time(item.get("time"))
    if parsed:
        return parsed.strftime("%Y-%m-%d")
    text = str(item.get("time") or "")
    return text[:10] if text else "unknown"


def _time_label(item: dict[str, Any], fallback: str) -> str:
    parsed = _parse_time(item.get("time"))
    if parsed:
        return parsed.strftime("%m-%d %H:%M")
    return fallback or str(item.get("time") or "-")[:16]


def _counted(values: list[str]) -> list[dict[str, Any]]:
    counts = Counter(value for value in values if value)
    return [{"name": key, "count": int(count)} for key, count in counts.most_common()]


def timeline_event_display(signal_item: dict[str, Any]) -> dict[str, Any]:
    enhanced = enhance_signal_item(signal_item)
    display = signal_display(enhanced)
    message_ids = enhanced.get("message_ids", []) if isinstance(enhanced.get("message_ids"), list) else []
    event = {
        "id": enhanced.get("id"),
        "symbol": str(enhanced.get("symbol") or ""),
        "coin": str(enhanced.get("coin") or ""),
        "time": str(enhanced.get("time") or ""),
        "time_label": _time_label(enhanced, str(display.get("time_label") or "")),
        "date_label": _date_label(enhanced),
        "module": str(enhanced.get("module") or ""),
        "module_label": display.get("module_label") or enhanced.get("module") or "",
        "status": str(enhanced.get("status") or ""),
        "status_label": display.get("status_label") or enhanced.get("status") or "",
        "tone": display.get("card_tone") or "neutral",
        "title": display.get("title") or enhanced.get("title") or "",
        "summary": display.get("summary") or enhanced.get("excerpt") or "",
        "score_label": display.get("score_label") or "-",
        "stage_label": display.get("stage_label") or "-",
        "badges": display.get("badges") or [],
        "telegram": {
            "has_message": bool(message_ids),
            "message_ids": message_ids,
            "topic_id": str(enhanced.get("topic_id") or ""),
            "reply_to_message_id": int(enhanced.get("reply_to_message_id") or 0),
        },
        "signal": enhanced,
        "display": display,
    }
    return redact_api_payload(event)


def group_timeline_by_day(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _group_timeline_events([timeline_event_display(item) for item in items])


def _group_timeline_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    order: list[str] = []
    for event in events:
        date = str(event.get("date_label") or "unknown")
        if date not in grouped:
            order.append(date)
        grouped[date].append(event)
    groups: list[dict[str, Any]] = []
    for date in order:
        events = grouped[date]
        groups.append({
            "date": date,
            "label": date,
            "count": len(events),
            "modules": _counted([str(item.get("module") or "") for item in events]),
            "statuses": _counted([str(item.get("status") or "") for item in events]),
            "items": events,
        })
    return groups


def timeline_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    return _timeline_summary_events([timeline_event_display(item) for item in items])


def _timeline_summary_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    statuses = Counter(str(item.get("status") or "") for item in events)
    modules = Counter(str(item.get("module") or "") for item in events)
    dominant_module = modules.most_common(1)[0][0] if modules else ""
    dominant_label = ""
    if dominant_module:
        dominant_label = next(
            (str(item.get("module_label") or "") for item in events if item.get("module") == dominant_module),
            dominant_module,
        )
    total = len(events)
    failed_count = int(statuses.get("failed", 0) + statuses.get("blocked", 0))
    health = "risk" if failed_count else ("attention" if int(statuses.get("skipped", 0) + statuses.get("dry_run", 0)) else "ok")
    first_at = str(events[-1].get("time") or "") if events else ""
    latest_at = str(events[0].get("time") or "") if events else ""
    headline = (
        f"最近窗口共有 {total} 条信号，失败/阻止 {failed_count} 条"
        + (f"，主要来自 {dominant_label}。" if dominant_label else "。")
    )
    return {
        "total": total,
        "sent": int(statuses.get("sent", 0)),
        "failed": int(statuses.get("failed", 0)),
        "blocked": int(statuses.get("blocked", 0)),
        "skipped": int(statuses.get("skipped", 0)),
        "dry_run": int(statuses.get("dry_run", 0)),
        "module_count": len([key for key in modules if key]),
        "first_at": first_at,
        "latest_at": latest_at,
        "dominant_module": dominant_module,
        "dominant_module_label": dominant_label,
        "health": health,
        "headline": headline,
    }


def timeline_payload(
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
    start_ts, end_ts = _window_bounds(window_sec)
    symbol_info = normalize_symbol_filter(symbol)
    normalized_symbol = symbol_info.get("symbol", "") if str(symbol or "").strip() else ""
    safe_limit = max(1, min(int(limit or 100), 300))
    store = _store(settings)
    listed = store.list_timeline(
        symbol=normalized_symbol,
        limit=safe_limit,
        cursor=cursor,
        start_ts=start_ts,
        end_ts=end_ts,
        module=str(module or "").strip().lower(),
        status=str(status or "").strip().lower(),
        q=str(q or "").strip()[:80],
        compact=True,
    )
    items = listed.get("items", [])
    events = [timeline_event_display(item) for item in items]
    groups = _group_timeline_events(events)
    summary = _timeline_summary_events(events)
    payload = {
        "ok": True,
        "symbol": normalized_symbol,
        "coin": symbol_info.get("coin", "") if normalized_symbol else "",
        "summary": summary,
        "groups": groups,
        "items": events,
        "count": len(events),
        "next_cursor": listed.get("next_cursor"),
        "filters": {
            "symbol": normalized_symbol,
            "module": str(module or "").strip().lower(),
            "status": str(status or "").strip().lower(),
            "q": str(q or "").strip()[:80],
            "limit": safe_limit,
            "window_sec": int(window_sec or 604800),
        },
        "message": "已读取信号时间线",
    }
    return api_ok(payload, **payload)
