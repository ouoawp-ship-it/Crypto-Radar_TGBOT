from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .binance_lifecycle_data import BinanceLifecycleDataClient, safe_float
from .config import Settings
from .lifecycle_store import (
    LifecycleStore,
    coin_from_symbol,
    normalize_lifecycle_symbol,
    pct_change,
    safe_int,
    utc_iso,
)
from .signal_store import SignalEventStore
from .storage import JsonStore
from .telegram import TelegramGateway
from .web_services.api_core import redact_api_payload


NOT_ADVICE = "仅用于信号整理和风险提示，不构成投资建议，不执行自动交易。"
LEVEL_RANKS = {"unknown": 0, "15m": 1, "1h": 2, "4h": 3, "24h": 4}
RANK_LEVELS = {rank: level for level, rank in LEVEL_RANKS.items()}
STATE_LABELS = {
    "warming": "启动观察",
    "launching": "启动中",
    "upgraded_1h": "升级到 1H",
    "upgraded_4h": "升级到 4H",
    "trend_confirmed": "大周期确认",
    "cooling": "短线冷却",
    "risk_warning": "风险升高",
    "failed": "启动失败",
    "closed": "已结束",
}
EVENT_LABELS = {
    "first_signal": "首次信号",
    "same_level_confirm": "同级确认",
    "timeframe_upgrade": "周期升级",
    "timeframe_upgrade_1h": "升级到 1H",
    "timeframe_upgrade_4h": "升级到 4H",
    "timeframe_upgrade_24h": "大周期确认",
    "volume_expansion": "成交量放大",
    "oi_accumulation": "OI 累积",
    "oi_price_divergence": "OI 价格背离",
    "futures_cvd_confirmed": "合约 CVD 确认",
    "spot_cvd_confirmed": "现货 CVD 确认",
    "cvd_divergence": "CVD 背离",
    "funding_crowded": "资金费率拥挤",
    "funding_cooling": "资金费率冷却",
    "risk_warning": "风险升高",
    "short_term_weakening": "短线走弱",
    "major_timeframe_weakening": "大周期走弱",
    "launch_failed": "启动失败",
    "lifecycle_closed": "生命周期结束",
}
IMPORTANT_TELEGRAM_EVENTS = {
    "first_signal",
    "timeframe_upgrade_1h",
    "timeframe_upgrade_4h",
    "timeframe_upgrade_24h",
    "spot_cvd_confirmed",
    "oi_accumulation",
    "risk_warning",
    "short_term_weakening",
    "launch_failed",
}
WEAKENING_KEYWORDS = ("走弱", "冷却", "假突破", "破位", "失败", "回落", "跌破", "诱多")
RISK_KEYWORDS = ("拥挤", "高杠杆", "追高", "风险", "极端", "结算周期缩短", "破位", "假突破")
ANNOUNCEMENT_MODULES = {"announcement", "summary", "test"}
SIGNAL_DENSITY_WINDOW_SEC = 3600
SIGNAL_DENSITY_RISK_THRESHOLD = 4
LIFECYCLE_TELEGRAM_EVENT_COOLDOWN_SEC = 4 * 3600
LIFECYCLE_TELEGRAM_SYMBOL_HOURLY_LIMIT = 2
LIFECYCLE_TELEGRAM_GLOBAL_HOURLY_LIMIT = 30
METRIC_EVENT_TYPES = {
    "volume_expansion",
    "oi_accumulation",
    "oi_price_divergence",
    "futures_cvd_confirmed",
    "spot_cvd_confirmed",
    "cvd_divergence",
    "funding_crowded",
    "funding_cooling",
    "short_term_weakening",
    "major_timeframe_weakening",
    "launch_failed",
}

MetricsProvider = Callable[[str, str], dict[str, Any]]


def _text_of_signal(signal: dict[str, Any]) -> str:
    payload = signal.get("payload") if isinstance(signal.get("payload"), dict) else {}
    parts = [
        signal.get("timeframe"),
        signal.get("interval"),
        signal.get("level"),
        signal.get("signal_type"),
        signal.get("stage"),
        signal.get("module"),
        signal.get("title"),
        signal.get("excerpt"),
        payload.get("timeframe") if isinstance(payload, dict) else "",
        payload.get("interval") if isinstance(payload, dict) else "",
        payload.get("level") if isinstance(payload, dict) else "",
    ]
    return " ".join(str(part or "") for part in parts)


LEVEL_PATTERNS = (
    ("24h", re.compile(r"(?i)(?<!\w)(?:24\s*h|1\s*d)(?!\w)|日线|24\s*小时")),
    ("4h", re.compile(r"(?i)(?<![\d\w])4\s*h(?!\w)|4\s*小时")),
    ("1h", re.compile(r"(?i)(?<![\d\w])1\s*h(?!\w)|1\s*小时")),
    ("15m", re.compile(r"(?i)(?<![\d\w])15\s*(?:m|min)(?!\w)|15\s*分钟|15\s*分")),
)


def _level_from_values(values: list[Any]) -> tuple[str, int] | None:
    searchable = " ".join(str(value or "") for value in values if value is not None)
    if not searchable.strip():
        return None
    for level, pattern in LEVEL_PATTERNS:
        if pattern.search(searchable):
            return level, LEVEL_RANKS[level]
    return None


def extract_signal_level(signal: dict[str, Any]) -> tuple[str, int]:
    """Resolve a signal level using strict, fall-through precedence."""
    payload = signal.get("payload") if isinstance(signal.get("payload"), dict) else {}
    metadata = signal.get("metadata") if isinstance(signal.get("metadata"), dict) else {}
    payload_metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    layers = (
        [
            signal.get("timeframe"),
            signal.get("interval"),
            signal.get("level"),
            payload.get("timeframe"),
            payload.get("interval"),
            payload.get("level"),
            metadata.get("timeframe"),
            metadata.get("interval"),
            metadata.get("level"),
            payload_metadata.get("timeframe"),
            payload_metadata.get("interval"),
            payload_metadata.get("level"),
        ],
        [
            signal.get("signal_type"),
            signal.get("stage"),
            signal.get("module"),
            metadata.get("signal_type"),
            metadata.get("stage"),
            metadata.get("module"),
            payload_metadata.get("signal_type"),
            payload_metadata.get("stage"),
            payload_metadata.get("module"),
        ],
        [signal.get("excerpt"), signal.get("title")],
    )
    for values in layers:
        match = _level_from_values(values)
        if match is not None:
            return match
    return "unknown", 0


def is_valid_lifecycle_signal(signal: dict[str, Any]) -> bool:
    symbol = normalize_lifecycle_symbol(signal.get("symbol"))
    if not symbol:
        return False
    if str(signal.get("status") or "").lower() != "sent":
        return False
    module = str(signal.get("module") or "").lower()
    if module in ANNOUNCEMENT_MODULES:
        return False
    text = _text_of_signal(signal).lower()
    if any(token in text for token in ("dry-run", "dry_run", "测试消息", "test message")):
        return False
    return True


def state_for_level(level: str) -> str:
    return {
        "15m": "warming",
        "1h": "launching",
        "4h": "upgraded_4h",
        "24h": "trend_confirmed",
    }.get(str(level or ""), "warming")


def state_for_upgrade(level: str) -> str:
    return {
        "1h": "upgraded_1h",
        "4h": "upgraded_4h",
        "24h": "trend_confirmed",
    }.get(str(level or ""), "launching")


def lifecycle_event_type_for_upgrade(level: str) -> str:
    return {
        "1h": "timeframe_upgrade_1h",
        "4h": "timeframe_upgrade_4h",
        "24h": "timeframe_upgrade_24h",
    }.get(str(level or ""), "timeframe_upgrade")


def _metric(metrics: dict[str, Any], key: str) -> float | None:
    return safe_float(metrics.get(key))


def _config_float(settings: Settings, name: str, default: float) -> float:
    return float(getattr(settings, name, default) or default)


def _volume_multiplier(metrics: dict[str, Any], previous: dict[str, Any] | None = None) -> float | None:
    volume = _metric(metrics, "volume")
    previous_metrics = (previous or {}).get("metrics") if isinstance((previous or {}).get("metrics"), dict) else {}
    previous_volume = (
        _metric(previous or {}, "first_volume_15m")
        or _metric(previous_metrics, "first_volume_15m")
        or _metric(previous_metrics, "volume")
    )
    if volume is None or previous_volume is None or previous_volume <= 0:
        return None
    return round(volume / previous_volume, 4)


def _cvd_change(current: Any, first: Any) -> float | None:
    current_value = safe_float(current)
    first_value = safe_float(first)
    if current_value is None or first_value is None:
        return None
    return round(current_value - first_value, 4)


