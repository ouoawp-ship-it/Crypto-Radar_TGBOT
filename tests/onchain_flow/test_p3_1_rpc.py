from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

import requests

from paopao_radar.onchain_flow.collectors.base import BlockRange
from paopao_radar.onchain_flow.collectors.evm_http import (
    AdaptiveRangeError,
    BaseHttpCollector,
    FinalizedRangeConsistencyError,
    JsonRpcClient,
    LogValidationError,
    RpcAuthError,
    RpcRateLimitError,
    RpcRangeError,
    RpcResponseError,
    RpcServiceError,
    RpcTimeoutError,
    build_transfer_filters,
    normalize_transfer_log,
    pad_topic_address,
)
from paopao_radar.onchain_flow.collectors.evm_ws import (
    WssError,
    WssHeadTrigger,
)
from paopao_radar.onchain_flow.constants import TRANSFER_TOPIC

from .support import make_settings


CEX = "0x1111111111111111111111111111111111111111"
OUTSIDE = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
TOKEN = "0x9999999999999999999999999999999999999999"


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class ScriptedSession:
    def __init__(self, actions):
        self.actions = list(actions)
        self.requests = []

    def post(self, _url, *, json, timeout, headers):
        self.requests.append(
            {"json": json, "timeout": timeout, "headers": headers}
        )
        action = self.actions.pop(0)
        if isinstance(action, Exception):
            raise action
        result = action(json) if callable(action) else action
        return FakeResponse(result)


def rpc_result(result):
    return lambda request: {
        "jsonrpc": "2.0",
        "id": request["id"],
        "result": result,
    }


def transfer_log():
    return {
        "address": TOKEN,
        "topics": [
            TRANSFER_TOPIC,
            pad_topic_address(OUTSIDE),
            pad_topic_address(CEX),
        ],
        "data": "0x" + f"{123:064x}",
        "blockNumber": "0x64",
        "blockHash": "0x" + ("ab" * 32),
        "transactionHash": "0x" + ("cd" * 32),
        "logIndex": "0x2",
        "removed": False,
    }


class JsonRpcTests(unittest.TestCase):
    def test_request_ids_increment_and_timeout_is_finite(self) -> None:
        session = ScriptedSession([rpc_result("0x2105"), rpc_result("0x64")])
        client = JsonRpcClient(
            "https://example.invalid/private",
            timeout_sec=7,
            retry=1,
            backoff_sec=0,
            session=session,
            sleep=lambda _seconds: None,
        )
        self.assertEqual(client.chain_id(), 8453)
        self.assertEqual(client.block_number(), 100)
        self.assertEqual(
            [item["json"]["id"] for item in session.requests], [1, 2]
        )
        self.assertEqual(
            [item["timeout"] for item in session.requests], [7.0, 7.0]
        )

    def test_timeout_retries_with_bounded_backoff(self) -> None:
        sleeps = []
        session = ScriptedSession(
            [
                requests.Timeout("secret endpoint"),
                rpc_result("0x2105"),
            ]
        )
        client = JsonRpcClient(
            "https://example.invalid/key",
            timeout_sec=1,
            retry=2,
            backoff_sec=0.5,
            session=session,
            sleep=sleeps.append,
        )
        self.assertEqual(client.chain_id(), 8453)
        self.assertEqual(sleeps, [0.5])
        self.assertEqual(len(session.requests), 2)

    def test_malformed_and_error_responses_are_structured_and_redacted(self) -> None:
        session = ScriptedSession(
            [
                lambda request: {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "error": {"code": -32000, "message": "boom"},
                }
            ]
        )
        client = JsonRpcClient(
            "https://user:secret@example.invalid/key",
            timeout_sec=1,
            retry=1,
            backoff_sec=0,
            session=session,
        )
        with self.assertRaises(RpcResponseError) as raised:
            client.chain_id()
        self.assertNotIn("secret", str(raised.exception))
        self.assertNotIn("example.invalid", str(raised.exception))

    def test_provider_check_rejects_chain_mismatch(self) -> None:
        class Client:
            def chain_id(self):
                return 1

        with TemporaryDirectory() as tmp:
            settings = replace(
                make_settings(Path(tmp)),
                base_http_rpc_url="https://example.invalid",
            )
            with self.assertRaises(RpcResponseError):
                BaseHttpCollector(Client(), settings).provider_check()

    def test_provider_check_reads_finalized_target_and_block(self) -> None:
        session = ScriptedSession(
            [
                rpc_result("0x2105"),
                rpc_result("0x64"),
                rpc_result(
                    {"hash": "0x" + ("ab" * 32)}
                ),
            ]
        )
        with TemporaryDirectory() as tmp:
            settings = replace(
                make_settings(Path(tmp)),
                base_http_rpc_url=(
                    "https://user:secret@example.invalid/private-key"
                ),
            )
            client = JsonRpcClient(
                settings.base_http_rpc_url,
                timeout_sec=1,
                retry=1,
                backoff_sec=0,
                session=session,
            )
            result = BaseHttpCollector(client, settings).provider_check()
        self.assertEqual(result["chain_id"], 8453)
        self.assertEqual(result["latest_head"], 100)
        self.assertEqual(result["target_finalized"], 80)
        self.assertEqual(result["block_lookup"], "ok")
        self.assertNotIn("secret", str(result))

    def test_auth_and_rate_limit_are_structured_without_split_semantics(self) -> None:
        class StatusSession:
            def __init__(self, status):
                self.status = status
                self.calls = 0

            def post(self, *_args, **_kwargs):
                self.calls += 1
                return FakeResponse({}, self.status)

        auth_session = StatusSession(401)
        auth = JsonRpcClient(
            "https://example.invalid",
            timeout_sec=1,
            retry=3,
            backoff_sec=0,
            session=auth_session,
        )
        with self.assertRaises(RpcAuthError):
            auth.block_number()
        self.assertEqual(auth_session.calls, 1)

        rate_session = StatusSession(429)
        rate = JsonRpcClient(
            "https://example.invalid",
            timeout_sec=1,
            retry=3,
            backoff_sec=0,
            session=rate_session,
            sleep=lambda _seconds: None,
        )
        with self.assertRaises(RpcRateLimitError):
            rate.block_number()
        self.assertEqual(rate_session.calls, 3)


