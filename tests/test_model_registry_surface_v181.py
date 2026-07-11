from __future__ import annotations

import json
import sqlite3
import unittest
from argparse import Namespace
from io import BytesIO, StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from unittest.mock import Mock

from paopao_radar import cli, web
from paopao_radar.config import Settings
from paopao_radar.model_approval import approve_model
from paopao_radar.model_registry import (
    ModelRegistryStore,
    bootstrap_production_model,
    current_model,
    register_candidate,
)
from paopao_radar.runtime_cache import clear as clear_runtime_cache
from paopao_radar.web_services import jobs
from paopao_radar.web_services.model_registry import (
    model_list_payload,
    public_model_registry_payload,
)


COMMANDS = (
    "model-list", "model-show", "model-diff", "model-register",
    "model-approve", "model-reject", "model-rollback", "model-health",
)


def make_settings(tmp: str) -> Settings:
    base = Path(tmp)
    return Settings(
        data_dir=base,
        signal_events_db_path=base / "signals.db",
        outcome_db_path=base / "outcomes.db",
        lifecycle_db_path=base / "lifecycle.db",
        web_jobs_db_path=base / "jobs.db",
    )


def args(**overrides: object) -> Namespace:
    values: dict[str, object] = {
        "model": "signal-decision",
        "version": "candidate-test-v1",
        "approved_by": "operator",
        "reason": "reviewed historical simulation",
        "source_version": "",
        "description": "",
        "scenario": "",
        "activate": False,
        "confirm_production": False,
        "bootstrap_production": False,
        "dry_run": False,
        "pretty": True,
        "limit": None,
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


class ModelRegistryCliSurfaceTests(unittest.TestCase):
    def test_parser_exposes_all_commands_and_manual_approval_flags(self) -> None:
        parser = cli.build_parser()
        for command in COMMANDS:
            parsed = parser.parse_args([
                command, "--model", "signal-decision", "--version", "candidate-test-v1",
                "--approved-by", "operator", "--reason", "manual review", "--dry-run", "--pretty",
            ])
            self.assertEqual(parsed.command, command)
            self.assertEqual(parsed.model, "signal-decision")
            self.assertTrue(parsed.dry_run)

    def test_explicit_production_bootstrap_dry_run_does_not_create_database(self) -> None:
        with TemporaryDirectory() as tmp, patch(
            "paopao_radar.cli.Settings.load", return_value=make_settings(tmp)
        ), patch("sys.stdout", new_callable=StringIO):
            code = cli.run_model_register(args(
                version="", bootstrap_production=True, dry_run=True,
            ))
            self.assertEqual(code, 0)
            self.assertFalse((Path(tmp) / "model_registry.db").exists())

    def test_production_bootstrap_requires_explicit_flag(self) -> None:
        with TemporaryDirectory() as tmp, patch(
            "paopao_radar.cli.Settings.load", return_value=make_settings(tmp)
        ), patch("sys.stdout", new_callable=StringIO):
            code = cli.run_model_register(args(version="", dry_run=False))
            self.assertNotEqual(code, 0)
            self.assertFalse((Path(tmp) / "model_registry.db").exists())

    def test_scenario_only_candidate_registration_is_bounded_dry_run(self) -> None:
        core = Mock(return_value={"ok": True, "dry_run": True, "changed": False})
        with TemporaryDirectory() as tmp, patch(
            "paopao_radar.cli.Settings.load", return_value=make_settings(tmp)
        ), patch(
            "paopao_radar.cli._model_registry_core_function", return_value=core
        ), patch("sys.stdout", new_callable=StringIO):
            code = cli.run_model_register(args(
                version="", scenario="threshold_tuning", dry_run=True,
            ))
            self.assertEqual(code, 0)
            self.assertEqual(core.call_args.kwargs["scenario"], "threshold_tuning")
            self.assertTrue(core.call_args.kwargs["dry_run"])
            self.assertFalse((Path(tmp) / "model_registry.db").exists())

    def test_approval_dry_run_uses_real_core_without_writes(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            bootstrap_production_model(settings)
            register_candidate(
                settings,
                model_key="signal-decision",
                version="candidate-test-v1",
                parameters={"base_model_version": "signal-decision-v1.1", "changes": {"probe": 75}},
                status="simulation",
            )
            db = Path(tmp) / "model_registry.db"
            conn = sqlite3.connect(db)
            try:
                before = int(conn.execute("SELECT COUNT(*) FROM model_approvals").fetchone()[0])
            finally:
                conn.close()
            with patch("paopao_radar.cli.Settings.load", return_value=settings), patch(
                "sys.stdout", new_callable=StringIO
            ):
                code = cli.run_model_approve(args(dry_run=True))
            conn = sqlite3.connect(db)
            try:
                after = int(conn.execute("SELECT COUNT(*) FROM model_approvals").fetchone()[0])
                status = str(conn.execute(
                    "SELECT status FROM models WHERE model_version='candidate-test-v1'"
                ).fetchone()[0])
            finally:
                conn.close()
            self.assertEqual(code, 0)
            self.assertEqual(before, after)
            self.assertEqual(status, "simulation")

    def test_simulation_cannot_skip_manual_approval_to_production(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            bootstrap_production_model(settings)
            register_candidate(
                settings,
                model_key="signal-decision",
                version="candidate-test-v1",
                parameters={"base_model_version": "signal-decision-v1.1", "changes": {"probe": 75}},
                status="simulation",
            )
            with patch("paopao_radar.cli.Settings.load", return_value=settings), patch(
                "sys.stdout", new_callable=StringIO
            ):
                code = cli.run_model_approve(args(activate=True, confirm_production=True))
            self.assertNotEqual(code, 0)
            self.assertEqual(
                ModelRegistryStore(settings).get("signal-decision", "candidate-test-v1")["status"],
                "simulation",
            )

    def test_model_health_refreshes_timeline_but_respects_dry_run(self) -> None:
        core = Mock(return_value={"ok": True, "snapshots": [], "dry_run": True})
        with TemporaryDirectory() as tmp, patch(
            "paopao_radar.cli.Settings.load", return_value=make_settings(tmp)
        ), patch(
            "paopao_radar.cli._model_registry_core_function", return_value=core
        ), patch("sys.stdout", new_callable=StringIO):
            code = cli.run_model_health(args(version="", dry_run=True))
            self.assertEqual(code, 0)
            self.assertTrue(core.call_args.kwargs["refresh"])
            self.assertTrue(core.call_args.kwargs["dry_run"])
            self.assertFalse((Path(tmp) / "model_registry.db").exists())


class ModelRegistryApiSurfaceTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_runtime_cache()

    def test_public_read_does_not_create_uninitialized_database(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            payload = public_model_registry_payload("current", settings=settings)
            self.assertFalse(payload["ok"])
            self.assertEqual(payload["code"], "model_registry_not_initialized")
            self.assertFalse((Path(tmp) / "model_registry.db").exists())

    def test_public_payloads_project_real_core_and_hide_parameters(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            bootstrap_production_model(settings)
            model = current_model(settings)
            self.assertIsNotNone(model)
            store = ModelRegistryStore(settings)
            with store.transaction() as conn:
                conn.execute(
                    "INSERT INTO model_performance_snapshots("
                    "model_id,period,sample_count,success_ratio,avg_return,avg_drawdown,risk_score,metrics_json,created_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?)",
                    (int(model["id"]), "30d", 100, 0.64, 2.5, -1.8, 20.0,
                     json.dumps({"period": "30d", "sample_count": 100, "success_ratio": 0.64}),
                     "2026-07-12T00:00:00+00:00"),
                )
            for section in ("current", "history", "performance", "health"):
                payload = public_model_registry_payload(section, settings=settings)
                self.assertTrue(payload["ok"], section)
                serialized = json.dumps(payload, ensure_ascii=False).lower()
                for forbidden in (
                    "parameters", "source_commit", "approved_by", "reason", "database", "/home/ubuntu",
                ):
                    self.assertNotIn(forbidden, serialized)
            performance = public_model_registry_payload("performance", settings=settings)["data"]
            self.assertEqual(performance["model_key"], "signal-decision")
            self.assertEqual(performance["periods"][0]["period"], "30d")
            health = public_model_registry_payload("health", settings=settings)["data"]
            self.assertIn(health["health_status"], {"healthy", "warning", "degraded", "deprecated"})
            self.assertFalse(health["auto_action"])

    def test_private_list_supports_real_core_list_shape(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            bootstrap_production_model(settings)
            payload = model_list_payload(settings=settings)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["data"]["count"], 1)
            self.assertEqual(payload["data"]["items"][0]["status"], "production")

    def test_private_routes_require_auth(self) -> None:
        for path in (
            "/api/models/list", "/api/models/detail", "/api/models/diff",
            "/api/models/register", "/api/models/approve", "/api/models/reject", "/api/models/rollback",
        ):
            handler, statuses = make_handler(path)
            if path.startswith("/api/models/") and path.rsplit("/", 1)[-1] in {
                "register", "approve", "reject", "rollback",
            }:
                web.WebHandler.do_POST(handler)
            else:
                web.WebHandler.do_GET(handler)
            self.assertEqual(statuses[-1], 401, path)

    def test_public_routes_are_wired_without_authentication(self) -> None:
        with patch(
            "paopao_radar.web.public_model_registry_payload",
            return_value={"ok": True, "data": {}},
        ) as payload:
            for section in ("current", "history", "performance", "health"):
                handler, statuses = make_handler(f"/public-api/models/{section}")
                web.WebHandler.do_GET(handler)
                self.assertEqual(statuses[-1], 200)
                self.assertEqual(payload.call_args.args[0], section)

    def test_private_approval_uses_authenticated_identity_and_returns_job_id(self) -> None:
        handler, _statuses = make_handler("/api/models/approve")
        handler.read_json = lambda: {
            "model": "signal-decision", "version": "candidate-test-v1",
            "reason": "manual review complete", "approved_by": "spoofed-user",
        }
        captured: dict[str, object] = {}
        handler.send_audited_json = lambda route, data, result, **kwargs: captured.update({
            "route": route, "result": result, "kwargs": kwargs,
        })
        with patch.object(web.WebHandler, "require_auth", return_value=True), patch(
            "paopao_radar.web.session_payload", return_value={"username": "real-admin"}
        ), patch(
            "paopao_radar.web.create_job_payload",
            return_value={"ok": True, "job_id": 42, "job": {"id": 42}},
        ) as create:
            web.WebHandler.do_POST(handler)
        self.assertEqual(captured["result"]["job_id"], 42)  # type: ignore[index]
        self.assertEqual(create.call_args.args[0], "model-approve")
        self.assertEqual(create.call_args.args[1]["approved_by"], "real-admin")
        self.assertEqual(captured["route"], "/api/models/approve")

    def test_all_private_writes_are_audited_jobs(self) -> None:
        cases = {
            "/api/models/register": ("model-register", {
                "model": "signal-decision", "version": "candidate-test-v1",
            }),
            "/api/models/approve": ("model-approve", {
                "model": "signal-decision", "version": "candidate-test-v1", "reason": "approved",
            }),
            "/api/models/reject": ("model-reject", {
                "model": "signal-decision", "version": "candidate-test-v1", "reason": "rejected",
            }),
            "/api/models/rollback": ("model-rollback", {
                "model": "signal-decision", "version": "signal-decision-v1.1", "reason": "rollback intent",
            }),
        }
        for path, (job_type, body) in cases.items():
            handler, _statuses = make_handler(path)
            handler.read_json = lambda body=body: body
            captured: dict[str, object] = {}
            handler.send_audited_json = lambda route, data, result, **kwargs: captured.update({
                "route": route, "result": result,
            })
            with patch.object(web.WebHandler, "require_auth", return_value=True), patch(
                "paopao_radar.web.session_payload", return_value={"username": "real-admin"}
            ), patch(
                "paopao_radar.web.create_job_payload",
                return_value={"ok": True, "job_id": 42, "job": {"id": 42}},
            ) as create:
                web.WebHandler.do_POST(handler)
            self.assertEqual(captured["route"], path)
            self.assertEqual(captured["result"]["job_id"], 42)  # type: ignore[index]
            self.assertEqual(create.call_args.args[0], job_type)


class ModelRegistryJobSurfaceTests(unittest.TestCase):
    def test_job_commands_validate_two_stage_activation(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            invalid = jobs.create_job_payload(
                "model-approve",
                {
                    "model": "signal-decision", "version": "candidate-test-v1",
                    "approved_by": "operator", "reason": "reviewed",
                    "activate": True, "confirm_production": False,
                },
                settings=settings,
                start=False,
            )
            valid = jobs.create_job_payload(
                "model-approve",
                {
                    "model": "signal-decision", "version": "candidate-test-v1",
                    "approved_by": "operator", "reason": "reviewed",
                },
                settings=settings,
                start=False,
            )
            command = valid["job"]["command"]
            self.assertFalse(invalid["ok"])
            self.assertEqual(invalid["code"], "invalid_job_scope")
            self.assertTrue(valid["ok"])
            self.assertNotIn("--activate", command)
            self.assertNotIn("--confirm-production", command)

    def test_registry_writes_share_guard_and_do_not_auto_schedule(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            first = jobs.create_job_payload(
                "model-register",
                {"model": "signal-decision", "version": "candidate-test-v1"},
                settings=settings,
                start=False,
            )
            blocked = jobs.create_job_payload(
                "model-reject",
                {
                    "model": "signal-decision", "version": "candidate-test-v1",
                    "approved_by": "operator", "reason": "failed review",
                },
                settings=settings,
                start=False,
            )
            self.assertTrue(first["ok"])
            self.assertFalse(blocked["ok"])
            self.assertEqual(blocked["code"], "model_registry_busy")
            self.assertNotIn("model-register", jobs.DELAYED_RESEARCH_JOB_TYPES)
            self.assertNotIn("model-approve", jobs.DELAYED_RESEARCH_JOB_TYPES)

    def test_register_job_accepts_scenario_without_version_but_rejects_bare_scope(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            valid = jobs.create_job_payload(
                "model-register",
                {"model": "signal-decision", "scenario": "threshold_tuning"},
                settings=settings,
                start=False,
            )
            store = jobs.store_for_settings(settings)
            store.finish_job(valid["job_id"], status="success", returncode=0)
            invalid = jobs.create_job_payload(
                "model-register", {"model": "signal-decision"},
                settings=settings, start=False,
            )
            self.assertTrue(valid["ok"])
            self.assertIn("--scenario", valid["job"]["command"])
            self.assertNotIn("--version", valid["job"]["command"])
            self.assertFalse(invalid["ok"])
            self.assertEqual(invalid["code"], "invalid_job_scope")

    def test_manual_model_write_jobs_cannot_be_rerun(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            created = jobs.create_job_payload(
                "model-approve",
                {
                    "model": "signal-decision", "version": "candidate-test-v1",
                    "approved_by": "operator", "reason": "manual review",
                },
                settings=settings,
                start=False,
            )
            store = jobs.store_for_settings(settings)
            store.finish_job(created["job_id"], status="success", returncode=0)
            rerun = jobs.rerun_job_payload(created["job_id"], settings=settings, start=False)
            self.assertFalse(rerun["ok"])
            self.assertEqual(rerun["code"], "manual_model_action_requires_new_request")

    def test_approval_core_requires_simulation_before_approval(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            bootstrap_production_model(settings)
            result = approve_model(
                settings,
                model_key="signal-decision",
                version="signal-decision-v1.1",
                approved_by="operator",
                reason="must not approve production again",
            )
            self.assertFalse(result["ok"])
            self.assertEqual(result["code"], "simulation_required_before_approval")


if __name__ == "__main__":
    unittest.main()
