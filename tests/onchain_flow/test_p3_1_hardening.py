from __future__ import annotations

import hashlib
import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.onchain_flow.db import OnchainStore
from paopao_radar.onchain_flow.live_runtime import BaseOnchainRuntime
from paopao_radar.onchain_flow.migrations import apply_migrations
from paopao_radar.onchain_flow.models import (
    OnchainAlert,
    ProcessedBlock,
)
from paopao_radar.onchain_flow.runtime import (
    isolated_replay_settings,
    replay_fixture,
)

from .support import FIXTURE_PATH, make_settings


TOKEN = "0x9999999999999999999999999999999999999999"


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def alert(
    alert_key: str,
    notification_key: str,
    *,
    direction: str = "inflow",
    confidence: str = "medium",
    created_at: int = 1000,
) -> OnchainAlert:
    return OnchainAlert(
        alert_key=alert_key,
        chain_id=8453,
        token_address=TOKEN,
        symbol="ABC",
        direction=direction,
        score=-55 if direction == "inflow" else 55,
        horizon="1h",
        confidence=confidence,
        reasons=("test",),
        detection_types=("continuous_flow",),
        window_start=created_at - 3600,
        window_end=created_at,
        total_usd=Decimal("100"),
        tx_count=8,
        exchanges=("Binance",),
        label_confidence=0.95,
        price_status="available",
        created_at=created_at,
        severity_version="p3.1-test",
        notification_key=notification_key,
    )


class ReplayIsolationTests(unittest.TestCase):
    def test_replay_leaves_live_database_and_state_byte_for_byte_unchanged(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            live_labels = root / "private-live-labels.csv"
            live_labels.write_text(
                "chain_id,address,entity_name,entity_type,address_type,"
                "source,confidence,valid_from,valid_to\n"
                "8453,0x1111111111111111111111111111111111111111,"
                "Private,cex,hot,manual_review,0.99,,\n",
                encoding="utf-8",
            )
            settings = replace(settings, labels_path=live_labels)
            store = OnchainStore(settings)
            store.migrate()
            store.commit_finalized_range(
                blocks=[
                    ProcessedBlock(
                        8453,
                        123,
                        "0x" + ("ab" * 32),
                        1000,
                        processed_at=1000,
                    )
                ],
                transfers=[],
                flows=[],
                last_seen_head=123,
                provider_status="seeded",
                updated_at=1000,
            )
            settings.runtime_status_path.write_text(
                '{"status":"live-seed"}', encoding="utf-8"
            )
            settings.tg_push_history_path.write_text(
                '[{"status":"live-seed"}]', encoding="utf-8"
            )
            settings.tg_outbox_path.write_text(
                '{"live":"outbox"}', encoding="utf-8"
            )
            settings.tg_topic_routes_path.write_text(
                '{"live":"routes"}', encoding="utf-8"
            )
            live_files = (
                settings.db_path,
                settings.runtime_status_path,
                settings.tg_push_history_path,
                settings.tg_outbox_path,
                settings.tg_topic_routes_path,
            )
            before_hashes = {
                path: file_hash(path) for path in live_files
            }
            before_counts = store.table_counts()

            first = replay_fixture(
                settings, FIXTURE_PATH, notify=True
            )
            replay_settings = isolated_replay_settings(
                settings, FIXTURE_PATH
            )
            with closing(
                sqlite3.connect(replay_settings.db_path)
            ) as conn:
                first_dump = "\n".join(conn.iterdump())
            second = replay_fixture(
                settings, FIXTURE_PATH, notify=True
            )
            with closing(
                sqlite3.connect(replay_settings.db_path)
            ) as conn:
                second_dump = "\n".join(conn.iterdump())

            self.assertEqual(first.as_dict(), second.as_dict())
            self.assertEqual(first_dump, second_dump)
            self.assertEqual(store.table_counts(), before_counts)
            self.assertEqual(
                {path: file_hash(path) for path in live_files},
                before_hashes,
            )
            self.assertEqual(
                Path(first.replay_directory),
                replay_settings.data_dir,
            )
            self.assertNotEqual(
                replay_settings.db_path.resolve(),
                settings.db_path.resolve(),
            )


class NotificationLifecycleTests(unittest.TestCase):
    def test_cooldown_uses_stable_notification_key_but_allows_escalation_and_reversal(self) -> None:
        with TemporaryDirectory() as tmp:
            now = [1000]
            settings = make_settings(Path(tmp))
            store = OnchainStore(settings)
            store.migrate()
            runtime = BaseOnchainRuntime(
                settings, clock=lambda: now[0]
            )
            stable = (
                f"8453:{TOKEN}:inflow:3600:continuous_flow:medium"
            )
            store.persist_alert_for_delivery(
                alert("fact-1", stable), created_at=1000
            )
            runtime._deliver_pending(
                store, send=False, confirm_real_send=False
            )
            now[0] = 1300
            store.persist_alert_for_delivery(
                alert("fact-2", stable, created_at=1300),
                created_at=1300,
            )
            runtime._deliver_pending(
                store, send=False, confirm_real_send=False
            )
            escalated_key = (
                f"8453:{TOKEN}:inflow:3600:continuous_flow:high"
            )
            store.persist_alert_for_delivery(
                alert(
                    "fact-3",
                    escalated_key,
                    confidence="high",
                    created_at=1300,
                ),
                created_at=1300,
            )
            reversed_key = (
                f"8453:{TOKEN}:outflow:3600:continuous_flow:medium"
            )
            store.persist_alert_for_delivery(
                alert(
                    "fact-4",
                    reversed_key,
                    direction="outflow",
                    created_at=1300,
                ),
                created_at=1300,
            )
            runtime._deliver_pending(
                store, send=False, confirm_real_send=False
            )
            with closing(sqlite3.connect(settings.db_path)) as conn:
                statuses = dict(
                    conn.execute(
                        "SELECT alert_key, status FROM alert_deliveries"
                    ).fetchall()
                )
                facts = conn.execute(
                    "SELECT COUNT(*) FROM alerts"
                ).fetchone()[0]
        self.assertEqual(facts, 4)
        self.assertEqual(statuses["fact-1"], "dry_run")
        self.assertEqual(statuses["fact-2"], "cooldown_suppressed")
        self.assertEqual(statuses["fact-3"], "dry_run")
        self.assertEqual(statuses["fact-4"], "dry_run")


class MigrationRecoveryTests(unittest.TestCase):
    def test_interrupted_migration_reruns_without_database_deletion(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "migration.db"
            with closing(sqlite3.connect(path)) as conn:
                def fail_during_migration_three(
                    version: int, statement: int
                ) -> None:
                    if version == 3 and statement == 2:
                        raise RuntimeError("injected migration interruption")

                with self.assertRaises(RuntimeError):
                    apply_migrations(
                        conn,
                        after_statement=fail_during_migration_three,
                    )
                versions = [
                    row[0]
                    for row in conn.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                ]
                self.assertEqual(versions, [1, 2])
                apply_migrations(conn)
                versions = [
                    row[0]
                    for row in conn.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                ]
                alert_columns = {
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(alerts)"
                    )
                }
                decision_table = conn.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type='table' AND name='single_event_decisions'
                    """
                ).fetchone()
            self.assertTrue(path.exists())
        self.assertEqual(versions, [1, 2, 3])
        self.assertIn("notification_key", alert_columns)
        self.assertIsNotNone(decision_table)


if __name__ == "__main__":
    unittest.main()
