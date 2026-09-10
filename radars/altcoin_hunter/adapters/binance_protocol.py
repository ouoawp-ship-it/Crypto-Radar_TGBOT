"""Offline Binance USD-M public payload adapters; all times are injected."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, localcontext
from types import MappingProxyType
from typing import Any, Mapping

from ..models import (BookTickerEvent, BookTickerPayload, FundingEvent, FundingPayload,
    LiquidationEvent, LiquidationPayload, MarkPriceEvent, MarkPricePayload,
    OpenInterestEvent, OpenInterestPayload, TradeEvent, TradePayload, decimal_value,
    strict_int, timestamp_ms)
from .base import PROTOCOL_VERSION, ParseLimits, ParseResult, RejectedItem, Route, deterministic_digest
from .binance_usdm import BinanceInstrumentSpec, _decode_json, _decimal, _text, _validate_structure


KINDS = {"aggtrade": "agg_trade", "agg_trade": "agg_trade", "trade": "agg_trade",
         "markprice": "mark_price", "markpriceupdate": "mark_price", "mark_price": "mark_price",
         "bookticker": "book_ticker", "book_ticker": "book_ticker",
         "forceorder": "liquidation", "force_order": "liquidation", "liquidation": "liquidation",
         "openinterest": "open_interest", "open_interest": "open_interest"}
EXPECTED_EVENT = {"agg_trade": "aggTrade", "mark_price": "markPriceUpdate", "book_ticker": "bookTicker", "liquidation": "forceOrder"}
FIELDS = {"agg_trade": {"e", "E", "s", "a", "p", "q", "nq", "f", "l", "T", "m", "st"},
          "mark_price": {"e", "E", "s", "p", "i", "P", "r", "T", "ap", "st"},
          "book_ticker": {"e", "E", "s", "u", "T", "b", "B", "a", "A", "ps", "st"},
          "liquidation": {"e", "E", "o", "ps", "st"},
          "open_interest": {"symbol", "openInterest", "time"}}
ORDER_FIELDS = {"s", "S", "o", "f", "q", "p", "ap", "X", "l", "z", "T"}


class ProtocolError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class FundingMetadata:
    symbol: str
    interval_hours: int
    adjusted_cap: str
    adjusted_floor: str


@dataclass(frozen=True, slots=True)
class FundingInfoResult:
    entries: Mapping[str, FundingMetadata]
    rejected_items: tuple[RejectedItem, ...]
    diagnostics: Mapping[str, int]


def _signed_decimal(value: Any, name: str) -> str:
    if type(value) is not str:
        raise ValueError(f"invalid_{name}")
    decimal_value(value, name)
    return value


def _oi_base_quantity(quantity: str, unit: str, multiplier: str) -> tuple[str | None, str | None]:
    """Convert explicit contract units exactly; a quote quantity needs a price."""
    raw = decimal_value(quantity, "open_interest", nonnegative=True)
    scale = decimal_value(multiplier, "contract_multiplier", positive=True)
    if unit == "quote":
        return None, "quote_unit_requires_price"
    if unit == "base":
        return quantity, None
    if unit != "contracts":
        raise ProtocolError("invalid_open_interest_unit")
    with localcontext() as context:
        context.prec = max(28, len(raw.as_tuple().digits) + len(scale.as_tuple().digits))
        converted = raw * scale
    # Reuse the event contract's finite analytical range, including underflow.
    value = str(converted)
    decimal_value(value, "open_interest_base_quantity", nonnegative=True)
    return value, None


def _spec(symbol: Any, registry: Mapping[str, BinanceInstrumentSpec]) -> BinanceInstrumentSpec:
    symbol = _text(symbol, "symbol")
    spec = registry.get(symbol)
    if not isinstance(spec, BinanceInstrumentSpec) or spec.symbol != symbol:
        raise ProtocolError("unknown_symbol")
    if (not spec.eligible or spec.identity.exchange != "binance" or spec.identity.market != "usdt_perpetual"
            or spec.identity.quote_currency != "USDT"):
        raise ProtocolError("ineligible_symbol")
    return spec


def parse_funding_info(payload: Any, registry: Mapping[str, BinanceInstrumentSpec],
                       *, limits: ParseLimits | None = None) -> FundingInfoResult:
    if not isinstance(registry, Mapping):
        raise ValueError("invalid_registry")
    policy = limits or ParseLimits()
    entries, rejected, diagnostics = {}, [], Counter()
    try:
        items = _decode_json(payload, policy)
        if type(items) is not list or len(items) > policy.max_items:
            raise ProtocolError("invalid_funding_info")
    except ValueError:
        return FundingInfoResult(MappingProxyType({}), (RejectedItem(0, "invalid_funding_info"),), MappingProxyType({"rejected_count": 1}))
    total_bytes = 2
    for index, row in enumerate(items):
        try:
            total_bytes += _validate_structure(row, policy, depth=1) + 1
            if total_bytes > policy.max_payload_bytes:
                raise ProtocolError("payload_byte_limit")
            if type(row) is not dict:
                raise ProtocolError("item_not_object")
            spec = _spec(row.get("symbol"), registry)
            if spec.symbol in entries:
                raise ProtocolError("duplicate_funding_symbol")
            hours = strict_int(row.get("fundingIntervalHours"), "fundingIntervalHours", minimum=1, maximum=168)
            cap = _signed_decimal(row.get("adjustedFundingRateCap"), "funding_cap")
            floor = _signed_decimal(row.get("adjustedFundingRateFloor"), "funding_floor")
            if Decimal(floor) > Decimal(cap):
                raise ProtocolError("inverted_funding_limits")
            entries[spec.symbol] = FundingMetadata(spec.symbol, hours, cap, floor)
            diagnostics["unknown_field_count"] += len(set(row) - {"symbol", "fundingIntervalHours", "adjustedFundingRateCap", "adjustedFundingRateFloor", "disclaimer"})
        except (ValueError, TypeError):
            diagnostics["rejected_count"] += 1
            if len(rejected) < policy.max_rejected_items:
                rejected.append(RejectedItem(index, "invalid_funding_info_item"))
    # Partial metadata must not turn a missing symbol into an assumed 8h cycle.
    return FundingInfoResult(MappingProxyType(entries), tuple(rejected), MappingProxyType(dict(diagnostics)))


def parse_server_time(payload: Any, *, limits: ParseLimits | None = None) -> int:
    policy = limits or ParseLimits()
    decoded = _decode_json(payload, policy)
    _validate_structure(decoded, policy)
    if type(decoded) is not dict:
        raise ValueError("invalid_server_time")
    return timestamp_ms(decoded.get("serverTime"), "serverTime")


def _route(value: Route | str | None, kind: str) -> str | None:
    expected = Route.PUBLIC if kind == "book_ticker" else Route.MARKET
    if kind == "open_interest":
        if value is not None:
            raise ProtocolError("rest_payload_on_websocket_route")
        return None
    if value is None:
        return expected.value
    if isinstance(value, Route):
        actual = value
    elif type(value) is str and value.upper().strip("/") in {"PUBLIC", "MARKET"}:
        actual = Route(value.upper().strip("/"))
    else:
        raise ProtocolError("invalid_route")
    if actual != expected:
        raise ProtocolError("route_mismatch")
    return actual.value


def _stream(value: Any, kind: str, symbol: str) -> tuple[str | None, bool]:
    if value is None:
        return None, False
    stream = _text(value, "stream", 256).lower()
    single = symbol.lower()
    allowed = {"agg_trade": {f"{single}@aggtrade"},
               "mark_price": {f"{single}@markprice", f"{single}@markprice@1s", "!markprice@arr", "!markprice@arr@1s"},
               "book_ticker": {"!bookticker", f"{single}@bookticker"},
               "liquidation": {"!forceorder@arr", f"{single}@forceorder"}}
    if kind not in allowed or stream not in allowed[kind]:
        raise ProtocolError("stream_mismatch")
    return stream, kind == "book_ticker" and stream != "!bookticker"


def _one(row: Any, kind: str, registry: Mapping[str, BinanceInstrumentSpec], *, receive_time_ms: int,
         receive_monotonic_ns: int, connection_epoch: int, route: Route | str | None,
         stream: str | None, funding_info: Mapping[str, FundingMetadata] | FundingInfoResult | None):
    if type(row) is not dict:
        raise ProtocolError("item_not_object")
    actual_route = _route(route, kind)
    if kind in EXPECTED_EVENT:
        if "st" not in row:
            raise ProtocolError("missing_symbol_type")
        if type(row["st"]) is not int:
            raise ProtocolError("invalid_symbol_type")
        if row["st"] == 2:
            raise ProtocolError("cm_payload_rejected")
        if row["st"] != 1:
            raise ProtocolError("invalid_symbol_type")
        if row.get("e") != EXPECTED_EVENT[kind]:
            raise ProtocolError("event_type_mismatch")
    order = row.get("o") if kind == "liquidation" else None
    if kind == "liquidation" and type(order) is not dict:
        raise ProtocolError("invalid_liquidation_order")
    symbol = order.get("s") if order is not None else row.get("symbol") if kind == "open_interest" else row.get("s")
    spec = _spec(symbol, registry)
    canonical_stream, promoted = _stream(stream, kind, symbol)
    identity = spec.identity
    envelope = {"exchange": identity.exchange, "market": identity.market, "instrument_id": identity.instrument_id,
                "symbol": identity.symbol, "exchange_symbol": identity.exchange_symbol,
                "canonical_asset_id": identity.canonical_asset_id,
                "receive_time_ms": receive_time_ms, "receive_monotonic_ns": receive_monotonic_ns,
                "connection_epoch": connection_epoch}
    unknown = len(set(row) - FIELDS[kind])
    emitted = []
    metadata = {"protocol_version": PROTOCOL_VERSION, "kind": kind, "route": actual_route,
                "stream": stream, "canonical_stream": canonical_stream, "promoted": promoted,
                "unknown_field_count": unknown, "symbol": symbol}
    if kind in EXPECTED_EVENT:
        metadata["exchange_event_time_ms"] = timestamp_ms(row.get("E"), "E")
    if kind == "agg_trade":
        aggregate_id = strict_int(row.get("a"), "a")
        first, last = strict_int(row.get("f"), "f"), strict_int(row.get("l"), "l")
        if first > last:
            raise ProtocolError("inverted_trade_sequence")
        at = timestamp_ms(row.get("T"), "T")
        if type(row.get("m")) is not bool:
            raise ProtocolError("invalid_maker_flag")
        payload = TradePayload(_decimal(row.get("p"), "price", positive=True),
            _decimal(row.get("q"), "quantity", positive=True), row["m"], identity.quantity_unit,
            identity.contract_multiplier, identity.quote_currency)
        if "nq" in row:
            normal = _decimal(row["nq"], "normal_quantity")
            if Decimal(normal) > Decimal(payload.quantity):
                raise ProtocolError("normal_quantity_exceeds_total")
            metadata["normal_quantity_excluding_rpi"] = normal
        metadata["quantity_semantics"] = "all_market_trades_including_rpi"
        emitted.append(TradeEvent(**envelope, source="binance_usdm_agg_trade", source_event_id=str(aggregate_id),
            event_time_ms=at, sequence_start=first, sequence_end=last, payload=payload))
    elif kind == "mark_price":
        at = timestamp_ms(row.get("E"), "mark_time")
        price = _decimal(row.get("p"), "mark_price", positive=True)
        index = _decimal(row.get("i"), "index_price", positive=True)
        rate = _signed_decimal(row.get("r"), "funding_rate")
        next_at = timestamp_ms(row.get("T"), "next_funding_time")
        info = funding_info.entries if isinstance(funding_info, FundingInfoResult) else funding_info or {}
        funding = info.get(symbol)
        if funding is not None and (not isinstance(funding, FundingMetadata) or funding.symbol != symbol):
            raise ProtocolError("invalid_funding_metadata")
        interval = funding.interval_hours if funding is not None else None
        metadata["funding_interval_status"] = "configured" if interval is not None else "unknown"
        emitted.append(MarkPriceEvent(**envelope, source="binance_usdm_mark_price", event_time_ms=at,
            source_event_id=deterministic_digest((symbol, at, price, index)), payload=MarkPricePayload(price, index)))
        emitted.append(FundingEvent(**envelope, source="binance_usdm_funding", event_time_ms=at,
            source_event_id=deterministic_digest((symbol, at, rate, next_at)), payload=FundingPayload(rate, interval, next_at)))
    elif kind == "book_ticker":
        update = strict_int(row.get("u"), "u")
        at = timestamp_ms(row.get("T"), "T")
        payload = BookTickerPayload(_decimal(row.get("b"), "bid_price", positive=True),
            _decimal(row.get("a"), "ask_price", positive=True),
            _decimal(row.get("B"), "bid_quantity"), _decimal(row.get("A"), "ask_quantity"))
        metadata["book_semantics"] = "best_quotes_excluding_rpi"
        emitted.append(BookTickerEvent(**envelope, source="binance_usdm_book_ticker", event_time_ms=at,
            source_event_id=f"{symbol}:{update}", sequence_start=update, sequence_end=update, payload=payload))
    elif kind == "liquidation":
        metadata["unknown_field_count"] += len(set(order) - ORDER_FIELDS)
        at = timestamp_ms(order.get("T"), "order_T")
        side = order.get("S")
        if side not in {"BUY", "SELL"}:
            raise ProtocolError("invalid_liquidation_side")
        for field in ("o", "f", "X"):
            _text(order.get(field), f"order_{field}", 32)
        quantity = _decimal(order.get("q"), "original_quantity", positive=True)
        price = _decimal(order.get("p"), "order_price", positive=True)
        average = _decimal(order.get("ap"), "average_price")
        last_qty = _decimal(order.get("l"), "last_filled_quantity")
        cumulative = _decimal(order.get("z"), "cumulative_filled_quantity")
        if Decimal(last_qty) > Decimal(cumulative) or Decimal(cumulative) > Decimal(quantity):
            raise ProtocolError("invalid_liquidation_fills")
        metadata.update({"liquidation_semantics": "latest_order_snapshot_per_symbol_1000ms",
                         "quantity_semantics": "original_order_quantity", "snapshot_interval_ms": 1000,
                         "average_price": average, "last_filled_quantity": last_qty,
                         "cumulative_filled_quantity": cumulative, "order_status": order["X"]})
        canonical = {name: order[name] for name in sorted(ORDER_FIELDS)}
        emitted.append(LiquidationEvent(**envelope, source="binance_usdm_liquidation", event_time_ms=at,
            source_event_id=deterministic_digest(canonical), quality_flags=("liquidation_snapshot_not_exhaustive",),
            payload=LiquidationPayload(price, quantity, side.lower(), identity.quantity_unit, identity.contract_multiplier)))
    elif kind == "open_interest":
        at = timestamp_ms(row.get("time"), "time")
        interest = _decimal(row.get("openInterest"), "open_interest")
        base_quantity, missing_reason = _oi_base_quantity(interest, identity.quantity_unit, identity.contract_multiplier)
        metadata.update({"oi_semantics": "open_contract_quantity_not_directional_flow",
                         "raw_open_interest_quantity": interest, "raw_open_interest_unit": identity.quantity_unit,
                         "base_quantity": base_quantity, "base_quantity_missing_reason": missing_reason})
        emitted.append(OpenInterestEvent(**envelope, source="binance_usdm_open_interest", event_time_ms=at,
            source_event_id=f"{symbol}:{at}:{deterministic_digest((symbol, at, interest))}",
            payload=OpenInterestPayload(interest, identity.quantity_unit, None, identity.contract_multiplier)))
    return tuple(emitted), tuple({**metadata, "event_type": event.event_type,
                                  "event_dedup_key": event.dedup_key} for event in emitted)


def parse_binance_payload(payload: Any, kind: str, registry: Mapping[str, BinanceInstrumentSpec], *,
                          receive_time_ms: int, receive_monotonic_ns: int, connection_epoch: int = 0,
                          route: Route | str | None = None, stream: str | None = None,
                          funding_info: Mapping[str, FundingMetadata] | FundingInfoResult | None = None,
                          limits: ParseLimits | None = None) -> ParseResult:
    timestamp_ms(receive_time_ms, "receive_time_ms")
    strict_int(receive_monotonic_ns, "receive_monotonic_ns")
    strict_int(connection_epoch, "connection_epoch")
    if not isinstance(registry, Mapping):
        raise ValueError("invalid_registry")
    if stream is not None:
        _text(stream, "stream", 256)
    if funding_info is not None and not isinstance(funding_info, (Mapping, FundingInfoResult)):
        raise ValueError("invalid_funding_metadata")
    if type(kind) is not str or kind.lower() not in KINDS:
        return ParseResult(rejected_items=(RejectedItem(0, "unsupported_kind"),), diagnostics={"rejected_count": 1})
    kind = KINDS[kind.lower()]
    policy = limits or ParseLimits()
    events, rejected, metadata, diagnostics = [], [], [], Counter()
    envelope_bytes = 0
    item_depth = 0
    def reject(index: int, reason: str):
        diagnostics["rejected_count"] += 1
        diagnostics[reason] += 1
        if len(rejected) < policy.max_rejected_items:
            rejected.append(RejectedItem(index, reason))
    try:
        decoded = _decode_json(payload, policy)
        if type(decoded) is dict and "stream" in decoded:
            envelope_bytes = _validate_structure({key: value for key, value in decoded.items() if key != "data"}, policy) + 7
            item_depth = 1
            wrapper_stream = _text(decoded.get("stream"), "stream", 256)
            if stream is not None and wrapper_stream.lower() != stream.lower():
                raise ProtocolError("stream_wrapper_mismatch")
            stream = wrapper_stream
            if "data" not in decoded:
                raise ProtocolError("missing_combined_data")
            diagnostics["unknown_field_count"] += len(set(decoded) - {"stream", "data"})
            decoded = decoded["data"]
        if type(decoded) is list:
            item_depth += 1
            envelope_bytes += 2
        items = decoded if type(decoded) is list else [decoded]
        if len(items) > policy.max_items:
            raise ProtocolError("payload_item_limit")
    except (ValueError, TypeError, UnicodeError):
        reject(0, "malformed_envelope")
        return ParseResult(tuple(events), tuple(rejected), dict(diagnostics), tuple(metadata))
    total_bytes = envelope_bytes
    for index, item in enumerate(items):
        try:
            total_bytes += _validate_structure(item, policy, depth=item_depth) + 1
            if total_bytes > policy.max_payload_bytes:
                raise ProtocolError("payload_byte_limit")
            emitted, details = _one(item, kind, registry, receive_time_ms=receive_time_ms,
                receive_monotonic_ns=receive_monotonic_ns, connection_epoch=connection_epoch,
                route=route, stream=stream, funding_info=funding_info)
            events.extend(emitted)
            metadata.extend(details)
            diagnostics["accepted_items"] += 1
            diagnostics["unknown_field_count"] += details[0]["unknown_field_count"] if details else 0
        except ProtocolError as exc:
            reject(index, str(exc))
        except (ValueError, TypeError, KeyError, OverflowError, UnicodeError, RecursionError):
            reject(index, "malformed_item")
    diagnostics["event_count"] = len(events)
    return ParseResult(tuple(events), tuple(rejected), dict(diagnostics), tuple(metadata))


def parse_open_interest_response(payload: Any, registry: Mapping[str, BinanceInstrumentSpec], *,
                                 receive_time_ms: int, receive_monotonic_ns: int,
                                 connection_epoch: int = 0, limits: ParseLimits | None = None) -> ParseResult:
    return parse_binance_payload(payload, "open_interest", registry, receive_time_ms=receive_time_ms,
        receive_monotonic_ns=receive_monotonic_ns, connection_epoch=connection_epoch, limits=limits)
