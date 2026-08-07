from __future__ import annotations

from dataclasses import dataclass

from .models import CandidateSnapshot, FORMAL_MAPPING_METHODS, RULES_VERSION


SHORT_SQUEEZE_CANDIDATE = "short_squeeze_candidate"
HIGH_LEVERAGE_CANDIDATE = "high_leverage_candidate"


@dataclass(frozen=True)
class CandidateThresholds:
    market_cap_max_usd: float = 30_000_000.0
    short_squeeze_min_ratio: float = 0.20
    short_squeeze_max_funding_rate: float = 0.0
    high_leverage_min_ratio: float = 0.50

    def validate(self) -> None:
        if self.market_cap_max_usd <= 0:
            raise ValueError("market_cap_max_usd must be positive")
        if not 0 <= self.short_squeeze_min_ratio <= 100:
            raise ValueError("short_squeeze_min_ratio out of range")
        if not -1 <= self.short_squeeze_max_funding_rate <= 1:
            raise ValueError("short_squeeze_max_funding_rate out of range")
        if not 0 <= self.high_leverage_min_ratio <= 100:
            raise ValueError("high_leverage_min_ratio out of range")


def _fields_ready(snapshot: CandidateSnapshot, required: set[str]) -> bool:
    unavailable = set(snapshot.missing_fields) | set(snapshot.stale_fields) | set(snapshot.invalid_fields)
    return not (unavailable & required)


def apply_candidate_rules(
    snapshot: CandidateSnapshot,
    thresholds: CandidateThresholds,
) -> CandidateSnapshot:
    """Apply P1 rules in ratio units (0.20 means 20%)."""

    thresholds.validate()
    snapshot.candidate_tags = []
    snapshot.matched_rules = []
    mapping_ready = (
        snapshot.cmc_id is not None
        and snapshot.mapping_confidence == "high"
        and snapshot.mapping_method in FORMAL_MAPPING_METHODS
    )
    ratio = snapshot.binance_oi_market_cap_ratio
    cap = snapshot.market_cap_usd

    base_ready = mapping_ready and _fields_ready(
        snapshot,
        {"market_cap_usd", "oi_value_usd", "mark_price"},
    )
    if (
        base_ready
        and cap is not None
        and ratio is not None
        and cap <= thresholds.market_cap_max_usd
        and ratio >= thresholds.short_squeeze_min_ratio
        and snapshot.funding_rate is not None
        and snapshot.funding_rate < thresholds.short_squeeze_max_funding_rate
        and _fields_ready(snapshot, {"funding_rate"})
    ):
        snapshot.candidate_tags.append(SHORT_SQUEEZE_CANDIDATE)
        snapshot.matched_rules.append(f"{RULES_VERSION}:short_squeeze")

    if (
        base_ready
        and ratio is not None
        and ratio >= thresholds.high_leverage_min_ratio
    ):
        snapshot.candidate_tags.append(HIGH_LEVERAGE_CANDIDATE)
        snapshot.matched_rules.append(f"{RULES_VERSION}:high_leverage")
    return snapshot


__all__ = [
    "CandidateThresholds",
    "HIGH_LEVERAGE_CANDIDATE",
    "SHORT_SQUEEZE_CANDIDATE",
    "apply_candidate_rules",
]
