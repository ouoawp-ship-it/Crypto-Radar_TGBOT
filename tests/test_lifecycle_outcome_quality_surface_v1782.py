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
from paopao_radar.web_services.lifecycle_outcome_quality import (
    public_lifecycle_calibration_readiness_payload,
    public_lifecycle_outcome_quality_payload,
    public_lifecycle_outcome_summary_with_quality_payload,
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


class LifecycleOutcomeQualitySurfaceConfigCliTests(unittest.TestCase):
    def test_settings_env_and_sync_defaults(self) -> None:
        settings = Settings()
        expected = {
            "lifecycle_outcome_processing_stale_sec": 1800,
            "lifecycle_outcome_retry_max_attempts": 5,
            "lifecycle_outcome_retry_base_sec": 900,
            "lifecycle_outcome_retry_max_sec": 21600,
            "lifecycle_outcome_incremental_enable": True,
            "lifecycle_outcome_incremental_interval_sec": 3600,
            "lifecycle_outcome_incremental_batch_size": 200,
            "lifecycle_outcome_incremental_max_items": 1000,
            "lifecycle_outcome_incremental_max_symbols": 100,
            "lifecycle_calibration_min_24h_success": 50,
            "lifecycle_calibration_min_72h_success": 30,
            "lifecycle_calibration_min_due_resolution_ratio": 0.90,
            "lifecycle_calibration_min_lifecycle_maturity_ratio": 0.60,
            "lifecycle_calibration_max_error_ratio": 0.01,
        }
        for key, value in expected.items():
            self.assertEqual(getattr(settings, key), value)

        example = (BASE_DIR / ".env.oi.example").read_text(encoding="utf-8")
        sync = (BASE_DIR / "scripts/sync_env.py").read_text(encoding="utf-8")
        for key in (
            "LIFECYCLE_OUTCOME_PROCESSING_STALE_SEC",
            "LIFECYCLE_OUTCOME_RETRY_MAX_ATTEMPTS",
            "LIFECYCLE_OUTCOME_RETRY_BASE_SEC",
            "LIFECYCLE_OUTCOME_RETRY_MAX_SEC",
            "LIFECYCLE_OUTCOME_INCREMENTAL_ENABLE",
            "LIFECYCLE_OUTCOME_INCREMENTAL_INTERVAL_SEC",
            "LIFECYCLE_OUTCOME_INCREMENTAL_BATCH_SIZE",
            "LIFECYCLE_OUTCOME_INCREMENTAL_MAX_ITEMS",
            "LIFECYCLE_OUTCOME_INCREMENTAL_MAX_SYMBOLS",
            "LIFECYCLE_CALIBRATION_MIN_24H_SUCCESS",
            "LIFECYCLE_CALIBRATION_MIN_72H_SUCCESS",
            "LIFECYCLE_CALIBRATION_MIN_DUE_RESOLUTION_RATIO",
            "LIFECYCLE_CALIBRATION_MIN_LIFECYCLE_MATURITY_RATIO",
            "LIFECYCLE_CALIBRATION_MAX_ERROR_RATIO",
        ):
            self.assertIn(key, example)
            self.assertIn(key, sync)

    def test_cli_parser_exposes_quality_commands_and_scope(self) -> None:
        parser = cli.build_parser()
        commands = (
            "lifecycle-outcome-refresh-candidates",
            "lifecycle-outcome-classify-gaps",
            "lifecycle-outcome-incremental",
            "lifecycle-outcome-quality",
            "lifecycle-calibration-readiness",
        )
        for command in commands:
            args = parser.parse_args([
                command,
                "--symbol", "BTCUSDT",
                "--lifecycle-id", "12",
                "--horizon", "4h",
                "--module", "structure",
                "--limit", "50",
                "--dry-run",
                "--pretty",
                "--force",
            ])
            self.assertEqual(args.command, command)
            self.assertEqual(args.module, "structure")
            self.assertTrue(args.force)

    def test_cli_incremental_forwards_scope_and_dry_run(self) -> None:
        args = Namespace(
            symbol="BTCUSDT",
            lifecycle_id=12,
            horizon="4h",
            module="structure",
            limit=50,
            dry_run=True,
            pretty=False,
            force=True,
        )
        with TemporaryDirectory() as tmp, patch(
            "paopao_radar.cli.Settings.load", return_value=make_settings(tmp)
        ):
            with patch(
                "paopao_radar.lifecycle_outcome_quality.incremental_outcome_backfill",
                return_value={"ok": True},
            ) as incremental:
                self.assertEqual(cli.run_lifecycle_outcome_incremental(args), 0)
        kwargs = incremental.call_args.kwargs
        self.assertEqual(kwargs["symbol"], "BTCUSDT")
        self.assertEqual(kwargs["horizon"], "4h")
        self.assertEqual(kwargs["module"], "structure")
        self.assertTrue(kwargs["dry_run"])
        self.assertTrue(kwargs["force"])

    def test_quality_and_readiness_cli_write_reports_only_outside_dry_run(self) -> None:
        base = Namespace(
            symbol="", lifecycle_id=None, horizon="", module="", limit=50,
            dry_run=False, pretty=False, force=False,
        )
        with TemporaryDirectory() as tmp, patch(
            "paopao_radar.cli.Settings.load", return_value=make_settings(tmp)
        ), patch(
            "paopao_radar.lifecycle_outcome_quality.lifecycle_outcome_quality",
            return_value={"ok": True},
        ) as quality, patch(
            "paopao_radar.lifecycle_outcome_quality.lifecycle_calibration_readiness",
            return_value={"ok": True, "ready": False},
        ) as readiness:
            self.assertEqual(cli.run_lifecycle_outcome_quality(base), 0)
            self.assertEqual(cli.run_lifecycle_calibration_readiness(base), 0)
        self.assertTrue(quality.call_args.kwargs["write_reports"])
        self.assertTrue(readiness.call_args.kwargs["write_reports"])

    def test_legacy_backfill_eligible_due_flags_use_candidate_incremental_engine(self) -> None:
        args = Namespace(
            symbol="BTCUSDT", lifecycle_id=None, horizon="24h", module="flow",
            limit=20, dry_run=True, pretty=False, force_relink=False,
            force_outcome_rebuild=False, eligible_only=True, due_only=True,
        )
        with TemporaryDirectory() as tmp, patch(
            "paopao_radar.cli.Settings.load", return_value=make_settings(tmp)
        ), patch(
            "paopao_radar.lifecycle_outcome_quality.incremental_outcome_backfill",
            return_value={"ok": True},
        ) as incremental, patch(
            "paopao_radar.lifecycle_outcomes.backfill_lifecycle_outcomes"
        ) as legacy:
            self.assertEqual(cli.run_lifecycle_outcome_backfill(args), 0)
        legacy.assert_not_called()
        self.assertTrue(incremental.call_args.kwargs["dry_run"])
        self.assertEqual(incremental.call_args.kwargs["module"], "flow")


class LifecycleOutcomeQualitySurfaceJobTests(unittest.TestCase):
    def test_job_specs_are_guarded_and_commands_are_bounded(self) -> None:
        expected = {
            "lifecycle-outcome-refresh-candidates",
            "lifecycle-outcome-classify-gaps",
            "lifecycle-outcome-incremental-backfill",
            "lifecycle-outcome-quality-report",
            "lifecycle-calibration-readiness",
        }
        self.assertTrue(expected.issubset(jobs.JOB_SPECS))
        self.assertTrue(expected.issubset(jobs.CONCURRENT_GUARD_JOB_TYPES))
        self.assertTrue(expected.issubset(jobs.LIFECYCLE_RESEARCH_JOB_TYPES))
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            payload = jobs.create_job_payload(
                "lifecycle-outcome-incremental-backfill",
                {
                    "symbol": "btc",
                    "limit": 50000,
                    "horizon": "24h",
                    "module": "structure_review",
                    "force": True,
                },
                settings=settings,
                start=False,
            )
        command = payload["job"]["command"]
        self.assertIn("lifecycle-outcome-incremental", command)
        self.assertIn("BTCUSDT", command)
        self.assertEqual(command[command.index("--limit") + 1], "1000")
        self.assertEqual(command[command.index("--module") + 1], "structure_review")
        self.assertIn("--force", command)

    def test_invalid_quality_scope_and_boolean_never_create_job(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            results = [
                jobs.create_job_payload(
                    "lifecycle-outcome-refresh-candidates", {"symbol": "???"},
                    settings=settings, start=False,
                ),
                jobs.create_job_payload(
                    "lifecycle-outcome-incremental-backfill", {"horizon": "2h"},
                    settings=settings, start=False,
                ),
                jobs.create_job_payload(
                    "lifecycle-outcome-quality-report", {"module": "a/b"},
                    settings=settings, start=False,
                ),
                jobs.create_job_payload(
                    "lifecycle-outcome-classify-gaps", {"force": "false"},
                    settings=settings, start=False,
                ),
            ]
            created = jobs.store_for_settings(settings).list_jobs(limit=10)
        self.assertTrue(all(not item["ok"] for item in results))
        self.assertTrue(all(item["code"] == "invalid_job_scope" for item in results))
        self.assertEqual(created, [])

    def test_execute_and_rerun_preserve_validated_quality_scope(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            created = jobs.create_job_payload(
                "lifecycle-outcome-incremental-backfill",
                {
                    "symbol": "btc", "limit": 50, "horizon": "4h",
                    "module": "structure", "force": True,
                },
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
        self.assertEqual(rerun["job"]["command"], command)
        self.assertIn("--module", command)
        self.assertIn("--force", command)

    def test_scheduler_delays_candidate_refresh_until_first_interval(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(
                tmp,
                lifecycle_intelligence_enable=False,
                lifecycle_outcome_backfill_enable=False,
                lifecycle_outcome_incremental_enable=True,
            )
            base = int(time.time())
            with patch.object(jobs, "_now", return_value=base):
                first = jobs.lifecycle_intelligence_scheduler_tick(
                    settings=settings, now=base, start=False,
                )
            with patch.object(jobs, "_now", return_value=base + 899):
                waiting = jobs.lifecycle_intelligence_scheduler_tick(
                    settings=settings, now=base + 899, start=False,
                )
            with patch.object(jobs, "_now", return_value=base + 900):
                due = jobs.lifecycle_intelligence_scheduler_tick(
                    settings=settings, now=base + 900, start=False,
                )
        self.assertEqual(first["submitted"], [])
        self.assertEqual(waiting["submitted"], [])
        self.assertEqual(due["submitted"], ["lifecycle-outcome-refresh-candidates"])
        self.assertNotIn("lifecycle-outcome-backfill", due["submitted"])
        self.assertEqual(
            jobs.store_for_settings(settings).list_jobs(
                limit=10, job_type="lifecycle-outcome-backfill"
            ),
            [],
        )


class LifecycleOutcomeQualitySurfaceApiTests(unittest.TestCase):
    def test_quality_sections_keep_stable_shapes_and_normalize_time_range(self) -> None:
        core = {
            "ok": True,
            "summary": {"eligible_candidate_count": 3, "next_retry_at": "2026-07-11T12:30:00+00:00"},
            "status_counts": {"ready": 2, "retry_wait": 1},
            "reasons": {"backfill_not_attempted": 2, "provider_timeout": 1},
        }
        with TemporaryDirectory() as tmp, patch(
            "paopao_radar.web_services.lifecycle_outcome_quality.lifecycle_outcome_quality",
            return_value=core,
        ):
            settings = make_settings(tmp)
            summary = public_lifecycle_outcome_quality_payload(
                "summary", settings=settings, time_range="invalid",
            )
            reasons = public_lifecycle_outcome_quality_payload(
                "reasons", settings=settings, time_range="invalid",
            )
        self.assertEqual(summary["data"]["time_range"], "all")
        self.assertEqual(summary["data"]["status_counts"]["ready"], 2)
        self.assertEqual(summary["data"]["reasons"]["provider_timeout"], 1)
        self.assertEqual(reasons["data"]["reasons"]["backfill_not_attempted"], 2)

    def test_frontend_uses_v1782_quality_contract_and_five_named_metrics(self) -> None:
        lifecycle_page = (BASE_DIR / "frontend/app/lifecycle/page.tsx").read_text(encoding="utf-8")
        coin_page = (BASE_DIR / "frontend/app/coin/[symbol]/page.tsx").read_text(encoding="utf-8")
        for label in (
            "生命周期关联覆盖率", "候选信号关联覆盖率", "到期候选解决率",
            "有效 Outcome 成熟率", "生命周期成熟率", "此处仅判断数据是否足够，不会自动修改模型",
        ):
            self.assertIn(label, lifecycle_page)
        for field in (
            "success_count", "unavailable_count", "real_error_ratio",
            '"duplicate_links", "multiple_primary", "orphan_links"',
        ):
            self.assertIn(field, lifecycle_page)
        self.assertIn("coinQuality.status_counts", coin_page)
        self.assertIn("next_retry_at", coin_page)

    def test_public_quality_projection_removes_sensitive_fields(self) -> None:
        core = {
            "ok": True,
            "data": {
                "summary": {
                    "eligible_candidate_count": 10,
                    "candidate_link_coverage_ratio": 0.8,
                    "token": "secret",
                    "database_path": "/home/ubuntu/private.db",
                    "outcome_id": 22,
                },
            },
        }
        readiness = {
            "ok": True,
            "data": {
                "ready": False,
                "blocked": ["72h_success"],
                "internal_job_payload": {"token": "secret"},
            },
        }
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            with patch(
                "paopao_radar.web_services.lifecycle_outcome_quality.lifecycle_outcome_quality",
                return_value=core,
            ):
                quality = public_lifecycle_outcome_quality_payload(
                    "summary", settings=settings,
                )
            with patch(
                "paopao_radar.web_services.lifecycle_outcome_quality.lifecycle_calibration_readiness",
                return_value=readiness,
            ):
                gate = public_lifecycle_calibration_readiness_payload(settings=settings)
        text = json.dumps({"quality": quality, "gate": gate}, ensure_ascii=False)
        for forbidden in (
            "token", "database_path", "/home/ubuntu", "outcome_id", "internal_job_payload",
        ):
            self.assertNotIn(forbidden, text)
        self.assertTrue(gate["data"]["does_not_modify_model"])

    def test_legacy_summary_is_additively_extended(self) -> None:
        legacy = {"ok": True, "data": {"lifecycle_count": 106, "link_coverage_ratio": 0.53}}
        quality = {
            "ok": True,
            "data": {"summary": {
                "lifecycle_link_coverage_ratio": 0.62,
                "candidate_link_coverage_ratio": 0.53,
                "due_resolution_ratio": 0.75,
            }, "reasons": {"backfill_not_attempted": 9}},
        }
        with TemporaryDirectory() as tmp, patch(
            "paopao_radar.web_services.lifecycle_outcome_quality.legacy_outcome_summary_payload",
            return_value=legacy,
        ), patch(
            "paopao_radar.web_services.lifecycle_outcome_quality.lifecycle_outcome_quality",
            return_value=quality,
        ):
            payload = public_lifecycle_outcome_summary_with_quality_payload(
                settings=make_settings(tmp)
            )
        self.assertEqual(payload["data"]["link_coverage_ratio"], 0.53)
        self.assertEqual(payload["data"]["lifecycle_link_coverage_ratio"], 0.62)
        self.assertEqual(payload["data"]["candidate_link_coverage_ratio"], 0.53)
        self.assertEqual(payload["data"]["reasons"], {"backfill_not_attempted": 9})

    def test_public_and_private_routes_keep_auth_boundary(self) -> None:
        sections = ("summary", "reasons", "modules", "levels", "horizons", "timeline")
        with patch(
            "paopao_radar.web.public_lifecycle_outcome_quality_payload",
            return_value={"ok": True, "data": {"items": []}},
        ) as payload:
            for section in sections:
                public, public_status = make_handler(
                    f"/public-api/lifecycle/outcomes/quality/{section}?time_range=7d"
                )
                web.WebHandler.do_GET(public)
                self.assertEqual(public_status[-1], 200)
                self.assertEqual(payload.call_args.args[0], section)
        calibration, calibration_status = make_handler(
            "/public-api/lifecycle/calibration-readiness"
        )
        with patch(
            "paopao_radar.web.public_lifecycle_calibration_readiness_payload",
            return_value={"ok": True, "data": {"ready": False}},
        ):
            web.WebHandler.do_GET(calibration)
        self.assertEqual(calibration_status[-1], 200)

        for path in (
            "/api/lifecycle/outcomes/quality/summary",
            "/api/lifecycle/outcomes/quality/reasons",
            "/api/lifecycle/calibration-readiness",
        ):
            private, private_status = make_handler(path)
            web.WebHandler.do_GET(private)
            self.assertEqual(private_status[-1], 401)

    def test_private_quality_posts_use_authenticated_background_jobs(self) -> None:
        routes = {
            "/api/lifecycle/outcomes/run-refresh-candidates": "lifecycle-outcome-refresh-candidates",
            "/api/lifecycle/outcomes/run-classify-gaps": "lifecycle-outcome-classify-gaps",
            "/api/lifecycle/outcomes/run-incremental": "lifecycle-outcome-incremental-backfill",
            "/api/lifecycle/outcomes/run-quality-report": "lifecycle-outcome-quality-report",
        }
        for path, expected_type in routes.items():
            instance, _statuses = make_handler(path)
            captured: dict[str, object] = {}
            instance.read_json = lambda: {
                "limit": 50, "module": "structure", "force": True
            }
            instance.send_audited_json = lambda route, data, result, **kwargs: captured.update({
                "path": route, "data": data, "result": result, "kwargs": kwargs,
            })
            with patch.object(web.WebHandler, "require_auth", return_value=True), patch(
                "paopao_radar.web.create_job_payload",
                return_value={"ok": True, "job_id": 42, "job": {"id": 42}},
            ) as create:
                web.WebHandler.do_POST(instance)
            self.assertEqual(captured["result"]["job_id"], 42)  # type: ignore[index]
            self.assertEqual(create.call_args.args[0], expected_type)
            self.assertEqual(create.call_args.args[1]["module"], "structure")
            self.assertTrue(create.call_args.args[1]["force"])


if __name__ == "__main__":
    unittest.main()
