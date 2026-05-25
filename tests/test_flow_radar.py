from __future__ import annotations

import unittest

from paopao_radar.flow_radar import coinglass_tv_url, flow_category, market_by_symbol


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


if __name__ == "__main__":
    unittest.main()
