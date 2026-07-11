from __future__ import annotations

import json
import math
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from .config import Settings
from .lifecycle_intelligence import NOT_ADVICE
from .lifecycle_store import normalize_lifecycle_symbol


SIMILARITY_MODEL_VERSION = "lifecycle-similarity-v1"
SIMILARITY_DISCLAIMER = "历史相似样本仅用于研究，不代表未来结果。"
LEVEL_RANK = {"unknown": 0, "15m": 1, "1h": 2, "4h": 3, "24h": 4}
RISK_EVENT_TYPES = {
    "risk_warning",
    "oi_price_divergence",
    "cvd_divergence",
    "funding_crowded",
    "short_term_weakening",
    "major_timeframe_weakening",
    "launch_failed",
}


def _number(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            result = json.loads(value)
            return result if isinstance(result, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
    return {}


def _flat(record: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for section in ("lifecycle", "intelligence", "replay", "outcome"):
        if isinstance(record.get(section), dict):
            result.update(record[section])
    result.update({key: value for key, value in record.items() if key not in {"lifecycle", "intelligence", "replay", "outcome"}})
    return result


def _path_parts(value: Any) -> tuple[str, ...]:
    text = str(value or "unknown").replace("->", "→")
    return tuple(part.strip() for part in text.split("→") if part.strip()) or ("unknown",)


def _bucket(value: Any, boundaries: tuple[float, ...]) -> int | None:
    number = _number(value)
    if number is None:
        return None
    for index, boundary in enumerate(boundaries):
        if number < boundary:
            return index
    return len(boundaries)


def _status_features(item: dict[str, Any]) -> tuple[bool | None, bool | None]:
    label = str(item.get("capital_confirmation_label") or "")
    known = {
        "现货与合约同步确认",
        "仅现货 CVD 确认",
        "仅合约 CVD 确认",
        "OI 确认但 CVD 未确认",
        "成交量确认但 OI 未确认",
        "无资金确认",
    }
    if label not in known:
        return None, None
    return (
        label in {"现货与合约同步确认", "仅现货 CVD 确认"},
        label in {"现货与合约同步确认", "仅合约 CVD 确认"},
    )


def lifecycle_similarity_features(record: dict[str, Any]) -> dict[str, Any]:
    item = _flat(record)
    factors = _json_dict(item.get("factors") or item.get("factors_json"))
    event_types = item.get("event_types")
    if isinstance(event_types, str):
        event_types = [part for part in event_types.split(",") if part]
    if not isinstance(event_types, (list, tuple, set)):
        event_types = []
    spot, futures = _status_features(item)
    funding = _number(item.get("latest_funding_rate"))
    if funding is None:
        funding = _number(_json_dict(factors.get("source_metrics")).get("funding_rate"))
    return {
        "first_signal_level": str(item.get("first_signal_level") or "unknown"),
        "highest_level": str(item.get("highest_level") or "unknown"),
        "upgrade_path": _path_parts(item.get("upgrade_path") or item.get("first_signal_level") or "unknown"),
        "lifecycle_score_bucket": _bucket(item.get("lifecycle_score"), (20, 40, 60, 80)),
        "risk_score_bucket": _bucket(item.get("risk_score"), (20, 40, 60, 80)),
        "intelligence_score_bucket": _bucket(item.get("intelligence_score"), (20, 40, 60, 80)),
        "price_bucket": _bucket(item.get("price_change_from_first_pct", item.get("final_return_pct")), (-5, 0, 3, 8, 15)),
        "oi_bucket": _bucket(item.get("oi_change_from_first_pct"), (-5, 0, 8, 20, 40)),
        "spot_confirmed": spot,
        "futures_confirmed": futures,
        "funding_status": "unknown" if funding is None else "crowded" if abs(funding) >= 0.0008 else "normal",
        "modules": frozenset(str(item.get("first_signal_module") or "unknown").split(",")),
        "risk_events": frozenset(str(value) for value in event_types if str(value) in RISK_EVENT_TYPES),
    }


def _set_similarity(left: Iterable[str], right: Iterable[str]) -> float:
    a, b = set(left), set(right)
    if not a and not b:
        return 1.0
    return len(a & b) / len(a | b) if (a | b) else 0.0


def _bucket_similarity(left: int | None, right: int | None) -> float:
    if left is None or right is None:
        return 0.0
    distance = abs(left - right)
    return 1.0 if distance == 0 else 0.5 if distance == 1 else 0.0


def similarity_score(current: dict[str, Any], candidate: dict[str, Any]) -> float:
    """Return the documented explainable 0–100 weighted distance score."""
    left = lifecycle_similarity_features(current)
    right = lifecycle_similarity_features(candidate)
    first = 15.0 if left["first_signal_level"] == right["first_signal_level"] else 0.0
    rank_distance = abs(LEVEL_RANK.get(left["highest_level"], 0) - LEVEL_RANK.get(right["highest_level"], 0))
    highest = 15.0 if rank_distance == 0 else 9.0 if rank_distance == 1 else 3.0 if rank_distance == 2 else 0.0
    path = 20.0 * _set_similarity(left["upgrade_path"], right["upgrade_path"])
    if left["upgrade_path"] == right["upgrade_path"]:
        path = 20.0
    oi = 10.0 * _bucket_similarity(left["oi_bucket"], right["oi_bucket"])
    spot = 10.0 if left["spot_confirmed"] is not None and left["spot_confirmed"] == right["spot_confirmed"] else 0.0
    futures = 8.0 if left["futures_confirmed"] is not None and left["futures_confirmed"] == right["futures_confirmed"] else 0.0
    funding = 7.0 if left["funding_status"] != "unknown" and left["funding_status"] == right["funding_status"] else 0.0
    price = 10.0 * _bucket_similarity(left["price_bucket"], right["price_bucket"])
    risk_pattern = (
        5.0 * _set_similarity(left["risk_events"], right["risk_events"])
        if left["risk_events"] or right["risk_events"] else 0.0
    )
    return round(max(0.0, min(100.0, first + highest + path + oi + spot + futures + funding + price + risk_pattern)), 2)


def _eligible_historical(item: dict[str, Any]) -> bool:
    active = int(item.get("is_active", 1) or 0) == 1
    outcome_status = str(item.get("outcome_status") or "")
    try:
        outcome_count = int(item.get("outcome_count") or 0)
    except (TypeError, ValueError):
        outcome_count = 0
    has_outcome = outcome_count > 0 or outcome_status in {"linked", "success"}
    return (not active) or has_outcome


def _avg(values: Iterable[Any]) -> float | None:
    numbers = [value for item in values if (value := _number(item)) is not None]
    return round(sum(numbers) / len(numbers), 4) if numbers else None


def find_similar_lifecycles(
    current: dict[str, Any],
    candidates: Iterable[dict[str, Any]],
    *,
    limit: int = 10,
    min_samples: int = 5,
) -> dict[str, Any]:
    current_item = _flat(dict(current))
    current_id = int(current_item.get("lifecycle_id", current_item.get("id", 0)) or 0)
    eligible: list[dict[str, Any]] = []
    for raw in candidates:
        item = _flat(dict(raw))
        item_id = int(item.get("lifecycle_id", item.get("id", 0)) or 0)
        if (current_id and item_id == current_id) or not _eligible_historical(item):
            continue
        eligible.append(item)
    if len(eligible) < max(1, int(min_samples)):
        return {
            "status": "insufficient_samples",
            "model_version": SIMILARITY_MODEL_VERSION,
            "similar_count": len(eligible),
            "required_samples": max(1, int(min_samples)),
            "avg_final_return_pct": None,
            "positive_ratio": None,
            "avg_max_drawdown_pct": None,
            "strong_success_ratio": None,
            "samples": [],
            "message": "当前相似样本不足，暂不生成统计结论。",
            "disclaimer": SIMILARITY_DISCLAIMER,
            "not_advice": NOT_ADVICE,
        }
    scored = sorted(
        ((similarity_score(current_item, item), item) for item in eligible),
        key=lambda pair: (-pair[0], -int(pair[1].get("lifecycle_id", pair[1].get("id", 0)) or 0)),
    )[: max(1, min(int(limit or 10), 50))]
    selected = [item for _, item in scored]
    with_returns = [item for item in selected if _number(item.get("final_return_pct")) is not None]
    samples = [
        {
            "lifecycle_id": int(item.get("lifecycle_id", item.get("id", 0)) or 0),
            "symbol": str(item.get("symbol") or ""),
            "similarity_score": score,
            "first_signal_level": str(item.get("first_signal_level") or "unknown"),
            "highest_level": str(item.get("highest_level") or "unknown"),
            "upgrade_path": str(item.get("upgrade_path") or item.get("first_signal_level") or "unknown"),
            "quality_label": str(item.get("quality_label") or ""),
            "result_label": str(item.get("result_label") or "insufficient_data"),
            "final_return_pct": _number(item.get("final_return_pct")),
            "max_drawdown_pct": _number(item.get("max_drawdown_pct")),
        }
        for score, item in scored
    ]
    return {
        "status": "ok",
        "model_version": SIMILARITY_MODEL_VERSION,
        "similar_count": len(eligible),
        "returned_count": len(samples),
        "avg_final_return_pct": _avg(item.get("final_return_pct") for item in with_returns),
        "positive_ratio": round(sum((_number(item.get("final_return_pct")) or 0.0) > 0 for item in with_returns) / len(with_returns), 4) if with_returns else None,
        "avg_max_drawdown_pct": _avg(item.get("max_drawdown_pct") for item in selected),
        "strong_success_ratio": round(sum(str(item.get("result_label") or "") == "strong_success" for item in selected) / len(selected), 4) if selected else None,
        "samples": samples,
        "disclaimer": SIMILARITY_DISCLAIMER,
        "not_advice": NOT_ADVICE,
    }


def _load_similarity_rows(store: Any, *, dry_run: bool = False) -> list[dict[str, Any]]:
    if dry_run:
        db_path = Path(getattr(store, "db_path", ""))
        if not db_path.exists():
            return []
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        tables = {
            str(row[0])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if not {"signal_lifecycles", "lifecycle_intelligence", "lifecycle_replays", "lifecycle_events"}.issubset(tables):
            conn.close()
            return []
        close_when_done = True
    else:
        connection_context = store.connect()
        conn = connection_context.__enter__()
        close_when_done = False
    try:
        conn.row_factory = sqlite3.Row
        rows = [
            dict(row)
            for row in conn.execute(
                """
                SELECT l.id AS lifecycle_id, l.symbol, l.first_signal_level,
                       l.first_signal_module, l.highest_level, l.is_active,
                       l.lifecycle_score, l.risk_score, l.price_change_from_first_pct,
                       l.oi_change_from_first_pct, l.latest_funding_rate,
                       i.intelligence_score, i.quality_label,
                       i.capital_confirmation_label, i.factors_json,
                       r.upgrade_path, r.final_return_pct, r.max_drawdown_pct,
                       r.result_label, r.outcome_status, r.outcome_count,
                       GROUP_CONCAT(DISTINCT e.event_type) AS event_types
                FROM signal_lifecycles AS l
                LEFT JOIN lifecycle_intelligence AS i ON i.lifecycle_id = l.id
                LEFT JOIN lifecycle_replays AS r ON r.lifecycle_id = l.id
                LEFT JOIN lifecycle_events AS e ON e.lifecycle_id = l.id
                GROUP BY l.id
                ORDER BY l.updated_at DESC, l.id DESC
                """
            ).fetchall()
        ]
        return rows
    finally:
        if close_when_done:
            conn.close()
        else:
            connection_context.__exit__(None, None, None)


def find_similar_for_symbol(
    *,
    settings: Settings | None = None,
    store: Any | None = None,
    symbol: str,
    limit: int = 10,
    min_samples: int | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    started = time.perf_counter()
    loaded = settings or Settings.load()
    if store is None:
        from .lifecycle_intelligence_store import IntelligenceStore

        store = IntelligenceStore(loaded)
        if not dry_run:
            store.ensure_schema()
    rows = _load_similarity_rows(store, dry_run=dry_run)
    normalized = normalize_lifecycle_symbol(symbol)
    if not normalized:
        return {
            "ok": False,
            "status": "invalid_symbol",
            "symbol": str(symbol or ""),
            "model_version": SIMILARITY_MODEL_VERSION,
            "processed": 0,
            "skipped": 0,
            "failed": 1,
            "dry_run": bool(dry_run),
            "duration_sec": round(time.perf_counter() - started, 4),
            "message": "请提供币种，例如 BTCUSDT。",
            "disclaimer": SIMILARITY_DISCLAIMER,
        }
    current = next((item for item in rows if str(item.get("symbol") or "").upper() == normalized), None)
    if current is None:
        return {
            "ok": True,
            "status": "insufficient_samples",
            "symbol": normalized,
            "model_version": SIMILARITY_MODEL_VERSION,
            "similar_count": 0,
            "required_samples": int(min_samples if min_samples is not None else getattr(loaded, "lifecycle_similarity_min_samples", 5) or 5),
            "avg_final_return_pct": None,
            "positive_ratio": None,
            "avg_max_drawdown_pct": None,
            "strong_success_ratio": None,
            "samples": [],
            "processed": 0,
            "skipped": 1,
            "failed": 0,
            "dry_run": bool(dry_run),
            "duration_sec": round(time.perf_counter() - started, 4),
            "message": "当前相似样本不足，暂不生成统计结论。",
            "disclaimer": SIMILARITY_DISCLAIMER,
        }
    required = int(min_samples if min_samples is not None else getattr(loaded, "lifecycle_similarity_min_samples", 5) or 5)
    result = find_similar_lifecycles(current, rows, limit=limit, min_samples=required)
    return {
        "ok": True,
        "symbol": normalized,
        **result,
        "processed": 1,
        "skipped": 0,
        "failed": 0,
        "dry_run": bool(dry_run),
        "duration_sec": round(time.perf_counter() - started, 4),
    }


def find_similar(**kwargs: Any) -> dict[str, Any]:
    return find_similar_for_symbol(**kwargs)


__all__ = [
    "SIMILARITY_DISCLAIMER",
    "SIMILARITY_MODEL_VERSION",
    "find_similar",
    "find_similar_for_symbol",
    "find_similar_lifecycles",
    "lifecycle_similarity_features",
    "similarity_score",
]
