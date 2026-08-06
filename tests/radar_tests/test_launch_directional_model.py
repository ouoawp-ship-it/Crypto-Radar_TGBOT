from __future__ import annotations

import unittest

from radars.launch_warning.directional_model import (
    SCORE_SEMANTICS,
    evaluate_directional_readiness,
)


def _facts(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "asset_category": "altcoin",
        "price_change_pct": 3.2,
        "oi_change_pct": 3.5,
        "spot_cvd_ratio": 0.14,
        "futures_cvd_ratio": 0.12,
        "funding_rate_pct": 0.01,
        "basis_pct": 0.08,
        "structure": "bullish",
        "macro_direction": "bullish",
        "main_structure": "bullish",
        "confirmation": "bullish",
        "trigger": "bullish",
        "entry": "bullish",
        "timeframe_2h": "bullish",
        "timeframe_1h": "bullish",
        "timeframe_4h": "neutral",
        "timeframe_15m": "bullish",
        "timeframe_5m": "bullish",
        "liquidity_tier": "medium",
        "risk_reward_ratio": 2.5,
        "data_complete": True,
    }
    values.update(overrides)
    return values


def _bearish_facts(**overrides: object) -> dict[str, object]:
    values = _facts(
        price_change_pct=-3.2,
        oi_change_pct=3.5,
        spot_cvd_ratio=-0.14,
        futures_cvd_ratio=-0.12,
        structure="bearish",
        macro_direction="bearish",
        main_structure="bearish",
        confirmation="bearish",
        trigger="bearish",
        entry="bearish",
        timeframe_2h="bearish",
        timeframe_1h="bearish",
        timeframe_4h="neutral",
        timeframe_15m="bearish",
        timeframe_5m="bearish",
        funding_rate_pct=-0.01,
        basis_pct=-0.08,
    )
    values.update(overrides)
    return values


