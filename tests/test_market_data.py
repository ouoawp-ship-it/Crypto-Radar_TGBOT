from __future__ import annotations


# Source group: test_binance_liquidity.py

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
                liquidity_min_distance_pct=0.1,
                liquidity_max_distance_pct=5,
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


# Source group: test_coinalyze_liquidity.py

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.coinalyze_liquidity import CoinalyzeLiquidationProvider
from paopao_radar.config import Settings


class FakeCoinalyzeSource:
    enabled = True

    def __init__(self, payload):
        self.payload = payload

    def liquidation_history(self, symbol, from_ts, to_ts, interval="1hour"):
        return self.payload

    def diagnostics(self):
        return {"enabled": True}


class CoinalyzeLiquidityTests(unittest.TestCase):
    def test_short_liquidations_create_up_bias(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), coinalyze_enable=True, coinalyze_api_key="key")
            source = FakeCoinalyzeSource([
                {"symbol": "BTCUSDT_PERP.A", "history": [{"l": 100, "s": 500}, {"l": 100, "s": 500}]}
            ])

            context = CoinalyzeLiquidationProvider(settings, source).context("BTCUSDT", 100)

        self.assertTrue(context.available)
        self.assertEqual(context.source, "CoinalyzeHistory")
        self.assertEqual(context.liquidation_bias, "up")
        self.assertIn("历史清算量", context.reason_lines[0])

    def test_empty_history_is_unavailable(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), coinalyze_enable=True, coinalyze_api_key="key")
            context = CoinalyzeLiquidationProvider(settings, FakeCoinalyzeSource([])).context("BTCUSDT", 100)

        self.assertFalse(context.available)


if __name__ == "__main__":
    unittest.main()


# Source group: test_data_sources.py

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from paopao_radar.config import Settings
from paopao_radar.data_sources import BinanceDataSource, DataQuality, HttpClient


class MarketCapSourceTests(unittest.TestCase):
    def test_http_client_reuses_owned_session(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), http_cache_enable=False)
            with patch("requests.Session") as session_factory:
                session = session_factory.return_value
                response = session.get.return_value
                response.status_code = 200
                response.json.return_value = {"ok": True}
                client = HttpClient(settings, DataQuality())
                client.get_json("https://example.test/one")
                client.get_json("https://example.test/two")
                client.close()

        session_factory.assert_called_once_with()
        self.assertEqual(session.get.call_count, 2)
        session.close.assert_called_once_with()

    def test_http_client_does_not_close_injected_session(self) -> None:
        with TemporaryDirectory() as tmp:
            session = Mock()
            client = HttpClient(Settings(data_dir=Path(tmp)), DataQuality(), session=session)
            client.close()

        session.close.assert_not_called()

    def test_coinpaprika_market_caps_parse_usd_quotes_and_prefer_better_rank(self) -> None:
        with TemporaryDirectory() as tmp:
            source = BinanceDataSource(Settings(data_dir=Path(tmp)))
            payload = [
                {"symbol": "TEST", "rank": 200, "quotes": {"USD": {"market_cap": 10_000_000}}},
                {"symbol": "TEST", "rank": 50, "quotes": {"USD": {"market_cap": 123_000_000}}},
                {"symbol": "BAD", "rank": 10, "quotes": {"USD": {"market_cap": 0}}},
            ]

            with patch.object(source.http, "get_json", return_value=payload) as get_json:
                result = source.coinpaprika_market_caps()

        self.assertEqual(result, {"TEST": 123_000_000})
        get_json.assert_called_once()


if __name__ == "__main__":
    unittest.main()


# Source group: test_liquidity_router.py

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
