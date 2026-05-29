from __future__ import annotations

from datetime import datetime
from typing import Any

from .config import Settings
from .data_sources import BinanceDataSource, CoinglassDataSource
from .storage import JsonStore
from .time_windows import CST, closed_window
from .wash_risk import calculate_wash_risk, to_float


TIMEFRAME = "15m"


def now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="seconds")


def pct_change(new: float, old: float) -> float:
    if old == 0:
        return 0.0
    return (new - old) / abs(old) * 100


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def score_level(score: float) -> str:
    if score >= 90:
        return "S"
    if score >= 75:
        return "A"
    if score >= 60:
        return "B"
    return "C"


def signal_type_for(metrics: dict[str, Any]) -> str:
    oi_change_pct = to_float(metrics.get("oi_change_pct"))
    price_change_pct = to_float(metrics.get("price_change_pct"))
    funding_rate = to_float(metrics.get("funding_rate"))
    taker_buy_sell_ratio = to_float(metrics.get("taker_buy_sell_ratio"), 1.0)
    long_short_ratio = to_float(metrics.get("long_short_ratio"), 1.0)

    if oi_change_pct >= 10 and funding_rate < 0 and (long_short_ratio <= 0.95 or taker_buy_sell_ratio >= 1.05):
        return "SHORT_SQUEEZE_FUEL"
    if oi_change_pct >= 8 and abs(price_change_pct) <= 3 and funding_rate <= 0.0015:
        return "ACCUMULATION_BUILDUP"
    if oi_change_pct >= 8 and price_change_pct >= 4 and taker_buy_sell_ratio >= 1.08:
        return "MOMENTUM_BREAKOUT"
    if funding_rate >= 0.003 and long_short_ratio >= 1.15:
        return "LONG_CROWDED_RISK"
    return "WATCH"


def calculate_launch_score(metrics: dict[str, Any]) -> dict[str, Any]:
    oi_change_pct = to_float(metrics.get("oi_change_pct"))
    price_change_pct = to_float(metrics.get("price_change_pct"))
    divergence_ratio = to_float(metrics.get("divergence_ratio"))
    funding_rate = to_float(metrics.get("funding_rate"))
    taker_buy_sell_ratio = to_float(metrics.get("taker_buy_sell_ratio"), 1.0)
    long_short_ratio = to_float(metrics.get("long_short_ratio"), 1.0)
    volume_ratio = to_float(metrics.get("volume_ratio"), 1.0)
    oi_marketcap_ratio = to_float(metrics.get("oi_marketcap_ratio"))
    cross_exchange_confirmed = bool(metrics.get("cross_exchange_confirmed"))

    oi_score = clamp(abs(oi_change_pct) * 1.2, 0, 25)
    divergence_score = clamp(divergence_ratio * 2.0, 0, 18)
    volume_score = clamp((volume_ratio - 1.0) * 6.0, 0, 12)
    funding_score = 0.0
    if funding_rate < 0:
        funding_score = clamp(abs(funding_rate) * 2500, 0, 15)
    elif funding_rate >= 0.003:
        funding_score = clamp(funding_rate * 2200, 0, 10)

    taker_bias = abs(taker_buy_sell_ratio - 1.0)
    taker_score = clamp(taker_bias * 40, 0, 8)
    if long_short_ratio and long_short_ratio < 0.85:
        taker_score += 4
    oi_mcap_score = clamp(oi_marketcap_ratio * 35, 0, 10)
    trend_score = clamp(max(price_change_pct, 0) * 1.2, 0, 7)
    cross_score = 5 if cross_exchange_confirmed else 0

    wash = calculate_wash_risk(metrics)
    penalty = to_float(wash["wash_risk_score"]) * 0.28
    final_score = round(clamp(
        oi_score
        + divergence_score
        + volume_score
        + funding_score
        + taker_score
        + oi_mcap_score
        + trend_score
        + cross_score
        - penalty
    ), 1)

    signal_type = signal_type_for(metrics)
    return {
        "score": final_score,
        "level": score_level(final_score),
        "signal_type": signal_type,
        "wash_risk_score": wash["wash_risk_score"],
        "wash_risk_level": wash["risk_level"],
        "risk_reasons": wash["risk_reasons"],
    }


