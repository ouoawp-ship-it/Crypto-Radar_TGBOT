from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import unittest

from radars.altcoin_hunter.adapters.base import ParseLimits, Route
from radars.altcoin_hunter.adapters.binance_protocol import (parse_binance_payload, parse_funding_info,
                                                           parse_open_interest_response, parse_server_time,
                                                           _oi_base_quantity)
from radars.altcoin_hunter.adapters.binance_usdm import parse_exchange_info
from radars.altcoin_hunter.adapters.fixtures import FIXTURE_TIME_MS as NOW, fixture_exchange_info, fixture_registry
from radars.altcoin_hunter.identity import InstrumentIdentity

FIXTURES = Path(__file__).with_name("fixtures") / "binance"


def payload(name):
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))["payload"]


class BinanceProtocolTests(unittest.TestCase):
    def setUp(self):
        self.registry = fixture_registry()

    def parse(self, value, kind="agg_trade", **kwargs):
        return parse_binance_payload(value, kind, self.registry, receive_time_ms=NOW + 200,
                                     receive_monotonic_ns=7_000_000, **kwargs)

    def test_trade_preserves_exchange_time_sequence_maker_direction_and_decimal_text(self):
        result = self.parse(payload("agg_trade"))
        event = result.events[0]
        self.assertEqual(event.source, "binance_usdm_agg_trade")
        self.assertEqual(event.market, "usdt_perpetual")
        self.assertEqual((event.event_time_ms, event.receive_time_ms, event.receive_monotonic_ns), (NOW + 90, NOW + 200, 7_000_000))
        self.assertEqual((event.sequence_start, event.sequence_end, event.source_event_id), (1001, 1003, "101"))
        self.assertFalse(event.payload.buyer_is_maker)
        self.assertEqual((event.payload.price, event.payload.quantity), ("10.0100", "2.500"))
        self.assertEqual(event.payload.quote_notional, Decimal("25.0250000"))
        self.assertEqual(result.event_metadata[0]["exchange_event_time_ms"], NOW + 100)

    def test_quantity_includes_rpi_and_nq_never_replaces_it(self):
        event = self.parse(payload("agg_trade_rpi")).events[0]
        self.assertEqual(event.payload.quantity, "5")
        self.assertEqual(event.payload.quote_notional, Decimal("50.0500"))
        self.assertTrue(self.parse(payload("agg_trade_sell")).events[0].payload.buyer_is_maker)

    def test_raw_json_bytes_combined_and_array_have_same_identity(self):
        raw = payload("agg_trade")
        values = (raw, json.dumps(raw), json.dumps(raw).encode(), payload("agg_trade_combined"), [raw])
        keys = [self.parse(value).events[0].dedup_key for value in values]
        self.assertTrue(all(key == keys[0] for key in keys))

    def test_symbol_type_is_required_native_integer_and_cm_never_falls_back(self):
        for name, reason in (("missing_st", "missing_symbol_type"), ("string_st", "invalid_symbol_type"),
                             ("boolean_st", "invalid_symbol_type"), ("cm_rejected", "cm_payload_rejected")):
            with self.subTest(name=name):
                result = self.parse(payload(name))
                self.assertEqual(result.events, ())
                self.assertEqual(result.rejected_items[0].reason, reason)
        for value in (None, 0, 3, 1.0):
            row = payload("agg_trade")
            row["st"] = value
            self.assertEqual(self.parse(row).events, ())

    def test_bad_sibling_does_not_discard_valid_symbols(self):
        result = self.parse(payload("mixed_valid_invalid"))
        self.assertEqual([event.exchange_symbol for event in result.events], ["AAAUSDT", "BBBUSDT"])
        self.assertEqual(result.diagnostics["rejected_count"], 1)
        self.assertEqual(result.rejected_items[0].index, 1)

    def test_unknown_fields_are_counted_without_changing_event_identity(self):
        base = self.parse(payload("agg_trade"))
        unknown = self.parse(payload("unknown_fields"))
        self.assertEqual(unknown.events, base.events)
        self.assertEqual(unknown.diagnostics["unknown_field_count"], 2)

    def test_invalid_core_values_never_become_events(self):
        for name in ("unknown_symbol", "partial_payload", "nonfinite_price", "negative_quantity", "float_quantity", "seconds_timestamp", "inverted_sequence"):
            with self.subTest(name=name):
                result = self.parse(payload(name))
                self.assertEqual(result.events, ())
                self.assertEqual(result.diagnostics["rejected_count"], 1)

    def test_raw_boolean_and_float_ids_and_maker_flags_are_rejected(self):
        for field, value in (("a", True), ("f", 1.5), ("l", "1003"), ("m", "false"), ("m", 0), ("E", NOW // 1000)):
            row = payload("agg_trade")
            row[field] = value
            with self.subTest(field=field):
                self.assertEqual(self.parse(row).events, ())

    def test_duplicate_out_of_order_and_gap_packets_remain_observable_for_ingestion(self):
        duplicate = self.parse(payload("agg_trade_duplicate"))
        self.assertEqual(len(duplicate.events), 2)
        self.assertEqual(duplicate.events[0].dedup_key, duplicate.events[1].dedup_key)
        out_of_order = self.parse(payload("agg_trade_out_of_order"))
        self.assertGreater(out_of_order.events[0].event_time_ms, out_of_order.events[1].event_time_ms)
        gaps = self.parse(payload("agg_trade_gap"))
        self.assertEqual(gaps.events[1].sequence_start - gaps.events[0].sequence_end, 7)

    def test_mark_packet_produces_separate_mark_and_funding_events(self):
        result = self.parse(payload("mark_price"), "mark_price")
        mark, funding = result.events
        self.assertEqual((mark.event_type, funding.event_type), ("mark_price", "funding"))
        self.assertEqual((mark.payload.mark_price, mark.payload.index_price), ("10.0000", "9.9900"))
        self.assertEqual(funding.event_time_ms, NOW + 100)
        self.assertEqual(funding.payload.next_funding_time_ms, NOW + 28_800_000)
        self.assertEqual(funding.payload.funding_rate, "-0.000100")
        self.assertIsNone(funding.payload.interval_hours)

    def test_funding_interval_is_explicit_metadata_and_absent_is_not_eight(self):
        info = parse_funding_info(payload("funding_info"), self.registry)
        result = self.parse(payload("mark_price_array"), "mark_price", funding_info=info)
        funding = [event for event in result.events if event.event_type == "funding"]
        self.assertEqual([event.payload.interval_hours for event in funding], [4, None])
        self.assertEqual(info.entries["AAAUSDT"].adjusted_cap, "0.025")

    def test_mark_combined_array_preserves_sibling_isolation(self):
        frame = payload("mark_price_combined")
        frame["data"][1]["st"] = 2
        result = self.parse(frame, "mark_price", route=Route.MARKET)
        self.assertEqual(len(result.events), 2)
        self.assertEqual(result.diagnostics["cm_payload_rejected"], 1)

    def test_bbo_global_and_promoted_have_identical_dedup_identity(self):
        global_result = self.parse(payload("book_ticker_global"), "book_ticker", route=Route.PUBLIC)
        promoted = self.parse(payload("book_ticker_promoted"), "book_ticker", route=Route.PUBLIC)
        self.assertEqual(global_result.events[0].dedup_key, promoted.events[0].dedup_key)
        self.assertEqual(global_result.events[0].source_event_id, "AAAUSDT:201")
        self.assertFalse(global_result.event_metadata[0]["promoted"])
        self.assertTrue(promoted.event_metadata[0]["promoted"])
        self.assertEqual(global_result.events[0].event_time_ms, NOW + 90)

    def test_bbo_crossed_prices_or_missing_quantity_reject(self):
        self.assertEqual(self.parse(payload("crossed_book"), "book_ticker").events, ())
        row = payload("book_ticker")
        del row["B"]
        self.assertEqual(self.parse(row, "book_ticker").events, ())

    def test_route_and_stream_symbol_mismatch_reject(self):
        self.assertEqual(self.parse(payload("agg_trade"), route=Route.PUBLIC).rejected_items[0].reason, "route_mismatch")
        self.assertEqual(self.parse(payload("agg_trade"), stream="bbbusdt@aggTrade").rejected_items[0].reason, "stream_mismatch")
        self.assertEqual(self.parse(payload("mark_price"), "mark_price", stream="!markPrice@arr@3s").events, ())

    def test_liquidation_uses_outer_st_and_nested_order_without_inventing_exhaustive_flow(self):
        raw = self.parse(payload("liquidation"), "liquidation")
        combined = self.parse(payload("liquidation_combined"), "liquidation")
        event = raw.events[0]
        self.assertEqual(event.dedup_key, combined.events[0].dedup_key)
        self.assertEqual(len(event.source_event_id), 64)
        self.assertEqual(event.payload.quantity, "3.000")
        self.assertEqual(event.payload.side, "sell")
        self.assertIn("liquidation_snapshot_not_exhaustive", event.quality_flags)
        self.assertEqual(raw.event_metadata[0]["snapshot_interval_ms"], 1000)
        self.assertEqual(raw.event_metadata[0]["cumulative_filled_quantity"], "2.000")
        self.assertEqual(self.parse(payload("liquidation_nested_st"), "liquidation").rejected_items[0].reason, "missing_symbol_type")
        self.assertEqual(self.parse(payload("liquidation_cm"), "liquidation").rejected_items[0].reason, "cm_payload_rejected")

    def test_liquidation_surrogate_is_order_independent_and_ignores_new_fields(self):
        row = payload("liquidation")
        first = self.parse(row, "liquidation").events[0]
        row["o"] = dict(reversed(list(row["o"].items())))
        row["o"]["futureField"] = "retained by upstream"
        self.assertEqual(self.parse(row, "liquidation").events[0].source_event_id, first.source_event_id)

    def test_open_interest_does_not_infer_price_quote_value_or_direction(self):
        result = parse_open_interest_response(payload("open_interest"), self.registry,
                                              receive_time_ms=NOW + 200, receive_monotonic_ns=7)
        event = result.events[0]
        self.assertEqual(event.payload.open_interest, "1200.500")
        self.assertEqual(event.payload.unit, "base")
        self.assertIsNone(event.payload.quote_notional)
        self.assertEqual(event.event_time_ms, NOW + 100)
        self.assertEqual(result.event_metadata[0]["base_quantity"], "1200.500")
        self.assertIsNone(result.event_metadata[0]["base_quantity_missing_reason"])
        self.assertTrue(event.source_event_id.startswith(f"AAAUSDT:{NOW + 100}:"))

    def test_explicit_contract_identity_and_multiplier_are_not_lost(self):
        identity = InstrumentIdentity("binance", "usdt_perpetual", "test:contract", "TEST", "AAAUSDT", "asset:test",
                                      "1000", "contracts", "USDT", "explicit")
        self.registry = parse_exchange_info(fixture_exchange_info(), observed_at_ms=NOW, identities={"AAAUSDT": identity}).registry
        trade = self.parse(payload("agg_trade")).events[0]
        self.assertEqual(trade.instrument_id, "test:contract")
        self.assertEqual(trade.payload.base_quantity, Decimal("2500"))
        result = self.parse(payload("open_interest"), "open_interest")
        oi = result.events[0]
        self.assertEqual((oi.payload.unit, oi.payload.contract_multiplier), ("contracts", "1000"))
        self.assertEqual(oi.payload.open_interest, "1200.500")
        self.assertEqual(result.event_metadata[0]["raw_open_interest_quantity"], "1200.500")
        self.assertEqual(result.event_metadata[0]["raw_open_interest_unit"], "contracts")
        self.assertEqual(Decimal(result.event_metadata[0]["base_quantity"]), Decimal("1200500"))
        self.assertIsNone(oi.payload.quote_notional)

    def test_oi_conversion_is_exact_and_quote_unit_does_not_infer_price(self):
        quantity = "1.12345678901234567890123456789012345"
        multiplier = "1000"
        converted, reason = _oi_base_quantity(quantity, "contracts", multiplier)
        self.assertEqual(Decimal(converted), Decimal("1123.45678901234567890123456789012345000"))
        self.assertIsNone(reason)
        self.assertEqual(_oi_base_quantity("1200.500", "quote", "1000"), (None, "quote_unit_requires_price"))

    def test_oi_conversion_rejects_finite_inputs_with_overflow_or_underflow_product(self):
        for quantity, multiplier in (("1e308", "1000"), ("1e-300", "1e-100")):
            with self.subTest(quantity=quantity, multiplier=multiplier):
                identity = InstrumentIdentity("binance", "usdt_perpetual", "AAAUSDT", "AAAUSDT", "AAAUSDT",
                                              contract_multiplier=multiplier, quantity_unit="contracts")
                self.registry = parse_exchange_info(fixture_exchange_info(), observed_at_ms=NOW,
                                                   identities={"AAAUSDT": identity}).registry
                row = payload("open_interest")
                row["openInterest"] = quantity
                result = self.parse(row, "open_interest")
                self.assertEqual(result.events, ())
                self.assertEqual(result.diagnostics["rejected_count"], 1)

    def test_server_time_is_strict_millisecond_time_and_extra_rest_kind_is_not_enabled(self):
        self.assertEqual(parse_server_time(payload("server_time")), NOW + 100)
        with self.assertRaises(ValueError):
            parse_server_time({"serverTime": NOW // 1000})
        self.assertEqual(self.parse({}, "premium_index").rejected_items[0].reason, "unsupported_kind")

    def test_bounds_and_rejection_details_are_hard_limits(self):
        rows = [None] * 100
        result = self.parse(rows, limits=ParseLimits(max_rejected_items=3))
        self.assertEqual(result.diagnostics["rejected_count"], 100)
        self.assertEqual(len(result.rejected_items), 3)
        self.assertEqual(self.parse([payload("agg_trade")] * 3, limits=ParseLimits(max_items=2)).events, ())
        deep = payload("agg_trade")
        deep["nested"] = {"a": {"b": {"c": {"d": {"e": 1}}}}}
        self.assertEqual(self.parse(deep, limits=ParseLimits(max_depth=3)).events, ())
        long = payload("agg_trade")
        long["text"] = "x" * 5000
        result = self.parse([long, payload("agg_trade")])
        self.assertEqual(len(result.events), 1)
        self.assertEqual(result.rejected_items[0].index, 0)

    def test_two_thousand_item_array_is_supported_without_unbounded_rejections(self):
        result = self.parse([payload("agg_trade")] * 2000)
        self.assertEqual(len(result.events), 2000)
        self.assertEqual(result.diagnostics.get("rejected_count", 0), 0)

    def test_invalid_json_unknown_kind_and_bad_clock_fail_explicitly(self):
        for value in ('{"a":', '{"a":1,"a":2}', '{"st":NaN}'):
            self.assertEqual(self.parse(value).events, ())
        self.assertEqual(self.parse({}, "private_order").rejected_items[0].reason, "unsupported_kind")
        with self.assertRaises(ValueError):
            parse_binance_payload(payload("agg_trade"), "agg_trade", self.registry,
                                  receive_time_ms=NOW // 1000, receive_monotonic_ns=1)

    def test_invalid_injected_stream_and_metadata_contracts_raise_value_error(self):
        for stream in (True, 1, 1.5, "", " x "):
            with self.subTest(stream=stream), self.assertRaises(ValueError):
                self.parse(payload("agg_trade_combined"), stream=stream)
        with self.assertRaises(ValueError):
            self.parse(payload("mark_price"), "mark_price", funding_info=True)
        with self.assertRaises(ValueError):
            parse_binance_payload(payload("agg_trade"), "agg_trade", True,
                                  receive_time_ms=NOW, receive_monotonic_ns=1)

    def test_combined_wrapper_bytes_and_depth_share_the_payload_budget(self):
        frame = {"stream": "aaausdt@aggTrade", "data": payload("agg_trade"), "extra": "x" * 900}
        result = self.parse(frame, limits=ParseLimits(max_payload_bytes=1000))
        self.assertEqual(result.events, ())
        self.assertEqual(result.diagnostics["payload_byte_limit"], 1)
        result = self.parse(payload("agg_trade_combined"), limits=ParseLimits(max_depth=1))
        self.assertEqual(result.events, ())
        self.assertEqual(result.diagnostics["rejected_count"], 1)

    def test_explicit_static_failure_fixtures_are_rejected(self):
        for name in ("wrong_boolean", "oversized_array", "malformed_combined", "infinite_price"):
            with self.subTest(name=name):
                result = self.parse(payload(name))
                self.assertEqual(result.events, ())
                self.assertGreater(result.diagnostics["rejected_count"], 0)


if __name__ == "__main__":
    unittest.main()
