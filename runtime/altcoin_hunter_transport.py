"""Opt-in, bounded Binance public transport for an isolated short smoke run.

Constructing or importing this module does not import a network client, read
configuration, or open a socket. This is not a production shared-IP coordinator.
Only the caller may decide to enable network access and send budgeted controls.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Any, Callable, Mapping

from radars.altcoin_hunter.adapters.base import Route, identifier
from radars.altcoin_hunter.models import strict_int
from radars.altcoin_hunter.rest_budget import BudgetDecision, ENDPOINTS, RequestSpec


REST_ORIGIN = "https://fapi.binance.com"
WS_HOST = "fstream.binance.com"
_PATHS = frozenset(spec[0] for spec in ENDPOINTS.values())


class PublicTransportError(RuntimeError):
    """Only a stable reason and explicitly safe metadata may leave transport."""

    def __init__(self, reason: str, *, metadata: Mapping[str, Any] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.metadata = MappingProxyType(dict(metadata or {}))


def _seconds(value: Any, name: str, *, maximum: float = 30.0) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 < value <= maximum:
        raise ValueError(f"invalid_{name}")
    return float(value)


def _clock_value(clock: Callable[[], int]) -> int:
    value = clock()
    strict_int(value, "clock_ms")
    return value


def _symbol(value: Any) -> str:
    if (type(value) is not str or not 1 <= len(value) <= 32 or not value.isascii()
            or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for char in value)):
        raise PublicTransportError("invalid_public_symbol")
    return value


class SmokeRateBudget:
    """Finite, short-lived isolated-run allowance; never a live coordinator.

    A consumed attempt is not refunded after an error. The caller must stop on
    rate limiting; this object never sleeps, retries, or bypasses cooldown.
    """

    live_capable = True
    smoke_only = True

    def __init__(self, *, started_at_ms: int, deadline_ms: int,
                 max_requests: int = 100, weight_limit: int = 120,
                 funding_limit: int = 10) -> None:
        strict_int(started_at_ms, "started_at_ms")
        strict_int(deadline_ms, "deadline_ms", minimum=started_at_ms + 1,
                   maximum=started_at_ms + 600_000)
        strict_int(max_requests, "max_requests", minimum=1, maximum=1000)
        strict_int(weight_limit, "weight_limit", minimum=1, maximum=1200)
        strict_int(funding_limit, "funding_limit", minimum=1, maximum=100)
        self.started_at_ms, self.deadline_ms = started_at_ms, deadline_ms
        self.max_requests, self.weight_limit, self.funding_limit = max_requests, weight_limit, funding_limit
        self.attempts_total = 0
        self._last_ms = started_at_ms
        self._weight: deque[tuple[int, int]] = deque()
        self._funding: deque[int] = deque()
        self._reported_window = -1
        self._reported_weight = 0
        self._blocked_until = 0
        self._tickets: dict[str, RequestSpec] = {}
        self._reserved_ids: set[str] = set()
        self.closed = False

    def _advance(self, now_ms: int) -> None:
        strict_int(now_ms, "now_ms", minimum=self._last_ms)
        self._last_ms = now_ms
        while self._weight and self._weight[0][0] <= now_ms - 60_000:
            self._weight.popleft()
        while self._funding and self._funding[0] <= now_ms - 300_000:
            self._funding.popleft()
        if self._reported_window != now_ms // 60_000:
            self._reported_window, self._reported_weight = now_ms // 60_000, 0
        for request_id, request in tuple(self._tickets.items()):
            if now_ms >= request.deadline_ms:
                del self._tickets[request_id]

    def reserve(self, request: RequestSpec, now_ms: int) -> BudgetDecision:
        self._advance(now_ms)
        if type(request) is not RequestSpec:
            raise ValueError("request_spec_required")
        if self.closed:
            return BudgetDecision(False, self.deadline_ms, "smoke_budget_closed")
        if now_ms >= self.deadline_ms:
            return BudgetDecision(False, self.deadline_ms, "smoke_deadline")
        if now_ms < self._blocked_until:
            return BudgetDecision(False, self._blocked_until, "source_cooldown")
        if self.attempts_total >= self.max_requests:
            return BudgetDecision(False, self.deadline_ms, "smoke_request_limit")
        if request.retry_count:
            return BudgetDecision(False, self.deadline_ms, "smoke_retries_disabled")
        if request.request_id in self._reserved_ids:
            return BudgetDecision(False, self.deadline_ms, "dispatch_already_reserved")
        used = max(sum(weight for _, weight in self._weight), self._reported_weight)
        if used + request.logical_weight > self.weight_limit:
            return BudgetDecision(False, self.deadline_ms, "smoke_weight_limit")
        if request.budget_class == "funding_requests" and len(self._funding) >= self.funding_limit:
            return BudgetDecision(False, self.deadline_ms, "smoke_funding_limit")
        self.attempts_total += 1
        self._tickets[request.request_id] = request
        self._reserved_ids.add(request.request_id)
        self._weight.append((now_ms, request.logical_weight))
        if request.budget_class == "funding_requests":
            self._funding.append(now_ms)
        return BudgetDecision(True, now_ms, "smoke_admitted")

    def consume_dispatch(self, request: RequestSpec, now_ms: int) -> None:
        """Consume poll_due's exact-object dispatch once without a second charge."""
        self._advance(now_ms)
        if self.closed:
            raise PublicTransportError("smoke_budget_closed")
        ticket = self._tickets.get(request.request_id)
        if ticket is not request:
            raise PublicTransportError("missing_dispatch_ticket")
        if now_ms >= min(request.deadline_ms, self.deadline_ms) or now_ms < request.not_before_ms:
            raise PublicTransportError("dispatch_ticket_expired")
        if now_ms < self._blocked_until:
            raise PublicTransportError("source_cooldown")
        del self._tickets[request.request_id]

    def cancel_dispatch(self, request_id: str) -> bool:
        """Discard an unconsumed ticket without refunding its reserved attempt."""
        identifier(request_id, "request_id")
        return self._tickets.pop(request_id, None) is not None

    def close(self) -> None:
        """Close dispatch admission and clear pending tickets, retaining totals."""
        self.closed = True
        self._tickets.clear()

    def observe(self, request: RequestSpec, headers: Mapping[str, str], now_ms: int) -> None:
        self._advance(now_ms)
        if request.endpoint.endswith("bookTicker"):
            return  # Binance documents this endpoint's weight header as inaccurate.
        value = next((value for key, value in headers.items()
                      if key.lower() == "x-mbx-used-weight-1m"), None)
        if value is not None:
            if type(value) is not str or not value.isascii() or not value.isdigit() or len(value) > 12:
                raise PublicTransportError("invalid_used_weight_header")
            self._reported_weight = max(self._reported_weight, int(value))
            if self._reported_weight >= self.weight_limit:
                raise PublicTransportError("observed_smoke_weight_limit")

    def cooldown(self, until_ms: int, *, reason: str) -> None:
        strict_int(until_ms, "until_ms")
        self._blocked_until = max(self._blocked_until, until_ms)

    def diagnostics(self, now_ms: int) -> dict[str, Any]:
        self._advance(now_ms)
        return {"scope": "isolated_smoke_only", "production_shared_budget": False,
                "closed": self.closed,
                "attempts_total": self.attempts_total, "max_requests": self.max_requests,
                "rolling_weight": sum(weight for _, weight in self._weight),
                "reported_weight": self._reported_weight, "weight_limit": self.weight_limit,
                "funding_requests": len(self._funding), "deadline_ms": self.deadline_ms,
                "pending_dispatch_tickets": len(self._tickets),
                "cooldown_until_ms": self._blocked_until}


