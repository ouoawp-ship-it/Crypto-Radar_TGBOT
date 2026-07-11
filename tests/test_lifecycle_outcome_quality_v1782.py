from __future__ import annotations

import copy
import json
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import paopao_radar.lifecycle_outcome_quality as quality_module
from paopao_radar.config import Settings
from paopao_radar.lifecycle_intelligence_store import IntelligenceStore, LIFECYCLE_SCHEMA_VERSION
from paopao_radar.lifecycle_outcome_quality import (
    build_candidate_record,
    calculate_quality_metrics,
    classify_outcome_candidate,
    classify_outcome_gaps,
    classify_provider_failure,
    evaluate_calibration_readiness,
    incremental_outcome_backfill,
    lifecycle_outcome_quality,
    refresh_outcome_candidates,
    retry_delay_seconds,
    stable_candidate_key,
    write_lifecycle_calibration_readiness_report,
    write_lifecycle_outcome_quality_report,
)
from paopao_radar.lifecycle_store import LifecycleStore
from paopao_radar.outcome_tracker import OUTCOME_WINDOWS, OutcomeStore


NOW = datetime(2026, 7, 11, 12, tzinfo=timezone.utc)


def settings_for(tmp: str) -> Settings:
    root = Path(tmp)
    return Settings(
        data_dir=root,
        lifecycle_db_path=root / "lifecycle.db",
        signal_events_db_path=root / "signals.db",
        outcome_db_path=root / "outcomes.db",
        web_jobs_db_path=root / "jobs.db",
    )


def signal(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "id": 10,
        "symbol": "BTCUSDT",
        "time": "2026-07-11T08:00:00+00:00",
        "module": "structure",
        "template_id": "STRUCTURE_ALERT",
        "signal_type": "launch",
        "status": "sent",
    }
    item.update(overrides)
    return item


def candidate_row(**overrides: object) -> dict[str, object]:
    item: dict[str, object] = {
        "candidate_key": "1:10:1h",
        "lifecycle_id": 1,
        "lifecycle_event_id": 1,
        "signal_id": 10,
        "symbol": "BTCUSDT",
        "signal_time": "2026-07-10T00:00:00+00:00",
        "source_module": "structure",
        "source_template": "STRUCTURE_ALERT",
        "source_signal_type": "launch",
        "first_signal_level": "15m",
        "horizon": "1h",
        "due_at": "2026-07-10T01:00:00+00:00",
        "eligibility_status": "eligible",
        "eligibility_reason": "outcome_success",
        "candidate_status": "success",
        "outcome_id": 100,
        "is_terminal": 1,
        "is_retryable": 0,
        "attempt_count": 1,
        "source_status": "sent",
    }
    item.update(overrides)
    return item


