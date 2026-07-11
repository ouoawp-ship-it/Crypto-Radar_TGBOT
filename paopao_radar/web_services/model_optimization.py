from __future__ import annotations

import importlib
import inspect
import json
import math
import re
from collections.abc import Mapping
from typing import Any, Callable

from ..config import Settings
from ..lifecycle_intelligence import NOT_ADVICE
from ..runtime_cache import get_or_set
from .api_core import api_error, api_ok


SECTIONS = {"summary", "scenarios", "report", "readiness"}
SCENARIOS = {"threshold_tuning", "risk_control", "lifecycle_quality", "module_rebalance"}
METADATA_KEYS = (
    "optimization_version", "production_model", "base_model", "generated_at", "status",
)
SENSITIVE_KEY_RE = re.compile(
    r"(?i)(?:token|secret|password|cookie|authorization|chat_?id|topic_?id|"
    r"message_?id|dedup_?key|payload_?json|text_?html|api_?key|raw_?telegram|"
    r"internal_?job|exception_?stack|traceback|database|server_?path|db_?path|"
    r"file_?path|absolute_?path|command_?line)"
)
SENSITIVE_VALUE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{25,}\b"), "<redacted>"),
    (re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9][A-Za-z0-9_-]{12,}\b"), "<redacted>"),
    (re.compile(r"(?i)\b(?:authorization|cookie)\s*:\s*[^\r\n]+"), "<redacted>"),
    (re.compile(r"(?i)(?:/home/|/Users/|[A-Z]:\\)[^\s\"']+"), "<redacted-path>"),
)


def _settings(settings: Settings | None) -> Settings:
    return settings or Settings.load()


def _core_function(name: str) -> Callable[..., Any]:
    module = importlib.import_module("paopao_radar.model_optimizer")
    function = getattr(module, name, None)
    if not callable(function):
        raise RuntimeError(f"optimizer core function is unavailable: {name}")
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
        text = value[:2000]
        if "Traceback (most recent call last)" in text:
            return "<redacted-error>"
        for pattern, replacement in SENSITIVE_VALUE_PATTERNS:
            text = pattern.sub(replacement, text)
        return text
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _limit_lists(value: Any, limit: int) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _limit_lists(item, limit) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_limit_lists(item, limit) for item in list(value)[:limit]]
    return value


def _report_data(result: Any) -> tuple[bool, dict[str, Any], str, str]:
    if not isinstance(result, Mapping):
        return False, {}, "Optimization report is unavailable.", "invalid_optimization_report"
    item = dict(result)
    ok = bool(item.get("ok", True))
    message = str(item.get("message") or item.get("error") or "")
    code = str(item.get("code") or ("ok" if ok else "optimization_report_unavailable"))
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


def _load_latest_report(settings: Settings) -> tuple[bool, dict[str, Any], str, str]:
    ttl = max(1, min(int(getattr(settings, "model_optimization_cache_ttl_sec", 30) or 30), 300))
    cache_key = f"optimization:latest:{settings.data_dir}"
    try:
        return _report_data(get_or_set(
            cache_key,
            ttl,
            lambda: _call_supported(_core_function("get_optimization_report"), settings=settings),
        ))
    except Exception:
        return False, {}, "Optimization report is unavailable.", "optimization_report_unavailable"


def _metadata(report: Mapping[str, Any]) -> dict[str, Any]:
    return {key: report.get(key) for key in METADATA_KEYS if key in report}


def _scenario_items(value: Any, limit: int) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)[:limit]
    if isinstance(value, Mapping):
        nested = value.get("items")
        if isinstance(nested, (list, tuple)):
            return list(nested)[:limit]
        return [
            ({"scenario": str(key), **dict(item)} if isinstance(item, Mapping) else {
                "scenario": str(key), "value": item,
            })
            for key, item in list(value.items())[:limit]
        ]
    return []


def _filter_scenario(value: Any, scenario: str) -> Any:
    if not scenario:
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _filter_scenario(item, scenario)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        rows = list(value)
        scoped = [
            item for item in rows
            if not isinstance(item, Mapping)
            or str(item.get("scenario") or item.get("scenario_key") or "") in {"", scenario}
        ]
        return scoped
    return value


def _section_data(
    report: dict[str, Any],
    section: str,
    *,
    scenario: str,
    limit: int,
) -> dict[str, Any]:
    metadata = _metadata(report)
    if section == "summary":
        raw = report.get("summary")
        data = dict(raw) if isinstance(raw, Mapping) else {}
    elif section == "scenarios":
        scenarios = _scenario_items(report.get("scenarios"), limit)
        data = {"items": _filter_scenario(scenarios, scenario), "scenarios": scenarios}
    elif section == "readiness":
        raw = report.get("readiness")
        data = dict(raw) if isinstance(raw, Mapping) else {"available": False}
    else:
        data = dict(report)
    data = _limit_lists(_filter_scenario(data, scenario), limit)
    for key, value in metadata.items():
        data.setdefault(key, value)
    data.setdefault("not_advice", NOT_ADVICE)
    data.setdefault("does_not_modify_model", True)
    data.setdefault("auto_apply", False)
    return data


def optimization_section_payload(
    section: str,
    *,
    scenario: str = "",
    symbol: str = "",
    limit: int = 100,
    settings: Settings | None = None,
    public: bool = False,
) -> dict[str, Any]:
    normalized = str(section or "summary").strip().lower()
    if normalized not in SECTIONS:
        return api_error("Unknown optimization report section.", code="invalid_optimization_section")
    if str(symbol or "").strip():
        return api_error(
            "Symbol-scoped optimization is available through the dry-run CLI only.",
            code="optimization_symbol_scope_requires_cli",
        )
    selected_scenario = str(scenario or "").strip().lower()
    if selected_scenario == "all":
        selected_scenario = ""
    if selected_scenario and selected_scenario not in SCENARIOS:
        return api_error("Unknown optimization scenario.", code="invalid_optimization_scenario")
    bounded_limit = max(1, min(int(limit or 100), 1000))
    ok, report, message, code = _load_latest_report(_settings(settings))
    if not ok:
        return api_error("Optimization report is unavailable.", code=code)
    data = _section_data(
        report, normalized, scenario=selected_scenario, limit=bounded_limit,
    )
    return api_ok(_safe(data), message=message or "Optimization report loaded.")


def optimization_report_payload(
    *,
    scenario: str = "",
    symbol: str = "",
    limit: int = 100,
    settings: Settings | None = None,
) -> dict[str, Any]:
    return optimization_section_payload(
        "report", scenario=scenario, symbol=symbol, limit=limit, settings=settings,
    )


def public_optimization_section_payload(section: str, **kwargs: Any) -> dict[str, Any]:
    return optimization_section_payload(section, public=True, **kwargs)


__all__ = [name for name in globals() if name.endswith("_payload")]