def build_item(metrics: dict[str, Any]) -> dict[str, Any]:
    scored = calculate_launch_score(metrics)
    updated_at = str(metrics.get("updated_at") or now_iso())
    item = {
        "rank": int(metrics.get("rank") or 0),
        "symbol": str(metrics.get("symbol") or "").upper(),
        "timeframe": str(metrics.get("timeframe") or TIMEFRAME),
        "signal_type": scored["signal_type"],
        "level": scored["level"],
        "score": scored["score"],
        "oi_change_pct": round(to_float(metrics.get("oi_change_pct")), 3),
        "price_change_pct": round(to_float(metrics.get("price_change_pct")), 3),
        "divergence_ratio": round(to_float(metrics.get("divergence_ratio")), 3),
        "funding_rate": round(to_float(metrics.get("funding_rate")), 8),
        "taker_buy_sell_ratio": round(to_float(metrics.get("taker_buy_sell_ratio"), 1.0), 3),
        "long_short_ratio": round(to_float(metrics.get("long_short_ratio"), 1.0), 3),
        "oi_marketcap_ratio": round(to_float(metrics.get("oi_marketcap_ratio")), 6),
        "wash_risk_score": scored["wash_risk_score"],
        "wash_risk_level": scored["wash_risk_level"],
        "risk_reasons": scored["risk_reasons"],
        "cross_exchange_confirmed": bool(metrics.get("cross_exchange_confirmed")),
        "updated_at": updated_at,
    }
    item["lifecycle"] = lifecycle_for(item)
    return item


def lifecycle_for(item: dict[str, Any]) -> str:
    score = to_float(item.get("score"))
    if score >= 90:
        return "启动确认"
    if score >= 75:
        return "强启动候选"
    if score >= 60:
        return "观察增强"
    return "早期观察"


def build_mock_payload() -> dict[str, Any]:
    updated_at = now_iso()
    samples = [
        {
            "symbol": "FIDAUSDT",
            "timeframe": "15m",
            "oi_change_pct": 18.5,
            "price_change_pct": 2.1,
            "divergence_ratio": 8.8,
            "funding_rate": -0.0041,
            "taker_buy_sell_ratio": 1.18,
            "long_short_ratio": 0.82,
            "oi_marketcap_ratio": 0.242,
            "volume_ratio": 2.6,
            "trade_count_ratio": 1.4,
            "avg_trade_usd": 620,
            "cross_exchange_confirmed": True,
            "volume_marketcap_ratio": 0.42,
            "price_1h_change_pct": 3.2,
            "updated_at": updated_at,
        },
        {
            "symbol": "XAIUSDT",
            "timeframe": "15m",
            "oi_change_pct": 12.2,
            "price_change_pct": 0.9,
            "divergence_ratio": 13.5,
            "funding_rate": -0.0018,
            "taker_buy_sell_ratio": 1.03,
            "long_short_ratio": 0.94,
            "oi_marketcap_ratio": 0.168,
            "volume_ratio": 3.4,
            "trade_count_ratio": 2.0,
            "avg_trade_usd": 240,
            "cross_exchange_confirmed": False,
            "volume_marketcap_ratio": 0.66,
            "price_1h_change_pct": 1.7,
            "updated_at": updated_at,
        },
    ]
    items = [build_item({**metrics, "rank": idx}) for idx, metrics in enumerate(samples, start=1)]
    return {
        "updated_at": updated_at,
        "stale": False,
        "mock": True,
        "mode": "mock",
        "items": items,
    }


def _base_asset(symbol: str) -> str:
    upper = symbol.upper()
    return upper[:-4] if upper.endswith("USDT") else upper


