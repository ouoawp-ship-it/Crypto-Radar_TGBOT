from __future__ import annotations

import json
import time
import unittest
from argparse import Namespace
from io import BytesIO, StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from paopao_radar import cli, web
from paopao_radar.config import Settings
from paopao_radar.web_services import jobs
from paopao_radar.web_services.model_optimization import (
    optimization_report_payload,
    public_optimization_section_payload,
)


BASE_DIR = Path(__file__).resolve().parent.parent
SCENARIOS = (
    "threshold_tuning",
    "risk_control",
    "lifecycle_quality",
    "module_rebalance",
)


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


def cli_args(**overrides: object) -> Namespace:
    values: dict[str, object] = {
        "scenario": "",
        "symbol": "",
        "limit": None,
        "dry_run": False,
        "pretty": False,
        "force": False,
        "force_rebuild": False,
    }
    values.update(overrides)
    return Namespace(**values)


def make_handler(path: str) -> tuple[web.WebHandler, list[int]]:
    statuses: list[int] = []
    instance = object.__new__(web.WebHandler)
    instance.path = path
    instance.headers = {}
    instance.server = type(
        "Server", (), {"admin_token": "secret", "settings": Settings(web_auth_mode="password")}
    )()
    instance.wfile = BytesIO()
    instance.send_response = lambda status: statuses.append(status)
    instance.send_header = lambda _key, _value: None
    instance.end_headers = lambda: None
    return instance, statuses


class ModelOptimizationConfigCliTests(unittest.TestCase):
    def test_settings_and_env_defaults_keep_optimization_manual(self) -> None:
        settings = Settings()
        self.assertFalse(settings.model_optimization_enable)
        self.assertEqual(settings.model_optimization_interval_sec, 21600)
        self.assertEqual(settings.model_optimization_cache_ttl_sec, 30)
        example = (BASE_DIR / ".env.oi.example").read_text(encoding="utf-8")
        sync = (BASE_DIR / "scripts/sync_env.py").read_text(encoding="utf-8")
        for key in (
            "MODEL_OPTIMIZATION_ENABLE",
            "MODEL_OPTIMIZATION_INTERVAL_SEC",
            "MODEL_OPTIMIZATION_CACHE_TTL_SEC",
        ):
            self.assertIn(key, example)
            self.assertIn(key, sync)

    def test_parser_exposes_commands_and_scenario_scope(self) -> None:
        parser = cli.build_parser()
        for command in (
            "optimization-scenarios",
            "optimization-run",
            "optimization-report",
            "optimization-readiness",
        ):
            args = parser.parse_args([
                command,
                "--scenario", "risk_control",
                "--symbol", "BTCUSDT",
                "--limit", "200",
                "--dry-run",
                "--pretty",
            ])
            self.assertEqual(args.command, command)
            self.assertEqual(args.scenario, "risk_control")
            self.assertTrue(args.dry_run)

    def test_default_global_run_is_full_history_and_writes_report(self) -> None:
        run = Mock(return_value={"ok": True, "status": "complete"})
        with TemporaryDirectory() as tmp, patch(
            "paopao_radar.cli.Settings.load", return_value=make_settings(tmp)
        ), patch(
            "paopao_radar.cli._optimizer_core_function", return_value=run
        ), patch("sys.stdout", new_callable=StringIO):
            code = cli.run_optimization(cli_args())
        self.assertEqual(code, 0)
        self.assertNotIn("limit", run.call_args.kwargs)
        self.assertFalse(run.call_args.kwargs["dry_run"])
        self.assertTrue(run.call_args.kwargs["write_reports"])

    def test_scoped_dry_run_is_in_memory_and_never_writes(self) -> None:
        run = Mock(return_value={"ok": True, "status": "dry_run"})
        with TemporaryDirectory() as tmp, patch(
            "paopao_radar.cli.Settings.load", return_value=make_settings(tmp)
        ), patch(
            "paopao_radar.cli._optimizer_core_function", return_value=run
        ), patch("sys.stdout", new_callable=StringIO):
            code = cli.run_optimization(cli_args(
                scenario="threshold_tuning",
                symbol="BTCUSDT",
                limit=50000,
                dry_run=True,
            ))
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.kwargs["limit"], 10000)
        self.assertEqual(run.call_args.kwargs["symbol"], "BTCUSDT")
        self.assertTrue(run.call_args.kwargs["dry_run"])
        self.assertFalse(run.call_args.kwargs["write_reports"])

    def test_symbol_without_dry_run_is_rejected_before_core(self) -> None:
        resolve = Mock()
        with patch(
            "paopao_radar.cli._optimizer_core_function", resolve
        ), patch("sys.stdout", new_callable=StringIO):
            code = cli.run_optimization(cli_args(symbol="BTCUSDT"))
        self.assertNotEqual(code, 0)
        resolve.assert_not_called()

    def test_report_aggregates_persisted_runs_without_simulation(self) -> None:
        generate = Mock(return_value={"ok": True, "status": "dry_run"})
        names: list[str] = []

        def resolve(name: str) -> object:
            names.append(name)
            return generate

        with TemporaryDirectory() as tmp, patch(
            "paopao_radar.cli.Settings.load", return_value=make_settings(tmp)
        ), patch(
            "paopao_radar.cli._optimizer_core_function", side_effect=resolve
        ), patch("sys.stdout", new_callable=StringIO):
            code = cli.run_optimization_report(cli_args(dry_run=True))
        self.assertEqual(code, 0)
        self.assertEqual(names, ["generate_optimization_report"])
        self.assertFalse(generate.call_args.kwargs["write_reports"])
        self.assertNotIn("limit", generate.call_args.kwargs)


