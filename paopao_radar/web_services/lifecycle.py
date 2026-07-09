from __future__ import annotations

from typing import Any

from ..config import Settings
from ..lifecycle_engine import (
    NOT_ADVICE,
    enrich_event_display,
    enrich_lifecycle_display,
    backfill_lifecycles,
    scan_lifecycles,
)
from ..lifecycle_store import (
    LifecycleStore,
    normalize_lifecycle_symbol,
    public_lifecycle_event,
    public_lifecycle_item,
)
from .api_core import api_error, api_ok, redact_api_payload


def _settings(settings: Settings | None = None) -> Settings:
    return settings or Settings.load()


def _store(settings: Settings | None = None) -> LifecycleStore:
    loaded = _settings(settings)
    return LifecycleStore(getattr(loaded, "lifecycle_db_path", loaded.data_dir / "lifecycle.db"))


def _limit(value: Any, default: int = 50, maximum: int = 300) -> int:
    try:
        number = int(value if value is not None and value != "" else default)
    except (TypeError, ValueError):
        number = default
    return max(1, min(number, maximum))


def _summary_payload(store: LifecycleStore, *, public: bool) -> dict[str, Any]:
    summary = store.summary()
    top_items = [enrich_lifecycle_display(item) for item in summary.pop("top_items", [])]
    if public:
        top_items = [public_lifecycle_item(item) for item in top_items]
    data = {
        "summary": summary,
        "items": top_items,
        "not_advice": NOT_ADVICE,
    }
    payload = api_ok(redact_api_payload(data), message="已读取生命周期概览")
    payload.update(redact_api_payload(data))
    return payload


def lifecycle_summary_payload(*, settings: Settings | None = None, public: bool = False) -> dict[str, Any]:
    return _summary_payload(_store(settings), public=public)


def lifecycle_list_payload(
    *,
    symbol: str = "",
    state: str = "",
    level: str = "",
    risk: str = "",
    limit: int = 50,
    cursor: int | None = None,
    settings: Settings | None = None,
    public: bool = False,
) -> dict[str, Any]:
    store = _store(settings)
    listed = store.list_lifecycles(
        limit=_limit(limit),
        cursor=cursor,
        symbol=symbol,
        state=str(state or ""),
        level=str(level or ""),
        risk=str(risk or ""),
    )
    items = [enrich_lifecycle_display(item) for item in listed.get("items", [])]
    if public:
        items = [public_lifecycle_item(item) for item in items]
    filters = {
        "symbol": normalize_lifecycle_symbol(symbol) if str(symbol or "").strip() else "",
        "state": str(state or ""),
        "level": str(level or ""),
        "risk": str(risk or ""),
    }
    data = {
        "items": items,
        "count": len(items),
        "pagination": {"limit": _limit(limit), "next_cursor": listed.get("next_cursor")},
        "filters": filters,
        "not_advice": NOT_ADVICE,
    }
    payload = api_ok(redact_api_payload(data), message="已读取生命周期列表")
    payload.update(redact_api_payload(data))
    return payload


def lifecycle_detail_payload(
    symbol: str,
    *,
    settings: Settings | None = None,
    public: bool = False,
) -> dict[str, Any]:
    normalized = normalize_lifecycle_symbol(symbol)
    if not normalized:
        return api_error("请提供币种，例如 BTCUSDT。", code="bad_request", message="请提供币种，例如 BTCUSDT。")
    store = _store(settings)
    lifecycle = enrich_lifecycle_display(store.get_lifecycle(normalized))
    events = [enrich_event_display(item) for item in store.list_events(symbol=normalized, limit=30)]
    snapshots = store.list_snapshots(symbol=normalized, limit=60)
    if public:
        lifecycle = public_lifecycle_item(lifecycle)
        events = [public_lifecycle_event(item) for item in events]
        snapshots = [redact_api_payload(_public_snapshot(item)) for item in snapshots]
    data = {
        "symbol": normalized,
        "lifecycle": lifecycle,
        "events": events,
        "metrics": snapshots,
        "not_advice": NOT_ADVICE,
    }
    payload = api_ok(redact_api_payload(data), message="已读取单币生命周期")
    payload.update(redact_api_payload(data))
    return payload