def _is_excluded(symbol: str, settings: Settings) -> bool:
    base = _base_asset(symbol)
    return base in set(settings.excluded_base_assets)


def _ticker_candidates(settings: Settings, source: BinanceDataSource) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for item in source.ticker_24h():
        symbol = str(item.get("symbol") or "").upper()
        if not symbol.endswith("USDT") or _is_excluded(symbol, settings):
            continue
        quote_volume = to_float(item.get("quoteVolume"))
        if quote_volume < settings.radar_min_quote_volume:
            continue
        candidates.append(item)
    candidates.sort(key=lambda row: to_float(row.get("quoteVolume")), reverse=True)
    return candidates[: max(0, settings.launch_scan_limit)]


def _funding_map(source: BinanceDataSource) -> dict[str, float]:
    rates: dict[str, float] = {}
    for item in source.premium_index():
        symbol = str(item.get("symbol") or "").upper()
        if symbol:
            rates[symbol] = to_float(item.get("lastFundingRate"))
    return rates


def _closed_klines(raw_klines: list[list[Any]], end_ms: int) -> list[list[Any]]:
    rows = [row for row in raw_klines if len(row) >= 11 and int(to_float(row[6])) <= end_ms]
    rows.sort(key=lambda row: int(to_float(row[0])))
    return rows


def _kline_metrics(klines: list[list[Any]]) -> dict[str, float]:
    latest = klines[-1]
    previous = klines[:-1]
    open_price = to_float(latest[1])
    close_price = to_float(latest[4])
    quote_volume = to_float(latest[7])
    trades = to_float(latest[8])
    taker_buy_quote = to_float(latest[10])
    taker_sell_quote = max(quote_volume - taker_buy_quote, 0.0)
    previous_volume = sum(to_float(row[7]) for row in previous) / max(1, len(previous))
    previous_trades = sum(to_float(row[8]) for row in previous) / max(1, len(previous))
    avg_trade = quote_volume / trades if trades > 0 else 0.0
    baseline_volume = sum(to_float(row[7]) for row in previous)
    baseline_trades = sum(to_float(row[8]) for row in previous)
    baseline_avg_trade = baseline_volume / baseline_trades if baseline_trades > 0 else 0.0
    first_open = to_float(klines[0][1])
    return {
        "price_change_pct": pct_change(close_price, open_price),
        "price_1h_change_pct": pct_change(close_price, first_open),
        "quote_volume": quote_volume,
        "volume_ratio": quote_volume / previous_volume if previous_volume > 0 else 1.0,
        "trade_count_ratio": trades / previous_trades if previous_trades > 0 else 1.0,
        "avg_trade_usd": avg_trade,
        "avg_trade_usd_baseline": baseline_avg_trade,
        "taker_buy_sell_ratio": taker_buy_quote / taker_sell_quote if taker_sell_quote > 0 else 1.0,
    }


def _oi_metrics(oi_hist: list[dict[str, Any]]) -> dict[str, float]:
    rows = [
        row for row in oi_hist
        if to_float(row.get("sumOpenInterestValue") or row.get("sumOpenInterest")) > 0
    ]
    rows.sort(key=lambda row: int(to_float(row.get("timestamp") or row.get("time"))))
    if len(rows) < 2:
        return {"oi_change_pct": 0.0, "oi_value": 0.0}
    first = to_float(rows[0].get("sumOpenInterestValue") or rows[0].get("sumOpenInterest"))
    last = to_float(rows[-1].get("sumOpenInterestValue") or rows[-1].get("sumOpenInterest"))
    return {
        "oi_change_pct": pct_change(last, first),
        "oi_value": last,
    }


def _cross_exchange_confirmed(coinglass: CoinglassDataSource | None, symbol: str) -> bool:
    if coinglass is None or not coinglass.enabled:
        return False
    data = coinglass.open_interest_exchange_list(_base_asset(symbol))
    names: set[str] = set()
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                name = str(item.get("exchange") or item.get("exchangeName") or item.get("name") or "").lower()
                if name:
                    names.add(name)
    elif isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, (dict, list, int, float, str)):
                names.add(str(key).lower())
    return "binance" in names and bool(names & {"okx", "bybit"})


