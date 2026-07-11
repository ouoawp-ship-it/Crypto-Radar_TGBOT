from __future__ import annotations

import importlib
import inspect
import json
import re
from collections.abc import Mapping
from typing import Any, Callable

from ..config import Settings
from ..lifecycle_intelligence import NOT_ADVICE
from ..runtime_cache import get_or_set
from .api_core import api_error, api_ok


SECTIONS = {"summary", "decision", "lifecycle", "factors", "risk", "readiness"}
SENSITIVE_KEY_RE = re.compile(
    r"(?i)(?:token|secret|password|cookie|authorization|chat_?id|topic_?id|"
    r"message_?id|dedup_?key|payload_?json|text_?html|api_?key|raw_?telegram|"
    r"internal_?job|exception_?stack|traceback|database|server_?path|db_?path|"
    r"file_?path|absolute_?path|command_?line)"
)
METADATA_KEYS = ("calibration_version", "model_version", "status", "generated_at")


def _settings(settings: Settings | None) -> Settings:
    return settings or Settings.load()


def _core_function(name: str) -> Callable[..., Any]:
    module = importlib.import_module("paopao_radar.lifecycle_calibration")
    function = getattr(module, name, None)
    if not callable(function):
        raise RuntimeError(f"calibration core function is unavailable: {name}")
    return function


def _call_supported(function: Callable[..., Any], **kwargs: Any) -> Any:
    signature = inspect.signature(function)
    accepts_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    supported = kwargs if accepts_kwargs else {
        key: value for key, value in kwargs.items() if key in signature.parameters
    }
    return function(**supported)


def _safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _safe(item)
            for key, item in value.items()
            if not SENSITIVE_KEY_RE.search(str(key))
        }
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in list(value)[:1000]]
    if isinstance(value, str):
        return value[:2000]
    return value


def _report_data(result: Any) -> tuple[bool, dict[str, Any], str, str]:
    if not isinstance(result, Mapping):
        return False, {}, "Calibration report is unavailable.", "invalid_calibration_report"
    item = dict(result)
    ok = bool(item.get("ok", True))
    message = str(item.get("message") or item.get("error") or "")
    code = str(item.get("code") or ("ok" if ok else "calibration_report_unavailable"))
    raw_data = item.get("data")
    if isinstance(raw_data, Mapping):
        data = dict(raw_data)
        for key, value in item.items():
            if key not in {"ok", "data", "message", "error", "code"} and key not in data:
                data[key] = value
    else:
        data = {
            key: value
            for key, value in item.items()
            if key not in {"ok", "data", "message", "error", "code"}
        }
    return ok, data, message, code


