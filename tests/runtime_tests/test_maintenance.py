from __future__ import annotations

import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from config import Settings
from runtime.maintenance import (
    cleanup_generated_root_artifacts,
    cleanup_runtime_artifacts,
)
from shared.storage import JsonStore


class MaintenanceTests(unittest.TestCase):
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
                cleanup_state_path=data / "cleanup_state.json",
                cleanup_interval_sec=3600,
                tg_push_history_limit=100,
            )
            store = JsonStore(data)
            now = int(time.time())
            store.save(settings.tg_push_history_path, [
                {"ts": 1, "status": "sent", "template_id": "TG_LAUNCH_ALERT"},
                {"ts": now, "status": "sent", "template_id": "TG_LAUNCH_ALERT"},
            ])
            pulse_state_path = data / "simple_alert_state.json"
            pulse_state = {"BTCUSDT": {"template": "health_up", "count": 1}}
            store.save(pulse_state_path, pulse_state)

            result = cleanup_runtime_artifacts(settings, store, force=True)

            self.assertFalse(cache.exists())
            self.assertFalse(old_tmp.exists())
            self.assertFalse(result["skipped"])
            self.assertEqual(
                store.load(settings.tg_push_history_path, []),
                [{"ts": now, "status": "sent", "template_id": "TG_LAUNCH_ALERT"}],
            )
            self.assertEqual(store.load(pulse_state_path, {}), pulse_state)
            self.assertEqual(result["signal_database"]["status"], "ok")
            self.assertTrue(settings.cleanup_state_path.exists())

    def test_cleanup_generated_root_artifacts_removes_reports_only(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            generated = [
                base / "PROJECT_CURRENT_SUMMARY.md",
                base / "UPGRADE_TEST.md",
                base / "SOME_REPORT.md",
                base / "SOME_SUMMARY.txt",
            ]
            keep = [
                base / "README.md",
                base / "requirements.txt",
                base / ".env.oi",
            ]
            docs = base / "docs"
            docs.mkdir()
            docs_report = docs / "KEEP_REPORT.md"
            for path in generated + keep + [docs_report]:
                path.write_text("x", encoding="utf-8")

            result = cleanup_generated_root_artifacts(base)

            self.assertEqual(result["deleted"], len(generated))
            for path in generated:
                self.assertFalse(path.exists())
            for path in keep + [docs_report]:
                self.assertTrue(path.exists())

    def test_cleanup_removes_retired_signal_records_from_telegram_history(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            data = base / "data"
            settings = Settings(
                base_dir=base,
                data_dir=data,
                tg_push_history_path=data / "tg_push_history.json",
                cleanup_state_path=data / "cleanup_state.json",
            )
            store = JsonStore(data)
            now = int(time.time())
            active = {"ts": now, "template_id": "TG_FLOW_RADAR", "symbol": "BTCUSDT"}
            retired = {"ts": now, "template_id": "TG_RETIRED_FEATURE", "symbol": "BTCUSDT"}
            store.save(settings.tg_push_history_path, [active, retired])

            cleanup_runtime_artifacts(settings, store, force=True)

            self.assertEqual(store.load(settings.tg_push_history_path, []), [active])


if __name__ == "__main__":
    unittest.main()
