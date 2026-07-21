from __future__ import annotations

import math
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


REALTIME_FEATURE_SCHEMA_VERSION = 2


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _timestamp_ms(value: Any) -> int:
    number = _number(value)
    return int(number) if number is not None and number > 0 else 0


def select_realtime_symbols(
    ticker_rows: list[dict[str, Any]],
    *,
    valid_symbols: set[str] | None = None,
    excluded_base_assets: set[str] | None = None,
    min_quote_volume: float = 0,
    limit: int = 80,
) -> list[str]:
    allowed = {str(symbol).upper() for symbol in valid_symbols or set()}
    excluded = {str(symbol).upper() for symbol in excluded_base_assets or set()}
    candidates: list[tuple[float, str]] = []
    seen: set[str] = set()
    for row in ticker_rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper()
        volume = _number(row.get("quoteVolume"))
        if (
            not symbol.endswith("USDT")
            or symbol in seen
            or (allowed and symbol not in allowed)
            or symbol[:-4] in excluded
            or volume is None
            or volume < float(min_quote_volume)
        ):
            continue
        seen.add(symbol)
        candidates.append((volume, symbol))
    candidates.sort(key=lambda item: (-item[0], item[1]))
    return [symbol for _volume, symbol in candidates[: max(1, min(500, int(limit or 80)))]]


def select_bybit_realtime_symbols(
    instruments_payload: Any,
    tickers_payload: Any,
    *,
    excluded_base_assets: set[str] | None = None,
    min_quote_volume: float = 0,
    limit: int = 80,
) -> list[str]:
    instrument_result = instruments_payload.get("result") if isinstance(instruments_payload, dict) else {}
    instruments = instrument_result.get("list") if isinstance(instrument_result, dict) else []
    valid_symbols = {
        str(item.get("symbol") or "").upper()
        for item in instruments or []
        if isinstance(item, dict)
        and str(item.get("quoteCoin") or "").upper() == "USDT"
        and str(item.get("settleCoin") or "").upper() == "USDT"
        and str(item.get("status") or "") == "Trading"
        and str(item.get("contractType") or "") == "LinearPerpetual"
    }
    ticker_result = tickers_payload.get("result") if isinstance(tickers_payload, dict) else {}
    tickers = ticker_result.get("list") if isinstance(ticker_result, dict) else []
    rows = [
        {
            "symbol": str(item.get("symbol") or "").upper(),
            "quoteVolume": item.get("turnover24h"),
        }
        for item in tickers or []
        if isinstance(item, dict)
    ]
    return select_realtime_symbols(
        rows,
        valid_symbols=valid_symbols,
        excluded_base_assets=excluded_base_assets,
        min_quote_volume=min_quote_volume,
        limit=limit,
    )


def select_okx_realtime_contracts(
    instruments_payload: Any,
    tickers_payload: Any,
    *,
    excluded_base_assets: set[str] | None = None,
    min_quote_volume: float = 0,
    limit: int = 80,
) -> tuple[list[str], dict[str, dict[str, Any]]]:
    instruments = instruments_payload.get("data") if isinstance(instruments_payload, dict) else []
    specs: dict[str, dict[str, Any]] = {}
    symbol_to_inst: dict[str, str] = {}
    for item in instruments or []:
        if not isinstance(item, dict):
            continue
        inst_id = str(item.get("instId") or "").upper()
        symbol = _okx_symbol(inst_id)
        contract_value = _number(item.get("ctVal"))
        contract_ccy = str(item.get("ctValCcy") or "").upper()
        if (
            not symbol
            or str(item.get("instType") or "").upper() != "SWAP"
            or str(item.get("ctType") or "").lower() != "linear"
            or str(item.get("settleCcy") or "").upper() != "USDT"
            or str(item.get("state") or "").lower() != "live"
            or contract_value is None
            or contract_value <= 0
            or not contract_ccy
        ):
            continue
        specs[inst_id] = {
            "symbol": symbol,
            "ct_val": contract_value,
            "ct_val_ccy": contract_ccy,
        }
        symbol_to_inst[symbol] = inst_id
    tickers = tickers_payload.get("data") if isinstance(tickers_payload, dict) else []
    rows: list[dict[str, Any]] = []
    for item in tickers or []:
        if not isinstance(item, dict):
            continue
        inst_id = str(item.get("instId") or "").upper()
        spec = specs.get(inst_id)
        if spec is None:
            continue
        last_price = _number(item.get("last")) or 0
        volume_ccy = _number(item.get("volCcy24h"))
        contract_volume = _number(item.get("vol24h")) or 0
        if volume_ccy is not None and volume_ccy > 0 and last_price > 0:
            quote_volume = volume_ccy * last_price
        elif spec["ct_val_ccy"] == spec["symbol"][:-4]:
            quote_volume = contract_volume * float(spec["ct_val"]) * last_price
        else:
            quote_volume = contract_volume * float(spec["ct_val"])
        rows.append({"symbol": spec["symbol"], "quoteVolume": quote_volume})
    symbols = select_realtime_symbols(
        rows,
        valid_symbols=set(symbol_to_inst),
        excluded_base_assets=excluded_base_assets,
        min_quote_volume=min_quote_volume,
        limit=limit,
    )
    selected_specs = {symbol_to_inst[symbol]: specs[symbol_to_inst[symbol]] for symbol in symbols}
    return symbols, selected_specs


def binance_stream_subscriptions(symbols: list[str], *, limit: int = 200) -> list[str]:
    safe_limit = max(1, min(500, int(limit or 200)))
    result: list[str] = []
    seen: set[str] = set()
    for value in symbols:
        symbol = str(value or "").upper()
        if not symbol.endswith("USDT") or symbol in seen:
            continue
        seen.add(symbol)
        result.append(f"{symbol.lower()}@aggTrade")
        if len(result) >= safe_limit:
            break
    result.append("!forceOrder@arr")
    return result


def _iso_seconds(value: int) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z") if value > 0 else ""


