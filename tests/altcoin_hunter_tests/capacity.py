"""Reproducible OFFLINE benchmark; every database lives in TemporaryDirectory.

Measurements are intentionally outside deterministic replay output. Tracemalloc
reports Python allocations, not process RSS or SQLite's native cache. SQLite
size projections include schema, metadata, indexes and minute health rollups.
"""
from __future__ import annotations

import argparse
from collections import Counter
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


def _btree_page_ownership(path: Path, objects: list[tuple]) -> dict[str, int]:
    """Read-only allocation audit when SQLite was built without dbstat.

    Walk table/index B-trees and their overflow chains from sqlite_schema roots.
    Format reference: https://www.sqlite.org/fileformat2.html#b_tree_pages
    Only closed, checkpointed, auto_vacuum=NONE benchmark databases are accepted
    by profile_database. This is a test measurement, never a runtime reader.
    """
    assigned: set[int] = set()
    sizes: dict[str, int] = {}
    with path.open("rb") as handle:
        header = handle.read(100)
        if header[:16] != b"SQLite format 3\x00":
            raise ValueError("invalid_sqlite_header")
        page_size = int.from_bytes(header[16:18], "big")
        page_size = 65536 if page_size == 1 else page_size
        usable = page_size - header[20]
        page_count = path.stat().st_size // page_size

        def page(number):
            if type(number) is not int or not 1 <= number <= page_count or number in assigned:
                raise ValueError("invalid_or_shared_sqlite_page")
            assigned.add(number)
            handle.seek((number - 1) * page_size)
            value = handle.read(page_size)
            if len(value) != page_size:
                raise ValueError("truncated_sqlite_page")
            return value

        def varint(data, at):
            value = 0
            for index in range(9):
                if at >= usable:
                    raise ValueError("invalid_sqlite_varint")
                byte = data[at]
                at += 1
                if index == 8:
                    return (value << 8) | byte, at
                value = (value << 7) | (byte & 127)
                if byte < 128:
                    return value, at
            raise ValueError("invalid_sqlite_varint")

        for name, _kind, _table, root_page in objects:
            before = len(assigned)
            pending = [root_page]
            while pending:
                number = pending.pop()
                data = page(number)
                offset = 100 if number == 1 else 0
                kind = data[offset]
                if kind not in (2, 5, 10, 13):
                    raise ValueError("invalid_sqlite_btree_kind")
                interior = kind in (2, 5)
                count = int.from_bytes(data[offset + 3:offset + 5], "big")
                pointers = offset + (12 if interior else 8)
                if pointers + count * 2 > usable:
                    raise ValueError("invalid_sqlite_cell_pointers")
                if interior:
                    pending.append(int.from_bytes(data[offset + 8:offset + 12], "big"))
                for index in range(count):
                    at = int.from_bytes(data[pointers + index * 2:pointers + index * 2 + 2], "big")
                    if not pointers + count * 2 <= at < usable:
                        raise ValueError("invalid_sqlite_cell_offset")
                    if interior:
                        pending.append(int.from_bytes(data[at:at + 4], "big"))
                        at += 4
                    if kind == 5:
                        continue  # Table interior cells have no payload.
                    payload, at = varint(data, at)
                    if kind == 13:
                        _row_id, at = varint(data, at)
                    maximum = usable - 35 if kind == 13 else (usable - 12) * 64 // 255 - 23
                    if payload <= maximum:
                        continue
                    minimum = (usable - 12) * 32 // 255 - 23
                    local = minimum + (payload - minimum) % (usable - 4)
                    local = local if local <= maximum else minimum
                    overflow_at = at + local
                    if overflow_at + 4 > usable:
                        raise ValueError("invalid_sqlite_overflow_offset")
                    overflow = int.from_bytes(data[overflow_at:overflow_at + 4], "big")
                    remaining = payload - local
                    while remaining > 0:
                        extra = page(overflow)
                        overflow = int.from_bytes(extra[:4], "big")
                        remaining -= usable - 4
                    if overflow:
                        raise ValueError("excess_sqlite_overflow_pages")
            sizes[name] = (len(assigned) - before) * page_size
    return sizes


