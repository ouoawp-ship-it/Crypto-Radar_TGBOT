from __future__ import annotations

import unittest

from paopao_radar.flow_radar import (
    binance_oi_stats,
    coinglass_tv_url,
    flow_category,
    fmt_cvd,
    market_by_symbol,
    series_delta_info,
)
from paopao_radar.time_windows import closed_window


class FlowRadarTests(unittest.TestCase):
    def test_coinglass_link_defaults_to_chinese_tv_page(self) -> None:
        self.assertEqual(
            coinglass_tv_url("BTC"),
            "https://www.coinglass.com/tv/zh/Binance_BTCUSDT",
        )

    def test_market_map_accepts_coin_symbols(self) -> None:
        data = {"data": [{"symbol": "BTC", "open_interest_usd": 1}, {"baseAsset": "ETHUSDT"}]}

        mapped = market_by_symbol(data)

        self.assertIn("BTC", mapped)
        self.assertIn("ETH", mapped)

    def test_series_delta_reports_missing_data(self) -> None:
        delta, ready, count = series_delta_info({"data": [{"cvd": 100}]})

        self.assertEqual(delta, 0.0)
        self.assertFalse(ready)
        self.assertEqual(count, 1)

    def test_binance_oi_stats_calculates_fallback_change(self) -> None:
        class Source:
            def open_interest_hist(
                self,
                symbol: str,
                period: str = "1h",
                limit: int = 25,
                start_time: int | None = None,
                end_time: int | None = None,
            ):
                self.args = (symbol, period, limit, start_time, end_time)
                return [
                    {"sumOpenInterestValue": "100"},
                    {"sumOpenInterestValue": "115"},
                ]

        source = Source()
        pct, last, ready, points = binance_oi_stats(source, "BTCUSDT")

        self.assertEqual(source.args, ("BTCUSDT", "1h", 25, None, None))
        self.assertEqual(pct, 15.0)
        self.assertEqual(last, 115.0)
        self.assertTrue(ready)
        self.assertEqual(points, 2)

    def test_series_delta_filters_to_closed_window_timestamps(self) -> None:
        data = {
            "data": [
                {"time": 1_771_965_600_000, "cvd": 10},
                {"time": 1_771_969_200_000, "cvd": 30},
                {"time": 1_771_972_800_000, "cvd": 99},
            ]
        }

        delta, ready, points = series_delta_info(
            data,
            start_ms=1_771_965_600_000,
            end_ms=1_771_969_200_000,
        )

        self.assertEqual(delta, 20.0)
        self.assertTrue(ready)
        self.assertEqual(points, 2)

    def test_closed_window_waits_for_delay_before_using_latest_hour(self) -> None:
        from datetime import datetime, timedelta, timezone

        window = closed_window(
            now=datetime(2026, 5, 26, 18, 4, 0, tzinfo=timezone(timedelta(hours=8))),
            interval_sec=3600,
            delay_sec=300,
        )

        self.assertEqual(window.label(), "05-26 16:00-17:00 CST")

    def test_true_launch_category_scores_multi_factor_confirmation(self) -> None:
        category, score, _reason = flow_category({
            "price_24h": 6.0,
            "oi_24h": 8.0,
            "spot_cvd_delta": 1_000_000,
            "futures_cvd_delta": 800_000,
            "funding_pct": 0.02,
            "quote_volume": 80_000_000,
        })

        self.assertEqual(category, "真启动候选")
        self.assertGreaterEqual(score, 90)

    def test_neutral_cvd_does_not_trigger_distribution(self) -> None:
        category, _score, _reason = flow_category({
            "price_24h": -1.0,
            "oi_24h": -1.0,
            "spot_cvd_delta": 0.0,
            "futures_cvd_delta": 0.0,
            "spot_cvd_ready": True,
            "futures_cvd_ready": True,
            "funding_pct": 0.2,
            "quote_volume": 100_000_000,
        })

        self.assertNotEqual(category, "诱多/派发")

    def test_fmt_cvd_distinguishes_missing_neutral_and_signed_values(self) -> None:
        self.assertEqual(fmt_cvd(0.0, True), "近0")
        self.assertEqual(fmt_cvd(0.25, True), "近0")
        self.assertEqual(fmt_cvd(1_250_000, True), "+$1.2M")
        self.assertEqual(fmt_cvd(-2_500, True), "-$2.5K")
        self.assertEqual(fmt_cvd(0.0, False), "缺失")

    def test_missing_cvd_does_not_create_fake_distribution_signal(self) -> None:
        category, score, reason = flow_category({
            "price_24h": 20.0,
            "oi_24h": 0.0,
            "spot_cvd_delta": 0.0,
            "futures_cvd_delta": 0.0,
            "spot_cvd_ready": False,
            "futures_cvd_ready": False,
            "funding_pct": 0.1,
            "quote_volume": 100_000_000,
        })

        self.assertEqual(category, "数据不足")
        self.assertEqual(score, 0)
        self.assertIn("CVD 数据缺失", reason)


if __name__ == "__main__":
    unittest.main()
