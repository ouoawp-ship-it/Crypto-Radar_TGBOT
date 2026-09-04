"""Bounded event-time minute aggregation with an explicit commit handshake."""
from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from dataclasses import asdict, dataclass
from decimal import Decimal
from math import isfinite
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from .models import MarketEvent, TradePayload, bounded_text, decimal_value, strict_int, timestamp_ms
from .quality import HealthRollup, QualityTracker, validate_health_identity

MINUTE_MS = 60_000
SeriesKey = tuple[str, str, str, str]


@dataclass(frozen=True, slots=True)
class MinuteBucket:
    source: str
    exchange: str
    market: str
    instrument_id: str
    symbol: str
    start_ms: int
    end_ms: int
    connection_epoch: int
    connection_epochs: tuple[int, ...]
    price_open: str
    price_high: str
    price_low: str
    price_close: str
    buy_quote: str
    sell_quote: str
    quote_volume: str
    delta_quote: str
    trade_count: int
    first_event_ms: int
    last_event_ms: int
    sequence_start: int | None
    sequence_end: int | None
    first_source_event_id: str
    last_source_event_id: str
    event_id_digest: str
    coverage_ms: int
    quality_status: str
    quality_flags: tuple[str, ...]
    quote_currency: str
    base_quantity: str = "0"
    duplicate_count: int = 0
    late_count: int = 0
    gap_count: int = 0

    def __post_init__(self) -> None:
        for field in ("source", "exchange", "market", "instrument_id", "symbol", "first_source_event_id", "last_source_event_id", "event_id_digest", "quote_currency"):
            bounded_text(getattr(self, field), field)
        timestamp_ms(self.start_ms, "start_ms")
        timestamp_ms(self.end_ms, "end_ms")
        for field in ("trade_count", "coverage_ms", "duplicate_count", "late_count", "gap_count"):
            strict_int(getattr(self, field), field)
        strict_int(self.connection_epoch, "connection_epoch", minimum=-1)
        if not isinstance(self.connection_epochs, tuple) or not self.connection_epochs:
            raise ValueError("connection epochs must be a nonempty tuple")
        for epoch in self.connection_epochs:
            strict_int(epoch, "epoch")
        if len(set(self.connection_epochs)) != len(self.connection_epochs):
            raise ValueError("duplicate connection epoch")
        if (len(self.connection_epochs) == 1 and self.connection_epoch != self.connection_epochs[0]) or (len(self.connection_epochs) > 1 and self.connection_epoch != -1):
            raise ValueError("inconsistent connection epoch")
        if not isinstance(self.quality_flags, tuple) or any(not isinstance(flag, str) or not flag for flag in self.quality_flags):
            raise ValueError("quality flags must be strings")
        for field in ("sequence_start", "sequence_end"):
            if getattr(self, field) is not None:
                strict_int(getattr(self, field), field)
        if (self.sequence_start is None) != (self.sequence_end is None) or (self.sequence_start is not None and self.sequence_start > self.sequence_end):
            raise ValueError("inconsistent sequence span")
        if not self.start_ms <= self.first_event_ms <= self.last_event_ms < self.end_ms:
            raise ValueError("event timestamps outside minute")
        strict_int(self.first_event_ms, "first_event_ms")
        strict_int(self.last_event_ms, "last_event_ms")
        if self.start_ms % MINUTE_MS or self.end_ms - self.start_ms != MINUTE_MS:
            raise ValueError("invalid minute boundaries")
        if type(self.trade_count) is not int or self.trade_count < 1:
            raise ValueError("a minute bucket requires trades")
        values = {name: decimal_value(getattr(self, name), name) for name in
                  ("price_open", "price_high", "price_low", "price_close", "buy_quote", "sell_quote", "quote_volume", "delta_quote")}
        if any(not value.is_finite() for value in values.values()):
            raise ValueError("nonfinite bucket metric")
        if min(values[name] for name in ("price_open", "price_high", "price_low", "price_close")) <= 0:
            raise ValueError("bucket prices must be positive")
        if values["price_low"] > min(values["price_open"], values["price_close"]) or values["price_high"] < max(values["price_open"], values["price_close"]):
            raise ValueError("inconsistent OHLC")
        if min(values["buy_quote"], values["sell_quote"]) < 0 or values["quote_volume"] != values["buy_quote"] + values["sell_quote"] or values["delta_quote"] != values["buy_quote"] - values["sell_quote"]:
            raise ValueError("inconsistent taker amounts")
        if not 0 <= self.coverage_ms <= MINUTE_MS or self.quality_status not in {"complete", "incomplete"}:
            raise ValueError("invalid bucket quality")
        if self.quality_status == "complete" and (self.coverage_ms != MINUTE_MS or self.quality_flags or len(self.connection_epochs) != 1):
            raise ValueError("complete bucket requires full coverage and one epoch")
        decimal_value(self.base_quantity, "base_quantity", positive=True)

    @property
    def complete(self) -> bool:
        return self.quality_status == "complete" and not self.quality_flags

    @property
    def missing_reasons(self) -> tuple[str, ...]:
        return self.quality_flags

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "coverage_ratio": self.coverage_ms / MINUTE_MS, "complete": self.complete}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MinuteBucket":
        row = dict(value)
        complete = row.pop("complete", None)
        coverage = row.pop("coverage_ratio", None)
        row["connection_epochs"] = tuple(row["connection_epochs"])
        row["quality_flags"] = tuple(row["quality_flags"])
        bucket = cls(**row)
        if complete is not None and (type(complete) is not bool or complete != bucket.complete):
            raise ValueError("inconsistent derived complete flag")
        if coverage is not None and (type(coverage) not in {int, float} or coverage != bucket.coverage_ms / MINUTE_MS):
            raise ValueError("inconsistent derived coverage ratio")
        return bucket


