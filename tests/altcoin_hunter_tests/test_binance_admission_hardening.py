"""Explicit WS, scheduler-correlated REST and offline replay admission."""
from dataclasses import replace
import socket
import sqlite3
import unittest
from unittest.mock import patch

from radars.altcoin_hunter.adapters.base import ParseResult, RejectedItem, Route
from radars.altcoin_hunter.ingestion import (
    AdmissionContext, IngressChannel, OfflineIngestion, ReplayAdmissionContext, RestAdmissionContext,
)
from radars.altcoin_hunter.models import OpenInterestEvent, OpenInterestPayload
from radars.altcoin_hunter.rest_budget import FakeCoordinator, make_request
from radars.altcoin_hunter.rest_scheduler import Completion, RestScheduler
from tests.altcoin_hunter_tests.test_binance_ingestion import TIME, trade


def oi(**changes):
    return replace(OpenInterestEvent(exchange="binance", market="usdt_perpetual", instrument_id="AAAUSDT",
        symbol="AAAUSDT", exchange_symbol="AAAUSDT", event_time_ms=TIME, receive_time_ms=TIME,
        receive_monotonic_ns=100, source="binance_usdm_open_interest", source_event_id=f"AAAUSDT:{TIME}",
        payload=OpenInterestPayload("100")), **changes)


