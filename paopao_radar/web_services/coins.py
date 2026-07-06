from __future__ import annotations

import re
import time
from collections import defaultdict
from typing import Any

from ..config import Settings
from ..signal_store import SignalEventStore
from .api_core import api_error, api_ok, normalize_symbol_filter, redact_api_payload
from .signals import enhance_signal_item, enhance_signal_items, signal_stats_display
from .timeline import group_timeline_by_day, timeline_payload, timeline_summary


COIN_QUERY_RE = re.compile(r"^[A-Za-z0-9]{1,24}$")


def normalize_coin_query(value: Any) -> dict[str, Any]:
    raw = str(value or "").strip()
    clean = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
    if not clean or len(clean) > 24 or not COIN_QUERY_RE.match(clean):
        return {
            "ok": False,
            "input": raw[:40],
            "coin": "",
            "symbol": "",
            "error": "请输入有效币种，例如 BTC 或 BTCUSDT",
        }
    info = normalize_symbol_filter(clean)
    return {
        "ok": True,
        "input": raw[:40],
        "coin": info["coin"],
        "symbol": info["symbol"],
    }


def _store(settings: Settings | None = None) -> SignalEventStore:
    loaded = settings or Settings.load()
    return SignalEventStore(loaded.signal_events_db_path)


def _window_bounds(window_sec: int) -> tuple[int, int]:
    end_ts = int(time.time())
    safe_window = max(1, min(int(window_sec or 604800), 2592000))
    return max(0, end_ts - safe_window), end_ts


