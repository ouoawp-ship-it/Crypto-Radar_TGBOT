from __future__ import annotations

import json
import time
import unittest
from argparse import Namespace
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from paopao_radar import cli, web
from paopao_radar.config import Settings
from paopao_radar.web_services import jobs
from paopao_radar.web_services.lifecycle_outcomes import (
    public_lifecycle_outcome_coverage_payload,
    public_lifecycle_outcome_detail_payload,
    public_lifecycle_outcome_summary_payload,
)


BASE_DIR = Path(__file__).resolve().parent.parent


def make_settings(tmp: str, **overrides: object) -> Settings:
    base = Path(tmp)
    values: dict[str, object] = {
        "data_dir": base,
        "lifecycle_db_path": base / "lifecycle.db",
        "outcome_db_path": base / "outcomes.db",
        "signal_events_db_path": base / "signals.db",
        "web_jobs_db_path": base / "jobs.db",
    }
    values.update(overrides)
    return Settings(**values)


def make_handler(path: str) -> tuple[web.WebHandler, list[int]]:
    statuses: list[int] = []
    instance = object.__new__(web.WebHandler)
    instance.path = path
    instance.headers = {}
    instance.server = type("Server", (), {"admin_token": "secret", "settings": Settings(web_auth_mode="password")})()
    instance.wfile = BytesIO()
    instance.send_response = lambda status: statuses.append(status)
    instance.send_header = lambda _key, _value: None
    instance.end_headers = lambda: None
    return instance, statuses


class LifecycleOutcomeSurfaceConfigCliTests(unittest.TestCase):
    def test_frontend_distinguishes_coverage_maturity_and_keeps_outcome_optional(self) -> None:
        lifecycle_page = (BASE_DIR / "frontend/app/lifecycle/page.tsx").read_text(encoding="utf-8")
        replay_page = (BASE_DIR / "frontend/app/lifecycle/replay/page.tsx").read_text(encoding="utf-8")
        coin_page = (BASE_DIR / "frontend/app/coin/[symbol]/page.tsx").read_text(encoding="utf-8")

        self.assertIn("Outcome 关联覆盖率", lifecycle_page)
        self.assertIn("关联覆盖率与数据成熟度分别统计", lifecycle_page)
        self.assertIn("主要 Outcome", replay_page)
        self.assertIn("已成熟周期", replay_page)
        self.assertIn("Outcome 关联卡", coin_page)
        for source in (lifecycle_page, replay_page, coin_page):
            self.assertIn(".catch(() =>", source)

    def test_settings_and_example_defaults(self) -> None:
        settings = Settings()
        self.assertTrue(settings.lifecycle_outcome_backfill_enable)
        self.assertEqual(settings.lifecycle_outcome_backfill_batch_size, 200)
        self.assertEqual(settings.lifecycle_outcome_backfill_max_outcomes, 1000)
        self.assertEqual(settings.lifecycle_outcome_link_time_tolerance_sec, 300)
        self.assertEqual(settings.lifecycle_outcome_backfill_interval_sec, 3600)
        example = (BASE_DIR / ".env.oi.example").read_text(encoding="utf-8")
        sync = (BASE_DIR / "scripts/sync_env.py").read_text(encoding="utf-8")
        for key in (
            "LIFECYCLE_OUTCOME_BACKFILL_ENABLE",
            "LIFECYCLE_OUTCOME_BACKFILL_BATCH_SIZE",
            "LIFECYCLE_OUTCOME_BACKFILL_MAX_OUTCOMES",
            "LIFECYCLE_OUTCOME_LINK_TIME_TOLERANCE_SEC",
            "LIFECYCLE_OUTCOME_BACKFILL_INTERVAL_SEC",
        ):
            self.assertIn(key, example)
            self.assertIn(key, sync)

    def test_cli_parser_exposes_commands_and_bounded_options(self) -> None:
        parser = cli.build_parser()
        for command in (
            "lifecycle-outcome-link", "lifecycle-outcome-backfill",
            "lifecycle-outcome-status", "lifecycle-outcome-reconcile",
        ):
            args = parser.parse_args([
                command, "--symbol", "BTCUSDT", "--lifecycle-id", "12",
                "--limit", "50", "--horizon", "1h", "--dry-run", "--pretty",
                "--force-relink", "--force-outcome-rebuild", "--repair",
            ])
            self.assertEqual(args.command, command)
            self.assertTrue(args.dry_run)
            self.assertTrue(args.force_relink)
            self.assertTrue(args.force_outcome_rebuild)
            self.assertTrue(args.repair)

    def test_cli_backfill_forwards_dry_run_and_force_flags(self) -> None:
        args = Namespace(
            symbol="BTCUSDT", lifecycle_id=12, limit=50, horizon="4h", dry_run=True,
            pretty=False, force_relink=True, force_outcome_rebuild=True,
        )
        with TemporaryDirectory() as tmp, patch("paopao_radar.cli.Settings.load", return_value=make_settings(tmp)):
            with patch("paopao_radar.lifecycle_outcomes.backfill_lifecycle_outcomes", return_value={"ok": True}) as backfill:
                self.assertEqual(cli.run_lifecycle_outcome_backfill(args), 0)
        self.assertTrue(backfill.call_args.kwargs["dry_run"])
        self.assertTrue(backfill.call_args.kwargs["force_relink"])
        self.assertTrue(backfill.call_args.kwargs["force_outcome_rebuild"])
        self.assertEqual(backfill.call_args.kwargs["horizon"], "4h")


