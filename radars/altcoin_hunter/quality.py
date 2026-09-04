"""Bounded, clock-injected market-data quality accounting.

These are minute rollups, never a second copy of the input event stream.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Mapping

from .models import bounded_text, strict_int, timestamp_ms


@dataclass(frozen=True)
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source, "exchange": self.exchange, "market": self.market,
            "instrument_id": self.instrument_id, "minute_ms": self.minute_ms,
            "counters": dict(self.counters),
            "max_processing_latency_ms": self.max_processing_latency_ms,
            "max_event_latency_ms": self.max_event_latency_ms,
            "max_queue_depth": self.max_queue_depth,
            "max_checkpoint_lag_ms": self.max_checkpoint_lag_ms,
            "status_changes": [
                {"at_ms": at, "status": status, "reason": reason}
                for at, status, reason in self.status_changes
            ],
        }


class QualityTracker:
    """Finite counters and compressed status transitions with explicit timestamps."""

    def __init__(self, *, max_rollups: int = 8192, max_status_changes: int = 16) -> None:
        if max_rollups < 1 or max_status_changes < 1:
            raise ValueError("quality limits must be positive")
        self.max_rollups = max_rollups
        self.max_status_changes = max_status_changes
        self._rows: dict[tuple[str, str, str, str, int], dict[str, Any]] = {}
        self._overflow = 0

    @staticmethod
    def _key(context: Any, observed_ms: int) -> tuple[str, str, str, str, int]:
        timestamp_ms(observed_ms, "observed_ms")
        def field(name: str) -> str:
            return str(context.get(name, "") if isinstance(context, Mapping) else getattr(context, name, ""))
        return (*(field(name) for name in ("source", "exchange", "market", "instrument_id")),
                int(observed_ms) // 60_000 * 60_000)

    def _row(self, context: Any, observed_ms: int) -> dict[str, Any] | None:
        key = self._key(context, observed_ms)
        if key not in self._rows:
            if len(self._rows) >= self.max_rollups:
                self._overflow += 1
                return None
            self._rows[key] = {"counters": Counter(), "latency": 0, "event_latency": 0,
                               "queue_depth": 0, "checkpoint_lag": 0, "changes": []}
        return self._rows[key]

    def record(
        self, context: Any, counter: str, *, observed_ms: int,
        amount: int = 1, processing_latency_ms: int = 0,
        event_latency_ms: int = 0,
        queue_depth: int = 0, checkpoint_lag_ms: int = 0,
    ) -> None:
        bounded_text(counter, "counter", limit=64)
        for name, value in (("amount", amount), ("processing_latency_ms", processing_latency_ms),
                            ("event_latency_ms", event_latency_ms), ("queue_depth", queue_depth),
                            ("checkpoint_lag_ms", checkpoint_lag_ms)):
            strict_int(value, name)
        row = self._row(context, observed_ms)
        if row is not None:
            name = str(counter)
            if name not in row["counters"] and len(row["counters"]) >= 63:
                name = "other_counters"
            row["counters"][name] += amount
            row["latency"] = max(row["latency"], max(0, int(processing_latency_ms)))
            row["event_latency"] = max(row["event_latency"], max(0, int(event_latency_ms)))
            row["queue_depth"] = max(row["queue_depth"], queue_depth)
            row["checkpoint_lag"] = max(row["checkpoint_lag"], checkpoint_lag_ms)
        key = self._key(context, observed_ms)
        if key[3] not in {"", "*"}:
            source_context = dict(zip(("source", "exchange", "market", "instrument_id"), (*key[:3], "*")))
            self.record(source_context, counter, observed_ms=observed_ms, amount=amount,
                        processing_latency_ms=processing_latency_ms, event_latency_ms=event_latency_ms,
                        queue_depth=queue_depth, checkpoint_lag_ms=checkpoint_lag_ms)

    def status(self, context: Any, status: str, reason: str, *, observed_ms: int) -> None:
        bounded_text(status, "status", limit=32)
        if not isinstance(reason, str) or len(reason) > 2048:
            raise ValueError("invalid status reason")
        row = self._row(context, observed_ms)
        if row is None:
            return
        changes = row["changes"]
        value = (int(observed_ms), str(status), str(reason))
        if changes and changes[-1][1:] == value[1:]:
            return
        if len(changes) < self.max_status_changes:
            changes.append(value)
        else:
            row["counters"]["status_changes_truncated"] += 1

    def prepare(self, through_ms: int) -> tuple[HealthRollup, ...]:
        timestamp_ms(through_ms, "through_ms")
        return tuple(
            HealthRollup(*key[:4], key[4], tuple(sorted(row["counters"].items())),
                         row["latency"], tuple(row["changes"]), row["event_latency"],
                         row["queue_depth"], row["checkpoint_lag"])
            for key, row in sorted(self._rows.items()) if key[4] + 60_000 <= through_ms
        )

    def acknowledge(self, rollups: tuple[HealthRollup, ...]) -> None:
        # New observations may arrive while a prepared batch awaits retry. Remove
        # only the frozen counts; retain the later observations for another batch.
        for frozen in rollups:
            key = (frozen.source, frozen.exchange, frozen.market, frozen.instrument_id, frozen.minute_ms)
            row = self._rows.get(key)
            if row is None:
                continue
            row["counters"].subtract(dict(frozen.counters))
            row["counters"] += Counter()
            prefix = len(frozen.status_changes)
            row["changes"] = row["changes"][prefix:]
            if not row["counters"] and not row["changes"]:
                self._rows.pop(key, None)

    def stats(self) -> dict[str, int]:
        total: Counter[str] = Counter()
        # Source aggregate rows already include instrument observations; do not
        # double-count them in the process-level diagnostic total.
        for key, row in self._rows.items():
            if key[3] in {"", "*"}:
                total.update(row["counters"])
        return {**dict(total), "open_quality_rollups": len(self._rows), "quality_overflow": self._overflow}
