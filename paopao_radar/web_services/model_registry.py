from __future__ import annotations

import importlib
import inspect
import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable

from ..config import Settings
from ..runtime_cache import get_or_set
from .api_core import api_error, api_ok


PUBLIC_SECTIONS = {"current", "history", "performance", "health"}
MODEL_KEY_RE = re.compile(r"[a-z0-9][a-z0-9._-]{1,63}")
VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
SENSITIVE_KEY_RE = re.compile(
    r"(?i)(?:token|secret|password|cookie|authorization|chat_?id|topic_?id|"
    r"message_?id|dedup_?key|payload_?json|text_?html|api_?key|raw_?telegram|"
    r"exception|traceback|database|db_?path|server_?path|file_?path|absolute_?path|"
    r"command(?:_?line)?|internal_?job)"
)
PRIVATE_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(?:token|secret|password|cookie|authorization|chat_?id|topic_?id|"
    r"message_?id|api_?key|raw_?telegram|exception|traceback|database|db_?path|"
    r"server_?path|file_?path|absolute_?path)"
)
PATH_RE = re.compile(r"(?i)(?:/home/|/Users/|[A-Z]:\\)[^\s\"']+")

CURRENT_FIELDS = {
    "model_key", "model_version", "model_type", "status", "health_status", "health_label",
    "production_since", "released_at", "approved_at", "created_at", "updated_at", "performance_summary",
}
HISTORY_FIELDS = {
    "model_key", "model_version", "model_type", "status", "health_status", "health_label",
    "production_since", "released_at", "approved_at", "deprecated_at", "created_at", "updated_at",
    "performance_summary",
}
PERFORMANCE_FIELDS = {
    "model_key", "model_version", "status", "period", "sample_count", "success_ratio",
    "avg_return", "avg_drawdown", "risk_score", "health_status", "created_at", "items",
    "periods", "timeline", "summary",
}
HEALTH_FIELDS = {
    "model_key", "model_version", "status", "health_status", "health_label", "period",
    "sample_count", "baseline", "current", "success_ratio_change", "alerts", "warnings",
    "decline_ratio", "recent_success_ratio", "baseline_success_ratio",
    "checked_at", "updated_at", "auto_action", "does_not_replace_model",
}


def _settings(settings: Settings | None) -> Settings:
    return settings or Settings.load()


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


def _core_function(names: tuple[str, ...]) -> Callable[..., Any]:
    modules = (
        "paopao_radar.model_registry",
        "paopao_radar.model_approval",
        "paopao_radar.model_performance",
    )
    for module_name in modules:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        for name in names:
            function = getattr(module, name, None)
            if callable(function):
                return function
    raise RuntimeError(f"model registry core function is unavailable: {names[0]}")


def _unwrap(value: Any) -> tuple[bool, dict[str, Any], str, str]:
    if isinstance(value, (list, tuple)):
        items = list(value)
        return True, {"items": items, "count": len(items)}, "", "ok"
    if not isinstance(value, Mapping):
        return False, {}, "Model registry data is unavailable.", "model_registry_unavailable"
    item = dict(value)
    ok = bool(item.get("ok", True))
    message = str(item.get("message") or item.get("error") or "")
    code = str(item.get("code") or ("ok" if ok else "model_registry_unavailable"))
    data = item.get("data")
    if isinstance(data, Mapping):
        payload = dict(data)
        for key, nested in item.items():
            if key not in {"ok", "data", "message", "error", "code"} and key not in payload:
                payload[key] = nested
    else:
        payload = {
            key: nested for key, nested in item.items()
            if key not in {"ok", "message", "error", "code"}
        }
    return ok, payload, message, code