class LifecycleOutcomeSurfaceJobTests(unittest.TestCase):
    def test_job_specs_are_whitelisted_guarded_and_bounded(self) -> None:
        expected = {
            "lifecycle-outcome-link", "lifecycle-outcome-backfill",
            "lifecycle-outcome-reconcile", "lifecycle-outcome-refresh-analytics",
        }
        self.assertTrue(expected.issubset(jobs.JOB_SPECS))
        self.assertTrue(expected.issubset(jobs.CONCURRENT_GUARD_JOB_TYPES))
        self.assertTrue(expected.issubset(jobs.LIFECYCLE_RESEARCH_JOB_TYPES))
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            payload = jobs.create_job_payload(
                "lifecycle-outcome-backfill",
                {
                    "symbol": "btc", "limit": 50000, "horizon": "4h",
                    "force_relink": True, "force_outcome_rebuild": True,
                },
                settings=settings,
                start=False,
            )
        command = payload["job"]["command"]
        self.assertIn("BTCUSDT", command)
        self.assertEqual(command[command.index("--limit") + 1], "1000")
        self.assertIn("--force-relink", command)
        self.assertIn("--force-outcome-rebuild", command)

    def test_outcome_jobs_reuse_active_job(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            first = jobs.create_job_payload("lifecycle-outcome-link", settings=settings, start=False)
            second = jobs.create_job_payload("lifecycle-outcome-link", settings=settings, start=False)
        self.assertTrue(first["ok"])
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertTrue(second["reused"])

    def test_invalid_scope_never_falls_back_to_full_batch(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            invalid_symbol = jobs.create_job_payload(
                "lifecycle-outcome-backfill", {"symbol": "???"}, settings=settings, start=False,
            )
            invalid_id = jobs.create_job_payload(
                "lifecycle-outcome-link", {"lifecycle_id": -9}, settings=settings, start=False,
            )
            invalid_short_symbols = [
                jobs.create_job_payload(
                    "lifecycle-outcome-link", {"symbol": value}, settings=settings, start=False,
                )
                for value in ("USDT", "AUSDT")
            ]
            invalid_flag = jobs.create_job_payload(
                "lifecycle-outcome-backfill",
                {"force_outcome_rebuild": "false"},
                settings=settings,
                start=False,
            )
            store = jobs.store_for_settings(settings)
            created = store.list_jobs(limit=10)
        self.assertFalse(invalid_symbol["ok"])
        self.assertEqual(invalid_symbol["code"], "invalid_job_scope")
        self.assertFalse(invalid_id["ok"])
        self.assertTrue(all(not item["ok"] for item in invalid_short_symbols))
        self.assertFalse(invalid_flag["ok"])
        self.assertEqual(created, [])

    def test_execute_and_rerun_preserve_validated_outcome_scope(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            created = jobs.create_job_payload(
                "lifecycle-outcome-backfill",
                {
                    "symbol": "btc",
                    "limit": 50,
                    "horizon": "4h",
                    "force_relink": True,
                    "force_outcome_rebuild": True,
                },
                settings=settings,
                start=False,
            )
            store = jobs.store_for_settings(settings)
            completed = type("Completed", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()
            with patch("paopao_radar.web_services.jobs.subprocess.run", return_value=completed) as run:
                jobs.execute_job(store, created["job_id"])
            command = run.call_args.args[0]
            rerun = jobs.rerun_job_payload(created["job_id"], settings=settings, start=False)

        self.assertIn("BTCUSDT", command)
        self.assertEqual(command[command.index("--limit") + 1], "50")
        self.assertIn("--horizon", command)
        self.assertIn("--force-relink", command)
        self.assertIn("--force-outcome-rebuild", command)
        self.assertEqual(rerun["job"]["command"], command)

    def test_scheduler_runs_hourly_backfill_and_daily_reconcile_off_main_thread(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(
                tmp,
                lifecycle_intelligence_enable=False,
                lifecycle_outcome_backfill_enable=True,
                lifecycle_outcome_backfill_interval_sec=3600,
            )
            base = int(time.time())
            with patch.object(jobs, "_now", return_value=base):
                first = jobs.lifecycle_intelligence_scheduler_tick(settings=settings, now=base, start=False)
                waiting = jobs.lifecycle_intelligence_scheduler_tick(settings=settings, now=base + 60, start=False)
            with patch.object(jobs, "_now", return_value=base + 3_600):
                hourly = jobs.lifecycle_intelligence_scheduler_tick(settings=settings, now=base + 3_600, start=False)
            store = jobs.store_for_settings(settings)
            store.finish_job(hourly["jobs"][0]["job_id"], status="success", returncode=0)
            with patch.object(jobs, "_now", return_value=base + 86_400):
                next_backfill = jobs.lifecycle_intelligence_scheduler_tick(settings=settings, now=base + 86_400, start=False)
            store.finish_job(next_backfill["jobs"][0]["job_id"], status="success", returncode=0)
            with patch.object(jobs, "_now", return_value=base + 86_460):
                daily = jobs.lifecycle_intelligence_scheduler_tick(settings=settings, now=base + 86_460, start=False)
        self.assertEqual(first["submitted"], [])
        self.assertEqual(waiting["submitted"], [])
        self.assertEqual(hourly["submitted"], ["lifecycle-outcome-backfill"])
        self.assertEqual(next_backfill["submitted"], ["lifecycle-outcome-backfill"])
        self.assertEqual(daily["submitted"], ["lifecycle-outcome-reconcile"])


class LifecycleOutcomeSurfaceApiTests(unittest.TestCase):
    def test_public_summary_and_detail_drop_sensitive_fields(self) -> None:
        status = {
            "ok": True,
            "data": {
                "lifecycle_count": 10,
                "linked_lifecycle_count": 8,
                "token": "secret",
                "database_path": "/private/lifecycle.db",
                "unlinked_reasons": {"not_due": 2},
            },
        }
        detail = {
            "ok": True,
            "data": {
                "symbol": "BTCUSDT",
                "outcome_id": 99,
                "primary_outcome_id": 99,
                "links": [{"horizon": "1h", "outcome_status": "success", "payload_json": "private"}],
            },
        }
        with patch("paopao_radar.web_services.lifecycle_outcomes.lifecycle_outcome_status", return_value=status):
            summary_payload = public_lifecycle_outcome_summary_payload()
        with patch("paopao_radar.web_services.lifecycle_outcomes.lifecycle_outcome_detail", return_value=detail):
            detail_payload = public_lifecycle_outcome_detail_payload("BTCUSDT")
        text = json.dumps({"summary": summary_payload, "detail": detail_payload}, ensure_ascii=False)
        for forbidden in ("token", "database_path", "outcome_id", "primary_outcome_id", "payload_json", "/private"):
            self.assertNotIn(forbidden, text)

    def test_public_coverage_uses_projection_and_pagination(self) -> None:
        result = {
            "ok": True,
            "data": {
                "items": [{
                    "lifecycle_id": 1, "symbol": "BTCUSDT", "maturity_label": "部分成熟",
                    "link_coverage_ratio": 0.8, "payload_json": "private", "outcome_id": 7,
                }],
                "total": 1,
            },
        }
        with patch("paopao_radar.web_services.lifecycle_outcomes.lifecycle_outcome_coverage_list", return_value=result):
            payload = public_lifecycle_outcome_coverage_payload(limit=10)
        item = payload["data"]["items"][0]
        self.assertEqual(item["symbol"], "BTCUSDT")
        self.assertNotIn("payload_json", item)
        self.assertNotIn("outcome_id", item)
        self.assertFalse(payload["data"]["pagination"]["has_more"])

    def test_public_routes_and_private_auth_boundary(self) -> None:
        public, public_status = make_handler("/public-api/lifecycle/outcomes/summary")
        with patch("paopao_radar.web.public_lifecycle_outcome_summary_payload", return_value={"ok": True, "data": {}}):
            web.WebHandler.do_GET(public)
        self.assertEqual(public_status[-1], 200)

        private, private_status = make_handler("/api/lifecycle/outcomes/summary")
        web.WebHandler.do_GET(private)
        self.assertEqual(private_status[-1], 401)

        invalid, invalid_status = make_handler(
            "/public-api/lifecycle/outcomes/coverage?lifecycle_id=abc"
        )
        web.WebHandler.do_GET(invalid)
        self.assertEqual(invalid_status[-1], 400)

    def test_private_job_post_is_authenticated_task_and_returns_job_id(self) -> None:
        instance, _statuses = make_handler("/api/lifecycle/outcomes/run-backfill")
        captured: dict[str, object] = {}
        instance.read_json = lambda: {"limit": 50, "horizon": "1h"}
        instance.send_audited_json = lambda path, data, result, **kwargs: captured.update({
            "path": path, "data": data, "result": result, "kwargs": kwargs,
        })
        with patch.object(web.WebHandler, "require_auth", return_value=True):
            with patch("paopao_radar.web.create_job_payload", return_value={"ok": True, "job_id": 42, "job": {"id": 42}}) as create:
                web.WebHandler.do_POST(instance)
        self.assertEqual(captured["path"], "/api/lifecycle/outcomes/run-backfill")
        self.assertEqual(captured["result"]["job_id"], 42)  # type: ignore[index]
        self.assertEqual(create.call_args.args[0], "lifecycle-outcome-backfill")
        self.assertNotIn("lifecycle_id", create.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