def calculate_lifecycle_scores(
    *,
    signal: dict[str, Any],
    first_level: str,
    highest_level: str,
    metrics: dict[str, Any],
    previous: dict[str, Any] | None,
    settings: Settings,
) -> tuple[float, float, list[str]]:
    reasons: list[str] = []
    score = {"15m": 10, "1h": 20, "4h": 30, "24h": 40}.get(first_level, 0)
    raw_score = safe_float(signal.get("score"))
    if raw_score is not None:
        score += min(20.0, max(0.0, raw_score / 5.0))
        reasons.append(f"信号分数 {round(raw_score, 2)} 已纳入生命周期强度。")
    upgrade_levels = metrics.get("upgrade_levels") if isinstance(metrics.get("upgrade_levels"), list) else []
    upgrade_levels = list(dict.fromkeys(str(item) for item in upgrade_levels))
    score += sum({"1h": 10, "4h": 15, "24h": 20}.get(level, 0) for level in upgrade_levels)
    if upgrade_levels:
        reasons.append(f"已确认周期升级：{', '.join(upgrade_levels)}。")
    if LEVEL_RANKS.get(highest_level, 0):
        reasons.append(f"当前最高周期为 {highest_level}。")

    risk = 0.0
    price_change = _metric(metrics, "price_change_from_first_pct")
    oi_change = _metric(metrics, "oi_change_from_first_pct")
    futures_cvd = _metric(metrics, "futures_cvd_delta")
    spot_cvd = _metric(metrics, "spot_cvd_delta")
    funding = _metric(metrics, "funding_rate")
    volume_multiplier = _metric(metrics, "volume_multiplier")

    if volume_multiplier is not None and volume_multiplier >= _config_float(settings, "lifecycle_volume_expansion_multiplier", 2.0):
        score += 10
        reasons.append(f"Binance 成交量约为首信号附近的 {round(volume_multiplier, 2)}x。")
    if oi_change is not None and oi_change >= _config_float(settings, "lifecycle_oi_accumulation_pct", 8.0):
        score += 10
        reasons.append(f"Binance OI 较首信号增长 {round(oi_change, 2)}%。")
    if futures_cvd is not None and futures_cvd > 0:
        score += 10
        reasons.append("Binance 合约 CVD 显示主动买入增强。")
    if spot_cvd is not None and spot_cvd > 0:
        score += 15
        reasons.append("Binance 现货 CVD 显示买盘跟随。")
    funding_threshold = _config_float(settings, "lifecycle_funding_crowded_threshold", 0.0008)
    if funding is not None and abs(funding) < funding_threshold:
        score += 5
        reasons.append("Binance funding 未明显拥挤。")

    if oi_change is not None and oi_change > 0 and price_change is not None and price_change < 0:
        risk += 25
        reasons.append("OI 上升但价格低于首信号，存在杠杆拥挤风险。")
    if futures_cvd is not None and futures_cvd > 0 and (spot_cvd is None or spot_cvd <= 0):
        risk += 20
        reasons.append("合约 CVD 增强但现货 CVD 未跟随。")
    if funding is not None and funding >= funding_threshold:
        risk += 20
        reasons.append("资金费率偏热，追高风险上升。")
    if price_change is not None and price_change >= 12 and funding is not None and funding >= funding_threshold / 2:
        risk += 15
        reasons.append("价格较首信号快速拉升且 funding 转热。")
    signal_density = safe_int(metrics.get("signal_density_1h"), 0)
    if signal_density >= SIGNAL_DENSITY_RISK_THRESHOLD:
        risk += 10
        reasons.append(f"最近一小时出现 {signal_density} 次信号，需观察信号过密风险。")
    text = _text_of_signal(signal)
    has_weakening_keyword = any(keyword in text for keyword in WEAKENING_KEYWORDS)
    if has_weakening_keyword:
        risk += 10
        reasons.append("信号文本出现走弱或失败关键词。")
    if not has_weakening_keyword and any(keyword in text for keyword in RISK_KEYWORDS):
        risk += 10
        reasons.append("信号文本出现风险关键词。")
    return round(max(0.0, min(score, 100.0)), 2), round(max(0.0, min(risk, 100.0)), 2), reasons


def lifecycle_state_from_scores(
    *,
    current_state: str,
    lifecycle_score: float,
    risk_score: float,
    metrics: dict[str, Any],
    signal: dict[str, Any],
    settings: Settings,
) -> str:
    price_change = _metric(metrics, "price_change_from_first_pct")
    if price_change is not None and price_change <= -_config_float(settings, "lifecycle_fail_price_drop_pct", 8.0):
        return "failed"
    text = _text_of_signal(signal)
    level, level_rank = extract_signal_level(signal)
    if any(keyword in text for keyword in WEAKENING_KEYWORDS):
        return "risk_warning" if level_rank >= 2 else "cooling"
    oi_change = _metric(metrics, "oi_change_from_first_pct")
    if oi_change is not None and oi_change > 0 and price_change is not None and price_change < 0:
        return "risk_warning"
    funding = _metric(metrics, "funding_rate")
    if funding is not None and funding >= _config_float(settings, "lifecycle_funding_crowded_threshold", 0.0008):
        return "risk_warning"
    if risk_score >= 70:
        return "risk_warning"
    pullback = _metric(metrics, "pullback_from_peak_pct")
    if pullback is not None and pullback <= -_config_float(settings, "lifecycle_cooling_pullback_pct", 5.0):
        return "cooling"
    futures_cvd = _metric(metrics, "futures_cvd_delta")
    oi_change = _metric(metrics, "oi_change_from_first_pct")
    if futures_cvd is not None and futures_cvd < 0 and oi_change is not None and oi_change < 0:
        return "cooling"
    if lifecycle_score >= 60 and risk_score < 60:
        return "launching" if current_state == "warming" else current_state
    return current_state


def build_lifecycle_metrics(
    *,
    lifecycle: dict[str, Any] | None,
    signal: dict[str, Any],
    metrics: dict[str, Any],
) -> dict[str, Any]:
    first = lifecycle or {}
    previous_metrics = first.get("metrics") if isinstance(first.get("metrics"), dict) else {}
    price = _metric(metrics, "price")
    market_cap = _metric(metrics, "market_cap_usd")
    oi = _metric(metrics, "oi")
    oi_value = _metric(metrics, "oi_value_usdt")
    futures_cvd = _metric(metrics, "futures_cvd_delta")
    spot_cvd = _metric(metrics, "spot_cvd_delta")
    funding = _metric(metrics, "funding_rate")
    result = dict(metrics)
    result.update({
        "latest_signal_id": safe_int(signal.get("id")),
        "latest_signal_at": str(signal.get("time") or ""),
        "latest_price": price,
        "latest_market_cap_usd": market_cap,
        "latest_oi": oi,
        "latest_oi_value_usdt": oi_value,
        "latest_futures_cvd_15m": futures_cvd,
        "latest_spot_cvd_15m": spot_cvd,
        "latest_funding_rate": funding,
        "price_change_from_first_pct": pct_change(price, first.get("first_price") if first else price),
        "market_cap_change_from_first_pct": pct_change(market_cap, first.get("first_market_cap_usd") if first else market_cap),
        "oi_change_from_first_pct": pct_change(oi, first.get("first_oi") if first else oi),
        "oi_value_change_from_first_pct": pct_change(oi_value, first.get("first_oi_value_usdt") if first else oi_value),
        "futures_cvd_change_from_first": _cvd_change(futures_cvd, first.get("first_futures_cvd_15m") if first else futures_cvd),
        "spot_cvd_change_from_first": _cvd_change(spot_cvd, first.get("first_spot_cvd_15m") if first else spot_cvd),
    })
    result["volume_multiplier"] = _volume_multiplier(result, first)
    result["volume_change_from_first_pct"] = pct_change(
        _metric(result, "volume"),
        first.get("first_volume_15m") if first else _metric(result, "volume"),
    )
    result["quote_volume_change_from_first_pct"] = pct_change(
        _metric(result, "quote_volume"),
        first.get("first_quote_volume_15m") if first else _metric(result, "quote_volume"),
    )
    previous_peak = _metric(previous_metrics, "peak_price")
    first_price = _metric(first, "first_price")
    candidates = [value for value in (previous_peak, first_price, price) if value is not None]
    peak_price = max(candidates) if candidates else None
    result["peak_price"] = peak_price
    result["pullback_from_peak_pct"] = pct_change(price, peak_price)
    previous_upgrades = previous_metrics.get("upgrade_levels") if isinstance(previous_metrics.get("upgrade_levels"), list) else []
    result["upgrade_levels"] = list(dict.fromkeys(str(item) for item in previous_upgrades))
    return result


def event_dedup_key(
    symbol: str,
    event_type: str,
    signal_id: Any,
    level: str,
    token: str = "",
) -> str:
    normalized_signal_id = safe_int(signal_id)
    identity = str(normalized_signal_id) if normalized_signal_id > 0 else str(token or "refresh")
    raw = f"{symbol}:{event_type}:{identity}:{level}"
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"lifecycle:{digest}"