@dataclass(frozen=True)
class HttpResult:
    body: bytes
    status: int
    metadata: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


def _safe_headers(headers: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {"content_length": None, "used_weight_1m": None,
                              "retry_after": None, "content_encoding": "identity"}
    allowed = {"content-length": "content_length", "x-mbx-used-weight-1m": "used_weight_1m",
               "retry-after": "retry_after", "content-encoding": "content_encoding"}
    for key, value in headers.items():
        name = allowed.get(key.lower()) if type(key) is str else None
        if name is None:
            continue
        if (type(value) is not str or len(value) > 128
                or any(ord(char) < 32 or ord(char) > 126 for char in value)):
            raise PublicTransportError("invalid_public_response_header")
        if name in {"content_length", "used_weight_1m"}:
            if not value.isdigit() or len(value) > 12:
                raise PublicTransportError("invalid_public_response_header")
            result[name] = int(value)
        else:
            result[name] = value
    return result


class PublicRestClient:
    """GET-only client. No session, cookies, proxies or dependencies at init."""

    def __init__(self, *, budget: SmokeRateBudget, clock_ms: Callable[[], int],
                 allow_network: bool = False, timeout_sec: float = 5.0,
                 exchange_info_max_bytes: int = 8 * 1024 * 1024,
                 max_body_bytes: int = 1_000_000, session_factory: Callable | None = None) -> None:
        if type(budget) is not SmokeRateBudget:
            raise ValueError("explicit_smoke_rate_budget_required")
        if type(allow_network) is not bool or not callable(clock_ms):
            raise ValueError("invalid_transport_configuration")
        self.timeout_sec = _seconds(timeout_sec, "timeout")
        strict_int(exchange_info_max_bytes, "exchange_info_max_bytes", minimum=1, maximum=32 * 1024 * 1024)
        strict_int(max_body_bytes, "max_body_bytes", minimum=1, maximum=8 * 1024 * 1024)
        self.budget, self.clock_ms, self.allow_network = budget, clock_ms, allow_network
        self.exchange_info_max_bytes, self.max_body_bytes = exchange_info_max_bytes, max_body_bytes
        self._session_factory, self._session = session_factory, None
        self.request_attempts = 0
        self.closed = False

    def _open(self) -> None:
        if not self.allow_network:
            raise PublicTransportError("network_disabled")
        if self.closed:
            raise PublicTransportError("transport_closed")
        if self._session is None:
            factory = self._session_factory
            if factory is None:
                import requests
                factory = requests.Session
            session = factory()
            session.trust_env = False
            session.auth = None
            session.proxies.clear()
            session.headers.clear()
            session.cookies.clear()
            session.hooks = {"response": []}
            self._session = session

    def request(self, request: RequestSpec, *, symbol: str | None = None,
                now_ms: int | None = None) -> HttpResult:
        if type(request) is not RequestSpec or request.endpoint not in _PATHS or request.method != "GET":
            raise PublicTransportError("unsupported_public_rest_endpoint")
        if request.retry_count:
            raise PublicTransportError("smoke_retries_disabled")
        needs_symbol = request.endpoint.endswith("openInterest") or request.instrument_id is not None
        params = {"symbol": _symbol(symbol)} if needs_symbol else {}
        if not needs_symbol and symbol is not None:
            raise PublicTransportError("unexpected_public_symbol")
        # No arbitrary query object is accepted, so credentials/account options
        # cannot enter this client through request parameters.
        now = _clock_value(self.clock_ms)
        if now_ms is not None:
            strict_int(now_ms, "now_ms", maximum=now)
        deadline = min(request.deadline_ms, self.budget.deadline_ms)
        if now < request.not_before_ms or now >= deadline:
            raise PublicTransportError("request_outside_deadline")
        if not self.allow_network:
            raise PublicTransportError("network_disabled")
        self.budget.consume_dispatch(request, now)
        limit = self.exchange_info_max_bytes if request.endpoint.endswith("exchangeInfo") else self.max_body_bytes
        timeout = min(self.timeout_sec, (deadline - now) / 1000)
        response = None
        metadata: dict[str, Any] = {"endpoint": request.endpoint, "request_id": request.request_id,
                                   "generation": request.generation, "body_bytes": 0,
                                   "http_status": None, "content_length": None,
                                   "used_weight_1m": None, "retry_after": None}
        try:
            self._open()
            self._session.cookies.clear()
            self.request_attempts += 1
            response = self._session.get(REST_ORIGIN + request.endpoint, params=params,
                                         timeout=(timeout, timeout), allow_redirects=False,
                                         stream=True, verify=True,
                                         headers={"Accept": "application/json", "Accept-Encoding": "identity"})
            metadata["http_status"] = response.status_code
            metadata.update(_safe_headers(response.headers))
            if response.status_code in (418, 429):
                self.budget.cooldown(self.budget.deadline_ms, reason="rate_limited")
                raise PublicTransportError("rate_limited", metadata=metadata)
            if response.status_code != 200:
                raise PublicTransportError("unexpected_http_status", metadata=metadata)
            if metadata["content_encoding"].lower() not in {"", "identity"}:
                raise PublicTransportError("unsupported_content_encoding", metadata=metadata)
            if metadata["content_length"] is not None and metadata["content_length"] > limit:
                raise PublicTransportError("response_body_too_large", metadata=metadata)
            used = metadata["used_weight_1m"]
            self.budget.observe(request, {} if used is None else {"x-mbx-used-weight-1m": str(used)},
                                _clock_value(self.clock_ms))
            body = bytearray()
            while True:
                if _clock_value(self.clock_ms) >= deadline:
                    raise PublicTransportError("request_deadline_exceeded", metadata=metadata)
                remaining_timeout = min(self.timeout_sec, (deadline - _clock_value(self.clock_ms)) / 1000)
                raw_socket = getattr(getattr(response.raw, "_connection", None), "sock", None)
                if raw_socket is not None:
                    raw_socket.settimeout(remaining_timeout)
                # read1 performs at most one underlying body read instead of
                # filling the complete requested chunk under a slow trickle.
                reader = getattr(response.raw, "read1", None)
                if not callable(reader):
                    raise PublicTransportError("bounded_http_reader_unavailable", metadata=metadata)
                chunk = reader(min(8192, limit + 1 - len(body)), decode_content=False)
                if type(chunk) is not bytes:
                    raise PublicTransportError("invalid_response_body", metadata=metadata)
                body.extend(chunk)
                metadata["body_bytes"] = len(body)
                if len(body) > limit:
                    raise PublicTransportError("response_body_too_large", metadata=metadata)
                if not chunk:
                    break
            if _clock_value(self.clock_ms) >= deadline:
                raise PublicTransportError("request_deadline_exceeded", metadata=metadata)
            if metadata["content_length"] is not None and metadata["content_length"] != len(body):
                raise PublicTransportError("content_length_mismatch", metadata=metadata)
            return HttpResult(bytes(body), response.status_code, metadata)
        except PublicTransportError as exc:
            self.close()
            if not exc.metadata:
                raise PublicTransportError(exc.reason, metadata=metadata) from None
            raise
        except Exception as exc:
            # Never interpolate exception text: requests may include URLs or
            # response headers. Classifying a timeout requires no client import.
            reason = "public_request_timeout" if "timeout" in type(exc).__name__.lower() else "public_request_failed"
            self.close()
            raise PublicTransportError(reason, metadata=metadata) from None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    self.closed = True
                    raise PublicTransportError("public_response_close_failed", metadata=metadata) from None
            if self._session is not None:
                self._session.cookies.clear()

    def close(self) -> None:
        session, self._session = self._session, None
        self.closed = True
        if session is not None:
            try:
                session.close()
            except Exception:
                raise PublicTransportError("public_session_close_failed") from None


@dataclass(frozen=True)
class PublicFrame:
    kind: str
    payload: str | bytes


class _DeadlineSocket:
    """Reapply remaining time before every underlying frame read or write."""

    def __init__(self, sock: Any, clock_ms: Callable[[], int], deadline_ms: int, timeout_sec: float) -> None:
        self._socket, self._clock = sock, clock_ms
        self._deadline, self._timeout = deadline_ms, timeout_sec

    def _prepare(self) -> None:
        remaining = (self._deadline - _clock_value(self._clock)) / 1000
        if remaining <= 0:
            raise PublicTransportError("smoke_deadline")
        self._socket.settimeout(min(self._timeout, remaining))

    def recv(self, count: int) -> bytes:
        self._prepare()
        return self._socket.recv(count)

    def send(self, payload: bytes) -> int:
        self._prepare()
        return self._socket.send(payload)

    def settimeout(self, value: float) -> None:
        self._timeout = value
        self._prepare()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._socket, name)


