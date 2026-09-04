"""Strict, source-preserving market event contracts; no I/O at import time."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from decimal import Decimal, InvalidOperation, localcontext
from math import isfinite
import re
from typing import Any, ClassVar, Mapping

MIN_TIMESTAMP_MS = 946684800000  # 2000-01-01 UTC
MAX_TIMESTAMP_MS = 4102444800000  # 2100-01-01 UTC, exclusive


def bounded_text(value: Any, name: str, *, optional: bool = False, limit: int = 128) -> str | None:
    if value is None and optional:
        return None
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{name} must be a nonempty, trimmed string")
    if len(value) > limit or any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{name} contains invalid text")
    return value


def strict_int(value: Any, name: str, *, minimum: int = 0, maximum: int = 2**63 - 1) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer in range")
    return value


def timestamp_ms(value: Any, name: str = "timestamp") -> int:
    return strict_int(value, name, minimum=MIN_TIMESTAMP_MS, maximum=MAX_TIMESTAMP_MS - 1)


def decimal_value(value: Any, name: str, *, positive: bool = False, nonnegative: bool = False) -> Decimal:
    if not isinstance(value, str) or not value or len(value) > 100 or not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", value):
        raise ValueError(f"{name} must be a finite decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be a finite decimal string") from exc
    if not parsed.is_finite() or (positive and parsed <= 0) or (nonnegative and parsed < 0):
        raise ValueError(f"{name} is outside its valid domain")
    # Downstream analytical float conversion must not silently become infinity.
    if not isfinite(float(parsed)) or (parsed != 0 and float(parsed) == 0):
        raise ValueError(f"{name} cannot be represented as a finite analytical value")
    return parsed


def _multiply_exact(*values: Decimal) -> Decimal:
    with localcontext() as context:
        context.prec = max(28, sum(len(value.as_tuple().digits) for value in values))
        result = Decimal(1)
        for value in values:
            result *= value
        return result


def _missing_core_metrics(core: Mapping[str, Any], reason: str | None) -> None:
    """Describe missing core data once per payload, never source quality.

    Optional supporting metadata does not participate in this contract. An
    available core value with degraded quality belongs in event.quality_flags.
    """
    if reason is not None:
        bounded_text(reason, "missing_reason", limit=256)
    missing = tuple(name for name, value in core.items() if value is None)
    if missing and reason is None:
        raise ValueError(f"{','.join(missing)}=null requires missing_reason")
    if not missing and reason is not None:
        raise ValueError("complete core metrics require missing_reason=null; use event quality_flags for degraded quality")


def _optional_metric(value: str | None, name: str, *, positive: bool = False, nonnegative: bool = False) -> None:
    if value is not None:
        decimal_value(value, name, positive=positive, nonnegative=nonnegative)


@dataclass(frozen=True)
class TradePayload:
    price: str
    quantity: str
    buyer_is_maker: bool
    quantity_unit: str = "base"
    contract_multiplier: str = "1"
    quote_currency: str = "USDT"

    def __post_init__(self) -> None:
        decimal_value(self.price, "price", positive=True)
        decimal_value(self.quantity, "quantity", positive=True)
        decimal_value(self.contract_multiplier, "contract_multiplier", positive=True)
        if type(self.buyer_is_maker) is not bool:
            raise ValueError("buyer_is_maker must be a boolean")
        if self.quantity_unit not in {"base", "contracts"}:
            raise ValueError("quantity_unit must be base or contracts")
        bounded_text(self.quote_currency, "quote_currency", limit=16)
        decimal_value(str(self.base_quantity), "base_quantity", positive=True)
        decimal_value(str(self.quote_notional), "quote_notional", positive=True)

    @property
    def base_quantity(self) -> Decimal:
        quantity = Decimal(self.quantity)
        return _multiply_exact(quantity, Decimal(self.contract_multiplier)) if self.quantity_unit == "contracts" else quantity

    @property
    def quote_notional(self) -> Decimal:
        return _multiply_exact(Decimal(self.price), self.base_quantity)


@dataclass(frozen=True)
class MarkPricePayload:
    mark_price: str | None
    index_price: str | None = None
    missing_reason: str | None = None

    def __post_init__(self) -> None:
        _missing_core_metrics({"mark_price": self.mark_price}, self.missing_reason)
        _optional_metric(self.mark_price, "mark_price", positive=True)
        if self.index_price is not None:
            decimal_value(self.index_price, "index_price", positive=True)


@dataclass(frozen=True)
class FundingPayload:
    funding_rate: str | None
    interval_hours: int | None = None
    next_funding_time_ms: int | None = None
    missing_reason: str | None = None

    def __post_init__(self) -> None:
        _missing_core_metrics({"funding_rate": self.funding_rate}, self.missing_reason)
        _optional_metric(self.funding_rate, "funding_rate")
        if self.interval_hours is not None:
            strict_int(self.interval_hours, "interval_hours", minimum=1, maximum=168)
        if self.next_funding_time_ms is not None:
            timestamp_ms(self.next_funding_time_ms, "next_funding_time_ms")


@dataclass(frozen=True)
class OpenInterestPayload:
    open_interest: str | None
    unit: str = "base"
    quote_notional: str | None = None
    contract_multiplier: str = "1"
    missing_reason: str | None = None

    def __post_init__(self) -> None:
        _missing_core_metrics({"open_interest": self.open_interest}, self.missing_reason)
        _optional_metric(self.open_interest, "open_interest", nonnegative=True)
        if self.unit not in {"base", "contracts", "quote"}:
            raise ValueError("OI unit must be base, contracts or quote")
        if self.quote_notional is not None:
            decimal_value(self.quote_notional, "quote_notional", nonnegative=True)
        decimal_value(self.contract_multiplier, "contract_multiplier", positive=True)


@dataclass(frozen=True)
class BookTickerPayload:
    """One-sided quotes are partial data and require a payload missing reason.

    Both prices present means no missing_reason; optional quantities may remain
    null. Source-quality concerns are carried by MarketEvent.quality_flags.
    """
    bid_price: str | None
    ask_price: str | None
    bid_quantity: str | None = None
    ask_quantity: str | None = None
    missing_reason: str | None = None

    def __post_init__(self) -> None:
        _missing_core_metrics({"bid_price": self.bid_price, "ask_price": self.ask_price}, self.missing_reason)
        _optional_metric(self.bid_price, "bid_price", positive=True)
        _optional_metric(self.ask_price, "ask_price", positive=True)
        if self.bid_price is not None and self.ask_price is not None and Decimal(self.bid_price) > Decimal(self.ask_price):
            raise ValueError("bid_price exceeds ask_price")
        for name in ("bid_quantity", "ask_quantity"):
            if getattr(self, name) is not None:
                decimal_value(getattr(self, name), name, nonnegative=True)


@dataclass(frozen=True)
class LiquidationPayload:
    price: str | None
    quantity: str | None
    side: str | None
    quantity_unit: str = "base"
    contract_multiplier: str = "1"
    missing_reason: str | None = None

    def __post_init__(self) -> None:
        _missing_core_metrics({"price": self.price, "quantity": self.quantity, "side": self.side}, self.missing_reason)
        _optional_metric(self.price, "price", positive=True)
        _optional_metric(self.quantity, "quantity", nonnegative=True)
        if self.side not in {"buy", "sell", None}:
            raise ValueError("liquidation side requires buy/sell or null")
        if self.quantity_unit not in {"base", "contracts"}:
            raise ValueError("quantity_unit must be base or contracts")
        decimal_value(self.contract_multiplier, "contract_multiplier", positive=True)


PAYLOAD_TYPES = {
    "trade": TradePayload, "mark_price": MarkPricePayload, "funding": FundingPayload,
    "open_interest": OpenInterestPayload, "book_ticker": BookTickerPayload, "liquidation": LiquidationPayload,
}


@dataclass(frozen=True, kw_only=True)
class MarketEvent:
    exchange: str
    market: str
    instrument_id: str
    symbol: str
    exchange_symbol: str
    event_type: str
    event_time_ms: int
    receive_time_ms: int
    receive_monotonic_ns: int
    source: str
    source_event_id: str
    payload: TradePayload | MarkPricePayload | FundingPayload | OpenInterestPayload | BookTickerPayload | LiquidationPayload
    schema_version: int = 1
    canonical_asset_id: str | None = None
    sequence_start: int | None = None
    sequence_end: int | None = None
    connection_epoch: int = 0
    quality_flags: tuple[str, ...] = ()
    EXPECTED_TYPE: ClassVar[str | None] = None

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("unsupported event schema_version")
        for name in ("exchange", "market", "instrument_id", "symbol", "exchange_symbol", "source", "source_event_id"):
            bounded_text(getattr(self, name), name)
        bounded_text(self.canonical_asset_id, "canonical_asset_id", optional=True)
        if not isinstance(self.event_type, str) or self.event_type not in PAYLOAD_TYPES or (self.EXPECTED_TYPE and self.event_type != self.EXPECTED_TYPE):
            raise ValueError("invalid event_type")
        if type(self.payload) is not PAYLOAD_TYPES[self.event_type]:
            raise ValueError("payload type does not match event_type")
        timestamp_ms(self.event_time_ms, "event_time_ms")
        timestamp_ms(self.receive_time_ms, "receive_time_ms")
        strict_int(self.receive_monotonic_ns, "receive_monotonic_ns")
        strict_int(self.connection_epoch, "connection_epoch")
        for name in ("sequence_start", "sequence_end"):
            value = getattr(self, name)
            if value is not None:
                strict_int(value, name)
        if (self.sequence_start is None) != (self.sequence_end is None):
            raise ValueError("sequence_start and sequence_end must both be present or null")
        if self.sequence_start is not None and self.sequence_start > self.sequence_end:
            raise ValueError("sequence_start exceeds sequence_end")
        if not isinstance(self.quality_flags, tuple) or len(self.quality_flags) > 32:
            raise ValueError("quality_flags must be a bounded tuple")
        for flag in self.quality_flags:
            bounded_text(flag, "quality_flag", limit=64)

    @property
    def dedup_key(self) -> tuple[str, str, str, str, str, str]:
        return (self.source, self.exchange, self.market, self.instrument_id, self.event_type, self.source_event_id)


@dataclass(frozen=True, kw_only=True)
class TradeEvent(MarketEvent):
    event_type: str = "trade"
    EXPECTED_TYPE: ClassVar[str] = "trade"


@dataclass(frozen=True, kw_only=True)
class MarkPriceEvent(MarketEvent):
    event_type: str = "mark_price"
    EXPECTED_TYPE: ClassVar[str] = "mark_price"


@dataclass(frozen=True, kw_only=True)
class FundingEvent(MarketEvent):
    event_type: str = "funding"
    EXPECTED_TYPE: ClassVar[str] = "funding"


@dataclass(frozen=True, kw_only=True)
class OpenInterestEvent(MarketEvent):
    event_type: str = "open_interest"
    EXPECTED_TYPE: ClassVar[str] = "open_interest"


@dataclass(frozen=True, kw_only=True)
class BookTickerEvent(MarketEvent):
    event_type: str = "book_ticker"
    EXPECTED_TYPE: ClassVar[str] = "book_ticker"


@dataclass(frozen=True, kw_only=True)
class LiquidationEvent(MarketEvent):
    event_type: str = "liquidation"
    EXPECTED_TYPE: ClassVar[str] = "liquidation"


EVENT_TYPES = dict(zip(PAYLOAD_TYPES, (TradeEvent, MarkPriceEvent, FundingEvent, OpenInterestEvent, BookTickerEvent, LiquidationEvent)))


def event_to_dict(event: MarketEvent) -> dict[str, Any]:
    if not isinstance(event, MarketEvent):
        raise ValueError("expected MarketEvent")
    result = asdict(event)
    result["quality_flags"] = list(event.quality_flags)
    return result


def event_from_dict(data: Mapping[str, Any]) -> MarketEvent:
    if not isinstance(data, Mapping):
        raise ValueError("event must be an object")
    values = dict(data)
    event_type = values.get("event_type")
    if not isinstance(event_type, str) or event_type not in EVENT_TYPES:
        raise ValueError("unsupported event_type")
    payload = values.get("payload")
    if not isinstance(payload, Mapping):
        raise ValueError("payload must be an object")
    allowed = {field.name for field in fields(MarketEvent)}
    if set(values) - allowed:
        raise ValueError("unknown event fields")
    flags = values.get("quality_flags", ())
    if not isinstance(flags, (tuple, list)):
        raise ValueError("quality_flags must be a sequence")
    values["quality_flags"] = tuple(flags)
    try:
        values["payload"] = PAYLOAD_TYPES[event_type](**dict(payload))
        return EVENT_TYPES[event_type](**values)
    except TypeError as exc:
        raise ValueError("invalid event or payload fields") from exc
