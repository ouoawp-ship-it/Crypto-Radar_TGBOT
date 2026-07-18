from __future__ import annotations

import html
import time
from pathlib import Path
from typing import Any

from .realtime_intelligence import build_realtime_intelligence
from .realtime_market import RealtimeFeatureStore
from .runtime_cache import get_or_set as runtime_cache_get_or_set


BOT_MARKET_CONTEXT_SCHEMA_VERSION = "bot.market-context.v1"
BOT_MARKET_CONTEXT_TTL_SEC = 15


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in {float("inf"), float("-inf")} else None


def _symbol(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    return text if text.endswith("USDT") else f"{text}USDT"


def build_bot_market_context(item: dict[str, Any]) -> dict[str, Any]:
    windows = item.get("windows") if isinstance(item.get("windows"), dict) else {}
    five_minute = windows.get("5m") if isinstance(windows.get("5m"), dict) else {}
    surge = item.get("surge") if isinstance(item.get("surge"), dict) else {}
    ambush = item.get("ambush") if isinstance(item.get("ambush"), dict) else {}
    resonance = item.get("resonance") if isinstance(item.get("resonance"), dict) else {}
    anomaly = item.get("anomaly_24h") if isinstance(item.get("anomaly_24h"), dict) else {}
    lifecycle = item.get("lifecycle") if isinstance(item.get("lifecycle"), dict) else {}
    return {
        "schema_version": BOT_MARKET_CONTEXT_SCHEMA_VERSION,
        "symbol": _symbol(item.get("symbol") or item.get("coin")),
        "observed_at": str(item.get("observed_at") or ""),
        "data_status": str(item.get("data_status") or "unavailable"),
        "cvd_ratio_5m_pct": _number(five_minute.get("cvd_ratio_pct")),
        "gross_trade_5m_usd": _number(five_minute.get("gross_trade_usd")),
        "surge": {
            "triggered": bool(surge.get("triggered")),
            "direction": str(surge.get("direction") or "neutral"),
            "score": _number(surge.get("score")),
        },
        "ambush": {
            "triggered": bool(ambush.get("triggered")),
            "direction": str(ambush.get("direction") or "neutral"),
            "score": _number(ambush.get("score")),
        },
        "resonance": {
            "direction": str(resonance.get("direction") or "neutral"),
            "active_count": int(_number(resonance.get("active_count")) or 0),
            "window_count": int(_number(resonance.get("window_count")) or 5),
        },
        "anomaly_count_24h": int(_number(anomaly.get("count")) or 0),
        "lifecycle": {
            "state": str(lifecycle.get("state") or "inactive"),
            "label": str(lifecycle.get("label") or "未触发"),
        },
        "boundary": "仅追加 Web 工作站封闭窗口事实，不改变原 BOT 模块的触发阈值、去重或冷却。",
    }


def _load_payload(settings: Any, *, now_ts: int | None = None) -> dict[str, Any]:
    raw_path = getattr(settings, "realtime_features_db_path", None)
    if raw_path in (None, ""):
        return {}
    path = Path(raw_path)
    if not path.exists():
        return {}
    now = int(now_ts or time.time())

    def build() -> dict[str, Any]:
        rows = RealtimeFeatureStore(path).recent_rows(now_ts=now, window_sec=86_400)
        return build_realtime_intelligence(rows, now_ts=now, limit=30, include_backtest=False)

    if now_ts is not None:
        return build()
    try:
        return runtime_cache_get_or_set(f"bot:market-context:{path}", BOT_MARKET_CONTEXT_TTL_SEC, build)
    except Exception:
        return {}


def bot_market_contexts_for_records(
    settings: Any,
    records: list[dict[str, Any]],
    *,
    now_ts: int | None = None,
) -> list[dict[str, Any]]:
    symbols: list[str] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        symbol = _symbol(record.get("symbol") or record.get("coin"))
        if symbol and symbol not in symbols:
            symbols.append(symbol)
        if len(symbols) >= 3:
            break
    if not symbols:
        return []
    payload = _load_payload(settings, now_ts=now_ts)
    by_symbol = {
        _symbol(item.get("symbol") or item.get("coin")): item
        for item in payload.get("items", [])
        if isinstance(item, dict)
    }
    return [build_bot_market_context(by_symbol[symbol]) for symbol in symbols if symbol in by_symbol]


def _direction_label(value: Any) -> str:
    return {"long": "偏多", "short": "偏空", "neutral": "中性"}.get(str(value or "neutral"), "中性")


def _context_line(context: dict[str, Any]) -> str:
    symbol = html.escape(str(context.get("symbol") or ""))
    coin = symbol[:-4] if symbol.endswith("USDT") else symbol
    cvd = _number(context.get("cvd_ratio_5m_pct"))
    surge = context.get("surge") if isinstance(context.get("surge"), dict) else {}
    ambush = context.get("ambush") if isinstance(context.get("ambush"), dict) else {}
    resonance = context.get("resonance") if isinstance(context.get("resonance"), dict) else {}
    parts = [f"<b>{html.escape(coin)}</b>"]
    if cvd is not None:
        parts.append(f"5m CVD {cvd:+.2f}%")
    if surge.get("triggered"):
        parts.append(f"Surge {_direction_label(surge.get('direction'))} {float(surge.get('score') or 0):.1f}")
    elif ambush.get("triggered"):
        parts.append(f"埋伏 {_direction_label(ambush.get('direction'))} {float(ambush.get('score') or 0):.1f}")
    active = int(_number(resonance.get("active_count")) or 0)
    total = int(_number(resonance.get("window_count")) or 5)
    if active:
        parts.append(f"五窗 {active}/{total} {_direction_label(resonance.get('direction'))}")
    count = int(_number(context.get("anomaly_count_24h")) or 0)
    if count:
        parts.append(f"24h 异动 {count}次")
    return "｜".join(parts)


def enrich_telegram_with_market_context(
    settings: Any,
    text: str,
    template_id: str,
    signal_records: list[dict[str, Any]] | None,
    *,
    now_ts: int | None = None,
) -> str:
    if template_id not in {"TG_LAUNCH_ALERT", "TG_FLOW_RADAR", "TG_FUNDING_ALERT", "TG_ANNOUNCEMENT_ALERT"}:
        return text
    contexts = bot_market_contexts_for_records(settings, list(signal_records or []), now_ts=now_ts)
    if not contexts:
        return text
    block = [
        "",
        "<blockquote><b>Web 市场事实增强</b></blockquote>",
        *[_context_line(context) for context in contexts],
        "<i>封闭窗口参考，不改变本模块原触发阈值；不构成投资建议。</i>",
    ]
    return f"{text.rstrip()}\n" + "\n".join(block)


__all__ = [
    "BOT_MARKET_CONTEXT_SCHEMA_VERSION",
    "bot_market_contexts_for_records",
    "build_bot_market_context",
    "enrich_telegram_with_market_context",
]
