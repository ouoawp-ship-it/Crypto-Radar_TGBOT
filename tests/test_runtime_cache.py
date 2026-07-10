from __future__ import annotations

import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from paopao_radar import web
from paopao_radar.config import Settings
from paopao_radar.runtime_cache import RuntimeCache, clear
from paopao_radar.web_services import jobs, ops
from paopao_radar.web_services.dashboard import dashboard_payload


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

    def test_repeated_dashboard_reuses_systemctl_and_git_results(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                signal_events_path=Path(tmp) / "signal_events.json",
                signal_events_db_path=Path(tmp) / "signals.db",
                web_jobs_db_path=Path(tmp) / "jobs.db",
            )
            with (
                patch.object(Settings, "load", return_value=settings),
                patch.object(web, "command_exists", return_value=True),
                patch.object(web, "run_subprocess", side_effect=self._fake_web_subprocess) as run_web,
                patch.object(ops.subprocess, "run", side_effect=self._fake_ops_subprocess) as run_ops,
            ):
                first = dashboard_payload(settings=settings)
                first_web_calls = run_web.call_count
                first_ops_calls = run_ops.call_count
                second = dashboard_payload(settings=settings)

        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(first_web_calls, 11)
        self.assertEqual(first_ops_calls, 1)
        self.assertEqual(run_web.call_count, first_web_calls)
        self.assertEqual(run_ops.call_count, first_ops_calls)

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
