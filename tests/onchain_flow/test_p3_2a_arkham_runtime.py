from __future__ import annotations

import unittest
from dataclasses import replace
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from paopao_radar.onchain_flow.cli import main as onchain_cli
from paopao_radar.onchain_flow.arkham_runtime import (
    ArkhamPageProcessingError,
    ArkhamRestRuntime,
)
from paopao_radar.onchain_flow.db import OnchainStore
from paopao_radar.onchain_flow.formatter import format_alert

from .support import make_settings
from .test_p3_2a_arkham_model import party, transfer_payload


NOW = 1_784_937_900


class FakeArkhamClient:
    def __init__(self, handler=None):
        self.handler = handler
        self.calls = []

    def transfers(self, params):
        self.calls.append(dict(params))
        if self.handler is None:
            raise AssertionError("unexpected Arkham network call")
        return self.handler(dict(params))

    def capability_check(self, **_kwargs):
        self.calls.append({"capability": True})
        return {"authenticated": True, "type_cex_rest_supported": True}


def arkham_settings(root: Path, **overrides):
    settings = replace(
        make_settings(root),
        enable=True,
        real_send=False,
        source_mode="arkham",
        arkham_enable=True,
        arkham_api_key="private-key",
        arkham_rest_enable=True,
        arkham_rest_limit=2,
        arkham_rest_max_pages=3,
        arkham_rest_overlap_sec=180,
        single_large_floor_usd=1_000_000,
        alert_cooldown_sec=0,
    )
    return replace(settings, **overrides)


def event(
    transfer_id: str,
    *,
    timestamp: str = "2026-07-25T00:00:00Z",
    direction: str = "inflow",
    historical_usd=2_000_000,
    token_id="test-token",
):
    if direction == "inflow":
        from_party = party("0xfrom" + transfer_id, "fund", "fund", "Fund")
        to_party = party(
            "0xto" + transfer_id, "binance", "cex", "Binance"
        )
    else:
        from_party = party(
            "0xfrom" + transfer_id, "binance", "cex", "Binance"
        )
        to_party = party("0xto" + transfer_id, "fund", "fund", "Fund")
    payload = transfer_payload(
        transfer_id=transfer_id,
        from_party=from_party,
        to_party=to_party,
        historical_usd=historical_usd,
        token_id=token_id,
    )
    payload["blockTimestamp"] = timestamp
    payload["transactionHash"] = "0x" + transfer_id
    return payload


