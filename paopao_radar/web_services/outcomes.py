from __future__ import annotations

import time
from typing import Any

from ..config import Settings
from ..outcome_tracker import OutcomeStore, scan_outcomes
from .api_core import api_error, api_ok, normalize_symbol_filter, redact_api_payload


PUBLIC_OUTCOME_COLUMNS = (
    "id",
    "signal_id",
    "symbol",
    "coin",
    "signal_time",
    "horizon",
    "horizon_sec",
    "direction",
    "entry_price",
    "future_price",
    "max_high_price",
    "min_low_price",
    "final_return_pct",
    "max_gain_pct",
    "max_drawdown_pct",
    "result_label",
    "result_tone",
    "decision_code",
    "decision_label",
    "decision_confidence",
    "risk_level",
    "module",
    "signal_type",
    "signal_score",
    "signal_stage",
    "data_status",
)


def _settings(settings: Settings | None = None) -> Settings:
    return settings or Settings.load()


def _store(settings: Settings | None = None) -> OutcomeStore:
    loaded = _settings(settings)
    return OutcomeStore(loaded.outcome_db_path)


def _limit(value: Any, default: int = 50, maximum: int = 300) -> int:
    try:
        number = int(value if value is not None and value != "" else default)
    except (TypeError, ValueError):
        number = default
    return max(1, min(number, maximum))


def _safe_window(value: Any, default: int = 604800) -> int:
    try:
        number = int(value if value is not None and value != "" else default)
    except (TypeError, ValueError):
        number = default
    return max(1, min(number, 2592000))


def _time_bounds(window_sec: int) -> tuple[str, str]:
    end_ts = int(time.time())
    start_ts = end_ts - max(1, int(window_sec or 604800))
    import datetime as _dt

    return (
        _dt.datetime.fromtimestamp(start_ts, _dt.timezone.utc).isoformat(),
        _dt.datetime.fromtimestamp(end_ts, _dt.timezone.utc).isoformat(),
    )


def _public_item(item: dict[str, Any]) -> dict[str, Any]:
    allowed = set(PUBLIC_OUTCOME_COLUMNS)
    return {key: redact_api_payload(value) for key, value in item.items() if key in allowed}


def _summary_from_stats(stats: dict[str, Any], *, horizon: str = "", symbol: str = "") -> dict[str, Any]:
    total = int(stats.get("total") or 0)
    success = int(stats.get("success_count") or 0)
    horizon_text = f"{horizon} " if horizon else ""
    symbol_text = f"{symbol} " if symbol else ""
    return {
        "total": total,
        "success_count": success,
        "pending_count": int(stats.get("pending_count") or 0),
        "unavailable_count": int(stats.get("unavailable_count") or 0),
        "error_count": int(stats.get("error_count") or 0),
        "headline": f"{symbol_text}{horizon_text}结果追踪共 {total} 条，已计算 {success} 条，正收益比例 {round(float(stats.get('positive_ratio') or 0) * 100, 1)}%。",
    }


