from __future__ import annotations

from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from .capacity import _btree_page_ownership, profile_database, run_capacity


def _fingerprint(directory):
    return {path.name: (path.stat().st_size, path.stat().st_mtime_ns,
                       hashlib.sha256(path.read_bytes()).hexdigest())
            for path in directory.iterdir() if path.is_file()}


def _read_connection(path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)


class DatabaseProfileTests(unittest.TestCase):
    def test_profile_rejects_existing_wal_shm_or_journal_without_touching_files(self):
        for suffix in ("-wal", "-shm", "-journal"):
            with self.subTest(suffix=suffix), TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / "sidecar.db"
                with closing(sqlite3.connect(path)) as connection:
                    connection.execute("CREATE TABLE sample(value TEXT)")
                    connection.commit()
                # Even an empty sidecar is refused; the profiler must not infer
                # an offline snapshot from an apparently empty live artifact.
                Path(str(path) + suffix).write_bytes(b"")
                before = _fingerprint(root)
                with self.assertRaisesRegex(ValueError, "profile_requires_closed_checkpointed_database"):
                    profile_database(path)
                self.assertEqual(_fingerprint(root), before)

    def test_profile_rejects_trailing_bytes_or_pages_without_touching_files(self):
        for trailing_bytes in (1, 512):
            with self.subTest(trailing_bytes=trailing_bytes), TemporaryDirectory() as directory:
                root = Path(directory)
                path = root / "trailing.db"
                with closing(sqlite3.connect(path)) as connection:
                    connection.execute("PRAGMA page_size=512")
                    connection.execute("CREATE TABLE sample(value TEXT)")
                    connection.commit()
                with path.open("ab") as handle:
                    handle.write(b"\x00" * trailing_bytes)
                before = _fingerprint(root)
                with self.assertRaisesRegex(ValueError, "profile_requires_exact_database_page_length"):
                    profile_database(path)
                self.assertEqual(_fingerprint(root), before)

    def test_btree_table_index_overflow_and_freelist_account_for_every_file_page(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "allocation.db"
            records = [json.dumps({"row": index, "note": "中文样本" * (2500 if index % 67 == 0 else 20)},
                                  ensure_ascii=False, separators=(",", ":")) for index in range(200)]
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("PRAGMA page_size=512")
                connection.execute("PRAGMA auto_vacuum=NONE")
                connection.execute("CREATE TABLE samples(id INTEGER PRIMARY KEY, lookup TEXT, record_json TEXT)")
                connection.execute("CREATE INDEX idx_samples_lookup ON samples(lookup)")
                connection.executemany("INSERT INTO samples VALUES (?, ?, ?)",
                    ((index, "索引键" * 100 + f"{index:04d}", raw) for index, raw in enumerate(records)))
                connection.execute("CREATE TABLE discarded(record_json TEXT)")
                connection.executemany("INSERT INTO discarded VALUES (?)", (("x" * 5000,) for _ in range(40)))
                connection.commit()
                connection.execute("DELETE FROM discarded")
                connection.commit()
            before = _fingerprint(root)
            with closing(_read_connection(path)) as connection:
                objects = [("sqlite_schema", "table", "sqlite_schema", 1), *connection.execute(
                    "SELECT name,type,tbl_name,rootpage FROM sqlite_schema WHERE rootpage>0 ORDER BY name")]
                free_pages = connection.execute("PRAGMA freelist_count").fetchone()[0]
                try:
                    independent_dbstat = dict(connection.execute("SELECT name,SUM(pgsize) FROM dbstat GROUP BY name"))
                except sqlite3.OperationalError as exc:
                    self.assertIn("no such table: dbstat", str(exc))
                    independent_dbstat = None
            self.assertGreater(free_pages, 0)
            # The fixture truly contains both kinds of interior page. Its large
            # row and index keys exceed one 512-byte page and require overflow.
            roots = {name: page for name, _kind, _table, page in objects}
            with path.open("rb") as handle:
                handle.seek((roots["samples"] - 1) * 512)
                self.assertEqual(handle.read(1), b"\x05")
                handle.seek((roots["idx_samples_lookup"] - 1) * 512)
                self.assertEqual(handle.read(1), b"\x02")
            self.assertGreater(max(len(raw.encode("utf-8")) for raw in records), 10 * 512)
            allocation = _btree_page_ownership(path, objects)
            profile = profile_database(path)
            self.assertEqual(profile["page_size"], 512)
            self.assertEqual(profile["freelist_pages"], free_pages)
            self.assertEqual(sum(allocation.values()) + free_pages * 512, path.stat().st_size)
            self.assertEqual(profile["page_count"] * profile["page_size"], profile["database_bytes"])
            self.assertEqual(set(allocation), {item[0] for item in objects})
            self.assertTrue(all(value > 0 and value % 512 == 0 for value in allocation.values()))
            self.assertEqual(profile["tables"]["samples"]["data_page_bytes"], allocation["samples"])
            self.assertEqual(profile["indexes"]["idx_samples_lookup"]["bytes"], allocation["idx_samples_lookup"])
            self.assertEqual(profile["tables"]["samples"]["index_bytes"], allocation["idx_samples_lookup"])
            self.assertEqual(profile["tables"]["discarded"]["rows"], 0)
            expected_json_bytes = sum(len(raw.encode("utf-8")) for raw in records)
            self.assertGreater(expected_json_bytes, sum(map(len, records)))
            self.assertEqual(profile["tables"]["samples"]["rows"], 200)
            self.assertEqual(profile["tables"]["samples"]["total_record_json_bytes"], expected_json_bytes)
            self.assertAlmostEqual(profile["tables"]["samples"]["average_record_json_bytes"], expected_json_bytes / 200)
            self.assertEqual(profile["dbstat_cross_checked"], independent_dbstat is not None)
            if independent_dbstat is not None:
                self.assertEqual(allocation, independent_dbstat)
            self.assertEqual(_fingerprint(root), before)

    def test_profile_separates_source_and_instrument_health_evidence_without_writes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "health.db"
            rows = (
                ("AAA", {"source": "sample", "counters": {"accepted": 4, "late_events": 1},
                         "status_changes": [{"at_ms": 10, "status": "complete", "reason": ""}]}),
                ("BBB", {"source": "sample", "counters": {"accepted": 6}, "status_changes": []}),
                ("*", {"source": "sample", "counters": {"accepted": 10, "queue_pressure": 2},
                       "status_changes": [{"at_ms": 10, "status": "ready", "reason": ""},
                                          {"at_ms": 20, "status": "degraded", "reason": "排队"}]}),
                ("*", {"source": "writer", "counters": {"writer_failures": 1}, "status_changes": []}),
            )
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE health_rollups_1m(instrument_id TEXT,record_json TEXT)")
                connection.executemany("INSERT INTO health_rollups_1m VALUES (?, ?)",
                    ((instrument, json.dumps(record, ensure_ascii=False)) for instrument, record in rows))
                connection.commit()
            before = _fingerprint(root)
            profile = profile_database(path)
            health = profile["health_evidence"]
            self.assertEqual(health["instrument_rows"], 2)
            self.assertEqual(health["source_rows"], 2)
            self.assertEqual(dict(health["instrument_counters"]), {"accepted": 10, "late_events": 1})
            self.assertEqual(dict(health["source_counters"]), {"accepted": 10, "queue_pressure": 2, "writer_failures": 1})
            self.assertEqual(health["instrument_status_changes"], 1)
            self.assertEqual(health["source_status_changes"], 2)
            self.assertEqual(profile["tables"]["health_rollups_1m"]["rows"], 4)
            self.assertEqual(_fingerprint(root), before)

    def test_btree_walker_rejects_a_shared_root_instead_of_double_counting(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "shared-root.db"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE sample(value TEXT)")
                connection.commit()
            before = _fingerprint(root)
            with self.assertRaisesRegex(ValueError, "invalid_or_shared_sqlite_page"):
                _btree_page_ownership(path, [("first", "table", "first", 1), ("second", "table", "second", 1)])
            self.assertEqual(_fingerprint(root), before)


class CapacityTests(unittest.TestCase):
    def test_600_instruments_normal_stream(self):
        result = run_capacity(instruments=600, minutes=2, trace_memory=False)
        self.assertEqual(result["events"], 2400)
        self.assertEqual(result["bucket_count"], 1200)
        self.assertEqual(result["database_rows"]["instruments"], 600)
        self.assertGreater(result["database_bytes"], 0)
        health = result["storage_profile"]["health_evidence"]
        self.assertEqual(health["instrument_rows"], 0)
        self.assertEqual(health["source_counters"]["accepted_events"], 2400)

    def test_1000_instruments_duplicate_stream_and_writer_faults(self):
        result = run_capacity(instruments=1000, minutes=2, pattern="duplicates", inject_failure=True, trace_memory=False)
        self.assertGreater(result["events"], 4000)
        self.assertEqual(result["bucket_count"], 2000)
        self.assertTrue(all(result["faults"].values()))
        self.assertEqual(result["database_rows"]["instruments"], 1000)
        health = result["storage_profile"]["health_evidence"]
        self.assertGreater(health["instrument_rows"], 0)
        self.assertEqual(health["instrument_counters"]["duplicate_events"], result["events"] - 4000)
        self.assertEqual(health["source_counters"]["duplicate_events"], result["events"] - 4000)

    def test_600_instrument_burst_stream_stays_bounded(self):
        result = run_capacity(instruments=600, minutes=2, pattern="burst", trace_memory=False)
        self.assertGreater(result["events"], 2400)
        self.assertEqual(result["bucket_count"], 1200)

    def test_100k_minute_bucket_storage_has_source_totals_without_routine_instrument_rows(self):
        result = run_capacity(instruments=1000, minutes=100, trace_memory=False)
        self.assertEqual(result["events"], 200000)
        self.assertEqual(result["bucket_count"], 100000)
        health = result["storage_profile"]["health_evidence"]
        self.assertEqual(health["instrument_rows"], 0)
        self.assertEqual(health["source_rows"], 199)  # 100 market source + 99 closed runtime minutes
        self.assertEqual(health["source_counters"]["accepted_events"], 200000)
        self.assertEqual(health["source_counters"]["connection_observations"], 100000)
        self.assertEqual(result["database_rows"]["ingest_checkpoints"], 1100)
        tables = result["storage_profile"]["tables"]
        self.assertEqual(tables["market_buckets_1m"]["rows"], 100000)
        self.assertGreater(tables["market_buckets_1m"]["average_record_json_bytes"], 0)
        self.assertGreater(result["wal_peak_bytes"], 0)
        # Compare like-for-like SQL pages, not wall-clock throughput or a
        # production retention promise. The original exact-baseline run was
        # 209,965,056 bytes with 100,199 health rows.
        self.assertLess(result["database_bytes"], 209965056 * 0.8)


if __name__ == "__main__":
    unittest.main()
