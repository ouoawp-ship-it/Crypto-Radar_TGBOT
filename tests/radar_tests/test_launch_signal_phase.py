from __future__ import annotations

import unittest

from radars.launch_warning.directional_model import evaluate_directional_readiness
from radars.launch_warning.signal_phase import (
    build_one_hour_phase_summary,
    classify_launch_phase,
)


HOUR_MS = 3_600_000


def _one_hour_rows(count: int = 72) -> list[list[object]]:
    rows: list[list[object]] = []
    for index in range(count):
        opened = index * HOUR_MS
        open_price = 100.0 + index
        close = open_price + 0.5
        quote_volume = 200.0 if index == count - 1 else 100.0
        rows.append([
            opened,
            open_price,
            close + 0.5,
            open_price - 0.5,
            close,
            1.0,
            opened + HOUR_MS - 1,
            quote_volume,
            1,
            0.5,
            quote_volume * 0.5,
        ])
    return rows


def _facts(*, bearish: bool = False, **overrides: object) -> dict[str, object]:
    direction = "bearish" if bearish else "bullish"
    values: dict[str, object] = {
        "asset_category": "altcoin",
        "price_change_pct": -3.2 if bearish else 3.2,
        "oi_change_pct": 3.5,
        "spot_cvd_ratio": -0.14 if bearish else 0.14,
        "futures_cvd_ratio": -0.12 if bearish else 0.12,
        "spot_cvd_gross_usd": 200_000.0,
        "spot_cvd_net_usd": -30_000.0 if bearish else 30_000.0,
        "futures_cvd_gross_usd": 400_000.0,
        "futures_cvd_net_usd": -60_000.0 if bearish else 60_000.0,
        "funding_rate_pct": -0.01 if bearish else 0.01,
        "basis_pct": -0.08 if bearish else 0.08,
        "structure": direction,
        "macro_direction": direction,
        "main_structure": direction,
        "confirmation": direction,
        "trigger": direction,
        "entry": direction,
        "timeframe_2h": direction,
        "timeframe_1h": direction,
        "timeframe_4h": "neutral",
        "timeframe_15m": direction,
        "timeframe_5m": direction,
        "liquidity_tier": "medium",
        f"{direction}_risk_reward_ratio": 2.5,
        "data_complete": True,
    }
    values.update(overrides)
    return values


def _summary(*, bearish: bool = False, **overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "status": "complete",
        "closed_only": True,
        "continuous": True,
        "closed_candles": 72,
        "range_low_72h": 10.0,
        "range_high_72h": 20.0,
        "last_close": 15.0,
        "atr": 1.0,
        "bullish_reference_price": 13.0,
        "bearish_reference_price": 17.0,
        "volume_ratio_1h": 1.5,
    }
    values.update(overrides)
    return values


