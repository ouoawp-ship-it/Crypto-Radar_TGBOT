from __future__ import annotations

import json
import os
import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from config import Settings
from runtime.health import runtime_health_checks
from shared.storage import JsonStore


class RuntimeHealthTests(unittest.TestCase):
    def make_settings(self, root: Path) -> Settings:
        return Settings(
            base_dir=root,
            data_dir=root,
            runtime_status_path=root / "runtime_status.json",
            signal_events_db_path=root / "signals.db",
            market_snapshots_db_path=root / "market_snapshots.db",
            realtime_features_db_path=root / "realtime_features.db",
            database_backup_dir=root / "backups",
            health_runtime_max_age_sec=600,
            health_realtime_fresh_sec=180,
            health_database_backup_max_age_sec=3600,
            health_disk_warn_mb=1,
            health_disk_fail_mb=1,
        )

    @staticmethod
    def seed_databases(settings: Settings, now: int) -> None:
        with closing(sqlite3.connect(settings.signal_events_db_path)) as conn:
            conn.execute("CREATE TABLE signals(id INTEGER PRIMARY KEY)")
            conn.commit()
        with closing(sqlite3.connect(settings.market_snapshots_db_path)) as conn:
            conn.execute("CREATE TABLE market_snapshots(observed_at INTEGER NOT NULL)")
            conn.execute("INSERT INTO market_snapshots(observed_at) VALUES(?)", (now - 60,))
            conn.commit()
        with closing(sqlite3.connect(settings.realtime_features_db_path)) as conn:
            conn.execute(
                "CREATE TABLE realtime_market_features("
                "exchange TEXT, symbol TEXT, bucket_start INTEGER, bucket_sec INTEGER)"
            )
            conn.execute(
                "INSERT INTO realtime_market_features VALUES('binance', 'BTCUSDT', ?, 60)",
                (now - 120,),
            )
            conn.commit()

    def test_ready_when_runtime_databases_and_binance_are_fresh(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = 10_000
            settings = self.make_settings(root)
            store = JsonStore(root)
            store.save(settings.runtime_status_path, {"status": "running"})
            os.utime(settings.runtime_status_path, (now - 30, now - 30))
            self.seed_databases(settings, now)

            checks = runtime_health_checks(settings, store, now_ts=now)

        self.assertFalse([item for item in checks if item["status"] == "fail"])
        realtime = next(item for item in checks if item["name"] == "realtime_features_freshness")
        self.assertEqual(realtime["status"], "ok")

    def test_hourly_proximity_hard_failure_is_blocking_health(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = 10_000
            settings = replace(
                self.make_settings(root),
                consolidation_breakout_enable=True,
                consolidation_hourly_proximity_enable=True,
            )
            store = JsonStore(root)
            store.save(settings.runtime_status_path, {
                "status": "running",
                "diagnostics": {
                    "consolidation_breakout": {
                        "hourly_proximity": {
                            "status": "scan_failed",
                            "error_code": "private body must not leak",
                        },
                    },
                },
            })
            os.utime(settings.runtime_status_path, (now - 30, now - 30))
            self.seed_databases(settings, now)

            checks = runtime_health_checks(settings, store, now_ts=now)

        runtime = next(
            item for item in checks if item["name"] == "runtime_status"
        )
        self.assertEqual(runtime["status"], "fail")
        self.assertIn("scan_failed", runtime["detail"])
        self.assertFalse(
            runtime["metrics"]["hourly_proximity_state_file_exists"]
        )
        self.assertNotIn("private body", json.dumps(runtime))

    def test_hourly_proximity_degraded_scan_is_health_warning(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = 10_000
            settings = replace(
                self.make_settings(root),
                consolidation_breakout_enable=True,
                consolidation_hourly_proximity_enable=True,
            )
            store = JsonStore(root)
            store.save(settings.runtime_status_path, {
                "status": "running",
                "diagnostics": {
                    "consolidation_breakout": {
                        "hourly_proximity": {
                            "status": "shadow_idle",
                            "scan": {"status": "degraded"},
                        },
                    },
                },
            })
            os.utime(settings.runtime_status_path, (now - 30, now - 30))
            self.seed_databases(settings, now)

            checks = runtime_health_checks(settings, store, now_ts=now)

        runtime = next(
            item for item in checks if item["name"] == "runtime_status"
        )
        self.assertEqual(runtime["status"], "warn")
        self.assertEqual(
            runtime["metrics"]["hourly_proximity_scan_status"],
            "degraded",
        )

    def test_runtime_disabled_parent_ignores_stale_hourly_failure(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = 10_000
            settings = replace(
                self.make_settings(root),
                consolidation_breakout_enable=True,
                consolidation_hourly_proximity_enable=True,
            )
            store = JsonStore(root)
            store.save(settings.runtime_status_path, {
                "status": "running",
                "no_consolidation_breakout": True,
                "diagnostics": {
                    "consolidation_breakout": {
                        "hourly_proximity": {"status": "scan_failed"},
                    },
                },
            })
            os.utime(settings.runtime_status_path, (now - 30, now - 30))
            self.seed_databases(settings, now)

            checks = runtime_health_checks(settings, store, now_ts=now)

        runtime = next(
            item for item in checks if item["name"] == "runtime_status"
        )
        self.assertEqual(runtime["status"], "ok")

    def test_hourly_warning_does_not_mask_parent_runtime_failure(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = 10_000
            settings = replace(
                self.make_settings(root),
                consolidation_breakout_enable=True,
                consolidation_hourly_proximity_enable=True,
            )
            store = JsonStore(root)
            store.save(settings.runtime_status_path, {
                "status": "consolidation_breakout_failed",
                "diagnostics": {
                    "consolidation_breakout": {
                        "hourly_proximity": {
                            "status": "shadow_idle",
                            "scan": {"status": "degraded"},
                        },
                    },
                },
            })
            os.utime(settings.runtime_status_path, (now - 30, now - 30))
            self.seed_databases(settings, now)

            checks = runtime_health_checks(settings, store, now_ts=now)

        runtime = next(
            item for item in checks if item["name"] == "runtime_status"
        )
        self.assertEqual(runtime["status"], "fail")
        self.assertIn("consolidation_breakout_failed", runtime["detail"])

    def test_stale_runtime_and_exchange_data_are_blocking(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = 10_000
            settings = self.make_settings(root)
            store = JsonStore(root)
            store.save(settings.runtime_status_path, {"status": "running"})
            os.utime(settings.runtime_status_path, (now - 900, now - 900))
            self.seed_databases(settings, now - 600)

            checks = runtime_health_checks(settings, store, now_ts=now)

        failed = {item["name"] for item in checks if item["status"] == "fail"}
        self.assertIn("runtime_status", failed)
        self.assertIn("realtime_features_freshness", failed)

    def test_corrupt_database_is_blocking(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.make_settings(root)
            settings.signal_events_db_path.write_bytes(b"not-a-sqlite-database")

            checks = runtime_health_checks(settings, JsonStore(root), now_ts=10_000)

        signal = next(item for item in checks if item["name"] == "signal_store_integrity")
        self.assertEqual(signal["status"], "fail")

    def test_signal_effectiveness_warns_when_due_outcomes_are_not_evaluated(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = 10_000
            settings = self.make_settings(root)
            store = JsonStore(root)
            store.save(settings.runtime_status_path, {"status": "running"})
            os.utime(settings.runtime_status_path, (now - 30, now - 30))
            self.seed_databases(settings, now)
            with closing(sqlite3.connect(settings.signal_events_db_path)) as conn:
                conn.execute(
                    """
                    CREATE TABLE signal_outcomes (
                        id INTEGER PRIMARY KEY,
                        status TEXT NOT NULL,
                        due_at INTEGER NOT NULL,
                        evaluated_at INTEGER
                    )
                    """
                )
                conn.execute(
                    "INSERT INTO signal_outcomes(status, due_at) VALUES('pending', ?)",
                    (now - 3_600,),
                )
                conn.commit()

            checks = runtime_health_checks(settings, store, now_ts=now)

        effectiveness = next(item for item in checks if item["name"] == "signal_effectiveness")
        self.assertEqual(effectiveness["status"], "warn")
        self.assertEqual(effectiveness["metrics"]["overdue_pending"], 1)

    def test_pulse_review_health_reports_completed_records(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.make_settings(root)
            store = JsonStore(root)
            store.save(root / "review_signals.json", [{
                "radar": "alert",
                "ts": 1000,
                "outcomes": {"3600": {}, "14400": {}},
            }])

            checks = runtime_health_checks(settings, store, now_ts=10_000)

        outcome = next(item for item in checks if item["name"] == "pulse_reviews")
        self.assertEqual(outcome["status"], "ok")
        self.assertEqual(outcome["metrics"]["completed"], 1)

    def test_pulse_review_health_warns_for_overdue_windows(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.make_settings(root)
            store = JsonStore(root)
            store.save(root / "review_signals.json", [{
                "radar": "alert",
                "ts": 1000,
                "outcomes": {},
            }])

            checks = runtime_health_checks(settings, store, now_ts=20_000)

        outcome = next(item for item in checks if item["name"] == "pulse_reviews")
        self.assertEqual(outcome["status"], "warn")
        self.assertEqual(outcome["metrics"]["overdue_windows"], 2)

    def test_database_backup_is_ready_after_restore_verified_manifest(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = 10_000
            settings = self.make_settings(root)
            backup_set = settings.database_backup_dir / "20260101T000000Z"
            backup_set.mkdir(parents=True)
            (backup_set / "manifest.json").write_text(
                json.dumps({
                    "created_at": now - 60,
                    "databases": [{
                        "backup": "signals.db",
                        "integrity": "ok",
                        "restore_verification": "ok",
                    }],
                }),
                encoding="utf-8",
            )

            checks = runtime_health_checks(settings, JsonStore(root), now_ts=now)

        backup = next(item for item in checks if item["name"] == "database_backup")
        self.assertEqual(backup["status"], "ok")
        self.assertEqual(backup["metrics"]["age_sec"], 60)

    def test_altcoin_production_health_is_conditional_and_fail_closed(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = 10_000
            base = self.make_settings(root)
            store = JsonStore(root)

            disabled_checks = runtime_health_checks(base, store, now_ts=now)
            self.assertNotIn(
                "altcoin_contract_anomaly_production",
                {item["name"] for item in disabled_checks},
            )

            enabled = replace(
                base,
                altcoin_contract_anomaly_production_enable=True,
                altcoin_contract_anomaly_production_status_path=(
                    root / "altcoin-production-status.json"
                ),
            )
            enabled_checks = runtime_health_checks(enabled, store, now_ts=now)

        item = next(
            check
            for check in enabled_checks
            if check["name"] == "altcoin_contract_anomaly_production"
        )
        self.assertEqual(item["status"], "fail")

    def test_altcoin_production_health_requires_fresh_manifest_and_route(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = 10_000
            settings = replace(
                self.make_settings(root),
                altcoin_contract_anomaly_production_enable=True,
                altcoin_contract_anomaly_production_send_enable=True,
                altcoin_contract_anomaly_production_status_path=(
                    root / "altcoin-production-status.json"
                ),
            )
            store = JsonStore(root)
            payload = {
                "module": "altcoin_contract_anomaly",
                "mode": "production",
                "status": "running",
                "running": True,
                "process_lock_acquired": True,
                "manifest": {
                    "valid": True,
                    "age_sec": 30,
                    "candidate_count": 3,
                },
                "service": {
                    "connection_state": "connected",
                    "candidate_coverage_complete": True,
                    "force_order_active": True,
                    "accepted_events": 1,
                    "event_sink_ready": True,
                    "mark_price_data_coverage_ratio": 1.0,
                    "aligned_evaluation_rounds": 2,
                    "last_evaluation_candidate_count": 3,
                    "last_evaluation_complete_count": 3,
                    "last_evaluation_epoch_complete_count": 3,
                    "last_evaluation_funding_complete_count": 3,
                },
                "refresh": {"successes": 1, "running": True},
                "processor": {
                    "running": True,
                    "stop_timed_out": False,
                    "pending_batches": 0,
                    "last_error_class": "",
                },
                "telegram": {
                    "route_configured": True,
                    "real_send_enabled": True,
                },
            }
            store.save(settings.altcoin_contract_anomaly_production_status_path, payload)
            os.utime(
                settings.altcoin_contract_anomaly_production_status_path,
                (now - 10, now - 10),
            )

            ready = runtime_health_checks(settings, store, now_ts=now)
            payload["processor"]["quarantined_batches"] = 1
            payload["processor"]["quarantined_symbols"] = 1
            store.save(settings.altcoin_contract_anomaly_production_status_path, payload)
            os.utime(
                settings.altcoin_contract_anomaly_production_status_path,
                (now - 10, now - 10),
            )
            quarantined = runtime_health_checks(settings, store, now_ts=now)
            payload["processor"]["quarantined_batches"] = 0
            payload["processor"]["quarantined_symbols"] = 0
            processing_failures = {}
            for metric in (
                "evaluation_errors",
                "event_sink_failures",
                "event_sink_rejections",
            ):
                payload["service"][metric] = 1
                store.save(
                    settings.altcoin_contract_anomaly_production_status_path,
                    payload,
                )
                os.utime(
                    settings.altcoin_contract_anomaly_production_status_path,
                    (now - 10, now - 10),
                )
                processing_failures[metric] = runtime_health_checks(
                    settings,
                    store,
                    now_ts=now,
                )
                payload["service"][metric] = 0
            payload["manifest"]["age_sec"] = 3_000
            payload["telegram"]["route_configured"] = False
            store.save(settings.altcoin_contract_anomaly_production_status_path, payload)
            os.utime(
                settings.altcoin_contract_anomaly_production_status_path,
                (now - 10, now - 10),
            )
            blocked = runtime_health_checks(settings, store, now_ts=now)

        ready_item = next(
            item
            for item in ready
            if item["name"] == "altcoin_contract_anomaly_production"
        )
        blocked_item = next(
            item
            for item in blocked
            if item["name"] == "altcoin_contract_anomaly_production"
        )
        quarantined_item = next(
            item
            for item in quarantined
            if item["name"] == "altcoin_contract_anomaly_production"
        )
        self.assertEqual(ready_item["status"], "ok")
        self.assertEqual(quarantined_item["status"], "fail")
        self.assertEqual(
            quarantined_item["metrics"]["quarantined_symbols"],
            1,
        )
        self.assertEqual(
            quarantined_item["metrics"]["quarantined_batches"],
            1,
        )
        for metric, checks in processing_failures.items():
            item = next(
                check
                for check in checks
                if check["name"] == "altcoin_contract_anomaly_production"
            )
            self.assertEqual(item["status"], "fail")
            self.assertEqual(item["metrics"][metric], 1)
        self.assertEqual(blocked_item["status"], "fail")

        self.assertTrue(ready_item["metrics"]["candidate_data_ready"])

    def test_altcoin_preview_wal_admission_failure_blocks_readiness(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = 10_000
            settings = replace(
                self.make_settings(root),
                altcoin_contract_anomaly_production_enable=True,
                # Preview mode still needs a durable production WAL.  Real
                # Telegram being disabled must not hide an admission failure.
                altcoin_contract_anomaly_production_send_enable=False,
                altcoin_contract_anomaly_production_status_path=(
                    root / "altcoin-production-status.json"
                ),
            )
            store = JsonStore(root)
            payload = {
                "module": "altcoin_contract_anomaly",
                "mode": "production",
                "status": "running",
                "running": True,
                "process_lock_acquired": True,
                "manifest": {
                    "valid": True,
                    "age_sec": 30,
                    "candidate_count": 0,
                },
                "service": {
                    "connection_state": "connected",
                    "candidate_coverage_complete": True,
                    "force_order_active": True,
                    "accepted_events": 1,
                    "event_sink_ready": True,
                    "evaluation_errors": 1,
                    "event_sink_failures": 1,
                    "event_sink_rejections": 1,
                },
                "refresh": {"successes": 1, "running": True},
                "processor": {
                    "running": True,
                    "stop_timed_out": False,
                    "pending_batches": 0,
                    "queue_rejections": 1,
                    "quarantined_batches": 0,
                    "quarantined_symbols": 0,
                    "last_error_class": "",
                },
                "telegram": {
                    "route_configured": False,
                    "real_send_enabled": False,
                },
            }
            store.save(settings.altcoin_contract_anomaly_production_status_path, payload)
            os.utime(
                settings.altcoin_contract_anomaly_production_status_path,
                (now - 10, now - 10),
            )

            checks = runtime_health_checks(settings, store, now_ts=now)

        item = next(
            check
            for check in checks
            if check["name"] == "altcoin_contract_anomaly_production"
        )
        self.assertEqual(item["status"], "fail")
        self.assertEqual(item["metrics"]["evaluation_errors"], 1)
        self.assertEqual(item["metrics"]["event_sink_failures"], 1)
        self.assertEqual(item["metrics"]["event_sink_rejections"], 1)
        self.assertEqual(item["metrics"]["queue_rejections"], 1)
        self.assertFalse(item["metrics"]["real_send_enabled"])

    def test_altcoin_production_health_rejects_incomplete_candidate_features(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = 10_000
            settings = replace(
                self.make_settings(root),
                altcoin_contract_anomaly_production_enable=True,
                altcoin_contract_anomaly_production_status_path=(
                    root / "altcoin-production-status.json"
                ),
            )
            store = JsonStore(root)
            payload = {
                "module": "altcoin_contract_anomaly",
                "mode": "production",
                "status": "running",
                "running": True,
                "process_lock_acquired": True,
                "manifest": {
                    "valid": True,
                    "age_sec": 30,
                    "candidate_count": 2,
                },
                "service": {
                    "connection_state": "connected",
                    "candidate_coverage_complete": True,
                    "force_order_active": True,
                    "accepted_events": 10,
                    "event_sink_ready": True,
                    "mark_price_data_coverage_ratio": 1.0,
                    "aligned_evaluation_rounds": 1,
                    "last_evaluation_candidate_count": 2,
                    "last_evaluation_complete_count": 1,
                    "last_evaluation_epoch_complete_count": 2,
                    "last_evaluation_funding_complete_count": 2,
                },
                "refresh": {"successes": 1, "running": True},
                "processor": {
                    "running": True,
                    "stop_timed_out": False,
                    "pending_batches": 0,
                    "last_error_class": "",
                },
                "telegram": {
                    "route_configured": False,
                    "real_send_enabled": False,
                },
            }
            store.save(settings.altcoin_contract_anomaly_production_status_path, payload)
            os.utime(
                settings.altcoin_contract_anomaly_production_status_path,
                (now - 10, now - 10),
            )

            checks = runtime_health_checks(settings, store, now_ts=now)

        item = next(
            check
            for check in checks
            if check["name"] == "altcoin_contract_anomaly_production"
        )
        self.assertEqual(item["status"], "fail")
        self.assertFalse(item["metrics"]["candidate_data_ready"])
        self.assertEqual(item["metrics"]["complete_candidate_count"], 1)

    def test_altcoin_production_health_rejects_disconnected_zero_candidate_stream(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = 10_000
            settings = replace(
                self.make_settings(root),
                altcoin_contract_anomaly_production_enable=True,
                altcoin_contract_anomaly_production_status_path=(
                    root / "altcoin-production-status.json"
                ),
            )
            store = JsonStore(root)
            payload = {
                "module": "altcoin_contract_anomaly",
                "mode": "production",
                "status": "running",
                "running": True,
                "process_lock_acquired": True,
                "manifest": {
                    "valid": True,
                    "age_sec": 30,
                    "candidate_count": 0,
                },
                "service": {
                    "connection_state": "disconnected",
                    "candidate_coverage_complete": False,
                    "force_order_active": False,
                    "accepted_events": 0,
                    "event_sink_ready": True,
                },
                "refresh": {"successes": 1, "running": True},
                "processor": {
                    "running": True,
                    "stop_timed_out": False,
                    "pending_batches": 0,
                    "last_error_class": "",
                },
                "telegram": {
                    "route_configured": False,
                    "real_send_enabled": False,
                },
            }
            store.save(settings.altcoin_contract_anomaly_production_status_path, payload)
            os.utime(
                settings.altcoin_contract_anomaly_production_status_path,
                (now - 10, now - 10),
            )

            checks = runtime_health_checks(settings, store, now_ts=now)

        item = next(
            check
            for check in checks
            if check["name"] == "altcoin_contract_anomaly_production"
        )
        self.assertEqual(item["status"], "fail")
        self.assertTrue(item["metrics"]["process_lock_acquired"])
        self.assertEqual(item["metrics"]["connection_state"], "disconnected")
        self.assertFalse(item["metrics"]["force_order_active"])
        self.assertEqual(item["metrics"]["accepted_events"], 0)


if __name__ == "__main__":
    unittest.main()
