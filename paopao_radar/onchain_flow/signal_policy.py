from __future__ import annotations

from collections.abc import Mapping, Sequence


ACTIONABLE_BEHAVIORS = {
    "accumulation_candidate",
    "distribution_candidate",
    "wallet_consolidation_candidate",
    "fanout_candidate",
}


class DefaultSignalPolicy:
    """Preserves P2 gates and adds an explicit OR for event-risk signals."""

    def __init__(
        self,
        *,
        min_behavior_score: int,
        min_wallet_score: int,
        single_transfer_enabled: bool,
        min_single_transfer_score: int,
    ):
        self.min_behavior_score = int(min_behavior_score)
        self.min_wallet_score = int(min_wallet_score)
        self.single_transfer_enabled = bool(single_transfer_enabled)
        self.min_single_transfer_score = int(min_single_transfer_score)

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
    ) -> bool:
        if not (
            payload_complete
            and analysis_complete
            and analysis_status == "ok"
        ):
            return False
        sustained_behavior_gate = (
            behavior_type in ACTIONABLE_BEHAVIORS
            and int(behavior_score) >= self.min_behavior_score
        ) or int(max_wallet_score) >= self.min_wallet_score
        single_transfer_risk_gate = False
        if self.single_transfer_enabled:
            single_transfer_risk_gate = any(
                bool(item.get("actionable"))
                and int(item.get("rule_score") or 0)
                >= self.min_single_transfer_score
                and item.get("data_completeness") == "complete"
                for item in single_transfer_signals
            )
        return sustained_behavior_gate or single_transfer_risk_gate
