from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping, Protocol, Sequence

from .models import AddressLabel, NormalizedTransfer, TokenMetadata


SCORE_SEMANTICS = "rule_score_not_probability"
SIGNAL_LEVELS = ("info", "watch", "important", "high_risk", "critical")


@dataclass(frozen=True)
class ChainRef:
    """Namespace-aware identity shared by current and future chain adapters."""

    namespace: str
    reference: str
    slug: str
    family: str

    def __post_init__(self) -> None:
        if not all(
            value and value == value.strip()
            for value in (
                self.namespace,
                self.reference,
                self.slug,
                self.family,
            )
        ):
            raise ValueError("invalid_chain_ref")

    @property
    def key(self) -> str:
        return f"{self.namespace}:{self.reference}"


@dataclass(frozen=True)
class TokenSnapshot:
    """Exact-block token state used by deterministic risk rules.

    Missing values are represented explicitly instead of being coerced to zero.
    The snapshot provider owns RPC budgets; the domain engine only consumes facts.
    """

    chain_id: int
    token_address: str
    block_number: int
    decimals: int
    sender_balance_before: Decimal | None
    sender_balance_after: Decimal | None
    total_supply: Decimal | None
    circulating_supply: Decimal | None = None
    balance_status: str = "not_requested"
    supply_status: str = "not_requested"
    circulating_supply_status: str = "not_available"
    rpc_calls: int = 0


@dataclass(frozen=True)
class ProjectRelationship:
    chain_id: int
    address: str
    relationship_type: str
    evidence_type: str
    confidence: str
    reviewed: bool = False


@dataclass(frozen=True)
class SingleTransferRiskContext:
    transfer: NormalizedTransfer
    amount_token: Decimal
    usd_value: Decimal | None
    snapshot: TokenSnapshot
    source_role: str
    destination_role: str
    classification: str
    query_complete: bool
    finalized: bool
    identity_coverage: str
    project_relationship: ProjectRelationship | None = None
    historical_single_transfer_anomaly: Decimal | None = None
    same_window_cex_outflow_counter_evidence: Decimal | None = None
    internal_transfer_probability_evidence: tuple[str, ...] = ()
    liquidity_impact: str = "unknown"
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True)
class DeterministicSignal:
    signal_type: str
    level: str
    rule_score: int
    evidence_strength: str
    historical_anomaly: Decimal | None
    data_completeness: str
    identity_coverage: str
    support_evidence: tuple[str, ...]
    counter_evidence: tuple[str, ...]
    limitations: tuple[str, ...]
    actionable: bool
    score_semantics: str = SCORE_SEMANTICS
    facts: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.level not in SIGNAL_LEVELS:
            raise ValueError("invalid_signal_level")
        if not 0 <= int(self.rule_score) <= 100:
            raise ValueError("invalid_rule_score")
        if self.score_semantics != SCORE_SEMANTICS:
            raise ValueError("invalid_score_semantics")

    def to_dict(self) -> dict[str, object]:
        return {
            "signal_type": self.signal_type,
            "level": self.level,
            "rule_score": int(self.rule_score),
            "evidence_strength": self.evidence_strength,
            "historical_anomaly": (
                str(self.historical_anomaly)
                if self.historical_anomaly is not None
                else None
            ),
            "data_completeness": self.data_completeness,
            "identity_coverage": self.identity_coverage,
            "support_evidence": list(self.support_evidence),
            "counter_evidence": list(self.counter_evidence),
            "limitations": list(self.limitations),
            "actionable": bool(self.actionable),
            "score_semantics": self.score_semantics,
            "facts": dict(self.facts),
        }


class ChainFactProvider(Protocol):
    def execute(self, query: Any) -> dict[str, object]: ...


class TokenSnapshotProvider(Protocol):
    def snapshot_for_transfer(
        self,
        transfer: NormalizedTransfer,
        *,
        decimals: int,
    ) -> TokenSnapshot: ...


class AddressLabelRepository(Protocol):
    def labels_for(
        self,
        chain_id: int,
        addresses: Sequence[str],
        *,
        at: int,
    ) -> Mapping[str, AddressLabel]: ...


class ProjectRelationshipRepository(Protocol):
    def relationship_for(
        self,
        chain_id: int,
        address: str,
        *,
        at: int,
    ) -> ProjectRelationship | None: ...


class TransferClassifier(Protocol):
    def classify(
        self,
        transfer: NormalizedTransfer,
        metadata: TokenMetadata | None,
        registry: Any,
    ) -> Any: ...


class SingleTransferRiskEnginePort(Protocol):
    def evaluate(
        self, context: SingleTransferRiskContext
    ) -> tuple[DeterministicSignal, ...]: ...


class BehaviorAnalysisEngine(Protocol):
    def analyze(self, payload: dict[str, object]) -> dict[str, object]: ...


class RollingMetricRepository(Protocol):
    def historical_single_transfer_anomaly(
        self,
        *,
        chain_id: int,
        token_address: str,
        amount_token: Decimal,
        at: int,
    ) -> Decimal | None: ...


class SignalPolicy(Protocol):
    def actionable(
        self,
        *,
        payload_complete: bool,
        analysis_complete: bool,
        analysis_status: str,
        behavior_type: str,
        behavior_score: int,
        max_wallet_score: int,
        single_transfer_signals: Sequence[Mapping[str, object]] = (),
    ) -> bool: ...


class MarketContextReader(Protocol):
    def read_by_public_refs(
        self,
        public_refs: list[str],
        *,
        limit: int = 100,
    ) -> dict[str, object]: ...


class ReportFormatter(Protocol):
    def format(self, payload: Mapping[str, object]) -> str: ...


class NotificationGateway(Protocol):
    def history_records(self) -> list[dict[str, Any]]: ...

    def record_result(
        self,
        *,
        template_id: str,
        dedup_key: str,
        result: Any,
        text: str,
        signal_records: list[dict[str, Any]] | None = None,
    ) -> None: ...

    def send(
        self,
        text: str,
        template_id: str,
        dedup_key: str,
        *,
        send: bool,
        confirm_real_send: bool,
        **kwargs: object,
    ) -> Any: ...

    def delete_messages_detailed(
        self,
        message_ids: list[int],
        *,
        reason: str,
    ) -> dict[str, object]: ...

    def annotate_delivery_history(
        self,
        delivery_id: str,
        **updates: object,
    ) -> None: ...
