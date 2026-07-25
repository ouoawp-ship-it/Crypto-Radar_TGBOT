from __future__ import annotations

import sqlite3
import unittest
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

    def test_internal_cross_cex_and_missing_entity(self) -> None:
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

        missing = self.normalize(
            transfer_payload(
                transfer_id="ark-missing",
                from_party=party(
                    "0xfrom", None, None, label="Possible fund"
                ),
            )
        )
        self.assertEqual(missing.flow.flow_type, "unidentified")
        self.assertEqual(
            missing.flow.attribution_quality, "arkham_label"
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
            self.assertEqual(counts["transfer_events"], 1)
            self.assertEqual(counts["flow_events"], 1)

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
                "arkham_sync_state",
                "entity_snapshots",
            }.issubset(tables)
        )
        conn.close()
