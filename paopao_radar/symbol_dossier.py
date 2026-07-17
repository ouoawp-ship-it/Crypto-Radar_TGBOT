from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Any

from .config import Settings
from .data_sources import BinanceDataSource
from .funding_sources import (
    MultiExchangeFundingClient,
    funding_cycle_text,
    funding_last_settlement_text,
    funding_settlement_period_text,
    to_float,
    to_int,
)
from .price_alerts import format_price, normalize_symbol
from .storage import JsonStore


CST = timezone(timedelta(hours=8))
MAX_LEGACY_SIGNAL_HISTORY_ITEMS = 500

TEMPLATE_LABELS = {
    "TG_LAUNCH_ALERT": "启动雷达",
    "TG_FLOW_RADAR": "资金流雷达",
    "TG_FUNDING_ALERT": "资金费率警报",
    "TG_RADAR_SUMMARY": "资金摘要",
    "TG_ANNOUNCEMENT_ALERT": "公告风险",
}
ACTIVE_SIGNAL_TEMPLATE_IDS = frozenset((*TEMPLATE_LABELS, "TG_TEST_MESSAGE"))

SYMBOL_ALIASES = {
    "比特币": "BTC",
    "大饼": "BTC",
    "以太坊": "ETH",
    "以太": "ETH",
    "币安币": "BNB",
    "索拉纳": "SOL",
    "狗狗币": "DOGE",
    "狗狗": "DOGE",
}

SYMBOL_STOP_WORDS = {
    "AI",
    "API",
    "APP",
    "BOT",
    "CST",
    "UTC",
    "WEB",
    "TV",
    "USD",
    "USDT",
    "OI",
    "CVD",
    "K",
    "H",
    "M",
    "VIP",
    "OK",
    "OKX",
    "BYBIT",
    "BITGET",
    "GATE",
    "BINANCE",
    "COINGLASS",
    "COINPAPRIKA",
}

DOSSIER_INTENT_RE = re.compile(
    r"(查|查询|分析|怎么看|看法|雷达档案|币种档案|档案|多空|方向|走势|趋势|值得|适合|做多|做空|能多|能空|可以多|可以空)",
    re.IGNORECASE,
)


def pct_change(current: float, previous: float) -> float | None:
    if previous <= 0:
        return None
    return (current - previous) / previous * 100


def fmt_pct(value: Any, digits: int = 1) -> str:
    if value is None:
        return "暂无"
    try:
        return f"{float(value):+.{digits}f}%"
    except (TypeError, ValueError):
        return "暂无"


