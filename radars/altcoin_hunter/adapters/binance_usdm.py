"""Pure USD-M directory parsing. No transport, clock, credentials or database."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from math import isfinite
from types import MappingProxyType
from typing import Any, Mapping
from unicodedata import category

from ..identity import InstrumentIdentity
from ..models import decimal_value, strict_int, timestamp_ms
from ..universe import EligibilityStatus, Instrument, ListingStage, UniverseRegistry
from .base import ParseLimits, RejectedItem


def _text(value: Any, name: str, maximum: int = 128) -> str:
    if (type(value) is not str or not value or value != value.strip() or len(value) > maximum
            or any(category(char) in {"Cc", "Cf"} for char in value)):
        raise ValueError(f"invalid_{name}")
    return value


def _decimal(value: Any, name: str, *, positive: bool = False) -> str:
    if type(value) is not str:
        raise ValueError(f"invalid_{name}")
    decimal_value(value, name, positive=positive, nonnegative=not positive)
    return value


def _validate_structure(value: Any, limits: ParseLimits, *, depth: int = 0) -> int:
    """Bound already-decoded inputs as well as text; never traverse arbitrary objects."""
    if depth > limits.max_depth:
        raise ValueError("payload_depth_limit")
    if type(value) is str:
        if len(value) > limits.max_string_length:
            raise ValueError("payload_string_limit")
        return len(value.encode("utf-8")) + 2
    if value is None or type(value) in (bool, int, float):
        if type(value) is float and not isfinite(value):
            raise ValueError("nonfinite_json")
        if type(value) is int and value.bit_length() > 64:
            raise ValueError("integer_limit")
        return 24
    if type(value) not in (dict, list):
        raise ValueError("invalid_json_type")
    if len(value) > limits.max_items:
        raise ValueError("payload_item_limit")
    total = 2
    values = value.items() if type(value) is dict else enumerate(value)
    for key, child in values:
        if type(value) is dict:
            if type(key) is not str:
                raise ValueError("invalid_json_key")
            total += _validate_structure(key, limits, depth=depth + 1)
        total += _validate_structure(child, limits, depth=depth + 1) + (2 if type(value) is dict else 1)
        if total > limits.max_payload_bytes:
            raise ValueError("payload_byte_limit")
    return total


def _decode_json(payload: Any, limits: ParseLimits) -> Any:
    if type(payload) in (bytes, str):
        raw = payload if type(payload) is bytes else payload.encode("utf-8")
        if len(raw) > limits.max_payload_bytes:
            raise ValueError("payload_byte_limit")
        def pairs(values):
            result = {}
            for key, value in values:
                if key in result:
                    raise ValueError("duplicate_json_key")
                result[key] = value
            return result
        try:
            return json.loads(raw, object_pairs_hook=pairs,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_json")))
        except (json.JSONDecodeError, UnicodeError, RecursionError) as exc:
            raise ValueError("malformed_json") from exc
    return payload


@dataclass(frozen=True, slots=True)
class BinanceInstrumentSpec:
    identity: InstrumentIdentity
    pair: str
    contract_type: str
    status: str
    base_asset: str
    quote_asset: str
    margin_asset: str
    underlying_type: str
    underlying_subtypes: tuple[str, ...]
    onboard_date_ms: int
    delivery_date_ms: int
    tick_size: str
    min_price: str
    max_price: str
    step_size: str
    min_quantity: str
    max_quantity: str
    min_notional: str
    market_step_size: str | None
    market_min_quantity: str | None
    market_max_quantity: str | None
    observed_at_ms: int
    max_notional: str | None = None
    metadata_version: int = 1
    unknown_field_count: int = 0

    @property
    def symbol(self) -> str:
        return self.identity.exchange_symbol

    @property
    def eligible(self) -> bool:
        return (self.contract_type == "PERPETUAL" and self.status == "TRADING"
                and self.quote_asset == "USDT" and self.margin_asset == "USDT"
                and self.underlying_type == "COIN")

    @property
    def metadata_fingerprint(self) -> str:
        values = self.to_dict()
        for name in ("observed_at_ms", "metadata_version", "unknown_field_count"):
            values.pop(name)
        return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_hunter_instrument(self) -> Instrument:
        reasons = []
        for condition, reason in ((self.contract_type != "PERPETUAL", "not_perpetual"),
                                  (self.status != "TRADING", "not_trading"),
                                  (self.quote_asset != "USDT" or self.margin_asset != "USDT", "not_usdt_settled"),
                                  (self.underlying_type != "COIN", "not_crypto_underlying")):
            if condition:
                reasons.append(reason)
        delisting = self.status in {"SETTLING", "DELIVERING", "DELIVERED", "CLOSE"}
        return Instrument(**self.identity.to_dict(), source="binance_exchange_info",
                          effective_at_ms=self.observed_at_ms,
                          eligibility_status=EligibilityStatus.ELIGIBLE if self.eligible else EligibilityStatus.INELIGIBLE,
                          listing_stage=ListingStage.DELISTING if delisting else ListingStage.UNKNOWN,
                          reason_codes=tuple(reasons), metadata_version=self.metadata_version,
                          data_quality="complete")


@dataclass(frozen=True, slots=True)
class ExchangeInfoResult:
    status: str
    observed_at_ms: int
    instruments: tuple[BinanceInstrumentSpec, ...] = ()
    rejected_items: tuple[RejectedItem, ...] = ()
    diagnostics: Mapping[str, int] | None = None

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"

    @property
    def registry(self) -> Mapping[str, BinanceInstrumentSpec]:
        return MappingProxyType({spec.symbol: spec for spec in self.instruments})


SYMBOL_FIELDS = frozenset({"symbol", "pair", "contractType", "status", "baseAsset", "quoteAsset", "marginAsset",
    "underlyingType", "underlyingSubType", "onboardDate", "deliveryDate", "filters", "pricePrecision",
    "quantityPrecision", "baseAssetPrecision", "quotePrecision", "maintMarginPercent", "requiredMarginPercent",
    "settlePlan", "triggerProtect", "orderTypes", "timeInForce", "liquidationFee", "marketTakeBound"})
FILTER_FIELDS = {"PRICE_FILTER": {"filterType", "tickSize", "minPrice", "maxPrice"},
                 "LOT_SIZE": {"filterType", "stepSize", "minQty", "maxQty"},
                 "MARKET_LOT_SIZE": {"filterType", "stepSize", "minQty", "maxQty"},
                 "MIN_NOTIONAL": {"filterType", "notional"},
                 "NOTIONAL": {"filterType", "minNotional", "maxNotional", "applyMinToMarket", "applyMaxToMarket", "avgPriceMins"}}


def _parse_symbol(row: Any, observed_at_ms: int, identities: Mapping[str, InstrumentIdentity]) -> BinanceInstrumentSpec:
    if type(row) is not dict:
        raise ValueError("symbol_not_object")
    symbol = _text(row.get("symbol"), "symbol")
    values = {key: _text(row.get(key), key) for key in (
        "pair", "contractType", "status", "baseAsset", "quoteAsset", "marginAsset", "underlyingType")}
    subtypes = row.get("underlyingSubType")
    if type(subtypes) is not list or len(subtypes) > 32:
        raise ValueError("invalid_underlying_subtypes")
    subtypes = tuple(_text(value, "underlying_subtype", 64) for value in subtypes)
    onboard = timestamp_ms(row.get("onboardDate"), "onboardDate")
    # Binance's perpetual delivery sentinel is in 2101, outside event-time range.
    delivery = strict_int(row.get("deliveryDate"), "deliveryDate", minimum=onboard)
    filters = row.get("filters")
    if type(filters) is not list or not filters or len(filters) > 64:
        raise ValueError("invalid_filters")
    by_type = {}
    unknown = len(set(row) - SYMBOL_FIELDS)
    for item in filters:
        if type(item) is not dict:
            raise ValueError("invalid_filter")
        kind = _text(item.get("filterType"), "filter_type", 64)
        if kind in by_type:
            raise ValueError("duplicate_filter")
        by_type[kind] = item
        unknown += len(set(item) - FILTER_FIELDS[kind]) if kind in FILTER_FIELDS else 1
    if not {"PRICE_FILTER", "LOT_SIZE"}.issubset(by_type) or not {"MIN_NOTIONAL", "NOTIONAL"}.intersection(by_type):
        raise ValueError("missing_required_filter")
    price, lot, market = by_type["PRICE_FILTER"], by_type["LOT_SIZE"], by_type.get("MARKET_LOT_SIZE")
    minimum = by_type["MIN_NOTIONAL"].get("notional") if "MIN_NOTIONAL" in by_type else by_type["NOTIONAL"].get("minNotional")
    numeric = {
        "tick_size": _decimal(price.get("tickSize"), "tick_size", positive=True),
        "min_price": _decimal(price.get("minPrice"), "min_price"),
        "max_price": _decimal(price.get("maxPrice"), "max_price", positive=True),
        "step_size": _decimal(lot.get("stepSize"), "step_size", positive=True),
        "min_quantity": _decimal(lot.get("minQty"), "min_quantity"),
        "max_quantity": _decimal(lot.get("maxQty"), "max_quantity", positive=True),
        "min_notional": _decimal(minimum, "min_notional"),
        "market_step_size": _decimal(market.get("stepSize"), "market_step_size") if market else None,
        "market_min_quantity": _decimal(market.get("minQty"), "market_min_quantity") if market else None,
        "market_max_quantity": _decimal(market.get("maxQty"), "market_max_quantity", positive=True) if market else None,
    }
    max_notional = None
    if "NOTIONAL" in by_type:
        alternative = _decimal(by_type["NOTIONAL"].get("minNotional"), "min_notional")
        if Decimal(alternative) != Decimal(numeric["min_notional"]):
            raise ValueError("conflicting_min_notional")
        if "maxNotional" in by_type["NOTIONAL"]:
            max_notional = _decimal(by_type["NOTIONAL"]["maxNotional"], "max_notional", positive=True)
            if Decimal(max_notional) < Decimal(alternative):
                raise ValueError("inverted_notional_range")
    for low, high in (("min_price", "max_price"), ("min_quantity", "max_quantity"),
                      ("market_min_quantity", "market_max_quantity")):
        if numeric[low] is not None and Decimal(numeric[low]) > Decimal(numeric[high]):
            raise ValueError("inverted_filter_range")
    identity = identities.get(symbol)
    if identity is None:
        identity = InstrumentIdentity(exchange="binance", market="usdt_perpetual", instrument_id=symbol,
                                      symbol=symbol, exchange_symbol=symbol, quote_currency=values["quoteAsset"])
    if (not isinstance(identity, InstrumentIdentity) or identity.exchange != "binance" or identity.market != "usdt_perpetual"
            or identity.exchange_symbol != symbol or identity.quote_currency != values["quoteAsset"]):
        raise ValueError("identity_mapping_conflict")
    return BinanceInstrumentSpec(identity=identity, pair=values["pair"], contract_type=values["contractType"],
        status=values["status"], base_asset=values["baseAsset"], quote_asset=values["quoteAsset"],
        margin_asset=values["marginAsset"], underlying_type=values["underlyingType"], underlying_subtypes=subtypes,
        onboard_date_ms=onboard, delivery_date_ms=delivery, observed_at_ms=observed_at_ms,
        unknown_field_count=unknown, max_notional=max_notional, **numeric)


def parse_exchange_info(payload: Any, *, observed_at_ms: int, complete: bool = True,
                        source_healthy: bool = True, previous_observed_at_ms: int | None = None,
                        identities: Mapping[str, InstrumentIdentity] | None = None,
                        limits: ParseLimits | None = None) -> ExchangeInfoResult:
    timestamp_ms(observed_at_ms, "observed_at_ms")
    if identities is not None and not isinstance(identities, Mapping):
        raise ValueError("invalid_identity_registry")
    if type(complete) is not bool or type(source_healthy) is not bool:
        raise ValueError("directory_health_flags_must_be_bool")
    if not source_healthy:
        return ExchangeInfoResult("source_unavailable", observed_at_ms)
    if not complete:
        return ExchangeInfoResult("incomplete", observed_at_ms)
    if previous_observed_at_ms is not None:
        timestamp_ms(previous_observed_at_ms, "previous_observed_at_ms")
        if observed_at_ms < previous_observed_at_ms:
            return ExchangeInfoResult("stale", observed_at_ms)
    policy = limits or ParseLimits()
    try:
        decoded = _decode_json(payload, policy)
        _validate_structure(decoded, policy)
        if type(decoded) is not dict or type(decoded.get("symbols")) is not list:
            raise ValueError("invalid_exchange_info")
        symbols = decoded["symbols"]
        if not symbols:
            return ExchangeInfoResult("incomplete", observed_at_ms)
        specs, rejected, seen = [], [], set()
        rejected_count = 0
        unknown = len(set(decoded) - {"symbols", "serverTime", "timezone", "rateLimits", "exchangeFilters", "assets"})
        for index, row in enumerate(symbols):
            try:
                spec = _parse_symbol(row, observed_at_ms, identities or {})
                if spec.symbol in seen:
                    raise ValueError("duplicate_directory_symbol")
                seen.add(spec.symbol)
                unknown += spec.unknown_field_count
                specs.append(spec)
            except (ValueError, TypeError, KeyError) as exc:
                rejected_count += 1
                if len(rejected) < policy.max_rejected_items:
                    rejected.append(RejectedItem(index, "malformed_symbol", str(exc)[:256]))
        diagnostics = MappingProxyType({"rejected_count": rejected_count, "unknown_field_count": unknown})
        # A valid sibling never makes a partially malformed directory publishable.
        return ExchangeInfoResult("malformed" if rejected_count else "accepted", observed_at_ms,
                                  () if rejected_count else tuple(specs), tuple(rejected), diagnostics)
    except (ValueError, TypeError, RecursionError) as exc:
        return ExchangeInfoResult("malformed", observed_at_ms, rejected_items=(RejectedItem(0, "malformed_directory", str(exc)[:256]),),
                                  diagnostics=MappingProxyType({"rejected_count": 1}))


def map_exchange_info_to_universe(result: ExchangeInfoResult, registry: UniverseRegistry,
                                 *, previous_specs: Mapping[str, BinanceInstrumentSpec] | None = None) -> ExchangeInfoResult:
    if not isinstance(result, ExchangeInfoResult) or not isinstance(registry, UniverseRegistry):
        raise ValueError("directory_mapping_contract")
    previous = dict(previous_specs or {})
    if not result.accepted:
        return replace(result, instruments=tuple(previous[key] for key in sorted(previous)))
    staged = {}
    for spec in result.instruments:
        old = previous.get(spec.symbol)
        version = (old.metadata_version + (old.metadata_fingerprint != spec.metadata_fingerprint)) if old else 1
        staged[spec.symbol] = replace(spec, metadata_version=version)
    try:
        refreshed = registry.refresh((spec.to_hunter_instrument() for spec in staged.values()),
                                     observed_at_ms=result.observed_at_ms)
    except (ValueError, TypeError):
        return replace(result, status="malformed", instruments=tuple(previous[key] for key in sorted(previous)))
    if not refreshed.accepted:
        status = "stale" if refreshed.reason.startswith("stale") else "incomplete"
        return replace(result, status=status, instruments=tuple(previous[key] for key in sorted(previous)))
    previous.update(staged)
    return replace(result, instruments=tuple(previous[key] for key in sorted(previous)))


class BinanceInstrumentDirectory:
    """One offline owner of a last-good directory and its orthogonal Universe view."""
    def __init__(self, *, max_instruments: int = 4096,
                 identities: Mapping[str, InstrumentIdentity] | None = None) -> None:
        self.universe = UniverseRegistry(max_instruments=max_instruments)
        self._specs: Mapping[str, BinanceInstrumentSpec] = MappingProxyType({})
        self._last_observed_ms: int | None = None
        self._identities = MappingProxyType(dict(identities or {}))

    def snapshot(self) -> Mapping[str, BinanceInstrumentSpec]:
        return self._specs

    def refresh(self, payload: Any, *, observed_at_ms: int, complete: bool = True,
                source_healthy: bool = True, limits: ParseLimits | None = None) -> ExchangeInfoResult:
        result = parse_exchange_info(payload, observed_at_ms=observed_at_ms, complete=complete,
            source_healthy=source_healthy, previous_observed_at_ms=self._last_observed_ms,
            identities=self._identities, limits=limits)
        result = map_exchange_info_to_universe(result, self.universe, previous_specs=self._specs)
        if result.accepted:
            self._specs = result.registry
            self._last_observed_ms = observed_at_ms
        return result
