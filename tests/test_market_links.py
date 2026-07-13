from __future__ import annotations

import unittest

from paopao_radar.market_links import (
    binance_usdt_symbol,
    coinglass_tv_url,
    telegram_coin_links,
    tradingview_tv_url,
)


class MarketLinksTests(unittest.TestCase):
    def test_normalizes_base_coin_and_pair(self) -> None:
        self.assertEqual(binance_usdt_symbol(" btc "), "BTCUSDT")
        self.assertEqual(binance_usdt_symbol("btcusdt"), "BTCUSDT")

    def test_keeps_coinglass_link(self) -> None:
        self.assertEqual(
            coinglass_tv_url("BTC"),
            "https://www.coinglass.com/tv/zh/Binance_BTCUSDT",
        )

    def test_builds_direct_tradingview_link(self) -> None:
        self.assertEqual(
            tradingview_tv_url("BTCUSDT"),
            "https://www.tradingview.com/chart/?symbol=BINANCE%3ABTCUSDT.P",
        )

    def test_telegram_links_include_copyable_pair_and_both_charts(self) -> None:
        links = telegram_coin_links("BTC")

        self.assertIn('href="https://www.coinglass.com/tv/zh/Binance_BTCUSDT"', links)
        self.assertIn("<b>BTC</b>", links)
        self.assertIn("📋 <code>BTCUSDT</code>", links)
        self.assertIn('href="https://www.tradingview.com/chart/?symbol=BINANCE%3ABTCUSDT.P"', links)
        self.assertIn("<b>TV</b>", links)


if __name__ == "__main__":
    unittest.main()
