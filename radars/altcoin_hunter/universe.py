"""Offline, orthogonal instrument registry and change-only history."""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, replace
from enum import Enum
from typing import Any, Iterable, Mapping

from .identity import InstrumentIdentity
from .models import bounded_text, strict_int, timestamp_ms


class EligibilityStatus(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    INELIGIBLE = "INELIGIBLE"
    BLACKLISTED = "BLACKLISTED"


class ListingStage(str, Enum):
    NEW_LISTING = "NEW_LISTING"
    MATURE = "MATURE"
    DELISTING = "DELISTING"
    UNKNOWN = "UNKNOWN"


class ActivityTier(str, Enum):
    NORMAL = "NORMAL"
    HOT = "HOT"
    HUNTER = "HUNTER"
    EXTREME = "EXTREME"


class SamplingPriority(str, Enum):
    BASE = "BASE"
    ELEVATED = "ELEVATED"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True, kw_only=True)
class Instrument:
    exchange: str
    market: str
    instrument_id: str
    symbol: str
    exchange_symbol: str
    source: str
    effective_at_ms: int
    canonical_asset_id: str | None = None
    contract_multiplier: str = "1"
    quantity_unit: str = "base"
    quote_currency: str = "USDT"
    mapping_method: str = "unresolved"
    eligibility_status: EligibilityStatus = EligibilityStatus.INELIGIBLE
    listing_stage: ListingStage = ListingStage.UNKNOWN
    activity_tier: ActivityTier = ActivityTier.NORMAL
    sampling_priority: SamplingPriority = SamplingPriority.BASE
    reason_codes: tuple[str, ...] = ()
    metadata_version: int = 1
    data_quality: str = "insufficient"

    def __post_init__(self) -> None:
        InstrumentIdentity(**{name: getattr(self, name) for name in (
            "exchange", "market", "instrument_id", "symbol", "exchange_symbol",
            "canonical_asset_id", "contract_multiplier", "quantity_unit", "quote_currency", "mapping_method",
        )})
        bounded_text(self.source, "source")
        timestamp_ms(self.effective_at_ms, "effective_at_ms")
        strict_int(self.metadata_version, "metadata_version", minimum=1, maximum=1000000)
        for name, enum in (
            ("eligibility_status", EligibilityStatus), ("listing_stage", ListingStage),
            ("activity_tier", ActivityTier), ("sampling_priority", SamplingPriority),
        ):
            try:
                object.__setattr__(self, name, enum(getattr(self, name)))
            except (ValueError, TypeError) as exc:
                raise ValueError(f"invalid {name}") from exc
        if self.data_quality not in {"complete", "partial", "insufficient", "stale", "invalid"}:
            raise ValueError("invalid data_quality")
        if not isinstance(self.reason_codes, tuple) or len(self.reason_codes) > 32:
            raise ValueError("reason_codes must be a bounded tuple")
        for reason in self.reason_codes:
            bounded_text(reason, "reason_code", limit=64)
        object.__setattr__(self, "reason_codes", tuple(sorted(set(self.reason_codes))))
        if self.listing_stage == ListingStage.DELISTING and self.eligibility_status == EligibilityStatus.ELIGIBLE:
            raise ValueError("a delisting instrument cannot be eligible")

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.exchange, self.market, self.instrument_id)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        for name in ("eligibility_status", "listing_stage", "activity_tier", "sampling_priority"):
            result[name] = getattr(self, name).value
        result["reason_codes"] = list(self.reason_codes)
        return result


def instrument_from_dict(data: Mapping[str, Any]) -> Instrument:
    if not isinstance(data, Mapping):
        raise ValueError("instrument must be an object")
    values = dict(data)
    reasons = values.get("reason_codes", ())
    if not isinstance(reasons, (list, tuple)):
        raise ValueError("reason_codes must be a sequence")
    values["reason_codes"] = tuple(reasons)
    try:
        return Instrument(**values)
    except TypeError as exc:
        raise ValueError("invalid instrument fields") from exc


@dataclass(frozen=True)
class UniverseChange:
    key: tuple[str, str, str]
    previous: Instrument | None
    current: Instrument
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": list(self.key), "previous": self.previous.to_dict() if self.previous else None,
            "current": self.current.to_dict(), "reason": self.reason,
        }


@dataclass(frozen=True)
class UniverseRefreshResult:
    accepted: bool
    reason: str
    changes: tuple[UniverseChange, ...]
    instruments: tuple[Instrument, ...]


