from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import unittest

from radars.altcoin_hunter.adapters.binance_usdm import BinanceInstrumentDirectory, parse_exchange_info
from radars.altcoin_hunter.adapters.fixtures import FIXTURE_TIME_MS as NOW, fixture_exchange_info
from radars.altcoin_hunter.identity import InstrumentIdentity
from radars.altcoin_hunter.universe import EligibilityStatus, ListingStage


class BinanceExchangeInfoTests(unittest.TestCase):
    def test_static_directory_preserves_filter_precision_and_binance_metadata(self):
        path = Path(__file__).with_name("fixtures") / "binance" / "exchange_info.json"
        result = parse_exchange_info(path.read_text(encoding="utf-8"), observed_at_ms=NOW)
        self.assertTrue(result.accepted)
        spec = result.registry["AAAUSDT"]
        self.assertEqual((spec.tick_size, spec.step_size, spec.min_notional), ("0.0100", "0.100", "5.00"))
        self.assertEqual(spec.underlying_subtypes, ("SYNTHETIC",))
        self.assertEqual(spec.delivery_date_ms, 4_133_404_800_000)
        self.assertEqual(spec.identity.market, "usdt_perpetual")
        self.assertEqual(spec.to_hunter_instrument().eligibility_status, EligibilityStatus.ELIGIBLE)

    def test_exchange_server_time_is_not_used_as_refresh_clock(self):
        payload = fixture_exchange_info()
        payload["serverTime"] = 1
        result = parse_exchange_info(payload, observed_at_ms=NOW)
        self.assertTrue(result.accepted)
        self.assertTrue(all(spec.observed_at_ms == NOW for spec in result.instruments))

    def test_each_filter_is_required_and_precision_fields_never_substitute(self):
        for missing in ("PRICE_FILTER", "LOT_SIZE", "MIN_NOTIONAL"):
            with self.subTest(missing=missing):
                payload = fixture_exchange_info()
                row = payload["symbols"][0]
                row["filters"] = [f for f in row["filters"] if f["filterType"] != missing]
                result = parse_exchange_info(payload, observed_at_ms=NOW)
                self.assertEqual(result.status, "malformed")
                self.assertEqual(result.instruments, ())

    def test_optional_market_filter_remains_null_without_invented_lot_values(self):
        payload = fixture_exchange_info()
        payload["symbols"][0]["filters"] = [f for f in payload["symbols"][0]["filters"] if f["filterType"] != "MARKET_LOT_SIZE"]
        spec = parse_exchange_info(payload, observed_at_ms=NOW).registry["AAAUSDT"]
        self.assertIsNone(spec.market_step_size)
        self.assertIsNone(spec.market_min_quantity)
        self.assertIsNone(spec.market_max_quantity)

    def test_notional_filter_alternative_retains_exact_minimum_and_maximum(self):
        payload = fixture_exchange_info()
        payload["symbols"][0]["filters"][-1] = {"filterType": "NOTIONAL", "minNotional": "5.000", "maxNotional": "5000.00"}
        spec = parse_exchange_info(payload, observed_at_ms=NOW).registry["AAAUSDT"]
        self.assertEqual((spec.min_notional, spec.max_notional), ("5.000", "5000.00"))

    def test_invalid_core_filter_values_fail_entire_refresh(self):
        for value in (None, 0.01, True, "NaN", "Infinity", "-0.01", "0"):
            with self.subTest(value=value):
                payload = fixture_exchange_info()
                payload["symbols"][0]["filters"][0]["tickSize"] = value
                self.assertEqual(parse_exchange_info(payload, observed_at_ms=NOW).status, "malformed")

    def test_valid_out_of_scope_instruments_are_explicitly_ineligible(self):
        for changes in ({"quoteAsset": "USDC", "marginAsset": "USDC"}, {"contractType": "CURRENT_QUARTER"},
                        {"status": "PENDING_TRADING"}, {"underlyingType": "INDEX"}):
            with self.subTest(changes=changes):
                payload = fixture_exchange_info()
                payload["symbols"][0].update(changes)
                result = parse_exchange_info(payload, observed_at_ms=NOW)
                self.assertTrue(result.accepted)
                spec = result.registry["AAAUSDT"]
                self.assertFalse(spec.eligible)
                self.assertEqual(spec.to_hunter_instrument().eligibility_status, EligibilityStatus.INELIGIBLE)

    def test_duplicate_symbols_filters_and_malformed_sibling_fail_atomically(self):
        for case in ("symbol", "filter", "sibling"):
            payload = fixture_exchange_info()
            if case == "symbol":
                payload["symbols"].append(deepcopy(payload["symbols"][0]))
            elif case == "filter":
                payload["symbols"][0]["filters"].append(deepcopy(payload["symbols"][0]["filters"][0]))
            else:
                payload["symbols"].append({"symbol": "BROKENUSDT"})
            with self.subTest(case=case):
                result = parse_exchange_info(payload, observed_at_ms=NOW)
                self.assertEqual(result.status, "malformed")
                self.assertEqual(result.instruments, ())
                self.assertGreater(result.diagnostics["rejected_count"], 0)

    def test_unknown_fields_including_finite_float_are_counted_and_compatible(self):
        payload = fixture_exchange_info()
        payload["newTopMetric"] = 0.25
        payload["symbols"][0]["newSymbolMetric"] = 0.75
        payload["symbols"][0]["filters"][0]["newFilterMetric"] = 1.5
        result = parse_exchange_info(payload, observed_at_ms=NOW)
        self.assertTrue(result.accepted)
        self.assertEqual(result.diagnostics["unknown_field_count"], 3)

    def test_unknown_identity_and_thousand_prefix_are_never_guessed(self):
        spec = parse_exchange_info(fixture_exchange_info(), observed_at_ms=NOW).registry["1000TESTUSDT"]
        self.assertIsNone(spec.identity.canonical_asset_id)
        self.assertEqual((spec.identity.symbol, spec.identity.contract_multiplier, spec.identity.mapping_method),
                         ("1000TESTUSDT", "1", "unresolved"))

    def test_explicit_identity_is_preserved_and_conflicts_rejected(self):
        identity = InstrumentIdentity("binance", "usdt_perpetual", "explicit-id", "TEST-UNIT", "1000TESTUSDT",
                                      "test:token", "1000", "contracts", "USDT", "explicit")
        result = parse_exchange_info(fixture_exchange_info(), observed_at_ms=NOW, identities={"1000TESTUSDT": identity})
        self.assertEqual(result.registry["1000TESTUSDT"].identity, identity)
        bad = replace(identity, exchange="other")
        self.assertEqual(parse_exchange_info(fixture_exchange_info(), observed_at_ms=NOW,
                         identities={"1000TESTUSDT": bad}).status, "malformed")

    def test_last_good_directory_survives_every_failed_refresh_status(self):
        directory = BinanceInstrumentDirectory()
        self.assertTrue(directory.refresh(fixture_exchange_info(), observed_at_ms=NOW).accepted)
        original = directory.snapshot()
        cases = (({"symbols": []}, {}, "incomplete"), ({}, {}, "malformed"),
                 ({}, {"complete": False}, "incomplete"), ({}, {"source_healthy": False}, "source_unavailable"),
                 (fixture_exchange_info(), {"observed_at_ms": NOW - 1}, "stale"))
        for payload, options, status in cases:
            with self.subTest(status=status):
                kwargs = {"observed_at_ms": NOW + 1000, **options}
                result = directory.refresh(payload, **kwargs)
                self.assertEqual(result.status, status)
                self.assertIs(directory.snapshot(), original)
                self.assertEqual(len(result.instruments), 3)

    def test_absence_retains_member_and_explicit_delisting_changes_eligibility(self):
        directory = BinanceInstrumentDirectory()
        directory.refresh(fixture_exchange_info(), observed_at_ms=NOW)
        payload = fixture_exchange_info()
        payload["symbols"] = payload["symbols"][:1]
        directory.refresh(payload, observed_at_ms=NOW + 1000)
        self.assertEqual(len(directory.snapshot()), 3)
        payload["symbols"][0]["status"] = "DELIVERED"
        result = directory.refresh(payload, observed_at_ms=NOW + 2000)
        self.assertTrue(result.accepted)
        self.assertEqual(directory.snapshot()["AAAUSDT"].to_hunter_instrument().listing_stage, ListingStage.DELISTING)

    def test_filter_change_increments_metadata_version_without_spurious_repeat(self):
        directory = BinanceInstrumentDirectory()
        directory.refresh(fixture_exchange_info(), observed_at_ms=NOW)
        payload = fixture_exchange_info()
        payload["symbols"][0]["filters"][0]["tickSize"] = "0.0200"
        directory.refresh(payload, observed_at_ms=NOW + 1000)
        self.assertEqual(directory.snapshot()["AAAUSDT"].metadata_version, 2)
        count = len(directory.universe.history)
        directory.refresh(payload, observed_at_ms=NOW + 2000)
        self.assertEqual(directory.snapshot()["AAAUSDT"].metadata_version, 2)
        self.assertEqual(len(directory.universe.history), count)

    def test_capacity_failure_does_not_publish_any_new_directory_member(self):
        directory = BinanceInstrumentDirectory(max_instruments=2)
        result = directory.refresh(fixture_exchange_info(), observed_at_ms=NOW)
        self.assertEqual(result.status, "malformed")
        self.assertEqual(dict(directory.snapshot()), {})
        self.assertEqual(directory.universe.snapshot(), ())

    def test_non_native_dates_and_invalid_refresh_time_are_rejected(self):
        for field in ("onboardDate", "deliveryDate"):
            payload = fixture_exchange_info()
            payload["symbols"][0][field] = str(payload["symbols"][0][field])
            self.assertEqual(parse_exchange_info(payload, observed_at_ms=NOW).status, "malformed")
        with self.assertRaises(ValueError):
            parse_exchange_info(fixture_exchange_info(), observed_at_ms=NOW // 1000)

    def test_static_partial_directory_does_not_replace_last_good_members(self):
        directory = BinanceInstrumentDirectory()
        directory.refresh(fixture_exchange_info(), observed_at_ms=NOW)
        before = directory.snapshot()
        path = Path(__file__).with_name("fixtures") / "binance" / "exchange_info_partial.json"
        result = directory.refresh(path.read_text(encoding="utf-8"), observed_at_ms=NOW + 1000)
        self.assertEqual(result.status, "malformed")
        self.assertIs(directory.snapshot(), before)


if __name__ == "__main__":
    unittest.main()