class CandidateSchemaStoreTests(unittest.TestCase):
    def test_candidate_schema_is_idempotent_and_indexed(self) -> None:
        with TemporaryDirectory() as tmp:
            store = IntelligenceStore(settings_for(tmp))
            store.ensure_schema()
            store.ensure_schema()
            with store.connect() as conn:
                names = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master")}
                version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        self.assertEqual(LIFECYCLE_SCHEMA_VERSION, 1800)
        self.assertEqual(version, 1800)
        self.assertIn("lifecycle_outcome_candidates", names)
        self.assertIn("idx_lifecycle_outcome_candidates_due", names)
        self.assertIn("idx_lifecycle_outcome_candidates_retry", names)

    def test_candidate_key_is_stable_and_unique_upsert_is_idempotent(self) -> None:
        key = stable_candidate_key(
            lifecycle_id=1, signal_id=10, lifecycle_event_id=3,
            signal_time="2026-07-10T00:00:00Z", horizon="1h",
        )
        self.assertEqual(key, "1:10:1h")
        legacy_a = stable_candidate_key(
            lifecycle_id=1, signal_id=None, lifecycle_event_id=3,
            signal_time="2026-07-10T00:00:00Z", horizon="4h",
        )
        legacy_b = stable_candidate_key(
            lifecycle_id=1, signal_id=0, lifecycle_event_id=3,
            signal_time="2026-07-10T00:00:00+00:00", horizon="4h",
        )
        self.assertEqual(legacy_a, legacy_b)
        with TemporaryDirectory() as tmp:
            store = IntelligenceStore(settings_for(tmp))
            row = candidate_row(candidate_key=key)
            first = store.upsert_outcome_candidates([row])
            second = store.upsert_outcome_candidates([row])
            with store.connect() as conn:
                count = conn.execute("SELECT COUNT(*) FROM lifecycle_outcome_candidates").fetchone()[0]
        self.assertEqual((first["inserted"], second["updated"], count), (1, 1, 1))

    def test_batch_transaction_rolls_back(self) -> None:
        with TemporaryDirectory() as tmp:
            store = IntelligenceStore(settings_for(tmp))
            store.ensure_schema()
            with self.assertRaises(RuntimeError):
                with store.transaction() as conn:
                    store.upsert_outcome_candidates([candidate_row()], conn=conn)
                    raise RuntimeError("rollback")
            with store.connect() as conn:
                count = conn.execute("SELECT COUNT(*) FROM lifecycle_outcome_candidates").fetchone()[0]
        self.assertEqual(count, 0)

    def test_processing_stale_recovers_but_fresh_processing_is_preserved(self) -> None:
        with TemporaryDirectory() as tmp:
            store = IntelligenceStore(settings_for(tmp))
            stale = candidate_row(
                candidate_key="1:10:1h", candidate_status="processing", is_terminal=0,
                last_attempt_at=(NOW - timedelta(hours=2)).isoformat(), outcome_id=None,
            )
            fresh = candidate_row(
                candidate_key="1:10:4h", horizon="4h", candidate_status="processing", is_terminal=0,
                last_attempt_at=(NOW - timedelta(minutes=5)).isoformat(), outcome_id=None,
            )
            store.upsert_outcome_candidates([stale, fresh])
            changed = store.recover_stale_outcome_candidates(NOW - timedelta(minutes=30))
            statuses = {row["candidate_key"]: row["candidate_status"] for row in store.list_outcome_candidates()}
        self.assertEqual(changed, 1)
        self.assertEqual(statuses["1:10:1h"], "ready")
        self.assertEqual(statuses["1:10:4h"], "processing")

    def test_claim_is_atomic_and_only_claims_ready_or_retry(self) -> None:
        with TemporaryDirectory() as tmp:
            store = IntelligenceStore(settings_for(tmp))
            rows = [
                candidate_row(candidate_key="1:10:1h", outcome_id=None, candidate_status="ready", is_terminal=0, attempt_count=0),
                candidate_row(candidate_key="1:10:4h", horizon="4h", outcome_id=None, candidate_status="not_due", is_terminal=0),
                candidate_row(
                    candidate_key="1:10:24h", horizon="24h", outcome_id=None,
                    candidate_status="retry_wait", is_terminal=0, is_retryable=1,
                    next_retry_at=(NOW + timedelta(hours=1)).isoformat(),
                ),
            ]
            store.upsert_outcome_candidates(rows)
            changed = store.claim_outcome_candidates([row["candidate_key"] for row in rows], now=NOW)
            stored = {row["candidate_key"]: row for row in store.list_outcome_candidates()}
        self.assertEqual(changed, 1)
        self.assertEqual(stored["1:10:1h"]["candidate_status"], "processing")
        self.assertEqual(stored["1:10:1h"]["attempt_count"], 1)
        self.assertEqual(stored["1:10:4h"]["candidate_status"], "not_due")
        self.assertEqual(stored["1:10:24h"]["candidate_status"], "retry_wait")

    def test_actionable_sql_filter_cannot_be_starved_by_success_rows(self) -> None:
        with TemporaryDirectory() as tmp:
            store = IntelligenceStore(settings_for(tmp))
            rows = [
                candidate_row(
                    candidate_key=f"1:{index}:1h", signal_id=index,
                    due_at=f"2026-07-{index:02d}T01:00:00+00:00",
                )
                for index in range(1, 11)
            ]
            rows.append(candidate_row(
                candidate_key="1:99:1h", signal_id=99, outcome_id=None,
                candidate_status="ready", is_terminal=0,
                due_at="2026-07-11T01:00:00+00:00",
            ))
            store.upsert_outcome_candidates(rows)
            actionable = store.list_outcome_candidates(
                eligibility_status="eligible", candidate_statuses=["ready", "retry_wait"],
                due_before=NOW, retry_due_before=NOW,
                exclude_eligibility_reasons=["signal_not_found"], limit=1,
            )
        self.assertEqual([row["candidate_key"] for row in actionable], ["1:99:1h"])

    def test_second_worker_cannot_claim_already_claimed_candidate(self) -> None:
        with TemporaryDirectory() as tmp:
            store = IntelligenceStore(settings_for(tmp))
            store.upsert_outcome_candidates([
                candidate_row(outcome_id=None, candidate_status="ready", is_terminal=0, attempt_count=0),
            ])
            first = store.claim_outcome_candidates(["1:10:1h"], now=NOW, return_keys=True)
            second = store.claim_outcome_candidates(["1:10:1h"], now=NOW, return_keys=True)
        self.assertEqual(first, ["1:10:1h"])
        self.assertEqual(second, [])

    def test_new_unique_source_recovers_previous_terminal_ineligible_candidate(self) -> None:
        with TemporaryDirectory() as tmp:
            store = IntelligenceStore(settings_for(tmp))
            unknown = candidate_row(
                outcome_id=None, eligibility_status="unknown",
                eligibility_reason="ambiguous_legacy_match",
                candidate_status="terminal_ineligible", is_terminal=1,
            )
            store.upsert_outcome_candidates([unknown])
            recovered = dict(unknown)
            recovered.update({
                "signal_id": 20,
                "eligibility_status": "eligible",
                "eligibility_reason": "backfill_not_attempted",
                "candidate_status": "ready",
                "is_terminal": 0,
            })
            store.upsert_outcome_candidates([recovered])
            row = store.get_outcome_candidate(str(unknown["candidate_key"]))
        self.assertEqual(row["eligibility_status"], "eligible")
        self.assertEqual(row["candidate_status"], "ready")
        self.assertEqual(row["signal_id"], 20)


