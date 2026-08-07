from __future__ import annotations

from dataclasses import asdict, dataclass, field
from math import isfinite
from typing import Any


SCHEMA_VERSION = 1
RULES_VERSION = "altcoin_contract_anomaly.p1.v1"
FORMAL_MAPPING_METHODS = frozenset({
    "verified_override",
    "contract_address_match",
    "existing_verified_anchor",
})


def finite_float(value: Any, *, minimum: float | None = None) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not isfinite(parsed):
        return None
    if minimum is not None and parsed < minimum:
        return None
    return parsed


def json_safe(value: Any) -> Any:
    if isinstance(value, float):
        return value if isfinite(value) else None
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(item) for item in value]
    return value


@dataclass(frozen=True)
class MappingRecord:
    binance_symbol: str
    base_asset: str
    normalized_asset: str
    contract_multiplier: int
    cmc_id: int | None
    cmc_name: str | None
    cmc_symbol: str | None
    cmc_slug: str | None
    token_address: str | None
    mapping_method: str
    mapping_confidence: str
    mapping_evidence: tuple[str, ...] = ()
    verified_at: str | None = None
    rejection_reason: str | None = None

    @property
    def is_formal(self) -> bool:
        return (
            self.mapping_method in FORMAL_MAPPING_METHODS
            and self.mapping_confidence == "high"
            and isinstance(self.cmc_id, int)
            and self.cmc_id > 0
        )

    def to_dict(self) -> dict[str, Any]:
        return json_safe(asdict(self))


@dataclass
class CandidateSnapshot:
    schema_version: int
    symbol: str
    base_asset: str
    normalized_asset: str
    contract_multiplier: int
    exchange: str
    contract_type: str
    cmc_id: int | None
    mapping_method: str
    mapping_confidence: str
    market_cap_usd: float | None
    market_cap_source: str | None
    market_cap_updated_at: str | None
    open_interest_raw: float | None
    open_interest_unit: str | None
    oi_value_usd: float | None
    mark_price: float | None
    funding_rate: float | None
    oi_market_cap_ratio: float | None
    candidate_tags: list[str] = field(default_factory=list)
    matched_rules: list[str] = field(default_factory=list)
    data_quality: str = "partial"
    missing_fields: list[str] = field(default_factory=list)
    collected_at: str = ""
    open_interest_updated_at: str | None = None
    mark_price_updated_at: str | None = None
    funding_rate_updated_at: str | None = None
    stale_fields: list[str] = field(default_factory=list)
    invalid_fields: list[str] = field(default_factory=list)
    mapping_evidence: list[str] = field(default_factory=list)
    mapping_rejection_reason: str | None = None
    oi_value_method: str | None = None
    binance_oi_usd: float | None = None
    binance_oi_market_cap_ratio: float | None = None
    binance_oi_source: str | None = None
    global_oi_usd: float | None = None
    global_oi_market_cap_ratio: float | None = None
    global_oi_source: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return json_safe(asdict(self))


def calculate_oi_value_usd(
    open_interest: Any,
    *,
    unit: str,
    mark_price: Any = None,
) -> float | None:
    """Normalize Binance OI without applying contract multipliers twice."""

    raw = finite_float(open_interest, minimum=0.0)
    if raw is None:
        return None
    normalized_unit = str(unit or "").strip().lower()
    if normalized_unit == "usd_notional":
        return raw
    if normalized_unit in {
        "base_asset",
        "binance_sum_open_interest",
        "contract_base_asset_quantity",
    }:
        mark = finite_float(mark_price, minimum=0.0)
        if mark is None or mark <= 0:
            return None
        return raw * mark
    return None


def calculate_oi_market_cap_ratio(oi_value_usd: Any, market_cap_usd: Any) -> float | None:
    oi_value = finite_float(oi_value_usd, minimum=0.0)
    market_cap = finite_float(market_cap_usd, minimum=0.0)
    if oi_value is None or market_cap is None or market_cap <= 0:
        return None
    return oi_value / market_cap


__all__ = [
    "CandidateSnapshot",
    "FORMAL_MAPPING_METHODS",
    "MappingRecord",
    "RULES_VERSION",
    "SCHEMA_VERSION",
    "calculate_oi_market_cap_ratio",
    "calculate_oi_value_usd",
    "finite_float",
    "json_safe",
]
