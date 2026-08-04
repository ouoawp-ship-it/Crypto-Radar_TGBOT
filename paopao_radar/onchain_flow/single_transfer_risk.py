from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .domain import (
    DeterministicSignal,
    SCORE_SEMANTICS,
    SingleTransferRiskContext,
)


CEX_ROLES = {"cex_wallet", "deposit", "hot", "cold", "collector"}
PROJECT_RELATIONSHIPS = {
    "deployer",
    "owner",
    "proxy_admin",
    "treasury",
    "vesting",
    "initial_lp",
    "unlock",
}
UNLOCK_RELATIONSHIPS = {"vesting", "unlock"}


@dataclass(frozen=True)
class SingleTransferRiskThresholds:
    enabled: bool = False
    min_score: int = 60
    critical_score: int = 80
    exit_high: Decimal = Decimal("0.80")
    exit_near_full: Decimal = Decimal("0.95")
    exit_full: Decimal = Decimal("0.99")
    supply_share_watch: Decimal = Decimal("0.001")
    supply_share_high: Decimal = Decimal("0.005")
    min_usd: Decimal = Decimal("1000000")

    def __post_init__(self) -> None:
        if not 0 <= int(self.min_score) <= 100:
            raise ValueError("single_transfer_min_score_invalid")
        if not int(self.min_score) <= int(self.critical_score) <= 100:
            raise ValueError("single_transfer_critical_score_invalid")
        if not (
            Decimal("0")
            < self.exit_high
            < self.exit_near_full
            < self.exit_full
            <= Decimal("1")
        ):
            raise ValueError("single_transfer_exit_thresholds_invalid")
        if not (
            Decimal("0")
            < self.supply_share_watch
            < self.supply_share_high
            <= Decimal("1")
        ):
            raise ValueError("single_transfer_supply_thresholds_invalid")
        if self.min_usd < 0:
            raise ValueError("single_transfer_min_usd_invalid")


