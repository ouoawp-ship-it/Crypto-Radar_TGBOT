"""Bounded, dependency-free contracts shared by offline adapter simulations."""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
from types import MappingProxyType
from typing import Any, Mapping, Protocol
import unicodedata

from ..models import MarketEvent, event_to_dict, strict_int, timestamp_ms

PROTOCOL_VERSION = "binance-usdm-2026-09-04-p1bi-v1"


class Route(str, Enum):
    MARKET = "MARKET"
    PUBLIC = "PUBLIC"

    @property
    def path(self) -> str:
        return f"/{self.value.lower()}/stream"


def identifier(value: Any, name: str = "identity", *, maximum: int = 128) -> str:
    if (type(value) is not str or not value or value != value.strip()
            or len(value) > maximum
            or any(unicodedata.category(char).startswith("C") for char in value)):
        raise ValueError(f"invalid_{name}")
    return value


def plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(item) for item in value]
    return value


def deterministic_digest(value: Any) -> str:
    encoded = json.dumps(plain(value), ensure_ascii=True, sort_keys=True,
                         separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class ParseLimits:
    max_items: int = 2048
    max_depth: int = 8
    max_string_length: int = 4096
    max_payload_bytes: int = 1_000_000
    max_rejected_items: int = 64

    def __post_init__(self) -> None:
        for name, ceiling in (("max_items", 16384), ("max_depth", 32),
                              ("max_string_length", 65536), ("max_payload_bytes", 8_000_000),
                              ("max_rejected_items", 1024)):
            strict_int(getattr(self, name), name, minimum=1, maximum=ceiling)


@dataclass(frozen=True)
class RejectedItem:
    index: int
    reason: str
    details: str = ""

    def __post_init__(self) -> None:
        strict_int(self.index, "index", minimum=-1)
        identifier(self.reason, "reason", maximum=96)
        # Details describe protocol fields, never raw response values or headers.
        if type(self.details) is not str or len(self.details.encode("utf-8")) > 256:
            raise ValueError("invalid_rejection_details")

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "reason": self.reason, "details": self.details}


RejectedPayload = RejectedItem


@dataclass(frozen=True)
class ParseResult:
    events: tuple[MarketEvent, ...] = ()
    rejected_items: tuple[RejectedItem, ...] = ()
    diagnostics: Mapping[str, int] = field(default_factory=dict)
    event_metadata: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "events", tuple(self.events))
        object.__setattr__(self, "rejected_items", tuple(self.rejected_items))
        object.__setattr__(self, "diagnostics", MappingProxyType(dict(self.diagnostics)))
        object.__setattr__(self, "event_metadata", tuple(MappingProxyType(dict(item))
                                                       for item in self.event_metadata))
        if self.event_metadata and len(self.events) != len(self.event_metadata):
            raise ValueError("event_metadata_misaligned")

    def to_dict(self) -> dict[str, Any]:
        return {"events": [event_to_dict(event) for event in self.events],
                "rejected_items": [item.to_dict() for item in self.rejected_items],
                "diagnostics": dict(self.diagnostics), "event_metadata": plain(self.event_metadata)}


class BoundedDiagnostics:
    """Bounded diagnostic samples; aggregate counters survive sample eviction.

    Caller text is deliberately not retained: the sample is a safe shape summary
    (UTF-8 byte length) plus a stable reason code. No raw headers or URLs enter it.
    """

    def __init__(self, *, max_samples: int = 128, samples_per_reason_minute: int = 3,
                 max_reasons: int = 128, max_summary_bytes: int = 256) -> None:
        for name, value in (("max_samples", max_samples),
                            ("samples_per_reason_minute", samples_per_reason_minute),
                            ("max_reasons", max_reasons), ("max_summary_bytes", max_summary_bytes)):
            strict_int(value, name, minimum=1, maximum=65536)
        self.max_samples = max_samples
        self.samples_per_reason_minute = samples_per_reason_minute
        self.max_reasons = max_reasons
        self.max_summary_bytes = max_summary_bytes
        self.counters: Counter[str] = Counter()
        self._samples: deque[dict[str, Any]] = deque(maxlen=max_samples)
        self._minute: int | None = None
        self._sample_counts: Counter[str] = Counter()
        self.last_error_time_ms: int | None = None
        self.suppressed_samples = 0

    def record(self, reason: str, *, observed_at_ms: int, detail: str = "", amount: int = 1) -> None:
        identifier(reason, "reason", maximum=96)
        strict_int(observed_at_ms, "observed_at_ms")
        strict_int(amount, "amount", minimum=1)
        if type(detail) is not str:
            raise ValueError("invalid_diagnostic_detail")
        if reason not in self.counters and len(self.counters) >= self.max_reasons - 1:
            reason = "other_reason"
        self.counters[reason] += amount
        self.last_error_time_ms = max(self.last_error_time_ms or 0, observed_at_ms)
        minute = observed_at_ms // 60000
        if self._minute is None or minute > self._minute:
            self._minute = minute
            self._sample_counts.clear()
        if minute != self._minute or self._sample_counts[reason] >= self.samples_per_reason_minute:
            self.suppressed_samples += amount
            return
        self._sample_counts[reason] += 1
        summary = f"detail_bytes={len(detail.encode('utf-8'))}".encode("utf-8")[:self.max_summary_bytes].decode("utf-8", "ignore")
        self._samples.append({"reason": reason, "observed_at_ms": observed_at_ms, "summary": summary})

    def snapshot(self) -> dict[str, Any]:
        return {"counters": dict(sorted(self.counters.items())), "samples": [dict(item) for item in self._samples],
                "last_error_time_ms": self.last_error_time_ms,
                "suppressed_samples": self.suppressed_samples, "capacity": self.max_samples,
                "samples_per_reason_minute": self.samples_per_reason_minute}


class ParseDiagnostics(BoundedDiagnostics):
    pass


class ConnectionDiagnostics(BoundedDiagnostics):
    pass


class SubscriptionDiagnostics(BoundedDiagnostics):
    pass


class RestBudgetDiagnostics(BoundedDiagnostics):
    pass


class Transport(Protocol):
    def open(self, *, route: Route, shard_id: str, epoch: int) -> None: ...
    def send(self, message: Mapping[str, Any]) -> None: ...
    def close(self, *, reason: str) -> None: ...


TransportProtocol = Transport


class FakeTransport:
    """Synchronous action recorder; has no network implementation."""

    network_calls = 0

    def __init__(self, *, max_actions: int = 4096) -> None:
        strict_int(max_actions, "max_actions", minimum=1, maximum=65536)
        self.max_actions = max_actions
        self.actions: list[dict[str, Any]] = []
        self.dropped_actions = 0
        self.opened = False

    def _record(self, action: dict[str, Any]) -> None:
        if len(self.actions) == self.max_actions:
            self.actions.pop(0)
            self.dropped_actions += 1
        self.actions.append(action)

    def open(self, *, route: Route, shard_id: str, epoch: int) -> None:
        identifier(shard_id, "shard_id")
        strict_int(epoch, "epoch")
        self.opened = True
        self._record({"action": "open", "route": Route(route).value, "shard_id": shard_id, "epoch": epoch})

    def send(self, message: Mapping[str, Any]) -> None:
        if not self.opened:
            raise ValueError("transport_not_open")
        self._record({"action": "send", "message": plain(message)})

    def close(self, *, reason: str) -> None:
        identifier(reason, "reason")
        self.opened = False
        self._record({"action": "close", "reason": reason})

    def drain_actions(self) -> tuple[dict[str, Any], ...]:
        result = tuple(self.actions)
        self.actions.clear()
        return result