def build_realtime_radar_boards(rows: list[dict[str, Any]], *, limit: int = 8) -> list[dict[str, Any]]:
    safe_limit = max(1, min(20, int(limit or 8)))
    grouped: dict[tuple[str, int, int], dict[str, Any]] = {}
    latest_bucket_by_symbol: dict[str, tuple[int, int]] = {}
    for source in rows:
        if not isinstance(source, dict):
            continue
        symbol = str(source.get("symbol") or "").upper()
        bucket_start = int(source.get("bucket_start") or 0)
        bucket_sec = max(1, int(source.get("bucket_sec") or 60))
        if not symbol:
            continue
        latest_bucket_by_symbol[symbol] = max(
            latest_bucket_by_symbol.get(symbol, (0, bucket_sec)),
            (bucket_start, bucket_sec),
        )
        key = (symbol, bucket_start, bucket_sec)
        target = grouped.setdefault(key, {
            "symbol": symbol,
            "bucket_start": bucket_start,
            "bucket_sec": bucket_sec,
            "cvd_usd": 0.0,
            "long_liquidation_usd": 0.0,
            "short_liquidation_usd": 0.0,
            "exchanges": [],
        })
        target["cvd_usd"] += float(source.get("cvd_usd") or 0)
        target["long_liquidation_usd"] += float(source.get("long_liquidation_usd") or 0)
        target["short_liquidation_usd"] += float(source.get("short_liquidation_usd") or 0)
        exchange = str(source.get("exchange") or "")
        if exchange and exchange not in target["exchanges"]:
            target["exchanges"].append(exchange)
    rows = [
        row for (symbol, bucket_start, bucket_sec), row in grouped.items()
        if latest_bucket_by_symbol.get(symbol) == (bucket_start, bucket_sec)
    ]

    def item(row: dict[str, Any], value: float, percentile: float) -> dict[str, Any]:
        symbol = str(row.get("symbol") or "")
        bucket_end = int(row.get("bucket_start") or 0) + int(row.get("bucket_sec") or 60)
        return {
            "symbol": symbol,
            "coin": symbol[:-4] if symbol.endswith("USDT") else symbol,
            "value": round(value, 2),
            "unit": "usd",
            "magnitude_usd": round(abs(value), 2),
            "strength_percentile": round(percentile, 1),
            "updated_at": _iso_seconds(bucket_end),
            "status": "fresh",
            "quality": "websocket_closed_bucket",
            "exchanges": sorted(str(value) for value in row.get("exchanges", [])),
        }

    def ranked(source: list[tuple[dict[str, Any], float]]) -> list[dict[str, Any]]:
        valid = [(row, value) for row, value in source if math.isfinite(value) and value != 0]
        strengths = sorted(abs(value) for _row, value in valid)
        output = []
        for row, value in sorted(valid, key=lambda pair: abs(pair[1]), reverse=True)[:safe_limit]:
            percentile = 100.0 * sum(1 for sample in strengths if sample <= abs(value)) / len(strengths)
            output.append(item(row, value, percentile))
        return output

    cvd_rows = [(row, float(row.get("cvd_usd") or 0)) for row in rows if isinstance(row, dict)]
    positive_cvd = ranked([(row, value) for row, value in cvd_rows if value > 0])
    negative_cvd = ranked([(row, value) for row, value in cvd_rows if value < 0])
    short_liquidations = ranked([
        (row, float(row.get("short_liquidation_usd") or 0)) for row in rows if isinstance(row, dict)
    ])
    long_liquidations = ranked([
        (row, -float(row.get("long_liquidation_usd") or 0)) for row in rows if isinstance(row, dict)
    ])
    return [
        {
            "key": "realtime_futures_flow",
            "title": "实时合约主动资金",
            "metric": "realtime_cvd_usd",
            "unit": "usd",
            "available": bool(positive_cvd or negative_cvd),
            "coverage": len(cvd_rows),
            "positive": {"title": "实时主动买入", "items": positive_cvd},
            "negative": {"title": "实时主动卖出", "items": negative_cvd},
            "reason": "" if positive_cvd or negative_cvd else "实时成交分钟特征尚未就绪",
        },
        {
            "key": "realtime_liquidations",
            "title": "实时清算",
            "metric": "liquidation_usd",
            "unit": "usd",
            "available": bool(long_liquidations or short_liquidations),
            "coverage": len(rows),
            "positive": {"title": "空头强平", "items": short_liquidations},
            "negative": {"title": "多头强平", "items": long_liquidations},
            "reason": "" if long_liquidations or short_liquidations else "当前分钟没有清算事件",
        },
    ]


@dataclass(frozen=True)
class MarketEvent:
    event_id: str
    event_type: str
    exchange: str
    market: str
    symbol: str
    event_time_ms: int
    side: str
    price: float
    quantity: float
    notional_usd: float
    position_side: str = ""


def parse_binance_market_event(payload: Any, *, market: str = "futures") -> MarketEvent | None:
    source = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
    if not isinstance(source, dict):
        return None
    event_type = str(source.get("e") or "")
    safe_market = "spot" if str(market).lower() == "spot" else "futures"
    if event_type == "aggTrade":
        symbol = str(source.get("s") or "").upper()
        price = _number(source.get("p"))
        quantity = _number(source.get("q"))
        event_time_ms = _timestamp_ms(source.get("T") or source.get("E"))
        trade_id = str(source.get("a") or "")
        if not symbol.endswith("USDT") or not trade_id or not event_time_ms:
            return None
        if price is None or quantity is None or price <= 0 or quantity <= 0:
            return None
        side = "sell" if bool(source.get("m")) else "buy"
        return MarketEvent(
            event_id=f"binance:trade:{symbol}:{trade_id}",
            event_type="trade",
            exchange="binance",
            market=safe_market,
            symbol=symbol,
            event_time_ms=event_time_ms,
            side=side,
            price=price,
            quantity=quantity,
            notional_usd=price * quantity,
        )
    if event_type != "forceOrder" or safe_market != "futures":
        return None
    order = source.get("o") if isinstance(source.get("o"), dict) else {}
    symbol = str(order.get("s") or "").upper()
    side = str(order.get("S") or "").lower()
    event_time_ms = _timestamp_ms(order.get("T") or source.get("E"))
    average_price = _number(order.get("ap"))
    order_price = _number(order.get("p"))
    executed_quantity = _number(order.get("z"))
    order_quantity = _number(order.get("q"))
    price = average_price if average_price is not None and average_price > 0 else order_price
    quantity = executed_quantity if executed_quantity is not None and executed_quantity > 0 else order_quantity
    if not symbol.endswith("USDT") or side not in {"buy", "sell"} or not event_time_ms:
        return None
    if price is None or quantity is None or price <= 0 or quantity <= 0:
        return None
    event_key = f"{event_time_ms}:{side}:{price:.12g}:{quantity:.12g}"
    return MarketEvent(
        event_id=f"binance:liquidation:{symbol}:{event_key}",
        event_type="liquidation",
        exchange="binance",
        market="futures",
        symbol=symbol,
        event_time_ms=event_time_ms,
        side=side,
        price=price,
        quantity=quantity,
        notional_usd=price * quantity,
        position_side="long" if side == "sell" else "short",
    )


