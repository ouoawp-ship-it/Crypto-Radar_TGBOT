from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from paopao_radar.config import Settings
from paopao_radar.lifecycle_intelligence_store import IntelligenceStore, LIFECYCLE_SCHEMA_VERSION
from paopao_radar.lifecycle_outcomes import (
    backfill_lifecycle_outcomes,
    extract_lifecycle_signal_candidates,
    lifecycle_outcome_detail,
    lifecycle_outcome_status,
    link_lifecycle_outcomes,
    reconcile_lifecycle_outcomes,
)
from paopao_radar.lifecycle_store import LifecycleStore
from paopao_radar.outcome_tracker import OutcomeStore


def make_settings(tmp: str) -> Settings:
    base = Path(tmp)
    return Settings(
        data_dir=base,
        signal_events_db_path=base / "signals.db",
        outcome_db_path=base / "outcomes.db",
        lifecycle_db_path=base / "lifecycle.db",
        web_jobs_db_path=base / "jobs.db",
        lifecycle_outcome_link_time_tolerance_sec=300,
    )


def seed_signals(settings: Settings, rows: list[tuple[int, str, str, str, str]]) -> None:
    conn = sqlite3.connect(settings.signal_events_db_path)
    try:
        conn.execute(
            """
            CREATE TABLE signals (
                id INTEGER PRIMARY KEY, ts INTEGER NOT NULL, time TEXT NOT NULL,
                module TEXT NOT NULL, template_id TEXT NOT NULL, signal_type TEXT NOT NULL,
                symbol TEXT NOT NULL, status TEXT NOT NULL, score REAL, stage TEXT
            )
            """
        )
        conn.executemany(
            "INSERT INTO signals(id, ts, time, module, template_id, signal_type, symbol, status, score, stage) "
            "VALUES (?, strftime('%s', ?), ?, ?, ?, 'launch', ?, 'sent', 70, 'launch')",
            [(identifier, when, when, module, template, symbol) for identifier, symbol, when, module, template in rows],
        )
        conn.commit()
    finally:
        conn.close()


def seed_lifecycle(
    settings: Settings,
    *,
    first_signal_id: int | None = 101,
    latest_signal_id: int | None = 104,
    with_events: bool = True,
    module: str = "structure",
    template: str = "STRUCTURE_ALERT",
) -> int:
    store = LifecycleStore(settings.lifecycle_db_path)
    lifecycle, _ = store.create_lifecycle({
        "symbol": "BTCUSDT",
        "first_signal_id": first_signal_id,
        "first_signal_at": "2026-07-01T00:00:00+00:00",
        "first_signal_module": module,
        "first_signal_template": template,
        "first_signal_type": "launch",
        "first_signal_level": "15m",
        "current_state": "upgraded_4h",
        "highest_level": "4h",
        "latest_signal_id": latest_signal_id,
        "latest_signal_at": "2026-07-01T03:00:00+00:00" if latest_signal_id else None,
    })
    lifecycle_id = int(lifecycle["id"])
    if with_events:
        event_rows = [
            (1, "2026-07-01T00:00:00+00:00", "first_signal", 101, "structure", "STRUCTURE_ALERT"),
            (2, "2026-07-01T01:00:00+00:00", "same_level_confirm", 102, "flow", "FLOW_ALERT"),
            (3, "2026-07-01T02:00:00+00:00", "timeframe_upgrade_4h", 103, "structure", "STRUCTURE_4H"),
            (4, "2026-07-01T03:00:00+00:00", "risk_warning", 104, "funding", "FUNDING_ALERT"),
            (5, "2026-07-01T04:00:00+00:00", "short_term_weakening", 0, "lifecycle_metrics", "LIFECYCLE_METRIC_REFRESH"),
        ]
        with store.transaction() as conn:
            for identifier, when, event_type, signal_id, source_module, source_template in event_rows:
                store.insert_event({
                    "lifecycle_id": lifecycle_id,
                    "symbol": "BTCUSDT",
                    "event_time": when,
                    "event_type": event_type,
                    "event_level": "15m",
                    "signal_id": signal_id,
                    "source_module": source_module,
                    "source_template": source_template,
                    "dedup_key": f"event-{identifier}",
                }, conn=conn)
    return lifecycle_id