class LaunchSignalPhaseTests(unittest.TestCase):
    def test_one_hour_summary_uses_exactly_72_closed_continuous_rows(self) -> None:
        rows = _one_hour_rows()
        rows.append([
            72 * HOUR_MS,
            172.0,
            173.0,
            171.5,
            172.5,
            1.0,
            73 * HOUR_MS - 1,
            999.0,
        ])

        result = build_one_hour_phase_summary(
            rows,
            window_end_ms=72 * HOUR_MS,
        )

        self.assertEqual(result["data_status"], "complete")
        self.assertTrue(result["closed_only"])
        self.assertTrue(result["continuous"])
        self.assertEqual(result["closed_candles"], 72)
        self.assertEqual(result["excluded_unclosed_candles"], 1)
        self.assertEqual(result["volume_ratio_1h"], 2.0)
        self.assertGreater(result["atr"], 0)

    def test_one_hour_summary_gap_and_invalid_row_fail_closed(self) -> None:
        rows = _one_hour_rows(73)
        gap = [row for index, row in enumerate(rows) if index != 30]
        invalid = _one_hour_rows()
        invalid[-2][2] = "bad"

        gap_result = build_one_hour_phase_summary(
            gap,
            window_end_ms=73 * HOUR_MS,
        )
        invalid_result = build_one_hour_phase_summary(
            invalid,
            window_end_ms=72 * HOUR_MS,
        )

        self.assertEqual(gap_result["data_status"], "gap")
        self.assertFalse(gap_result["continuous"])
        self.assertEqual(invalid_result["data_status"], "invalid")
        self.assertFalse(invalid_result["continuous"])

    def test_one_hour_summary_zero_volume_baseline_is_not_zero_filled(self) -> None:
        rows = _one_hour_rows()
        for row in rows:
            row[7] = 0.0

        result = build_one_hour_phase_summary(
            rows,
            window_end_ms=72 * HOUR_MS,
        )

        self.assertEqual(result["data_status"], "complete")
        self.assertIsNone(result["volume_ratio_1h"])

    def test_confirmed_bull_and_bear_are_mirrored_without_changing_score(self) -> None:
        for bearish in (False, True):
            with self.subTest(bearish=bearish):
                facts = _facts(bearish=bearish)
                signal = evaluate_directional_readiness(facts)
                score_key = "bearish_readiness" if bearish else "bullish_readiness"
                before = signal[score_key]

                result = classify_launch_phase(
                    facts,
                    _summary(bearish=bearish),
                    directional_signal=signal,
                )

                self.assertEqual(result["bias"], "bearish" if bearish else "bullish")
                self.assertEqual(result["timing_stage"], "confirmed")
                self.assertEqual(result["execution_status"], "retest_ready")
                self.assertTrue(result["initial_alert_eligible"])
                self.assertTrue(result["ai_eligible"])
                self.assertTrue(result["plan_eligible"])
                self.assertEqual(result["score_effect"], "none")
                self.assertEqual(result["directional_score"], before)
                self.assertEqual(signal[score_key], before)

    def test_tst_style_bullish_high_extension_is_no_chase(self) -> None:
        result = classify_launch_phase(
            _facts(),
            _summary(
                last_close=19.0,
                bullish_reference_price=17.0,
                atr=1.0,
            ),
        )

        self.assertEqual(result["bias"], "bullish")
        self.assertEqual(result["range_position_72h"], 0.9)
        self.assertEqual(result["extension_atr"], 2.0)
        self.assertEqual(result["position_status"], "high_extended")
        self.assertEqual(result["timing_stage"], "extended_no_chase")
        self.assertEqual(result["execution_status"], "blocked_extension")
        self.assertFalse(result["initial_alert_eligible"])
        self.assertFalse(result["ai_eligible"])
        self.assertFalse(result["plan_eligible"])
        self.assertIn("bullish_72h_high_extended", result["reason_codes"])

    def test_near_style_bearish_low_extension_is_no_chase(self) -> None:
        result = classify_launch_phase(
            _facts(bearish=True),
            _summary(
                bearish=True,
                last_close=11.0,
                bearish_reference_price=13.0,
                atr=1.0,
            ),
        )

        self.assertEqual(result["bias"], "bearish")
        self.assertEqual(result["range_position_72h"], 0.1)
        self.assertEqual(result["extension_atr"], 2.0)
        self.assertEqual(result["position_status"], "low_extended")
        self.assertEqual(result["timing_stage"], "extended_no_chase")
        self.assertFalse(result["initial_alert_eligible"])
        self.assertFalse(result["ai_eligible"])
        self.assertFalse(result["plan_eligible"])
        self.assertIn("bearish_72h_low_extended", result["reason_codes"])

    def test_range_and_atr_must_both_reach_extension_boundary(self) -> None:
        range_not_reached = classify_launch_phase(
            _facts(),
            _summary(
                last_close=18.99,
                bullish_reference_price=16.99,
                atr=1.0,
            ),
        )
        atr_not_reached = classify_launch_phase(
            _facts(),
            _summary(
                last_close=19.0,
                bullish_reference_price=17.01,
                atr=1.0,
            ),
        )

        self.assertNotEqual(range_not_reached["timing_stage"], "extended_no_chase")
        self.assertNotEqual(atr_not_reached["timing_stage"], "extended_no_chase")
        self.assertTrue(range_not_reached["plan_eligible"])
        self.assertTrue(atr_not_reached["plan_eligible"])

    def test_low_one_hour_volume_blocks_confirmation_without_flipping_bias(self) -> None:
        result = classify_launch_phase(
            _facts(),
            _summary(volume_ratio_1h=0.5),
        )

        self.assertEqual(result["bias"], "bullish")
        self.assertEqual(result["volume_status"], "low")
        self.assertEqual(result["timing_stage"], "forming")
        self.assertEqual(result["execution_status"], "blocked_volume")
        self.assertFalse(result["initial_alert_eligible"])
        self.assertFalse(result["plan_eligible"])

    def test_small_active_flow_blocks_confirmation_without_flipping_bias(self) -> None:
        result = classify_launch_phase(
            _facts(
                spot_cvd_gross_usd=100.0,
                spot_cvd_net_usd=20.0,
                futures_cvd_gross_usd=200.0,
                futures_cvd_net_usd=40.0,
            ),
            _summary(),
        )

        self.assertEqual(result["bias"], "bullish")
        self.assertEqual(result["active_flow_scale_status"], "low")
        self.assertEqual(result["execution_status"], "blocked_flow_scale")
        self.assertFalse(result["initial_alert_eligible"])
        self.assertFalse(result["plan_eligible"])

    def test_missing_or_gapped_position_data_fails_closed_for_promotion(self) -> None:
        for overrides in (
            {"closed_candles": 71},
            {"continuous": False},
            {"closed_only": False},
            {"status": "gap"},
            {"atr": None},
            {"bullish_reference_price": None},
        ):
            with self.subTest(overrides=overrides):
                result = classify_launch_phase(
                    _facts(),
                    _summary(**overrides),
                )

                self.assertEqual(result["bias"], "bullish")
                self.assertEqual(result["position_status"], "insufficient")
                self.assertEqual(result["timing_stage"], "insufficient")
                self.assertEqual(result["execution_status"], "blocked_data")
                self.assertFalse(result["initial_alert_eligible"])
                self.assertFalse(result["plan_eligible"])

    def test_oi_release_mechanism_cannot_be_promoted_to_new_positioning(self) -> None:
        facts = _facts(oi_change_pct=-3.5)
        result = classify_launch_phase(facts, _summary())

        self.assertEqual(result["bias"], "bullish")
        self.assertEqual(result["mechanism"], "short_covering")
        self.assertEqual(result["timing_stage"], "mechanism_watch")
        self.assertEqual(result["execution_status"], "wait_new_positioning")
        self.assertFalse(result["initial_alert_eligible"])
        self.assertFalse(result["plan_eligible"])


if __name__ == "__main__":
    unittest.main()
