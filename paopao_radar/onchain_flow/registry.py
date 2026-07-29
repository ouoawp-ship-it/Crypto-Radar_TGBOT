from __future__ import annotations

import hashlib
import json
from typing import Any

from .automation_store import AutomationStore, AutomationStoreError
from .collectors.evm_http import JsonRpcClient, RpcError
from .config import OnchainSettings
from .constants import BASE_CHAIN_ID
from .token_metadata import TokenMetadataResolver


class RegistryService:
    def __init__(
        self,
        settings: OnchainSettings,
        store: AutomationStore,
        *,
        rpc: Any | None = None,
        bridge: Any | None = None,
    ):
        self.settings = settings
        self.store = store
        self._rpc = rpc
        self._bridge = bridge

    def verify(
        self,
        token_key: str,
        *,
        allow_network: bool,
        set_primary: bool,
        accept_symbol_mismatch: bool,
    ) -> dict[str, object]:
        if not allow_network:
            raise AutomationStoreError(
                "allow_network_required",
                "registry-verify requires explicit --allow-network",
            )
        registry = self.store.get_registry(token_key)
        if registry is None:
            raise AutomationStoreError(
                "registry_not_found", "registry token does not exist"
            )
        if not self.settings.base_http_rpc_url and self._rpc is None:
            raise AutomationStoreError(
                "rpc_not_configured", "Base HTTP RPC is not configured"
            )
        rpc = self._rpc or JsonRpcClient(
            self.settings.base_http_rpc_url,
            timeout_sec=float(self.settings.rpc_timeout_sec),
            retry=self.settings.rpc_retry,
            backoff_sec=float(self.settings.rpc_backoff_sec),
            rate_limit_per_second=self.settings.rpc_rate_limit_per_second,
            max_requests=min(32, self.settings.token_activity_max_rpc_requests),
        )
        try:
            chain_id = int(rpc.chain_id())
        except RpcError as exc:
            raise AutomationStoreError(
                "rpc_unavailable", "Base RPC provider check failed"
            ) from exc
        if chain_id != BASE_CHAIN_ID:
            raise AutomationStoreError(
                "wrong_chain", "RPC chain ID must be 8453"
            )
        resolver = TokenMetadataResolver(
            rpc,
            None,
            raise_rpc_errors=True,
        )
        try:
            metadata = resolver.resolve(
                BASE_CHAIN_ID,
                str(registry["contract_address"]),
            )
        except RpcError as exc:
            raise AutomationStoreError(
                "rpc_unavailable", "token metadata verification failed"
            ) from exc
        if metadata.metadata_status != "verified_erc20" or (
            metadata.decimals is None
        ):
            raise AutomationStoreError(
                resolver.last_resolution_reason or "token_not_erc20",
                "contract could not be verified as a standard ERC-20",
            )
        expected_symbol = str(registry["market_symbol"])[:-4].upper()
        actual_symbol = str(metadata.symbol or "").upper()
        if actual_symbol != expected_symbol and not accept_symbol_mismatch:
            raise AutomationStoreError(
                "symbol_mismatch_requires_confirmation",
                f"on-chain symbol {actual_symbol or '<empty>'} differs from "
                f"market symbol {expected_symbol}; explicit confirmation "
                "is required",
            )
        facts = {
            "chain_id": BASE_CHAIN_ID,
            "contract": str(registry["contract_address"]).lower(),
            "symbol": actual_symbol,
            "name": str(metadata.name or ""),
            "decimals": int(metadata.decimals),
            "metadata_status": metadata.metadata_status,
        }
        metadata_hash = hashlib.sha256(
            json.dumps(
                facts,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        verified = self.store.verify_registry(
            token_key,
            token_symbol=actual_symbol,
            token_name=str(metadata.name or ""),
            decimals=int(metadata.decimals),
            metadata_hash=metadata_hash,
            verification_method="base_rpc_erc20_metadata",
            verification_note=(
                "symbol mismatch explicitly accepted"
                if actual_symbol != expected_symbol
                else ""
            ),
            set_primary=set_primary,
        )
        bridge = self._bridge
        if bridge is None:
            from .signal_bridge import SignalBridge

            bridge = SignalBridge(self.settings, self.store)
        verified["reconciliation"] = bridge.reconcile_token(verified)
        return verified