@dataclass(frozen=True)
class PendingBatch:
    batch_id: str
    buckets: tuple[MinuteBucket, ...]
    checkpoints: tuple[Mapping[str, Any], ...]
    health_rollups: tuple[HealthRollup, ...]

    @property
    def health(self) -> tuple[HealthRollup, ...]:
        return self.health_rollups

    def to_dict(self) -> dict[str, Any]:
        return {"batch_id": self.batch_id, "buckets": [b.to_dict() for b in self.buckets],
                "checkpoints": [dict(c) for c in self.checkpoints],
                "health_rollups": [r.to_dict() for r in self.health_rollups]}


class BoundedMinuteAggregator:
    """Only acknowledge() releases a prepared batch after the writer commits it.

    Source liveness must be supplied separately via note_connection(). A trade
    observed in a minute is never evidence that the whole minute was observed.
    No missing minute is synthesized as a zero-volume observation.
    """

    def __init__(
        self, *, grace_ms: int = 2000, max_instruments: int = 1024,
        max_event_ids: int = 200_000, max_open_buckets: int = 4096,
        max_pending_events: int = 200_000, max_future_skew_ms: int = 2000,
        max_coverage_intervals: int = 256, quality: QualityTracker | None = None,
    ) -> None:
        if min(max_instruments, max_event_ids, max_open_buckets, max_pending_events, max_coverage_intervals) < 1:
            raise ValueError("aggregation capacities must be positive")
        if min(grace_ms, max_future_skew_ms) < 0:
            raise ValueError("time tolerances must be nonnegative")
        self.grace_ms = grace_ms
        self.max_instruments = max_instruments
        self.max_event_ids = max_event_ids
        self.max_open_buckets = max_open_buckets
        self.max_pending_events = max_pending_events
        self.max_future_skew_ms = max_future_skew_ms
        self.max_coverage_intervals = max_coverage_intervals
        self.quality = quality or QualityTracker()
        self._instruments: set[SeriesKey] = set()
        self._buckets: dict[tuple[SeriesKey, int], dict[str, Any]] = {}
        self._event_ids: OrderedDict[tuple[Any, ...], bool] = OrderedDict()
        self._watermarks: dict[SeriesKey, int] = {}
        self._sequences: dict[SeriesKey, tuple[int, int | None]] = {}
        self._coverage: dict[SeriesKey, list[tuple[int, int, int, bool]]] = {}
        self._pending: PendingBatch | None = None
        self._batch_generation = 0
        self._pending_keys: tuple[tuple[SeriesKey, int], ...] = ()
        self._pending_events = 0
        self._now_ms = 0
        self._processing_ms = 0
        self._last_gauge_minute = -1

    @staticmethod
    def series(event: Any) -> SeriesKey:
        if isinstance(event, Mapping):
            return tuple(str(event[name]) for name in ("source", "exchange", "market", "instrument_id"))  # type: ignore[return-value]
        return event.source, event.exchange, event.market, event.instrument_id

    def _record(self, event: MarketEvent, name: str) -> None:
        self.quality.record(event, name, observed_ms=max(event.receive_time_ms, self._processing_ms, self._now_ms),
                            event_latency_ms=max(0, event.receive_time_ms - event.event_time_ms),
                            processing_latency_ms=max(0, self._processing_ms - event.receive_time_ms),
                            queue_depth=self._pending_events, connection_epoch=event.connection_epoch)

    def _observe_non_trade_quality(self, event: MarketEvent, *, future: bool = False) -> None:
        """Retain typed-data failures without constructing non-trade aggregates."""
        self._record(event, "non_trade_events")
        missing_reason = getattr(event.payload, "missing_reason", None)
        flags = list(event.quality_flags)
        if missing_reason is not None:
            self._record(event, "missing_payload_events")
        if flags:
            self._record(event, "flagged_quality_events")
        evidence = {"event_type": event.event_type, "missing_reason": missing_reason,
                    "quality_flags": flags}
        if future:
            evidence["future_event"] = True
        reason = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        omitted = 0
        while len(reason) > 2048 and flags:
            flags.pop()
            omitted += 1
            evidence["quality_flags_omitted"] = omitted
            reason = json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if omitted:
            self._record(event, "health_reason_truncated")
        self.quality.status(event, "incomplete" if future or missing_reason is not None or event.quality_flags else "complete",
                            reason, observed_ms=max(event.receive_time_ms, self._processing_ms, self._now_ms),
                            dimension=event.event_type)

    def _admit_series(self, series: SeriesKey) -> bool:
        if series not in self._instruments:
            if len(self._instruments) >= self.max_instruments:
                return False
            self._instruments.add(series)
        return True

    def ingest(self, event: MarketEvent, *, processing_time_ms: int | None = None) -> bool:
        if not isinstance(event, MarketEvent):
            raise TypeError("ingest requires a validated MarketEvent")
        validate_health_identity(event)
        self._processing_ms = max(self._now_ms, event.receive_time_ms) if processing_time_ms is None else timestamp_ms(processing_time_ms, "processing_time_ms")
        if self._processing_ms < event.receive_time_ms:
            raise ValueError("processing time precedes receive time")
        is_trade = event.event_type == "trade" and isinstance(event.payload, TradePayload)
        if event.event_time_ms > event.receive_time_ms + self.max_future_skew_ms:
            self._record(event, "future_events")
            if not is_trade:
                self._observe_non_trade_quality(event, future=True)
            return False
        if not is_trade:
            self._observe_non_trade_quality(event)
            return False
        key = self.series(event)
        if not self._admit_series(key):
            self._record(event, "instrument_limit")
            return False
        event_key = tuple(event.dedup_key)
        start = event.event_time_ms // MINUTE_MS * MINUTE_MS
        bucket_key = (key, start)
        if event_key in self._event_ids:
            if bucket_key in self._buckets and bucket_key not in self._pending_keys:
                self._buckets[bucket_key]["duplicates"] += 1
            self._record(event, "duplicate_events")
            return False
        if start < self._watermarks.get(key, 0) or bucket_key in self._pending_keys or start + MINUTE_MS <= self._now_ms - self.grace_ms:
            self._record(event, "late_events")
            return False
        if self._pending_events >= self.max_pending_events or (
            bucket_key not in self._buckets and len(self._buckets) >= self.max_open_buckets
        ):
            if bucket_key in self._buckets:
                self._buckets[bucket_key]["flags"].update(("local_data_loss", "queue_overflow"))
            self._record(event, "queue_overflow")
            return False
        while len(self._event_ids) >= self.max_event_ids:
            old_key, active = next(iter(self._event_ids.items()))
            if active:
                # Never evict an ID still participating in an uncommitted bucket.
                if bucket_key in self._buckets:
                    self._buckets[bucket_key]["flags"].update(("local_data_loss", "dedup_capacity"))
                self._record(event, "dedup_capacity")
                return False
            self._event_ids.pop(old_key)
        payload = event.payload
        price = Decimal(payload.price)
        amount = payload.quote_notional
        if not price.is_finite() or not amount.is_finite() or price <= 0 or amount <= 0:
            self._record(event, "invalid_amount")
            return False
        row = self._buckets.setdefault(bucket_key, {
            "symbol": event.symbol, "quote_currency": payload.quote_currency,
            "open_key": None, "close_key": None, "open": price, "high": price,
            "low": price, "close": price, "buy": Decimal(0), "sell": Decimal(0),
            "events": 0, "ids": [], "epochs": set(), "spans": [], "flags": set(),
            "base_quantity": Decimal(0), "duplicates": 0, "late": 0,
        })
        if row["quote_currency"] != payload.quote_currency:
            row["flags"].add("mixed_quote_currency")
            self._record(event, "invalid_amount")
            return False
        if not isfinite(float(row["buy"] + row["sell"] + amount)) or not isfinite(float(row["base_quantity"] + payload.base_quantity)):
            row["flags"].add("amount_overflow")
            self._record(event, "amount_overflow")
            return False
        sort_key = (event.event_time_ms, event.sequence_start if event.sequence_start is not None else -1,
                    event.source_event_id)
        if row["open_key"] is None or sort_key < row["open_key"]:
            row["open_key"], row["open"] = sort_key, price
        if row["close_key"] is None or sort_key > row["close_key"]:
            row["close_key"], row["close"] = sort_key, price
        row["high"], row["low"] = max(row["high"], price), min(row["low"], price)
        row["sell" if payload.buyer_is_maker else "buy"] += amount
        row["base_quantity"] += payload.base_quantity
        row["events"] += 1
        if event.receive_time_ms >= start + MINUTE_MS:
            row["late"] += 1
        row["ids"].append(event_key)
        row["epochs"].add(event.connection_epoch)
        row["flags"].update(event.quality_flags)
        if event.sequence_start is None or event.sequence_end is None:
            row["flags"].add("sequence_unavailable")
        else:
            row["spans"].append((event.connection_epoch, event.sequence_start, event.sequence_end))
        self._event_ids[event_key] = True
        self._pending_events += 1
        self._record(event, "accepted_events")
        return True

    def note_connection(
        self, *, source: str, exchange: str, market: str, instrument_id: str,
        connection_epoch: int, start_ms: int, end_ms: int, complete: bool = True,
    ) -> bool:
        timestamp_ms(start_ms, "coverage start_ms")
        timestamp_ms(end_ms, "coverage end_ms")
        strict_int(connection_epoch, "connection_epoch")
        validate_health_identity({"source": source, "exchange": exchange, "market": market,
                                  "instrument_id": instrument_id})
        if start_ms >= end_ms or not isinstance(complete, bool):
            raise ValueError("coverage must be a nonempty explicit interval")
        key = (source, exchange, market, instrument_id)
        if not self._admit_series(key):
            return False
        rows = self._coverage.setdefault(key, [])
        value = (start_ms, end_ms, connection_epoch, complete)
        # Merge touching intervals only when their connection epoch and evidence agree.
        merged: list[tuple[int, int, int, bool]] = []
        for interval in sorted([*rows, value]):
            if merged and merged[-1][2:] == interval[2:] and interval[0] <= merged[-1][1]:
                old = merged[-1]
                merged[-1] = old[0], max(old[1], interval[1]), old[2], old[3]
            else:
                merged.append(interval)
        if len(merged) > self.max_coverage_intervals:
            self.quality.record(dict(zip(("source", "exchange", "market", "instrument_id"), key)),
                                "coverage_overflow", observed_ms=end_ms)
            return False
        self._coverage[key] = merged
        self.quality.record({"source": source, "exchange": exchange, "market": market, "instrument_id": "*"},
                            "connection_observations", observed_ms=end_ms - 1,
                            connection_epoch=connection_epoch)
        return True

    def _coverage_ms(self, key: SeriesKey, start: int, epoch: int) -> tuple[int, bool]:
        end = start + MINUTE_MS
        intervals = self._coverage.get(key, ())
        pieces = sorted((max(a, start), min(b, end)) for a, b, e, good in intervals
                        if good and e == epoch and a < end and b > start)
        covered, cursor = 0, start
        for a, b in pieces:
            if b > cursor:
                covered += b - max(a, cursor)
                cursor = b
        bad = any(a < end and b > start and (not good or e != epoch) for a, b, e, good in intervals)
        return covered, bad

    def _freeze(self, key: SeriesKey, start: int, row: dict[str, Any],
                sequences: dict[SeriesKey, tuple[int, int | None]]) -> MinuteBucket:
        epochs = tuple(sorted(row["epochs"]))
        epoch = epochs[0] if len(epochs) == 1 else -1
        flags = set(row["flags"])
        if len(epochs) != 1:
            flags.add("connection_epoch_changed")
        coverage, bad_coverage = self._coverage_ms(key, start, epoch)
        if coverage != MINUTE_MS or bad_coverage:
            flags.add("coverage_incomplete")
        previous_epoch, last = sequences.get(key, (epoch, None))
        if previous_epoch != epoch:
            flags.add("connection_epoch_changed")
            last = None
        spans = sorted(row["spans"])
        gap_count = 0
        for span_epoch, first, end in spans:
            if span_epoch != epoch:
                continue
            if last is not None and first > last + 1:
                flags.add("sequence_gap")
                gap_count += first - last - 1
            if last is not None and first <= last:
                flags.add("sequence_overlap")
            last = max(last if last is not None else end, end)
        sequences[key] = epoch, last
        digest = hashlib.sha256(json.dumps(sorted(row["ids"]), separators=(",", ":")).encode()).hexdigest()
        values = [span[1] for span in spans]
        ends = [span[2] for span in spans]
        return MinuteBucket(
            *key, row["symbol"], start, start + MINUTE_MS, epoch, epochs,
            *(str(row[name]) for name in ("open", "high", "low", "close", "buy", "sell")),
            str(row["buy"] + row["sell"]), str(row["buy"] - row["sell"]), row["events"],
            row["open_key"][0], row["close_key"][0], min(values) if values else None,
            max(ends) if ends else None, row["open_key"][-1], row["close_key"][-1], digest,
            coverage, "incomplete" if flags else "complete", tuple(sorted(flags)), row["quote_currency"],
            str(row["base_quantity"]), row["duplicates"], row["late"], gap_count,
        )

    def prepare(self, now_ms: int) -> PendingBatch | None:
        timestamp_ms(now_ms, "now_ms")
        if isinstance(now_ms, bool) or not isinstance(now_ms, int) or now_ms < self._now_ms:
            raise ValueError("virtual time must be a monotonic integer millisecond timestamp")
        self._now_ms = now_ms
        if self._pending is not None:
            return self._pending
        if self._instruments and now_ms // MINUTE_MS != self._last_gauge_minute:
            self._last_gauge_minute = now_ms // MINUTE_MS
            self.quality.record({"source": "hunter_runtime", "exchange": "internal", "market": "diagnostic", "instrument_id": "*"},
                                "health_observations", observed_ms=now_ms,
                                queue_depth=self._pending_events,
                                checkpoint_lag_ms=self.stats()["checkpoint_lag_ms"])
        cutoff = now_ms - self.grace_ms
        keys = tuple(sorted((key for key in self._buckets if key[1] + MINUTE_MS <= cutoff),
                            key=lambda key: (key[1], key[0])))
        sequences = dict(self._sequences)
        buckets = tuple(self._freeze(key, start, self._buckets[(key, start)], sequences) for key, start in keys)
        checkpoints: dict[SeriesKey, Mapping[str, Any]] = {}
        for bucket in buckets:
            key = self.series(bucket)
            checkpoints[key] = MappingProxyType({
                "source": bucket.source, "exchange": bucket.exchange, "market": bucket.market,
                "instrument_id": bucket.instrument_id, "committed_through_ms": bucket.end_ms,
                "connection_epoch": bucket.connection_epoch,
                "sequence_start": bucket.sequence_start, "sequence_end": sequences[key][1],
                "last_sequence": sequences[key][1], "last_source_event_id": bucket.last_source_event_id,
            })
            if "sequence_gap" in bucket.quality_flags:
                self.quality.record(bucket, "sequence_gaps", observed_ms=bucket.end_ms - 1)
            self.quality.status(bucket, bucket.quality_status, ",".join(bucket.quality_flags), observed_ms=bucket.end_ms - 1)
        health = self.quality.prepare(cutoff)
        if not buckets and not health:
            # Even a generation whose routine instrument rows were filtered
            # must be released. It has no durable output requiring a DB commit.
            self.quality.acknowledge(health)
            return None
        points = tuple(checkpoints[key] for key in sorted(checkpoints))
        # Distinct health delta generations can have identical values in one
        # minute. Their IDs must differ so the writer does not mistake a new
        # observation for a retry. This local counter is deterministic within
        # fresh replay runs; it is not an incremental crash-resume protocol.
        self._batch_generation += 1
        content = {"generation": self._batch_generation,
                   "buckets": [b.to_dict() for b in buckets], "checkpoints": [dict(p) for p in points],
                   "health_rollups": [r.to_dict() for r in health]}
        batch_id = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        self._pending = PendingBatch(batch_id, buckets, points, health)
        self._pending_keys = keys
        return self._pending

    def acknowledge(self, batch_id: str) -> None:
        if self._pending is None or batch_id != self._pending.batch_id:
            raise ValueError("acknowledgement does not match the pending batch")
        for key in self._pending_keys:
            row = self._buckets.pop(key)
            self._pending_events -= row["events"]
            for event_key in row["ids"]:
                self._event_ids[event_key] = False
        for point in self._pending.checkpoints:
            key = self.series(point)
            end = int(point["committed_through_ms"])
            self._watermarks[key] = max(end, self._watermarks.get(key, 0))
            self._sequences[key] = int(point["connection_epoch"]), point["sequence_end"]
            self._coverage[key] = [(max(a, end), b, epoch, good) for a, b, epoch, good
                                   in self._coverage.get(key, ()) if b > end]
        self.quality.acknowledge(self._pending.health_rollups)
        self._pending = None
        self._pending_keys = ()

    ack_committed = acknowledge

    def restore_checkpoints(self, entries: Iterable[Mapping[str, Any]] | Mapping[Any, Mapping[str, Any]]) -> None:
        if self._buckets or self._pending is not None:
            raise ValueError("restore checkpoints before ingesting events")
        values = entries.values() if isinstance(entries, Mapping) else entries
        for row in values:
            key = self.series(row)
            if not self._admit_series(key):
                raise ValueError("checkpoint instrument capacity exceeded")
            end = timestamp_ms(row["committed_through_ms"], "committed_through_ms")
            if end % MINUTE_MS:
                raise ValueError("checkpoint is not a closed minute boundary")
            self._watermarks[key] = max(end, self._watermarks.get(key, 0))
            epoch = strict_int(row["connection_epoch"], "connection_epoch", minimum=-1)
            sequence = row.get("sequence_end", row.get("last_sequence"))
            if sequence is not None:
                strict_int(sequence, "sequence_end")
            self._sequences[key] = epoch, sequence

    def record_writer_failure(self, *, now_ms: int) -> None:
        self.quality.record({"source": "hunter_writer", "exchange": "internal", "market": "diagnostic", "instrument_id": "*"}, "writer_failures", observed_ms=now_ms,
                            queue_depth=self._pending_events, checkpoint_lag_ms=self.stats()["checkpoint_lag_ms"])

    def stats(self) -> dict[str, Any]:
        oldest: dict[SeriesKey, int] = {}
        for key, start in self._buckets:
            oldest[key] = min(start, oldest.get(key, start))
        lag = max((max(0, self._now_ms - self._watermarks.get(key, oldest.get(key, self._now_ms)))
                   for key in self._instruments), default=0)
        return {"instrument_count": len(self._instruments), "open_buckets": len(self._buckets),
                "retained_event_ids": len(self._event_ids), "pending_events": self._pending_events,
                "queue_depth": self._pending_events, "checkpoint_lag_ms": lag,
                "pending_batch_id": self._pending.batch_id if self._pending else None,
                **self.quality.stats()}


MinuteAggregator = BoundedMinuteAggregator
