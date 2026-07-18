from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from paopao_radar.bot_market_context import enrich_telegram_with_market_context
from paopao_radar.realtime_market import RealtimeFeatureStore


def feature_row(symbol: str, minute: int, *, buy: float, sell: float) -> dict[str, object]:
    return {
        "exchange": "binance",
        "market": "futures",
        "symbol": symbol,
        "bucket_start": minute * 60,
        "bucket_sec": 60,
        "trade_buy_usd": buy,
        "trade_sell_usd": sell,
        "trade_count": 10,
        "price_open": 100 + minute * 0.1,
        "price_high": 100.2 + minute * 0.1,
        "price_low": 99.9 + minute * 0.1,
        "price_close": 100.1 + minute * 0.1,
        "long_liquidation_usd": 0,
        "short_liquidation_usd": 100 if minute >= 15 else 0,
    }


class BotMarketContextTests(unittest.TestCase):
    def test_appends_closed_window_web_facts_without_changing_trigger_copy(self) -> None:
        rows = [
            feature_row(
                "BTCUSDT",
                minute,
                buy=2_000 if minute >= 15 else 700,
                sell=500 if minute >= 15 else 800,
            )
            for minute in range(20)
        ]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "realtime.db"
            RealtimeFeatureStore(path).replace_many(rows)
            text = enrich_telegram_with_market_context(
                SimpleNamespace(realtime_features_db_path=path),
                "🚀 原启动预警",
                "TG_LAUNCH_ALERT",
                [{"symbol": "BTCUSDT"}],
                now_ts=1_200,
            )

        self.assertTrue(text.startswith("🚀 原启动预警"))
        self.assertIn("Web 市场事实增强", text)
        self.assertIn("5m CVD", text)
        self.assertIn("Surge 偏多", text)
        self.assertIn("五窗", text)
        self.assertIn("24h 异动", text)
        self.assertIn("不改变本模块原触发阈值", text)

    def test_missing_realtime_facts_leave_bot_message_unchanged(self) -> None:
        original = "资金费率警报"
        enriched = enrich_telegram_with_market_context(
            SimpleNamespace(realtime_features_db_path=Path("missing.db")),
            original,
            "TG_FUNDING_ALERT",
            [{"symbol": "ETHUSDT"}],
            now_ts=1_200,
        )
        self.assertEqual(enriched, original)

    def test_summary_and_test_messages_are_never_enriched(self) -> None:
        settings = SimpleNamespace(realtime_features_db_path=Path("missing.db"))
        self.assertEqual(
            enrich_telegram_with_market_context(settings, "摘要", "TG_RADAR_SUMMARY", [{"symbol": "BTC"}]),
            "摘要",
        )


if __name__ == "__main__":
    unittest.main()