class CandidateClassificationTests(unittest.TestCase):
    def test_valid_signal_is_ready_when_due_and_not_due_before_horizon(self) -> None:
        due = classify_outcome_candidate(signal(), None, "1h", NOW)
        future = classify_outcome_candidate(signal(time="2026-07-11T11:30:00+00:00"), None, "1h", NOW)
        self.assertEqual((due.eligibility_status, due.candidate_status), ("eligible", "ready"))
        self.assertEqual((future.eligibility_reason, future.candidate_status), ("not_due", "not_due"))

    def test_aggregate_announcement_test_and_delivery_status_reasons(self) -> None:
        cases = (
            ({"module": "summary"}, "aggregate_summary_signal"),
            ({"module": "announcement"}, "announcement_signal"),
            ({"module": "test"}, "test_signal"),
            ({"status": "dry_run"}, "dry_run_signal"),
            ({"status": "failed"}, "failed_signal"),
            ({"status": "blocked"}, "blocked_signal"),
            ({"status": "skipped"}, "skipped_signal"),
        )
        for overrides, expected in cases:
            with self.subTest(expected):
                result = classify_outcome_candidate(signal(**overrides), None, "1h", NOW)
                self.assertEqual((result.eligibility_status, result.eligibility_reason), ("ineligible", expected))
                self.assertEqual(result.candidate_status, "terminal_ineligible")

    def test_symbol_and_time_reasons(self) -> None:
        cases = (
            ({"symbol": ""}, "missing_symbol"),
            ({"symbol": "BTC!"}, "invalid_symbol"),
            ({"symbol": "BTCUSDC"}, "unsupported_quote_asset"),
            ({"symbol": "OKX:BTCUSDT"}, "non_binance_symbol"),
            ({"time": ""}, "missing_signal_time"),
            ({"time": "not-a-time"}, "invalid_signal_time"),
        )
        for overrides, expected in cases:
            with self.subTest(expected):
                result = classify_outcome_candidate(signal(**overrides), None, "1h", NOW)
                self.assertEqual(result.eligibility_reason, expected)

    def test_missing_signal_id_and_event_without_signal_are_distinct(self) -> None:
        missing = classify_outcome_candidate(signal(id=None), None, "1h", NOW)
        event = classify_outcome_candidate(signal(id=None), {"id": 9}, "1h", NOW)
        self.assertEqual(missing.eligibility_reason, "missing_signal_id")
        self.assertEqual(event.eligibility_reason, "lifecycle_event_without_signal")

    def test_legacy_unique_match_is_eligible_and_ambiguous_match_is_not_auto_linked(self) -> None:
        unique = classify_outcome_candidate(
            signal(id=None, legacy_match_unique=True), {"id": 9}, "1h", NOW,
        )
        ambiguous = classify_outcome_candidate(
            signal(id=None, legacy_ambiguous=True), {"id": 9}, "1h", NOW,
        )
        self.assertEqual((unique.eligibility_status, unique.candidate_status), ("eligible", "ready"))
        self.assertEqual((ambiguous.eligibility_status, ambiguous.eligibility_reason), ("unknown", "ambiguous_legacy_match"))

    def test_existing_success_linked_pending_and_unavailable(self) -> None:
        success = classify_outcome_candidate(signal(), None, "1h", NOW, outcome={"data_status": "success"})
        pending = classify_outcome_candidate(signal(), None, "1h", NOW, outcome={"data_status": "pending"})
        unavailable = classify_outcome_candidate(signal(), None, "1h", NOW, outcome={"data_status": "unavailable"})
        self.assertEqual(success.candidate_status, "success")
        self.assertEqual(pending.candidate_status, "linked")
        self.assertEqual(unavailable.candidate_status, "terminal_unavailable")
        self.assertTrue(unavailable.is_terminal)

    def test_provider_failures_retry_or_terminate(self) -> None:
        timeout = classify_provider_failure("ReadTimeout", attempt_count=1, now=NOW)
        rate = classify_provider_failure("HTTP 429 too many requests", attempt_count=1, now=NOW)
        invalid = classify_provider_failure("invalid symbol", attempt_count=1, now=NOW)
        exhausted = classify_provider_failure("connection reset", attempt_count=5, now=NOW)
        self.assertEqual((timeout.eligibility_reason, timeout.candidate_status), ("provider_timeout", "retry_wait"))
        self.assertEqual((rate.eligibility_reason, rate.candidate_status), ("provider_rate_limited", "retry_wait"))
        self.assertEqual((invalid.eligibility_reason, invalid.candidate_status), ("symbol_delisted", "terminal_unavailable"))
        self.assertEqual((exhausted.eligibility_reason, exhausted.candidate_status), ("retry_exhausted", "terminal_error"))

    def test_retry_backoff_is_exponential_and_bounded(self) -> None:
        self.assertEqual([retry_delay_seconds(index, base_sec=10, max_sec=25) for index in (1, 2, 3, 8)], [10, 20, 25, 25])

    def test_error_refresh_preserves_retry_deadline_then_becomes_ready(self) -> None:
        retry_at = NOW + timedelta(minutes=15)
        current = {
            "candidate_status": "retry_wait", "attempt_count": 1,
            "next_retry_at": retry_at.isoformat(), "eligibility_reason": "provider_timeout",
            "last_error_code": "provider_timeout", "last_error_summary": "timed out",
        }
        first = classify_outcome_candidate(
            signal(), None, "1h", NOW,
            outcome={"data_status": "error", "error": "timed out"}, current=current,
        )
        second = classify_outcome_candidate(
            signal(), None, "1h", NOW + timedelta(minutes=5),
            outcome={"data_status": "error", "error": "timed out"}, current=current,
        )
        due = classify_outcome_candidate(
            signal(), None, "1h", retry_at,
            outcome={"data_status": "error", "error": "timed out"}, current=current,
        )
        self.assertEqual(first.next_retry_at, retry_at.isoformat())
        self.assertEqual(second.next_retry_at, retry_at.isoformat())
        self.assertEqual(due.candidate_status, "ready")

    def test_processing_stale_classification_returns_ready(self) -> None:
        stale = classify_outcome_candidate(
            signal(), None, "1h", NOW,
            current={"candidate_status": "processing", "last_attempt_at": (NOW - timedelta(hours=1)).isoformat()},
            processing_stale_sec=1800,
        )
        fresh = classify_outcome_candidate(
            signal(), None, "1h", NOW,
            current={"candidate_status": "processing", "last_attempt_at": (NOW - timedelta(minutes=5)).isoformat()},
            processing_stale_sec=1800,
        )
        self.assertEqual(stale.candidate_status, "ready")
        self.assertEqual(fresh.candidate_status, "processing")

    def test_build_record_never_emits_generic_reason(self) -> None:
        record = build_candidate_record(
            {"id": 1, "symbol": "BTCUSDT"},
            {"signal_id": 10, "signal_time": "2026-07-10T00:00:00Z", "symbol": "BTCUSDT", "module": "flow"},
            signal(id=10, time="2026-07-10T00:00:00Z", module="flow"),
            "4h", now=NOW,
        )
        self.assertEqual(record["candidate_status"], "ready")
        self.assertNotEqual(record["eligibility_reason"], "no_outcome_row")