class SingleTransferRiskEngine:
    """Pure, deterministic event-risk rules kept separate from P2 behavior."""

    def __init__(self, thresholds: SingleTransferRiskThresholds):
        self.thresholds = thresholds

    def evaluate(
        self, context: SingleTransferRiskContext
    ) -> tuple[DeterministicSignal, ...]:
        if not self.thresholds.enabled:
            return ()
        self._validate_context(context)
        transfer = context.transfer
        if (
            not context.query_complete
            or not context.finalized
            or transfer.removed
            or transfer.confirmation_status != "finalized"
        ):
            return ()
        if context.classification in {"internal", "cross_cex"}:
            return ()

        metrics = self._metrics(context)
        large = bool(
            (
                context.usd_value is not None
                and context.usd_value >= self.thresholds.min_usd
            )
            or (
                metrics["total_supply_share"] is not None
                and metrics["total_supply_share"]
                >= self.thresholds.supply_share_watch
            )
            or (
                metrics["circulating_supply_share"] is not None
                and metrics["circulating_supply_share"]
                >= self.thresholds.supply_share_watch
            )
            or (
                context.historical_single_transfer_anomaly is not None
                and context.historical_single_transfer_anomaly >= Decimal("3")
            )
        )
        destination_is_cex = context.destination_role in CEX_ROLES
        source_is_cex = context.source_role in CEX_ROLES
        signal_types: list[str] = []
        if large and context.classification == "inflow" and destination_is_cex:
            signal_types.append("large_cex_inflow")
        if large and context.classification == "outflow" and source_is_cex:
            signal_types.append("large_cex_outflow")

        exit_ratio = metrics["sender_exit_ratio"]
        if (
            context.classification == "inflow"
            and destination_is_cex
            and exit_ratio is not None
        ):
            if exit_ratio >= self.thresholds.exit_full:
                signal_types.append("full_exit_to_cex")
            elif exit_ratio >= self.thresholds.exit_near_full:
                signal_types.append("near_full_exit_to_cex")

        relationship = context.project_relationship
        relationship_type = (
            relationship.relationship_type if relationship is not None else ""
        )
        relationship_eligible = bool(
            relationship is not None
            and relationship_type in PROJECT_RELATIONSHIPS
            and relationship.evidence_type
            and (
                relationship.reviewed
                or relationship.confidence in {"verified", "deterministic"}
            )
        )
        if (
            large
            and context.classification == "inflow"
            and destination_is_cex
            and relationship_eligible
        ):
            signal_types.append("project_related_cex_inflow")
            if relationship_type in UNLOCK_RELATIONSHIPS:
                signal_types.append("unlock_related_cex_inflow")

        if not signal_types:
            return ()
        return tuple(
            self._build_signal(signal_type, context, metrics)
            for signal_type in dict.fromkeys(signal_types)
        )

    @staticmethod
    def _validate_context(context: SingleTransferRiskContext) -> None:
        transfer = context.transfer
        if not isinstance(transfer.amount_raw, int) or transfer.amount_raw < 0:
            raise ValueError("malformed_transfer")
        if context.amount_token < 0:
            raise ValueError("malformed_transfer")
        for value in (
            context.usd_value,
            context.historical_single_transfer_anomaly,
            context.same_window_cex_outflow_counter_evidence,
        ):
            if value is not None and (
                not isinstance(value, Decimal) or not value.is_finite()
            ):
                raise ValueError("invalid_risk_metric")
        snapshot = context.snapshot
        if snapshot.chain_id != transfer.chain_id:
            raise ValueError("snapshot_chain_mismatch")
        if snapshot.token_address.lower() != transfer.token_address.lower():
            raise ValueError("snapshot_token_mismatch")

    @staticmethod
    def _ratio(numerator: Decimal, denominator: Decimal | None) -> Decimal | None:
        if denominator is None or denominator <= 0:
            return None
        try:
            value = numerator / denominator
        except (InvalidOperation, ZeroDivisionError):
            return None
        return max(Decimal("0"), value)

    def _metrics(
        self, context: SingleTransferRiskContext
    ) -> dict[str, Decimal | bool | None]:
        snapshot = context.snapshot
        before = snapshot.sender_balance_before
        after = snapshot.sender_balance_after
        balance_inconsistent = bool(
            before is not None and context.amount_token > before
        )
        if before is None or before <= 0 or balance_inconsistent:
            exit_ratio = None
            remaining_ratio = None
        else:
            exit_ratio = self._ratio(context.amount_token, before)
            if exit_ratio is not None:
                exit_ratio = min(Decimal("1"), exit_ratio)
            remaining_ratio = self._ratio(
                max(Decimal("0"), after or Decimal("0")), before
            )
        return {
            "sender_exit_ratio": exit_ratio,
            "sender_remaining_ratio": remaining_ratio,
            "total_supply_share": self._ratio(
                context.amount_token, snapshot.total_supply
            ),
            "circulating_supply_share": self._ratio(
                context.amount_token, snapshot.circulating_supply
            ),
            "sender_balance_inconsistent": balance_inconsistent,
        }

    def _build_signal(
        self,
        signal_type: str,
        context: SingleTransferRiskContext,
        metrics: dict[str, Decimal | bool | None],
    ) -> DeterministicSignal:
        support: list[str] = ["complete_finalized_transfer"]
        counter: list[str] = []
        limitations = set(context.limitations)
        score = 20

        if context.classification == "inflow":
            support.append("reviewed_cex_destination")
            score += 20
        elif context.classification == "outflow":
            support.append("reviewed_cex_source")
            score += 20

        if context.usd_value is not None:
            support.append("usd_value_available")
            if context.usd_value >= self.thresholds.min_usd:
                support.append("single_transfer_usd_threshold_met")
                score += 15
        else:
            limitations.add("price_unavailable")

        share_values = [
            value
            for value in (
                metrics["total_supply_share"],
                metrics["circulating_supply_share"],
            )
            if isinstance(value, Decimal)
        ]
        max_share = max(share_values, default=None)
        if max_share is not None:
            if max_share >= self.thresholds.supply_share_high:
                support.append("high_supply_share")
                score += 25
            elif max_share >= self.thresholds.supply_share_watch:
                support.append("watch_supply_share")
                score += 15
        else:
            limitations.add("supply_share_unavailable")

        exit_ratio = metrics["sender_exit_ratio"]
        if isinstance(exit_ratio, Decimal):
            if exit_ratio >= self.thresholds.exit_full:
                support.append("sender_full_exit")
                score += 30
            elif exit_ratio >= self.thresholds.exit_near_full:
                support.append("sender_near_full_exit")
                score += 25
            elif exit_ratio >= self.thresholds.exit_high:
                support.append("sender_high_exit")
                score += 15
        else:
            limitations.add("sender_balance_unavailable")
        if metrics["sender_balance_inconsistent"]:
            limitations.discard("sender_balance_unavailable")
            limitations.add("sender_balance_inconsistent")

        anomaly = context.historical_single_transfer_anomaly
        if anomaly is not None and anomaly >= Decimal("3"):
            support.append("historical_single_transfer_anomaly")
            score += 10
        elif anomaly is None:
            limitations.add("historical_baseline_unavailable")

        relationship = context.project_relationship
        if signal_type in {
            "project_related_cex_inflow",
            "unlock_related_cex_inflow",
        } and relationship is not None:
            support.append(
                f"project_relationship:{relationship.relationship_type}"
            )
            score += 20

        if context.identity_coverage in {"complete", "reviewed"}:
            support.append("identity_coverage_sufficient")
            score += 10
        else:
            limitations.add("identity_coverage_insufficient")

        if (
            context.same_window_cex_outflow_counter_evidence is not None
            and context.same_window_cex_outflow_counter_evidence > 0
        ):
            counter.append("same_window_cex_outflow")
            score -= 10
        for item in context.internal_transfer_probability_evidence:
            if item:
                counter.append(str(item))
        if context.internal_transfer_probability_evidence:
            score -= 20
        if context.liquidity_impact in {"low", "none"}:
            counter.append("limited_liquidity_impact")
            score -= 5
        elif context.liquidity_impact == "unknown":
            limitations.add("liquidity_impact_unknown")

        score = max(0, min(100, score))
        if score >= self.thresholds.critical_score:
            level = "critical"
        elif score >= 75:
            level = "high_risk"
        elif score >= self.thresholds.min_score:
            level = "important"
        elif score >= 40:
            level = "watch"
        else:
            level = "info"
        if score >= 80 and len(support) >= 5:
            evidence_strength = "strong"
        elif score >= 60 and len(support) >= 3:
            evidence_strength = "medium"
        else:
            evidence_strength = "limited"

        facts: dict[str, object] = {
            "event_id": context.transfer.event_id,
            "chain_id": context.transfer.chain_id,
            "block_number": context.transfer.block_number,
            "tx_hash": context.transfer.tx_hash,
            "token_address": context.transfer.token_address,
            "from_address": context.transfer.from_address,
            "to_address": context.transfer.to_address,
            "amount_token": str(context.amount_token),
            "usd_value": (
                str(context.usd_value)
                if context.usd_value is not None
                else None
            ),
            "sender_balance_before": self._decimal_text(
                context.snapshot.sender_balance_before
            ),
            "sender_balance_after": self._decimal_text(
                context.snapshot.sender_balance_after
            ),
            "sender_exit_ratio": self._decimal_text(exit_ratio),
            "sender_remaining_ratio": self._decimal_text(
                metrics["sender_remaining_ratio"]
            ),
            "total_supply_share": self._decimal_text(
                metrics["total_supply_share"]
            ),
            "circulating_supply_share": self._decimal_text(
                metrics["circulating_supply_share"]
            ),
            "destination_role": context.destination_role,
            "source_role": context.source_role,
            "project_relationship": (
                context.project_relationship.relationship_type
                if context.project_relationship is not None
                else "unclassified"
            ),
            "liquidity_impact": context.liquidity_impact,
        }
        data_completeness = (
            "complete"
            if context.snapshot.balance_status == "ok"
            and context.snapshot.supply_status == "ok"
            and not metrics["sender_balance_inconsistent"]
            else "partial"
        )
        if data_completeness != "complete":
            limitations.add("snapshot_incomplete")
        return DeterministicSignal(
            signal_type=signal_type,
            level=level,
            rule_score=score,
            evidence_strength=evidence_strength,
            historical_anomaly=anomaly,
            data_completeness=data_completeness,
            identity_coverage=context.identity_coverage,
            support_evidence=tuple(dict.fromkeys(support)),
            counter_evidence=tuple(dict.fromkeys(counter)),
            limitations=tuple(sorted(limitations)),
            actionable=(
                data_completeness == "complete"
                and score >= self.thresholds.min_score
            ),
            score_semantics=SCORE_SEMANTICS,
            facts=facts,
        )

    @staticmethod
    def _decimal_text(value: Decimal | None) -> str | None:
        return str(value) if value is not None else None
