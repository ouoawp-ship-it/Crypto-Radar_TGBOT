from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class NormalizedTransfer:
    event_id: str
    chain_id: int
    chain_name: str
    block_number: int
    block_hash: str
    block_time: int
    tx_hash: str
    log_index: int
    token_address: str
    from_address: str
    to_address: str
    amount_raw: int
    removed: bool
    confirmation_status: str
    source: str

    @classmethod
    def create(
        cls,
        *,
        chain_id: int,
        chain_name: str,
        block_number: int,
        block_hash: str,
        block_time: int,
        tx_hash: str,
        log_index: int,
        token_address: str,
        from_address: str,
        to_address: str,
        amount_raw: int | str,
        removed: bool = False,
        confirmation_status: str = "finalized",
        source: str = "replay",
    ) -> "NormalizedTransfer":
        normalized_tx = tx_hash.lower()
        status = "orphaned" if removed else confirmation_status
        return cls(
            event_id=f"{int(chain_id)}:{normalized_tx}:{int(log_index)}",
            chain_id=int(chain_id),
            chain_name=str(chain_name),
            block_number=int(block_number),
            block_hash=str(block_hash).lower(),
            block_time=int(block_time),
            tx_hash=normalized_tx,
            log_index=int(log_index),
            token_address=token_address.lower(),
            from_address=from_address.lower(),
            to_address=to_address.lower(),
            amount_raw=int(amount_raw),
            removed=bool(removed),
            confirmation_status=status,
            source=str(source),
        )


@dataclass(frozen=True)
class AddressLabel:
    chain_id: int
    address: str
    entity_name: str
    entity_type: str
    address_type: str
    source: str
    confidence: float
    valid_from: int | None = None
    valid_to: int | None = None

    def active_at(self, timestamp: int) -> bool:
        if self.valid_from is not None and timestamp < self.valid_from:
            return False
        return self.valid_to is None or timestamp <= self.valid_to


@dataclass(frozen=True)
class TokenMetadata:
    chain_id: int
    token_address: str
    symbol: str
    name: str
    decimals: int | None
    token_kind: str
    metadata_status: str
    updated_at: int
    price_usd: Decimal | None = None
    volume_24h_usd: Decimal | None = None
    historical_single_p99_usd: Decimal | None = None
    historical_15m_p99_usd: Decimal | None = None
    historical_60m_p99_usd: Decimal | None = None
    historical_window_median_usd: Decimal | None = None
    historical_window_mad_usd: Decimal | None = None


@dataclass(frozen=True)
class ClassifiedFlow:
    event_id: str
    chain_id: int
    token_address: str
    symbol: str
    block_time: int
    flow_type: str
    exchange_from: str | None
    exchange_to: str | None
    counterparty_address: str
    amount: Decimal | None
    amount_usd: Decimal | None
    label_confidence: float
    price_status: str

    @property
    def exchange(self) -> str | None:
        if self.flow_type == "inflow":
            return self.exchange_to
        if self.flow_type == "outflow":
            return self.exchange_from
        return self.exchange_to or self.exchange_from


@dataclass(frozen=True)
class FlowWindow:
    window_key: str
    chain_id: int
    token_address: str
    symbol: str
    direction: str
    window_start: int
    window_end: int
    duration_sec: int
    total_usd: Decimal
    tx_count: int
    distinct_counterparties: int
    exchanges: tuple[str, ...]
    active_15m_buckets: int
    min_label_confidence: float
    algorithm_version: str
    threshold_version: str


@dataclass(frozen=True)
class DetectedFlow:
    window: FlowWindow
    detection_types: tuple[str, ...]
    threshold_usd: Decimal
    source_event_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class OnchainAlert:
    alert_key: str
    chain_id: int
    token_address: str
    symbol: str
    direction: str
    score: int
    horizon: str
    confidence: str
    reasons: tuple[str, ...]
    detection_types: tuple[str, ...]
    window_start: int
    window_end: int
    total_usd: Decimal
    tx_count: int
    exchanges: tuple[str, ...]
    label_confidence: float
    price_status: str
    created_at: int
    severity_version: str


@dataclass(frozen=True)
class ReplaySummary:
    fixture: str
    transfers_seen: int
    unique_transfers: int
    duplicate_deliveries: int
    orphaned_transfers: int
    classified_flows: int
    windows: int
    alerts: int
    alert_keys: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        return {
            "fixture": self.fixture,
            "transfers_seen": self.transfers_seen,
            "unique_transfers": self.unique_transfers,
            "duplicate_deliveries": self.duplicate_deliveries,
            "orphaned_transfers": self.orphaned_transfers,
            "classified_flows": self.classified_flows,
            "windows": self.windows,
            "alerts": self.alerts,
            "alert_keys": list(self.alert_keys),
        }
