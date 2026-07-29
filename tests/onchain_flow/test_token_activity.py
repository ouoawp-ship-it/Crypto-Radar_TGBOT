from __future__ import annotations

import json
import unittest
from collections import Counter
from contextlib import redirect_stdout
from dataclasses import replace
from decimal import Decimal
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from paopao_radar.onchain_flow.cli import build_parser, main
from paopao_radar.onchain_flow.collectors.base import BlockRange
from paopao_radar.onchain_flow.collectors.evm_http import (
    BaseHttpCollector,
    JsonRpcClient,
    LogValidationError,
    RpcRangeError,
    RpcRateLimitError,
    RpcRequestBudgetError,
    RpcTimeoutError,
    build_token_transfer_filter,
    build_transfer_filters,
    pad_topic_address,
)
from paopao_radar.onchain_flow.constants import TRANSFER_TOPIC, ZERO_ADDRESS
from paopao_radar.onchain_flow.models import PriceQuote
from paopao_radar.onchain_flow.token_activity import (
    TokenActivityQuery,
    TokenActivityQueryError,
    TokenActivityQueryService,
)
from paopao_radar.onchain_flow.token_metadata import (
    DECIMALS_SELECTOR,
    NAME_SELECTOR,
    SYMBOL_SELECTOR,
    TOTAL_SUPPLY_SELECTOR,
    TokenMetadataResolver,
)

from .support import make_settings


TOKEN = "0x9999999999999999999999999999999999999999"
OTHER_TOKEN = "0x8888888888888888888888888888888888888888"
WALLET_A = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
WALLET_B = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
WALLET_C = "0xcccccccccccccccccccccccccccccccccccccccc"
WALLET_D = "0xdddddddddddddddddddddddddddddddddddddddd"
CEX_A_HOT = "0x1111111111111111111111111111111111111111"
CEX_A_DEPOSIT = "0x2222222222222222222222222222222222222222"
CEX_A_COLLECTOR = "0x3333333333333333333333333333333333333333"
CEX_B_HOT = "0x4444444444444444444444444444444444444444"
GENESIS_TIME = 1_700_000_000
HEAD = 2000
CONFIRMATIONS = 20
FINALIZED = HEAD - CONFIRMATIONS


def block_hash(number: int) -> str:
    return "0x" + f"{number:064x}"


def tx_hash(number: int) -> str:
    return "0x" + f"{100_000 + number:064x}"


def uint256(value: int) -> str:
    return "0x" + f"{value:064x}"


def bytes32_text(value: str) -> str:
    return "0x" + value.encode().ljust(32, b"\x00").hex()


def abi_text(value: str) -> str:
    raw = value.encode()
    padded = raw + b"\x00" * ((32 - len(raw) % 32) % 32)
    return "0x" + (
        (32).to_bytes(32, "big")
        + len(raw).to_bytes(32, "big")
        + padded
    ).hex()


def transfer_log(
    block: int,
    index: int,
    from_address: str,
    to_address: str,
    *,
    amount: int = 1_000_000,
    token: str = TOKEN,
) -> dict[str, object]:
    return {
        "address": token,
        "topics": [
            TRANSFER_TOPIC,
            pad_topic_address(from_address),
            pad_topic_address(to_address),
        ],
        "data": uint256(amount),
        "blockNumber": hex(block),
        "blockHash": block_hash(block),
        "transactionHash": tx_hash(block * 100 + index),
        "logIndex": hex(index),
        "removed": False,
    }


def indexed_value_log(block: int, index: int) -> dict[str, object]:
    item = transfer_log(block, index, WALLET_A, WALLET_B)
    item["topics"] = [*item["topics"], uint256(7)]
    item["data"] = "0x"
    return item


class FakeRpc:
    def __init__(
        self,
        logs: list[dict[str, object]] | None = None,
        *,
        chain_id: int = 8453,
        head: int = HEAD,
        range_limit: int | None = None,
        log_error: Exception | None = None,
        log_error_after: int | None = None,
        ignore_log_range: bool = False,
        fail_after: int | None = None,
        code: str = "0x6000",
        decimals: int = 6,
    ):
        self.logs = list(logs or [])
        self.chain = chain_id
        self.head = head
        self.range_limit = range_limit
        self.log_error = log_error
        self.log_error_after = log_error_after
        self.ignore_log_range = ignore_log_range
        self.fail_after = fail_after
        self.code = code
        self.decimals = decimals
        self.request_count = 0
        self.log_filters: list[dict[str, object]] = []
        self.log_call_count = 0
        self.block_calls: list[int] = []

    def _hit(self) -> None:
        if (
            self.fail_after is not None
            and self.request_count >= self.fail_after
        ):
            raise RpcRequestBudgetError("budget")
        self.request_count += 1

    def chain_id(self) -> int:
        self._hit()
        return self.chain

    def block_number(self) -> int:
        self._hit()
        return self.head

    def get_block(self, number: int) -> dict[str, object]:
        self._hit()
        self.block_calls.append(number)
        return {
            "number": hex(number),
            "hash": block_hash(number),
            "timestamp": hex(GENESIS_TIME + number * 60),
        }

    def get_code(self, _address: str) -> str:
        self._hit()
        return self.code

    def eth_call(self, _address: str, selector: str) -> str:
        self._hit()
        if selector == DECIMALS_SELECTOR:
            return uint256(self.decimals)
        if selector == TOTAL_SUPPLY_SELECTOR:
            return uint256(10**30)
        if selector == SYMBOL_SELECTOR:
            return bytes32_text("TST")
        if selector == NAME_SELECTOR:
            return abi_text("Test Token")
        raise AssertionError(selector)

    def get_logs(self, payload: dict[str, object]) -> list[dict[str, object]]:
        self._hit()
        self.log_filters.append(payload)
        self.log_call_count += 1
        if self.log_error is not None and (
            self.log_error_after is None
            or self.log_call_count > self.log_error_after
        ):
            raise self.log_error
        start = int(str(payload["fromBlock"]), 16)
        end = int(str(payload["toBlock"]), 16)
        if self.range_limit is not None and end - start + 1 > self.range_limit:
            raise RpcRangeError("range too large")
        if self.ignore_log_range:
            return list(self.logs)
        return [
            item
            for item in self.logs
            if start <= int(str(item["blockNumber"]), 16) <= end
        ]