class TransferCollectionTests(unittest.TestCase):
    def test_filters_batch_addresses_and_never_set_token_address(self) -> None:
        filters = build_transfer_filters(
            [CEX, "0x2222222222222222222222222222222222222222"],
            batch_size=1,
        )
        self.assertEqual(len(filters), 4)
        for item in filters:
            payload = item.as_rpc(BlockRange(10, 20))
            self.assertNotIn("address", payload)
            self.assertEqual(payload["topics"][0], TRANSFER_TOPIC)
        self.assertIsNone(filters[0].topics[2])
        self.assertIsNone(filters[1].topics[1])
        self.assertEqual(len(pad_topic_address(CEX)), 66)

    def test_inbound_outbound_duplicates_are_merged(self) -> None:
        class Client:
            def get_logs(self, _filter):
                return [transfer_log()]

        with TemporaryDirectory() as tmp:
            settings = replace(
                make_settings(Path(tmp)),
                rpc_topic_address_batch=50,
                rpc_max_block_range=1000,
            )
            logs = BaseHttpCollector(Client(), settings).fetch_cex_logs(
                100, 100, [CEX]
            )
        self.assertEqual(logs, [transfer_log()])

    def test_adaptive_range_recursively_splits(self) -> None:
        class Client:
            def __init__(self):
                self.ranges = []

            def get_logs(self, payload):
                start = int(payload["fromBlock"], 16)
                end = int(payload["toBlock"], 16)
                self.ranges.append((start, end))
                if end - start + 1 > 2:
                    raise RpcRangeError("range too large")
                return []

        with TemporaryDirectory() as tmp:
            settings = replace(
                make_settings(Path(tmp)),
                rpc_max_block_range=8,
                rpc_min_block_range=1,
            )
            client = Client()
            BaseHttpCollector(client, settings).fetch_cex_logs(
                1, 8, [CEX]
            )
        self.assertIn((1, 8), client.ranges)
        self.assertIn((1, 2), client.ranges)
        self.assertIn((7, 8), client.ranges)

    def test_minimum_range_failure_is_not_silently_skipped(self) -> None:
        class Client:
            def get_logs(self, _payload):
                raise RpcRangeError("still too large")

        with TemporaryDirectory() as tmp:
            settings = replace(
                make_settings(Path(tmp)),
                rpc_max_block_range=2,
                rpc_min_block_range=1,
            )
            with self.assertRaises(AdaptiveRangeError):
                BaseHttpCollector(Client(), settings).fetch_cex_logs(
                    1, 2, [CEX]
                )

    def test_generic_outage_and_timeout_budget_have_bounded_call_counts(self) -> None:
        class ServiceClient:
            def __init__(self):
                self.calls = 0

            def get_logs(self, _payload):
                self.calls += 1
                raise RpcServiceError("provider unavailable")

        class TimeoutClient:
            def __init__(self):
                self.calls = 0

            def get_logs(self, _payload):
                self.calls += 1
                raise RpcTimeoutError("timeout")

        with TemporaryDirectory() as tmp:
            base = replace(
                make_settings(Path(tmp)),
                rpc_max_block_range=64,
                rpc_min_block_range=1,
                rpc_adaptive_max_requests=5,
                rpc_adaptive_max_depth=12,
            )
            service = ServiceClient()
            with self.assertRaises(RpcServiceError):
                BaseHttpCollector(service, base).fetch_cex_logs(
                    1, 64, [CEX]
                )
            self.assertEqual(service.calls, 1)

            timeout = TimeoutClient()
            with self.assertRaises(AdaptiveRangeError):
                BaseHttpCollector(timeout, base).fetch_cex_logs(
                    1, 64, [CEX]
                )
            self.assertEqual(timeout.calls, 5)

    def test_conflicting_duplicate_event_key_fails_closed(self) -> None:
        first = transfer_log()
        second = dict(first)
        second["data"] = "0x" + f"{456:064x}"

        class Client:
            def __init__(self):
                self.calls = 0

            def get_logs(self, _payload):
                self.calls += 1
                return [first] if self.calls == 1 else [second]

        with TemporaryDirectory() as tmp:
            with self.assertRaises(FinalizedRangeConsistencyError):
                BaseHttpCollector(
                    Client(), make_settings(Path(tmp))
                ).fetch_cex_logs(100, 100, [CEX])

    def test_transfer_log_is_strictly_decoded(self) -> None:
        transfer = normalize_transfer_log(
            transfer_log(), block_time=1700000000
        )
        self.assertEqual(transfer.amount_raw, 123)
        self.assertEqual(transfer.from_address, OUTSIDE)
        self.assertEqual(transfer.to_address, CEX)
        self.assertEqual(
            transfer.event_id,
            f"8453:{transfer_log()['transactionHash']}:2",
        )
        malformed = transfer_log()
        malformed["topics"] = [TRANSFER_TOPIC, "0x1234", pad_topic_address(CEX)]
        with self.assertRaises(LogValidationError):
            normalize_transfer_log(malformed, block_time=1700000000)
        removed_log = transfer_log()
        removed_log["removed"] = True
        removed = normalize_transfer_log(
            removed_log, block_time=1700000000
        )
        self.assertTrue(removed.removed)
        self.assertEqual(removed.confirmation_status, "orphaned")


