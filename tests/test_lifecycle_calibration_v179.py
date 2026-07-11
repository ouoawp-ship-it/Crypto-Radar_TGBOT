from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.config import Settings
from paopao_radar import decision_model
from paopao_radar.lifecycle_calibration import (
    CALIBRATION_MODEL_VERSION,
    CALIBRATION_VERSION,
    build_calibration_report,
    calibration_validation_readiness,
    evaluate_calibration_validation_readiness,
    generate_calibration_report,
    get_calibration_report,
    write_calibration_report_files,
)
from paopao_radar.lifecycle_intelligence_store import (
    LIFECYCLE_SCHEMA_VERSION,
    IntelligenceStore,
)
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


def lifecycle(identifier: int = 1, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "lifecycle_id": identifier,
        "symbol": f"C{identifier}USDT",
        "first_signal_id": identifier * 10,
        "first_signal_at": "2026-07-01T00:00:00+00:00",
        "first_signal_module": "structure",
        "first_signal_type": "launch",
        "first_signal_level": "15m",
        "highest_level": "4h",
        "current_state": "upgraded_4h",
        "lifecycle_score": 82,
        "risk_score": 18,
        "price_change_from_first_pct": 8,
        "oi_change_from_first_pct": 12,
        "spot_cvd_change_from_first": 100,
        "futures_cvd_change_from_first": 80,
        "latest_funding_rate": 0.0001,
        "metrics_json": json.dumps({"volume_multiplier": 2.4}),
        "intelligence_score": 88,
        "upgrade_path": "15m → 1h → 4h",
        "duration_sec": 21600,
        "replay_result_label": "strong_success",
        "is_active": 0,
        "created_at": "2026-07-01T00:00:00+00:00",
        "updated_at": "2026-07-02T00:00:00+00:00",
        "closed_at": "2026-07-02T00:00:00+00:00",
    }
    row.update(overrides)
    return row


def outcome(identifier: int = 1, **overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": identifier,
        "signal_id": identifier * 10,
        "lifecycle_id": identifier,
        "lifecycle_event_id": None,
        "symbol": f"C{identifier}USDT",
        "horizon": "24h",
        "data_status": "success",
        "final_return_pct": 6,
        "max_gain_pct": 10,
        "max_drawdown_pct": -2,
        "decision_code": "probe",
        "decision_label": "可试仓",
        "decision_confidence": 80,
        "link_role": "first_signal",
    }
    row.update(overrides)
    return row


class CalibrationSchemaTests(unittest.TestCase):
    def test_schema_is_idempotent_and_report_store_is_atomic(self) -> None:
        with TemporaryDirectory() as tmp:
            store = IntelligenceStore(settings_for(tmp))
            store.ensure_schema()
            store.ensure_schema()
            with store.connect() as conn:
                names = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master")}
                version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            stored = store.write_calibration_report(
                {
                    "report_version": CALIBRATION_VERSION,
                    "model_version": CALIBRATION_MODEL_VERSION,
                    "source_signature": "stable-source",
                    "sample_count": 3,
                    "mature_sample_count": 2,
                    "summary": {"summary": {"sample_count": 3}},
                    "recommendations": [{"auto_apply": False}],
                },
                [{
                    "metric_type": "decision_label",
                    "metric_key": "probe",
                    "sample_count": 2,
                    "success_ratio": 0.5,
                    "avg_return_pct": 1.2,
                    "avg_max_drawdown_pct": -2,
                }],
            )
            latest = store.latest_calibration_report(source_signature="stable-source")
            with self.assertRaises(RuntimeError):
                with store.transaction() as conn:
                    store.write_calibration_report(
                        {
                            "report_version": CALIBRATION_VERSION,
                            "model_version": CALIBRATION_MODEL_VERSION,
                            "source_signature": "rollback-source",
                        },
                        [],
                        conn=conn,
                    )
                    raise RuntimeError("rollback")
            self.assertIsNone(store.latest_calibration_report(source_signature="rollback-source"))
        self.assertEqual(LIFECYCLE_SCHEMA_VERSION, 1800)
        self.assertEqual(version, 1800)
        self.assertIn("calibration_reports", names)
        self.assertIn("calibration_metrics", names)
        self.assertIn("idx_calibration_metrics_report_type", names)
        self.assertEqual(stored["id"], latest["id"])
        self.assertEqual(latest["metrics"][0]["metric_key"], "probe")