def profile_database(path: Path) -> dict:
    """Audit physical table/index pages and UTF-8 JSON bytes after writer close."""
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise ValueError("profile_requires_closed_checkpointed_database")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA auto_vacuum").fetchone()[0]:
            raise ValueError("profile_does_not_support_pointer_map_pages")
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        page_count = connection.execute("PRAGMA page_count").fetchone()[0]
        if path.stat().st_size != page_count * page_size:
            raise ValueError("profile_requires_exact_database_page_length")
        free_pages = connection.execute("PRAGMA freelist_count").fetchone()[0]
        objects = [("sqlite_schema", "table", "sqlite_schema", 1), *connection.execute(
            "SELECT name,type,tbl_name,rootpage FROM sqlite_schema WHERE rootpage>0 ORDER BY name")]
        allocation = _btree_page_ownership(path, objects)
        # dbstat is an independent cross-check when the build provides it.
        dbstat_checked = False
        try:
            dbstat = dict(connection.execute("SELECT name,SUM(pgsize) FROM dbstat GROUP BY name"))
        except sqlite3.OperationalError as exc:
            if "no such table: dbstat" not in str(exc):
                raise
        else:
            if dbstat != allocation:
                raise AssertionError("page_parser_dbstat_disagreement")
            dbstat_checked = True
        tables, indexes = {}, {}
        for name, kind, table, _root in objects:
            if kind == "index":
                indexes[name] = {"table": table, "bytes": allocation[name]}
                continue
            quoted = '"' + name.replace('"', '""') + '"'
            columns = {row[1] for row in connection.execute("PRAGMA table_info(" + quoted + ")")}
            rows = connection.execute("SELECT COUNT(*) FROM " + quoted).fetchone()[0]
            average = total = None
            if "record_json" in columns:
                average, total = connection.execute("SELECT AVG(LENGTH(CAST(record_json AS BLOB))),"
                    "COALESCE(SUM(LENGTH(CAST(record_json AS BLOB))),0) FROM " + quoted).fetchone()
            tables[name] = {"rows": rows, "data_page_bytes": allocation[name], "index_bytes": 0,
                            "average_record_json_bytes": average, "total_record_json_bytes": total}
        for item in indexes.values():
            tables[item["table"]]["index_bytes"] += item["bytes"]
        reserved_lock_bytes = page_size if page_count > 0x40000000 // page_size else 0
        if sum(allocation.values()) + free_pages * page_size + reserved_lock_bytes != page_count * page_size:
            raise AssertionError("unattributed_database_pages")
        health = {"instrument_rows": 0, "source_rows": 0, "instrument_counters": Counter(),
                  "source_counters": Counter(), "instrument_status_changes": 0, "source_status_changes": 0}
        if "health_rollups_1m" in tables:
            for instrument, raw in connection.execute("SELECT instrument_id,record_json FROM health_rollups_1m"):
                level = "source" if instrument == "*" else "instrument"
                row = json.loads(raw)
                health[level + "_rows"] += 1
                health[level + "_counters"].update(row.get("counters", {}))
                health[level + "_status_changes"] += len(row.get("status_changes", ()))
    return {"method": "readonly_btree_page_walk", "dbstat_cross_checked": dbstat_checked,
            "page_size": page_size, "page_count": page_count, "freelist_pages": free_pages,
            "database_bytes": path.stat().st_size, "index_bytes": sum(item["bytes"] for item in indexes.values()),
            "tables": tables, "indexes": indexes, "health_evidence": health}


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
            storage_profile = profile_database(path)
            buckets = rows["market_buckets_1m"]
            return {
                "benchmark_version": 2, "python": platform.python_version(), "platform": platform.system(),
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
                "storage_profile": storage_profile,
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
