from __future__ import annotations

import re
import time
from collections.abc import Mapping
from typing import Any


TOKEN_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{25,}\b"), "<redacted:telegram-token>"),
    (re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9][A-Za-z0-9_-]{12,}\b"), "<redacted:api-key>"),
)
SENSITIVE_KEY_RE = re.compile(r"(?i)(token|api[_-]?key|apikey|secret|password)")


def _first_value(query: Mapping[str, Any] | None, key: str, default: Any = "") -> Any:
    if not query:
        return default
    value = query.get(key, default)
    if isinstance(value, (list, tuple)):
        return value[0] if value else default
    return value


def _has_query_key(query: Mapping[str, Any] | None, key: str) -> bool:
    return bool(query is not None and key in query)


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return int(default)
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return int(default)


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return float(default)
        return float(str(value).strip())
    except (TypeError, ValueError):
        return float(default)


def clamp(value: Any, minimum: int | float, maximum: int | float) -> Any:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(minimum)
    if number < minimum:
        number = float(minimum)
    if number > maximum:
        number = float(maximum)
    if isinstance(minimum, int) and isinstance(maximum, int):
        return int(number)
    return number


def pagination_params(
    query: Mapping[str, Any] | None,
    *,
    default_limit: int = 50,
    max_limit: int = 200,
) -> dict[str, Any]:
    limit = clamp(safe_int(_first_value(query, "limit", default_limit), default_limit), 1, max_limit)
    cursor_raw = _first_value(query, "cursor", "")
    cursor = safe_int(cursor_raw, 0) if str(cursor_raw or "").strip() else None
    offset = max(0, safe_int(_first_value(query, "offset", 0), 0))
    page = max(1, safe_int(_first_value(query, "page", 1), 1))
    return {
        "limit": limit,
        "cursor": cursor,
        "offset": offset,
        "page": page,
        "max_limit": int(max_limit),
    }


def filter_params(query: Mapping[str, Any] | None, allowed_fields: set[str] | tuple[str, ...] | list[str]) -> dict[str, str]:
    filters: dict[str, str] = {}
    for field in allowed_fields:
        value = str(_first_value(query, str(field), "") or "").strip()
        if value:
            filters[str(field)] = value
    return filters


def sort_params(
    query: Mapping[str, Any] | None,
    allowed_fields: set[str] | tuple[str, ...] | list[str],
    *,
    default: str = "-ts",
) -> dict[str, str]:
    allowed = {str(field) for field in allowed_fields}
    raw = str(_first_value(query, "sort", default) or default).strip() or default

    def parse(value: str) -> tuple[str, str, str]:
        direction = "desc" if value.startswith("-") else "asc"
        field = value[1:] if value.startswith("-") else value
        return field, direction, f"{'-' if direction == 'desc' else ''}{field}"

    field, direction, normalized = parse(raw)
    if field not in allowed:
        field, direction, normalized = parse(default)
        if field not in allowed:
            field = sorted(allowed)[0] if allowed else "id"
            direction = "desc"
            normalized = f"-{field}"
    return {"field": field, "direction": direction, "raw": normalized}


def time_range_params(
    query: Mapping[str, Any] | None,
    *,
    default_window_sec: int = 86400,
) -> dict[str, Any]:
    applied = any(_has_query_key(query, key) for key in ("window_sec", "start_ts", "end_ts"))
    window_sec = max(1, safe_int(_first_value(query, "window_sec", default_window_sec), default_window_sec))
    start_raw = _first_value(query, "start_ts", "")
    end_raw = _first_value(query, "end_ts", "")
    start_ts = safe_int(start_raw, 0) if str(start_raw or "").strip() else None
    end_ts = safe_int(end_raw, 0) if str(end_raw or "").strip() else None
    if applied and start_ts is None and end_ts is None:
        end_ts = int(time.time())
        start_ts = max(0, end_ts - window_sec)
    return {
        "window_sec": window_sec,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "applied": applied,
    }


def normalize_symbol_filter(value: Any) -> dict[str, str]:
    raw = str(value or "").strip()
    clean = re.sub(r"[^A-Za-z0-9]", "", raw).upper()
    if not clean:
        return {"input": raw, "symbol": "", "coin": ""}
    if clean.endswith("USD") and not clean.endswith("USDT"):
        clean = f"{clean}T"
    if clean.endswith("USDT"):
        symbol = clean
        coin = clean[:-4]
    else:
        coin = clean
        symbol = f"{coin}USDT"
    return {"input": raw, "symbol": symbol, "coin": coin}


def redact_api_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            redacted[key_text] = "<redacted>" if SENSITIVE_KEY_RE.search(key_text) else redact_api_payload(item)
        return redacted
    if isinstance(value, list):
        return [redact_api_payload(item) for item in value]
    if isinstance(value, tuple):
        return [redact_api_payload(item) for item in value]
    if isinstance(value, str):
        text = value
        for pattern, replacement in TOKEN_PATTERNS:
            text = pattern.sub(replacement, text)
        if SENSITIVE_KEY_RE.search(text) and "=" in text:
            return "<redacted:sensitive-line>"
        return text
    return value


