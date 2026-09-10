"""Transport boundaries with injected sockets/sessions; never real networking."""
from __future__ import annotations

from dataclasses import replace
import base64
import hashlib
import io
import json
from types import SimpleNamespace
import unittest

from radars.altcoin_hunter.adapters.base import Route
from runtime.altcoin_hunter_transport import (
    PublicRestClient, PublicTransportError, PublicWebSocket, REST_ORIGIN,
    SmokeRateBudget, WS_HOST, _bound_frame_buffer,
)
from radars.altcoin_hunter.rest_budget import FakeCoordinator, make_request


class Clock:
    def __init__(self, now=0):
        self.now = now

    def __call__(self):
        return self.now


class RawBody:
    def __init__(self, body):
        self.body = io.BytesIO(body)
        self.read_sizes = []

    def read(self, count, *, decode_content):
        if decode_content is not False:
            raise AssertionError("compressed_response_must_not_expand")
        self.read_sizes.append(count)
        return self.body.read(count)

    read1 = read


class Response:
    def __init__(self, body=b"{}", status=200, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.raw = RawBody(body)
        self.closed = False

    def close(self):
        self.closed = True


class Session:
    def __init__(self, response):
        self.response = response
        self.cookies = {"private": "never-send"}
        self.headers = {"Authorization": "never-send"}
        self.proxies = {"https": "never-use"}
        self.auth = ("never", "send")
        self.trust_env = True
        self.hooks = {}
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response

    def close(self):
        self.closed = True


class Socket:
    def __init__(self, *, status=101, extra_headers="", invalid_accept=False):
        self.status, self.extra_headers = status, extra_headers
        self.invalid_accept = invalid_accept
        self.outgoing = []
        self.incoming = io.BytesIO()
        self.closed = False
        self.timeouts = []

    def sendall(self, value):
        self.outgoing.append(value)
        key = next(line.split(": ", 1)[1] for line in value.decode("ascii").split("\r\n")
                   if line.startswith("Sec-WebSocket-Key:"))
        accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
        if self.invalid_accept:
            accept = "invalid"
        data = (f"HTTP/1.1 {self.status} Status\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n{self.extra_headers}\r\n").encode("ascii")
        self.incoming = io.BytesIO(data)

    def recv(self, count):
        return self.incoming.read(count)

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def close(self):
        self.closed = True


class FrameBuffer:
    def __init__(self):
        self.header = (1, 0, 0, 0, 1, 0, 0)
        self.length = 0
        self.advertised_length = 0
        self.payload_reads = 0

    def recv_length(self):
        self.length = self.advertised_length


def frame(opcode=1, data=b"{}", fin=1):
    return SimpleNamespace(opcode=opcode, data=data, fin=fin)


class WebSocket:
    def __init__(self, frames=()):
        self.frames = list(frames)
        self.frame_buffer = FrameBuffer()
        self.sock = None
        self.connected = False
        self.sent = []
        self.timeouts = []
        self.shutdown_count = 0

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def recv_frame(self):
        item = self.frames.pop(0)
        if isinstance(item, Exception):
            raise item
        self.frame_buffer.header = (item.fin, 0, 0, 0, item.opcode, 0, 0)
        self.frame_buffer.advertised_length = len(item.data)
        self.frame_buffer.recv_length()
        self.frame_buffer.payload_reads += 1
        return item

    def send(self, payload, *, opcode):
        self.sent.append((opcode, payload))

    def shutdown(self):
        self.shutdown_count += 1
        self.sock.close()
        self.sock = None
        self.connected = False


class PublicRestTests(unittest.TestCase):
    def dispatch(self, client, request, **kwargs):
        decision = client.budget.reserve(request, client.clock_ms())
        if not decision.allowed:
            raise PublicTransportError(decision.reason)
        return client.request(request, **kwargs)

    def rest(self, response=None, **kwargs):
        clock = Clock()
        budget = SmokeRateBudget(started_at_ms=0, deadline_ms=180_000)
        session = Session(response or Response())
        client = PublicRestClient(budget=budget, clock_ms=clock, allow_network=True,
                                  session_factory=lambda: session, **kwargs)
        return client, session, clock, budget

    def test_constructor_does_not_construct_session_and_defaults_off(self):
        calls = []
        client = PublicRestClient(budget=SmokeRateBudget(started_at_ms=0, deadline_ms=180_000),
                                  clock_ms=Clock(), session_factory=lambda: calls.append(1))
        self.assertEqual(calls, [])
        with self.assertRaisesRegex(PublicTransportError, "network_disabled"):
            self.dispatch(client, make_request("exchangeInfo", 0))
        self.assertEqual(calls, [])

    def test_explicit_finite_smoke_budget_required(self):
        for budget in (None, FakeCoordinator(), object()):
            with self.subTest(budget=type(budget).__name__), self.assertRaises(ValueError):
                PublicRestClient(budget=budget, clock_ms=Clock())
        for kwargs in ({"deadline_ms": 600_001}, {"max_requests": 0}, {"weight_limit": True},
                       {"funding_limit": 0}):
            config = {"started_at_ms": 0, "deadline_ms": 180_000, **kwargs}
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                SmokeRateBudget(**config)

    def test_only_fixed_origin_get_no_redirect_environment_proxy_or_credentials(self):
        client, session, _, _ = self.rest()
        result = self.dispatch(client, make_request("exchangeInfo", 0))
        url, options = session.calls[0]
        self.assertEqual(url, REST_ORIGIN + "/fapi/v1/exchangeInfo")
        self.assertEqual(options["params"], {})
        self.assertFalse(options["allow_redirects"])
        self.assertTrue(options["verify"])
        self.assertTrue(options["stream"])
        self.assertEqual(options["headers"]["Accept-Encoding"], "identity")
        self.assertFalse(session.trust_env)
        self.assertIsNone(session.auth)
        self.assertEqual((session.cookies, session.headers, session.proxies), ({}, {}, {}))
        self.assertEqual(result.body, b"{}")
        self.assertTrue(session.response.closed)

    def test_private_endpoint_rejected_even_if_dataclass_is_tampered(self):
        client, session, _, _ = self.rest()
        request = make_request("exchangeInfo", 0)
        object.__setattr__(request, "endpoint", "/fapi/v1/order")
        with self.assertRaisesRegex(PublicTransportError, "unsupported_public_rest_endpoint"):
            self.dispatch(client, request)
        self.assertEqual(session.calls, [])

    def test_oi_symbol_is_explicit_and_strict(self):
        client, session, _, _ = self.rest()
        request = make_request("openInterest", 0, instrument_id="binance:usdt_perpetual:BTCUSDT")
        for symbol in (None, True, "BTCUSDT&signature=x", " btcusdt", "BTC\nUSDT"):
            with self.subTest(symbol=symbol), self.assertRaises(PublicTransportError):
                client.request(request, symbol=symbol)
        self.dispatch(client, request, symbol="BTCUSDT")
        self.assertEqual(session.calls[0][1]["params"], {"symbol": "BTCUSDT"})

    def test_unknown_parameters_and_retry_fail_before_request(self):
        client, session, _, _ = self.rest()
        with self.assertRaisesRegex(PublicTransportError, "unexpected_public_symbol"):
            self.dispatch(client, make_request("exchangeInfo", 0), symbol="BTCUSDT")
        with self.assertRaisesRegex(PublicTransportError, "smoke_retries_disabled"):
            self.dispatch(client, replace(make_request("exchangeInfo", 0), retry_count=1))
        self.assertEqual(session.calls, [])

    def test_status_and_safe_metadata_without_headers_or_body_dump(self):
        response = Response(b"{}", headers={"Content-Length": "2", "X-MBX-USED-WEIGHT-1M": "2",
                                            "Authorization": "secret", "Set-Cookie": "secret"})
        client, _, _, _ = self.rest(response)
        result = self.dispatch(client, make_request("serverTime", 0))
        self.assertEqual((result.status, result.metadata["body_bytes"], result.metadata["content_length"]), (200, 2, 2))
        self.assertEqual(result.metadata["used_weight_1m"], 2)
        self.assertNotIn("secret", json.dumps(dict(result.metadata)))
        with self.assertRaises(TypeError):
            result.metadata["body_bytes"] = 5

    def test_rate_limit_stops_without_retry_and_retains_status(self):
        for status in (418, 429):
            client, session, _, budget = self.rest(Response(status=status, headers={"Retry-After": "60"}))
            with self.subTest(status=status), self.assertRaises(PublicTransportError) as caught:
                self.dispatch(client, make_request("exchangeInfo", 0))
            self.assertEqual(caught.exception.reason, "rate_limited")
            self.assertEqual(caught.exception.metadata["http_status"], status)
            self.assertEqual(caught.exception.metadata["retry_after"], "60")
            self.assertEqual(len(session.calls), 1)
            self.assertEqual(session.response.raw.read_sizes, [])
            self.assertFalse(budget.reserve(make_request("serverTime", 0), 0).allowed)

    def test_redirect_and_other_error_status_never_followed(self):
        for status in (301, 302, 307, 308, 400, 401, 403, 404, 408, 500, 503):
            client, session, _, _ = self.rest(Response(status=status, headers={"Location": "https://private.example/secret"}))
            with self.subTest(status=status), self.assertRaisesRegex(PublicTransportError, "unexpected_http_status"):
                self.dispatch(client, make_request("exchangeInfo", 0))
            self.assertEqual(len(session.calls), 1)
            self.assertEqual(session.response.raw.read_sizes, [])

    def test_body_limit_honors_content_length_before_read(self):
        response = Response(b"123456", headers={"Content-Length": "6"})
        client, _, _, _ = self.rest(response, exchange_info_max_bytes=5)
        with self.assertRaisesRegex(PublicTransportError, "response_body_too_large"):
            self.dispatch(client, make_request("exchangeInfo", 0))
        self.assertEqual(response.raw.read_sizes, [])

    def test_body_limit_without_length_reads_only_limit_plus_one(self):
        response = Response(b"123456789")
        client, _, _, _ = self.rest(response, exchange_info_max_bytes=5)
        with self.assertRaises(PublicTransportError) as caught:
            self.dispatch(client, make_request("exchangeInfo", 0))
        self.assertEqual(caught.exception.reason, "response_body_too_large")
        self.assertEqual(caught.exception.metadata["body_bytes"], 6)
        self.assertEqual(response.raw.read_sizes, [6])

    def test_exchange_info_and_other_body_limits_are_independent(self):
        client, _, _, _ = self.rest(Response(b"12345"), max_body_bytes=2, exchange_info_max_bytes=6)
        self.assertEqual(self.dispatch(client, make_request("exchangeInfo", 0)).body, b"12345")
        client, _, _, _ = self.rest(Response(b"12345"), max_body_bytes=2, exchange_info_max_bytes=6)
        with self.assertRaisesRegex(PublicTransportError, "response_body_too_large"):
            self.dispatch(client, make_request("serverTime", 0))

    def test_compression_invalid_headers_and_truncation_fail_closed(self):
        for headers, reason in (({"Content-Encoding": "gzip"}, "unsupported_content_encoding"),
                                ({"Content-Length": "NaN"}, "invalid_public_response_header"),
                                ({"Content-Length": "3"}, "content_length_mismatch")):
            client, _, _, _ = self.rest(Response(b"{}", headers=headers))
            with self.subTest(headers=headers), self.assertRaisesRegex(PublicTransportError, reason):
                self.dispatch(client, make_request("serverTime", 0))

    def test_budget_counts_failed_attempt_and_is_bounded(self):
        clock = Clock()
        budget = SmokeRateBudget(started_at_ms=0, deadline_ms=180_000, max_requests=1)
        session = Session(Response(status=500))
        client = PublicRestClient(budget=budget, clock_ms=clock, allow_network=True, session_factory=lambda: session)
        with self.assertRaises(PublicTransportError):
            self.dispatch(client, make_request("serverTime", 0))
        with self.assertRaisesRegex(PublicTransportError, "smoke_request_limit"):
            self.dispatch(client, make_request("serverTime", 0))
        self.assertEqual((budget.attempts_total, len(session.calls)), (1, 1))

    def test_budget_weight_funding_and_reported_usage_stop(self):
        budget = SmokeRateBudget(started_at_ms=0, deadline_ms=180_000, weight_limit=1)
        request = make_request("serverTime", 0)
        self.assertTrue(budget.reserve(request, 0).allowed)
        self.assertEqual(budget.reserve(make_request("serverTime", 0, generation=1), 0).reason, "smoke_weight_limit")
        budget = SmokeRateBudget(started_at_ms=0, deadline_ms=180_000, funding_limit=1)
        request = make_request("fundingInfo", 0)
        self.assertTrue(budget.reserve(request, 0).allowed)
        self.assertEqual(budget.reserve(make_request("fundingInfo", 0, generation=1), 0).reason, "smoke_funding_limit")
        with self.assertRaisesRegex(PublicTransportError, "observed_smoke_weight_limit"):
            budget.observe(make_request("serverTime", 0), {"X-MBX-USED-WEIGHT-1M": "120"}, 0)

    def test_stale_request_rejected_and_close_releases_session(self):
        client, session, clock, _ = self.rest()
        request = make_request("serverTime", 0, ttl_ms=100)
        clock.now = 100
        with self.assertRaisesRegex(PublicTransportError, "request_outside_deadline"):
            self.dispatch(client, request)
        self.assertEqual(session.calls, [])
        self.dispatch(client, make_request("serverTime", 100, ttl_ms=100))
        client.close()
        self.assertTrue(session.closed)
        self.assertIsNone(client._session)

    def test_scheduler_dispatch_charged_once_and_client_requires_ticket(self):
        from radars.altcoin_hunter.rest_scheduler import RestScheduler
        client, session, clock, budget = self.rest()
        scheduler = RestScheduler(clock=clock, coordinator=budget, live=True)
        request = make_request("exchangeInfo", 0)
        with self.assertRaisesRegex(PublicTransportError, "missing_dispatch_ticket"):
            client.request(request)
        self.assertEqual((budget.attempts_total, session.calls), (0, []))
        self.assertTrue(scheduler.submit(request))
        dispatched = scheduler.poll_due()
        self.assertEqual(dispatched, (request,))
        self.assertEqual(budget.attempts_total, 1)
        result = client.request(dispatched[0])
        self.assertEqual(result.status, 200)
        self.assertEqual(budget.attempts_total, 1)
        with self.assertRaisesRegex(PublicTransportError, "missing_dispatch_ticket"):
            client.request(request)
        self.assertEqual(len(session.calls), 1)

    def test_copied_or_expired_dispatch_ticket_is_not_reusable(self):
        client, session, clock, budget = self.rest()
        request = make_request("serverTime", 0, ttl_ms=100)
        self.assertTrue(budget.reserve(request, 0).allowed)
        with self.assertRaisesRegex(PublicTransportError, "missing_dispatch_ticket"):
            client.request(replace(request))
        clock.now = 100
        with self.assertRaisesRegex(PublicTransportError, "request_outside_deadline"):
            client.request(request)
        self.assertEqual(session.calls, [])

    def test_cancel_dispatch_drops_ticket_without_refund_or_reissue(self):
        client, session, clock, budget = self.rest()
        request = make_request("serverTime", 0)
        self.assertTrue(budget.reserve(request, 0).allowed)
        self.assertTrue(budget.cancel_dispatch(request.request_id))
        self.assertFalse(budget.cancel_dispatch(request.request_id))
        self.assertEqual(budget.diagnostics(clock())["pending_dispatch_tickets"], 0)
        self.assertEqual(budget.attempts_total, 1)
        self.assertEqual(budget.reserve(request, clock()).reason, "dispatch_already_reserved")
        with self.assertRaisesRegex(PublicTransportError, "missing_dispatch_ticket"):
            client.request(request)
        self.assertEqual(session.calls, [])

    def test_budget_close_cleans_ticket_after_pre_dispatch_identity_error(self):
        from radars.altcoin_hunter.rest_scheduler import RestScheduler
        client, session, clock, budget = self.rest()
        scheduler = RestScheduler(clock=clock, coordinator=budget, live=True)
        request = make_request("openInterest", 0, instrument_id="binance:usdt_perpetual:BTCUSDT")
        self.assertTrue(scheduler.submit(request))
        self.assertEqual(scheduler.poll_due(), (request,))
        with self.assertRaisesRegex(PublicTransportError, "invalid_public_symbol"):
            client.request(request, symbol="BTCUSDT&signature=forbidden")
        self.assertEqual(budget.diagnostics(clock())["pending_dispatch_tickets"], 1)
        self.assertTrue(scheduler.cancel(request.request_id))
        budget.close()
        budget.close()
        diagnostics = budget.diagnostics(clock())
        self.assertTrue(diagnostics["closed"])
        self.assertEqual(diagnostics["pending_dispatch_tickets"], 0)
        self.assertEqual(diagnostics["attempts_total"], 1)
        self.assertEqual(budget.reserve(make_request("serverTime", 0), 0).reason, "smoke_budget_closed")
        with self.assertRaisesRegex(PublicTransportError, "smoke_budget_closed"):
            budget.consume_dispatch(request, clock())
        self.assertEqual(session.calls, [])

    def test_timeout_is_bounded_error_without_private_exception_text(self):
        client, session, _, _ = self.rest()

        def timeout(*args, **kwargs):
            raise TimeoutError("Authorization=secret Cookie=secret private URL")

        session.get = timeout
        with self.assertRaises(PublicTransportError) as caught:
            self.dispatch(client, make_request("serverTime", 0))
        self.assertEqual(caught.exception.reason, "public_request_timeout")
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("secret", json.dumps(dict(caught.exception.metadata)))
        self.assertTrue(client.closed)

    def test_late_body_result_is_never_returned_as_success(self):
        client, session, clock, _ = self.rest()
        read = session.response.raw.read1

        def late_read(count, *, decode_content):
            clock.now = 101
            return read(count, decode_content=decode_content)

        session.response.raw.read1 = late_read
        with self.assertRaisesRegex(PublicTransportError, "request_deadline_exceeded"):
            self.dispatch(client, make_request("serverTime", 0, ttl_ms=100))
        self.assertTrue(client.closed)


class PublicWebSocketTests(unittest.TestCase):
    def connected(self, frames=(), *, route=Route.MARKET, socket=None, **kwargs):
        clock, sock, ws = Clock(), socket or Socket(), WebSocket(frames)
        destinations = []

        def socket_factory(host, port, timeout):
            destinations.append((host, port, timeout))
            return sock

        client = PublicWebSocket(clock_ms=clock, deadline_ms=180_000, allowed_symbols=("BTCUSDT",), allow_network=True,
                                 socket_factory=socket_factory, ws_factory=lambda **_kwargs: ws, **kwargs)
        client.open(route)
        return client, sock, ws, clock, destinations

    def test_default_off_constructor_does_not_open(self):
        calls = []
        client = PublicWebSocket(clock_ms=Clock(), deadline_ms=180_000, allowed_symbols=("BTCUSDT",),
                                 socket_factory=lambda *args: calls.append(args))
        self.assertEqual(calls, [])
        with self.assertRaisesRegex(PublicTransportError, "network_disabled"):
            client.open(Route.MARKET)
        self.assertEqual(calls, [])

    def test_fixed_verified_handshake_no_cookie_auth_proxy_or_extensions(self):
        client, sock, _, _, destinations = self.connected()
        self.assertEqual(destinations, [(WS_HOST, 443, 5.0)])
        request = sock.outgoing[0].decode("ascii")
        self.assertTrue(request.startswith("GET /market/stream HTTP/1.1\r\n"))
        self.assertIn("Host: fstream.binance.com\r\n", request)
        for field in ("Cookie:", "Authorization:", "Sec-WebSocket-Extensions:", "Origin:"):
            self.assertNotIn(field, request)
        client.close()
        self.assertTrue(sock.closed)

    def test_public_route_and_invalid_route(self):
        _, sock, _, _, _ = self.connected(route=Route.PUBLIC)
        self.assertTrue(sock.outgoing[0].startswith(b"GET /public/stream "))
        client = PublicWebSocket(clock_ms=Clock(), deadline_ms=180_000, allowed_symbols=("BTCUSDT",), allow_network=True)
        for route in ("MARKET", "/private/stream", None, True):
            with self.subTest(route=route), self.assertRaisesRegex(PublicTransportError, "invalid_websocket_route"):
                client.open(route)

    def test_handshake_redirect_rate_limit_and_invalid_accept_never_open(self):
        for sock, reason in ((Socket(status=302), "unexpected_websocket_status"),
                             (Socket(status=429), "rate_limited"),
                             (Socket(status=418), "rate_limited"),
                             (Socket(invalid_accept=True), "invalid_websocket_handshake")):
            with self.subTest(reason=reason), self.assertRaisesRegex(PublicTransportError, reason):
                self.connected(socket=sock)
            self.assertTrue(sock.closed)
            self.assertEqual(len(sock.outgoing), 1)

    def test_handshake_headers_bounded_and_extensions_not_negotiated(self):
        for sock, reason in ((Socket(extra_headers="X-Extra: " + "x" * 17000 + "\r\n"), "websocket_handshake_too_large"),
                             (Socket(extra_headers="Sec-WebSocket-Extensions: permessage-deflate\r\n"), "invalid_websocket_handshake")):
            with self.subTest(reason=reason), self.assertRaisesRegex(PublicTransportError, reason):
                self.connected(socket=sock)
            self.assertTrue(sock.closed)

    def test_handshake_uses_remaining_connect_deadline_before_first_send(self):
        sock, clock = Socket(), Clock()

        def expired_socket(*args):
            clock.now = 5000
            return sock

        client = PublicWebSocket(clock_ms=clock, deadline_ms=180_000, allowed_symbols=("BTCUSDT",),
                                 allow_network=True, socket_factory=expired_socket)
        with self.assertRaisesRegex(PublicTransportError, "websocket_handshake_deadline"):
            client.open(Route.MARKET)
        self.assertEqual(sock.outgoing, [])
        self.assertTrue(sock.closed)

    def test_reconnect_calls_are_explicit_and_finitely_bounded(self):
        client, sock, _, _, destinations = self.connected(max_connections=1)
        client.close()
        with self.assertRaisesRegex(PublicTransportError, "smoke_connection_limit"):
            client.open(Route.MARKET)
        self.assertEqual(len(destinations), 1)
        self.assertTrue(sock.closed)

    def test_oversized_advertised_frame_rejected_before_payload_read(self):
        ws = WebSocket()
        ws.frame_buffer.advertised_length = 1_000_001
        _bound_frame_buffer(ws, 1_000_000)
        with self.assertRaisesRegex(PublicTransportError, "websocket_frame_too_large"):
            ws.frame_buffer.recv_length()
        self.assertEqual(ws.frame_buffer.payload_reads, 0)

    def test_real_websocket_frame_buffer_rejects_length_before_payload_read(self):
        # Exercise the installed framing dependency with a byte supplier, never
        # its connect/recv network path. The supplier has no payload bytes.
        from websocket._abnf import frame_buffer
        wire = io.BytesIO(b"\x81\x7f" + (1_000_001).to_bytes(8, "big"))
        sizes = []

        def source(count):
            sizes.append(count)
            data = wire.read(count)
            if not data:
                raise AssertionError("must_reject_before_payload_read")
            return data

        ws = SimpleNamespace(frame_buffer=frame_buffer(source, False))
        _bound_frame_buffer(ws, 1_000_000)
        with self.assertRaisesRegex(PublicTransportError, "websocket_frame_too_large"):
            ws.frame_buffer.recv_frame()
        self.assertEqual(sizes, [2, 8])

    def test_ping_and_pong_are_returned_without_automatic_send(self):
        client, _, ws, _, _ = self.connected((frame(9, b"nonce"), frame(10, b"nonce")))
        ping, pong = client.recv(), client.recv()
        self.assertEqual((ping.kind, ping.payload), ("ping", b"nonce"))
        self.assertEqual(pong.kind, "pong")
        self.assertEqual(ws.sent, [])
        client.send_pong(ping.payload)
        self.assertEqual(ws.sent, [(10, b"nonce")])

    def test_text_fragmentation_is_bounded_and_utf8_strict(self):
        client, _, _, _, _ = self.connected((frame(1, b'{"v":', 0), frame(0, b"1}")))
        self.assertEqual(client.recv().kind, "fragment")
        self.assertEqual(client.recv().payload, '{"v":1}')
        client, _, _, _, _ = self.connected((frame(1, b"123", 0), frame(0, b"456")), max_frame_bytes=5)
        client.recv()
        with self.assertRaisesRegex(PublicTransportError, "websocket_message_too_large"):
            client.recv()
        client, _, _, _, _ = self.connected((frame(1, b"\xff"),))
        with self.assertRaisesRegex(PublicTransportError, "invalid_websocket_utf8"):
            client.recv()

    def test_binary_and_malformed_control_rejected(self):
        for value in (frame(2, b"binary"), frame(9, b"ping", 0), frame(10, b"x" * 126)):
            client, _, _, _, _ = self.connected((value,))
            with self.subTest(opcode=value.opcode), self.assertRaises(PublicTransportError):
                client.recv()

    def test_subscription_controls_only_and_strict_ack_type(self):
        client, _, ws, _, _ = self.connected()
        for ack in (1, "ack-1"):
            client.send_control({"method": "SUBSCRIBE", "params": ["btcusdt@aggTrade"], "id": ack})
        self.assertEqual([json.loads(payload)["id"] for _, payload in ws.sent], [1, "ack-1"])
        for ack in (True, None, "", "a" * 65, "a\nb"):
            with self.subTest(ack=ack), self.assertRaisesRegex(PublicTransportError, "invalid_ack_id"):
                client.send_control({"method": "SUBSCRIBE", "params": ["btcusdt@aggTrade"], "id": ack})
        with self.assertRaisesRegex(PublicTransportError, "unsupported_subscription_method"):
            client.send_control({"method": "ORDER", "params": [], "id": 1})

    def test_wrong_route_private_stream_and_overlarge_batch_rejected(self):
        client, _, ws, _, _ = self.connected()
        for params, reason in ((["btcusdt@bookTicker"], "wrong_route"),
                               (["listen-key"], "unsupported_public_stream"),
                               (["btcusdt@depth"], "unsupported_public_stream"),
                               (["btcusdt@aggTrade"] * 51, "invalid_subscription_batch")):
            with self.subTest(reason=reason), self.assertRaisesRegex(PublicTransportError, reason):
                client.send_control({"method": "SUBSCRIBE", "params": params, "id": 1})
        self.assertEqual(ws.sent, [])

    def test_global_and_nonselected_symbols_cannot_expand_smoke_scope(self):
        client, _, ws, _, _ = self.connected()
        for stream, reason in (("!markPrice@arr", "unsupported_public_stream"),
                               ("!forceOrder@arr", "unsupported_public_stream"),
                               ("ethusdt@aggTrade", "symbol_outside_smoke_scope")):
            with self.subTest(stream=stream), self.assertRaisesRegex(PublicTransportError, reason):
                client.send_control({"method": "SUBSCRIBE", "params": [stream], "id": 1})
        self.assertEqual(ws.sent, [])

    def test_controls_including_pong_have_independent_rolling_safety_bound(self):
        client, sock, ws, _, _ = self.connected()
        for _ in range(8):
            client.send_pong(b"nonce")
        with self.assertRaisesRegex(PublicTransportError, "websocket_control_rate_limit"):
            client.send_pong(b"nonce")
        self.assertEqual(len(ws.sent), 8)
        self.assertTrue(sock.closed)

    def test_protocol_failure_closes_socket_before_a_second_read_can_bypass_length_guard(self):
        client, sock, ws, _, _ = self.connected((frame(1, b"123456"),), max_frame_bytes=5)
        with self.assertRaisesRegex(PublicTransportError, "websocket_frame_too_large"):
            client.recv()
        self.assertEqual(ws.frame_buffer.payload_reads, 0)
        self.assertTrue(sock.closed)
        with self.assertRaisesRegex(PublicTransportError, "websocket_not_open"):
            client.recv()

    def test_timeout_is_idle_poll_and_smoke_deadline_is_hard(self):
        client, _, _, clock, _ = self.connected((TimeoutError("private-diagnostic"),))
        self.assertEqual(client.recv().kind, "timeout")
        clock.now = 180_000
        with self.assertRaisesRegex(PublicTransportError, "smoke_deadline"):
            client.recv()

    def test_close_has_no_implicit_pong_task_or_closing_read_loop(self):
        client, sock, ws, _, _ = self.connected((frame(9, b"pending"),))
        client.close()
        client.close()
        self.assertTrue(sock.closed)
        self.assertEqual(ws.shutdown_count, 1)
        self.assertEqual(ws.sent, [])
        self.assertEqual(len(ws.frames), 1)
        self.assertIsNone(client._fragment)
