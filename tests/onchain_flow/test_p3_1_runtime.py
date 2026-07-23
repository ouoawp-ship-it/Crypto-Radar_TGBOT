from __future__ import annotations

import json
import sqlite3
import threading
import unittest
from contextlib import closing
from dataclasses import replace
from decimal import Decimal
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from paopao_radar.onchain_flow.cli import main
from paopao_radar.onchain_flow.collectors.evm_http import (
    AdaptiveRangeError,
    FinalizedRangeConsistencyError,
    pad_topic_address,
)
from paopao_radar.onchain_flow.collectors.evm_ws import WssError
from paopao_radar.onchain_flow.constants import TRANSFER_TOPIC
from paopao_radar.onchain_flow.db import OnchainStore
from paopao_radar.onchain_flow.live_runtime import (
    BaseOnchainRuntime,
    ReorgManualInterventionRequired,
)
from paopao_radar.onchain_flow.models import PriceQuote
from paopao_radar.onchain_flow.price_oracle import StaticPriceProvider
from paopao_radar.onchain_flow.token_metadata import (
    DECIMALS_SELECTOR,
    NAME_SELECTOR,
    SYMBOL_SELECTOR,
    TOTAL_SUPPLY_SELECTOR,
)

from .support import make_settings


CEX = "0x1111111111111111111111111111111111111111"
OUTSIDE = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TOKEN = "0x9999999999999999999999999999999999999999"


def uint256(value: int) -> str:
    return "0x" + f"{value:064x}"


def abi_string(value: str) -> str:
    raw = value.encode()
    padded = raw + (b"\x00" * ((32 - len(raw) % 32) % 32))
    return "0x" + (
        (32).to_bytes(32, "big")
        + len(raw).to_bytes(32, "big")
        + padded
    ).hex()


def block_hash(block_number: int, variant: int = 0) -> str:
    return "0x" + f"{block_number + (variant * 1000):064x}"


def transfer_log(block_number: int, tx_number: int):
    return {
        "address": TOKEN,
        "topics": [
            TRANSFER_TOPIC,
            pad_topic_address(OUTSIDE),
            pad_topic_address(CEX),
        ],
        "data": uint256(1_000_000),
        "blockNumber": hex(block_number),
        "blockHash": block_hash(block_number),
        "transactionHash": "0x" + f"{tx_number:064x}",
        "logIndex": "0x0",
        "removed": False,
    }


class FakeRpc:
    def __init__(self, head=2):
        self.head = head
        self.hash_variants = {}
        self.error_count = 0

    def chain_id(self):
        return 8453

    def block_number(self):
        return self.head

    def get_block(self, number):
        return {
            "number": hex(number),
            "hash": block_hash(number, self.hash_variants.get(number, 0)),
            "timestamp": hex(1_700_000_000 + number),
        }

    def get_code(self, _address):
        return "0x6000"

    def eth_call(self, _address, selector):
        return {
            DECIMALS_SELECTOR: uint256(6),
            TOTAL_SUPPLY_SELECTOR: uint256(1_000_000_000),
            SYMBOL_SELECTOR: abi_string("ABC"),
            NAME_SELECTOR: abi_string("ABC Token"),
        }[selector]


class FakeCollector:
    def __init__(self, logs=None):
        self.logs = list(logs or [])
        self.ranges = []
        self.fail = False

    def fetch_cex_logs(self, start, end, _addresses):
        self.ranges.append((start, end))
        if self.fail:
            raise AdaptiveRangeError("failed range")
        return [
            item
            for item in self.logs
            if start <= int(item["blockNumber"], 16) <= end
        ]