def insert_outcome(
    settings: Settings,
    *,
    signal_id: int,
    horizon: str,
    status: str = "success",
    signal_time: str = "2026-07-01T00:00:00+00:00",
    module: str = "structure",
    final_return: float | None = 3.0,
) -> int:
    store = OutcomeStore(settings.outcome_db_path)
    store.ensure_schema()
    seconds = {"1h": 3600, "4h": 14400, "24h": 86400, "72h": 259200}[horizon]
    started = datetime.fromisoformat(signal_time)
    due = datetime.fromtimestamp(started.timestamp() + seconds, timezone.utc).isoformat()
    with store.connect() as conn:
        cursor = conn.execute(
            """
            INSERT INTO signal_outcomes (
                signal_id, symbol, coin, signal_time, horizon, horizon_sec, due_time,
                direction, final_return_pct, max_gain_pct, max_drawdown_pct, result_label,
                module, signal_type, data_status, created_at, updated_at
            ) VALUES (?, 'BTCUSDT', 'BTC', ?, ?, ?, ?, 'long', ?, 5, -2, 'ok', ?,
                      'launch', ?, '2026-07-10T00:00:00+00:00', '2026-07-10T00:00:00+00:00')
            """,
            (signal_id, signal_time, horizon, seconds, due, final_return, module, status),
        )
        return int(cursor.lastrowid)