def parse_bybit_market_events(payload: Any, *, market: str = "futures") -> list[MarketEvent]:
    if not isinstance(payload, dict):
        return []
    topic = str(payload.get("topic") or "")
    raw_data = payload.get("data")
    records = raw_data if isinstance(raw_data, list) else [raw_data] if isinstance(raw_data, dict) else []
    events: list[MarketEvent] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        symbol = str(record.get("s") or "").upper()
        side = str(record.get("S") or "").lower()
        event_time_ms = _timestamp_ms(record.get("T") or payload.get("ts"))
        price = _number(record.get("p"))
        quantity = _number(record.get("v"))
        if (
            not symbol.endswith("USDT")
            or side not in {"buy", "sell"}
            or not event_time_ms
            or price is None
            or quantity is None
            or price <= 0
            or quantity <= 0
        ):
            continue
        if topic.startswith("publicTrade."):
            trade_id = str(record.get("i") or "")
            if not trade_id or bool(record.get("BT")):
                continue
            events.append(MarketEvent(
                event_id=f"bybit:trade:{symbol}:{trade_id}",
                event_type="trade",
                exchange="bybit",
                market="futures" if str(market).lower() != "spot" else "spot",
                symbol=symbol,
                event_time_ms=event_time_ms,
                side=side,
                price=price,
                quantity=quantity,
                notional_usd=price * quantity,
            ))
        elif topic.startswith("allLiquidation."):
            event_key = (
                f"{int(payload.get('ts') or 0)}:{event_time_ms}:{side}:"
                f"{price:.12g}:{quantity:.12g}:{index}"
            )
            events.append(MarketEvent(
                event_id=f"bybit:liquidation:{symbol}:{event_key}",
                event_type="liquidation",
                exchange="bybit",
                market="futures",
                symbol=symbol,
                event_time_ms=event_time_ms,
                side=side,
                price=price,
                quantity=quantity,
                notional_usd=price * quantity,
                position_side="long" if side == "buy" else "short",
            ))
    return events


def _okx_symbol(inst_id: str) -> str:
    parts = str(inst_id or "").upper().split("-")
    if len(parts) >= 3 and parts[1] == "USDT" and parts[-1] == "SWAP":
        return f"{parts[0]}USDT"
    return ""


def parse_okx_market_events(
    payload: Any,
    *,
    contract_specs: dict[str, Any],
) -> list[MarketEvent]:
    if not isinstance(payload, dict):
        return []
    arg = payload.get("arg") if isinstance(payload.get("arg"), dict) else {}
    if str(arg.get("channel") or "") != "trades":
        return []
    records = payload.get("data") if isinstance(payload.get("data"), list) else []
    events: list[MarketEvent] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        inst_id = str(record.get("instId") or arg.get("instId") or "").upper()
        symbol = _okx_symbol(inst_id)
        spec = contract_specs.get(inst_id)
        if not symbol or not isinstance(spec, dict):
            continue
        price = _number(record.get("px"))
        contract_count = _number(record.get("sz"))
        contract_value = _number(spec.get("ct_val") if "ct_val" in spec else spec.get("ctVal"))
        contract_ccy = str(spec.get("ct_val_ccy") or spec.get("ctValCcy") or "").upper()
        side = str(record.get("side") or "").lower()
        event_time_ms = _timestamp_ms(record.get("ts"))
        trade_id = str(record.get("tradeId") or "")
        if (
            price is None
            or contract_count is None
            or contract_value is None
            or price <= 0
            or contract_count <= 0
            or contract_value <= 0
            or side not in {"buy", "sell"}
            or not event_time_ms
            or not trade_id
        ):
            continue
        base_ccy = symbol[:-4]
        if contract_ccy == base_ccy:
            quantity = contract_count * contract_value
            notional_usd = price * quantity
        elif contract_ccy in {"USDT", "USD"}:
            notional_usd = contract_count * contract_value
            quantity = notional_usd / price
        else:
            continue
        events.append(MarketEvent(
            event_id=f"okx:trade:{inst_id}:{trade_id}",
            event_type="trade",
            exchange="okx",
            market="futures",
            symbol=symbol,
            event_time_ms=event_time_ms,
            side=side,
            price=price,
            quantity=quantity,
            notional_usd=notional_usd,
        ))
    return events