def live_settings(root: Path):
    labels = root / "config" / "onchain" / "cex_addresses.local.csv"
    labels.parent.mkdir(parents=True)
    labels.write_text(
        "chain_id,address,entity_name,entity_type,address_type,source,"
        "confidence,valid_from,valid_to\n"
        f"8453,{CEX},Binance,cex,hot,manual_review,0.99,,\n",
        encoding="utf-8",
    )
    return replace(
        make_settings(root),
        enable=True,
        base_enable=True,
        base_http_rpc_url="https://user:secret@example.invalid/rpc-key",
        base_confirmation_depth=0,
        base_bootstrap_lookback_blocks=1,
        base_reorg_lookback_blocks=4,
        labels_path=labels,
        price_enable=True,
        price_provider="static",
        single_large_floor_usd=Decimal("1000000"),
    )


def static_prices():
    return StaticPriceProvider(
        {
            (8453, TOKEN): PriceQuote(
                chain_id=8453,
                token_address=TOKEN,
                price_usd=Decimal("2"),
                volume_24h_usd=Decimal("1000000"),
                source="static",
                observed_at=1_700_000_002,
            )
        }
    )


class RuntimeTests(unittest.TestCase):
    def test_finalized_log_consistency_failure_keeps_cursor_unchanged(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = replace(
                live_settings(Path(tmp)),
                base_bootstrap_lookback_blocks=0,
            )
            rpc = FakeRpc(head=1)
            collector = FakeCollector()
            runtime = BaseOnchainRuntime(
                settings,
                rpc=rpc,
                http_collector=collector,
                price_provider=static_prices(),
                clock=lambda: 1_700_000_002,
            )
            runtime.process_once()
            self.assertEqual(
                OnchainStore(settings).cursor(8453).last_finalized_block,
                1,
            )
            rpc.head = 2
            old_fork = transfer_log(2, 1)
            old_fork["blockHash"] = block_hash(2, 1)
            collector.logs = [old_fork]
            with self.assertRaises(FinalizedRangeConsistencyError):
                runtime.process_once()
            store = OnchainStore(settings)
            self.assertEqual(store.cursor(8453).last_finalized_block, 1)
            self.assertEqual(store.table_counts()["transfer_events"], 0)

    def test_out_of_range_and_conflicting_event_contents_fail_complete_range(self) -> None:
        class RawCollector:
            def __init__(self, logs):
                self.logs = logs

            def fetch_cex_logs(self, *_args):
                return self.logs

        cases = []
        outside = transfer_log(3, 1)
        cases.append([outside])
        first = transfer_log(2, 1)
        conflict = dict(first)
        conflict["data"] = uint256(2_000_000)
        cases.append([first, conflict])
        for logs in cases:
            with self.subTest(logs=logs), TemporaryDirectory() as tmp:
                settings = replace(
                    live_settings(Path(tmp)),
                    base_bootstrap_lookback_blocks=0,
                )
                runtime = BaseOnchainRuntime(
                    settings,
                    rpc=FakeRpc(head=2),
                    http_collector=RawCollector(logs),
                    price_provider=static_prices(),
                    clock=lambda: 1_700_000_002,
                )
                with self.assertRaises(FinalizedRangeConsistencyError):
                    runtime.process_once()
                store = OnchainStore(settings)
                self.assertIsNone(store.cursor(8453))
                self.assertEqual(
                    store.table_counts()["transfer_events"], 0
                )

    def test_restart_recovers_committed_range_and_decides_single_event_once(self) -> None:
        class FailSecondRangeOnce(FakeCollector):
            def __init__(self, logs):
                super().__init__(logs)
                self.failed = False

            def fetch_cex_logs(self, start, end, addresses):
                if (start, end) == (2, 2) and not self.failed:
                    self.failed = True
                    raise AdaptiveRangeError("range B failed")
                return super().fetch_cex_logs(start, end, addresses)

        with TemporaryDirectory() as tmp:
            settings = replace(
                live_settings(Path(tmp)),
                rpc_max_block_range=1,
                single_large_floor_usd=Decimal("1"),
                single_volume_ratio=Decimal("0"),
            )
            rpc = FakeRpc(head=2)
            collector = FailSecondRangeOnce([transfer_log(1, 1)])
            runtime = BaseOnchainRuntime(
                settings,
                rpc=rpc,
                http_collector=collector,
                price_provider=static_prices(),
                clock=lambda: 1_700_000_002,
            )
            with self.assertRaises(AdaptiveRangeError):
                runtime.process_once()
            self.assertEqual(
                OnchainStore(settings).cursor(8453).last_finalized_block,
                1,
            )
            restarted = BaseOnchainRuntime(
                settings,
                rpc=rpc,
                http_collector=collector,
                price_provider=static_prices(),
                clock=lambda: 1_700_000_002,
            )
            restarted.process_once()
            restarted.process_once()
            with closing(sqlite3.connect(settings.db_path)) as conn:
                decisions = conn.execute(
                    "SELECT decision_status FROM single_event_decisions"
                ).fetchall()
                alerts = conn.execute(
                    "SELECT COUNT(*) FROM alerts"
                ).fetchone()[0]
        self.assertEqual(decisions, [("evaluated",)])
        self.assertEqual(alerts, 1)

    def test_restart_after_ingestion_before_evaluation_recovers(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = replace(
                live_settings(Path(tmp)),
                single_large_floor_usd=Decimal("1"),
                single_volume_ratio=Decimal("0"),
            )
            rpc = FakeRpc(head=2)
            collector = FakeCollector([transfer_log(2, 1)])
            interrupted = BaseOnchainRuntime(
                settings,
                rpc=rpc,
                http_collector=collector,
                price_provider=static_prices(),
                clock=lambda: 1_700_000_002,
            )
            with (
                patch.object(
                    interrupted,
                    "_evaluate_and_notify",
                    side_effect=SystemExit("crash point"),
                ),
                self.assertRaises(SystemExit),
            ):
                interrupted.process_once()
            with closing(sqlite3.connect(settings.db_path)) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM single_event_decisions"
                    ).fetchone()[0],
                    0,
                )
            BaseOnchainRuntime(
                settings,
                rpc=rpc,
                http_collector=collector,
                price_provider=static_prices(),
                clock=lambda: 1_700_000_002,
            ).process_once()
            with closing(sqlite3.connect(settings.db_path)) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT decision_status FROM single_event_decisions"
                    ).fetchone()[0],
                    "evaluated",
                )

    def test_alert_persists_before_failure_and_delivery_retries(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = replace(
                live_settings(Path(tmp)),
                single_large_floor_usd=Decimal("1"),
                single_volume_ratio=Decimal("0"),
            )
            rpc = FakeRpc(head=2)
            collector = FakeCollector([transfer_log(2, 1)])
            first = BaseOnchainRuntime(
                settings,
                rpc=rpc,
                http_collector=collector,
                price_provider=static_prices(),
                clock=lambda: 1_700_000_002,
            )
            with (
                patch(
                    "paopao_radar.onchain_flow.notifier.TelegramGateway.send",
                    side_effect=RuntimeError("notifier failed"),
                ),
                self.assertRaises(RuntimeError),
            ):
                first.process_once()
            with closing(sqlite3.connect(settings.db_path)) as conn:
                alert_count = conn.execute(
                    "SELECT COUNT(*) FROM alerts"
                ).fetchone()[0]
                decision = conn.execute(
                    "SELECT decision_status FROM single_event_decisions"
                ).fetchone()[0]
                failed = conn.execute(
                    "SELECT status FROM alert_deliveries"
                ).fetchone()[0]
            self.assertEqual(alert_count, 1)
            self.assertEqual(decision, "evaluated")
            self.assertEqual(failed, "failed")
            BaseOnchainRuntime(
                settings,
                rpc=rpc,
                http_collector=collector,
                price_provider=static_prices(),
                clock=lambda: 1_700_000_003,
            ).process_once()
            with closing(sqlite3.connect(settings.db_path)) as conn:
                delivery = conn.execute(
                    "SELECT status, attempt_count FROM alert_deliveries"
                ).fetchone()
        self.assertEqual(delivery, ("dry_run", 2))

    def test_old_catchup_event_is_stored_but_notification_is_suppressed(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = replace(
                live_settings(Path(tmp)),
                single_large_floor_usd=Decimal("1"),
                alert_max_event_age_sec=60,
            )
            BaseOnchainRuntime(
                settings,
                rpc=FakeRpc(head=2),
                http_collector=FakeCollector([transfer_log(2, 1)]),
                price_provider=static_prices(),
                clock=lambda: 1_700_001_000,
            ).process_once()
            with closing(sqlite3.connect(settings.db_path)) as conn:
                flow_count = conn.execute(
                    "SELECT COUNT(*) FROM flow_events"
                ).fetchone()[0]
                decision = conn.execute(
                    """
                    SELECT decision_status, catchup_suppression_reason
                    FROM single_event_decisions
                    """
                ).fetchone()
                alert_count = conn.execute(
                    "SELECT COUNT(*) FROM alerts"
                ).fetchone()[0]
        self.assertEqual(flow_count, 1)
        self.assertEqual(decision, ("suppressed", "event_too_old"))
        self.assertEqual(alert_count, 0)

    def test_startup_uses_bounded_lookback_and_advances_atomic_cursor(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = live_settings(Path(tmp))
            rpc = FakeRpc(head=2)
            collector = FakeCollector([transfer_log(2, 1)])
            runtime = BaseOnchainRuntime(
                settings,
                rpc=rpc,
                http_collector=collector,
                price_provider=static_prices(),
                clock=lambda: 1_700_000_002,
            )
            result = runtime.process_once()
            store = OnchainStore(settings)
            cursor = store.cursor(8453)
            counts = store.table_counts()
        self.assertEqual(collector.ranges, [(1, 2)])
        self.assertEqual(cursor.last_finalized_block, 2)
        self.assertEqual(counts["processed_blocks"], 2)
        self.assertEqual(counts["transfer_events"], 1)
        self.assertEqual(result["cursor_lag_blocks"], 0)

    def test_gap_fill_is_idempotent_and_range_failure_keeps_cursor(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = live_settings(Path(tmp))
            rpc = FakeRpc(head=2)
            collector = FakeCollector(
                [transfer_log(2, 1), transfer_log(3, 2)]
            )
            runtime = BaseOnchainRuntime(
                settings,
                rpc=rpc,
                http_collector=collector,
                price_provider=static_prices(),
                clock=lambda: 1_700_000_003,
            )
            runtime.process_once()
            rpc.head = 3
            runtime.process_once()
            store = OnchainStore(settings)
            self.assertEqual(store.cursor(8453).last_finalized_block, 3)
            self.assertEqual(store.table_counts()["transfer_events"], 2)
            collector.fail = True
            rpc.head = 4
            with self.assertRaises(AdaptiveRangeError):
                runtime.process_once()
            self.assertEqual(store.cursor(8453).last_finalized_block, 3)

    def test_removed_provider_log_is_audited_but_never_classified(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = live_settings(Path(tmp))
            removed_log = transfer_log(2, 1)
            removed_log["removed"] = True
            runtime = BaseOnchainRuntime(
                settings,
                rpc=FakeRpc(head=2),
                http_collector=FakeCollector([removed_log]),
                price_provider=static_prices(),
                clock=lambda: 1_700_000_003,
            )
            runtime.process_once()
            counts = OnchainStore(settings).table_counts()
        self.assertEqual(counts["transfer_events"], 1)
        self.assertEqual(counts["flow_events"], 0)
        self.assertEqual(counts["alerts"], 0)

    def test_reorg_finds_common_ancestor_and_orphans_affected_event(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = live_settings(Path(tmp))
            rpc = FakeRpc(head=2)
            collector = FakeCollector([transfer_log(2, 1)])
            runtime = BaseOnchainRuntime(
                settings,
                rpc=rpc,
                http_collector=collector,
                price_provider=static_prices(),
                clock=lambda: 1_700_000_004,
            )
            runtime.process_once()
            rpc.head = 3
            rpc.hash_variants[2] = 1
            collector.logs = []
            runtime.process_once()
            with closing(sqlite3.connect(settings.db_path)) as conn:
                removed, flow_status = conn.execute(
                    """
                    SELECT t.removed, f.status
                    FROM transfer_events t
                    JOIN flow_events f ON f.event_id=t.event_id
                    """
                ).fetchone()
                orphan_audit_count = conn.execute(
                    "SELECT COUNT(*) FROM orphaned_transfer_audit"
                ).fetchone()[0]
            cursor = OnchainStore(settings).cursor(8453)
        self.assertEqual(removed, 1)
        self.assertEqual(flow_status, "orphaned")
        self.assertEqual(orphan_audit_count, 1)
        self.assertEqual(cursor.last_finalized_block, 3)

    def test_reorg_without_common_ancestor_fails_closed(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = replace(
                live_settings(Path(tmp)),
                base_reorg_lookback_blocks=1,
            )
            rpc = FakeRpc(head=2)
            runtime = BaseOnchainRuntime(
                settings,
                rpc=rpc,
                http_collector=FakeCollector(),
                price_provider=static_prices(),
                clock=lambda: 1_700_000_004,
            )
            runtime.process_once()
            rpc.hash_variants[1] = 1
            rpc.hash_variants[2] = 1
            with self.assertRaises(ReorgManualInterventionRequired):
                runtime.process_once()
            self.assertEqual(
                OnchainStore(settings).cursor(8453).last_finalized_block,
                2,
            )

    def test_live_wss_disconnect_runs_http_reconciliation(self) -> None:
        class Trigger:
            connected = False

            def __init__(self):
                self.connects = 0

            def connect(self):
                self.connects += 1
                self.connected = True

            def receive_head(self):
                self.connected = False
                raise WssError("disconnect")

            def close(self):
                self.connected = False

        clock_values = iter([0, 0, 0, 61, 61, 61])
        with TemporaryDirectory() as tmp:
            settings = replace(
                live_settings(Path(tmp)),
                base_wss_rpc_url="wss://example.invalid/ws-key",
                wss_reconnect_sec=Decimal("0.01"),
            )
            trigger = Trigger()
            runtime = BaseOnchainRuntime(
                settings,
                wss_trigger=trigger,
                clock=lambda: next(clock_values),
                sleep=lambda _seconds: None,
            )
            with patch.object(
                runtime, "process_once", return_value={"status": "ok"}
            ) as process:
                runtime.run_live(duration_minutes=1)
        self.assertGreaterEqual(process.call_count, 2)
        self.assertGreaterEqual(runtime.metrics["reconnect_count"], 1)

    def test_disabled_mode_blocks_http_wss_threads_db_and_telegram(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp), enable=False)
            output = StringIO()
            with (
                patch("requests.sessions.Session.request") as request_mock,
                patch("websocket.create_connection") as wss_mock,
                patch.object(threading.Thread, "start") as thread_mock,
                patch("paopao_radar.telegram.requests.post") as tg_mock,
                patch("sys.stdout", output),
            ):
                code = main(["once"], settings=settings)
            payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertFalse(payload["network_activity"])
        self.assertFalse(payload["database_writes"])
        self.assertFalse(payload["telegram_calls"])
        request_mock.assert_not_called()
        wss_mock.assert_not_called()
        thread_mock.assert_not_called()
        tg_mock.assert_not_called()
        self.assertFalse(settings.data_dir.exists())


if __name__ == "__main__":
    unittest.main()
