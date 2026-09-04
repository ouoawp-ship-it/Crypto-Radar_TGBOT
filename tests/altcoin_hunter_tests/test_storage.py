from __future__ import annotations

from contextlib import closing
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest

from radars.altcoin_hunter.aggregation import MinuteBucket, PendingBatch
from radars.altcoin_hunter.baselines import BaselineKey, BaselinePolicy, RollingBaseline
from radars.altcoin_hunter.read_model import HunterReadModel, ReadOnlyUnavailable
from radars.altcoin_hunter.quality import QualityTracker
from radars.altcoin_hunter.storage import HunterWriter, MigrationError, StorageError, TABLES, migrate
from radars.altcoin_hunter.universe import Instrument


START = 1_800_000_000_000
IDENTITY = {"source": "binance_public", "exchange": "binance", "market": "perpetual", "instrument_id": "AAAUSDT"}


def make_bucket(start_ms=START):
    return MinuteBucket(
        **IDENTITY, symbol="AAAUSDT", start_ms=start_ms, end_ms=start_ms + 60_000,
        connection_epoch=1, connection_epochs=(1,), price_open="100", price_high="110",
        price_low="90", price_close="105", buy_quote="300", sell_quote="100",
        quote_volume="400", delta_quote="200", trade_count=2, first_event_ms=start_ms + 1000,
        last_event_ms=start_ms + 59000, sequence_start=1, sequence_end=2,
        first_source_event_id="trade-1", last_source_event_id="trade-2", event_id_digest="abc",
        coverage_ms=60_000, quality_status="complete", quality_flags=(), quote_currency="USDT",
        base_quantity="4",
    )


def make_pending(batch_id="batch-one", *, health=()):
    bucket = make_bucket()
    checkpoint = {**IDENTITY, "committed_through_ms": bucket.end_ms, "connection_epoch": 1,
                  "sequence_start": 1, "sequence_end": 2, "last_source_event_id": "trade-2"}
    return PendingBatch(batch_id, (bucket,), (checkpoint,), health)


def baseline():
    instance = RollingBaseline(BaselinePolicy())
    instance.evaluate_and_observe(START + 60_000, "400")
    return json.loads(json.dumps(instance.export(BaselineKey(**IDENTITY, feature="volume", window_sec=60))))


