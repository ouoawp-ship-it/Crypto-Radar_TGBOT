from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from paopao_radar.atomic_json import (
    append_jsonl,
    atomic_write_text,
    locked_read_json,
    locked_update_json,
    locked_write_json,
)
from paopao_radar.config import Settings
from paopao_radar.storage import JsonStore
from paopao_radar.symbol_dossier import append_signal_events
from paopao_radar.telegram import TelegramGateway


class AtomicJsonTests(unittest.TestCase):
    def test_failed_replace_leaves_complete_original_file(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            locked_write_json(path, {"generation": "old", "items": list(range(20))})

            with patch("paopao_radar.atomic_json.os.replace", side_effect=OSError("interrupted")):
                with self.assertRaises(OSError):
                    atomic_write_text(path, '{"generation":"new"}')

            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["generation"], "old")
            self.assertFalse(path.with_name(f"{path.name}.tmp.{os.getpid()}").exists())

    def test_locked_update_is_safe_across_processes(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "counter.json"
            locked_write_json(path, {"count": 0})
            script = (
                "import sys\n"
                "from paopao_radar.atomic_json import locked_update_json\n"
                "path = sys.argv[1]\n"
                "for _ in range(20):\n"
                "    def increment(value):\n"
                "        return {'count': int(value.get('count', 0)) + 1}\n"
                "    locked_update_json(path, increment, {'count': 0})\n"
            )
            processes = [
                subprocess.Popen([sys.executable, "-c", script, str(path)])
                for _ in range(4)
            ]
            return_codes = [process.wait(timeout=30) for process in processes]

            self.assertEqual(return_codes, [0, 0, 0, 0])
            self.assertEqual(locked_read_json(path, {}), {"count": 80})

    def test_updater_error_does_not_pollute_cache_file(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            locked_write_json(path, {"ok": True})

            def fail(_current: object) -> object:
                raise RuntimeError("loader failed")

            with self.assertRaises(RuntimeError):
                locked_update_json(path, fail, {})

            self.assertEqual(locked_read_json(path, {}), {"ok": True})

    def test_update_quarantines_a_corrupt_document(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            path.write_text("{broken", encoding="utf-8")

            locked_update_json(path, lambda value: {**value, "recovered": True}, {})

            self.assertEqual(locked_read_json(path, {}), {"recovered": True})
            self.assertEqual(len(list(Path(tmp).glob("state.json.corrupt.*"))), 1)

    def test_append_jsonl_accepts_legacy_array_and_caps_lines(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.jsonl"
            path.write_text(json.dumps([{"id": 1}, {"id": 2}]), encoding="utf-8")

            append_jsonl(path, {"id": 3}, max_lines=2)

            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(records, [{"id": 2}, {"id": 3}])

    def test_json_store_appends_to_legacy_array_with_cap(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "history.json"
            path.write_text(json.dumps([{"id": 1}, {"id": 2}]), encoding="utf-8")
            store = JsonStore(Path(tmp))

            store.append_record(path, {"id": 3}, limit=2)

            self.assertEqual(store.load(path, []), [{"id": 2}, {"id": 3}])

    def test_concurrent_telegram_history_appends_do_not_lose_records(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "tg_push_history.json"
            settings = Settings(
                data_dir=Path(tmp),
                tg_push_history_path=path,
                tg_push_history_limit=100,
            )
            store = JsonStore(Path(tmp))
            gateway = TelegramGateway(settings, store)
            now = int(time.time())

            def append(index: int) -> None:
                gateway._append_history_record({"ts": now, "dedup_key": f"event-{index}"})

            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(append, range(80)))

            history = store.load(path, [])
            self.assertEqual(len(history), 80)
            self.assertEqual(len({record["dedup_key"] for record in history}), 80)

    def test_telegram_history_has_a_hard_cap(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "tg_push_history.json"
            settings = Settings(
                data_dir=Path(tmp),
                tg_push_history_path=path,
                tg_push_history_limit=5000,
            )
            store = JsonStore(Path(tmp))
            now = int(time.time())
            store.save(path, [{"ts": now, "dedup_key": f"old-{index}"} for index in range(1000)])

            TelegramGateway(settings, store)._append_history_record({"ts": now, "dedup_key": "new"})

            history = store.load(path, [])
            self.assertEqual(len(history), 1000)
            self.assertEqual(history[0]["dedup_key"], "old-1")
            self.assertEqual(history[-1]["dedup_key"], "new")

    def test_telegram_history_compaction_keeps_sent_decision_records(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "tg_push_history.json"
            settings = Settings(
                data_dir=Path(tmp),
                tg_push_history_path=path,
                tg_push_history_limit=5000,
            )
            store = JsonStore(Path(tmp))
            now = int(time.time())
            sent = {
                "ts": now,
                "dedup_key": "keep-sent",
                "template_id": "TG_FLOW_RADAR",
                "status": "sent",
            }
            skipped = [
                {"ts": now, "dedup_key": f"skip-{index}", "status": "skipped"}
                for index in range(1100)
            ]
            store.save(path, [sent, *skipped])

            gateway = TelegramGateway(settings, store)
            gateway._append_history_record({"ts": now, "dedup_key": "new-skip", "status": "skipped"})

            history = store.load(path, [])
            self.assertEqual(len(history), 1000)
            self.assertTrue(gateway._recent_match(history, "keep-sent", 3600))
            self.assertEqual(history[-1]["dedup_key"], "new-skip")

    def test_concurrent_legacy_signal_history_appends_are_capped(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "signal_events.json"
            settings = Settings(
                data_dir=Path(tmp),
                signal_events_path=path,
                signal_events_limit=100,
            )
            store = JsonStore(Path(tmp))
            now = int(time.time())

            def append(index: int) -> None:
                append_signal_events(settings, store, [{"ts": now, "id": f"signal-{index}"}])

            with ThreadPoolExecutor(max_workers=8) as executor:
                list(executor.map(append, range(120)))

            history = store.load(path, [])
            self.assertEqual(len(history), 100)
            self.assertEqual(len({record["id"] for record in history}), 100)

    def test_legacy_signal_history_has_a_hard_cap(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "signal_events.json"
            settings = Settings(
                data_dir=Path(tmp),
                signal_events_path=path,
                signal_events_limit=5000,
            )
            store = JsonStore(Path(tmp))
            now = int(time.time())
            store.save(path, [{"ts": now, "id": f"old-{index}"} for index in range(500)])

            append_signal_events(settings, store, [{"ts": now, "id": "new"}])

            history = store.load(path, [])
            self.assertEqual(len(history), 500)
            self.assertEqual(history[0]["id"], "old-1")
            self.assertEqual(history[-1]["id"], "new")


if __name__ == "__main__":
    unittest.main()