def analyze_symbol(
    settings: Settings,
    source: BinanceDataSource,
    symbol: str,
    funding_rate: float,
    market_cap: float,
    coinglass: CoinglassDataSource | None,
) -> dict[str, Any] | None:
    window = closed_window(interval_sec=15 * 60, delay_sec=settings.launch_close_delay_sec)
    klines = _closed_klines(source.klines(symbol, interval=TIMEFRAME, limit=6, end_time=window.end_ms), window.end_ms)
    if len(klines) < 2:
        return None
    kline_metrics = _kline_metrics(klines[-4:] if len(klines) >= 4 else klines)
    oi = _oi_metrics(source.open_interest_hist(symbol, period="5m", limit=8, end_time=window.end_ms))
    oi_change_pct = oi["oi_change_pct"]
    price_change_pct = kline_metrics["price_change_pct"]
    oi_value = oi["oi_value"]
    oi_marketcap_ratio = oi_value / market_cap if market_cap > 0 else 0.0
    volume_marketcap_ratio = kline_metrics["quote_volume"] / market_cap if market_cap > 0 else 0.0
    divergence_ratio = abs(oi_change_pct) / max(abs(price_change_pct), 0.1)

    return {
        "symbol": symbol,
        "timeframe": TIMEFRAME,
        "oi_change_pct": oi_change_pct,
        "price_change_pct": price_change_pct,
        "divergence_ratio": divergence_ratio,
        "funding_rate": funding_rate,
        "taker_buy_sell_ratio": kline_metrics["taker_buy_sell_ratio"],
        "long_short_ratio": 1.0,
        "oi_marketcap_ratio": oi_marketcap_ratio,
        "volume_marketcap_ratio": volume_marketcap_ratio,
        "volume_ratio": kline_metrics["volume_ratio"],
        "trade_count_ratio": kline_metrics["trade_count_ratio"],
        "avg_trade_usd": kline_metrics["avg_trade_usd"],
        "avg_trade_usd_baseline": kline_metrics["avg_trade_usd_baseline"],
        "cross_exchange_confirmed": _cross_exchange_confirmed(coinglass, symbol),
        "price_1h_change_pct": kline_metrics["price_1h_change_pct"],
        "updated_at": window.end.isoformat(timespec="seconds"),
    }


def build_real_payload(
    settings: Settings,
    store: JsonStore,
    source: BinanceDataSource | None = None,
    coinglass: CoinglassDataSource | None = None,
) -> dict[str, Any]:
    source = source or BinanceDataSource(settings)
    coinglass = coinglass or CoinglassDataSource(settings)
    funding = _funding_map(source)
    market_caps = source.market_caps()
    raw_items: list[dict[str, Any]] = []
    for ticker in _ticker_candidates(settings, source):
        symbol = str(ticker.get("symbol") or "").upper()
        metrics = analyze_symbol(
            settings,
            source,
            symbol,
            funding.get(symbol, 0.0),
            market_caps.get(_base_asset(symbol), 0.0),
            coinglass,
        )
        if metrics is not None:
            raw_items.append(metrics)

    items = [build_item(metrics) for metrics in raw_items]
    items.sort(key=lambda row: to_float(row.get("score")), reverse=True)
    for idx, item in enumerate(items, start=1):
        item["rank"] = idx
    payload = {
        "updated_at": now_iso(),
        "stale": False,
        "mock": False,
        "mode": "real",
        "items": items,
        "diagnostics": {
            "source": source.diagnostics(),
            "coinglass": coinglass.diagnostics() if coinglass else {},
        },
    }
    save_launch_payload(settings, store, payload)
    return payload


