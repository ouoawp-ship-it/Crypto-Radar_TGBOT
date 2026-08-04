from __future__ import annotations

import time
from decimal import Decimal
from typing import Callable

from .collectors.evm_http import JsonRpcClient, RpcError
from .domain import TokenSnapshot
from .labels import normalize_evm_address
from .models import NormalizedTransfer


BALANCE_OF_SELECTOR = "70a08231"
TOTAL_SUPPLY_SELECTOR = "18160ddd"


class SnapshotBudgetExhausted(RuntimeError):
    pass


def _block_tag(block_number: int) -> str:
    if isinstance(block_number, bool) or int(block_number) < 0:
        raise ValueError("invalid_snapshot_block")
    return hex(int(block_number))


def _decode_uint256(value: str) -> int:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ValueError("invalid_uint256_result")
    payload = value[2:]
    if not payload or len(payload) > 64:
        raise ValueError("invalid_uint256_result")
    try:
        decoded = int(payload, 16)
    except ValueError as exc:
        raise ValueError("invalid_uint256_result") from exc
    if decoded < 0 or decoded >= 2**256:
        raise ValueError("invalid_uint256_result")
    return decoded


def _balance_of_data(address: str) -> str:
    normalized = normalize_evm_address(address)
    return "0x" + BALANCE_OF_SELECTOR + normalized[2:].rjust(64, "0")


class EvmTokenSnapshotProvider:
    """Bounded exact-block ERC-20 balance and supply reader.

    It reuses the query's injected JSON-RPC client. No URL or environment value
    is read here, keeping the domain boundary deterministic and testable.
    """

    def __init__(
        self,
        rpc: JsonRpcClient,
        *,
        max_balance_calls: int = 20,
        max_supply_calls: int = 5,
        ttl_sec: int = 300,
        clock: Callable[[], float] = time.monotonic,
        circulating_supply_reader: Callable[
            [int, str, int, int], Decimal | None
        ]
        | None = None,
    ):
        if not 1 <= int(max_balance_calls) <= 100:
            raise ValueError("invalid_balance_call_budget")
        if not 1 <= int(max_supply_calls) <= 20:
            raise ValueError("invalid_supply_call_budget")
        if not 0 <= int(ttl_sec) <= 3600:
            raise ValueError("invalid_snapshot_ttl")
        self.rpc = rpc
        self.max_balance_calls = int(max_balance_calls)
        self.max_supply_calls = int(max_supply_calls)
        self.ttl_sec = int(ttl_sec)
        self.clock = clock
        self.circulating_supply_reader = circulating_supply_reader
        self.balance_calls = 0
        self.supply_calls = 0
        self._balance_cache: dict[tuple[str, str, int], tuple[float, Decimal]] = {}
        self._supply_cache: dict[tuple[str, int], tuple[float, Decimal]] = {}

    def snapshot_for_transfer(
        self,
        transfer: NormalizedTransfer,
        *,
        decimals: int,
    ) -> TokenSnapshot:
        if transfer.block_number <= 0:
            raise ValueError("invalid_snapshot_block")
        if not 0 <= int(decimals) <= 255:
            raise ValueError("invalid_token_decimals")
        token = normalize_evm_address(transfer.token_address)
        sender = normalize_evm_address(transfer.from_address)
        before: Decimal | None = None
        after: Decimal | None = None
        supply: Decimal | None = None
        balance_errors = 0
        supply_status = "ok"

        for block, target in (
            (transfer.block_number - 1, "before"),
            (transfer.block_number, "after"),
        ):
            try:
                value = self._balance(token, sender, block, int(decimals))
            except SnapshotBudgetExhausted:
                balance_errors += 1
                value = None
            except (RpcError, ValueError):
                balance_errors += 1
                value = None
            if target == "before":
                before = value
            else:
                after = value

        try:
            supply = self._total_supply(
                token, transfer.block_number, int(decimals)
            )
        except SnapshotBudgetExhausted:
            supply_status = "budget_exhausted"
        except (RpcError, ValueError):
            supply_status = "rpc_failed"

        circulating: Decimal | None = None
        circulating_status = "not_available"
        if self.circulating_supply_reader is not None:
            try:
                circulating = self.circulating_supply_reader(
                    transfer.chain_id,
                    token,
                    transfer.block_number,
                    int(decimals),
                )
                circulating_status = (
                    "ok" if circulating is not None else "not_available"
                )
            except (ArithmeticError, TypeError, ValueError):
                circulating_status = "provider_failed"
                circulating = None

        if balance_errors == 0:
            balance_status = "ok"
        elif self.balance_calls >= self.max_balance_calls:
            balance_status = "budget_exhausted"
        elif balance_errors == 1:
            balance_status = "partial"
        else:
            balance_status = "rpc_failed"
        return TokenSnapshot(
            chain_id=transfer.chain_id,
            token_address=token,
            block_number=transfer.block_number,
            decimals=int(decimals),
            sender_balance_before=before,
            sender_balance_after=after,
            total_supply=supply,
            circulating_supply=circulating,
            balance_status=balance_status,
            supply_status=supply_status,
            circulating_supply_status=circulating_status,
            rpc_calls=self.balance_calls + self.supply_calls,
        )

    def _balance(
        self,
        token: str,
        account: str,
        block_number: int,
        decimals: int,
    ) -> Decimal:
        key = (token, account, int(block_number))
        cached = self._cached(self._balance_cache, key)
        if cached is not None:
            return cached
        if self.balance_calls >= self.max_balance_calls:
            raise SnapshotBudgetExhausted("snapshot_balance_budget_exhausted")
        self.balance_calls += 1
        raw = _decode_uint256(
            self.rpc.eth_call(
                token,
                _balance_of_data(account),
                block_tag=_block_tag(block_number),
            )
        )
        value = Decimal(raw) / (Decimal(10) ** decimals)
        self._balance_cache[key] = (self.clock(), value)
        return value

    def _total_supply(
        self,
        token: str,
        block_number: int,
        decimals: int,
    ) -> Decimal:
        key = (token, int(block_number))
        cached = self._cached(self._supply_cache, key)
        if cached is not None:
            return cached
        if self.supply_calls >= self.max_supply_calls:
            raise SnapshotBudgetExhausted("snapshot_supply_budget_exhausted")
        self.supply_calls += 1
        raw = _decode_uint256(
            self.rpc.eth_call(
                token,
                "0x" + TOTAL_SUPPLY_SELECTOR,
                block_tag=_block_tag(block_number),
            )
        )
        value = Decimal(raw) / (Decimal(10) ** decimals)
        self._supply_cache[key] = (self.clock(), value)
        return value

    def _cached(self, cache: dict, key: object) -> Decimal | None:
        item = cache.get(key)
        if item is None:
            return None
        observed_at, value = item
        if self.ttl_sec and self.clock() - observed_at > self.ttl_sec:
            cache.pop(key, None)
            return None
        return value
