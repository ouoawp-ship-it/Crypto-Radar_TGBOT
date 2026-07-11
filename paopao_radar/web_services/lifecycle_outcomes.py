from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ..config import Settings
from ..lifecycle_intelligence import NOT_ADVICE
from ..lifecycle_outcomes import (
    lifecycle_outcome_coverage_list,
    lifecycle_outcome_detail,
    lifecycle_outcome_status,
)
from .api_core import api_error, api_ok


SENSITIVE_KEY_RE = re.compile(
    r"(?i)(?:token|secret|password|cookie|authorization|chat_?id|topic_?id|message_?id|"
    r"dedup_?key|payload_?json|text_?html|api_?key|raw_?telegram|internal_?job|"
    r"exception_?stack|traceback|database|server_?path|db_?path|file_?path|"
    r"(?:^|_)outcome_id$|primary_outcome_id)"
)

PUBLIC_COVERAGE_FIELDS = {
    "lifecycle_id", "symbol", "candidate_signal_count", "linked_signal_count",
    "linked_outcome_count", "horizon_1h_status", "horizon_4h_status",
    "horizon_24h_status", "horizon_72h_status", "linked_horizon_count",
    "mature_horizon_count", "link_coverage_ratio", "maturity_ratio",
    "coverage_label", "maturity_label", "unlinked_reason", "reasons",
    "calculated_at", "updated_at", "first_signal_level", "highest_level",
    "current_state", "is_active", "lifecycle_updated_at",
}


def _settings(settings: Settings | None = None) -> Settings:
    return settings or Settings.load()


def _limit(value: Any, default: int = 50, maximum: int = 300) -> int:
    try:
        return max(1, min(int(value if value not in {None, ""} else default), maximum))
    except (TypeError, ValueError):
        return default