def event_type_for_transition(
    *,
    lifecycle: dict[str, Any] | None,
    level: str,
    level_rank: int,
    metrics: dict[str, Any],
    signal: dict[str, Any],
    settings: Settings,
) -> tuple[str, str]:
    if not lifecycle:
        return "first_signal", state_for_level(level)
    previous_rank = safe_int(lifecycle.get("highest_level_rank"), 0)
    if level_rank > previous_rank:
        if level_rank >= LEVEL_RANKS["1h"]:
            event_type = lifecycle_event_type_for_upgrade(level)
            return event_type, state_for_upgrade(level)
        return "same_level_confirm", state_for_level(level)
    price_change = _metric(metrics, "price_change_from_first_pct")
    oi_change = _metric(metrics, "oi_change_from_first_pct")
    funding = _metric(metrics, "funding_rate")
    if price_change is not None and price_change <= -_config_float(settings, "lifecycle_fail_price_drop_pct", 8.0):
        return "launch_failed", "failed"
    weakening = any(keyword in _text_of_signal(signal) for keyword in WEAKENING_KEYWORDS)
    if weakening and level_rank >= 2:
        return "major_timeframe_weakening", "risk_warning"
    if weakening:
        return "short_term_weakening", "cooling"
    if oi_change is not None and oi_change >= _config_float(settings, "lifecycle_oi_accumulation_pct", 8.0) and price_change is not None and price_change < 0:
        return "oi_price_divergence", "risk_warning"
    if funding is not None and funding >= _config_float(settings, "lifecycle_funding_crowded_threshold", 0.0008):
        return "funding_crowded", "risk_warning"
    if level_rank == previous_rank and level_rank > 0:
        return "same_level_confirm", str(lifecycle.get("current_state") or "warming")
    if _metric(metrics, "spot_cvd_delta") is not None and (_metric(metrics, "spot_cvd_delta") or 0) > 0:
        return "spot_cvd_confirmed", str(lifecycle.get("current_state") or "warming")
    if _metric(metrics, "futures_cvd_delta") is not None and (_metric(metrics, "futures_cvd_delta") or 0) > 0:
        return "futures_cvd_confirmed", str(lifecycle.get("current_state") or "warming")
    return "same_level_confirm", str(lifecycle.get("current_state") or "warming")


def _previous_metrics(lifecycle: dict[str, Any] | None) -> dict[str, Any]:
    value = (lifecycle or {}).get("metrics")
    return value if isinstance(value, dict) else {}


def _crossed_threshold(
    current: float | None,
    previous: float | None,
    threshold: float,
    *,
    edge_only: bool,
) -> bool:
    if current is None or current < threshold:
        return False
    return not edge_only or previous is None or previous < threshold


def metric_event_types(
    *,
    lifecycle: dict[str, Any],
    level: str,
    level_rank: int,
    metrics: dict[str, Any],
    signal: dict[str, Any],
    settings: Settings,
    edge_only: bool = False,
) -> list[str]:
    """Return all independent Binance confirmation/risk events for a sample."""
    previous = _previous_metrics(lifecycle)
    event_types: list[str] = []
    price_change = _metric(metrics, "price_change_from_first_pct")
    oi_change = _metric(metrics, "oi_change_from_first_pct")
    futures_cvd = _metric(metrics, "futures_cvd_delta")
    spot_cvd = _metric(metrics, "spot_cvd_delta")
    futures_change = _metric(metrics, "futures_cvd_change_from_first")
    spot_change = _metric(metrics, "spot_cvd_change_from_first")
    funding = _metric(metrics, "funding_rate")
    volume_multiplier = _metric(metrics, "volume_multiplier")
    pullback = _metric(metrics, "pullback_from_peak_pct")
    previous_price_change = _metric(previous, "price_change_from_first_pct")
    previous_oi_change = _metric(previous, "oi_change_from_first_pct")
    previous_futures_change = _metric(previous, "futures_cvd_change_from_first")
    previous_spot_change = _metric(previous, "spot_cvd_change_from_first")
    previous_funding = _metric(previous, "funding_rate")
    previous_volume_multiplier = _metric(previous, "volume_multiplier")
    previous_pullback = _metric(previous, "pullback_from_peak_pct")
    volume_threshold = _config_float(settings, "lifecycle_volume_expansion_multiplier", 2.0)
    oi_threshold = _config_float(settings, "lifecycle_oi_accumulation_pct", 8.0)
    funding_threshold = _config_float(settings, "lifecycle_funding_crowded_threshold", 0.0008)
    cooling_threshold = -_config_float(settings, "lifecycle_cooling_pullback_pct", 5.0)
    fail_threshold = -_config_float(settings, "lifecycle_fail_price_drop_pct", 8.0)

    if _crossed_threshold(
        volume_multiplier,
        previous_volume_multiplier,
        volume_threshold,
        edge_only=edge_only,
    ):
        event_types.append("volume_expansion")
    if oi_change is not None and oi_change >= oi_threshold and (price_change is None or price_change >= 0):
        previous_true = (
            previous_oi_change is not None
            and previous_oi_change >= oi_threshold
            and (previous_price_change is None or previous_price_change >= 0)
        )
        if not edge_only or not previous_true:
            event_types.append("oi_accumulation")
    if oi_change is not None and oi_change > 0 and price_change is not None and price_change < 0:
        previous_true = (
            previous_oi_change is not None
            and previous_oi_change > 0
            and previous_price_change is not None
            and previous_price_change < 0
        )
        if not edge_only or not previous_true:
            event_types.append("oi_price_divergence")
    if _crossed_threshold(futures_change, previous_futures_change, 0.0000001, edge_only=edge_only):
        event_types.append("futures_cvd_confirmed")
    if _crossed_threshold(spot_change, previous_spot_change, 0.0000001, edge_only=edge_only):
        event_types.append("spot_cvd_confirmed")
    if futures_cvd is not None and futures_cvd > 0 and (spot_cvd is None or spot_cvd <= 0):
        previous_futures = _metric(previous, "futures_cvd_delta")
        previous_spot = _metric(previous, "spot_cvd_delta")
        previous_true = previous_futures is not None and previous_futures > 0 and (previous_spot is None or previous_spot <= 0)
        if not edge_only or not previous_true:
            event_types.append("cvd_divergence")
    if _crossed_threshold(funding, previous_funding, funding_threshold, edge_only=edge_only):
        event_types.append("funding_crowded")
    if (
        funding is not None
        and funding < funding_threshold
        and previous_funding is not None
        and previous_funding >= funding_threshold
    ):
        event_types.append("funding_cooling")

    text_has_weakening = any(keyword in _text_of_signal(signal) for keyword in WEAKENING_KEYWORDS)
    failed = price_change is not None and price_change <= fail_threshold
    if failed:
        previous_failed = previous_price_change is not None and previous_price_change <= fail_threshold
        if not edge_only or not previous_failed:
            event_types.append("launch_failed")
    elif text_has_weakening and level_rank >= 2:
        event_types.append("major_timeframe_weakening")
    else:
        pullback_weak = pullback is not None and pullback <= cooling_threshold
        previous_pullback_weak = previous_pullback is not None and previous_pullback <= cooling_threshold
        cvd_oi_weak = futures_cvd is not None and futures_cvd < 0 and oi_change is not None and oi_change < 0
        previous_cvd = _metric(previous, "futures_cvd_delta")
        previous_cvd_oi_weak = (
            previous_cvd is not None
            and previous_cvd < 0
            and previous_oi_change is not None
            and previous_oi_change < 0
        )
        if (
            text_has_weakening
            or (pullback_weak and (not edge_only or not previous_pullback_weak))
            or (cvd_oi_weak and (not edge_only or not previous_cvd_oi_weak))
        ):
            event_types.append("short_term_weakening")
    return list(dict.fromkeys(event_types))


def lifecycle_event_payload(
    *,
    lifecycle: dict[str, Any],
    signal: dict[str, Any],
    event_type: str,
    level: str,
    level_rank: int,
    previous_state: str,
    new_state: str,
    metrics: dict[str, Any],
    reasons: list[str],
    score: float,
    risk_score: float,
) -> dict[str, Any]:
    symbol = normalize_lifecycle_symbol(signal.get("symbol"))
    return {
        "lifecycle_id": lifecycle.get("id", 0),
        "symbol": symbol,
        "event_time": str(signal.get("time") or utc_iso(safe_int(signal.get("ts"), int(time.time())))),
        "event_type": event_type,
        "event_level": level,
        "event_level_rank": level_rank,
        "signal_id": safe_int(signal.get("id")),
        "source_module": str(signal.get("module") or ""),
        "source_template": str(signal.get("template_id") or ""),
        "source_excerpt": str(signal.get("excerpt") or "")[:500],
        "previous_state": previous_state,
        "new_state": new_state,
        "price": _metric(metrics, "price"),
        "price_change_from_first_pct": _metric(metrics, "price_change_from_first_pct"),
        "volume_change_pct": _metric(metrics, "volume_change_from_first_pct"),
        "quote_volume_change_pct": _metric(metrics, "quote_volume_change_from_first_pct"),
        "oi_change_pct": _metric(metrics, "oi_change_from_first_pct"),
        "oi_value_change_pct": _metric(metrics, "oi_value_change_from_first_pct"),
        "futures_cvd_delta": _metric(metrics, "futures_cvd_delta"),
        "spot_cvd_delta": _metric(metrics, "spot_cvd_delta"),
        "funding_rate": _metric(metrics, "funding_rate"),
        "event_score": score,
        "risk_score": risk_score,
        "metrics": metrics,
        "reasons": reasons,
        "exchange_context": metrics.get("exchange_context") or {},
        "dedup_key": event_dedup_key(
            symbol,
            event_type,
            signal.get("id"),
            level,
            str(signal.get("_lifecycle_dedup_token") or ""),
        ),
        "pushed_to_telegram": 0,
    }


