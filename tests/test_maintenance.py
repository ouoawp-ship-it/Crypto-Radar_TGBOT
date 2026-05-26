from __future__ import annotations

import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.config import Settings
from paopao_radar.maintenance import cleanup_runtime_artifacts, cleanup_structure_charts, migrate_legacy_state
from paopao_radar.storage import JsonStore


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

    def test_cleanup_structure_charts_deletes_old_and_over_limit_png_only(self) -> None:
        with TemporaryDirectory() as tmp:
            chart_dir = Path(tmp) / "data" / "charts"
            chart_dir.mkdir(parents=True)
            old_png = chart_dir / "old.png"
            newest_png = chart_dir / "newest.png"
            overflow_png = chart_dir / "overflow.png"
            state = chart_dir.parent / "structure_state.json"
            history = chart_dir.parent / "structure_history.json"
            report = chart_dir.parent / "structure_report.txt"
            for path in (old_png, newest_png, overflow_png):
                path.write_bytes(b"\x89PNG\r\n\x1a\n")
            state.write_text("{}", encoding="utf-8")
            history.write_text("[]", encoding="utf-8")
            report.write_text("report", encoding="utf-8")
            now = time.time()
            os.utime(old_png, (now - 48 * 3600, now - 48 * 3600))
            os.utime(overflow_png, (now - 100, now - 100))
            os.utime(newest_png, (now, now))

            result = cleanup_structure_charts(chart_dir, retention_hours=12, max_files=1)

            self.assertEqual(result["scanned"], 3)
            self.assertEqual(result["deleted_old"], 1)
            self.assertEqual(result["deleted_over_limit"], 1)
            self.assertFalse(old_png.exists())
            self.assertFalse(overflow_png.exists())
            self.assertTrue(newest_png.exists())
            self.assertTrue(state.exists())
            self.assertTrue(history.exists())
            self.assertTrue(report.exists())

    def test_cleanup_runtime_artifacts_includes_structure_charts(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            data = base / "data"
            chart_dir = data / "charts"
            chart_dir.mkdir(parents=True)
            chart = chart_dir / "old.png"
            chart.write_bytes(b"\x89PNG\r\n\x1a\n")
            old_ts = time.time() - 48 * 3600
            os.utime(chart, (old_ts, old_ts))
            settings = Settings(
                base_dir=base,
                data_dir=data,
                cleanup_state_path=data / "cleanup_state.json",
                tg_push_history_path=data / "tg_push_history.json",
                launch_watch_history_path=data / "launch_watch_history.json",
                structure_chart_dir=chart_dir,
                structure_chart_retention_hours=12,
                structure_max_chart_files=200,
            )
            store = JsonStore(data)

            result = cleanup_runtime_artifacts(settings, store, force=True)

            self.assertFalse(chart.exists())
            self.assertEqual(result["structure_charts"]["deleted_old"], 1)


if __name__ == "__main__":
    unittest.main()