class QualityMetricTests(unittest.TestCase):
    def quality_rows(self) -> list[dict[str, object]]:
        return [
            candidate_row(),
            candidate_row(candidate_key="1:10:4h", horizon="4h", outcome_id=101),
            candidate_row(candidate_key="1:10:24h", horizon="24h", due_at="2026-07-12T00:00:00Z", outcome_id=None, candidate_status="not_due", eligibility_reason="not_due", is_terminal=0),
            candidate_row(candidate_key="2:20:1h", lifecycle_id=2, signal_id=20, source_module="flow", first_signal_level="1h", outcome_id=102, candidate_status="terminal_unavailable", eligibility_reason="historical_kline_unavailable"),
            candidate_row(candidate_key="2:20:4h", lifecycle_id=2, signal_id=20, source_module="flow", first_signal_level="1h", outcome_id=None, candidate_status="terminal_error", eligibility_reason="retry_exhausted"),
            candidate_row(candidate_key="3:30:1h", lifecycle_id=3, signal_id=30, source_module="summary", first_signal_level="unknown", outcome_id=None, eligibility_status="ineligible", eligibility_reason="aggregate_summary_signal", candidate_status="terminal_ineligible"),
        ]

    def test_five_coverage_metrics_have_distinct_denominators(self) -> None:
        quality = calculate_quality_metrics(self.quality_rows(), lifecycle_count=4, linked_lifecycle_count=2, now=NOW)
        summary = quality["summary"]
        self.assertEqual(summary["lifecycle_link_coverage_ratio"], 0.5)
        self.assertEqual(summary["eligible_candidate_count"], 5)
        self.assertEqual(summary["linked_candidate_count"], 3)
        self.assertEqual(summary["candidate_link_coverage_ratio"], 0.6)
        self.assertEqual(summary["due_candidate_count"], 4)
        self.assertEqual(summary["resolved_due_candidate_count"], 4)
        self.assertEqual(summary["due_resolution_ratio"], 1.0)
        self.assertEqual(summary["usable_outcome_maturity_ratio"], 0.5)
        self.assertEqual(summary["lifecycle_maturity_ratio"], 0.5)

    def test_not_due_is_not_in_due_denominator_and_unavailable_is_not_success(self) -> None:
        quality = calculate_quality_metrics(self.quality_rows(), lifecycle_count=3, linked_lifecycle_count=2, now=NOW)
        horizon = {item["horizon"]: item for item in quality["horizons"]}
        self.assertEqual(horizon["24h"]["not_due"], 1)
        self.assertEqual(horizon["1h"]["unavailable"], 1)
        self.assertEqual(horizon["1h"]["success"], 1)

    def test_plain_legacy_unavailable_is_not_resolved_until_terminally_classified(self) -> None:
        rows = [
            candidate_row(
                candidate_key="1:10:1h", candidate_status="unavailable",
                eligibility_reason="outcome_unavailable",
            ),
            candidate_row(
                candidate_key="1:10:4h", horizon="4h",
                candidate_status="terminal_unavailable",
                eligibility_reason="historical_kline_unavailable",
            ),
        ]
        summary = calculate_quality_metrics(
            rows, lifecycle_count=1, linked_lifecycle_count=1, now=NOW,
        )["summary"]
        self.assertEqual(summary["due_candidate_count"], 2)
        self.assertEqual(summary["resolved_due_candidate_count"], 1)
        self.assertEqual(summary["due_resolution_ratio"], 0.5)
        self.assertEqual(summary["successful_due_candidate_count"], 0)

    def test_module_level_signal_type_horizon_and_timeline_dimensions(self) -> None:
        quality = calculate_quality_metrics(self.quality_rows(), lifecycle_count=3, linked_lifecycle_count=2, now=NOW)
        self.assertEqual({row["module"] for row in quality["modules"]}, {"structure", "flow", "summary"})
        self.assertEqual({row["first_signal_level"] for row in quality["levels"]}, {"15m", "1h", "unknown"})
        self.assertEqual({row["signal_type"] for row in quality["signal_types"]}, {"launch"})
        self.assertEqual({row["horizon"] for row in quality["horizons"]}, {"1h", "4h", "24h"})
        self.assertEqual({row["time_range"] for row in quality["timeline"]}, {"24h", "7d", "30d", "all"})

    def test_readiness_fails_and_passes_without_mutating_quality(self) -> None:
        settings = SimpleNamespace(
            lifecycle_calibration_min_24h_success=2,
            lifecycle_calibration_min_72h_success=1,
            lifecycle_calibration_min_due_resolution_ratio=0.9,
            lifecycle_calibration_min_lifecycle_maturity_ratio=0.6,
            lifecycle_calibration_max_error_ratio=0.01,
        )
        low = calculate_quality_metrics(self.quality_rows(), lifecycle_count=3, linked_lifecycle_count=2, now=NOW)
        before = copy.deepcopy(low)
        blocked = evaluate_calibration_readiness(low, settings=settings)
        self.assertFalse(blocked["ready"])
        self.assertEqual(low, before)
        high = {
            "summary": {
                "due_resolution_ratio": 1.0, "lifecycle_maturity_ratio": 0.8,
                "real_error_ratio": 0, "generic_unclassified_count": 0,
            },
            "horizons": [
                {"horizon": "24h", "success": 2},
                {"horizon": "72h", "success": 1},
            ],
            "consistency": {"duplicate_links": 0, "multiple_primary": 0, "orphan_links": 0},
        }
        ready = evaluate_calibration_readiness(high, settings=settings)
        self.assertTrue(ready["ready"])
        self.assertIn("此处仅判断", ready["note"])