class CalibrationStatisticsTests(unittest.TestCase):
    def test_report_covers_decision_lifecycle_factors_risk_and_excludes_unavailable(self) -> None:
        lifecycles = [
            lifecycle(1),
            lifecycle(
                2,
                first_signal_level="1h",
                highest_level="1h",
                upgrade_path="1h",
                intelligence_score=35,
                replay_result_label="failed",
                price_change_from_first_pct=-5,
                oi_change_from_first_pct=10,
                spot_cvd_change_from_first=-20,
                futures_cvd_change_from_first=30,
                latest_funding_rate=0.001,
            ),
            lifecycle(3, replay_result_label="failed"),
        ]
        outcomes = [
            outcome(1, lifecycle_event_id=101),
            outcome(
                2,
                final_return_pct=-5,
                max_gain_pct=1,
                max_drawdown_pct=-8,
                decision_code="risk_alert",
                decision_confidence=90,
            ),
            outcome(
                3,
                data_status="unavailable",
                final_return_pct=None,
                max_gain_pct=None,
                max_drawdown_pct=None,
                decision_code="probe",
            ),
        ]
        events = [{
            "id": 101,
            "lifecycle_id": 1,
            "signal_id": 10,
            "event_time": "2026-07-01T02:00:00+00:00",
            "event_type": "risk_warning",
            "price": 100,
        }]
        frames = [{
            "lifecycle_id": 1,
            "frame_index": 2,
            "event_time": "2026-07-01T03:00:00+00:00",
            "price": 94,
        }]
        report = build_calibration_report(
            lifecycles,
            outcomes,
            events=events,
            frames=frames,
            base_readiness={"ready": False, "current": {"success_24h": 38}, "required": {"success_24h": 50}},
        )
        self.assertEqual(report["calibration_version"], "calibration-v1")
        self.assertEqual(report["model_version"], "signal-decision-v1.1")
        self.assertEqual(report["summary"]["sample_count"], 3)
        self.assertEqual(report["summary"]["mature_sample_count"], 2)
        self.assertEqual(report["summary"]["unavailable_count"], 1)
        self.assertIn("spot_cvd", report["factors"])
        self.assertIn("futures_cvd", report["factors"])
        self.assertIn("spot_futures_cvd", report["factors"])
        factor_labels = {
            item["label"]
            for key in ("oi_quadrants", "spot_cvd", "futures_cvd", "funding")
            for item in report["factors"][key]
        }
        self.assertIn("OI 增长 + 价格上涨", factor_labels)
        self.assertIn("Spot CVD 现货买盘确认", factor_labels)
        self.assertIn("Futures CVD 主动买入增强", factor_labels)
        self.assertIn("资金费率健康", factor_labels)
        self.assertIn("strong_success_ratio", report["intelligence_buckets"][-1])
        self.assertEqual(report["risk_alerts"]["summary"]["event_count"], 1)
        self.assertEqual(
            next(item for item in report["risk_alerts"]["items"] if item["metric_key"] == "risk_warning")["label"],
            "风险警报",
        )
        self.assertEqual(report["readiness"]["current"]["success_24h"], 38)
        self.assertEqual(report["readiness"]["required"]["success_24h"], 50)
        self.assertTrue(report["findings"])
        self.assertTrue(all(item["auto_apply"] is False for item in report["recommendations"]))

    def test_upgrade_oi_and_combination_metrics_include_relative_risk_results(self) -> None:
        report = build_calibration_report(
            [lifecycle(1), lifecycle(2, replay_result_label="failed", price_change_from_first_pct=-4)],
            [outcome(1), outcome(2, final_return_pct=-4, max_drawdown_pct=-7)],
            events=[{
                "id": 20,
                "lifecycle_id": 2,
                "signal_id": 20,
                "event_time": "2026-07-01T04:00:00+00:00",
                "event_type": "risk_warning",
                "price": 100,
            }],
            base_readiness={"ready": True},
        )
        paths = report["upgrade_paths"]
        self.assertTrue(all("risk_warning_ratio" in item for item in paths))
        self.assertTrue(all("risk_ratio" in item for item in report["factors"]["oi_quadrants"]))
        self.assertTrue(all("success_lift" in item for item in report["factors"]["combinations"]))

    def test_readiness_needs_mature_stable_samples_and_never_mutates_input(self) -> None:
        source = {
            "summary": {"mature_sample_count": 60, "mature_lifecycle_count": 20},
            "decision_labels": [
                {"metric_key": "probe", "mature_sample_count": 30},
                {"metric_key": "observe", "mature_sample_count": 20},
            ],
        }
        original = copy.deepcopy(source)
        ready = evaluate_calibration_validation_readiness(
            source,
            base_readiness={
                "ready": True,
                "current": {"success_24h": 55, "success_72h": 31},
                "required": {"success_24h": 50, "success_72h": 30},
            },
        )
        blocked = evaluate_calibration_validation_readiness(
            {"summary": {"mature_sample_count": 3}, "decision_labels": []},
            base_readiness={"ready": False},
        )
        self.assertTrue(ready["ready"])
        self.assertFalse(blocked["ready"])
        self.assertEqual(source, original)
        self.assertTrue(ready["does_not_modify_model"])

    def test_findings_cover_factor_intelligence_and_risk_effectiveness(self) -> None:
        lifecycles: list[dict[str, object]] = []
        outcomes: list[dict[str, object]] = []
        events: list[dict[str, object]] = []
        for identifier in range(1, 21):
            strong = identifier <= 10
            lifecycles.append(lifecycle(
                identifier,
                intelligence_score=90 if strong else 30,
                replay_result_label="strong_success" if strong else "failed",
                price_change_from_first_pct=8 if strong else -5,
                oi_change_from_first_pct=12,
                spot_cvd_change_from_first=100 if strong else -20,
                futures_cvd_change_from_first=80 if strong else 30,
            ))
            outcomes.append(outcome(
                identifier,
                lifecycle_event_id=1000 + identifier,
                final_return_pct=6 if strong else -4,
                max_drawdown_pct=-2 if strong else -7,
            ))
            events.append({
                "id": 1000 + identifier,
                "lifecycle_id": identifier,
                "signal_id": identifier * 10,
                "event_time": "2026-07-01T02:00:00+00:00",
                "event_type": "risk_warning",
                "price": 100,
            })
        report = build_calibration_report(
            lifecycles,
            outcomes,
            events=events,
            base_readiness={"ready": True},
        )
        finding_keys = {str(item.get("key")) for item in report["findings"]}
        self.assertIn("strongest_factor_combination", finding_keys)
        self.assertIn("weakest_factor_combination", finding_keys)
        self.assertIn("intelligence_bucket_ordering", finding_keys)
        self.assertIn("risk_alert_effectiveness", finding_keys)
        self.assertEqual(report["risk_alerts"]["summary"]["event_count"], 20)


