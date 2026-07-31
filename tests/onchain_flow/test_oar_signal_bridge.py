from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from paopao_radar.onchain_flow.automation_store import AutomationStore
from paopao_radar.onchain_flow.registry import RegistryService
from paopao_radar.onchain_flow.signal_bridge import (
    MainSignalReader,
    SignalBridge,
)

from tests.onchain_flow.support import make_settings


CONTRACT_A = "0x1111111111111111111111111111111111111111"
CONTRACT_B = "0x2222222222222222222222222222222222222222"


class MetadataRpc:
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
        text = b"AAA" if selector == "0x95d89b41" else b"Token"
        return "0x" + text.ljust(32, b"\x00").hex()


def create_signal_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            """
            CREATE TABLE signals (
                id INTEGER PRIMARY KEY,
                public_ref TEXT NOT NULL,
                ts INTEGER NOT NULL,
                module TEXT NOT NULL,
                template_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                stage TEXT NOT NULL,
                severity TEXT NOT NULL,
                score REAL,
                excerpt TEXT NOT NULL,
                text_html TEXT NOT NULL,
                status TEXT NOT NULL,
                sent INTEGER NOT NULL,
                ingest_mode TEXT NOT NULL,
                quality_status TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        conn.commit()


def insert_signal(
    path: Path,
    *,
    signal_id: int,
    public_ref: str,
    ts: int,
    module: str = "launch",
    symbol: str = "AAAUSDT",
    status: str = "sent",
    sent: int = 1,
    ingest_mode: str = "structured",
    quality_status: str = "ready",
    payload: dict[str, object] | None = None,
) -> None:
    with closing(sqlite3.connect(path)) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO signals(
                id, public_ref, ts, module, template_id, symbol, stage,
                severity, score, excerpt, text_html, status, sent,
                ingest_mode, quality_status, payload_json
            ) VALUES(?, ?, ?, ?, 'T', ?, 'watch', 'info', 80,
                     'safe summary', 'SECRET FULL MESSAGE', ?, ?, ?, ?, ?)
            """,
            (
                signal_id,
                public_ref,
                ts,
                module,
                symbol,
                status,
                sent,
                ingest_mode,
                quality_status,
                json.dumps(payload or {}, separators=(",", ":")),
            ),
        )
        conn.commit()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SignalBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = make_settings(self.root)
        self.store = AutomationStore.from_settings(self.settings)
        self.signal_db = self.settings.main_signal_db_path

    def tearDown(self) -> None:
        self.temp.cleanup()

    def verified(self, contract: str = CONTRACT_A) -> str:
        item = self.store.add_registry(
            market_symbol="AAAUSDT",
            contract=contract,
            source="manual",
            now=1000,
        )
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
        return str(item["token_key"])

    def test_missing_main_database_is_not_created(self) -> None:
        result = MainSignalReader(self.signal_db).read(
            checkpoint_ts=0,
            checkpoint_id=0,
            overlap_sec=300,
            bootstrap_lookback_sec=3600,
            limit=100,
            now=5000,
        )
        self.assertEqual(result["status"], "source_not_initialized")
        self.assertFalse(self.signal_db.exists())

    def test_bridge_missing_source_creates_no_automation_database(self) -> None:
        result = SignalBridge(
            self.settings, self.store, clock=lambda: 5000
        ).run_once()
        self.assertEqual(result["source_status"], "source_not_initialized")
        self.assertFalse(result["database_writes"])
        self.assertFalse(self.store.path.exists())

    def test_reader_is_read_only_and_excludes_full_text(self) -> None:
        create_signal_db(self.signal_db)
        insert_signal(
            self.signal_db,
            signal_id=1,
            public_ref="launch:1",
            ts=4500,
        )
        before_hash = sha256(self.signal_db)
        before_schema = self._schema()
        result = MainSignalReader(self.signal_db).read(
            checkpoint_ts=0,
            checkpoint_id=0,
            overlap_sec=300,
            bootstrap_lookback_sec=3600,
            limit=100,
            now=5000,
        )
        self.assertEqual(result["status"], "ok")
        self.assertNotIn(
            "SECRET FULL MESSAGE",
            json.dumps(result, ensure_ascii=False),
        )
        self.assertEqual(before_hash, sha256(self.signal_db))
        self.assertEqual(before_schema, self._schema())
        self.assertFalse(self.signal_db.with_name("signals.db-wal").exists())

    def test_invalid_source_database_fails_without_being_called_locked(
        self,
    ) -> None:
        self.signal_db.parent.mkdir(parents=True)
        self.signal_db.write_bytes(b"not-a-sqlite-database")
        result = MainSignalReader(self.signal_db).read(
            checkpoint_ts=0,
            checkpoint_id=0,
            overlap_sec=300,
            bootstrap_lookback_sec=3600,
            limit=100,
            now=5000,
        )
        self.assertEqual(result["status"], "source_failed")

    def test_locked_source_degrades_without_writes(self) -> None:
        create_signal_db(self.signal_db)
        with patch(
            "paopao_radar.onchain_flow.signal_bridge.sqlite3.connect",
            side_effect=sqlite3.OperationalError("database is locked"),
        ):
            result = SignalBridge(
                self.settings, self.store, clock=lambda: 5000
            ).run_once()
        self.assertEqual(result["source_status"], "source_locked")
        self.assertFalse(result["database_writes"])
        self.assertFalse(self.store.path.exists())

    def test_bridge_resolves_verified_primary_and_creates_watch(self) -> None:
        key = self.verified()
        create_signal_db(self.signal_db)
        insert_signal(
            self.signal_db,
            signal_id=1,
            public_ref="launch:1",
            ts=4500,
        )
        result = SignalBridge(
            self.settings, self.store, clock=lambda: 5000
        ).run_once()
        self.assertEqual(result["resolved"], 1)
        self.assertEqual(result["watch_created"], 1)
        self.assertEqual(self.store.get_watch(key)["status"], "active")

    def test_pending_and_missing_registry_are_unresolved(self) -> None:
        self.store.add_registry(
            market_symbol="AAAUSDT",
            contract=CONTRACT_A,
            source="manual",
            now=1000,
        )
        create_signal_db(self.signal_db)
        insert_signal(
            self.signal_db,
            signal_id=1,
            public_ref="launch:1",
            ts=4500,
        )
        result = SignalBridge(
            self.settings, self.store, clock=lambda: 5000
        ).run_once()
        self.assertEqual(result["unresolved"], 1)
        with closing(sqlite3.connect(self.store.path)) as conn:
            reason = conn.execute(
                "SELECT reason FROM unresolved_signals"
            ).fetchone()[0]
        self.assertEqual(reason, "registry_not_verified")

    def test_primary_verification_reconciles_signal_outside_overlap(self) -> None:
        pending = self.store.add_registry(
            market_symbol="AAAUSDT",
            contract=CONTRACT_A,
            source="manual",
            now=1000,
        )
        create_signal_db(self.signal_db)
        insert_signal(
            self.signal_db,
            signal_id=1,
            public_ref="launch:old",
            ts=4500,
        )
        SignalBridge(
            self.settings, self.store, clock=lambda: 5000
        ).run_once()
        insert_signal(
            self.signal_db,
            signal_id=2,
            public_ref="onchain:new",
            ts=10000,
            module="onchain",
        )
        SignalBridge(
            self.settings, self.store, clock=lambda: 10010
        ).run_once()
        self.assertEqual(self.store.bridge_checkpoint(), (10000, 2))
        before_hash = sha256(self.signal_db)
        before_schema = self._schema()
        with closing(sqlite3.connect(self.signal_db)) as conn:
            before_count = conn.execute(
                "SELECT COUNT(*) FROM signals"
            ).fetchone()[0]
        bridge = SignalBridge(
            self.settings, self.store, clock=lambda: 10020
        )
        verified = RegistryService(
            self.settings,
            self.store,
            rpc=MetadataRpc(),
            bridge=bridge,
        ).verify(
            str(pending["token_key"]),
            allow_network=True,
            set_primary=True,
            accept_symbol_mismatch=False,
        )
        self.assertEqual(verified["reconciliation"]["resolved"], 1)
        self.assertEqual(verified["reconciliation"]["watch_created"], 1)
        self.assertIsNotNone(self.store.get_watch(str(pending["token_key"])))
        with closing(sqlite3.connect(self.store.path)) as conn:
            unresolved = conn.execute(
                "SELECT status, resolved_token_key FROM unresolved_signals "
                "WHERE source_public_ref='launch:old'"
            ).fetchone()
        self.assertEqual(
            unresolved,
            ("resolved", str(pending["token_key"])),
        )
        self.assertEqual(before_hash, sha256(self.signal_db))
        self.assertEqual(before_schema, self._schema())
        with closing(sqlite3.connect(self.signal_db)) as conn:
            after_count = conn.execute(
                "SELECT COUNT(*) FROM signals"
            ).fetchone()[0]
        self.assertEqual(before_count, after_count)
        self.assertFalse(self.signal_db.with_name("signals.db-wal").exists())

    def test_expired_unresolved_is_audited_without_watch(self) -> None:
        settings = make_settings(
            self.root,
            oar_watch_launch_ttl_sec=100,
        )
        pending = self.store.add_registry(
            market_symbol="AAAUSDT",
            contract=CONTRACT_A,
            source="manual",
            now=1000,
        )
        create_signal_db(self.signal_db)
        insert_signal(
            self.signal_db,
            signal_id=1,
            public_ref="launch:expired",
            ts=1000,
        )
        SignalBridge(settings, self.store, clock=lambda: 1050).run_once()
        verified = RegistryService(
            settings,
            self.store,
            rpc=MetadataRpc(),
            bridge=SignalBridge(
                settings, self.store, clock=lambda: 1200
            ),
        ).verify(
            str(pending["token_key"]),
            allow_network=True,
            set_primary=True,
            accept_symbol_mismatch=False,
        )
        self.assertEqual(verified["reconciliation"]["expired"], 1)
        self.assertIsNone(self.store.get_watch(str(pending["token_key"])))
        with closing(sqlite3.connect(self.store.path)) as conn:
            status = conn.execute(
                "SELECT status FROM unresolved_signals "
                "WHERE source_public_ref='launch:expired'"
            ).fetchone()[0]
        self.assertEqual(status, "expired")

    def test_secondary_verification_does_not_reconcile(self) -> None:
        pending = self.store.add_registry(
            market_symbol="AAAUSDT",
            contract=CONTRACT_A,
            source="manual",
            now=1000,
        )
        reader = type(
            "NoReadReader",
            (),
            {
                "read_by_public_refs": lambda self, refs, limit=100: (
                    (_ for _ in ()).throw(AssertionError("source read"))
                )
            },
        )()
        verified = RegistryService(
            self.settings,
            self.store,
            rpc=MetadataRpc(),
            bridge=SignalBridge(
                self.settings,
                self.store,
                reader=reader,
                clock=lambda: 2000,
            ),
        ).verify(
            str(pending["token_key"]),
            allow_network=True,
            set_primary=False,
            accept_symbol_mismatch=False,
        )
        self.assertEqual(verified["is_primary"], 0)
        self.assertEqual(verified["reconciliation"]["status"], "not_primary")

    def test_bridge_retries_open_unresolved_when_source_recovers(self) -> None:
        pending = self.store.add_registry(
            market_symbol="AAAUSDT",
            contract=CONTRACT_A,
            source="manual",
            now=1000,
        )
        create_signal_db(self.signal_db)
        insert_signal(
            self.signal_db,
            signal_id=1,
            public_ref="launch:retry",
            ts=4500,
        )
        SignalBridge(
            self.settings, self.store, clock=lambda: 5000
        ).run_once()
        locked_reader = type(
            "LockedReader",
            (),
            {
                "read_by_public_refs": lambda self, refs, limit=100: {
                    "status": "source_locked",
                    "signals": [],
                }
            },
        )()
        token = RegistryService(
            self.settings,
            self.store,
            rpc=MetadataRpc(),
            bridge=SignalBridge(
                self.settings,
                self.store,
                reader=locked_reader,
                clock=lambda: 5002,
            ),
        ).verify(
            str(pending["token_key"]),
            allow_network=True,
            set_primary=True,
            accept_symbol_mismatch=False,
        )
        degraded = token["reconciliation"]
        self.assertEqual(token["status"], "verified")
        self.assertEqual(degraded["status"], "source_unavailable")
        self.assertEqual(self.store.open_unresolved_count(), 1)
        recovered = SignalBridge(
            self.settings, self.store, clock=lambda: 5003
        ).run_once()
        self.assertEqual(recovered["reconciliation"]["resolved"], 1)
        self.assertEqual(self.store.open_unresolved_count(), 0)
        sources = self.store.active_sources(
            str(pending["token_key"]), now=5003
        )
        self.assertEqual(len(sources), 1)

    def test_onchain_unresolved_is_never_reconciled(self) -> None:
        key = self.verified()
        signal = {
            "id": 1,
            "public_ref": "onchain:fixture",
            "ts": 4500,
            "module": "onchain",
            "symbol": "AAAUSDT",
            "status": "sent",
            "sent": 1,
            "ingest_mode": "structured",
            "quality_status": "ready",
            "payload_hash": "x",
        }
        self.store.process_bridge_signal(
            signal,
            resolution={"status": "registry_not_verified", "token": None},
            source_ttl_sec=3600,
            source_priority=100,
            query_window="4h",
            scan_interval_sec=900,
            max_active_tokens=50,
            now=4501,
        )
        create_signal_db(self.signal_db)
        insert_signal(
            self.signal_db,
            signal_id=1,
            public_ref="onchain:fixture",
            ts=4500,
            module="onchain",
        )
        result = SignalBridge(
            self.settings, self.store, clock=lambda: 5000
        ).reconcile_open()
        self.assertEqual(result["resolved"], 0)
        self.assertEqual(self.store.open_unresolved_count(), 1)
        self.assertIsNone(self.store.get_watch(key))

    def test_structured_contract_creates_pending_not_watch(self) -> None:
        create_signal_db(self.signal_db)
        insert_signal(
            self.signal_db,
            signal_id=1,
            public_ref="announcement:1",
            ts=4500,
            module="announcement",
            payload={
                "facts": {"chain": "base", "contract": CONTRACT_A}
            },
        )
        SignalBridge(
            self.settings, self.store, clock=lambda: 5000
        ).run_once()
        registry = self.store.list_registry(market_symbol="AAAUSDT") or []
        self.assertEqual(registry[0]["status"], "pending")
        self.assertIsNone(self.store.get_watch(registry[0]["token_key"]))

    def test_ineligible_and_onchain_signals_never_create_watch(self) -> None:
        key = self.verified()
        create_signal_db(self.signal_db)
        insert_signal(
            self.signal_db,
            signal_id=1,
            public_ref="dry:1",
            ts=4500,
            status="dry_run",
            sent=0,
            payload={
                "facts": {"chain": "base", "contract": CONTRACT_A}
            },
        )
        insert_signal(
            self.signal_db,
            signal_id=2,
            public_ref="onchain:2",
            ts=4501,
            module="onchain",
        )
        result = SignalBridge(
            self.settings, self.store, clock=lambda: 5000
        ).run_once()
        self.assertEqual(result["eligible_signals"], 0)
        self.assertEqual(result["ignored_onchain"], 1)
        self.assertEqual(result["ignored_not_sent"], 1)
        self.assertEqual(result["unresolved"], 0)
        self.assertEqual(self.store.list_watch_items() or [], [])
        self.assertEqual(
            self.store.active_sources(key, now=5000),
            [],
        )
        with closing(sqlite3.connect(self.store.path)) as conn:
            unresolved = conn.execute(
                "SELECT source_public_ref FROM unresolved_signals "
                "ORDER BY source_public_ref"
            ).fetchall()
        self.assertEqual(unresolved, [])

    def test_reconciliation_expires_legacy_dry_run_unresolved(self) -> None:
        signal = {
            "id": 1,
            "public_ref": "legacy-dry:1",
            "ts": 4500,
            "module": "launch",
            "symbol": "AAAUSDT",
            "status": "dry_run",
            "sent": 0,
            "ingest_mode": "structured",
            "quality_status": "ready",
            "payload_hash": "legacy",
        }
        self.store.process_bridge_signal(
            signal,
            resolution={"status": "ineligible_signal", "token": None},
            source_ttl_sec=3600,
            source_priority=100,
            query_window="4h",
            scan_interval_sec=900,
            max_active_tokens=50,
            now=4501,
        )
        create_signal_db(self.signal_db)
        insert_signal(
            self.signal_db,
            signal_id=1,
            public_ref="legacy-dry:1",
            ts=4500,
            status="dry_run",
            sent=0,
        )

        result = SignalBridge(
            self.settings, self.store, clock=lambda: 5000
        ).reconcile_open()

        self.assertEqual(result["expired"], 1)
        self.assertEqual(result["remaining_open"], 0)
        self.assertEqual(self.store.open_unresolved_count(), 0)

    def test_text_fallback_and_degraded_are_not_consumed(self) -> None:
        self.verified()
        create_signal_db(self.signal_db)
        insert_signal(
            self.signal_db,
            signal_id=1,
            public_ref="fallback:1",
            ts=4500,
            ingest_mode="text_fallback",
        )
        insert_signal(
            self.signal_db,
            signal_id=2,
            public_ref="degraded:2",
            ts=4501,
            quality_status="degraded",
        )
        result = SignalBridge(
            self.settings, self.store, clock=lambda: 5000
        ).run_once()
        self.assertEqual(result["eligible_signals"], 0)
        self.assertEqual(self.store.list_watch_items() or [], [])

    def test_same_public_ref_refreshes_source_without_duplicate(self) -> None:
        key = self.verified()
        create_signal_db(self.signal_db)
        insert_signal(
            self.signal_db,
            signal_id=1,
            public_ref="launch:stable",
            ts=4500,
        )
        bridge = SignalBridge(
            self.settings, self.store, clock=lambda: 5000
        )
        bridge.run_once()
        insert_signal(
            self.signal_db,
            signal_id=1,
            public_ref="launch:stable",
            ts=5100,
            payload={"facts": {"revision": 2}},
        )
        SignalBridge(
            self.settings, self.store, clock=lambda: 5200
        ).run_once()
        sources = self.store.active_sources(key, now=5200)
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["source_ts"], 5100)

    def test_unchanged_overlap_does_not_bypass_scan_interval(self) -> None:
        key = self.verified()
        create_signal_db(self.signal_db)
        insert_signal(
            self.signal_db,
            signal_id=1,
            public_ref="launch:stable",
            ts=4500,
        )
        SignalBridge(
            self.settings, self.store, clock=lambda: 5000
        ).run_once()
        self.assertEqual(
            len(
                self.store.claim_due(
                    owner="bridge-test",
                    limit=1,
                    lease_sec=60,
                    now=5000,
                )
            ),
            1,
        )
        self.store.record_scan(
            key,
            lease_owner="bridge-test",
            started_at=5000,
            status="ok",
            activity_complete=True,
            analysis_complete=True,
            source_refs=["launch:stable"],
            scan_interval_sec=900,
            max_consecutive_failures=10,
            now=5001,
        )
        self.assertEqual(self.store.get_watch(key)["next_scan_at"], 5901)
        SignalBridge(
            self.settings, self.store, clock=lambda: 5200
        ).run_once()
        self.assertEqual(self.store.get_watch(key)["next_scan_at"], 5901)
        insert_signal(
            self.signal_db,
            signal_id=1,
            public_ref="launch:stable",
            ts=4500,
            payload={"facts": {"revision": 2}},
        )
        SignalBridge(
            self.settings, self.store, clock=lambda: 5300
        ).run_once()
        self.assertEqual(self.store.get_watch(key)["next_scan_at"], 5300)

    def test_source_priority_and_ttl_are_persisted_and_recomputed(self) -> None:
        key = self.verified()
        create_signal_db(self.signal_db)
        insert_signal(
            self.signal_db,
            signal_id=1,
            public_ref="funding:1",
            ts=4500,
            module="funding",
        )
        settings = make_settings(
            self.root,
            oar_watch_funding_priority=13,
            oar_watch_funding_ttl_sec=600,
        )
        SignalBridge(settings, self.store, clock=lambda: 5000).run_once()
        watch = self.store.get_watch(key) or {}
        sources = self.store.active_sources(key, now=5000)
        self.assertEqual(watch["priority"], 13)
        self.assertEqual(watch["expires_at"], 5100)
        self.assertEqual(sources[0]["source_priority"], 13)
        self.assertEqual(sources[0]["expires_at"], 5100)

    def test_bootstrap_reads_only_configured_lookback(self) -> None:
        self.verified()
        create_signal_db(self.signal_db)
        insert_signal(
            self.signal_db,
            signal_id=1,
            public_ref="old:1",
            ts=1000,
        )
        insert_signal(
            self.signal_db,
            signal_id=2,
            public_ref="new:2",
            ts=4500,
        )
        result = SignalBridge(
            self.settings, self.store, clock=lambda: 5000
        ).run_once()
        self.assertEqual(result["scanned_signals"], 1)

    def test_capacity_rejection_is_audited_without_deleting_watch(self) -> None:
        first_key = self.verified(CONTRACT_A)
        self.store.add_manual_watch(
            first_key,
            ttl_sec=10000,
            priority=100,
            query_window="4h",
            scan_interval_sec=900,
            now=1002,
        )
        second = self.store.add_registry(
            market_symbol="BBBUSDT",
            contract=CONTRACT_B,
            source="manual",
            now=1000,
        )
        self.store.verify_registry(
            str(second["token_key"]),
            token_symbol="BBB",
            token_name="BBB",
            decimals=18,
            metadata_hash="b" * 64,
            verification_method="fixture",
            set_primary=True,
            now=1001,
        )
        create_signal_db(self.signal_db)
        insert_signal(
            self.signal_db,
            signal_id=1,
            public_ref="flow:1",
            ts=4500,
            module="flow",
            symbol="BBBUSDT",
        )
        settings = make_settings(
            self.root, oar_watch_max_active_tokens=1
        )
        result = SignalBridge(
            settings, self.store, clock=lambda: 5000
        ).run_once()
        self.assertEqual(result["capacity_rejected"], 1)
        self.assertEqual(self.store.get_watch(first_key)["status"], "active")

    def test_bridge_checkpoint_does_not_advance_on_processing_failure(self) -> None:
        self.verified()
        create_signal_db(self.signal_db)
        insert_signal(
            self.signal_db,
            signal_id=1,
            public_ref="launch:1",
            ts=4500,
        )
        original = self.store.process_bridge_signal

        def fail(*args: object, **kwargs: object) -> str:
            del args, kwargs
            raise sqlite3.OperationalError("fixture failure")

        self.store.process_bridge_signal = fail  # type: ignore[method-assign]
        with self.assertRaises(sqlite3.OperationalError):
            SignalBridge(
                self.settings, self.store, clock=lambda: 5000
            ).run_once()
        self.store.process_bridge_signal = original  # type: ignore[method-assign]
        self.assertEqual(self.store.bridge_checkpoint(), (0, 0))

    def _schema(self) -> list[tuple[str, str]]:
        with closing(sqlite3.connect(self.signal_db)) as conn:
            return [
                (str(row[0]), str(row[1]))
                for row in conn.execute(
                    "SELECT type, name FROM sqlite_master ORDER BY type, name"
                ).fetchall()
            ]


if __name__ == "__main__":
    unittest.main()
