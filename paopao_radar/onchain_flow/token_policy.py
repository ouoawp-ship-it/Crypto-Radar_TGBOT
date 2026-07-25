from __future__ import annotations

from typing import Protocol

from .config import DEFAULT_STABLECOIN_TOKEN_IDS, OnchainSettings


NORMAL_TOKEN = "normal_token"
STABLECOIN = "stablecoin"
WRAPPED_OR_RECEIPT = "wrapped_or_receipt"
UNKNOWN = "unknown"


class TokenPolicy(Protocol):
    def classify(
        self,
        *,
        chain: str,
        token_id: str,
        token_address: str,
    ) -> str:
        ...


def _contract_identity(chain: str, token_address: str) -> str:
    if not chain.strip() or not token_address.strip():
        return ""
    return f"{chain.strip().lower()}:{token_address.strip().lower()}"


class ConfiguredTokenPolicy:
    def __init__(self, settings: OnchainSettings):
        self._stablecoin_ids = set(DEFAULT_STABLECOIN_TOKEN_IDS)
        self._stablecoin_ids.update(settings.stablecoin_token_ids)
        self._stablecoin_contracts = set(settings.stablecoin_contracts)
        self._wrapped_ids = set(settings.wrapped_or_receipt_token_ids)
        self._wrapped_contracts = set(
            settings.wrapped_or_receipt_contracts
        )

    def classify(
        self,
        *,
        chain: str,
        token_id: str,
        token_address: str,
    ) -> str:
        normalized_id = token_id.strip().lower()
        contract = _contract_identity(chain, token_address)
        if (
            normalized_id in self._stablecoin_ids
            or contract in self._stablecoin_contracts
        ):
            return STABLECOIN
        if (
            normalized_id in self._wrapped_ids
            or contract in self._wrapped_contracts
        ):
            return WRAPPED_OR_RECEIPT
        if normalized_id or token_address.strip():
            return NORMAL_TOKEN
        return UNKNOWN
