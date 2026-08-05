from __future__ import annotations

import unittest

from radars.market_summary.quality import (
    DAY_MS,
    analyze_accumulation_quality,
)


NOW_MS = 1_800_000_000_000


def daily_rows(
    *,
    days: int = 52,
    price_fn=lambda _index: 100.0,
    volume_fn=lambda index: 30_000_000 if index >= 45 else 10_000_000,
) -> list[list[object]]:
    start = NOW_MS - days * DAY_MS
    rows: list[list[object]] = []
    for index in range(days):
        price = float(price_fn(index))
        open_time = start + index * DAY_MS
        rows.append([
            open_time,
            str(price),
            str(price * 1.05),
            str(price * 0.95),
            str(price),
            "1",
            open_time + DAY_MS - 1,
            str(volume_fn(index)),
        ])
    return rows


class AccumulationQualityTests(unittest.TestCase):
    def test_valid_sideways_low_volume_and_recent_volume_ratio(self) -> None:
        result = analyze_accumulation_quality(daily_rows(), now_ms=NOW_MS)
        self.assertTrue(result["eligible"])
        self.assertEqual(result["history_days"], 52)
        self.assertAlmostEqual(result["recent_volume_ratio"], 3.0)
        self.assertEqual(result["data_source"], "Binance USDⓈ-M Futures 已闭合1d K线")

    def test_insufficient_and_missing_rows(self) -> None:
        insufficient = analyze_accumulation_quality(
            daily_rows(days=30),
            now_ms=NOW_MS,
        )
        self.assertEqual(insufficient["exclusion_reason"], "insufficient_history")
        malformed = analyze_accumulation_quality(
            [[1, 2], ["bad"]],
            now_ms=NOW_MS,
        )
        self.assertEqual(malformed["history_days"], 0)

    def test_wide_range_and_long_trend_are_excluded(self) -> None:
        wide = daily_rows(price_fn=lambda index: 200.0 if index == 20 else 100.0)
        wide_result = analyze_accumulation_quality(wide, now_ms=NOW_MS)
        self.assertEqual(wide_result["exclusion_reason"], "range_too_wide")

        trending = daily_rows(price_fn=lambda index: 100.0 + index)
        trend_result = analyze_accumulation_quality(trending, now_ms=NOW_MS)
        self.assertEqual(trend_result["exclusion_reason"], "trend_too_strong")

    def test_high_baseline_volume_and_extended_recent_price_are_excluded(self) -> None:
        high_volume = analyze_accumulation_quality(
            daily_rows(volume_fn=lambda _index: 25_000_000),
            now_ms=NOW_MS,
        )
        self.assertEqual(high_volume["exclusion_reason"], "baseline_volume_too_high")

        extended = analyze_accumulation_quality(
            daily_rows(price_fn=lambda index: 410.0 if index >= 45 else 100.0),
            now_ms=NOW_MS,
        )
        self.assertEqual(
            extended["exclusion_reason"],
            "recent_price_already_extended",
        )

    def test_boundary_values_are_allowed(self) -> None:
        result = analyze_accumulation_quality(
            daily_rows(volume_fn=lambda _index: 20_000_000),
            now_ms=NOW_MS,
            max_avg_daily_quote_volume=20_000_000,
        )
        self.assertTrue(result["eligible"])

    def test_requires_45_day_baseline_plus_7_recent_days(self) -> None:
        rejected = analyze_accumulation_quality(
            daily_rows(days=51),
            now_ms=NOW_MS,
        )
        allowed = analyze_accumulation_quality(
            daily_rows(days=52),
            now_ms=NOW_MS,
        )
        self.assertEqual(rejected["exclusion_reason"], "insufficient_history")
        self.assertEqual(rejected["required_history_days"], 52)
        self.assertTrue(allowed["eligible"])
        self.assertEqual(allowed["sideways_days"], 45)
