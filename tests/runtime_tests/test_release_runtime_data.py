from __future__ import annotations

import importlib.util
import json
import os
from contextlib import closing
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[2]


def load_module():
    path = ROOT / "scripts" / "release_runtime_data.py"
    spec = importlib.util.spec_from_file_location("release_runtime_data", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReleaseRuntimeDataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()

    def test_nested_state_and_sqlite_sidecars_round_trip(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            backup_root = data / "release-backups"
            backup = backup_root / "set" / "data"
            nested = data / "altcoin" / "state"
            nested.mkdir(parents=True)
            (nested / "cursor.json").write_text('{"cursor": 7}\n', encoding="utf-8")
            (nested / "process.lock").write_bytes(b"")
            (nested / "audit-wal").write_text("ordinary-file", encoding="utf-8")
            database = data / "db" / "runtime.db"
            database.parent.mkdir()
            source_connection = sqlite3.connect(database)
            self.addCleanup(source_connection.close)
            source_connection.execute("PRAGMA journal_mode=WAL")
            source_connection.execute("PRAGMA wal_autocheckpoint=0")
            source_connection.execute("CREATE TABLE state (value TEXT NOT NULL)")
            source_connection.execute("INSERT INTO state VALUES ('before')")
            source_connection.commit()
            self.assertTrue(Path(f"{database}-wal").exists())
            self.assertTrue(Path(f"{database}-shm").exists())
            backup_root.mkdir()
            (backup_root / "must-not-copy.txt").write_text(
                "excluded",
                encoding="utf-8",
            )

            inventory = self.module.backup_runtime_data(
                data,
                backup,
                exclude_root=backup_root,
            )
            source_connection.close()

            copied = {entry["path"] for entry in inventory["files"]}
            self.assertIn("altcoin/state/cursor.json", copied)
            self.assertIn("altcoin/state/process.lock", copied)
            self.assertIn("altcoin/state/audit-wal", copied)
            self.assertIn("db/runtime.db", copied)
            self.assertNotIn("db/runtime.db-wal", copied)
            self.assertNotIn("db/runtime.db-shm", copied)
            self.assertFalse(any("release-backups" in value for value in copied))
            self.assertEqual(inventory["sqlite_files"], ["db/runtime.db"])
            self.assertEqual(
                inventory["sqlite_sidecars_excluded"],
                ["db/runtime.db-shm", "db/runtime.db-wal"],
            )

            (nested / "cursor.json").write_text('{"cursor": 99}\n', encoding="utf-8")
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("UPDATE state SET value = 'after'")
                connection.commit()
            Path(f"{database}-wal").write_bytes(b"wal-after")
            Path(f"{database}-shm").write_bytes(b"shm-after")

            restored = self.module.restore_runtime_data(backup, data)

            self.assertEqual(restored["sqlite_files"], 1)
            self.assertEqual(
                (nested / "cursor.json").read_text(encoding="utf-8"),
                '{"cursor": 7}\n',
            )
            self.assertEqual((nested / "process.lock").read_bytes(), b"")
            with closing(sqlite3.connect(database)) as connection:
                value = connection.execute("SELECT value FROM state").fetchone()[0]
                integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
            self.assertEqual(value, "before")
            self.assertEqual(integrity, "ok")
            self.assertFalse(Path(f"{database}-wal").exists())
            self.assertFalse(Path(f"{database}-shm").exists())

    def test_restore_rejects_tampered_nested_file(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            backup = root / "backup"
            data.mkdir()
            (data / "state.json").write_text("before", encoding="utf-8")
            self.module.backup_runtime_data(data, backup)
            (backup / "state.json").write_text("tampered", encoding="utf-8")

            with self.assertRaisesRegex(
                self.module.RuntimeDataError,
                "runtime_backup_file_checksum_failed",
            ):
                self.module.restore_runtime_data(backup, data)

    def test_restore_rejects_unlisted_backup_file(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            backup = root / "backup"
            data.mkdir()
            (data / "state.json").write_text("before", encoding="utf-8")
            self.module.backup_runtime_data(data, backup)
            (backup / "unlisted.json").write_text("unexpected", encoding="utf-8")

            with self.assertRaisesRegex(
                self.module.RuntimeDataError,
                "runtime_backup_inventory_tree_mismatch",
            ):
                self.module.restore_runtime_data(backup, data)

    @unittest.skipIf(os.name == "nt", "Windows symlink creation needs privileges")
    def test_backup_rejects_symlink_in_runtime_tree(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            backup = root / "backup"
            data.mkdir()
            target = root / "outside.json"
            target.write_text("secret", encoding="utf-8")
            (data / "linked.json").symlink_to(target)

            with self.assertRaisesRegex(
                self.module.RuntimeDataError,
                "runtime_data_symlink_rejected",
            ):
                self.module.backup_runtime_data(data, backup)

    def test_inventory_cannot_escape_restore_root(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            backup = root / "backup"
            data.mkdir()
            backup.mkdir()
            (backup / self.module.INVENTORY_NAME).write_text(
                json.dumps(
                    {
                        "schema_version": self.module.SCHEMA_VERSION,
                        "directories": [],
                        "files": [
                            {"path": "../escape.json", "bytes": 0, "sha256": ""}
                        ],
                        "sqlite_files": [],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                self.module.RuntimeDataError,
                "runtime_backup_inventory_path_invalid",
            ):
                self.module.restore_runtime_data(backup, data)


if __name__ == "__main__":
    unittest.main()