class StaticProvider:
    def __init__(self, quote: PriceQuote | None):
        self.quote = quote
        self.calls = 0

    def quote_many(
        self, _chain_id: int, addresses: list[str]
    ) -> dict[str, PriceQuote]:
        self.calls += 1
        return {addresses[0]: self.quote} if self.quote is not None else {}


LABEL_HEADER = (
    "chain_id,address,entity_name,entity_type,address_type,source,"
    "confidence,valid_from,valid_to\n"
)


def write_label_rows(path: Path, rows: list[str]) -> None:
    path.write_text(LABEL_HEADER + "".join(rows), encoding="utf-8")


def write_labels(path: Path) -> None:
    write_label_rows(
        path,
        [
            f"8453,{CEX_A_HOT},Binance,cex,hot,reviewed,0.99,,\n",
            (
                f"8453,{CEX_A_DEPOSIT},Binance,cex,deposit,"
                "reviewed,0.99,,\n"
            ),
            (
                f"8453,{CEX_A_COLLECTOR},Binance,cex,collector,"
                "reviewed,0.99,,\n"
            ),
            f"8453,{CEX_B_HOT},OKX,cex,hot,reviewed,0.98,,\n",
            f"8453,{WALLET_C},Fund A,wallet,wallet,reviewed,0.90,,\n",
            f"8453,{WALLET_D},Fund B,wallet,wallet,reviewed,0.90,,\n",
        ],
    )


class TokenActivityTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.labels = self.root / "labels.csv"
        write_labels(self.labels)
        self.settings = replace(
            make_settings(self.root),
            labels_path=self.labels,
            base_http_rpc_url="https://user:secret@example.invalid/private",
            base_confirmation_depth=CONFIRMATIONS,
            rpc_max_block_range=10_000,
            rpc_min_block_range=1,
            token_activity_max_events=100,
            token_activity_max_rpc_requests=128,
            token_activity_max_unique_block_headers=100,
            token_activity_top_n=20,
            token_activity_block_search_max_calls=32,
        )

    def query(
        self,
        *,
        window: str = "15m",
        max_events: int | None = None,
        max_rpc_requests: int | None = None,
        top: int | None = None,
        with_price: bool = False,
        min_usd: str | None = None,
        settings=None,
    ) -> TokenActivityQuery:
        return TokenActivityQuery.create(
            settings or self.settings,
            chain="base",
            contract=TOKEN,
            window=window,
            max_events=max_events,
            max_rpc_requests=max_rpc_requests,
            top_n=top,
            with_price=with_price,
            min_usd=min_usd,
        )

    def run_query(
        self,
        logs: list[dict[str, object]],
        *,
        rpc: FakeRpc | None = None,
        query: TokenActivityQuery | None = None,
        settings=None,
        provider=None,
    ) -> tuple[dict[str, object], FakeRpc]:
        rpc = rpc or FakeRpc(logs)
        result = TokenActivityQueryService(
            settings or self.settings,
            rpc,
            price_provider=provider,
            clock=lambda: 100.0,
        ).execute(query or self.query(settings=settings))
        return result, rpc


