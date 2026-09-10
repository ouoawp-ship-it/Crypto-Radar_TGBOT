"""End-to-end driver tests on a virtual clock and explicit fake public I/O."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from radars.altcoin_hunter.adapters.base import Route
from radars.altcoin_hunter.adapters.fixtures import FIXTURE_TIME_MS, fixture_exchange_info
from radars.altcoin_hunter.connection import ConnectionState
from runtime.altcoin_hunter_smoke import (
    PublicNetworkGuard, SmokeSession, SmokeStop, local_temporary_directory, run_public_smoke,
)
from runtime.altcoin_hunter_transport import HttpResult, PublicFrame, PublicTransportError
from radars.altcoin_hunter.smoke_policy import FIRST_SMOKE_SYMBOLS, SmokeConfig, seal_smoke_report, validate_first_success_proof


class VirtualClock:
    def __init__(self):
        self.elapsed_ms = 0

    def now_ms(self):
        return FIXTURE_TIME_MS + self.elapsed_ms

    def monotonic_ns(self):
        return 1_000_000_000 + self.elapsed_ms * 1_000_000

    def pause(self, seconds):
        self.elapsed_ms += max(1, int(seconds * 1000))


def directory():
    template = fixture_exchange_info()["symbols"][0]
    rows = []
    for symbol in FIRST_SMOKE_SYMBOLS:
        row = deepcopy(template)
        row.update(symbol=symbol, pair=symbol, baseAsset="SYNTHETIC")
        rows.append(row)
    return {"symbols": rows}


class FakeRest:
    def __init__(self, *, budget, clock_ms, allow_network, **kwargs):
        self.budget, self.clock_ms = budget, clock_ms
        self.request_attempts = 0
        self.closed = False
        self.metadata_error = False
        self.failure = None
        self.fail_endpoint = None

    def request(self, request, *, symbol=None):
        self.budget.consume_dispatch(request, self.clock_ms())
        self.request_attempts += 1
        if self.failure or request.endpoint == self.fail_endpoint:
            raise PublicTransportError(self.failure or "rate_limited", metadata={"http_status": 429})
        if request.endpoint.endswith("/time"):
            body = {"serverTime": self.clock_ms()}
        elif request.endpoint.endswith("exchangeInfo"):
            body = directory()
            if self.metadata_error:
                body["symbols"][0]["filters"] = None
        elif request.endpoint.endswith("fundingInfo"):
            body = []
        else:
            body = {"symbol": symbol, "openInterest": "100.0", "time": self.clock_ms()}
        raw = json.dumps(body).encode()
        return HttpResult(raw, 200, {"http_status": 200, "endpoint": request.endpoint,
                                     "content_length": len(raw), "body_bytes": len(raw)})

    def close(self):
        self.closed = True


class FakeSocket:
    """Generate synthetic messages after the actual driver sends a subscription."""
    def __init__(self, *, clock_ms, allowed_symbols, **kwargs):
        self.clock = clock_ms.__self__
        self.symbols = allowed_symbols
        self.route = None
        self.connection_attempts = self.controls_sent = 0
        self.pending = []
        self.streams = []
        self.index = 0
        self.sequences = {}
        self.closed = False
        self.override = None

    def open(self, route):
        self.route = route
        self.connection_attempts += 1

    def send_control(self, message):
        self.controls_sent += 1
        self.streams = list(message["params"])
        self.pending.append(PublicFrame("data", json.dumps({"id": message["id"], "result": None})))

    def send_pong(self, payload):
        self.controls_sent += 1
        self.last_pong = payload

    def recv(self):
        self.clock.pause(0.01)
        if self.override is not None:
            return self.override
        if self.pending:
            return self.pending.pop(0)
        stream = self.streams[self.index % len(self.streams)]
        self.index += 1
        symbol, kind = stream.split("@", 1)
        at = self.clock.now_ms()
        symbol = symbol.upper()
        key = (symbol, kind)
        sequence = self.sequences.get(key, 0) + 1
        self.sequences[key] = sequence
        if kind == "aggTrade":
            data = {"e": "aggTrade", "E": at, "T": at, "s": symbol, "st": 1,
                    "a": sequence, "f": sequence, "l": sequence, "p": "10", "q": "1", "m": False}
        elif kind == "markPrice":
            data = {"e": "markPriceUpdate", "E": at, "T": at + 3600_000,
                    "s": symbol, "st": 1, "p": "10", "i": "10", "r": "0.0001"}
        else:
            data = {"e": "bookTicker", "E": at, "T": at, "s": symbol, "st": 1,
                    "u": sequence, "b": "10", "a": "11", "B": "2", "A": "3"}
        return PublicFrame("data", json.dumps({"stream": stream, "data": data}))

    def close(self):
        self.closed = True


class PublicSmokeSessionTests(unittest.TestCase):
    def session(self, **kwargs):
        return SmokeSession(SmokeConfig(enable=True), clock=VirtualClock(),
                            rest_factory=FakeRest, ws_factory=FakeSocket, **kwargs)

    def test_virtual_five_symbol_capture_uses_exact_channels_and_receipts(self):
        session = self.session()
        with patch.object(socket, "getaddrinfo", side_effect=AssertionError("network_forbidden")), \
                patch.object(socket.socket, "connect", side_effect=AssertionError("network_forbidden")):
            session.capture()
            self.assertTrue(session.close())
        report = seal_smoke_report(session.report(status="passed", violations=[], peak=None))
        self.assertEqual(validate_first_success_proof(report), report["deterministic_digest"])
        self.assertEqual(report["http_attempts"], 8)  # Time, directory, FundingInfo, five correlated OI.
        self.assertEqual(report["budget"]["attempts_total"], 8)
        self.assertEqual(report["scheduler"]["completed"], 8)
        self.assertEqual(report["oi"]["coverage"], 1)
        self.assertEqual(len(report["trade_coverage_intervals"]), 5)
        self.assertEqual(report["evidence"]["parser_rejected_count"], 0)
        self.assertEqual(report["evidence"]["admission_rejected_count"], 0)
        self.assertTrue(all(item["cleanup_complete"] for item in report["connections"]))
        self.assertTrue(session.rest.closed)
        self.assertTrue(all(sock.closed for _, sock in session.connections))
        for symbol in FIRST_SMOKE_SYMBOLS:
            for kind in ("trade", "mark_price", "funding", "book_ticker", "open_interest"):
                self.assertGreater(report["evidence"]["event_counts"][symbol + ":" + kind], 0)
        self.assertFalse(report["database_created"])

    def test_preflight_preserves_partial_directory_failure_and_never_opens_ws(self):
        session = self.session()
        session.rest.metadata_error = True
        with self.assertRaisesRegex(SmokeStop, "exchange_info_not_complete"):
            session.preflight_directory()
        self.assertEqual(session.connections, [])
        self.assertGreater(session.preflight[-1]["parse_rejected_count"], 0)
        self.assertEqual(session.rest.request_attempts, 2)
        session.close()

    def test_rate_limit_is_terminal_without_retry(self):
        session = self.session()
        session.rest.failure = "rate_limited"
        with self.assertRaisesRegex(SmokeStop, "rate_limited"):
            session.capture()
        session.close()
        self.assertEqual(session.rest.request_attempts, 1)
        self.assertEqual(session.scheduler.diagnostics()["retries"], 0)
        self.assertEqual(session.transport_failure["http_status"], 429)

    def start_one(self):
        session = self.session()
        session.preflight_directory()
        supervisor, sock = session.connections[0]
        supervisor.start(session.clock.now_ms())
        session.pump(supervisor, sock)
        session.receive(supervisor, sock)
        self.assertEqual(supervisor.state, ConnectionState.ACTIVE)
        return session, supervisor, sock

    def test_real_observation_bridge_opens_only_after_ack(self):
        session = self.session()
        session.preflight_directory()
        supervisor, sock = session.connections[0]
        supervisor.start(session.clock.now_ms())
        session.pump(supervisor, sock)
        self.assertEqual(supervisor.state, ConnectionState.SUBSCRIBING)
        self.assertIsNone(supervisor.snapshot()["coverage_open_since_ms"])
        session.receive(supervisor, sock)
        self.assertEqual(supervisor.state, ConnectionState.ACTIVE)
        self.assertIsNotNone(supervisor.snapshot()["coverage_open_since_ms"])
        session.close()

    def test_cm_and_bad_fields_fail_before_evidence_can_be_complete(self):
        for change in ({"st": 2}, {"m": "false"}, {"q": "NaN"}):
            session, supervisor, sock = self.start_one()
            message = json.loads(sock.recv().payload)
            # Select the aggTrade stream explicitly, independent of sort order.
            name = next(stream for stream in sock.streams if stream.endswith("@aggTrade"))
            at = session.clock.now_ms()
            message = {"stream": name, "data": {"e": "aggTrade", "st": 1, "E": at,
                "T": at, "s": name.split("@")[0].upper(), "a": 1, "f": 1, "l": 1,
                "p": "10", "q": "1", "m": False}}
            message["data"].update(change)
            sock.override = PublicFrame("data", json.dumps(message))
            with self.subTest(change=change), self.assertRaisesRegex(SmokeStop, "event_parse_or_admission_rejected"):
                session.receive(supervisor, sock)
            self.assertEqual(session.evidence.parser_rejected, 1)
            session.close()

    def test_remote_close_stops_with_no_automatic_reconnect(self):
        session, supervisor, sock = self.start_one()
        sock.override = PublicFrame("close", b"")
        with self.assertRaisesRegex(SmokeStop, "unexpected_remote_close"):
            session.receive(supervisor, sock)
        session.close()
        self.assertEqual(sock.connection_attempts, 1)
        self.assertEqual(supervisor.state, ConnectionState.STOPPED)

    def test_ping_pong_is_sent_through_supervisor_control_ledger(self):
        session, supervisor, sock = self.start_one()
        sock.override = PublicFrame("ping", b"123")
        session.receive(supervisor, sock)
        self.assertEqual(sock.last_pong, b"123")
        self.assertEqual(supervisor.counts["pong_sent"], 1)
        session.close()

    def test_opaque_binary_ping_is_returned_byte_for_byte(self):
        session, supervisor, sock = self.start_one()
        sock.override = PublicFrame("ping", b"\xff\x00\x80")
        session.receive(supervisor, sock)
        self.assertEqual(sock.last_pong, b"\xff\x00\x80")
        self.assertEqual(session.opaque_pings, {})
        session.close()

    def test_duplicate_json_keys_are_rejected_before_good_coverage_advances(self):
        session, supervisor, sock = self.start_one()
        previous = supervisor.snapshot()["coverage_open_since_ms"]
        sock.override = PublicFrame("data", '{"stream":"btcusdt@aggTrade","stream":"ethusdt@aggTrade","data":{}}')
        with self.assertRaisesRegex(SmokeStop, "malformed_public_frame"):
            session.receive(supervisor, sock)
        self.assertEqual(supervisor.counts["parser_drop"], 1)
        session.close()
        report = session.report(status="failed", violations=["malformed_public_frame"], peak=None)
        self.assertTrue(all(not row["complete"] for row in report["trade_coverage_intervals"]))
        self.assertTrue(all(row["end_ms"] <= previous for row in report["trade_coverage_intervals"]))

    def test_mid_initial_oi_failure_cancels_queued_work_and_budget_tickets(self):
        session = self.session()
        session.rest.fail_endpoint = "/fapi/v1/openInterest"
        with self.assertRaisesRegex(SmokeStop, "rate_limited"):
            session.preflight_directory()
        self.assertGreater(session.scheduler.diagnostics()["queue_depth"], 0)
        self.assertTrue(session.close())
        self.assertEqual(session.scheduler.diagnostics()["queue_depth"], 0)
        self.assertEqual(session.scheduler.diagnostics()["inflight"], 0)
        self.assertEqual(session.budget.diagnostics(session.clock.now_ms())["pending_dispatch_tickets"], 0)

    def test_received_timestamp_does_not_move_scheduler_back_after_diagnostics(self):
        session = self.session()
        original = session.scheduler.diagnostics
        def advancing_diagnostics(now_ms=None):
            if now_ms is None:
                session.clock.pause(0.001)
            return original(now_ms)
        session.scheduler.diagnostics = advancing_diagnostics
        session.preflight_directory()
        self.assertEqual(session.evidence.events["BTCUSDT:open_interest"], 1)
        self.assertTrue(session.close())

    def test_wrong_route_or_unknown_stream_stops(self):
        session, supervisor, sock = self.start_one()
        sock.override = PublicFrame("data", json.dumps({"stream": "btcusdt@bookTicker", "data": {}}))
        with self.assertRaisesRegex(SmokeStop, "connection_frame_not_admitted"):
            session.receive(supervisor, sock)
        session.close()

    def test_request_symbol_cannot_substitute_another_identity(self):
        from radars.altcoin_hunter.rest_budget import make_request
        session = self.session()
        session.preflight_directory()
        request = make_request("openInterest", session.clock.now_ms(),
                               instrument_id=session.registry["BTCUSDT"].identity.instrument_id)
        with self.assertRaisesRegex(SmokeStop, "request_symbol_identity_mismatch"):
            session._http(request, symbol="ETHUSDT")
        session.close()

    def test_parent_startup_counts_toward_budget(self):
        clock = VirtualClock()
        deadline = clock.monotonic_ns() + 180_000_000_000
        clock.pause(0.5)
        session = SmokeSession(SmokeConfig(enable=True), clock=clock, rest_factory=FakeRest,
                               ws_factory=FakeSocket, deadline_ns=deadline)
        self.assertEqual(session.start_ns, 1_000_000_000)
        self.assertEqual(session.deadline_ns, deadline)
        session.close()

    def test_no_enabled_config_no_network_clients_or_output(self):
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "not-created"
            with self.assertRaisesRegex(ValueError, "public_smoke_disabled"):
                run_public_smoke(SmokeConfig(), output_dir=target)
            self.assertFalse(target.exists())

    def test_fresh_temp_directory_only(self):
        with tempfile.TemporaryDirectory(prefix="altcoin-hunter-smoke-") as temp:
            self.assertEqual(local_temporary_directory(Path(temp)), Path(temp).absolute())
            (Path(temp) / "existing.txt").write_text("owned test fixture", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "fresh_temporary_output_required"):
                local_temporary_directory(Path(temp))
        with self.assertRaises(ValueError):
            local_temporary_directory(Path(__file__).parent)

    def test_network_guard_blocks_other_host_without_resolving(self):
        original = socket.getaddrinfo
        with PublicNetworkGuard() as guard:
            with self.assertRaisesRegex(SmokeStop, "non_public_dns_blocked"):
                socket.getaddrinfo("not-public.invalid", 443)
            self.assertEqual(guard.dns_attempts, 0)
            self.assertEqual(guard.blocked_attempts, 1)
        self.assertIs(socket.getaddrinfo, original)

    def test_network_guard_only_admits_resolved_public_443(self):
        calls = []
        address = ("192.0.2.1", 443)
        def resolve(host, port, *args, **kwargs):
            calls.append((host, port))
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", address)]
        with patch.object(socket, "getaddrinfo", side_effect=resolve), \
                patch.object(socket.socket, "connect", autospec=True) as connect:
            with PublicNetworkGuard() as guard:
                socket.getaddrinfo("fapi.binance.com", 443)
                # Calling the unbound method with a sentinel avoids creating a real socket.
                socket.socket.connect(object(), address)
                with self.assertRaisesRegex(SmokeStop, "non_public_connection_blocked"):
                    socket.socket.connect(object(), ("192.0.2.2", 443))
                self.assertEqual((guard.dns_attempts, guard.tcp_attempts, guard.blocked_attempts), (1, 1, 1))
            self.assertEqual(connect.call_count, 1)
        self.assertEqual(calls, [("fapi.binance.com", 443)])
