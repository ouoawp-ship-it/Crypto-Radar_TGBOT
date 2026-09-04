"""Offline admission, bounded deduplication and honest all-market observations.

No persistence or transport is constructed here. Coverage intervals are supplied
by the connection simulator, never inferred from the presence of a trade.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Iterable, Mapping
from types import MappingProxyType

from .adapters.base import ParseDiagnostics, ParseResult, Route, identifier
from .models import MarketEvent, strict_int, timestamp_ms


@dataclass(frozen=True)
class AdmissionContext:
    route: Route
    connection_epoch: int
    active: bool
    subscription_acked: bool
    liveness_valid: bool
    local_data_loss: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "route", Route(self.route))
        strict_int(self.connection_epoch, "connection_epoch")
        for field in ("active", "subscription_acked", "liveness_valid", "local_data_loss"):
            if type(getattr(self, field)) is not bool:
                raise ValueError("invalid_admission_boolean")

    @property
    def unavailable_reason(self) -> str | None:
        if not self.active:
            return "connection_not_active"
        if not self.subscription_acked:
            return "subscription_not_acked"
        if not self.liveness_valid:
            return "liveness_expired"
        if self.local_data_loss:
            return "local_data_loss"
        return None


@dataclass(frozen=True)
class AdmissionResult:
    events: tuple[MarketEvent, ...]
    duplicate_count: int
    rejected_count: int
    priority_upgrades: int
    diagnostics: dict[str, Any]
    event_metadata: tuple[Mapping[str, Any], ...] = ()
    priority_updates: tuple[Mapping[str, Any], ...] = ()


class OfflineIngestion:
    """At most max_dedup_keys keys; no exactly-once promise beyond that horizon.

    Global and promoted BBO share source identity. An identical promoted update
    can upgrade provenance without emitting a second event. Update IDs are
    monotonic per instrument; an older BBO cannot replace a newer quote.
    """

    def __init__(self, *, max_dedup_keys: int = 100_000, max_instruments: int = 4096,
                 max_future_skew_ms: int = 2000,
                 diagnostics: ParseDiagnostics | None = None) -> None:
        strict_int(max_dedup_keys, "max_dedup_keys", minimum=1, maximum=1_000_000)
        strict_int(max_instruments, "max_instruments", minimum=1, maximum=100_000)
        self.max_dedup_keys = max_dedup_keys
        self.max_instruments = max_instruments
        self.max_future_skew_ms = strict_int(max_future_skew_ms, "max_future_skew_ms", maximum=60000)
        self.diagnostics = diagnostics if diagnostics is not None else ParseDiagnostics()
        self._seen: OrderedDict[tuple, int] = OrderedDict()
        self._bbo: OrderedDict[tuple, tuple[int, int]] = OrderedDict()
        self.dedup_evictions = 0

    def admit(self, result: ParseResult, *, context: AdmissionContext, now_ms: int,
              promoted: bool | None = None) -> AdmissionResult:
        timestamp_ms(now_ms)
        if promoted is not None and type(promoted) is not bool:
            raise ValueError("invalid_promoted")
        accepted: list[MarketEvent] = []
        accepted_metadata: list[Mapping[str, Any]] = []
        priority_updates: list[Mapping[str, Any]] = []
        duplicates = rejected = upgrades = 0
        for item in result.rejected_items:
            self.diagnostics.record(item.reason, observed_at_ms=now_ms, detail=item.details)
        # Parser aggregate counters include rejects whose bounded detail was omitted.
        omitted = max(0, result.diagnostics.get("rejected_count", len(result.rejected_items))
                      - len(result.rejected_items))
        if omitted:
            self.diagnostics.record("parser_reject_details_suppressed", observed_at_ms=now_ms, amount=omitted)
        for index, event in enumerate(result.events):
            reason = context.unavailable_reason
            expected_route = Route.PUBLIC if event.event_type == "book_ticker" else Route.MARKET
            if reason is None and context.route != expected_route:
                reason = "wrong_route"
            if reason is None and context.connection_epoch != event.connection_epoch:
                reason = "stale_connection_epoch"
            if reason is None and event.event_time_ms > now_ms + self.max_future_skew_ms:
                reason = "future_event_time"
            if reason is not None:
                rejected += 1
                self.diagnostics.record(reason, observed_at_ms=now_ms)
                continue
            metadata = result.event_metadata[index] if result.event_metadata else {}
            is_promoted = metadata.get("promoted", False) if promoted is None else promoted
            if type(is_promoted) is not bool:
                raise ValueError("invalid_promoted_metadata")
            priority = 2 if is_promoted else 1
            key = event.dedup_key
            if key in self._seen:
                if event.event_type == "book_ticker" and priority > self._seen[key]:
                    self._seen[key] = priority
                    upgrades += 1
                    identity = (event.exchange, event.market, event.instrument_id)
                    if self._bbo.get(identity, (None, None))[0] == event.sequence_end:
                        self._bbo[identity] = (event.sequence_end, priority)
                    priority_updates.append(MappingProxyType({"dedup_key": key, "priority": priority,
                                            "metadata": MappingProxyType(dict(metadata))}))
                duplicates += 1
                self._seen.move_to_end(key)
                self.diagnostics.record("duplicate_event", observed_at_ms=now_ms)
                continue
            if event.event_type == "book_ticker":
                identity = (event.exchange, event.market, event.instrument_id)
                sequence = event.sequence_end
                if sequence is None:
                    rejected += 1
                    self.diagnostics.record("missing_bbo_update_id", observed_at_ms=now_ms)
                    continue
                last = self._bbo.get(identity)
                if last is not None and sequence <= last[0]:
                    rejected += 1
                    self.diagnostics.record("stale_bbo_update", observed_at_ms=now_ms)
                    continue
                if identity not in self._bbo and len(self._bbo) >= self.max_instruments:
                    rejected += 1
                    self.diagnostics.record("instrument_capacity_exceeded", observed_at_ms=now_ms)
                    continue
                self._bbo[identity] = (sequence, priority)
            self._seen[key] = priority
            if len(self._seen) > self.max_dedup_keys:
                self._seen.popitem(last=False)
                self.dedup_evictions += 1
                self.diagnostics.record("dedup_horizon_evicted", observed_at_ms=now_ms)
            accepted.append(event)
            accepted_metadata.append(MappingProxyType(dict(metadata)))
        return AdmissionResult(tuple(accepted), duplicates, rejected, upgrades, self.diagnostics.snapshot(),
                               tuple(accepted_metadata), tuple(priority_updates))

    @property
    def retained_dedup_keys(self) -> int:
        return len(self._seen)


class AllMarketObservation:
    """An array is an observation, not proof of exchange directory completeness."""

    def __init__(self, *, max_instruments: int = 4096, sample_limit: int = 64) -> None:
        strict_int(max_instruments, "max_instruments", minimum=1, maximum=100_000)
        strict_int(sample_limit, "sample_limit", minimum=1, maximum=1024)
        self.max_instruments = max_instruments
        self.sample_limit = sample_limit
        self._last: dict[str, Any] | None = None

    def record(self, *, expected_symbols: Iterable[str], observed_symbols: Iterable[str],
               receive_time_ms: int, last_valid_event_time_ms: int | None,
               connection_active: bool, st_filtered_count: int = 0) -> dict[str, Any]:
        timestamp_ms(receive_time_ms)
        if last_valid_event_time_ms is not None:
            timestamp_ms(last_valid_event_time_ms)
            if last_valid_event_time_ms > receive_time_ms:
                raise ValueError("future_array_event_time")
        if type(connection_active) is not bool:
            raise ValueError("invalid_connection_active")
        strict_int(st_filtered_count, "st_filtered_count")
        def bounded(values: Iterable[str]) -> set[str]:
            result = set()
            for index, symbol in enumerate(values):
                if index >= self.max_instruments:
                    raise ValueError("observation_capacity_exceeded")
                result.add(identifier(symbol, "symbol"))
            return result
        expected, observed = bounded(expected_symbols), bounded(observed_symbols)
        known = expected & observed
        missing, unknown = sorted(expected - observed), sorted(observed - expected)
        self._last = {"connection_active": connection_active,
                      "latest_array_received_ms": receive_time_ms,
                      "expected_universe_count": len(expected), "observed_universe_count": len(known),
                      "unknown_symbol_count": len(unknown), "unknown_symbols": unknown[:self.sample_limit],
                      "missing_symbol_count": len(missing), "missing_symbols": missing[:self.sample_limit],
                      "st_filtered_count": st_filtered_count,
                      "last_valid_event_time_ms": last_valid_event_time_ms,
                      "observed_symbol_ratio": len(known) / len(expected) if expected else None,
                      "directory_completeness_proven": False, "depth_available": False}
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        if self._last is None:
            return {"status": "unavailable", "reason": "no_array_received"}
        return {key: list(value) if isinstance(value, list) else value for key, value in self._last.items()}