def first_lifecycle_values(
    *,
    signal: dict[str, Any],
    level: str,
    level_rank: int,
    metrics: dict[str, Any],
    score: float,
    risk_score: float,
    reasons: list[str],
) -> dict[str, Any]:
    symbol = normalize_lifecycle_symbol(signal.get("symbol"))
    signal_time = str(signal.get("time") or utc_iso(safe_int(signal.get("ts"), int(time.time()))))
    initial_metrics = dict(metrics)
    initial_metrics.update({
        "price_change_from_first_pct": 0,
        "market_cap_change_from_first_pct": 0,
        "oi_change_from_first_pct": 0,
        "oi_value_change_from_first_pct": 0,
        "futures_cvd_change_from_first": 0,
        "spot_cvd_change_from_first": 0,
        "volume_change_from_first_pct": 0,
        "quote_volume_change_from_first_pct": 0,
        "volume_multiplier": 1.0 if _metric(metrics, "volume") not in {None, 0} else None,
        "peak_price": _metric(metrics, "price"),
        "pullback_from_peak_pct": 0,
        "upgrade_levels": [],
    })
    return {
        "symbol": symbol,
        "first_signal_id": safe_int(signal.get("id")),
        "first_signal_at": signal_time,
        "first_signal_module": str(signal.get("module") or ""),
        "first_signal_template": str(signal.get("template_id") or ""),
        "first_signal_type": str(signal.get("signal_type") or ""),
        "first_signal_level": level,
        "first_signal_level_rank": level_rank,
        "first_signal_score": safe_float(signal.get("score")),
        "first_signal_excerpt": str(signal.get("excerpt") or "")[:800],
        "first_price": _metric(metrics, "price"),
        "first_market_cap_usd": _metric(metrics, "market_cap_usd"),
        "first_volume_15m": _metric(metrics, "volume"),
        "first_quote_volume_15m": _metric(metrics, "quote_volume"),
        "first_oi": _metric(metrics, "oi"),
        "first_oi_value_usdt": _metric(metrics, "oi_value_usdt"),
        "first_futures_cvd_15m": _metric(metrics, "futures_cvd_delta"),
        "first_spot_cvd_15m": _metric(metrics, "spot_cvd_delta"),
        "first_funding_rate": _metric(metrics, "funding_rate"),
        "current_state": state_for_level(level),
        "highest_level": level,
        "highest_level_rank": level_rank,
        "lifecycle_score": score,
        "risk_score": risk_score,
        "latest_signal_id": safe_int(signal.get("id")),
        "latest_signal_at": signal_time,
        "latest_price": _metric(metrics, "price"),
        "latest_market_cap_usd": _metric(metrics, "market_cap_usd"),
        "latest_oi": _metric(metrics, "oi"),
        "latest_oi_value_usdt": _metric(metrics, "oi_value_usdt"),
        "latest_futures_cvd_15m": _metric(metrics, "futures_cvd_delta"),
        "latest_spot_cvd_15m": _metric(metrics, "spot_cvd_delta"),
        "latest_funding_rate": _metric(metrics, "funding_rate"),
        "price_change_from_first_pct": 0,
        "market_cap_change_from_first_pct": 0,
        "oi_change_from_first_pct": 0,
        "oi_value_change_from_first_pct": 0,
        "futures_cvd_change_from_first": 0,
        "spot_cvd_change_from_first": 0,
        "exchange_context": metrics.get("exchange_context") or {},
        "metrics": initial_metrics,
        "reasons": reasons,
        "is_active": 1,
    }


