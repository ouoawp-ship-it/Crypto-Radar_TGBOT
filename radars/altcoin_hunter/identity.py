"""Explicit instrument identity: an exchange symbol is never an asset ID."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable

from .models import bounded_text, decimal_value


@dataclass(frozen=True)
class InstrumentIdentity:
    exchange: str
    market: str
    instrument_id: str
    symbol: str
    exchange_symbol: str
    canonical_asset_id: str | None = None
    contract_multiplier: str = "1"
    quantity_unit: str = "base"
    quote_currency: str = "USDT"
    mapping_method: str = "unresolved"

    def __post_init__(self) -> None:
        for name in ("exchange", "market", "instrument_id", "symbol", "exchange_symbol", "quote_currency"):
            bounded_text(getattr(self, name), name)
        bounded_text(self.canonical_asset_id, "canonical_asset_id", optional=True)
        decimal_value(self.contract_multiplier, "contract_multiplier", positive=True)
        if self.quantity_unit not in {"base", "contracts"}:
            raise ValueError("quantity_unit must be base or contracts")
        if self.mapping_method not in {"unresolved", "explicit", "verified_override", "contract_address_match", "exchange_asset_id"}:
            raise ValueError("unsupported mapping_method")
        if (self.canonical_asset_id is None) != (self.mapping_method == "unresolved"):
            raise ValueError("canonical identity requires an explicit mapping method")

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.exchange, self.market, self.instrument_id)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class IdentityRegistry:
    """Small deterministic registry of explicitly supplied mappings, with no lookup I/O."""

    def __init__(self, identities: Iterable[InstrumentIdentity] = ()) -> None:
        records: dict[tuple[str, str, str], InstrumentIdentity] = {}
        for identity in identities:
            if not isinstance(identity, InstrumentIdentity):
                raise ValueError("identity must be InstrumentIdentity")
            if identity.key in records and records[identity.key] != identity:
                raise ValueError("conflicting identity for the same instrument")
            records[identity.key] = identity
        self._records = records

    def resolve(self, exchange: str, market: str, instrument_id: str) -> InstrumentIdentity | None:
        return self._records.get((exchange, market, instrument_id))

    def snapshot(self) -> tuple[InstrumentIdentity, ...]:
        return tuple(self._records[key] for key in sorted(self._records))