class RealtimeFeatureAggregator:
    def __init__(self, *, bucket_sec: int = 60):
        self.bucket_sec = max(1, int(bucket_sec))
        self._buckets: dict[tuple[str, str, str, int], dict[str, Any]] = {}
        self._event_ids: dict[tuple[str, str, str, int], set[str]] = {}
        self._finalized_through_ms: dict[tuple[str, str, str], int] = {}
        self._accepted_events = 0
        self._duplicate_events = 0
        self._late_events = 0
        self._invalid_events = 0
        self._lock = threading.Lock()

    def add(self, event: MarketEvent | None) -> bool:
        with self._lock:
            return self._add(event)

    def _add(self, event: MarketEvent | None) -> bool:
        if event is None:
            self._invalid_events += 1
            return False
        series_key = (event.exchange, event.market, event.symbol)
        if event.event_time_ms < self._finalized_through_ms.get(series_key, 0):
            self._late_events += 1
            return False
        bucket_start = event.event_time_ms // (self.bucket_sec * 1000) * self.bucket_sec
        key = (event.exchange, event.market, event.symbol, bucket_start)
        event_ids = self._event_ids.setdefault(key, set())
        if event.event_id in event_ids:
            self._duplicate_events += 1
            return False
        event_ids.add(event.event_id)
        row = self._buckets.setdefault(key, {
            "exchange": event.exchange,
            "market": event.market,
            "symbol": event.symbol,
            "bucket_start": bucket_start,
            "bucket_sec": self.bucket_sec,
            "trade_buy_usd": 0.0,
            "trade_sell_usd": 0.0,
            "cvd_usd": 0.0,
            "trade_count": 0,
            "price_open": None,
            "price_high": None,
            "price_low": None,
            "price_close": None,
            "first_trade_ms": 0,
            "last_trade_ms": 0,
            "long_liquidation_usd": 0.0,
            "short_liquidation_usd": 0.0,
            "liquidation_count": 0,
            "last_event_ms": 0,
        })
        if event.event_type == "trade":
            key_name = "trade_buy_usd" if event.side == "buy" else "trade_sell_usd"
            row[key_name] += event.notional_usd
            row["cvd_usd"] += event.notional_usd if event.side == "buy" else -event.notional_usd
            row["trade_count"] += 1
            if row["price_high"] is None:
                row["price_high"] = event.price
                row["price_low"] = event.price
            else:
                row["price_high"] = max(float(row["price_high"]), event.price)
                row["price_low"] = min(float(row["price_low"]), event.price)
            if not int(row["first_trade_ms"]) or event.event_time_ms < int(row["first_trade_ms"]):
                row["first_trade_ms"] = event.event_time_ms
                row["price_open"] = event.price
            if event.event_time_ms >= int(row["last_trade_ms"]):
                row["last_trade_ms"] = event.event_time_ms
                row["price_close"] = event.price
        elif event.event_type == "liquidation":
            key_name = "long_liquidation_usd" if event.position_side == "long" else "short_liquidation_usd"
            row[key_name] += event.notional_usd
            row["liquidation_count"] += 1
        else:
            event_ids.remove(event.event_id)
            self._invalid_events += 1
            return False
        row["last_event_ms"] = max(int(row["last_event_ms"]), event.event_time_ms)
        self._accepted_events += 1
        return True

    def finalize_ready(self, now_ms: int, *, grace_ms: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            return self._finalize_ready(now_ms, grace_ms=grace_ms)

    def seed_finalized_through(self, watermarks: dict[tuple[str, str, str], int]) -> None:
        with self._lock:
            for raw_key, raw_value in watermarks.items():
                if len(raw_key) != 3:
                    continue
                key = tuple(str(part) for part in raw_key)
                value = max(0, int(raw_value or 0))
                self._finalized_through_ms[key] = max(
                    self._finalized_through_ms.get(key, 0),
                    value,
                )

    def _finalize_ready(self, now_ms: int, *, grace_ms: int = 0) -> list[dict[str, Any]]:
        cutoff_ms = max(0, int(now_ms) - max(0, int(grace_ms)))
        ready_keys = [
            key for key, row in self._buckets.items()
            if (int(row["bucket_start"]) + int(row["bucket_sec"])) * 1000 <= cutoff_ms
        ]
        ready_keys.sort(key=lambda key: (key[3], key[2], key[0], key[1]))
        rows: list[dict[str, Any]] = []
        for key in ready_keys:
            row = self._buckets.pop(key)
            self._event_ids.pop(key, None)
            rows.append(dict(row))
            series_key = (str(row["exchange"]), str(row["market"]), str(row["symbol"]))
            self._finalized_through_ms[series_key] = max(
                self._finalized_through_ms.get(series_key, 0),
                (int(row["bucket_start"]) + int(row["bucket_sec"])) * 1000,
            )
        return rows

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "accepted_events": self._accepted_events,
                "duplicate_events": self._duplicate_events,
                "late_events": self._late_events,
                "invalid_events": self._invalid_events,
                "open_buckets": len(self._buckets),
            }


class RealtimeFeatureStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=15000")
            self._ensure_schema(conn)
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS realtime_market_features (
                exchange TEXT NOT NULL,
                market TEXT NOT NULL,
                symbol TEXT NOT NULL,
                bucket_start INTEGER NOT NULL,
                bucket_sec INTEGER NOT NULL,
                trade_buy_usd REAL NOT NULL DEFAULT 0,
                trade_sell_usd REAL NOT NULL DEFAULT 0,
                cvd_usd REAL NOT NULL DEFAULT 0,
                trade_count INTEGER NOT NULL DEFAULT 0,
                price_open REAL NOT NULL DEFAULT 0,
                price_high REAL NOT NULL DEFAULT 0,
                price_low REAL NOT NULL DEFAULT 0,
                price_close REAL NOT NULL DEFAULT 0,
                long_liquidation_usd REAL NOT NULL DEFAULT 0,
                short_liquidation_usd REAL NOT NULL DEFAULT 0,
                liquidation_count INTEGER NOT NULL DEFAULT 0,
                last_event_ms INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                PRIMARY KEY(exchange, market, symbol, bucket_start, bucket_sec)
            );
            CREATE INDEX IF NOT EXISTS idx_realtime_symbol_time
                ON realtime_market_features(symbol, bucket_start DESC);
            CREATE TABLE IF NOT EXISTS realtime_feature_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        existing_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(realtime_market_features)").fetchall()
        }
        for column in ("price_open", "price_high", "price_low", "price_close"):
            if column not in existing_columns:
                try:
                    conn.execute(
                        f"ALTER TABLE realtime_market_features ADD COLUMN {column} REAL NOT NULL DEFAULT 0"
                    )
                except sqlite3.OperationalError as exc:
                    if "duplicate column" not in str(exc).lower():
                        raise
        conn.execute(
            "INSERT OR REPLACE INTO realtime_feature_meta(key, value) VALUES('schema_version', ?)",
            (str(REALTIME_FEATURE_SCHEMA_VERSION),),
        )

    def replace_many(self, rows: list[dict[str, Any]]) -> int:
        now = int(time.time())
        written = 0
        with self.connect() as conn:
            for row in rows:
                symbol = str(row.get("symbol") or "").upper()
                if not symbol.endswith("USDT"):
                    continue
                conn.execute(
                    """
                    INSERT INTO realtime_market_features(
                        exchange, market, symbol, bucket_start, bucket_sec,
                        trade_buy_usd, trade_sell_usd, cvd_usd, trade_count,
                        price_open, price_high, price_low, price_close,
                        long_liquidation_usd, short_liquidation_usd, liquidation_count,
                        last_event_ms, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(exchange, market, symbol, bucket_start, bucket_sec) DO UPDATE SET
                        trade_buy_usd=excluded.trade_buy_usd,
                        trade_sell_usd=excluded.trade_sell_usd,
                        cvd_usd=excluded.cvd_usd,
                        trade_count=excluded.trade_count,
                        price_open=excluded.price_open,
                        price_high=excluded.price_high,
                        price_low=excluded.price_low,
                        price_close=excluded.price_close,
                        long_liquidation_usd=excluded.long_liquidation_usd,
                        short_liquidation_usd=excluded.short_liquidation_usd,
                        liquidation_count=excluded.liquidation_count,
                        last_event_ms=excluded.last_event_ms,
                        updated_at=excluded.updated_at
                    """,
                    (
                        str(row.get("exchange") or ""),
                        str(row.get("market") or ""),
                        symbol,
                        int(row.get("bucket_start") or 0),
                        max(1, int(row.get("bucket_sec") or 60)),
                        float(row.get("trade_buy_usd") or 0),
                        float(row.get("trade_sell_usd") or 0),
                        float(row.get("cvd_usd") or 0),
                        int(row.get("trade_count") or 0),
                        float(row.get("price_open") or 0),
                        float(row.get("price_high") or 0),
                        float(row.get("price_low") or 0),
                        float(row.get("price_close") or 0),
                        float(row.get("long_liquidation_usd") or 0),
                        float(row.get("short_liquidation_usd") or 0),
                        int(row.get("liquidation_count") or 0),
                        int(row.get("last_event_ms") or 0),
                        now,
                    ),
                )
                written += 1
        return written

    def latest_by_symbol(self, *, now_ts: int | None = None, max_age_sec: int = 180) -> list[dict[str, Any]]:
        now = int(now_ts or time.time())
        oldest_end = now - max(1, int(max_age_sec))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM realtime_market_features
                WHERE bucket_start + bucket_sec >= ?
                ORDER BY bucket_start DESC, symbol ASC
                """,
                (oldest_end,),
            ).fetchall()
        latest: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            key = (str(item["exchange"]), str(item["market"]), str(item["symbol"]))
            latest.setdefault(key, item)
        return list(latest.values())

    def health_summary(self, *, now_ts: int | None = None, fresh_sec: int = 180) -> dict[str, Any]:
        now = int(now_ts or time.time())
        with self.connect() as conn:
            counts = conn.execute(
                """
                SELECT COUNT(*) AS feature_count, COUNT(DISTINCT symbol) AS symbol_count
                FROM realtime_market_features
                """
            ).fetchone()
            latest = conn.execute(
                """
                SELECT bucket_start + bucket_sec AS bucket_end
                FROM realtime_market_features
                ORDER BY bucket_end DESC
                LIMIT 1
                """
            ).fetchone()
            exchange_rows = conn.execute(
                """
                SELECT exchange, COUNT(*) AS feature_count,
                       COUNT(DISTINCT symbol) AS symbol_count,
                       MAX(bucket_start + bucket_sec) AS latest_bucket_end
                FROM realtime_market_features
                GROUP BY exchange
                ORDER BY exchange ASC
                """
            ).fetchall()
        latest_end = int(latest["bucket_end"] or 0) if latest else 0
        age_sec = max(0, now - latest_end) if latest_end else None
        if age_sec is None:
            status = "empty"
        else:
            status = "ready" if age_sec <= max(1, int(fresh_sec)) else "stale"
        exchange_health: dict[str, dict[str, Any]] = {}
        for row in exchange_rows:
            exchange_latest = int(row["latest_bucket_end"] or 0)
            exchange_age = max(0, now - exchange_latest) if exchange_latest else None
            exchange_health[str(row["exchange"])] = {
                "status": "ready" if exchange_age is not None and exchange_age <= max(1, int(fresh_sec)) else "stale",
                "feature_count": int(row["feature_count"] or 0),
                "symbol_count": int(row["symbol_count"] or 0),
                "latest_bucket_end": exchange_latest,
                "age_sec": exchange_age,
            }
        return {
            "status": status,
            "feature_count": int(counts["feature_count"] or 0) if counts else 0,
            "symbol_count": int(counts["symbol_count"] or 0) if counts else 0,
            "latest_bucket_end": latest_end,
            "age_sec": age_sec,
            "exchanges": exchange_health,
        }

    def recent_rows(self, *, now_ts: int | None = None, window_sec: int = 86_400) -> list[dict[str, Any]]:
        now = int(now_ts or time.time())
        oldest_end = now - max(1, int(window_sec))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM realtime_market_features
                WHERE bucket_start + bucket_sec > ?
                  AND bucket_start + bucket_sec <= ?
                ORDER BY symbol ASC, bucket_start ASC, exchange ASC, market ASC
                """,
                (oldest_end, now),
            ).fetchall()
        return [dict(row) for row in rows]

    def finalized_watermarks(self) -> dict[tuple[str, str, str], int]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT exchange, market, symbol,
                       MAX((bucket_start + bucket_sec) * 1000) AS finalized_through_ms
                FROM realtime_market_features
                GROUP BY exchange, market, symbol
                """
            ).fetchall()
        return {
            (str(row["exchange"]), str(row["market"]), str(row["symbol"])):
                int(row["finalized_through_ms"] or 0)
            for row in rows
        }

    def prune(self, *, before_ts: int) -> int:
        with self.connect() as conn:
            cursor = conn.execute(
                "DELETE FROM realtime_market_features WHERE bucket_start + bucket_sec < ?",
                (int(before_ts),),
            )
            return int(cursor.rowcount or 0)


class RealtimeMarketPipeline:
    def __init__(
        self,
        store: RealtimeFeatureStore,
        *,
        bucket_sec: int = 60,
        grace_ms: int = 2_000,
    ):
        self.store = store
        self.grace_ms = max(0, int(grace_ms))
        self.aggregator = RealtimeFeatureAggregator(bucket_sec=bucket_sec)
        self.messages = 0
        self.decode_errors = 0
        self.last_message_ms = 0
        self.last_flush_ms = 0

    def handle_message(self, message: str | bytes | dict[str, Any]) -> bool:
        if isinstance(message, bytes):
            try:
                message = message.decode("utf-8")
            except UnicodeDecodeError:
                self.decode_errors += 1
                return False
        if isinstance(message, str):
            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                self.decode_errors += 1
                return False
        else:
            payload = message
        event = parse_binance_market_event(payload)
        return self.handle_event(event)

    def handle_event(self, event: MarketEvent | None) -> bool:
        accepted = self.aggregator.add(event)
        self.messages += 1
        if event is not None:
            self.last_message_ms = max(self.last_message_ms, event.event_time_ms)
        return accepted

    def flush(self, *, now_ms: int | None = None) -> int:
        current_ms = int(now_ms or time.time() * 1000)
        rows = self.aggregator.finalize_ready(current_ms, grace_ms=self.grace_ms)
        if not rows:
            return 0
        written = self.store.replace_many(rows)
        self.last_flush_ms = current_ms
        return written

    def stats(self) -> dict[str, Any]:
        return {
            **self.aggregator.stats(),
            "messages": self.messages,
            "decode_errors": self.decode_errors,
            "last_message_ms": self.last_message_ms,
            "last_flush_ms": self.last_flush_ms,
        }


def load_binance_realtime_symbols(settings: Any) -> list[str]:
    from .data_sources import BinanceDataSource

    source = BinanceDataSource(settings)
    try:
        valid_symbols = {
            str(item.get("symbol") or "").upper()
            for item in source.usdt_perp_symbols()
            if isinstance(item, dict)
        }
        return select_realtime_symbols(
            source.ticker_24h(),
            valid_symbols=valid_symbols,
            excluded_base_assets=set(getattr(settings, "excluded_base_assets", ())),
            min_quote_volume=float(getattr(settings, "realtime_market_min_quote_volume", 5_000_000) or 0),
            limit=int(getattr(settings, "realtime_market_symbol_limit", 80) or 80),
        )
    finally:
        source.http.close()


def load_bybit_realtime_symbols(settings: Any) -> list[str]:
    from .data_sources import DataQuality, HttpClient

    quality = DataQuality()
    client = HttpClient(settings, quality)
    base_url = str(getattr(settings, "bybit_public_rest_url", "https://api.bybit.com") or "https://api.bybit.com")
    try:
        instruments = client.get_json(
            f"{base_url}/v5/market/instruments-info",
            params={"category": "linear", "limit": 1000},
            cache_key="realtime:bybit:linear:instruments",
            quality_key="realtime:bybit:instruments",
        )
        tickers = client.get_json(
            f"{base_url}/v5/market/tickers",
            params={"category": "linear"},
            cache_key="realtime:bybit:linear:tickers",
            quality_key="realtime:bybit:tickers",
        )
        return select_bybit_realtime_symbols(
            instruments,
            tickers,
            excluded_base_assets=set(getattr(settings, "excluded_base_assets", ())),
            min_quote_volume=float(getattr(settings, "realtime_market_min_quote_volume", 5_000_000) or 0),
            limit=int(getattr(settings, "realtime_market_symbol_limit", 80) or 80),
        )
    finally:
        client.close()


def load_okx_realtime_contracts(settings: Any) -> tuple[list[str], dict[str, dict[str, Any]]]:
    from .data_sources import DataQuality, HttpClient

    quality = DataQuality()
    client = HttpClient(settings, quality)
    base_url = str(getattr(settings, "okx_public_rest_url", "https://www.okx.com") or "https://www.okx.com")
    try:
        instruments = client.get_json(
            f"{base_url}/api/v5/public/instruments",
            params={"instType": "SWAP"},
            cache_key="realtime:okx:swap:instruments",
            quality_key="realtime:okx:instruments",
        )
        tickers = client.get_json(
            f"{base_url}/api/v5/market/tickers",
            params={"instType": "SWAP"},
            cache_key="realtime:okx:swap:tickers",
            quality_key="realtime:okx:tickers",
        )
        return select_okx_realtime_contracts(
            instruments,
            tickers,
            excluded_base_assets=set(getattr(settings, "excluded_base_assets", ())),
            min_quote_volume=float(getattr(settings, "realtime_market_min_quote_volume", 5_000_000) or 0),
            limit=int(getattr(settings, "realtime_market_symbol_limit", 80) or 80),
        )
    finally:
        client.close()


def bybit_stream_subscriptions(symbols: list[str]) -> list[str]:
    topics: list[str] = []
    for symbol in symbols:
        normalized = str(symbol or "").upper()
        if not normalized.endswith("USDT"):
            continue
        topics.extend((f"publicTrade.{normalized}", f"allLiquidation.{normalized}"))
    return topics


def okx_stream_subscriptions(contract_specs: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {"channel": "trades", "instId": inst_id}
        for inst_id in sorted(contract_specs)
    ]


class BinanceRealtimeMarketService:
    service_name = "binance_realtime_market"
    thread_name = "binance-market-websocket"

    def __init__(
        self,
        settings: Any,
        *,
        store: RealtimeFeatureStore | None = None,
        websocket_app_factory: Any = None,
    ):
        self.settings = settings
        self.store = store or RealtimeFeatureStore(settings.realtime_features_db_path)
        self.pipeline = RealtimeMarketPipeline(
            self.store,
            bucket_sec=int(getattr(settings, "realtime_market_bucket_sec", 60) or 60),
            grace_ms=int(getattr(settings, "realtime_market_grace_ms", 2_000) or 0),
        )
        self.pipeline.aggregator.seed_finalized_through(self.store.finalized_watermarks())
        self.websocket_app_factory = websocket_app_factory
        self.connection_attempts = 0
        self.connection_errors = 0
        self.last_error = ""
        self.symbol_count = 0
        self.open_count = 0
        self.subscription_acks = 0
        self.control_messages = 0
        self.last_open_ms = 0
        self.last_receive_ms = 0
        self._subscription_id = 1
        self._connection_context: dict[str, Any] = {}
        self._cached_connection: tuple[list[str], list[Any], dict[str, Any]] | None = None
        self._connection_cache_until = 0.0
        self._connected = threading.Event()
        self._last_receive_mono = 0.0

    def _factory(self) -> Any:
        if self.websocket_app_factory is not None:
            return self.websocket_app_factory
        from websocket import WebSocketApp

        return WebSocketApp

    def _websocket_url(self) -> str:
        return str(
            getattr(self.settings, "binance_futures_ws_url", "wss://fstream.binance.com/market/ws")
            or "wss://fstream.binance.com/market/ws"
        )

    def _load_connection(self) -> tuple[list[str], list[Any], dict[str, Any]]:
        symbols = load_binance_realtime_symbols(self.settings)
        subscriptions = binance_stream_subscriptions(
            symbols,
            limit=int(getattr(self.settings, "realtime_market_symbol_limit", 80) or 80),
        )
        return symbols, subscriptions, {}

    def _connection_definition(self) -> tuple[list[str], list[Any], dict[str, Any]]:
        now = time.monotonic()
        if self._cached_connection is not None and now < self._connection_cache_until:
            return self._cached_connection
        loaded = self._load_connection()
        refresh_sec = max(
            30,
            int(getattr(self.settings, "realtime_market_symbol_refresh_sec", 300) or 300),
        )
        self._cached_connection = loaded
        self._connection_cache_until = now + refresh_sec
        return loaded

    def _subscription_payload(self, subscriptions: list[Any]) -> dict[str, Any]:
        payload = {"method": "SUBSCRIBE", "params": subscriptions, "id": self._subscription_id}
        self._subscription_id += 1
        return payload

    def _handle_control(self, payload: dict[str, Any]) -> bool:
        if "result" not in payload or "id" not in payload:
            return False
        self.control_messages += 1
        if payload.get("result") is None:
            self.subscription_acks += 1
        return True

    def _events_from_payload(self, payload: dict[str, Any]) -> list[MarketEvent]:
        event = parse_binance_market_event(payload)
        return [event] if event is not None else []

    def _keepalive_payload(self) -> str | None:
        return None

    def _on_open(self, ws: Any, subscriptions: list[Any]) -> None:
        self.open_count += 1
        self.last_open_ms = int(time.time() * 1000)
        self._last_receive_mono = time.monotonic()
        self._connected.set()
        ws.send(json.dumps(self._subscription_payload(subscriptions), separators=(",", ":")))

    def _on_message(self, _ws: Any, message: str | bytes) -> None:
        self.last_receive_ms = int(time.time() * 1000)
        self._last_receive_mono = time.monotonic()
        try:
            raw_message = message.decode("utf-8") if isinstance(message, bytes) else message
            if raw_message == "pong":
                self.control_messages += 1
                return
            control = json.loads(raw_message)
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
            self.pipeline.decode_errors += 1
            return
        if not isinstance(control, dict):
            return
        if self._handle_control(control):
            return
        for event in self._events_from_payload(control):
            self.pipeline.handle_event(event)

    def _on_error(self, _ws: Any, error: Any) -> None:
        self.connection_errors += 1
        self.last_error = f"{type(error).__name__}: {error}"[:300]

    def _on_close(self, _ws: Any, status_code: Any, message: Any) -> None:
        self._connected.clear()
        if status_code not in (None, 1000):
            self.last_error = f"closed:{status_code}:{message}"[:300]

    def stats(self) -> dict[str, Any]:
        return {
            "service": self.service_name,
            "symbol_count": self.symbol_count,
            "connection_attempts": self.connection_attempts,
            "connection_errors": self.connection_errors,
            "last_error": self.last_error,
            "open_count": self.open_count,
            "subscription_acks": self.subscription_acks,
            "control_messages": self.control_messages,
            "last_open_ms": self.last_open_ms,
            "last_receive_ms": self.last_receive_ms,
            **self.pipeline.stats(),
        }

    def run(self, stop_event: threading.Event | None = None) -> None:
        stop = stop_event or threading.Event()
        reconnect_delay = max(1, int(getattr(self.settings, "realtime_market_reconnect_sec", 5) or 5))
        flush_interval = max(1, int(getattr(self.settings, "realtime_market_flush_interval_sec", 1) or 1))
        connect_timeout = max(5, int(getattr(self.settings, "realtime_market_connect_timeout_sec", 15) or 15))
        idle_timeout = max(10, int(getattr(self.settings, "realtime_market_idle_timeout_sec", 30) or 30))
        retention_days = max(1, int(getattr(self.settings, "realtime_market_retention_days", 7) or 7))
        next_prune = 0.0
        while not stop.is_set():
            try:
                symbols, subscriptions, context = self._connection_definition()
            except Exception as exc:
                self.connection_errors += 1
                self.last_error = f"symbol_load:{type(exc).__name__}: {exc}"[:300]
                stop.wait(max(30, reconnect_delay))
                continue
            if not symbols:
                self.connection_errors += 1
                self.last_error = "symbol_load:empty"
                stop.wait(max(30, reconnect_delay))
                continue
            self.symbol_count = len(symbols)
            self._connection_context = context
            self.connection_attempts += 1
            open_count_before = self.open_count
            errors_before = self.connection_errors
            self._connected.clear()
            self._last_receive_mono = 0.0
            factory = self._factory()
            ws = factory(
                self._websocket_url(),
                on_open=lambda app: self._on_open(app, subscriptions),
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close,
            )
            runner = threading.Thread(
                target=ws.run_forever,
                kwargs={"ping_interval": 20, "ping_timeout": 10, "skip_utf8_validation": True},
                name=self.thread_name,
                daemon=True,
            )
            runner.start()
            connect_deadline = time.monotonic() + connect_timeout
            last_keepalive = 0.0
            while runner.is_alive() and not stop.wait(flush_interval):
                self.pipeline.flush()
                now = time.time()
                now_mono = time.monotonic()
                keepalive = self._keepalive_payload()
                if (
                    keepalive is not None
                    and self._connected.is_set()
                    and self._last_receive_mono
                    and now_mono - self._last_receive_mono >= 15
                    and now_mono - last_keepalive >= 15
                ):
                    try:
                        ws.send(keepalive)
                        last_keepalive = now_mono
                    except Exception:
                        pass
                if not self._connected.is_set() and now_mono >= connect_deadline:
                    self.connection_errors += 1
                    self.last_error = "connect_timeout"
                    try:
                        ws.close()
                    except Exception:
                        pass
                    break
                if self._connected.is_set() and self._last_receive_mono and now_mono - self._last_receive_mono >= idle_timeout:
                    self.connection_errors += 1
                    self.last_error = "stream_idle_timeout"
                    try:
                        ws.close()
                    except Exception:
                        pass
                    break
                if now >= next_prune:
                    self.store.prune(before_ts=int(now) - retention_days * 86400)
                    next_prune = now + 3600
            if self.open_count == open_count_before and self.connection_errors == errors_before:
                self.connection_errors += 1
                self.last_error = "connection_ended_before_open"
            elif (
                self.open_count > open_count_before
                and not stop.is_set()
                and self.connection_errors == errors_before
            ):
                self.connection_errors += 1
                self.last_error = "unexpected_disconnect"
            try:
                ws.close()
            except Exception:
                pass
            runner.join(timeout=5)
            self.pipeline.flush()
            if not stop.is_set():
                stop.wait(reconnect_delay)


class BybitRealtimeMarketService(BinanceRealtimeMarketService):
    service_name = "bybit_realtime_market"
    thread_name = "bybit-market-websocket"

    def _websocket_url(self) -> str:
        return str(
            getattr(self.settings, "bybit_linear_ws_url", "wss://stream.bybit.com/v5/public/linear")
            or "wss://stream.bybit.com/v5/public/linear"
        )

    def _load_connection(self) -> tuple[list[str], list[Any], dict[str, Any]]:
        symbols = load_bybit_realtime_symbols(self.settings)
        return symbols, bybit_stream_subscriptions(symbols), {}

    def _subscription_payload(self, subscriptions: list[Any]) -> dict[str, Any]:
        return {"op": "subscribe", "args": subscriptions}

    def _handle_control(self, payload: dict[str, Any]) -> bool:
        operation = str(payload.get("op") or "")
        if operation not in {"subscribe", "pong", "ping"}:
            return False
        self.control_messages += 1
        if operation == "subscribe" and bool(payload.get("success")):
            self.subscription_acks += 1
        return True

    def _events_from_payload(self, payload: dict[str, Any]) -> list[MarketEvent]:
        return parse_bybit_market_events(payload)

    def _keepalive_payload(self) -> str | None:
        return json.dumps({"op": "ping"}, separators=(",", ":"))


class OkxRealtimeMarketService(BinanceRealtimeMarketService):
    service_name = "okx_realtime_market"
    thread_name = "okx-market-websocket"

    def _websocket_url(self) -> str:
        return str(
            getattr(self.settings, "okx_public_ws_url", "wss://ws.okx.com:8443/ws/v5/public")
            or "wss://ws.okx.com:8443/ws/v5/public"
        )

    def _load_connection(self) -> tuple[list[str], list[Any], dict[str, Any]]:
        symbols, contract_specs = load_okx_realtime_contracts(self.settings)
        return symbols, okx_stream_subscriptions(contract_specs), {"contract_specs": contract_specs}

    def _subscription_payload(self, subscriptions: list[Any]) -> dict[str, Any]:
        payload = {"id": str(self._subscription_id), "op": "subscribe", "args": subscriptions}
        self._subscription_id += 1
        return payload

    def _handle_control(self, payload: dict[str, Any]) -> bool:
        event = str(payload.get("event") or "")
        if event not in {"subscribe", "unsubscribe", "error"}:
            return False
        self.control_messages += 1
        if event == "subscribe":
            self.subscription_acks += 1
        elif event == "error":
            self.connection_errors += 1
            self.last_error = f"subscription_error:{str(payload.get('code') or 'unknown')[:24]}"
        return True

    def _events_from_payload(self, payload: dict[str, Any]) -> list[MarketEvent]:
        return parse_okx_market_events(
            payload,
            contract_specs=self._connection_context.get("contract_specs") or {},
        )

    def _keepalive_payload(self) -> str | None:
        return "ping"


def build_realtime_market_services(settings: Any) -> list[BinanceRealtimeMarketService]:
    services: list[BinanceRealtimeMarketService] = [BinanceRealtimeMarketService(settings)]
    if bool(getattr(settings, "realtime_bybit_enable", True)):
        services.append(BybitRealtimeMarketService(settings))
    if bool(getattr(settings, "realtime_okx_enable", True)):
        services.append(OkxRealtimeMarketService(settings))
    return services


def run_realtime_market_service(settings: Any, *, duration_sec: float = 0) -> int:
    services = build_realtime_market_services(settings)
    stop = threading.Event()
    failures: list[str] = []
    deadline = time.monotonic() + max(0.0, float(duration_sec or 0)) if duration_sec else 0.0

    def run_one(service: BinanceRealtimeMarketService) -> None:
        try:
            service.run(stop)
            if not stop.is_set():
                failures.append(f"{service.service_name}:unexpected_exit")
                stop.set()
        except Exception as exc:
            failures.append(f"{service.service_name}:{type(exc).__name__}")
            stop.set()

    threads = [
        threading.Thread(target=run_one, args=(service,), name=service.service_name, daemon=True)
        for service in services
    ]
    exchange_stats: list[dict[str, Any]] = []
    try:
        for thread in threads:
            thread.start()
        while not stop.wait(1):
            if deadline and time.monotonic() >= deadline:
                stop.set()
                break
            if not any(thread.is_alive() for thread in threads):
                break
    except KeyboardInterrupt:
        stop.set()
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=10)
        exchange_stats = [service.stats() for service in services]
        if duration_sec and not any(
            int(stats.get("accepted_events") or 0) > 0
            for stats in exchange_stats
        ):
            failures.append("bounded_verification:no_exchange_received_events")
        print(json.dumps({
            "service": "multi_exchange_realtime_market",
            "failures": failures,
            "exchanges": exchange_stats,
        }, ensure_ascii=False))
    return 1 if failures else 0


__all__ = [
    "MarketEvent",
    "REALTIME_FEATURE_SCHEMA_VERSION",
    "RealtimeFeatureAggregator",
    "RealtimeFeatureStore",
    "RealtimeMarketPipeline",
    "BinanceRealtimeMarketService",
    "BybitRealtimeMarketService",
    "OkxRealtimeMarketService",
    "binance_stream_subscriptions",
    "build_realtime_radar_boards",
    "parse_binance_market_event",
    "parse_bybit_market_events",
    "parse_okx_market_events",
    "load_binance_realtime_symbols",
    "load_bybit_realtime_symbols",
    "load_okx_realtime_contracts",
    "bybit_stream_subscriptions",
    "okx_stream_subscriptions",
    "build_realtime_market_services",
    "run_realtime_market_service",
    "select_realtime_symbols",
    "select_bybit_realtime_symbols",
    "select_okx_realtime_contracts",
]
