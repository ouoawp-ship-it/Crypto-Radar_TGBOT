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
    price_source: str = ""
    price_observed_at: int = 0
    retry_after: int = 0


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
    block_number: int = 0
    block_hash: str = ""
    price_source: str = ""
    price_observed_at: int = 0

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
class DetectedRollingFlow:
    snapshot: RollingFlowSnapshot
    detection_types: tuple[str, ...]
    threshold_usd: Decimal


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
    gross_inflow_usd: Decimal | None = None
    gross_outflow_usd: Decimal | None = None
    net_flow_usd: Decimal | None = None
    duration_sec: int = 0
    inflow_tx_count: int = 0
    outflow_tx_count: int = 0
    distinct_inbound_counterparties: int = 0
    distinct_outbound_counterparties: int = 0
    evaluation_block: int = 0
    price_source: str = ""
    price_observed_at: int = 0
    chain_name: str = ""
    notification_key: str = ""


@dataclass(frozen=True)
class ChainCursor:
    chain_id: int
    last_seen_head: int
    last_finalized_block: int
    finalized_block_hash: str
    updated_at: int


@dataclass(frozen=True)
class ProcessedBlock:
    chain_id: int
    block_number: int
    block_hash: str
    block_time: int
    status: str = "finalized"
    processed_at: int = 0


@dataclass(frozen=True)
class PriceQuote:
    chain_id: int
    token_address: str
    price_usd: Decimal
    volume_24h_usd: Decimal | None
    source: str
    observed_at: int
    market_observed_at: int = 0
    fetched_at: int = 0

    @property
    def freshness_timestamp(self) -> int:
        return self.market_observed_at or self.observed_at


@dataclass(frozen=True)
class RollingFlowSnapshot:
    snapshot_key: str
    chain_id: int
    token_address: str
    symbol: str
    evaluation_time: int
    duration_sec: int
    gross_inflow_usd: Decimal
    gross_outflow_usd: Decimal
    net_flow_usd: Decimal
    inflow_tx_count: int
    outflow_tx_count: int
    distinct_inbound_counterparties: int
    distinct_outbound_counterparties: int
    exchanges: tuple[str, ...]
    active_15m_buckets: int
    min_label_confidence: float
    price_source: str
    price_observed_at: int
    evaluation_block: int
    algorithm_version: str
    inflow_exchanges: tuple[str, ...] = ()
    outflow_exchanges: tuple[str, ...] = ()
    valuation_price_usd: Decimal | None = None
    price_market_observed_at: int = 0
    price_fetched_at: int = 0

    @property
    def max_gross_usd(self) -> Decimal:
        return max(self.gross_inflow_usd, self.gross_outflow_usd)

    @property
    def net_dominance(self) -> Decimal:
        denominator = self.max_gross_usd
        if denominator == 0:
            return Decimal("0")
        return abs(self.net_flow_usd) / denominator

    @property
    def direction(self) -> str:
        if self.net_flow_usd > 0:
            return "inflow"
        if self.net_flow_usd < 0:
            return "outflow"
        return "balanced"

    @property
    def directional_exchanges(self) -> tuple[str, ...]:
        if self.direction == "inflow":
            return self.inflow_exchanges
        if self.direction == "outflow":
            return self.outflow_exchanges
        return ()


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
    replay_directory: str = ""

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
            "replay_directory": self.replay_directory,
        }
