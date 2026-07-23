from __future__ import annotations

import html
import time
from pathlib import Path
from typing import Any

from .market_cockpit import MarketSnapshotStore, build_market_cockpit
from .realtime_intelligence import build_realtime_intelligence
from .realtime_market import RealtimeFeatureStore
from .runtime_cache import get_or_set as runtime_cache_get_or_set


BOT_MARKET_CONTEXT_SCHEMA_VERSION = "bot.market-context.v3"
BOT_MARKET_CONTEXT_TTL_SEC = 15
BOT_MARKET_WINDOW_SEC = 900
BOT_MARKET_SOURCES = {
    "binance_futures_batch",
    "market_flow_15m",
}


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
        "boundary": "仅追加本地封闭窗口市场事实，不改变原 BOT 模块的触发阈值、去重或冷却。",
    }


def _load_realtime_payload(settings: Any, *, now_ts: int | None = None) -> dict[str, Any]:
    raw_path = getattr(settings, "realtime_features_db_path", None)
    if raw_path in (None, ""):
        return {}
    path = Path(raw_path)
    if not path.exists():
        return {}
    now = int(now_ts or time.time())

    def build() -> dict[str, Any]:
        rows = [
            row
            for row in RealtimeFeatureStore(path).recent_rows(now_ts=now, window_sec=86_400)
            if str(row.get("exchange") or "").lower() == "binance"
        ]
        return build_realtime_intelligence(rows, now_ts=now, limit=30, include_backtest=False)

    if now_ts is not None:
        return build()
    try:
        return runtime_cache_get_or_set(f"bot:market-context:{path}", BOT_MARKET_CONTEXT_TTL_SEC, build)
    except Exception:
        return {}


def _load_market_contexts(
    settings: Any,
    symbols: list[str],
    *,
    now_ts: int | None = None,
) -> dict[str, dict[str, Any]]:
    raw_path = getattr(settings, "market_snapshots_db_path", None)
    if raw_path in (None, ""):
        return {}
    path = Path(raw_path)
    if not path.exists():
        return {}
    now = int(now_ts or time.time())

    def build() -> dict[str, dict[str, Any]]:
        comparisons = MarketSnapshotStore(path).comparisons(
            now_ts=now,
            window_secs=(BOT_MARKET_WINDOW_SEC,),
            max_symbols=len(symbols),
            symbols=symbols,
        )
        latest, baselines = comparisons.get(BOT_MARKET_WINDOW_SEC, ([], {}))
        latest = [
            item
            for item in latest
            if str(item.get("source") or "") in BOT_MARKET_SOURCES
        ]
        allowed_symbols = {
            str(item.get("symbol") or "")
            for item in latest
        }
        baselines = {
            symbol: item
            for symbol, item in baselines.items()
            if symbol in allowed_symbols
            and str(item.get("source") or "") in BOT_MARKET_SOURCES
        }
        payload = build_market_cockpit(
            latest,
            baselines,
            now_ts=now,
            window_sec=BOT_MARKET_WINDOW_SEC,
            board_limit=3,
        )
        return {
            _symbol(item.get("symbol") or item.get("coin")): {
                "window_sec": BOT_MARKET_WINDOW_SEC,
                "price_change_pct": _number(item.get("price_change_pct")),
                "oi_change_pct": _number(item.get("oi_change_pct")),
                "spot_flow_usd": _number(item.get("spot_flow_usd")),
                "futures_flow_usd": _number(item.get("futures_flow_usd")),
                "funding_pct": _number(item.get("funding_pct")),
                "age_sec": int(_number(item.get("age_sec")) or 0),
                "status": str(item.get("status") or "unavailable"),
            }
            for item in payload.get("assets", [])
            if isinstance(item, dict) and str(item.get("status") or "") != "stale"
        }

    try:
        if now_ts is not None:
            all_contexts = build()
        else:
            symbol_key = ",".join(sorted(symbols))
            all_contexts = runtime_cache_get_or_set(
                f"bot:funds-context:{path}:{symbol_key}",
                BOT_MARKET_CONTEXT_TTL_SEC,
                build,
            )
        return {symbol: all_contexts[symbol] for symbol in symbols if symbol in all_contexts}
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
    payload = _load_realtime_payload(settings, now_ts=now_ts)
    by_symbol = {
        _symbol(item.get("symbol") or item.get("coin")): item
        for item in payload.get("items", [])
        if isinstance(item, dict)
    }
    markets = _load_market_contexts(settings, symbols, now_ts=now_ts)
    contexts: list[dict[str, Any]] = []
    for symbol in symbols:
        realtime = by_symbol.get(symbol)
        market = markets.get(symbol)
        if realtime is None and market is None:
            continue
        context = build_bot_market_context(realtime or {"symbol": symbol})
        if market is not None:
            context["market"] = market
        contexts.append(context)
    return contexts


