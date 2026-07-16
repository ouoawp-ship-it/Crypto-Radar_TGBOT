from __future__ import annotations

import math
import re
import time
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any


INTELLIGENCE_SCHEMA_VERSION = "2026-07-16"
RESONANCE_WINDOWS = (
    ("15m", 900),
    ("30m", 1800),
    ("1h", 3600),
    ("4h", 14400),
    ("1d", 86400),
)
LIFECYCLE_LABELS = {
    "new": "NEW",
    "enhancing": "增强",
    "continuing": "持续",
    "cooling": "降温",
    "restarted": "重启",
    "expired": "失效",
}
ABSOLUTE_KEYS = (
    ("quote_volume", "24h 成交额", "usd"),
    ("oi_usd", "持仓金额", "usd"),
    ("market_cap", "流通市值", "usd"),
)
MONEY_LABELS = {
    "quote_volume": ("24h成交额", "成交额", "quote volume", "volume", "vol"),
    "oi_usd": ("oi金额", "持仓金额", "open interest"),
    "market_cap": ("流通市值", "市值", "market cap"),
}


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _score(item: dict[str, Any]) -> float | None:
    return _number(item.get("score"))


def _rank(value: float | None, samples: list[float], *, label: str, method: str) -> dict[str, Any]:
    valid = sorted((item for item in samples if math.isfinite(item)), reverse=True)
    if value is None or len(valid) < 2:
        return {
            "available": False,
            "label": label,
            "sample_size": len(valid),
            "method": method,
            "reason": "至少需要 2 个同口径样本",
        }
    rank = 1 + sum(1 for item in valid if item > value)
    percentile = 100.0 * sum(1 for item in valid if item <= value) / len(valid)
    return {
        "available": True,
        "label": label,
        "value": round(value, 4),
        "rank": rank,
        "sample_size": len(valid),
        "percentile": round(percentile, 1),
        "method": method,
    }


def _money_number(value: str, suffix: str) -> float | None:
    number = _number(str(value or "").replace(",", ""))
    if number is None:
        return None
    multiplier = {
        "k": 1_000,
        "m": 1_000_000,
        "b": 1_000_000_000,
        "t": 1_000_000_000_000,
        "万": 10_000,
        "亿": 100_000_000,
    }.get(str(suffix or "").lower(), 1)
    return number * multiplier


def absolute_metric(item: dict[str, Any]) -> dict[str, Any] | None:
    payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
    for key, label, unit in ABSOLUTE_KEYS:
        value = _number(payload.get(key))
        if value is not None and value > 0:
            return {"key": key, "label": label, "unit": unit, "value": value, "quality": "structured"}

    text = str(item.get("text_html") or item.get("excerpt") or "")
    for key, label, unit in ABSOLUTE_KEYS:
        names = "|".join(re.escape(name) for name in MONEY_LABELS[key])
        match = re.search(
            rf"(?:{names})\s*[:：]?\s*\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*([KMBT万亿]?)",
            text,
            flags=re.IGNORECASE,
        )
        if not match:
            continue
        value = _money_number(match.group(1), match.group(2))
        if value is not None and value > 0:
            return {"key": key, "label": label, "unit": unit, "value": value, "quality": "parsed"}
    return None