@dataclass
class LifecycleEngine:
    settings: Settings
    store: LifecycleStore | None = None
    metrics_provider: MetricsProvider | None = None

    def __post_init__(self) -> None:
        if self.store is None:
            self.store = LifecycleStore(getattr(self.settings, "lifecycle_db_path", self.settings.data_dir / "lifecycle.db"))
        if self.metrics_provider is None:
            client = BinanceLifecycleDataClient(self.settings)
            self.metrics_provider = client.snapshot

    def metrics_for_signal(
        self,
        signal: dict[str, Any],
        level: str,
        *,
        dry_run: bool = False,
        cache: dict[tuple[str, str], dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        symbol = normalize_lifecycle_symbol(signal.get("symbol"))
        timeframe = level if level in LEVEL_RANKS and level != "unknown" else "15m"
        cache_key = (symbol, timeframe)
        if cache is not None and cache_key in cache:
            return cache[cache_key]
        result = self._metrics_for_signal_uncached(signal, timeframe, dry_run=dry_run)
        if cache is not None:
            cache[cache_key] = result
        return result

    def _metrics_for_signal_uncached(
        self,
        signal: dict[str, Any],
        level: str,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        if dry_run:
            return {
                "symbol": normalize_lifecycle_symbol(signal.get("symbol")),
                "timeframe": level or "15m",
                "data_source": "binance",
                "data_source_status": "dry_run",
                "exchange_context": {"items": [], "note": "dry-run 未访问外部行情源。"},
            }
        assert self.metrics_provider is not None
        try:
            return self.metrics_provider(
                normalize_lifecycle_symbol(signal.get("symbol")),
                level if level in LEVEL_RANKS and level != "unknown" else "15m",
            )
        except Exception as exc:
            return {
                "symbol": normalize_lifecycle_symbol(signal.get("symbol")),
                "timeframe": level or "15m",
                "data_source": "binance",
                "data_source_status": "unavailable",
                "data_source_reason": f"{type(exc).__name__}: {exc}"[:180],
                "exchange_context": {"items": [], "note": "Binance 生命周期数据暂不可用。"},
            }

    def process_signal(
        self,
        signal: dict[str, Any],
        *,
        dry_run: bool = False,
        conn: sqlite3.Connection | None = None,
        metrics_cache: dict[tuple[str, str], dict[str, Any]] | None = None,
        metrics: dict[str, Any] | None = None,
        check_processed: bool = True,
    ) -> dict[str, Any]:
        assert self.store is not None
        if not is_valid_lifecycle_signal(signal):
            return {"ok": True, "skipped": True, "reason": "not_lifecycle_signal"}
        signal_id = safe_int(signal.get("id"))
        if (
            check_processed
            and not dry_run
            and signal_id > 0
            and self.store.is_signal_processed(signal_id, conn=conn)
        ):
            return {
                "ok": True,
                "skipped": True,
                "reason": "already_processed",
                "created": False,
                "event_inserted": False,
            }
        symbol = normalize_lifecycle_symbol(signal.get("symbol"))
        level, level_rank = extract_signal_level(signal)
        if metrics is None:
            metrics = self.metrics_for_signal(signal, level, dry_run=dry_run, cache=metrics_cache)
        if not dry_run and conn is None:
            with self.store.transaction() as owned_conn:
                return self.process_signal(
                    signal,
                    conn=owned_conn,
                    metrics_cache=metrics_cache,
                    metrics=metrics,
                    check_processed=check_processed,
                )
        existing = None
        if conn is not None:
            existing = self.store.get_lifecycle(symbol, conn=conn)
        elif self.store.db_path.exists():
            existing = self.store.get_lifecycle(symbol)
        if existing is None:
            first_score, first_risk, reasons = calculate_lifecycle_scores(
                signal=signal,
                first_level=level,
                highest_level=level,
                metrics=metrics,
                previous=None,
                settings=self.settings,
            )
            lifecycle_values = first_lifecycle_values(
                signal=signal,
                level=level,
                level_rank=level_rank,
                metrics=metrics,
                score=first_score,
                risk_score=first_risk,
                reasons=reasons or ["首次有效信号已创建生命周期档案。"],
            )
            lifecycle, created = self.store.create_lifecycle(lifecycle_values, dry_run=dry_run, conn=conn)
            stored_metrics = lifecycle.get("metrics") if isinstance(lifecycle.get("metrics"), dict) else metrics
            event_payload = lifecycle_event_payload(
                lifecycle=lifecycle,
                signal=signal,
                event_type="first_signal",
                level=level,
                level_rank=level_rank,
                previous_state="",
                new_state=str(lifecycle_values["current_state"]),
                metrics=stored_metrics,
                reasons=lifecycle_values["reasons"],
                score=first_score,
                risk_score=first_risk,
            )
            event, inserted = self.store.insert_event(event_payload, dry_run=dry_run, conn=conn)
            self.store.insert_snapshot(_snapshot_values(symbol, level, stored_metrics), dry_run=dry_run, conn=conn)
            return {
                "ok": True,
                "created": created,
                "event_inserted": inserted,
                "events_inserted": 1 if inserted else 0,
                "lifecycle": lifecycle,
                "event": event,
                "events": [event] if event else [],
            }

        metrics = dict(metrics)
        signal_ts = safe_int(signal.get("ts"), int(time.time()))
        if conn is not None and signal_ts > 0:
            density_start = utc_iso(signal_ts - SIGNAL_DENSITY_WINDOW_SEC)
            density_end = utc_iso(signal_ts)
            metrics["signal_density_1h"] = self.store.recent_signal_count(
                symbol,
                start_time=density_start,
                end_time=density_end,
                conn=conn,
            ) + (1 if signal_id > 0 else 0)
        merged_metrics = build_lifecycle_metrics(lifecycle=existing, signal=signal, metrics=metrics)
        highest_rank = max(safe_int(existing.get("highest_level_rank")), level_rank)
        highest_level = RANK_LEVELS.get(highest_rank, str(existing.get("highest_level") or level))
        upgrade_levels = list(merged_metrics.get("upgrade_levels") or [])
        if level_rank > safe_int(existing.get("highest_level_rank")) and level in {"1h", "4h", "24h"}:
            upgrade_levels.append(level)
        merged_metrics["upgrade_levels"] = list(dict.fromkeys(str(item) for item in upgrade_levels))
        score, risk_score, reasons = calculate_lifecycle_scores(
            signal=signal,
            first_level=str(existing.get("first_signal_level") or level),
            highest_level=highest_level,
            metrics=merged_metrics,
            previous=existing,
            settings=self.settings,
        )
        event_type, event_state = event_type_for_transition(
            lifecycle=existing,
            level=level,
            level_rank=level_rank,
            metrics=merged_metrics,
            signal=signal,
            settings=self.settings,
        )
        new_state = lifecycle_state_from_scores(
            current_state=event_state,
            lifecycle_score=score,
            risk_score=risk_score,
            metrics=merged_metrics,
            signal=signal,
            settings=self.settings,
        )
        update = {
            "current_state": new_state,
            "highest_level": highest_level,
            "highest_level_rank": highest_rank,
            "lifecycle_score": score,
            "risk_score": risk_score,
            "latest_signal_id": safe_int(signal.get("id")),
            "latest_signal_at": str(signal.get("time") or utc_iso(safe_int(signal.get("ts"), int(time.time())))),
            "latest_price": merged_metrics.get("latest_price"),
            "latest_market_cap_usd": merged_metrics.get("latest_market_cap_usd"),
            "latest_oi": merged_metrics.get("latest_oi"),
            "latest_oi_value_usdt": merged_metrics.get("latest_oi_value_usdt"),
            "latest_futures_cvd_15m": merged_metrics.get("latest_futures_cvd_15m"),
            "latest_spot_cvd_15m": merged_metrics.get("latest_spot_cvd_15m"),
            "latest_funding_rate": merged_metrics.get("latest_funding_rate"),
            "price_change_from_first_pct": merged_metrics.get("price_change_from_first_pct"),
            "market_cap_change_from_first_pct": merged_metrics.get("market_cap_change_from_first_pct"),
            "oi_change_from_first_pct": merged_metrics.get("oi_change_from_first_pct"),
            "oi_value_change_from_first_pct": merged_metrics.get("oi_value_change_from_first_pct"),
            "futures_cvd_change_from_first": merged_metrics.get("futures_cvd_change_from_first"),
            "spot_cvd_change_from_first": merged_metrics.get("spot_cvd_change_from_first"),
            "exchange_context": merged_metrics.get("exchange_context") or {},
            "metrics": merged_metrics,
            "reasons": reasons,
        }
        if new_state in {"failed", "closed"}:
            update.update({
                "is_active": 0,
                "closed_at": utc_iso(),
                "close_reason": event_type,
            })
        lifecycle = self.store.update_lifecycle(symbol, update, dry_run=dry_run, conn=conn) or existing
        all_event_types = [event_type]
        all_event_types.extend(
            metric_event_types(
                lifecycle=existing,
                level=level,
                level_rank=level_rank,
                metrics=merged_metrics,
                signal=signal,
                settings=self.settings,
            )
        )
        if new_state == "risk_warning" and str(existing.get("current_state") or "") != "risk_warning":
            all_event_types.append("risk_warning")
        all_event_types = list(dict.fromkeys(all_event_types))
        events: list[dict[str, Any]] = []
        inserted_count = 0
        for current_event_type in all_event_types:
            event_payload = lifecycle_event_payload(
                lifecycle=lifecycle,
                signal=signal,
                event_type=current_event_type,
                level=level,
                level_rank=level_rank,
                previous_state=str(existing.get("current_state") or ""),
                new_state=new_state,
                metrics=merged_metrics,
                reasons=reasons or ["同币种生命周期已更新。"],
                score=score,
                risk_score=risk_score,
            )
            current_event, inserted = self.store.insert_event(event_payload, dry_run=dry_run, conn=conn)
            if current_event:
                events.append(current_event)
            inserted_count += 1 if inserted else 0
        self.store.insert_snapshot(_snapshot_values(symbol, level, merged_metrics), dry_run=dry_run, conn=conn)
        return {
            "ok": True,
            "created": False,
            "event_inserted": inserted_count > 0,
            "events_inserted": inserted_count,
            "lifecycle": lifecycle,
            "event": events[0] if events else {},
            "events": events,
        }

    def refresh_active_lifecycle(
        self,
        lifecycle: dict[str, Any],
        metrics: dict[str, Any],
        *,
        dry_run: bool = False,
        conn: sqlite3.Connection | None = None,
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        """Refresh one active lifecycle without fabricating a source signal."""
        assert self.store is not None
        symbol = normalize_lifecycle_symbol(lifecycle.get("symbol"))
        if not symbol or not bool(safe_int(lifecycle.get("is_active"), 1)):
            return {"ok": True, "skipped": True, "active_refresh": True, "reason": "inactive_lifecycle"}
        level = str(lifecycle.get("highest_level") or lifecycle.get("first_signal_level") or "15m")
        if level not in LEVEL_RANKS or level == "unknown":
            level = "15m"
        level_rank = LEVEL_RANKS.get(level, 1)
        timestamp = int(time.time() if now_ts is None else now_ts)
        signal = {
            "id": 0,
            "symbol": symbol,
            "status": "sent",
            "module": "lifecycle_metrics",
            "template_id": "LIFECYCLE_METRIC_REFRESH",
            "timeframe": level,
            "signal_type": "metrics_refresh",
            "score": lifecycle.get("first_signal_score"),
            "excerpt": "",
            "time": utc_iso(timestamp),
            "ts": timestamp,
            "_lifecycle_dedup_token": f"metrics:{timestamp // max(60, int(getattr(self.settings, 'lifecycle_scan_interval_sec', 900) or 900))}",
        }
        merged_metrics = build_lifecycle_metrics(lifecycle=lifecycle, signal=signal, metrics=dict(metrics))
        if not _lifecycle_metrics_changed(lifecycle, merged_metrics):
            return {"ok": True, "skipped": True, "active_refresh": True, "reason": "metrics_unchanged"}
        score, risk_score, reasons = calculate_lifecycle_scores(
            signal=signal,
            first_level=str(lifecycle.get("first_signal_level") or "unknown"),
            highest_level=str(lifecycle.get("highest_level") or level),
            metrics=merged_metrics,
            previous=lifecycle,
            settings=self.settings,
        )
        new_state = lifecycle_state_from_scores(
            current_state=str(lifecycle.get("current_state") or state_for_level(level)),
            lifecycle_score=score,
            risk_score=risk_score,
            metrics=merged_metrics,
            signal=signal,
            settings=self.settings,
        )
        event_types = metric_event_types(
            lifecycle=lifecycle,
            level=level,
            level_rank=level_rank,
            metrics=merged_metrics,
            signal=signal,
            settings=self.settings,
            edge_only=True,
        )
        if new_state == "risk_warning" and str(lifecycle.get("current_state") or "") != "risk_warning":
            event_types.append("risk_warning")
        event_types = list(dict.fromkeys(event_types))
        update = {
            "current_state": new_state,
            "lifecycle_score": score,
            "risk_score": risk_score,
            "latest_price": merged_metrics.get("latest_price"),
            "latest_market_cap_usd": merged_metrics.get("latest_market_cap_usd"),
            "latest_oi": merged_metrics.get("latest_oi"),
            "latest_oi_value_usdt": merged_metrics.get("latest_oi_value_usdt"),
            "latest_futures_cvd_15m": merged_metrics.get("latest_futures_cvd_15m"),
            "latest_spot_cvd_15m": merged_metrics.get("latest_spot_cvd_15m"),
            "latest_funding_rate": merged_metrics.get("latest_funding_rate"),
            "price_change_from_first_pct": merged_metrics.get("price_change_from_first_pct"),
            "market_cap_change_from_first_pct": merged_metrics.get("market_cap_change_from_first_pct"),
            "oi_change_from_first_pct": merged_metrics.get("oi_change_from_first_pct"),
            "oi_value_change_from_first_pct": merged_metrics.get("oi_value_change_from_first_pct"),
            "futures_cvd_change_from_first": merged_metrics.get("futures_cvd_change_from_first"),
            "spot_cvd_change_from_first": merged_metrics.get("spot_cvd_change_from_first"),
            "exchange_context": merged_metrics.get("exchange_context") or {},
            "metrics": merged_metrics,
            "reasons": reasons,
        }
        if new_state in {"failed", "closed"}:
            update.update({
                "is_active": 0,
                "closed_at": utc_iso(timestamp),
                "close_reason": event_types[0] if event_types else new_state,
            })
        stored = self.store.update_lifecycle(symbol, update, dry_run=dry_run, conn=conn) or lifecycle
        events: list[dict[str, Any]] = []
        inserted_count = 0
        for event_type in event_types:
            payload = lifecycle_event_payload(
                lifecycle=stored,
                signal=signal,
                event_type=event_type,
                level=level,
                level_rank=level_rank,
                previous_state=str(lifecycle.get("current_state") or ""),
                new_state=new_state,
                metrics=merged_metrics,
                reasons=reasons,
                score=score,
                risk_score=risk_score,
            )
            event, inserted = self.store.insert_event(payload, dry_run=dry_run, conn=conn)
            if event:
                events.append(event)
            inserted_count += 1 if inserted else 0
        self.store.insert_snapshot(
            _snapshot_values(symbol, level, merged_metrics, snapshot_time=utc_iso(timestamp)),
            dry_run=dry_run,
            conn=conn,
        )
        return {
            "ok": True,
            "created": False,
            "event_inserted": inserted_count > 0,
            "events_inserted": inserted_count,
            "lifecycle": stored,
            "event": events[0] if events else {},
            "events": events,
            "refreshed": True,
            "active_refresh": True,
        }


def _lifecycle_metrics_changed(lifecycle: dict[str, Any], metrics: dict[str, Any]) -> bool:
    previous = _previous_metrics(lifecycle)
    keys = (
        "price",
        "volume",
        "quote_volume",
        "oi",
        "oi_value_usdt",
        "futures_cvd_delta",
        "spot_cvd_delta",
        "funding_rate",
        "market_cap_usd",
    )
    for key in keys:
        current_value = _metric(metrics, key)
        previous_value = _metric(previous, key)
        if current_value is None and previous_value is None:
            continue
        if current_value is None or previous_value is None:
            return True
        tolerance = max(1e-12, abs(previous_value) * 1e-9)
        if abs(current_value - previous_value) > tolerance:
            return True
    return False


def _snapshot_values(
    symbol: str,
    level: str,
    metrics: dict[str, Any],
    *,
    snapshot_time: str | None = None,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "timeframe": level if level in LEVEL_RANKS else "15m",
        "snapshot_time": snapshot_time or utc_iso(),
        "price": _metric(metrics, "price") or _metric(metrics, "latest_price"),
        "volume": _metric(metrics, "volume"),
        "quote_volume": _metric(metrics, "quote_volume"),
        "oi": _metric(metrics, "oi") or _metric(metrics, "latest_oi"),
        "oi_value_usdt": _metric(metrics, "oi_value_usdt") or _metric(metrics, "latest_oi_value_usdt"),
        "futures_cvd_delta": _metric(metrics, "futures_cvd_delta"),
        "spot_cvd_delta": _metric(metrics, "spot_cvd_delta"),
        "funding_rate": _metric(metrics, "funding_rate"),
        "market_cap_usd": _metric(metrics, "market_cap_usd"),
        "metrics": metrics,
    }


def candidate_lifecycle_signals(
    *,
    settings: Settings,
    lookback_hours: int = 24,
    limit: int = 500,
    symbol: str = "",
) -> list[dict[str, Any]]:
    end_ts = int(time.time())
    start_ts = end_ts - max(1, int(lookback_hours or 24)) * 3600
    normalized = normalize_lifecycle_symbol(symbol)
    store = SignalEventStore(settings.signal_events_db_path)
    target = max(1, min(int(limit or 500), 20_000))
    cursor: int | None = None
    candidates: list[dict[str, Any]] = []
    while len(candidates) < target:
        page_limit = min(200, max(1, target - len(candidates)))
        result = store.list_signals(
            limit=page_limit,
            cursor=cursor,
            symbol=normalized,
            status="sent",
            start_ts=start_ts,
            end_ts=end_ts,
            sort_field="id",
            sort_direction="asc",
        )
        items = result.get("items", [])
        if not items:
            break
        for item in items:
            if is_valid_lifecycle_signal(item):
                candidates.append(item)
                if len(candidates) >= target:
                    break
        next_cursor = safe_int(items[-1].get("id"))
        if next_cursor <= 0 or next_cursor == cursor:
            break
        cursor = next_cursor
        if len(items) < page_limit:
            break
    return candidates


def _unavailable_metrics(symbol: str, timeframe: str, exc: BaseException) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "data_source": "binance",
        "data_source_status": "unavailable",
        "data_source_reason": f"{type(exc).__name__}: {exc}"[:180],
        "exchange_context": {"items": [], "note": "Binance 生命周期数据暂不可用。"},
    }


def _defer_unavailable_refresh(lifecycle: dict[str, Any], *, ttl_sec: int) -> bool:
    previous = _previous_metrics(lifecycle)
    if str(previous.get("data_source_status") or "") != "unavailable":
        return False
    try:
        updated_at = datetime.fromisoformat(str(lifecycle.get("updated_at") or ""))
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - updated_at).total_seconds() < max(1, int(ttl_sec))
    except (TypeError, ValueError):
        return False


def _prefetch_lifecycle_metrics(
    engine: LifecycleEngine,
    pairs: list[tuple[str, str]],
    *,
    max_workers: int = 8,
) -> dict[tuple[str, str], dict[str, Any]]:
    unique_pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for symbol, timeframe in pairs:
        normalized_symbol = normalize_lifecycle_symbol(symbol)
        normalized_timeframe = timeframe if timeframe in LEVEL_RANKS and timeframe != "unknown" else "15m"
        pair = (normalized_symbol, normalized_timeframe)
        if normalized_symbol and pair not in seen:
            seen.add(pair)
            unique_pairs.append(pair)
    if not unique_pairs:
        return {}

    provider = engine.metrics_provider
    owner = getattr(provider, "__self__", None)
    snapshot_many = getattr(owner, "snapshot_many", None)
    if callable(snapshot_many):
        try:
            batch = snapshot_many(unique_pairs, max_workers=max(1, min(int(max_workers), 8)))
            if isinstance(batch, dict):
                return {
                    pair: dict(batch.get(pair) or _unavailable_metrics(pair[0], pair[1], RuntimeError("missing batch result")))
                    for pair in unique_pairs
                }
        except Exception as exc:
            return {pair: _unavailable_metrics(pair[0], pair[1], exc) for pair in unique_pairs}

    assert provider is not None
    results: dict[tuple[str, str], dict[str, Any]] = {}
    worker_count = max(1, min(int(max_workers), 8, len(unique_pairs)))
    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="lifecycle-metrics") as executor:
        futures = {executor.submit(provider, symbol, timeframe): (symbol, timeframe) for symbol, timeframe in unique_pairs}
        for future in as_completed(futures):
            pair = futures[future]
            try:
                value = future.result()
                results[pair] = dict(value) if isinstance(value, dict) else _unavailable_metrics(
                    pair[0], pair[1], TypeError("metrics provider returned non-object")
                )
            except Exception as exc:
                results[pair] = _unavailable_metrics(pair[0], pair[1], exc)
    return results


def scan_lifecycles(
    *,
    settings: Settings | None = None,
    lookback_hours: int = 24,
    limit_symbols: int = 80,
    symbol: str = "",
    dry_run: bool = False,
    push: bool = False,
    send: bool = False,
    confirm_real_send: bool = False,
    metrics_provider: MetricsProvider | None = None,
) -> dict[str, Any]:
    loaded = settings or Settings.load()
    store = LifecycleStore(getattr(loaded, "lifecycle_db_path", loaded.data_dir / "lifecycle.db"))
    if not dry_run:
        store.ensure_schema()
    configured_limit = max(1, int(getattr(loaded, "lifecycle_active_max_symbols", 80) or 80))
    limit = max(1, min(int(limit_symbols or configured_limit), configured_limit, 80))
    # Read the whole bounded lookback window before filtering processed IDs.
    # Fetching only ``limit * 4`` oldest rows would permanently starve newer
    # signals once that prefix had already been processed.
    signals = candidate_lifecycle_signals(settings=loaded, lookback_hours=lookback_hours, limit=20_000, symbol=symbol)
    engine = LifecycleEngine(loaded, store=store, metrics_provider=metrics_provider)
    selected: list[dict[str, Any]] = []
    active_lifecycles: list[dict[str, Any]] = []
    processed_ids: set[int] = set()
    pending_symbols: set[str] = set()
    all_signal_ids = [safe_int(item.get("id")) for item in signals if safe_int(item.get("id")) > 0]
    if dry_run:
        for item in signals:
            if len(selected) >= max(1, limit * 4):
                break
            item_symbol = normalize_lifecycle_symbol(item.get("symbol"))
            if not item_symbol:
                continue
            if not symbol and item_symbol not in pending_symbols and len(pending_symbols) >= limit:
                continue
            pending_symbols.add(item_symbol)
            selected.append(item)
    else:
        with store.connect() as read_conn:
            processed_ids = store.processed_signal_ids(all_signal_ids, conn=read_conn)
            max_pending_signals = max(1, limit * 4)
            for item in signals:
                item_symbol = normalize_lifecycle_symbol(item.get("symbol"))
                if not item_symbol:
                    continue
                signal_id = safe_int(item.get("id"))
                if signal_id in processed_ids:
                    continue
                if len(selected) >= max_pending_signals:
                    break
                if not symbol and item_symbol not in pending_symbols and len(pending_symbols) >= limit:
                    continue
                pending_symbols.add(item_symbol)
                selected.append(item)
            active_limit = 1 if symbol else min(80, max(0, min(limit, 80) - len(pending_symbols)))
            if active_limit > 0:
                active_lifecycles = store.list_active_lifecycles(
                    limit=active_limit,
                    symbol=symbol,
                    exclude_symbols=pending_symbols,
                    conn=read_conn,
                )
                active_lifecycles = [
                    item
                    for item in active_lifecycles
                    if not _defer_unavailable_refresh(
                        item,
                        ttl_sec=int(getattr(loaded, "lifecycle_binance_cache_ttl_sec", 300) or 300),
                    )
                ]

    counts = {
        "signals": len(selected),
        "active_refresh": len(active_lifecycles),
        "refreshed": 0,
        "created": 0,
        "events": 0,
        "skipped": len(processed_ids),
        "refresh_skipped": 0,
        "telegram": 0,
        "dry_run": bool(dry_run),
    }
    events: list[dict[str, Any]] = []
    metrics_cache: dict[tuple[str, str], dict[str, Any]] = {}
    results: list[dict[str, Any] | None] = [None] * len(selected)
    refresh_results: list[dict[str, Any]] = []

    if dry_run:
        for index, item in enumerate(selected):
            results[index] = engine.process_signal(item, dry_run=True, metrics_cache=metrics_cache)
    else:
        pending: list[tuple[int, dict[str, Any]]] = []
        scheduled_ids: set[int] = set()
        for index, item in enumerate(selected):
            signal_id = safe_int(item.get("id"))
            if signal_id > 0 and (signal_id in processed_ids or signal_id in scheduled_ids):
                results[index] = {
                    "ok": True,
                    "skipped": True,
                    "reason": "already_processed",
                    "created": False,
                    "event_inserted": False,
                }
                continue
            if signal_id > 0:
                scheduled_ids.add(signal_id)
            pending.append((index, item))

        requested_pairs: list[tuple[str, str]] = []
        signal_pair_keys: dict[int, tuple[str, str]] = {}
        for index, item in pending:
            level, _ = extract_signal_level(item)
            timeframe = level if level != "unknown" else "15m"
            pair = (normalize_lifecycle_symbol(item.get("symbol")), timeframe)
            signal_pair_keys[index] = pair
            requested_pairs.append(pair)
        active_pair_keys: dict[str, tuple[str, str]] = {}
        for lifecycle in active_lifecycles:
            active_symbol = normalize_lifecycle_symbol(lifecycle.get("symbol"))
            active_level = str(lifecycle.get("highest_level") or lifecycle.get("first_signal_level") or "15m")
            if active_level not in LEVEL_RANKS or active_level == "unknown":
                active_level = "15m"
            pair = (active_symbol, active_level)
            active_pair_keys[active_symbol] = pair
            requested_pairs.append(pair)
        metrics_cache.update(_prefetch_lifecycle_metrics(engine, requested_pairs, max_workers=8))

        if pending or active_lifecycles:
            with store.transaction() as conn:
                pending_ids = [safe_int(item.get("id")) for _, item in pending if safe_int(item.get("id")) > 0]
                concurrently_processed = store.processed_signal_ids(pending_ids, conn=conn)
                for index, item in pending:
                    if safe_int(item.get("id")) in concurrently_processed:
                        results[index] = {
                            "ok": True,
                            "skipped": True,
                            "reason": "already_processed",
                            "created": False,
                            "event_inserted": False,
                        }
                        continue
                    results[index] = engine.process_signal(
                        item,
                        conn=conn,
                        metrics_cache=metrics_cache,
                        check_processed=False,
                    )
                refresh_ts = int(time.time())
                for stale_lifecycle in active_lifecycles:
                    active_symbol = normalize_lifecycle_symbol(stale_lifecycle.get("symbol"))
                    current_lifecycle = store.get_lifecycle(active_symbol, conn=conn) or stale_lifecycle
                    pair = active_pair_keys.get(active_symbol, (active_symbol, "15m"))
                    refresh_results.append(
                        engine.refresh_active_lifecycle(
                            current_lifecycle,
                            metrics_cache.get(pair) or _unavailable_metrics(
                                pair[0], pair[1], RuntimeError("missing prefetched metrics")
                            ),
                            conn=conn,
                            now_ts=refresh_ts,
                        )
                    )

    for result in [*results, *refresh_results]:
        if result is None:
            continue
        if result.get("skipped"):
            if result.get("active_refresh"):
                counts["refresh_skipped"] += 1
            else:
                counts["skipped"] += 1
            continue
        if result.get("refreshed"):
            counts["refreshed"] += 1
        if result.get("created"):
            counts["created"] += 1
        counts["events"] += safe_int(result.get("events_inserted"), 1 if result.get("event_inserted") else 0)
        result_events = result.get("events") if isinstance(result.get("events"), list) else []
        if not result_events and result.get("event"):
            result_events = [result.get("event")]
        for event in result_events:
            if not isinstance(event, dict) or not event:
                continue
            events.append(event)
            if push and not dry_run and str(event.get("event_type") or "") in IMPORTANT_TELEGRAM_EVENTS:
                pushed = push_lifecycle_event(
                    settings=loaded,
                    lifecycle=result.get("lifecycle") or {},
                    event=event,
                    send=bool(send and confirm_real_send),
                )
                if pushed:
                    counts["telegram"] += 1
                    if not dry_run and event.get("id"):
                        store.mark_event_pushed(int(event["id"]))
    return {
        "ok": True,
        "counts": counts,
        "events": events[:20],
        "settings": {
            "db_path": str(getattr(loaded, "lifecycle_db_path", loaded.data_dir / "lifecycle.db")),
            "lookback_hours": int(lookback_hours or 24),
            "limit_symbols": limit,
            "symbol": normalize_lifecycle_symbol(symbol),
        },
        "message": "生命周期扫描 dry-run 完成" if dry_run else "生命周期扫描完成",
    }


def backfill_lifecycles(
    *,
    settings: Settings | None = None,
    lookback_hours: int = 168,
    dry_run: bool = False,
    metrics_provider: MetricsProvider | None = None,
) -> dict[str, Any]:
    loaded = settings or Settings.load()
    return scan_lifecycles(
        settings=loaded,
        lookback_hours=lookback_hours,
        limit_symbols=min(80, max(1, int(getattr(loaded, "lifecycle_active_max_symbols", 80) or 80))),
        dry_run=dry_run,
        push=False,
        metrics_provider=metrics_provider,
    )


def lifecycle_status_payload(*, settings: Settings | None = None, symbol: str = "") -> dict[str, Any]:
    loaded = settings or Settings.load()
    store = LifecycleStore(getattr(loaded, "lifecycle_db_path", loaded.data_dir / "lifecycle.db"))
    lifecycle = store.get_lifecycle(symbol)
    normalized = normalize_lifecycle_symbol(symbol)
    return {
        "ok": True,
        "symbol": normalized,
        "lifecycle": enrich_lifecycle_display(lifecycle) if lifecycle else None,
        "events": [enrich_event_display(item) for item in store.list_events(symbol=normalized, limit=20)] if lifecycle else [],
        "not_advice": NOT_ADVICE,
    }


def lifecycle_report_text(result: dict[str, Any]) -> str:
    counts = result.get("counts") or {}
    lines = [
        "信号生命周期扫描",
        f"候选信号: {counts.get('signals', 0)}",
        f"新增生命周期: {counts.get('created', 0)}",
        f"新增事件: {counts.get('events', 0)}",
        f"跳过: {counts.get('skipped', 0)}",
        f"生命周期推送: {counts.get('telegram', 0)}",
    ]
    if counts.get("dry_run"):
        lines.append("模式: dry-run，未写入 lifecycle.db。")
    for event in (result.get("events") or [])[:10]:
        lines.append(f"- {event.get('symbol')} {EVENT_LABELS.get(str(event.get('event_type')), event.get('event_type'))}: {event.get('event_level') or '-'}")
    return "\n".join(lines)


def enrich_lifecycle_display(item: dict[str, Any] | None) -> dict[str, Any]:
    if not item:
        return {}
    state = str(item.get("current_state") or "")
    enriched = dict(item)
    enriched["state_label"] = STATE_LABELS.get(state, state or "未识别")
    enriched["coin"] = coin_from_symbol(str(item.get("symbol") or ""))
    enriched["futures_cvd_status"] = _cvd_status(item.get("latest_futures_cvd_15m"))
    enriched["spot_cvd_status"] = _cvd_status(item.get("latest_spot_cvd_15m"), spot=True)
    enriched["funding_status"] = _funding_status(item.get("latest_funding_rate"))
    enriched["not_advice"] = NOT_ADVICE
    return redact_api_payload(enriched)


def enrich_event_display(item: dict[str, Any]) -> dict[str, Any]:
    event_type = str(item.get("event_type") or "")
    enriched = dict(item)
    enriched["event_label"] = EVENT_LABELS.get(event_type, event_type)
    enriched["state_label"] = STATE_LABELS.get(str(item.get("new_state") or ""), str(item.get("new_state") or ""))
    return redact_api_payload(enriched)


def _cvd_status(value: Any, *, spot: bool = False) -> str:
    number = safe_float(value)
    if number is None:
        return "数据不足"
    if number > 0:
        return "现货买盘跟随" if spot else "主动买入增强"
    if number < 0:
        return "现货主动卖出" if spot else "主动卖出增强"
    return "主动量中性"


def _funding_status(value: Any) -> str:
    number = safe_float(value)
    if number is None:
        return "数据不足"
    if number >= 0.0008:
        return "资金费率偏热"
    if number <= -0.0008:
        return "资金费率偏负"
    return "未明显拥挤"


def build_lifecycle_telegram_message(lifecycle: dict[str, Any], event: dict[str, Any]) -> str:
    symbol = str(event.get("symbol") or lifecycle.get("symbol") or "")
    event_label = EVENT_LABELS.get(str(event.get("event_type") or ""), str(event.get("event_type") or "生命周期更新"))
    state_label = STATE_LABELS.get(str(event.get("new_state") or lifecycle.get("current_state") or ""), str(event.get("new_state") or "-"))
    reasons = event.get("reasons") if isinstance(event.get("reasons"), list) else []
    metrics = event.get("metrics") if isinstance(event.get("metrics"), dict) else {}
    price_change = _first_present(metrics.get("price_change_from_first_pct"), event.get("price_change_from_first_pct"))
    oi_change = _first_present(metrics.get("oi_change_from_first_pct"), event.get("oi_change_pct"))
    futures_cvd = _first_present(metrics.get("futures_cvd_delta"), event.get("futures_cvd_delta"))
    spot_cvd = _first_present(metrics.get("spot_cvd_delta"), event.get("spot_cvd_delta"))
    funding = _first_present(metrics.get("funding_rate"), event.get("funding_rate"))
    funding_number = safe_float(funding)
    funding_text = _funding_status(funding)
    if funding_number is not None:
        funding_text = f"{funding_number * 100:+.4f}%\uFF0C{funding_text}"
    risk_reasons = [
        str(item)
        for item in reasons
        if any(token in str(item) for token in ("风险", "拥挤", "走弱", "失败", "背离", "过热", "下跌"))
    ]
    lines = [
        f"🧬 生命周期跟随 {symbol}",
        "",
        f"阶段：{state_label}",
        f"事件：{event_label}",
        f"首信号：{lifecycle.get('first_signal_level') or '-'}｜{lifecycle.get('first_signal_at') or '-'}",
        f"当前信号：{event.get('event_level') or '-'}｜{event.get('event_time') or '-'}",
        "",
        "Binance 跟随：",
        f"价格：{_fmt_pct(price_change)}",
        f"成交量：{_first_present(metrics.get('volume_multiplier'), '-')}x",
        f"OI：{_fmt_pct(oi_change)}",
        f"合约 CVD：{_cvd_status(futures_cvd)}",
        f"现货 CVD：{_cvd_status(spot_cvd, spot=True)}",
        f"资金费率：{funding_text}",
        "",
        "判断：",
        "；".join(str(item) for item in reasons[:3]) if reasons else "生命周期状态已更新。",
        "",
        "风险：",
        "；".join(risk_reasons[:2]) if risk_reasons else "继续观察 OI、价格、现货 CVD 与资金费率是否出现背离。",
        "",
        NOT_ADVICE,
    ]
    return "\n".join(lines)


def _fmt_pct(value: Any) -> str:
    number = safe_float(value)
    if number is None:
        return "-"
    return f"{number:+.2f}%"


def _first_present(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def _lifecycle_telegram_dedup_key(event: dict[str, Any]) -> str:
    symbol = normalize_lifecycle_symbol(event.get("symbol")) or str(event.get("symbol") or "UNKNOWN").upper()
    event_type = str(event.get("event_type") or "lifecycle_update")
    level = str(event.get("event_level") or "unknown")
    return f"lifecycle:{symbol}:{event_type}:{level}"


def _lifecycle_telegram_rate_limit_reason(
    history: list[dict[str, Any]],
    *,
    symbol: str,
    now_ts: int | None = None,
) -> str:
    now = int(time.time() if now_ts is None else now_ts)
    cutoff = now - 3600
    recent = [
        item
        for item in history
        if isinstance(item, dict)
        and item.get("template_id") == "TG_LIFECYCLE_FOLLOWUP"
        and item.get("status") == "sent"
        and safe_int(item.get("ts"), 0) >= cutoff
    ]
    if len(recent) >= LIFECYCLE_TELEGRAM_GLOBAL_HOURLY_LIMIT:
        return "lifecycle_global_hourly_limit"
    normalized = normalize_lifecycle_symbol(symbol) or str(symbol or "").upper()
    prefix = f"lifecycle:{normalized}:"
    symbol_count = sum(1 for item in recent if str(item.get("dedup_key") or "").startswith(prefix))
    if symbol_count >= LIFECYCLE_TELEGRAM_SYMBOL_HOURLY_LIMIT:
        return "lifecycle_symbol_hourly_limit"
    return ""


def push_lifecycle_event(
    *,
    settings: Settings,
    lifecycle: dict[str, Any],
    event: dict[str, Any],
    send: bool = False,
    gateway: TelegramGateway | None = None,
) -> bool:
    if not bool(getattr(settings, "lifecycle_telegram_enable", False)):
        return False
    if str(event.get("event_type") or "") not in IMPORTANT_TELEGRAM_EVENTS:
        return False
    if safe_float(event.get("event_score")) is not None and (safe_float(event.get("event_score")) or 0) < float(getattr(settings, "lifecycle_telegram_min_score", 60) or 60):
        if str(event.get("event_type")) not in {"risk_warning", "launch_failed", "short_term_weakening", "first_signal"}:
            return False
    gw = gateway or TelegramGateway(settings, JsonStore(settings.data_dir))
    if send:
        try:
            history = gw._load_history()  # noqa: SLF001 - same package, read-only rate-limit ledger
        except Exception:
            history = []
        if _lifecycle_telegram_rate_limit_reason(
            history if isinstance(history, list) else [],
            symbol=str(event.get("symbol") or lifecycle.get("symbol") or ""),
        ):
            return False
    text = build_lifecycle_telegram_message(lifecycle, event)
    result = gw.send(
        text,
        "TG_LIFECYCLE_FOLLOWUP",
        _lifecycle_telegram_dedup_key(event),
        send=send,
        confirm_real_send=send,
        cooldown_sec=max(
            LIFECYCLE_TELEGRAM_EVENT_COOLDOWN_SEC,
            int(getattr(settings, "lifecycle_telegram_min_event_interval_sec", 3600) or 3600),
        ),
        daily_limit=None,
        parse_mode="Markdown",
    )
    return bool(result.sent)


def lifecycle_payload_for_json(result: dict[str, Any]) -> str:
    return json.dumps(redact_api_payload(result), ensure_ascii=False, indent=2)


def lifecycle_db_path(settings: Settings) -> Path:
    return Path(getattr(settings, "lifecycle_db_path", settings.data_dir / "lifecycle.db"))