class ModelOptimizationJobTests(unittest.TestCase):
    def test_job_specs_use_shared_research_lock_and_full_history_default(self) -> None:
        expected = {"optimization-run", "optimization-rebuild"}
        self.assertTrue(expected.issubset(jobs.JOB_SPECS))
        self.assertTrue(expected.issubset(jobs.CONCURRENT_GUARD_JOB_TYPES))
        self.assertTrue(expected.issubset(jobs.LIFECYCLE_RESEARCH_JOB_TYPES))
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            created = jobs.create_job_payload(
                "optimization-run", settings=settings, start=False
            )
            store = jobs.store_for_settings(settings)
            store.finish_job(created["job_id"], status="success", returncode=0)
            rebuilt = jobs.create_job_payload(
                "optimization-rebuild", settings=settings, start=False
            )
        self.assertNotIn("--limit", created["job"]["command"])
        self.assertIn("--force", rebuilt["job"]["command"])

    def test_scenario_and_explicit_limit_are_validated_and_rerunnable(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            created = jobs.create_job_payload(
                "optimization-run",
                {"scenario": "risk_control", "limit": 50000, "force": True},
                settings=settings,
                start=False,
            )
            store = jobs.store_for_settings(settings)
            completed = type(
                "Completed", (), {"returncode": 0, "stdout": "ok", "stderr": ""}
            )()
            with patch(
                "paopao_radar.web_services.jobs.subprocess.run", return_value=completed
            ) as run:
                jobs.execute_job(store, created["job_id"])
            command = run.call_args.args[0]
            rerun = jobs.rerun_job_payload(
                created["job_id"], settings=settings, start=False
            )
        self.assertEqual(command[command.index("--scenario") + 1], "risk_control")
        self.assertEqual(command[command.index("--limit") + 1], "10000")
        self.assertIn("--force", command)
        self.assertEqual(rerun["job"]["command"], command)

    def test_invalid_or_symbol_scoped_jobs_never_create_rows(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            invalid = [
                jobs.create_job_payload(
                    "optimization-run", {"scenario": "unknown"}, settings=settings, start=False
                ),
                jobs.create_job_payload(
                    "optimization-run", {"symbol": "BTCUSDT"}, settings=settings, start=False
                ),
                jobs.create_job_payload(
                    "optimization-run", {"limit": True}, settings=settings, start=False
                ),
                jobs.create_job_payload(
                    "optimization-rebuild", {"force": "false"}, settings=settings, start=False
                ),
            ]
            rows = jobs.store_for_settings(settings).list_jobs(limit=10)
        self.assertTrue(all(not item["ok"] for item in invalid))
        self.assertTrue(all(item["code"] == "invalid_job_scope" for item in invalid))
        self.assertEqual(rows, [])

    def test_duplicate_submission_reuses_active_job(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            first = jobs.create_job_payload(
                "optimization-run", settings=settings, start=False
            )
            second = jobs.create_job_payload(
                "optimization-run", settings=settings, start=False
            )
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["job_id"], second["job_id"])

    def test_disabled_by_default_scheduler_waits_full_interval_when_enabled(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(
                tmp,
                lifecycle_intelligence_enable=False,
                lifecycle_outcome_backfill_enable=False,
                lifecycle_outcome_incremental_enable=False,
                model_calibration_enable=False,
                model_optimization_enable=True,
                model_optimization_interval_sec=21600,
            )
            base = int(time.time())
            first = jobs.lifecycle_intelligence_scheduler_tick(
                settings=settings, now=base, start=False
            )
            waiting = jobs.lifecycle_intelligence_scheduler_tick(
                settings=settings, now=base + 21599, start=False
            )
            due = jobs.lifecycle_intelligence_scheduler_tick(
                settings=settings, now=base + 21600, start=False
            )
        self.assertEqual(first["submitted"], [])
        self.assertEqual(waiting["submitted"], [])
        self.assertEqual(due["submitted"], ["optimization-run"])
        self.assertNotIn("--limit", due["jobs"][0]["job"]["command"])


class ModelOptimizationApiTests(unittest.TestCase):
    def test_public_sections_only_load_cached_report_and_recursively_redact(self) -> None:
        report = {
            "ok": True,
            "optimization_version": "optimization-v1",
            "production_model": "decision-model-v1",
            "base_model": "calibration-validation-v1",
            "generated_at": "2026-07-12T00:00:00Z",
            "status": "review_required",
            "summary": {"scenario_count": 4, "token": "private"},
            "scenarios": [
                {"scenario": name, "candidate_params": {"threshold": index}}
                for index, name in enumerate(SCENARIOS)
            ],
            "runs": [
                {"scenario": "risk_control", "note": "/home/ubuntu/private.json"},
                {"scenario": "risk_control", "authorization": "Bearer private"},
                {"scenario": "threshold_tuning", "metric": 3},
            ],
            "readiness": {"ready": False, "blocked": ["manual_review"]},
            "database_path": "/home/ubuntu/data/lifecycle.db",
            "auto_apply": False,
        }
        loader = Mock(return_value=report)
        names: list[str] = []

        def resolve(name: str) -> object:
            names.append(name)
            self.assertEqual(name, "get_optimization_report")
            return loader

        with TemporaryDirectory() as tmp, patch(
            "paopao_radar.web_services.model_optimization._core_function",
            side_effect=resolve,
        ):
            settings = make_settings(tmp)
            payloads = {
                section: public_optimization_section_payload(
                    section, settings=settings, limit=2
                )
                for section in ("summary", "scenarios", "report", "readiness")
            }
            again = public_optimization_section_payload(
                "summary", settings=settings, limit=2
            )
        self.assertEqual(names, ["get_optimization_report"])
        self.assertEqual(loader.call_count, 1)
        self.assertEqual(payloads["summary"]["data"]["scenario_count"], 4)
        self.assertEqual(len(payloads["scenarios"]["data"]["items"]), 2)
        self.assertEqual(len(payloads["report"]["data"]["runs"]), 2)
        self.assertFalse(payloads["readiness"]["data"]["ready"])
        self.assertEqual(again["data"]["scenario_count"], 4)
        serialized = json.dumps(payloads, ensure_ascii=False).lower()
        for forbidden in (
            "token", "authorization", "database_path", "/home/ubuntu", "bearer private"
        ):
            self.assertNotIn(forbidden, serialized)

    def test_private_report_is_read_only_and_symbol_scope_is_rejected(self) -> None:
        report = {
            "optimization_version": "optimization-v1",
            "summary": {"runs": 1},
            "runs": [{"scenario": "risk_control"}],
            "auto_apply": False,
        }
        with TemporaryDirectory() as tmp, patch(
            "paopao_radar.web_services.model_optimization._load_latest_report",
            return_value=(True, report, "", "ok"),
        ) as load:
            payload = optimization_report_payload(settings=make_settings(tmp))
            rejected = optimization_report_payload(
                settings=make_settings(tmp), symbol="BTCUSDT"
            )
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["data"]["auto_apply"])
        self.assertFalse(rejected["ok"])
        self.assertEqual(rejected["code"], "optimization_symbol_scope_requires_cli")
        self.assertEqual(load.call_count, 1)

    def test_public_routes_and_private_report_auth_boundary(self) -> None:
        with patch(
            "paopao_radar.web.public_optimization_section_payload",
            return_value={"ok": True, "data": {}},
        ) as payload:
            for section in ("summary", "scenarios", "report", "readiness"):
                handler, statuses = make_handler(f"/public-api/optimization/{section}")
                web.WebHandler.do_GET(handler)
                self.assertEqual(statuses[-1], 200)
                self.assertEqual(payload.call_args.args[0], section)
        private, statuses = make_handler("/api/optimization/report")
        web.WebHandler.do_GET(private)
        self.assertEqual(statuses[-1], 401)

    def test_private_run_and_rebuild_are_authenticated_jobs(self) -> None:
        for path, expected in (
            ("/api/optimization/run", "optimization-run"),
            ("/api/optimization/report", "optimization-run"),
            ("/api/optimization/rebuild", "optimization-rebuild"),
        ):
            handler, _statuses = make_handler(path)
            captured: dict[str, object] = {}
            handler.read_json = lambda: {"scenario": "risk_control"}
            handler.send_audited_json = lambda route, data, result, **kwargs: captured.update({
                "route": route, "result": result, "kwargs": kwargs,
            })
            with patch.object(web.WebHandler, "require_auth", return_value=True), patch(
                "paopao_radar.web.create_job_payload",
                return_value={"ok": True, "job_id": 42, "job": {"id": 42}},
            ) as create:
                web.WebHandler.do_POST(handler)
            self.assertEqual(captured["result"]["job_id"], 42)  # type: ignore[index]
            self.assertEqual(create.call_args.args[0], expected)
            self.assertEqual(create.call_args.args[1]["scenario"], "risk_control")

        for path in (
            "/api/optimization/run",
            "/api/optimization/report",
            "/api/optimization/rebuild",
        ):
            unauthorized, statuses = make_handler(path)
            web.WebHandler.do_POST(unauthorized)
            self.assertEqual(statuses[-1], 401)


if __name__ == "__main__":
    unittest.main()
