from __future__ import annotations

import copy
import hashlib
import json
import math
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import paopao_radar.model_optimizer as optimizer
from paopao_radar import decision_model
from paopao_radar.config import Settings
from paopao_radar.lifecycle_intelligence_store import IntelligenceStore, LIFECYCLE_SCHEMA_VERSION
from paopao_radar.lifecycle_store import LifecycleStore
from paopao_radar.outcome_tracker import OutcomeStore


def settings_for(root: str) -> Settings:
    base = Path(root)
    return Settings(
        data_dir=base,
        lifecycle_db_path=base / "lifecycle.db",
        signal_events_db_path=base / "signals.db",
        outcome_db_path=base / "outcomes.db",
        web_jobs_db_path=base / "jobs.db",
    )


def sample(identifier: int, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": identifier,
        "signal_id": identifier,
        "lifecycle_id": identifier,
        "symbol": f"C{identifier % 12}USDT",
        "horizon": "24h" if identifier % 3 else "72h",
        "data_status": "success",
        "final_return_pct": 5 if identifier % 4 else -3,
        "max_gain_pct": 8,
        "max_drawdown_pct": -2 if identifier % 4 else -6,
        "decision_code": "probe",
        "decision_confidence": 82,
        "risk_level": "低",
        "module": ("launch", "flow", "structure", "funding")[identifier % 4],
        "lifecycle_score": 80,
        "lifecycle_risk_score": 30,
        "intelligence_score": 78,
        "price_change_from_first_pct": 5,
        "oi_change_from_first_pct": 10,
        "spot_cvd_change_from_first": 20,
        "futures_cvd_change_from_first": 15,
        "latest_funding_rate": 0.0001,
        "volume_multiplier": 2.0,
        "as_of_feature_status": "available",
        "temporal_feature_mode": "lifecycle_event_exact",
    }
    row.update(overrides)
    return row


def source_table_digest(path: Path) -> str:
    conn = sqlite3.connect(path)
    try:
        payload: dict[str, object] = {}
        for table in ("signal_lifecycles", "lifecycle_events", "lifecycle_outcome_links"):
            columns = [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]
            payload[table] = [
                dict(zip(columns, row))
                for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            ]
    finally:
        conn.close()
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


