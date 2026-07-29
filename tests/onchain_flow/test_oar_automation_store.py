from __future__ import annotations

import sqlite3
import tempfile
import unittest
from contextlib import closing
import json
from pathlib import Path
from unittest.mock import patch

from paopao_radar.onchain_flow.automation_store import (
    AutomationStore,
    AutomationStoreError,
    canonical_token_key,
)
from paopao_radar.onchain_flow.registry import RegistryService

from tests.onchain_flow.support import make_settings


CONTRACT_A = "0x1111111111111111111111111111111111111111"
CONTRACT_B = "0x2222222222222222222222222222222222222222"
FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "onchain"
    / "oar_p4_automation.json"
)


class AutomationStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = make_settings(self.root)
        self.store = AutomationStore.from_settings(self.settings)
        self.key_a = canonical_token_key(8453, CONTRACT_A)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def add(
        self,
        contract: str = CONTRACT_A,
        symbol: str = "AAAUSDT",
    ) -> str:
        item = self.store.add_registry(
            market_symbol=symbol,
            contract=contract,
            source="manual",
            now=1000,
        )
        return str(item["token_key"])

    def verify(
        self,
        key: str | None = None,
        *,
        primary: bool = True,
        symbol: str = "AAA",
    ) -> dict[str, object]:
        return self.store.verify_registry(
            key or self.key_a,
            token_symbol=symbol,
            token_name=symbol,
            decimals=18,
            metadata_hash="a" * 64,
            verification_method="fixture",
            set_primary=primary,
            now=1001,
        )

    def test_registry_is_versioned_and_token_key_is_canonical(self) -> None:
        item = self.store.add_registry(
            market_symbol="aaausdt",
            contract=CONTRACT_A.upper().replace("0X", "0x"),
            source="manual",
            now=1000,
        )
        self.assertEqual(item["token_key"], self.key_a)
        self.assertEqual(item["market_symbol"], "AAAUSDT")
        with closing(sqlite3.connect(self.store.path)) as conn:
            version = conn.execute(
                "SELECT value FROM automation_meta WHERE key='schema_version'"
            ).fetchone()
        self.assertEqual(version[0], "1")

    def test_same_chain_contract_is_idempotent(self) -> None:
        self.add()
        self.add()
        self.assertEqual(len(self.store.list_registry() or []), 1)

    def test_contract_cannot_be_reassigned_by_another_market_symbol(self) -> None:
        self.add(CONTRACT_A, "AAAUSDT")
        with self.assertRaisesRegex(
            AutomationStoreError, "another market symbol"
        ):
            self.add(CONTRACT_A, "BBBUSDT")
        self.assertEqual(
            (self.store.get_registry(self.key_a) or {})["market_symbol"],
            "AAAUSDT",
        )

    def test_pending_token_cannot_enter_watchlist(self) -> None:
        self.add()
        with self.assertRaisesRegex(
            AutomationStoreError, "only verified"
        ):
            self.store.add_manual_watch(
                self.key_a,
                ttl_sec=3600,
                priority=100,
                query_window="4h",
                scan_interval_sec=900,
                now=1002,
            )

    def test_verified_token_can_enter_watchlist_without_duplicates(self) -> None:
        self.add()
        self.verify()
        first = self.store.add_manual_watch(
            self.key_a,
            ttl_sec=3600,
            priority=90,
            query_window="4h",
            scan_interval_sec=900,
            now=1002,
        )
        second = self.store.add_manual_watch(
            self.key_a,
            ttl_sec=7200,
            priority=100,
            query_window="4h",
            scan_interval_sec=900,
            now=1003,
        )
        self.assertEqual(first["token_key"], second["token_key"])
        self.assertEqual(second["priority"], 100)
        self.assertEqual(len(self.store.list_watch_items() or []), 1)

    def test_manual_watch_respects_active_token_capacity(self) -> None:
        first_key = self.add(CONTRACT_A, "AAAUSDT")
        self.verify(first_key, symbol="AAA")
        second_key = self.add(CONTRACT_B, "BBBUSDT")
        self.verify(second_key, symbol="BBB")
        self.store.add_manual_watch(
            first_key,
            ttl_sec=3600,
            priority=100,
            query_window="4h",
            scan_interval_sec=900,
            max_active_tokens=1,
            now=1002,
        )
        with self.assertRaisesRegex(
            AutomationStoreError, "capacity"
        ):
            self.store.add_manual_watch(
                second_key,
                ttl_sec=3600,
                priority=100,
                query_window="4h",
                scan_interval_sec=900,
                max_active_tokens=1,
                now=1002,
            )

    def test_same_symbol_does_not_merge_contracts(self) -> None:
        self.add(CONTRACT_A, "AAAUSDT")
        self.add(CONTRACT_B, "AAAUSDT")
        self.assertEqual(len(self.store.list_registry() or []), 2)

    def test_only_unique_verified_primary_resolves(self) -> None:
        key_a = self.add(CONTRACT_A, "AAAUSDT")
        key_b = self.add(CONTRACT_B, "AAAUSDT")
        self.verify(key_a, primary=False)
        self.verify(key_b, primary=False)
        self.assertEqual(
            self.store.resolve_registry("AAAUSDT")["status"],
            "ambiguous_contract",
        )
        self.store.verify_registry(
            key_b,
            token_symbol="AAA",
            token_name="AAA",
            decimals=18,
            metadata_hash="b" * 64,
            verification_method="fixture",
            set_primary=True,
            now=1002,
        )
        resolved = self.store.resolve_registry("AAAUSDT")
        self.assertEqual(resolved["status"], "resolved")
        self.assertEqual(resolved["token"]["token_key"], key_b)

    def test_doctor_detects_ambiguous_verified_registry(self) -> None:
        key_a = self.add(CONTRACT_A, "AAAUSDT")
        key_b = self.add(CONTRACT_B, "AAAUSDT")
        self.verify(key_a, primary=False)
        self.verify(key_b, primary=False)
        result = self.store.doctor(now=2000)
        self.assertEqual(result["status"], "failed")
        self.assertIn("ambiguous_primary", result["issues"])

    def test_disabled_token_no_longer_resolves_and_audit_remains(self) -> None:
        self.add()
        self.verify()
        self.store.add_manual_watch(
            self.key_a,
            ttl_sec=3600,
            priority=100,
            query_window="4h",
            scan_interval_sec=900,
            now=1002,
        )
        item = self.store.disable_registry(self.key_a, now=1003)
        self.assertEqual(item["status"], "disabled")
        self.assertEqual(
            self.store.resolve_registry("AAAUSDT")["status"],
            "registry_not_verified",
        )
        self.assertEqual(self.store.get_watch(self.key_a)["status"], "paused")

    def test_registry_list_does_not_create_missing_database(self) -> None:
        self.assertIsNone(self.store.list_registry())
        self.assertFalse(self.store.path.exists())

    def test_manual_remove_only_expires_when_no_source_remains(self) -> None:
        self.add()
        self.verify()
        self.store.add_manual_watch(
            self.key_a,
            ttl_sec=3600,
            priority=100,
            query_window="4h",
            scan_interval_sec=900,
            now=1002,
        )
        removed = self.store.remove_manual_watch(self.key_a, now=1003)
        self.assertEqual(removed["manual_watch"], 0)
        self.assertEqual(removed["status"], "expired")

    def test_multiple_sources_merge_priority_and_expiry(self) -> None:
        self.add()
        token = self.verify()
        for public_ref, priority, ttl in (
            ("funding:1", 70, 600),
            ("launch:2", 90, 1200),
        ):
            self.store.process_bridge_signal(
                {
                    "id": priority,
                    "public_ref": public_ref,
                    "ts": 1000,
                    "module": public_ref.split(":", 1)[0],
                    "symbol": "AAAUSDT",
                    "score": priority,
                    "stage": "",
                    "severity": "",
                    "excerpt": "",
                    "payload_hash": public_ref,
                },
                resolution={"status": "resolved", "token": token},
                source_ttl_sec=ttl,
                source_priority=priority,
                query_window="4h",
                scan_interval_sec=900,
                max_active_tokens=50,
                now=1001,
            )
        watch = self.store.get_watch(self.key_a) or {}
        self.assertEqual(watch["priority"], 90)
        self.assertEqual(watch["expires_at"], 2200)
        self.store.expire_and_recompute(manual_priority=100, now=2201)
        self.assertEqual(
            (self.store.get_watch(self.key_a) or {})["status"], "expired"
        )

    def test_manual_watch_survives_source_expiry(self) -> None:
        self.add()
        token = self.verify()
        self.store.add_manual_watch(
            self.key_a,
            ttl_sec=5000,
            priority=100,
            query_window="4h",
            scan_interval_sec=900,
            now=1000,
        )
        self.store.process_bridge_signal(
            {
                "id": 1,
                "public_ref": "funding:1",
                "ts": 1000,
                "module": "funding",
                "symbol": "AAAUSDT",
                "score": 70,
                "stage": "",
                "severity": "",
                "excerpt": "",
                "payload_hash": "x",
            },
            resolution={"status": "resolved", "token": token},
            source_ttl_sec=60,
            source_priority=70,
            query_window="4h",
            scan_interval_sec=900,
            max_active_tokens=50,
            now=1001,
        )
        self.store.expire_and_recompute(manual_priority=100, now=1100)
        watch = self.store.get_watch(self.key_a) or {}
        self.assertEqual(watch["status"], "active")
        self.assertEqual(watch["priority"], 100)
        self.assertEqual(watch["expires_at"], 6000)

    def test_claim_lease_prevents_duplicate_and_recovers(self) -> None:
        self.add()
        self.verify()
        self.store.add_manual_watch(
            self.key_a,
            ttl_sec=3600,
            priority=100,
            query_window="4h",
            scan_interval_sec=900,
            now=1002,
        )
        first = self.store.claim_due(
            owner="one", limit=1, lease_sec=60, now=1002
        )
        second = self.store.claim_due(
            owner="two", limit=1, lease_sec=60, now=1003
        )
        recovered = self.store.claim_due(
            owner="two", limit=1, lease_sec=60, now=1063
        )
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(len(recovered), 1)

    def test_failure_backoff_and_pause_are_bounded(self) -> None:
        self.add()
        self.verify()
        self.store.add_manual_watch(
            self.key_a,
            ttl_sec=10000,
            priority=100,
            query_window="4h",
            scan_interval_sec=900,
            now=1002,
        )
        self.store.record_scan(
            self.key_a,
            started_at=1010,
            status="failed",
            activity_complete=None,
            analysis_complete=None,
            error_code="rpc_failed",
            source_refs=[],
            scan_interval_sec=900,
            max_consecutive_failures=2,
            now=1011,
        )
        first = self.store.get_watch(self.key_a)
        self.assertEqual(first["next_scan_at"], 1311)
        self.assertEqual(first["status"], "active")
        self.store.record_scan(
            self.key_a,
            started_at=1311,
            status="failed",
            activity_complete=None,
            analysis_complete=None,
            error_code="rpc_failed",
            source_refs=[],
            scan_interval_sec=900,
            max_consecutive_failures=2,
            now=1312,
        )
        self.assertEqual(self.store.get_watch(self.key_a)["status"], "paused")
        self.store.expire_and_recompute(manual_priority=100, now=1313)
        self.assertEqual(self.store.get_watch(self.key_a)["status"], "paused")

    def test_partial_scan_uses_failure_backoff(self) -> None:
        self.add()
        self.verify()
        self.store.add_manual_watch(
            self.key_a,
            ttl_sec=10000,
            priority=100,
            query_window="4h",
            scan_interval_sec=900,
            now=1002,
        )
        self.store.record_scan(
            self.key_a,
            started_at=1010,
            status="partial",
            activity_complete=False,
            analysis_complete=False,
            source_refs=[],
            scan_interval_sec=900,
            max_consecutive_failures=10,
            now=1011,
        )
        watch = self.store.get_watch(self.key_a) or {}
        self.assertEqual(watch["next_scan_at"], 1311)
        self.assertEqual(watch["consecutive_failures"], 1)

    def test_schema_migration_rolls_back_as_one_transaction(self) -> None:
        def fail_mid_migration(conn: sqlite3.Connection) -> None:
            conn.execute("CREATE TABLE partial_schema(id INTEGER)")
            raise RuntimeError("fixture")

        with patch.object(
            AutomationStore, "_create_schema", side_effect=fail_mid_migration
        ):
            with self.assertRaises(RuntimeError):
                self.store.migrate()
        with closing(sqlite3.connect(self.store.path)) as conn:
            names = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertNotIn("partial_schema", names)
        self.assertNotIn("automation_meta", names)

    def test_scan_audit_is_bounded_per_token(self) -> None:
        self.add()
        self.verify()
        self.store.add_manual_watch(
            self.key_a,
            ttl_sec=100000,
            priority=100,
            query_window="4h",
            scan_interval_sec=60,
            now=1002,
        )
        with self.store.connect() as conn:
            conn.executemany(
                """
                INSERT INTO watch_scan_runs(
                    scan_id, token_key, started_at, completed_at, status,
                    activity_complete, analysis_complete
                ) VALUES(?, ?, ?, ?, 'ok', 1, 1)
                """,
                [
                    (
                        f"fixture-{index:03d}",
                        self.key_a,
                        1100 + index,
                        1100 + index,
                    )
                    for index in range(104)
                ],
            )
            conn.commit()
        self.store.record_scan(
            self.key_a,
            started_at=1204,
            status="ok",
            activity_complete=True,
            analysis_complete=True,
            source_refs=[],
            scan_interval_sec=60,
            max_consecutive_failures=10,
            now=1204,
        )
        with closing(sqlite3.connect(self.store.path)) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM watch_scan_runs"
            ).fetchone()[0]
        self.assertEqual(count, 100)

    def test_registry_verification_requires_symbol_confirmation(self) -> None:
        self.add()

        class Rpc:
            @staticmethod
            def chain_id() -> int:
                return 8453

            @staticmethod
            def get_code(address: str) -> str:
                del address
                return "0x6000"

            @staticmethod
            def eth_call(address: str, selector: str) -> str:
                del address
                if selector in {"0x313ce567", "0x18160ddd"}:
                    value = 18 if selector == "0x313ce567" else 1000
                    return f"0x{value:064x}"
                text = b"BBB" if selector == "0x95d89b41" else b"Token"
                return "0x" + text.ljust(32, b"\x00").hex()

        service = RegistryService(self.settings, self.store, rpc=Rpc())
        with self.assertRaisesRegex(
            AutomationStoreError, "explicit confirmation"
        ):
            service.verify(
                self.key_a,
                allow_network=True,
                set_primary=True,
                accept_symbol_mismatch=False,
            )
        verified = service.verify(
            self.key_a,
            allow_network=True,
            set_primary=True,
            accept_symbol_mismatch=True,
        )
        self.assertEqual(verified["status"], "verified")
        self.assertEqual(verified["token_symbol"], "BBB")

    def test_registry_verify_without_network_does_not_touch_rpc(self) -> None:
        self.add()
        service = RegistryService(
            self.settings,
            self.store,
            rpc=type(
                "NeverRpc",
                (),
                {
                    "chain_id": lambda self: (_ for _ in ()).throw(
                        AssertionError("RPC called")
                    )
                },
            )(),
        )
        with self.assertRaisesRegex(
            AutomationStoreError, "allow-network"
        ):
            service.verify(
                self.key_a,
                allow_network=False,
                set_primary=False,
                accept_symbol_mismatch=False,
            )

    def test_offline_fixture_is_synthetic_and_versioned(self) -> None:
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], 1)
        self.assertGreaterEqual(len(payload["registry"]), 4)
        serialized = json.dumps(payload, sort_keys=True)
        self.assertNotIn("https://", serialized)
        self.assertNotIn("api_key", serialized.lower())


if __name__ == "__main__":
    unittest.main()
