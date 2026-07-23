from __future__ import annotations

import re
import time
from typing import Callable

from .collectors.evm_http import JsonRpcClient, RpcError, RpcTransportError
from .db import OnchainStore
from .labels import normalize_evm_address
from .models import TokenMetadata


DECIMALS_SELECTOR = "0x313ce567"
TOTAL_SUPPLY_SELECTOR = "0x18160ddd"
SYMBOL_SELECTOR = "0x95d89b41"
NAME_SELECTOR = "0x06fdde03"
HEX_DATA_RE = re.compile(r"^0x[0-9a-fA-F]*$")


def decode_uint256(data: str) -> int:
    if not isinstance(data, str) or not re.fullmatch(
        r"0x[0-9a-fA-F]{64}", data
    ):
        raise ValueError("ABI uint256 is malformed")
    return int(data, 16)


def decode_abi_text(data: str) -> str:
    if not isinstance(data, str) or not HEX_DATA_RE.fullmatch(data):
        raise ValueError("ABI text is malformed")
    raw = bytes.fromhex(data[2:])
    if len(raw) == 32:
        return raw.rstrip(b"\x00").decode("utf-8", errors="strict").strip()
    if len(raw) < 64:
        raise ValueError("ABI string is incomplete")
    offset = int.from_bytes(raw[:32], "big")
    if offset + 32 > len(raw):
        raise ValueError("ABI string offset is invalid")
    length = int.from_bytes(raw[offset : offset + 32], "big")
    start = offset + 32
    end = start + length
    if length < 0 or end > len(raw):
        raise ValueError("ABI string length is invalid")
    return raw[start:end].decode("utf-8", errors="strict").strip()


class TokenMetadataResolver:
    def __init__(
        self,
        rpc: JsonRpcClient,
        store: OnchainStore,
        *,
        clock: Callable[[], float] = time.time,
        retry_delay_sec: int = 60,
    ):
        self.rpc = rpc
        self.store = store
        self.clock = clock
        self.retry_delay_sec = retry_delay_sec

    def resolve(self, chain_id: int, token_address: str) -> TokenMetadata:
        address = normalize_evm_address(token_address)
        now = int(self.clock())
        cached = self.store.metadata_map().get((chain_id, address))
        if cached is not None:
            if cached.metadata_status in {
                "verified_erc20",
                "rejected_non_erc20",
                "malformed",
            }:
                return cached
            if cached.retry_after > now:
                return cached
        try:
            code = self.rpc.get_code(address)
            if code in {"0x", "0x0", ""}:
                return self._save(
                    chain_id,
                    address,
                    status="rejected_non_erc20",
                    kind="unknown",
                    now=now,
                )
            decimals_raw = self.rpc.eth_call(address, DECIMALS_SELECTOR)
            try:
                decimals = decode_uint256(decimals_raw)
            except ValueError:
                return self._save(
                    chain_id,
                    address,
                    status="rejected_non_erc20",
                    kind="unknown",
                    now=now,
                )
            if decimals < 0 or decimals > 36:
                return self._save(
                    chain_id,
                    address,
                    status="rejected_non_erc20",
                    kind="unknown",
                    now=now,
                )
            supply_raw = self.rpc.eth_call(address, TOTAL_SUPPLY_SELECTOR)
            try:
                decode_uint256(supply_raw)
            except ValueError:
                return self._save(
                    chain_id,
                    address,
                    status="malformed",
                    kind="unknown",
                    decimals=decimals,
                    now=now,
                )
            symbol = self._optional_text(address, SYMBOL_SELECTOR) or "UNKNOWN"
            name = self._optional_text(address, NAME_SELECTOR) or symbol
            return self._save(
                chain_id,
                address,
                status="verified_erc20",
                kind="erc20",
                decimals=decimals,
                symbol=symbol.upper(),
                name=name,
                now=now,
            )
        except RpcTransportError:
            return self._save(
                chain_id,
                address,
                status="rpc_failed",
                kind="unknown",
                now=now,
                retry_after=now + self.retry_delay_sec,
            )
        except RpcError:
            return self._save(
                chain_id,
                address,
                status="incomplete",
                kind="unknown",
                now=now,
                retry_after=now + self.retry_delay_sec,
            )

    def _optional_text(self, address: str, selector: str) -> str:
        try:
            return decode_abi_text(self.rpc.eth_call(address, selector))
        except (RpcError, UnicodeDecodeError, ValueError):
            return ""

    def _save(
        self,
        chain_id: int,
        address: str,
        *,
        status: str,
        kind: str,
        now: int,
        decimals: int | None = None,
        symbol: str = "UNKNOWN",
        name: str = "",
        retry_after: int = 0,
    ) -> TokenMetadata:
        metadata = TokenMetadata(
            chain_id=chain_id,
            token_address=address,
            symbol=symbol,
            name=name,
            decimals=decimals,
            token_kind=kind,
            metadata_status=status,
            updated_at=now,
            retry_after=retry_after,
        )
        self.store.upsert_token_metadata(metadata)
        return metadata
