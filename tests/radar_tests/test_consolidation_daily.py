from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
import unittest

from radars.consolidation_breakout.daily import (
    DAILY_HORIZONS,
    DailyCandle,
    detect_daily_boxes,
    select_daily_candidate,
)


DAY_MS = 86_400_000
BASE_MS = 1_700_000_000_000


def range_candles(count: int) -> list[DailyCandle]:
    candles: list[DailyCandle] = []
    for index in range(count):
        phase = index % 4
        candles.append(DailyCandle(
            open=100.0,
            high=103.0 if phase == 0 else 101.0,
            low=97.0 if phase == 2 else 99.0,
            close=99.8 if index % 2 == 0 else 100.2,
            volume=100.0,
            close_time=BASE_MS + (index + 1) * DAY_MS - 1,
        ))
    return candles


class DailyConsolidationDetectorTests(unittest.TestCase):
    def test_trimmed_boundaries_ignore_isolated_extreme_wicks(self) -> None:
        candles = range_candles(60)
        candles[10] = replace(candles[10], high=140.0)
        candles[11] = replace(candles[11], low=60.0)

        candidate, diagnostics = select_daily_candidate(
            candles,
            DAILY_HORIZONS[0],
        )

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate["base_bars"], 50)
        self.assertEqual(candidate["upper"], 103.0)
        self.assertEqual(candidate["lower"], 97.0)
        self.assertGreaterEqual(candidate["candle_coverage"], 0.90)
        self.assertEqual(candidate["boundary_method"], "trimmed_wick_5pct_v1")
        self.assertIn(candidate["quality_label"], {"strong", "standard", "watch"})
        self.assertTrue(candidate["quality_reasons"])
        self.assertEqual(diagnostics["status"], "accepted")

    def test_candle_coverage_rejection_is_reported_per_reason(self) -> None:
        # The default 90% floor is the mathematical lower bound of two-sided
        # 5% trimming. A stricter profile proves that coverage is independently
        # enforced and reported rather than being folded into width rejection.
        strict = replace(
            DAILY_HORIZONS[0],
            anchors=(40,),
            min_candle_coverage=0.96,
        )
        candles = range_candles(43)
        candles[10] = replace(candles[10], high=140.0)
        candles[11] = replace(candles[11], low=60.0)

        candidate, diagnostics = select_daily_candidate(candles, strict)

        self.assertIsNone(candidate)
        self.assertEqual(diagnostics["reason_counts"]["candle_coverage"], 1)
        evaluation = diagnostics["evaluations"][0]
        self.assertAlmostEqual(evaluation["metrics"]["candle_coverage"], 0.95)
        self.assertIn("candle_coverage", evaluation["reasons"])

    def test_43_days_selects_40_day_short_anchor(self) -> None:
        candidate, diagnostics = select_daily_candidate(
            range_candles(43),
            DAILY_HORIZONS[0],
        )

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate["base_bars"], 40)
        self.assertEqual(diagnostics["selected_length"], 40)
        self.assertIn(40, diagnostics["accepted_lengths"])
        self.assertEqual(diagnostics["reason_counts"]["insufficient_history"], 1)

    def test_130_days_selects_120_day_medium_anchor(self) -> None:
        candidate, diagnostics = select_daily_candidate(
            range_candles(130),
            DAILY_HORIZONS[1],
        )

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate["base_bars"], 120)
        self.assertEqual(diagnostics["selected_length"], 120)

    def test_430_days_selects_420_day_long_anchor(self) -> None:
        candidate, diagnostics = select_daily_candidate(
            range_candles(430),
            DAILY_HORIZONS[2],
        )

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate["base_bars"], 420)
        self.assertEqual(diagnostics["selected_length"], 420)

    def test_missing_separated_touches_rejects_candidate(self) -> None:
        candles = [
            DailyCandle(
                open=100.0,
                high=101.0,
                low=97.0 if index % 4 == 2 else 99.0,
                close=99.8 if index % 2 == 0 else 100.2,
                close_time=BASE_MS + (index + 1) * DAY_MS - 1,
            )
            for index in range(43)
        ]
        candidate, diagnostics = select_daily_candidate(
            candles,
            replace(DAILY_HORIZONS[0], anchors=(40,)),
        )

        self.assertIsNone(candidate)
        self.assertEqual(diagnostics["reason_counts"]["upper_touches"], 1)
        self.assertNotIn("lower_touches", diagnostics["reason_counts"])

    def test_insufficient_history_is_not_reported_as_a_bad_structure(self) -> None:
        result = detect_daily_boxes(range_candles(19))

        self.assertTrue(all(box is None for box in result["boxes"].values()))
        for diagnostics in result["diagnostics"]["horizons"].values():
            self.assertEqual(diagnostics["status"], "insufficient_history")
            self.assertEqual(
                set(diagnostics["reason_counts"]),
                {"insufficient_history"},
            )

    def test_existing_candle_duck_type_is_accepted(self) -> None:
        ducks = [
            SimpleNamespace(
                open=candle.open,
                high=candle.high,
                low=candle.low,
                close=candle.close,
                volume=candle.volume,
                close_time=candle.close_time,
            )
            for candle in range_candles(43)
        ]

        candidate, _diagnostics = select_daily_candidate(
            ducks,
            DAILY_HORIZONS[0],
        )

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(candidate["base_bars"], 40)


if __name__ == "__main__":
    unittest.main()
