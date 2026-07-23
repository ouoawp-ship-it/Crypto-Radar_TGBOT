from __future__ import annotations

import json
import subprocess
import unittest
from contextlib import closing, redirect_stdout
from dataclasses import replace
from decimal import Decimal
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.onchain_flow.cli import main
from paopao_radar.onchain_flow.config import (
    OnchainSettings,
    SettingsValidationError,
    UnsafeOnchainPath,
)
from paopao_radar.onchain_flow.db import OnchainStore
from paopao_radar.onchain_flow.labels import (
    LabelValidationError,
    load_labels_csv,
    validate_live_labels,
)
from paopao_radar.onchain_flow.models import (
    OnchainAlert,
    ProcessedBlock,
    RollingFlowSnapshot,
)

from .support import REPO_ROOT, make_settings


class P31SafetyTests(unittest.TestCase):
    def test_endpoint_diagnostics_redact_credentials_and_paths(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = OnchainSettings.load(
                base_dir=Path(tmp),
                environ={
                    "ONCHAIN_BASE_HTTP_RPC_URL": (
                        "https://user:secret@example.invalid/private-key"
                    ),
                    "ONCHAIN_BASE_WSS_RPC_URL": (
                        "wss://token@example-ws.invalid/secret"
                    ),
                    "ONCHAIN_COINGECKO_API_KEY": "coingecko-secret",
                },
            )
            diagnostic = settings.diagnostic()
        rendered = json.dumps(diagnostic)
        self.assertIn("example.invalid", rendered)
        self.assertIn("example-ws.invalid", rendered)
        self.assertNotIn("user", rendered)
        self.assertNotIn("secret", rendered)
        self.assertNotIn("private-key", rendered)

    def test_live_mode_rejects_synthetic_labels_before_database_creation(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = replace(
                make_settings(Path(tmp)),
                enable=True,
                base_enable=True,
                base_http_rpc_url="https://example.invalid",
            )
            output = StringIO()
            with redirect_stdout(output):
                code = main(["labels-check"], settings=settings)
            payload = json.loads(output.getvalue())
            self.assertNotEqual(code, 0)
            self.assertEqual(payload["error"], "LabelValidationError")
            self.assertFalse(settings.db_path.exists())

    def test_live_label_requires_real_active_high_confidence_base_cex(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "labels.csv"
            path.write_text(
                "chain_id,address,entity_name,entity_type,address_type,source,"
                "confidence,valid_from,valid_to\n"
                "8453,0x1111111111111111111111111111111111111111,"
                "Binance,cex,hot,manual_review,0.95,,\n",
                encoding="utf-8",
            )
            labels = load_labels_csv(path)
            active = validate_live_labels(
                labels, min_confidence=0.8, timestamp=1000
            )
            self.assertEqual(len(active), 1)
            with self.assertRaises(LabelValidationError):
                validate_live_labels(
                    [replace(labels[0], confidence=0.5)],
                    min_confidence=0.8,
                    timestamp=1000,
                )

    def test_invalid_p31_ranges_and_dominance_fail_closed(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            invalid = (
                replace(
                    settings,
                    rpc_min_block_range=10,
                    rpc_max_block_range=5,
                ),
                replace(settings, net_dominance_min=Decimal("1.01")),
                replace(settings, base_chain_id=1),
                replace(settings, rpc_timeout_sec=Decimal("NaN")),
            )
            for item in invalid:
                with self.subTest(item=item):
                    with self.assertRaises(SettingsValidationError):
                        item.validate()

    def test_production_topic_route_path_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = replace(
                make_settings(root),
                data_dir=root,
                tg_topic_routes_path=root / "data" / "tg_topic_routes.json",
            )
            with self.assertRaises(UnsafeOnchainPath):
                settings.validate()

    def test_migration_two_adds_cursor_price_and_rolling_tables(self) -> None:
        with TemporaryDirectory() as tmp:
            store = OnchainStore(make_settings(Path(tmp)))
            store.migrate()
            with closing(store._connect()) as conn:
                versions = [
                    row[0]
                    for row in conn.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                ]
                tables = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
        self.assertEqual(versions, [1, 2])
        self.assertTrue(
            {
                "processed_blocks",
                "price_cache",
                "flow_window_snapshots",
                "orphaned_transfer_audit",
            }.issubset(tables)
        )

    def test_reorg_rollback_marks_windows_and_alerts_inactive(self) -> None:
        with TemporaryDirectory() as tmp:
            store = OnchainStore(make_settings(Path(tmp)))
            store.migrate()
            store.commit_finalized_range(
                blocks=[
                    ProcessedBlock(8453, 1, "0x01", 100, processed_at=100),
                    ProcessedBlock(8453, 2, "0x02", 200, processed_at=200),
                ],
                transfers=[],
                flows=[],
                last_seen_head=2,
                provider_status="ok",
                updated_at=200,
            )
            snapshot = RollingFlowSnapshot(
                snapshot_key="snapshot",
                chain_id=8453,
                token_address="0x9999999999999999999999999999999999999999",
                symbol="ABC",
                evaluation_time=200,
                duration_sec=900,
                gross_inflow_usd=Decimal("100"),
                gross_outflow_usd=Decimal("0"),
                net_flow_usd=Decimal("100"),
                inflow_tx_count=5,
                outflow_tx_count=0,
                distinct_inbound_counterparties=3,
                distinct_outbound_counterparties=0,
                exchanges=("Binance",),
                active_15m_buckets=1,
                min_label_confidence=0.95,
                price_source="test",
                price_observed_at=200,
                evaluation_block=2,
                algorithm_version="p3.1-test",
            )
            store.upsert_snapshot(snapshot)
            store.upsert_alert(
                OnchainAlert(
                    alert_key="alert",
                    chain_id=8453,
                    token_address=snapshot.token_address,
                    symbol="ABC",
                    direction="inflow",
                    score=-55,
                    horizon="1h",
                    confidence="medium",
                    reasons=("test",),
                    detection_types=("batch_flow",),
                    window_start=0,
                    window_end=200,
                    total_usd=Decimal("100"),
                    tx_count=5,
                    exchanges=("Binance",),
                    label_confidence=0.95,
                    price_status="available",
                    created_at=200,
                    severity_version="test",
                    evaluation_block=2,
                )
            )
            store.rollback_to_block(8453, 1, 300)
            with closing(store._connect()) as conn:
                snapshot_status = conn.execute(
                    "SELECT status FROM flow_window_snapshots"
                ).fetchone()[0]
                alert_status = conn.execute(
                    "SELECT status FROM alerts"
                ).fetchone()[0]
        self.assertEqual(snapshot_status, "orphaned")
        self.assertEqual(alert_status, "orphaned")


class P31DeploymentTests(unittest.TestCase):
    def test_local_label_pattern_is_ignored(self) -> None:
        result = subprocess.run(
            [
                "git",
                "check-ignore",
                "config/onchain/cex_addresses.local.csv",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0)

    def test_installer_defaults_to_disabled_and_has_all_enable_gates(self) -> None:
        script = (
            REPO_ROOT / "scripts" / "install_onchain_flow.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('enable_service=false', script)
        self.assertIn(
            'if [[ "${enable_service}" != "true" ]]; then', script
        )
        self.assertIn("ONCHAIN_ENABLE=true is required", script)
        self.assertIn("ONCHAIN_BASE_ENABLE=true is required", script)
        self.assertIn("provider-check --chain base", script)
        self.assertIn("MemoryMax=384M", script)
        self.assertNotIn("paopao-radar.service", script)
        self.assertNotIn("paopao-market-stream.service", script)

    def test_production_entrypoints_and_existing_installers_are_unchanged(self) -> None:
        result = subprocess.run(
            [
                "git",
                "diff",
                "--exit-code",
                "HEAD",
                "--",
                "main.py",
                "paopao_radar/cli.py",
                "scripts/install_server.sh",
                "scripts/update_server.sh",
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
