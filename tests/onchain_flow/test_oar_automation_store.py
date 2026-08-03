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

    @staticmethod
    def rolling_observation(
        start: int,
        end: int,
        events: list[tuple[int, int, str, str]] | None = None,
    ) -> dict[str, object]:
        normalized = []
        for identity, event_time, amount, flow_type in events or []:
            normalized.append(
                {
                    "event_hash": f"{identity:064x}",
                    "event_time": event_time,
                    "amount": amount,
                    "from_hash": f"{identity + 1000:064x}",
                    "to_hash": f"{identity + 2000:064x}",
                    "flow_type": flow_type,
                }
            )
        return {
            "schema_version": 1,
            "from_time": start,
            "to_time": end,
            "event_count": len(normalized),
            "events": normalized,
        }

    def persist_rolling_scan(
        self,
        token_key: str,
        observation: dict[str, object],
        *,
        owner: str,
    ) -> str:
        to_time = int(observation["to_time"])
        claimed = self.store.claim_due(
            owner=owner,
            limit=1,
            lease_sec=600,
            now=to_time,
        )
        self.assertEqual([item["token_key"] for item in claimed], [token_key])
        return self.store.record_scan(
            token_key,
            lease_owner=owner,
            started_at=to_time,
            status="ok",
            activity_complete=True,
            analysis_complete=True,
            query_window="15m",
            historical_baseline={"status": "cold_start"},
            rolling_observation=observation,
            source_refs=[],
            scan_interval_sec=60,
            max_consecutive_failures=10,
            now=to_time,
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
        self.assertEqual(version[0], "6")

    def test_same_chain_contract_is_idempotent(self) -> None:
        self.add()
        self.add()
        self.assertEqual(len(self.store.list_registry() or []), 1)

    def test_registry_keys_and_primary_resolution_are_chain_scoped(self) -> None:
        base = self.store.add_registry(
            market_symbol="AAAUSDT",
            contract=CONTRACT_A,
            chain="base",
            chain_id=8453,
            source="manual",
            now=1000,
        )
        ethereum = self.store.add_registry(
            market_symbol="AAAUSDT",
            contract=CONTRACT_A,
            chain="ethereum",
            chain_id=1,
            source="manual",
            now=1000,
        )
        self.assertEqual(base["token_key"], f"8453:{CONTRACT_A}")
        self.assertEqual(ethereum["token_key"], f"1:{CONTRACT_A}")
        for item in (base, ethereum):
            self.store.verify_registry(
                str(item["token_key"]),
                token_symbol="AAA",
                token_name="AAA",
                decimals=18,
                metadata_hash="a" * 64,
                verification_method="fixture",
                set_primary=True,
                now=1001,
            )
        self.assertEqual(
            self.store.resolve_registry("AAAUSDT")["status"],
            "ambiguous_contract",
        )

    def test_invalid_chain_identity_is_rejected_before_registry_write(self) -> None:
        with self.assertRaisesRegex(AutomationStoreError, "positive integer"):
            canonical_token_key("invalid", CONTRACT_A)  # type: ignore[arg-type]
        with self.assertRaisesRegex(AutomationStoreError, "canonical"):
            self.store.add_registry(
                market_symbol="AAAUSDT",
                contract=CONTRACT_A,
                chain="Ethereum Mainnet",
                chain_id=1,
                source="manual",
                now=1000,
            )
        self.assertFalse(self.store.path.exists())

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

    def test_reverify_preserves_primary_and_secondary_roles(self) -> None:
        key_a = self.add(CONTRACT_A, "AAAUSDT")
        key_b = self.add(CONTRACT_B, "AAAUSDT")
        self.verify(key_a, primary=True)
        self.verify(key_b, primary=False)
        primary = self.verify(key_a, primary=False)
        secondary = self.verify(key_b, primary=False)
        self.assertEqual(primary["is_primary"], 1)
        self.assertEqual(secondary["is_primary"], 0)
        self.assertFalse(primary["verification"]["primary_changed"])
        self.assertFalse(secondary["verification"]["primary_changed"])

    def test_explicit_primary_switch_is_atomic_and_idempotent(self) -> None:
        key_a = self.add(CONTRACT_A, "AAAUSDT")
        key_b = self.add(CONTRACT_B, "AAAUSDT")
        self.verify(key_a, primary=True)
        switched = self.verify(key_b, primary=True)
        repeated = self.verify(key_b, primary=False)
        self.assertEqual((self.store.get_registry(key_a) or {})["is_primary"], 0)
        self.assertEqual(switched["is_primary"], 1)
        self.assertTrue(switched["verification"]["primary_changed"])
        self.assertEqual(repeated["is_primary"], 1)
        self.assertFalse(repeated["verification"]["primary_changed"])

    def test_primary_switch_failure_rolls_back_previous_primary(self) -> None:
        key_a = self.add(CONTRACT_A, "AAAUSDT")
        key_b = self.add(CONTRACT_B, "AAAUSDT")
        self.verify(key_a, primary=True)
        self.verify(key_b, primary=False)
        with self.store.connect() as conn:
            conn.execute(
                f"""
                CREATE TRIGGER fail_primary_switch
                BEFORE UPDATE ON token_registry
                WHEN NEW.token_key='{key_b}' AND NEW.is_primary=1
                BEGIN
                    SELECT RAISE(ABORT, 'fixture');
                END
                """
            )
            conn.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.verify(key_b, primary=True)
        self.assertEqual((self.store.get_registry(key_a) or {})["is_primary"], 1)
        self.assertEqual((self.store.get_registry(key_b) or {})["is_primary"], 0)

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
        self.assertEqual(recovered[0]["lease_owner"], "two")
        self.assertEqual(recovered[0]["lease_until"], 1123)

    def test_lease_owner_fences_stale_completion_and_failure(self) -> None:
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
        self.store.claim_due(owner="worker-a", limit=1, lease_sec=60, now=1002)
        claimed_b = self.store.claim_due(
            owner="worker-b", limit=1, lease_sec=60, now=1063
        )
        self.assertEqual(len(claimed_b), 1)
        before = self.store.get_watch(self.key_a) or {}
        result = self.store.record_scan(
            self.key_a,
            lease_owner="worker-a",
            started_at=1002,
            status="failed",
            activity_complete=None,
            analysis_complete=None,
            error_code="stale_failure",
            source_refs=[],
            scan_interval_sec=900,
            max_consecutive_failures=2,
            now=1064,
        )
        after = self.store.get_watch(self.key_a) or {}
        self.assertEqual(result, "lease_lost")
        self.assertEqual(after["lease_owner"], "worker-b")
        self.assertEqual(after["lease_until"], before["lease_until"])
        self.assertEqual(after["next_scan_at"], before["next_scan_at"])
        self.assertEqual(after["consecutive_failures"], 0)
        self.assertNotEqual(
            self.store.record_scan(
                self.key_a,
                lease_owner="worker-b",
                started_at=1063,
                status="ok",
                activity_complete=True,
                analysis_complete=True,
                source_refs=[],
                scan_interval_sec=900,
                max_consecutive_failures=2,
                now=1065,
            ),
            "lease_lost",
        )
        self.assertEqual((self.store.get_watch(self.key_a) or {})["lease_owner"], "")

    def test_lease_renew_and_deferred_release_require_owner(self) -> None:
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
        self.store.claim_due(owner="owner", limit=1, lease_sec=60, now=1002)
        self.assertFalse(
            self.store.renew_lease(
                self.key_a,
                lease_owner="wrong",
                lease_sec=60,
                now=1010,
            )
        )
        self.assertTrue(
            self.store.renew_lease(
                self.key_a,
                lease_owner="owner",
                lease_sec=60,
                now=1010,
            )
        )
        self.assertFalse(
            self.store.release_claim_without_failure(
                self.key_a, lease_owner="wrong", now=1011
            )
        )
        self.assertTrue(
            self.store.release_claim_without_failure(
                self.key_a, lease_owner="owner", now=1011
            )
        )
        watch = self.store.get_watch(self.key_a) or {}
        self.assertEqual(watch["lease_owner"], "")
        self.assertEqual(watch["consecutive_failures"], 0)
        self.assertEqual(watch["next_scan_at"], 1002)

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
        self.assertEqual(
            len(
                self.store.claim_due(
                    owner="failure-one", limit=1, lease_sec=60, now=1010
                )
            ),
            1,
        )
        self.store.record_scan(
            self.key_a,
            lease_owner="failure-one",
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
        self.assertEqual(
            len(
                self.store.claim_due(
                    owner="failure-two", limit=1, lease_sec=60, now=1311
                )
            ),
            1,
        )
        self.store.record_scan(
            self.key_a,
            lease_owner="failure-two",
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
        self.assertEqual(
            len(
                self.store.claim_due(
                    owner="partial", limit=1, lease_sec=60, now=1010
                )
            ),
            1,
        )
        self.store.record_scan(
            self.key_a,
            lease_owner="partial",
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

    def test_schema_v1_unresolved_rows_migrate_to_open_v2(self) -> None:
        self.store.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.store.path)) as conn:
            conn.executescript(
                """
                CREATE TABLE automation_meta(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT INTO automation_meta VALUES('schema_version', '1');
                CREATE TABLE unresolved_signals(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_public_ref TEXT NOT NULL,
                    source_signal_id INTEGER,
                    source_module TEXT NOT NULL,
                    source_symbol TEXT NOT NULL,
                    source_ts INTEGER NOT NULL,
                    source_payload_hash TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    candidate_chain TEXT NOT NULL DEFAULT '',
                    candidate_contract TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 1,
                    first_seen_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL,
                    UNIQUE(source_public_ref, source_payload_hash, reason)
                );
                INSERT INTO unresolved_signals(
                    source_public_ref, source_module, source_symbol, source_ts,
                    source_payload_hash, reason, first_seen_at, last_seen_at
                ) VALUES('launch:1', 'launch', 'AAAUSDT', 1000, 'x',
                         'registry_not_verified', 1001, 1001);
                """
            )
            conn.commit()
        self.store.migrate()
        with closing(sqlite3.connect(self.store.path)) as conn:
            version = conn.execute(
                "SELECT value FROM automation_meta WHERE key='schema_version'"
            ).fetchone()[0]
            row = conn.execute(
                "SELECT status, resolved_at, resolved_token_key, "
                "resolution_note FROM unresolved_signals"
            ).fetchone()
        self.assertEqual(version, "6")
        self.assertEqual(row, ("open", None, None, ""))

    def test_schema_v1_to_v2_failure_rolls_back(self) -> None:
        other_path = self.root / "rollback" / "oar_automation.db"
        other_path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(other_path)) as conn:
            conn.executescript(
                """
                CREATE TABLE automation_meta(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT INTO automation_meta VALUES('schema_version', '1');
                CREATE TABLE unresolved_signals(
                    id INTEGER PRIMARY KEY,
                    source_public_ref TEXT NOT NULL,
                    source_signal_id INTEGER,
                    source_module TEXT NOT NULL,
                    source_symbol TEXT NOT NULL,
                    source_ts INTEGER NOT NULL,
                    source_payload_hash TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    candidate_chain TEXT NOT NULL DEFAULT '',
                    candidate_contract TEXT NOT NULL DEFAULT '',
                    attempts INTEGER NOT NULL DEFAULT 1,
                    first_seen_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL,
                    UNIQUE(source_public_ref, source_payload_hash, reason)
                );
                """
            )
            conn.commit()
        store = AutomationStore(other_path, data_dir=self.root)

        def fail_v2(conn: sqlite3.Connection) -> None:
            conn.execute(
                "ALTER TABLE unresolved_signals "
                "ADD COLUMN status TEXT NOT NULL DEFAULT 'open'"
            )
            raise RuntimeError("fixture")

        with patch.object(
            AutomationStore, "_migrate_v1_to_v2", side_effect=fail_v2
        ):
            with self.assertRaises(RuntimeError):
                store.migrate()
        with closing(sqlite3.connect(other_path)) as conn:
            version = conn.execute(
                "SELECT value FROM automation_meta WHERE key='schema_version'"
            ).fetchone()[0]
            columns = {
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info(unresolved_signals)"
                ).fetchall()
            }
        self.assertEqual(version, "1")
        self.assertNotIn("status", columns)

    def test_schema_v2_migrates_historical_baseline_columns(self) -> None:
        other_path = self.root / "v2" / "oar_automation.db"
        other_path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(other_path)) as conn:
            conn.executescript(
                """
                CREATE TABLE automation_meta(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT INTO automation_meta VALUES('schema_version', '2');
                CREATE TABLE watch_scan_runs(
                    scan_id TEXT PRIMARY KEY,
                    token_key TEXT NOT NULL,
                    started_at INTEGER NOT NULL,
                    completed_at INTEGER,
                    status TEXT NOT NULL,
                    activity_complete INTEGER,
                    analysis_complete INTEGER,
                    analysis_status TEXT NOT NULL DEFAULT '',
                    behavior_type TEXT NOT NULL DEFAULT '',
                    behavior_score INTEGER,
                    max_wallet_group_score INTEGER,
                    transfer_count INTEGER,
                    rpc_request_count INTEGER,
                    context_hash TEXT NOT NULL DEFAULT '',
                    notification_status TEXT NOT NULL DEFAULT '',
                    notification_reason TEXT NOT NULL DEFAULT '',
                    error_code TEXT NOT NULL DEFAULT '',
                    source_refs_json TEXT NOT NULL DEFAULT '[]'
                );
                """
            )
            conn.commit()
        store = AutomationStore(other_path, data_dir=self.root)
        store.migrate()
        with closing(sqlite3.connect(other_path)) as conn:
            version = conn.execute(
                "SELECT value FROM automation_meta WHERE key='schema_version'"
            ).fetchone()[0]
            columns = {
                str(row[1])
                for row in conn.execute(
                    "PRAGMA table_info(watch_scan_runs)"
                ).fetchall()
            }
        self.assertEqual(version, "6")
        self.assertTrue(
            {
                "query_window",
                "total_token_amount",
                "unique_senders",
                "unique_receivers",
                "baseline_status",
                "baseline_anomaly",
                "baseline_json",
                "address_intelligence_queue_status",
                "address_intelligence_queue_error",
                "unknown_addresses_queued",
                "external_label_provider_calls",
            }.issubset(columns)
        )

    def test_schema_v3_migrates_scan_side_effect_audit_columns(self) -> None:
        other_path = self.root / "v3" / "oar_automation.db"
        other_path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(other_path)) as conn:
            conn.executescript(
                """
                CREATE TABLE automation_meta(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT INTO automation_meta VALUES('schema_version', '3');
                CREATE TABLE watch_scan_runs(
                    scan_id TEXT PRIMARY KEY,
                    token_key TEXT NOT NULL,
                    started_at INTEGER NOT NULL,
                    completed_at INTEGER,
                    status TEXT NOT NULL,
                    activity_complete INTEGER,
                    analysis_complete INTEGER,
                    analysis_status TEXT NOT NULL DEFAULT '',
                    behavior_type TEXT NOT NULL DEFAULT '',
                    behavior_score INTEGER,
                    max_wallet_group_score INTEGER,
                    transfer_count INTEGER,
                    rpc_request_count INTEGER,
                    query_window TEXT NOT NULL DEFAULT '',
                    total_token_amount TEXT NOT NULL DEFAULT '',
                    unique_senders INTEGER,
                    unique_receivers INTEGER,
                    baseline_status TEXT NOT NULL DEFAULT '',
                    baseline_anomaly INTEGER NOT NULL DEFAULT 0,
                    baseline_json TEXT NOT NULL DEFAULT '{}',
                    context_hash TEXT NOT NULL DEFAULT '',
                    notification_status TEXT NOT NULL DEFAULT '',
                    notification_reason TEXT NOT NULL DEFAULT '',
                    error_code TEXT NOT NULL DEFAULT '',
                    source_refs_json TEXT NOT NULL DEFAULT '[]'
                );
                INSERT INTO watch_scan_runs(
                    scan_id, token_key, started_at, completed_at, status,
                    activity_complete, analysis_complete
                ) VALUES('old', '8453:0x1111111111111111111111111111111111111111',
                         1000, 1001, 'ok', 1, 1);
                """
            )
            conn.commit()
        store = AutomationStore(other_path, data_dir=self.root)
        store.migrate()
        with closing(sqlite3.connect(other_path)) as conn:
            version = conn.execute(
                "SELECT value FROM automation_meta WHERE key='schema_version'"
            ).fetchone()[0]
            row = conn.execute(
                "SELECT address_intelligence_queue_status, "
                "address_intelligence_queue_error, "
                "unknown_addresses_queued, external_label_provider_calls "
                "FROM watch_scan_runs WHERE scan_id='old'"
            ).fetchone()
        self.assertEqual(version, "6")
        self.assertEqual(row, ("not_recorded", "", 0, 0))

    def test_schema_v4_migrates_watch_source_direction(self) -> None:
        other_path = self.root / "v4" / "oar_automation.db"
        other_path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(other_path)) as conn:
            conn.executescript(
                """
                CREATE TABLE automation_meta(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT INTO automation_meta VALUES('schema_version', '4');
                CREATE TABLE watch_sources(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    token_key TEXT NOT NULL,
                    source_module TEXT NOT NULL,
                    source_public_ref TEXT NOT NULL,
                    source_signal_id INTEGER,
                    source_symbol TEXT NOT NULL,
                    source_score REAL,
                    source_priority INTEGER NOT NULL,
                    source_stage TEXT NOT NULL DEFAULT '',
                    source_severity TEXT NOT NULL DEFAULT '',
                    source_summary TEXT NOT NULL DEFAULT '',
                    source_ts INTEGER NOT NULL,
                    source_payload_hash TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(source_public_ref, token_key)
                );
                INSERT INTO watch_sources(
                    token_key, source_module, source_public_ref,
                    source_symbol, source_priority, source_ts,
                    source_payload_hash, expires_at, created_at, updated_at
                ) VALUES(
                    '8453:0x1111111111111111111111111111111111111111',
                    'launch', 'sig_old', 'AAAUSDT', 100, 1000,
                    'hash', 2000, 1000, 1000
                );
                """
            )
            conn.commit()
        store = AutomationStore(other_path, data_dir=self.root)
        store.migrate()
        with closing(sqlite3.connect(other_path)) as conn:
            version = conn.execute(
                "SELECT value FROM automation_meta WHERE key='schema_version'"
            ).fetchone()[0]
            row = conn.execute(
                "SELECT source_direction FROM watch_sources "
                "WHERE source_public_ref='sig_old'"
            ).fetchone()
        self.assertEqual(version, "6")
        self.assertEqual(row, ("",))

    def test_schema_v5_migrates_private_rolling_tables(self) -> None:
        other_path = self.root / "v5" / "oar_automation.db"
        other_path.parent.mkdir(parents=True)
        with closing(sqlite3.connect(other_path)) as conn:
            conn.executescript(
                """
                CREATE TABLE automation_meta(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT INTO automation_meta VALUES('schema_version', '5');
                """
            )
            conn.commit()
        store = AutomationStore(other_path, data_dir=self.root)
        store.migrate()
        with closing(sqlite3.connect(other_path)) as conn:
            version = conn.execute(
                "SELECT value FROM automation_meta WHERE key='schema_version'"
            ).fetchone()[0]
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        self.assertEqual(version, "6")
        self.assertIn("watch_scan_coverage", tables)
        self.assertIn("watch_scan_events", tables)

    def test_rolling_windows_require_continuous_coverage_and_deduplicate(self) -> None:
        self.add()
        self.verify()
        self.store.add_manual_watch(
            self.key_a,
            ttl_sec=100000,
            priority=100,
            query_window="15m",
            scan_interval_sec=60,
            now=1002,
        )
        observations = (
            self.rolling_observation(
                2000, 2900, [(1, 2880, "1", "inflow")]
            ),
            self.rolling_observation(
                2840, 3740, [(1, 2880, "1", "inflow")]
            ),
            self.rolling_observation(
                3680, 4580, [(2, 4000, "2", "unclassified")]
            ),
            self.rolling_observation(
                4520, 5420, [(3, 5000, "3", "outflow")]
            ),
        )
        for index, observation in enumerate(observations):
            self.persist_rolling_scan(
                self.key_a,
                observation,
                owner=f"rolling-{index}",
            )
        current = self.rolling_observation(
            4700, 5600, [(4, 5500, "4", "unclassified")]
        )

        result = self.store.rolling_window_snapshot(self.key_a, current)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["coverage"]["1h"]["status"], "complete")
        self.assertEqual(result["coverage"]["1h"]["covered_seconds"], 3600)
        self.assertEqual(
            result["coverage"]["1h"]["deduplicated_event_count"], 1
        )
        self.assertEqual(result["windows"]["1h"]["transfer_count"], 4)
        self.assertEqual(result["windows"]["1h"]["total_token_amount"], "10")
        self.assertEqual(result["windows"]["1h"]["inflow_count"], 1)
        self.assertEqual(result["windows"]["1h"]["outflow_count"], 1)
        self.assertEqual(result["windows"]["1h"]["unclassified_count"], 2)
        self.assertEqual(result["coverage"]["4h"]["status"], "collecting")
        self.assertNotIn("4h", result["windows"])
        self.assertFalse(result["network_activity"])
        self.assertEqual(result["external_provider_calls"], 0)

    def test_rolling_window_gap_never_claims_complete(self) -> None:
        self.add()
        self.verify()
        self.store.add_manual_watch(
            self.key_a,
            ttl_sec=100000,
            priority=100,
            query_window="15m",
            scan_interval_sec=60,
            now=1002,
        )
        for index, observation in enumerate(
            (
                self.rolling_observation(2000, 2900),
                self.rolling_observation(3000, 3900),
                self.rolling_observation(3900, 4800),
            )
        ):
            self.persist_rolling_scan(
                self.key_a,
                observation,
                owner=f"gap-{index}",
            )
        current = self.rolling_observation(4700, 5600)

        result = self.store.rolling_window_snapshot(self.key_a, current)

        self.assertEqual(result["coverage"]["1h"]["status"], "collecting")
        self.assertLess(result["coverage"]["1h"]["covered_seconds"], 3600)
        self.assertNotIn("1h", result["windows"])

    def test_rolling_snapshot_supports_exact_complete_24h_coverage(self) -> None:
        self.add()
        self.verify()
        self.store.migrate()
        anchor = 100000
        first = anchor - 24 * 60 * 60
        with self.store.connect() as conn:
            conn.executemany(
                """
                INSERT INTO watch_scan_coverage(
                    scan_id, token_key, from_time, to_time,
                    event_count, created_at
                ) VALUES(?, ?, ?, ?, 0, ?)
                """,
                [
                    (
                        f"coverage-{index}",
                        self.key_a,
                        first + index * 900,
                        first + (index + 1) * 900,
                        anchor,
                    )
                    for index in range(95)
                ],
            )
            conn.commit()
        current = self.rolling_observation(anchor - 900, anchor)

        result = self.store.rolling_window_snapshot(self.key_a, current)

        self.assertEqual(result["coverage"]["15m"]["status"], "complete")
        self.assertEqual(result["coverage"]["1h"]["status"], "complete")
        self.assertEqual(result["coverage"]["4h"]["status"], "complete")
        self.assertEqual(result["coverage"]["24h"]["status"], "complete")
        self.assertEqual(
            result["coverage"]["24h"]["covered_seconds"], 24 * 60 * 60
        )
        self.assertEqual(result["windows"]["24h"]["transfer_count"], 0)

    def test_success_schedule_uses_scan_start_to_avoid_duration_drift(self) -> None:
        self.add()
        self.verify()
        self.store.add_manual_watch(
            self.key_a,
            ttl_sec=100000,
            priority=100,
            query_window="4h",
            scan_interval_sec=900,
            now=1002,
        )
        self.store.claim_due(owner="schedule", limit=1, lease_sec=600, now=2000)
        self.store.record_scan(
            self.key_a,
            lease_owner="schedule",
            started_at=2000,
            status="ok",
            activity_complete=True,
            analysis_complete=True,
            source_refs=[],
            scan_interval_sec=900,
            max_consecutive_failures=10,
            now=2060,
        )

        self.assertEqual(self.store.get_watch(self.key_a)["next_scan_at"], 2900)

    def test_scan_side_effect_audit_is_fixed_and_readable(self) -> None:
        self.add()
        self.verify()
        self.store.add_manual_watch(
            self.key_a,
            ttl_sec=100000,
            priority=100,
            query_window="15m",
            scan_interval_sec=60,
            now=1002,
        )
        self.store.claim_due(owner="audit", limit=1, lease_sec=60, now=1002)
        scan_id = self.store.record_scan(
            self.key_a,
            lease_owner="audit",
            started_at=1002,
            status="ok",
            activity_complete=True,
            analysis_complete=True,
            historical_baseline={"status": "cold_start", "anomaly": False},
            source_refs=[],
            scan_interval_sec=60,
            max_consecutive_failures=10,
            now=1003,
        )
        self.assertTrue(
            self.store.update_scan_address_intelligence_audit(
                scan_id,
                self.key_a,
                queue_status="ok",
                queue_error="",
                unknown_addresses_queued=7,
            )
        )
        audit = self.store.latest_scan_baseline(self.key_a) or {}
        self.assertEqual(audit["address_intelligence_queue_status"], "ok")
        self.assertEqual(audit["address_intelligence_queue_error"], "")
        self.assertEqual(audit["unknown_addresses_queued"], 7)
        self.assertEqual(audit["external_label_provider_calls"], 0)
        with self.assertRaisesRegex(
            AutomationStoreError, "unsupported values"
        ):
            self.store.update_scan_address_intelligence_audit(
                scan_id,
                self.key_a,
                queue_status="local_error",
                queue_error="secret provider response",
                unknown_addresses_queued=0,
            )

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
        self.assertEqual(
            len(
                self.store.claim_due(
                    owner="audit", limit=1, lease_sec=60, now=1204
                )
            ),
            1,
        )
        self.store.record_scan(
            self.key_a,
            lease_owner="audit",
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

    def test_registry_verifies_configured_non_base_chain(self) -> None:
        chains_path = self.root / "chains.json"
        chains_path.write_text(
            json.dumps(
                {
                    "schema_version": "test-v1",
                    "chains": [
                        {
                            "chain_id": 1,
                            "slug": "ethereum",
                            "name": "Ethereum",
                            "enabled": True,
                            "confirmation_depth": 64,
                            "bootstrap_lookback_blocks": 512,
                            "reorg_lookback_blocks": 128,
                            "http_rpc_env": "ONCHAIN_ETHEREUM_HTTP_RPC_URL",
                            "explorer_tx_url": (
                                "https://etherscan.invalid/tx/{tx_hash}"
                            ),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        settings = make_settings(self.root, chains_path=chains_path)
        store = AutomationStore.from_settings(settings)
        pending = store.add_registry(
            market_symbol="AAAUSDT",
            contract=CONTRACT_A,
            chain="ethereum",
            chain_id=1,
            source="manual",
            now=1000,
        )

        class EthereumRpc:
            @staticmethod
            def chain_id() -> int:
                return 1

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
                text = b"AAA" if selector == "0x95d89b41" else b"Token"
                return "0x" + text.ljust(32, b"\x00").hex()

        verified = RegistryService(
            settings, store, rpc=EthereumRpc()
        ).verify(
            str(pending["token_key"]),
            allow_network=True,
            set_primary=True,
            accept_symbol_mismatch=False,
        )
        self.assertEqual(verified["chain"], "ethereum")
        self.assertEqual(verified["chain_id"], 1)
        self.assertEqual(
            verified["verification_method"],
            "ethereum_rpc_erc20_metadata",
        )

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
