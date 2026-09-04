"""Deterministic, bounded offline replay. No transport or real-time clock.

Bucket/checkpoint/health commits precede acknowledgement and window ingestion.
Baseline snapshots are a separate, rebuildable transaction; they are never
claimed to be atomic with their input buckets. A replay requires an explicitly
migrated, otherwise empty database and never resumes or overwrites a prior run.
"""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import random
import sqlite3
from typing import Any, Iterable, Iterator, Mapping

from .aggregation import BoundedMinuteAggregator, MINUTE_MS, MinuteBucket
from .baselines import BaselineEngine, BaselineKey, BaselinePolicy
from .configuration import AltcoinHunterConfig
from .models import MIN_TIMESTAMP_MS, event_from_dict, strict_int, timestamp_ms
from .storage import HunterWriter, StorageError
from .universe import UniverseRegistry, instrument_from_dict
from .windows import RollingWindowEngine, WINDOW_MINUTES


DEFAULT_START_MS = 1_704_067_200_000
PATTERNS = ("normal", "duplicates", "burst", "out_of_order", "late", "gap", "epoch")
FEATURE_FLOORS = {"price_return_ratio": 0.000001, "quote_volume": 1.0, "delta_ratio": 0.000001}


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      allow_nan=False).encode("utf-8")


class VirtualClock:
    """Explicit UTC milliseconds with nondecreasing processing time."""

    def __init__(self, now_ms: int = MIN_TIMESTAMP_MS) -> None:
        self._now_ms = timestamp_ms(now_ms, "now_ms")
        self._origin_ms = now_ms

    @property
    def now_ms(self) -> int:
        return self._now_ms

    @property
    def monotonic_ns(self) -> int:
        return (self._now_ms - self._origin_ms) * 1_000_000

    def advance(self, to_ms: int) -> int:
        timestamp_ms(to_ms, "to_ms")
        if to_ms < self._now_ms:
            raise ValueError("virtual_clock_cannot_move_backwards")
        self._now_ms = to_ms
        return self._now_ms


def offline_policy(window_minutes: int, feature: str, *, min_sample_count: int = 3) -> BaselinePolicy:
    """Uncalibrated fixture policy: one observation per disjoint window stride."""
    if window_minutes not in WINDOW_MINUTES or feature not in FEATURE_FLOORS:
        raise ValueError("unsupported_baseline_policy")
    strict_int(min_sample_count, "min_sample_count", minimum=1, maximum=128)
    return BaselinePolicy(max_samples=128, min_sample_count=min_sample_count,
                          min_span_ms=(min_sample_count - 1) * window_minutes * MINUTE_MS,
                          min_coverage_ratio=0.95, sampling_stride=window_minutes,
                          sample_interval_ms=MINUTE_MS, metric_floor=FEATURE_FLOORS[feature],
                          ewma_alpha=0.1, clip_z=6.0)


