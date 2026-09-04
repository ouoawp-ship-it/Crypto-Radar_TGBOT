"""Virtual-clock public REST scheduling and bounded OI sampling, with no I/O."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from email.utils import parsedate_to_datetime
import hashlib
from typing import Callable, Mapping

from .adapters.base import identifier
from .models import strict_int
from .rest_budget import BudgetDecision, PRIORITIES, RateBudget, RequestSpec, make_request


@dataclass(frozen=True, slots=True)
class Completion:
    request: RequestSpec
    accepted: bool
    retry_scheduled: bool
    reason: str


def _retry_after_ms(headers: Mapping[str, str], now_ms: int) -> int:
    raw = next((value for key, value in headers.items() if key.lower() == "retry-after"), None)
    if not isinstance(raw, str) or len(raw) > 128:
        return 0
    try:
        seconds = Decimal(raw)
        if seconds.is_finite() and 0 <= seconds <= Decimal(2**50):
            return int((seconds * 1000).to_integral_value(rounding=ROUND_CEILING))
    except InvalidOperation:
        pass
    try:
        value = parsedate_to_datetime(raw)
        if value.tzinfo is not None:
            return max(0, int(value.timestamp() * 1000) - now_ms)
    except (ValueError, TypeError, OverflowError):
        pass
    return 0


class RestScheduler:
    """Return admitted specs for an injected harness; never execute a request.

    Fair dispatch permits at most three HIGH admissions before one waiting
    NORMAL admission, provided the shared coordinator grants the budget.
    Queue and identity history are bounded. Generations may never be reused;
    this makes cancelled, expired and superseded responses rejectable without
    retaining an unbounded set of completed request IDs.
    """
    def __init__(self, *, clock: Callable[[], int], coordinator: RateBudget | None,
                 live: bool = False, max_queue: int = 4096, max_inflight: int = 16,
                 max_identities: int = 4096, max_attempts: int = 3,
                 timeout_ms: int = 5000, base_backoff_ms: int = 1000,
                 max_backoff_ms: int = 30_000,
                 jitter: Callable[[RequestSpec, int], int] | None = None) -> None:
        if not callable(clock):
            raise ValueError("explicit_virtual_clock_required")
        if type(live) is not bool:
            raise ValueError("invalid_live_flag")
        if live and (coordinator is None or not getattr(coordinator, "live_capable", False)):
            raise ValueError("live_requires_shared_production_coordinator")
        for name, value in (("max_queue", max_queue), ("max_inflight", max_inflight),
                            ("max_identities", max_identities), ("max_attempts", max_attempts),
                            ("timeout_ms", timeout_ms), ("base_backoff_ms", base_backoff_ms),
                            ("max_backoff_ms", max_backoff_ms)):
            strict_int(value, name, minimum=1)
        if max_attempts > 10 or base_backoff_ms > max_backoff_ms:
            raise ValueError("invalid_retry_policy")
        self.clock, self.coordinator = clock, coordinator
        self.max_queue, self.max_inflight = max_queue, max_inflight
        self.max_identities, self.max_attempts = max_identities, max_attempts
        self.timeout_ms, self.base_backoff_ms = timeout_ms, base_backoff_ms
        self.max_backoff_ms = max_backoff_ms
        self.jitter = jitter or (lambda request, delay: int(hashlib.sha256(request.request_id.encode()).hexdigest()[:8], 16) % 251)
        self._queued: dict[str, RequestSpec] = {}
        self._inflight: dict[str, tuple[RequestSpec, int]] = {}
        self._latest: dict[tuple[str, str | None], int] = {}
        self._counts: Counter[str] = Counter()
        self._high_streak = 0
        self._last_now = 0
        self._source_until = 0
        self._budget_trusted = coordinator is not None
        self._live = live

    def _now(self, now_ms: int | None) -> int:
        value = self.clock() if now_ms is None else now_ms
        strict_int(value, "now_ms")
        if value < self._last_now:
            raise ValueError("scheduler_time_regression")
        self._last_now = value
        return value

    def submit(self, request: RequestSpec) -> bool:
        if not isinstance(request, RequestSpec):
            raise ValueError("request_spec_required")
        now = self._now(None)
        if request.retry_count or request.deadline_ms <= now:
            self._counts["stale"] += 1
            return False
        if request.request_id in self._queued or request.request_id in self._inflight:
            self._counts["dropped"] += 1
            return False
        latest = self._latest.get(request.key)
        if latest is not None and request.generation <= latest:
            self._counts["stale"] += 1
            return False
        if request.key not in self._latest and len(self._latest) >= self.max_identities:
            self._counts["dropped"] += 1
            return False
        replaced = [item.request_id for item in self._queued.values() if item.key == request.key]
        replaced += [item.request_id for item, _at in self._inflight.values() if item.key == request.key]
        if len(self._queued) + len(self._inflight) - len(replaced) >= self.max_queue:
            self._counts["dropped"] += 1
            return False
        for request_id in replaced:
            self.cancel(request_id)
        self._latest[request.key] = request.generation
        self._queued[request.request_id] = request
        self._counts["submitted"] += 1
        return True

    def cancel(self, request_id: str) -> bool:
        found = self._queued.pop(request_id, None) is not None
        found = self._inflight.pop(request_id, None) is not None or found
        if found:
            self._counts["cancelled"] += 1
        return found

    def contains(self, request_id: str) -> bool:
        return request_id in self._queued or request_id in self._inflight

    def restore_budget(self, coordinator: RateBudget) -> None:
        """Explicit harness action after reconciling shared-IP budget state.

        No automatic reset or replacement coordinator is ever created. Existing
        local source cooldown survives recovery and is copied to the replacement.
        """
        if coordinator is None or not all(callable(getattr(coordinator, name, None))
                                          for name in ("reserve", "observe", "cooldown")):
            raise ValueError("explicit_coordinator_required")
        if self._live and not getattr(coordinator, "live_capable", False):
            raise ValueError("live_requires_shared_production_coordinator")
        self._budget_trusted = False
        if self._source_until:
            coordinator.cooldown(self._source_until, reason="preserved_source_cooldown")
        self.coordinator = coordinator
        self._budget_trusted = True

    def poll_due(self, now_ms: int | None = None, *, limit: int = 16) -> tuple[RequestSpec, ...]:
        now = self._now(now_ms)
        strict_int(limit, "limit", minimum=1)
        for request, started in tuple(self._inflight.values()):
            if now >= min(request.deadline_ms, started + self.timeout_ms):
                self.complete(request, status_code=None, response_time_ms=now, timed_out=True)
        for request in tuple(self._queued.values()):
            if now >= request.deadline_ms:
                del self._queued[request.request_id]
                self._counts["stale"] += 1
        eligible = [item for item in self._queued.values() if item.not_before_ms <= now]
        high = sorted((item for item in eligible if item.priority != "NORMAL"),
                      key=lambda item: (item.scheduled_at_ms, item.request_id))
        normal = sorted((item for item in eligible if item.priority == "NORMAL"),
                        key=lambda item: (item.scheduled_at_ms, item.request_id))
        due: list[RequestSpec] = []
        capacity = min(limit, self.max_inflight - len(self._inflight))
        while (high or normal) and len(due) < capacity:
            queue = high if high and (self._high_streak < 3 or not normal) else normal
            request = queue.pop(0)
            if now < self._source_until or self.coordinator is None or not self._budget_trusted:
                self._counts["budget_blocked"] += 1
                continue
            try:
                decision = self.coordinator.reserve(request, now)
                if not isinstance(decision, BudgetDecision):
                    raise ValueError("invalid_coordinator_decision")
            except Exception:
                self._budget_trusted = False
                self._counts["budget_blocked"] += 1
                continue  # Shared budget unavailable: fail closed.
            if not decision.allowed:
                self._counts["budget_blocked"] += 1
                retry_at = max(now + 1, decision.retry_at_ms)
                if retry_at >= request.deadline_ms:
                    del self._queued[request.request_id]
                    self._counts["dropped"] += 1
                else:
                    self._queued[request.request_id] = replace(request, not_before_ms=retry_at)
                continue
            del self._queued[request.request_id]
            self._inflight[request.request_id] = request, now
            self._high_streak = min(3, self._high_streak + 1) if request.priority != "NORMAL" else 0
            due.append(request)
            self._counts["due"] += 1
            if request.retry_count == 0:
                self._counts["unique_admitted"] += 1
        return tuple(due)

    def complete(self, request: RequestSpec, *, status_code: int | None,
                 response_time_ms: int, headers: Mapping[str, str] | None = None,
                 timed_out: bool = False) -> Completion:
        now = self._now(response_time_ms)
        if status_code is not None:
            strict_int(status_code, "status_code", minimum=100, maximum=599)
        if type(timed_out) is not bool:
            raise ValueError("invalid_timeout_flag")
        headers = {} if headers is None else headers
        if (not isinstance(headers, Mapping) or len(headers) > 64
                or any(type(key) is not str or type(value) is not str or not key
                       or len(key) > 128 or len(value) > 4096
                       or any(ord(character) < 32 or ord(character) == 127 for character in key + value)
                       for key, value in headers.items())
                or len({key.lower() for key in headers}) != len(headers)):
            raise ValueError("invalid_response_headers")
        # Even a cancelled response can convey an IP-wide ban. Its payload is
        # still stale, but ignoring its rate-limit feedback would be unsafe.
        server_delay = _retry_after_ms(headers, now)
        if status_code in (418, 429):
            minimum = 120_000 if status_code == 418 else self.base_backoff_ms
            self._source_until = max(self._source_until, now + max(minimum, server_delay))
            if self.coordinator is not None:
                try:
                    self.coordinator.cooldown(self._source_until, reason="http_" + str(status_code))
                except Exception:
                    self._budget_trusted = False
                    self._counts["budget_feedback_failed"] += 1
        if self.coordinator is not None:
            try:
                self.coordinator.observe(request, headers, now)
            except Exception:
                self._budget_trusted = False
                self._counts["budget_feedback_failed"] += 1
        active = self._inflight.get(request.request_id)
        if active is None or active[0] != request or self._latest.get(request.key) != request.generation:
            self._counts["stale"] += 1
            return Completion(request, False, False, "stale_response")
        del self._inflight[request.request_id]
        if now >= request.deadline_ms:
            self._counts["stale"] += 1
            return Completion(request, False, False, "deadline_expired")
        if not timed_out and status_code is not None and 200 <= status_code < 300:
            self._counts["completed"] += 1
            return Completion(request, True, False, "completed")
        retryable = timed_out or status_code in (408, 418, 429) or (status_code is not None and status_code >= 500)
        self._counts["timeouts" if timed_out or status_code == 408 else "failed"] += 1
        if not retryable or request.retry_count + 1 >= self.max_attempts:
            self._counts["dropped"] += 1
            return Completion(request, False, False, "retry_exhausted" if retryable else "nonretryable_status")
        base = min(self.max_backoff_ms, self.base_backoff_ms * 2**request.retry_count)
        jitter = self.jitter(request, base)
        strict_int(jitter, "jitter_ms", maximum=self.max_backoff_ms)
        backoff = min(self.max_backoff_ms, base + jitter)
        retry_at = max(now + backoff, now + server_delay, self._source_until)
        if retry_at >= request.deadline_ms:
            self._counts["dropped"] += 1
            return Completion(request, False, False, "retry_after_deadline")
        self._queued[request.request_id] = replace(request, retry_count=request.retry_count + 1, not_before_ms=retry_at)
        self._counts["retries"] += 1
        return Completion(request, False, True, "retry_scheduled")

    def diagnostics(self, now_ms: int | None = None) -> dict:
        now = self._now(now_ms)
        queued = tuple(self._queued.values())
        active = (*queued, *(request for request, _at in self._inflight.values()))
        counters = {name: self._counts[name] for name in (
            "submitted", "due", "dropped", "stale", "budget_blocked", "completed",
            "failed", "timeouts", "retries", "cancelled", "budget_feedback_failed")}
        return {**counters, "queue_depth": len(queued), "inflight": len(self._inflight),
                "coverage": self._counts["unique_admitted"] / self._counts["submitted"] if self._counts["submitted"] else None,
                "coverage_numerator": self._counts["unique_admitted"],
                "coverage_denominator": self._counts["submitted"],
                "coverage_basis": "unique_admitted_requests/submitted_requests",
                "delayed": sum(request.not_before_ms > now for request in queued),
                "oldest_age_ms": max((now - request.scheduled_at_ms for request in active
                                      if request.scheduled_at_ms <= now), default=None),
                "source_cooldown_until_ms": self._source_until,
                "identity_count": len(self._latest), "coordinator_configured": self.coordinator is not None,
                "budget_trusted": self._budget_trusted}


class OiSamplingPlanner:
    """NORMAL 300s, selected HOT/HUNTER/EXTREME 60s; excess HIGH stays 300s.

    Coverage uses the requested tier's freshness target. Overflow is therefore
    visible as degraded coverage instead of silently redefining success.
    Last-good data survives failure; missing ages are None, never zero.
    """
    def __init__(self, *, max_instruments: int = 1000, high_cap: int = 80) -> None:
        strict_int(max_instruments, "max_instruments", minimum=1)
        strict_int(high_cap, "high_cap", minimum=1, maximum=80)
        self.max_instruments, self.high_cap = max_instruments, high_cap
        self._tiers: dict[str, str] = {}
        self._selected: set[str] = set()
        self._next_due: dict[str, int] = {}
        self._last_good: dict[str, int] = {}
        self._pending: dict[str, str] = {}
        self._generation = 0
        self._failures = 0

    def update_universe(self, instruments: Mapping[str, str], now_ms: int) -> None:
        strict_int(now_ms, "now_ms")
        if not isinstance(instruments, Mapping) or len(instruments) > self.max_instruments:
            raise ValueError("oi_universe_capacity_exceeded")
        for instrument, tier in instruments.items():
            identifier(instrument, "instrument_id")
            if type(tier) is not str or tier not in PRIORITIES:
                raise ValueError("invalid_oi_tier")
        tiers = dict(instruments)
        ranking = {"EXTREME": 0, "HUNTER": 1, "HOT": 2}
        selected = set(sorted((key for key in tiers if tiers[key] != "NORMAL"),
                              key=lambda key: (ranking[tiers[key]], key))[:self.high_cap])
        self._next_due = {key: min(self._next_due.get(key, now_ms), now_ms) if key in selected and key not in self._selected
                          else self._next_due.get(key, now_ms) for key in tiers}
        self._last_good = {key: value for key, value in self._last_good.items() if key in tiers}
        self._pending = {key: value for key, value in self._pending.items() if key in tiers}
        self._tiers, self._selected = tiers, selected

    def schedule(self, scheduler: RestScheduler, now_ms: int) -> tuple[RequestSpec, ...]:
        strict_int(now_ms, "now_ms")
        emitted = []
        for instrument in sorted(self._tiers, key=lambda key: (self._next_due[key], key)):
            pending = self._pending.get(instrument)
            if pending is not None and scheduler.contains(pending):
                continue
            if now_ms < self._next_due[instrument]:
                continue
            self._generation += 1
            selected = instrument in self._selected
            interval = 60_000 if selected else 300_000
            request = make_request("openInterest", now_ms, instrument_id=instrument,
                                   priority=self._tiers[instrument] if selected else "NORMAL",
                                   ttl_ms=interval, generation=self._generation)
            if scheduler.submit(request):
                self._pending[instrument] = request.request_id
                self._next_due[instrument] = now_ms + interval
                emitted.append(request)
        return tuple(emitted)

    def record_result(self, instrument_id: str, event_time_ms: int | None, now_ms: int, *, success: bool = True) -> bool:
        strict_int(now_ms, "now_ms")
        identifier(instrument_id, "instrument_id")
        if type(success) is not bool or instrument_id not in self._tiers:
            raise ValueError("unknown_oi_result")
        if not success:
            self._failures += 1
            return False
        strict_int(event_time_ms, "event_time_ms")
        if event_time_ms > now_ms or event_time_ms < self._last_good.get(instrument_id, 0):
            self._failures += 1
            return False
        self._last_good[instrument_id] = event_time_ms
        return True

    def coverage(self, now_ms: int) -> dict:
        strict_int(now_ms, "now_ms")
        ages = {key: (now_ms - self._last_good[key] if key in self._last_good and self._last_good[key] <= now_ms else None)
                for key in self._tiers}
        covered = sum(age is not None and age < (300_000 if self._tiers[key] == "NORMAL" else 60_000)
                      for key, age in ages.items())
        requested_high = sum(tier != "NORMAL" for tier in self._tiers.values())
        known = [age for age in ages.values() if age is not None]
        overflow = max(0, requested_high - len(self._selected))
        return {"instruments": len(ages), "covered_instruments": covered,
                "coverage": covered / len(ages) if ages else None,
                "oldest_age_ms": max(known, default=None), "missing_instruments": len(ages) - len(known),
                "high_requested": requested_high, "high_selected": len(self._selected),
                "high_overflow": overflow, "high_selection_coverage": len(self._selected) / requested_high if requested_high else None,
                "degraded": bool(overflow or covered < len(ages)), "oi_failures": self._failures,
                "normal_interval_ms": 300_000, "high_interval_ms": 60_000}