def fingerprint(directory):
    return {path.name: (path.stat().st_size, path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
            for path in directory.iterdir() if path.is_file()}


class FailingCommitWriter(HunterWriter):
    fail_next = False
    ambiguous_next = False

    def _commit_transaction(self, connection):
        if self.fail_next:
            self.fail_next = False
            raise sqlite3.OperationalError("injected_before_commit")
        super()._commit_transaction(connection)
        if self.ambiguous_next:
            self.ambiguous_next = False
            raise sqlite3.OperationalError("injected_after_commit")


class HunterStorageTests(unittest.TestCase):
    def test_constructors_and_missing_status_create_nothing(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "absent" / "hunter.db"
            writer = HunterWriter(path)
            reader = HunterReadModel(path)
            self.assertEqual(reader.status()["status"], "missing")
            with self.assertRaises(StorageError):
                writer.open()
            self.assertEqual(list(root.iterdir()), [])

    def test_explicit_migration_is_exact_seven_tables_and_idempotent(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "hunter.db"
            self.assertEqual(migrate(path)["applied"], [1])
            before = fingerprint(root)
            self.assertEqual(migrate(path)["status"], "current")
            self.assertEqual(fingerprint(root), before)
            with closing(sqlite3.connect(path)) as connection:
                tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                indexes = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            self.assertEqual(tables, set(TABLES))
            self.assertIn("idx_market_buckets_symbol_time", indexes)

    def test_checksum_tamper_and_higher_version_are_rejected_without_repair(self):
        for mutation in ("UPDATE schema_migrations SET checksum='tampered'", "UPDATE schema_migrations SET version=2"):
            with self.subTest(mutation=mutation), TemporaryDirectory() as tmp:
                root = Path(tmp)
                path = root / "hunter.db"
                migrate(path)
                with closing(sqlite3.connect(path)) as connection:
                    connection.execute(mutation)
                    connection.commit()
                before = fingerprint(root)
                with self.assertRaises(MigrationError):
                    migrate(path)
                with self.assertRaises(MigrationError):
                    HunterWriter(path).open()
                self.assertEqual(HunterReadModel(path).status()["status"], "unavailable")
                self.assertEqual(fingerprint(root), before)

    def test_unmanaged_and_legacy_paths_never_get_migrated(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("signals.db", "market_snapshots.db", "realtime_features.db", "binance_coordination.db", "jobs.db", "onchain_signals.db"):
                with self.subTest(name=name), self.assertRaises(StorageError):
                    migrate(root / name)
            path = root / "unmanaged.db"
            with closing(sqlite3.connect(path)) as connection:
                connection.execute("CREATE TABLE existing(value TEXT)")
                connection.commit()
            before = fingerprint(root)
            with self.assertRaises(MigrationError):
                migrate(path)
            self.assertEqual(fingerprint(root), before)

    def test_atomic_bucket_checkpoint_health_baseline_rollback_then_retry(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "hunter.db"
            migrate(path)
            health = {**IDENTITY, "minute_ms": START, "counters": {"accepted": 2}}
            pending = make_pending(health=(health,))
            with FailingCommitWriter(path) as writer:
                writer.fail_next = True
                with self.assertRaises(sqlite3.OperationalError):
                    writer.commit_batch(pending, baseline_states=(baseline(),))
                counts = writer.read_counts()
                for table in ("market_buckets_1m", "ingest_checkpoints", "health_rollups_1m", "baseline_state"):
                    self.assertEqual(counts[table], 0)
                result = writer.commit_batch(pending, baseline_states=(baseline(),))
                self.assertFalse(result.receipt.already_committed)
                self.assertEqual(result.buckets[0].quote_volume, "400")
                self.assertEqual(writer.checkpoints()[0]["committed_through_ms"], START + 60000)
                self.assertEqual(writer.load_baselines(), [baseline()])

    def test_ambiguous_commit_retry_uses_durable_receipt(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "hunter.db"
            migrate(path)
            pending = make_pending()
            with FailingCommitWriter(path) as writer:
                writer.ambiguous_next = True
                with self.assertRaises(sqlite3.OperationalError):
                    writer.commit_batch(pending)
            with HunterWriter(path) as writer:
                result = writer.commit_batch(pending)
                self.assertTrue(result.receipt.already_committed)
                self.assertEqual(writer.read_counts()["market_buckets_1m"], 1)
                self.assertEqual(writer.read_counts()["ingest_checkpoints"], 2)
                with self.assertRaisesRegex(StorageError, "batch_id_payload_mismatch"):
                    writer.commit_batch(pending, baseline_states=(baseline(),))

    def test_busy_database_failure_is_bounded_and_does_not_advance_checkpoint(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "hunter.db"
            migrate(path)
            with HunterWriter(path, busy_timeout_ms=10) as writer:
                blocker = sqlite3.connect(path, isolation_level=None)
                try:
                    blocker.execute("BEGIN IMMEDIATE")
                    with self.assertRaises(sqlite3.OperationalError):
                        writer.commit_batch(make_pending())
                    self.assertEqual(writer.checkpoints(), [])
                finally:
                    blocker.rollback()
                    blocker.close()
                writer.commit_batch(make_pending())
                self.assertEqual(writer.read_counts()["market_buckets_1m"], 1)

    def test_owner_thread_enforced_before_sqlite_access(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "hunter.db"
            migrate(path)
            failures = []
            with HunterWriter(path) as writer:
                def other_thread():
                    try:
                        writer.read_counts()
                    except BaseException as exc:
                        failures.append(exc)
                thread = threading.Thread(target=other_thread)
                thread.start()
                thread.join(timeout=2)
                self.assertFalse(thread.is_alive())
                self.assertEqual(len(failures), 1)
                self.assertIsInstance(failures[0], StorageError)

    def test_reader_refuses_active_writer_and_existing_wal_without_filesystem_change(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "hunter.db"
            migrate(path)
            with HunterWriter(path) as writer:
                writer.commit_batch(make_pending())
                before = fingerprint(root)
                self.assertEqual(HunterReadModel(path).status()["status"], "unavailable")
                self.assertEqual(fingerprint(root), before)
            wal = Path(str(path) + "-wal")
            wal.write_bytes(b"")
            before = fingerprint(root)
            self.assertEqual(HunterReadModel(path).status()["status"], "unavailable")
            self.assertEqual(fingerprint(root), before)

    def test_closed_database_queries_are_zero_write_and_query_only(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "hunter.db"
            migrate(path)
            with HunterWriter(path) as writer:
                writer.commit_batch(make_pending(), baseline_states=(baseline(),))
            reader = HunterReadModel(path)
            before = fingerprint(root)
            self.assertEqual(reader.status()["counts"]["market_buckets_1m"], 1)
            self.assertEqual(reader.list_buckets(instrument_id="AAAUSDT")[0]["buy_quote"], "300")
            self.assertEqual(reader.load_baselines(), [baseline()])
            self.assertEqual(len(reader.checkpoints()), 1)
            with reader._connect() as connection:
                self.assertEqual(connection.execute("PRAGMA query_only").fetchone()[0], 1)
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute("DELETE FROM market_buckets_1m")
            self.assertEqual(fingerprint(root), before)

    def test_health_only_delta_batches_merge_once_and_preserve_both_latency_metrics(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "hunter.db"
            migrate(path)
            change = {"at_ms": START + 1, "status": "incomplete", "reason": "gap"}
            first = {**IDENTITY, "minute_ms": START, "counters": {"late": 2}, "max_processing_latency_ms": 10,
                     "max_event_latency_ms": 200, "max_queue_depth": 7, "max_checkpoint_lag_ms": 500,
                     "connection_epochs": [1], "status_changes": [change]}
            second = {**first, "counters": {"late": 3, "invalid": 1}, "max_processing_latency_ms": 20,
                      "max_event_latency_ms": 100, "max_queue_depth": 10, "max_checkpoint_lag_ms": 100,
                      "connection_epochs": [2]}
            with HunterWriter(path) as writer:
                writer.commit_batch(PendingBatch("health-one", (), (), (first,)))
                batch = PendingBatch("health-two", (), (), (second,))
                writer.commit_batch(batch)
                writer.commit_batch(batch)
            with closing(sqlite3.connect(path)) as connection:
                value = json.loads(connection.execute("SELECT record_json FROM health_rollups_1m").fetchone()[0])
            self.assertEqual(value["counters"], {"late": 5, "invalid": 1})
            self.assertEqual(value["max_processing_latency_ms"], 20)
            self.assertEqual(value["max_event_latency_ms"], 200)
            self.assertEqual(value["max_queue_depth"], 10)
            self.assertEqual(value["max_checkpoint_lag_ms"], 500)
            self.assertEqual(value["status_changes"], [change])
            self.assertEqual(value["connection_epochs"], [1, 2])

    def test_health_native_identity_and_epoch_contracts_reject_without_commit(self):
        class StringSubclass(str):
            pass

        invalid_values = (None, True, 0, 1.0, "", " ", " leading", "trailing ", "bad\x00id",
                          "bad\x7fid", "x" * 129, StringSubclass("looks-valid"))
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "hunter.db"
            migrate(path)
            valid = {**IDENTITY, "minute_ms": START, "counters": {"duplicates": 1}}
            with HunterWriter(path) as writer:
                for field in IDENTITY:
                    for value in invalid_values:
                        with self.subTest(field=field, value=repr(value)), self.assertRaises(StorageError):
                            writer.commit_batch(PendingBatch("invalid-health", (), (), ({**valid, field: value},)))
                    absent = dict(valid)
                    del absent[field]
                    with self.subTest(field=field, missing=True), self.assertRaises(StorageError):
                        writer.commit_batch(PendingBatch("missing-health", (), (), (absent,)))
                for epochs in ([True], [-1], [1.5], ["1"], [1, 1], list(range(33)), None):
                    with self.subTest(epochs=epochs), self.assertRaises(StorageError):
                        writer.commit_batch(PendingBatch("invalid-epochs", (), (), ({**valid, "connection_epochs": epochs},)))
                self.assertEqual(writer.read_counts()["health_rollups_1m"], 0)
                self.assertEqual(writer.read_counts()["ingest_checkpoints"], 0)
                writer.commit_batch(PendingBatch("explicit-source", (), (), ({**valid, "instrument_id": "*"},)))
                self.assertEqual(writer.read_counts()["health_rollups_1m"], 1)

    def test_health_frozen_retry_then_post_prepare_delta_is_committed_once(self):
        quality = QualityTracker()
        quality.record(IDENTITY, "accepted", observed_ms=START + 1, processing_latency_ms=900,
                       event_latency_ms=800, queue_depth=100, checkpoint_lag_ms=5000)
        quality.status(IDENTITY, "incomplete", "gap", observed_ms=START + 1)
        frozen = quality.prepare(START + 60000)
        serialized = [row.to_dict() for row in frozen]
        quality.record(IDENTITY, "accepted", observed_ms=START + 2, processing_latency_ms=20,
                       event_latency_ms=30, queue_depth=2, checkpoint_lag_ms=100)
        quality.status(IDENTITY, "complete", "", observed_ms=START + 2)
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "hunter.db"
            migrate(path)
            with FailingCommitWriter(path) as writer:
                first = PendingBatch("generation-one", (), (), frozen)
                writer.fail_next = True
                with self.assertRaises(sqlite3.OperationalError):
                    writer.commit_batch(first)
                self.assertIs(quality.prepare(START + 60000), frozen)
                self.assertEqual([row.to_dict() for row in frozen], serialized)
                self.assertEqual(writer.read_counts()["ingest_checkpoints"], 0)
                self.assertEqual(writer.read_counts()["health_rollups_1m"], 0)
                writer.commit_batch(first)
                self.assertTrue(writer.commit_batch(first).receipt.already_committed)
                quality.acknowledge(frozen)
                newer = quality.prepare(START + 60000)
                for row in newer:
                    self.assertEqual(dict(row.counters)["accepted"], 1)
                    self.assertEqual((row.max_processing_latency_ms, row.max_event_latency_ms,
                                      row.max_queue_depth, row.max_checkpoint_lag_ms), (20, 30, 2, 100))
                second = PendingBatch("generation-two", (), (), newer)
                writer.commit_batch(second)
                writer.commit_batch(second)
                quality.acknowledge(newer)
            with closing(sqlite3.connect(path)) as connection:
                rows = [json.loads(row[0]) for row in connection.execute("SELECT record_json FROM health_rollups_1m")]
            self.assertEqual(len(rows), 2)
            for row in rows:
                self.assertEqual(row["counters"]["accepted"], 2)
                # The durable minute total keeps its correct all-generation
                # maxima; only the new pending delta must exclude old gauges.
                self.assertEqual((row["max_processing_latency_ms"], row["max_event_latency_ms"],
                                  row["max_queue_depth"], row["max_checkpoint_lag_ms"]), (900, 800, 100, 5000))
            instrument = next(row for row in rows if row["instrument_id"] != "*")
            self.assertEqual([change["status"] for change in instrument["status_changes"]], ["incomplete", "complete"])

    def test_health_epoch_capacity_merge_keeps_overflow_evidence_and_retry_is_idempotent(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "hunter.db"
            migrate(path)
            first = {**IDENTITY, "minute_ms": START, "counters": {"epoch_changed": 31},
                     "connection_epochs": list(range(32))}
            second = {**IDENTITY, "minute_ms": START, "counters": {"epoch_changed": 2},
                      "connection_epochs": [32, 33]}
            with HunterWriter(path) as writer:
                writer.commit_batch(PendingBatch("epochs-one", (), (), (first,)))
                batch = PendingBatch("epochs-two", (), (), (second,))
                writer.commit_batch(batch)
                writer.commit_batch(batch)
            with closing(sqlite3.connect(path)) as connection:
                row = json.loads(connection.execute("SELECT record_json FROM health_rollups_1m").fetchone()[0])
            self.assertEqual(row["connection_epochs"], list(range(32)))
            self.assertEqual(row["counters"]["epoch_changed"], 33)
            self.assertEqual(row["counters"]["connection_epoch_merge_overflow_values"], 2)
            self.assertNotIn("connection_epoch_overflow_observations", row["counters"])

    def test_epoch_merge_overflow_is_not_mislabeled_as_event_observation_count(self):
        quality = QualityTracker()
        for epoch in range(32):
            quality.record(IDENTITY, "accepted", observed_ms=START + 1, connection_epoch=epoch)
        first = quality.prepare(START + 60000)
        quality.acknowledge(first)
        for _ in range(10):
            quality.record(IDENTITY, "accepted", observed_ms=START + 2, connection_epoch=32)
        second = quality.prepare(START + 60000)
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "hunter.db"
            migrate(path)
            with HunterWriter(path) as writer:
                writer.commit_batch(PendingBatch("epoch-seen-first", (), (), first))
                batch = PendingBatch("epoch-seen-second", (), (), second)
                writer.commit_batch(batch)
                writer.commit_batch(batch)
            with closing(sqlite3.connect(path)) as connection:
                rows = [json.loads(row[0]) for row in connection.execute("SELECT record_json FROM health_rollups_1m")]
            self.assertEqual(len(rows), 2)
            for row in rows:
                self.assertEqual(row["counters"]["accepted"], 42)
                self.assertEqual(row["counters"]["connection_epoch_merge_overflow_values"], 1)
                self.assertNotIn("connection_epoch_overflow_observations", row["counters"])

    def test_checkpoint_cannot_advance_without_its_bucket(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "hunter.db"
            migrate(path)
            pending = make_pending()
            invalid = PendingBatch("no-bucket", (), pending.checkpoints, ())
            wrong = replace(pending, checkpoints=({**pending.checkpoints[0], "committed_through_ms": START + 120_000},))
            with HunterWriter(path) as writer:
                for value in (invalid, wrong, replace(pending, checkpoints=())):
                    with self.subTest(value=value.batch_id), self.assertRaises(StorageError):
                        writer.commit_batch(value)
                self.assertEqual(writer.checkpoints(), [])

    def test_string_nan_is_rejected_at_storage_boundary(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "hunter.db"
            migrate(path)
            good = make_pending()
            bad_bucket = {**good.buckets[0].to_dict(), "price_close": "NaN"}
            bad = SimpleNamespace(batch_id="nonfinite", buckets=(bad_bucket,), checkpoints=good.checkpoints, health_rollups=())
            with HunterWriter(path) as writer:
                with self.assertRaises(StorageError):
                    writer.commit_batch(bad)
                self.assertEqual(writer.read_counts()["market_buckets_1m"], 0)

    def test_instrument_refresh_records_only_semantic_changes(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "hunter.db"
            migrate(path)
            instrument = Instrument(**{key: value for key, value in IDENTITY.items() if key != "source"},
                                    symbol="AAAUSDT", exchange_symbol="AAAUSDT", source="binance_public",
                                    effective_at_ms=START)
            with HunterWriter(path) as writer:
                first = writer.upsert_instruments((instrument,))
                repeat = writer.upsert_instruments((replace(instrument, effective_at_ms=START + 1000),))
                changed = writer.upsert_instruments((replace(instrument, effective_at_ms=START + 2000, sampling_priority="ELEVATED"),))
                self.assertEqual((first.inserted, repeat.unchanged, changed.updated), (1, 1, 1))
                self.assertEqual(writer.read_counts()["universe_history"], 2)
            self.assertEqual(HunterReadModel(path).instruments()[0]["sampling_priority"], "ELEVATED")

    def test_invalid_baseline_sample_or_instrument_version_rolls_back(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "hunter.db"
            migrate(path)
            invalid = baseline()
            invalid["payload"]["samples"][0] = [START + 60_000, "NaN"]
            instrument = Instrument(**{key: value for key, value in IDENTITY.items() if key != "source"},
                                    symbol="AAAUSDT", exchange_symbol="AAAUSDT", source="binance_public",
                                    effective_at_ms=START).to_dict()
            instrument["metadata_version"] = 1.5
            with HunterWriter(path) as writer:
                with self.assertRaises(StorageError):
                    writer.commit_batch(make_pending(), baseline_states=(invalid,))
                with self.assertRaises(StorageError):
                    writer.upsert_instruments((instrument,))
                counts = writer.read_counts()
                for table in ("market_buckets_1m", "ingest_checkpoints", "baseline_state", "instruments", "universe_history"):
                    self.assertEqual(counts[table], 0)

    def test_baseline_checkpoint_cannot_regress_or_change_payload_at_same_time(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "hunter.db"
            migrate(path)
            current = baseline()
            older = RollingBaseline(BaselinePolicy())
            older.evaluate_and_observe(START, "400")
            old_record = older.export(BaselineKey(**IDENTITY, feature="volume", window_sec=60))
            conflict = baseline()
            conflict["payload"]["samples"][0][1] = "401"
            with HunterWriter(path) as writer:
                writer.save_baselines((current,))
                writer.save_baselines((current,))
                with self.assertRaisesRegex(StorageError, "baseline_checkpoint_regression"):
                    writer.save_baselines((old_record,))
                with self.assertRaisesRegex(StorageError, "baseline_checkpoint_payload_conflict"):
                    writer.save_baselines((conflict,))
                self.assertEqual(writer.load_baselines(), [current])
                self.assertEqual(writer.read_counts()["baseline_state"], 1)


if __name__ == "__main__":
    unittest.main()
