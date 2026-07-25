from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.onchain_flow.arkham_normalizer import (
    normalize_arkham_transfer,
)
from paopao_radar.onchain_flow.db import OnchainStore
from paopao_radar.onchain_flow.migrations import apply_migrations
from paopao_radar.onchain_flow.token_policy import (
    ConfiguredTokenPolicy,
    NORMAL_TOKEN,
    STABLECOIN,
    UNKNOWN,
    WRAPPED_OR_RECEIPT,
)

from .support import make_settings


def party(
    address: str,
    entity_id: str | None,
    entity_type: str | None,
    name: str = "",
    label: str = "",
):
    result = {"address": address}
    if entity_id is not None or entity_type is not None:
        result["arkhamEntity"] = {
            "id": entity_id or "",
            "name": name,
            "type": entity_type or "",
        }
    if label:
        result["arkhamLabel"] = {"name": label}
    return result


def transfer_payload(
    *,
    transfer_id: str | None = "ark-1",
    from_party=None,
    to_party=None,
    historical_usd=2_000_000,
    token_id="test-token",
    token_address="0xtoken",
):
    payload = {
        "transactionHash": "0xtx",
        "blockTimestamp": "2026-07-25T00:00:00Z",
        "blockNumber": 100,
        "blockHash": "0xblock",
        "chain": "base",
        "tokenAddress": token_address,
        "tokenId": token_id,
        "tokenName": "Test Token",
        "tokenSymbol": "TST",
        "tokenDecimals": 18,
        "unitValue": 2,
        "historicalUSD": historical_usd,
        "fromAddress": from_party
        or party("0xfrom", "fund", "fund", "Fund"),
        "toAddress": to_party
        or party("0xto", "binance", "cex", "Binance"),
    }
    if transfer_id is not None:
        payload["id"] = transfer_id
    return payload


class ArkhamNormalizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.settings = make_settings(Path(self.temp.name))
        self.policy = ConfiguredTokenPolicy(self.settings)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def normalize(self, payload):
        return normalize_arkham_transfer(
            payload, token_policy=self.policy, received_at=1
        )

    def test_inflow_and_outflow_use_entity_types(self) -> None:
        inflow = self.normalize(transfer_payload())
        self.assertEqual(inflow.flow.flow_type, "inflow")
        self.assertEqual(inflow.flow.exchange_to, "Binance")
        self.assertEqual(
            inflow.flow.attribution_quality, "arkham_entity"
        )
        self.assertEqual(inflow.flow.label_confidence, 0.0)

        outbound = transfer_payload(
            transfer_id="ark-2",
            from_party=party(
                "0xfrom", "binance", "cex", "Binance"
            ),
            to_party=party("0xto", "fund", "fund", "Fund"),
        )
        outflow = self.normalize(outbound)
        self.assertEqual(outflow.flow.flow_type, "outflow")
        self.assertEqual(outflow.flow.exchange_from, "Binance")

    def test_anonymous_wallet_to_cex_is_inflow(self) -> None:
        inflow = self.normalize(
            transfer_payload(
                from_party=party("0xanonymous", None, None),
                to_party=party(
                    "0xbinance", "binance", "cex", "Binance"
                ),
            )
        )
        self.assertEqual(inflow.flow.flow_type, "inflow")
        self.assertEqual(inflow.flow.exchange_to, "Binance")
        self.assertEqual(
            inflow.flow.attribution_quality, "arkham_entity"
        )

    def test_cex_to_anonymous_wallet_is_outflow(self) -> None:
        outflow = self.normalize(
            transfer_payload(
                from_party=party(
                    "0xbinance", "binance", "cex", "Binance"
                ),
                to_party=party("0xanonymous", None, None),
            )
        )
        self.assertEqual(outflow.flow.flow_type, "outflow")
        self.assertEqual(outflow.flow.exchange_from, "Binance")
        self.assertEqual(
            outflow.flow.attribution_quality, "arkham_entity"
        )

    def test_label_only_counterparty_does_not_hide_cex(self) -> None:
        inflow = self.normalize(
            transfer_payload(
                from_party=party(
                    "0xlabeled",
                    None,
                    None,
                    label="Possible fund",
                ),
                to_party=party(
                    "0xbinance", "binance", "cex", "Binance"
                ),
            )
        )
        self.assertEqual(inflow.flow.flow_type, "inflow")
        self.assertEqual(
            inflow.flow.attribution_quality, "arkham_entity"
        )

    def test_internal_cross_cex_unresolved_and_non_cex(self) -> None:
        internal = self.normalize(
            transfer_payload(
                from_party=party(
                    "0xfrom", "binance", "cex", "Binance"
                ),
                to_party=party(
                    "0xto", "binance", "cex", "Binance"
                ),
            )
        )
        self.assertEqual(internal.flow.flow_type, "internal")

        cross = self.normalize(
            transfer_payload(
                transfer_id="ark-cross",
                from_party=party(
                    "0xfrom", "binance", "cex", "Binance"
                ),
                to_party=party(
                    "0xto", "coinbase", "cex", "Coinbase"
                ),
            )
        )
        self.assertEqual(cross.flow.flow_type, "cross_cex")

        unresolved = self.normalize(
            transfer_payload(
                from_party=party(
                    "0xfrom", "", "cex", "Binance"
                ),
                to_party=party("0xto", "", "cex", "Binance"),
            )
        )
        self.assertEqual(
            unresolved.flow.flow_type, "cex_to_cex_unresolved"
        )

        non_cex = self.normalize(
            transfer_payload(
                transfer_id="ark-non-cex",
                from_party=party("0xfrom", None, None),
                to_party=party("0xto", None, None),
            )
        )
        self.assertEqual(non_cex.flow.flow_type, "non_cex")
        self.assertEqual(
            non_cex.flow.attribution_quality, "unlabeled"
        )

    def test_historical_usd_validation_and_token_identity(self) -> None:
        valid = self.normalize(transfer_payload())
        self.assertEqual(str(valid.flow.amount_usd), "2000000")
        self.assertEqual(
            valid.flow.price_source, "arkham_historical_usd"
        )
        for invalid in (None, 0, -1, "NaN", "Infinity", "bad"):
            with self.subTest(value=invalid):
                event = self.normalize(
                    transfer_payload(
                        transfer_id=f"invalid-{invalid}",
                        historical_usd=invalid,
                    )
                )
                self.assertIsNone(event.flow.amount_usd)
                self.assertEqual(event.flow.price_status, "missing")
                self.assertEqual(event.raw.processed_status, "unpriced")

        no_contract = self.normalize(
            transfer_payload(
                transfer_id=None,
                token_address="",
                token_id="usd-coin",
            )
        )
        self.assertEqual(
            no_contract.transfer.token_address,
            "arkham-token:usd-coin",
        )
        again = self.normalize(
            transfer_payload(
                transfer_id=None,
                token_address="",
                token_id="usd-coin",
            )
        )
        self.assertEqual(no_contract.transfer.event_id, again.transfer.event_id)
        self.assertTrue(no_contract.transfer.event_id.startswith("arkham:"))

    def test_configured_token_policy_is_conservative(self) -> None:
        settings = replace(
            self.settings,
            stablecoin_token_ids=("usd-coin",),
            stablecoin_contracts=("base:0xstable",),
            wrapped_or_receipt_token_ids=("wrapped-eth",),
        )
        policy = ConfiguredTokenPolicy(settings)
        self.assertEqual(
            policy.classify(
                chain="base", token_id="usd-coin", token_address=""
            ),
            STABLECOIN,
        )
        self.assertEqual(
            policy.classify(
                chain="base",
                token_id="",
                token_address="0xstable",
            ),
            STABLECOIN,
        )
        self.assertEqual(
            policy.classify(
                chain="base",
                token_id="wrapped-eth",
                token_address="",
            ),
            WRAPPED_OR_RECEIPT,
        )
        self.assertEqual(
            policy.classify(
                chain="base", token_id="token", token_address=""
            ),
            NORMAL_TOKEN,
        )
        self.assertEqual(
            policy.classify(
                chain="base", token_id="", token_address=""
            ),
            UNKNOWN,
        )
        stable_event = normalize_arkham_transfer(
            transfer_payload(token_id="usd-coin", token_address=""),
            token_policy=policy,
            received_at=1,
        )
        self.assertEqual(stable_event.flow.token_policy, STABLECOIN)
        self.assertEqual(
            stable_event.flow.signal_context,
            "market_liquidity_context",
        )

    def test_default_stablecoin_ids_cannot_be_erased(self) -> None:
        policy = ConfiguredTokenPolicy(
            replace(self.settings, stablecoin_token_ids=())
        )
        for token_id in ("usd-coin", "tether", "dai"):
            with self.subTest(token_id=token_id):
                self.assertEqual(
                    policy.classify(
                        chain="base",
                        token_id=token_id,
                        token_address="",
                    ),
                    STABLECOIN,
                )
                normalized = normalize_arkham_transfer(
                    transfer_payload(
                        transfer_id=f"stable-{token_id}",
                        token_id=token_id,
                        token_address="",
                    ),
                    token_policy=policy,
                    received_at=1,
                )
                self.assertEqual(
                    normalized.flow.signal_context,
                    "market_liquidity_context",
                )


