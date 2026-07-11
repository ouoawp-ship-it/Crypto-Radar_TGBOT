from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.config import Settings
from paopao_radar.lifecycle_intelligence_store import IntelligenceStore
from paopao_radar.lifecycle_replay import (
    REPLAY_MODEL_VERSION,
    associate_outcomes,
    build_replay,
    get_replay_payload,
    lifecycle_result_label,
    rebuild_replays,
)
from paopao_radar.lifecycle_store import LifecycleStore
from paopao_radar.outcome_tracker import OutcomeStore


def make_settings(tmp: str) -> Settings:
    base = Path(tmp)
    return Settings(
        data_dir=base,
        signal_events_path=base / "signals.json",
        signal_events_db_path=base / "signals.db",
        lifecycle_db_path=base / "lifecycle.db",
        outcome_db_path=base / "outcomes.db",
    )


def lifecycle() -> dict:
    return {
        "id": 7,
        "symbol": "BTCUSDT",
        "first_signal_id": 101,
        "latest_signal_id": 104,
        "first_signal_at": "2026-07-09T07:00:00+00:00",
        "latest_signal_at": "2026-07-09T13:00:00+00:00",
        "first_signal_level": "15m",
        "highest_level": "24h",
        "first_price": 100.0,
        "latest_price": 108.0,
        "lifecycle_score": 82.0,
        "risk_score": 30.0,
        "intelligence_score": 84.0,
        "current_state": "trend_confirmed",
        "is_active": 1,
        "created_at": "2026-07-09T07:00:00+00:00",
        "updated_at": "2026-07-09T13:00:00+00:00",
    }


def events() -> list[dict]:
    return [
        {
            "id": 4,
            "lifecycle_id": 7,
            "symbol": "BTCUSDT",
            "event_time": "2026-07-09T13:00:00+00:00",
            "event_type": "timeframe_upgrade_24h",
            "event_level": "24h",
            "signal_id": 104,
            "previous_state": "upgraded_4h",
            "new_state": "trend_confirmed",
            "price": 108,
            "event_score": 82,
            "risk_score": 30,
        },
        {
            "id": 1,
            "lifecycle_id": 7,
            "symbol": "BTCUSDT",
            "event_time": "2026-07-09T07:00:00+00:00",
            "event_type": "first_signal",
            "event_level": "15m",
            "signal_id": 101,
            "previous_state": "",
            "new_state": "warming",
            "price": 100,
            "event_score": 30,
            "risk_score": 0,
        },
        {
            "id": 3,
            "lifecycle_id": 7,
            "symbol": "BTCUSDT",
            "event_time": "2026-07-09T10:00:00+00:00",
            "event_type": "timeframe_upgrade_4h",
            "event_level": "4h",
            "signal_id": 103,
            "previous_state": "upgraded_1h",
            "new_state": "upgraded_4h",
            "price": 106,
            "event_score": 70,
            "risk_score": 20,
        },
        {
            "id": 2,
            "lifecycle_id": 7,
            "symbol": "BTCUSDT",
            "event_time": "2026-07-09T08:00:00+00:00",
            "event_type": "timeframe_upgrade_1h",
            "event_level": "1h",
            "signal_id": 102,
            "previous_state": "warming",
            "new_state": "upgraded_1h",
            "price": 103,
            "oi_change_pct": 8.5,
            "spot_cvd_delta": 1200,
            "futures_cvd_delta": 1800,
            "event_score": 55,
            "risk_score": 10,
        },
    ]


def outcomes() -> list[dict]:
    return [
        {
            "id": 2,
            "signal_id": 104,
            "symbol": "BTCUSDT",
            "signal_time": "2026-07-09T13:00:00+00:00",
            "horizon": "24h",
            "horizon_sec": 86400,
            "data_status": "success",
            "final_return_pct": -20,
            "max_gain_pct": 1,
            "max_drawdown_pct": -22,
        },
        {
            "id": 1,
            "signal_id": 101,
            "symbol": "BTCUSDT",
            "signal_time": "2026-07-09T07:00:00+00:00",
            "horizon": "24h",
            "horizon_sec": 86400,
            "data_status": "success",
            "final_return_pct": 8,
            "max_gain_pct": 12,
            "max_drawdown_pct": -2,
        },
    ]


