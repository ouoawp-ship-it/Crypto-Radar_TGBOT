from __future__ import annotations

import unittest

from radars.launch_warning.directional_runtime import (
    active_flow_window,
    build_directional_facts,
    build_trade_plans,
    select_directional_candidates,
)


class LaunchDirectionalRuntimeTests(unittest.TestCase):
    def test_selection_is_bounded_and_includes_active_first(self) -> None:
        selected = select_directional_candidates([
            {"symbol": "AAAUSDT", "score": 99},
            {"symbol": "BBBUSDT", "score": 1, "launch_lifecycle_active": True},
            {"symbol": "CCCUSDT", "price_1h": -10, "oi_1h": 8},
        ], limit=2)

        self.assertEqual(selected[0], "BBBUSDT")
        self.assertEqual(len(selected), 2)

    def test_active_flow_requires_one_exact_closed_window(self) -> None:
        interval = 15 * 60 * 1000
        end = 4 * interval
        rows = [
            [index * interval, 1, 2, 0.5, 1.5, 1, (index + 1) * interval - 1,
             100, 1, 1, 60, 0]
            for index in range(4)
        ]

        result = active_flow_window(
            rows,
            interval_ms=interval,
            window_end_ms=end,
            periods=4,
        )

        self.assertEqual(result["status"], "available")
        self.assertAlmostEqual(result["ratio"], 0.2)

    def test_trade_plan_uses_closed_structure_target_for_real_rr(self) -> None:
        result = build_trade_plans({
            "timeframes": {
                "1h": {
                    "last_close": 100,
                    "atr": 4,
                    "reference_low": 98,
                    "reference_high": 110,
                },
            },
        })

        self.assertEqual(result["bullish"]["status"], "available")
        self.assertEqual(result["bearish"]["status"], "unavailable")
        self.assertEqual(result["bullish"]["targets"][0], 110)
        self.assertGreater(result["bullish"]["risk_reward_ratio"], 2.0)
        self.assertNotEqual(result["bullish"]["risk_reward_ratio"], 2.0)
        self.assertEqual(
            result["bullish"]["source"],
            "closed_1h_4h_structure_space",
        )
        self.assertLess(
            result["bullish"]["invalidation_price"],
            result["bullish"]["entry_zone"]["low"],
        )

    def test_bearish_plan_uses_closed_structure_target_for_real_rr(self) -> None:
        result = build_trade_plans({
            "timeframes": {
                "1h": {
                    "last_close": 100,
                    "atr": 4,
                    "reference_low": 90,
                    "reference_high": 102,
                },
            },
        })

        self.assertEqual(result["bullish"]["status"], "unavailable")
        self.assertEqual(result["bearish"]["status"], "available")
        self.assertEqual(result["bearish"]["targets"][0], 90)
        self.assertGreater(result["bearish"]["risk_reward_ratio"], 2.0)
        self.assertGreater(
            result["bearish"]["invalidation_price"],
            result["bearish"]["entry_zone"]["high"],
        )

    def test_trade_plan_can_use_four_hour_structure_target(self) -> None:
        result = build_trade_plans({
            "timeframes": {
                "1h": {
                    "last_close": 100,
                    "atr": 4,
                    "reference_low": 98,
                    "reference_high": 99,
                },
                "4h": {
                    "reference_low": 80,
                    "reference_high": 120,
                },
            },
        })

        self.assertEqual(result["bullish"]["status"], "available")
        self.assertEqual(result["bullish"]["targets"][0], 120)

    def test_trade_plan_rejects_structure_space_below_two_r(self) -> None:
        result = build_trade_plans({
            "timeframes": {
                "1h": {
                    "last_close": 100,
                    "atr": 4,
                    "reference_low": 90,
                    "reference_high": 110,
                },
            },
        })

        self.assertEqual(result["bullish"]["status"], "unavailable")
        self.assertEqual(result["bearish"]["status"], "unavailable")

    def test_trade_plan_rejects_missing_forward_structure_target(self) -> None:
        result = build_trade_plans({
            "timeframes": {
                "1h": {
                    "last_close": 100,
                    "atr": 4,
                    "reference_low": 90,
                    "reference_high": 99,
                },
            },
        })

        self.assertEqual(result["bullish"]["status"], "unavailable")
        self.assertIsNone(result["bullish"]["risk_reward_ratio"])

    def test_trade_plan_never_builds_negative_extension_target(self) -> None:
        result = build_trade_plans({
            "timeframes": {
                "1h": {
                    "last_close": 1,
                    "atr": 1,
                    "reference_low": 0.05,
                    "reference_high": 3,
                },
            },
        })

        self.assertEqual(result["bullish"]["status"], "available")
        self.assertTrue(all(value > 0 for value in result["bullish"]["targets"]))
        self.assertEqual(result["bearish"]["status"], "unavailable")
        self.assertIsNone(result["bearish"]["risk_reward_ratio"])
        self.assertEqual(result["bearish"]["targets"], [])

    def test_trade_plan_never_emits_non_positive_price(self) -> None:
        result = build_trade_plans({
            "timeframes": {
                "1h": {
                    "last_close": 0.1,
                    "atr": 2,
                    "reference_low": 0.01,
                    "reference_high": 1,
                },
            },
        })

        self.assertEqual(result["bullish"]["status"], "unavailable")
        self.assertEqual(result["bearish"]["status"], "unavailable")

    def test_directional_facts_include_all_timeframe_gates(self) -> None:
        multi = {
            "status": "ok",
            "role_groups": {
                "macro_direction": {"direction": "bullish"},
                "main_structure": {"direction": "bullish"},
                "confirmation": {"direction": "bullish"},
                "trigger": {"direction": "bullish"},
                "entry": {"direction": "bullish"},
            },
            "timeframes": {
                "2h": {"direction": "bullish"},
                "1h": {"direction": "bullish"},
                "4h": {"direction": "neutral"},
                "15m": {"direction": "bullish"},
                "5m": {"direction": "bullish"},
            },
        }
        facts = build_directional_facts(
            {
                "asset_subclass": "altcoin",
                "price_1h": 3.2,
                "oi_1h": 3.5,
                "funding_pct": 0.01,
                "funding_available": True,
                "basis_pct": 0.08,
                "liquidity_tier": "medium",
            },
            multi,
            spot_flow={"status": "available", "ratio": 0.14},
            futures_flow={"status": "available", "ratio": 0.12},
            trade_plans={
                "bullish": {"status": "available", "risk_reward_ratio": 2.0},
                "bearish": {"status": "unavailable", "risk_reward_ratio": 99},
            },
        )

        self.assertEqual(facts["macro_direction"], "bullish")
        self.assertEqual(facts["main_structure"], "bullish")
        self.assertEqual(facts["confirmation"], "bullish")
        self.assertEqual(facts["trigger"], "bullish")
        self.assertEqual(facts["entry"], "bullish")
        self.assertEqual(facts["timeframe_2h"], "bullish")
        self.assertEqual(facts["timeframe_15m"], "bullish")
        self.assertEqual(facts["timeframe_5m"], "bullish")
        self.assertEqual(facts["spot_cvd_status"], "available")
        self.assertEqual(facts["futures_cvd_status"], "available")
        self.assertTrue(facts["data_complete"])
        self.assertFalse(facts["observation_ready"])
        self.assertEqual(facts["bullish_risk_reward_ratio"], 2.0)
        self.assertIsNone(facts["bearish_risk_reward_ratio"])

    def test_spot_pair_not_listed_is_observation_ready_not_complete(self) -> None:
        multi = {
            "status": "ok",
            "role_groups": {
                key: {"direction": "bullish"}
                for key in (
                    "macro_direction",
                    "main_structure",
                    "confirmation",
                    "trigger",
                    "entry",
                )
            },
            "timeframes": {
                key: {"direction": "bullish"}
                for key in ("2h", "1h", "4h", "15m", "5m")
            },
        }
        facts = build_directional_facts(
            {
                "asset_subclass": "single_stock",
                "price_1h": 3.2,
                "oi_1h": 3.5,
                "funding_pct": 0.01,
                "funding_available": True,
                "basis_pct": 0.08,
                "liquidity_tier": "medium",
            },
            multi,
            spot_flow={"status": "spot_pair_not_listed", "ratio": None},
            futures_flow={"status": "available", "ratio": 0.12},
            trade_plans={
                "bullish": {"status": "available", "risk_reward_ratio": 2.4},
                "bearish": {"status": "unavailable", "risk_reward_ratio": None},
            },
        )

        self.assertEqual(facts["spot_cvd_status"], "spot_pair_not_listed")
        self.assertEqual(facts["futures_cvd_status"], "available")
        self.assertIsNone(facts["spot_cvd_ratio"])
        self.assertFalse(facts["data_complete"])
        self.assertTrue(facts["observation_ready"])


if __name__ == "__main__":
    unittest.main()
