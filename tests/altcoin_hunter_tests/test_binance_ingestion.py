"""Admission and bounded diagnostics independent of protocol fixture details."""
from dataclasses import replace
import unittest

from radars.altcoin_hunter.adapters.base import (
    BoundedDiagnostics, FakeTransport, ParseLimits, ParseResult, Route, identifier,
)
from radars.altcoin_hunter.ingestion import AdmissionContext, AllMarketObservation, OfflineIngestion
from radars.altcoin_hunter.models import BookTickerEvent, BookTickerPayload, TradeEvent, TradePayload

TIME = 1788518400000


def trade(**changes):
    return replace(TradeEvent(exchange="binance", market="usdt_perpetual", instrument_id="AAAUSDT",
        symbol="AAAUSDT", exchange_symbol="AAAUSDT", event_time_ms=TIME, receive_time_ms=TIME + 1,
        receive_monotonic_ns=100, source="binance_usdm_agg_trade", source_event_id="1",
        payload=TradePayload("10", "1", False)), **changes)


def bbo(update=1):
    return BookTickerEvent(exchange="binance", market="usdt_perpetual", instrument_id="AAAUSDT",
        symbol="AAAUSDT", exchange_symbol="AAAUSDT", event_time_ms=TIME, receive_time_ms=TIME + 1,
        receive_monotonic_ns=100, source="binance_usdm_book_ticker", source_event_id=f"AAAUSDT:{update}",
        sequence_start=update, sequence_end=update, payload=BookTickerPayload("10", "11", "1", "2"))


