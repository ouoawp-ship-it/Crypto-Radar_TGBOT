from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from paopao_radar.market_cockpit import MarketSnapshotStore
from paopao_radar.signal_effectiveness import (
    SignalOutcomeTracker,
    infer_signal_direction,
)
from paopao_radar.signal_store import SignalEventStore


class SignalEffectivenessTests(unittest.TestCase):
    def test_infers_only_explicit_directional_hypotheses(self) -> None:
        self.assertEqual(
            infer_signal_direction("flow", {"category": "真启动候选"}),
            "long",
        )
        self.assertEqual(
            infer_signal_direction("flow", {"category": "诱多/派发"}),
            "short",
        )
        self.assertEqual(
            infer_signal_direction("launch", {"stage": "breakout"}),
            "long",
        )
        self.assertEqual(
            infer_signal_direction("funding", {"primary_kind": "multi_negative"}),
            "long",
        )
        self.assertEqual(
            infer_signal_direction("funding", {"primary_kind": "multi_positive"}),
            "short",
        )
        self.assertEqual(
            infer_signal_direction("flow", {"category": "合约拉盘"}),
            "",
        )
        self.assertEqual(
            infer_signal_direction("summary", {"category": "真启动候选"}),
            "",
        )

    def test_refresh_matures_due_outcome_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            signal_db = Path(tmp) / "signals.db"
            market_db = Path(tmp) / "market.db"
            store = SignalEventStore(signal_db)
            store.append_from_push(
                template_id="TG_FLOW_RADAR",
                dedup_key="flow:btc",
                status="sent",
                sent=True,
                text="BTCUSDT 真启动候选",
                ts=1_000,
                structured_records=[{
                    "symbol": "BTCUSDT",
                    "category": "真启动候选",
                    "score": 82,
                    "price": 100,
                    "quality_gate": "allow",
                    "data_quality_score": 96,
                }],
            )
            MarketSnapshotStore(market_db).append_many([
                {
                    "symbol": "BTCUSDT",
                    "observed_at": 1_900,
                    "source": "binance_futures_batch",
                    "price": 110,
                },
            ])

            tracker = SignalOutcomeTracker(signal_db, market_db)
            first = tracker.refresh(now_ts=2_000)
            second = tracker.refresh(now_ts=2_000)

            self.assertEqual(first["signals_tracked"], 1)
            self.assertEqual(first["outcomes_created"], 4)
            self.assertEqual(first["outcomes_matured"], 1)
            self.assertEqual(first["summary"]["by_category"][0]["horizon"], "15m")
            self.assertEqual(first["summary"]["by_score_bucket"][0]["horizon"], "15m")
            self.assertEqual(first["summary"]["by_quality_gate"][0]["quality_gate"], "allow")
            self.assertEqual(first["summary"]["trusted_matured_signals"], 1)
            self.assertEqual(first["summary"]["trusted_by_horizon"][0]["samples"], 1)
            self.assertEqual(first["summary"]["review_ready_horizons"], [])
            self.assertEqual(second["outcomes_created"], 0)
            with closing(sqlite3.connect(signal_db)) as conn:
                rows = conn.execute(
                    """
                    SELECT horizon, status, direction, entry_price, exit_price,
                           raw_return_pct, directional_return_pct, is_hit
                    FROM signal_outcomes ORDER BY horizon_sec
                    """
                ).fetchall()
            self.assertEqual(len(rows), 4)
            self.assertEqual(rows[0][0:3], ("15m", "matured", "long"))
            self.assertEqual(rows[0][3], 100)
            self.assertEqual(rows[0][4], 110)
            self.assertAlmostEqual(rows[0][5], 10)
            self.assertAlmostEqual(rows[0][6], 10)
            self.assertEqual(rows[0][7], 1)
            self.assertTrue(all(row[1] == "pending" for row in rows[1:]))

    def test_short_direction_inverts_raw_return(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            signal_db = Path(tmp) / "signals.db"
            market_db = Path(tmp) / "market.db"
            SignalEventStore(signal_db).append_from_push(
                template_id="TG_FLOW_RADAR",
                dedup_key="flow:eth",
                status="sent",
                sent=True,
                text="ETHUSDT 诱多/派发",
                ts=2_000,
                structured_records=[{
                    "symbol": "ETHUSDT",
                    "category": "诱多/派发",
                    "score": 75,
                    "price": 100,
                    "quality_gate": "allow",
                }],
            )
            MarketSnapshotStore(market_db).append_many([
                {
                    "symbol": "ETHUSDT",
                    "observed_at": 2_900,
                    "source": "binance_futures_batch",
                    "price": 90,
                },
            ])

            SignalOutcomeTracker(signal_db, market_db).refresh(now_ts=3_000)

            with closing(sqlite3.connect(signal_db)) as conn:
                row = conn.execute(
                    """
                    SELECT raw_return_pct, directional_return_pct, is_hit
                    FROM signal_outcomes WHERE horizon = '15m'
                    """
                ).fetchone()
            self.assertAlmostEqual(row[0], -10)
            self.assertAlmostEqual(row[1], 10)
            self.assertEqual(row[2], 1)

    def test_blocked_and_non_sent_signals_are_not_tracked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            signal_db = Path(tmp) / "signals.db"
            market_db = Path(tmp) / "market.db"
            store = SignalEventStore(signal_db)
            for index, values in enumerate((
                {"status": "sent", "sent": True, "quality_gate": "block"},
                {"status": "dry_run", "sent": False, "quality_gate": "allow"},
            )):
                store.append_from_push(
                    template_id="TG_FLOW_RADAR",
                    dedup_key=f"flow:blocked:{index}",
                    status=values["status"],
                    sent=values["sent"],
                    text="BTCUSDT 真启动候选",
                    ts=1_000 + index,
                    structured_records=[{
                        "symbol": "BTCUSDT",
                        "category": "真启动候选",
                        "score": 90,
                        "price": 100,
                        "quality_gate": values["quality_gate"],
                    }],
                )

            result = SignalOutcomeTracker(signal_db, market_db).refresh(now_ts=5_000)

            self.assertEqual(result["signals_tracked"], 0)
            with closing(sqlite3.connect(signal_db)) as conn:
                count = conn.execute("SELECT COUNT(*) FROM signal_outcomes").fetchone()[0]
            self.assertEqual(count, 0)

    def test_missing_exit_becomes_unavailable_and_prune_cascades(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            signal_db = Path(tmp) / "signals.db"
            market_db = Path(tmp) / "market.db"
            store = SignalEventStore(signal_db)
            store.append_from_push(
                template_id="TG_LAUNCH_ALERT",
                dedup_key="launch:sol",
                status="sent",
                sent=True,
                text="SOLUSDT breakout",
                ts=1_000,
                structured_records=[{
                    "symbol": "SOLUSDT",
                    "stage": "breakout",
                    "score": 91,
                    "price": 50,
                    "quality_gate": "allow",
                }],
            )
            tracker = SignalOutcomeTracker(signal_db, market_db)

            result = tracker.refresh(now_ts=4_000)

            self.assertGreaterEqual(result["outcomes_unavailable"], 1)
            summary = tracker.summary()
            self.assertEqual(summary["status_counts"]["unavailable"], 1)
            self.assertEqual(summary["status_counts"]["pending"], 3)

            store.prune(before_ts=2_000, max_rows=100)
            with closing(sqlite3.connect(signal_db)) as conn:
                count = conn.execute("SELECT COUNT(*) FROM signal_outcomes").fetchone()[0]
            self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