def lifecycle_events_payload(
    *,
    symbol: str = "",
    limit: int = 100,
    settings: Settings | None = None,
    public: bool = False,
) -> dict[str, Any]:
    store = _store(settings)
    events = [enrich_event_display(item) for item in store.list_events(symbol=symbol, limit=_limit(limit, 100, 300))]
    if public:
        events = [public_lifecycle_event(item) for item in events]
    data = {
        "items": events,
        "count": len(events),
        "symbol": normalize_lifecycle_symbol(symbol) if str(symbol or "").strip() else "",
        "not_advice": NOT_ADVICE,
    }
    payload = api_ok(redact_api_payload(data), message="已读取生命周期事件")
    payload.update(redact_api_payload(data))
    return payload


def lifecycle_metrics_payload(
    *,
    symbol: str = "",
    timeframe: str = "",
    limit: int = 100,
    settings: Settings | None = None,
    public: bool = False,
) -> dict[str, Any]:
    normalized = normalize_lifecycle_symbol(symbol)
    if not normalized:
        return api_error("请提供币种，例如 BTCUSDT。", code="bad_request", message="请提供币种，例如 BTCUSDT。")
    store = _store(settings)
    items = store.list_snapshots(symbol=normalized, timeframe=str(timeframe or ""), limit=_limit(limit, 100, 300))
    if public:
        items = [_public_snapshot(item) for item in items]
    data = {
        "items": redact_api_payload(items),
        "count": len(items),
        "symbol": normalized,
        "timeframe": str(timeframe or ""),
        "not_advice": NOT_ADVICE,
    }
    payload = api_ok(data, message="已读取生命周期指标")
    payload.update(data)
    return redact_api_payload(payload)


def lifecycle_run_scan_payload(
    data: dict[str, Any] | None = None,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    payload = data or {}
    result = scan_lifecycles(
        settings=settings,
        lookback_hours=int(payload.get("lookback_hours") or 24),
        limit_symbols=int(payload.get("limit_symbols") or payload.get("limit") or 80),
        symbol=str(payload.get("symbol") or ""),
        dry_run=bool(payload.get("dry_run", False)),
        push=bool(payload.get("push", False)),
    )
    return api_ok(result, message=result.get("message", "生命周期扫描完成"), **result)


def lifecycle_run_backfill_payload(
    data: dict[str, Any] | None = None,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    payload = data or {}
    result = backfill_lifecycles(
        settings=settings,
        lookback_hours=int(payload.get("lookback_hours") or 168),
        dry_run=bool(payload.get("dry_run", False)),
    )
    return api_ok(result, message=result.get("message", "生命周期回填完成"), **result)


def _public_snapshot(item: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "id", "symbol", "timeframe", "snapshot_time", "price", "volume", "quote_volume", "oi",
        "oi_value_usdt", "futures_cvd_delta", "spot_cvd_delta", "funding_rate", "market_cap_usd",
        "metrics", "created_at",
    }
    return {key: value for key, value in item.items() if key in allowed}


def public_lifecycle_summary_payload(**kwargs: Any) -> dict[str, Any]:
    kwargs["public"] = True
    return lifecycle_summary_payload(**kwargs)


def public_lifecycle_list_payload(**kwargs: Any) -> dict[str, Any]:
    kwargs["public"] = True
    return lifecycle_list_payload(**kwargs)


def public_lifecycle_detail_payload(symbol: str, **kwargs: Any) -> dict[str, Any]:
    kwargs["public"] = True
    return lifecycle_detail_payload(symbol, **kwargs)


def public_lifecycle_events_payload(**kwargs: Any) -> dict[str, Any]:
    kwargs["public"] = True
    return lifecycle_events_payload(**kwargs)


def public_lifecycle_metrics_payload(**kwargs: Any) -> dict[str, Any]:
    kwargs["public"] = True
    return lifecycle_metrics_payload(**kwargs)