class LifecycleIntelligenceStoreTests(unittest.TestCase):
    def test_extension_schema_is_idempotent_and_indexed(self) -> None:
        with TemporaryDirectory() as tmp:
            store = IntelligenceStore(Path(tmp) / "lifecycle.db")
            store.ensure_schema()
            store.ensure_schema()
            with closing(sqlite3.connect(store.db_path)) as conn:
                names = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
                    ).fetchall()
                }
                version = int(conn.execute("PRAGMA user_version").fetchone()[0])
                intelligence_columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(lifecycle_intelligence)")
                }

        self.assertTrue(
            {
                "lifecycle_intelligence",
                "lifecycle_replays",
                "lifecycle_replay_frames",
                "lifecycle_analytics_cache",
                "idx_lifecycle_intelligence_score",
                "idx_lifecycle_replay_frames_time",
            }.issubset(names)
        )
        self.assertGreaterEqual(version, 1780)
        self.assertIn("stage", intelligence_columns)

    def test_replay_upsert_replaces_frames_and_public_projection_omits_metrics(self) -> None:
        with TemporaryDirectory() as tmp:
            store = IntelligenceStore(Path(tmp) / "lifecycle.db")
            replay = build_replay(lifecycle(), events(), [], outcomes())
            record = {**replay["summary"], "frame_count": len(replay["frames"]), "summary": replay["summary"]}
            store.upsert_replay(record, replay["frames"])
            stored = store.get_replay(symbol="BTCUSDT") or {}
            frames = store.list_replay_frames(stored["lifecycle_id"], limit=100)

        self.assertEqual(stored["replay_version"], REPLAY_MODEL_VERSION)
        self.assertEqual(len(frames), 4)
        self.assertNotIn("metrics_json", frames[0])
        self.assertNotIn("metrics", frames[0])

    def test_analytics_cache_expires_and_hits(self) -> None:
        with TemporaryDirectory() as tmp:
            store = IntelligenceStore(Path(tmp) / "lifecycle.db")
            store.put_analytics_cache("lifecycle:first-level", {"count": 3}, ttl_sec=60)
            first = store.get_analytics_cache("lifecycle:first-level")
            missing = store.get_analytics_cache("missing")

        self.assertEqual(first, {"count": 3})
        self.assertIsNone(missing)


