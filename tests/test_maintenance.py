from __future__ import annotations

import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from config import Settings
from maintenance import cleanup_runtime_artifacts, migrate_legacy_state
from storage import JsonStore


class MaintenanceTests(unittest.TestCase):
    def test_migrate_state_dry_run_does_not_copy(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "bn_signal_history.json").write_text('{"x": 1}', encoding="utf-8")
            settings = Settings(base_dir=base, data_dir=base / "data")

            result = migrate_legacy_state(settings, apply=False)

            self.assertFalse((base / "data" / "bn_signal_history.json").exists())
            actions = {Path(item["source"]).name: item["action"] for item in result["actions"]}
            self.assertEqual(actions["bn_signal_history.json"], "dry_run_copy_available")

    def test_migrate_state_apply_copies_without_deleting_source(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "bn_signal_history.json"
            source.write_text('{"x": 1}', encoding="utf-8")
            settings = Settings(base_dir=base, data_dir=base / "data")

            result = migrate_legacy_state(settings, apply=True)

            target = base / "data" / "bn_signal_history.json"
            self.assertTrue(source.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), '{"x": 1}')
            actions = {Path(item["source"]).name: item["action"] for item in result["actions"]}
            self.assertEqual(actions["bn_signal_history.json"], "copied")

    def test_cleanup_removes_cache_and_prunes_histories_without_touching_state(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            data = base / "data"
            cache = base / "__pycache__"
            cache.mkdir()
            (cache / "main.cpython.pyc").write_text("x", encoding="utf-8")
            data.mkdir()
            old_tmp = data / "state.tmp"
            old_tmp.write_text("tmp", encoding="utf-8")
            old_ts = time.time() - 7200
            os.utime(old_tmp, (old_ts, old_ts))
            settings = Settings(
                base_dir=base,
                data_dir=data,
                tg_push_history_path=data / "tg_push_history.json",
                launch_watch_history_path=data / "launch_watch_history.json",
                cleanup_state_path=data / "cleanup_state.json",
                cleanup_interval_sec=3600,
                launch_watch_history_limit=2,
                tg_push_history_limit=2,
            )
            store = JsonStore(data)
            store.save(settings.tg_push_history_path, [
                {"ts": 1, "status": "sent"},
                {"ts": 2, "status": "sent"},
                {"ts": 3, "status": "sent"},
            ])
            store.save(settings.launch_watch_history_path, [
                {"updated_at": "1"},
                {"updated_at": "2"},
                {"updated_at": "3"},
            ])

            result = cleanup_runtime_artifacts(settings, store, force=True)

            self.assertFalse(cache.exists())
            self.assertFalse(old_tmp.exists())
            self.assertFalse(result["skipped"])
            self.assertEqual(len(store.load(settings.launch_watch_history_path, [])), 2)
            self.assertTrue(settings.cleanup_state_path.exists())


if __name__ == "__main__":
    unittest.main()
