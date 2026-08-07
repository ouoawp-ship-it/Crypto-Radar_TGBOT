from __future__ import annotations

import unittest

from radars.launch_warning.scoring import (
    SCORE_SEMANTICS,
    build_threshold_profile,
    score_launch_signal,
)


class LaunchScoringTests(unittest.TestCase):
    def test_unknown_classification_uses_most_conservative_profile(self) -> None:
        known = build_threshold_profile(
            asset_subclass="altcoin",
            liquidity_tier="medium",
            recent_volatility_pct=1.2,
        )
        unknown = build_threshold_profile(
            asset_subclass="unknown",
            liquidity_tier="unknown",
            recent_volatility_pct=None,
        )

        self.assertGreater(unknown["price_trigger_pct"], known["price_trigger_pct"])
        self.assertGreater(unknown["oi_trigger_pct"], known["oi_trigger_pct"])
        self.assertGreater(
            unknown["volume_ratio_trigger"], known["volume_ratio_trigger"]
        )
        self.assertEqual(unknown["asset_profile"], "unknown_conservative")

    def test_low_liquidity_and_high_volatility_raise_thresholds(self) -> None:
        ordinary = build_threshold_profile(
            asset_subclass="altcoin",
            liquidity_tier="medium",
            recent_volatility_pct=1.0,
        )
        noisy = build_threshold_profile(
            asset_subclass="altcoin",
            liquidity_tier="low",
            recent_volatility_pct=4.0,
        )

        self.assertGreater(noisy["price_trigger_pct"], ordinary["price_trigger_pct"])
        self.assertGreater(noisy["oi_trigger_pct"], ordinary["oi_trigger_pct"])

    def test_each_group_is_capped_and_total_never_exceeds_100(self) -> None:
        result = score_launch_signal({
            "asset_subclass": "altcoin",
            "liquidity_tier": "medium",
            "recent_volatility_pct": 1.0,
            "price_15m": 100.0,
            "price_1h": 100.0,
            "oi_15m": 100.0,
            "oi_1h": 100.0,
            "volume_ratio": 100.0,
            "breakout": True,
            "spot_active_ratio": 1.0,
            "futures_active_ratio": 1.0,
        })

        self.assertEqual(result["score"], 100)
        self.assertEqual(result["discovery_score"], 100)
        self.assertEqual(result["discovery_score"], result["score"])
        self.assertEqual(
            result["group_scores"],
            {"price": 25, "open_interest": 25, "volume": 20, "structure": 15, "active_funds": 15},
        )
        self.assertEqual(result["score_semantics"], SCORE_SEMANTICS)
        self.assertEqual(result["discovery_score_semantics"], SCORE_SEMANTICS)

    def test_multiple_timeframes_do_not_double_count_a_group(self) -> None:
        both = score_launch_signal({
            "asset_subclass": "altcoin",
            "liquidity_tier": "medium",
            "recent_volatility_pct": 1.0,
            "price_15m": 8.0,
            "price_1h": 12.0,
            "oi_15m": 8.0,
            "oi_1h": 12.0,
        })
        strongest_only = score_launch_signal({
            "asset_subclass": "altcoin",
            "liquidity_tier": "medium",
            "recent_volatility_pct": 1.0,
            "price_15m": 0.0,
            "price_1h": 12.0,
            "oi_15m": 0.0,
            "oi_1h": 12.0,
        })

        self.assertEqual(both["group_scores"]["price"], strongest_only["group_scores"]["price"])
        self.assertEqual(
            both["group_scores"]["open_interest"],
            strongest_only["group_scores"]["open_interest"],
        )

    def test_momentum_path_requires_price_and_participation(self) -> None:
        result = score_launch_signal({
            "asset_subclass": "altcoin",
            "liquidity_tier": "medium",
            "recent_volatility_pct": 1.0,
            "price_15m": 4.0,
            "price_1h": 6.0,
            "oi_15m": 4.0,
            "oi_1h": 6.0,
            "volume_ratio": 2.2,
            "breakout": True,
            "spot_active_ratio": 0.20,
            "futures_active_ratio": 0.16,
        })

        self.assertEqual(result["trigger_path"], "momentum")
        self.assertIn("price_momentum_met", result["supporting_evidence"])
        self.assertIn("open_interest_growth_met", result["supporting_evidence"])
        self.assertIn("spot_active_buying_met", result["supporting_evidence"])

    def test_dark_current_path_preserves_early_detection(self) -> None:
        result = score_launch_signal({
            "asset_subclass": "altcoin",
            "liquidity_tier": "medium",
            "recent_volatility_pct": 1.0,
            "price_15m": 0.5,
            "price_1h": 0.7,
            "oi_15m": 5.0,
            "oi_1h": 8.0,
            "volume_ratio": 2.3,
            "breakout": False,
            "futures_active_ratio": 0.18,
        })

        self.assertEqual(result["trigger_path"], "dark_current")
        self.assertIn("price_still_quiet", result["supporting_evidence"])

    def test_opposing_oi_and_active_flow_are_counter_evidence(self) -> None:
        result = score_launch_signal({
            "asset_subclass": "altcoin",
            "liquidity_tier": "medium",
            "recent_volatility_pct": 1.0,
            "price_15m": 5.0,
            "price_1h": 6.0,
            "oi_15m": -4.0,
            "oi_1h": -5.0,
            "volume_ratio": 2.0,
            "breakout": True,
            "spot_active_ratio": -0.20,
            "futures_active_ratio": -0.18,
        })

        self.assertIn("price_up_oi_down", result["counter_evidence"])
        self.assertIn("active_selling_against_move", result["counter_evidence"])
        self.assertNotEqual(result["trigger_path"], "momentum")

    def test_price_down_with_oi_growth_blocks_bullish_launch_path(self) -> None:
        result = score_launch_signal({
            "asset_subclass": "altcoin",
            "liquidity_tier": "medium",
            "recent_volatility_pct": 1.0,
            "price_15m": -4.0,
            "price_1h": -6.0,
            "oi_15m": 4.0,
            "oi_1h": 6.0,
            "volume_ratio": 2.2,
            "breakout": False,
            "futures_active_ratio": 0.18,
        })

        self.assertIn("price_down_oi_up", result["counter_evidence"])
        self.assertEqual(result["trigger_path"], "none")

    def test_missing_price_does_not_qualify_as_quiet_price(self) -> None:
        result = score_launch_signal({
            "asset_subclass": "altcoin",
            "liquidity_tier": "medium",
            "recent_volatility_pct": 1.0,
            "price_15m": None,
            "price_1h": 0.5,
            "oi_15m": 5.0,
            "oi_1h": 8.0,
            "volume_ratio": 2.3,
            "breakout": False,
            "futures_active_ratio": 0.18,
        })

        self.assertEqual(result["trigger_path"], "none")
        self.assertTrue(result["data_availability"]["price"])

    def test_nonfinite_values_are_missing_not_zero_or_available(self) -> None:
        result = score_launch_signal({
            "asset_subclass": "altcoin",
            "liquidity_tier": "medium",
            "price_15m": float("nan"),
            "price_1h": float("inf"),
            "oi_15m": None,
            "oi_1h": None,
        })

        self.assertFalse(result["data_availability"]["price"])
        self.assertFalse(result["data_availability"]["open_interest"])
        self.assertEqual(result["group_scores"]["price"], 0)

    def test_accepts_strict_market_fact_field_names(self) -> None:
        result = score_launch_signal({
            "asset_subclass": "altcoin",
            "liquidity_tier": "medium",
            "recent_volatility_pct": 1.0,
            "price_15m_pct": 4.0,
            "price_1h_pct": 6.0,
            "oi_15m_pct": 4.0,
            "oi_1h_pct": 6.0,
            "volume_ratio_15m": 2.2,
            "breakout": True,
        })

        self.assertGreater(result["group_scores"]["price"], 0)
        self.assertGreater(result["group_scores"]["open_interest"], 0)
        self.assertGreater(result["group_scores"]["volume"], 0)

    def test_missing_active_flow_is_not_scored_as_zero(self) -> None:
        result = score_launch_signal({
            "asset_subclass": "altcoin",
            "liquidity_tier": "medium",
            "recent_volatility_pct": 1.0,
            "price_15m": 4.0,
            "oi_15m": 4.0,
            "volume_ratio": 2.0,
            "breakout": True,
            "spot_active_ratio": None,
            "futures_active_ratio": None,
        })

        self.assertEqual(result["group_scores"]["active_funds"], 0)
        self.assertFalse(result["data_availability"]["active_funds"])
        self.assertNotIn("active_selling_against_move", result["counter_evidence"])

    def test_historical_calibration_is_report_only(self) -> None:
        facts = {
            "asset_subclass": "altcoin",
            "liquidity_tier": "medium",
            "recent_volatility_pct": 1.0,
            "price_15m": 4.0,
            "oi_15m": 4.0,
            "volume_ratio": 2.0,
            "breakout": True,
            "historical_success_rate": 0.01,
        }

        with_history = score_launch_signal(facts)
        without_history = score_launch_signal({
            key: value for key, value in facts.items() if key != "historical_success_rate"
        })

        self.assertEqual(with_history["score"], without_history["score"])
        self.assertEqual(with_history["threshold_profile"], without_history["threshold_profile"])
        self.assertEqual(
            with_history["historical_calibration"], "report_only_not_applied"
        )


if __name__ == "__main__":
    unittest.main()