class AdmissionHardeningTests(unittest.TestCase):
    def setUp(self):
        self.clock = [TIME - 100]
        self.scheduler = RestScheduler(clock=lambda: self.clock[0], coordinator=FakeCoordinator())
        self.request = make_request("openInterest", self.clock[0], instrument_id="AAAUSDT", generation=7)
        self.assertTrue(self.scheduler.submit(self.request))
        self.assertEqual(self.scheduler.poll_due(), (self.request,))
        self.clock[0] = TIME
        self.completion = self.scheduler.complete(self.request, status_code=200, response_time_ms=TIME)
        self.assertTrue(self.completion.accepted)
        self.rest = RestAdmissionContext(self.scheduler, self.completion, self.request.request_id, self.request.generation)
        self.ws = AdmissionContext.for_channel(IngressChannel.WS_MARKET, connection_epoch=0,
                                               active=True, subscription_acked=True, liveness_valid=True)

    def admit(self, event, context=None, now=TIME):
        return OfflineIngestion().admit(ParseResult((event,)), context=context or self.rest, now_ms=now)

    def test_oi_rest_needs_no_ws_epoch_or_subscription(self):
        event = oi(connection_epoch=12345)
        result = self.admit(event)
        self.assertEqual(result.events, (event,))
        self.assertEqual(result.total_rejected_count, 0)
        self.assertEqual(result.event_metadata[0]["ingress_channel"], "REST_PUBLIC")
        self.assertEqual(result.event_metadata[0]["request_generation"], 7)
        self.assertEqual(result.event_metadata[0]["request_id"], self.request.request_id)
        self.assertFalse(hasattr(self.rest, "route"))

    def test_oi_ws_and_trade_rest_are_rejected(self):
        for event, context in ((oi(), self.ws), (trade(), self.rest)):
            with self.subTest(channel=context.channel):
                result = self.admit(event, context)
                self.assertEqual(result.events, ())
                self.assertEqual(result.admission_rejected_count, 1)
                self.assertEqual(result.diagnostics["counters"]["wrong_ingress_channel"], 1)

    def test_legacy_ws_context_still_enforces_every_gate(self):
        for field, value in (("active", False), ("subscription_acked", False), ("liveness_valid", False),
                             ("local_data_loss", True), ("route", Route.PUBLIC), ("connection_epoch", 9)):
            with self.subTest(field=field):
                self.assertEqual(self.admit(trade(), replace(self.ws, **{field: value})).events, ())
        self.assertEqual(self.admit(trade(), self.ws).events, (trade(),))

    def test_request_id_generation_identity_and_receipt_must_all_match(self):
        for fields in ({"request_id": "wrong"}, {"generation": 8},
                       {"completion": replace(self.completion)},
                       {"completion": Completion(self.request, True, False, "accepted", TIME, 1)}):
            with self.subTest(fields=fields):
                self.assertEqual(self.admit(oi(), replace(self.rest, **fields)).events, ())
        self.assertEqual(self.admit(oi(instrument_id="BBBUSDT")).events, ())
        other = RestScheduler(clock=lambda: TIME, coordinator=FakeCoordinator())
        self.assertEqual(self.admit(oi(), replace(self.rest, scheduler=other)).events, ())

    def test_receipt_invalid_after_new_generation_or_retirement(self):
        next_request = make_request("openInterest", TIME, instrument_id="AAAUSDT", generation=8)
        self.assertTrue(self.scheduler.submit(next_request))
        self.assertEqual(self.admit(oi()).events, ())
        self.scheduler.retire_identity(self.request.key)
        self.assertEqual(self.admit(oi()).events, ())

    def test_deadline_and_future_event_fail_closed_without_ws_skew_allowance(self):
        self.assertEqual(self.admit(oi(event_time_ms=TIME + 1)).events, ())
        self.assertEqual(self.admit(oi(), now=self.request.deadline_ms).events, ())

    def test_unsuccessful_scheduler_completion_is_not_admission(self):
        request = make_request("openInterest", TIME, instrument_id="AAAUSDT", generation=8)
        self.assertTrue(self.scheduler.submit(request))
        self.scheduler.poll_due(TIME)
        failed = self.scheduler.complete(request, status_code=400, response_time_ms=TIME)
        self.assertFalse(failed.accepted)
        context = RestAdmissionContext(self.scheduler, failed, request.request_id, request.generation)
        self.assertEqual(self.admit(oi(), context).events, ())

    def test_replay_requires_explicit_offline_source_and_records_it(self):
        context = ReplayAdmissionContext("offline_replay")
        result = self.admit(oi(connection_epoch=99), context)
        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.event_metadata[0]["replay_source"], "offline_replay")
        self.assertEqual(result.event_metadata[0]["ingress_channel"], "REPLAY")
        for bad in (None, True, "", "REST_PUBLIC", "binance", "offline_replay\n"):
            with self.subTest(source=bad), self.assertRaises(ValueError):
                ReplayAdmissionContext(bad)
        with self.assertRaises(ValueError):
            AdmissionContext.for_channel(IngressChannel.REST_PUBLIC)

    def test_rejection_counts_include_suppressed_parser_details_and_duplicates(self):
        parsed = ParseResult((trade(), trade(), trade(event_time_ms=TIME + 3000)),
                             (RejectedItem(1, "missing_required_field"),), {"rejected_count": 100})
        result = OfflineIngestion().admit(parsed, context=self.ws, now_ms=TIME)
        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.parser_rejected_count, 100)
        self.assertEqual(result.admission_rejected_count, 1)
        self.assertEqual(result.duplicate_count, 1)
        self.assertEqual(result.total_rejected_count, 102)
        self.assertEqual(result.rejected_count, 102)
        self.assertEqual(result.diagnostics["counters"]["parser_reject_details_suppressed"], 99)
        parser_only = OfflineIngestion().admit(ParseResult(diagnostics={"rejected_count": 100}),
                                                context=self.ws, now_ms=TIME)
        self.assertEqual(parser_only.total_rejected_count, 100)
        self.assertEqual(parser_only.admission_rejected_count, 0)

    def test_runtime_admission_has_zero_network_dns_and_database_calls(self):
        with patch.object(socket, "socket", side_effect=AssertionError("network")) as sockets, \
             patch.object(socket, "getaddrinfo", side_effect=AssertionError("dns")) as dns, \
             patch.object(sqlite3, "connect", side_effect=AssertionError("database")) as database:
            self.assertEqual(len(self.admit(oi()).events), 1)
            self.assertEqual(len(self.admit(trade(), self.ws).events), 1)
            self.assertEqual(len(self.admit(oi(), ReplayAdmissionContext("offline_fixture")).events), 1)
            sockets.assert_not_called()
            dns.assert_not_called()
            database.assert_not_called()