class IngestionTests(unittest.TestCase):
    def setUp(self):
        self.context = AdmissionContext(Route.MARKET, 0, True, True, True)

    def test_connection_requirements_are_independent(self):
        gate = OfflineIngestion()
        for field, value in (("active", False), ("subscription_acked", False),
                             ("liveness_valid", False), ("local_data_loss", True),
                             ("route", Route.PUBLIC), ("connection_epoch", 1)):
            with self.subTest(field=field):
                result = gate.admit(ParseResult((trade(),)), context=replace(self.context, **{field: value}), now_ms=TIME)
                self.assertEqual((result.events, result.rejected_count), ((), 1))
        self.assertEqual(gate.retained_dedup_keys, 0)

    def test_duplicate_and_eviction_are_explicit(self):
        gate = OfflineIngestion(max_dedup_keys=2)
        for event_id in ("1", "1", "2", "3"):
            result = gate.admit(ParseResult((trade(source_event_id=event_id),)), context=self.context, now_ms=TIME)
        self.assertEqual(gate.retained_dedup_keys, 2)
        self.assertEqual(gate.dedup_evictions, 1)
        self.assertEqual(result.diagnostics["counters"]["duplicate_event"], 1)
        self.assertEqual(result.diagnostics["counters"]["dedup_horizon_evicted"], 1)

    def test_promoted_bbo_upgrades_provenance_without_double_counting(self):
        gate = OfflineIngestion()
        context = replace(self.context, route=Route.PUBLIC)
        first = gate.admit(ParseResult((bbo(),)), context=context, now_ms=TIME)
        second = gate.admit(ParseResult((bbo(),)), context=context, now_ms=TIME, promoted=True)
        self.assertEqual(len(first.events), 1)
        self.assertEqual((second.events, second.duplicate_count, second.priority_upgrades), ((), 1, 1))
        self.assertEqual(gate.admit(ParseResult((bbo(),)), context=context, now_ms=TIME).priority_upgrades, 0)

    def test_older_global_bbo_cannot_replace_newer_promoted(self):
        gate = OfflineIngestion()
        context = replace(self.context, route=Route.PUBLIC)
        gate.admit(ParseResult((bbo(10),)), context=context, now_ms=TIME, promoted=True)
        result = gate.admit(ParseResult((bbo(9),)), context=context, now_ms=TIME)
        self.assertEqual(result.rejected_count, 1)
        self.assertEqual(result.diagnostics["counters"]["stale_bbo_update"], 1)

    def test_promoted_protocol_metadata_drives_priority(self):
        gate = OfflineIngestion()
        context = replace(self.context, route=Route.PUBLIC)
        gate.admit(ParseResult((bbo(),)), context=context, now_ms=TIME)
        promoted = ParseResult((bbo(),), event_metadata=({"promoted": True},))
        result = gate.admit(promoted, context=context, now_ms=TIME)
        self.assertEqual(result.priority_upgrades, 1)
        self.assertEqual(result.events, ())
        self.assertTrue(result.priority_updates[0]["metadata"]["promoted"])
        self.assertEqual(result.priority_updates[0]["dedup_key"], bbo().dedup_key)

    def test_adapter_metadata_remains_aligned_after_filtering(self):
        gate = OfflineIngestion()
        result = gate.admit(ParseResult((trade(event_time_ms=TIME + 90000), trade()),
                              event_metadata=({"marker": "rejected"}, {"rpi_included": True})),
                            context=self.context, now_ms=TIME)
        self.assertEqual(len(result.events), 1)
        self.assertEqual(dict(result.event_metadata[0]), {"rpi_included": True})

    def test_boolean_context_and_identity_are_strict(self):
        for bad in (None, 1, 1.0, True, "", " ", "x\n", "x\u200b"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                identifier(bad)
        for bad in (1, "true", None):
            with self.assertRaises(ValueError):
                replace(self.context, active=bad)

    def test_missing_array_symbol_is_observation_not_delisting(self):
        tracker = AllMarketObservation(sample_limit=1)
        result = tracker.record(expected_symbols=("AAAUSDT", "BBBUSDT"),
            observed_symbols=("AAAUSDT", "UNKNOWN"), receive_time_ms=TIME,
            last_valid_event_time_ms=TIME - 1, connection_active=True, st_filtered_count=2)
        self.assertEqual(result["observed_symbol_ratio"], .5)
        self.assertEqual(result["missing_symbols"], ["BBBUSDT"])
        self.assertEqual(result["unknown_symbol_count"], 1)
        self.assertFalse(result["directory_completeness_proven"])
        self.assertFalse(result["depth_available"])
        result["missing_symbols"].clear()
        self.assertEqual(tracker.snapshot()["missing_symbol_count"], 1)

    def test_observation_iterables_are_bounded_even_if_duplicates(self):
        with self.assertRaises(ValueError):
            AllMarketObservation(max_instruments=2).record(expected_symbols=("A",) * 3,
                observed_symbols=(), receive_time_ms=TIME, last_valid_event_time_ms=None, connection_active=True)

    def test_diagnostics_counts_survive_suppression_and_do_not_echo_secrets(self):
        diagnostics = BoundedDiagnostics(max_samples=4, max_reasons=4, samples_per_reason_minute=2)
        for _ in range(100):
            diagnostics.record("malformed", observed_at_ms=TIME, detail="Authorization: Bearer secret https://private/path")
        data = diagnostics.snapshot()
        self.assertEqual(data["counters"]["malformed"], 100)
        self.assertEqual(len(data["samples"]), 2)
        self.assertNotIn("secret", str(data))
        self.assertNotIn("private", str(data))
        for n in range(100):
            diagnostics.record(f"reason_{n}", observed_at_ms=TIME + n * 60000)
        self.assertLessEqual(len(diagnostics.snapshot()["counters"]), 4)
        self.assertLessEqual(len(diagnostics.snapshot()["samples"]), 4)

    def test_diagnostics_late_clock_cannot_reset_minute_sample_limit(self):
        diagnostics = BoundedDiagnostics(samples_per_reason_minute=1)
        for offset in (60000, 0, 60000, 0):
            diagnostics.record("bad", observed_at_ms=TIME + offset)
        self.assertEqual(len(diagnostics.snapshot()["samples"]), 1)
        self.assertEqual(diagnostics.snapshot()["last_error_time_ms"], TIME + 60000)
        diagnostics.snapshot()["samples"][0]["reason"] = "tampered"
        self.assertEqual(diagnostics.snapshot()["samples"][0]["reason"], "bad")

    def test_future_trade_and_array_are_unavailable(self):
        result = OfflineIngestion().admit(ParseResult((trade(event_time_ms=TIME + 2001),)),
                                         context=self.context, now_ms=TIME)
        self.assertEqual(result.rejected_count, 1)
        self.assertEqual(result.diagnostics["counters"]["future_event_time"], 1)
        with self.assertRaises(ValueError):
            AllMarketObservation().record(expected_symbols=(), observed_symbols=(), receive_time_ms=TIME,
                                          last_valid_event_time_ms=TIME + 1, connection_active=True)

    def test_transport_is_a_bounded_recorder(self):
        transport = FakeTransport(max_actions=2)
        transport.open(route=Route.MARKET, shard_id="market-0", epoch=1)
        transport.send({"id": 1})
        transport.close(reason="stop")
        self.assertEqual(transport.network_calls, 0)
        self.assertEqual(transport.dropped_actions, 1)
        self.assertEqual(len(transport.drain_actions()), 2)
        self.assertEqual(transport.actions, [])
        with self.assertRaises(ValueError):
            transport.send({"id": 2})

    def test_limits_require_finite_native_integers(self):
        for bad in (0, True, 2.0, float("inf")):
            with self.assertRaises(ValueError):
                ParseLimits(max_items=bad)
