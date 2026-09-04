"""Bounded minute health observations with immutable commit generations.

Ordinary instrument activity remains an in-memory diagnostic. Durable output
contains source aggregates and instrument exceptions, never a raw event log.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Mapping
from unicodedata import category

from .models import bounded_text, strict_int, timestamp_ms


IDENTITY_FIELDS = ("source", "exchange", "market", "instrument_id")
Identity = tuple[str, str, str, str]
StatusIdentity = tuple[str, str, str, str, str]
HealthKey = tuple[str, str, str, str, int]
NORMAL_COUNTERS = frozenset({"accepted_events", "non_trade_events", "health_observations", "connection_observations"})


def validate_health_identity(context: Any) -> Identity:
    """Reject absent/ambiguous identity; only callers may explicitly supply '*'."""
    values = []
    for name in IDENTITY_FIELDS:
        value = context.get(name) if isinstance(context, Mapping) else getattr(context, name, None)
        if type(value) is not str:
            raise ValueError(f"{name} must be an explicit string")
        bounded_text(value, name, limit=128)
        if any(category(character) in {"Cc", "Cf"} for character in value):
            raise ValueError(f"{name} contains control or format characters")
        values.append(value)
    return tuple(values)  # type: ignore[return-value]


class PreparedHealth(tuple):
    """An immutable tuple whose object identity is its acknowledgement token.

    A tuple subclass gives even an empty generation its own identity. A plain
    empty tuple is a singleton and cannot safely serve as a generation token.
    """


@dataclass(frozen=True, slots=True)
class HealthRollup:
    source: str
    exchange: str
    market: str
    instrument_id: str
    minute_ms: int
    counters: tuple[tuple[str, int], ...] = ()
    max_processing_latency_ms: int = 0
    status_changes: tuple[tuple[int, str, str], ...] = ()
    max_event_latency_ms: int = 0
    max_queue_depth: int = 0
    max_checkpoint_lag_ms: int = 0
    connection_epochs: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        validate_health_identity(self)
        timestamp_ms(self.minute_ms, "minute_ms")
        if self.minute_ms % 60_000:
            raise ValueError("health minute must be aligned")
        for name in ("max_processing_latency_ms", "max_event_latency_ms", "max_queue_depth", "max_checkpoint_lag_ms"):
            strict_int(getattr(self, name), name)
        if not isinstance(self.counters, tuple) or len(self.counters) > 64:
            raise ValueError("invalid health counters")
        seen = set()
        for name, value in self.counters:
            bounded_text(name, "counter", limit=64)
            strict_int(value, "counter value")
            if name in seen:
                raise ValueError("duplicate health counter")
            seen.add(name)
        if (not isinstance(self.connection_epochs, tuple) or len(self.connection_epochs) > 32
                or len(set(self.connection_epochs)) != len(self.connection_epochs)):
            raise ValueError("invalid connection epochs")
        for epoch in self.connection_epochs:
            strict_int(epoch, "connection_epoch")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source, "exchange": self.exchange, "market": self.market,
            "instrument_id": self.instrument_id, "minute_ms": self.minute_ms,
            "counters": dict(self.counters),
            "max_processing_latency_ms": self.max_processing_latency_ms,
            "max_event_latency_ms": self.max_event_latency_ms,
            "max_queue_depth": self.max_queue_depth,
            "max_checkpoint_lag_ms": self.max_checkpoint_lag_ms,
            "connection_epochs": list(self.connection_epochs),
            "status_changes": [
                {"at_ms": at, "status": status, "reason": reason}
                for at, status, reason in self.status_changes
            ],
        }


class QualityTracker:
    """Two bounded buffers: a frozen retry generation and new active observations.

    max_rollups applies to each buffer, bounding retained rows at twice that
    value while a writer is blocked. Instrument rows persist for a non-routine
    counter, a genuine status change, or event latency >= 2000 ms / processing
    latency >= 500 ms by default. Queue and checkpoint gauges alone stay in the
    source aggregate. These explicit thresholds are monitoring policy, not a
    promise of acceptable production latency.
    """

    def __init__(self, *, max_rollups: int = 8192, max_status_changes: int = 16,
                 max_status_identities: int = 8192, max_connection_epochs: int = 32,
                 event_latency_threshold_ms: int = 2000,
                 processing_latency_threshold_ms: int = 500) -> None:
        for name, value in (("max_rollups", max_rollups), ("max_status_changes", max_status_changes),
                            ("max_status_identities", max_status_identities), ("max_connection_epochs", max_connection_epochs),
                            ("event_latency_threshold_ms", event_latency_threshold_ms),
                            ("processing_latency_threshold_ms", processing_latency_threshold_ms)):
            strict_int(value, name, minimum=1)
        strict_int(max_connection_epochs, "max_connection_epochs", minimum=1, maximum=32)
        self.max_rollups = max_rollups
        self.max_status_changes = max_status_changes
        self.max_status_identities = max_status_identities
        self.max_connection_epochs = max_connection_epochs
        self.event_latency_threshold_ms = event_latency_threshold_ms
        self.processing_latency_threshold_ms = processing_latency_threshold_ms
        self._rows: dict[HealthKey, dict[str, Any]] = {}
        self._prepared_rows: dict[HealthKey, dict[str, Any]] = {}
        self._prepared: PreparedHealth | None = None
        self._last_status: dict[StatusIdentity, tuple[str, str]] = {}
        self._overflow = 0
        self._generation = 0

    @staticmethod
    def _key(context: Any, observed_ms: int) -> HealthKey:
        timestamp_ms(observed_ms, "observed_ms")
        return (*validate_health_identity(context), observed_ms // 60_000 * 60_000)

    def _row(self, key: HealthKey) -> dict[str, Any] | None:
        if key not in self._rows:
            if len(self._rows) >= self.max_rollups:
                self._overflow += 1
                return None
            self._rows[key] = {"counters": Counter(), "latency": 0, "event_latency": 0,
                               "queue_depth": 0, "checkpoint_lag": 0, "changes": [], "epochs": set()}
        return self._rows[key]

    @staticmethod
    def _count(row: dict[str, Any], name: str, amount: int = 1) -> None:
        if name not in row["counters"] and len(row["counters"]) >= 63:
            name = "other_counters"
        row["counters"][name] += amount

    def _update(self, key: HealthKey, counter: str, amount: int, processing_latency_ms: int,
                event_latency_ms: int, queue_depth: int, checkpoint_lag_ms: int,
                connection_epoch: int | None) -> dict[str, Any] | None:
        row = self._row(key)
        if row is None:
            return None
        self._count(row, counter, amount)
        row["latency"] = max(row["latency"], processing_latency_ms)
        row["event_latency"] = max(row["event_latency"], event_latency_ms)
        row["queue_depth"] = max(row["queue_depth"], queue_depth)
        row["checkpoint_lag"] = max(row["checkpoint_lag"], checkpoint_lag_ms)
        if connection_epoch is not None and connection_epoch not in row["epochs"]:
            if len(row["epochs"]) < self.max_connection_epochs:
                row["epochs"].add(connection_epoch)
            else:
                # This counts observations whose epoch could not be retained,
                # including repeated observations of the same omitted value.
                self._count(row, "connection_epoch_overflow_observations")
        return row

    def record(self, context: Any, counter: str, *, observed_ms: int, amount: int = 1,
               processing_latency_ms: int = 0, event_latency_ms: int = 0,
               queue_depth: int = 0, checkpoint_lag_ms: int = 0,
               connection_epoch: int | None = None) -> None:
        key = self._key(context, observed_ms)
        bounded_text(counter, "counter", limit=64)
        for name, value in (("amount", amount), ("processing_latency_ms", processing_latency_ms),
                            ("event_latency_ms", event_latency_ms), ("queue_depth", queue_depth),
                            ("checkpoint_lag_ms", checkpoint_lag_ms)):
            strict_int(value, name)
        if connection_epoch is not None:
            strict_int(connection_epoch, "connection_epoch")
        arguments = (counter, amount, processing_latency_ms, event_latency_ms,
                     queue_depth, checkpoint_lag_ms, connection_epoch)
        if key[3] == "*":
            self._update(key, *arguments)
            return
        # Reserve the source total first so capacity pressure on instrument
        # detail cannot silently remove the aggregate failure evidence.
        source_key = (*key[:3], "*", key[4])
        source = self._update(source_key, *arguments)
        row = self._update(key, *arguments)
        if row is None and source is not None:
            self._count(source, "instrument_rollup_overflow")

    def status(self, context: Any, status: str, reason: str, *, observed_ms: int,
               dimension: str = "default") -> None:
        key = self._key(context, observed_ms)
        bounded_text(status, "status", limit=32)
        bounded_text(dimension, "status dimension", limit=32)
        if type(reason) is not str or len(reason) > 2048 or any(ord(c) < 32 or ord(c) == 127 for c in reason):
            raise ValueError("invalid status reason")
        # Different typed metrics can share a source and instrument. A healthy
        # funding observation must not reset an outstanding OI quality problem.
        identity = (*key[:4], dimension)
        previous = self._last_status.get(identity)
        current = status, reason
        cache_available = identity in self._last_status or len(self._last_status) < self.max_status_identities
        if cache_available:
            self._last_status[identity] = current
        else:
            self.record(context, "status_memory_overflow", observed_ms=observed_ms)
        if status != "complete":
            self.record(context, "incomplete_observations", observed_ms=observed_ms)
        # First healthy observation initializes the baseline status. It is not
        # a recovery or a change, and must not write one row per new instrument.
        if previous == current or (previous is None and status == "complete"):
            return
        row = self._row(key)
        if row is None:
            source_key = (*key[:3], "*", key[4])
            source = self._row(source_key)
            if source is not None:
                self._count(source, "status_rollup_overflow")
            return
        if len(row["changes"]) < self.max_status_changes:
            row["changes"].append((observed_ms, status, reason))
        else:
            self._count(row, "status_changes_truncated")

    def _persist(self, key: HealthKey, row: dict[str, Any]) -> bool:
        return (key[3] == "*" or bool(row["changes"])
                or any(name not in NORMAL_COUNTERS and amount for name, amount in row["counters"].items())
                or row["event_latency"] >= self.event_latency_threshold_ms
                or row["latency"] >= self.processing_latency_threshold_ms)

    def prepare(self, through_ms: int) -> PreparedHealth:
        timestamp_ms(through_ms, "through_ms")
        if self._prepared is not None:
            return self._prepared
        keys = sorted(key for key in self._rows if key[4] + 60_000 <= through_ms)
        prepared_rows = {key: self._rows[key] for key in keys}
        prepared = PreparedHealth(
            HealthRollup(*key[:4], key[4], tuple(sorted(row["counters"].items())),
                         row["latency"], tuple(row["changes"]), row["event_latency"],
                         row["queue_depth"], row["checkpoint_lag"], tuple(sorted(row["epochs"])))
            for key, row in prepared_rows.items() if self._persist(key, row)
        )
        # Construct and validate every immutable row before detaching anything.
        # If freezing fails, the active observations remain available to retry.
        for key in keys:
            del self._rows[key]
        self._prepared_rows = prepared_rows
        self._prepared = prepared
        self._generation += 1
        return self._prepared

    def acknowledge(self, rollups: tuple[HealthRollup, ...]) -> None:
        if self._prepared is None or rollups is not self._prepared:
            raise ValueError("health acknowledgement must match its prepared generation")
        self._prepared_rows = {}
        self._prepared = None

    def stats(self) -> dict[str, int]:
        total: Counter[str] = Counter()
        for buffer in (self._rows, self._prepared_rows):
            for key, row in buffer.items():
                if key[3] == "*":
                    total.update(row["counters"])
        return {**dict(total), "open_quality_rollups": len(self._rows) + len(self._prepared_rows),
                "active_quality_rollups": len(self._rows), "prepared_quality_rollups": len(self._prepared_rows),
                "quality_generation": self._generation, "status_identity_count": len(self._last_status),
                "quality_overflow": self._overflow}
