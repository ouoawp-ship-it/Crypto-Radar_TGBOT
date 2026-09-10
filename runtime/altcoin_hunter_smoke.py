"""Explicit, temporary-only short public-data verification; never a daemon.

The old supervisor remains an offline contract. This driver executes its action
ledger and only supplies callbacks observed on the real transport. All writes
are bounded evidence in a fresh local temporary directory, never a database.
The separate CLI supplies an outer process deadline, including unbounded OS DNS.
"""
from __future__ import annotations

from collections import Counter
from contextlib import AbstractContextManager
import hashlib
import json
from pathlib import Path
import stat
import time
import tracemalloc
from typing import Any

from radars.altcoin_hunter.adapters.base import PROTOCOL_VERSION, ExchangeInfoPayloadLimits, FakeTransport, Route
from radars.altcoin_hunter.adapters.binance_protocol import parse_binance_payload, parse_funding_info, parse_server_time
from radars.altcoin_hunter.adapters.binance_usdm import parse_exchange_info
from radars.altcoin_hunter.connection import ConnectionState, ConnectionSupervisor
from radars.altcoin_hunter.ingestion import AdmissionContext, OfflineIngestion, RestAdmissionContext
from radars.altcoin_hunter.models import event_to_dict
from radars.altcoin_hunter.public_paths import safe_temporary_root
from runtime.altcoin_hunter_transport import PublicRestClient, PublicTransportError, PublicWebSocket, SmokeRateBudget
from radars.altcoin_hunter.rest_budget import make_request
from radars.altcoin_hunter.rest_scheduler import OiSamplingPlanner, RestScheduler
from radars.altcoin_hunter.smoke_policy import SmokeConfig, seal_smoke_report, smoke_shards


class SmokeStop(RuntimeError):
    """Stable, local reason; never raw server or exception text."""


