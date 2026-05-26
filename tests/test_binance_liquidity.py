from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.binance_liquidity import BinanceOrderbookLiquidityProvider
from paopao_radar.config import Settings


class FakeBinanceSource:
    def __init__(self, payload):
        self.payload = payload

    def order_book(self, symbol, limit=100):
        return self.payload

    def diagnostics(self):
        return {"quality": {"successes": {"depth": 1}, "failures": {}}}


class BinanceLiquidityTests(unittest.TestCase):
    def test_orderbook_snapshot_generates_walls(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                liquidity_fallback_enable=True,
                binance_orderbook_liquidity_enable=True,
                coinglass_liquidity_min_distance_pct=0.1,
                coinglass_liquidity_max_distance_pct=5,
            )
            source = FakeBinanceSource({
                "asks": [["101", "10"], ["104", "1"]],
                "bids": [["99", "20"], ["95", "1"]],
            })

            context = BinanceOrderbookLiquidityProvider(settings, source).context("BTCUSDT", 100)

        self.assertTrue(context.available)
        self.assertEqual(context.source, "BinanceOrderBook")
        self.assertEqual(context.upper_liquidity_wall, "$101")
        self.assertEqual(context.lower_liquidity_wall, "$99")
        self.assertEqual(context.orderbook_bias, "up")

    def test_disabled_fallback_returns_unavailable(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), liquidity_fallback_enable=False)
            context = BinanceOrderbookLiquidityProvider(settings, FakeBinanceSource({})).context("BTCUSDT", 100)

        self.assertFalse(context.available)
        self.assertEqual(context.source, "BinanceOrderBook")


if __name__ == "__main__":
    unittest.main()
