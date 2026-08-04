from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from .domain import (
    ProjectRelationshipRepository,
    RollingMetricRepository,
    SingleTransferRiskContext,
    TokenSnapshotProvider,
)
from .models import NormalizedTransfer
from .single_transfer_risk import SingleTransferRiskEngine


class SingleTransferRiskService:
    """Thin orchestration adapter from Token Activity facts to pure rules."""

    def __init__(
        self,
        engine: SingleTransferRiskEngine,
        snapshot_provider: TokenSnapshotProvider,
        *,
        relationship_repository: ProjectRelationshipRepository | None = None,
        rolling_repository: RollingMetricRepository | None = None,
    ):
        self.engine = engine
        self.snapshot_provider = snapshot_provider
        self.relationship_repository = relationship_repository
        self.rolling_repository = rolling_repository

    def evaluate(
        self,
        transfers: Sequence[NormalizedTransfer],
        records: Sequence[Mapping[str, object]],
        *,
        decimals: int,
        query_complete: bool,
        labels_status: str,
    ) -> dict[str, object]:
        if not self.engine.thresholds.enabled:
            return {
                "status": "disabled",
                "complete": True,
                "signals": [],
                "evaluated_transfers": 0,
                "snapshot_rpc_calls": 0,
                "score_semantics": "rule_score_not_probability",
            }
        if not query_complete:
            return {
                "status": "skipped_incomplete",
                "complete": False,
                "signals": [],
                "evaluated_transfers": 0,
                "snapshot_rpc_calls": 0,
                "score_semantics": "rule_score_not_probability",
            }
        record_by_event = {
            str(record.get("event_id") or ""): record for record in records
        }
        ordered = sorted(
            transfers,
            key=lambda transfer: (-transfer.amount_raw, transfer.event_id),
        )
        total_outflow = sum(
            (
                self._decimal(record.get("amount"))
                for record in records
                if record.get("flow_type") == "outflow"
            ),
            Decimal("0"),
        )
        signals: list[dict[str, object]] = []
        evaluated = 0
        incomplete_snapshots = 0
        skipped_records = 0
        for transfer in ordered:
            record = record_by_event.get(transfer.event_id)
            if record is None:
                skipped_records += 1
                continue
            try:
                amount = self._decimal(record.get("amount"))
                amount_usd = self._optional_decimal(record.get("amount_usd"))
                from_payload = self._mapping(record.get("from"))
                to_payload = self._mapping(record.get("to"))
                snapshot = self.snapshot_provider.snapshot_for_transfer(
                    transfer, decimals=decimals
                )
                if (
                    snapshot.balance_status != "ok"
                    or snapshot.supply_status != "ok"
                ):
                    incomplete_snapshots += 1
                relationship = (
                    self.relationship_repository.relationship_for(
                        transfer.chain_id,
                        transfer.from_address,
                        at=transfer.block_time,
                    )
                    if self.relationship_repository is not None
                    else None
                )
                anomaly = (
                    self.rolling_repository.historical_single_transfer_anomaly(
                        chain_id=transfer.chain_id,
                        token_address=transfer.token_address,
                        amount_token=amount,
                        at=transfer.block_time,
                    )
                    if self.rolling_repository is not None
                    else None
                )
                context = SingleTransferRiskContext(
                    transfer=transfer,
                    amount_token=amount,
                    usd_value=amount_usd,
                    snapshot=snapshot,
                    source_role=str(from_payload.get("address_type") or "unclassified"),
                    destination_role=str(
                        to_payload.get("address_type") or "unclassified"
                    ),
                    classification=str(record.get("flow_type") or "unclassified"),
                    query_complete=True,
                    finalized=(
                        transfer.confirmation_status == "finalized"
                        and not transfer.removed
                    ),
                    identity_coverage=(
                        "reviewed" if labels_status == "ok" else "insufficient"
                    ),
                    project_relationship=relationship,
                    historical_single_transfer_anomaly=anomaly,
                    same_window_cex_outflow_counter_evidence=(
                        total_outflow
                        if record.get("flow_type") == "inflow"
                        and total_outflow > 0
                        else None
                    ),
                    internal_transfer_probability_evidence=(
                        ("same_cex_identity",)
                        if record.get("flow_type") in {"internal", "consolidation"}
                        else ()
                    ),
                    liquidity_impact="unknown",
                    limitations=("block_boundary_balance_snapshot",),
                )
                signals.extend(
                    signal.to_dict() for signal in self.engine.evaluate(context)
                )
                evaluated += 1
            except (InvalidOperation, TypeError, ValueError):
                skipped_records += 1

        snapshot_calls = int(
            getattr(self.snapshot_provider, "balance_calls", 0)
        ) + int(getattr(self.snapshot_provider, "supply_calls", 0))
        complete = incomplete_snapshots == 0 and skipped_records == 0
        return {
            "status": "ok" if complete else "partial",
            "complete": complete,
            "signals": signals,
            "evaluated_transfers": evaluated,
            "skipped_transfers": skipped_records,
            "incomplete_snapshots": incomplete_snapshots,
            "snapshot_rpc_calls": snapshot_calls,
            "score_semantics": "rule_score_not_probability",
        }

    @staticmethod
    def _mapping(value: object) -> Mapping[str, object]:
        return value if isinstance(value, Mapping) else {}

    @staticmethod
    def _decimal(value: object) -> Decimal:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise ValueError("invalid_transfer_amount")
        return result

    @classmethod
    def _optional_decimal(cls, value: object) -> Decimal | None:
        if value is None:
            return None
        return cls._decimal(value)