def _load_latest_report(
    settings: Settings,
    *,
    symbol: str = "",
    limit: int = 100,
) -> tuple[bool, dict[str, Any], str, str]:
    normalized_symbol = str(symbol or "").strip().upper()
    bounded_limit = max(1, min(int(limit or 100), 1000))
    cache_key = "calibration:latest:" + json.dumps(
        {
            "scope": str(settings.data_dir),
            "symbol": normalized_symbol,
            "limit": bounded_limit,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    ttl = max(1, min(int(getattr(settings, "model_calibration_cache_ttl_sec", 30) or 30), 300))

    def load() -> Any:
        return _call_supported(
            _core_function("get_calibration_report"),
            settings=settings,
            symbol=normalized_symbol,
            limit=bounded_limit,
        )

    try:
        return _report_data(get_or_set(cache_key, ttl, load))
    except Exception:
        return False, {}, "Calibration report is unavailable.", "calibration_report_unavailable"


def _metadata(report: Mapping[str, Any]) -> dict[str, Any]:
    return {key: report.get(key) for key in METADATA_KEYS if key in report}


def _list(value: Any, limit: int) -> list[Any]:
    return list(value or [])[:limit] if isinstance(value, (list, tuple)) else []


def _limit_lists(value: Any, limit: int) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _limit_lists(item, limit) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_limit_lists(item, limit) for item in list(value)[:limit]]
    return value


def _section_data(report: dict[str, Any], section: str, limit: int) -> dict[str, Any]:
    metadata = _metadata(report)
    if section == "summary":
        raw = report.get("summary")
        data = dict(raw) if isinstance(raw, Mapping) else {}
    elif section == "decision":
        raw = report.get("decision") if "decision" in report else report.get("decision_labels")
        data = dict(raw) if isinstance(raw, Mapping) else {"items": _list(raw, limit)}
        data.setdefault("decision_labels", _list(report.get("decision_labels"), limit))
    elif section == "lifecycle":
        raw = report.get("lifecycle")
        data = dict(raw) if isinstance(raw, Mapping) else {}
        first_levels = _list(report.get("first_levels"), limit)
        data.setdefault("items", first_levels)
        data.setdefault("first_levels", first_levels)
        data.setdefault("upgrade_paths", _list(report.get("upgrade_paths"), limit))
        data.setdefault("intelligence_buckets", _list(report.get("intelligence_buckets"), limit))
    elif section == "factors":
        raw = report.get("factors")
        data = dict(raw) if isinstance(raw, Mapping) else {"items": _list(raw, limit)}
    elif section == "risk":
        raw = report.get("risk") if "risk" in report else report.get("risk_alerts")
        data = dict(raw) if isinstance(raw, Mapping) else {"items": _list(raw, limit)}
        data.setdefault("risk_alerts", _list(report.get("risk_alerts"), limit))
    else:
        raw = report.get("readiness") if "readiness" in report else report.get("calibration_readiness")
        data = dict(raw) if isinstance(raw, Mapping) else {"available": False}
    data = _limit_lists(data, limit)
    for key, value in metadata.items():
        data.setdefault(key, value)
    data.setdefault("not_advice", NOT_ADVICE)
    data.setdefault("does_not_modify_model", True)
    return data


def calibration_section_payload(
    section: str,
    *,
    symbol: str = "",
    limit: int = 100,
    settings: Settings | None = None,
    public: bool = False,
) -> dict[str, Any]:
    normalized = str(section or "summary").strip().lower()
    if normalized not in SECTIONS:
        return api_error("Unknown calibration report section.", code="invalid_calibration_section")
    if str(symbol or "").strip():
        return api_error(
            "Symbol-scoped calibration is available through the dry-run CLI only.",
            code="calibration_symbol_scope_requires_cli",
        )
    bounded_limit = max(1, min(int(limit or 100), 1000))
    ok, report, message, code = _load_latest_report(
        _settings(settings), symbol=symbol, limit=bounded_limit,
    )
    if not ok:
        return api_error("Calibration report is unavailable.", code=code)
    data = _section_data(report, normalized, bounded_limit)
    return api_ok(_safe(data), message=message or "Calibration report loaded.")


def calibration_report_payload(
    *,
    symbol: str = "",
    limit: int = 100,
    settings: Settings | None = None,
) -> dict[str, Any]:
    if str(symbol or "").strip():
        return api_error(
            "Symbol-scoped calibration is available through the dry-run CLI only.",
            code="calibration_symbol_scope_requires_cli",
        )
    ok, report, message, code = _load_latest_report(
        _settings(settings), symbol=symbol, limit=max(1, min(int(limit or 100), 1000)),
    )
    if not ok:
        return api_error("Calibration report is unavailable.", code=code)
    safe_report = _safe(_limit_lists(report, max(1, min(int(limit or 100), 1000))))
    if isinstance(safe_report, dict):
        safe_report.setdefault("not_advice", NOT_ADVICE)
        safe_report.setdefault("does_not_modify_model", True)
    return api_ok(safe_report, message=message or "Calibration report loaded.")


def public_calibration_section_payload(section: str, **kwargs: Any) -> dict[str, Any]:
    return calibration_section_payload(section, public=True, **kwargs)


__all__ = [name for name in globals() if name.endswith("_payload")]
