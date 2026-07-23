from __future__ import annotations

import unittest
from datetime import datetime, timezone

from paopao_radar.binance_confirmation import apply_binance_confirmation
from paopao_radar.config import Settings
from paopao_radar.flow_radar import (
    coinglass_tv_url,
    binance_oi_stats,
    FlowRadarEngine,
    flow_category,
    fmt_cvd,
    kline_cvd_delta_info,
    kline_cvd_flow_info,
    series_delta_info,
)
from paopao_radar.time_windows import ClosedWindow, closed_window


class FlowRadarTests(unittest.TestCase):
    def test_coin_link_defaults_to_coinglass_binance_tv_page(self) -> None:
        self.assertEqual(
            coinglass_tv_url("BTC"),
            "https://www.coinglass.com/tv/zh/Binance_BTCUSDT",
        )

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

    def test_binance_oi_stats_uses_exact_closed_window(self) -> None:
        start_ms = 1_771_965_600_000
        end_ms = start_ms + 3_600_000

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
                    {"timestamp": start_ms - 3_600_000, "sumOpenInterestValue": "100"},
                    {"timestamp": start_ms, "sumOpenInterestValue": "110"},
                    {"timestamp": end_ms, "sumOpenInterestValue": "132"},
                ]

        window = ClosedWindow(
            start=datetime.fromtimestamp(start_ms / 1000, timezone.utc),
            end=datetime.fromtimestamp(end_ms / 1000, timezone.utc),
            interval_sec=3600,
            delay_sec=300,
        )
        source = Source()
        change, last, ready, points = binance_oi_stats(source, "BTCUSDT", window=window)

        self.assertEqual(source.args[3:], (start_ms, end_ms))
        self.assertTrue(ready)
        self.assertEqual(points, 2)
        self.assertEqual(last, 132.0)
        self.assertAlmostEqual(change, 20.0)

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

    def test_kline_cvd_uses_taker_buy_quote_volume(self) -> None:
        klines = [
            [
                1_771_965_600_000,
                "1",
                "1",
                "1",
                "1",
                "100",
                1_771_969_199_999,
                "1000",
                10,
                "55",
                "650",
                "0",
            ]
        ]

        delta, ready, points = kline_cvd_delta_info(klines)

        self.assertEqual(delta, 300.0)
        self.assertTrue(ready)
        self.assertEqual(points, 1)

        gross_delta, inflow, outflow, gross_ready, gross_points = kline_cvd_flow_info(klines)
        self.assertEqual(gross_delta, 300.0)
        self.assertEqual(inflow, 650.0)
        self.assertEqual(outflow, 350.0)
        self.assertTrue(gross_ready)
        self.assertEqual(gross_points, 1)

    def test_candidate_symbols_keeps_binance_funding_percent_once(self) -> None:
        class Source:
            def usdt_perp_symbols(self):
                return [{"symbol": "BTCUSDT"}]

            def premium_index(self):
                return [{"symbol": "BTCUSDT", "lastFundingRate": "0.0001"}]

            def ticker_24h(self):
                return [{"symbol": "BTCUSDT", "quoteVolume": "10000000", "priceChangePercent": "2"}]

        candidates = FlowRadarEngine(Settings(radar_min_quote_volume=1))._candidate_symbols(Source())

        self.assertEqual(candidates[0]["funding_pct"], 0.01)

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
        self.assertIn("Binance 主动成交数据缺失", reason)

    def test_missing_funding_is_not_treated_as_zero(self) -> None:
        category, score, reason = flow_category({
            "price_24h": 6.0,
            "oi_24h": 8.0,
            "spot_cvd_delta": 1_000_000,
            "futures_cvd_delta": 800_000,
            "funding_pct": 0.0,
            "funding_ready": False,
            "quote_volume": 80_000_000,
        })

        self.assertEqual(category, "数据不足")
        self.assertEqual(score, 0)
        self.assertIn("资金费率缺失", reason)

    def test_binance_confirmation_requires_every_declared_input(self) -> None:
        item: dict[str, object] = {}

        confirmation = apply_binance_confirmation(
            item,
            {"价格": True, "OI": True, "费率": False},
            scope="Binance USDⓈ-M Futures",
            window="1h闭合窗口",
            observed_at=1000,
        )

        self.assertEqual(confirmation["status"], "incomplete")
        self.assertEqual(confirmation["missing"], ["费率"])
        self.assertEqual(item["quality_gate"], "block")
        self.assertEqual(item["primary_data_source"], "binance_native")


if __name__ == "__main__":
    unittest.main()
