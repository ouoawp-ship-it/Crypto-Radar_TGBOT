from __future__ import annotations

import inspect
import json
import re
from collections.abc import Mapping
from typing import Any, Callable

from ..config import Settings
from ..lifecycle_intelligence import NOT_ADVICE
from ..lifecycle_outcome_quality import (
    lifecycle_calibration_readiness,
    lifecycle_outcome_quality,
)
from ..runtime_cache import get_or_set
from .api_core import api_error, api_ok
from .lifecycle_outcomes import lifecycle_outcome_summary_payload as legacy_outcome_summary_payload


SENSITIVE_KEY_RE = re.compile(
    r"(?i)(?:token|secret|password|cookie|authorization|chat_?id|topic_?id|"
    r"message_?id|dedup_?key|payload_?json|text_?html|api_?key|raw_?telegram|"
    r"internal_?job|exception_?stack|traceback|database|server_?path|db_?path|"
    r"file_?path|candidate_?id|outcome_?id|last_error_summary)"
)
QUALITY_SECTIONS = {"summary", "reasons", "modules", "levels", "horizons", "timeline"}
TIME_RANGES = {"24h", "7d", "30d", "all"}


def _settings(settings: Settings | None = None) -> Settings:
    return settings or Settings.load()


def _safe_public(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _safe_public(item)
            for key, item in value.items()
            if not SENSITIVE_KEY_RE.search(str(key))
        }
    if isinstance(value, (list, tuple)):
        return [_safe_public(item) for item in list(value)[:1000]]
    if isinstance(value, str):
        return value[:1200]
    return value


def _core_data(result: Any) -> tuple[bool, dict[str, Any], str, str]:
    if not isinstance(result, Mapping):
        return False, {}, "Lifecycle Outcome quality returned an invalid result.", "invalid_result"
    item = dict(result)
    ok = bool(item.get("ok", True))
    message = str(item.get("message") or item.get("error") or "")
    code = str(item.get("code") or ("ok" if ok else "lifecycle_outcome_quality_error"))
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


def _quality_data(
    *,
    settings: Settings,
    symbol: str = "",
    lifecycle_id: int | None = None,
    horizon: str = "",
    module: str = "",
    time_range: str = "all",
) -> tuple[bool, dict[str, Any], str, str]:
    normalized_range = str(time_range or "all").strip().lower()
    if normalized_range not in TIME_RANGES:
        normalized_range = "all"
    cache_key = "lifecycle:outcome-quality:" + json.dumps(
        {
            "db": str(settings.lifecycle_db_path),
            "symbol": str(symbol or "").strip().upper(),
            "lifecycle_id": lifecycle_id,
            "horizon": str(horizon or "").strip().lower(),
            "module": str(module or "").strip().lower(),
            "time_range": normalized_range,
        },
        sort_keys=True,
        separators=(",", ":"),
    )

    def load() -> Any:
        return _call_supported(
            lifecycle_outcome_quality,
            settings=settings,
            symbol=str(symbol or ""),
            lifecycle_id=lifecycle_id,
            horizon=str(horizon or ""),
            module=str(module or ""),
            time_range=normalized_range,
            write_reports=False,
        )

    try:
        return _core_data(get_or_set(cache_key, 10, load))
    except Exception as exc:
        return False, {}, f"{type(exc).__name__}: {exc}", "quality_unavailable"