def _handshake(sock: Any, route: Route, *, clock_ms: Callable[[], int],
               deadline_ms: int, max_header_bytes: int = 16_384) -> None:
    """Bounded RFC6455 handshake without websocket-client's cookie jar."""
    import base64
    import hmac
    import os

    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (f"GET {route.path} HTTP/1.1\r\nHost: {WS_HOST}\r\nUpgrade: websocket\r\n"
               f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
    remaining = deadline_ms - _clock_value(clock_ms)
    if remaining <= 0:
        raise PublicTransportError("websocket_handshake_deadline")
    sock.settimeout(remaining / 1000)
    sock.sendall(request.encode("ascii"))
    header = bytearray()
    while not header.endswith(b"\r\n\r\n"):
        remaining = deadline_ms - _clock_value(clock_ms)
        if remaining <= 0:
            raise PublicTransportError("websocket_handshake_deadline")
        sock.settimeout(remaining / 1000)
        if len(header) >= max_header_bytes:
            raise PublicTransportError("websocket_handshake_too_large")
        part = sock.recv(1)
        if not part:
            raise PublicTransportError("websocket_handshake_closed")
        header.extend(part)
    try:
        lines = header.decode("ascii").split("\r\n")
        status = int(lines[0].split(" ")[1])
    except (UnicodeError, ValueError, IndexError):
        raise PublicTransportError("invalid_websocket_handshake") from None
    if status != 101:
        reason = "rate_limited" if status in (418, 429) else "unexpected_websocket_status"
        raise PublicTransportError(reason, metadata={"http_status": status, "header_bytes": len(header)})
    headers: dict[str, str] = {}
    for line in lines[1:-2]:
        if ":" not in line or line[0].isspace():
            raise PublicTransportError("invalid_websocket_handshake")
        name, value = line.split(":", 1)
        name, value = name.lower(), value.strip()
        if name in headers:
            raise PublicTransportError("duplicate_websocket_header")
        headers[name] = value
    expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()).decode("ascii")
    if (headers.get("upgrade", "").lower() != "websocket"
            or "upgrade" not in {token.strip().lower() for token in headers.get("connection", "").split(",")}
            or not hmac.compare_digest(headers.get("sec-websocket-accept", ""), expected)
            or "sec-websocket-extensions" in headers or "sec-websocket-protocol" in headers):
        raise PublicTransportError("invalid_websocket_handshake")