def closed_market_contexts_for_symbols(
    settings: Any,
    symbols: list[str],
    *,
    now_ts: int | None = None,
) -> dict[str, dict[str, Any]]:
    """Return Binance-backed closed 15m market facts without the Telegram 3-symbol cap."""

    normalized = list(dict.fromkeys(
        symbol
        for symbol in (_symbol(value) for value in symbols)
        if symbol
    ))
    if not normalized:
        return {}
    return _load_market_contexts(settings, normalized, now_ts=now_ts)


def _direction_label(value: Any) -> str:
    return {"long": "偏多", "short": "偏空", "neutral": "中性"}.get(str(value or "neutral"), "中性")


def _money(value: Any) -> str:
    number = _number(value)
    if number is None:
        return ""
    absolute = abs(number)
    if absolute >= 1_000_000_000:
        rendered = f"${absolute / 1_000_000_000:.2f}B"
    elif absolute >= 1_000_000:
        rendered = f"${absolute / 1_000_000:.2f}M"
    elif absolute >= 1_000:
        rendered = f"${absolute / 1_000:.1f}K"
    else:
        rendered = f"${absolute:.0f}"
    return f"{'+' if number >= 0 else '-'}{rendered}"


def _context_lines(context: dict[str, Any]) -> list[str]:
    symbol = html.escape(str(context.get("symbol") or ""))
    coin = symbol[:-4] if symbol.endswith("USDT") else symbol
    cvd = _number(context.get("cvd_ratio_5m_pct"))
    surge = context.get("surge") if isinstance(context.get("surge"), dict) else {}
    ambush = context.get("ambush") if isinstance(context.get("ambush"), dict) else {}
    resonance = context.get("resonance") if isinstance(context.get("resonance"), dict) else {}
    parts = [f"<b>{html.escape(coin)}</b>"]
    if cvd is not None:
        parts.append(f"5m合约主动净占比 {cvd:+.2f}%")
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
    lines = ["｜".join(parts)]
    market = context.get("market") if isinstance(context.get("market"), dict) else {}
    market_parts: list[str] = []
    spot_flow = _money(market.get("spot_flow_usd"))
    futures_flow = _money(market.get("futures_flow_usd"))
    oi_change = _number(market.get("oi_change_pct"))
    funding = _number(market.get("funding_pct"))
    if spot_flow:
        market_parts.append(f"现货主动成交净额 {spot_flow}")
    if futures_flow:
        market_parts.append(f"合约主动成交净额 {futures_flow}")
    if oi_change is not None:
        market_parts.append(f"OI {oi_change:+.2f}%")
    if funding is not None:
        market_parts.append(f"费率 {funding:+.4f}%")
    if market_parts:
        lines.append("↳ 15m " + " · ".join(market_parts))
    return lines


def enrich_telegram_with_market_context(
    settings: Any,
    text: str,
    template_id: str,
    signal_records: list[dict[str, Any]] | None,
    *,
    now_ts: int | None = None,
) -> str:
    if template_id not in {
        "TG_RADAR_SUMMARY",
        "TG_LAUNCH_ALERT",
        "TG_FLOW_RADAR",
        "TG_FUNDING_ALERT",
        "TG_ANNOUNCEMENT_ALERT",
    }:
        return text
    contexts = bot_market_contexts_for_records(settings, list(signal_records or []), now_ts=now_ts)
    if not contexts:
        return text
    block = [
        "",
        "<blockquote><b>BOT Binance 原生数据确认</b></blockquote>",
        *[line for context in contexts for line in _context_lines(context)],
        "<i>来源: Binance Spot + Binance USDⓈ-M Futures；仅代表 Binance 市场。</i>",
        "<i>计算: 主动成交净额=taker主动买入报价额-taker主动卖出报价额；主动净占比=主动成交净额/总成交额；OI变化=(窗口末OI-窗口初OI)/窗口初OI。</i>",
        "<i>只采用实时或已闭合窗口行情，不采用新闻、社交情报或 CoinGlass/Coinalyze；不改变本模块原触发阈值；不构成投资建议。</i>",
    ]
    return f"{text.rstrip()}\n" + "\n".join(block)


__all__ = [
    "BOT_MARKET_CONTEXT_SCHEMA_VERSION",
    "bot_market_contexts_for_records",
    "build_bot_market_context",
    "closed_market_contexts_for_symbols",
    "enrich_telegram_with_market_context",
]