def _latest_by_symbol(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the latest row per symbol from rows sorted by (ts, id)."""
    latest: dict[str, dict[str, Any]] = {}
    for item in reversed(items):
        symbol = str(item.get("symbol") or "")
        if symbol and symbol not in latest:
            latest[symbol] = item
    return list(latest.values())


def _time_slice(
    rows: list[dict[str, Any]],
    timestamps: list[int],
    *,
    start_ts: int,
    end_ts: int,
) -> list[dict[str, Any]]:
    """Slice an ascending time index while preserving same-timestamp rows."""
    start = bisect_left(timestamps, start_ts)
    end = bisect_right(timestamps, end_ts)
    return rows[start:end]


def _lifecycle(item: dict[str, Any], history: list[dict[str, Any]], now_ts: int) -> dict[str, Any]:
    item_ts = int(item.get("ts") or 0)
    age_sec = max(0, now_ts - item_ts)
    prior = [row for row in history if int(row.get("ts") or 0) < item_ts]
    previous = max(prior, key=lambda row: (int(row.get("ts") or 0), int(row.get("id") or 0)), default=None)
    if age_sec > 86400:
        state = "expired"
        basis = "最近一次同类信号已超过 24 小时"
    elif previous is None:
        state = "new"
        basis = "30 天内没有更早的同币同模块信号"
    else:
        gap = max(0, item_ts - int(previous.get("ts") or 0))
        current_score = _score(item)
        previous_score = _score(previous)
        delta = current_score - previous_score if current_score is not None and previous_score is not None else None
        if gap > 14400:
            state = "restarted"
            basis = f"沉寂 {max(1, round(gap / 3600))} 小时后再次出现"
        elif delta is not None and delta >= 5:
            state = "enhancing"
            basis = f"规则分数较上次提高 {delta:.1f}"
        elif delta is not None and delta <= -5:
            state = "cooling"
            basis = f"规则分数较上次下降 {abs(delta):.1f}"
        else:
            state = "continuing"
            basis = "4 小时内同类信号延续，强度未明显回落"
    return {
        "state": state,
        "label": LIFECYCLE_LABELS[state],
        "derived": True,
        "observed_at": str(item.get("time") or ""),
        "age_sec": age_sec,
        "basis": basis,
        "previous_signal_id": int(previous.get("id") or 0) if previous else None,
    }


def _resonance(
    item: dict[str, Any],
    symbol_events: list[dict[str, Any]],
    symbol_timestamps: list[int],
) -> dict[str, Any]:
    anchor = int(item.get("ts") or 0)
    end = bisect_right(symbol_timestamps, anchor)
    windows = []
    for key, seconds in RESONANCE_WINDOWS:
        start = bisect_left(symbol_timestamps, anchor - seconds, 0, end)
        relevant = symbol_events[start:end]
        modules = sorted({str(row.get("module") or "") for row in relevant if str(row.get("module") or "")})
        windows.append({
            "key": key,
            "seconds": seconds,
            "active": len(modules) >= 2,
            "module_count": len(modules),
            "signal_count": len(relevant),
            "modules": modules,
        })
    active_count = sum(1 for window in windows if window["active"])
    return {
        "label": "跨模块信号共振",
        "active_count": active_count,
        "window_count": len(windows),
        "windows": windows,
        "available": any(window["signal_count"] for window in windows),
        "method": "同币在各时间窗内至少出现 2 个不同雷达模块时记为共振；不代表方向一致",
    }


def _signal_intelligence(
    item: dict[str, Any],
    *,
    self_history: list[dict[str, Any]],
    market_candidates: list[dict[str, Any]],
    symbol_events: list[dict[str, Any]],
    symbol_timestamps: list[int],
    absolute_metrics: dict[int, dict[str, Any] | None],
    now_ts: int,
    market_window_sec: int,
) -> dict[str, Any]:
    current_score = _score(item)
    latest_market_candidates = _latest_by_symbol(market_candidates)
    metric = absolute_metrics.get(id(item))
    absolute_samples: list[float] = []
    if metric:
        for row in latest_market_candidates:
            peer = absolute_metrics.get(id(row))
            if peer and peer.get("key") == metric.get("key"):
                value = _number(peer.get("value"))
                if value is not None:
                    absolute_samples.append(value)
    absolute_rank = _rank(
        _number(metric.get("value")) if metric else None,
        absolute_samples,
        label="市场绝对规模",
        method=f"同模块最新信号按{metric.get('label')}排序" if metric else "需要结构化或可解析的成交额/OI/市值",
    )
    if metric:
        absolute_rank["metric"] = metric
    return {
        "self_rank": _rank(
            current_score,
            [value for value in (_score(row) for row in self_history) if value is not None],
            label="自身历史极端度",
            method="同币同模块近 30 天规则分数百分位",
        ),
        "market_strength_rank": _rank(
            current_score,
            [value for value in (_score(row) for row in latest_market_candidates) if value is not None],
            label="市场相对强度",
            method=f"同模块近 {max(1, market_window_sec // 3600)} 小时每币最新规则分数横截面排名",
        ),
        "market_absolute_rank": absolute_rank,
        "resonance": _resonance(item, symbol_events, symbol_timestamps),
        "lifecycle": _lifecycle(item, self_history, now_ts),
    }


def build_radar_intelligence(
    events: list[dict[str, Any]],
    *,
    now_ts: int | None = None,
    window_sec: int = 86400,
    board_limit: int = 5,
    target_refs: set[str] | None = None,
) -> dict[str, Any]:
    now = int(now_ts or time.time())
    safe_window = min(2_592_000, max(3600, int(window_sec or 86400)))
    sent_events = [
        item for item in events
        if str(item.get("status") or "") == "sent" and str(item.get("symbol") or "")
    ]
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_module: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_symbol_module: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in sent_events:
        symbol = str(item.get("symbol") or "")
        module = str(item.get("module") or "")
        by_symbol[symbol].append(item)
        by_module[module].append(item)
        by_symbol_module[(symbol, module)].append(item)
    all_groups = [*by_symbol.values(), *by_module.values(), *by_symbol_module.values()]
    for items in all_groups:
        items.sort(key=lambda row: (int(row.get("ts") or 0), int(row.get("id") or 0)))

    symbol_timestamps = {
        key: [int(row.get("ts") or 0) for row in rows]
        for key, rows in by_symbol.items()
    }
    module_timestamps = {
        key: [int(row.get("ts") or 0) for row in rows]
        for key, rows in by_module.items()
    }
    self_timestamps = {
        key: [int(row.get("ts") or 0) for row in rows]
        for key, rows in by_symbol_module.items()
    }
    absolute_metrics = {id(item): absolute_metric(item) for item in sent_events}

    if target_refs is None:
        analysis_items = [item for item in sent_events if int(item.get("ts") or 0) >= now - safe_window]
    else:
        normalized_refs = {str(reference or "").strip().lower() for reference in target_refs if str(reference or "").strip()}
        analysis_items = [
            item
            for item in sent_events
            if str(item.get("public_ref") or "").strip().lower() in normalized_refs
            or str(item.get("id") or "").strip().lower() in normalized_refs
        ]

    analyzed = []
    for item in analysis_items:
        symbol = str(item.get("symbol") or "")
        module = str(item.get("module") or "")
        item_ts = int(item.get("ts") or 0)
        symbol_rows = by_symbol.get(symbol, [])
        module_rows = by_module.get(module, [])
        self_key = (symbol, module)
        self_rows = by_symbol_module.get(self_key, [])
        analyzed.append({
            "signal": item,
            "intelligence": _signal_intelligence(
                item,
                self_history=_time_slice(
                    self_rows,
                    self_timestamps.get(self_key, []),
                    start_ts=item_ts - 2_592_000,
                    end_ts=item_ts,
                ),
                market_candidates=_time_slice(
                    module_rows,
                    module_timestamps.get(module, []),
                    start_ts=item_ts - safe_window,
                    end_ts=item_ts,
                ),
                symbol_events=symbol_rows,
                symbol_timestamps=symbol_timestamps.get(symbol, []),
                absolute_metrics=absolute_metrics,
                now_ts=now,
                market_window_sec=safe_window,
            ),
        })
    current = [entry for entry in analyzed if int(entry["signal"].get("ts") or 0) >= now - safe_window]
    latest_entries: dict[str, dict[str, Any]] = {}
    for entry in sorted(current, key=lambda row: int(row["signal"].get("ts") or 0), reverse=True):
        latest_entries.setdefault(str(entry["signal"].get("symbol") or ""), entry)
    latest = list(latest_entries.values())

    def priority(entry: dict[str, Any]) -> tuple[float, float, int]:
        intelligence = entry["intelligence"]
        resonance = float(intelligence["resonance"].get("active_count") or 0)
        score = _score(entry["signal"]) or 0.0
        ts = int(entry["signal"].get("ts") or 0)
        return resonance, score, ts

    def board(key: str, title: str, description: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
        ordered = sorted(candidates, key=priority, reverse=True)[: max(1, min(20, int(board_limit or 5)))]
        return {"key": key, "title": title, "description": description, "count": len(candidates), "items": ordered}

    def latest_matching(predicate: Any) -> list[dict[str, Any]]:
        selected: dict[str, dict[str, Any]] = {}
        for entry in sorted(current, key=lambda row: int(row["signal"].get("ts") or 0), reverse=True):
            symbol = str(entry["signal"].get("symbol") or "")
            if symbol and symbol not in selected and predicate(entry):
                selected[symbol] = entry
        return list(selected.values())

    boards = [
        board("launch", "启动候选", "启动模块最新高分信号，优先看增强与重启状态。", latest_matching(lambda row: row["signal"].get("module") == "launch")),
        board("resonance", "跨模块共振", "同币在一个或多个时间窗内出现至少两个不同雷达模块。", [row for row in latest if int(row["intelligence"]["resonance"].get("active_count") or 0) > 0]),
        board("funding", "极端费率", "资金费率模块的最新异常；费率代表拥挤，不等于交易方向。", latest_matching(lambda row: row["signal"].get("module") == "funding")),
        board("risk", "结构与公告风险", "结构、公告或高风险级别信号，用于优先排查风险。", latest_matching(
            lambda row: row["signal"].get("module") in {"structure", "announcement"}
            or row["signal"].get("severity") in {"warning", "critical", "error"}
        )),
    ]
    return {
        "schema_version": INTELLIGENCE_SCHEMA_VERSION,
        "generated_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        "window_sec": safe_window,
        "data_status": "ready" if current else "empty",
        "methodology": {
            "ranking": "排名基于泡泡雷达自身可验证的规则分数和同口径绝对规模；样本不足时明确不可用。",
            "resonance": "共振表示跨模块同时出现，不推断多空方向。",
            "lifecycle": "生命周期由同币同模块间隔与规则分数变化确定。",
        },
        "summary": {
            "signals": len(current),
            "symbols": len(latest),
            "resonance_symbols": sum(1 for row in latest if int(row["intelligence"]["resonance"].get("active_count") or 0) > 0),
            "enhancing_symbols": sum(1 for row in latest if row["intelligence"]["lifecycle"].get("state") == "enhancing"),
        },
        "items": analyzed,
        "boards": boards,
    }