class LifecycleReplayTests(unittest.TestCase):
    def test_frames_sort_by_time_are_contiguous_and_upgrade_times_are_correct(self) -> None:
        replay = build_replay(lifecycle(), events(), [], outcomes())

        self.assertEqual([item["frame_index"] for item in replay["frames"]], [1, 2, 3, 4])
        self.assertEqual([item["event_id"] for item in replay["frames"]], [1, 2, 3, 4])
        self.assertEqual(replay["upgrade_path"], "15m → 1h → 4h → 24h")
        self.assertEqual(replay["time_to_1h_sec"], 3600)
        self.assertEqual(replay["time_to_4h_sec"], 10800)
        self.assertEqual(replay["time_to_24h_sec"], 21600)

    def test_outcome_link_prefers_first_signal_id(self) -> None:
        link = associate_outcomes(lifecycle(), events(), outcomes())
        replay = build_replay(lifecycle(), events(), [], outcomes())

        self.assertEqual(link["method"], "first_signal_id")
        self.assertEqual([item["signal_id"] for item in link["items"]], [101])
        self.assertEqual(replay["final_return_pct"], 8.0)
        self.assertEqual(replay["result_label"], "strong_success")

    def test_symbol_fallback_requires_strict_time_window(self) -> None:
        value = {**lifecycle(), "first_signal_id": None, "latest_signal_id": None}
        event_rows = [{**item, "signal_id": None} for item in events()]
        candidates = [
            {
                "id": 1,
                "signal_id": 999,
                "symbol": "BTCUSDT",
                "signal_time": "2026-07-08T23:00:00+00:00",
            },
            {
                "id": 2,
                "signal_id": 998,
                "symbol": "BTCUSDT",
                "signal_time": "2026-07-09T09:00:00+00:00",
            },
        ]

        link = associate_outcomes(value, event_rows, candidates)

        self.assertEqual(link["method"], "symbol_time_window")
        self.assertEqual([item["id"] for item in link["items"]], [2])

    def test_result_label_covers_failure_and_risk_avoided(self) -> None:
        failed = lifecycle_result_label(
            final_return_pct=None,
            max_price_gain_pct=None,
            max_drawdown_pct=None,
            highest_level="15m",
            final_state="failed",
            risk_event_count=0,
            has_outcome=False,
        )
        avoided = lifecycle_result_label(
            final_return_pct=-4,
            max_price_gain_pct=1,
            max_drawdown_pct=-6,
            highest_level="1h",
            final_state="risk_warning",
            risk_event_count=1,
            has_outcome=True,
        )

        self.assertEqual(failed, "failed")
        self.assertEqual(avoided, "risk_avoided")

    def test_dry_run_does_not_create_extension_tables(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            self._seed_lifecycle(settings)
            result = rebuild_replays(settings, dry_run=True)
            with closing(sqlite3.connect(settings.lifecycle_db_path)) as conn:
                table = conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='lifecycle_replays'"
                ).fetchone()

        self.assertEqual(result["processed"], 1)
        self.assertIsNone(table)

    def test_replay_backfill_is_idempotent_and_payload_is_precomputed(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            self._seed_lifecycle(settings)
            first = rebuild_replays(settings)
            second = rebuild_replays(settings)
            payload = get_replay_payload(settings, symbol="BTCUSDT", frame_limit=2)

        self.assertEqual(first["processed"], 1)
        self.assertEqual(second["processed"], 0)
        self.assertEqual(second["skipped"], 1)
        self.assertTrue(payload["data"]["available"])
        self.assertEqual(len(payload["data"]["frames"]), 2)
        self.assertNotIn("source_signature", payload["data"]["replay"])

    @staticmethod
    def _seed_lifecycle(settings: Settings) -> None:
        lifecycle_store = LifecycleStore(settings.lifecycle_db_path)
        lifecycle_store.ensure_schema()
        with lifecycle_store.transaction() as conn:
            stored, _ = lifecycle_store.create_lifecycle(
                {
                    **lifecycle(),
                    "first_signal_excerpt": "BTC 15m lifecycle",
                    "first_signal_module": "launch",
                    "first_signal_template": "TG_LAUNCH_ALERT",
                    "first_signal_type": "launch",
                },
                conn=conn,
            )
            lifecycle_id = int(stored["id"])
            for item in events():
                row = {**item, "lifecycle_id": lifecycle_id, "dedup_key": f"event-{item['id']}"}
                lifecycle_store.insert_event(row, conn=conn)
            lifecycle_store.insert_snapshot(
                {
                    "symbol": "BTCUSDT",
                    "timeframe": "1h",
                    "snapshot_time": "2026-07-09T13:00:00+00:00",
                    "price": 108,
                },
                conn=conn,
            )

        outcome_store = OutcomeStore(settings.outcome_db_path)
        outcome_store.ensure_schema()
        with outcome_store.connect() as conn:
            conn.execute(
                """
                INSERT INTO signal_outcomes (
                    signal_id, symbol, coin, signal_time, horizon, horizon_sec, due_time,
                    direction, final_return_pct, max_gain_pct, max_drawdown_pct, data_status,
                    created_at, updated_at
                ) VALUES (101, 'BTCUSDT', 'BTC', '2026-07-09T07:00:00+00:00', '24h', 86400,
                    '2026-07-10T07:00:00+00:00', 'long', 8, 12, -2, 'success',
                    '2026-07-10T07:00:00+00:00', '2026-07-10T07:00:00+00:00')
                """
            )


if __name__ == "__main__":
    unittest.main()