class QualityIntegrationTests(unittest.TestCase):
    @staticmethod
    def seed(settings: Settings) -> int:
        lifecycle, _ = LifecycleStore(settings.lifecycle_db_path).create_lifecycle({
            "symbol": "BTCUSDT",
            "first_signal_id": 10,
            "first_signal_at": "2026-07-10T00:00:00+00:00",
            "first_signal_module": "structure",
            "first_signal_template": "STRUCTURE_ALERT",
            "first_signal_type": "launch",
            "first_signal_level": "15m",
            "current_state": "warming",
            "highest_level": "15m",
            "latest_signal_id": 10,
            "latest_signal_at": "2026-07-10T00:00:00+00:00",
        })
        conn = sqlite3.connect(settings.signal_events_db_path)
        try:
            conn.execute(
                "CREATE TABLE signals (id INTEGER PRIMARY KEY, ts INTEGER, time TEXT, module TEXT, "
                "template_id TEXT, signal_type TEXT, symbol TEXT, status TEXT, stage TEXT, sent INTEGER, score REAL)"
            )
            conn.execute(
                "INSERT INTO signals VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    10, int(datetime(2026, 7, 10, tzinfo=timezone.utc).timestamp()),
                    "2026-07-10T00:00:00+00:00", "structure", "STRUCTURE_ALERT",
                    "launch", "BTCUSDT", "sent", "launch", 1, 70,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return int(lifecycle["id"])

    @staticmethod
    def seed_legacy(
        settings: Settings,
        signal_rows: list[tuple[int, str, str, str, str]],
    ) -> int:
        lifecycle, _ = LifecycleStore(settings.lifecycle_db_path).create_lifecycle({
            "symbol": "BTCUSDT",
            "first_signal_id": None,
            "first_signal_at": "2026-07-10T00:00:00+00:00",
            "first_signal_module": "structure",
            "first_signal_template": "STRUCTURE_ALERT",
            "first_signal_type": "launch",
            "first_signal_level": "15m",
            "current_state": "warming",
            "highest_level": "15m",
            "latest_signal_id": None,
            "latest_signal_at": None,
        })
        conn = sqlite3.connect(settings.signal_events_db_path)
        try:
            conn.execute(
                "CREATE TABLE signals (id INTEGER PRIMARY KEY, ts INTEGER, time TEXT, module TEXT, "
                "template_id TEXT, signal_type TEXT, symbol TEXT, status TEXT, stage TEXT, sent INTEGER, score REAL)"
            )
            for identifier, when, module, template, symbol in signal_rows:
                conn.execute(
                    "INSERT INTO signals VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        identifier, int(datetime.fromisoformat(when).timestamp()), when,
                        module, template, "launch", symbol, "sent", "launch", 1, 70,
                    ),
                )
            conn.commit()
        finally:
            conn.close()
        return int(lifecycle["id"])

    def test_legacy_without_persisted_link_unique_source_match_is_eligible(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = settings_for(tmp)
            lifecycle_id = self.seed_legacy(settings, [
                (20, "2026-07-10T00:02:00+00:00", "structure", "OTHER_TEMPLATE", "BTCUSDT"),
            ])
            result = refresh_outcome_candidates(settings, lifecycle_id=lifecycle_id, now=NOW)
            rows = IntelligenceStore(settings).list_outcome_candidates(lifecycle_id=lifecycle_id)
            def complete(signals: list[dict[str, object]], **kwargs: object) -> dict[str, object]:
                current_horizon = str(kwargs.get("horizon") or "1h")
                outcome_store = OutcomeStore(settings.outcome_db_path)
                outcome_store.ensure_schema()
                outcome_store.create_pending(signals, {current_horizon: OUTCOME_WINDOWS[current_horizon]})
                with outcome_store.connect() as conn:
                    conn.execute(
                        "UPDATE signal_outcomes SET data_status='success', final_return_pct=1.5 "
                        "WHERE signal_id=20 AND horizon=?",
                        (current_horizon,),
                    )
                return {"ok": True, "counts": {"success": 1}}

            with patch(
                "paopao_radar.lifecycle_outcome_quality.scan_signal_outcomes", side_effect=complete,
            ), patch(
                "paopao_radar.lifecycle_replay.rebuild_replays", return_value={"ok": True, "failed": 0},
            ), patch(
                "paopao_radar.lifecycle_intelligence.generate_intelligence", return_value={"ok": True, "failed": 0},
            ), patch(
                "paopao_radar.lifecycle_analytics.generate_lifecycle_analytics", return_value={"ok": True, "failed": 0},
            ):
                incremental = incremental_outcome_backfill(
                    settings, lifecycle_id=lifecycle_id, limit=1, now=NOW,
                )
            rows_after = IntelligenceStore(settings).list_outcome_candidates(lifecycle_id=lifecycle_id)
            with IntelligenceStore(settings).connect() as conn:
                linked_signal_ids = {
                    row[0]
                    for row in conn.execute(
                        "SELECT signal_id FROM lifecycle_outcome_links WHERE lifecycle_id=?",
                        (lifecycle_id,),
                    )
                }
        self.assertEqual(result["eligible"], 4)
        self.assertEqual({row["signal_id"] for row in rows}, {20})
        self.assertIn("ready", {row["candidate_status"] for row in rows})
        self.assertTrue({row["candidate_status"] for row in rows}.issubset({"ready", "not_due"}))
        self.assertTrue(all(":event:legacy:2026-07-10T00:00:00+00:00:" in row["candidate_key"] for row in rows))
        self.assertEqual(incremental["backfilled"], 1)
        self.assertIn("success", {row["candidate_status"] for row in rows_after})
        self.assertEqual(linked_signal_ids, {20})

    def test_legacy_without_persisted_link_multiple_matches_is_ambiguous(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = settings_for(tmp)
            lifecycle_id = self.seed_legacy(settings, [
                (20, "2026-07-10T00:01:00+00:00", "structure", "A", "BTCUSDT"),
                (21, "2026-07-10T00:02:00+00:00", "structure", "B", "BTCUSDT"),
            ])
            refresh_outcome_candidates(settings, lifecycle_id=lifecycle_id, now=NOW)
            rows = IntelligenceStore(settings).list_outcome_candidates(lifecycle_id=lifecycle_id)
        self.assertEqual({row["eligibility_status"] for row in rows}, {"unknown"})
        self.assertEqual({row["eligibility_reason"] for row in rows}, {"ambiguous_legacy_match"})
        self.assertEqual({row["signal_id"] for row in rows}, {None})

    def test_legacy_without_match_records_specific_failure_reason(self) -> None:
        cases = (
            ([(20, "2026-07-10T00:01:00+00:00", "structure", "A", "ETHUSDT")], "signal_not_found"),
            ([(20, "2026-07-10T01:00:00+00:00", "structure", "A", "BTCUSDT")], "time_mismatch"),
            ([(20, "2026-07-10T00:01:00+00:00", "flow", "FLOW_ALERT", "BTCUSDT")], "module_mismatch"),
        )
        for signal_rows, expected in cases:
            with self.subTest(expected), TemporaryDirectory() as tmp:
                settings = settings_for(tmp)
                lifecycle_id = self.seed_legacy(settings, signal_rows)
                refresh_outcome_candidates(settings, lifecycle_id=lifecycle_id, now=NOW)
                rows = IntelligenceStore(settings).list_outcome_candidates(lifecycle_id=lifecycle_id)
                self.assertEqual({row["eligibility_status"] for row in rows}, {"unknown"})
                self.assertEqual({row["eligibility_reason"] for row in rows}, {expected})

    def test_refresh_dry_run_does_not_create_candidate_table_then_real_is_idempotent(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = settings_for(tmp)
            self.seed(settings)
            dry = refresh_outcome_candidates(settings, dry_run=True, now=NOW)
            conn = sqlite3.connect(settings.lifecycle_db_path)
            try:
                table_after_dry = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='lifecycle_outcome_candidates'"
                ).fetchone()
            finally:
                conn.close()
            first = refresh_outcome_candidates(settings, now=NOW)
            second = refresh_outcome_candidates(settings, now=NOW)
            quality = lifecycle_outcome_quality(settings)
        self.assertEqual(dry["processed"], 4)
        self.assertIsNone(table_after_dry)
        self.assertEqual((first["inserted"], second["inserted"]), (4, 0))
        self.assertEqual(quality["summary"]["eligible_candidate_count"], 4)
        self.assertEqual(quality["summary"]["generic_unclassified_count"], 0)

    def test_scoped_refresh_reads_only_current_batch_candidate_keys(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = settings_for(tmp)
            lifecycle_id = self.seed(settings)
            store = IntelligenceStore(settings)
            store.upsert_outcome_candidates([
                candidate_row(
                    candidate_key=f"999:{index}:1h", lifecycle_id=999, signal_id=index,
                    outcome_id=None, candidate_status="ready", is_terminal=0,
                )
                for index in range(100, 140)
            ])
            with patch.object(
                quality_module,
                "_read_existing_candidates",
                wraps=quality_module._read_existing_candidates,
            ) as reader:
                refresh_outcome_candidates(settings, lifecycle_id=lifecycle_id, now=NOW)
            requested = list(reader.call_args.args[1])
        self.assertEqual(len(requested), 4)
        self.assertTrue(all(key.startswith(f"{lifecycle_id}:") for key in requested))
        self.assertFalse(any(key.startswith("999:") for key in requested))

    def test_gap_classification_reports_zero_generic_after(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = settings_for(tmp)
            self.seed(settings)
            result = classify_outcome_gaps(settings, now=NOW)
        self.assertEqual(result["generic_no_outcome_row_after"], 0)
        self.assertGreater(result["eligible_due"], 0)

    def test_gap_classification_migrates_legacy_coverage_generic_reason(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = settings_for(tmp)
            lifecycle_id = self.seed(settings)
            store = IntelligenceStore(settings)
            store.ensure_schema()
            with store.transaction() as conn:
                conn.execute(
                    "INSERT INTO lifecycle_outcome_coverage ("
                    "lifecycle_id,symbol,unlinked_reason,reasons_json,calculated_at,updated_at"
                    ") VALUES (?,?,?,?,?,?)",
                    (
                        lifecycle_id, "BTCUSDT", "no_outcome_row",
                        json.dumps({"reason_counts": {"no_outcome_row": 1}}),
                        NOW.isoformat(), NOW.isoformat(),
                    ),
                )
            result = classify_outcome_gaps(settings, now=NOW)
            with store.connect() as conn:
                row = conn.execute(
                    "SELECT unlinked_reason,reasons_json FROM lifecycle_outcome_coverage WHERE lifecycle_id=?",
                    (lifecycle_id,),
                ).fetchone()
            reasons = json.loads(str(row["reasons_json"]))
        self.assertEqual(result["generic_no_outcome_row_before"], 1)
        self.assertEqual(result["generic_no_outcome_row_after"], 0)
        self.assertEqual(result["legacy_generic_migrated"], 1)
        self.assertNotEqual(row["unlinked_reason"], "no_outcome_row")
        self.assertNotIn("no_outcome_row", reasons["reason_counts"])
        self.assertEqual(sum(reasons["candidate_quality_reason_counts"].values()), 1)

    def test_incremental_timeout_enters_retry_wait_and_leaves_no_processing(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = settings_for(tmp)
            self.seed(settings)
            refresh_outcome_candidates(settings, now=NOW)
            with patch(
                "paopao_radar.lifecycle_outcome_quality.scan_signal_outcomes",
                side_effect=TimeoutError("provider timed out"),
            ):
                result = incremental_outcome_backfill(settings, limit=1, now=NOW)
            rows = IntelligenceStore(settings).list_outcome_candidates(limit=20)
        self.assertEqual(result["retry"], 1)
        self.assertEqual(sum(row["candidate_status"] == "processing" for row in rows), 0)
        self.assertEqual(sum(row["candidate_status"] == "retry_wait" for row in rows), 1)

    def test_incremental_dry_run_does_not_refresh_derived_models(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = settings_for(tmp)
            self.seed(settings)
            with patch("paopao_radar.lifecycle_replay.rebuild_replays") as replay, patch(
                "paopao_radar.lifecycle_intelligence.generate_intelligence"
            ) as intelligence, patch(
                "paopao_radar.lifecycle_analytics.generate_lifecycle_analytics"
            ) as analytics:
                result = incremental_outcome_backfill(settings, limit=1, dry_run=True, now=NOW)
        self.assertTrue(result["dry_run"])
        replay.assert_not_called()
        intelligence.assert_not_called()
        analytics.assert_not_called()

    def test_incremental_captured_provider_error_uses_retry_backoff(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = settings_for(tmp)
            self.seed(settings)
            refresh_outcome_candidates(settings, now=NOW)

            def captured_error(signals: list[dict[str, object]], **kwargs: object) -> dict[str, object]:
                current_horizon = str(kwargs.get("horizon") or "1h")
                outcome_store = OutcomeStore(settings.outcome_db_path)
                outcome_store.ensure_schema()
                outcome_store.create_pending(signals, {current_horizon: OUTCOME_WINDOWS[current_horizon]})
                with outcome_store.connect() as conn:
                    conn.execute(
                        "UPDATE signal_outcomes SET data_status='error', error='provider timed out' "
                        "WHERE signal_id=10 AND horizon=?",
                        (current_horizon,),
                    )
                return {"ok": True, "counts": {"error": 1}}

            with patch(
                "paopao_radar.lifecycle_outcome_quality.scan_signal_outcomes",
                side_effect=captured_error,
            ), patch(
                "paopao_radar.lifecycle_replay.rebuild_replays",
                return_value={"ok": True, "failed": 0},
            ) as replay, patch(
                "paopao_radar.lifecycle_intelligence.generate_intelligence",
                return_value={"ok": True, "failed": 0},
            ) as intelligence, patch(
                "paopao_radar.lifecycle_analytics.generate_lifecycle_analytics",
                return_value={"ok": True, "failed": 0},
            ) as analytics:
                result = incremental_outcome_backfill(settings, limit=1, now=NOW)
            rows = IntelligenceStore(settings).list_outcome_candidates(limit=20)
            retried = [row for row in rows if row["candidate_status"] == "retry_wait"]
        self.assertEqual(result["retry"], 1)
        self.assertEqual(len(retried), 1)
        self.assertIsNotNone(retried[0]["next_retry_at"])
        self.assertEqual(sum(row["candidate_status"] == "processing" for row in rows), 0)
        self.assertEqual(result["changed_outcomes"], 1)
        self.assertEqual(result["refresh_failed"], 0)
        replay.assert_called_once()
        intelligence.assert_called_once()
        analytics.assert_called_once()

    def test_incremental_reclassifies_legacy_generic_rewritten_by_link_step(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = settings_for(tmp)
            lifecycle_id = self.seed(settings)
            refresh_outcome_candidates(settings, now=NOW)
            store = IntelligenceStore(settings)
            with store.transaction() as conn:
                conn.execute(
                    "INSERT INTO lifecycle_outcome_coverage ("
                    "lifecycle_id,symbol,unlinked_reason,reasons_json,calculated_at,updated_at"
                    ") VALUES (?,?,?,?,?,?)",
                    (
                        lifecycle_id, "BTCUSDT", "ready",
                        json.dumps({"reason_counts": {"backfill_not_attempted": 1}}),
                        NOW.isoformat(), NOW.isoformat(),
                    ),
                )

            def rewrite_generic(*_args: object, **_kwargs: object) -> dict[str, object]:
                with store.transaction() as conn:
                    conn.execute(
                        "UPDATE lifecycle_outcome_coverage SET unlinked_reason='no_outcome_row', "
                        "reasons_json=? WHERE lifecycle_id=?",
                        (json.dumps({"reason_counts": {"no_outcome_row": 1}}), lifecycle_id),
                    )
                return {"ok": True, "linked": 0}

            with patch(
                "paopao_radar.lifecycle_outcome_quality.scan_signal_outcomes",
                side_effect=TimeoutError("provider timed out"),
            ), patch(
                "paopao_radar.lifecycle_outcomes.link_lifecycle_outcomes",
                side_effect=rewrite_generic,
            ):
                incremental_outcome_backfill(settings, limit=1, now=NOW)
            with store.connect() as conn:
                row = conn.execute(
                    "SELECT unlinked_reason,reasons_json FROM lifecycle_outcome_coverage WHERE lifecycle_id=?",
                    (lifecycle_id,),
                ).fetchone()
            reasons = json.loads(str(row["reasons_json"]))
        self.assertNotEqual(row["unlinked_reason"], "no_outcome_row")
        self.assertNotIn("no_outcome_row", reasons["reason_counts"])

    def test_reports_are_atomic_structured_and_do_not_contain_sensitive_values(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            quality = calculate_quality_metrics([candidate_row()], lifecycle_count=1, linked_lifecycle_count=1, now=NOW)
            paths = write_lifecycle_outcome_quality_report(
                quality, json_path=root / "quality.json", markdown_path=root / "quality.md",
            )
            readiness = evaluate_calibration_readiness(quality, settings=SimpleNamespace())
            ready_path = write_lifecycle_calibration_readiness_report(readiness, json_path=root / "ready.json")
            text = (root / "quality.json").read_text(encoding="utf-8") + (root / "ready.json").read_text(encoding="utf-8")
            parsed = json.loads((root / "quality.json").read_text(encoding="utf-8"))
        self.assertTrue(parsed["ok"])
        self.assertIn("quality.json", paths["json"])
        self.assertIn("ready.json", ready_path["json"])
        self.assertNotIn("BOT_TOKEN", text)
        self.assertNotIn("/home/ubuntu", text)


if __name__ == "__main__":
    unittest.main()
