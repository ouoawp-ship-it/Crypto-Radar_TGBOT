"""Pure public-REST contracts and an explicitly OFFLINE shared-budget fake.

Weights: Binance USD-M market-data reference, verified 2026-09-04.
https://developers.binance.com/en/docs/catalog/core-trading-derivatives-trading-usd-s-m-futures/api/rest-api/market-data
The fake is not the production coordinator and never opens a socket or file.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Mapping, Protocol

from .adapters.base import identifier
from .models import strict_int

ENDPOINTS = {
    "exchangeInfo": ("/fapi/v1/exchangeInfo", 1, "request_weight"),
    "serverTime": ("/fapi/v1/time", 1, "request_weight"),
    "fundingInfo": ("/fapi/v1/fundingInfo", 0, "funding_requests"),
    "openInterest": ("/fapi/v1/openInterest", 1, "request_weight"),
    "bookTicker": ("/fapi/v1/ticker/bookTicker", 2, "request_weight"),
}
PRIORITIES = frozenset({"NORMAL", "HOT", "HUNTER", "EXTREME"})


@dataclass(frozen=True, slots=True)
class RequestSpec:
    endpoint: str
    logical_weight: int
    scheduled_at_ms: int
    deadline_ms: int
    priority: str
    instrument_id: str | None
    retry_count: int
    not_before_ms: int
    budget_class: str
    request_id: str
    generation: int = 0
    method: str = "GET"

    def __post_init__(self) -> None:
        for name in ("endpoint", "priority", "budget_class", "request_id", "method"):
            identifier(getattr(self, name), name)
        rules = {value[0]: value for value in ENDPOINTS.values()}
        if self.endpoint not in rules or self.method != "GET":
            raise ValueError("unsupported_public_rest_endpoint")
        _path, weight, budget = rules[self.endpoint]
        if self.endpoint.endswith("bookTicker") and self.instrument_id is None:
            weight = 5
        strict_int(self.logical_weight, "logical_weight")
        if self.logical_weight != weight or self.budget_class != budget:
            raise ValueError("endpoint_budget_mismatch")
        for name in ("scheduled_at_ms", "deadline_ms", "retry_count", "not_before_ms", "generation"):
            strict_int(getattr(self, name), name)
        if not self.scheduled_at_ms <= self.not_before_ms <= self.deadline_ms:
            raise ValueError("invalid_request_time_bounds")
        if self.priority not in PRIORITIES:
            raise ValueError("invalid_request_priority")
        if self.instrument_id is not None:
            identifier(self.instrument_id, "instrument_id")
        if self.endpoint.endswith("openInterest") and self.instrument_id is None:
            raise ValueError("open_interest_requires_instrument")

    @property
    def key(self) -> tuple[str, str | None]:
        return self.endpoint, self.instrument_id


def make_request(endpoint: str, now_ms: int, *, instrument_id: str | None = None,
                 priority: str = "NORMAL", ttl_ms: int = 15_000, generation: int = 0) -> RequestSpec:
    strict_int(now_ms, "now_ms")
    strict_int(ttl_ms, "ttl_ms", minimum=1)
    identifier(endpoint, "endpoint")
    rules = ENDPOINTS.get(endpoint)
    if rules is None:
        rules = next((rule for rule in ENDPOINTS.values() if rule[0] == endpoint), None)
    if rules is None:
        raise ValueError("unsupported_public_rest_endpoint")
    path, weight, budget = rules
    if path.endswith("bookTicker") and instrument_id is None:
        weight = 5
    request_id = hashlib.sha256(repr((path, instrument_id, now_ms, generation)).encode()).hexdigest()
    return RequestSpec(path, weight, now_ms, now_ms + ttl_ms, priority, instrument_id,
                       0, now_ms, budget, request_id, generation)


@dataclass(frozen=True, slots=True)
class BudgetDecision:
    allowed: bool
    retry_at_ms: int
    reason: str

    def __post_init__(self) -> None:
        if type(self.allowed) is not bool:
            raise ValueError("invalid_budget_decision")
        strict_int(self.retry_at_ms, "retry_at_ms")
        identifier(self.reason, "reason")


class RateBudget(Protocol):
    """Production implementation must coordinate all users of the same IP.

    reserve atomically charges an attempt, including failed requests. observe
    conservatively incorporates exchange feedback; cooldown applies IP-wide.
    """
    live_capable: bool

    def reserve(self, request: RequestSpec, now_ms: int) -> BudgetDecision: ...
    def observe(self, request: RequestSpec, headers: Mapping[str, str], now_ms: int) -> None: ...
    def cooldown(self, until_ms: int, *, reason: str) -> None: ...


class FakeCoordinator:
    """Deterministic in-memory stand-in, shared by injecting the same object.

    Limits are simulation policy, not evidence of production headroom. Funding
    consumes one of 500 requests / 5 minutes even though its IP weight is zero.
    Reserved weight prevents NORMAL requests from consuming all HOT capacity.
    """
    live_capable = False

    def __init__(self, *, weight_limit: int = 2400, high_reserve: int = 80,
                 funding_limit: int = 500) -> None:
        strict_int(weight_limit, "weight_limit", minimum=1)
        strict_int(high_reserve, "high_reserve", maximum=weight_limit - 1)
        strict_int(funding_limit, "funding_limit", minimum=1, maximum=500)
        self.weight_limit = weight_limit
        self.high_reserve = high_reserve
        self.funding_limit = funding_limit
        self._windows = {"request_weight": (-1, 0), "funding_requests": (-1, 0)}
        self._normal_used = 0
        self._cooldown_until = 0
        self._last_now = 0

    def _advance(self, now_ms: int) -> None:
        strict_int(now_ms, "now_ms")
        if now_ms < self._last_now:
            raise ValueError("budget_time_regression")
        self._last_now = now_ms
        for budget, period in (("request_weight", 60_000), ("funding_requests", 300_000)):
            window = now_ms // period
            if self._windows[budget][0] != window:
                self._windows[budget] = window, 0
                if budget == "request_weight":
                    self._normal_used = 0

    def reserve(self, request: RequestSpec, now_ms: int) -> BudgetDecision:
        self._advance(now_ms)
        if now_ms < self._cooldown_until:
            return BudgetDecision(False, self._cooldown_until, "source_cooldown")
        budget = request.budget_class
        period = 300_000 if budget == "funding_requests" else 60_000
        limit = self.funding_limit if budget == "funding_requests" else self.weight_limit
        cost = 1 if budget == "funding_requests" else request.logical_weight
        window, used = self._windows[budget]
        reserved = (budget == "request_weight" and request.priority == "NORMAL"
                    and self._normal_used + cost > limit - self.high_reserve)
        if used + cost > limit or reserved:
            return BudgetDecision(False, (window + 1) * period, "high_reserve" if reserved else "budget_exhausted")
        self._windows[budget] = window, used + cost
        if budget == "request_weight" and request.priority == "NORMAL":
            self._normal_used += cost
        return BudgetDecision(True, now_ms, "admitted")

    def observe(self, request: RequestSpec, headers: Mapping[str, str], now_ms: int) -> None:
        self._advance(now_ms)
        # Binance explicitly marks this endpoint's used-weight header inaccurate.
        if request.endpoint.endswith("bookTicker"):
            return
        value = next((value for key, value in headers.items() if key.lower() == "x-mbx-used-weight-1m"), None)
        if isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 12:
            window, used = self._windows["request_weight"]
            self._windows["request_weight"] = window, max(used, int(value))

    def cooldown(self, until_ms: int, *, reason: str) -> None:
        strict_int(until_ms, "until_ms")
        identifier(reason, "reason")
        self._cooldown_until = max(self._cooldown_until, until_ms)

    def diagnostics(self, now_ms: int) -> dict:
        self._advance(now_ms)
        return {"offline_only": True, "weight_used": self._windows["request_weight"][1],
                "funding_requests_used": self._windows["funding_requests"][1],
                "source_cooldown_until_ms": self._cooldown_until,
                "normal_weight_used": self._normal_used, "high_reserve": self.high_reserve}