def outcomes_payload(
    *,
    symbol: str = "",
    horizon: str = "",
    decision: str = "",
    result: str = "",
    module: str = "",
    data_status: str = "",
    window_sec: int = 604800,
    limit: int = 50,
    cursor: int | None = None,
    sort: str = "-id",
    settings: Settings | None = None,
    public: bool = False,
) -> dict[str, Any]:
    loaded = _settings(settings)
    start_time, end_time = _time_bounds(_safe_window(window_sec, 604800))
    normalized = normalize_symbol_filter(symbol).get("symbol", "") if str(symbol or "").strip() else ""
    store = _store(loaded)
    with store.connect() as connection:
        listed = store.list_outcomes(
            limit=_limit(limit),
            cursor=cursor,
            symbol=normalized,
            horizon=str(horizon or ""),
            decision=str(decision or ""),
            result=str(result or ""),
            module=str(module or ""),
            data_status=str(data_status or ""),
            start_time=start_time,
            end_time=end_time,
            sort=sort,
            columns=PUBLIC_OUTCOME_COLUMNS if public else None,
            connection=connection,
        )
        stats = store.stats(
            horizon=str(horizon or ""),
            symbol=normalized,
            decision=str(decision or ""),
            module=str(module or ""),
            connection=connection,
        )
    raw_items = listed.get("items", [])
    items = [_public_item(item) for item in raw_items] if public else redact_api_payload(raw_items)
    filters = {
        "symbol": normalized,
        "horizon": str(horizon or ""),
        "decision": str(decision or ""),
        "result": str(result or ""),
        "module": str(module or ""),
        "data_status": str(data_status or ""),
        "window_sec": _safe_window(window_sec, 604800),
    }
    summary = _summary_from_stats(stats, horizon=str(horizon or ""), symbol=normalized)
    data = {
        "items": items,
        "summary": summary,
        "stats": redact_api_payload(stats),
        "filters": filters,
        "pagination": {"limit": _limit(limit), "next_cursor": listed.get("next_cursor")},
    }
    payload = api_ok(data, message="已读取信号结果追踪")
    payload.update({
        "items": items,
        "count": len(items),
        "next_cursor": listed.get("next_cursor"),
        "summary": summary,
        "filters": filters,
        "pagination": data["pagination"],
    })
    return redact_api_payload(payload)


def outcome_stats_payload(
    *,
    horizon: str = "",
    symbol: str = "",
    decision: str = "",
    module: str = "",
    settings: Settings | None = None,
    public: bool = False,
) -> dict[str, Any]:
    normalized = normalize_symbol_filter(symbol).get("symbol", "") if str(symbol or "").strip() else ""
    store = _store(settings)
    stats = store.stats(horizon=str(horizon or ""), symbol=normalized, decision=str(decision or ""), module=str(module or ""))
    data: dict[str, Any] = {
        "horizon": str(horizon or ""),
        "symbol": normalized,
        **stats,
        "summary": _summary_from_stats(stats, horizon=str(horizon or ""), symbol=normalized),
    }
    if public:
        data.pop("error", None)
    payload = api_ok(redact_api_payload(data), message="已读取结果追踪统计")
    payload.update(redact_api_payload(data))
    return payload


def symbol_outcomes_payload(
    symbol: str,
    *,
    horizon: str = "",
    limit: int = 50,
    window_sec: int = 2592000,
    settings: Settings | None = None,
    public: bool = False,
) -> dict[str, Any]:
    normalized = normalize_symbol_filter(symbol).get("symbol", "")
    if not normalized:
        return api_error("请提供币种，例如 BTCUSDT。", code="bad_request", message="请提供币种，例如 BTCUSDT。")
    return outcomes_payload(
        symbol=normalized,
        horizon=horizon,
        limit=limit,
        window_sec=window_sec,
        settings=settings,
        public=public,
    )


def outcome_scan_payload(
    data: dict[str, Any] | None = None,
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    payload = data or {}
    result = scan_outcomes(
        settings=settings,
        limit=_limit(payload.get("limit"), 100, 500),
        horizon=str(payload.get("horizon") or ""),
        symbol=str(payload.get("symbol") or ""),
        dry_run=bool(payload.get("dry_run", False)),
        backfill_days=int(payload.get("backfill_days") or 7),
    )
    return api_ok(result, message=result.get("message", "信号结果追踪扫描完成"), **result)


def public_outcomes_payload(**kwargs: Any) -> dict[str, Any]:
    kwargs["public"] = True
    return outcomes_payload(**kwargs)


def public_outcome_stats_payload(**kwargs: Any) -> dict[str, Any]:
    kwargs["public"] = True
    return outcome_stats_payload(**kwargs)


def public_symbol_outcomes_payload(symbol: str, **kwargs: Any) -> dict[str, Any]:
    kwargs["public"] = True
    return symbol_outcomes_payload(symbol, **kwargs)
