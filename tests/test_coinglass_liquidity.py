from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.coinglass_liquidity import (
    CoinglassLiquidityAnalyzer,
    LiquidityContext,
    score_liquidity_context,
)
from paopao_radar.config import Settings
from paopao_radar.structure_radar import (
    SIGNAL_PRE_BREAKDOWN_NEAR,
    SIGNAL_PRE_BREAKOUT_NEAR,
    StructureRadarEngine,
    StructureSignal,
)


def make_signal(signal_type: str = SIGNAL_PRE_BREAKOUT_NEAR) -> StructureSignal:
    return StructureSignal(
        symbol="TESTUSDT",
        interval="15m",
        signal_type=signal_type,
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
        reason_lines=["unit"],
        base_score=70,
        final_score=70,
    )


class FakeCoinglassSource:
    def __init__(self, liquidation=None, orderbook=None, enabled: bool = True):
        self._liquidation = liquidation
        self._orderbook = orderbook
        self.enabled = enabled

    def liquidation_heatmap(self, exchange, symbol, range_="24h"):
        return self._liquidation

    def orderbook_heatmap(self, exchange, symbol, range_="24h"):
        return self._orderbook

    def diagnostics(self):
        return {"enabled": self.enabled, "quality": {"warnings": []}}


class CoinglassLiquidityTests(unittest.TestCase):
    def make_settings(self, tmp: str, **kwargs) -> Settings:
        return Settings(
            data_dir=Path(tmp),
            coinglass_enable=True,
            coinglass_api_key="test-key",
            coinglass_liquidity_enable=True,
            **kwargs,
        )

    def test_disabled_without_key_returns_unavailable_context(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), coinglass_liquidity_enable=False)
            analyzer = CoinglassLiquidityAnalyzer(settings, FakeCoinglassSource(enabled=False))
            signal = make_signal()

            analyzer.enhance(signal)

        self.assertFalse(signal.liquidity_context.available)
        self.assertEqual(signal.score, 70)
        self.assertEqual(signal.liquidity_score_delta, 0)

    def test_endpoint_failure_keeps_structure_signal_usable(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            analyzer = CoinglassLiquidityAnalyzer(settings, FakeCoinglassSource(None, None))
            signal = make_signal()

            analyzer.enhance(signal)

        self.assertFalse(signal.liquidity_context.available)
        self.assertEqual(signal.final_score, 70)

    def test_available_payload_generates_liquidity_context(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            source = FakeCoinglassSource(
                liquidation=[{"price": 104, "amount": 1000}, {"price": 94, "amount": 50}],
                orderbook=[{"price": 96, "amount": 800, "side": "bid"}],
            )
            context = CoinglassLiquidityAnalyzer(settings, source).context("TESTUSDT", 100)

        self.assertTrue(context.available)
        self.assertEqual(context.liquidation_bias, "up")
        self.assertAlmostEqual(context.nearest_liquidation_above_pct, 4.0)

    def test_up_signal_scores_higher_with_upper_liquidation_pool(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            source = FakeCoinglassSource(liquidation=[{"price": 103, "amount": 1000}], orderbook=[])
            signal = make_signal()

            CoinglassLiquidityAnalyzer(settings, source).enhance(signal)

        self.assertGreater(signal.liquidity_score_delta, 0)
        self.assertGreater(signal.score, signal.base_score)

    def test_up_signal_scores_lower_with_near_upper_sell_wall(self) -> None:
        context = LiquidityContext(
            symbol="TESTUSDT",
            available=True,
            source="unit",
            upper_liquidity_wall="102",
            upper_wall_distance_pct=2.0,
            liquidation_bias="neutral",
            orderbook_bias="down",
            liquidity_gap_direction="none",
        )

        delta = score_liquidity_context(make_signal(), context, 15)

        self.assertLess(delta, 0)

    def test_down_signal_scores_higher_with_lower_liquidation_pool(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            source = FakeCoinglassSource(liquidation=[{"price": 96, "amount": 1000}], orderbook=[])
            signal = make_signal(SIGNAL_PRE_BREAKDOWN_NEAR)

            CoinglassLiquidityAnalyzer(settings, source).enhance(signal)

        self.assertGreater(signal.liquidity_score_delta, 0)

    def test_score_delta_is_capped_by_config(self) -> None:
        context = LiquidityContext(
            symbol="TESTUSDT",
            available=True,
            source="unit",
            upper_liquidation_zone="101",
            nearest_liquidation_above_pct=1.0,
            liquidation_bias="up",
            liquidity_gap_direction="up",
            orderbook_bias="up",
        )

        delta = score_liquidity_context(make_signal(), context, 3)

        self.assertEqual(delta, 3)

    def test_structure_template_shows_unavailable_state(self) -> None:
        signal = make_signal()
        lines = StructureRadarEngine._liquidity_lines(signal)

        self.assertIn("未启用", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