def fmt_money(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = 0.0
    if number <= 0:
        return "暂无"
    if number >= 1_000_000_000:
        return f"${number / 1_000_000_000:.1f}B"
    if number >= 1_000_000:
        return f"${number / 1_000_000:.0f}M"
    if number >= 1_000:
        return f"${number / 1_000:.0f}K"
    return f"${number:.0f}"


def market_cap_tier(value: Any) -> str:
    cap = to_float(value)
    if cap <= 0:
        return "未知市值"
    if cap >= 10_000_000_000:
        return "高市值"
    if cap >= 1_000_000_000:
        return "中市值"
    return "低市值"


def liquidity_tier(value: Any) -> str:
    volume = to_float(value)
    if volume <= 0:
        return "未知流动性"
    if volume >= 100_000_000:
        return "高流动性"
    if volume >= 20_000_000:
        return "中流动性"
    return "低流动性"


def cst_time_text(ts: int) -> str:
    if ts <= 0:
        return "未知时间"
    return datetime.fromtimestamp(ts, CST).strftime("%m-%d %H:%M CST")


def clean_signal_text(text: str) -> str:
    cleaned = unescape(str(text or ""))
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 \2", cleaned)
    cleaned = re.sub(r"`([^`]*)`", r"\1", cleaned)
    cleaned = re.sub(r"[*_#]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _safe_normalize_symbol(value: str) -> str:
    try:
        symbol = normalize_symbol(value)
    except ValueError:
        return ""
    coin = symbol[:-4] if symbol.endswith("USDT") else symbol
    if coin in SYMBOL_STOP_WORDS or len(coin) < 2:
        return ""
    return symbol


def extract_symbols_from_text(text: str) -> list[str]:
    raw = str(text or "")
    found: list[str] = []

    def add(value: str) -> None:
        symbol = _safe_normalize_symbol(value)
        if symbol and symbol not in found:
            found.append(symbol)

    for alias, symbol in SYMBOL_ALIASES.items():
        if alias in raw:
            add(symbol)
    for match in re.finditer(r"Binance[_\-/]([A-Za-z0-9]{2,24}USDT)", raw, flags=re.IGNORECASE):
        add(match.group(1))
    for match in re.finditer(r"\b([A-Za-z0-9]{2,24}USDT)\b", raw, flags=re.IGNORECASE):
        add(match.group(1))
    for match in re.finditer(r"\[([A-Za-z][A-Za-z0-9]{1,20})\]", raw):
        add(match.group(1))
    if not found:
        cleaned = clean_signal_text(raw)
        for match in re.finditer(r"\b([A-Za-z][A-Za-z0-9]{1,20})\b", cleaned):
            token = match.group(1).upper()
            if token not in SYMBOL_STOP_WORDS:
                add(token)
                if found:
                    break
    return found[:12]


def extract_symbol_from_query(text: str) -> str:
    clean = str(text or "").strip()
    for alias, symbol in SYMBOL_ALIASES.items():
        if alias in clean:
            return normalize_symbol(symbol)
    symbols = extract_symbols_from_text(clean)
    return symbols[0] if symbols else ""


def is_symbol_dossier_request(text: str) -> bool:
    clean = str(text or "").strip()
    if not clean:
        return False
    if not extract_symbol_from_query(clean):
        return False
    return bool(DOSSIER_INTENT_RE.search(clean))


def signal_event_template_label(template_id: str) -> str:
    return TEMPLATE_LABELS.get(str(template_id or ""), str(template_id or "未知信号"))


def extract_signal_events_from_push(
    *,
    template_id: str,
    dedup_key: str,
    status: str,
    sent: bool,
    text: str,
    ts: int | None = None,
    topic_id: str = "",
    message_ids: list[int] | None = None,
    reply_to_message_id: int | None = None,
) -> list[dict[str, Any]]:
    symbols = extract_symbols_from_text(text)
    if not symbols:
        return []
    now = int(ts or time.time())
    clean_excerpt = clean_signal_text(text)[:1200]
    return [
        {
            "source": "telegram_push",
            "ts": now,
            "time": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "symbol": symbol,
            "coin": symbol[:-4] if symbol.endswith("USDT") else symbol,
            "template_id": template_id,
            "signal_type": signal_event_template_label(template_id),
            "dedup_key": dedup_key,
            "status": status,
            "sent": bool(sent),
            "topic_id": str(topic_id or ""),
            "message_ids": list(message_ids or []),
            "reply_to_message_id": int(reply_to_message_id or 0),
            "excerpt": clean_excerpt,
        }
        for symbol in symbols
    ]


def append_signal_events(
    settings: Settings,
    store: JsonStore,
    events: list[dict[str, Any]],
) -> int:
    if not events:
        return 0
    now = int(time.time())
    cutoff = now - max(1, int(settings.signal_events_retention_days)) * 86400
    limit = min(MAX_LEGACY_SIGNAL_HISTORY_ITEMS, max(100, int(settings.signal_events_limit)))

    def append(current: Any) -> list[dict[str, Any]]:
        records = current if isinstance(current, list) else []
        retained = [
            record for record in records
            if isinstance(record, dict) and int(record.get("ts", now) or now) >= cutoff
        ]
        retained.extend(events)
        return retained[-limit:]

    store.update(settings.signal_events_path, append, [])
    return len(events)


def append_signal_events_from_push(
    settings: Settings,
    store: JsonStore,
    *,
    template_id: str,
    dedup_key: str,
    status: str,
    sent: bool,
    text: str,
    ts: int | None = None,
    topic_id: str = "",
    message_ids: list[int] | None = None,
    reply_to_message_id: int | None = None,
) -> int:
    events = extract_signal_events_from_push(
        template_id=template_id,
        dedup_key=dedup_key,
        status=status,
        sent=sent,
        text=text,
        ts=ts,
        topic_id=topic_id,
        message_ids=message_ids,
        reply_to_message_id=reply_to_message_id,
    )
    return append_signal_events(settings, store, events)


def load_json_records(store: JsonStore, path: Any, default: Any) -> Any:
    try:
        return store.load(path, default)
    except Exception:
        return default


def launch_stage_from_score(settings: Settings, score: float) -> str:
    if score >= settings.launch_launched_score:
        return "启动瞬间"
    if score >= settings.launch_breakout_score:
        return "启动确认"
    if score >= settings.launch_primed_score:
        return "提前预警"
    if score >= settings.launch_watch_score:
        return "提前观察"
    return "未触发"


def event_from_push_record(record: dict[str, Any], symbol: str) -> dict[str, Any] | None:
    template_id = str(record.get("template_id") or "")
    if template_id and template_id not in TEMPLATE_LABELS:
        return None
    preview = str(record.get("preview") or "")
    if symbol not in extract_symbols_from_text(preview):
        return None
    return {
        "source": "tg_push_history",
        "ts": int(record.get("ts", 0) or 0),
        "time": record.get("time") or "",
        "symbol": symbol,
        "signal_type": signal_event_template_label(template_id),
        "template_id": template_id,
        "status": str(record.get("status") or ""),
        "sent": bool(record.get("sent")),
        "message_ids": record.get("message_ids") or [],
        "reply_to_message_id": int(record.get("reply_to_message_id", 0) or 0),
        "summary": clean_signal_text(preview)[:260],
    }


def launch_events(settings: Settings, store: JsonStore, symbol: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    state = load_json_records(store, settings.launch_state_path, {})
    if isinstance(state, dict) and isinstance(state.get(symbol), dict):
        item = state[symbol]
        events.append({
            "source": "launch_state",
            "ts": int(item.get("last_seen", item.get("updated_at", 0)) or 0),
            "symbol": symbol,
            "signal_type": "启动雷达当前状态",
            "stage": item.get("stage") or launch_stage_from_score(settings, to_float(item.get("score"))),
            "score": to_float(item.get("score")),
            "price_15m": item.get("price_15m"),
            "price_1h": item.get("price_1h"),
            "oi_15m": item.get("oi_15m"),
            "oi_1h": item.get("oi_1h"),
            "volume_ratio": item.get("volume_ratio"),
            "summary": "启动雷达仍在状态文件中跟踪这个币。",
        })
    records = load_json_records(store, settings.launch_watch_history_path, [])
    if isinstance(records, list):
        for record in records[-300:]:
            if not isinstance(record, dict):
                continue
            for item in record.get("items", []) if isinstance(record.get("items"), list) else []:
                if not isinstance(item, dict) or str(item.get("symbol") or "").upper() != symbol:
                    continue
                score = to_float(item.get("score"))
                events.append({
                    "source": "launch_watch_history",
                    "ts": int(item.get("ts", record.get("ts", 0)) or 0),
                    "symbol": symbol,
                    "signal_type": "启动雷达监控",
                    "stage": item.get("stage") or launch_stage_from_score(settings, score),
                    "score": score,
                    "price_15m": item.get("price_15m"),
                    "price_1h": item.get("price_1h"),
                    "oi_15m": item.get("oi_15m"),
                    "oi_1h": item.get("oi_1h"),
                    "volume_ratio": item.get("volume_ratio"),
                    "summary": (
                        f"分数 {score:g}，15m价格 {fmt_pct(item.get('price_15m'))}，"
                        f"1h价格 {fmt_pct(item.get('price_1h'))}，1h OI {fmt_pct(item.get('oi_1h'))}"
                    ),
                })
    return events


def funding_state_events(settings: Settings, store: JsonStore, symbol: str) -> list[dict[str, Any]]:
    state = load_json_records(store, settings.funding_alert_state_path, {})
    if not isinstance(state, dict):
        return []
    symbols = state.get("symbols", {}) if isinstance(state.get("symbols"), dict) else {}
    item = symbols.get(symbol)
    if not isinstance(item, dict):
        return []
    rows = item.get("exchanges", {}) if isinstance(item.get("exchanges"), dict) else {}
    rates = [
        f"{name} {funding_cycle_text(to_float(row.get('funding_pct')), to_int(row.get('interval_hours')))}"
        for name, row in rows.items()
        if isinstance(row, dict)
    ]
    return [{
        "source": "funding_alert_state",
        "ts": int(item.get("last_seen", item.get("updated_at", 0)) or 0),
        "symbol": symbol,
        "signal_type": "资金费率警报状态",
        "stage": item.get("stage"),
        "price_24h_pct": item.get("price_24h_pct"),
        "mcap": item.get("mcap"),
        "quote_volume": item.get("quote_volume"),
        "summary": f"阶段 {item.get('stage') or '未知'}；多交易所费率：{'; '.join(rates[:5]) or '暂无'}",
    }]


def signal_event_index_events(settings: Settings, store: JsonStore, symbol: str) -> list[dict[str, Any]]:
    events = load_json_records(store, settings.signal_events_path, [])
    result: list[dict[str, Any]] = []
    if isinstance(events, list):
        for event in events[-settings.signal_events_limit:]:
            if not isinstance(event, dict) or str(event.get("symbol") or "").upper() != symbol:
                continue
            template_id = str(event.get("template_id") or "")
            if template_id and template_id not in TEMPLATE_LABELS:
                continue
            result.append({
                **event,
                "summary": str(event.get("excerpt") or "")[:260],
            })
    history = load_json_records(store, settings.tg_push_history_path, [])
    if isinstance(history, list):
        for record in history[-500:]:
            if isinstance(record, dict):
                event = event_from_push_record(record, symbol)
                if event:
                    result.append(event)
    return result


def load_symbol_history(settings: Settings, symbol: str, store: JsonStore | None = None, limit: int = 30) -> list[dict[str, Any]]:
    loaded_store = store or JsonStore(settings.data_dir)
    events: list[dict[str, Any]] = []
    events.extend(signal_event_index_events(settings, loaded_store, symbol))
    events.extend(launch_events(settings, loaded_store, symbol))
    events.extend(funding_state_events(settings, loaded_store, symbol))

    deduped: dict[str, dict[str, Any]] = {}
    for event in events:
        key = "|".join([
            str(event.get("source") or ""),
            str(event.get("template_id") or event.get("signal_type") or ""),
            str(event.get("dedup_key") or ""),
            str(event.get("ts") or ""),
            str(event.get("summary") or "")[:80],
        ])
        deduped[key] = event
    ordered = sorted(deduped.values(), key=lambda item: int(item.get("ts", 0) or 0), reverse=True)
    return ordered[:limit]


def _ticker_item(source: BinanceDataSource, symbol: str) -> dict[str, Any]:
    try:
        for item in source.ticker_24h():
            if str(item.get("symbol") or "").upper() == symbol:
                return item if isinstance(item, dict) else {}
    except Exception:
        return {}
    return {}


def _premium_item(source: BinanceDataSource, symbol: str) -> dict[str, Any]:
    try:
        data = source.premium_index()
        if isinstance(data, dict):
            return data if str(data.get("symbol") or "").upper() == symbol else {}
        for item in data:
            if isinstance(item, dict) and str(item.get("symbol") or "").upper() == symbol:
                return item
    except Exception:
        return {}
    return {}


def _market_cap(source: BinanceDataSource, coin: str) -> tuple[float, str]:
    try:
        cap = to_float(source.market_caps().get(coin))
        if cap > 0:
            return cap, "Binance"
    except Exception:
        pass
    try:
        cap = to_float(source.coinpaprika_market_caps().get(coin))
        if cap > 0:
            return cap, "CoinPaprika"
    except Exception:
        pass
    return 0.0, ""


def _price_metrics(klines: list[list[Any]]) -> dict[str, Any]:
    closes = [to_float(item[4]) for item in klines if isinstance(item, list) and len(item) > 4]
    quote_volumes = [to_float(item[7]) for item in klines if isinstance(item, list) and len(item) > 7]
    if not closes:
        return {}
    latest = closes[-1]

    def change(steps: int) -> float | None:
        if len(closes) <= steps:
            return None
        return pct_change(latest, closes[-1 - steps])

    avg_volume = sum(quote_volumes[:-1]) / max(1, len(quote_volumes[:-1])) if len(quote_volumes) > 1 else 0.0
    return {
        "price": latest,
        "price_15m_pct": change(1),
        "price_1h_pct": change(4),
        "price_4h_pct": change(16),
        "volume_ratio": quote_volumes[-1] / avg_volume if avg_volume > 0 and quote_volumes else None,
    }


def _oi_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [
        to_float(item.get("sumOpenInterestValue") or item.get("sumOpenInterest"))
        for item in rows
        if isinstance(item, dict)
    ]
    values = [item for item in values if item > 0]
    if not values:
        return {}
    latest = values[-1]

    def change(steps: int) -> float | None:
        if len(values) <= steps:
            return None
        return pct_change(latest, values[-1 - steps])

    return {
        "oi_value": latest,
        "oi_15m_pct": change(1),
        "oi_1h_pct": change(4),
        "oi_4h_pct": change(16),
    }


def current_market_snapshot(settings: Settings, symbol: str, source: BinanceDataSource | None = None) -> dict[str, Any]:
    loaded_source = source or BinanceDataSource(settings)
    coin = symbol[:-4] if symbol.endswith("USDT") else symbol
    ticker = _ticker_item(loaded_source, symbol)
    premium = _premium_item(loaded_source, symbol)
    klines = loaded_source.klines(symbol, interval="15m", limit=64)
    oi_rows = loaded_source.open_interest_hist(symbol, period="15m", limit=17)
    mcap, mcap_source = _market_cap(loaded_source, coin)

    snapshot: dict[str, Any] = {
        "symbol": symbol,
        "coin": coin,
        "updated_at": int(time.time()),
        "price": to_float(ticker.get("lastPrice")) if ticker else 0.0,
        "price_24h_pct": to_float(ticker.get("priceChangePercent")) if ticker else None,
        "quote_volume": to_float(ticker.get("quoteVolume")) if ticker else 0.0,
        "market_cap": mcap,
        "market_cap_source": mcap_source,
        "market_cap_tier": market_cap_tier(mcap),
        "liquidity_tier": liquidity_tier(to_float(ticker.get("quoteVolume")) if ticker else 0.0),
        "funding_pct": to_float(premium.get("lastFundingRate")) * 100 if premium else None,
        "next_funding_time_ms": to_int(premium.get("nextFundingTime")) if premium else 0,
    }
    snapshot.update({key: value for key, value in _price_metrics(klines).items() if value is not None})
    if not snapshot.get("price"):
        snapshot["price"] = to_float(snapshot.get("price"))
    snapshot.update({key: value for key, value in _oi_metrics(oi_rows).items() if value is not None})
    http = getattr(loaded_source, "http", None)
    if http is not None:
        try:
            snapshot["funding_exchanges"] = MultiExchangeFundingClient(settings, http).snapshot(symbol, include_history=True)
        except Exception:
            snapshot["funding_exchanges"] = []
    snapshot["data_quality"] = loaded_source.diagnostics() if hasattr(loaded_source, "diagnostics") else {}
    return snapshot


def latest_funding_rows(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    rows = snapshot.get("funding_exchanges")
    return rows if isinstance(rows, list) else []


def rule_based_verdict(snapshot: dict[str, Any], history: list[dict[str, Any]]) -> dict[str, Any]:
    bullish: list[str] = []
    bearish: list[str] = []
    risks: list[str] = []

    price_1h = snapshot.get("price_1h_pct")
    price_4h = snapshot.get("price_4h_pct")
    oi_1h = snapshot.get("oi_1h_pct")
    funding_pct = snapshot.get("funding_pct")

    if price_1h is not None and oi_1h is not None:
        if price_1h >= 3 and oi_1h >= 3:
            bullish.append(f"1h价涨 {fmt_pct(price_1h)} 且 OI 增加 {fmt_pct(oi_1h)}，资金与价格同向。")
        elif price_1h >= 3 and oi_1h <= -2:
            risks.append(f"1h价格上涨但 OI 下降 {fmt_pct(oi_1h)}，可能是挤空后的减仓行情。")
        elif price_1h <= -3 and oi_1h >= 3:
            bearish.append(f"1h价跌 {fmt_pct(price_1h)} 且 OI 增加 {fmt_pct(oi_1h)}，偏空加仓压力。")
        elif price_1h <= -3 and oi_1h <= -2:
            risks.append(f"1h价跌且 OI 下降 {fmt_pct(oi_1h)}，可能是多头去杠杆后的释放。")
    if price_4h is not None:
        if price_4h >= 6:
            bullish.append(f"4h价格涨幅 {fmt_pct(price_4h)}，短线动量偏强。")
        elif price_4h <= -6:
            bearish.append(f"4h价格跌幅 {fmt_pct(price_4h)}，短线动量偏弱。")
    if funding_pct is not None:
        if funding_pct <= -0.5:
            bullish.append(f"Binance 资金费率 {fmt_pct(funding_pct, 3)} 极负，存在空头燃料。")
            risks.append("极负资金费率也代表杠杆拥挤，容易出现双向插针。")
        elif funding_pct >= 0.5:
            bearish.append(f"Binance 资金费率 {fmt_pct(funding_pct, 3)} 极正，多头拥挤偏高。")
            risks.append("极正资金费率下追多容易遇到多头兑现和清算回撤。")

    rows = latest_funding_rows(snapshot)
    negative_rows = [row for row in rows if to_float(row.get("funding_pct")) <= -0.5]
    positive_rows = [row for row in rows if to_float(row.get("funding_pct")) >= 0.5]
    if len(negative_rows) >= 2:
        bullish.append(f"{len(negative_rows)} 家交易所资金费率极负，多交易所空头拥挤。")
        risks.append("多所极负费率说明情绪极端，不适合盲目追单。")
    if len(positive_rows) >= 2:
        bearish.append(f"{len(positive_rows)} 家交易所资金费率极正，多头拥挤明显。")

    for event in history[:12]:
        signal_type = str(event.get("signal_type") or "")
        stage = str(event.get("stage") or "")
        score = to_float(event.get("score"))
        if "启动" in signal_type and score >= 75:
            bullish.append(f"历史启动雷达出现过 {stage or '高分信号'}，分数 {score:g}。")
        if "资金费率" in signal_type and "极负" in str(event.get("summary") or ""):
            bullish.append("历史资金费率警报提示过极负费率，可能有空头燃料。")

    bull_score = len(bullish)
    bear_score = len(bearish)
    risk_score = len(risks)
    if risk_score >= 4 and abs(bull_score - bear_score) <= 2:
        stance = "高风险观望"
    elif bull_score - bear_score >= 2:
        stance = "偏多"
    elif bear_score - bull_score >= 2:
        stance = "偏空"
    else:
        stance = "观望"
    return {
        "stance": stance,
        "bullish": bullish[:8],
        "bearish": bearish[:8],
        "risks": risks[:8],
        "score": {"bullish": bull_score, "bearish": bear_score, "risk": risk_score},
    }


def build_symbol_dossier(
    settings: Settings,
    query_or_symbol: str,
    *,
    store: JsonStore | None = None,
    source: BinanceDataSource | None = None,
) -> dict[str, Any]:
    symbol = extract_symbol_from_query(query_or_symbol)
    if not symbol:
        raise ValueError("没有识别到币种。示例：查 BTC、GWEI 怎么看、SOL 可以做多吗")
    loaded_store = store or JsonStore(settings.data_dir)
    history = load_symbol_history(settings, symbol, loaded_store)
    snapshot = current_market_snapshot(settings, symbol, source=source)
    verdict = rule_based_verdict(snapshot, history)
    return {
        "ok": True,
        "symbol": symbol,
        "coin": symbol[:-4] if symbol.endswith("USDT") else symbol,
        "snapshot": snapshot,
        "history": history,
        "verdict": verdict,
    }


def event_line(event: dict[str, Any]) -> str:
    when = cst_time_text(int(event.get("ts", 0) or 0))
    signal_type = str(event.get("signal_type") or event.get("template_id") or "历史信号")
    details: list[str] = []
    if event.get("stage"):
        details.append(f"阶段 {event.get('stage')}")
    if event.get("score") not in {None, ""}:
        details.append(f"分数 {to_float(event.get('score')):g}")
    if event.get("outcome") and event.get("outcome") != "pending":
        details.append(f"结果 {event.get('outcome')}")
    summary = str(event.get("summary") or event.get("excerpt") or "").strip()
    if summary and len(summary) > 110:
        summary = summary[:110] + "..."
    suffix = " | ".join(details)
    return f"- {when}｜{signal_type}{'｜' + suffix if suffix else ''}{'｜' + summary if summary else ''}"


def funding_rows_text(rows: list[dict[str, Any]]) -> list[str]:
    if not rows:
        return ["暂无多交易所资金费率快照"]
    lines = []
    for row in rows[:6]:
        exchange = str(row.get("exchange") or "Unknown")
        rate = funding_cycle_text(to_float(row.get("funding_pct")), to_int(row.get("interval_hours")))
        last_time = funding_last_settlement_text(row) or "未知"
        period = funding_settlement_period_text(row)
        next_time = str(row.get("next_funding_time") or "未知")
        label = str(row.get("extreme_label") or "")
        lines.append(f"{exchange}: {rate}{'（' + label + '）' if label else ''}｜上次 {last_time}｜周期 {period}｜下次 {next_time}")
    return lines


def format_symbol_dossier_report(dossier: dict[str, Any]) -> str:
    symbol = str(dossier.get("symbol") or "")
    snapshot = dossier.get("snapshot") if isinstance(dossier.get("snapshot"), dict) else {}
    verdict = dossier.get("verdict") if isinstance(dossier.get("verdict"), dict) else {}
    history = dossier.get("history") if isinstance(dossier.get("history"), list) else []
    requested = dossier.get("requested_signal") if isinstance(dossier.get("requested_signal"), dict) else {}
    lines = [f"{symbol} 币种雷达档案"]
    if requested:
        lines.extend([
            "",
            "本次引用信号",
            f"引用: {requested.get('public_ref') or requested.get('id') or '未知'}",
            f"类型: {requested.get('signal_type') or requested.get('module') or '未知'}",
            f"状态: {requested.get('stage') or requested.get('status') or '未知'}{('，分数 ' + str(requested.get('score'))) if requested.get('score') is not None else ''}",
            f"摘要: {str(requested.get('excerpt') or '')[:260] or '暂无'}",
        ])
    lines.extend([
        "",
        "当前状态",
        f"价格: {format_price(to_float(snapshot.get('price')))}",
        f"15m/1h/4h: {fmt_pct(snapshot.get('price_15m_pct'))} / {fmt_pct(snapshot.get('price_1h_pct'))} / {fmt_pct(snapshot.get('price_4h_pct'))}",
        f"24h: {fmt_pct(snapshot.get('price_24h_pct'))}，成交额 {fmt_money(snapshot.get('quote_volume'))}（{liquidity_tier(snapshot.get('quote_volume'))}）",
        f"市值: {fmt_money(snapshot.get('market_cap'))}（{snapshot.get('market_cap_tier') or '未知市值'}{('，来源 ' + snapshot.get('market_cap_source')) if snapshot.get('market_cap_source') else ''}）",
        f"OI 15m/1h/4h: {fmt_pct(snapshot.get('oi_15m_pct'))} / {fmt_pct(snapshot.get('oi_1h_pct'))} / {fmt_pct(snapshot.get('oi_4h_pct'))}",
        "",
        "多交易所资金费率",
        *funding_rows_text(latest_funding_rows(snapshot)),
        "",
        "历史雷达信号",
    ])
    if history:
        lines.extend(event_line(event) for event in history[:10])
    else:
        lines.append("- 暂无这个币的历史信号记录；后续推送会自动积累。")

    lines.extend(["", "本地规则结论", f"倾向: {verdict.get('stance') or '观望'}"])
    bullish = verdict.get("bullish") if isinstance(verdict.get("bullish"), list) else []
    bearish = verdict.get("bearish") if isinstance(verdict.get("bearish"), list) else []
    risks = verdict.get("risks") if isinstance(verdict.get("risks"), list) else []
    lines.append("多头证据:")
    lines.extend([f"- {item}" for item in bullish] or ["- 暂无明确多头证据"])
    lines.append("空头证据:")
    lines.extend([f"- {item}" for item in bearish] or ["- 暂无明确空头证据"])
    lines.append("风险:")
    lines.extend([f"- {item}" for item in risks] or ["- 暂无突出风险，但仍需等结构确认"])
    lines.extend(["", "这份结论是雷达证据整理，不是自动交易指令。"])
    return "\n".join(lines)


def format_symbol_dossier_ai_context(dossier: dict[str, Any], user_text: str) -> str:
    compact = {
        "user_question": user_text,
        "symbol": dossier.get("symbol"),
        "requested_signal": dossier.get("requested_signal"),
        "snapshot": dossier.get("snapshot"),
        "history": dossier.get("history", [])[:18],
        "local_rule_verdict": dossier.get("verdict"),
    }
    return "\n".join([
        "请基于下面的泡泡雷达币种档案，给出多空证据研判。",
        "要求：先总结当前状态，再列多头证据、空头证据、风险，最后给出偏多/偏空/观望/高风险观望。",
        "不要承诺收益，不要直接命令开仓；如果信号冲突，要明确说等待什么确认条件。",
        "",
        json.dumps(compact, ensure_ascii=False, indent=2)[:12000],
    ])