class LifecycleOutcomeSchemaTests(unittest.TestCase):
    def test_schema_is_idempotent_and_enforces_one_primary(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            store = IntelligenceStore(settings)
            store.ensure_schema()
            store.ensure_schema()
            with closing(sqlite3.connect(settings.lifecycle_db_path)) as conn:
                names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
                version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        self.assertGreaterEqual(version, 1781)
        self.assertEqual(LIFECYCLE_SCHEMA_VERSION, 1800)
        self.assertIn("lifecycle_outcome_links", names)
        self.assertIn("lifecycle_outcome_coverage", names)
        self.assertIn("ux_lifecycle_outcome_links_one_primary", names)

    def test_store_batch_transaction_rolls_back(self) -> None:
        with TemporaryDirectory() as tmp:
            store = IntelligenceStore(make_settings(tmp))
            store.ensure_schema()
            with self.assertRaises(RuntimeError):
                with store.transaction() as conn:
                    conn.execute(
                        "INSERT INTO lifecycle_outcome_coverage "
                        "(lifecycle_id,symbol,calculated_at,updated_at) VALUES (1,'BTCUSDT','x','x')"
                    )
                    raise RuntimeError("rollback")
            with store.connect() as conn:
                count = conn.execute("SELECT COUNT(*) FROM lifecycle_outcome_coverage").fetchone()[0]
        self.assertEqual(count, 0)

    def test_actual_plan_batch_integrity_failure_rolls_back_links_and_coverage(self) -> None:
        with TemporaryDirectory() as tmp:
            store = IntelligenceStore(make_settings(tmp))
            store.ensure_schema()
            plans = [
                {
                    "links": [{
                        "lifecycle_id": 1, "symbol": "BTCUSDT", "signal_id": 101,
                        "outcome_id": outcome_id, "horizon": horizon,
                        "outcome_status": "success", "link_role": "first_signal",
                        "link_method": "first_signal_id",
                    }],
                    "coverage": {
                        "lifecycle_id": 1, "symbol": "BTCUSDT", "candidate_signal_count": 1,
                        "linked_signal_count": 1, "linked_outcome_count": 1,
                        "primary_outcome_id": outcome_id,
                        "reasons": {"primary_outcome_signal_id": 101},
                    },
                }
                for outcome_id, horizon in ((1, "1h"), (2, "4h"))
            ]
            with self.assertRaises(sqlite3.IntegrityError):
                store.write_outcome_plan_batch(plans)
            with store.connect() as conn:
                links = conn.execute("SELECT COUNT(*) FROM lifecycle_outcome_links").fetchone()[0]
                coverage = conn.execute("SELECT COUNT(*) FROM lifecycle_outcome_coverage").fetchone()[0]
        self.assertEqual((links, coverage), (0, 0))


class LifecycleOutcomeLinkTests(unittest.TestCase):
    def test_candidate_order_roles_and_metric_refresh_exclusion(self) -> None:
        lifecycle = {
            "id": 1, "symbol": "BTCUSDT", "first_signal_id": 101,
            "first_signal_at": "2026-07-01T00:00:00+00:00", "latest_signal_id": 104,
            "latest_signal_at": "2026-07-01T03:00:00+00:00",
        }
        events = [
            {"id": 1, "event_time": "2026-07-01T00:00:00+00:00", "event_type": "first_signal", "signal_id": 101},
            {"id": 2, "event_time": "2026-07-01T01:00:00+00:00", "event_type": "same_level_confirm", "signal_id": 102},
            {"id": 3, "event_time": "2026-07-01T02:00:00+00:00", "event_type": "timeframe_upgrade_4h", "signal_id": 103},
            {"id": 4, "event_time": "2026-07-01T03:00:00+00:00", "event_type": "risk_warning", "signal_id": 104},
            {"id": 5, "event_time": "2026-07-01T04:00:00+00:00", "event_type": "short_term_weakening", "signal_id": 0, "source_module": "lifecycle_metrics", "source_template": "LIFECYCLE_METRIC_REFRESH"},
        ]
        candidates = extract_lifecycle_signal_candidates(lifecycle, events)
        self.assertEqual([item["signal_id"] for item in candidates], [101, 102, 103, 104])
        self.assertEqual([item["link_role"] for item in candidates], ["first_signal", "same_level_confirm", "timeframe_upgrade", "risk_event"])

    def test_exact_first_event_latest_links_are_multi_signal_and_idempotent(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            seed_signals(settings, [
                (101, "BTCUSDT", "2026-07-01T00:00:00+00:00", "structure", "STRUCTURE_ALERT"),
                (102, "BTCUSDT", "2026-07-01T01:00:00+00:00", "flow", "FLOW_ALERT"),
                (103, "BTCUSDT", "2026-07-01T02:00:00+00:00", "structure", "STRUCTURE_4H"),
                (104, "BTCUSDT", "2026-07-01T03:00:00+00:00", "funding", "FUNDING_ALERT"),
            ])
            lifecycle_id = seed_lifecycle(settings)
            insert_outcome(settings, signal_id=101, horizon="1h")
            insert_outcome(settings, signal_id=101, horizon="4h", status="pending")
            insert_outcome(settings, signal_id=102, horizon="1h", signal_time="2026-07-01T01:00:00+00:00", module="flow")
            insert_outcome(settings, signal_id=103, horizon="4h", status="unavailable", signal_time="2026-07-01T02:00:00+00:00")
            insert_outcome(settings, signal_id=104, horizon="72h", status="error", signal_time="2026-07-01T03:00:00+00:00", module="funding")
            first = link_lifecycle_outcomes(settings, now=datetime(2026, 7, 10, tzinfo=timezone.utc))
            with closing(sqlite3.connect(settings.lifecycle_db_path)) as conn:
                first_link_ids = [row[0] for row in conn.execute(
                    "SELECT id FROM lifecycle_outcome_links ORDER BY outcome_id"
                ).fetchall()]
            second = link_lifecycle_outcomes(settings, now=datetime(2026, 7, 10, tzinfo=timezone.utc))
            detail = lifecycle_outcome_detail(settings, lifecycle_id=lifecycle_id)["data"]
            with closing(sqlite3.connect(settings.lifecycle_db_path)) as conn:
                count = conn.execute("SELECT COUNT(*) FROM lifecycle_outcome_links").fetchone()[0]
                primary = conn.execute("SELECT COUNT(*) FROM lifecycle_outcome_links WHERE is_primary=1").fetchone()[0]
                second_link_ids = [row[0] for row in conn.execute(
                    "SELECT id FROM lifecycle_outcome_links ORDER BY outcome_id"
                ).fetchall()]
        self.assertTrue(first["ok"])
        self.assertEqual(first["linked"], 5)
        self.assertEqual(second["linked"], 5)
        self.assertEqual(count, 5)
        self.assertEqual(primary, 1)
        self.assertEqual(second_link_ids, first_link_ids)
        self.assertEqual(detail["coverage"]["candidate_signal_count"], 4)
        self.assertEqual(detail["coverage"]["linked_signal_count"], 4)
        self.assertEqual(detail["coverage"]["mature_horizon_count"], 1)
        methods = {(item["signal_id"], item["link_method"]) for item in detail["links"]}
        self.assertIn((101, "first_signal_id"), methods)
        self.assertIn((102, "event_signal_id"), methods)
        self.assertIn((104, "event_signal_id"), methods)

    def test_link_uses_batch_store_not_per_lifecycle_writes(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            seed_signals(settings, [
                (101, "BTCUSDT", "2026-07-01T00:00:00+00:00", "structure", "STRUCTURE_ALERT"),
            ])
            seed_lifecycle(settings, latest_signal_id=101, with_events=False)
            insert_outcome(settings, signal_id=101, horizon="1h")
            with patch.object(IntelligenceStore, "replace_outcome_links", side_effect=AssertionError("N+1 link write")), \
                 patch.object(IntelligenceStore, "upsert_outcome_coverage", side_effect=AssertionError("N+1 coverage write")):
                result = link_lifecycle_outcomes(settings, now=datetime(2026, 7, 10, tzinfo=timezone.utc))
        self.assertTrue(result["ok"])
        self.assertEqual(result["linked"], 1)

    def test_global_batches_rotate_to_uncovered_lifecycles(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            store = LifecycleStore(settings.lifecycle_db_path)
            for index, symbol in enumerate(("AAAUSDT", "BBBUSDT", "CCCUSDT"), 1):
                store.create_lifecycle({
                    "symbol": symbol,
                    "first_signal_id": index,
                    "first_signal_at": f"2026-07-01T0{index}:00:00+00:00",
                    "first_signal_module": "flow",
                    "first_signal_level": "15m",
                    "current_state": "warming",
                    "latest_signal_id": index,
                    "latest_signal_at": f"2026-07-01T0{index}:00:00+00:00",
                })
            OutcomeStore(settings.outcome_db_path).ensure_schema()
            first = link_lifecycle_outcomes(settings, limit=2)
            second = link_lifecycle_outcomes(settings, limit=2)
        first_symbols = {item["symbol"] for item in first["items"]}
        second_symbols = {item["symbol"] for item in second["items"]}
        self.assertEqual(len(first_symbols), 2)
        self.assertEqual(len(second_symbols), 2)
        self.assertEqual(len(first_symbols | second_symbols), 3)

    def test_not_due_is_excluded_from_maturity_denominator(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            seed_signals(settings, [(101, "BTCUSDT", "2026-07-01T00:00:00+00:00", "structure", "STRUCTURE_ALERT")])
            seed_lifecycle(settings, latest_signal_id=101, with_events=False)
            OutcomeStore(settings.outcome_db_path).ensure_schema()
            link_lifecycle_outcomes(settings, now=datetime(2026, 7, 1, 0, 30, tzinfo=timezone.utc))
            status = lifecycle_outcome_status(settings)["data"]
        self.assertEqual(status["maturity_ratio"], 0.0)
        self.assertEqual(status["horizons"]["1h"]["not_due"], 1)
        self.assertEqual(status["horizons"]["72h"]["not_due"], 1)
        self.assertEqual(status["unlinked_reasons"], {"not_due": 1})

    def test_primary_uses_earliest_usable_event_when_first_is_unavailable(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            seed_signals(settings, [
                (101, "BTCUSDT", "2026-07-01T00:00:00+00:00", "structure", "STRUCTURE_ALERT"),
                (102, "BTCUSDT", "2026-07-01T01:00:00+00:00", "flow", "FLOW_ALERT"),
            ])
            lifecycle_id = seed_lifecycle(settings, latest_signal_id=102)
            first_outcome_id = insert_outcome(
                settings, signal_id=101, horizon="1h", status="unavailable", final_return=None,
            )
            event_outcome_id = insert_outcome(
                settings,
                signal_id=102,
                horizon="1h",
                status="success",
                signal_time="2026-07-01T01:00:00+00:00",
                module="flow",
            )
            link_lifecycle_outcomes(settings, now=datetime(2026, 7, 10, tzinfo=timezone.utc))
            detail = lifecycle_outcome_detail(settings, lifecycle_id=lifecycle_id)["data"]
            self.assertEqual(detail["coverage"]["primary_outcome_id"], event_outcome_id)
            self.assertEqual(detail["coverage"]["reasons"]["primary_outcome_signal_id"], 102)
            with OutcomeStore(settings.outcome_db_path).connect() as conn:
                conn.execute(
                    "UPDATE signal_outcomes SET data_status='success', final_return_pct=4 WHERE id=?",
                    (first_outcome_id,),
                )
            link_lifecycle_outcomes(settings, now=datetime(2026, 7, 10, tzinfo=timezone.utc))
            upgraded = lifecycle_outcome_detail(settings, lifecycle_id=lifecycle_id)["data"]

        self.assertEqual(upgraded["coverage"]["primary_outcome_id"], first_outcome_id)
        self.assertEqual(upgraded["coverage"]["reasons"]["primary_outcome_signal_id"], 101)

    def test_latest_signal_id_is_used_only_after_missing_first_and_events(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            seed_signals(settings, [
                (201, "BTCUSDT", "2026-07-01T03:00:00+00:00", "funding", "FUNDING_ALERT"),
            ])
            lifecycle_id = seed_lifecycle(
                settings,
                first_signal_id=None,
                latest_signal_id=201,
                with_events=False,
            )
            outcome_id = insert_outcome(
                settings,
                signal_id=201,
                horizon="1h",
                signal_time="2026-07-01T03:00:00+00:00",
                module="funding",
            )
            link_lifecycle_outcomes(settings, now=datetime(2026, 7, 10, tzinfo=timezone.utc))
            detail = lifecycle_outcome_detail(settings, lifecycle_id=lifecycle_id)["data"]

        self.assertEqual(detail["coverage"]["primary_outcome_id"], outcome_id)
        self.assertEqual(detail["links"][0]["link_method"], "latest_signal_id")

    def test_primary_is_deterministic_and_stale_links_are_pruned(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            seed_signals(settings, [
                (101, "BTCUSDT", "2026-07-01T00:00:00+00:00", "structure", "STRUCTURE_ALERT"),
            ])
            lifecycle_id = seed_lifecycle(settings, latest_signal_id=101, with_events=False)
            one_hour_id = insert_outcome(settings, signal_id=101, horizon="1h", status="pending")
            four_hour_id = insert_outcome(settings, signal_id=101, horizon="4h", status="pending")
            link_lifecycle_outcomes(settings, now=datetime(2026, 7, 10, tzinfo=timezone.utc))
            with closing(sqlite3.connect(settings.lifecycle_db_path)) as conn:
                conn.execute("UPDATE lifecycle_outcome_links SET is_primary=0 WHERE lifecycle_id=?", (lifecycle_id,))
                conn.execute(
                    "UPDATE lifecycle_outcome_links SET is_primary=1 WHERE lifecycle_id=? AND outcome_id=?",
                    (lifecycle_id, four_hour_id),
                )
                conn.commit()
            link_lifecycle_outcomes(settings, now=datetime(2026, 7, 10, tzinfo=timezone.utc))
            detail = lifecycle_outcome_detail(settings, lifecycle_id=lifecycle_id)["data"]
            self.assertEqual(detail["coverage"]["primary_outcome_id"], one_hour_id)

            with closing(sqlite3.connect(settings.outcome_db_path)) as conn:
                conn.execute("DELETE FROM signal_outcomes WHERE id=?", (one_hour_id,))
                conn.commit()
            link_lifecycle_outcomes(settings, now=datetime(2026, 7, 10, tzinfo=timezone.utc))
            with closing(sqlite3.connect(settings.lifecycle_db_path)) as conn:
                remaining = conn.execute(
                    "SELECT COUNT(*) FROM lifecycle_outcome_links WHERE lifecycle_id=?", (lifecycle_id,)
                ).fetchone()[0]
            checked = reconcile_lifecycle_outcomes(settings, dry_run=True)

        self.assertEqual(remaining, 1)
        self.assertEqual(checked["issues"]["orphan_links"], 0)

    def test_strict_symbol_time_module_fallback_and_ambiguous_rejection(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            seed_signals(settings, [
                (201, "BTCUSDT", "2026-07-01T00:00:30+00:00", "structure", "STRUCTURE_ALERT"),
            ])
            lifecycle_id = seed_lifecycle(settings, first_signal_id=None, latest_signal_id=None, with_events=False)
            insert_outcome(settings, signal_id=201, horizon="1h", signal_time="2026-07-01T00:00:30+00:00")
            linked = link_lifecycle_outcomes(settings, now=datetime(2026, 7, 10, tzinfo=timezone.utc))
            detail = lifecycle_outcome_detail(settings, lifecycle_id=lifecycle_id)["data"]
        self.assertEqual(linked["linked"], 1)
        self.assertEqual(detail["links"][0]["link_method"], "symbol_time_module")
        self.assertEqual(detail["links"][0]["link_confidence"], 0.8)

        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            seed_signals(settings, [
                (201, "BTCUSDT", "2026-07-01T00:00:30+00:00", "structure", "STRUCTURE_ALERT"),
                (202, "BTCUSDT", "2026-06-30T23:59:30+00:00", "structure", "STRUCTURE_ALERT"),
            ])
            lifecycle_id = seed_lifecycle(settings, first_signal_id=None, latest_signal_id=None, with_events=False)
            insert_outcome(settings, signal_id=201, horizon="1h", signal_time="2026-07-01T00:00:30+00:00")
            insert_outcome(settings, signal_id=202, horizon="1h", signal_time="2026-06-30T23:59:30+00:00")
            result = link_lifecycle_outcomes(settings, now=datetime(2026, 7, 10, tzinfo=timezone.utc))
            detail = lifecycle_outcome_detail(settings, lifecycle_id=lifecycle_id)["data"]
        self.assertEqual(result["linked"], 0)
        self.assertEqual(detail["coverage"]["unlinked_reason"], "ambiguous_match")

    def test_symbol_only_fallback_is_forbidden(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            seed_signals(settings, [(201, "BTCUSDT", "2026-07-01T00:00:30+00:00", "flow", "OTHER")])
            lifecycle_id = seed_lifecycle(
                settings, first_signal_id=None, latest_signal_id=None, with_events=False,
                module="structure", template="STRUCTURE_ALERT",
            )
            insert_outcome(settings, signal_id=201, horizon="1h", signal_time="2026-07-01T00:00:30+00:00", module="flow")
            result = link_lifecycle_outcomes(settings, now=datetime(2026, 7, 10, tzinfo=timezone.utc))
            detail = lifecycle_outcome_detail(settings, lifecycle_id=lifecycle_id)["data"]
        self.assertEqual(result["linked"], 0)
        self.assertEqual(detail["coverage"]["unlinked_reason"], "module_mismatch")

    def test_symbol_and_lifecycle_id_scope_is_an_intersection(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            lifecycle_id = seed_lifecycle(settings, latest_signal_id=101, with_events=False)
            OutcomeStore(settings.outcome_db_path).ensure_schema()
            result = link_lifecycle_outcomes(
                settings,
                symbol="ETHUSDT",
                lifecycle_id=lifecycle_id,
                now=datetime(2026, 7, 10, tzinfo=timezone.utc),
            )
            detail = lifecycle_outcome_detail(
                settings,
                symbol="ETHUSDT",
                lifecycle_id=lifecycle_id,
            )
        self.assertEqual(result["processed"], 0)
        self.assertFalse(detail["data"]["available"])

    def test_dry_run_does_not_add_extension_tables(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            seed_lifecycle(settings, with_events=False)
            result = link_lifecycle_outcomes(settings, dry_run=True)
            with closing(sqlite3.connect(settings.lifecycle_db_path)) as conn:
                table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='lifecycle_outcome_links'"
                ).fetchone()
        self.assertTrue(result["dry_run"])
        self.assertIsNone(table)

    def test_missing_outcome_store_fails_closed_without_deleting_links(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            seed_lifecycle(settings, latest_signal_id=101, with_events=False)
            store = IntelligenceStore(settings)
            store.ensure_schema()
            with store.transaction() as conn:
                conn.execute(
                    "INSERT INTO lifecycle_outcome_links ("
                    "lifecycle_id,symbol,signal_id,outcome_id,horizon,outcome_status,link_role,"
                    "link_method,link_confidence,is_primary,created_at,updated_at) "
                    "VALUES (1,'BTCUSDT',101,99,'1h','success','first_signal',"
                    "'first_signal_id',1,1,'x','x')"
                )
            linked = link_lifecycle_outcomes(settings)
            reconciled = reconcile_lifecycle_outcomes(settings, repair=True)
            with store.connect() as conn:
                remaining = conn.execute("SELECT COUNT(*) FROM lifecycle_outcome_links").fetchone()[0]
        self.assertFalse(linked["ok"])
        self.assertEqual(linked["error"], "outcome_store_unavailable")
        self.assertFalse(reconciled["ok"])
        self.assertEqual(reconciled["error"], "outcome_store_unavailable")
        self.assertEqual(remaining, 1)


class LifecycleOutcomeBackfillAndReconcileTests(unittest.TestCase):
    def test_backfill_keeps_detection_scan_and_relink_on_one_rotated_batch(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            rows = [
                (1, "AAAUSDT", "2026-07-01T01:00:00+00:00", "flow", "FLOW"),
                (2, "BBBUSDT", "2026-07-01T02:00:00+00:00", "flow", "FLOW"),
                (3, "CCCUSDT", "2026-07-01T03:00:00+00:00", "flow", "FLOW"),
            ]
            seed_signals(settings, rows)
            lifecycle_store = LifecycleStore(settings.lifecycle_db_path)
            for signal_id, symbol, when, module, _template in rows:
                lifecycle_store.create_lifecycle({
                    "symbol": symbol,
                    "first_signal_id": signal_id,
                    "first_signal_at": when,
                    "first_signal_module": module,
                    "first_signal_level": "15m",
                    "current_state": "warming",
                    "latest_signal_id": signal_id,
                    "latest_signal_at": when,
                })
            OutcomeStore(settings.outcome_db_path).ensure_schema()
            scanned: list[int] = []

            def scanner(source_rows, **_kwargs):
                scanned.extend(int(item["id"]) for item in source_rows)
                return {"ok": True, "counts": {"success": 0, "unavailable": 0, "error": 0}}

            with patch("paopao_radar.outcome_tracker.scan_signal_outcomes", side_effect=scanner), \
                 patch("paopao_radar.lifecycle_replay.rebuild_replays", return_value={"ok": True}), \
                 patch("paopao_radar.lifecycle_intelligence.generate_intelligence", return_value={"ok": True}), \
                 patch("paopao_radar.lifecycle_analytics.generate_lifecycle_analytics", return_value={"ok": True}):
                result = backfill_lifecycle_outcomes(
                    settings,
                    limit=2,
                    horizon="1h",
                    now=datetime(2026, 7, 10, tzinfo=timezone.utc),
                )

        expected_symbols = {rows[signal_id - 1][1] for signal_id in scanned}
        self.assertEqual(len(scanned), 2)
        self.assertEqual({item["symbol"] for item in result["items"]}, expected_symbols)

    def test_backfill_only_scans_missing_due_pair_and_is_idempotent(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            seed_signals(settings, [(101, "BTCUSDT", "2026-07-01T00:00:00+00:00", "structure", "STRUCTURE_ALERT")])
            seed_lifecycle(settings, latest_signal_id=101, with_events=False)
            calls: list[tuple[list[int], str]] = []

            def scanner(rows, **kwargs):
                calls.append(([int(item["id"]) for item in rows], kwargs["horizon"]))
                for row in rows:
                    insert_outcome(
                        settings,
                        signal_id=int(row["id"]),
                        horizon=kwargs["horizon"],
                        signal_time=str(row["time"]),
                    )
                return {"ok": True, "counts": {"success": len(rows), "unavailable": 0, "error": 0}}

            with patch("paopao_radar.outcome_tracker.scan_signal_outcomes", side_effect=scanner, create=True), \
                 patch("paopao_radar.lifecycle_replay.rebuild_replays", return_value={"ok": True}), \
                 patch("paopao_radar.lifecycle_intelligence.generate_intelligence", return_value={"ok": True}), \
                 patch("paopao_radar.lifecycle_analytics.generate_lifecycle_analytics", return_value={"ok": True}):
                first = backfill_lifecycle_outcomes(
                    settings, horizon="1h", now=datetime(2026, 7, 10, tzinfo=timezone.utc)
                )
                second = backfill_lifecycle_outcomes(
                    settings, horizon="1h", now=datetime(2026, 7, 10, tzinfo=timezone.utc)
                )
        self.assertTrue(first["ok"])
        self.assertEqual(first["backfilled"], 1)
        self.assertEqual(second["planned"], 0)
        self.assertEqual(calls, [([101], "1h")])

    def test_reconcile_finds_orphan_and_repair_rebuilds_coverage(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            seed_signals(settings, [(101, "BTCUSDT", "2026-07-01T00:00:00+00:00", "structure", "STRUCTURE_ALERT")])
            lifecycle_id = seed_lifecycle(settings, latest_signal_id=101, with_events=False)
            OutcomeStore(settings.outcome_db_path).ensure_schema()
            store = IntelligenceStore(settings)
            store.ensure_schema()
            with store.transaction() as conn:
                conn.execute(
                    """
                    INSERT INTO lifecycle_outcome_links (
                        lifecycle_id,symbol,signal_id,outcome_id,horizon,outcome_status,link_role,
                        link_method,link_confidence,is_primary,created_at,updated_at
                    ) VALUES (?, 'BTCUSDT', 101, 9999, '1h', 'success', 'first_signal',
                              'first_signal_id', 1, 1, 'x', 'x')
                    """,
                    (lifecycle_id,),
                )
            check = reconcile_lifecycle_outcomes(settings, dry_run=True)
            repaired = reconcile_lifecycle_outcomes(settings, repair=True)
            with store.connect() as conn:
                link_count = conn.execute("SELECT COUNT(*) FROM lifecycle_outcome_links").fetchone()[0]
                coverage_count = conn.execute("SELECT COUNT(*) FROM lifecycle_outcome_coverage").fetchone()[0]
        self.assertEqual(check["issues"]["orphan_links"], 1)
        self.assertGreaterEqual(check["issues"]["coverage_mismatch"], 1)
        self.assertTrue(repaired["ok"])
        self.assertEqual(link_count, 0)
        self.assertEqual(coverage_count, 1)

    def test_reconcile_detects_wrong_primary_and_semantic_coverage_then_repairs(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            seed_signals(settings, [
                (101, "BTCUSDT", "2026-07-01T00:00:00+00:00", "structure", "STRUCTURE_ALERT"),
            ])
            lifecycle_id = seed_lifecycle(settings, latest_signal_id=101, with_events=False)
            one_hour_id = insert_outcome(settings, signal_id=101, horizon="1h")
            four_hour_id = insert_outcome(settings, signal_id=101, horizon="4h")
            link_lifecycle_outcomes(settings, now=datetime(2026, 7, 10, tzinfo=timezone.utc))
            with closing(sqlite3.connect(settings.lifecycle_db_path)) as conn:
                conn.execute("UPDATE lifecycle_outcome_links SET is_primary=0 WHERE lifecycle_id=?", (lifecycle_id,))
                conn.execute(
                    "UPDATE lifecycle_outcome_links SET is_primary=1 WHERE lifecycle_id=? AND outcome_id=?",
                    (lifecycle_id, four_hour_id),
                )
                conn.execute(
                    "UPDATE lifecycle_outcome_coverage SET primary_outcome_id=?, "
                    "horizon_1h_status='error', link_coverage_ratio=0 WHERE lifecycle_id=?",
                    (four_hour_id, lifecycle_id),
                )
                conn.commit()
            check = reconcile_lifecycle_outcomes(settings, dry_run=True)
            repaired = reconcile_lifecycle_outcomes(settings, repair=True)
            detail = lifecycle_outcome_detail(settings, lifecycle_id=lifecycle_id)["data"]
            with closing(sqlite3.connect(settings.lifecycle_db_path)) as conn:
                conn.execute(
                    "UPDATE lifecycle_outcome_coverage SET reasons_json='{\"reason_counts\":{\"fake\":3}}' "
                    "WHERE lifecycle_id=?",
                    (lifecycle_id,),
                )
                conn.commit()
            reason_check = reconcile_lifecycle_outcomes(settings, dry_run=True)
            reason_repaired = reconcile_lifecycle_outcomes(settings, repair=True)

        self.assertEqual(check["issues"]["primary_mismatch"], 1)
        self.assertEqual(check["issues"]["coverage_mismatch"], 1)
        self.assertTrue(repaired["ok"])
        self.assertFalse(any(repaired["issues"].values()))
        self.assertGreaterEqual(repaired["detected_issues"]["coverage_mismatch"], 1)
        self.assertEqual(detail["coverage"]["primary_outcome_id"], one_hour_id)
        self.assertEqual(detail["coverage"]["horizon_1h_status"], "success")
        self.assertEqual(reason_check["issues"]["coverage_mismatch"], 1)
        self.assertTrue(reason_repaired["ok"])

    def test_backfill_retries_real_error_without_rebuilding_success_rows(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            seed_signals(settings, [
                (101, "BTCUSDT", "2026-07-01T00:00:00+00:00", "structure", "STRUCTURE_ALERT"),
            ])
            seed_lifecycle(settings, latest_signal_id=101, with_events=False)
            outcome_id = insert_outcome(settings, signal_id=101, horizon="1h", status="error", final_return=None)
            observed: list[bool] = []

            def scanner(_rows, **kwargs):
                observed.append(bool(kwargs.get("force_rebuild")))
                with OutcomeStore(settings.outcome_db_path).connect() as conn:
                    conn.execute(
                        "UPDATE signal_outcomes SET data_status='success', final_return_pct=2.5 WHERE id=?",
                        (outcome_id,),
                    )
                return {"ok": True, "counts": {"success": 1, "unavailable": 0, "error": 0}}

            with patch("paopao_radar.outcome_tracker.scan_signal_outcomes", side_effect=scanner), \
                 patch("paopao_radar.lifecycle_replay.rebuild_replays", return_value={"ok": True}), \
                 patch("paopao_radar.lifecycle_intelligence.generate_intelligence", return_value={"ok": True}), \
                 patch("paopao_radar.lifecycle_analytics.generate_lifecycle_analytics", return_value={"ok": True}):
                result = backfill_lifecycle_outcomes(
                    settings, horizon="1h", now=datetime(2026, 7, 10, tzinfo=timezone.utc)
                )

        self.assertEqual(observed, [True])
        self.assertEqual(result["backfilled"], 1)


if __name__ == "__main__":
    unittest.main()
