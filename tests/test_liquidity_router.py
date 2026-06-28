from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.config import Settings
from paopao_radar.liquidity_context import LiquidityContext
from paopao_radar.liquidity_router import MultiSourceLiquidityAnalyzer, merge_liquidity_contexts
from paopao_radar.structure_radar import SIGNAL_PRE_BREAKOUT_NEAR, StructureSignal


class StaticProvider:
    def __init__(self, context: LiquidityContext):
        self._context = context

    def context(self, symbol: str, price: float) -> LiquidityContext:
        return self._context

    def diagnostics(self):
        return {"enabled": True}


def make_signal() -> StructureSignal:
    return StructureSignal(
        symbol="TESTUSDT",
        interval="15m",
        signal_type=SIGNAL_PRE_BREAKOUT_NEAR,
        level="A",
        score=70,
        price=100,
        box_high=102,
        box_low=95,
        box_width_pct=7,
        position_in_box=80,
        distance_to_high_pct=1.0,
        distance_to_low_pct=5.0,
        touch_high_count=3,
        touch_low_count=3,
        atr_pct=1.0,
        atr_compressed=True,
        bb_width_pct=3.0,
        bb_compressed=True,
        volume_ratio=1.5,
        oi_change_pct_1h=4,
        oi_change_pct_4h=8,
        taker_buy_ratio=0.58,
        reason_lines=[],
        base_score=70,
        final_score=70,
    )


class LiquidityRouterTests(unittest.TestCase):
    def test_merges_liquidation_and_orderbook_context(self) -> None:
        base = LiquidityContext(
            symbol="TESTUSDT",
            available=False,
            source="MultiSource",
        )
        liquidation = LiquidityContext(
            symbol="TESTUSDT",
            available=True,
            source="CoinalyzeHistory",
            upper_liquidation_zone="$104",
            nearest_liquidation_above_pct=4,
            liquidation_bias="up",
        )
        orderbook = LiquidityContext(
            symbol="TESTUSDT",
            available=True,
            source="BinanceOrderBook",
            upper_liquidity_wall="$101",
            upper_wall_distance_pct=1,
            orderbook_bias="down",
        )

        merged = merge_liquidity_contexts(base, liquidation, orderbook)

        self.assertEqual(merged.source, "CoinalyzeHistory+BinanceOrderBook")
        self.assertEqual(merged.upper_liquidity_wall, "$101")
        self.assertEqual(merged.upper_liquidation_zone, "$104")

    def test_uses_binance_orderbook_when_only_orderbook_is_available(self) -> None:
        base = LiquidityContext(symbol="TESTUSDT", available=False, source="MultiSource")
        orderbook = LiquidityContext(
            symbol="TESTUSDT",
            available=True,
            source="BinanceOrderBook",
            upper_liquidity_wall="$101",
            upper_wall_distance_pct=1,
            orderbook_bias="down",
            liquidity_gap_direction="up",
            reason_lines=["盘口热力降级为 Binance 免费深度快照估算"],
        )

        merged = merge_liquidity_contexts(base, None, orderbook)

        self.assertTrue(merged.available)
        self.assertEqual(merged.upper_liquidity_wall, "$101")
        self.assertEqual(merged.orderbook_bias, "down")
        self.assertIn("BinanceOrderBook", merged.source)

    def test_enhance_scores_with_fallback_context(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), liquidity_score_max_delta=15)
            orderbook = StaticProvider(LiquidityContext(
                symbol="TESTUSDT",
                available=True,
                source="BinanceOrderBook",
                lower_liquidity_wall="$99",
                lower_wall_distance_pct=-1,
                orderbook_bias="up",
                liquidity_gap_direction="none",
            ))
            analyzer = MultiSourceLiquidityAnalyzer(settings, binance_orderbook=orderbook)
            signal = make_signal()

            analyzer.enhance(signal)

        self.assertGreater(signal.score, 70)
        self.assertEqual(signal.liquidity_context.source, "BinanceOrderBook")


if __name__ == "__main__":
    unittest.main()
