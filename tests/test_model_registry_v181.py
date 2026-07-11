from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from paopao_radar.config import Settings
from paopao_radar.model_approval import (
    activate_model,
    approve_model,
    mark_simulation_complete,
    reject_model,
    rollback_model,
)
from paopao_radar.model_performance import (
    calculate_performance_snapshot,
    evaluate_model_health,
    generate_model_performance,
    model_performance,
)
from paopao_radar.model_registry import (
    MODEL_REGISTRY_SCHEMA_VERSION,
    ModelRegistryStore,
    bootstrap_production_model,
    canonical_model_hash,
    current_model,
    initialize_model_registry,
    list_models,
    model_diff,
    model_history,
    register_candidate,
    register_optimization_candidates,
    runtime_model_hash,
    runtime_model_snapshot,
)


NOW = datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc)


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ModelRegistryCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = Settings(
            data_dir=self.root,
            outcome_db_path=self.root / "outcomes.db",
            lifecycle_db_path=self.root / "lifecycle.db",
        )
        self.registry_path = self.root / "model_registry.db"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def bootstrap(self) -> dict:
        initialize_model_registry(self.settings)
        return bootstrap_production_model(
            self.settings, source_commit="abc123", released_at="2026-07-10T00:00:00+00:00"
        )

    def register_simulation(self, version: str = "candidate-v2", parameters: dict | None = None) -> dict:
        result = register_candidate(
            self.settings,
            version=version,
            parameters=parameters or {"model_version": version, "threshold": 75},
        )
        self.assertTrue(result["ok"])
        marked = mark_simulation_complete(
            self.settings, model_key="signal-decision", version=version,
            metrics_snapshot={"success_ratio_delta": 0.07},
        )
        self.assertTrue(marked["ok"])
        return marked

    def seed_outcomes(self) -> None:
        conn = sqlite3.connect(self.settings.outcome_db_path)
        conn.executescript(
            """
            CREATE TABLE signal_outcomes (
              id INTEGER PRIMARY KEY, signal_time TEXT, horizon TEXT,
              data_status TEXT, final_return_pct REAL,
              max_gain_pct REAL, max_drawdown_pct REAL
            );
            """
        )
        rows = [
            (1, "2026-07-10T01:00:00+00:00", "24h", "success", 4.0, 6.0, -1.0),
            (2, "2026-07-10T02:00:00+00:00", "24h", "success", -2.0, 1.0, -5.0),
            (3, "2026-07-10T03:00:00+00:00", "24h", "unavailable", None, None, None),
            (4, "2026-07-10T04:00:00+00:00", "72h", "pending", None, None, None),
        ]
        conn.executemany("INSERT INTO signal_outcomes VALUES(?,?,?,?,?,?,?)", rows)
        conn.commit()
        conn.close()

    def test_schema_initialization_is_idempotent_and_independent(self) -> None:
        first = initialize_model_registry(self.settings)
        second = initialize_model_registry(self.settings)
        self.assertTrue(first["ok"] and second["ok"])
        conn = sqlite3.connect(self.registry_path)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        indexes = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        conn.close()
        self.assertTrue({"models", "model_versions", "model_approvals", "model_performance_snapshots"}.issubset(tables))
        self.assertIn("ux_models_one_production", indexes)
        self.assertEqual(version, MODEL_REGISTRY_SCHEMA_VERSION)
        self.assertFalse(self.settings.outcome_db_path.exists())
        self.assertFalse(self.settings.lifecycle_db_path.exists())

    def test_public_style_reads_do_not_create_registry_file(self) -> None:
        self.assertIsNone(current_model(self.settings))
        self.assertEqual(list_models(self.settings), [])
        self.assertFalse(model_history(self.settings)["available"])
        self.assertFalse(self.registry_path.exists())

    def test_production_bootstrap_is_idempotent_and_hashes_runtime(self) -> None:
        first = self.bootstrap()
        second = bootstrap_production_model(self.settings, source_commit="other")
        model = current_model(self.settings)
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        self.assertEqual(model["model_version"], "signal-decision-v1.1")
        self.assertEqual(model["model_hash"], runtime_model_hash())
        self.assertEqual(model["source_commit"], "abc123")
        self.assertEqual(canonical_model_hash(runtime_model_snapshot()), runtime_model_hash())
        self.assertEqual(len([item for item in list_models(self.settings) if item["status"] == "production"]), 1)

    def test_candidate_registration_is_immutable_and_deduplicated(self) -> None:
        self.bootstrap()
        first = register_candidate(
            self.settings, version="candidate-v2",
            parameters={"model_version": "candidate-v2", "probe": 75},
        )
        same = register_candidate(
            self.settings, version="candidate-v2",
            parameters={"probe": 75, "model_version": "candidate-v2"},
        )
        conflict = register_candidate(
            self.settings, version="candidate-v2",
            parameters={"model_version": "candidate-v2", "probe": 80},
        )
        self.assertTrue(first["changed"])
        self.assertEqual(same["code"], "already_registered")
        self.assertEqual(conflict["code"], "immutable_version_conflict")
        self.assertEqual(len(list_models(self.settings, model_key="signal-decision")), 2)

    def test_approval_requires_simulation_and_never_auto_produces(self) -> None:
        self.bootstrap()
        register_candidate(
            self.settings, version="candidate-v2",
            parameters={"model_version": "candidate-v2", "threshold": 75},
        )
        blocked = approve_model(
            self.settings, model_key="signal-decision", version="candidate-v2",
            approved_by="operator", reason="reviewed",
        )
        self.assertEqual(blocked["code"], "simulation_required_before_approval")
        self.register_simulation("candidate-v3")
        approved = approve_model(
            self.settings, model_key="signal-decision", version="candidate-v3",
            approved_by="operator", reason="simulation reviewed",
        )
        self.assertTrue(approved["requires_activation"])
        self.assertEqual(approved["model"]["status"], "approved")
        self.assertEqual(current_model(self.settings)["model_version"], "signal-decision-v1.1")

    def test_activation_requires_second_confirmation_and_runtime_identity(self) -> None:
        self.bootstrap()
        self.register_simulation("candidate-v2")
        approve_model(
            self.settings, model_key="signal-decision", version="candidate-v2",
            approved_by="operator", reason="approved",
        )
        intent = activate_model(
            self.settings, model_key="signal-decision", version="candidate-v2",
            approved_by="operator", reason="deploy", confirm_production=False,
        )
        mismatch = activate_model(
            self.settings, model_key="signal-decision", version="candidate-v2",
            approved_by="operator", reason="deploy", confirm_production=True,
        )
        self.assertEqual(intent["code"], "confirmation_required")
        self.assertEqual(mismatch["code"], "runtime_model_mismatch")
        self.assertTrue(mismatch["manual_deployment_required"])
        self.assertEqual(current_model(self.settings)["model_version"], "signal-decision-v1.1")

    def test_explicit_runtime_verified_activation_and_rollback(self) -> None:
        self.bootstrap()
        params = {"model_version": "candidate-v2", "threshold": 75}
        self.register_simulation("candidate-v2", params)
        approve_model(
            self.settings, model_key="signal-decision", version="candidate-v2",
            approved_by="operator", reason="approved",
        )
        target_hash = canonical_model_hash(params)
        with patch("paopao_radar.model_approval.runtime_model_hash", return_value=target_hash), patch(
            "paopao_radar.model_approval.runtime_model_snapshot", return_value={"model_version": "candidate-v2"}
        ):
            activated = activate_model(
                self.settings, model_key="signal-decision", version="candidate-v2",
                approved_by="operator", reason="deployed manually", confirm_production=True,
            )
        self.assertTrue(activated["runtime_verified"])
        self.assertEqual(current_model(self.settings)["model_version"], "candidate-v2")
        old = next(item for item in list_models(self.settings) if item["model_version"] == "signal-decision-v1.1")
        self.assertEqual(old["status"], "deprecated")
        intent = rollback_model(
            self.settings, model_key="signal-decision", version="signal-decision-v1.1",
            approved_by="operator", reason="regression", confirm_production=False,
        )
        self.assertFalse(intent["changed"])
        self.assertEqual(current_model(self.settings)["model_version"], "candidate-v2")
        with patch("paopao_radar.model_approval.runtime_model_hash", return_value=old["model_hash"]), patch(
            "paopao_radar.model_approval.runtime_model_snapshot", return_value={"model_version": "signal-decision-v1.1"}
        ):
            rolled = rollback_model(
                self.settings, model_key="signal-decision", version="signal-decision-v1.1",
                approved_by="operator", reason="regression", confirm_production=True,
            )
        self.assertTrue(rolled["changed"])
        self.assertEqual(rolled["rollback"]["previous_model"], "candidate-v2")
        self.assertEqual(rolled["rollback"]["current_model"], "signal-decision-v1.1")

    def test_rejection_is_audited_and_production_cannot_be_rejected(self) -> None:
        self.bootstrap()
        self.register_simulation("candidate-v2")
        rejected = reject_model(
            self.settings, model_key="signal-decision", version="candidate-v2",
            approved_by="operator", reason="insufficient confidence",
        )
        blocked = reject_model(
            self.settings, model_key="signal-decision", version="signal-decision-v1.1",
            approved_by="operator", reason="no",
        )
        self.assertEqual(rejected["model"]["status"], "rejected")
        self.assertEqual(blocked["code"], "production_or_deprecated_model_cannot_be_rejected")
        history = model_history(self.settings)
        self.assertTrue(any(item["approval_status"] == "rejected" for item in history["approvals"]))

    def test_rollback_cannot_activate_approved_candidate(self) -> None:
        self.bootstrap()
        self.register_simulation("candidate-v2")
        approve_model(
            self.settings, model_key="signal-decision", version="candidate-v2",
            approved_by="operator", reason="approved",
        )
        target = ModelRegistryStore(self.settings).get("signal-decision", "candidate-v2")
        with patch("paopao_radar.model_approval.runtime_model_hash", return_value=target["model_hash"]), patch(
            "paopao_radar.model_approval.runtime_model_snapshot", return_value={"model_version": "candidate-v2"}
        ):
            result = rollback_model(
                self.settings, model_key="signal-decision", version="candidate-v2",
                approved_by="operator", reason="must use activation", confirm_production=True,
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "rollback_target_not_previously_production")
        self.assertEqual(current_model(self.settings)["model_version"], "signal-decision-v1.1")

    def test_generic_parameter_diff_is_stable(self) -> None:
        self.bootstrap()
        base = runtime_model_snapshot()
        candidate = json.loads(json.dumps(base))
        candidate["decision_thresholds"]["probe_min_total"] = 75
        register_candidate(self.settings, version="candidate-v2", parameters=candidate)
        result = model_diff(
            self.settings, model_key="signal-decision",
            base_version="signal-decision-v1.1", candidate_version="candidate-v2",
        )
        self.assertEqual(result["changes"], [{
            "parameter": "decision_thresholds.probe_min_total", "old": 70, "new": 75,
            "impact_scope": "candidate",
        }])

    def test_four_optimization_candidates_use_authoritative_factor_diff(self) -> None:
        self.bootstrap()
        comparisons = []
        scenarios = {
            "threshold_tuning": [("probe_min_total", 70, 75)],
            "risk_control": [("oi_divergence", 25, 35), ("funding_crowding", 20, 10)],
            "lifecycle_quality": [("spot_cvd", 10, 15), ("futures_cvd", 10, 5)],
            "module_rebalance": [("flow", 1.0, 1.1)],
        }
        for key, values in scenarios.items():
            comparisons.append({
                "scenario_key": key, "candidate_params": {name: new for name, _old, new in values},
                "factor_changes": [
                    {"factor": name, "old_value": old, "new_value": new, "label": name}
                    for name, old, new in values
                ],
                "status": "simulation_complete", "readiness": {"ready": False},
                "delta": {"success_ratio_delta": 0.01}, "confidence": {"score": 0.5},
            })
        report = {
            "ok": True, "source_signature": "source-1", "comparisons": comparisons,
        }
        with patch("paopao_radar.model_registry.get_optimization_report", return_value=report):
            registered = register_optimization_candidates(self.settings)
        self.assertTrue(registered["ok"])
        self.assertEqual(len(registered["registered"]), 4)
        for item, expected in zip(registered["registered"], scenarios.values()):
            model = item["model"]
            diff = model_diff(
                self.settings, model_key=model["model_key"], candidate_version=model["model_version"]
            )
            self.assertEqual(diff["diff_method"], "optimization_factor_changes")
            self.assertEqual(len(diff["changes"]), len(expected))
            self.assertLess(len(diff["changes"]), 10)

    def test_performance_excludes_pending_and_unavailable(self) -> None:
        self.bootstrap()
        self.seed_outcomes()
        outcome_hash = file_hash(self.settings.outcome_db_path)
        result = generate_model_performance(self.settings, dry_run=True)
        snapshot = next(item for item in result["snapshots"] if item["period"] == "all")
        self.assertEqual(snapshot["sample_count"], 2)
        self.assertEqual(snapshot["success_ratio"], 0.5)
        self.assertEqual(snapshot["avg_return"], 1.0)
        self.assertEqual(snapshot["avg_drawdown"], -3.0)
        self.assertEqual(snapshot["status_counts"], {"pending": 1, "success": 2, "unavailable": 1})
        self.assertEqual(snapshot["attribution_method"], "bootstrap_current_runtime_assumption")
        self.assertEqual(file_hash(self.settings.outcome_db_path), outcome_hash)

    def test_future_models_use_strict_deployment_window(self) -> None:
        model = {
            "id": 9, "model_key": "signal-decision", "model_version": "v2",
            "status": "deprecated", "released_at": "2026-07-10T01:30:00+00:00",
            "deprecated_at": "2026-07-10T03:30:00+00:00", "metadata": {},
        }
        rows = [
            {"signal_time": "2026-07-10T01:00:00+00:00", "data_status": "success", "final_return_pct": 8},
            {"signal_time": "2026-07-10T02:00:00+00:00", "data_status": "success", "final_return_pct": -2, "max_drawdown_pct": -3},
            {"signal_time": "2026-07-10T04:00:00+00:00", "data_status": "success", "final_return_pct": 9},
        ]
        result = calculate_performance_snapshot(rows, model=model, period="all", now=NOW)
        self.assertEqual(result["sample_count"], 1)
        self.assertEqual(result["avg_return"], -2.0)
        self.assertEqual(result["attribution_method"], "deployment_window")

    def test_performance_snapshots_persist_and_cached_read_does_not_recompute(self) -> None:
        self.bootstrap()
        self.seed_outcomes()
        written = generate_model_performance(self.settings)
        cached = model_performance(self.settings)
        self.assertTrue(written["changed"])
        self.assertEqual({item["period"] for item in cached["snapshots"]}, {"7d", "30d", "90d", "all"})
        self.assertTrue(cached["cached"])
        only_30d = model_performance(self.settings, period="30d")
        self.assertEqual([item["period"] for item in only_30d["snapshots"]], ["30d"])

    def test_model_health_thresholds_and_deprecated_state(self) -> None:
        model = {"status": "production"}
        healthy = evaluate_model_health(model, [
            {"period": "30d", "sample_count": 100, "success_ratio": 0.58},
            {"period": "all", "sample_count": 500, "success_ratio": 0.60},
        ])
        warning = evaluate_model_health(model, [
            {"period": "30d", "sample_count": 100, "success_ratio": 0.50},
            {"period": "all", "sample_count": 500, "success_ratio": 0.60},
        ])
        degraded = evaluate_model_health(model, [
            {"period": "30d", "sample_count": 100, "success_ratio": 0.40},
            {"period": "all", "sample_count": 500, "success_ratio": 0.60},
        ])
        deprecated = evaluate_model_health({"status": "deprecated"}, [])
        self.assertEqual(healthy["status"], "healthy")
        self.assertEqual(warning["status"], "warning")
        self.assertEqual(degraded["status"], "degraded")
        self.assertEqual(deprecated["status"], "deprecated")
        self.assertFalse(degraded["auto_replace"])

    def test_dry_run_does_not_create_database_or_modify_model_source(self) -> None:
        decision_path = Path(__file__).parents[1] / "paopao_radar" / "decision_model.py"
        before = file_hash(decision_path)
        bootstrap_production_model(self.settings, dry_run=True)
        register_candidate(
            self.settings, version="candidate-v2",
            parameters={"model_version": "candidate-v2", "threshold": 75}, dry_run=True,
        )
        self.assertFalse(self.registry_path.exists())
        self.assertEqual(file_hash(decision_path), before)

    def test_transaction_failure_rolls_back(self) -> None:
        initialize_model_registry(self.settings)
        store = ModelRegistryStore(self.settings)
        with self.assertRaises(RuntimeError):
            with store.transaction() as conn:
                conn.execute(
                    "INSERT INTO models(model_key,model_version,model_type,status,parameters_json,model_hash,created_at,updated_at) VALUES('x','v1','x','draft','{}','h','t','t')"
                )
                raise RuntimeError("fail")
        self.assertEqual(store.list(model_key="x"), [])


if __name__ == "__main__":
    unittest.main()
