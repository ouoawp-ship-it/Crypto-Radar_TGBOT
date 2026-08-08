from __future__ import annotations

import math
import json
import sqlite3
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping


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


def _subscription_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    return symbol if symbol.endswith("USDT") and len(symbol) > 4 else ""


def _subscription_stream(value: Any) -> str:
    stream = str(value or "").strip()
    if stream.lower() == "!forceorder@arr":
        return "!forceOrder@arr"
    if not stream or any(character.isspace() for character in stream) or "@" not in stream:
        return ""
    symbol, separator, channel = stream.partition("@")
    if not separator or not symbol or not channel:
        return ""
    canonical_channels = {
        "aggtrade": "aggTrade",
        "markprice": "markPrice",
    }
    return f"{symbol.lower()}@{canonical_channels.get(channel.lower(), channel)}"


@dataclass(frozen=True)
class BinanceSubscriptionPlan:
    requested_base_symbols: tuple[str, ...]
    requested_candidate_symbols: tuple[str, ...]
    base_symbols: tuple[str, ...]
    candidate_symbols: tuple[str, ...]
    union_symbols: tuple[str, ...]
    subscriptions: tuple[str, ...]
    omitted_base_symbols: tuple[str, ...]
    omitted_candidate_symbols: tuple[str, ...]
    max_streams: int
    expected_stream_count: int
    capacity_degraded: bool
    candidate_capacity_degraded: bool

    @property
    def actual_stream_count(self) -> int:
        return len(self.subscriptions)

    def stats(self) -> dict[str, Any]:
        return {
            "requested_base_symbol_count": len(self.requested_base_symbols),
            "requested_candidate_symbol_count": len(self.requested_candidate_symbols),
            "base_symbol_count": len(self.base_symbols),
            "candidate_symbol_count": len(self.candidate_symbols),
            "union_symbol_count": len(self.union_symbols),
            "expected_stream_count": self.expected_stream_count,
            "actual_stream_count": self.actual_stream_count,
            "max_streams": self.max_streams,
            "omitted_base_symbol_count": len(self.omitted_base_symbols),
            "omitted_candidate_symbol_count": len(self.omitted_candidate_symbols),
            "capacity_degraded": self.capacity_degraded,
            "candidate_capacity_degraded": self.candidate_capacity_degraded,
        }


def build_binance_subscription_plan(
    base_symbols: Iterable[Any],
    candidate_symbols: Iterable[Any],
    *,
    max_streams: int,
) -> BinanceSubscriptionPlan:
    """Build one deterministic Binance stream plan with candidate-first capacity."""

    requested_base: list[str] = []
    seen_base: set[str] = set()
    for value in base_symbols:
        symbol = _subscription_symbol(value)
        if symbol and symbol not in seen_base:
            seen_base.add(symbol)
            requested_base.append(symbol)

    requested_candidates = sorted({
        symbol
        for value in candidate_symbols
        if (symbol := _subscription_symbol(value))
    })
    requested_candidate_set = set(requested_candidates)
    safe_max_streams = max(1, int(max_streams or 1))
    remaining = safe_max_streams - 1  # The all-market liquidation stream is mandatory.

    selected_candidates: list[str] = []
    omitted_candidates: list[str] = []
    for symbol in requested_candidates:
        if remaining >= 2:
            selected_candidates.append(symbol)
            remaining -= 2
        else:
            omitted_candidates.append(symbol)

    selected_base_only: list[str] = []
    omitted_base: list[str] = []
    for symbol in requested_base:
        if symbol in selected_candidates:
            continue
        if remaining >= 1:
            selected_base_only.append(symbol)
            remaining -= 1
        else:
            omitted_base.append(symbol)

    selected_union = sorted(set(selected_candidates) | set(selected_base_only))
    selected_base = tuple(
        symbol for symbol in requested_base if symbol in set(selected_union)
    )
    agg_trade_streams = [f"{symbol.lower()}@aggTrade" for symbol in selected_union]
    mark_price_streams = [f"{symbol.lower()}@markPrice" for symbol in selected_candidates]
    subscriptions = tuple(agg_trade_streams + mark_price_streams + ["!forceOrder@arr"])
    expected_stream_count = (
        1
        + len(set(requested_base) | requested_candidate_set)
        + len(requested_candidates)
    )
    capacity_degraded = len(subscriptions) < expected_stream_count
    return BinanceSubscriptionPlan(
        requested_base_symbols=tuple(requested_base),
        requested_candidate_symbols=tuple(requested_candidates),
        base_symbols=selected_base,
        candidate_symbols=tuple(selected_candidates),
        union_symbols=tuple(selected_union),
        subscriptions=subscriptions,
        omitted_base_symbols=tuple(omitted_base),
        omitted_candidate_symbols=tuple(omitted_candidates),
        max_streams=safe_max_streams,
        expected_stream_count=expected_stream_count,
        capacity_degraded=capacity_degraded,
        candidate_capacity_degraded=bool(omitted_candidates),
    )


@dataclass(frozen=True)
class SubscriptionCommand:
    request_id: int
    method: str
    streams: tuple[str, ...]
    generation: int
    sent_monotonic: float

    def payload(self) -> dict[str, Any]:
        return {"method": self.method, "params": list(self.streams), "id": self.request_id}


@dataclass(frozen=True)
class SubscriptionAck:
    request_id: int | None
    status: str
    method: str = ""
    streams: tuple[str, ...] = ()
    error: str = ""
    generation: int = 0