class CliAndValidationTests(TokenActivityTestCase):
    def run_output_command(
        self,
        output_path: Path,
        *,
        payload: dict[str, object] | None = None,
    ) -> tuple[int, dict[str, object]]:
        result = payload or {
            "schema_version": 1,
            "status": "ok",
            "complete": True,
            "truncated": False,
            "truncation_reason": None,
            "summary": {"transfer_count": 2},
            "transfers": [{"event_id": "one"}, {"event_id": "two"}],
        }

        class Service:
            def execute(self, _query):
                return result

        output = StringIO()
        with (
            patch.object(
                TokenActivityQueryService,
                "from_settings",
                return_value=Service(),
            ),
            redirect_stdout(output),
        ):
            code = main(
                [
                    "token-activity",
                    "--chain",
                    "base",
                    "--contract",
                    TOKEN,
                    "--window",
                    "15m",
                    "--allow-network",
                    "--output-file",
                    str(output_path),
                ],
                settings=self.settings,
            )
        return code, json.loads(output.getvalue())

    def test_cli_registers_token_activity_without_send_flags(self) -> None:
        args = build_parser().parse_args(
            [
                "token-activity",
                "--chain",
                "base",
                "--contract",
                TOKEN,
                "--window",
                "15m",
            ]
        )
        self.assertEqual(args.command, "token-activity")
        self.assertFalse(hasattr(args, "send"))
        self.assertFalse(hasattr(args, "confirm_real_send"))

    def test_missing_allow_network_makes_zero_network_calls(self) -> None:
        output = StringIO()
        with (
            patch.object(
                TokenActivityQueryService,
                "from_settings",
            ) as service_factory,
            redirect_stdout(output),
        ):
            code = main(
                [
                    "token-activity",
                    "--chain",
                    "base",
                    "--contract",
                    TOKEN,
                    "--window",
                    "15m",
                ],
                settings=self.settings,
            )
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"], "allow_network_required")
        self.assertFalse(payload["network_activity"])
        self.assertFalse(payload["database_writes"])
        self.assertFalse(payload["telegram_calls"])
        service_factory.assert_not_called()

    def test_non_base_chain_is_structurally_rejected(self) -> None:
        with self.assertRaisesRegex(TokenActivityQueryError, "only supports Base"):
            TokenActivityQuery.create(
                self.settings,
                chain="ethereum",
                contract=TOKEN,
                window="15m",
                max_events=None,
                max_rpc_requests=None,
                top_n=None,
                with_price=False,
                min_usd=None,
            )

    def test_invalid_contract_is_rejected_before_network(self) -> None:
        with self.assertRaises(TokenActivityQueryError) as raised:
            TokenActivityQuery.create(
                self.settings,
                chain="base",
                contract="0x1234",
                window="15m",
                max_events=None,
                max_rpc_requests=None,
                top_n=None,
                with_price=False,
                min_usd=None,
            )
        self.assertEqual(raised.exception.code, "invalid_contract")

    def test_wrong_rpc_chain_id_is_rejected(self) -> None:
        with self.assertRaises(TokenActivityQueryError) as raised:
            TokenActivityQueryService(
                self.settings, FakeRpc(chain_id=1)
            ).execute(self.query())
        self.assertEqual(raised.exception.code, "wrong_chain")

    def test_non_contract_and_invalid_decimals_fail_closed(self) -> None:
        for rpc, expected in (
            (FakeRpc(code="0x"), "token_not_contract"),
            (FakeRpc(decimals=37), "invalid_decimals"),
        ):
            with self.subTest(expected=expected):
                with self.assertRaises(TokenActivityQueryError) as raised:
                    TokenActivityQueryService(
                        self.settings, rpc
                    ).execute(self.query())
                self.assertEqual(raised.exception.code, expected)

    def test_invalid_query_budgets_and_min_usd_are_rejected(self) -> None:
        cases = (
            {"max_events": 0},
            {"max_events": 101},
            {"max_rpc_requests": 129},
            {"top": 21},
            {"with_price": False, "min_usd": "10"},
            {"with_price": True, "min_usd": "NaN"},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                with self.assertRaises(TokenActivityQueryError):
                    self.query(**overrides)

    def test_query_budget_defaults_and_hard_caps_are_validated(self) -> None:
        defaults = make_settings(self.root)
        self.assertEqual(defaults.token_activity_max_window_hours, 24)
        self.assertEqual(defaults.token_activity_max_events, 5000)
        self.assertEqual(defaults.token_activity_max_rpc_requests, 256)
        self.assertEqual(
            defaults.token_activity_max_unique_block_headers, 2000
        )
        self.assertEqual(defaults.token_activity_top_n, 50)
        self.assertEqual(defaults.token_activity_block_search_max_calls, 32)
        for field, value in (
            ("token_activity_max_window_hours", 25),
            ("token_activity_max_events", 5001),
            ("token_activity_max_rpc_requests", 257),
            ("token_activity_max_unique_block_headers", 2001),
            ("token_activity_top_n", 101),
            ("token_activity_block_search_max_calls", 33),
        ):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    replace(defaults, **{field: value}).validate()

    def test_output_file_writes_full_json_and_stdout_summary(self) -> None:
        output_path = self.root / "reports" / "onchain" / "result.json"
        output_path.parent.mkdir(parents=True)
        fake_payload = {
            "schema_version": 1,
            "status": "ok",
            "complete": True,
            "truncated": False,
            "truncation_reason": None,
            "summary": {"transfer_count": 2},
            "transfers": [{"event_id": "one"}, {"event_id": "two"}],
        }
        code, summary = self.run_output_command(
            output_path, payload=fake_payload
        )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output_path.read_text()), fake_payload)
        self.assertEqual(summary["transfer_count"], 2)
        self.assertEqual(summary["output_file"], str(output_path))
        self.assertFalse(list(output_path.parent.glob(".*.tmp")))

    def test_output_file_cannot_overwrite_business_state(self) -> None:
        protected_paths = (
            self.settings.base_dir / "data" / "query-result.json",
            self.settings.base_dir / ".env.onchain",
            self.settings.base_dir / ".env.oi",
            self.settings.base_dir / "root-result.json",
            self.settings.base_dir / "paopao_radar" / "new.py",
            self.settings.base_dir / "tests" / "new.py",
            self.settings.base_dir / "scripts" / "new.py",
            self.settings.base_dir / "config" / "new.json",
            self.settings.base_dir / "docs" / "new.md",
            self.settings.base_dir / ".github" / "new.yml",
            self.settings.base_dir / "migrations" / "new.sql",
        )
        for protected_path in protected_paths:
            with self.subTest(path=protected_path):
                output = StringIO()
                with (
                    patch.object(
                        TokenActivityQueryService,
                        "from_settings",
                    ) as service_factory,
                    redirect_stdout(output),
                ):
                    code = main(
                        [
                            "token-activity",
                            "--chain",
                            "base",
                            "--contract",
                            TOKEN,
                            "--window",
                            "15m",
                            "--allow-network",
                            "--output-file",
                            str(protected_path),
                        ],
                        settings=self.settings,
                    )
                self.assertEqual(code, 1)
                self.assertEqual(
                    json.loads(output.getvalue())["error"],
                    "unsafe_output_file",
                )
                service_factory.assert_not_called()

    def test_existing_files_are_never_overwritten(self) -> None:
        existing_paths = (
            self.root / "existing.json",
            self.root / "README.md",
            self.root / "paopao_radar" / "module.py",
            self.settings.db_path,
        )
        for index, path in enumerate(existing_paths):
            with self.subTest(path=path):
                path.parent.mkdir(parents=True, exist_ok=True)
                original = f"original-{index}".encode()
                path.write_bytes(original)
                with patch.object(
                    TokenActivityQueryService, "from_settings"
                ) as service_factory:
                    code, payload = self.run_output_command(path)
                self.assertEqual(code, 1)
                self.assertEqual(payload["error"], "output_file_exists")
                self.assertEqual(path.read_bytes(), original)
                service_factory.assert_not_called()

    def test_symbolic_link_output_is_rejected(self) -> None:
        target = self.root / "outside.json"
        target.write_bytes(b"original")
        link = self.root / "reports" / "onchain" / "link.json"
        link.parent.mkdir(parents=True)
        try:
            link.symlink_to(target)
        except OSError as exc:
            self.skipTest(f"symbolic links are unavailable: {exc}")
        code, payload = self.run_output_command(link)
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"], "unsafe_output_file")
        self.assertEqual(target.read_bytes(), b"original")

    def test_symbolic_link_parent_cannot_escape_repository_allowlist(
        self,
    ) -> None:
        protected = self.root / "docs"
        protected.mkdir()
        reports = self.root / "reports"
        reports.mkdir()
        allowed_link = reports / "onchain"
        try:
            allowed_link.symlink_to(protected, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symbolic links are unavailable: {exc}")
        output_path = allowed_link / "escaped.json"
        code, payload = self.run_output_command(output_path)
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"], "unsafe_output_file")
        self.assertFalse((protected / "escaped.json").exists())

    def test_new_external_output_file_is_allowed(self) -> None:
        with TemporaryDirectory() as external:
            output_path = Path(external) / "result.json"
            code, summary = self.run_output_command(output_path)
            self.assertEqual(code, 0)
            self.assertEqual(summary["output_file"], str(output_path))
            self.assertTrue(output_path.exists())

    def test_finalization_race_does_not_overwrite_or_leave_temp_file(
        self,
    ) -> None:
        output_path = self.root / "reports" / "onchain" / "race.json"
        output_path.parent.mkdir(parents=True)

        def competing_writer(_source, destination):
            Path(destination).write_bytes(b"competitor")
            raise FileExistsError("simulated race")

        with patch(
            "paopao_radar.onchain_flow.cli.os.link",
            side_effect=competing_writer,
        ):
            code, payload = self.run_output_command(output_path)
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"], "output_file_exists")
        self.assertEqual(output_path.read_bytes(), b"competitor")
        self.assertFalse(list(output_path.parent.glob(".*.tmp")))

    def test_output_write_failure_leaves_no_target_or_temp_file(self) -> None:
        output_path = self.root / "reports" / "onchain" / "failed.json"
        output_path.parent.mkdir(parents=True)
        with patch(
            "paopao_radar.onchain_flow.cli.os.link",
            side_effect=OSError("simulated failure"),
        ):
            code, payload = self.run_output_command(output_path)
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"], "output_write_failed")
        self.assertFalse(output_path.exists())
        self.assertFalse(list(output_path.parent.glob(".*.tmp")))


class FilterAndCollectionTests(TokenActivityTestCase):
    def test_token_filter_sets_contract_and_only_transfer_topic(self) -> None:
        payload = build_token_transfer_filter(TOKEN).as_rpc(BlockRange(10, 20))
        self.assertEqual(payload["address"], TOKEN)
        self.assertEqual(payload["topics"], [TRANSFER_TOPIC])

    def test_existing_cex_filters_never_gain_contract_address(self) -> None:
        filters = build_transfer_filters([CEX_A_HOT], 50)
        self.assertEqual(len(filters), 2)
        for item in filters:
            payload = item.as_rpc(BlockRange(1, 2))
            self.assertNotIn("address", payload)
            self.assertEqual(payload["topics"][0], TRANSFER_TOPIC)

    def test_provider_log_before_or_after_requested_range_is_rejected(
        self,
    ) -> None:
        collector = BaseHttpCollector(
            FakeRpc(ignore_log_range=True), self.settings
        )
        for block in (99, 111):
            with self.subTest(block=block):
                collector.client.logs = [
                    transfer_log(block, 0, WALLET_A, WALLET_B)
                ]
                with self.assertRaises(LogValidationError):
                    collector.fetch_token_logs(
                        100, 110, TOKEN, max_events=10
                    )

    def test_provider_log_must_belong_to_current_adaptive_segment(
        self,
    ) -> None:
        settings = replace(self.settings, rpc_max_block_range=5)
        rpc = FakeRpc(
            [transfer_log(108, 0, WALLET_A, WALLET_B)],
            ignore_log_range=True,
        )
        collector = BaseHttpCollector(rpc, settings)
        with self.assertRaises(LogValidationError):
            collector.fetch_token_logs(100, 110, TOKEN, max_events=10)
        self.assertEqual(
            (rpc.log_filters[0]["fromBlock"], rpc.log_filters[0]["toBlock"]),
            (hex(100), hex(104)),
        )
        self.assertEqual(len(rpc.log_filters), 1)

    def test_requested_segment_boundaries_are_accepted(self) -> None:
        logs = [
            transfer_log(100, 0, WALLET_A, WALLET_B),
            transfer_log(110, 0, WALLET_A, WALLET_B),
        ]
        rpc = FakeRpc(logs, ignore_log_range=True)
        settings = replace(self.settings, rpc_max_block_range=11)
        result = BaseHttpCollector(rpc, settings).fetch_token_logs(
            100, 110, TOKEN, max_events=10
        )
        self.assertEqual(
            [int(str(item["blockNumber"]), 16) for item in result.logs],
            [100, 110],
        )

    def test_out_of_segment_log_stops_before_header_or_price_lookup(
        self,
    ) -> None:
        out_of_segment_block = FINALIZED - 1
        rpc = FakeRpc(
            [
                transfer_log(
                    out_of_segment_block, 0, WALLET_A, WALLET_B
                )
            ],
            ignore_log_range=True,
        )
        settings = replace(self.settings, rpc_max_block_range=5)
        provider = StaticProvider(
            PriceQuote(
                chain_id=8453,
                token_address=TOKEN,
                price_usd=Decimal("2.5"),
                volume_24h_usd=None,
                source="static",
                observed_at=GENESIS_TIME,
            )
        )
        with self.assertRaises(TokenActivityQueryError) as raised:
            self.run_query(
                rpc.logs,
                rpc=rpc,
                settings=settings,
                query=self.query(settings=settings, with_price=True),
                provider=provider,
            )
        self.assertEqual(raised.exception.code, "malformed_log")
        self.assertNotIn(out_of_segment_block, rpc.block_calls)
        self.assertEqual(provider.calls, 0)

    def test_adaptive_range_is_reused_for_token_queries(self) -> None:
        log = transfer_log(FINALIZED - 1, 0, WALLET_A, WALLET_B)
        settings = replace(
            self.settings,
            rpc_max_block_range=1000,
            rpc_min_block_range=1,
        )
        rpc = FakeRpc([log], range_limit=4)
        result, _ = self.run_query(
            [log],
            rpc=rpc,
            settings=settings,
            query=self.query(settings=settings),
        )
        self.assertGreater(result["diagnostics"]["adaptive_split_count"], 0)
        self.assertTrue(result["complete"])

    def test_different_token_log_is_rejected(self) -> None:
        wrong = transfer_log(
            FINALIZED - 1,
            0,
            WALLET_A,
            WALLET_B,
            token=OTHER_TOKEN,
        )
        with self.assertRaises(TokenActivityQueryError) as raised:
            self.run_query([wrong])
        self.assertEqual(raised.exception.code, "malformed_log")

    def test_duplicate_event_is_idempotent_and_counted(self) -> None:
        item = transfer_log(FINALIZED - 1, 0, WALLET_A, WALLET_B)
        result, _ = self.run_query([item, dict(item)])
        self.assertEqual(result["summary"]["transfer_count"], 1)
        self.assertEqual(result["diagnostics"]["duplicate_log_count"], 1)

    def test_equivalent_hex_log_indices_use_one_canonical_identity(self) -> None:
        item = transfer_log(FINALIZED - 1, 0, WALLET_A, WALLET_B)
        equivalent = dict(item)
        equivalent["logIndex"] = "0x00"
        result, _ = self.run_query([item, equivalent])
        self.assertEqual(result["summary"]["transfer_count"], 1)
        self.assertEqual(result["diagnostics"]["duplicate_log_count"], 1)

    def test_results_are_sorted_by_block_log_and_hash(self) -> None:
        logs = [
            transfer_log(FINALIZED - 1, 3, WALLET_A, WALLET_B),
            transfer_log(FINALIZED - 2, 2, WALLET_A, WALLET_B),
            transfer_log(FINALIZED - 1, 1, WALLET_A, WALLET_B),
        ]
        result, _ = self.run_query(logs)
        keys = [
            (item["block_number"], item["log_index"], item["tx_hash"])
            for item in result["transfers"]
        ]
        self.assertEqual(keys, sorted(keys))

    def test_indexed_value_shape_is_skipped(self) -> None:
        result, _ = self.run_query(
            [indexed_value_log(FINALIZED - 1, 0)]
        )
        self.assertEqual(result["summary"]["transfer_count"], 0)
        self.assertEqual(
            result["diagnostics"]["skipped_indexed_value_count"], 1
        )

    def test_finalized_block_hash_mismatch_fails_closed(self) -> None:
        item = transfer_log(FINALIZED - 1, 0, WALLET_A, WALLET_B)
        item["blockHash"] = "0x" + ("ff" * 32)
        with self.assertRaises(TokenActivityQueryError) as raised:
            self.run_query([item])
        self.assertEqual(raised.exception.code, "malformed_log")


class WindowAndBudgetTests(TokenActivityTestCase):
    def test_all_supported_window_boundaries_use_chain_timestamps(self) -> None:
        for window, seconds in (
            ("15m", 900),
            ("1h", 3600),
            ("4h", 14_400),
            ("24h", 86_400),
        ):
            with self.subTest(window=window):
                boundary = FINALIZED - seconds // 60
                logs = [
                    transfer_log(boundary - 1, 0, WALLET_A, WALLET_B),
                    transfer_log(boundary, 1, WALLET_A, WALLET_B),
                ]
                result, _ = self.run_query(
                    logs, query=self.query(window=window)
                )
                self.assertEqual(result["query"]["from_block"], boundary)
                self.assertEqual(result["summary"]["transfer_count"], 1)
                self.assertEqual(
                    result["transfers"][0]["block_number"], boundary
                )

    def test_binary_search_call_limit_is_enforced(self) -> None:
        settings = replace(
            self.settings,
            token_activity_block_search_max_calls=1,
        )
        with self.assertRaises(TokenActivityQueryError) as raised:
            self.run_query(
                [],
                settings=settings,
                query=self.query(settings=settings),
            )
        self.assertEqual(
            raised.exception.code, "block_search_budget_exhausted"
        )

    def test_block_headers_are_cached_within_one_query(self) -> None:
        logs = [
            transfer_log(FINALIZED - 1, 0, WALLET_A, WALLET_B),
            transfer_log(FINALIZED - 1, 1, WALLET_A, WALLET_B),
        ]
        result, rpc = self.run_query(logs)
        counts = Counter(rpc.block_calls)
        self.assertTrue(all(value == 1 for value in counts.values()))
        self.assertEqual(
            result["diagnostics"]["unique_block_header_count"],
            len(counts),
        )

    def test_max_events_returns_partial_without_claiming_complete(self) -> None:
        settings = replace(self.settings, token_activity_max_events=1)
        logs = [
            transfer_log(FINALIZED - 2, 0, WALLET_A, WALLET_B),
            transfer_log(FINALIZED - 1, 0, WALLET_A, WALLET_B),
        ]
        result, _ = self.run_query(
            logs,
            settings=settings,
            query=self.query(settings=settings),
        )
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["complete"])
        self.assertTrue(result["truncated"])
        self.assertEqual(result["truncation_reason"], "max_events")
        self.assertEqual(result["summary"]["transfer_count"], 1)

    def test_max_rpc_requests_after_some_facts_returns_partial(self) -> None:
        logs = [
            transfer_log(FINALIZED - offset, 0, WALLET_A, WALLET_B)
            for offset in range(1, 8)
        ]
        rpc = FakeRpc(logs, fail_after=25)
        result, _ = self.run_query(logs, rpc=rpc)
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["complete"])
        self.assertEqual(result["truncation_reason"], "max_rpc_requests")
        self.assertGreaterEqual(result["summary"]["transfer_count"], 1)

    def test_max_block_headers_after_some_facts_returns_partial(self) -> None:
        settings = replace(
            self.settings,
            token_activity_max_unique_block_headers=15,
        )
        logs = [
            transfer_log(FINALIZED - offset, 0, WALLET_A, WALLET_B)
            for offset in range(1, 6)
        ]
        result, _ = self.run_query(
            logs,
            settings=settings,
            query=self.query(settings=settings),
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["truncation_reason"], "max_block_headers")
        self.assertFalse(result["complete"])

    def test_rate_limit_and_timeout_before_any_fact_fail_closed(self) -> None:
        for error, reason in (
            (RpcRateLimitError("limited"), "provider_rate_limit"),
            (RpcTimeoutError("timeout"), "provider_timeout"),
        ):
            with self.subTest(reason=reason):
                rpc = FakeRpc(log_error=error)
                with self.assertRaises(TokenActivityQueryError) as raised:
                    self.run_query([], rpc=rpc)
                self.assertEqual(
                    raised.exception.code,
                    "query_budget_exhausted_before_any_result",
                )
                self.assertLessEqual(
                    rpc.request_count,
                    self.settings.token_activity_max_rpc_requests,
                )
                self.assertLessEqual(
                    len(rpc.log_filters),
                    self.settings.rpc_adaptive_max_requests,
                )

    def test_timeout_after_reliable_fact_is_partial(self) -> None:
        settings = replace(self.settings, rpc_max_block_range=100)
        logs = [
            transfer_log(FINALIZED - 200, 0, WALLET_A, WALLET_B),
        ]
        rpc = FakeRpc(
            logs,
            log_error=RpcTimeoutError("timeout"),
            log_error_after=1,
        )
        result, _ = self.run_query(
            logs,
            rpc=rpc,
            settings=settings,
            query=self.query(settings=settings, window="4h"),
        )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["truncation_reason"], "provider_timeout")
        self.assertEqual(result["summary"]["transfer_count"], 1)

    def test_json_rpc_total_request_budget_counts_retry_attempts(self) -> None:
        class Response:
            status_code = 200

            def __init__(self, request):
                self.request = request

            def json(self):
                return {
                    "jsonrpc": "2.0",
                    "id": self.request["id"],
                    "result": "0x2105",
                }

        class Session:
            def __init__(self):
                self.calls = 0

            def post(self, _url, *, json, timeout, headers):
                self.calls += 1
                return Response(json)

        session = Session()
        client = JsonRpcClient(
            "https://example.invalid",
            timeout_sec=1,
            retry=3,
            backoff_sec=0,
            session=session,
            max_requests=1,
        )
        self.assertEqual(client.chain_id(), 8453)
        with self.assertRaises(RpcRequestBudgetError):
            client.chain_id()
        self.assertEqual(session.calls, 1)


