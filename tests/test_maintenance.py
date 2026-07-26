from __future__ import annotations

import os
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.config import Settings
from paopao_radar.maintenance import (
    cleanup_generated_root_artifacts,
    cleanup_runtime_artifacts,
    migrate_legacy_state,
)
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
            self.assertEqual(result["signal_database"]["status"], "ok")
            self.assertTrue(settings.cleanup_state_path.exists())

    def test_cleanup_compacts_legacy_launch_analysis_without_losing_signal_facts(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            data = base / "data"
            history_path = data / "launch_watch_history.json"
            state_path = data / "launch_state.json"
            settings = Settings(
                base_dir=base,
                data_dir=data,
                launch_watch_history_path=history_path,
                launch_state_path=state_path,
                cleanup_state_path=data / "cleanup_state.json",
            )
            store = JsonStore(data)
            large_analysis = {
                "publication": {
                    "checkpoints": [
                        {"price_action": {"smc": "x" * 100_000}},
                    ],
                },
            }
            store.save(
                history_path,
                [{
                    "ts": 1,
                    "items": [{
                        "symbol": "TESTUSDT",
                        "score": 75,
                        "launch_lifecycle": large_analysis,
                    }],
                }],
            )
            store.save(
                state_path,
                {
                    "TESTUSDT": {
                        "stage": "breakout",
                        "score": 75,
                        "message_ids": [321],
                        "launch_lifecycle": large_analysis,
                        "launch_package": large_analysis["publication"],
                        "price_action_analysis": {"smc": "x" * 100_000},
                    },
                },
            )

            result = cleanup_runtime_artifacts(settings, store, force=True)

            history = store.load(history_path, [])
            state = store.load(state_path, {})
            self.assertNotIn("launch_lifecycle", history[0]["items"][0])
            self.assertEqual(history[0]["items"][0]["score"], 75)
            self.assertNotIn("launch_lifecycle", state["TESTUSDT"])
            self.assertNotIn("launch_package", state["TESTUSDT"])
            self.assertNotIn("price_action_analysis", state["TESTUSDT"])
            self.assertEqual(state["TESTUSDT"]["message_ids"], [321])
            history_result = next(
                item
                for item in result["pruned"]
                if item["path"] == str(history_path)
            )
            self.assertGreater(history_result["freed_bytes"], 0)
            self.assertGreater(
                result["launch_state_compaction"]["freed_bytes"],
                0,
            )

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

    def test_cleanup_removes_retired_signal_records_from_shared_histories(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            data = base / "data"
            settings = Settings(
                base_dir=base,
                data_dir=data,
                tg_push_history_path=data / "tg_push_history.json",
                signal_events_path=data / "signal_events.json",
                cleanup_state_path=data / "cleanup_state.json",
            )
            store = JsonStore(data)
            now = int(time.time())
            active = {"ts": now, "template_id": "TG_FLOW_RADAR", "symbol": "BTCUSDT"}
            retired = {"ts": now, "template_id": "TG_RETIRED_FEATURE", "symbol": "BTCUSDT"}
            store.save(settings.tg_push_history_path, [active, retired])
            store.save(settings.signal_events_path, [active, retired])

            cleanup_runtime_artifacts(settings, store, force=True)

            self.assertEqual(store.load(settings.tg_push_history_path, []), [active])
            self.assertEqual(store.load(settings.signal_events_path, []), [active])


if __name__ == "__main__":
    unittest.main()