class SubscriptionLedger:
    """Thread-safe desired/active subscription state driven by Binance ACKs."""

    def __init__(
        self,
        *,
        batch_size: int = 50,
        min_interval_sec: float = 0.25,
        ack_timeout_sec: float = 10.0,
        protected_streams: Iterable[str] = ("!forceOrder@arr",),
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.batch_size = max(1, int(batch_size or 1))
        self.min_interval_sec = max(0.0, float(min_interval_sec or 0.0))
        self.ack_timeout_sec = max(0.001, float(ack_timeout_sec or 0.001))
        self._clock = monotonic or time.monotonic
        self._protected = frozenset(
            stream for value in protected_streams if (stream := _subscription_stream(value))
        )
        self._desired: set[str] = set(self._protected)
        self._active: set[str] = set()
        self._stale_actual: set[str] = set()
        self._pending: dict[int, SubscriptionCommand] = {}
        self._completed_ids: set[int] = set()
        self._completed_order: deque[int] = deque()
        self._next_request_id = 1
        self._generation = 0
        self._last_command_monotonic: float | None = None
        self._subscribe_success = 0
        self._subscribe_failure = 0
        self._unsubscribe_success = 0
        self._unsubscribe_failure = 0
        self._ack_timeouts = 0
        self._duplicate_acks = 0
        self._unknown_acks = 0
        self._invalid_acks = 0
        self._stale_acks = 0
        self._lock = threading.RLock()

    @property
    def desired_subscriptions(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._desired)

    @property
    def active_subscriptions(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._active)

    @property
    def pending_subscribe(self) -> frozenset[str]:
        with self._lock:
            return frozenset(
                stream
                for command in self._pending.values()
                if command.method == "SUBSCRIBE"
                for stream in command.streams
            )

    @property
    def pending_unsubscribe(self) -> frozenset[str]:
        with self._lock:
            return frozenset(
                stream
                for command in self._pending.values()
                if command.method == "UNSUBSCRIBE"
                for stream in command.streams
            )

    @property
    def pending_requests(self) -> dict[int, SubscriptionCommand]:
        with self._lock:
            return dict(self._pending)

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def set_desired(self, streams: Iterable[Any]) -> dict[str, Any]:
        normalized = {
            stream for value in streams if (stream := _subscription_stream(value))
        } | set(self._protected)
        with self._lock:
            previous = set(self._desired)
            self._desired = normalized
            changed = previous != normalized
            if changed:
                self._generation += 1
            return {
                "changed": changed,
                "added": sorted(normalized - previous),
                "removed": sorted(previous - normalized),
                "desired": sorted(normalized),
                "generation": self._generation,
            }

    def reset_connection(self) -> int:
        with self._lock:
            for request_id in tuple(self._pending):
                self._remember_completed(request_id)
            self._active.clear()
            self._stale_actual.clear()
            self._pending.clear()
            self._generation += 1
            self._last_command_monotonic = None
            return self._generation

    def invalidate_active(self, streams: Iterable[Any]) -> tuple[str, ...]:
        """Require a fresh ACK for streams whose candidate epoch was retired."""

        normalized = {
            stream for value in streams if (stream := _subscription_stream(value))
        }
        with self._lock:
            invalidated = tuple(sorted(self._active & normalized))
            self._active.difference_update(invalidated)
            self._stale_actual.update(invalidated)
            return invalidated

    def next_command(self, *, now_monotonic: float | None = None) -> SubscriptionCommand | None:
        now = self._clock() if now_monotonic is None else float(now_monotonic)
        with self._lock:
            if (
                self._last_command_monotonic is not None
                and now - self._last_command_monotonic < self.min_interval_sec
            ):
                return None
            pending_subscribe = {
                stream
                for command in self._pending.values()
                if command.method == "SUBSCRIBE"
                for stream in command.streams
            }
            pending_unsubscribe = {
                stream
                for command in self._pending.values()
                if command.method == "UNSUBSCRIBE"
                for stream in command.streams
            }
            unsubscribe = sorted(
                (self._active | self._stale_actual)
                - self._desired
                - pending_unsubscribe
                - set(self._protected)
            )
            if unsubscribe:
                method = "UNSUBSCRIBE"
                selected = tuple(unsubscribe[: self.batch_size])
            else:
                subscribe = sorted(
                    self._desired - self._active - pending_subscribe
                )
                if not subscribe:
                    return None
                method = "SUBSCRIBE"
                selected = tuple(subscribe[: self.batch_size])
            request_id = self._next_request_id
            self._next_request_id += 1
            command = SubscriptionCommand(
                request_id=request_id,
                method=method,
                streams=selected,
                generation=self._generation,
                sent_monotonic=now,
            )
            self._pending[request_id] = command
            self._last_command_monotonic = now
            return command

    def handle_ack(self, payload: Mapping[str, Any]) -> SubscriptionAck:
        raw_request_id = payload.get("id") if isinstance(payload, Mapping) else None
        if (
            isinstance(raw_request_id, bool)
            or not isinstance(raw_request_id, int)
            or raw_request_id <= 0
        ):
            with self._lock:
                self._invalid_acks += 1
            return SubscriptionAck(None, "invalid")
        request_id = int(raw_request_id)
        with self._lock:
            if request_id in self._completed_ids:
                self._duplicate_acks += 1
                return SubscriptionAck(request_id, "duplicate")
            command = self._pending.pop(request_id, None)
            if command is None:
                self._unknown_acks += 1
                return SubscriptionAck(request_id, "unknown")
            success = (
                "result" in payload
                and payload.get("result") is None
                and "code" not in payload
            )
            if success:
                stale_generation = command.generation != self._generation
                if stale_generation:
                    if command.method == "SUBSCRIBE":
                        self._stale_actual.update(command.streams)
                    else:
                        self._stale_actual.difference_update(command.streams)
                        self._active.difference_update(command.streams)
                        self._active.update(self._protected & set(command.streams))
                    self._stale_acks += 1
                    status = "stale"
                elif command.method == "SUBSCRIBE":
                    self._active.update(command.streams)
                    self._stale_actual.difference_update(command.streams)
                    self._subscribe_success += 1
                    status = "success"
                else:
                    self._active.difference_update(command.streams)
                    self._stale_actual.difference_update(command.streams)
                    self._active.update(self._protected & set(command.streams))
                    self._unsubscribe_success += 1
                    status = "success"
                error = ""
            else:
                if command.method == "SUBSCRIBE":
                    self._subscribe_failure += 1
                else:
                    self._unsubscribe_failure += 1
                status = "failure"
                code = payload.get("code")
                message = str(payload.get("msg") or "subscription command rejected")
                error = f"{code}:{message}"[:300] if code is not None else message[:300]
            self._remember_completed(request_id)
            return SubscriptionAck(
                request_id,
                status,
                method=command.method,
                streams=command.streams,
                error=error,
                generation=command.generation,
            )

    def mark_send_failed(self, request_id: int, error: Any = "send_failed") -> SubscriptionAck:
        with self._lock:
            command = self._pending.pop(int(request_id), None)
            if command is None:
                return SubscriptionAck(int(request_id), "unknown")
            if command.method == "SUBSCRIBE":
                self._subscribe_failure += 1
            else:
                self._unsubscribe_failure += 1
            self._remember_completed(command.request_id)
            return SubscriptionAck(
                command.request_id,
                "failure",
                method=command.method,
                streams=command.streams,
                error=f"{type(error).__name__}: {error}"[:300],
                generation=command.generation,
            )

    def expire_timeouts(
        self,
        *,
        now_monotonic: float | None = None,
    ) -> list[SubscriptionCommand]:
        now = self._clock() if now_monotonic is None else float(now_monotonic)
        with self._lock:
            expired = [
                command
                for command in self._pending.values()
                if now - command.sent_monotonic >= self.ack_timeout_sec
            ]
            expired.sort(key=lambda command: command.request_id)
            for command in expired:
                self._pending.pop(command.request_id, None)
                self._remember_completed(command.request_id)
                self._ack_timeouts += 1
                if command.method == "SUBSCRIBE":
                    self._subscribe_failure += 1
                else:
                    self._unsubscribe_failure += 1
            return expired

    def stats(self) -> dict[str, Any]:
        with self._lock:
            pending_subscribe = sum(
                len(command.streams)
                for command in self._pending.values()
                if command.method == "SUBSCRIBE"
            )
            pending_unsubscribe = sum(
                len(command.streams)
                for command in self._pending.values()
                if command.method == "UNSUBSCRIBE"
            )
            return {
                "desired_subscription_count": len(self._desired),
                "active_subscription_count": len(self._active),
                "stale_actual_subscription_count": len(self._stale_actual),
                "pending_subscribe_count": pending_subscribe,
                "pending_unsubscribe_count": pending_unsubscribe,
                "pending_request_count": len(self._pending),
                "subscribe_success": self._subscribe_success,
                "subscribe_failure": self._subscribe_failure,
                "unsubscribe_success": self._unsubscribe_success,
                "unsubscribe_failure": self._unsubscribe_failure,
                "ack_timeouts": self._ack_timeouts,
                "duplicate_acks": self._duplicate_acks,
                "unknown_acks": self._unknown_acks,
                "invalid_acks": self._invalid_acks,
                "stale_acks": self._stale_acks,
                "subscription_generation": self._generation,
            }

    def _remember_completed(self, request_id: int) -> None:
        if request_id in self._completed_ids:
            return
        if len(self._completed_order) >= 4096:
            oldest = self._completed_order.popleft()
            self._completed_ids.discard(oldest)
        self._completed_order.append(request_id)
        self._completed_ids.add(request_id)


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


@dataclass(frozen=True)
class MarkPriceUpdate:
    symbol: str
    mark_price: float
    funding_rate: float
    next_funding_time_ms: int
    event_time_ms: int
    exchange: str = "binance"
    market: str = "futures"
    source: str = "binance_ws_mark_price"


def parse_binance_mark_price_update(payload: Any) -> MarkPriceUpdate | None:
    source = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
    if not isinstance(source, dict) or str(source.get("e") or "") != "markPriceUpdate":
        return None
    symbol = _subscription_symbol(source.get("s"))
    mark_price = _number(source.get("p"))
    funding_rate = _number(source.get("r"))
    event_time_ms = _timestamp_ms(source.get("E"))
    next_funding_time_ms = _timestamp_ms(source.get("T"))
    if (
        not symbol
        or mark_price is None
        or mark_price <= 0
        or funding_rate is None
        or not event_time_ms
        or not next_funding_time_ms
    ):
        return None
    return MarkPriceUpdate(
        symbol=symbol,
        mark_price=mark_price,
        funding_rate=funding_rate,
        next_funding_time_ms=next_funding_time_ms,
        event_time_ms=event_time_ms,
    )


class MarkPriceBook:
    """Bounded mark/funding history with closed-window funding deltas."""

    def __init__(self, *, max_history_per_symbol: int = 2_048) -> None:
        self._latest: dict[str, MarkPriceUpdate] = {}
        self._latest_epochs: dict[str, str] = {}
        self._history: dict[str, deque[tuple[MarkPriceUpdate, str]]] = {}
        self._max_history_per_symbol = max(16, int(max_history_per_symbol))
        self._accepted_updates = 0
        self._duplicate_updates = 0
        self._out_of_order_updates = 0
        self._invalid_updates = 0
        self._lock = threading.RLock()

    def update(
        self,
        value: MarkPriceUpdate | None,
        *,
        subscription_epoch: str = "",
    ) -> bool:
        if not self._valid(value):
            with self._lock:
                self._invalid_updates += 1
            return False
        assert value is not None
        with self._lock:
            previous = self._latest.get(value.symbol)
            if previous is not None and value.event_time_ms <= previous.event_time_ms:
                if value == previous:
                    self._duplicate_updates += 1
                else:
                    self._out_of_order_updates += 1
                return False
            self._latest[value.symbol] = value
            epoch = str(subscription_epoch or "")
            self._latest_epochs[value.symbol] = epoch
            history = self._history.setdefault(
                value.symbol,
                deque(maxlen=self._max_history_per_symbol),
            )
            history.append((value, epoch))
            self._accepted_updates += 1
            return True

    def latest(self, symbol: str) -> MarkPriceUpdate | None:
        normalized = _subscription_symbol(symbol)
        with self._lock:
            return self._latest.get(normalized)

    def snapshot(self, symbol: str) -> dict[str, Any] | None:
        normalized = _subscription_symbol(symbol)
        with self._lock:
            update = self._latest.get(normalized)
            if update is None:
                return None
            return {
                "symbol": update.symbol,
                "mark_price": update.mark_price,
                "funding_rate": update.funding_rate,
                "next_funding_time_ms": update.next_funding_time_ms,
                "event_time_ms": update.event_time_ms,
                "subscription_epoch": self._latest_epochs.get(normalized, ""),
                "exchange": update.exchange,
                "market": update.market,
                "source": update.source,
            }

    def snapshot_window(
        self,
        symbol: str,
        *,
        window_end_ms: int,
        window_sec: int = 300,
        subscription_epoch: str = "",
        epoch_started_ms: int = 0,
        max_gap_ms: int = 15_000,
    ) -> dict[str, Any] | None:
        """Return one complete, closed funding window or an explicit partial row."""

        normalized = _subscription_symbol(symbol)
        end_ms = int(window_end_ms)
        duration_ms = max(1, int(window_sec)) * 1_000
        start_ms = end_ms - duration_ms
        safe_gap = max(1, int(max_gap_ms))
        wanted_epoch = str(subscription_epoch or "")
        with self._lock:
            history = list(self._history.get(normalized, ()))
        eligible = [
            (update, epoch)
            for update, epoch in history
            if (not wanted_epoch or epoch == wanted_epoch)
            and update.event_time_ms >= max(1, int(epoch_started_ms or 0))
            and start_ms - safe_gap <= update.event_time_ms <= end_ms
        ]
        eligible.sort(key=lambda item: item[0].event_time_ms)

        start_candidates = [item for item in eligible if item[0].event_time_ms <= start_ms]
        if start_candidates:
            start_item = start_candidates[-1]
        else:
            after_start = [item for item in eligible if item[0].event_time_ms > start_ms]
            start_item = after_start[0] if after_start else None
        end_candidates = [item for item in eligible if item[0].event_time_ms <= end_ms]
        end_item = end_candidates[-1] if end_candidates else None

        quality = "complete"
        if start_item is None or end_item is None:
            quality = "insufficient_history"
        elif (
            abs(start_item[0].event_time_ms - start_ms) > safe_gap
            or end_ms - end_item[0].event_time_ms > safe_gap
            or end_item[0].event_time_ms <= start_item[0].event_time_ms
        ):
            quality = "insufficient_history"
        else:
            sequence = [
                item
                for item in eligible
                if start_item[0].event_time_ms <= item[0].event_time_ms <= end_item[0].event_time_ms
            ]
            if any(
                current[0].event_time_ms - previous[0].event_time_ms > safe_gap
                for previous, current in zip(sequence, sequence[1:])
            ):
                quality = "stale"

        endpoint = end_item[0] if end_item is not None else None
        output: dict[str, Any] = {
            "symbol": normalized,
            "mark_price": endpoint.mark_price if endpoint is not None else None,
            "funding_rate": endpoint.funding_rate if endpoint is not None else None,
            "funding_rate_start_5m": None,
            "funding_rate_end_5m": endpoint.funding_rate if endpoint is not None else None,
            "funding_rate_change_5m": None,
            "funding_rate_changed_5m": False,
            "funding_window_start_ms": start_ms,
            "funding_window_end_ms": end_ms,
            "funding_window_start_event_time_ms": (
                start_item[0].event_time_ms if start_item is not None else None
            ),
            "funding_window_end_event_time_ms": (
                endpoint.event_time_ms if endpoint is not None else None
            ),
            "funding_window_quality": quality,
            "next_funding_time_ms": (
                endpoint.next_funding_time_ms if endpoint is not None else None
            ),
            "event_time_ms": endpoint.event_time_ms if endpoint is not None else None,
            "subscription_epoch": wanted_epoch,
            "exchange": endpoint.exchange if endpoint is not None else "binance",
            "market": endpoint.market if endpoint is not None else "futures",
            "source": endpoint.source if endpoint is not None else "binance_ws_mark_price",
        }
        if quality == "complete" and start_item is not None and endpoint is not None:
            change = endpoint.funding_rate - start_item[0].funding_rate
            output.update({
                "funding_rate_start_5m": start_item[0].funding_rate,
                "funding_rate_change_5m": change,
                "funding_rate_changed_5m": change != 0,
            })
        return output

    def stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "symbol_count": len(self._latest),
                "history_entry_count": sum(len(rows) for rows in self._history.values()),
                "accepted_updates": self._accepted_updates,
                "duplicate_updates": self._duplicate_updates,
                "out_of_order_updates": self._out_of_order_updates,
                "invalid_updates": self._invalid_updates,
            }

    @staticmethod
    def _valid(value: MarkPriceUpdate | None) -> bool:
        return bool(
            isinstance(value, MarkPriceUpdate)
            and _subscription_symbol(value.symbol) == value.symbol
            and value.mark_price > 0
            and math.isfinite(value.mark_price)
            and math.isfinite(value.funding_rate)
            and value.event_time_ms > 0
            and value.next_funding_time_ms > 0
            and value.exchange == "binance"
            and value.market == "futures"
        )


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
    from .binance_data import BinanceDataSource

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