class ClassificationAndSchemaTests(TokenActivityTestCase):
    def test_all_supported_flow_types_and_unknown_wallets(self) -> None:
        block = FINALIZED - 1
        logs = [
            transfer_log(block, 0, WALLET_A, WALLET_B),
            transfer_log(block, 1, WALLET_C, WALLET_D),
            transfer_log(block, 2, WALLET_A, CEX_A_HOT),
            transfer_log(block, 3, CEX_A_HOT, WALLET_A),
            transfer_log(block, 4, CEX_A_HOT, CEX_A_DEPOSIT),
            transfer_log(block, 5, CEX_A_DEPOSIT, CEX_A_HOT),
            transfer_log(block, 6, CEX_A_HOT, CEX_B_HOT),
            transfer_log(block, 7, ZERO_ADDRESS, WALLET_A),
            transfer_log(block, 8, WALLET_A, ZERO_ADDRESS),
        ]
        result, _ = self.run_query(logs)
        self.assertEqual(
            [item["flow_type"] for item in result["transfers"]],
            [
                "unclassified",
                "non_cex",
                "inflow",
                "outflow",
                "internal",
                "consolidation",
                "cross_cex",
                "mint",
                "burn",
            ],
        )
        self.assertEqual(
            result["transfers"][0]["from"]["entity_name"], "未知钱包"
        )
        self.assertFalse(result["transfers"][0]["from"]["known"])
        self.assertFalse(
            result["transfers"][0]["from"]["classification_eligible"]
        )
        self.assertTrue(result["transfers"][2]["to"]["known"])
        self.assertTrue(
            result["transfers"][2]["to"]["classification_eligible"]
        )
        for key in (
            "unclassified_count",
            "inflow_count",
            "outflow_count",
            "internal_count",
            "consolidation_count",
            "cross_cex_count",
            "mint_count",
            "burn_count",
            "non_cex_count",
        ):
            self.assertEqual(result["summary"][key], 1)

    def test_missing_labels_degrade_to_unclassified(self) -> None:
        settings = replace(
            self.settings,
            labels_path=self.root / "missing.csv",
        )
        item = transfer_log(
            FINALIZED - 1, 0, WALLET_A, CEX_A_HOT
        )
        result, _ = self.run_query(
            [item],
            settings=settings,
            query=self.query(settings=settings),
        )
        self.assertEqual(result["labels"]["status"], "missing")
        self.assertEqual(
            result["transfers"][0]["flow_type"], "unclassified"
        )
        self.assertFalse(result["transfers"][0]["to"]["known"])
        self.assertEqual(result["summary"]["inflow_count"], 0)
        self.assertEqual(
            result["labels"]["classification_eligible_cex_count"], 0
        )
        self.assertEqual(result["labels"]["identity_label_count"], 0)
        self.assertTrue(result["warnings"])

    def test_missing_labels_preserve_mint_and_burn(self) -> None:
        settings = replace(
            self.settings,
            labels_path=self.root / "missing.csv",
        )
        block = FINALIZED - 1
        result, _ = self.run_query(
            [
                transfer_log(block, 0, ZERO_ADDRESS, WALLET_A),
                transfer_log(block, 1, WALLET_A, ZERO_ADDRESS),
            ],
            settings=settings,
            query=self.query(settings=settings),
        )
        self.assertEqual(
            [item["flow_type"] for item in result["transfers"]],
            ["mint", "burn"],
        )

    def test_insufficient_cex_coverage_preserves_mint_and_burn(self) -> None:
        write_label_rows(
            self.labels,
            [
                f"8453,{CEX_A_HOT},Binance,cex,hot,reviewed,0.40,,\n",
            ],
        )
        block = FINALIZED - 1
        result, _ = self.run_query(
            [
                transfer_log(block, 0, ZERO_ADDRESS, WALLET_A),
                transfer_log(block, 1, WALLET_A, ZERO_ADDRESS),
            ]
        )
        self.assertEqual(
            result["labels"]["status"], "insufficient_cex_coverage"
        )
        self.assertEqual(
            [item["flow_type"] for item in result["transfers"]],
            ["mint", "burn"],
        )

    def test_low_confidence_cex_is_identity_only(self) -> None:
        write_label_rows(
            self.labels,
            [
                f"8453,{CEX_A_HOT},Binance,cex,hot,reviewed,0.40,,\n",
            ],
        )
        result, _ = self.run_query(
            [
                transfer_log(
                    FINALIZED - 1, 0, WALLET_A, CEX_A_HOT
                )
            ]
        )
        destination = result["transfers"][0]["to"]
        self.assertEqual(
            result["labels"]["status"], "insufficient_cex_coverage"
        )
        self.assertEqual(result["transfers"][0]["flow_type"], "unclassified")
        self.assertTrue(destination["known"])
        self.assertEqual(destination["entity_name"], "Binance")
        self.assertEqual(destination["confidence"], 0.4)
        self.assertFalse(destination["classification_eligible"])
        self.assertTrue(result["warnings"])

    def test_high_confidence_cex_produces_inflow(self) -> None:
        result, _ = self.run_query(
            [
                transfer_log(
                    FINALIZED - 1, 0, WALLET_A, CEX_A_HOT
                )
            ]
        )
        destination = result["transfers"][0]["to"]
        self.assertEqual(result["labels"]["status"], "ok")
        self.assertEqual(result["transfers"][0]["flow_type"], "inflow")
        self.assertTrue(destination["classification_eligible"])
        self.assertGreater(
            result["labels"]["classification_eligible_cex_count"], 0
        )

    def test_low_confidence_cex_does_not_become_directional_via_peer(
        self,
    ) -> None:
        write_label_rows(
            self.labels,
            [
                f"8453,{CEX_A_HOT},Binance,cex,hot,reviewed,0.40,,\n",
                f"8453,{CEX_B_HOT},OKX,cex,hot,reviewed,0.99,,\n",
            ],
        )
        result, _ = self.run_query(
            [
                transfer_log(
                    FINALIZED - 1, 0, CEX_A_HOT, CEX_B_HOT
                )
            ]
        )
        record = result["transfers"][0]
        self.assertEqual(result["labels"]["status"], "ok")
        self.assertEqual(record["flow_type"], "unclassified")
        self.assertTrue(record["from"]["known"])
        self.assertFalse(record["from"]["classification_eligible"])
        self.assertTrue(record["to"]["classification_eligible"])

    def test_expired_cex_label_does_not_produce_direction(self) -> None:
        expired_at = GENESIS_TIME + (FINALIZED - 10) * 60
        write_label_rows(
            self.labels,
            [
                (
                    f"8453,{CEX_A_HOT},Binance,cex,hot,reviewed,"
                    f"0.99,,{expired_at}\n"
                ),
                f"8453,{CEX_B_HOT},OKX,cex,hot,reviewed,0.99,,\n",
            ],
        )
        result, _ = self.run_query(
            [
                transfer_log(
                    FINALIZED - 1, 0, WALLET_A, CEX_A_HOT
                )
            ]
        )
        self.assertEqual(result["labels"]["status"], "ok")
        self.assertEqual(result["transfers"][0]["flow_type"], "unclassified")
        self.assertFalse(result["transfers"][0]["to"]["known"])
        self.assertFalse(
            result["transfers"][0]["to"]["classification_eligible"]
        )

    def test_known_non_cex_identities_can_produce_non_cex(self) -> None:
        result, _ = self.run_query(
            [
                transfer_log(
                    FINALIZED - 1, 0, WALLET_C, WALLET_D
                )
            ]
        )
        record = result["transfers"][0]
        self.assertEqual(record["flow_type"], "non_cex")
        self.assertTrue(record["from"]["known"])
        self.assertEqual(record["from"]["entity_type"], "wallet")
        self.assertFalse(record["from"]["classification_eligible"])

    def test_low_confidence_non_cex_identity_stays_unclassified(self) -> None:
        write_label_rows(
            self.labels,
            [
                f"8453,{CEX_A_HOT},Binance,cex,hot,reviewed,0.99,,\n",
                f"8453,{WALLET_C},Fund A,wallet,wallet,reviewed,0.40,,\n",
                f"8453,{WALLET_D},Fund B,wallet,wallet,reviewed,0.90,,\n",
            ],
        )
        result, _ = self.run_query(
            [
                transfer_log(
                    FINALIZED - 1, 0, WALLET_C, WALLET_D
                )
            ]
        )
        record = result["transfers"][0]
        self.assertEqual(record["flow_type"], "unclassified")
        self.assertTrue(record["from"]["known"])
        self.assertEqual(record["from"]["confidence"], 0.4)

    def test_synthetic_cex_label_is_rejected_for_network_query(self) -> None:
        write_label_rows(
            self.labels,
            [
                (
                    f"8453,{CEX_A_HOT},Binance,cex,hot,"
                    "synthetic_fixture,0.99,,\n"
                ),
            ],
        )
        with self.assertRaises(TokenActivityQueryError) as raised:
            self.run_query([])
        self.assertEqual(raised.exception.code, "label_file_invalid")

    def test_malformed_labels_fail_closed(self) -> None:
        self.labels.write_text("bad,columns\n1,2\n", encoding="utf-8")
        with self.assertRaises(TokenActivityQueryError) as raised:
            self.run_query([])
        self.assertEqual(raised.exception.code, "label_file_invalid")

    def test_schema_contains_required_sections_and_basescan_link(self) -> None:
        item = transfer_log(FINALIZED - 1, 0, WALLET_A, WALLET_B)
        result, _ = self.run_query([item])
        self.assertEqual(result["schema_version"], 1)
        for key in (
            "status",
            "complete",
            "truncated",
            "truncation_reason",
            "query",
            "token",
            "price",
            "labels",
            "summary",
            "largest_transfers",
            "transfers",
            "limits",
            "diagnostics",
            "warnings",
        ):
            self.assertIn(key, result)
        record = result["transfers"][0]
        self.assertEqual(
            record["explorer_url"],
            f"https://basescan.org/tx/{record['tx_hash']}",
        )
        self.assertEqual(record["block_hash"], block_hash(FINALIZED - 1))
        self.assertEqual(record["token_contract"], TOKEN)
        self.assertEqual(record["price_status"], "disabled")

    def test_large_amount_raw_and_decimal_are_lossless_strings(self) -> None:
        raw = 10**30 + 123_456
        item = transfer_log(
            FINALIZED - 1,
            0,
            WALLET_A,
            WALLET_B,
            amount=raw,
        )
        result, _ = self.run_query([item])
        record = result["transfers"][0]
        self.assertEqual(record["amount_raw"], str(raw))
        self.assertIsInstance(record["amount"], str)
        self.assertEqual(
            Decimal(record["amount"]),
            Decimal(raw) / Decimal(10**6),
        )
        json.dumps(result)

    def test_largest_transfers_use_amount_and_deterministic_ties(self) -> None:
        logs = [
            transfer_log(FINALIZED - 1, 2, WALLET_A, WALLET_B, amount=5),
            transfer_log(FINALIZED - 2, 1, WALLET_A, WALLET_B, amount=5),
            transfer_log(FINALIZED - 1, 0, WALLET_A, WALLET_B, amount=9),
        ]
        result, _ = self.run_query(logs, query=self.query(top=2))
        largest = result["largest_transfers"]
        self.assertEqual([item["amount_raw"] for item in largest], ["9", "5"])
        self.assertLess(
            largest[1]["block_number"], FINALIZED - 1
        )

    def test_payload_never_exposes_rpc_url_or_credentials(self) -> None:
        result, _ = self.run_query([])
        text = json.dumps(result)
        for secret in ("secret", "example.invalid", "private", "Authorization"):
            self.assertNotIn(secret, text)