class ReplayRunner:
    """One offline run, retaining bounded engines, digests, counts and latest only."""

    def __init__(self, writer: HunterWriter, *, config: AltcoinHunterConfig | None = None,
                 clock: VirtualClock | None = None, max_instruments: int = 1024,
                 baseline_windows: tuple[int, ...] = WINDOW_MINUTES,
                 min_sample_count: int = 3) -> None:
        self.config = config or AltcoinHunterConfig(enable=True)
        if not self.config.enable:
            raise ValueError("offline_replay_requires_enable")
        strict_int(max_instruments, "max_instruments", minimum=1, maximum=4096)
        if not isinstance(baseline_windows, tuple) or any(type(w) is not int or w not in WINDOW_MINUTES for w in baseline_windows):
            raise ValueError("invalid_baseline_windows")
        if len(set(baseline_windows)) != len(baseline_windows):
            raise ValueError("duplicate_baseline_window")
        strict_int(min_sample_count, "min_sample_count", minimum=1, maximum=128)
        self.writer = writer
        self.clock = clock or VirtualClock()
        self.baseline_windows = tuple(sorted(baseline_windows))
        self.policies = {(w, f): offline_policy(w, f, min_sample_count=min_sample_count)
                         for w in self.baseline_windows for f in FEATURE_FLOORS}
        self.aggregator = BoundedMinuteAggregator(
            grace_ms=self.config.allowed_lateness_ms, max_instruments=max_instruments,
            max_open_buckets=max_instruments * 4)
        self.windows = RollingWindowEngine(max_instruments=max_instruments)
        self.baselines = BaselineEngine(max_series=max(1, max_instruments * len(self.policies)))
        self.universe = UniverseRegistry(max_instruments=max_instruments)
        algorithm_config = asdict(self.config)
        algorithm_config.pop("db_file")  # Physical output location is not an algorithm input.
        algorithm_config.update({"baseline_policies": {f"{w}:{f}": asdict(p) for (w, f), p in self.policies.items()},
                                 "max_instruments": max_instruments, "replay_version": 1})
        self.algorithm_hash = hashlib.sha256(_canonical(algorithm_config)).hexdigest()
        self.counts = {name: 0 for name in ("records", "events", "accepted_events", "rejected_events",
                     "directory_refreshes", "directory_rejections", "committed_batches", "committed_buckets",
                     "complete_buckets", "incomplete_buckets", "baseline_evaluations", "ready_baselines")}
        self._bucket_digest = hashlib.sha256()
        self._baseline_digest = hashlib.sha256()
        self._latest_baseline: dict[str, Any] | None = None
        self._latest_window: dict[str, Any] | None = None
        self._maxima = {"queue_depth": 0, "checkpoint_lag_ms": 0, "open_buckets": 0,
                        "retained_event_ids": 0, "pending_events": 0, "minute_buckets": 0}
        self._started = False
        self._last_prepared_cutoff: int | None = None
        self._last_observation_ms: int | None = None
        self._commit_pending = False

    def _observe_bounds(self, *, force: bool = False) -> None:
        if not force and self._last_observation_ms == self.clock.now_ms:
            return
        self._last_observation_ms = self.clock.now_ms
        stats = {**self.aggregator.stats(), **self.windows.stats()}
        for name in self._maxima:
            self._maxima[name] = max(self._maxima[name], stats.get(name, 0))

    def _flush(self, *, force: bool = False) -> None:
        cutoff = (self.clock.now_ms - self.config.allowed_lateness_ms) // MINUTE_MS
        if not force and not self._commit_pending and cutoff == self._last_prepared_cutoff:
            return
        self._last_prepared_cutoff = cutoff
        pending = self.aggregator.prepare(self.clock.now_ms)
        if pending is None:
            return
        self._observe_bounds(force=True)
        self._commit_pending = True
        try:
            committed = self.writer.commit_batch(pending)
        except Exception:
            self.aggregator.record_writer_failure(now_ms=self.clock.now_ms)
            raise
        # The returned receipt is proof that all bucket/checkpoint/health rows
        # were committed. Never acknowledge a failed or merely prepared batch.
        self.aggregator.acknowledge(committed.batch_id)
        self._commit_pending = False
        self.windows.ingest_committed(committed)
        self.counts["committed_batches"] += 1
        for raw_bucket in committed.buckets:
            bucket = raw_bucket if isinstance(raw_bucket, MinuteBucket) else MinuteBucket.from_dict(raw_bucket)
            self.counts["committed_buckets"] += 1
            self.counts["complete_buckets" if bucket.complete else "incomplete_buckets"] += 1
            self._bucket_digest.update(_canonical(bucket.to_dict()) + b"\n")
            for minutes in self.baseline_windows:
                window = self.windows.query(source=bucket.source, exchange=bucket.exchange,
                    market=bucket.market, instrument_id=bucket.instrument_id,
                    end_ms=bucket.end_ms, window_minutes=minutes)
                self._latest_window = window
                for feature in FEATURE_FLOORS:
                    key = BaselineKey(bucket.source, bucket.exchange, bucket.market,
                                      bucket.instrument_id, feature, minutes * 60)
                    self.baselines.register(key, self.policies[(minutes, feature)])
                    result = self.baselines.evaluate_and_observe(key, bucket.end_ms, window[feature])
                    record = {"key": asdict(key), "end_ms": bucket.end_ms, "result": result.to_dict()}
                    self._latest_baseline = record
                    self._baseline_digest.update(_canonical(record) + b"\n")
                    self.counts["baseline_evaluations"] += 1
                    self.counts["ready_baselines"] += int(result.ready)
        self._observe_bounds(force=True)

    def consume(self, records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        if self._started:
            raise ValueError("replay_runner_is_single_use")
        if any(value for name, value in self.writer.read_counts().items() if name != "schema_migrations"):
            raise StorageError("replay_requires_empty_migrated_database")
        self._started = True
        for record in records:
            if not isinstance(record, Mapping):
                raise ValueError("replay_record_must_be_mapping")
            self.counts["records"] += 1
            kind = record.get("kind")
            if kind == "event":
                event = event_from_dict(record["event"])
                self.clock.advance(record.get("processing_time_ms", event.receive_time_ms))
                self._flush()
                accepted = self.aggregator.ingest(event, processing_time_ms=self.clock.now_ms)
                self.counts["events"] += 1
                self.counts["accepted_events" if accepted else "rejected_events"] += 1
            elif kind == "coverage":
                self.clock.advance(record["observed_at_ms"])
                if record["coverage"]["end_ms"] > self.clock.now_ms:
                    raise ValueError("coverage_cannot_claim_future_continuity")
                self.aggregator.note_connection(**record["coverage"])
                # Coverage is a set of metadata inputs for this instant. Wait
                # for advance/event before closing, including zero-grace runs,
                # so the first symbol cannot close all other symbols too early.
            elif kind == "advance":
                self.clock.advance(record["to_ms"])
                self._flush()
            elif kind == "directory":
                self.clock.advance(record["observed_at_ms"])
                complete, healthy = record.get("complete", True), record.get("source_healthy", True)
                if type(complete) is not bool or type(healthy) is not bool:
                    raise ValueError("directory_flags_must_be_bool")
                result = self.universe.refresh((instrument_from_dict(row) for row in record["instruments"]),
                    observed_at_ms=self.clock.now_ms, complete=complete, source_healthy=healthy)
                self.counts["directory_refreshes"] += 1
                if result.accepted:
                    self.writer.upsert_instruments((row.to_dict() for row in result.instruments),
                                                   observed_at_ms=self.clock.now_ms)
                else:
                    self.counts["directory_rejections"] += 1
            else:
                raise ValueError("unknown_replay_record_kind")
        # No implicit advancement. The fixture must explicitly close its final
        # minute; otherwise the pending data is honestly reported as uncommitted.
        self._flush(force=True)
        self._observe_bounds(force=True)
        baseline_persisted = True
        try:
            if self.baseline_windows:
                self.writer.save_baselines(self.baselines.export())
        except (StorageError, sqlite3.Error):
            baseline_persisted = False
        final_stats = self.aggregator.stats()
        uncommitted_input = bool(final_stats.get("open_buckets", 0))
        status = "degraded" if not baseline_persisted else "incomplete" if uncommitted_input else "ok"
        reason = "baseline_snapshot_failed_replay_to_new_database" if not baseline_persisted else "fixture_did_not_close_final_bucket" if uncommitted_input else None
        return {"replay_version": 1, "status": status, "reason": reason,
                "mode": "offline_dry_run", "real_send": False, "network_calls": 0,
                "algorithm_config_hash": self.algorithm_hash,
                "baseline_policy_status": "uncalibrated_offline_fixture_policy",
                "baseline_policies": {f"{w}:{f}": {**asdict(p), "config_hash": p.config_hash}
                                      for (w, f), p in self.policies.items()},
                "baseline_state_persisted": baseline_persisted,
                "baseline_rebuild_required": not baseline_persisted,
                "baseline_persistence": "separate_rebuildable_transaction_after_bucket_commit",
                "baseline_windows": list(self.baseline_windows), "virtual_time_ms": self.clock.now_ms,
                "counts": dict(self.counts), "storage_counts": self.writer.read_counts(),
                "bucket_digest": self._bucket_digest.hexdigest(),
                "baseline_digest": self._baseline_digest.hexdigest(),
                "latest_baseline": self._latest_baseline, "latest_window": self._latest_window,
                "maxima": dict(self._maxima), "aggregator_stats": final_stats,
                "window_stats": self.windows.stats(),
                "uncommitted_input": uncommitted_input,
                "universe_recent_history_count": len(self.universe.history),
                "universe_history_truncated": self.universe.history_truncated}


def run_replay(db_path: str | Path, records: Iterable[Mapping[str, Any]], **kwargs: Any) -> dict[str, Any]:
    """Open an existing schema only. Migration is always a separate action."""
    with HunterWriter(db_path) as writer:
        return ReplayRunner(writer, **kwargs).consume(records)


def iter_synthetic_records(instruments: int, minutes: int, seed: int, *, pattern: str = "normal",
                           start_ms: int = DEFAULT_START_MS, trades_per_minute: int = 2) -> Iterator[dict[str, Any]]:
    """Generate a repeatable recipe with O(instruments) generator memory.

    Prices/quantities are invented fixtures, not market calibration. A fixed
    2 second grace is explicit in this fixture's advances. Gap skips a sequence;
    epoch splits one minute across reconnect epochs; neither invents continuity.
    """
    strict_int(instruments, "instruments", minimum=1, maximum=4096)
    strict_int(minutes, "minutes", minimum=1, maximum=100_000)
    strict_int(seed, "seed", maximum=2**63 - 1)
    strict_int(trades_per_minute, "trades_per_minute", minimum=2, maximum=100)
    timestamp_ms(start_ms, "start_ms")
    timestamp_ms(start_ms + minutes * MINUTE_MS + 3000, "end_ms")
    if start_ms % MINUTE_MS or pattern not in PATTERNS:
        raise ValueError("invalid_recipe_start_or_pattern")
    rng = random.Random(seed)
    directory = [{"exchange": "fixture", "market": "usdt_perpetual", "instrument_id": f"asset-{i}",
                  "symbol": f"COIN{i}USDT", "exchange_symbol": f"COIN{i}USDT", "source": "synthetic",
                  "effective_at_ms": start_ms, "eligibility_status": "ELIGIBLE", "listing_stage": "MATURE",
                  "data_quality": "complete"} for i in range(instruments)]
    yield {"kind": "directory", "observed_at_ms": start_ms, "instruments": directory}
    del directory
    sequences = [0] * instruments
    midpoint = minutes // 2
    for minute in range(minutes):
        begin = start_ms + minute * MINUTE_MS
        count = trades_per_minute * (10 if pattern == "burst" and minute == midpoint else 1)
        initial_sequences = sequences.copy()
        for ordinal in range(count):
            # 5..54 seconds ensures previous minute's explicit grace has elapsed.
            receive_ms = begin + 5000 + ordinal * 49000 // max(1, count - 1)
            if pattern == "out_of_order":
                receive_ms = begin + 54000 + ordinal
            logical_ordinal = count - ordinal - 1 if pattern == "out_of_order" else ordinal
            event_ms = begin + 4000 + logical_ordinal * 49000 // max(1, count - 1)
            for i in range(instruments):
                sequence = initial_sequences[i] + logical_ordinal + 1
                sequences[i] = initial_sequences[i] + count
                price = f"{100 + i % 100}.{rng.randrange(10000):04d}"
                quantity = f"{1 + rng.randrange(9)}.{rng.randrange(1000):03d}"
                if pattern == "gap" and minute == midpoint and ordinal == 0 and i % 7 == 0:
                    continue
                epoch = int(pattern == "epoch" and (minute > midpoint or (minute == midpoint and ordinal >= count // 2)))
                event = {"schema_version": 1, "exchange": "fixture", "market": "usdt_perpetual",
                         "instrument_id": f"asset-{i}", "canonical_asset_id": None,
                         "symbol": f"COIN{i}USDT", "exchange_symbol": f"COIN{i}USDT", "event_type": "trade",
                         "event_time_ms": event_ms, "receive_time_ms": receive_ms,
                         "receive_monotonic_ns": (receive_ms - start_ms) * 1_000_000,
                         "source": "synthetic", "source_event_id": f"{i}:{sequence}",
                         "sequence_start": sequence, "sequence_end": sequence, "connection_epoch": epoch,
                         "quality_flags": [], "payload": {"price": price, "quantity": quantity,
                         "buyer_is_maker": bool(sequence % 2), "quantity_unit": "base",
                         "contract_multiplier": "1", "quote_currency": "USDT"}}
                yield {"kind": "event", "event": event}
                if pattern == "duplicates" and i % 7 == 0:
                    yield {"kind": "event", "event": event}
        for i in range(instruments):
            intervals = ((0, begin, begin + 30000), (1, begin + 30000, begin + MINUTE_MS)) if pattern == "epoch" and minute == midpoint else ((int(pattern == "epoch" and minute > midpoint), begin, begin + MINUTE_MS),)
            for epoch, left, right in intervals:
                yield {"kind": "coverage", "observed_at_ms": begin + MINUTE_MS,
                       "coverage": {"source": "synthetic", "exchange": "fixture", "market": "usdt_perpetual",
                                    "instrument_id": f"asset-{i}", "connection_epoch": epoch,
                                    "start_ms": left, "end_ms": right, "complete": True}}
        yield {"kind": "advance", "to_ms": begin + MINUTE_MS + 2000}
        if pattern == "late" and minute == midpoint:
            late = dict(event)
            late.update({"source_event_id": "deliberately-late", "receive_time_ms": begin + MINUTE_MS + 3000,
                         "receive_monotonic_ns": (begin + MINUTE_MS + 3000 - start_ms) * 1_000_000})
            yield {"kind": "event", "event": late}


def load_fixture(path: str | Path) -> Iterable[Mapping[str, Any]]:
    """Read a bounded local JSON record list or stream its deterministic recipe."""
    fixture = Path(path)
    if fixture.stat().st_size > 10 * 1024 * 1024:
        raise ValueError("fixture_size_limit_exceeded")
    with fixture.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict) or type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise ValueError("unsupported_fixture_schema")
    if set(data) == {"schema_version", "recipe"} and isinstance(data["recipe"], dict):
        return iter_synthetic_records(**data["recipe"])
    if set(data) == {"schema_version", "records"} and isinstance(data["records"], list):
        return iter(data["records"])
    raise ValueError("fixture_requires_records_or_recipe")