class BinanceRealtimeMarketService:
    service_name = "binance_realtime_market"
    thread_name = "binance-market-websocket"

    def __init__(
        self,
        settings: Any,
        *,
        store: RealtimeFeatureStore | None = None,
        websocket_app_factory: Any = None,
        realtime_controller: Any = None,
    ):
        self.settings = settings
        p2_gate_enabled = bool(
            getattr(settings, "altcoin_contract_anomaly_enable", False)
            and getattr(settings, "altcoin_contract_anomaly_realtime_enable", False)
        )
        if realtime_controller is not None and not p2_gate_enabled:
            raise ValueError("explicit P2 controller requires both feature gates")
        # P2 is deliberately an explicit bounded-session dependency.  The
        # long-running market-stream service must stay on its legacy path even
        # when both environment switches happen to be enabled.
        self._p2_enabled = realtime_controller is not None
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
        self._p2_controller: Any = realtime_controller if self._p2_enabled else None
        self._p2_ledger: SubscriptionLedger | None = None
        self._p2_mark_prices: MarkPriceBook | None = None
        self._p2_plan: BinanceSubscriptionPlan | None = None
        self._p2_base_symbols: tuple[str, ...] = ()
        self._p2_manifest_degraded = False
        self._p2_last_applied_manifest_hash = ""
        self._p2_next_manifest_poll_mono = 0.0
        self._p2_next_evaluate_mono = 0.0
        self._p2_last_market_receive_ms = 0
        self._p2_last_market_receive_mono = 0.0
        self._p2_agg_trade_messages = 0
        self._p2_force_order_messages = 0
        self._p2_mark_price_messages = 0
        self._p2_mark_price_rejected = 0
        self._p2_mark_price_symbols: set[str] = set()
        self._p2_candidate_epochs: dict[str, dict[str, Any]] = {}
        self._p2_epoch_counter = 0
        self._p2_session_token = f"{time.time_ns():x}"
        self._p2_metrics_lock = threading.RLock()
        self._p2_closed_buckets = 0
        self._p2_evaluation_errors = 0
        self._p2_runner_shutdown_timeouts = 0
        if self._p2_enabled:
            self._p2_ledger = SubscriptionLedger(
                batch_size=int(getattr(
                    settings,
                    "altcoin_contract_anomaly_subscription_batch_size",
                    50,
                ) or 50),
                min_interval_sec=float(getattr(
                    settings,
                    "altcoin_contract_anomaly_subscription_min_interval_sec",
                    1.0,
                ) or 0.0),
                ack_timeout_sec=float(getattr(
                    settings,
                    "altcoin_contract_anomaly_subscription_ack_timeout_sec",
                    10,
                ) or 10),
            )
            candidate_book = getattr(self._p2_controller, "mark_price_book", None)
            self._p2_mark_prices = (
                candidate_book if isinstance(candidate_book, MarkPriceBook) else MarkPriceBook()
            )

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

    def _apply_p2_subscription_plan(self, base_symbols: Iterable[Any]) -> BinanceSubscriptionPlan | None:
        if not self._p2_enabled or self._p2_ledger is None or self._p2_controller is None:
            return None
        self._p2_base_symbols = tuple(
            symbol for value in base_symbols if (symbol := _subscription_symbol(value))
        )
        previous_candidates = set(self._p2_plan.candidate_symbols) if self._p2_plan else set()
        plan = build_binance_subscription_plan(
            self._p2_base_symbols,
            tuple(getattr(self._p2_controller, "candidate_symbols", ()) or ()),
            max_streams=int(getattr(
                self.settings,
                "altcoin_contract_anomaly_max_streams",
                300,
            ) or 300),
        )
        self._p2_plan = plan
        self._p2_ledger.set_desired(plan.subscriptions)
        current_candidates = set(plan.candidate_symbols)
        removed = previous_candidates - current_candidates
        with self._p2_metrics_lock:
            for symbol in removed:
                self._p2_candidate_epochs.pop(symbol, None)
        reintroduced = current_candidates - previous_candidates
        if reintroduced:
            self._p2_ledger.invalidate_active(
                stream
                for symbol in reintroduced
                for stream in (
                    f"{symbol.lower()}@aggTrade",
                    f"{symbol.lower()}@markPrice",
                )
            )
        self._refresh_candidate_epochs(now_ms=int(time.time() * 1000))
        return plan

    def _refresh_candidate_epochs(self, *, now_ms: int) -> None:
        if not self._p2_enabled or self._p2_ledger is None:
            return
        requested = set(self._p2_plan.candidate_symbols) if self._p2_plan else set()
        active = set(self._p2_ledger.active_subscriptions)
        force_active = "!forceOrder@arr" in active
        with self._p2_metrics_lock:
            for symbol in tuple(self._p2_candidate_epochs):
                streams_ready = (
                    f"{symbol.lower()}@aggTrade" in active
                    and f"{symbol.lower()}@markPrice" in active
                    and force_active
                )
                if symbol not in requested or not streams_ready:
                    self._p2_candidate_epochs.pop(symbol, None)
            if not force_active:
                return
            clock_skew_ms = 5_000
            for symbol in sorted(requested):
                if symbol in self._p2_candidate_epochs:
                    continue
                if not (
                    f"{symbol.lower()}@aggTrade" in active
                    and f"{symbol.lower()}@markPrice" in active
                ):
                    continue
                self._p2_epoch_counter += 1
                activated = max(1, int(now_ms))
                safe_after = activated + clock_skew_ms
                eligible_1m = ((safe_after + 59_999) // 60_000) * 60_000
                eligible_5m = ((safe_after + 299_999) // 300_000) * 300_000
                self._p2_candidate_epochs[symbol] = {
                    "epoch_id": (
                        f"{self._p2_session_token}:"
                        f"{self._p2_ledger.generation}:{self._p2_epoch_counter}"
                    ),
                    "activated_at_ms": activated,
                    "eligible_1m_bucket_start_ms": eligible_1m,
                    "eligible_5m_boundary_ms": eligible_5m,
                    "subscription_generation": self._p2_ledger.generation,
                    "last_agg_trade_event_ms": 0,
                    "last_mark_price_event_ms": 0,
                }

    def _poll_p2_manifest(
        self,
        *,
        now_ts: float,
        now_monotonic: float,
        force: bool = False,
    ) -> dict[str, Any]:
        if not self._p2_enabled or self._p2_controller is None:
            return {"status": "disabled", "changed": False}
        if not force and now_monotonic < self._p2_next_manifest_poll_mono:
            return {"status": "not_due", "changed": False}
        poll_sec = max(1, int(getattr(
            self.settings,
            "altcoin_contract_anomaly_manifest_poll_sec",
            5,
        ) or 5))
        self._p2_next_manifest_poll_mono = now_monotonic + poll_sec
        try:
            result = self._p2_controller.poll_manifest(now_ts=now_ts)
            result = dict(result) if isinstance(result, Mapping) else {
                "status": "manifest_degraded",
                "reason": "manifest poll returned an invalid result",
                "changed": False,
            }
        except Exception as exc:
            self._p2_manifest_degraded = True
            self.last_error = f"manifest_poll:{type(exc).__name__}"
            return {
                "status": "manifest_degraded",
                "reason": self.last_error,
                "changed": False,
            }
        status = str(result.get("status") or "manifest_degraded")
        self._p2_manifest_degraded = status not in {"valid_changed", "valid_unchanged"}
        if not self._p2_manifest_degraded:
            try:
                controller_stats = self._p2_controller.stats()
            except Exception:
                controller_stats = {}
            if isinstance(controller_stats, Mapping):
                self._p2_last_applied_manifest_hash = str(
                    controller_stats.get("manifest_hash") or ""
                )
        # Invalid manifests retain the controller's last valid candidates. Rebuilding
        # the pure plan is safe because unchanged desired streams create no command.
        self._apply_p2_subscription_plan(self._p2_base_symbols)
        return result

    def _send_next_p2_control(
        self,
        ws: Any,
        *,
        now_monotonic: float | None = None,
    ) -> SubscriptionCommand | None:
        if not self._p2_enabled or self._p2_ledger is None or not self._connected.is_set():
            return None
        now = time.monotonic() if now_monotonic is None else float(now_monotonic)
        expired = self._p2_ledger.expire_timeouts(now_monotonic=now)
        if expired:
            self.last_error = f"subscription_ack_timeout:count={len(expired)}"[:300]
        command = self._p2_ledger.next_command(now_monotonic=now)
        if command is None:
            return None
        try:
            ws.send(json.dumps(command.payload(), separators=(",", ":")))
        except Exception as exc:
            self._p2_ledger.mark_send_failed(command.request_id, exc)
            self.last_error = f"subscription_send:{type(exc).__name__}"
            return None
        return command

    def _record_p2_market_data(
        self,
        event_time_ms: int,
        *,
        symbol: str = "",
        event_kind: str = "",
    ) -> None:
        if not self._p2_enabled or event_time_ms <= self._p2_last_market_receive_ms:
            pass
        else:
            self._p2_last_market_receive_ms = int(event_time_ms)
            self._p2_last_market_receive_mono = time.monotonic()
        normalized = _subscription_symbol(symbol)
        if not normalized:
            return
        with self._p2_metrics_lock:
            epoch = self._p2_candidate_epochs.get(normalized)
            if not epoch or int(event_time_ms) < int(epoch["activated_at_ms"]):
                return
            if event_kind == "trade":
                epoch["last_agg_trade_event_ms"] = max(
                    int(epoch.get("last_agg_trade_event_ms") or 0),
                    int(event_time_ms),
                )
            elif event_kind == "mark_price":
                epoch["last_mark_price_event_ms"] = max(
                    int(epoch.get("last_mark_price_event_ms") or 0),
                    int(event_time_ms),
                )

    def _p2_subscription_status(self) -> dict[str, Any]:
        if not self._p2_enabled or self._p2_ledger is None:
            return {}
        active = set(self._p2_ledger.active_subscriptions)
        requested_candidates = tuple(sorted({
            symbol
            for value in (getattr(self._p2_controller, "candidate_symbols", ()) or ())
            if (symbol := _subscription_symbol(value))
        }))
        active_candidates = []
        for value in requested_candidates:
            symbol = value
            if (
                f"{symbol.lower()}@aggTrade" in active
                and f"{symbol.lower()}@markPrice" in active
            ):
                active_candidates.append(symbol)
        force_active = "!forceOrder@arr" in active
        candidate_capacity_degraded = bool(
            self._p2_plan and self._p2_plan.candidate_capacity_degraded
        )
        capacity_degraded = bool(
            self._p2_plan and self._p2_plan.capacity_degraded
        )
        with self._p2_metrics_lock:
            candidate_epochs = {
                symbol: dict(value)
                for symbol, value in self._p2_candidate_epochs.items()
                if symbol in requested_candidates
            }
        candidate_coverage_complete = (
            self._connected.is_set()
            and not candidate_capacity_degraded
            and force_active
            and set(requested_candidates).issubset(active_candidates)
            and set(requested_candidates).issubset(candidate_epochs)
        )
        manifest_ready = bool(
            getattr(self._p2_controller, "manifest_event_ready", False)
        )
        return {
            "connected": self._connected.is_set(),
            "connection_state": "connected" if self._connected.is_set() else "disconnected",
            # Deliberately excludes ACK/PONG arrival time.
            "last_receive_ms": self._p2_last_market_receive_ms,
            "last_market_receive_ms": self._p2_last_market_receive_ms,
            "active_subscriptions": sorted(active),
            "active_candidate_symbols": sorted(active_candidates),
            "candidate_epochs": candidate_epochs,
            "candidate_coverage_complete": candidate_coverage_complete,
            "force_order_active": force_active,
            "capacity_degraded": capacity_degraded,
            "candidate_capacity_degraded": candidate_capacity_degraded,
            "manifest_degraded": self._p2_manifest_degraded or not manifest_ready,
            "subscription_generation": self._p2_ledger.generation,
        }

    def _evaluate_p2(self, *, now_ts: float, now_monotonic: float) -> list[dict[str, Any]]:
        if not self._p2_enabled or self._p2_controller is None:
            return []
        if now_monotonic < self._p2_next_evaluate_mono:
            return []
        interval = max(1, int(getattr(self.settings, "realtime_market_bucket_sec", 60) or 60))
        self._p2_next_evaluate_mono = now_monotonic + interval
        try:
            events = self._p2_controller.evaluate(
                self._p2_subscription_status(),
                now_ts=now_ts,
            )
            return [dict(event) for event in events if isinstance(event, Mapping)]
        except Exception as exc:
            self._p2_evaluation_errors += 1
            self.last_error = f"altcoin_evaluate:{type(exc).__name__}"
            return []

    def _subscription_payload(self, subscriptions: list[Any]) -> dict[str, Any]:
        payload = {"method": "SUBSCRIBE", "params": subscriptions, "id": self._subscription_id}
        self._subscription_id += 1
        return payload

    def _handle_control(self, payload: dict[str, Any]) -> bool:
        if self._p2_enabled and self._p2_ledger is not None:
            if "id" not in payload or ("result" not in payload and "code" not in payload):
                return False
            self.control_messages += 1
            ack = self._p2_ledger.handle_ack(payload)
            if ack.status == "success":
                self.subscription_acks += 1
                self._refresh_candidate_epochs(now_ms=int(time.time() * 1000))
            elif ack.status == "stale":
                self.last_error = "subscription_ack:stale_generation"
                self._refresh_candidate_epochs(now_ms=int(time.time() * 1000))
            elif ack.status == "failure":
                self.last_error = "subscription_ack:failure"
            return True
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
        if self._p2_enabled and self._p2_ledger is not None:
            self._p2_last_market_receive_ms = 0
            self._p2_last_market_receive_mono = time.monotonic()
            with self._p2_metrics_lock:
                self._p2_candidate_epochs.clear()
            self._p2_ledger.reset_connection()
            self._send_next_p2_control(ws)
            return
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
        if self._p2_enabled and self._p2_controller is not None and self._p2_mark_prices is not None:
            mark_price = parse_binance_mark_price_update(control)
            if mark_price is not None:
                self._p2_mark_price_messages += 1
                with self._p2_metrics_lock:
                    epoch = dict(self._p2_candidate_epochs.get(mark_price.symbol) or {})
                if (
                    not epoch
                    or mark_price.event_time_ms
                    <= int(epoch.get("last_mark_price_event_ms") or 0)
                ):
                    self._p2_mark_price_rejected += 1
                    return
                try:
                    try:
                        accepted = bool(self._p2_controller.handle_mark_price(
                            mark_price,
                            subscription_epoch=str(epoch.get("epoch_id") or ""),
                        ))
                    except TypeError:
                        accepted = bool(self._p2_controller.handle_mark_price(mark_price))
                except Exception as exc:
                    accepted = False
                    self.last_error = f"mark_price:{type(exc).__name__}"
                if accepted:
                    self._record_p2_market_data(
                        mark_price.event_time_ms,
                        symbol=mark_price.symbol,
                        event_kind="mark_price",
                    )
                    with self._p2_metrics_lock:
                        self._p2_mark_price_symbols.add(mark_price.symbol)
                else:
                    self._p2_mark_price_rejected += 1
                return
        for event in self._events_from_payload(control):
            if self._p2_enabled:
                self._record_p2_market_data(
                    event.event_time_ms,
                    symbol=event.symbol,
                    event_kind=event.event_type,
                )
                if event.event_type == "trade":
                    self._p2_agg_trade_messages += 1
                elif event.event_type == "liquidation":
                    self._p2_force_order_messages += 1
            self.pipeline.handle_event(event)

    def _on_error(self, _ws: Any, error: Any) -> None:
        self.connection_errors += 1
        self.last_error = (
            f"websocket:{type(error).__name__}"
            if self._p2_enabled
            else f"{type(error).__name__}: {error}"[:300]
        )

    def _on_close(self, _ws: Any, status_code: Any, message: Any) -> None:
        self._connected.clear()
        if status_code not in (None, 1000):
            self.last_error = (
                f"closed:{status_code}"
                if self._p2_enabled
                else f"closed:{status_code}:{message}"[:300]
            )

    def stats(self) -> dict[str, Any]:
        output = {
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
        if not self._p2_enabled or self._p2_ledger is None:
            return output
        try:
            controller_stats = self._p2_controller.stats() if self._p2_controller is not None else {}
        except Exception as exc:
            controller_stats = {}
            self.last_error = f"altcoin_stats:{type(exc).__name__}"
        if not isinstance(controller_stats, Mapping):
            controller_stats = {}
        plan_stats = self._p2_plan.stats() if self._p2_plan is not None else {}
        ledger_stats = self._p2_ledger.stats()
        subscription_status = self._p2_subscription_status()
        active_candidates = list(subscription_status.get("active_candidate_symbols") or [])
        requested_candidates = tuple(sorted({
            symbol
            for value in (getattr(self._p2_controller, "candidate_symbols", ()) or ())
            if (symbol := _subscription_symbol(value))
        }))
        oi_stats = controller_stats.get("oi")
        oi_stats = dict(oi_stats) if isinstance(oi_stats, Mapping) else {}
        feature_stats = controller_stats.get("features")
        feature_stats = dict(feature_stats) if isinstance(feature_stats, Mapping) else {}
        skip_reasons = controller_stats.get("data_quality_skip_reasons")
        skip_reasons = dict(skip_reasons) if isinstance(skip_reasons, Mapping) else {}
        manifest_age = _number(controller_stats.get("manifest_age_sec"))
        if manifest_age is not None and manifest_age < 0:
            manifest_age = None
        with self._p2_metrics_lock:
            mark_price_data_symbols = sorted(
                symbol
                for symbol in requested_candidates
                if (
                    symbol in self._p2_candidate_epochs
                    and int(
                        self._p2_candidate_epochs[symbol].get(
                            "last_mark_price_event_ms"
                        )
                        or 0
                    )
                    >= int(
                        self._p2_candidate_epochs[symbol].get("activated_at_ms")
                        or 0
                    )
                    > 0
                )
            )
        output.update({
            "last_error": self.last_error,
            "altcoin_contract_anomaly_realtime_enabled": True,
            "connection_state": subscription_status.get("connection_state"),
            "last_market_receive_ms": self._p2_last_market_receive_ms,
            "manifest_hash": str(controller_stats.get("manifest_hash") or ""),
            "manifest_snapshot_hash": str(controller_stats.get("manifest_snapshot_hash") or ""),
            "manifest_age_sec": manifest_age,
            "last_applied_manifest_hash": self._p2_last_applied_manifest_hash,
            "manifest_event_ready": bool(controller_stats.get("manifest_event_ready")),
            "manifest_degraded": bool(subscription_status.get("manifest_degraded")),
            "manifest_polls": int(controller_stats.get("manifest_polls") or 0),
            "manifest_failures": int(controller_stats.get("manifest_failures") or 0),
            "base_symbol_count": len(self._p2_base_symbols),
            "candidate_count": len(requested_candidates),
            "union_symbol_count": int(plan_stats.get("union_symbol_count") or 0),
            "expected_stream_count": int(plan_stats.get("expected_stream_count") or 0),
            "desired_stream_count": int(ledger_stats["desired_subscription_count"]),
            "active_stream_count": int(ledger_stats["active_subscription_count"]),
            "pending_subscribe_count": int(ledger_stats["pending_subscribe_count"]),
            "pending_unsubscribe_count": int(ledger_stats["pending_unsubscribe_count"]),
            "subscribe_success": int(ledger_stats["subscribe_success"]),
            "subscribe_failure": int(ledger_stats["subscribe_failure"]),
            "unsubscribe_success": int(ledger_stats["unsubscribe_success"]),
            "unsubscribe_failure": int(ledger_stats["unsubscribe_failure"]),
            "ack_timeouts": int(ledger_stats["ack_timeouts"]),
            "subscription_generation": int(ledger_stats["subscription_generation"]),
            "candidate_epochs": subscription_status.get("candidate_epochs", {}),
            "capacity_degraded": bool(subscription_status.get("capacity_degraded")),
            "candidate_capacity_degraded": bool(
                subscription_status.get("candidate_capacity_degraded")
            ),
            "base_capacity_trimmed": bool(
                self._p2_plan
                and self._p2_plan.capacity_degraded
                and not self._p2_plan.candidate_capacity_degraded
            ),
            "candidate_coverage_complete": bool(
                subscription_status.get("candidate_coverage_complete")
            ),
            "active_candidate_count": len(active_candidates),
            "candidate_epoch_count": len(
                subscription_status.get("candidate_epochs") or {}
            ),
            "candidate_epoch_coverage_ratio": (
                len(subscription_status.get("candidate_epochs") or {})
                / len(requested_candidates)
                if requested_candidates else 1.0
            ),
            "mark_price_coverage_ratio": (
                len(active_candidates) / len(requested_candidates)
                if requested_candidates else 1.0
            ),
            "mark_price_data_symbol_count": len(mark_price_data_symbols),
            "mark_price_data_coverage_ratio": (
                len(mark_price_data_symbols) / len(requested_candidates)
                if requested_candidates else 1.0
            ),
            "force_order_subscription_count": sum(
                stream == "!forceOrder@arr"
                for stream in self._p2_ledger.desired_subscriptions
            ),
            "force_order_active": bool(subscription_status.get("force_order_active")),
            "active_subscriptions": subscription_status.get("active_subscriptions", []),
            "mark_price_messages": self._p2_mark_price_messages,
            "mark_price_rejected": self._p2_mark_price_rejected,
            "agg_trade_messages": self._p2_agg_trade_messages,
            "force_order_messages": self._p2_force_order_messages,
            "closed_bucket_count": self._p2_closed_buckets,
            "feature_evaluations": int(controller_stats.get("feature_evaluations") or 0),
            "last_evaluation_candidate_count": int(
                controller_stats.get("last_evaluation_candidate_count") or 0
            ),
            "last_evaluation_complete_count": int(
                controller_stats.get("last_evaluation_complete_count") or 0
            ),
            "last_evaluation_epoch_complete_count": int(
                controller_stats.get("last_evaluation_epoch_complete_count") or 0
            ),
            "last_evaluation_funding_complete_count": int(
                controller_stats.get("last_evaluation_funding_complete_count") or 0
            ),
            "aligned_evaluation_rounds": int(
                controller_stats.get("aligned_evaluation_rounds") or 0
            ),
            "non_aligned_evaluation_skips": int(
                controller_stats.get("non_aligned_evaluation_skips") or 0
            ),
            "last_aligned_evaluation_at": str(
                controller_stats.get("last_aligned_evaluation_at") or ""
            ),
            "last_evaluation_complete_ratio": _number(
                controller_stats.get("last_evaluation_complete_ratio")
            ),
            "feature_coverage": feature_stats,
            "data_quality_skips": int(controller_stats.get("data_quality_skips") or 0),
            "data_quality_skip_reasons": skip_reasons,
            "event_counts": dict(controller_stats.get("events") or {}),
            "last_event_at": str(controller_stats.get("last_event_at") or ""),
            "evaluation_errors": self._p2_evaluation_errors,
            "runner_shutdown_timeouts": self._p2_runner_shutdown_timeouts,
            "oi_candidate_count": int(oi_stats.get("candidate_count") or 0),
            "oi_requests": int(oi_stats.get("requests") or 0),
            "oi_cache_hits": int(oi_stats.get("cache_hits") or 0),
            "oi_successes": int(oi_stats.get("successes") or 0),
            "oi_failures": int(oi_stats.get("failures") or 0),
            "oi_budget_used": int(oi_stats.get("budget_used") or 0),
            "oi_budget_limit": int(oi_stats.get("budget_limit") or 0),
            "oi_budget_exhausted": int(oi_stats.get("budget_exhausted") or 0),
            "oi_rate_limit_blocked": int(
                oi_stats.get("rate_limit_blocked") or 0
            ),
            "oi_http_429": int(oi_stats.get("http_429") or 0),
            "oi_http_418": int(oi_stats.get("http_418") or 0),
            "oi_refresh_rounds": int(oi_stats.get("refresh_rounds") or 0),
            "oi_last_round": dict(oi_stats.get("last_round") or {}),
        })
        return output

    def recent_events(self) -> list[dict[str, Any]]:
        if not self._p2_enabled or self._p2_controller is None:
            return []
        try:
            events = getattr(self._p2_controller, "recent_events", [])
            return [dict(event) for event in events if isinstance(event, Mapping)]
        except Exception:
            return []

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
                self.last_error = (
                    f"symbol_load:{type(exc).__name__}"
                    if self._p2_enabled
                    else f"symbol_load:{type(exc).__name__}: {exc}"[:300]
                )
                stop.wait(max(30, reconnect_delay))
                continue
            if not symbols:
                self.connection_errors += 1
                self.last_error = "symbol_load:empty"
                stop.wait(max(30, reconnect_delay))
                continue
            if self._p2_enabled:
                self._p2_base_symbols = tuple(symbols)
                self._apply_p2_subscription_plan(symbols)
                self._poll_p2_manifest(
                    now_ts=time.time(),
                    now_monotonic=time.monotonic(),
                    force=True,
                )
            self.symbol_count = len(symbols)
            self._connection_context = context
            self.connection_attempts += 1
            open_count_before = self.open_count
            errors_before = self.connection_errors
            self._connected.clear()
            self._last_receive_mono = 0.0
            if self._p2_enabled:
                self._p2_last_market_receive_ms = 0
                self._p2_last_market_receive_mono = 0.0
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
                written = self.pipeline.flush()
                if self._p2_enabled:
                    self._p2_closed_buckets += written
                now = time.time()
                now_mono = time.monotonic()
                if self._p2_enabled:
                    self._poll_p2_manifest(
                        now_ts=now,
                        now_monotonic=now_mono,
                    )
                    self._send_next_p2_control(ws, now_monotonic=now_mono)
                    self._evaluate_p2(now_ts=now, now_monotonic=now_mono)
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
                idle_reference = (
                    self._p2_last_market_receive_mono
                    if self._p2_enabled
                    else self._last_receive_mono
                )
                if self._connected.is_set() and idle_reference and now_mono - idle_reference >= idle_timeout:
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
            if self._p2_enabled and runner.is_alive():
                self._p2_runner_shutdown_timeouts += 1
                self.connection_errors += 1
                self.last_error = "websocket_runner_shutdown_timeout"
                raise RuntimeError("websocket_runner_shutdown_timeout")
            written = self.pipeline.flush()
            if self._p2_enabled:
                self._p2_closed_buckets += written
            if not stop.is_set():
                stop.wait(reconnect_delay)


def run_realtime_market_session(
    settings: Any,
    *,
    duration_sec: float,
    service: BinanceRealtimeMarketService | None = None,
) -> dict[str, Any]:
    """Run one bounded, no-output realtime session and return structured results."""

    duration = float(duration_sec)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("duration_sec must be a finite positive number")
    market_service = service or BinanceRealtimeMarketService(settings)
    stop = threading.Event()
    failures: list[str] = []
    interrupted = False
    live_stats: dict[str, Any] | None = None
    started_at = time.time()
    started_mono = time.monotonic()

    def capture_live_stats() -> dict[str, Any] | None:
        try:
            value = market_service.stats()
        except Exception:
            return None
        return dict(value) if isinstance(value, Mapping) else None

    def run_service() -> None:
        try:
            market_service.run(stop)
            if not stop.is_set():
                failures.append("binance_realtime_market:unexpected_exit")
                stop.set()
        except Exception as exc:
            failures.append(f"binance_realtime_market:{type(exc).__name__}")
            stop.set()

    worker = threading.Thread(
        target=run_service,
        name="altcoin-anomaly-realtime-session",
        daemon=True,
    )
    worker_started = False
    try:
        worker.start()
        worker_started = True
        deadline = started_mono + duration
        while worker.is_alive() and not stop.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                live_stats = capture_live_stats()
                stop.set()
                break
            stop.wait(min(0.25, remaining))
    except KeyboardInterrupt:
        interrupted = True
        live_stats = capture_live_stats()
        stop.set()
    except Exception as exc:
        failures.append(f"binance_realtime_market:session:{type(exc).__name__}")
        stop.set()
    finally:
        if live_stats is None and worker_started and worker.is_alive():
            live_stats = capture_live_stats()
        stop.set()
        if worker_started:
            worker.join(timeout=15)
        if worker_started and worker.is_alive():
            failures.append("binance_realtime_market:shutdown_timeout")
    ended_at = time.time()
    try:
        shutdown_stats = market_service.stats()
    except Exception as exc:
        shutdown_stats = {}
        failures.append(f"binance_realtime_market:stats:{type(exc).__name__}")
    # Post-shutdown counters are authoritative because the service may finish
    # one final flush/evaluation after the deadline snapshot. Only connection
    # health is taken from immediately before the intentional close.
    stats = dict(shutdown_stats)
    if live_stats is not None:
        health_keys = (
            "connection_state",
            "candidate_coverage_complete",
            "active_candidate_count",
            "candidate_epoch_count",
            "candidate_epoch_coverage_ratio",
            "mark_price_coverage_ratio",
            "mark_price_data_symbol_count",
            "mark_price_data_coverage_ratio",
            "force_order_active",
            "active_stream_count",
            "last_market_receive_ms",
        )
        for key in health_keys:
            if key in live_stats:
                stats[key] = live_stats[key]
        stats["connection_state_at_stop"] = live_stats.get("connection_state")
        stats["candidate_coverage_complete_at_stop"] = bool(
            live_stats.get("candidate_coverage_complete")
        )
    try:
        events = market_service.recent_events()
    except Exception as exc:
        events = []
        failures.append(f"binance_realtime_market:events:{type(exc).__name__}")
    return {
        "schema_version": 1,
        "dry_run": True,
        "started_at": datetime.fromtimestamp(started_at, timezone.utc).isoformat().replace("+00:00", "Z"),
        "ended_at": datetime.fromtimestamp(ended_at, timezone.utc).isoformat().replace("+00:00", "Z"),
        "duration_sec_requested": duration,
        "duration_sec_actual": round(max(0.0, time.monotonic() - started_mono), 3),
        "interrupted": interrupted,
        "ok": not failures,
        "failures": failures,
        "stats": stats,
        "events": events,
    }


def run_realtime_market_service(settings: Any, *, duration_sec: float = 0) -> int:
    services = [BinanceRealtimeMarketService(settings)]
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
            "service": "binance_realtime_market",
            "failures": failures,
            "exchanges": exchange_stats,
        }, ensure_ascii=False))
    return 1 if failures else 0


__all__ = [
    "BinanceSubscriptionPlan",
    "MarkPriceBook",
    "MarkPriceUpdate",
    "MarketEvent",
    "REALTIME_FEATURE_SCHEMA_VERSION",
    "RealtimeFeatureAggregator",
    "RealtimeFeatureStore",
    "RealtimeMarketPipeline",
    "BinanceRealtimeMarketService",
    "SubscriptionAck",
    "SubscriptionCommand",
    "SubscriptionLedger",
    "binance_stream_subscriptions",
    "build_binance_subscription_plan",
    "build_realtime_radar_boards",
    "parse_binance_mark_price_update",
    "parse_binance_market_event",
    "load_binance_realtime_symbols",
    "run_realtime_market_session",
    "run_realtime_market_service",
    "select_realtime_symbols",
]