class ArkhamPersistenceTests(unittest.TestCase):
    def test_duplicate_rest_and_ws_style_ids_are_idempotent(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            store = OnchainStore(settings)
            store.migrate()
            event = normalize_arkham_transfer(
                transfer_payload(),
                token_policy=ConfiguredTokenPolicy(settings),
                received_at=1,
            )
            first = store.persist_arkham_page(
                [event],
                stream_name="inflow",
                cursor_timestamp_ms=event.timestamp_ms,
                last_event_id=event.transfer.event_id,
                last_success_at=1,
            )
            ws_style = replace(
                event,
                raw=replace(event.raw, received_via="ws"),
            )
            second = store.persist_arkham_page(
                [ws_style],
                stream_name="inflow",
                cursor_timestamp_ms=event.timestamp_ms,
                last_event_id=event.transfer.event_id,
                last_success_at=2,
            )
            self.assertEqual(first, (1, 0))
            self.assertEqual(second, (0, 1))
            counts = store.table_counts()
            self.assertEqual(counts["arkham_raw_events"], 1)
            self.assertEqual(
                counts["arkham_raw_event_versions"], 1
            )
            self.assertEqual(counts["transfer_events"], 1)
            self.assertEqual(counts["flow_events"], 1)

    def test_mutable_enrichment_is_versioned_without_conflict(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            store = OnchainStore(settings)
            store.migrate()
            policy = ConfiguredTokenPolicy(settings)
            original_payload = transfer_payload()
            original = normalize_arkham_transfer(
                original_payload,
                token_policy=policy,
                received_at=1,
            )
            store.persist_arkham_page(
                [original],
                stream_name="inflow",
                cursor_timestamp_ms=original.timestamp_ms,
                last_event_id=original.transfer.event_id,
                last_success_at=1,
            )
            store.persist_arkham_page(
                [original],
                stream_name="inflow",
                cursor_timestamp_ms=original.timestamp_ms,
                last_event_id=original.transfer.event_id,
                last_success_at=2,
            )
            self.assertEqual(
                store.table_counts()["arkham_raw_event_versions"], 1
            )

            label_payload = transfer_payload()
            label_payload["toAddress"]["arkhamLabel"] = {
                "name": "Binance Hot Wallet"
            }
            label_update = normalize_arkham_transfer(
                label_payload,
                token_policy=policy,
                received_at=3,
            )
            self.assertEqual(
                original.raw.immutable_fingerprint,
                label_update.raw.immutable_fingerprint,
            )
            store.persist_arkham_page(
                [label_update],
                stream_name="inflow",
                cursor_timestamp_ms=label_update.timestamp_ms,
                last_event_id=label_update.transfer.event_id,
                last_success_at=3,
            )

            entity_payload = transfer_payload()
            entity_payload["fromAddress"]["arkhamEntity"] = {
                "id": "new-fund",
                "name": "New Fund",
                "type": "fund",
            }
            entity_update = normalize_arkham_transfer(
                entity_payload,
                token_policy=policy,
                received_at=4,
            )
            store.persist_arkham_page(
                [entity_update],
                stream_name="inflow",
                cursor_timestamp_ms=entity_update.timestamp_ms,
                last_event_id=entity_update.transfer.event_id,
                last_success_at=4,
            )
            self.assertEqual(
                store.table_counts()["arkham_raw_event_versions"], 3
            )
            with closing(store._connect()) as conn:
                snapshot = conn.execute(
                    """
                    SELECT entity_id, entity_name
                    FROM entity_snapshots
                    WHERE chain='base' AND address='0xfrom'
                    """
                ).fetchone()
            self.assertEqual(snapshot["entity_id"], "new-fund")
            self.assertEqual(snapshot["entity_name"], "New Fund")

    def test_immutable_transfer_conflict_fails_closed(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            store = OnchainStore(settings)
            store.migrate()
            policy = ConfiguredTokenPolicy(settings)
            original = normalize_arkham_transfer(
                transfer_payload(),
                token_policy=policy,
                received_at=1,
            )
            store.persist_arkham_page(
                [original],
                stream_name="inflow",
                cursor_timestamp_ms=original.timestamp_ms,
                last_event_id=original.transfer.event_id,
                last_success_at=1,
            )
            conflict_payload = transfer_payload()
            conflict_payload["unitValue"] = 3
            conflict = normalize_arkham_transfer(
                conflict_payload,
                token_policy=policy,
                received_at=2,
            )
            with self.assertRaises(sqlite3.IntegrityError):
                store.persist_arkham_page(
                    [conflict],
                    stream_name="inflow",
                    cursor_timestamp_ms=conflict.timestamp_ms + 60_000,
                    last_event_id=conflict.transfer.event_id,
                    last_success_at=2,
                )
            self.assertEqual(
                store.table_counts()["arkham_raw_event_versions"], 1
            )
            state = store.arkham_sync_state("inflow")
            self.assertEqual(
                state.last_timestamp_ms, original.timestamp_ms
            )

    def test_migration_four_recovers_after_interruption(self) -> None:
        conn = sqlite3.connect(":memory:")

        def interrupt(version: int, index: int) -> None:
            if version == 4 and index == 2:
                raise RuntimeError("interrupted")

        with self.assertRaises(RuntimeError):
            apply_migrations(conn, after_statement=interrupt)
        applied = {
            row[0]
            for row in conn.execute(
                "SELECT version FROM schema_migrations"
            ).fetchall()
        }
        self.assertEqual(applied, {1, 2, 3})
        apply_migrations(conn)
        applied = {
            row[0]
            for row in conn.execute(
                "SELECT version FROM schema_migrations"
            ).fetchall()
        }
        self.assertEqual(applied, {1, 2, 3, 4})
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        self.assertTrue(
            {
                "arkham_raw_events",
                "arkham_raw_event_versions",
                "arkham_sync_state",
                "entity_snapshots",
            }.issubset(tables)
        )
        conn.close()