class OptimizationSchemaTests(unittest.TestCase):
    def test_schema_1800_is_idempotent_and_has_compatible_columns(self) -> None:
        with TemporaryDirectory() as tmp:
            store = IntelligenceStore(settings_for(tmp))
            store.ensure_schema()
            store.ensure_schema()
            with store.connect() as conn:
                version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master")}
                scenario_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(optimization_scenarios)")}
                run_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(optimization_runs)")}
                metric_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(optimization_metrics)")}
        self.assertEqual((LIFECYCLE_SCHEMA_VERSION, version), (1800, 1800))
        self.assertTrue({"optimization_scenarios", "optimization_runs", "optimization_metrics"}.issubset(tables))
        self.assertTrue({"scenario_name", "base_model_version", "scenario_version", "parameters_json", "status"}.issubset(scenario_columns))
        self.assertTrue({"sample_count", "mature_sample_count", "started_at", "finished_at", "result_json"}.issubset(run_columns))
        self.assertTrue({"metric_type", "metric_key", "production_value", "candidate_value", "delta_value", "metrics_json"}.issubset(metric_columns))

    def test_minimal_pre_release_schema_is_migrated_before_indexes(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = settings_for(tmp)
            conn = sqlite3.connect(settings.lifecycle_db_path)
            conn.executescript(
                """
                CREATE TABLE optimization_scenarios (
                  id INTEGER PRIMARY KEY, scenario_name TEXT, base_model_version TEXT,
                  scenario_version TEXT, scenario_type TEXT, parameters_json TEXT,
                  description TEXT, status TEXT, created_at TEXT, updated_at TEXT
                );
                CREATE TABLE optimization_runs (
                  id INTEGER PRIMARY KEY, scenario_id INTEGER, sample_count INTEGER,
                  mature_sample_count INTEGER, started_at TEXT, finished_at TEXT,
                  status TEXT, result_json TEXT
                );
                CREATE TABLE optimization_metrics (
                  id INTEGER PRIMARY KEY, run_id INTEGER, metric_type TEXT,
                  metric_key TEXT, production_value REAL, candidate_value REAL,
                  delta_value REAL, metrics_json TEXT
                );
                """
            )
            conn.commit()
            conn.close()
            IntelligenceStore(settings).ensure_schema()
            conn = sqlite3.connect(settings.lifecycle_db_path)
            try:
                indexes = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
                columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(optimization_runs)")}
            finally:
                conn.close()
        self.assertIn("idx_optimization_runs_signature", indexes)
        self.assertIn("source_signature", columns)

    def test_bundle_transaction_rolls_back(self) -> None:
        with TemporaryDirectory() as tmp:
            store = IntelligenceStore(settings_for(tmp))
            scenario = optimizer.list_optimization_scenarios()["scenarios"][0]
            with self.assertRaises(RuntimeError):
                with store.transaction() as conn:
                    store.write_optimization_bundle([scenario], [], conn=conn)
                    raise RuntimeError("rollback")
            with store.connect() as conn:
                count = int(conn.execute("SELECT COUNT(*) FROM optimization_scenarios").fetchone()[0])
        self.assertEqual(count, 0)


class OptimizationValidationTests(unittest.TestCase):
    def test_candidate_parameter_whitelist_and_ranges(self) -> None:
        valid = optimizer.validate_candidate_params(
            {"min_probe_confidence": 75, "probe_high_confidence": 80},
            scenario="threshold_tuning",
        )
        self.assertEqual(valid["min_probe_confidence"], 75)
        with self.assertRaises(ValueError):
            optimizer.validate_candidate_params({"unknown_weight": 1}, scenario="risk_control")
        with self.assertRaises(ValueError):
            optimizer.validate_candidate_params({"min_probe_confidence": 101}, scenario="threshold_tuning")
        with self.assertRaises(ValueError):
            optimizer.validate_candidate_params({"spot_cvd_weight": 15}, scenario="risk_control")
        with self.assertRaises(ValueError):
            optimizer.validate_candidate_params({"min_confidence": True}, scenario="module_rebalance")

    def test_production_snapshot_is_deeply_detached_and_fingerprint_stable(self) -> None:
        before = optimizer.production_model_fingerprint()
        snapshot = optimizer.production_model_snapshot()
        snapshot["decision_thresholds"]["probe_min_total"] = -999
        snapshot["module_weights"]["launch"] = -5
        self.assertEqual(decision_model.DEFAULT_DECISION_THRESHOLDS["probe_min_total"], 70)
        self.assertEqual(decision_model.MODULE_WEIGHTS["launch"], 1.0)
        self.assertEqual(before, optimizer.production_model_fingerprint())
        with self.assertRaises(TypeError):
            optimizer.PRODUCTION_MODEL["model_version"] = "mutated"  # type: ignore[index]

    def test_builtin_factor_changes_are_explicit_and_draft_only(self) -> None:
        scenarios = optimizer.list_optimization_scenarios()["scenarios"]
        by_key = {item["scenario_key"]: item for item in scenarios}
        self.assertEqual({item["status"] for item in scenarios}, {"draft"})
        threshold_values = {item["new_value"] for item in by_key["threshold_tuning"]["factor_changes"]}
        self.assertTrue({75.0, 80.0}.issubset(threshold_values))
        risk = {item["factor"]: item for item in by_key["risk_control"]["factor_changes"]}
        self.assertEqual((risk["oi_divergence"]["old_value"], risk["oi_divergence"]["new_value"]), (25.0, 35.0))
        lifecycle = {item["factor"]: item for item in by_key["lifecycle_quality"]["factor_changes"]}
        self.assertEqual((lifecycle["spot_cvd"]["old_value"], lifecycle["spot_cvd"]["new_value"]), (10.0, 15.0))
        self.assertEqual((lifecycle["futures_cvd"]["old_value"], lifecycle["futures_cvd"]["new_value"]), (10.0, 5.0))


class OptimizationMetricTests(unittest.TestCase):
    def test_metrics_delta_aliases_and_all_numbers_are_json_finite(self) -> None:
        production = optimizer.calculate_optimization_metrics([sample(i) for i in range(1, 121)])
        candidate = optimizer.calculate_optimization_metrics([
            sample(i, final_return_pct=6, max_drawdown_pct=-1) for i in range(1, 81)
        ])
        delta = optimizer.calculate_metric_delta(production, candidate)
        self.assertEqual(production["mature_sample_count"], production["sample_count"])
        self.assertEqual(production["avg_return"], production["avg_return_pct"])
        self.assertEqual(production["drawdown_ratio"], production["drawdown_event_ratio"])
        self.assertIn("risk_adjusted_score", production)
        self.assertGreater(delta["success_ratio_delta"], 0)
        encoded = json.dumps({"production": production, "candidate": candidate, "delta": delta}, allow_nan=False)
        self.assertNotIn("Infinity", encoded)

    def test_less_than_50_is_low_confidence_and_quality_affects_confidence(self) -> None:
        low = optimizer.calculate_optimization_metrics([sample(i) for i in range(1, 50)])
        low_confidence = optimizer.optimization_confidence(low)
        self.assertLess(low_confidence["score"], 0.5)
        self.assertEqual(low_confidence["label"], "low_confidence")

        base = optimizer.calculate_optimization_metrics([sample(i) for i in range(1, 201)])
        good = optimizer.calculate_optimization_metrics([
            sample(i, final_return_pct=7, max_drawdown_pct=-1) for i in range(1, 201)
        ])
        volatile = optimizer.calculate_optimization_metrics([
            sample(i, final_return_pct=20 if i % 2 else -18, max_drawdown_pct=-8)
            for i in range(1, 201)
        ])
        good_delta = optimizer.calculate_metric_delta(base, good)
        bad_delta = optimizer.calculate_metric_delta(base, volatile)
        good_conf = optimizer.optimization_confidence(good, production=base, delta=good_delta)
        bad_conf = optimizer.optimization_confidence(volatile, production=base, delta=bad_delta)
        self.assertGreater(good_conf["components"]["performance_delta_factor"], bad_conf["components"]["performance_delta_factor"])
        self.assertGreater(good_conf["components"]["variance_factor"], bad_conf["components"]["variance_factor"])
        self.assertGreater(good_conf["components"]["drawdown_change_factor"], bad_conf["components"]["drawdown_change_factor"])
        self.assertGreater(good_conf["score"], bad_conf["score"])

    def test_readiness_requires_horizons_delta_drawdown_distribution_and_confidence(self) -> None:
        production = {
            "avg_max_drawdown_pct": -4,
            "success_ratio": 0.50,
        }
        candidate = {
            "avg_max_drawdown_pct": -3,
            "success_ratio": 0.56,
            "unique_symbol_count": 8,
            "top_symbol_share": 0.25,
            "horizons": {"24h": {"sample_count": 100}, "72h": {"sample_count": 50}},
        }
        comparison = {
            "production": production,
            "candidate": candidate,
            "delta": {"success_ratio_delta": 0.06, "avg_drawdown_improvement_pct": 1.0},
            "confidence": {"score": 0.82},
        }
        ready = optimizer.evaluate_optimization_readiness(comparison, settings=SimpleNamespace())
        concentrated = copy.deepcopy(comparison)
        concentrated["candidate"]["top_symbol_share"] = 0.95
        blocked = optimizer.evaluate_optimization_readiness(concentrated, settings=SimpleNamespace())
        self.assertTrue(ready["ready"])
        self.assertFalse(blocked["ready"])
        self.assertIn("symbol_distribution", blocked["blocked"])

    def test_four_scenarios_expose_specialized_replay_metrics(self) -> None:
        samples = [
            sample(
                i,
                price_change_from_first_pct=-4 if i % 3 == 0 else 5,
                oi_change_from_first_pct=12,
                latest_funding_rate=0.001 if i % 5 == 0 else 0.0001,
                spot_cvd_change_from_first=-5 if i % 4 == 0 else 20,
                futures_cvd_change_from_first=20,
            )
            for i in range(1, 81)
        ]
        settings = SimpleNamespace()
        comparisons = {
            key: optimizer._simulate_one(
                optimizer._scenario_definition(key),
                samples,
                settings=settings,
                source_signature="source",
                scope={"global": False},
                generated_at="2026-07-11T00:00:00+00:00",
                risk_history={"summary": {"avg_lead_time_sec": 1800, "event_count": 8}},
            )
            for key in optimizer.SCENARIO_KEYS
        }
        threshold = comparisons["threshold_tuning"]["scenario_metrics"]
        risk = comparisons["risk_control"]["scenario_metrics"]
        lifecycle = comparisons["lifecycle_quality"]["scenario_metrics"]
        module = comparisons["module_rebalance"]["scenario_metrics"]
        self.assertEqual({item["probe_min_confidence"] for item in threshold["probe_threshold_variants"]}, {75.0, 80.0})
        self.assertEqual({item["variant"] for item in risk["risk_weight_variants"]}, {"A", "B"})
        self.assertIn("risk_hit_rate", risk)
        self.assertEqual(risk["warning_lead_time"]["avg_lead_time_sec"], 1800)
        self.assertEqual(risk["total_source_count"], risk["comparable_cohort_count"])
        self.assertEqual(risk["as_of_coverage_ratio"], 1.0)
        self.assertEqual(lifecycle["factor_contribution"]["candidate_weights"], {"spot_cvd": 15.0, "futures_cvd": 5.0})
        self.assertTrue(module["module_metrics"])
        for comparison in comparisons.values():
            self.assertIn("confidence", comparison["candidate"])
            self.assertTrue(all(item["auto_apply"] is False for item in comparison["recommendations"]))
            self.assertTrue(all("confidence" in item for item in comparison["recommendations"]))

    def test_risk_and_lifecycle_compare_candidate_to_same_asof_cohort(self) -> None:
        rows = [
            sample(i, as_of_feature_status="available" if i <= 60 else "unavailable")
            for i in range(1, 101)
        ]
        comparison = optimizer._simulate_one(
            optimizer._scenario_definition("risk_control"),
            rows,
            settings=SimpleNamespace(model_optimization_min_asof_coverage_ratio=0.5),
            source_signature="cohort",
            scope={"global": False},
            generated_at="2026-07-11T00:00:00+00:00",
        )
        self.assertEqual(comparison["source_production"]["sample_count"], 100)
        self.assertEqual(comparison["production"]["sample_count"], 60)
        self.assertLessEqual(comparison["candidate"]["sample_count"], 60)
        self.assertEqual(comparison["scenario_metrics"]["total_source_count"], 100)
        self.assertEqual(comparison["scenario_metrics"]["comparable_cohort_count"], 60)
        self.assertEqual(comparison["scenario_metrics"]["excluded_unavailable_count"], 40)
        self.assertEqual(comparison["scenario_metrics"]["as_of_coverage_ratio"], 0.6)


class OptimizationPersistenceTests(unittest.TestCase):
    def _seed(self, settings: Settings) -> None:
        symbols = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT")
        lifecycle_store = LifecycleStore(settings.lifecycle_db_path)
        lifecycle_rows: list[dict[str, object]] = []
        for index, symbol in enumerate(symbols, start=1):
            stored, _ = lifecycle_store.create_lifecycle({
                "symbol": symbol,
                "first_signal_id": 100 + index,
                "first_signal_at": "2026-07-01T00:00:00+00:00",
                "first_signal_module": ("launch", "flow", "structure", "funding")[index - 1],
                "first_signal_type": "launch",
                "first_signal_level": "15m",
                "current_state": "upgraded_4h",
                "highest_level": "4h",
                "lifecycle_score": 82,
                "risk_score": 30 if index != 4 else 65,
                "price_change_from_first_pct": 6 if index != 4 else -4,
                "oi_change_from_first_pct": 12,
                "spot_cvd_change_from_first": 20 if index != 4 else -5,
                "futures_cvd_change_from_first": 15,
                "latest_funding_rate": 0.001 if index == 4 else 0.0001,
                "metrics": {"volume_multiplier": 2.0},
                "is_active": 0,
            })
            lifecycle_rows.append(stored)
        store = IntelligenceStore(settings)
        store.ensure_schema()
        outcome_store = OutcomeStore(settings.outcome_db_path)
        signals = [
            {
                "id": 100 + index,
                "ts": 1782864000,
                "time": "2026-07-01T00:00:00+00:00",
                "symbol": symbol,
                "module": ("launch", "flow", "structure", "funding")[index - 1],
                "signal_type": "launch",
            }
            for index, symbol in enumerate(symbols, start=1)
        ]
        outcome_store.create_pending(signals, {"24h": 86400, "72h": 259200})
        with outcome_store.connect() as conn:
            rows = conn.execute("SELECT id,signal_id,horizon FROM signal_outcomes ORDER BY id").fetchall()
            for row in rows:
                success = int(row["signal_id"]) % 4 != 0
                conn.execute(
                    "UPDATE signal_outcomes SET data_status='success',final_return_pct=?,"
                    "max_gain_pct=?,max_drawdown_pct=?,decision_code='probe',"
                    "decision_label='可试仓',decision_confidence=82,risk_level=? WHERE id=?",
                    (5 if success else -4, 8 if success else 1, -2 if success else -7, "低" if success else "高", int(row["id"])),
                )
        with store.connect() as conn:
            now = "2026-07-12T00:00:00+00:00"
            for lifecycle_row in lifecycle_rows:
                signal_id = int(lifecycle_row["first_signal_id"])
                outcome_rows = outcome_store.list_by_signal_ids([signal_id])
                for offset, outcome_row in enumerate(outcome_rows):
                    conn.execute(
                        "INSERT INTO lifecycle_outcome_links ("
                        "lifecycle_id,symbol,signal_id,lifecycle_event_id,outcome_id,horizon,outcome_status,"
                        "link_role,link_method,link_confidence,signal_time,outcome_time,is_primary,created_at,updated_at"
                        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            int(lifecycle_row["id"]), lifecycle_row["symbol"], signal_id, None,
                            int(outcome_row["id"]), outcome_row["horizon"], "success", "first_signal",
                            "first_signal_id", 1.0, "2026-07-01T00:00:00+00:00", now,
                            1 if offset == 0 else 0, now, now,
                        ),
                    )
        signal_conn = sqlite3.connect(settings.signal_events_db_path)
        signal_conn.execute("CREATE TABLE marker (id INTEGER PRIMARY KEY, value TEXT)")
        signal_conn.execute("INSERT INTO marker(value) VALUES ('unchanged')")
        signal_conn.commit()
        signal_conn.close()

    def test_dry_run_and_scopes_never_create_or_persist(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = settings_for(tmp)
            dry = optimizer.run_optimization(settings, dry_run=True, write_reports=False)
            self.assertTrue(dry["ok"])
            self.assertFalse(dry["persisted"])
            self.assertFalse(settings.lifecycle_db_path.exists())
            scoped_error = optimizer.run_optimization(settings, symbol="BTCUSDT", dry_run=False, write_reports=False)
            self.assertFalse(scoped_error["ok"])
            self.assertFalse(settings.lifecycle_db_path.exists())

    def test_real_run_writes_only_optimization_tables_and_cache_survives_force(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = settings_for(tmp)
            self._seed(settings)
            decision_path = Path(decision_model.__file__ or "")
            hashes_before = {
                "decision": hashlib.sha256(decision_path.read_bytes()).hexdigest(),
                "signals": hashlib.sha256(settings.signal_events_db_path.read_bytes()).hexdigest(),
                "outcomes": hashlib.sha256(settings.outcome_db_path.read_bytes()).hexdigest(),
                "lifecycle_source": source_table_digest(settings.lifecycle_db_path),
            }
            first = optimizer.run_optimization(settings, force=False, write_reports=False)
            self.assertTrue(first["persisted"])
            self.assertEqual(len(first["run_ids"]), 4)
            with patch.object(optimizer, "_simulate_one", side_effect=AssertionError("cache miss")):
                cached = optimizer.run_optimization(settings, write_reports=False)
            self.assertTrue(cached["cached"])
            forced = optimizer.run_optimization(settings, force=True, write_reports=False)
            self.assertTrue(forced["persisted"])
            with patch.object(optimizer, "_simulate_one", side_effect=AssertionError("force poisoned cache")):
                after_force = optimizer.run_optimization(settings, write_reports=False)
            self.assertTrue(after_force["cached"])
            hashes_after = {
                "decision": hashlib.sha256(decision_path.read_bytes()).hexdigest(),
                "signals": hashlib.sha256(settings.signal_events_db_path.read_bytes()).hexdigest(),
                "outcomes": hashlib.sha256(settings.outcome_db_path.read_bytes()).hexdigest(),
                "lifecycle_source": source_table_digest(settings.lifecycle_db_path),
            }
            report = optimizer.get_optimization_report(settings)
            conn = sqlite3.connect(settings.lifecycle_db_path)
            try:
                counts = {
                    table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                    for table in ("optimization_scenarios", "optimization_runs", "optimization_metrics")
                }
            finally:
                conn.close()
        self.assertEqual(hashes_before, hashes_after)
        self.assertEqual(counts["optimization_scenarios"], 4)
        self.assertGreaterEqual(counts["optimization_runs"], 8)
        self.assertGreater(counts["optimization_metrics"], 0)
        self.assertTrue(report["complete"])
        self.assertEqual(len({item["source_signature"] for item in report["comparisons"]}), 1)

    def test_report_selects_latest_complete_signature_and_scenario_filter_is_explicit(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = settings_for(tmp)
            self._seed(settings)
            full = optimizer.run_optimization(settings, write_reports=False)
            original_signature = full["source_signature"]
            outcome_store = OutcomeStore(settings.outcome_db_path)
            with outcome_store.connect() as conn:
                outcome_id = int(conn.execute(
                    "SELECT id FROM signal_outcomes WHERE signal_id=101 AND horizon='24h'"
                ).fetchone()[0])
            outcome_store.update_outcome(outcome_id, {"final_return_pct": 6.25})
            partial = optimizer.run_optimization(
                settings, scenario="risk_control", force=True, write_reports=False
            )
            self.assertNotEqual(partial["source_signature"], original_signature)
            all_report = optimizer.get_optimization_report(settings)
            risk_report = optimizer.get_optimization_report(settings, scenario="risk_control")
            readiness = optimizer.optimization_readiness(settings, scenario="risk_control")
        self.assertTrue(all_report["complete"])
        self.assertEqual({item["source_signature"] for item in all_report["comparisons"]}, {original_signature})
        self.assertEqual(len(risk_report["comparisons"]), 1)
        self.assertEqual(risk_report["source_signature"], partial["source_signature"])
        self.assertIn("current", readiness)

    def test_exact_event_features_do_not_use_later_lifecycle_snapshot(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = settings_for(tmp)
            self._seed(settings)
            lifecycle_store = LifecycleStore(settings.lifecycle_db_path)
            btc = lifecycle_store.get_lifecycle("BTCUSDT")
            event, _ = lifecycle_store.insert_event({
                "lifecycle_id": int(btc["id"]),
                "symbol": "BTCUSDT",
                "event_time": "2026-07-01T00:00:00+00:00",
                "event_type": "first_signal",
                "event_level": "15m",
                "event_level_rank": 1,
                "signal_id": 101,
                "price_change_from_first_pct": 1.5,
                "volume_change_pct": 20,
                "oi_change_pct": 4,
                "futures_cvd_delta": 12,
                "spot_cvd_delta": 15,
                "funding_rate": 0.0002,
                "event_score": 72,
                "risk_score": 18,
                "dedup_key": "v180-asof-btc",
            })
            conn = sqlite3.connect(settings.lifecycle_db_path)
            try:
                conn.execute(
                    "UPDATE lifecycle_outcome_links SET lifecycle_event_id=? "
                    "WHERE lifecycle_id=? AND signal_id=101",
                    (int(event["id"]), int(btc["id"])),
                )
                conn.commit()
            finally:
                conn.close()
            before = optimizer._load_mature_samples(settings)
            before_btc = next(row for row in before["samples"] if row["symbol"] == "BTCUSDT")
            lifecycle_store.update_lifecycle("BTCUSDT", {
                "risk_score": 99,
                "lifecycle_score": 1,
                "price_change_from_first_pct": -80,
                "oi_change_from_first_pct": 200,
                "spot_cvd_change_from_first": -999,
                "futures_cvd_change_from_first": 999,
                "latest_funding_rate": 0.02,
            })
            after = optimizer._load_mature_samples(settings)
            after_btc = next(row for row in after["samples"] if row["symbol"] == "BTCUSDT")
        for key in (
            "lifecycle_score", "lifecycle_risk_score", "price_change_from_first_pct",
            "oi_change_from_first_pct", "spot_cvd_change_from_first",
            "futures_cvd_change_from_first", "latest_funding_rate",
        ):
            self.assertEqual(before_btc[key], after_btc[key])
        self.assertEqual(before_btc["as_of_feature_status"], "available")
        self.assertEqual(before["source_signature"], after["source_signature"])

    def test_generate_report_only_reads_persisted_runs_and_files_are_safe(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = settings_for(tmp)
            self._seed(settings)
            optimizer.run_optimization(settings, write_reports=False)
            with patch.object(optimizer, "run_optimization", side_effect=AssertionError("must not simulate")):
                report = optimizer.generate_optimization_report(settings, write_reports=False)
            root = Path(tmp)
            paths = optimizer.write_optimization_report_files(
                report,
                json_path=root / "optimization.json",
                markdown_path=root / "optimization.md",
            )
            text = Path(paths["json"]).read_text(encoding="utf-8") + Path(paths["markdown"]).read_text(encoding="utf-8")
        self.assertTrue(report["ok"])
        for token in ("chat_id", "topic_id", "payload_json", "Authorization", "Traceback"):
            self.assertNotIn(token, text)
        self.assertTrue(all(item["auto_apply"] is False for item in report["recommendations"]))


if __name__ == "__main__":
    unittest.main()