def _semantic(record: Instrument) -> dict[str, Any]:
    result = record.to_dict()
    result.pop("effective_at_ms")
    return result


class UniverseRegistry:
    """Keep last good directory; absence alone never implies delisting.

    Only a complete healthy refresh is admitted. Explicit DELISTING records are
    retained for later reporting and replay rather than removed from history.
    The in-memory history is only a bounded recent cache. Every returned change
    can be persisted by the caller; the database owns the full durable history.
    """

    def __init__(self, instruments: Iterable[Instrument] = (), *, max_instruments: int = 4096,
                 max_history: int = 4096) -> None:
        self.max_instruments = strict_int(max_instruments, "max_instruments", minimum=1, maximum=100000)
        self.max_history = strict_int(max_history, "max_history", minimum=1, maximum=100000)
        self._records: dict[tuple[str, str, str], Instrument] = {}
        self._history: deque[UniverseChange] = deque(maxlen=self.max_history)
        self.history_truncated = 0
        self._last_refresh_ms: int | None = None
        for record in instruments:
            if not isinstance(record, Instrument) or record.key in self._records:
                raise ValueError("invalid or duplicate instrument")
            if len(self._records) >= self.max_instruments:
                raise ValueError("instrument capacity exceeded")
            self._records[record.key] = record

    def snapshot(self) -> tuple[Instrument, ...]:
        return tuple(self._records[key] for key in sorted(self._records))

    @property
    def history(self) -> tuple[UniverseChange, ...]:
        return tuple(self._history)

    def refresh(
        self, records: Iterable[Instrument | Mapping[str, Any]], *, observed_at_ms: int,
        complete: bool = True, source_healthy: bool = True,
    ) -> UniverseRefreshResult:
        timestamp_ms(observed_at_ms, "observed_at_ms")
        if type(complete) is not bool or type(source_healthy) is not bool:
            raise ValueError("refresh health flags must be booleans")
        if not complete or not source_healthy:
            return UniverseRefreshResult(False, "incomplete_directory" if not complete else "source_unavailable", (), self.snapshot())
        if self._last_refresh_ms is not None and observed_at_ms < self._last_refresh_ms:
            return UniverseRefreshResult(False, "stale_refresh", (), self.snapshot())
        prepared: dict[tuple[str, str, str], Instrument] = {}
        for value in records:
            record = value if isinstance(value, Instrument) else instrument_from_dict(value)
            if record.key in prepared:
                raise ValueError("duplicate instrument in directory refresh")
            if record.effective_at_ms > observed_at_ms:
                raise ValueError("instrument effective time is after observation")
            if len(prepared) >= self.max_instruments:
                raise ValueError("instrument capacity exceeded")
            prepared[record.key] = record
        if not prepared:
            return UniverseRefreshResult(False, "empty_directory", (), self.snapshot())
        if len(set(prepared) | set(self._records)) > self.max_instruments:
            raise ValueError("instrument capacity exceeded")
        changes: list[UniverseChange] = []
        for key in sorted(prepared):
            current = prepared[key]
            previous = self._records.get(key)
            if previous is not None and (current.metadata_version < previous.metadata_version or current.effective_at_ms < previous.effective_at_ms):
                return UniverseRefreshResult(False, "stale_instrument_metadata", (), self.snapshot())
            if previous is None or _semantic(previous) != _semantic(current):
                reason = "listed" if previous is None else "delisting" if current.listing_stage == ListingStage.DELISTING else "metadata_changed"
                changes.append(UniverseChange(key, previous, current, reason))
        for change in changes:
            self._records[change.key] = change.current
        self.history_truncated += max(0, len(self._history) + len(changes) - self.max_history)
        self._history.extend(changes)
        self._last_refresh_ms = observed_at_ms
        return UniverseRefreshResult(True, "accepted", tuple(changes), self.snapshot())

    def mark_delisting(self, key: tuple[str, str, str], *, effective_at_ms: int, reason_code: str = "exchange_delisting") -> UniverseRefreshResult:
        previous = self._records.get(key)
        if previous is None:
            raise ValueError("unknown instrument")
        current = replace(
            previous, listing_stage=ListingStage.DELISTING,
            eligibility_status=EligibilityStatus.INELIGIBLE,
            reason_codes=tuple(sorted(set((*previous.reason_codes, reason_code)))),
            effective_at_ms=effective_at_ms,
        )
        return self.refresh([current], observed_at_ms=effective_at_ms)
