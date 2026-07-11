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
from paopao_radar.web_services.lifecycle_calibration import (
    calibration_report_payload,
    public_calibration_section_payload,
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
    instance.server = type(
        "Server", (), {"admin_token": "secret", "settings": Settings(web_auth_mode="password")}
    )()
    instance.wfile = BytesIO()
    instance.send_response = lambda status: statuses.append(status)
    instance.send_header = lambda _key, _value: None
    instance.end_headers = lambda: None
    return instance, statuses


def cli_args(**overrides: object) -> Namespace:
    values: dict[str, object] = {
        "symbol": "",
        "limit": None,
        "dry_run": False,
        "pretty": False,
        "force": False,
        "force_rebuild": False,
    }
    values.update(overrides)
    return Namespace(**values)


class CalibrationSurfaceConfigCliTests(unittest.TestCase):
    def test_settings_and_env_defaults_are_distinct_from_readiness_thresholds(self) -> None:
        settings = Settings()
        self.assertTrue(settings.model_calibration_enable)
        self.assertEqual(settings.model_calibration_interval_sec, 21600)
        self.assertEqual(settings.model_calibration_cache_ttl_sec, 30)
        example = (BASE_DIR / ".env.oi.example").read_text(encoding="utf-8")
        sync = (BASE_DIR / "scripts/sync_env.py").read_text(encoding="utf-8")
        for key in (
            "MODEL_CALIBRATION_ENABLE",
            "MODEL_CALIBRATION_INTERVAL_SEC",
            "MODEL_CALIBRATION_CACHE_TTL_SEC",
        ):
            self.assertIn(key, example)
            self.assertIn(key, sync)

    def test_cli_parser_exposes_read_only_calibration_commands(self) -> None:
        parser = cli.build_parser()
        for command in (
            "calibration-report",
            "calibration-decision",
            "calibration-lifecycle",
            "calibration-factors",
            "calibration-readiness",
        ):
            args = parser.parse_args([
                command, "--symbol", "BTCUSDT", "--limit", "200",
                "--dry-run", "--pretty",
            ])
            self.assertEqual(args.command, command)
            self.assertEqual(args.symbol, "BTCUSDT")
            self.assertTrue(args.dry_run)

    def test_report_dry_run_does_not_write_and_default_is_not_truncated(self) -> None:
        generate = Mock(return_value={"ok": True, "status": "dry_run"})
        with TemporaryDirectory() as tmp, patch(
            "paopao_radar.cli.Settings.load", return_value=make_settings(tmp)
        ), patch(
            "paopao_radar.cli._calibration_core_function", return_value=generate
        ), patch("sys.stdout", new_callable=StringIO):
            code = cli.run_calibration_report(cli_args(dry_run=True))
        self.assertEqual(code, 0)
        kwargs = generate.call_args.kwargs
        self.assertTrue(kwargs["dry_run"])
        self.assertFalse(kwargs["write_reports"])
        self.assertNotIn("limit", kwargs)

    def test_explicit_report_limit_and_force_are_forwarded(self) -> None:
        generate = Mock(return_value={"ok": True})
        with TemporaryDirectory() as tmp, patch(
            "paopao_radar.cli.Settings.load", return_value=make_settings(tmp)
        ), patch(
            "paopao_radar.cli._calibration_core_function", return_value=generate
        ), patch("sys.stdout", new_callable=StringIO):
            code = cli.run_calibration_report(
                cli_args(symbol="BTCUSDT", limit=50000, force=True)
            )
        self.assertEqual(code, 0)
        self.assertEqual(generate.call_args.kwargs["limit"], 10000)
        self.assertTrue(generate.call_args.kwargs["force"])
        self.assertTrue(generate.call_args.kwargs["write_reports"])

    def test_section_cli_reads_latest_report_without_generation(self) -> None:
        latest = Mock(return_value={
            "ok": True,
            "calibration_version": "calibration-validation-v1",
            "model_version": "decision-model-v1",
            "decision_labels": [{"decision": "observe", "sample_count": 20}],
        })
        names: list[str] = []

        def resolve(name: str) -> object:
            names.append(name)
            return latest

        with TemporaryDirectory() as tmp, patch(
            "paopao_radar.cli.Settings.load", return_value=make_settings(tmp)
        ), patch(
            "paopao_radar.cli._calibration_core_function", side_effect=resolve
        ), patch("sys.stdout", new_callable=StringIO) as output:
            code = cli.run_calibration_section(
                cli_args(symbol="", limit=10, dry_run=True), "decision"
            )
        self.assertEqual(code, 0)
        self.assertEqual(names, ["get_calibration_report"])
        self.assertIn("decision_labels", output.getvalue())

    def test_symbol_scoped_section_uses_in_memory_generation_only(self) -> None:
        generate = Mock(return_value={
            "ok": True,
            "decision_labels": [{"decision": "observe", "sample_count": 2}],
        })
        names: list[str] = []

        def resolve(name: str) -> object:
            names.append(name)
            return generate

        with TemporaryDirectory() as tmp, patch(
            "paopao_radar.cli.Settings.load", return_value=make_settings(tmp)
        ), patch(
            "paopao_radar.cli._calibration_core_function", side_effect=resolve
        ), patch("sys.stdout", new_callable=StringIO):
            code = cli.run_calibration_section(
                cli_args(symbol="BTCUSDT", limit=10), "decision"
            )
        self.assertEqual(code, 0)
        self.assertEqual(names, ["generate_calibration_report"])
        self.assertEqual(generate.call_args.kwargs["symbol"], "BTCUSDT")
        self.assertTrue(generate.call_args.kwargs["dry_run"])
        self.assertFalse(generate.call_args.kwargs["write_reports"])


class CalibrationSurfaceJobTests(unittest.TestCase):
    def test_job_specs_are_guarded_and_default_report_is_full_history(self) -> None:
        expected = {"calibration-report", "calibration-rebuild"}
        self.assertTrue(expected.issubset(jobs.JOB_SPECS))
        self.assertTrue(expected.issubset(jobs.CONCURRENT_GUARD_JOB_TYPES))
        self.assertTrue(expected.issubset(jobs.LIFECYCLE_RESEARCH_JOB_TYPES))
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            report = jobs.create_job_payload(
                "calibration-report", settings=settings, start=False
            )
            store = jobs.store_for_settings(settings)
            store.finish_job(report["job_id"], status="success", returncode=0)
            rebuild = jobs.create_job_payload(
                "calibration-rebuild", settings=settings, start=False
            )
        self.assertNotIn("--limit", report["job"]["command"])
        self.assertIn("--force", rebuild["job"]["command"])

    def test_explicit_scope_is_validated_bounded_and_rerunnable(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            created = jobs.create_job_payload(
                "calibration-report",
                {"symbol": "btc", "limit": 50000, "force": True},
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
        self.assertIn("BTCUSDT", command)
        self.assertEqual(command[command.index("--limit") + 1], "10000")
        self.assertIn("--force", command)
        self.assertEqual(rerun["job"]["command"], command)

    def test_invalid_scope_or_boolean_never_creates_job(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            invalid = [
                jobs.create_job_payload(
                    "calibration-report", {"symbol": "???"}, settings=settings, start=False
                ),
                jobs.create_job_payload(
                    "calibration-report", {"limit": True}, settings=settings, start=False
                ),
                jobs.create_job_payload(
                    "calibration-rebuild", {"force": "false"}, settings=settings, start=False
                ),
            ]
            rows = jobs.store_for_settings(settings).list_jobs(limit=10)
        self.assertTrue(all(not item["ok"] for item in invalid))
        self.assertTrue(all(item["code"] == "invalid_job_scope" for item in invalid))
        self.assertEqual(rows, [])

    def test_scheduler_waits_a_full_interval_before_first_report(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(
                tmp,
                lifecycle_intelligence_enable=False,
                lifecycle_outcome_backfill_enable=False,
                lifecycle_outcome_incremental_enable=False,
                model_calibration_enable=True,
                model_calibration_interval_sec=21600,
            )
            base = int(time.time())
            with patch.object(jobs, "_now", return_value=base):
                first = jobs.lifecycle_intelligence_scheduler_tick(
                    settings=settings, now=base, start=False
                )
            with patch.object(jobs, "_now", return_value=base + 21599):
                waiting = jobs.lifecycle_intelligence_scheduler_tick(
                    settings=settings, now=base + 21599, start=False
                )
            with patch.object(jobs, "_now", return_value=base + 21600):
                due = jobs.lifecycle_intelligence_scheduler_tick(
                    settings=settings, now=base + 21600, start=False
                )
        self.assertEqual(first["submitted"], [])
        self.assertEqual(waiting["submitted"], [])
        self.assertEqual(due["submitted"], ["calibration-report"])
        self.assertNotIn("--limit", due["jobs"][0]["job"]["command"])


class CalibrationSurfaceApiTests(unittest.TestCase):
    def test_public_sections_use_one_cached_report_and_strip_sensitive_fields(self) -> None:
        report = {
            "ok": True,
            "calibration_version": "calibration-validation-v1",
            "model_version": "decision-model-v1",
            "status": "ready",
            "generated_at": "2026-07-12T00:00:00Z",
            "summary": {"sample_count": 3010, "token": "secret"},
            "decision_labels": [{"decision": "observe", "sample_count": 100}],
            "first_levels": [{"first_signal_level": "15m", "sample_count": 20}],
            "upgrade_paths": [],
            "intelligence_buckets": [],
            "factors": {"spot_cvd": [{"bucket": "confirmed"}]},
            "risk_alerts": [{"event": "risk_warning"}],
            "readiness": {"ready": True},
            "database_path": "/home/ubuntu/private.db",
            "internal_job_payload": {"secret": "private"},
        }
        loader = Mock(return_value=report)
        with TemporaryDirectory() as tmp, patch(
            "paopao_radar.web_services.lifecycle_calibration._core_function",
            return_value=loader,
        ):
            settings = make_settings(tmp)
            payloads = {
                section: public_calibration_section_payload(
                    section, settings=settings, limit=100
                )
                for section in ("summary", "decision", "lifecycle", "factors", "risk", "readiness")
            }
            again = public_calibration_section_payload(
                "summary", settings=settings, limit=100
            )
        self.assertEqual(loader.call_count, 1)
        self.assertEqual(payloads["summary"]["data"]["sample_count"], 3010)
        self.assertEqual(payloads["summary"]["data"]["calibration_version"], "calibration-validation-v1")
        self.assertEqual(payloads["lifecycle"]["data"]["items"][0]["first_signal_level"], "15m")
        self.assertTrue(payloads["readiness"]["data"]["ready"])
        self.assertEqual(again["data"]["sample_count"], 3010)
        text = json.dumps(payloads, ensure_ascii=False)
        for forbidden in ("token", "database_path", "/home/ubuntu", "internal_job_payload", "secret"):
            self.assertNotIn(forbidden, text)

    def test_private_report_is_read_only_and_sanitized(self) -> None:
        report = {
            "ok": True,
            "summary": {"sample_count": 10},
            "decision_labels": [{"metric_key": str(index)} for index in range(150)],
            "server_path": "/private",
        }
        with TemporaryDirectory() as tmp, patch(
            "paopao_radar.web_services.lifecycle_calibration._load_latest_report",
            return_value=(True, report, "", "ok"),
        ) as load:
            payload = calibration_report_payload(settings=make_settings(tmp))
        self.assertTrue(payload["ok"])
        self.assertNotIn("server_path", payload["data"])
        self.assertEqual(len(payload["data"]["decision_labels"]), 100)
        self.assertEqual(load.call_count, 1)

    def test_public_routes_and_private_report_auth_boundary(self) -> None:
        sections = ("summary", "decision", "lifecycle", "factors", "risk", "readiness")
        with patch(
            "paopao_radar.web.public_calibration_section_payload",
            return_value={"ok": True, "data": {}},
        ) as payload:
            for section in sections:
                handler, statuses = make_handler(f"/public-api/calibration/{section}")
                web.WebHandler.do_GET(handler)
                self.assertEqual(statuses[-1], 200)
                self.assertEqual(payload.call_args.args[0], section)
        private, private_status = make_handler("/api/calibration/report")
        web.WebHandler.do_GET(private)
        self.assertEqual(private_status[-1], 401)

    def test_private_run_and_rebuild_are_authenticated_jobs(self) -> None:
        for path, expected in (
            ("/api/calibration/run", "calibration-report"),
            ("/api/calibration/rebuild", "calibration-rebuild"),
        ):
            handler, _statuses = make_handler(path)
            captured: dict[str, object] = {}
            handler.read_json = lambda: {"symbol": "BTCUSDT"}
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

        unauthorized, statuses = make_handler("/api/calibration/run")
        web.WebHandler.do_POST(unauthorized)
        self.assertEqual(statuses[-1], 401)


if __name__ == "__main__":
    unittest.main()
