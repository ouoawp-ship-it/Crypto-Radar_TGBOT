from __future__ import annotations

from typing import Any

from ..backtest_dashboard import (
    build_backtest_detail_payload,
    build_backtest_matrix_payload,
    build_backtest_payload,
)
from ..config import Settings
from .api_core import api_ok, clamp, redact_api_payload
from .public import _strip_forbidden


def _window(value: Any) -> int:
    return int(clamp(value or 2592000, 3600, 31536000))


def _limit(value: Any) -> int:
    return int(clamp(value or 20, 1, 100))


def _safe_public(payload: dict[str, Any]) -> dict[str, Any]:
    return _strip_forbidden(redact_api_payload(payload))


def backtest_decision_payload(
    *,
    horizon: str = "all",
    window_sec: int = 2592000,
    module: str = "",
    decision: str = "",
    risk_level: str = "",
    min_samples: int = 0,
    settings: Settings | None = None,
    public: bool = False,
) -> dict[str, Any]:
    data = build_backtest_payload(
        settings=settings,
        horizon=horizon,
        window_sec=_window(window_sec),
        module=module,
        decision=decision,
        risk_level=risk_level,
        min_samples=int(min_samples or 0),
    )
    if public:
        data = _safe_public(data)
    payload = api_ok(data, message="已读取决策回测看板")
    payload.update({
        "summary": data.get("summary", {}),
        "decision_groups": data.get("decision_groups", []),
        "model_diagnosis": data.get("model_diagnosis", {}),
        "filters": data.get("filters", {}),
        "coverage": data.get("coverage", {}),
    })
    return _safe_public(payload) if public else redact_api_payload(payload)


def backtest_matrix_payload(
    *,
    window_sec: int = 2592000,
    module: str = "",
    risk_level: str = "",
    settings: Settings | None = None,
    public: bool = False,
) -> dict[str, Any]:
    data = build_backtest_matrix_payload(
        settings=settings,
        window_sec=_window(window_sec),
        module=module,
        risk_level=risk_level,
    )
    if public:
        data = _safe_public(data)
    payload = api_ok(data, message="已读取决策回测矩阵")
    payload.update({
        "items": data.get("items", []),
        "horizons": data.get("horizons", []),
        "filters": data.get("filters", {}),
    })
    return _safe_public(payload) if public else redact_api_payload(payload)


def backtest_detail_payload(
    *,
    decision: str = "",
    horizon: str = "all",
    limit: int = 20,
    window_sec: int = 2592000,
    settings: Settings | None = None,
    public: bool = False,
) -> dict[str, Any]:
    data = build_backtest_detail_payload(
        settings=settings,
        decision=decision,
        horizon=horizon,
        limit=_limit(limit),
        window_sec=_window(window_sec),
        public=public,
    )
    if public:
        data = _safe_public(data)
    payload = api_ok(data, message="已读取决策回测样本")
    payload.update({
        "items": data.get("items", []),
        "count": data.get("count", 0),
        "filters": data.get("filters", {}),
    })
    return _safe_public(payload) if public else redact_api_payload(payload)


def public_backtest_decision_payload(**kwargs: Any) -> dict[str, Any]:
    kwargs["public"] = True
    return backtest_decision_payload(**kwargs)


def public_backtest_matrix_payload(**kwargs: Any) -> dict[str, Any]:
    kwargs["public"] = True
    return backtest_matrix_payload(**kwargs)


def public_backtest_detail_payload(**kwargs: Any) -> dict[str, Any]:
    kwargs["public"] = True
    return backtest_detail_payload(**kwargs)