def _safe(value: Any, *, public: bool) -> Any:
    key_pattern = SENSITIVE_KEY_RE if public else PRIVATE_SENSITIVE_KEY_RE
    if isinstance(value, Mapping):
        return {
            str(key): _safe(nested, public=public)
            for key, nested in value.items()
            if not key_pattern.search(str(key))
        }
    if isinstance(value, (list, tuple)):
        return [_safe(nested, public=public) for nested in list(value)[:1000]]
    if isinstance(value, str):
        text = value[:2000]
        if "Traceback (most recent call last)" in text:
            return "<redacted-error>"
        return PATH_RE.sub("<redacted-path>", text)
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _project_mapping(value: Any, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {
        str(key): _safe(nested, public=True)
        for key, nested in value.items()
        if str(key) in allowed and not SENSITIVE_KEY_RE.search(str(key))
    }


def _public_project(section: str, data: dict[str, Any], limit: int) -> dict[str, Any]:
    if section == "current":
        raw = data.get("current") if isinstance(data.get("current"), Mapping) else data
        result = _project_mapping(raw, CURRENT_FIELDS)
    elif section == "history":
        raw_items = data.get("items") or data.get("history") or data.get("models") or []
        items = list(raw_items) if isinstance(raw_items, (list, tuple)) else []
        result = {
            "items": [_project_mapping(item, HISTORY_FIELDS) for item in items[:limit]],
            "count": min(int(data.get("count") or len(items)), limit),
        }
    elif section == "performance":
        model = data.get("model") if isinstance(data.get("model"), Mapping) else {}
        raw_snapshots = data.get("snapshots") or data.get("periods") or data.get("items") or []
        snapshots = list(raw_snapshots) if isinstance(raw_snapshots, (list, tuple)) else []
        result = _project_mapping(model, PERFORMANCE_FIELDS)
        result["periods"] = [
            _project_mapping(item, PERFORMANCE_FIELDS) for item in snapshots[:limit]
        ]
        for key in ("items", "periods", "timeline"):
            if isinstance(result.get(key), list):
                result[key] = [
                    _project_mapping(item, PERFORMANCE_FIELDS) for item in result[key][:limit]
                ]
    else:
        model = data.get("model") if isinstance(data.get("model"), Mapping) else {}
        health = data.get("health") if isinstance(data.get("health"), Mapping) else data
        result = _project_mapping(model, HEALTH_FIELDS)
        health_fields = _project_mapping(health, HEALTH_FIELDS)
        health_fields.pop("status", None)
        result.update(health_fields)
        result["health_status"] = str(health.get("status") or model.get("health_status") or "warning")
        result["health_label"] = str(health.get("label") or result["health_status"])
        raw_snapshots = data.get("snapshots") or []
        if isinstance(raw_snapshots, (list, tuple)):
            result["current"] = [
                _project_mapping(item, PERFORMANCE_FIELDS) for item in list(raw_snapshots)[:limit]
            ]
    result.setdefault("auto_action", False)
    result.setdefault("does_not_replace_model", True)
    return result


def _validate_scope(model: str, version: str = "") -> tuple[str, str]:
    model_key = str(model or "signal-decision").strip().lower()
    model_version = str(version or "").strip()
    if not MODEL_KEY_RE.fullmatch(model_key):
        raise ValueError("invalid model key")
    if model_version and not VERSION_RE.fullmatch(model_version):
        raise ValueError("invalid model version")
    return model_key, model_version


def _registry_exists(settings: Settings) -> bool:
    return Path(
        getattr(settings, "model_registry_db_path", None)
        or (Path(settings.data_dir) / "model_registry.db")
    ).exists()


def _load_section(
    section: str,
    *,
    model: str,
    version: str,
    limit: int,
    settings: Settings,
) -> tuple[bool, dict[str, Any], str, str]:
    if not _registry_exists(settings):
        return False, {}, "Model registry is not initialized.", "model_registry_not_initialized"
    aliases: dict[str, tuple[str, ...]] = {
        "current": ("get_current_model", "current_model", "model_current"),
        "history": ("list_model_history", "model_history", "list_models"),
        "performance": ("get_model_performance", "model_performance", "performance_timeline"),
        "health": ("get_model_health", "model_health", "evaluate_model_health"),
    }
    ttl = max(1, min(int(getattr(settings, "model_registry_cache_ttl_sec", 30) or 30), 300))
    cache_key = f"models:{section}:{settings.data_dir}:{model}:{version}:{limit}"

    def loader() -> tuple[bool, dict[str, Any], str, str]:
        try:
            result = _call_supported(
                _core_function(aliases[section]),
                settings=settings,
                model=model,
                model_key=model,
                version=version,
                model_version=version,
                limit=limit,
                dry_run=True,
            )
            return _unwrap(result)
        except Exception:
            return False, {}, "Model registry data is unavailable.", "model_registry_unavailable"

    return get_or_set(cache_key, ttl, loader)


def public_model_registry_payload(
    section: str,
    *,
    model: str = "signal-decision",
    version: str = "",
    limit: int = 100,
    settings: Settings | None = None,
) -> dict[str, Any]:
    normalized = str(section or "current").strip().lower()
    if normalized not in PUBLIC_SECTIONS:
        return api_error("Unknown model registry section.", code="invalid_model_registry_section")
    try:
        model_key, model_version = _validate_scope(model, version)
    except ValueError as exc:
        return api_error(str(exc), code="invalid_model_scope")
    bounded_limit = max(1, min(int(limit or 100), 500))
    ok, data, message, code = _load_section(
        normalized,
        model=model_key,
        version=model_version,
        limit=bounded_limit,
        settings=_settings(settings),
    )
    if not ok:
        return api_error("Model registry data is unavailable.", code=code)
    return api_ok(
        _public_project(normalized, data, bounded_limit),
        message=message or "Model registry data loaded.",
    )


def model_list_payload(
    *,
    model: str = "signal-decision",
    status: str = "",
    limit: int = 100,
    settings: Settings | None = None,
) -> dict[str, Any]:
    try:
        model_key, _ = _validate_scope(model)
        loaded = _settings(settings)
        if not _registry_exists(loaded):
            return api_error("Model registry is not initialized.", code="model_registry_not_initialized")
        result = _call_supported(
            _core_function(("list_models", "list_model_history", "model_history")),
            settings=loaded, model=model_key, model_key=model_key,
            status=str(status or ""), limit=max(1, min(int(limit or 100), 500)),
        )
        ok, data, message, code = _unwrap(result)
        return api_ok(_safe(data, public=False), message=message) if ok else api_error(message, code=code)
    except ValueError as exc:
        return api_error(str(exc), code="invalid_model_scope")
    except Exception:
        return api_error("Model registry data is unavailable.", code="model_registry_unavailable")


def model_detail_payload(
    *,
    model: str = "signal-decision",
    version: str = "",
    settings: Settings | None = None,
) -> dict[str, Any]:
    try:
        model_key, model_version = _validate_scope(model, version)
        loaded = _settings(settings)
        if not _registry_exists(loaded):
            return api_error("Model registry is not initialized.", code="model_registry_not_initialized")
        result = _call_supported(
            _core_function(("get_model", "model_detail", "get_model_detail")),
            settings=loaded, model=model_key, model_key=model_key,
            version=model_version, model_version=model_version,
        )
        ok, data, message, code = _unwrap(result)
        return api_ok(_safe(data, public=False), message=message) if ok else api_error(message, code=code)
    except ValueError as exc:
        return api_error(str(exc), code="invalid_model_scope")
    except Exception:
        return api_error("Model registry data is unavailable.", code="model_registry_unavailable")


def model_diff_payload(
    *,
    model: str = "signal-decision",
    version: str = "",
    settings: Settings | None = None,
) -> dict[str, Any]:
    try:
        model_key, model_version = _validate_scope(model, version)
        if not model_version:
            return api_error("version is required", code="model_version_required")
        loaded = _settings(settings)
        if not _registry_exists(loaded):
            return api_error("Model registry is not initialized.", code="model_registry_not_initialized")
        result = _call_supported(
            _core_function(("diff_models", "model_diff", "compare_models")),
            settings=loaded, model=model_key, model_key=model_key,
            version=model_version, candidate_version=model_version,
        )
        ok, data, message, code = _unwrap(result)
        return api_ok(_safe(data, public=False), message=message) if ok else api_error(message, code=code)
    except ValueError as exc:
        return api_error(str(exc), code="invalid_model_scope")
    except Exception:
        return api_error("Model registry data is unavailable.", code="model_registry_unavailable")


__all__ = [
    "model_detail_payload",
    "model_diff_payload",
    "model_list_payload",
    "public_model_registry_payload",
]