def _decode_frame(payload):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result
    return json.loads(payload, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_json")))


class RunClock:
    def __init__(self) -> None:
        self.started_ns = time.monotonic_ns()
        self.started_ms = time.time_ns() // 1_000_000

    def monotonic_ns(self) -> int:
        return time.monotonic_ns()

    def now_ms(self) -> int:
        return self.started_ms + (self.monotonic_ns() - self.started_ns) // 1_000_000

    def pause(self, seconds: float) -> None:
        time.sleep(seconds)


def local_temporary_directory(path: Path) -> Path:
    """Reject links/reparse points and paths outside the OS local temp root."""
    target = Path(path).absolute()
    root = safe_temporary_root()
    if str(target).startswith(("\\\\", "//")) or ".." in target.parts:
        raise ValueError("local_temporary_output_required")
    if not target.is_relative_to(root) or target == root or not target.name.startswith("altcoin-hunter-smoke-"):
        raise ValueError("local_temporary_output_required")
    for part in (target, *target.parents):
        info = part.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("temporary_reparse_point_rejected")
        if part == root:
            break
    if not target.is_dir() or any(target.iterdir()):
        raise ValueError("fresh_temporary_output_required")
    return target


def _write_report(output: Path, report: dict) -> None:
    encoded = json.dumps(report, sort_keys=True, ensure_ascii=True, allow_nan=False).encode("ascii")
    if len(encoded) > 65536:
        raise SmokeStop("report_capacity_exceeded")
    pending = output / "report.pending"
    # The parent is fresh and owned by this run. No arbitrary output filename.
    with pending.open("xb") as handle:
        handle.write(encoded)
    pending.replace(output / "report.json")


class PublicNetworkGuard(AbstractContextManager):
    """Worker-process-only DNS/egress allowlist, with observed attempt counts.

    It grants only DNS for the two public hosts and TCP to their returned :443
    addresses. It does not claim DNS itself supports a synchronous deadline;
    the CLI's parent process enforces the absolute run lifetime.
    """
    def __init__(self) -> None:
        self.dns_attempts = self.tcp_attempts = self.blocked_attempts = 0
        self.addresses: set[tuple[str, int]] = set()

    def __enter__(self):
        import socket
        self.socket = socket
        self.originals = socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex
        resolve, connect, _ = self.originals

        def guarded_resolve(host, port, *args, **kwargs):
            if host not in {"fapi.binance.com", "fstream.binance.com"} or port != 443:
                self.blocked_attempts += 1
                raise SmokeStop("non_public_dns_blocked")
            self.dns_attempts += 1
            result = resolve(host, port, *args, **kwargs)
            for item in result:
                self.addresses.add((item[4][0], item[4][1]))
            if len(self.addresses) > 128:
                raise SmokeStop("resolved_address_capacity")
            return result

        def guarded_connect(sock, address):
            if (not isinstance(address, tuple) or len(address) < 2
                    or (address[0], address[1]) not in self.addresses):
                self.blocked_attempts += 1
                raise SmokeStop("non_public_connection_blocked")
            self.tcp_attempts += 1
            return connect(sock, address)

        def blocked_connect_ex(sock, address):
            self.blocked_attempts += 1
            raise SmokeStop("unplanned_connect_ex_blocked")

        socket.getaddrinfo = guarded_resolve
        socket.socket.connect = guarded_connect
        socket.socket.connect_ex = blocked_connect_ex
        return self

    def __exit__(self, *args):
        self.socket.getaddrinfo, self.socket.socket.connect, self.socket.socket.connect_ex = self.originals


class SmokeEvidence:
    """Bounded aggregate evidence; no raw trade archive or zero-filled minutes."""
    LATENCY_BOUNDS = (-2000, 0, 25, 50, 100, 250, 500, 1000, 2000, 5000)

    def __init__(self, symbols: tuple[str, ...]) -> None:
        self.symbols = frozenset(symbols)
        self.events: Counter[str] = Counter()
        self.latest: dict[str, int] = {}
        self.latency: Counter[str] = Counter()
        self.last_trade: dict[str, tuple[int, int]] = {}
        self.digest = hashlib.sha256()
        self.max_latency_ms: int | None = None
        self.parser_rejected = self.admission_rejected = self.duplicates = 0
        self.unknown_fields = 0
        self.accepted_events = 0

    def consume(self, result, *, now_ms: int) -> None:
        self.parser_rejected += result.parser_rejected_count
        self.admission_rejected += result.admission_rejected_count
        self.duplicates += result.duplicate_count
        if result.parser_rejected_count or result.admission_rejected_count:
            raise SmokeStop("event_parse_or_admission_rejected")
        for event, metadata in zip(result.events, result.event_metadata):
            if event.exchange_symbol not in self.symbols:
                raise SmokeStop("unselected_instrument")
            latency = now_ms - event.event_time_ms
            if latency < -2000 or latency > 5000:
                raise SmokeStop("event_latency_outside_smoke_gate")
            if event.event_type == "trade":
                aggregate_id = int(event.source_event_id)
                previous = self.last_trade.get(event.instrument_id)
                if previous is not None and (aggregate_id != previous[0] + 1
                                            or event.sequence_start != previous[1] + 1):
                    raise SmokeStop("unexplained_trade_sequence_gap")
                self.last_trade[event.instrument_id] = aggregate_id, event.sequence_end
            key = event.exchange_symbol + ":" + event.event_type
            self.events[key] += 1
            self.latest[key] = now_ms
            self.accepted_events += 1
            self.unknown_fields += metadata.get("unknown_field_count", 0)
            self.max_latency_ms = latency if self.max_latency_ms is None else max(self.max_latency_ms, latency)
            bound = next((value for value in self.LATENCY_BOUNDS if latency <= value), 5000)
            self.latency[str(bound)] += 1
            self.digest.update(json.dumps(event_to_dict(event), sort_keys=True,
                                          separators=(",", ":"), allow_nan=False).encode() + b"\n")

    def freshness(self, now_ms: int, *, include_oi: bool = False) -> None:
        for symbol in sorted(self.symbols):
            limits = {"trade": 30_000, "mark_price": 10_000, "funding": 10_000, "book_ticker": 30_000}
            if include_oi:
                limits["open_interest"] = 300_000
            for kind, limit in limits.items():
                last = self.latest.get(symbol + ":" + kind)
                if last is None or now_ms - last > limit:
                    raise SmokeStop("selected_stream_missing_or_stale")

    def snapshot(self) -> dict:
        return {"parsed_and_admitted_events": self.accepted_events,
                "event_counts": dict(sorted(self.events.items())),
                "parser_rejected_count": self.parser_rejected,
                "admission_rejected_count": self.admission_rejected,
                "duplicate_count": self.duplicates,
                "total_rejected_count": self.parser_rejected + self.admission_rejected + self.duplicates,
                "event_metadata_unknown_field_references": self.unknown_fields,
                "event_latency_ms_max": self.max_latency_ms,
                "event_latency_histogram_upper_bounds_ms": dict(sorted(self.latency.items())),
                "normalized_event_digest": self.digest.hexdigest(),
                "raw_event_capture": False, "depth_available": False}


class SmokeSession:
    """Injectable orchestrator; tests supply explicit offline transport doubles."""
    def __init__(self, config: SmokeConfig, *, clock, rest_factory=PublicRestClient,
                 ws_factory=PublicWebSocket, deadline_ns: int | None = None) -> None:
        config.require_enabled()
        self.config, self.clock = config, clock
        entered_ns = clock.monotonic_ns()
        self.start_ns = (min(entered_ns, deadline_ns - config.duration_sec * 1_000_000_000)
                         if deadline_ns is not None else entered_ns)
        self.start_ms = clock.now_ms() - (entered_ns - self.start_ns) // 1_000_000
        cap = config.deadline_ns(self.start_ns)
        self.deadline_ns = min(cap, deadline_ns) if deadline_ns is not None else cap
        self.deadline_ms = self.start_ms + (self.deadline_ns - self.start_ns) // 1_000_000
        if self.deadline_ms <= self.start_ms + 1000:
            raise SmokeStop("insufficient_smoke_time")
        self.budget = SmokeRateBudget(started_at_ms=self.start_ms, deadline_ms=self.deadline_ms - 500)
        self.scheduler = RestScheduler(clock=clock.now_ms, coordinator=self.budget, live=True,
                                       max_attempts=1, max_queue=32, max_inflight=1, timeout_ms=5000)
        self.rest = rest_factory(budget=self.budget, clock_ms=clock.now_ms, allow_network=True)
        self.ws_factory = ws_factory
        self.connections: list[tuple[ConnectionSupervisor, Any]] = []
        self.ingestion = OfflineIngestion(max_dedup_keys=200_000, max_instruments=20)
        self.evidence = SmokeEvidence(config.symbols)
        self.oi = OiSamplingPlanner(max_instruments=20)
        self.preflight: list[dict] = []
        self.registry, self.funding = {}, None
        self.last_oi_snapshot: dict = {}
        self.transport_failure: dict = {}
        self.current_operation = "preflight"
        self.current_route: str | None = None
        self.outstanding_requests: set[str] = set()
        self.opaque_pings: dict[tuple[str, str], bytes] = {}
        self.ping_sequence = 0
        self.generation = 0
        self.ws_bytes = self.frames = 0

    def _http(self, request, *, symbol: str | None = None):
        self.current_operation, self.current_route = "rest_request", None
        if symbol is not None and (symbol not in self.registry
                or self.registry[symbol].identity.instrument_id != request.instrument_id):
            raise SmokeStop("request_symbol_identity_mismatch")
        try:
            result = self.rest.request(request, symbol=symbol)
        except PublicTransportError as exc:
            self.transport_failure = {"reason": exc.reason, **dict(exc.metadata)}
            self.scheduler.complete(request, status_code=exc.metadata.get("http_status"),
                                    response_time_ms=self.clock.now_ms(), timed_out=True)
            self.outstanding_requests.discard(request.request_id)
            raise SmokeStop(exc.reason) from None
        completed = self.clock.now_ms()
        completion = self.scheduler.complete(request, status_code=result.status,
                                             response_time_ms=completed)
        self.outstanding_requests.discard(request.request_id)
        if not completion.accepted or not self.scheduler.diagnostics(completed)["budget_trusted"]:
            raise SmokeStop("rest_completion_not_accepted")
        return result, completion, completed

    def _metadata(self, endpoint: str):
        self.generation += 1
        request = make_request(endpoint, self.clock.now_ms(), generation=self.generation, ttl_ms=5000)
        if not self.scheduler.submit(request):
            raise SmokeStop("metadata_schedule_rejected")
        self.outstanding_requests.add(request.request_id)
        due = self.scheduler.poll_due(limit=1)
        if len(due) != 1 or due[0] != request:
            raise SmokeStop("metadata_budget_blocked")
        result, _, received = self._http(due[0])
        return result, received

    def preflight_directory(self) -> None:
        started = self.clock.now_ms()
        result, received = self._metadata("serverTime")
        server_ms = parse_server_time(result.body)
        offset = server_ms - (started + received) // 2
        self.preflight.append({**dict(result.metadata), "server_offset_estimate_ms": offset,
                               "round_trip_ms": received - started})
        if abs(offset) > 500:
            raise SmokeStop("local_clock_offset_exceeds_smoke_gate")
        result, received = self._metadata("exchangeInfo")
        directory = parse_exchange_info(result.body, observed_at_ms=received,
                                        limits=ExchangeInfoPayloadLimits())
        # Body bytes are bounded before JSON decode. Raw JSON is never logged.
        rejected = (directory.diagnostics or {}).get("rejected_count", 0)
        self.preflight.append({**dict(result.metadata), "symbol_count": None,
                               "parse_accepted_count": len(directory.instruments),
                               "parse_rejected_count": rejected, "directory_status": directory.status})
        try:
            decoded = _decode_frame(result.body)
            if isinstance(decoded, dict) and isinstance(decoded.get("symbols"), list):
                self.preflight[-1]["symbol_count"] = len(decoded["symbols"])
        except (ValueError, UnicodeError, RecursionError):
            pass  # Keep HTTP/parse evidence even when the JSON itself is invalid.
        if not directory.accepted or rejected:
            raise SmokeStop("exchange_info_not_complete")
        self.registry = directory.registry
        shards = smoke_shards(self.registry, self.config.symbols)  # Fixed symbols cannot be substituted.
        result, received = self._metadata("fundingInfo")
        self.funding = parse_funding_info(result.body, self.registry)
        self.preflight.append({**dict(result.metadata), "parse_accepted_count": len(self.funding.entries),
                               "parse_rejected_count": self.funding.diagnostics.get("rejected_count", 0)})
        if self.funding.rejected_items or self.funding.diagnostics.get("rejected_count", 0):
            raise SmokeStop("funding_info_rejected")
        self.oi.update_universe({self.registry[s].identity.instrument_id: "NORMAL" for s in self.config.symbols}, received)
        self.outstanding_requests.update(r.request_id for r in self.oi.schedule(self.scheduler, self.clock.now_ms()))
        while self.scheduler.diagnostics()["queue_depth"]:
            self.poll_oi()
        for shard in shards:
            socket = self.ws_factory(clock_ms=self.clock.now_ms, deadline_ms=self.deadline_ms - 500,
                                     allowed_symbols=self.config.symbols,
                                     allow_network=True, timeout_sec=0.001, max_connections=1)
            supervisor = ConnectionSupervisor(shard, transport=FakeTransport(), ack_id_strategy="STRING",
                                               max_reconnect_attempts=1)
            self.connections.append((supervisor, socket))

    def poll_oi(self) -> None:
        due = self.scheduler.poll_due(limit=1)
        if not due:
            if self.scheduler.diagnostics()["queue_depth"] or self.scheduler.diagnostics()["dropped"]:
                raise SmokeStop("oi_budget_blocked")
            return
        request = due[0]
        symbol = next((s for s in self.config.symbols
                       if self.registry[s].identity.instrument_id == request.instrument_id), None)
        if symbol is None:
            raise SmokeStop("unknown_oi_identity")
        result, completion, received = self._http(request, symbol=symbol)
        parsed = parse_binance_payload(result.body, "open_interest", self.registry,
                                       receive_time_ms=received, receive_monotonic_ns=self.clock.monotonic_ns())
        admitted = self.ingestion.admit(parsed, context=RestAdmissionContext(self.scheduler, completion,
                                       request.request_id, request.generation), now_ms=self.clock.now_ms())
        self.evidence.consume(admitted, now_ms=received)
        if len(admitted.events) != 1 or not self.oi.record_completion(
                self.scheduler, completion, admitted.events[0].event_time_ms, self.clock.now_ms()):
            raise SmokeStop("oi_result_not_current")

    def pump(self, supervisor, socket) -> None:
        for _ in range(4):
            actions = supervisor.transport.drain_actions()
            if supervisor.transport.dropped_actions:
                raise SmokeStop("control_action_loss")
            if not actions:
                return
            for action in actions:
                if action["action"] == "open":
                    self.current_operation, self.current_route = "ws_open", supervisor.shard.route.value
                    if socket.route is None:
                        socket.open(supervisor.shard.route)
                    elif socket.route != supervisor.shard.route:
                        raise SmokeStop("prepared_transport_route_mismatch")
                    supervisor.on_open(now_ms=self.clock.now_ms(), route=supervisor.shard.route)
                elif action["action"] == "send":
                    self.current_operation, self.current_route = "ws_control", supervisor.shard.route.value
                    message = action["message"]
                    if message.get("frame_type") == "pong":
                        key = supervisor.shard.shard_id, message["payload"]
                        payload = self.opaque_pings.pop(key, None)
                        if payload is None:
                            raise SmokeStop("unmatched_pong_token")
                        socket.send_pong(payload)
                    else:
                        socket.send_control(message)
                elif action["action"] == "close":
                    socket.close()
                else:
                    raise SmokeStop("unknown_transport_action")
        raise SmokeStop("control_pump_capacity")

    def receive(self, supervisor, socket) -> bool:
        try:
            return self._receive(supervisor, socket)
        except Exception as exc:
            reason = "sequence_gap" if isinstance(exc, SmokeStop) and str(exc) == "unexplained_trade_sequence_gap" else "parser_drop"
            supervisor.report_gap(now_ms=self.clock.now_ms(), reason=reason)
            if isinstance(exc, (ValueError, UnicodeError, RecursionError)):
                raise SmokeStop("malformed_public_frame") from None
            raise

    def _receive(self, supervisor, socket) -> bool:
        self.current_operation, self.current_route = "ws_receive", supervisor.shard.route.value
        frame = socket.recv()
        now, monotonic = self.clock.now_ms(), self.clock.monotonic_ns()
        if frame.kind == "timeout":
            return False
        if frame.kind == "fragment":
            return True
        self.frames += 1
        self.ws_bytes += len(frame.payload.encode() if isinstance(frame.payload, str) else frame.payload)
        if self.ws_bytes > 64 * 1024 * 1024:
            raise SmokeStop("smoke_byte_budget_exceeded")
        if frame.kind in {"ping", "pong"}:
            if not isinstance(frame.payload, bytes) or len(frame.payload) > 125:
                raise SmokeStop("invalid_public_control_payload")
            text = ""
            if frame.kind == "ping":
                if len(self.opaque_pings) >= 128:
                    raise SmokeStop("opaque_ping_capacity")
                self.ping_sequence += 1
                text = "opaque-ping-" + str(self.ping_sequence)
                self.opaque_pings[supervisor.shard.shard_id, text] = frame.payload
            if not supervisor.on_frame(text, epoch=supervisor.epoch, route=supervisor.shard.route,
                                       now_ms=now, frame_type=frame.kind):
                raise SmokeStop("control_frame_rejected")
            self.pump(supervisor, socket)
            return True
        if frame.kind == "close":
            supervisor.on_close(now_ms=now, epoch=supervisor.epoch)
            raise SmokeStop("unexpected_remote_close")
        if frame.kind != "data":
            raise SmokeStop("unsupported_public_frame")
        message = _decode_frame(frame.payload)
        if isinstance(message, dict) and "id" in message and "stream" not in message:
            if supervisor.on_ack(message, epoch=supervisor.epoch, route=supervisor.shard.route,
                                 now_ms=now) != "ACKNOWLEDGED":
                raise SmokeStop("subscription_ack_rejected")
            self.pump(supervisor, socket)
            return True
        from radars.altcoin_hunter.adapters.base import validate_combined_envelope
        stream, _, _ = validate_combined_envelope(message)
        stream = stream.lower()
        spec = next((s for s in supervisor.shard.streams if s.canonical_name == stream), None)
        if spec is None:
            supervisor.on_frame(message, epoch=supervisor.epoch, route=supervisor.shard.route, now_ms=now)
            raise SmokeStop("connection_frame_not_admitted")
        kind = "mark_price" if spec.kind == "mark_price_symbol" else spec.kind
        parsed = parse_binance_payload(message, kind, self.registry, receive_time_ms=now,
                                       receive_monotonic_ns=monotonic, connection_epoch=supervisor.epoch,
                                       route=supervisor.shard.route, funding_info=self.funding)
        context = AdmissionContext(supervisor.shard.route, supervisor.epoch,
                                   supervisor.state == ConnectionState.ACTIVE,
                                   stream in supervisor.acked, True)
        admitted = self.ingestion.admit(parsed, context=context, now_ms=self.clock.now_ms())
        self.evidence.consume(admitted, now_ms=now)
        if self.ingestion.dedup_evictions:
            raise SmokeStop("dedup_horizon_exhausted")
        if not supervisor.on_frame(message, epoch=supervisor.epoch, route=supervisor.shard.route, now_ms=now):
            raise SmokeStop("connection_frame_not_admitted")
        return True

    def capture(self) -> None:
        self.preflight_directory()
        # Complete both bounded handshakes before sending either subscription.
        # Otherwise the second handshake could consume the first ACK deadline.
        # These sockets receive no market data until the action ledger subscribes.
        for supervisor, socket in self.connections:
            self.current_operation, self.current_route = "ws_open", supervisor.shard.route.value
            socket.open(supervisor.shard.route)
        for supervisor, socket in self.connections:
            supervisor.start(self.clock.now_ms())
            self.pump(supervisor, socket)
        ready_at = None
        last_check = self.clock.now_ms()
        while self.clock.monotonic_ns() < self.deadline_ns - 1_000_000_000:
            now = self.clock.now_ms()
            progressed = False
            for supervisor, socket in self.connections:
                supervisor.step(now)
                self.pump(supervisor, socket)
                if supervisor.state not in {ConnectionState.SUBSCRIBING, ConnectionState.ACTIVE}:
                    raise SmokeStop("connection_not_healthy")
                for _ in range(128):
                    if self.clock.monotonic_ns() >= self.deadline_ns - 1_000_000_000:
                        break
                    if not self.receive(supervisor, socket):
                        break
                    progressed = True
                now = self.clock.now_ms()
            if all(s.state == ConnectionState.ACTIVE for s, _ in self.connections):
                ready_at = now if ready_at is None else ready_at
            if now - last_check >= 1000:
                if ready_at is not None and now - ready_at > 10_000:
                    self.evidence.freshness(now)
                if tracemalloc.is_tracing() and tracemalloc.get_traced_memory()[1] > 256 * 1024 * 1024:
                    raise SmokeStop("python_allocation_budget_exceeded")
                last_check = now
            # One due public REST read per iteration; it cannot block aggTrade
            # indefinitely. All socket buffers and this loop remain bounded.
            if now < self.deadline_ms - 6000:
                self.outstanding_requests.update(r.request_id for r in self.oi.schedule(self.scheduler, now))
                self.poll_oi()
            if not progressed:
                self.clock.pause(0.001)
        now = self.clock.now_ms()
        self.evidence.freshness(now, include_oi=True)
        if ready_at is None or now - ready_at < self.config.duration_sec * 1000 - 30_000:
            raise SmokeStop("insufficient_active_observation")
        self.last_oi_snapshot = self.oi.coverage(now)
        if self.last_oi_snapshot["degraded"]:
            raise SmokeStop("oi_coverage_incomplete")

    def close(self) -> bool:
        okay = True
        for request_id in tuple(self.outstanding_requests):
            self.scheduler.cancel(request_id)
        self.outstanding_requests.clear()
        self.budget.close()
        for supervisor, socket in self.connections:
            try:
                supervisor.stop(self.clock.now_ms())
                self.pump(supervisor, socket)
            except Exception:
                okay = False
            finally:
                try:
                    socket.close()
                except Exception:
                    okay = False
        self.opaque_pings.clear()
        try:
            self.rest.close()
        except Exception:
            okay = False
        diagnostics = self.scheduler.diagnostics()
        return (okay and not diagnostics["queue_depth"] and not diagnostics["inflight"]
                and all(s.snapshot()["cleanup_complete"] for s, _ in self.connections))

    def report(self, *, status: str, violations: list[str], peak: int | None) -> dict:
        snapshots = []
        intervals = []
        for supervisor, socket in self.connections:
            snapshot = supervisor.snapshot()
            # Do not relabel the offline supervisor's network_calls=0 as the
            # driver's I/O count. Keep the two provenance layers explicit.
            snapshots.append({"route": snapshot["route"], "state": snapshot["state"],
                              "epoch": snapshot["epoch"], "ack_id_strategy": snapshot["ack_id_strategy"],
                              "pending_ack_peak": snapshot["pending_ack_peak"],
                              "pending_acks": len(snapshot["pending_acks"]),
                              "control_peak_per_second": snapshot["control_peak_per_second"],
                              "required_streams": snapshot["required_streams"],
                              "cleanup_complete": snapshot["cleanup_complete"],
                              "counts": snapshot["counts"], "state_digest": snapshot["state_digest"],
                              "connection_attempts": socket.connection_attempts,
                              "controls_sent": socket.controls_sent})
            for row in supervisor.iter_coverage_records():
                intervals.append({**row, "complete": row["complete"] and status == "passed",
                                  "connection_interval_complete": row["complete"],
                                  "run_quality": "accepted" if status == "passed" else "uncertain",
                                  "evidence": "observed_transport_ack_route_epoch_liveness"})
        return {"report_version": 1, "tier": self.config.tier, "mode": "public_read_only_smoke",
                "status": status, "symbols": list(self.config.symbols),
                "requested_duration_sec": self.config.duration_sec,
                "actual_duration_ms": (self.clock.monotonic_ns() - self.start_ns) // 1_000_000,
                "started_at_ms": self.start_ms, "ended_at_ms": self.clock.now_ms(),
                "violations": violations, "network_scope": "binance_public_only",
                "real_send": False, "telegram_calls": 0, "production_writes": 0,
                "database_created": False, "schema_changed": False,
                "protocol_version": PROTOCOL_VERSION, "docs_checked_at": "2026-09-10",
                "preflight": self.preflight, "transport_failure": self.transport_failure,
                "http_attempts": self.rest.request_attempts,
                "completed_ws_messages_and_controls": self.frames,
                "physical_ws_frames": sum(getattr(sock, "frames_received", 0) for _, sock in self.connections),
                "completed_ws_message_payload_bytes": self.ws_bytes,
                "payload_bytes_are_not_wire_throughput": True, "python_allocation_peak_bytes": peak,
                "budget": self.budget.diagnostics(self.clock.now_ms()),
                "scheduler": self.scheduler.diagnostics(), "oi": self.last_oi_snapshot,
                "connections": snapshots, "trade_coverage_intervals": intervals,
                "coverage_semantics": "ACK-active intervals; startup/end partial minutes stay incomplete",
                "evidence": self.evidence.snapshot(), "dedup_evictions": self.ingestion.dedup_evictions,
                "dedup_horizon_limit": self.ingestion.max_dedup_keys,
                "long_running_validated": False, "shared_ip_production_budget_validated": False}


def run_public_smoke(config: SmokeConfig, *, output_dir: Path, deadline_ns: int | None = None) -> dict:
    """Run only from the supervised, explicitly enabled public CLI worker."""
    config.require_enabled()
    output = local_temporary_directory(output_dir)
    _write_report(output, seal_smoke_report({"status": "running", "mode": "public_read_only_smoke",
                                           "symbols": list(config.symbols), "real_send": False}))
    clock = RunClock()
    session = SmokeSession(config, clock=clock, deadline_ns=deadline_ns)
    guard = PublicNetworkGuard()
    tracing_was_active = tracemalloc.is_tracing()
    if not tracing_was_active:
        tracemalloc.start()
    violations: list[str] = []
    try:
        with guard:
            try:
                session.capture()
            finally:
                if not session.close():
                    violations.append("transport_cleanup_failed")
    except (SmokeStop, PublicTransportError) as exc:
        violations.append(str(exc))
        if isinstance(exc, PublicTransportError):
            session.transport_failure = {"reason": exc.reason, "operation": session.current_operation,
                                         "route": session.current_route, **dict(exc.metadata)}
    except Exception:
        violations.append("unexpected_smoke_failure")
    finally:
        peak = tracemalloc.get_traced_memory()[1]
        if not tracing_was_active:
            tracemalloc.stop()
    elapsed = (clock.monotonic_ns() - session.start_ns) // 1_000_000
    if guard.blocked_attempts:
        violations.append("network_allowlist_violation")
    if not violations and not config.duration_sec * 1000 - 1000 <= elapsed <= config.duration_sec * 1000:
        violations.append("smoke_duration_outside_gate")
    report = session.report(status="failed" if violations else "passed", violations=violations, peak=peak)
    report.update(dns_attempts=guard.dns_attempts, tcp_connection_attempts=guard.tcp_attempts,
                  blocked_network_attempts=guard.blocked_attempts)
    report = seal_smoke_report(report)
    _write_report(output, report)
    return report