class CalibrationGenerationTests(unittest.TestCase):
    def _seed(self, settings: Settings) -> None:
        lifecycle_store = LifecycleStore(settings.lifecycle_db_path)
        stored, _ = lifecycle_store.create_lifecycle({
            "symbol": "BTCUSDT",
            "first_signal_id": 10,
            "first_signal_at": "2026-07-01T00:00:00+00:00",
            "first_signal_module": "structure",
            "first_signal_type": "launch",
            "first_signal_level": "15m",
            "current_state": "launching",
            "highest_level": "1h",
            "lifecycle_score": 75,
            "risk_score": 20,
            "price_change_from_first_pct": 4,
            "oi_change_from_first_pct": 8,
            "spot_cvd_change_from_first": 4,
            "futures_cvd_change_from_first": 5,
            "latest_funding_rate": 0.0001,
            "metrics": {"volume_multiplier": 2.1},
            "is_active": 1,
        })
        IntelligenceStore(settings).ensure_schema()
        outcome_store = OutcomeStore(settings.outcome_db_path)
        outcome_store.create_pending(
            [{
                "id": 10,
                "ts": 1782864000,
                "time": "2026-07-01T00:00:00+00:00",
                "symbol": "BTCUSDT",
                "module": "structure",
                "signal_type": "launch",
            }],
            {"24h": 86400},
        )
        with outcome_store.connect() as conn:
            row = conn.execute(
                "SELECT id FROM signal_outcomes WHERE signal_id=10 AND horizon='24h'"
            ).fetchone()
            outcome_id = int(row[0])
            conn.execute(
                "UPDATE signal_outcomes SET data_status='success', final_return_pct=5, "
                "max_gain_pct=9, max_drawdown_pct=-2, decision_code='probe', "
                "decision_label='可试仓', decision_confidence=80 WHERE id=?",
                (outcome_id,),
            )
        with IntelligenceStore(settings).connect() as conn:
            now = "2026-07-12T00:00:00+00:00"
            conn.execute(
                "INSERT INTO lifecycle_outcome_links ("
                "lifecycle_id,symbol,signal_id,lifecycle_event_id,outcome_id,horizon,outcome_status,"
                "link_role,link_method,link_confidence,signal_time,outcome_time,is_primary,created_at,updated_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    int(stored["id"]), "BTCUSDT", 10, None, outcome_id, "24h", "success",
                    "first_signal", "first_signal_id", 1.0, "2026-07-01T00:00:00+00:00",
                    now, 1, now, now,
                ),
            )

    def test_nonexistent_database_dry_run_and_readiness_do_not_create_files(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = settings_for(tmp)
            result = generate_calibration_report(settings, dry_run=True, write_reports=False)
            readiness = calibration_validation_readiness(settings, report=result)
            self.assertTrue(result["ok"])
            self.assertFalse(result["persisted"])
            self.assertFalse(settings.lifecycle_db_path.exists())
            self.assertFalse(settings.outcome_db_path.exists())
            self.assertTrue(readiness["does_not_modify_model"])

    def test_global_persists_and_caches_but_scoped_and_dry_run_do_not(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = settings_for(tmp)
            self._seed(settings)
            first = generate_calibration_report(settings, write_reports=False)
            cached = generate_calibration_report(settings, write_reports=False)
            scoped = generate_calibration_report(
                settings, symbol="BTCUSDT", force=True, write_reports=False
            )
            limited = generate_calibration_report(settings, limit=1, force=True, write_reports=False)
            dry = generate_calibration_report(settings, dry_run=True, force=True, write_reports=False)
            conn = sqlite3.connect(settings.lifecycle_db_path)
            try:
                count = int(conn.execute("SELECT COUNT(*) FROM calibration_reports").fetchone()[0])
            finally:
                conn.close()
            loaded = get_calibration_report(settings)
        self.assertTrue(first["persisted"])
        self.assertTrue(cached["skipped"])
        self.assertFalse(scoped["persisted"])
        self.assertFalse(limited["persisted"])
        self.assertFalse(dry["persisted"])
        self.assertEqual(count, 1)
        self.assertTrue(loaded["cached"])
        self.assertEqual(loaded["model_version"], CALIBRATION_MODEL_VERSION)

    def test_generation_does_not_modify_decision_model_or_historical_outcomes(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = settings_for(tmp)
            self._seed(settings)
            model_path = Path(decision_model.__file__ or "")
            model_before = hashlib.sha256(model_path.read_bytes()).hexdigest()
            outcomes_before = hashlib.sha256(settings.outcome_db_path.read_bytes()).hexdigest()
            result = generate_calibration_report(settings, force=True, write_reports=False)
            model_after = hashlib.sha256(model_path.read_bytes()).hexdigest()
            outcomes_after = hashlib.sha256(settings.outcome_db_path.read_bytes()).hexdigest()
        self.assertTrue(result["persisted"])
        self.assertEqual(result["model_version"], decision_model.MODEL_VERSION)
        self.assertEqual(model_before, model_after)
        self.assertEqual(outcomes_before, outcomes_after)

    def test_report_files_are_aggregate_safe_and_have_required_sections(self) -> None:
        report = build_calibration_report(
            [lifecycle(1)],
            [outcome(1)],
            base_readiness={"ready": False},
        )
        report["recommendations"] = [{
            "key": "review",
            "priority": "low",
            "recommendation": "manual only",
            "auto_apply": False,
        }]
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = write_calibration_report_files(
                report,
                json_path=root / "report.json",
                markdown_path=root / "report.md",
            )
            json_text = Path(paths["json"]).read_text(encoding="utf-8")
            markdown = Path(paths["markdown"]).read_text(encoding="utf-8")
        for sensitive in ("chat_id", "topic_id", "payload_json", "Authorization", "Traceback"):
            self.assertNotIn(sensitive, json_text)
            self.assertNotIn(sensitive, markdown)
        for heading in (
            "## Decision Validation",
            "## First Signal Level",
            "## Intelligence Buckets",
            "## Factor: oi_quadrants",
            "## Risk Alert Validation",
            "## Anomalies",
            "## Review-only Recommendations",
        ):
            self.assertIn(heading, markdown)


if __name__ == "__main__":
    unittest.main()
