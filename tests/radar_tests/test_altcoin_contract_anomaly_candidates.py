from __future__ import annotations

import math
import unittest

from radars.altcoin_contract_anomaly.models import (
    CandidateSnapshot,
    SCHEMA_VERSION,
    calculate_oi_market_cap_ratio,
    calculate_oi_value_usd,
)
from radars.altcoin_contract_anomaly.rules import (
    HIGH_LEVERAGE_CANDIDATE,
    SHORT_SQUEEZE_CANDIDATE,
    CandidateThresholds,
    apply_candidate_rules,
)
from radars.altcoin_contract_anomaly.state import candidate_sort_key


NOW = "2026-08-07T00:00:00+00:00"


def snapshot(**overrides: object) -> CandidateSnapshot:
    values: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "symbol": "TESTUSDT",
        "base_asset": "TEST",
        "normalized_asset": "TEST",
        "contract_multiplier": 1,
        "exchange": "binance",
        "contract_type": "PERPETUAL",
        "cmc_id": 123,
        "mapping_method": "existing_verified_anchor",
        "mapping_confidence": "high",
        "market_cap_usd": 30_000_000.0,
        "market_cap_source": "coinmarketcap_official",
        "market_cap_updated_at": NOW,
        "open_interest_raw": 15_000_000.0,
        "open_interest_unit": "usd_notional",
        "oi_value_usd": 15_000_000.0,
        "mark_price": 1.0,
        "funding_rate": -0.0001,
        "oi_market_cap_ratio": 0.50,
        "candidate_tags": [],
        "matched_rules": [],
        "data_quality": "complete",
        "missing_fields": [],
        "collected_at": NOW,
        "open_interest_updated_at": NOW,
        "mark_price_updated_at": NOW,
        "funding_rate_updated_at": NOW,
        "stale_fields": [],
        "invalid_fields": [],
        "mapping_evidence": ["binance_cmc_unique_id"],
        "mapping_rejection_reason": None,
        "oi_value_method": "binance_reported_usd_notional",
        "binance_oi_usd": 15_000_000.0,
        "binance_oi_market_cap_ratio": 0.50,
        "binance_oi_source": "binance_open_interest_hist.sumOpenInterestValue",
        "global_oi_usd": None,
        "global_oi_market_cap_ratio": None,
        "global_oi_source": None,
    }
    values.update(overrides)
    if "oi_value_usd" in overrides and "binance_oi_usd" not in overrides:
        values["binance_oi_usd"] = overrides["oi_value_usd"]
    if (
        "oi_market_cap_ratio" in overrides
        and "binance_oi_market_cap_ratio" not in overrides
    ):
        values["binance_oi_market_cap_ratio"] = overrides["oi_market_cap_ratio"]
    return CandidateSnapshot(**values)  # type: ignore[arg-type]


class OpenInterestNormalizationTests(unittest.TestCase):
    def test_raw_open_interest_is_multiplied_by_mark_price(self) -> None:
        self.assertEqual(
            calculate_oi_value_usd(2_500, unit="base_asset", mark_price=4.0),
            10_000.0,
        )
        self.assertEqual(
            calculate_oi_value_usd(
                2_500,
                unit="binance_sum_open_interest",
                mark_price=4.0,
            ),
            10_000.0,
        )

    def test_reported_usd_notional_is_not_multiplied_again(self) -> None:
        self.assertEqual(
            calculate_oi_value_usd(12_345, unit="usd_notional", mark_price=99.0),
            12_345.0,
        )

    def test_multiplier_contract_uses_its_quoted_mark_without_second_conversion(self) -> None:
        # Binance's 1000PEPE mark already prices the multiplier contract. Dividing
        # or multiplying by 1,000 here would corrupt the USD notional.
        self.assertEqual(
            calculate_oi_value_usd(2_000_000, unit="base_asset", mark_price=0.012),
            24_000.0,
        )

    def test_invalid_open_interest_and_mark_values_are_rejected(self) -> None:
        for value in (math.nan, math.inf, -math.inf, -1):
            with self.subTest(open_interest=value):
                self.assertIsNone(calculate_oi_value_usd(value, unit="usd_notional"))
        for mark in (math.nan, math.inf, -math.inf, -1, 0):
            with self.subTest(mark_price=mark):
                self.assertIsNone(
                    calculate_oi_value_usd(10, unit="base_asset", mark_price=mark)
                )

        self.assertEqual(calculate_oi_value_usd(0, unit="usd_notional"), 0.0)
        self.assertIsNone(calculate_oi_value_usd(10, unit="unknown", mark_price=2))

    def test_market_cap_ratio_rejects_invalid_denominators(self) -> None:
        self.assertEqual(calculate_oi_market_cap_ratio(5_000_000, 20_000_000), 0.25)
        self.assertEqual(calculate_oi_market_cap_ratio(0, 20_000_000), 0.0)
        for market_cap in (None, 0, -1, math.nan, math.inf, -math.inf):
            with self.subTest(market_cap=market_cap):
                self.assertIsNone(calculate_oi_market_cap_ratio(1, market_cap))
        for oi_value in (None, -1, math.nan, math.inf, -math.inf):
            with self.subTest(oi_value=oi_value):
                self.assertIsNone(calculate_oi_market_cap_ratio(oi_value, 1))


class CandidateRuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.thresholds = CandidateThresholds(
            market_cap_max_usd=30_000_000,
            short_squeeze_min_ratio=0.20,
            high_leverage_min_ratio=0.50,
        )

    def test_short_squeeze_rule_includes_exact_cap_and_ratio_boundaries(self) -> None:
        result = apply_candidate_rules(
            snapshot(
                market_cap_usd=30_000_000.0,
                oi_market_cap_ratio=0.20,
                funding_rate=-0.00000001,
            ),
            self.thresholds,
        )

        self.assertEqual(result.candidate_tags, [SHORT_SQUEEZE_CANDIDATE])
        self.assertEqual(len(result.matched_rules), 1)
        self.assertIn("short_squeeze", result.matched_rules[0])

    def test_values_immediately_outside_short_squeeze_boundaries_do_not_match(self) -> None:
        cases = (
            {"market_cap_usd": 30_000_000.01, "oi_market_cap_ratio": 0.20},
            {"market_cap_usd": 30_000_000.0, "oi_market_cap_ratio": 0.199999},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                result = apply_candidate_rules(snapshot(**overrides), self.thresholds)
                self.assertNotIn(SHORT_SQUEEZE_CANDIDATE, result.candidate_tags)

    def test_high_leverage_rule_includes_exact_ratio_boundary(self) -> None:
        result = apply_candidate_rules(
            snapshot(
                market_cap_usd=100_000_000.0,
                oi_market_cap_ratio=0.50,
                funding_rate=0.0,
            ),
            self.thresholds,
        )

        self.assertEqual(result.candidate_tags, [HIGH_LEVERAGE_CANDIDATE])

    def test_zero_funding_does_not_satisfy_negative_funding_condition(self) -> None:
        result = apply_candidate_rules(
            snapshot(oi_market_cap_ratio=0.20, funding_rate=0.0),
            self.thresholds,
        )

        self.assertEqual(result.candidate_tags, [])

    def test_one_symbol_can_match_both_candidate_rules(self) -> None:
        result = apply_candidate_rules(
            snapshot(oi_market_cap_ratio=0.50, funding_rate=-0.001),
            self.thresholds,
        )

        self.assertEqual(
            result.candidate_tags,
            [SHORT_SQUEEZE_CANDIDATE, HIGH_LEVERAGE_CANDIDATE],
        )
        self.assertEqual(len(result.matched_rules), 2)

    def test_missing_or_stale_base_fields_gate_all_formal_rules(self) -> None:
        unavailable_cases = (
            ("missing_fields", "market_cap_usd"),
            ("missing_fields", "oi_value_usd"),
            ("missing_fields", "mark_price"),
            ("stale_fields", "market_cap_usd"),
            ("stale_fields", "oi_value_usd"),
            ("stale_fields", "mark_price"),
            ("invalid_fields", "oi_value_usd"),
        )
        for collection, field in unavailable_cases:
            with self.subTest(collection=collection, field=field):
                result = apply_candidate_rules(
                    snapshot(**{collection: [field]}),
                    self.thresholds,
                )
                self.assertEqual(result.candidate_tags, [])

    def test_authoritative_binance_usd_oi_does_not_require_raw_quantity(self) -> None:
        result = apply_candidate_rules(
            snapshot(
                open_interest_raw=None,
                missing_fields=["open_interest_raw"],
                oi_value_method="binance_sum_open_interest_value",
            ),
            self.thresholds,
        )

        self.assertEqual(
            result.candidate_tags,
            [SHORT_SQUEEZE_CANDIDATE, HIGH_LEVERAGE_CANDIDATE],
        )

    def test_stale_funding_blocks_only_the_funding_dependent_rule(self) -> None:
        result = apply_candidate_rules(
            snapshot(stale_fields=["funding_rate"]),
            self.thresholds,
        )

        self.assertEqual(result.candidate_tags, [HIGH_LEVERAGE_CANDIDATE])

    def test_symbol_only_diagnostic_mapping_never_enters_formal_pool(self) -> None:
        result = apply_candidate_rules(
            snapshot(
                mapping_method="unique_symbol_diagnostic",
                mapping_confidence="diagnostic",
            ),
            self.thresholds,
        )

        self.assertEqual(result.candidate_tags, [])

    def test_candidate_sorting_is_ratio_descending_then_symbol(self) -> None:
        rows = [
            snapshot(symbol="ZZZUSDT", oi_market_cap_ratio=0.40),
            snapshot(symbol="BBBUSDT", oi_market_cap_ratio=0.60),
            snapshot(symbol="AAAUSDT", oi_market_cap_ratio=0.60),
            snapshot(symbol="NONEUSDT", oi_market_cap_ratio=None),
        ]

        ordered = sorted(rows, key=candidate_sort_key)

        self.assertEqual(
            [row.symbol for row in ordered],
            ["AAAUSDT", "BBBUSDT", "ZZZUSDT", "NONEUSDT"],
        )


if __name__ == "__main__":
    unittest.main()
