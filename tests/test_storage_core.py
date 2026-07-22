from __future__ import annotations


# Source group: test_atomic_json.py

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


# Source group: test_storage.py

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.storage import JsonStore


class JsonStoreTests(unittest.TestCase):
    def test_save_and_load_json(self) -> None:
        with TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp))
            path = Path(tmp) / "state.json"

            store.save(path, {"symbol": "BTCUSDT", "count": 2})

            self.assertEqual(store.load(path, {}), {"symbol": "BTCUSDT", "count": 2})

    def test_corrupt_json_is_renamed_and_default_returned(self) -> None:
        with TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp))
            path = Path(tmp) / "state.json"
            path.write_text("{bad json", encoding="utf-8")

            self.assertEqual(store.load(path, {"ok": False}), {"ok": False})
            self.assertFalse(path.exists())
            self.assertTrue(list(Path(tmp).glob("state.json.corrupt.*")))


if __name__ == "__main__":
    unittest.main()


# Source group: test_runtime_cache.py

import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from paopao_radar import web
from paopao_radar.config import Settings
from paopao_radar.runtime_cache import RuntimeCache, clear
from paopao_radar.web_services import jobs, ops


class RuntimeCacheTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear()

    def test_ttl_hit_does_not_repeat_loader_and_expiry_refreshes(self) -> None:
        now = [100.0]
        cache = RuntimeCache(clock=lambda: now[0])
        loader = Mock(side_effect=["first", "second"])

        self.assertEqual(cache.get_or_set("key", 5, loader), "first")
        self.assertEqual(cache.get_or_set("key", 5, loader), "first")
        self.assertEqual(loader.call_count, 1)

        now[0] = 105.0
        self.assertEqual(cache.get_or_set("key", 5, loader), "second")
        self.assertEqual(loader.call_count, 2)
        self.assertEqual(cache.stats()["loads"], 2)

    def test_loader_error_does_not_pollute_cache(self) -> None:
        cache = RuntimeCache()
        loader = Mock(side_effect=[RuntimeError("boom"), "recovered"])

        with self.assertRaisesRegex(RuntimeError, "boom"):
            cache.get_or_set("key", 30, loader)

        self.assertEqual(cache.get_or_set("key", 30, loader), "recovered")
        self.assertEqual(loader.call_count, 2)
        self.assertEqual(cache.stats()["load_errors"], 1)

    def test_prefix_invalidate_and_clear(self) -> None:
        cache = RuntimeCache()
        cache.get_or_set("dashboard:service:a", 30, lambda: 1)
        cache.get_or_set("dashboard:git", 30, lambda: 2)
        cache.get_or_set("other:key", 30, lambda: 3)

        self.assertEqual(cache.invalidate("dashboard:"), 2)
        self.assertEqual(cache.stats()["entries"], 1)
        self.assertEqual(cache.clear(), 1)
        self.assertEqual(cache.stats()["entries"], 0)

    def test_concurrent_callers_share_one_loader(self) -> None:
        cache = RuntimeCache()
        loader_started = threading.Event()
        release_loader = threading.Event()
        results: list[str] = []
        load_count = 0
        count_lock = threading.Lock()

        def loader() -> str:
            nonlocal load_count
            with count_lock:
                load_count += 1
            loader_started.set()
            release_loader.wait(timeout=2)
            return "value"

        def worker() -> None:
            results.append(cache.get_or_set("shared", 30, loader))

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        self.assertTrue(loader_started.wait(timeout=1))
        release_loader.set()
        for thread in threads:
            thread.join(timeout=2)

        self.assertEqual(results, ["value"] * 6)
        self.assertEqual(load_count, 1)
        self.assertGreaterEqual(cache.stats()["waits"], 1)

    def test_capacity_is_bounded_and_recent_hits_survive_eviction(self) -> None:
        cache = RuntimeCache(max_entries=2)
        cache.get_or_set("first", 30, lambda: "first")
        cache.get_or_set("second", 30, lambda: "second")
        self.assertEqual(cache.get_or_set("first", 30, lambda: "unexpected"), "first")

        cache.get_or_set("third", 30, lambda: "third")
        stats = cache.stats()

        self.assertEqual(stats["entries"], 2)
        self.assertEqual(stats["max_entries"], 2)
        self.assertEqual(stats["evictions"], 1)
        second_loader = Mock(return_value="second-reloaded")
        self.assertEqual(cache.get_or_set("second", 30, second_loader), "second-reloaded")
        second_loader.assert_called_once_with()

    def test_unrelated_insert_prunes_expired_entries(self) -> None:
        now = [100.0]
        cache = RuntimeCache(clock=lambda: now[0], max_entries=10)
        cache.get_or_set("old-a", 1, lambda: "a")
        cache.get_or_set("old-b", 1, lambda: "b")

        now[0] = 102.0
        cache.get_or_set("new", 30, lambda: "new")
        stats = cache.stats()

        self.assertEqual(stats["entries"], 1)
        self.assertEqual(stats["expired_pruned"], 2)


class DashboardRuntimeCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        clear()

    def tearDown(self) -> None:
        clear()

    @staticmethod
    def _fake_web_subprocess(argv: list[str], **_kwargs: object) -> dict[str, object]:
        if argv[:2] == ["systemctl", "is-active"]:
            stdout = "active\n"
        elif argv[:2] == ["systemctl", "is-enabled"]:
            stdout = "enabled\n"
        elif argv[:3] == ["git", "rev-parse", "--short"]:
            stdout = "abc1234\n"
        elif argv[:2] == ["git", "log"]:
            stdout = "cached dashboard\n"
        elif argv[:2] == ["git", "branch"]:
            stdout = "main\n"
        else:
            stdout = "ok\n"
        return {"ok": True, "returncode": 0, "stdout": stdout, "stderr": "", "command": " ".join(argv)}

    @staticmethod
    def _fake_ops_subprocess(*_args: object, **_kwargs: object) -> object:
        return type("Completed", (), {"returncode": 0, "stdout": "abc1234\n"})()

    def test_cached_service_dict_cannot_be_mutated_by_caller(self) -> None:
        with (
            patch.object(web, "command_exists", return_value=True),
            patch.object(web, "run_subprocess", side_effect=self._fake_web_subprocess),
        ):
            first = web.service_status("paopao-test")
            first["active"] = "tampered"
            second = web.service_status("paopao-test")

        self.assertEqual(second["active"], "active")

    def test_service_action_and_config_save_invalidate_cached_results(self) -> None:
        with (
            patch.object(web, "command_exists", return_value=True),
            patch.object(web, "run_subprocess", side_effect=self._fake_web_subprocess) as run_web,
        ):
            web.service_status(web.MAIN_SERVICE)
            web.service_status(web.MAIN_SERVICE)
            self.assertEqual(run_web.call_count, 2)

            web.run_service_action("restart-main")
            web.service_status(web.MAIN_SERVICE)
            self.assertEqual(run_web.call_count, 5)

            web.git_info()
            git_calls = run_web.call_count
            web.git_info()
            self.assertEqual(run_web.call_count, git_calls)
            with TemporaryDirectory() as tmp:
                env_path = Path(tmp) / ".env.oi"
                env_path.write_text("WEB_PORT=8080\n", encoding="utf-8")
                result = web.write_env_updates({"WEB_PORT": "8081"}, path=env_path)
            self.assertTrue(result["ok"])
            web.git_info()
            self.assertEqual(run_web.call_count, git_calls + 3)

    def test_job_start_and_finish_invalidate_related_cache_prefixes(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), web_jobs_db_path=Path(tmp) / "jobs.db")
            with patch.object(jobs, "invalidate_runtime_cache") as invalidate:
                created = jobs.create_job_payload("api-self-test", settings=settings, start=False)
                store = jobs.store_for_settings(settings)
                finished = jobs.run_job_sync_for_tests(store, int(created["job"]["id"]))

        self.assertEqual(finished["status"], "success")
        prefixes = [call.args[0] for call in invalidate.call_args_list]
        self.assertGreaterEqual(prefixes.count("dashboard:"), 3)


if __name__ == "__main__":
    unittest.main()


# Source group: test_config.py

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from paopao_radar.config import load_env_file


class ConfigLoadTests(unittest.TestCase):
    def test_settings_loads_coinglass_provider_configuration(self) -> None:
        with patch.dict(os.environ, {
            "COINGLASS_ENABLE": "true",
            "COINGLASS_API_KEY": "cg-test-key",
            "COINGLASS_API_BASE_URL": "https://open-api-v4.coinglass.com/",
            "COINGLASS_RATE_LIMIT_PER_MINUTE": "80",
        }):
            settings = Settings.load()

        self.assertTrue(settings.coinglass_enable)
        self.assertEqual(settings.coinglass_api_key, "cg-test-key")
        self.assertEqual(settings.coinglass_api_base_url, "https://open-api-v4.coinglass.com")
        self.assertEqual(settings.coinglass_rate_limit_per_minute, 80)

    def test_load_env_file_overrides_empty_process_value_with_file_value(self) -> None:
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env.oi"
            env_path.write_text("WEB_ADMIN_TOKEN=file-token\n", encoding="utf-8")

            with patch.dict(os.environ, {"WEB_ADMIN_TOKEN": ""}):
                env = load_env_file(env_path)

                self.assertEqual(env["WEB_ADMIN_TOKEN"], "file-token")
                self.assertEqual(os.environ["WEB_ADMIN_TOKEN"], "file-token")

    def test_load_env_file_preserves_non_empty_process_value(self) -> None:
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env.oi"
            env_path.write_text("WEB_ADMIN_TOKEN=file-token\n", encoding="utf-8")

            with patch.dict(os.environ, {"WEB_ADMIN_TOKEN": "process-token"}):
                env = load_env_file(env_path)

                self.assertEqual(env["WEB_ADMIN_TOKEN"], "file-token")
                self.assertEqual(os.environ["WEB_ADMIN_TOKEN"], "process-token")


if __name__ == "__main__":
    unittest.main()