def _timeline_groups(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    order: list[str] = []
    for item in items:
        date_key = str(item.get("time") or "")[:10] or "unknown"
        if date_key not in grouped:
            order.append(date_key)
        grouped[date_key].append(item)
    return [{"date": key, "items": grouped[key]} for key in order]


def _health(summary: dict[str, Any]) -> tuple[str, str]:
    failed = int(summary.get("failed", 0) or 0) + int(summary.get("blocked", 0) or 0)
    if failed:
        return "risk", "高风险"
    if int(summary.get("dry_run", 0) or 0) or int(summary.get("skipped", 0) or 0):
        return "attention", "需要关注"
    return "ok", "正常"


def _telegram_summary(items: list[dict[str, Any]]) -> dict[str, Any]:
    message_ids: list[int] = []
    topic_ids: list[str] = []
    reply_chain_count = 0
    for item in items:
        for msg_id in item.get("message_ids", []) or []:
            if isinstance(msg_id, int) and msg_id not in message_ids:
                message_ids.append(msg_id)
        topic_id = str(item.get("topic_id") or "")
        if topic_id and topic_id not in topic_ids:
            topic_ids.append(topic_id)
        if int(item.get("reply_to_message_id") or 0):
            reply_chain_count += 1
    return {
        "latest_message_ids": message_ids[:20],
        "topic_ids": topic_ids[:12],
        "reply_chain_count": reply_chain_count,
    }


def _search_item_display(item: dict[str, Any]) -> dict[str, Any]:
    symbol = str(item.get("symbol") or "")
    count = int(item.get("count") or 0)
    latest_at = str(item.get("latest_at") or "")
    return {
        **item,
        "label": symbol,
        "subtitle": f"最近 7 天 {count} 条信号" if count else "暂无信号",
        "latest_label": latest_at[:19] if latest_at else "-",
        "tone": "bad" if int(item.get("failed_count") or 0) else "info",
    }


def coin_search_payload(
    q: str = "",
    *,
    limit: int = 20,
    window_sec: int = 604800,
    settings: Settings | None = None,
) -> dict[str, Any]:
    start_ts, end_ts = _window_bounds(window_sec)
    store = _store(settings)
    raw_items = store.search_symbols(
        str(q or "").strip()[:40],
        limit=max(1, min(int(limit or 20), 100)),
        start_ts=start_ts,
        end_ts=end_ts,
    )
    items = [_search_item_display(item) for item in raw_items]
    return api_ok(
        {"items": redact_api_payload(items)},
        items=redact_api_payload(items),
        count=len(items),
        filters={"q": str(q or "").strip()[:40], "window_sec": int(window_sec or 604800)},
        message="已读取活跃币种",
    )


def coin_detail_payload(
    symbol_or_coin: str,
    *,
    limit: int = 100,
    window_sec: int = 604800,
    module: str = "",
    status: str = "",
    q: str = "",
    settings: Settings | None = None,
) -> dict[str, Any]:
    normalized = normalize_coin_query(symbol_or_coin)
    if not normalized.get("ok"):
        return api_error(normalized.get("error") or "币种参数无效", code="bad_request")
    start_ts, end_ts = _window_bounds(window_sec)
    safe_limit = max(1, min(int(limit or 100), 300))
    store = _store(settings)
    listed = store.list_timeline(
        symbol=normalized["symbol"],
        limit=safe_limit,
        start_ts=start_ts,
        end_ts=end_ts,
        module=str(module or "").strip().lower(),
        status=str(status or "").strip().lower(),
        q=str(q or "").strip()[:80],
    )
    items = enhance_signal_items(listed["items"])
    latest = items[:20]
    tl_summary = timeline_summary(listed["items"])
    tl_groups = group_timeline_by_day(listed["items"])
    stats = store.timeline_stats(
        symbol=normalized["symbol"],
        start_ts=start_ts,
        end_ts=end_ts,
        module=str(module or "").strip().lower(),
        status=str(status or "").strip().lower(),
        q=str(q or "").strip()[:80],
    )
    stats_display = signal_stats_display({
        "by_module": stats.get("by_module", {}),
        "by_status": stats.get("by_status", {}),
        "top_symbols": [{"symbol": normalized["symbol"], "count": stats.get("total", 0)}],
    })
    summary = {
        "total": int(stats.get("total", 0) or 0),
        "sent": int(stats.get("sent", 0) or 0),
        "dry_run": int(stats.get("dry_run", 0) or 0),
        "skipped": int(stats.get("skipped", 0) or 0),
        "blocked": int(stats.get("blocked", 0) or 0),
        "failed": int(stats.get("failed", 0) or 0),
        "latest_at": stats.get("latest_at", ""),
        "first_at": stats.get("first_at", ""),
        "active_modules": len(stats.get("by_module", {}) or {}),
    }
    health, health_label = _health(summary)
    summary["health"] = health
    summary["health_label"] = health_label
    summary["headline"] = (
        f"{normalized['symbol']} 最近 {max(1, int(window_sec or 604800)) // 86400 or 1} 天共有 "
        f"{summary['total']} 条信号，失败 {summary['failed']} 条。"
    )
    latest_sent = next((item for item in items if item.get("status") == "sent"), None)
    latest_failed = next((item for item in items if item.get("status") == "failed"), None)
    payload = {
        "ok": True,
        "coin": normalized["coin"],
        "symbol": normalized["symbol"],
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "summary": summary,
        "module_counts": stats_display["by_module_display"],
        "status_counts": stats_display["by_status_display"],
        "timeline": tl_groups,
        "timeline_summary": tl_summary,
        "timeline_groups": tl_groups,
        "latest": latest,
        "telegram": _telegram_summary(items),
        "related": {
            "latest_signal_id": items[0]["id"] if items else None,
            "latest_sent": latest_sent,
            "latest_failed": latest_failed,
        },
        "filters": {
            "input": normalized["input"],
            "coin": normalized["coin"],
            "symbol": normalized["symbol"],
            "limit": safe_limit,
            "window_sec": int(window_sec or 604800),
            "module": str(module or "").strip().lower(),
            "status": str(status or "").strip().lower(),
            "q": str(q or "").strip()[:80],
        },
        "message": "已读取币种详情",
    }
    return redact_api_payload(payload)


def coin_timeline_payload(
    symbol_or_coin: str,
    *,
    limit: int = 100,
    cursor: int | None = None,
    window_sec: int = 604800,
    module: str = "",
    status: str = "",
    q: str = "",
    settings: Settings | None = None,
) -> dict[str, Any]:
    normalized = normalize_coin_query(symbol_or_coin)
    if not normalized.get("ok"):
        return api_error(normalized.get("error") or "币种参数无效", code="bad_request")
    payload = timeline_payload(
        symbol=normalized["symbol"],
        limit=limit,
        cursor=cursor,
        window_sec=window_sec,
        module=module,
        status=status,
        q=q,
        settings=settings,
    )
    payload["coin"] = normalized["coin"]
    payload["symbol"] = normalized["symbol"]
    payload["timeline"] = payload.get("groups", [])
    payload["message"] = "已读取币种时间线"
    if isinstance(payload.get("data"), dict):
        payload["data"]["coin"] = normalized["coin"]
        payload["data"]["symbol"] = normalized["symbol"]
        payload["data"]["timeline"] = payload.get("groups", [])
    return payload
