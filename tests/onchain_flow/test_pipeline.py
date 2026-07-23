from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.onchain_flow.aggregator import build_windows
from paopao_radar.onchain_flow.collectors.base import (
    BlockRange,
    EvmLogBackfillCollector,
)
from paopao_radar.onchain_flow.db import OnchainStore
from paopao_radar.onchain_flow.runtime import replay_fixture

from .support import FIXTURE_PATH, make_settings


class FakeBackfillCollector(EvmLogBackfillCollector):
    def fetch_logs(self, block_range: BlockRange):
        return [{"start": block_range.start_block, "end": block_range.end_block}]


class OnchainPipelineTests(unittest.TestCase):
    def test_backfill_interface_splits_bounded_ranges_without_network(self) -> None:
        collector = FakeBackfillCollector()
        self.assertEqual(
            list(collector.block_ranges(10, 25, batch_size=7)),
            [
                BlockRange(10, 16),
                BlockRange(17, 23),
                BlockRange(24, 25),
            ],
        )

    def test_migrations_create_isolated_wal_database(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            store = OnchainStore(settings)
            store.migrate()
            with closing(store._connect()) as conn:
                tables = {
                    str(row[0])
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                journal_mode = str(
                    conn.execute("PRAGMA journal_mode").fetchone()[0]
                ).lower()
                foreign_keys = int(
                    conn.execute("PRAGMA foreign_keys").fetchone()[0]
                )
            self.assertTrue(
                {
                    "schema_migrations",
                    "chain_cursors",
                    "address_labels",
                    "token_metadata",
                    "transfer_events",
                    "flow_events",
                    "flow_windows",
                    "alerts",
                    "alert_deliveries",
                }.issubset(tables)
            )
            self.assertEqual(journal_mode, "wal")
            self.assertEqual(foreign_keys, 1)
            self.assertEqual(store.integrity_check(), "ok")

    def test_replay_is_idempotent_and_detects_all_required_rules(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            first = replay_fixture(settings, FIXTURE_PATH, notify=False)
            store = OnchainStore(settings)
            counts_first = store.table_counts()
            alerts_first = store.active_alerts()
            with closing(sqlite3.connect(settings.db_path)) as conn:
                dump_first = "\n".join(conn.iterdump())

            second = replay_fixture(settings, FIXTURE_PATH, notify=False)
            counts_second = store.table_counts()
            alerts_second = store.active_alerts()
            with closing(sqlite3.connect(settings.db_path)) as conn:
                dump_second = "\n".join(conn.iterdump())

            self.assertEqual(first.as_dict(), second.as_dict())
            self.assertEqual(counts_first, counts_second)
            self.assertEqual(alerts_first, alerts_second)
            self.assertEqual(dump_first, dump_second)
            self.assertEqual(first.transfers_seen, 24)
            self.assertEqual(first.unique_transfers, 22)
            self.assertEqual(first.duplicate_deliveries, 2)
            self.assertEqual(first.orphaned_transfers, 1)
            detection_types = {
                kind for alert in alerts_first for kind in alert.detection_types
            }
            self.assertEqual(
                detection_types,
                {
                    "single_large",
                    "batch_flow",
                    "continuous_flow",
                    "multi_exchange",
                },
            )

    def test_orphan_is_excluded_from_rebuilt_aggregates(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            replay_fixture(settings, FIXTURE_PATH, notify=False)
            store = OnchainStore(settings)
            flows = store.finalized_flows()
            self.assertNotIn(
                "8453:0x0000000000000000000000000000000000000000000000000000000000000007:0",
                {flow.event_id for flow in flows},
            )
            windows = build_windows(
                flows,
                min_label_confidence=settings.min_label_confidence,
            )
            batch = next(
                window
                for window in windows
                if window.window_start == 1700000100
                and window.duration_sec == 900
                and window.direction == "inflow"
            )
            self.assertEqual(str(batch.total_usd), "2500000")
            self.assertEqual(batch.tx_count, 5)

    def test_unpriced_and_low_confidence_flows_never_generate_alerts(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            replay_fixture(settings, FIXTURE_PATH, notify=False)
            store = OnchainStore(settings)
            flows = store.finalized_flows()
            unpriced = [
                flow
                for flow in flows
                if flow.token_address
                == "0x8888888888888888888888888888888888888888"
            ]
            low_confidence = [
                flow for flow in flows if flow.label_confidence == 0.50
            ]
            self.assertEqual(len(unpriced), 1)
            self.assertEqual(unpriced[0].price_status, "missing")
            self.assertEqual(len(low_confidence), 1)
            self.assertTrue(
                all(
                    alert.token_address
                    != "0x8888888888888888888888888888888888888888"
                    for alert in store.active_alerts()
                )
            )
            self.assertTrue(
                all(alert.label_confidence >= 0.80 for alert in store.active_alerts())
            )

    def test_database_enforces_evm_idempotency_tuple(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            replay_fixture(settings, FIXTURE_PATH, notify=False)
            with closing(sqlite3.connect(settings.db_path)) as conn:
                duplicate_rows = conn.execute(
                    """
                    SELECT chain_id, tx_hash, log_index, COUNT(*)
                    FROM transfer_events
                    GROUP BY chain_id, tx_hash, log_index
                    HAVING COUNT(*) > 1
                    """
                ).fetchall()
            self.assertEqual(duplicate_rows, [])


if __name__ == "__main__":
    unittest.main()
