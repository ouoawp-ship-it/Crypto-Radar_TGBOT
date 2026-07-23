from __future__ import annotations

import json
import os
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.config import Settings
from paopao_radar.database_backup import backup_databases


class DatabaseBackupTests(unittest.TestCase):
    @staticmethod
    def seed(path: Path, value: str) -> None:
        with closing(sqlite3.connect(path)) as conn:
            conn.execute("CREATE TABLE facts(value TEXT NOT NULL)")
            conn.execute("INSERT INTO facts(value) VALUES(?)", (value,))
            conn.commit()

    def test_backup_is_consistent_restorable_and_prunes_old_sets(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            backup_root = root / "backups"
            signals = root / "signals.db"
            market = root / "market_snapshots.db"
            self.seed(signals, "signal")
            self.seed(market, "market")
            old_set = backup_root / "20230101T000000Z"
            old_set.mkdir(parents=True)
            old_ts = 1_800_000_000 - 8 * 86_400
            os.utime(old_set, (old_ts, old_ts))
            settings = Settings(
                base_dir=root,
                data_dir=root,
                signal_events_db_path=signals,
                market_snapshots_db_path=market,
                realtime_features_db_path=root / "realtime_features.db",
                news_events_db_path=root / "news_events.db",
                database_backup_dir=backup_root,
                database_backup_retention_days=7,
            )

            result = backup_databases(settings, now_ts=1_800_000_000)

            manifest_path = Path(result["backup_set"]) / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            with closing(sqlite3.connect(signals)) as conn:
                source_value = conn.execute("SELECT value FROM facts").fetchone()[0]

        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["databases"]), 2)
        self.assertEqual(result["pruned_sets"], ["20230101T000000Z"])
        self.assertEqual(source_value, "signal")
        self.assertTrue(all(item["integrity"] == "ok" for item in manifest["databases"]))
        self.assertTrue(all(item["restore_verification"] == "ok" for item in manifest["databases"]))
        self.assertEqual(sorted(manifest["skipped"]), ["news_events.db", "realtime_features.db"])


if __name__ == "__main__":
    unittest.main()
