"""Synchronous fake-transport connection supervision using explicit virtual time.

No network implementation, background task, wall clock, database, or service is
created here. Coverage describes observed simulation intervals, never evidence
that a production socket was connected. ACKs are scoped by request ID and epoch.
"""
from __future__ import annotations

from collections import Counter, OrderedDict, deque
from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import random
from typing import Any, Iterable, Mapping

from .adapters.base import (BoundedDiagnostics, EnvelopeLimits, FakeTransport,
                            PROTOCOL_VERSION, Route, Transport, validate_combined_envelope)
from .subscription_plan import ShardPlan, _integer, _route, plan_subscriptions, synthetic_instruments


class ConnectionState(str, Enum):
    STOPPED = "STOPPED"
    CONNECTING = "CONNECTING"
    SUBSCRIBING = "SUBSCRIBING"
    ACTIVE = "ACTIVE"
    DEGRADED = "DEGRADED"
    BACKOFF = "BACKOFF"
    RECYCLING = "RECYCLING"
    STOPPING = "STOPPING"


AckId = int | str


class AckIdStrategy(str, Enum):
    INTEGER = "INTEGER"
    STRING = "STRING"


def validate_ack_id(value: Any) -> AckId:
    """Preserve the JSON type; booleans are never integer request IDs."""
    if type(value) is int and 1 <= value <= 2**63 - 1:
        return value
    if (type(value) is str and 1 <= len(value) <= 64 and value.isascii()
            and all(33 <= ord(char) <= 126 for char in value)):
        return value
    raise ValueError("invalid_ack_id")


@dataclass(frozen=True, slots=True)
class PendingAck:
    request_id: AckId
    epoch: int
    method: str
    streams: tuple[str, ...]
    sent_at_ms: int
    expires_at_ms: int
    generation: int

    def __post_init__(self) -> None:
        validate_ack_id(self.request_id)
        _integer(self.epoch, "ack_epoch", 1, 2**63 - 1)
        _integer(self.generation, "ack_generation", 1, 2**63 - 1)
        if self.method not in {"SUBSCRIBE", "UNSUBSCRIBE"}:
            raise ValueError("invalid_ack_method")

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.request_id, "epoch": self.epoch, "method": self.method,
                "streams": list(self.streams), "sent_at_ms": self.sent_at_ms,
                "expires_at_ms": self.expires_at_ms, "generation": self.generation, "status": "PENDING"}


class ControlBudget:
    """8 tokens/second, also limited to 8 controls in any rolling second.

    The rolling guard prevents an initial token burst plus refill from violating
    Binance's 10 incoming messages/second limit. PONG uses the same budget.
    """
    def __init__(self) -> None:
        self.tokens_milli = 8000
        self.last_ms: int | None = None
        self.recent: deque[int] = deque()
        self.peak_per_second = 0

    def take(self, now_ms: int) -> bool:
        if self.last_ms is not None:
            self.tokens_milli = min(8000, self.tokens_milli + (now_ms - self.last_ms) * 8)
        self.last_ms = now_ms
        while self.recent and self.recent[0] <= now_ms - 1000:
            self.recent.popleft()
        if self.tokens_milli < 1000 or len(self.recent) >= 8:
            return False
        self.tokens_milli -= 1000
        self.recent.append(now_ms)
        self.peak_per_second = max(self.peak_per_second, len(self.recent))
        return True