def api_ok(
    data: Any = None,
    *,
    message: str = "",
    pagination: dict[str, Any] | None = None,
    filters: dict[str, Any] | None = None,
    sort: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": True, "data": redact_api_payload(data)}
    if message:
        payload["message"] = message
    if pagination is not None:
        payload["pagination"] = pagination
    if filters is not None:
        payload["filters"] = filters
    if sort is not None:
        payload["sort"] = sort
    payload.update(redact_api_payload(extra))
    return payload


def api_error(
    error: Any,
    *,
    message: str | None = None,
    code: str = "internal_error",
    details: Any = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "error": redact_api_payload(str(error)),
        "message": redact_api_payload(str(message if message is not None else error)),
        "code": str(code or "internal_error"),
    }
    if details is not None:
        payload["details"] = redact_api_payload(details)
    return payload


def api_list_payload(
    items: list[dict[str, Any]] | list[Any],
    *,
    count: int | None = None,
    next_cursor: Any = None,
    message: str = "",
    pagination: dict[str, Any] | None = None,
    filters: dict[str, Any] | None = None,
    sort: dict[str, Any] | None = None,
    alias: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    safe_items = redact_api_payload(items)
    item_count = len(items) if count is None else int(count)
    payload = api_ok(
        {"items": safe_items},
        message=message,
        pagination=pagination,
        filters=filters,
        sort=sort,
        items=safe_items,
        count=item_count,
        next_cursor=next_cursor,
        **extra,
    )
    if alias:
        payload[alias] = safe_items
    return payload


def api_contract_self_test(*, settings: Any = None) -> dict[str, Any]:
    started = time.time()
    checks: list[dict[str, Any]] = []

    def run_check(name: str, func: Any) -> None:
        item_started = time.time()
        try:
            payload = func()
            ok = bool(isinstance(payload, Mapping) and payload.get("ok", True))
            checks.append({
                "name": name,
                "ok": ok,
                "elapsed_ms": int((time.time() - item_started) * 1000),
            })
        except Exception as exc:  # pragma: no cover - defensive guard for production self-test.
            checks.append({
                "name": name,
                "ok": False,
                "elapsed_ms": int((time.time() - item_started) * 1000),
                "error": f"{type(exc).__name__}: {exc}",
            })

    from .. import web as web_module
    from .coins import coin_detail_payload, coin_search_payload, coin_timeline_payload
    from .dashboard import dashboard_payload
    from .jobs import jobs_payload, jobs_stats_payload
    from .ops import update_check_status_payload
    from .timeline import timeline_payload

    run_check("dashboard", lambda: dashboard_payload(settings=settings))
    run_check("signals", lambda: web_module.signals_payload(limit=1, settings=settings))
    run_check("jobs", lambda: jobs_payload(limit=1, settings=settings))
    run_check("jobs_stats", lambda: jobs_stats_payload(settings=settings))
    run_check("update-status", lambda: update_check_status_payload(settings=settings))
    run_check("signal-timeline", lambda: timeline_payload(limit=1, settings=settings))
    coin_search = coin_search_payload(q="", limit=1, settings=settings)
    run_check("coin-search", lambda: coin_search)
    sample_items = coin_search.get("items", []) if isinstance(coin_search, Mapping) else []
    sample_symbol = ""
    if sample_items and isinstance(sample_items[0], Mapping):
        sample_symbol = str(sample_items[0].get("symbol") or sample_items[0].get("coin") or "")
    if sample_symbol:
        run_check("coin-detail", lambda: coin_detail_payload(sample_symbol, limit=5, settings=settings))
        run_check("coin-timeline", lambda: coin_timeline_payload(sample_symbol, limit=5, settings=settings))
        run_check("coin-detail-timeline", lambda: {"ok": bool(coin_detail_payload(sample_symbol, limit=5, settings=settings).get("timeline_groups") is not None)})
    else:
        checks.append({"name": "coin-detail", "ok": True, "skipped": True, "elapsed_ms": 0})
        checks.append({"name": "coin-timeline", "ok": True, "skipped": True, "elapsed_ms": 0})
        checks.append({"name": "coin-detail-timeline", "ok": True, "skipped": True, "elapsed_ms": 0})

    failed = [item for item in checks if not item.get("ok")]
    return api_ok(
        {
            "elapsed_ms": int((time.time() - started) * 1000),
            "checks": checks,
            "ok_count": len(checks) - len(failed),
            "fail_count": len(failed),
        },
        message="Web API contract self-test completed" if not failed else "Web API contract self-test found issues",
        checks=checks,
        errors=[item.get("error", item["name"]) for item in failed],
    )
