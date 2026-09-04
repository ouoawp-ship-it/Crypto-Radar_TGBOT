from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import socket
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from radars.altcoin_hunter.configuration import AltcoinHunterConfig
from radars.altcoin_hunter.read_model import HunterReadModel
from radars.altcoin_hunter.replay import (
    DEFAULT_START_MS, PATTERNS, ReplayRunner, VirtualClock, iter_synthetic_records,
    load_fixture, offline_policy, run_replay,
)
from radars.altcoin_hunter.storage import HunterWriter, StorageError, migrate
from runtime.altcoin_hunter import main


FIXTURES = Path(__file__).parent / "fixtures"


def fingerprint(directory):
    return {p.name: (p.stat().st_size, p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
            for p in directory.iterdir() if p.is_file()}


class FailingWriter(HunterWriter):
    def commit_batch(self, pending, **kwargs):
        if getattr(self, "fail_once", True):
            self.fail_once = False
            raise sqlite3.OperationalError("injected_commit_failure")
        return super().commit_batch(pending, **kwargs)


class BaselineFailureWriter(HunterWriter):
    def save_baselines(self, records):
        raise sqlite3.OperationalError("injected_snapshot_failure")


class ReplayTests(unittest.TestCase):
    def test_virtual_clock_is_explicit_and_nondecreasing(self):
        clock = VirtualClock(DEFAULT_START_MS)
        self.assertEqual(clock.advance(DEFAULT_START_MS + 20), DEFAULT_START_MS + 20)
        self.assertEqual(clock.monotonic_ns, 20_000_000)
        clock.advance(DEFAULT_START_MS + 20)
        for value in (DEFAULT_START_MS + 19, True, 1704067200):
            with self.assertRaises(ValueError):
                clock.advance(value)

    def test_replay_never_initializes_missing_database(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "hunter.db"
            with self.assertRaises(StorageError):
                run_replay(path, iter_synthetic_records(1, 1, 42))
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_normal_fixture_has_known_counts_and_ready_causal_baselines(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "hunter.db"
            migrate(path)
            result = run_replay(path, load_fixture(FIXTURES / "replay_normal.json"), baseline_windows=(1,))
            self.assertEqual(result["counts"]["events"], 32)
            self.assertEqual(result["counts"]["committed_buckets"], 16)
            self.assertEqual(result["counts"]["complete_buckets"], 16)
            self.assertEqual(result["counts"]["incomplete_buckets"], 0)
            self.assertTrue(result["latest_baseline"]["result"]["ready"])
            self.assertEqual(result["latest_baseline"]["result"]["sample_count"], 7)
            self.assertFalse(result["uncommitted_input"])
            self.assertEqual(result["storage_counts"]["baseline_state"], 6)
            self.assertFalse(result["real_send"])
            self.assertEqual(result["network_calls"], 0)

    def test_identical_fixture_to_two_physical_paths_is_deterministic(self):
        with TemporaryDirectory() as directory:
            results = []
            hashes = []
            for name in ("first.db", "second.db"):
                path = Path(directory) / name
                migrate(path)
                config = AltcoinHunterConfig(enable=True, db_file=path)
                hashes.append(config.config_hash)
                results.append(run_replay(path, iter_synthetic_records(2, 5, 43), config=config))
            self.assertNotEqual(hashes[0], hashes[1])
            self.assertEqual(results[0], results[1])

    def test_nonempty_database_cannot_be_replayed_or_overwritten(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "hunter.db"
            migrate(path)
            run_replay(path, iter_synthetic_records(1, 1, 42), baseline_windows=())
            before = HunterReadModel(path).list_buckets()
            with self.assertRaisesRegex(StorageError, "empty_migrated"):
                run_replay(path, iter_synthetic_records(1, 2, 99))
            self.assertEqual(HunterReadModel(path).list_buckets(), before)

    def test_universe_only_database_is_also_nonempty(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "hunter.db"
            migrate(path)
            first = next(iter_synthetic_records(1, 1, 42))
            run_replay(path, [first], baseline_windows=())
            with self.assertRaisesRegex(StorageError, "empty_migrated"):
                run_replay(path, [])

    def test_failed_commit_never_acknowledges_or_enters_windows(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "hunter.db"
            migrate(path)
            with FailingWriter(path) as writer:
                runner = ReplayRunner(writer, baseline_windows=(1,))
                with self.assertRaises(sqlite3.OperationalError):
                    runner.consume(iter_synthetic_records(1, 1, 42))
                self.assertIsNotNone(runner.aggregator.stats()["pending_batch_id"])
                self.assertEqual(writer.read_counts()["market_buckets_1m"], 0)
                self.assertEqual(runner.windows.stats()["minute_buckets"], 0)
                self.assertEqual(runner.baselines.export(), ())
                runner._flush()  # Retry exactly the frozen pending batch.
                self.assertIsNone(runner.aggregator.stats()["pending_batch_id"])
                self.assertEqual(writer.read_counts()["market_buckets_1m"], 1)
                self.assertEqual(runner.windows.stats()["minute_buckets"], 1)

    def test_baseline_snapshot_failure_reports_rebuild_and_retains_committed_buckets(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "hunter.db"
            migrate(path)
            with BaselineFailureWriter(path) as writer:
                result = ReplayRunner(writer, baseline_windows=(1,)).consume(iter_synthetic_records(1, 2, 42))
                self.assertEqual(result["status"], "degraded")
                self.assertTrue(result["baseline_rebuild_required"])
                self.assertFalse(result["baseline_state_persisted"])
                self.assertEqual(writer.read_counts()["market_buckets_1m"], 2)
                self.assertEqual(writer.read_counts()["baseline_state"], 0)

    def test_duplicates_do_not_inflate_volume_or_trade_count(self):
        with TemporaryDirectory() as directory:
            buckets = []
            for pattern in ("normal", "duplicates"):
                path = Path(directory) / (pattern + ".db")
                migrate(path)
                result = run_replay(path, iter_synthetic_records(1, 2, 42, pattern=pattern), baseline_windows=())
                buckets.append(HunterReadModel(path).list_buckets())
                if pattern == "duplicates":
                    self.assertEqual(result["counts"]["rejected_events"], 4)
            for first, second in zip(*buckets):
                for field in ("quote_volume", "trade_count", "price_open", "price_close"):
                    self.assertEqual(first[field], second[field])

    def test_out_of_order_inside_grace_is_not_future_or_a_sequence_gap(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "hunter.db"
            migrate(path)
            result = run_replay(path, iter_synthetic_records(2, 3, 42, pattern="out_of_order"), baseline_windows=())
            self.assertEqual(result["counts"]["rejected_events"], 0)
            self.assertEqual(result["counts"]["complete_buckets"], 6)

    def test_late_event_is_rejected_after_watermark(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "hunter.db"
            migrate(path)
            result = run_replay(path, iter_synthetic_records(1, 3, 42, pattern="late"), baseline_windows=())
            self.assertEqual(result["counts"]["events"], 7)
            self.assertEqual(result["counts"]["accepted_events"], 6)
            self.assertEqual(result["counts"]["rejected_events"], 1)
            self.assertEqual(result["counts"]["committed_buckets"], 3)

    def test_gap_and_epoch_are_not_complete_data(self):
        with TemporaryDirectory() as directory:
            for pattern in ("gap", "epoch"):
                path = Path(directory) / (pattern + ".db")
                migrate(path)
                result = run_replay(path, iter_synthetic_records(1, 4, 42, pattern=pattern), baseline_windows=(1, 3))
                self.assertGreater(result["counts"]["incomplete_buckets"], 0)
                self.assertFalse(result["latest_window"]["complete"])
                self.assertIsNone(result["latest_baseline"]["result"]["raw_value"])

    def test_burst_recipe_changes_expected_event_count(self):
        events = [r for r in iter_synthetic_records(2, 3, 42, pattern="burst") if r["kind"] == "event"]
        self.assertEqual(len(events), 48)

    def test_six_windows_withhold_missing_long_window_values(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "hunter.db"
            migrate(path)
            result = run_replay(path, iter_synthetic_records(1, 4, 42))
            self.assertEqual(result["baseline_windows"], [1, 3, 5, 15, 30, 60])
            self.assertEqual(result["storage_counts"]["baseline_state"], 18)
            self.assertFalse(result["latest_window"]["complete"])
            self.assertIsNone(result["latest_window"]["price_return_ratio"])
            self.assertEqual(result["baseline_policies"]["60:quote_volume"]["sampling_stride"], 60)

    def test_capacity_mode_keeps_windows_bounded_without_baselines(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "hunter.db"
            migrate(path)
            result = run_replay(path, iter_synthetic_records(1, 125, 42), baseline_windows=())
            self.assertEqual(result["window_stats"]["minute_buckets"], 120)
            self.assertEqual(result["maxima"]["minute_buckets"], 120)
            self.assertEqual(result["counts"]["committed_buckets"], 125)
            self.assertEqual(result["counts"]["baseline_evaluations"], 0)
            self.assertEqual(result["storage_counts"]["baseline_state"], 0)

    def test_unclosed_final_minute_is_reported_without_inventing_time(self):
        records = [r for r in iter_synthetic_records(1, 1, 42) if r["kind"] != "advance"]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "hunter.db"
            migrate(path)
            result = run_replay(path, records, baseline_windows=())
            self.assertTrue(result["uncommitted_input"])
            self.assertEqual(result["status"], "incomplete")
            self.assertEqual(result["reason"], "fixture_did_not_close_final_bucket")
            self.assertEqual(result["counts"]["committed_buckets"], 0)

    def test_missing_coverage_does_not_become_complete(self):
        records = [r for r in iter_synthetic_records(1, 1, 42) if r["kind"] != "coverage"]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "hunter.db"
            migrate(path)
            result = run_replay(path, records, baseline_windows=(1,))
            self.assertEqual(result["counts"]["incomplete_buckets"], 1)
            self.assertIsNone(result["latest_baseline"]["result"]["raw_value"])

    def test_coverage_cannot_claim_future_and_zero_grace_supports_multiple_symbols(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "zero.db"
            migrate(path)
            result = run_replay(path, iter_synthetic_records(2, 1, 42),
                config=AltcoinHunterConfig(enable=True, allowed_lateness_ms=0), baseline_windows=())
            self.assertEqual(result["counts"]["complete_buckets"], 2)
            records = list(iter_synthetic_records(1, 1, 42))
            coverage = next(r for r in records if r["kind"] == "coverage")
            coverage["coverage"]["end_ms"] += 1
            other = Path(directory) / "future.db"
            migrate(other)
            with self.assertRaisesRegex(ValueError, "future_continuity"):
                run_replay(other, records, baseline_windows=())

    def test_generator_is_repeatable_and_rejects_invalid_recipes(self):
        for pattern in PATTERNS:
            self.assertEqual(list(iter_synthetic_records(1, 2, 1, pattern=pattern)),
                             list(iter_synthetic_records(1, 2, 1, pattern=pattern)))
        for args in ((0, 1, 1), (1, 0, 1), (True, 1, 1), (1, 1, -1)):
            with self.assertRaises(ValueError):
                list(iter_synthetic_records(*args))
        with self.assertRaises(ValueError):
            list(iter_synthetic_records(1, 1, 1, pattern="live"))

    def test_replay_does_not_open_sockets(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "hunter.db"
            migrate(path)
            with patch.object(socket, "socket", side_effect=AssertionError("network forbidden")):
                run_replay(path, iter_synthetic_records(1, 1, 42), baseline_windows=())

    def test_cli_explicit_migration_replay_and_zero_write_status(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "hunter.db"
            for args in (["migrate", "--db", str(path)],
                         ["replay", "--db", str(path), "--fixture", str(FIXTURES / "replay_normal.json")]):
                with redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(main(args), 0)
                self.assertEqual(json.loads(output.getvalue())["status"], "ok")
            before = fingerprint(Path(directory))
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(["status", "--db", str(path)]), 0)
            self.assertEqual(json.loads(output.getvalue())["read_mode"], "offline_immutable")
            self.assertEqual(fingerprint(Path(directory)), before)

    def test_cli_missing_status_and_replay_never_create_files(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "absent.db"
            for args in (["status", "--db", str(path)],
                         ["replay", "--db", str(path), "--instruments", "1"]):
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(main(args), 2)
            self.assertEqual(list(Path(directory).iterdir()), [])

    def test_fixture_schema_and_policy_validation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text('{"schema_version":1,"records":[],"recipe":{}}', encoding="utf-8")
            with self.assertRaises(ValueError):
                load_fixture(path)
        with self.assertRaises(ValueError):
            offline_policy(2, "quote_volume")
        with self.assertRaises(ValueError):
            offline_policy(1, "quote_volume", min_sample_count=0)


if __name__ == "__main__":
    unittest.main()