class PriceAndIsolationTests(TokenActivityTestCase):
    def quote(self) -> PriceQuote:
        return PriceQuote(
            chain_id=8453,
            token_address=TOKEN,
            price_usd=Decimal("2.5"),
            volume_24h_usd=None,
            source="static",
            observed_at=1_700_000_000,
        )

    def test_default_query_never_calls_price_provider(self) -> None:
        provider = StaticProvider(self.quote())
        result, _ = self.run_query(
            [transfer_log(FINALIZED - 1, 0, WALLET_A, WALLET_B)],
            provider=provider,
        )
        self.assertEqual(provider.calls, 0)
        self.assertEqual(result["price"]["status"], "disabled")
        self.assertIsNone(result["transfers"][0]["amount_usd"])

    def test_with_price_uses_decimal_current_estimate(self) -> None:
        provider = StaticProvider(self.quote())
        query = self.query(with_price=True)
        result, _ = self.run_query(
            [
                transfer_log(
                    FINALIZED - 1,
                    0,
                    WALLET_A,
                    WALLET_B,
                    amount=2_000_000,
                )
            ],
            query=query,
            provider=provider,
        )
        self.assertEqual(provider.calls, 1)
        self.assertEqual(result["price"]["price_usd"], "2.5")
        self.assertEqual(result["transfers"][0]["amount_usd"], "5")
        self.assertFalse(result["price"]["historical_price"])
        self.assertIn(
            "美元金额按查询时可用价格估算", result["warnings"]
        )

    def test_missing_price_preserves_transfer(self) -> None:
        result, _ = self.run_query(
            [transfer_log(FINALIZED - 1, 0, WALLET_A, WALLET_B)],
            query=self.query(with_price=True),
            provider=StaticProvider(None),
        )
        self.assertTrue(result["complete"])
        self.assertEqual(result["price"]["status"], "missing")
        self.assertEqual(result["summary"]["transfer_count"], 1)
        self.assertIsNone(result["transfers"][0]["amount_usd"])

    def test_missing_price_with_min_usd_is_partial_and_filter_not_applied(
        self,
    ) -> None:
        result, _ = self.run_query(
            [transfer_log(FINALIZED - 1, 0, WALLET_A, WALLET_B)],
            query=self.query(with_price=True, min_usd="10"),
            provider=StaticProvider(None),
        )
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["query"]["usd_filter_applied"])
        self.assertEqual(
            result["truncation_reason"],
            "price_unavailable_for_usd_filter",
        )
        self.assertEqual(result["summary"]["transfer_count"], 1)

    def test_min_usd_filter_applies_only_when_price_is_available(self) -> None:
        logs = [
            transfer_log(
                FINALIZED - 1, 0, WALLET_A, WALLET_B, amount=1_000_000
            ),
            transfer_log(
                FINALIZED - 1, 1, WALLET_A, WALLET_B, amount=10_000_000
            ),
        ]
        result, _ = self.run_query(
            logs,
            query=self.query(with_price=True, min_usd="10"),
            provider=StaticProvider(self.quote()),
        )
        self.assertTrue(result["query"]["usd_filter_applied"])
        self.assertEqual(result["summary"]["transfer_count"], 1)
        self.assertEqual(result["transfers"][0]["amount_usd"], "25")

    def test_read_only_metadata_resolver_uses_memory_without_store(self) -> None:
        rpc = FakeRpc()
        resolver = TokenMetadataResolver(rpc, None)
        first = resolver.resolve(8453, TOKEN)
        calls = rpc.request_count
        second = resolver.resolve(8453, TOKEN)
        self.assertEqual(first, second)
        self.assertEqual(rpc.request_count, calls)

    def test_query_does_not_create_or_modify_business_state(self) -> None:
        sentinels = {
            self.settings.db_path: b"onchain-db-sentinel",
            self.settings.signal_events_db_path: b"signals-db-sentinel",
            self.settings.tg_push_history_path: b"history-sentinel",
            self.settings.tg_outbox_path: b"outbox-sentinel",
        }
        for path, content in sentinels.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        before = {path: path.read_bytes() for path in sentinels}
        self.run_query(
            [transfer_log(FINALIZED - 1, 0, WALLET_A, WALLET_B)]
        )
        after = {path: path.read_bytes() for path in sentinels}
        self.assertEqual(before, after)
        self.assertFalse(self.settings.runtime_status_path.exists())
        self.assertFalse(self.settings.signal_events_path.exists())


if __name__ == "__main__":
    unittest.main()
