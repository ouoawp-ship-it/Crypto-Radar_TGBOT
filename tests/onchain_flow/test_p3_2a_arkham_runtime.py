from __future__ import annotations

import json
import unittest
from contextlib import closing
from dataclasses import replace
from decimal import Decimal
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from paopao_radar.onchain_flow.cli import main as onchain_cli
from paopao_radar.onchain_flow.arkham_runtime import ArkhamRestRuntime
from paopao_radar.onchain_flow.db import OnchainStore
from paopao_radar.onchain_flow.formatter import format_alert
from paopao_radar.telegram import PushResult

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
            safe_upper = (NOW * 1000) - (60 * 1000)
            expected_time = safe_upper - (3600 * 1000)
            self.assertEqual(
                client.calls[0]["timeGte"], str(expected_time)
            )
            self.assertTrue(
                all(
                    call["timeLte"] == str(safe_upper)
                    for call in client.calls
                )
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
                safe_upper,
            )
            self.assertGreaterEqual(
                result["telegram_dry_run_count"], 1
            )

            client.calls.clear()
            second = runtime.process_once()
            self.assertEqual(
                client.calls[0]["timeGte"],
                str(safe_upper - (180 * 1000)),
            )
            self.assertGreater(second["duplicate_events"], 0)

    def test_malformed_item_is_quarantined_without_poisoning_page(
        self,
    ) -> None:
        good = event("good")
        bad = event("bad")
        bad.pop("id")
        bad.pop("blockTimestamp")

        def handler(params):
            return ([good, bad], 2) if "to" in params else ([], 0)

        with TemporaryDirectory() as tmp:
            settings = arkham_settings(Path(tmp))
            runtime = ArkhamRestRuntime(
                settings,
                client=FakeArkhamClient(handler),
                clock=lambda: NOW,
            )
            result = runtime.process_once()
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["unique_inserted_events"], 1)
            self.assertEqual(result["rejected_schema_events"], 1)
            store = OnchainStore(settings)
            state = store.arkham_sync_state(
                "arkham_cex_inflow"
            )
            self.assertIsNotNone(state)
            self.assertEqual(
                state.last_timestamp_ms,
                (NOW * 1000) - (60 * 1000),
            )
            self.assertEqual(state.status, "ok")
            with closing(store._connect()) as conn:
                rows = conn.execute(
                    """
                    SELECT arkham_transfer_id, processed_status
                    FROM arkham_raw_events
                    ORDER BY arkham_transfer_id
                    """
                ).fetchall()
            statuses = {
                row["arkham_transfer_id"]: row["processed_status"]
                for row in rows
            }
            self.assertEqual(statuses["good"], "processed")
            rejected_ids = [
                transfer_id
                for transfer_id, status in statuses.items()
                if status == "rejected_schema"
            ]
            self.assertEqual(len(rejected_ids), 1)
            self.assertTrue(rejected_ids[0].startswith("invalid:"))
            rejection_id = rejected_ids[0]

            restarted = ArkhamRestRuntime(
                settings,
                client=FakeArkhamClient(handler),
                clock=lambda: NOW + 300,
            ).process_once()
            self.assertEqual(restarted["status"], "ok")
            self.assertEqual(
                store.table_counts()["arkham_raw_event_versions"], 2
            )
            with closing(store._connect()) as conn:
                retried = conn.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM arkham_raw_event_versions
                    WHERE arkham_transfer_id=?
                    """,
                    (rejection_id,),
                ).fetchone()
            self.assertEqual(retried["count"], 1)

    def test_unknown_token_and_bad_numeric_timestamp_are_isolated(
        self,
    ) -> None:
        unknown = event("unknown-token")
        unknown["tokenAddress"] = ""
        unknown["tokenId"] = ""
        valid = event("valid-sibling")
        malformed = event("bad-time")
        malformed["blockTimestamp"] = float("inf")

        def handler(params):
            return (
                ([unknown, valid, malformed], 3)
                if "to" in params
                else ([], 0)
            )

        with TemporaryDirectory() as tmp:
            settings = arkham_settings(
                Path(tmp), arkham_rest_limit=3
            )
            result = ArkhamRestRuntime(
                settings,
                client=FakeArkhamClient(handler),
                clock=lambda: NOW,
            ).process_once()
            self.assertEqual(result["unique_inserted_events"], 2)
            self.assertEqual(result["rejected_schema_events"], 1)
            store = OnchainStore(settings)
            with closing(store._connect()) as conn:
                unknown_row = conn.execute(
                    """
                    SELECT token_address, token_policy
                    FROM flow_events WHERE event_id='arkham:unknown-token'
                    """
                ).fetchone()
                rejected = conn.execute(
                    """
                    SELECT processed_status
                    FROM arkham_raw_events
                    WHERE arkham_transfer_id='bad-time'
                    """
                ).fetchone()
            self.assertTrue(
                unknown_row["token_address"].startswith(
                    "arkham-token-unknown:"
                )
            )
            self.assertEqual(unknown_row["token_policy"], "unknown")
            self.assertEqual(rejected["processed_status"], "rejected_schema")
            self.assertEqual(store.table_counts()["alerts"], 1)

    def test_processed_event_is_not_downgraded_by_later_rejection(
        self,
    ) -> None:
        good = event("same-id")
        malformed = dict(good)
        malformed.pop("blockTimestamp")
        calls = 0

        def handler(params):
            nonlocal calls
            if "from" in params:
                return [], 0
            calls += 1
            return ([good], 1) if calls == 1 else ([malformed], 1)

        with TemporaryDirectory() as tmp:
            settings = arkham_settings(Path(tmp))
            clock_now = [NOW]
            runtime = ArkhamRestRuntime(
                settings,
                client=FakeArkhamClient(handler),
                clock=lambda: clock_now[0],
            )
            runtime.process_once()
            clock_now[0] += 300
            runtime.process_once()
            store = OnchainStore(settings)
            with closing(store._connect()) as conn:
                row = conn.execute(
                    """
                    SELECT processed_status
                    FROM arkham_raw_events
                    WHERE arkham_transfer_id='same-id'
                    """
                ).fetchone()
                versions = conn.execute(
                    """
                    SELECT processed_status
                    FROM arkham_raw_event_versions
                    WHERE arkham_transfer_id='same-id'
                    ORDER BY processed_status
                    """
                ).fetchall()
            self.assertEqual(row["processed_status"], "processed")
            self.assertEqual(
                {version["processed_status"] for version in versions},
                {"processed", "rejected_schema"},
            )

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

    def test_frozen_time_window_prevents_pagination_drift(self) -> None:
        initial = [event("page-1"), event("page-2"), event("page-3")]
        later = event("arrived-during-pagination")
        inbound_calls = 0

        def handler(params):
            nonlocal inbound_calls
            if "from" in params:
                return [], 0
            inbound_calls += 1
            if inbound_calls == 1:
                return initial[:2], 3
            # A new record exists outside the frozen timeLte and therefore
            # must not change this cycle's count or offset window.
            self.assertNotEqual(later["id"], initial[2]["id"])
            return initial[2:], 3

        with TemporaryDirectory() as tmp:
            client = FakeArkhamClient(handler)
            result = ArkhamRestRuntime(
                arkham_settings(Path(tmp)),
                client=client,
                clock=lambda: NOW,
            ).process_once()
            self.assertEqual(result["status"], "ok")
            inbound = [call for call in client.calls if "to" in call]
            self.assertEqual(len(inbound), 2)
            frozen_fields = (
                "timeGte",
                "timeLte",
                "sortKey",
                "sortDir",
                "to",
            )
            for field in frozen_fields:
                self.assertEqual(inbound[0][field], inbound[1][field])
            self.assertEqual(
                result["unique_inserted_events"], len(initial)
            )

    def test_max_page_exhaustion_reports_and_recovers_backlog(
        self,
    ) -> None:
        records = [event(f"backlog-{index}") for index in range(5)]

        def handler(params):
            if "from" in params:
                return [], 0
            offset = int(params["offset"])
            limit = int(params["limit"])
            return records[offset : offset + limit], len(records)

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = arkham_settings(
                root, arkham_rest_max_pages=1
            )
            clock_now = [NOW]
            runtime = ArkhamRestRuntime(
                settings,
                client=FakeArkhamClient(handler),
                clock=lambda: clock_now[0],
            )
            partial = runtime.process_once()
            self.assertEqual(partial["status"], "partial_backlog")
            self.assertEqual(partial["streams_completed"], 1)
            self.assertEqual(partial["streams_partial_backlog"], 1)
            self.assertEqual(partial["backlog_remaining"], 3)
            state = OnchainStore(settings).arkham_sync_state(
                "arkham_cex_inflow"
            )
            self.assertEqual(state.status, "partial_backlog")
            self.assertEqual(state.backlog_remaining, 3)
            self.assertEqual(state.next_offset, 2)
            frozen_upper = state.window_upper_ms
            frozen_lower = state.window_lower_ms
            self.assertNotEqual(
                state.last_timestamp_ms, partial["query_upper_ms"]
            )

            clock_now[0] += 300
            second = runtime.process_once()
            self.assertEqual(second["status"], "partial_backlog")
            state = OnchainStore(settings).arkham_sync_state(
                "arkham_cex_inflow"
            )
            self.assertEqual(state.next_offset, 4)
            self.assertEqual(state.window_upper_ms, frozen_upper)
            self.assertEqual(state.window_lower_ms, frozen_lower)

            clock_now[0] += 300
            recovered = runtime.process_once()
            self.assertEqual(recovered["status"], "ok")
            self.assertEqual(recovered["streams_completed"], 2)
            state = OnchainStore(settings).arkham_sync_state(
                "arkham_cex_inflow"
            )
            self.assertEqual(state.status, "ok")
            self.assertEqual(state.backlog_remaining, 0)
            self.assertEqual(state.last_timestamp_ms, frozen_upper)
            self.assertEqual(state.next_offset, 0)
            self.assertEqual(state.window_lower_ms, 0)
            self.assertEqual(state.window_upper_ms, 0)
            inbound = [
                call
                for call in runtime._client_instance.calls
                if "to" in call
            ]
            self.assertEqual(
                [call["offset"] for call in inbound], [0, 2, 4]
            )
            self.assertEqual(
                {call["timeGte"] for call in inbound},
                {str(frozen_lower)},
            )
            self.assertEqual(
                {call["timeLte"] for call in inbound},
                {str(frozen_upper)},
            )
            self.assertEqual(
                OnchainStore(settings).table_counts()[
                    "arkham_raw_events"
                ],
                5,
            )

    def test_cli_returns_attention_exit_for_partial_backlog(self) -> None:
        with TemporaryDirectory() as tmp, patch.object(
            ArkhamRestRuntime,
            "process_once",
            return_value={"status": "partial_backlog"},
        ), patch("sys.stdout", new_callable=StringIO):
            self.assertEqual(
                onchain_cli(
                    ["arkham-once"],
                    settings=arkham_settings(Path(tmp)),
                ),
                2,
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

    def test_mixed_priced_and_unpriced_window_keeps_priced_flow(
        self,
    ) -> None:
        payloads = [
            event("priced", historical_usd=2_000_000),
            event("unpriced-mixed", historical_usd=None),
        ]

        def handler(params):
            return (payloads, 2) if "to" in params else ([], 0)

        with TemporaryDirectory() as tmp:
            settings = arkham_settings(Path(tmp))
            result = ArkhamRestRuntime(
                settings,
                client=FakeArkhamClient(handler),
                clock=lambda: NOW,
            ).process_once()
            self.assertEqual(result["unpriced_events"], 1)
            self.assertEqual(result["excluded_unpriced_count"], 1)
            self.assertGreaterEqual(result["alerts_generated"], 1)
            store = OnchainStore(settings)
            with closing(store._connect()) as conn:
                snapshots = conn.execute(
                    """
                    SELECT gross_inflow_usd, inflow_tx_count,
                           excluded_unpriced_count
                    FROM flow_window_snapshots
                    ORDER BY duration_sec
                    """
                ).fetchall()
                unpriced = conn.execute(
                    """
                    SELECT processed_status
                    FROM arkham_raw_events
                    WHERE arkham_transfer_id='unpriced-mixed'
                    """
                ).fetchone()
            self.assertEqual(len(snapshots), 2)
            for snapshot in snapshots:
                self.assertEqual(
                    Decimal(snapshot["gross_inflow_usd"]),
                    Decimal("2000000"),
                )
                self.assertEqual(snapshot["inflow_tx_count"], 1)
                self.assertEqual(
                    snapshot["excluded_unpriced_count"], 1
                )
            self.assertEqual(
                unpriced["processed_status"], "unpriced"
            )

    def test_all_unpriced_window_creates_no_snapshot_or_alert(
        self,
    ) -> None:
        payload = event("only-unpriced", historical_usd=None)

        def handler(params):
            return ([payload], 1) if "to" in params else ([], 0)

        with TemporaryDirectory() as tmp:
            settings = arkham_settings(Path(tmp))
            result = ArkhamRestRuntime(
                settings,
                client=FakeArkhamClient(handler),
                clock=lambda: NOW,
            ).process_once()
            self.assertEqual(result["alerts_generated"], 0)
            counts = OnchainStore(settings).table_counts()
            self.assertEqual(counts["flow_window_snapshots"], 0)
            self.assertEqual(counts["alerts"], 0)

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
            self.assertIn("市场流动性背景", message)
            self.assertIn("不预测稳定币自身价格", message)
            self.assertIn("评分不是概率", message)
            self.assertIn("arkham 实体归因为概率性情报", message)
            self.assertNotIn("必涨", message)
            self.assertNotIn("必跌", message)

            rolling_message = format_alert(
                replace(
                    stable_alert,
                    duration_sec=900,
                    gross_inflow_usd=Decimal("2500000"),
                    gross_outflow_usd=Decimal("500000"),
                    net_flow_usd=Decimal("2000000"),
                    inflow_tx_count=2,
                    outflow_tx_count=1,
                    excluded_unpriced_count=1,
                )
            )
            self.assertIn("15 分钟滚动信号", rolling_message)
            self.assertIn("总流入：$2.50M", rolling_message)
            self.assertIn("总流出：$500.00K", rolling_message)
            self.assertIn("净流量（流入-流出）：+$2.00M", rolling_message)
            self.assertIn("无价格事件：1 条", rolling_message)

    def test_delivery_cooldown_audits_second_evaluation(self) -> None:
        payload = event("cooldown")

        def handler(params):
            return ([payload], 1) if "to" in params else ([], 0)

        with TemporaryDirectory() as tmp:
            settings = arkham_settings(
                Path(tmp), alert_cooldown_sec=3600
            )
            clock_now = [NOW]
            runtime = ArkhamRestRuntime(
                settings,
                client=FakeArkhamClient(handler),
                clock=lambda: clock_now[0],
            )
            first = runtime.process_once()
            self.assertEqual(first["telegram_dry_run_count"], 1)
            self.assertEqual(first["cooldown_suppressed"], 0)

            clock_now[0] += 300
            second = runtime.process_once()
            self.assertEqual(second["telegram_dry_run_count"], 0)
            self.assertEqual(second["cooldown_suppressed"], 0)
            store = OnchainStore(settings)
            with closing(store._connect()) as conn:
                statuses = [
                    row["status"]
                    for row in conn.execute(
                        """
                        SELECT status
                        FROM alert_deliveries
                        ORDER BY created_at, alert_key
                        """
                    ).fetchall()
                ]
            self.assertEqual(statuses, ["dry_run"])
            self.assertEqual(store.table_counts()["alerts"], 1)

    def test_distinct_transfer_fact_is_audited_inside_cooldown(
        self,
    ) -> None:
        payloads = [event("distinct-1"), event("distinct-2")]

        def handler(params):
            return (payloads, 2) if "to" in params else ([], 0)

        with TemporaryDirectory() as tmp:
            settings = arkham_settings(
                Path(tmp), alert_cooldown_sec=3600
            )
            result = ArkhamRestRuntime(
                settings,
                client=FakeArkhamClient(handler),
                clock=lambda: NOW,
            ).process_once()
            self.assertEqual(result["telegram_dry_run_count"], 1)
            self.assertEqual(result["cooldown_suppressed"], 1)
            store = OnchainStore(settings)
            self.assertEqual(store.table_counts()["alerts"], 2)
            with closing(store._connect()) as conn:
                statuses = {
                    row["status"]
                    for row in conn.execute(
                        "SELECT status FROM alert_deliveries"
                    ).fetchall()
                }
            self.assertEqual(
                statuses, {"dry_run", "cooldown_suppressed"}
            )

    def test_mutable_attribution_direction_change_supersedes_fact(
        self,
    ) -> None:
        first = event("direction-revision")
        from_address = first["fromAddress"]["address"]
        to_address = first["toAddress"]["address"]
        revised = dict(first)
        revised["fromAddress"] = party(
            from_address, "binance", "cex", "Binance"
        )
        revised["toAddress"] = party(
            to_address, "fund", "fund", "Fund"
        )
        inbound_calls = [0]

        def handler(params):
            if "from" in params:
                return [], 0
            inbound_calls[0] += 1
            return (
                ([first], 1)
                if inbound_calls[0] == 1
                else ([revised], 1)
            )

        with TemporaryDirectory() as tmp:
            settings = arkham_settings(
                Path(tmp), alert_cooldown_sec=3600
            )
            clock_now = [NOW]
            runtime = ArkhamRestRuntime(
                settings,
                client=FakeArkhamClient(handler),
                clock=lambda: clock_now[0],
            )
            runtime.process_once()
            clock_now[0] += 300
            runtime.process_once()
            with closing(OnchainStore(settings)._connect()) as conn:
                rows = conn.execute(
                    """
                    SELECT direction, status FROM alerts
                    WHERE alert_key LIKE
                          'arkham:arkham:direction-revision:single:%'
                    ORDER BY direction
                    """
                ).fetchall()
            self.assertEqual(len(rows), 2)
            self.assertEqual(
                {row["direction"]: row["status"] for row in rows},
                {"inflow": "superseded", "outflow": "active"},
            )

    def test_severity_escalation_and_direction_reversal_bypass_cooldown(
        self,
    ) -> None:
        payload = event("tier-change")

        def handler(params):
            return ([payload], 1) if "to" in params else ([], 0)

        with TemporaryDirectory() as tmp:
            settings = arkham_settings(
                Path(tmp), alert_cooldown_sec=3600
            )
            runtime = ArkhamRestRuntime(
                settings,
                client=FakeArkhamClient(handler),
                clock=lambda: NOW,
            )
            runtime.process_once()
            store = OnchainStore(settings)
            base = store.active_alerts()[0]

            escalated = replace(
                base,
                alert_key="arkham:severity-escalation",
                score=-75,
                confidence="high",
                notification_key="",
            )
            escalated = replace(
                escalated,
                notification_key=runtime._notification_key(escalated),
            )
            store.persist_alert_for_delivery(
                escalated, created_at=NOW + 1
            )
            dry_run, failed, suppressed = runtime._deliver(store)
            self.assertEqual((dry_run, failed, suppressed), (1, 0, 0))

            reversed_alert = replace(
                base,
                alert_key="arkham:direction-reversal",
                direction="outflow",
                score=55,
                gross_inflow_usd=None,
                gross_outflow_usd=base.total_usd,
                notification_key="",
            )
            reversed_alert = replace(
                reversed_alert,
                notification_key=runtime._notification_key(
                    reversed_alert
                ),
            )
            store.persist_alert_for_delivery(
                reversed_alert, created_at=NOW + 2
            )
            dry_run, failed, suppressed = runtime._deliver(store)
            self.assertEqual((dry_run, failed, suppressed), (1, 0, 0))

    def test_failed_delivery_does_not_abort_later_delivery(self) -> None:
        payload = event("delivery-seed")

        def handler(params):
            return ([payload], 1) if "to" in params else ([], 0)

        with TemporaryDirectory() as tmp:
            settings = arkham_settings(
                Path(tmp), alert_cooldown_sec=3600
            )
            runtime = ArkhamRestRuntime(
                settings,
                client=FakeArkhamClient(handler),
                clock=lambda: NOW,
            )
            runtime.process_once()
            store = OnchainStore(settings)
            base = store.active_alerts()[0]
            first = replace(
                base,
                alert_key="arkham:delivery-failure",
                token_address="arkham-token:failure",
                notification_key="arkham:failure",
            )
            second = replace(
                base,
                alert_key="arkham:delivery-success",
                token_address="arkham-token:success",
                notification_key="arkham:success",
            )
            store.persist_alert_for_delivery(first, created_at=NOW + 1)
            store.persist_alert_for_delivery(second, created_at=NOW + 2)
            with patch(
                "paopao_radar.onchain_flow.arkham_runtime."
                "OnchainNotifier.notify",
                side_effect=[
                    RuntimeError("delivery failed"),
                    PushResult(
                        status="dry_run",
                        reason="send flag disabled",
                        sent=False,
                    ),
                ],
            ) as notify:
                dry_run, failed, suppressed = runtime._deliver(store)
            self.assertEqual(notify.call_count, 2)
            self.assertEqual((dry_run, failed, suppressed), (1, 1, 0))

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

    def test_arkham_status_exit_codes_follow_stream_health(self) -> None:
        cases = (
            ("not_initialized", None, 0),
            ("ok", "ok", 0),
            ("partial_backlog", "partial_backlog", 2),
            ("failed", "failed", 1),
        )
        for expected, stream_status, exit_code in cases:
            with self.subTest(status=expected), TemporaryDirectory() as tmp:
                settings = arkham_settings(Path(tmp))
                if stream_status is not None:
                    store = OnchainStore(settings)
                    store.migrate()
                    store.mark_arkham_stream_status(
                        "arkham_cex_inflow",
                        status=stream_status,
                        query_upper_ms=123,
                        backlog_remaining=(
                            7 if stream_status == "partial_backlog" else 0
                        ),
                        window_lower_ms=100,
                        window_upper_ms=200,
                        next_offset=4,
                    )
                with patch(
                    "sys.stdout", new_callable=StringIO
                ) as output:
                    result = onchain_cli(
                        ["arkham-status"], settings=settings
                    )
                payload = json.loads(output.getvalue())
                self.assertEqual(result, exit_code)
                self.assertEqual(payload["status"], expected)

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