def _offset(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _safe_public(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _safe_public(item)
            for key, item in value.items()
            if not SENSITIVE_KEY_RE.search(str(key))
        }
    if isinstance(value, (list, tuple)):
        return [_safe_public(item) for item in list(value)[:500]]
    if isinstance(value, str):
        return value[:1200]
    return value


def _core_data(result: Any) -> tuple[bool, dict[str, Any], str, str]:
    if not isinstance(result, Mapping):
        return False, {}, "Outcome 覆盖率服务返回格式异常", "invalid_result"
    item = dict(result)
    ok = bool(item.get("ok", True))
    message = str(item.get("message") or item.get("error") or "")
    code = str(item.get("code") or ("ok" if ok else "outcome_coverage_error"))
    data = item.get("data")
    if isinstance(data, Mapping):
        payload = dict(data)
        for key, value in item.items():
            if key not in {"ok", "data", "message", "error", "code"} and key not in payload:
                payload[key] = value
    else:
        payload = {
            key: value for key, value in item.items()
            if key not in {"ok", "data", "message", "error", "code"}
        }
    return ok, payload, message, code


def _envelope(result: Any, message: str, *, public: bool) -> dict[str, Any]:
    ok, data, result_message, code = _core_data(result)
    if not ok:
        return api_error(
            result_message or "生命周期 Outcome 数据暂时不可用。",
            code=code,
        )
    data.setdefault("not_advice", NOT_ADVICE)
    return api_ok(_safe_public(data) if public else data, message=result_message or message)


def _public_detail(data: dict[str, Any]) -> dict[str, Any]:
    coverage = data.get("coverage")
    safe_coverage = (
        {key: value for key, value in dict(coverage).items() if key in PUBLIC_COVERAGE_FIELDS}
        if isinstance(coverage, Mapping) else coverage
    )
    links: list[dict[str, Any]] = []
    for raw_link in list(data.get("links") or [])[:500]:
        if not isinstance(raw_link, Mapping):
            continue
        link = dict(raw_link)
        item = {
            key: link.get(key)
            for key in (
                "signal_id", "horizon", "outcome_status", "link_role", "link_method",
                "link_confidence", "signal_time", "outcome_time", "is_primary",
            )
            if key in link
        }
        outcome = link.get("outcome")
        if isinstance(outcome, Mapping):
            item["outcome"] = {
                key: outcome.get(key)
                for key in (
                    "signal_id", "horizon", "data_status", "signal_time", "due_time",
                    "final_return_pct", "max_gain_pct", "max_drawdown_pct", "result_label", "updated_at",
                )
                if key in outcome
            }
        links.append(item)
    coverage_symbol = safe_coverage.get("symbol") if isinstance(safe_coverage, Mapping) else ""
    return _safe_public({
        "available": data.get("available", False),
        "symbol": data.get("symbol") or coverage_symbol,
        "coverage": safe_coverage,
        "links": links,
        "not_advice": data.get("not_advice", NOT_ADVICE),
    })


def lifecycle_outcome_summary_payload(
    *, settings: Settings | None = None, public: bool = False,
) -> dict[str, Any]:
    result = lifecycle_outcome_status(settings=_settings(settings))
    return _envelope(result, "已读取生命周期 Outcome 覆盖率概览", public=public)


def lifecycle_outcome_coverage_payload(
    *,
    symbol: str = "",
    lifecycle_id: int | None = None,
    limit: int = 50,
    offset: int = 0,
    coverage_label: str = "",
    maturity_label: str = "",
    settings: Settings | None = None,
    public: bool = False,
) -> dict[str, Any]:
    page_limit = _limit(limit)
    page_offset = _offset(offset)
    result = lifecycle_outcome_coverage_list(
        settings=_settings(settings),
        symbol=str(symbol or ""),
        lifecycle_id=lifecycle_id,
        limit=page_limit,
        offset=page_offset,
        coverage_label=str(coverage_label or ""),
        maturity_label=str(maturity_label or ""),
    )
    ok, data, result_message, code = _core_data(result)
    if not ok:
        return api_error(result_message or "生命周期 Outcome 覆盖率列表暂时不可用。", code=code)
    if public and isinstance(data.get("items"), list):
        data["items"] = [
            _safe_public({key: value for key, value in dict(item).items() if key in PUBLIC_COVERAGE_FIELDS})
            for item in data["items"]
            if isinstance(item, Mapping)
        ]
    data.setdefault("pagination", {
        "limit": page_limit,
        "offset": page_offset,
        "has_more": page_offset + len(data.get("items") or []) < int(data.get("total") or 0),
    })
    data.setdefault("not_advice", NOT_ADVICE)
    return api_ok(_safe_public(data) if public else data, message=result_message or "已读取生命周期 Outcome 覆盖率列表")


def lifecycle_outcome_list_payload(**kwargs: Any) -> dict[str, Any]:
    return lifecycle_outcome_coverage_payload(**kwargs)


def lifecycle_outcome_detail_payload(
    symbol: str = "",
    *,
    lifecycle_id: int | None = None,
    settings: Settings | None = None,
    public: bool = False,
) -> dict[str, Any]:
    if not str(symbol or "").strip() and not lifecycle_id:
        return api_error("请提供币种或 lifecycle_id。", code="bad_request")
    result = lifecycle_outcome_detail(
        settings=_settings(settings), symbol=str(symbol or ""), lifecycle_id=lifecycle_id,
    )
    if not public:
        return _envelope(result, "已读取单币生命周期 Outcome 关联", public=False)
    ok, data, result_message, code = _core_data(result)
    if not ok:
        return api_error(result_message or "单币生命周期 Outcome 暂时不可用。", code=code)
    data.setdefault("not_advice", NOT_ADVICE)
    return api_ok(_public_detail(data), message=result_message or "已读取单币生命周期 Outcome 关联")


def lifecycle_outcome_reasons_payload(
    *, settings: Settings | None = None, public: bool = False,
) -> dict[str, Any]:
    ok, status, message, code = _core_data(lifecycle_outcome_status(settings=_settings(settings)))
    if not ok:
        return api_error(message or "Outcome 未关联原因暂时不可用。", code=code)
    data = {
        "unlinked_reasons": status.get("unlinked_reasons", status.get("reasons", {})),
        "lifecycle_count": status.get("lifecycle_count", status.get("total_lifecycles", 0)),
        "linked_lifecycle_count": status.get("linked_lifecycle_count", 0),
        "not_advice": NOT_ADVICE,
    }
    return api_ok(_safe_public(data) if public else data, message="已读取 Outcome 未关联原因")


def lifecycle_outcome_maturity_payload(
    *, settings: Settings | None = None, public: bool = False,
) -> dict[str, Any]:
    ok, status, message, code = _core_data(lifecycle_outcome_status(settings=_settings(settings)))
    if not ok:
        return api_error(message or "Outcome 数据成熟度暂时不可用。", code=code)
    keys = {
        "lifecycle_count", "linked_lifecycle_count", "mature_lifecycle_count",
        "link_coverage_ratio", "maturity_ratio", "horizons", "maturity",
        "not_due", "pending", "unavailable", "error",
    }
    data = {key: value for key, value in status.items() if key in keys}
    data["not_advice"] = NOT_ADVICE
    return api_ok(_safe_public(data) if public else data, message="已读取 Outcome 数据成熟度")


def public_lifecycle_outcome_summary_payload(**kwargs: Any) -> dict[str, Any]:
    return lifecycle_outcome_summary_payload(public=True, **kwargs)


def public_lifecycle_outcome_coverage_payload(**kwargs: Any) -> dict[str, Any]:
    return lifecycle_outcome_coverage_payload(public=True, **kwargs)


def public_lifecycle_outcome_list_payload(**kwargs: Any) -> dict[str, Any]:
    return lifecycle_outcome_list_payload(public=True, **kwargs)


def public_lifecycle_outcome_detail_payload(symbol: str = "", **kwargs: Any) -> dict[str, Any]:
    return lifecycle_outcome_detail_payload(symbol, public=True, **kwargs)


def public_lifecycle_outcome_reasons_payload(**kwargs: Any) -> dict[str, Any]:
    return lifecycle_outcome_reasons_payload(public=True, **kwargs)


def public_lifecycle_outcome_maturity_payload(**kwargs: Any) -> dict[str, Any]:
    return lifecycle_outcome_maturity_payload(public=True, **kwargs)


__all__ = [name for name in globals() if name.endswith("_payload")]