class WssTriggerTests(unittest.TestCase):
    def test_new_heads_subscription_is_only_a_trigger(self) -> None:
        class Connection:
            def __init__(self):
                self.sent = []
                self.responses = [
                    json.dumps(
                        {"jsonrpc": "2.0", "id": 1, "result": "sub-1"}
                    ),
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "method": "eth_subscription",
                            "params": {
                                "subscription": "sub-1",
                                "result": {"number": "0x64"},
                            },
                        }
                    ),
                ]
                self.closed = False

            def send(self, payload):
                self.sent.append(json.loads(payload))

            def recv(self):
                return self.responses.pop(0)

            def close(self):
                self.closed = True

        connection = Connection()
        with TemporaryDirectory() as tmp:
            settings = replace(
                make_settings(Path(tmp)),
                base_wss_rpc_url="wss://example.invalid/private",
            )
            trigger = WssHeadTrigger(
                settings,
                connection_factory=lambda *_args, **_kwargs: connection,
            )
            trigger.connect()
            head = trigger.receive_head()
            trigger.close()
        self.assertEqual(head, 100)
        self.assertEqual(
            connection.sent[0]["params"], ["newHeads"]
        )
        self.assertTrue(connection.closed)

    def test_unconfigured_wss_fails_structurally(self) -> None:
        with TemporaryDirectory() as tmp:
            trigger = WssHeadTrigger(make_settings(Path(tmp)))
            with self.assertRaises(WssError):
                trigger.connect()


if __name__ == "__main__":
    unittest.main()