class ConnectionSupervisor:
    """One route/shard, one explicitly injected FakeTransport, one owner."""
    def __init__(self, shard: ShardPlan, *, transport: Transport | None = None, seed: int = 0,
                 ack_timeout_ms: int = 5000, idle_timeout_ms: int = 240000,
                 pong_timeout_ms: int = 600000, connect_timeout_ms: int = 10000,
                 max_reconnect_attempts: int = 100, max_pending_acks: int = 128,
                 recycle_jitter_ms: int = 120000, stable_active_ms: int = 30000,
                 retiring_tombstone_ms: int = 2000, max_retiring_tombstones: int = 2048,
                 ack_id_strategy: AckIdStrategy | str = AckIdStrategy.INTEGER,
                 envelope_limits: EnvelopeLimits | None = None) -> None:
        if not isinstance(shard, ShardPlan) or not shard.streams:
            raise ValueError("nonempty_shard_required")
        self.transport = transport if transport is not None else FakeTransport()
        if not isinstance(self.transport, FakeTransport):
            raise ValueError("simulation_requires_fake_transport")
        self.shard = shard
        self.ack_timeout_ms = _integer(ack_timeout_ms, "ack_timeout", 1, 60000)
        self.idle_timeout_ms = _integer(idle_timeout_ms, "idle_timeout", 1, 600000)
        self.pong_timeout_ms = _integer(pong_timeout_ms, "pong_timeout", 1, 600000)
        self.connect_timeout_ms = _integer(connect_timeout_ms, "connect_timeout", 1, 60000)
        self.max_reconnect_attempts = _integer(max_reconnect_attempts, "reconnect_budget", 0, 1000)
        self.max_pending_acks = _integer(max_pending_acks, "pending_ack_capacity", 1, 128)
        self.recycle_jitter_ms = _integer(recycle_jitter_ms, "recycle_jitter", 0, 120000)
        self.stable_active_ms = _integer(stable_active_ms, "stable_active_window", 1, 600000)
        self.retiring_tombstone_ms = _integer(retiring_tombstone_ms, "retiring_tombstone_ttl", 1, 60000)
        self.max_retiring_tombstones = _integer(max_retiring_tombstones, "retiring_tombstone_capacity", 1, 4096)
        try:
            self.ack_id_strategy = AckIdStrategy(ack_id_strategy)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid_ack_id_strategy") from exc
        if envelope_limits is not None and not isinstance(envelope_limits, EnvelopeLimits):
            raise ValueError("invalid_envelope_limits")
        self.envelope_limits = envelope_limits or EnvelopeLimits()
        _integer(seed, "seed", 0, 2**63 - 1)
        self.rng = random.Random(seed)
        self.state = ConnectionState.STOPPED
        self.epoch = 0
        self.now_ms = 0
        self.desired_streams = {s.canonical_name for s in shard.streams}
        self.active_streams: set[str] = set()
        self.adding_streams: set[str] = set()
        self.retiring_streams: set[str] = set()
        self.acknowledged_streams: set[str] = set()
        self.transition_generation = 0
        self._retiring_tombstones: OrderedDict[str, tuple[int, int, int]] = OrderedDict()
        self.pending: dict[AckId, PendingAck] = {}
        self.ack_history: deque[dict[str, Any]] = deque(maxlen=256)
        self._acknowledged_ids: deque[PendingAck] = deque(maxlen=256)
        self._controls: deque[tuple[str, Any]] = deque()
        self._request_id = 0
        self._budget = ControlBudget()
        self._attempt_at_ms: int | None = None
        self._last_receive_ms: int | None = None
        self._last_good_ms: int | None = None
        self._pong_deadline_ms: int | None = None
        self._recycle_at_ms: int | None = None
        self._next_connect_ms: int | None = None
        self._coverage_start_ms: int | None = None
        self._active_since_ms: int | None = None
        self._stable_activation_recorded = False
        self.consecutive_reconnect_failures = 0
        self._coverage: deque[dict[str, Any]] = deque(maxlen=256)
        self._coverage_digest = hashlib.sha256()
        self._state_digest = hashlib.sha256()
        self._transitions: deque[dict[str, Any]] = deque(maxlen=256)
        self.diagnostics = BoundedDiagnostics()
        self.counts: Counter[str] = Counter()
        self.pending_ack_peak = 0
        self.pending_control_peak = 0
        self._started = False

    @property
    def required(self) -> set[str]:
        """Compatibility name for the desired plan, not proof of admission."""
        return self.desired_streams

    @property
    def acked(self) -> set[str]:
        return self.acknowledged_streams

    def _new_ack_id(self, sequence: int) -> AckId:
        if self.ack_id_strategy == AckIdStrategy.INTEGER:
            return validate_ack_id(sequence)
        return validate_ack_id(f"ah-e{self.epoch}-g{self.transition_generation}-r{sequence}")

    def _prune_tombstones(self) -> None:
        for stream, (expires_ms, epoch, _) in tuple(self._retiring_tombstones.items()):
            if self.now_ms >= expires_ms or epoch != self.epoch:
                del self._retiring_tombstones[stream]

    def _remember_retired(self, streams: Iterable[str]) -> bool:
        self._prune_tombstones()
        names = tuple(streams)
        if len(set(self._retiring_tombstones) | set(names)) > self.max_retiring_tombstones:
            # Do not silently forget a live tombstone and misclassify in-flight data.
            self._fail("retiring_tombstone_capacity_exceeded")
            return False
        for name in names:
            self._retiring_tombstones[name] = (
                self.now_ms + self.retiring_tombstone_ms, self.epoch, self.transition_generation)
        return True

    def _mark_stable_if_due(self, *, at_ms: int | None = None) -> None:
        at_ms = self.now_ms if at_ms is None else at_ms
        if (self.state != ConnectionState.ACTIVE or self._active_since_ms is None
                or self._stable_activation_recorded
                or at_ms < self._active_since_ms + self.stable_active_ms
                or self.pending or self.adding_streams or self.retiring_streams
                or self.desired_streams != self.acknowledged_streams):
            return
        # A delayed callback after the liveness lease expired is not stability.
        deadlines = [value for value in (
            self._last_receive_ms + self.idle_timeout_ms if self._last_receive_ms is not None else None,
            self._pong_deadline_ms, self._recycle_at_ms,
        ) if value is not None]
        if deadlines and at_ms >= min(deadlines):
            return
        self._stable_activation_recorded = True
        self.consecutive_reconnect_failures = 0
        self._record("stable_activation")

    def _activate_if_ready(self) -> None:
        if (self.state == ConnectionState.SUBSCRIBING
                and self.desired_streams == self.acknowledged_streams
                and not self.pending and not self.adding_streams and not self.retiring_streams
                and not any(method != "PONG" for method, _ in self._controls)):
            self.active_streams = set(self.desired_streams)
            self._state(ConnectionState.ACTIVE, "all_required_subscriptions_acknowledged")
            self.counts["successful_activations_total"] += 1
            self._active_since_ms = self.now_ms
            self._stable_activation_recorded = False
            self._coverage_start_ms = self.now_ms

    def _time(self, now_ms: int) -> None:
        _integer(now_ms, "virtual_time", 0, 2**63 - 1)
        if now_ms < self.now_ms:
            raise ValueError("virtual_time_cannot_move_backwards")
        self.now_ms = now_ms

    def _record(self, reason: str) -> None:
        self.diagnostics.record(reason, observed_at_ms=self.now_ms)
        self.counts[reason] += 1

    def _matches_route(self, route: Any) -> bool:
        try:
            return _route(route) == self.shard.route
        except ValueError:
            return False

    def _state(self, state: ConnectionState, reason: str) -> None:
        if state == self.state:
            return
        record = {"at_ms": self.now_ms, "from": self.state.value, "to": state.value,
                  "reason": reason, "epoch": self.epoch, "route": self.shard.route.value,
                  "shard_id": self.shard.shard_id}
        self._state_digest.update(json.dumps(record, sort_keys=True, separators=(",", ":")).encode() + b"\n")
        self._transitions.append(record)
        self.state = state

    def _finish_coverage(self, end_ms: int, reason: str) -> None:
        # A late stop/close callback cannot extend the already-expired lease.
        deadlines = [deadline for deadline in (
            self._last_receive_ms + self.idle_timeout_ms if self._last_receive_ms is not None else None,
            self._pong_deadline_ms, self._recycle_at_ms,
        ) if deadline is not None]
        if deadlines:
            end_ms = min(end_ms, *deadlines)
        if self._coverage_start_ms is not None and end_ms > self._coverage_start_ms:
            record = {"route": self.shard.route.value, "shard_id": self.shard.shard_id,
                      "connection_epoch": self.epoch, "start_ms": self._coverage_start_ms,
                      "end_ms": end_ms, "complete": True, "closed_by": reason,
                      "instruments": tuple(s.instrument_id for s in self.shard.streams if s.kind == "agg_trade"),
                      "evidence": "simulation_active_ack_route_epoch_liveness"}
            if len(self._coverage) == self._coverage.maxlen:
                # The caller must drain regularly. Evicted evidence stays missing;
                # the digest is an audit fingerprint, not reconstructable coverage.
                self._record("coverage_interval_evicted")
            self._coverage.append(record)
            self._coverage_digest.update(json.dumps(record, sort_keys=True, separators=(",", ":")).encode() + b"\n")
            self.counts["coverage_intervals"] += 1
            self.counts["covered_ms"] += end_ms - self._coverage_start_ms
        self._coverage_start_ms = None

    def _clear_work(self) -> None:
        self.pending.clear()
        self._controls.clear()
        self.acknowledged_streams.clear()
        self.active_streams.clear()
        self.adding_streams.clear()
        self.retiring_streams.clear()
        self._retiring_tombstones.clear()
        self._acknowledged_ids.clear()
        self._pong_deadline_ms = None
        self._active_since_ms = None
        self._stable_activation_recorded = False

    def _connect(self) -> None:
        self._attempt_at_ms = self.now_ms
        self._next_connect_ms = None
        self.counts["connection_attempts"] += 1
        self.counts["connection_attempts_total"] += 1
        self._state(ConnectionState.CONNECTING, "connection_attempt")
        self.transport.open(route=self.shard.route, shard_id=self.shard.shard_id, epoch=self.epoch + 1)

    def start(self, now_ms: int) -> None:
        self._time(now_ms)
        if self._started or self.state != ConnectionState.STOPPED:
            raise ValueError("supervisor_is_single_lifecycle")
        self._started = True
        self._connect()

    def on_open(self, *, now_ms: int, route: Route | str | None = None) -> None:
        self._time(now_ms)
        if self.state != ConnectionState.CONNECTING:
            self._record("unexpected_open")
            return
        if self._attempt_at_ms is not None and self.now_ms >= self._attempt_at_ms + self.connect_timeout_ms:
            self._fail("connect_timeout")
            return
        if route is not None and not self._matches_route(route):
            self._fail("route_mismatch")
            return
        self.epoch += 1  # A successful new connection is the only increment site.
        self.counts["successful_connections"] += 1
        self._clear_work()
        self.transition_generation += 1
        self.adding_streams = set(self.desired_streams)
        self._budget = ControlBudget()
        self._last_receive_ms = self._last_good_ms = self.now_ms
        self._recycle_at_ms = self.now_ms + 85500000 + self.rng.randint(-self.recycle_jitter_ms, self.recycle_jitter_ms)
        self._state(ConnectionState.SUBSCRIBING, "transport_open")
        self._queue_streams("SUBSCRIBE", (s.wire_name for s in self.shard.streams))
        self._pump()

    def _queue_streams(self, method: str, streams: Iterable[str]) -> None:
        names = tuple(sorted(streams))
        for offset in range(0, len(names), 50):
            self._controls.append((method, names[offset:offset + 50]))
        self.pending_control_peak = max(self.pending_control_peak, len(self._controls))

    def _pump(self) -> None:
        if self.state not in {ConnectionState.SUBSCRIBING, ConnectionState.ACTIVE}:
            return
        while self._controls:
            method, payload = self._controls[0]
            if method != "PONG" and len(self.pending) >= self.max_pending_acks:
                break
            if not self._budget.take(self.now_ms):
                break
            self._controls.popleft()
            if method == "PONG":
                self.transport.send({"frame_type": "pong", "payload": payload[0]})
                self._pong_deadline_ms = min((item[1] for name, item in self._controls if name == "PONG"), default=None)
                self.counts["pong_sent"] += 1
            else:
                self._request_id += 1
                request = PendingAck(self._new_ack_id(self._request_id), self.epoch, method,
                                     tuple(s.lower() for s in payload), self.now_ms,
                                     self.now_ms + self.ack_timeout_ms, self.transition_generation)
                self.pending[request.request_id] = request
                self.transport.send({"method": method, "params": list(payload), "id": request.request_id})
                self.pending_ack_peak = max(self.pending_ack_peak, len(self.pending))
            self.counts["controls_sent"] += 1

    def _fail(self, reason: str, *, uncertain: bool = False, deadline_ms: int | None = None) -> None:
        end = deadline_ms if deadline_ms is not None else self.now_ms
        if uncertain:
            end = min(end, self._last_good_ms if self._last_good_ms is not None else end)
        # Loss/malformed evidence invalidates the interval since the last good
        # observation for stability as well as coverage.
        self._mark_stable_if_due(at_ms=end)
        self._finish_coverage(end, reason)
        self._record(reason)
        self._state(ConnectionState.DEGRADED, reason)
        self.transport.close(reason=reason)
        self._clear_work()
        self._recycle_at_ms = None
        self.consecutive_reconnect_failures += 1
        if self.consecutive_reconnect_failures >= self.max_reconnect_attempts:
            self._next_connect_ms = None
            self._record("reconnect_budget_exhausted")
            self.counts["reconnect_budget_exhausted_total"] += 1
            return
        exponent = min(max(0, self.consecutive_reconnect_failures - 1), 6)
        base = min(60000, 1000 * (2 ** exponent))
        delay = min(60000, base + self.rng.randint(0, base // 5))
        self._next_connect_ms = self.now_ms + delay
        self._state(ConnectionState.BACKOFF, reason)

    def _planned_recycle(self) -> None:
        self._finish_coverage(self._recycle_at_ms or self.now_ms, "planned_recycle")
        self._record("scheduled_recycle")  # Retained legacy diagnostic name.
        self.counts["planned_recycles_total"] += 1
        self._state(ConnectionState.RECYCLING, "planned_24h_recycle")
        self.transport.close(reason="planned_24h_recycle")
        self._clear_work()
        self._recycle_at_ms = None
        self._next_connect_ms = self.now_ms
        self._state(ConnectionState.BACKOFF, "planned_24h_recycle")

    def on_ack(self, message: Mapping[str, Any], *, epoch: int, now_ms: int,
               route: Route | str | None = None, generation: int | None = None,
               method: str | None = None) -> str:
        previous_state = self.state
        # Every ingress path advances the same virtual lease checks. An ACK must
        # not revive coverage after a queued PONG, another ACK, or recycle deadline.
        self._advance(now_ms, allow_stable=False)
        if self.state not in {ConnectionState.SUBSCRIBING, ConnectionState.ACTIVE}:
            self._record("ack_while_disconnected")
            return ("EXPIRED" if previous_state in {ConnectionState.SUBSCRIBING, ConnectionState.ACTIVE}
                    else "UNKNOWN")
        if type(epoch) is not int or epoch != self.epoch:
            self._record("stale_epoch_ack")
            return "UNKNOWN"
        if route is not None and not self._matches_route(route):
            self._fail("route_mismatch", uncertain=True)
            return "UNKNOWN"
        request_id = message.get("id") if isinstance(message, Mapping) else None
        try:
            validate_ack_id(request_id)
        except ValueError:
            self._fail("malformed_ack", uncertain=True)
            return "UNKNOWN"
        expected_type = int if self.ack_id_strategy == AckIdStrategy.INTEGER else str
        if type(request_id) is not expected_type:
            self._record("ack_id_type_mismatch")
            return "UNKNOWN"
        if generation is not None and (type(generation) is not int or generation != self.transition_generation):
            self._record("stale_generation_ack")
            return "UNKNOWN"
        acknowledged = next((item for item in self._acknowledged_ids
                             if item.epoch == epoch and type(item.request_id) is type(request_id)
                             and item.request_id == request_id), None)
        if acknowledged is not None:
            if acknowledged.generation != self.transition_generation:
                self._record("stale_generation_ack")
                return "UNKNOWN"
            if method is not None and (type(method) is not str or method != acknowledged.method):
                self._record("ack_method_mismatch")
                return "UNKNOWN"
            self._record("duplicate_ack")
            return "DUPLICATE"
        request = self.pending.get(request_id)
        if request is None:
            self._record("unknown_ack")
            return "UNKNOWN"
        if (type(request.request_id) is not type(request_id) or request.epoch != epoch
                or request.generation != self.transition_generation):
            self._record("stale_generation_ack")
            return "UNKNOWN"
        if method is not None and (type(method) is not str or method != request.method):
            self._record("ack_method_mismatch")
            return "UNKNOWN"
        if self.now_ms >= request.expires_at_ms:
            self.ack_history.append({"id": request_id, "epoch": epoch, "status": "EXPIRED"})
            self._fail("ack_timeout")
            return "EXPIRED"
        if "result" not in message or message["result"] is not None or "code" in message:
            self.ack_history.append({"id": request_id, "epoch": epoch, "status": "REJECTED"})
            self._fail("subscription_rejected", uncertain=True)
            return "REJECTED"
        del self.pending[request_id]
        self._acknowledged_ids.append(request)
        if request.method == "SUBSCRIBE":
            self.acknowledged_streams.update(request.streams)
            self.adding_streams.difference_update(request.streams)
        else:
            self.acknowledged_streams.difference_update(request.streams)
            self.retiring_streams.difference_update(request.streams)
            if not self._remember_retired(request.streams):
                return "REJECTED"
        self.ack_history.append({"id": request_id, "epoch": epoch, "generation": request.generation,
                                 "method": request.method, "status": "ACKNOWLEDGED", "stream_count": len(request.streams)})
        self._last_receive_ms = self._last_good_ms = self.now_ms
        self.counts["acknowledged_batches"] += 1
        self._pump()
        self._activate_if_ready()
        return "ACKNOWLEDGED"

    def on_frame(self, message: Any, *, epoch: int, route: Route | str, now_ms: int,
                 frame_type: str = "data", parser_valid: bool = True) -> bool:
        self._advance(now_ms, allow_stable=False)
        if self.state not in {ConnectionState.SUBSCRIBING, ConnectionState.ACTIVE}:
            self._record("frame_while_disconnected")
            return False
        if type(epoch) is not int or epoch != self.epoch:
            self._record("stale_epoch_frame")
            return False
        if not self._matches_route(route):
            self._fail("route_mismatch", uncertain=True)
            return False
        if frame_type == "ack":
            return self.on_ack(message, epoch=epoch, route=route, now_ms=now_ms) == "ACKNOWLEDGED"
        if frame_type == "ping":
            if type(message) is not str or len(message.encode("utf-8")) > 125:
                self._fail("malformed_ping", uncertain=True)
                return False
            if len(self._controls) >= 128:
                self._fail("control_queue_overflow", uncertain=True)
                return False
            deadline = self.now_ms + self.pong_timeout_ms
            self._controls.appendleft(("PONG", (message, deadline)))
            self.pending_control_peak = max(self.pending_control_peak, len(self._controls))
            self._pong_deadline_ms = min(self._pong_deadline_ms or deadline, deadline)
            self._mark_stable_if_due()
            self._last_receive_ms = self._last_good_ms = self.now_ms
            self._pump()
            return True
        if frame_type == "pong":
            self._mark_stable_if_due()
            self._last_receive_ms = self._last_good_ms = self.now_ms
            self.counts["pong_received"] += 1
            return True
        if frame_type != "data" or type(parser_valid) is not bool or not parser_valid:
            self._fail("malformed_payload", uncertain=True)
            return False
        try:
            stream, _, unknown_fields = validate_combined_envelope(message, limits=self.envelope_limits)
        except ValueError as exc:
            # Stable reason code only, never an upstream field name or value.
            reason = str(exc)
            if reason in {"malformed_combined_envelope", "invalid_field_type", "oversized_payload", "too_many_items", "invalid_json_shape"}:
                self._record(reason)
            self._fail("malformed_payload", uncertain=True)
            return False
        if unknown_fields:
            self.diagnostics.record("unknown_envelope_field", observed_at_ms=self.now_ms, amount=unknown_fields)
            self.counts["unknown_envelope_field"] += unknown_fields
        name = stream.lower()
        if name in self.retiring_streams or name in self._retiring_tombstones:
            self._mark_stable_if_due()
            self._last_receive_ms = self._last_good_ms = self.now_ms
            self._record("retiring_stream_frame")
            return False
        if name in self.adding_streams:
            self._last_receive_ms = self._last_good_ms = self.now_ms
            self._record("adding_stream_early_frame")
            self._record("data_before_required_ack")
            return False
        if name not in self.desired_streams:
            self._fail("unplanned_stream", uncertain=True)
            return False
        self._mark_stable_if_due()
        self._last_receive_ms = self._last_good_ms = self.now_ms
        if self.state != ConnectionState.ACTIVE or name not in self.acked:
            self._record("data_before_required_ack")
            return False
        self.counts["valid_data_frames"] += 1
        return True

    def report_gap(self, *, now_ms: int, reason: str = "lost_frame") -> None:
        self._time(now_ms)
        if reason not in {"lost_frame", "sequence_gap", "parser_drop"}:
            raise ValueError("invalid_gap_reason")
        if self.state in {ConnectionState.SUBSCRIBING, ConnectionState.ACTIVE}:
            self._fail(reason, uncertain=True)

    def on_close(self, *, now_ms: int, epoch: int | None = None) -> None:
        self._time(now_ms)
        if epoch is not None and (type(epoch) is not int or epoch != self.epoch):
            self._record("stale_epoch_close")
            return
        if self.state in {ConnectionState.CONNECTING, ConnectionState.SUBSCRIBING, ConnectionState.ACTIVE}:
            self._fail("remote_close")

    def update_subscriptions(self, shard: ShardPlan, *, now_ms: int) -> None:
        self.step(now_ms)
        if self.state != ConnectionState.ACTIVE or self.pending or self._controls:
            raise ValueError("subscription_update_requires_quiescent_active_connection")
        if not isinstance(shard, ShardPlan) or shard.route != self.shard.route or shard.shard_id != self.shard.shard_id:
            raise ValueError("subscription_update_route_or_shard_mismatch")
        new = {s.canonical_name for s in shard.streams}
        if new == self.required:
            return
        self._finish_coverage(self.now_ms, "subscription_change")
        self._active_since_ms = None
        self._stable_activation_recorded = False
        self.transition_generation += 1
        self.adding_streams = new - self.active_streams
        self.retiring_streams = self.active_streams - new
        # A name deliberately reintroduced is governed by its new SUBSCRIBE ACK.
        for key in new:
            self._retiring_tombstones.pop(key, None)
        old_wire = {s.canonical_name: s.wire_name for s in self.shard.streams}
        new_wire = {s.canonical_name: s.wire_name for s in shard.streams}
        self._queue_streams("UNSUBSCRIBE", (old_wire[key] for key in self.retiring_streams))
        self._queue_streams("SUBSCRIBE", (new_wire[key] for key in self.adding_streams))
        self.shard, self.desired_streams = shard, new
        self._state(ConnectionState.SUBSCRIBING, "subscription_change")
        self._pump()

    def step(self, now_ms: int) -> None:
        self._advance(now_ms, allow_stable=True)

    def _advance(self, now_ms: int, *, allow_stable: bool) -> None:
        self._time(now_ms)
        self._prune_tombstones()
        if self.state == ConnectionState.BACKOFF and self._next_connect_ms is not None and now_ms >= self._next_connect_ms:
            self.counts["reconnect_attempts"] += 1
            self.counts["reconnect_attempts_total"] += 1
            self._connect()
            return
        if self.state == ConnectionState.CONNECTING:
            if self._attempt_at_ms is not None and now_ms >= self._attempt_at_ms + self.connect_timeout_ms:
                self._fail("connect_timeout")
            return
        if self.state not in {ConnectionState.SUBSCRIBING, ConnectionState.ACTIVE}:
            return
        expired = [request for request in self.pending.values() if now_ms >= request.expires_at_ms]
        if expired:
            for request in expired:
                self.ack_history.append({"id": request.request_id, "epoch": request.epoch, "status": "EXPIRED"})
            self._fail("ack_timeout", deadline_ms=min(r.expires_at_ms for r in expired))
            return
        if self._pong_deadline_ms is not None and now_ms >= self._pong_deadline_ms:
            self._fail("pong_timeout", deadline_ms=self._pong_deadline_ms)
            return
        if self._last_receive_ms is not None and now_ms >= self._last_receive_ms + self.idle_timeout_ms:
            self._fail("idle_timeout", deadline_ms=self._last_receive_ms + self.idle_timeout_ms)
            return
        if self._recycle_at_ms is not None and now_ms >= self._recycle_at_ms:
            self._planned_recycle()
            return
        if allow_stable:
            self._mark_stable_if_due()
        self._pump()

    def stop(self, now_ms: int) -> None:
        self._time(now_ms)
        if self.state == ConnectionState.STOPPED:
            return
        self._finish_coverage(self.now_ms, "explicit_stop")
        self._state(ConnectionState.STOPPING, "explicit_stop")
        self.transport.close(reason="explicit_stop")
        self._clear_work()
        self._next_connect_ms = self._recycle_at_ms = self._attempt_at_ms = None
        self._state(ConnectionState.STOPPED, "cleanup_complete")

    def iter_coverage_records(self, *, source: str = "binance_usdm_agg_trade", exchange: str = "binance",
                              market: str = "usdt_perpetual") -> Iterable[dict[str, Any]]:
        """Drain retained closed intervals as note_connection kwargs.

        Consume regularly to avoid bounded-buffer eviction. Consult snapshot's
        coverage_interval_evicted counter; lost intervals are never reconstructed
        or promoted to complete coverage by this iterator or its audit digest.
        """
        while self._coverage:
            interval = self._coverage.popleft()
            for instrument in interval["instruments"]:
                yield {"source": source, "exchange": exchange, "market": market,
                       "instrument_id": instrument, "connection_epoch": interval["connection_epoch"],
                       "start_ms": interval["start_ms"], "end_ms": interval["end_ms"], "complete": True}

    def snapshot(self) -> dict[str, Any]:
        return {"protocol_version": PROTOCOL_VERSION, "mode": "simulation_only", "network_calls": 0,
                "route": self.shard.route.value, "path": self.shard.route.path, "shard_id": self.shard.shard_id,
                "state": self.state.value, "epoch": self.epoch, "now_ms": self.now_ms,
                "required_streams": len(self.required), "acknowledged_streams": len(self.required & self.acked),
                "stream_transition": {"generation": self.transition_generation,
                    "active_streams": sorted(self.active_streams), "desired_streams": sorted(self.desired_streams),
                    "adding_streams": sorted(self.adding_streams), "retiring_streams": sorted(self.retiring_streams),
                    "acknowledged_streams": sorted(self.acknowledged_streams),
                    "retiring_tombstones": {name: {"expires_at_ms": item[0], "epoch": item[1], "generation": item[2]}
                                            for name, item in self._retiring_tombstones.items()},
                    "tombstone_ttl_ms": self.retiring_tombstone_ms,
                    "tombstone_capacity": self.max_retiring_tombstones},
                "ack_id_strategy": self.ack_id_strategy.value,
                "connection_attempts_total": self.counts["connection_attempts_total"],
                "reconnect_attempts_total": self.counts["reconnect_attempts_total"],
                "consecutive_reconnect_failures": self.consecutive_reconnect_failures,
                "successful_activations_total": self.counts["successful_activations_total"],
                "planned_recycles_total": self.counts["planned_recycles_total"],
                "reconnect_budget_exhausted_total": self.counts["reconnect_budget_exhausted_total"],
                "stable_active_ms": self.stable_active_ms, "active_since_ms": self._active_since_ms,
                "activation_stable": self._stable_activation_recorded,
                "pending_acks": [r.to_dict() for r in self.pending.values()],
                "pending_ack_peak": self.pending_ack_peak, "pending_controls": len(self._controls),
                "pending_control_peak": self.pending_control_peak,
                "control_peak_per_second": self._budget.peak_per_second,
                "next_connect_ms": self._next_connect_ms, "recycle_at_ms": self._recycle_at_ms,
                "coverage_open_since_ms": self._coverage_start_ms, "coverage": list(self._coverage),
                "coverage_interval_capacity": self._coverage.maxlen,
                "coverage_interval_evicted": self.counts["coverage_interval_evicted"],
                "coverage_evidence_lost": self.counts["coverage_interval_evicted"] > 0,
                "coverage_drain_required": bool(self._coverage),
                "coverage_digest": self._coverage_digest.hexdigest(), "state_digest": self._state_digest.hexdigest(),
                "transitions": list(self._transitions), "ack_history": list(self.ack_history),
                "counts": dict(sorted(self.counts.items())), "diagnostics": self.diagnostics.snapshot(),
                "cleanup_complete": (self.state == ConnectionState.STOPPED and not self.pending and not self._controls
                                     and not self.active_streams and not self.adding_streams and not self.retiring_streams
                                     and not self.acknowledged_streams and not self._retiring_tombstones),
                "transport_action_count": len(self.transport.actions), "transport_dropped_actions": self.transport.dropped_actions}


def run_connection_scenario(scenario: Mapping[str, Any] | str = "normal", seed: int = 0) -> dict[str, Any]:
    """Deterministic scenario recipes or explicit action lists, entirely offline."""
    if isinstance(scenario, str):
        scenario = {"name": scenario}
    if not isinstance(scenario, Mapping):
        raise ValueError("scenario_must_be_mapping")
    name = scenario.get("name", "custom")
    names = {"normal", "custom", "ack_loss", "reconnect_storm", "connect_failures", "route_mismatch", "malformed", "late_ack", "gap", "idle_timeout", "recycle"}
    if name not in names:
        raise ValueError("unknown_connection_scenario")
    count = _integer(scenario.get("instruments", 100), "instruments", 1, 1000)
    plan = plan_subscriptions(synthetic_instruments(count),
                              max_streams_per_connection=scenario.get("max_streams_per_connection", 800))
    route = _route(scenario.get("route", "MARKET"))
    requested_shard = scenario.get("shard_id")
    choices = [s for s in plan.shards if s.route == route and (requested_shard is None or s.shard_id == requested_shard)]
    if not choices:
        raise ValueError("scenario_shard_not_in_plan")
    shard = choices[0]
    connection = ConnectionSupervisor(shard, seed=seed, max_reconnect_attempts=scenario.get("max_reconnect_attempts", 100),
        stable_active_ms=scenario.get("stable_active_ms", 30000),
        ack_id_strategy=scenario.get("ack_id_strategy", AckIdStrategy.INTEGER))
    now = _integer(scenario.get("start_ms", 1704067200000), "start_ms", 0, 2**63 - 1)
    connection.start(now)
    rng = random.Random(seed)
    loss = scenario.get("ack_loss_ratio", 0.1 if name == "ack_loss" else 0.0)
    if type(loss) not in (int, float) or not 0 <= loss <= 1:
        raise ValueError("invalid_ack_loss_ratio")
    omitted: set[AckId] = set()
    selected_losses: set[AckId] = set()
    ack_requests_seen = 0
    ack_requests_omitted = 0
    def acknowledge_available(at_ms: int) -> None:
        nonlocal ack_requests_seen, ack_requests_omitted
        for request_id in tuple(connection.pending):
            if request_id in omitted:
                continue
            ack_requests_seen += 1
            if request_id in selected_losses:
                omitted.add(request_id)
                ack_requests_omitted += 1
                continue
            connection.on_ack({"id": request_id, "result": None}, epoch=connection.epoch, now_ms=at_ms)
    def activate(at_ms: int) -> int:
        nonlocal selected_losses
        omitted.clear()
        batches = (len(connection.shard.streams) + 49) // 50
        losses = max(1, round(batches * loss)) if loss else 0
        # IDs are generated for the forthcoming epoch and transition generation.
        selected_sequences = rng.sample(range(connection._request_id + 1, connection._request_id + batches + 1), losses)
        selected_losses = {sequence if connection.ack_id_strategy == AckIdStrategy.INTEGER
                           else f"ah-e{connection.epoch + 1}-g{connection.transition_generation + 1}-r{sequence}"
                           for sequence in selected_sequences}
        connection.on_open(now_ms=at_ms)
        for _ in range(10):
            acknowledge_available(at_ms)
            if not connection._controls:
                break
            at_ms += 1000
            connection.step(at_ms)
        return at_ms
    if "actions" in scenario:
        actions = scenario["actions"]
        if not isinstance(actions, list) or len(actions) > 10000:
            raise ValueError("invalid_scenario_actions")
        for action in actions:
            now = action["at_ms"]
            kind = action["action"]
            if kind == "open": connection.on_open(now_ms=now, route=action.get("route"))
            elif kind == "ack_all": acknowledge_available(now)
            elif kind == "ack": connection.on_ack(action["message"], epoch=action.get("epoch", connection.epoch), route=action.get("route"), now_ms=now)
            elif kind == "advance": connection.step(now)
            elif kind == "close": connection.on_close(now_ms=now, epoch=action.get("epoch"))
            elif kind == "stop": connection.stop(now)
            elif kind == "gap": connection.report_gap(now_ms=now)
            elif kind in {"data", "ping", "pong"}: connection.on_frame(action.get("message", ""), epoch=action.get("epoch", connection.epoch), route=action.get("route", route), now_ms=now, frame_type=kind, parser_valid=action.get("parser_valid", True))
            else: raise ValueError("unknown_scenario_action")
    elif name == "connect_failures":
        cycles = _integer(scenario.get("failures", 3), "failures", 1, 1000)
        for _ in range(cycles):
            now += connection.connect_timeout_ms
            connection.step(now)
            deadline = connection.snapshot()["next_connect_ms"]
            if deadline is None:
                break
            now = deadline
            connection.step(now)
    else:
        now = activate(now)
        if name == "reconnect_storm":
            cycles = _integer(scenario.get("reconnects", 100), "reconnects", 1, 1000)
            for _ in range(cycles):
                # A successful recovery means ACK-complete and stable, not TCP open.
                now += connection.stable_active_ms
                connection.on_frame("stable", epoch=connection.epoch, route=route, now_ms=now, frame_type="ping")
                now += 10
                connection.on_close(now_ms=now)
                deadline = connection.snapshot()["next_connect_ms"]
                if deadline is None:
                    break
                now = deadline
                connection.step(now)
                now = activate(now)
        elif name == "ack_loss":
            now += connection.ack_timeout_ms
            connection.step(now)
        elif name == "late_ack":
            connection.on_ack({"id": 1, "result": None}, epoch=connection.epoch - 1, now_ms=now)
        elif name == "route_mismatch":
            connection.on_frame({"stream": shard.streams[0].wire_name, "data": {}}, epoch=connection.epoch,
                route=Route.PUBLIC if route == Route.MARKET else Route.MARKET, now_ms=now + 1)
        elif name == "malformed":
            connection.on_frame("invalid", epoch=connection.epoch, route=route, now_ms=now + 1)
        elif name == "gap":
            connection.report_gap(now_ms=now + 1)
        elif name == "idle_timeout":
            now += connection.idle_timeout_ms
            connection.step(now)
        elif name == "recycle":
            deadline = connection.snapshot()["recycle_at_ms"]
            while now + 180000 < deadline:
                now += 180000
                connection.on_frame("ping", epoch=connection.epoch, route=route, now_ms=now, frame_type="ping")
            now = deadline
            connection.step(now)
    result = connection.snapshot()
    result["scenario"] = name
    result["seed"] = seed
    result["ack_loss_ratio"] = loss
    result["ack_loss_observations"] = {"requests": ack_requests_seen, "omitted": ack_requests_omitted,
                                       "actual_ratio": ack_requests_omitted / ack_requests_seen if ack_requests_seen else None,
                                       "rounding": "nearest_batch_minimum_one_for_nonzero_recipe"}
    result["subscription_plan"] = plan.to_dict()
    result["simulation_scope"] = "single_selected_shard_not_full_market_connections"
    result["simulated_shard_id"] = shard.shard_id
    result["connection_states"] = {connection.shard.shard_id: connection.state.value}
    result["epochs"] = {connection.shard.shard_id: connection.epoch}
    return result
