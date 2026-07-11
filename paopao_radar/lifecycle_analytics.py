from __future__ import annotations

import json
import math
import sqlite3
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .atomic_json import atomic_write_text
from .config import BASE_DIR, Settings
from .lifecycle_intelligence import INTELLIGENCE_MODEL_VERSION, NOT_ADVICE


ANALYTICS_MODEL_VERSION = INTELLIGENCE_MODEL_VERSION
DEFAULT_ANALYTICS_CACHE_KEY = f"lifecycle:analytics:{ANALYTICS_MODEL_VERSION}"
DEFAULT_REPORT_PATH = BASE_DIR / "docs" / "generated" / "lifecycle_analytics_latest.json"
DEFAULT_MARKDOWN_PATH = BASE_DIR / "docs" / "generated" / "lifecycle_analytics_latest.md"
SENSITIVE_KEYS = {
    "chat_id",
    "topic_id",
    "message_id",
    "dedup_key",
    "payload_json",
    "text_html",
    "token",
    "secret",
    "cookie",
    "authorization",
    "api_key",
    "server_path",
    "database_path",
    "db_path",
    "config",
    "raw_telegram",
    "job_payload",
    "exception",
    "traceback",
}
SUCCESS_RESULTS = {"strong_success", "success", "partial_success"}
FAILED_RESULTS = {"failed"}
LEVEL_RANK = {"unknown": 0, "15m": 1, "1h": 2, "4h": 3, "24h": 4}
CAPITAL_GROUPS = (
    "现货与合约同步确认",
    "仅现货 CVD 确认",
    "仅合约 CVD 确认",
    "OI 确认但 CVD 未确认",
    "成交量确认但 OI 未确认",
    "无资金确认",
)