def lifecycle_outcome_quality_payload(
    section: str,
    *,
    symbol: str = "",
    lifecycle_id: int | None = None,
    horizon: str = "",
    module: str = "",
    time_range: str = "all",
    settings: Settings | None = None,
    public: bool = False,
) -> dict[str, Any]:
    normalized_section = str(section or "summary").strip().lower()
    if normalized_section not in QUALITY_SECTIONS:
        return api_error("Unknown Lifecycle Outcome quality section.", code="invalid_quality_section")
    normalized_range = str(time_range or "all").strip().lower()
    if normalized_range not in TIME_RANGES:
        normalized_range = "all"
    ok, quality, message, code = _quality_data(
        settings=_settings(settings),
        symbol=symbol,
        lifecycle_id=lifecycle_id,
        horizon=horizon,
        module=module,
        time_range=normalized_range,
    )
    if not ok:
        return api_error(
            "Lifecycle Outcome quality is temporarily unavailable." if public else (
                message or "Lifecycle Outcome quality is temporarily unavailable."
            ),
            code=code,
        )
    section_data = quality.get(normalized_section)
    if isinstance(section_data, Mapping):
        data: Any = (
            {"reasons": dict(section_data)}
            if normalized_section == "reasons"
            else dict(section_data)
        )
    elif isinstance(section_data, (list, tuple)):
        data = {"items": list(section_data)}
    elif normalized_section == "summary":
        data = dict(quality)
    else:
        data = {"items": [], "available": False}
    if isinstance(data, dict):
        if normalized_section == "summary":
            data.setdefault("status_counts", dict(quality.get("status_counts") or {}))
            data.setdefault("reasons", dict(quality.get("reasons") or {}))
        data.setdefault("time_range", normalized_range)
        data.setdefault("not_advice", NOT_ADVICE)
    return api_ok(
        _safe_public(data) if public else data,
        message=message or "Lifecycle Outcome quality loaded.",
    )


def lifecycle_calibration_readiness_payload(
    *,
    settings: Settings | None = None,
    public: bool = False,
) -> dict[str, Any]:
    loaded = _settings(settings)
    cache_key = f"lifecycle:calibration-readiness:{loaded.lifecycle_db_path}"
    try:
        result = get_or_set(
            cache_key,
            10,
            lambda: _call_supported(
                lifecycle_calibration_readiness,
                settings=loaded,
                write_reports=False,
            ),
        )
    except Exception as exc:
        return api_error(
            "Calibration readiness is temporarily unavailable." if public else f"{type(exc).__name__}: {exc}",
            code="calibration_readiness_unavailable",
        )
    ok, data, message, code = _core_data(result)
    if not ok:
        return api_error(message or "Calibration readiness is temporarily unavailable.", code=code)
    data.setdefault("not_advice", NOT_ADVICE)
    data.setdefault("does_not_modify_model", True)
    return api_ok(
        _safe_public(data) if public else data,
        message=message or "Lifecycle calibration readiness loaded.",
    )


def lifecycle_outcome_summary_with_quality_payload(
    *, settings: Settings | None = None, public: bool = False,
) -> dict[str, Any]:
    """Add v1.78.2 metrics without removing or renaming v1.78.1 fields."""
    loaded = _settings(settings)
    legacy = legacy_outcome_summary_payload(settings=loaded, public=public)
    if not isinstance(legacy, Mapping) or not legacy.get("ok"):
        return dict(legacy)
    result = dict(legacy)
    legacy_data = dict(result.get("data") or {})
    ok, quality, _message, _code = _quality_data(settings=loaded)
    summary = quality.get("summary") if isinstance(quality.get("summary"), Mapping) else quality
    if ok and isinstance(summary, Mapping):
        for key, value in summary.items():
            # New metric names are additive. Existing v1.78.1 fields keep
            # their original values and semantics.
            if key not in legacy_data:
                legacy_data[str(key)] = value
        legacy_data["reasons"] = dict(quality.get("reasons") or {})
        legacy_data["quality_available"] = True
    else:
        legacy_data.setdefault("quality_available", False)
    result["data"] = _safe_public(legacy_data) if public else legacy_data
    return result


def public_lifecycle_outcome_quality_payload(section: str, **kwargs: Any) -> dict[str, Any]:
    return lifecycle_outcome_quality_payload(section, public=True, **kwargs)


def public_lifecycle_calibration_readiness_payload(**kwargs: Any) -> dict[str, Any]:
    return lifecycle_calibration_readiness_payload(public=True, **kwargs)


def public_lifecycle_outcome_summary_with_quality_payload(**kwargs: Any) -> dict[str, Any]:
    return lifecycle_outcome_summary_with_quality_payload(public=True, **kwargs)


__all__ = [name for name in globals() if name.endswith("_payload")]
