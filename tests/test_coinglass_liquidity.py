from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.coinglass_liquidity import (
    CoinglassLiquidityAnalyzer,
    LiquidityContext,
    api_status_summary,
    parsed_item_count,
    payload_shape_summary,
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

    def test_upgrade_plan_status_is_reported_as_reason(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            source = FakeCoinglassSource(
                liquidation={"code": "401", "msg": "Upgrade plan"},
                orderbook={"code": "401", "msg": "Upgrade plan"},
            )
            context = CoinglassLiquidityAnalyzer(settings, source).context("TESTUSDT", 100)

        self.assertFalse(context.available)
        self.assertIn("Upgrade plan", " ".join(context.reason_lines))

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

    def test_heatmap_matrix_payload_is_parsed(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            source = FakeCoinglassSource(
                liquidation={
                    "yAxis": [96, 100, 104],
                    "data": [[1700000000, 2, 900], [1700000000, 0, 100]],
                },
                orderbook={
                    "data": [[1700000000, 97, 700], [1700000000, 103, 50]],
                },
            )
            context = CoinglassLiquidityAnalyzer(settings, source).context("TESTUSDT", 100)

        self.assertTrue(context.available)
        self.assertEqual(context.upper_liquidation_zone, "$104")
        self.assertAlmostEqual(context.nearest_liquidation_above_pct, 4.0)
        self.assertEqual(context.lower_liquidity_wall, "$97")

    def test_nested_result_payload_is_parsed(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            source = FakeCoinglassSource(
                liquidation={"result": {"levels": [{"priceUsd": 104, "liquidityUsd": 900}]}},
                orderbook={"orderBook": {"bids": [{"price": 96, "bidSize": 700}]}},
            )
            context = CoinglassLiquidityAnalyzer(settings, source).context("TESTUSDT", 100)

        self.assertTrue(context.available)
        self.assertEqual(context.upper_liquidation_zone, "$104")
        self.assertEqual(context.lower_liquidity_wall, "$96")

    def test_payload_shape_summary_does_not_expose_raw_values(self) -> None:
        payload = {"data": [{"price": "123.45", "token": "secret-like-text"}], "status": "ok"}

        summary = payload_shape_summary(payload)

        self.assertEqual(summary["type"], "dict")
        self.assertIn("data", summary["keys"])
        self.assertNotIn("secret-like-text", str(summary))
        self.assertEqual(parsed_item_count(payload), 1)

    def test_api_status_summary_exposes_only_code_and_msg(self) -> None:
        payload = {"code": "400", "msg": "Params Error", "data": [{"price": 100}]}

        self.assertEqual(
            api_status_summary(payload),
            {"code": "400", "msg": "Params Error"},
        )

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

    def test_structure_template_localizes_liquidity_states(self) -> None:
        signal = make_signal()
        signal.liquidity_context = LiquidityContext(
            symbol="TESTUSDT",
            available=True,
            source="unit",
            liquidation_bias="down",
            orderbook_bias="unavailable",
            liquidity_gap_direction="unavailable",
            reason_lines=["Binance盘口快照没有命中配置距离内的买卖墙"],
        )

        body = "\n".join(StructureRadarEngine._liquidity_lines(signal))

        self.assertIn("清算磁吸: 下方清算池更近或更强", body)
        self.assertIn("盘口流动性: 不可用，暂无有效买墙/卖墙", body)
        self.assertIn("流动性缺口: 不可用，暂无可靠流动性缺口数据", body)
        self.assertIn("Binance盘口快照没有命中配置距离内的买卖墙", body)


if __name__ == "__main__":
    unittest.main()
