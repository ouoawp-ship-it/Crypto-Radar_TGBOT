"""Pure, route-aware Binance USD-M subscription plans; never opens a socket.

Wire spelling follows the official migration mapping: the default three-second
global mark stream is !markPrice@arr, never an invented @3s suffix. Internal
stream identities are lowercase. Global streams consume connection slots too.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable, Mapping

from .adapters.base import PROTOCOL_VERSION, Route, identifier


def _route(value: Route | str) -> Route:
    if isinstance(value, Route):
        return value
    if not isinstance(value, str):
        raise ValueError("invalid_route")
    try:
        return Route[value.upper()]
    except KeyError as exc:
        raise ValueError("invalid_route") from exc


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"invalid_{name}")
    return value


def _symbol(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_]{1,64}", value):
        raise ValueError("invalid_symbol")
    return value.upper()


@dataclass(frozen=True, slots=True)
class StreamSpec:
    route: Route
    canonical_name: str
    wire_name: str
    symbol: str | None
    kind: str
    expected_interval_ms: int
    instrument_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "route", _route(self.route))
        _integer(self.expected_interval_ms, "stream_interval", 0, 5000)
        identifier(self.wire_name, "wire_name")
        if self.canonical_name != self.wire_name.lower() or len(self.wire_name) > 128:
            raise ValueError("invalid_stream_identity")
        expected = {
            "agg_trade": (Route.MARKET, f"{(self.symbol or '').lower()}@aggTrade", 100),
            "mark_price": (Route.MARKET, "!markPrice@arr" if self.expected_interval_ms == 3000 else "!markPrice@arr@1s", self.expected_interval_ms),
            "book_ticker_all": (Route.PUBLIC, "!bookTicker", 5000),
            "book_ticker": (Route.PUBLIC, f"{(self.symbol or '').lower()}@bookTicker", 0),
            "liquidation": (Route.MARKET, "!forceOrder@arr", 1000),
        }.get(self.kind)
        if expected is None or expected != (self.route, self.wire_name, self.expected_interval_ms):
            raise ValueError("stream_route_or_wire_name_mismatch")
        if self.kind == "mark_price" and self.expected_interval_ms not in (1000, 3000):
            raise ValueError("invalid_mark_interval")
        if self.kind in {"agg_trade", "book_ticker"}:
            if self.symbol != _symbol(self.symbol):
                raise ValueError("symbol_must_be_canonical_uppercase")
            if not isinstance(self.instrument_id, str) or not self.instrument_id or len(self.instrument_id) > 128:
                raise ValueError("explicit_instrument_id_required")
            identifier(self.instrument_id, "instrument_id")
        elif self.symbol is not None or self.instrument_id is not None:
            raise ValueError("global_stream_cannot_have_symbol")

    @property
    def key(self) -> tuple[str, str]:
        return self.route.name, self.canonical_name

    def to_dict(self) -> dict[str, Any]:
        return {"route": self.route.name, "path": self.route.path,
                "canonical_name": self.canonical_name, "wire_name": self.wire_name,
                "symbol": self.symbol, "kind": self.kind,
                "instrument_id": self.instrument_id,
                "expected_interval_ms": self.expected_interval_ms}


@dataclass(frozen=True, slots=True)
class ShardPlan:
    route: Route
    shard_id: str
    streams: tuple[StreamSpec, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.route, Route) or not re.fullmatch(r"(?:market|public)-[0-9]{4}", self.shard_id):
            raise ValueError("invalid_shard_identity")
        if not self.shard_id.startswith(self.route.name.lower() + "-"):
            raise ValueError("shard_route_mismatch")
        if not isinstance(self.streams, tuple) or len(self.streams) > 1024:
            raise ValueError("invalid_shard_stream_count")
        if any(stream.route != self.route for stream in self.streams) or len({s.key for s in self.streams}) != len(self.streams):
            raise ValueError("invalid_shard_streams")

    def to_dict(self) -> dict[str, Any]:
        return {"route": self.route.name, "path": self.route.path, "shard_id": self.shard_id,
                "stream_count": len(self.streams), "streams": [s.to_dict() for s in self.streams]}


@dataclass(frozen=True, slots=True)
class SubscriptionPlan:
    generation: int
    shards: tuple[ShardPlan, ...]
    eligible_symbols: tuple[str, ...]
    promoted_symbols: tuple[str, ...]
    uncovered_streams: tuple[StreamSpec, ...]
    max_streams_per_connection: int
    input_count: int
    include_liquidations: bool
    reason: str

    @property
    def coverage_incomplete(self) -> bool:
        return bool(self.uncovered_streams)

    @property
    def uncovered_instruments(self) -> tuple[str, ...]:
        if any(stream.symbol is None for stream in self.uncovered_streams):
            return self.eligible_symbols
        return tuple(sorted({stream.symbol for stream in self.uncovered_streams if stream.symbol is not None}))

    @property
    def coverage(self) -> dict[str, Any]:
        streams = tuple(s for shard in self.shards for s in shard.streams)
        kinds = {s.kind for s in streams}
        aggregate = {s.symbol for s in streams if s.kind == "agg_trade"}
        promoted = {s.symbol for s in streams if s.kind == "book_ticker"}
        total = len(self.eligible_symbols)
        def metric(covered: int, denominator: int = total, *, enabled: bool = True) -> dict[str, Any]:
            return {"covered": covered, "total": denominator,
                    "ratio": covered / denominator if denominator else None, "enabled": enabled}
        return {"input_instruments": self.input_count, "eligible_instruments": total,
                "excluded_instruments": self.input_count - total,
                "agg_trade": metric(len(aggregate)),
                "mark_price": metric(total if "mark_price" in kinds else 0),
                "basic_bbo": metric(total if "book_ticker_all" in kinds else 0),
                "promoted_bbo": metric(len(promoted), len(self.promoted_symbols)),
                "liquidation": metric(total if "liquidation" in kinds else 0, enabled=self.include_liquidations),
                "uncovered_streams": len(self.uncovered_streams),
                "coverage_incomplete": self.coverage_incomplete,
                "uncovered_instruments": list(self.uncovered_instruments),
                "uncovered_agg_trade_symbols": sorted(set(self.eligible_symbols) - aggregate)}

    def to_dict(self) -> dict[str, Any]:
        return {"protocol_version": PROTOCOL_VERSION, "generation": self.generation,
                "reason": self.reason, "mode": "plan_only", "network_calls": 0,
                "max_streams_per_connection": self.max_streams_per_connection,
                "official_max_streams_per_connection": 1024,
                "shards": [s.to_dict() for s in self.shards], "coverage": self.coverage,
                "eligible_symbols": list(self.eligible_symbols),
                "promoted_symbols": list(self.promoted_symbols),
                "coverage_incomplete": self.coverage_incomplete,
                "uncovered_instruments": list(self.uncovered_instruments),
                "uncovered": [s.to_dict() for s in self.uncovered_streams]}


def _eligible(instruments: Iterable[Any]) -> tuple[tuple[str, ...], int, dict[str, str]]:
    symbols: set[str] = set()
    identities: dict[str, str] = {}
    count = 0
    for record in instruments:
        count += 1
        if count > 100_000:
            raise ValueError("instrument_input_capacity")
        if isinstance(record, str):
            raise ValueError("bare_symbols_require_explicit_synthetic_instruments_helper")
        else:
            if hasattr(record, "to_dict"):
                record = record.to_dict()
            if not isinstance(record, Mapping):
                raise ValueError("instrument_must_be_symbol_or_mapping")
            symbol = _symbol(record.get("exchange_symbol", record.get("symbol")))
            status = record.get("eligibility_status", "INELIGIBLE")
            status = getattr(status, "value", status)
            eligible = (status == "ELIGIBLE" and record.get("quote_currency") == "USDT"
                        and record.get("exchange") == "binance" and record.get("market") == "usdt_perpetual")
        if not symbol.endswith("USDT"):
            eligible = False
        if eligible:
            if symbol in symbols:
                raise ValueError("duplicate_subscription_symbol")
            instrument_id = record.get("instrument_id")
            if not isinstance(instrument_id, str) or not instrument_id or len(instrument_id) > 128:
                raise ValueError("explicit_instrument_id_required")
            symbols.add(symbol)
            identities[symbol] = instrument_id
    return tuple(sorted(symbols)), count, identities


def synthetic_instruments(count: int, *, start_index: int = 0) -> Iterable[dict[str, Any]]:
    """Explicitly invented directory for plan/simulation tests, no asset lookup."""
    _integer(count, "synthetic_instruments", 0, 100000)
    _integer(start_index, "synthetic_start", 0, 1000000)
    for index in range(start_index, start_index + count):
        symbol = f"COIN{index}USDT"
        yield {"exchange": "binance", "market": "usdt_perpetual", "instrument_id": symbol,
               "symbol": symbol, "exchange_symbol": symbol, "quote_currency": "USDT",
               "eligibility_status": "ELIGIBLE", "source": "synthetic_offline_directory"}


def plan_subscriptions(instruments: Iterable[Any], *, max_streams_per_connection: int = 800,
                       promoted_symbols: Iterable[str] = (), previous: SubscriptionPlan | None = None,
                       max_connections_per_route: Mapping[Route | str, int] | None = None,
                       include_liquidations: bool = False, mark_price_interval_sec: int = 3) -> SubscriptionPlan:
    """Preserve surviving assignments, fill vacancies, never compact on removal.

    A route connection budget limits assignment only; the eligible denominator
    and every uncovered requested stream remain visible in the returned plan.
    """
    cap = _integer(max_streams_per_connection, "stream_capacity", 1, 1024)
    if type(include_liquidations) is not bool or type(mark_price_interval_sec) is not int or mark_price_interval_sec not in (1, 3):
        raise ValueError("invalid_stream_options")
    if previous is not None and not isinstance(previous, SubscriptionPlan):
        raise ValueError("invalid_previous_plan")
    symbols, count, identities = _eligible(instruments)
    promoted = tuple(sorted({_symbol(s) for s in promoted_symbols}))
    if set(promoted) - set(symbols):
        raise ValueError("promoted_symbol_is_not_eligible")
    budgets = {route: 9999 for route in Route}
    if max_connections_per_route is not None:
        for route, budget in max_connections_per_route.items():
            budgets[_route(route)] = _integer(budget, "route_connection_budget", 0, 9999)
    desired: list[StreamSpec] = []
    def add(route: Route, wire: str, symbol: str | None, kind: str, interval: int) -> None:
        desired.append(StreamSpec(route, wire.lower(), wire, symbol, kind, interval, identities[symbol] if symbol else None))
    if symbols:
        add(Route.MARKET, "!markPrice@arr" + ("@1s" if mark_price_interval_sec == 1 else ""), None, "mark_price", mark_price_interval_sec * 1000)
        add(Route.PUBLIC, "!bookTicker", None, "book_ticker_all", 5000)
        if include_liquidations:
            add(Route.MARKET, "!forceOrder@arr", None, "liquidation", 1000)
    for symbol in symbols:
        add(Route.MARKET, symbol.lower() + "@aggTrade", symbol, "agg_trade", 100)
    for symbol in promoted:
        add(Route.PUBLIC, symbol.lower() + "@bookTicker", symbol, "book_ticker", 0)
    wanted = {s.key: s for s in desired}
    assigned: set[tuple[str, str]] = set()
    slots: dict[tuple[Route, str], list[StreamSpec]] = {}
    if previous:
        for shard in sorted(previous.shards, key=lambda s: s.shard_id):
            route_slots = [key for key in slots if key[0] == shard.route]
            if len(route_slots) >= budgets[shard.route]:
                continue
            surviving = sorted((wanted[s.key] for s in shard.streams if s.key in wanted), key=lambda s: s.canonical_name)[:cap]
            slots[(shard.route, shard.shard_id)] = surviving
            assigned.update(s.key for s in surviving)
    for stream in sorted(desired, key=lambda s: s.key):
        if stream.key in assigned:
            continue
        candidates = sorted(key for key, entries in slots.items() if key[0] == stream.route and len(entries) < cap)
        if not candidates:
            existing = {key[1] for key in slots if key[0] == stream.route}
            if len(existing) >= budgets[stream.route]:
                continue
            index = next(i for i in range(9999) if f"{stream.route.name.lower()}-{i:04d}" not in existing)
            key = stream.route, f"{stream.route.name.lower()}-{index:04d}"
            slots[key] = []
            candidates = [key]
        slots[candidates[0]].append(stream)
        assigned.add(stream.key)
    shards = tuple(ShardPlan(route, shard_id, tuple(sorted(entries, key=lambda s: s.canonical_name)))
                   for (route, shard_id), entries in sorted(slots.items(), key=lambda pair: pair[0][1]) if entries)
    uncovered = tuple(s for s in sorted(desired, key=lambda s: s.key) if s.key not in assigned)
    changed = (previous is None or shards != previous.shards or symbols != previous.eligible_symbols
               or promoted != previous.promoted_symbols or uncovered != previous.uncovered_streams
               or cap != previous.max_streams_per_connection or include_liquidations != previous.include_liquidations)
    generation = 1 if previous is None else previous.generation + int(changed)
    reason = "route_capacity_uncovered" if uncovered else "initial_plan" if previous is None else "directory_or_policy_changed" if changed else "unchanged"
    return SubscriptionPlan(generation, shards, symbols, promoted, uncovered, cap, count, include_liquidations, reason)


def diff_plans(before: SubscriptionPlan, after: SubscriptionPlan) -> dict[str, Any]:
    old = {stream.key: (shard.shard_id, stream) for shard in before.shards for stream in shard.streams}
    new = {stream.key: (shard.shard_id, stream) for shard in after.shards for stream in shard.streams}
    added, removed, retained = set(new) - set(old), set(old) - set(new), set(new) & set(old)
    moved = {key for key in retained if old[key][0] != new[key][0]}
    affected = {new[key][0] for key in added | moved} | {old[key][0] for key in removed | moved}
    def records(keys: set[tuple[str, str]], values: dict) -> list[dict[str, Any]]:
        return [{"shard_id": values[key][0], **values[key][1].to_dict()} for key in sorted(keys)]
    return {"from_generation": before.generation, "generation": after.generation, "reason": after.reason,
            "add": records(added, new), "remove": records(removed, old),
            "unchanged": records(retained - moved, new), "moved": records(moved, new),
            "streams_to_add": records(added | moved, new), "streams_to_remove": records(removed | moved, old),
            "unchanged_streams": records(retained - moved, new), "affected_connections": sorted(affected),
            "affected_shards": sorted(affected), "uncovered": [s.to_dict() for s in after.uncovered_streams]}
