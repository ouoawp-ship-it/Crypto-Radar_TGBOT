"""Reproducible OFFLINE benchmark; every database lives in TemporaryDirectory.

Measurements are intentionally outside deterministic replay output. Tracemalloc
reports Python allocations, not process RSS or SQLite's native cache. SQLite
size projections include schema, metadata, indexes and minute health rollups.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import json
from pathlib import Path
import platform
import sqlite3
import tempfile
import time
import tracemalloc

from radars.altcoin_hunter.configuration import AltcoinHunterConfig
from radars.altcoin_hunter.storage import HunterWriter, migrate


class MeasuredWriter(HunterWriter):
    def __init__(self, path, *, inject_failure=False):
        super().__init__(path, busy_timeout_ms=10)
        self.inject_failure = inject_failure
        self.fail_before_commit = False
        self.failure_verified = False
        self.lock_verified = False
        self.wal_peak_bytes = 0
        self.committed_batches = 0

    def _commit_transaction(self, connection):
        if self.fail_before_commit:
            self.fail_before_commit = False
            raise sqlite3.OperationalError("offline_injected_commit_failure")
        super()._commit_transaction(connection)
        # Include directory and final baseline transactions, not only buckets.
        wal = Path(str(self.db_path) + "-wal")
        self.wal_peak_bytes = max(self.wal_peak_bytes, wal.stat().st_size if wal.exists() else 0)

    def commit_batch(self, pending, *, baseline_states=()):
        baseline_states = tuple(baseline_states)
        if self.inject_failure and not self.failure_verified:
            before = self.read_counts(), self.checkpoints()
            self.fail_before_commit = True
            try:
                super().commit_batch(pending, baseline_states=baseline_states)
            except sqlite3.OperationalError:
                assert (self.read_counts(), self.checkpoints()) == before
                self.failure_verified = True
            else:
                raise AssertionError("commit failure injection did not execute")
            # A genuine second SQLite connection holds the write transaction.
            with closing(sqlite3.connect(self.db_path, isolation_level=None)) as blocker:
                blocker.execute("BEGIN IMMEDIATE")
                try:
                    try:
                        super().commit_batch(pending, baseline_states=baseline_states)
                    except sqlite3.OperationalError as exc:
                        assert "locked" in str(exc).lower(), str(exc)
                        self.lock_verified = True
                    else:
                        raise AssertionError("SQLite locked injection did not execute")
                finally:
                    blocker.rollback()
            assert (self.read_counts(), self.checkpoints()) == before
        result = super().commit_batch(pending, baseline_states=baseline_states)
        self.committed_batches += 1
        wal = Path(str(self.db_path) + "-wal")
        self.wal_peak_bytes = max(self.wal_peak_bytes, wal.stat().st_size if wal.exists() else 0)
        return result


def run_capacity(*, instruments: int, minutes: int, pattern: str = "normal", seed: int = 417,
                 trades_per_minute: int = 2, inject_failure: bool = False,
                 baseline_windows: tuple[int, ...] = (), trace_memory: bool = True) -> dict:
    from radars.altcoin_hunter.replay import ReplayRunner, iter_synthetic_records

    if trace_memory:
        tracemalloc.start()
    try:
        with tempfile.TemporaryDirectory(prefix="hunter-capacity-") as tmp:
            path = Path(tmp) / "altcoin_hunter.db"
            migrate(path)
            count = 0

            def stream():
                nonlocal count
                for record in iter_synthetic_records(instruments=instruments, minutes=minutes, seed=seed,
                                                     pattern=pattern, trades_per_minute=trades_per_minute):
                    if record.get("kind") == "event":
                        count += 1
                    yield record

            started = time.perf_counter()
            with MeasuredWriter(path, inject_failure=inject_failure) as writer:
                runner = ReplayRunner(writer, config=AltcoinHunterConfig(enable=True),
                                      max_instruments=max(1024, instruments), baseline_windows=baseline_windows)
                replay = runner.consume(stream())
                rows = writer.read_counts()
                wal_peak = writer.wal_peak_bytes
                faults = {"commit_failure_verified": writer.failure_verified, "sqlite_locked_verified": writer.lock_verified}
                batches = writer.committed_batches
            elapsed = time.perf_counter() - started
            peak = tracemalloc.get_traced_memory()[1] if trace_memory else None
            size = path.stat().st_size
            buckets = rows["market_buckets_1m"]
            return {
                "benchmark_version": 1, "python": platform.python_version(), "platform": platform.system(),
                "instruments": instruments, "minutes": minutes, "pattern": pattern, "seed": seed,
                "trades_per_minute": trades_per_minute, "baseline_windows": baseline_windows,
                "events": count, "elapsed_seconds": round(elapsed, 6),
                "events_per_second": round(count / elapsed, 3),
                "python_peak_bytes": peak, "memory_method": "tracemalloc" if trace_memory else "not_measured",
                "bucket_count": buckets, "database_rows": rows, "database_bytes": size,
                "wal_peak_bytes": wal_peak, "wal_sampling": "after_each_writer_transaction_commit",
                "bytes_per_100k_buckets_linear": round(size / buckets * 100_000) if buckets else None,
                "bytes_per_1m_buckets_linear": round(size / buckets * 1_000_000) if buckets else None,
                "bytes_for_2592000_buckets_linear": round(size / buckets * 2_592_000) if buckets else None,
                "committed_batches": batches, "faults": faults, "replay": replay,
                "projection_limit": "linear including fixed overhead; not a production retention approval",
            }
    finally:
        if trace_memory:
            tracemalloc.stop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instruments", type=int, required=True)
    parser.add_argument("--minutes", type=int, required=True)
    parser.add_argument("--pattern", default="normal")
    parser.add_argument("--trades-per-minute", type=int, default=2)
    parser.add_argument("--inject-failure", action="store_true")
    parser.add_argument("--six-baselines", action="store_true")
    parser.add_argument("--no-tracemalloc", action="store_true")
    args = parser.parse_args()
    result = run_capacity(instruments=args.instruments, minutes=args.minutes, pattern=args.pattern,
                          trades_per_minute=args.trades_per_minute, inject_failure=args.inject_failure,
                          baseline_windows=(1, 3, 5, 15, 30, 60) if args.six_baselines else (),
                          trace_memory=not args.no_tracemalloc)
    print(json.dumps(result, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