def _number(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _average(values: Iterable[Any], digits: int = 4) -> float | None:
    numbers = [value for item in values if (value := _number(item)) is not None]
    return round(sum(numbers) / len(numbers), digits) if numbers else None


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator > 0 else None


def _timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            result = json.loads(value)
            return result if isinstance(result, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return {}


def _flat(record: dict[str, Any]) -> dict[str, Any]:
    flattened: dict[str, Any] = {}
    for section in ("lifecycle", "intelligence", "replay", "outcome"):
        value = record.get(section)
        if isinstance(value, dict):
            flattened.update(value)
    flattened.update({key: value for key, value in record.items() if key not in {"lifecycle", "intelligence", "replay", "outcome"}})
    return flattened


def _has_outcome_link(item: dict[str, Any]) -> bool:
    outcome_status = str(item.get("outcome_status") or "")
    try:
        outcome_count = int(item.get("outcome_count") or 0)
    except (TypeError, ValueError):
        outcome_count = 0
    return outcome_count > 0 or outcome_status in {"linked", "success"}


def _has_result(item: dict[str, Any]) -> bool:
    return _has_outcome_link(item) and _number(item.get("final_return_pct")) is not None


def _success(item: dict[str, Any]) -> bool:
    result = str(item.get("result_label") or "")
    if result in SUCCESS_RESULTS:
        return True
    return result not in FAILED_RESULTS and (_number(item.get("final_return_pct")) or 0.0) > 0


def _failed(item: dict[str, Any]) -> bool:
    result = str(item.get("result_label") or "")
    return result in FAILED_RESULTS or (result == "" and (_number(item.get("final_return_pct")) or 0.0) <= -5)


def _group_metrics(items: list[dict[str, Any]]) -> dict[str, Any]:
    resolved = [item for item in items if _has_result(item)]
    successes = sum(1 for item in resolved if _success(item))
    failures = sum(1 for item in resolved if _failed(item))
    return {
        "sample_count": len(items),
        "outcome_linked_count": sum(_has_outcome_link(item) for item in items),
        "outcome_sample_count": len(resolved),
        "success_rate": _ratio(successes, len(resolved)),
        "failure_rate": _ratio(failures, len(resolved)),
        "avg_final_return_pct": _average(item.get("final_return_pct") for item in resolved),
        "avg_max_gain_pct": _average(item.get("max_price_gain_pct", item.get("max_gain_pct")) for item in resolved),
        "avg_max_drawdown_pct": _average(item.get("max_drawdown_pct") for item in resolved),
        "avg_duration_sec": _average((item.get("duration_sec") for item in items), digits=1),
        "avg_intelligence_score": _average((item.get("intelligence_score") for item in items), digits=2),
    }


def first_signal_level_statistics(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [_flat(dict(item)) for item in records]
    result: list[dict[str, Any]] = []
    for level in ("15m", "1h", "4h", "24h", "unknown"):
        group = [item for item in rows if str(item.get("first_signal_level") or "unknown") == level]
        metrics = _group_metrics(group)
        count = len(group)
        result.append({
            "first_signal_level": level,
            **metrics,
            "upgrade_1h_ratio": _ratio(sum(LEVEL_RANK.get(str(item.get("highest_level") or "unknown"), 0) >= 2 for item in group), count),
            "upgrade_4h_ratio": _ratio(sum(LEVEL_RANK.get(str(item.get("highest_level") or "unknown"), 0) >= 3 for item in group), count),
            "upgrade_24h_ratio": _ratio(sum(LEVEL_RANK.get(str(item.get("highest_level") or "unknown"), 0) >= 4 for item in group), count),
        })
    return result


def upgrade_path_statistics(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        item = _flat(dict(record))
        path = str(item.get("upgrade_path") or item.get("first_signal_level") or "unknown")
        groups[path].append(item)
    output: list[dict[str, Any]] = []
    for path, items in groups.items():
        metrics = _group_metrics(items)
        output.append({
            "upgrade_path": path,
            **metrics,
            "avg_upgrade_time_sec": _average(
                (
                    max(values)
                    for item in items
                    if (values := [
                        value
                        for value in (
                            _number(item.get("time_to_1h_sec")),
                            _number(item.get("time_to_4h_sec")),
                            _number(item.get("time_to_24h_sec")),
                        )
                        if value is not None
                    ])
                ),
                digits=1,
            ),
            "avg_time_to_1h_sec": _average((item.get("time_to_1h_sec") for item in items), digits=1),
            "avg_time_to_4h_sec": _average((item.get("time_to_4h_sec") for item in items), digits=1),
            "avg_time_to_24h_sec": _average((item.get("time_to_24h_sec") for item in items), digits=1),
            "risk_warning_rate": _ratio(sum(int(item.get("risk_event_count") or 0) > 0 for item in items), len(items)),
        })
    return sorted(output, key=lambda item: (-int(item["sample_count"]), str(item["upgrade_path"])))


def module_statistics(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        item = _flat(dict(record))
        groups[str(item.get("first_signal_module") or "unknown")].append(item)
    output = []
    for module, items in groups.items():
        metrics = _group_metrics(items)
        output.append({
            "module": module,
            **metrics,
            "upgrade_success_rate": _ratio(
                sum(LEVEL_RANK.get(str(item.get("highest_level") or "unknown"), 0) > LEVEL_RANK.get(str(item.get("first_signal_level") or "unknown"), 0) for item in items),
                len(items),
            ),
            "final_success_rate": metrics["success_rate"],
        })
    return sorted(output, key=lambda item: (-int(item["sample_count"]), str(item["module"])))


def capital_confirmation_statistics(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [_flat(dict(item)) for item in records]
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in rows:
        label = str(item.get("capital_confirmation_label") or "无资金确认")
        groups[label if label in CAPITAL_GROUPS else "无资金确认"].append(item)
    labels = [label for label in CAPITAL_GROUPS if groups.get(label)]
    return [
        {"capital_confirmation": label, **_group_metrics(groups[label])}
        for label in labels
    ]


def factor_effect_statistics(records: Iterable[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Expose CVD, OI, and funding effects explicitly in generated reports."""
    rows = [_flat(dict(item)) for item in records]
    cvd_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    oi_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    funding_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in rows:
        capital = str(item.get("capital_confirmation_label") or "无资金确认")
        if capital == "现货与合约同步确认":
            cvd_groups["spot_futures_sync"].append(item)
        elif capital == "仅现货 CVD 确认":
            cvd_groups["spot_only"].append(item)
        elif capital == "仅合约 CVD 确认":
            cvd_groups["futures_only"].append(item)
        else:
            cvd_groups["cvd_unconfirmed"].append(item)
        oi_change = _number(item.get("oi_change_from_first_pct"))
        oi_confirmed = bool((oi_change is not None and oi_change >= 8) or capital == "OI 确认但 CVD 未确认")
        oi_groups["confirmed" if oi_confirmed else "unconfirmed"].append(item)
        funding = _number(item.get("latest_funding_rate"))
        if funding is not None:
            funding_groups["overheated" if abs(funding) >= 0.0008 else "healthy"].append(item)

    cvd_labels = {
        "spot_futures_sync": "Spot/Futures CVD 同步确认",
        "spot_only": "仅 Spot CVD 确认",
        "futures_only": "仅 Futures CVD 确认",
        "cvd_unconfirmed": "CVD 未确认",
    }
    oi_labels = {"confirmed": "OI 确认", "unconfirmed": "OI 未确认"}
    funding_labels = {"overheated": "Funding 过热", "healthy": "Funding 健康"}
    return {
        "spot_futures_cvd": [
            {"group": key, "label": cvd_labels[key], **_group_metrics(items)}
            for key, items in cvd_groups.items()
        ],
        "oi_confirmation": [
            {"group": key, "label": oi_labels[key], **_group_metrics(items)}
            for key, items in oi_groups.items()
        ],
        "funding_overheat": [
            {"group": key, "label": funding_labels[key], **_group_metrics(items)}
            for key, items in funding_groups.items()
        ],
    }


def risk_warning_performance(
    events: Iterable[dict[str, Any]],
    frames: Iterable[dict[str, Any]],
) -> dict[str, Any]:
    frame_groups: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for frame in frames:
        lifecycle_id = int(frame.get("lifecycle_id") or 0)
        if lifecycle_id:
            frame_groups[lifecycle_id].append(dict(frame))
    for items in frame_groups.values():
        items.sort(key=lambda item: (_timestamp(item.get("event_time")) or 0, int(item.get("frame_index") or 0)))

    warnings: list[dict[str, Any]] = []
    accepted = {"risk_warning", "cooling", "short_term_weakening", "major_timeframe_weakening", "launch_failed"}
    for event in events:
        if str(event.get("event_type") or "") not in accepted:
            continue
        lifecycle_id = int(event.get("lifecycle_id") or 0)
        event_ts = _timestamp(event.get("event_time"))
        start_price = _number(event.get("price"))
        if not lifecycle_id or event_ts is None or start_price in {None, 0.0}:
            continue
        later = [
            item for item in frame_groups.get(lifecycle_id, [])
            if (_timestamp(item.get("event_time")) or 0) > event_ts and _number(item.get("price")) is not None
        ]
        horizon_returns: dict[str, float | None] = {}
        for label, seconds in (("1h", 3600), ("4h", 14400), ("24h", 86400)):
            target_time = event_ts + seconds
            tolerance = max(900, int(seconds * 0.25))
            target = next(
                (
                    item for item in later
                    if target_time <= (_timestamp(item.get("event_time")) or 0) <= target_time + tolerance
                ),
                None,
            )
            price = _number((target or {}).get("price"))
            horizon_returns[label] = round((price / start_price - 1.0) * 100.0, 4) if price is not None else None
        drawdowns = [
            ((price / start_price) - 1.0) * 100.0
            for item in later
            if (price := _number(item.get("price"))) is not None
        ]
        worst = min(drawdowns) if drawdowns else None
        worst_frame = None
        if worst is not None:
            worst_frame = min(later, key=lambda item: ((_number(item.get("price")) or start_price) / start_price - 1.0) * 100.0)
        warnings.append({
            "event_type": str(event.get("event_type") or ""),
            "returns": horizon_returns,
            "max_drawdown_pct": round(worst, 4) if worst is not None else None,
            "lead_time_sec": max(0, int((_timestamp((worst_frame or {}).get("event_time")) or event_ts) - event_ts)) if worst_frame else None,
        })
    observed = [item for item in warnings if item["max_drawdown_pct"] is not None]
    return {
        "sample_count": len(warnings),
        "observed_sample_count": len(observed),
        "avg_return_1h_pct": _average(item["returns"]["1h"] for item in warnings),
        "avg_return_4h_pct": _average(item["returns"]["4h"] for item in warnings),
        "avg_return_24h_pct": _average(item["returns"]["24h"] for item in warnings),
        "avg_max_drawdown_pct": _average(item["max_drawdown_pct"] for item in observed),
        "hit_rate": _ratio(sum((_number(item["max_drawdown_pct"]) or 0.0) < 0 for item in observed), len(observed)),
        "avg_lead_time_sec": _average((item["lead_time_sec"] for item in observed), digits=1),
    }


def build_lifecycle_analytics(
    records: Iterable[dict[str, Any]],
    events: Iterable[dict[str, Any]] | None = None,
    frames: Iterable[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    rows = [_flat(dict(item)) for item in records]
    active_count = sum(int(item.get("is_active", 0) or 0) == 1 for item in rows)
    linked = [item for item in rows if _has_outcome_link(item)]
    resolved = [item for item in rows if _has_result(item)]
    warnings: list[str] = []
    if len(rows) < 5:
        warnings.append("生命周期样本仍在积累，统计结果仅供研究。")
    if len(linked) < 5:
        warnings.append("具备 Outcome 关联的样本不足，关联统计仍在积累。")
    if len(resolved) < 5:
        warnings.append("具备 Outcome 的样本不足，不生成稳定成功率结论。")
    if not any(_number(item.get("latest_funding_rate")) is not None for item in rows):
        warnings.append("Funding 历史样本不足，暂不评价过热影响。")
    result_distribution: dict[str, int] = defaultdict(int)
    for item in rows:
        result_distribution[str(item.get("result_label") or "insufficient_data")] += 1
    return {
        "model_version": ANALYTICS_MODEL_VERSION,
        "intelligence_model_version": INTELLIGENCE_MODEL_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_lifecycle_count": len(rows),
            "active_count": active_count,
            "closed_count": len(rows) - active_count,
            "outcome_linked_count": len(linked),
            "resolved_outcome_count": len(resolved),
            "avg_final_return_pct": _average(item.get("final_return_pct") for item in resolved),
            "avg_max_drawdown_pct": _average(item.get("max_drawdown_pct") for item in resolved),
            "success_count": sum(_success(item) for item in resolved),
            "failed_count": sum(_failed(item) for item in resolved),
        },
        "first_level": first_signal_level_statistics(rows),
        "upgrade_path": upgrade_path_statistics(rows),
        "module": module_statistics(rows),
        "capital_confirmation": capital_confirmation_statistics(rows),
        "result_distribution": dict(sorted(result_distribution.items())),
        "factor_effects": factor_effect_statistics(rows),
        "risk_warning": risk_warning_performance(events or [], frames or []),
        "model_data_warnings": warnings,
        "not_advice": NOT_ADVICE,
    }


def _sanitize(value: Any, *, depth: int = 0) -> Any:
    if depth > 12:
        return None
    if isinstance(value, dict):
        return {
            str(key): _sanitize(item, depth=depth + 1)
            for key, item in value.items()
            if str(key).lower() not in SENSITIVE_KEYS
            and not any(token in str(key).lower() for token in ("token", "secret", "cookie", "authorization"))
        }
    if isinstance(value, (list, tuple)):
        return [_sanitize(item, depth=depth + 1) for item in value[:1000]]
    if isinstance(value, str):
        text = value.replace(str(BASE_DIR), "[project]").replace("/home/ubuntu", "[server]")
        return text[:4000]
    return value


def _cache_get(store: Any, key: str) -> dict[str, Any] | None:
    for name in ("get_analytics_cache", "cache_get"):
        method = getattr(store, name, None)
        if method is None:
            continue
        try:
            value = method(key)
        except TypeError:
            value = method(cache_key=key)
        if isinstance(value, dict):
            data = value.get("data", value.get("data_json", value))
            if isinstance(data, str):
                try:
                    data = json.loads(data)
                except (TypeError, ValueError, json.JSONDecodeError):
                    return None
            return data if isinstance(data, dict) else None
    return None


def _cache_set(store: Any, key: str, data: dict[str, Any], ttl_sec: int) -> None:
    for name in ("put_analytics_cache", "cache_set"):
        method = getattr(store, name, None)
        if method is None:
            continue
        attempts = (
            lambda: method(key, data, ttl_sec=ttl_sec),
            lambda: method(cache_key=key, data=data, ttl_sec=ttl_sec),
            lambda: method(key, data, ttl_sec),
        )
        for attempt in attempts:
            try:
                attempt()
                return
            except TypeError:
                continue
        return


def _load_records(
    store: Any,
    *,
    dry_run: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    owned: sqlite3.Connection | None = None
    context: Any
    if dry_run:
        db_path = Path(getattr(store, "db_path", ""))
        if not db_path.exists():
            return [], [], []
        owned = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        owned.row_factory = sqlite3.Row
        tables = {
            str(row[0])
            for row in owned.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        required = {
            "signal_lifecycles", "lifecycle_events", "lifecycle_intelligence",
            "lifecycle_replays", "lifecycle_replay_frames",
        }
        if not required.issubset(tables):
            owned.close()
            return [], [], []

        class _ReadOnlyContext:
            def __enter__(self) -> sqlite3.Connection:
                assert owned is not None
                return owned

            def __exit__(self, *_args: Any) -> None:
                assert owned is not None
                owned.close()

        context = _ReadOnlyContext()
    else:
        context = store.connect()
    with context as conn:
        conn.row_factory = sqlite3.Row
        records = [
            dict(row)
            for row in conn.execute(
                """
                SELECT l.id AS lifecycle_id, l.symbol, l.first_signal_level,
                       l.first_signal_module, l.highest_level, l.current_state, l.is_active,
                       l.lifecycle_score, l.risk_score, l.price_change_from_first_pct,
                       l.oi_change_from_first_pct, l.latest_funding_rate,
                       l.created_at, l.updated_at, l.closed_at,
                       i.intelligence_score, i.quality_label, i.stage, i.stage_label,
                       i.capital_confirmation_label,
                       r.upgrade_path, r.duration_sec, r.time_to_1h_sec, r.time_to_4h_sec,
                       r.time_to_24h_sec, r.max_price_gain_pct, r.max_drawdown_pct,
                       r.final_return_pct, r.result_label, r.outcome_status,
                       r.outcome_count, r.summary_json
                FROM signal_lifecycles AS l
                LEFT JOIN lifecycle_intelligence AS i ON i.lifecycle_id = l.id
                LEFT JOIN lifecycle_replays AS r ON r.lifecycle_id = l.id
                ORDER BY l.id
                """
            ).fetchall()
        ]
        for item in records:
            summary = _json_dict(item.pop("summary_json", None))
            for key in ("event_count", "risk_event_count", "confirmation_count", "cooling_count"):
                if key in summary and key not in item:
                    item[key] = summary[key]
        events = [
            dict(row)
            for row in conn.execute(
                """
                SELECT id, lifecycle_id, event_time, event_type, price
                FROM lifecycle_events
                WHERE event_type IN (
                    'risk_warning', 'cooling', 'short_term_weakening',
                    'major_timeframe_weakening', 'launch_failed'
                )
                ORDER BY lifecycle_id, event_time, id
                """
            ).fetchall()
        ]
        frames = [
            dict(row)
            for row in conn.execute(
                """
                SELECT lifecycle_id, frame_index, event_time, price
                FROM lifecycle_replay_frames
                ORDER BY lifecycle_id, event_time, frame_index
                """
            ).fetchall()
        ]
    return records, events, frames


def _markdown_report(data: dict[str, Any]) -> str:
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    lines = [
        "# Lifecycle Analytics Latest",
        "",
        f"- 模型版本：{data.get('model_version', ANALYTICS_MODEL_VERSION)}",
        f"- 总生命周期数：{summary.get('total_lifecycle_count', 0)}",
        f"- 活跃数：{summary.get('active_count', 0)}",
        f"- 关闭数：{summary.get('closed_count', 0)}",
        f"- Outcome 关联数：{summary.get('outcome_linked_count', 0)}",
        f"- 平均最终收益：{summary.get('avg_final_return_pct') if summary.get('avg_final_return_pct') is not None else '数据不足'}",
        f"- 平均最大回撤：{summary.get('avg_max_drawdown_pct') if summary.get('avg_max_drawdown_pct') is not None else '数据不足'}",
        "",
        NOT_ADVICE,
        "",
    ]
    return "\n".join(lines)


def generate_lifecycle_analytics(
    *,
    settings: Settings | None = None,
    store: Any | None = None,
    dry_run: bool = False,
    force: bool = False,
    ttl_sec: int | None = None,
    report_path: str | Path | None = None,
    markdown_path: str | Path | None = None,
    write_markdown: bool = False,
    records: list[dict[str, Any]] | None = None,
    events: list[dict[str, Any]] | None = None,
    frames: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    loaded = settings or Settings.load()
    cache_ttl_sec = max(1, int(ttl_sec or getattr(loaded, "lifecycle_analytics_interval_sec", 21600) or 21600))
    if store is None:
        from .lifecycle_intelligence_store import IntelligenceStore

        store = IntelligenceStore(loaded)
        if not dry_run:
            store.ensure_schema()

    if not force and not dry_run and records is None:
        cached = _cache_get(store, DEFAULT_ANALYTICS_CACHE_KEY)
        if cached is not None:
            return {
                "ok": True,
                "cache_hit": True,
                "processed": 0,
                "skipped": 1,
                "failed": 0,
                "data": cached,
                "duration_sec": round(time.perf_counter() - started, 4),
            }
    if records is None:
        records, loaded_events, loaded_frames = _load_records(store, dry_run=dry_run)
        events = events if events is not None else loaded_events
        frames = frames if frames is not None else loaded_frames
    data = _sanitize(build_lifecycle_analytics(records, events, frames))
    if not dry_run:
        _cache_set(store, DEFAULT_ANALYTICS_CACHE_KEY, data, cache_ttl_sec)
        target = Path(report_path) if report_path is not None else DEFAULT_REPORT_PATH
        atomic_write_text(target, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        if write_markdown or markdown_path is not None:
            markdown_target = Path(markdown_path) if markdown_path is not None else DEFAULT_MARKDOWN_PATH
            atomic_write_text(markdown_target, _markdown_report(data))
    return {
        "ok": True,
        "cache_hit": False,
        "dry_run": bool(dry_run),
        "processed": 1,
        "skipped": 0,
        "failed": 0,
        "data": data,
        "duration_sec": round(time.perf_counter() - started, 4),
    }


def generate_analytics(**kwargs: Any) -> dict[str, Any]:
    return generate_lifecycle_analytics(**kwargs)


def analytics_payload(result: dict[str, Any], dimension: str = "") -> dict[str, Any]:
    data = result.get("data") if isinstance(result.get("data"), dict) else result
    selected = data.get(dimension) if dimension else data
    return {
        "ok": bool(result.get("ok", True)),
        "data": selected,
        "cache_hit": bool(result.get("cache_hit", False)),
        "model_version": ANALYTICS_MODEL_VERSION,
        "not_advice": NOT_ADVICE,
    }


# Short aliases keep API adapters and research notebooks uncomplicated.
analyze_first_signal_levels = first_signal_level_statistics
analyze_upgrade_paths = upgrade_path_statistics
analyze_modules = module_statistics
analyze_capital_confirmation = capital_confirmation_statistics
analyze_risk_warnings = risk_warning_performance


__all__ = [
    "ANALYTICS_MODEL_VERSION",
    "analytics_payload",
    "build_lifecycle_analytics",
    "capital_confirmation_statistics",
    "factor_effect_statistics",
    "first_signal_level_statistics",
    "generate_analytics",
    "generate_lifecycle_analytics",
    "module_statistics",
    "risk_warning_performance",
    "upgrade_path_statistics",
]