class ArkhamRuntimeTests(unittest.TestCase):
    def test_disabled_mode_has_zero_side_effects(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = replace(
                arkham_settings(root), arkham_enable=False
            )
            client = FakeArkhamClient()
            runtime = ArkhamRestRuntime(settings, client=client)
            result = runtime.process_once()
            check = runtime.capability_check()
            self.assertEqual(result["status"], "disabled")
            self.assertEqual(check["status"], "disabled")
            self.assertEqual(client.calls, [])
            self.assertFalse(settings.db_path.exists())
            self.assertFalse(settings.data_dir.exists())

    def test_sequential_queries_pagination_and_cursor_overlap(self) -> None:
        inbound = [event("in-1"), event("in-2"), event("in-3")]
        outbound = [event("out-1", direction="outflow")]

        def handler(params):
            if "to" in params:
                offset = int(params["offset"])
                return inbound[offset : offset + 2], len(inbound)
            return outbound, len(outbound)

        with TemporaryDirectory() as tmp:
            settings = arkham_settings(Path(tmp))
            client = FakeArkhamClient(handler)
            runtime = ArkhamRestRuntime(
                settings, client=client, clock=lambda: NOW
            )
            result = runtime.process_once()
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["streams_completed"], 2)
            self.assertEqual(result["pages_processed"], 3)
            self.assertEqual(result["transfers_received"], 4)
            self.assertEqual(result["unique_inserted_events"], 4)
            self.assertEqual(result["real_telegram_requests"], 0)
            self.assertEqual(result["websocket_sessions_created"], 0)
            self.assertIn("to", client.calls[0])
            self.assertIn("to", client.calls[1])
            self.assertIn("from", client.calls[2])
            self.assertEqual(client.calls[0]["to"], "type:cex")
            self.assertEqual(client.calls[2]["from"], "type:cex")
            self.assertEqual(client.calls[0]["sortKey"], "time")
            self.assertEqual(client.calls[0]["sortDir"], "asc")
            self.assertEqual(client.calls[0]["offset"], 0)
            self.assertEqual(client.calls[1]["offset"], 2)
            expected_time = (NOW * 1000) - (180 * 1000)
            self.assertEqual(
                client.calls[0]["timeGte"], str(expected_time)
            )
            store = OnchainStore(settings)
            counts = store.table_counts()
            self.assertEqual(counts["arkham_raw_events"], 4)
            self.assertEqual(counts["transfer_events"], 4)
            self.assertEqual(counts["entity_snapshots"], 8)
            self.assertEqual(
                store.arkham_sync_state(
                    "arkham_cex_inflow"
                ).last_timestamp_ms,
                NOW * 1000,
            )
            self.assertGreaterEqual(
                result["telegram_dry_run_count"], 1
            )

            client.calls.clear()
            second = runtime.process_once()
            self.assertEqual(
                client.calls[0]["timeGte"], str(expected_time)
            )
            self.assertGreater(second["duplicate_events"], 0)

    def test_failed_page_does_not_advance_cursor(self) -> None:
        good = event("good")
        bad = event("bad")
        bad.pop("blockTimestamp")

        def handler(params):
            if int(params["offset"]) == 0:
                return [good, event("good-2")], 3
            return [bad], 3

        with TemporaryDirectory() as tmp:
            settings = arkham_settings(Path(tmp))
            runtime = ArkhamRestRuntime(
                settings,
                client=FakeArkhamClient(handler),
                clock=lambda: NOW,
            )
            with self.assertRaises(ArkhamPageProcessingError):
                runtime.process_once()
            state = OnchainStore(settings).arkham_sync_state(
                "arkham_cex_inflow"
            )
            self.assertIsNotNone(state)
            self.assertEqual(state.last_timestamp_ms, NOW * 1000)
            self.assertEqual(state.status, "failed")

    def test_entity_id_filter_is_explicit(self) -> None:
        def handler(params):
            return [], 0

        with TemporaryDirectory() as tmp:
            settings = arkham_settings(
                Path(tmp),
                arkham_cex_filter_mode="entity_ids",
                arkham_cex_entity_ids=("binance", "coinbase"),
            )
            client = FakeArkhamClient(handler)
            ArkhamRestRuntime(
                settings, client=client, clock=lambda: NOW
            ).process_once()
            self.assertEqual(
                client.calls[0]["to"], "binance,coinbase"
            )
            self.assertEqual(
                client.calls[1]["from"], "binance,coinbase"
            )

    def test_unpriced_and_wrapped_events_do_not_alert(self) -> None:
        payloads = [
            event("unpriced", historical_usd=None),
            event("wrapped", token_id="wrapped-eth"),
        ]

        def handler(params):
            return (payloads, len(payloads)) if "to" in params else ([], 0)

        with TemporaryDirectory() as tmp:
            settings = arkham_settings(
                Path(tmp),
                wrapped_or_receipt_token_ids=("wrapped-eth",),
            )
            result = ArkhamRestRuntime(
                settings,
                client=FakeArkhamClient(handler),
                clock=lambda: NOW,
            ).process_once()
            self.assertEqual(result["unpriced_events"], 1)
            self.assertEqual(result["policy_suppressed_events"], 1)
            self.assertEqual(result["alerts_generated"], 0)
            self.assertEqual(
                OnchainStore(settings).table_counts()["alerts"], 0
            )

    def test_stablecoin_alert_is_neutral_liquidity_context(self) -> None:
        stable = event("stable", token_id="usd-coin")

        def handler(params):
            return ([stable], 1) if "to" in params else ([], 0)

        with TemporaryDirectory() as tmp:
            settings = arkham_settings(
                Path(tmp), stablecoin_token_ids=("usd-coin",)
            )
            runtime = ArkhamRestRuntime(
                settings,
                client=FakeArkhamClient(handler),
                clock=lambda: NOW,
            )
            result = runtime.process_once()
            self.assertGreaterEqual(result["alerts_generated"], 1)
            alerts = OnchainStore(settings).active_alerts()
            stable_alert = next(
                alert
                for alert in alerts
                if alert.token_policy == "stablecoin"
            )
            self.assertEqual(stable_alert.score, 0)
            self.assertEqual(
                stable_alert.signal_context,
                "market_liquidity_context",
            )
            message = format_alert(stable_alert).lower()
            self.assertIn("market liquidity context", message)
            self.assertIn("does not predict", message)
            self.assertNotIn("label confidence", message)

    def test_diagnostics_never_expose_api_key(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = arkham_settings(Path(tmp))
            diagnostic = settings.diagnostic()
            self.assertTrue(
                diagnostic["arkham"]["api_key_configured"]
            )
            self.assertNotIn("private-key", str(diagnostic))

    def test_cli_disabled_commands_have_zero_side_effects(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = replace(
                arkham_settings(Path(tmp)),
                enable=False,
                arkham_enable=False,
            )
            for command in (
                "arkham-check",
                "arkham-once",
                "arkham-status",
            ):
                with self.subTest(command=command), patch(
                    "sys.stdout", new_callable=StringIO
                ) as output:
                    self.assertEqual(
                        onchain_cli([command], settings=settings), 0
                    )
                    self.assertNotIn("private-key", output.getvalue())
            self.assertFalse(settings.db_path.exists())

    def test_arkham_doctor_does_not_require_base_files_or_rpc(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = replace(
                arkham_settings(root),
                labels_path=root / "missing-labels.csv",
                chains_path=root / "missing-chains.json",
                base_enable=False,
                base_http_rpc_url="",
                base_wss_rpc_url="",
            )
            with patch(
                "sys.stdout", new_callable=StringIO
            ) as output:
                self.assertEqual(
                    onchain_cli(["doctor"], settings=settings), 0
                )
                result = output.getvalue()
            self.assertIn('"status": "not_required"', result)
            self.assertNotIn("private-key", result)