def _bound_frame_buffer(ws: Any, max_frame_bytes: int) -> None:
    """Check the decoded length before websocket-client reads payload bytes."""
    buffer = ws.frame_buffer
    original = buffer.recv_length

    def recv_length() -> None:
        original()
        if type(buffer.length) is not int or buffer.length < 0 or buffer.length > max_frame_bytes:
            raise PublicTransportError("websocket_frame_too_large")
        fin, rsv1, rsv2, rsv3, opcode, masked, _ = buffer.header
        if rsv1 or rsv2 or rsv3 or masked or opcode not in (0, 1, 8, 9, 10):
            raise PublicTransportError("unsupported_websocket_frame")
        if opcode >= 8 and (not fin or buffer.length > 125):
            raise PublicTransportError("invalid_websocket_control_frame")

    buffer.recv_length = recv_length


class PublicWebSocket:
    """Synchronous fixed-host public socket; no automatic ping/pong or tasks."""

    def __init__(self, *, clock_ms: Callable[[], int], deadline_ms: int,
                 allowed_symbols: tuple[str, ...],
                 allow_network: bool = False, timeout_sec: float = 1.0,
                 connect_timeout_sec: float = 5.0, max_frame_bytes: int = 1_000_000,
                 max_connections: int = 3, socket_factory: Callable | None = None,
                 ws_factory: Callable | None = None) -> None:
        if type(allow_network) is not bool or not callable(clock_ms):
            raise ValueError("invalid_transport_configuration")
        strict_int(deadline_ms, "deadline_ms")
        strict_int(max_frame_bytes, "max_frame_bytes", minimum=1, maximum=8_000_000)
        strict_int(max_connections, "max_connections", minimum=1, maximum=10)
        if (type(allowed_symbols) is not tuple or not 1 <= len(allowed_symbols) <= 20
                or len(set(allowed_symbols)) != len(allowed_symbols)):
            raise ValueError("explicit_smoke_symbols_required")
        self.allowed_symbols = frozenset(_symbol(symbol) for symbol in allowed_symbols)
        self.timeout_sec = _seconds(timeout_sec, "timeout")
        self.connect_timeout_sec = _seconds(connect_timeout_sec, "connect_timeout")
        self.clock_ms, self.deadline_ms = clock_ms, deadline_ms
        self.allow_network, self.max_frame_bytes = allow_network, max_frame_bytes
        self.max_connections, self.connection_attempts = max_connections, 0
        self._socket_factory, self._ws_factory = socket_factory, ws_factory
        self._ws = None
        self._fragment: bytearray | None = None
        self.route: Route | None = None
        self.frames_received = 0
        self.controls_sent = 0
        self._control_times: deque[int] = deque()

    def _remaining(self, ceiling: float) -> float:
        remaining = (self.deadline_ms - _clock_value(self.clock_ms)) / 1000
        if remaining <= 0:
            raise PublicTransportError("smoke_deadline")
        return min(ceiling, remaining)

    def open(self, route: Route) -> None:
        if type(route) is not Route:
            raise PublicTransportError("invalid_websocket_route")
        if not self.allow_network:
            raise PublicTransportError("network_disabled")
        if self._ws is not None:
            raise PublicTransportError("websocket_already_open")
        if self.connection_attempts >= self.max_connections:
            raise PublicTransportError("smoke_connection_limit")
        timeout = self._remaining(self.connect_timeout_sec)
        connect_deadline = min(self.deadline_ms, _clock_value(self.clock_ms) + int(timeout * 1000))
        self.connection_attempts += 1
        sock = None
        try:
            if self._socket_factory is None:
                import socket
                import ssl
                raw = socket.create_connection((WS_HOST, 443), timeout=timeout)
                try:
                    context = ssl.create_default_context()
                    remaining = (connect_deadline - _clock_value(self.clock_ms)) / 1000
                    if remaining <= 0:
                        raise PublicTransportError("websocket_open_timeout")
                    raw.settimeout(remaining)
                    sock = context.wrap_socket(raw, server_hostname=WS_HOST)
                except Exception:
                    raw.close()
                    raise
            else:
                sock = self._socket_factory(WS_HOST, 443, timeout)
            _handshake(sock, route, clock_ms=self.clock_ms,
                       deadline_ms=connect_deadline)
            factory = self._ws_factory
            if factory is None:
                import websocket
                factory = websocket.WebSocket
            ws = factory(enable_multithread=False, skip_utf8_validation=False)
            ws.sock, ws.connected = _DeadlineSocket(sock, self.clock_ms, self.deadline_ms, self.timeout_sec), True
            ws.settimeout(self._remaining(self.timeout_sec))
            _bound_frame_buffer(ws, self.max_frame_bytes)
            self._ws, self.route = ws, route
        except PublicTransportError:
            if sock is not None:
                sock.close()
            raise
        except Exception as exc:
            if sock is not None:
                sock.close()
            reason = "websocket_open_timeout" if "timeout" in type(exc).__name__.lower() else "websocket_open_failed"
            raise PublicTransportError(reason) from None

    def _connected(self) -> Any:
        if self._ws is None:
            raise PublicTransportError("websocket_not_open")
        self._ws.settimeout(self._remaining(self.timeout_sec))
        return self._ws

    def send_control(self, message: Mapping[str, Any]) -> None:
        if type(message) is not dict or set(message) != {"method", "params", "id"}:
            raise PublicTransportError("invalid_subscription_control")
        if message["method"] not in {"SUBSCRIBE", "UNSUBSCRIBE"}:
            raise PublicTransportError("unsupported_subscription_method")
        ack = message["id"]
        if not (type(ack) is int and 1 <= ack <= 2**63 - 1 or type(ack) is str and 1 <= len(ack) <= 64
                and ack.isascii() and all(33 <= ord(char) <= 126 for char in ack)):
            raise PublicTransportError("invalid_ack_id")
        params = message["params"]
        if type(params) is not list or not 1 <= len(params) <= 50:
            raise PublicTransportError("invalid_subscription_batch")
        for stream in params:
            if type(stream) is not str or len(stream) > 128:
                raise PublicTransportError("unsupported_public_stream")
            if re.fullmatch(r"[a-z0-9_]{1,64}@(aggTrade|markPrice|markPrice@1s)", stream):
                route = Route.MARKET
            elif re.fullmatch(r"[a-z0-9_]{1,64}@bookTicker", stream):
                route = Route.PUBLIC
            else:
                raise PublicTransportError("unsupported_public_stream")
            if stream.split("@", 1)[0].upper() not in self.allowed_symbols:
                raise PublicTransportError("symbol_outside_smoke_scope")
            if route is not self.route:
                raise PublicTransportError("wrong_route")
        encoded = json.dumps(message, separators=(",", ":"), allow_nan=False)
        self._send(encoded, 1)

    def _send(self, payload: str | bytes, opcode: int) -> None:
        try:
            now = _clock_value(self.clock_ms)
            while self._control_times and self._control_times[0] <= now - 1000:
                self._control_times.popleft()
            if len(self._control_times) >= 8:
                raise PublicTransportError("websocket_control_rate_limit")
            self._control_times.append(now)
            self._connected().send(payload, opcode=opcode)
            self.controls_sent += 1
        except PublicTransportError:
            self.close()
            raise
        except Exception:
            self.close()
            raise PublicTransportError("websocket_control_failed") from None

    def send_pong(self, payload: bytes) -> None:
        if type(payload) is not bytes or len(payload) > 125:
            raise PublicTransportError("invalid_pong_payload")
        self._send(payload, 10)

    def fileno(self) -> int:
        """Read-only descriptor for the owner's bounded select loop."""
        return -1 if self._ws is None else self._ws.sock.fileno()

    def recv(self) -> PublicFrame:
        try:
            frame = self._connected().recv_frame()  # recv() would auto-send PONG.
            self.frames_received += 1
            data = frame.data
            if type(data) is not bytes or len(data) > self.max_frame_bytes:
                raise PublicTransportError("invalid_websocket_frame")
            if frame.opcode in (8, 9, 10):
                return PublicFrame({8: "close", 9: "ping", 10: "pong"}[frame.opcode], data)
            if frame.opcode == 1:
                if self._fragment is not None:
                    raise PublicTransportError("invalid_websocket_fragment")
                self._fragment = bytearray(data)
            elif frame.opcode == 0 and self._fragment is not None:
                if len(self._fragment) + len(data) > self.max_frame_bytes:
                    raise PublicTransportError("websocket_message_too_large")
                self._fragment.extend(data)
            else:
                raise PublicTransportError("invalid_websocket_fragment")
            if not frame.fin:
                return PublicFrame("fragment", b"")
            text = self._fragment.decode("utf-8", errors="strict")
            self._fragment = None
            return PublicFrame("data", text)
        except PublicTransportError:
            self.close()
            raise
        except UnicodeError:
            self.close()
            raise PublicTransportError("invalid_websocket_utf8") from None
        except Exception as exc:
            # A finite read timeout is an idle poll, not evidence of a healthy
            # connection. The supervisor owns liveness and smoke-stop decisions.
            if "timeout" in type(exc).__name__.lower():
                return PublicFrame("timeout", b"")
            self.close()
            raise PublicTransportError("websocket_receive_failed") from None

    def close(self) -> None:
        ws, self._ws = self._ws, None
        self._fragment = None
        self._control_times.clear()
        self.route = None
        if ws is not None:
            # No blocking closing-handshake loop and no implicit control frame.
            try:
                ws.shutdown()
            except Exception:
                raise PublicTransportError("websocket_close_failed") from None
