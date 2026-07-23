from __future__ import annotations

import os
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.config import Settings
from paopao_radar.health import runtime_health_checks
from paopao_radar.storage import JsonStore


class RuntimeHealthTests(unittest.TestCase):
    def make_settings(self, root: Path) -> Settings:
        return Settings(
            base_dir=root,
            data_dir=root,
            runtime_status_path=root / "runtime_status.json",
            signal_events_db_path=root / "signals.db",
            market_snapshots_db_path=root / "market_snapshots.db",
            realtime_features_db_path=root / "realtime_features.db",
            news_events_db_path=root / "news_events.db",
            health_runtime_max_age_sec=600,
            health_realtime_fresh_sec=180,
            health_disk_warn_mb=1,
            health_disk_fail_mb=1,
        )

    @staticmethod
    def seed_databases(settings: Settings, now: int) -> None:
        with closing(sqlite3.connect(settings.signal_events_db_path)) as conn:
            conn.execute("CREATE TABLE signals(id INTEGER PRIMARY KEY)")
            conn.commit()
        with closing(sqlite3.connect(settings.news_events_db_path)) as conn:
            conn.execute("CREATE TABLE news_events(id INTEGER PRIMARY KEY)")
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
            for exchange in ("binance", "bybit", "okx"):
                conn.execute(
                    "INSERT INTO realtime_market_features VALUES(?, 'BTCUSDT', ?, 60)",
                    (exchange, now - 120),
                )
            conn.commit()

    def test_ready_when_runtime_databases_and_all_exchanges_are_fresh(self) -> None:
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

    def test_derivatives_validation_reports_partial_and_invalid_configuration(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            partial = self.make_settings(root)
            partial = Settings(**{
                **partial.__dict__,
                "coinalyze_enable": True,
                "coinalyze_api_key": "configured",
            })
            partial_check = next(
                item for item in runtime_health_checks(partial, JsonStore(root), now_ts=10_000)
                if item["name"] == "derivatives_validation"
            )

            invalid = Settings(**{
                **partial.__dict__,
                "coinglass_enable": True,
                "coinglass_api_key": "",
            })
            invalid_check = next(
                item for item in runtime_health_checks(invalid, JsonStore(root), now_ts=10_000)
                if item["name"] == "derivatives_validation"
            )

        self.assertEqual(partial_check["status"], "warn")
        self.assertEqual(invalid_check["status"], "fail")

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


if __name__ == "__main__":
    unittest.main()