def save_launch_payload(settings: Settings, store: JsonStore, payload: dict[str, Any]) -> None:
    items = list(payload.get("items") or [])
    store.save(settings.launch_radar_latest_path, payload)
    store.save(settings.oi_divergence_latest_path, {
        "updated_at": payload.get("updated_at"),
        "stale": payload.get("stale", False),
        "items": [
            {
                "rank": item.get("rank"),
                "symbol": item.get("symbol"),
                "timeframe": item.get("timeframe"),
                "level": item.get("level"),
                "score": item.get("score"),
                "oi_change_pct": item.get("oi_change_pct"),
                "price_change_pct": item.get("price_change_pct"),
                "divergence_ratio": item.get("divergence_ratio"),
                "updated_at": item.get("updated_at"),
            }
            for item in items
        ],
    })
    store.save(settings.wash_risk_latest_path, {
        "updated_at": payload.get("updated_at"),
        "stale": payload.get("stale", False),
        "items": [
            {
                "rank": item.get("rank"),
                "symbol": item.get("symbol"),
                "wash_risk_score": item.get("wash_risk_score"),
                "wash_risk_level": item.get("wash_risk_level"),
                "risk_reasons": item.get("risk_reasons", []),
                "updated_at": item.get("updated_at"),
            }
            for item in items
        ],
    })
    if items:
        store.append_record(
            settings.signal_history_path,
            {
                "updated_at": payload.get("updated_at"),
                "top_symbols": [item.get("symbol") for item in items[:10]],
                "items": items[:50],
            },
            limit=settings.launch_watch_history_limit,
        )


def load_latest_payload(settings: Settings, store: JsonStore) -> dict[str, Any]:
    data = store.load(settings.launch_radar_latest_path, {})
    if isinstance(data, dict):
        data.setdefault("items", [])
        return data
    return {"updated_at": now_iso(), "stale": True, "items": []}


def build_launch_radar_payload(
    settings: Settings,
    store: JsonStore,
    mode: str = "mock",
    source: BinanceDataSource | None = None,
    coinglass: CoinglassDataSource | None = None,
) -> dict[str, Any]:
    normalized = (mode or "mock").lower()
    if normalized == "mock":
        payload = build_mock_payload()
        save_launch_payload(settings, store, payload)
        return payload
    if normalized == "real":
        return build_real_payload(settings, store, source=source, coinglass=coinglass)

    latest = load_latest_payload(settings, store)
    if latest.get("items"):
        return latest
    payload = build_mock_payload()
    payload["stale"] = True
    payload["mode"] = "auto"
    save_launch_payload(settings, store, payload)
    return payload


def filter_items(
    payload: dict[str, Any],
    *,
    timeframe: str = "",
    level: str = "",
    signal_type: str = "",
    wash_risk_level: str = "",
    min_score: float = 0.0,
) -> dict[str, Any]:
    items = []
    for item in payload.get("items") or []:
        if timeframe and str(item.get("timeframe", "")).lower() != timeframe.lower():
            continue
        if level and str(item.get("level", "")).upper() != level.upper():
            continue
        if signal_type and str(item.get("signal_type", "")).upper() != signal_type.upper():
            continue
        if wash_risk_level and str(item.get("wash_risk_level", "")).upper() != wash_risk_level.upper():
            continue
        if to_float(item.get("score")) < min_score:
            continue
        items.append(dict(item))
    for idx, item in enumerate(items, start=1):
        item["rank"] = idx
    return {**payload, "items": items}


def launch_stats(items: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "s_count": sum(1 for item in items if item.get("level") == "S"),
        "a_count": sum(1 for item in items if item.get("level") == "A"),
        "high_wash_risk_count": sum(1 for item in items if item.get("wash_risk_level") == "HIGH"),
        "short_squeeze_fuel_count": sum(1 for item in items if item.get("signal_type") == "SHORT_SQUEEZE_FUEL"),
        "accumulation_count": sum(1 for item in items if item.get("signal_type") == "ACCUMULATION_BUILDUP"),
    }