class LaunchDirectionalModelTests(unittest.TestCase):
    def test_complete_bullish_setup_is_confirmed(self) -> None:
        result = evaluate_directional_readiness(_facts())

        self.assertEqual(result["status"], "多头确认")
        self.assertEqual(result["direction"], "bullish")
        self.assertTrue(result["hard_gates"]["bullish_passed"])
        self.assertEqual(result["score_semantics"], SCORE_SEMANTICS)

    def test_production_chinese_liquidity_tier_is_accepted(self) -> None:
        result = evaluate_directional_readiness(_facts(
            liquidity_tier="中流动性",
        ))

        self.assertTrue(result["hard_gates"]["bullish"]["liquidity"])

    def test_direction_specific_risk_reward_is_not_borrowed(self) -> None:
        result = evaluate_directional_readiness(_facts(
            risk_reward_ratio=None,
            bullish_risk_reward_ratio=2.5,
            bearish_risk_reward_ratio=None,
        ))

        self.assertTrue(result["hard_gates"]["bullish"]["risk_reward"])
        self.assertFalse(result["hard_gates"]["bearish"]["risk_reward"])

    def test_missing_structure_plan_blocks_gate_without_marking_facts_incomplete(self) -> None:
        result = evaluate_directional_readiness(_facts(
            risk_reward_ratio=None,
            bullish_risk_reward_ratio=None,
            bearish_risk_reward_ratio=None,
        ))

        self.assertTrue(result["data_complete"])
        self.assertFalse(result["hard_gates"]["bullish"]["risk_reward"])
        self.assertFalse(result["hard_gates"]["bearish"]["risk_reward"])
        self.assertNotEqual(result["status"], "多头确认")

    def test_complete_bearish_setup_is_confirmed(self) -> None:
        result = evaluate_directional_readiness(_bearish_facts())

        self.assertEqual(result["status"], "空头确认")
        self.assertEqual(result["direction"], "bearish")
        self.assertTrue(result["hard_gates"]["bearish_passed"])

    def test_one_hour_confirmation_is_a_gate_not_an_extra_score(self) -> None:
        confirmed = evaluate_directional_readiness(_facts())
        waiting = evaluate_directional_readiness(_facts(timeframe_1h="neutral"))

        self.assertEqual(
            confirmed["bullish_group_scores"], waiting["bullish_group_scores"]
        )
        self.assertEqual(waiting["status"], "多头候选")
        self.assertFalse(waiting["hard_gates"]["bullish_passed"])

    def test_four_hour_opposition_blocks_confirmation(self) -> None:
        result = evaluate_directional_readiness(_facts(timeframe_4h="bearish"))

        self.assertEqual(result["status"], "多头候选")
        self.assertFalse(
            result["hard_gates"]["bullish"]["four_hour_not_opposed"]
        )

    def test_bearish_setup_without_one_hour_confirmation_is_candidate(self) -> None:
        result = evaluate_directional_readiness(_bearish_facts(
            timeframe_1h="neutral",
        ))

        self.assertEqual(result["status"], "空头候选")
        self.assertFalse(result["hard_gates"]["bearish_passed"])

    def test_low_liquidity_and_low_rr_block_confirmation(self) -> None:
        result = evaluate_directional_readiness(_facts(
            liquidity_tier="low",
            risk_reward_ratio=1.8,
        ))

        self.assertEqual(result["status"], "多头候选")
        self.assertFalse(result["hard_gates"]["bullish"]["liquidity"])
        self.assertFalse(result["hard_gates"]["bullish"]["risk_reward"])

    def test_positive_funding_and_basis_only_subtract_bullish_risk(self) -> None:
        ordinary = evaluate_directional_readiness(_facts())
        crowded = evaluate_directional_readiness(_facts(
            funding_rate_pct=0.12,
            basis_pct=0.6,
        ))

        self.assertEqual(crowded["status"], "杠杆过热")
        self.assertLess(crowded["bullish_readiness"], ordinary["bullish_readiness"])
        self.assertLess(crowded["risk_adjustments"]["bullish"], 0)
        self.assertEqual(crowded["risk_adjustments"]["bearish"], 0)

    def test_negative_funding_is_not_a_bullish_bonus(self) -> None:
        neutral = evaluate_directional_readiness(_facts(
            funding_rate_pct=0.0,
            basis_pct=0.0,
        ))
        negative = evaluate_directional_readiness(_facts(
            funding_rate_pct=-0.12,
            basis_pct=-0.6,
        ))

        self.assertEqual(
            negative["bullish_readiness"], neutral["bullish_readiness"]
        )
        self.assertLess(negative["risk_adjustments"]["bearish"], 0)

    def test_price_up_and_oi_down_is_short_covering_not_buy_confirmation(self) -> None:
        result = evaluate_directional_readiness(_facts(oi_change_pct=-3.5))

        self.assertEqual(result["status"], "挤空反弹")
        self.assertEqual(result["direction"], "bullish_rebound_only")
        self.assertLess(
            result["bullish_group_scores"]["price_oi_participation"], 30
        )

    def test_price_down_and_oi_down_is_long_liquidation(self) -> None:
        result = evaluate_directional_readiness(_bearish_facts(
            oi_change_pct=-3.5,
        ))

        self.assertEqual(result["status"], "多头踩踏")
        self.assertEqual(result["direction"], "bearish_deleveraging_only")

    def test_quiet_price_oi_build_with_spot_buying_is_accumulation(self) -> None:
        result = evaluate_directional_readiness(_facts(
            price_change_pct=0.3,
            oi_change_pct=3.5,
            structure="neutral",
            timeframe_1h="neutral",
        ))

        self.assertEqual(result["status"], "潜伏积累")
        self.assertEqual(result["direction"], "bullish_candidate")

    def test_opposing_futures_flow_blocks_accumulation_label(self) -> None:
        result = evaluate_directional_readiness(_facts(
            price_change_pct=0.3,
            oi_change_pct=3.5,
            spot_cvd_ratio=0.14,
            futures_cvd_ratio=-0.14,
            structure="neutral",
            timeframe_1h="neutral",
        ))

        self.assertEqual(result["status"], "冲突等待")

    def test_spot_selling_with_bearish_structure_is_distribution_risk(self) -> None:
        result = evaluate_directional_readiness(_facts(
            price_change_pct=0.3,
            oi_change_pct=3.5,
            spot_cvd_ratio=-0.14,
            futures_cvd_ratio=0.02,
            structure="bearish",
            timeframe_1h="neutral",
        ))

        self.assertEqual(result["status"], "派发风险")
        self.assertEqual(result["direction"], "bearish_candidate")

    def test_opposing_price_and_both_flows_is_visible_divergence_watch(self) -> None:
        result = evaluate_directional_readiness(_facts(
            price_change_pct=3.2,
            oi_change_pct=3.5,
            spot_cvd_ratio=-0.14,
            futures_cvd_ratio=-0.12,
            structure="neutral",
            timeframe_1h="neutral",
        ))

        self.assertEqual(result["status"], "假强背离")
        self.assertEqual(result["direction"], "bearish_divergence_watch")

    def test_price_and_oi_rise_with_both_cvds_falling_is_fake_strength(self) -> None:
        result = evaluate_directional_readiness(_facts(
            spot_cvd_ratio=-0.14,
            futures_cvd_ratio=-0.12,
        ))

        self.assertEqual(result["status"], "假强背离")
        self.assertEqual(result["direction"], "bearish_divergence_watch")
        self.assertEqual(result["divergence_status"], "假强背离")
        self.assertEqual(
            result["participation_pattern"], "fake_strength_divergence"
        )
        self.assertEqual(
            result["divergence_evidence"],
            ["spot_and_futures_cvd_oppose_price_rise"],
        )
        self.assertFalse(result["hard_gates"]["bullish"]["spot_cvd_aligned"])
        self.assertFalse(
            result["hard_gates"]["bullish"]["futures_cvd_aligned"]
        )
        self.assertLess(result["bullish_readiness"], 70)
        self.assertLess(result["bearish_readiness"], 70)
        self.assertEqual(
            result["divergence_semantics"],
            "risk_watch_not_confirmed_reversal",
        )

    def test_price_falls_with_oi_rising_and_both_cvds_buying_is_fake_weakness(self) -> None:
        result = evaluate_directional_readiness(_bearish_facts(
            spot_cvd_ratio=0.14,
            futures_cvd_ratio=0.12,
        ))

        self.assertEqual(result["status"], "假弱背离")
        self.assertEqual(result["direction"], "bullish_divergence_watch")
        self.assertEqual(result["divergence_status"], "假弱背离")
        self.assertEqual(
            result["participation_pattern"], "fake_weakness_divergence"
        )
        self.assertEqual(
            result["divergence_evidence"],
            ["spot_and_futures_cvd_oppose_price_decline"],
        )
        self.assertLess(result["bullish_readiness"], 70)
        self.assertLess(result["bearish_readiness"], 70)

    def test_futures_only_contract_is_degraded_observation_candidate(self) -> None:
        result = evaluate_directional_readiness(_facts(
            spot_cvd_ratio=None,
            spot_cvd_status="spot_pair_not_listed",
            futures_cvd_status="available",
            data_complete=False,
            observation_ready=True,
        ))

        self.assertEqual(result["status"], "多头候选")
        self.assertEqual(result["direction"], "bullish_candidate")
        self.assertFalse(result["data_complete"])
        self.assertTrue(result["observation_ready"])
        self.assertEqual(
            result["observation_mode"],
            "futures_only_spot_pair_not_listed",
        )
        self.assertFalse(result["hard_gates"]["bullish"]["complete_data"])
        self.assertFalse(result["hard_gates"]["bullish"]["spot_cvd_aligned"])
        self.assertFalse(result["hard_gates"]["bullish_passed"])

    def test_futures_only_contract_without_aligned_futures_cvd_waits(self) -> None:
        result = evaluate_directional_readiness(_facts(
            spot_cvd_ratio=None,
            futures_cvd_ratio=-0.12,
            spot_cvd_status="spot_pair_not_listed",
            futures_cvd_status="available",
            data_complete=False,
            observation_ready=True,
        ))

        self.assertEqual(result["status"], "冲突等待")
        self.assertEqual(result["direction"], "none")

    def test_futures_only_contract_can_be_bearish_observation_candidate(self) -> None:
        result = evaluate_directional_readiness(_bearish_facts(
            spot_cvd_ratio=None,
            spot_cvd_status="spot_pair_not_listed",
            futures_cvd_status="available",
            data_complete=False,
            observation_ready=True,
        ))

        self.assertEqual(result["status"], "空头候选")
        self.assertEqual(result["direction"], "bearish_candidate")
        self.assertTrue(result["observation_ready"])
        self.assertFalse(result["hard_gates"]["bearish"]["complete_data"])
        self.assertFalse(result["hard_gates"]["bearish"]["spot_cvd_aligned"])
        self.assertFalse(result["hard_gates"]["bearish_passed"])

    def test_confirmation_requires_spot_and_futures_cvd_alignment(self) -> None:
        result = evaluate_directional_readiness(_facts(futures_cvd_ratio=0.02))

        self.assertNotEqual(result["status"], "多头确认")
        self.assertFalse(result["hard_gates"]["bullish_passed"])
        self.assertFalse(
            result["hard_gates"]["bullish"]["futures_cvd_aligned"]
        )

    def test_each_timeframe_layer_participates_in_confirmation_gate(self) -> None:
        cases = {
            "macro_direction": "macro_direction_aligned",
            "main_structure": "main_structure_aligned",
            "confirmation": "confirmation_group_aligned",
            "timeframe_2h": "confirmed_2h",
            "timeframe_1h": "confirmed_1h",
            "trigger": "trigger_15m_aligned",
            "timeframe_15m": "trigger_15m_aligned",
            "entry": "entry_5m_aligned",
            "timeframe_5m": "entry_5m_aligned",
        }
        for fact_key, gate_key in cases.items():
            with self.subTest(fact_key=fact_key):
                result = evaluate_directional_readiness(_facts(**{
                    fact_key: "neutral",
                }))

                self.assertNotEqual(result["status"], "多头确认")
                self.assertFalse(result["hard_gates"]["bullish"][gate_key])

    def test_mixed_timeframe_is_conflict_not_neutral_pass(self) -> None:
        result = evaluate_directional_readiness(_facts(macro_direction="mixed"))

        self.assertEqual(result["status"], "冲突等待")
        self.assertEqual(result["direction"], "none")
        self.assertIn("macro_direction", result["timeframe_conflicts"])

    def test_incomplete_or_missing_data_cannot_be_confirmed(self) -> None:
        incomplete = evaluate_directional_readiness(_facts(data_complete=False))
        missing = evaluate_directional_readiness(_facts(basis_pct=None))

        self.assertEqual(incomplete["status"], "数据不足")
        self.assertEqual(missing["status"], "数据不足")
        self.assertIn("basis_pct", missing["missing_fields"])
        self.assertFalse(missing["hard_gates"]["bullish_passed"])

    def test_each_family_is_capped_and_total_never_exceeds_100(self) -> None:
        result = evaluate_directional_readiness(_facts(
            price_change_pct=100,
            oi_change_pct=100,
            spot_cvd_ratio=1,
            futures_cvd_ratio=1,
            risk_reward_ratio=10,
        ))

        for key, cap in result["group_caps"].items():
            self.assertLessEqual(result["bullish_group_scores"][key], cap)
        self.assertLessEqual(result["bullish_raw_score"], 100)
        self.assertLessEqual(result["bullish_readiness"], 100)

    def test_asset_category_changes_thresholds_not_score_semantics(self) -> None:
        core = evaluate_directional_readiness(_facts(
            asset_category="core_crypto",
            price_change_pct=1.2,
            oi_change_pct=1.8,
            spot_cvd_ratio=0.06,
            futures_cvd_ratio=0.06,
        ))
        unknown = evaluate_directional_readiness(_facts(
            asset_category="unknown",
            price_change_pct=1.2,
            oi_change_pct=1.8,
            spot_cvd_ratio=0.06,
            futures_cvd_ratio=0.06,
        ))

        self.assertGreater(
            core["bullish_readiness"], unknown["bullish_readiness"]
        )
        self.assertEqual(core["score_semantics"], SCORE_SEMANTICS)
        self.assertEqual(unknown["score_semantics"], SCORE_SEMANTICS)

    def test_function_is_deterministic_and_does_not_mutate_input(self) -> None:
        source = _facts()
        before = dict(source)

        first = evaluate_directional_readiness(source)
        second = evaluate_directional_readiness(source)

        self.assertEqual(first, second)
        self.assertEqual(source, before)


if __name__ == "__main__":
    unittest.main()
