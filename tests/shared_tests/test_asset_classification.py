from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from shared.asset_classification import classify_binance_instrument
from config import Settings
from runtime.radar_engine import RadarEngine
from shared.storage import JsonStore


class AssetClassificationTests(unittest.TestCase):
    def test_spy_is_index_etf_perpetual_not_crypto_or_tokenized_stock(self) -> None:
        result = classify_binance_instrument("SPYUSDT", {"baseAsset": "SPY"})

        self.assertEqual(result["asset_family"], "tradfi")
        self.assertEqual(result["asset_class"], "etf_index")
        self.assertEqual(result["asset_subclass"], "broad_market_etf")
        self.assertEqual(result["instrument_type"], "usdt_perpetual")
        self.assertIn("标普500", result["asset_category_label"])
        self.assertNotIn("代币化股票", result["asset_category_label"])

    def test_reviewed_commodity_and_equity_products_are_distinct(self) -> None:
        gold = classify_binance_instrument("XAUUSDT")
        oil = classify_binance_instrument("CLUSDT")
        equity = classify_binance_instrument("NVDAUSDT")

        self.assertEqual(gold["asset_subclass"], "precious_metal")
        self.assertIn("黄金", gold["asset_category_label"])
        self.assertEqual(oil["asset_subclass"], "energy")
        self.assertIn("原油", oil["asset_category_label"])
        self.assertEqual(equity["asset_class"], "equity")
        self.assertIn("半导体", equity["asset_category_label"])

    def test_crypto_tier_and_exchange_theme_are_both_preserved(self) -> None:
        bitcoin = classify_binance_instrument(
            "BTCUSDT",
            {"baseAsset": "BTC", "underlyingSubType": ["PoW"]},
        )
        solana = classify_binance_instrument("SOLUSDT", {"baseAsset": "SOL"})
        alt = classify_binance_instrument(
            "TESTUSDT",
            {"baseAsset": "TEST", "underlyingSubType": ["AI", "Meme"]},
        )

        self.assertEqual(bitcoin["asset_subclass"], "core_crypto")
        self.assertIn("核心主流", bitcoin["asset_category_label"])
        self.assertEqual(solana["asset_subclass"], "large_crypto")
        self.assertIn("主流加密", solana["asset_category_label"])
        self.assertEqual(alt["asset_subclass"], "altcoin")
        self.assertEqual(alt["asset_theme_tags"], ["AI", "Meme"])

    def test_exchange_metadata_can_identify_new_tradfi_or_tokenized_products(self) -> None:
        stock = classify_binance_instrument(
            "NEWUSDT",
            {"baseAsset": "NEW", "underlyingType": "EQUITY"},
        )
        tokenized = classify_binance_instrument(
            "NEWUSD",
            {"baseAsset": "NEW", "underlyingType": "TOKENIZED_STOCK"},
        )

        self.assertEqual(stock["asset_class"], "equity")
        self.assertEqual(stock["asset_category_source"], "binance_exchange_info")
        self.assertEqual(tokenized["instrument_type"], "tokenized_equity")

    def test_launch_message_displays_asset_category_without_changing_metrics(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = RadarEngine(Settings(data_dir=Path(tmp)), JsonStore(Path(tmp)))
            item = {
                "symbol": "SPYUSDT",
                "coin": "SPY",
                "asset_category_label": "传统金融 · 指数ETF · 标普500",
                "score": 80,
                "stage": "breakout",
                "previous_stage": "primed",
                "appear_count": 2,
                "mcap": 0,
                "mcap_source": "",
                "quote_volume": 1_000_000,
                "funding_pct": 0,
                "funding_interval_hours": 0,
                "price_15m": 1.25,
                "price_1h": 2.5,
                "oi_15m": 1.5,
                "oi_1h": 3.0,
                "volume_ratio": 2.0,
                "breakout": True,
            }

            text = engine._format_launch_alert(item)

            self.assertIn("品类: 传统金融 · 指数ETF · 标普500", text)
            self.assertIn("15m价格: +1.2%", text)
            self.assertIn("1h价格: +2.5%", text)

    def test_launch_candidate_uses_exchange_metadata_classification(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                radar_min_quote_volume=1,
                launch_lifecycle_v2_enable=False,
                launch_message_package_v2_enable=False,
                launch_chart_v2_enable=False,
            )
            engine = RadarEngine(settings, JsonStore(Path(tmp)))

            def fake_analyze(_source: object, item: dict[str, object]) -> dict[str, object]:
                return {
                    **item,
                    "score": 95,
                    "price_15m": 5.0,
                    "price_1h": 8.0,
                    "oi_15m": 4.0,
                    "oi_1h": 8.0,
                    "volume_ratio": 2.5,
                    "breakout": True,
                    "reasons": ["测试"],
                }

            class Source:
                @staticmethod
                def usdt_perp_symbols() -> list[dict[str, object]]:
                    return [{
                        "symbol": "SPYUSDT",
                        "baseAsset": "SPY",
                        "underlyingType": "ETF",
                    }]

                @staticmethod
                def ticker_24h() -> list[dict[str, str]]:
                    return [{
                        "symbol": "SPYUSDT",
                        "quoteVolume": "10000000",
                        "priceChangePercent": "1",
                        "lastPrice": "700",
                    }]

                @staticmethod
                def premium_index() -> list[dict[str, str]]:
                    return []

                @staticmethod
                def market_caps() -> dict[str, float]:
                    return {}

            engine._analyze_launch_symbol = fake_analyze  # type: ignore[method-assign]

            result = engine.build_launch_alerts(Source())  # type: ignore[arg-type]

            self.assertEqual(result["alerts"][0]["asset_class"], "etf_index")
            self.assertEqual(
                result["alerts"][0]["asset_category_label"],
                "传统金融 · 指数ETF · 标普500",
            )
            self.assertIn("品类: 传统金融 · 指数ETF · 标普500", result["messages"][0])


if __name__ == "__main__":
    unittest.main()
