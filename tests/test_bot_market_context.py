from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from paopao_radar.bot_market_context import enrich_telegram_with_market_context
from paopao_radar.market_cockpit import MarketSnapshotStore
from paopao_radar.news_intelligence import NewsEventStore
from paopao_radar.realtime_market import RealtimeFeatureStore


def feature_row(
    symbol: str,
    minute: int,
    *,
    buy: float,
    sell: float,
    exchange: str = "binance",
) -> dict[str, object]:
    return {
        "exchange": exchange,
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
    def test_appends_closed_window_market_facts_without_changing_trigger_copy(self) -> None:
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
        self.assertIn("BOT Binance 原生数据确认", text)
        self.assertIn("5m合约主动净占比", text)
        self.assertIn("Surge 偏多", text)
        self.assertIn("五窗", text)
        self.assertIn("24h 异动", text)
        self.assertIn("不采用新闻、社交情报或 CoinGlass/Coinalyze", text)
        self.assertIn("主动成交净额=taker主动买入报价额-taker主动卖出报价额", text)
        self.assertIn("不采用新闻、社交情报或 CoinGlass/Coinalyze", text)
        self.assertIn("不改变本模块原触发阈值", text)

    def test_realtime_confirmation_ignores_non_binance_rows(self) -> None:
        rows = [
            feature_row("BTCUSDT", minute, buy=2_000, sell=500)
            for minute in range(15, 20)
        ]
        rows.extend(
            feature_row("BTCUSDT", minute, buy=0, sell=50_000, exchange="bybit")
            for minute in range(15, 20)
        )
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "realtime.db"
            RealtimeFeatureStore(path).replace_many(rows)
            text = enrich_telegram_with_market_context(
                SimpleNamespace(realtime_features_db_path=path),
                "启动预警",
                "TG_LAUNCH_ALERT",
                [{"symbol": "BTCUSDT"}],
                now_ts=1_200,
            )

        self.assertIn("5m合约主动净占比 +60.00%", text)

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

    def test_appends_exchange_market_facts_and_ignores_news_rows(self) -> None:
        with TemporaryDirectory() as tmp:
            market_path = Path(tmp) / "market.db"
            news_path = Path(tmp) / "news.db"
            MarketSnapshotStore(market_path).append_many([
                {
                    "symbol": "BTCUSDT",
                    "observed_at": 300,
                    "source": "binance_futures_batch",
                    "window_sec": 900,
                    "price": 100,
                    "oi_usd": 1_000_000,
                    "quote_volume": 5_000_000,
                },
                {
                    "symbol": "BTCUSDT",
                    "observed_at": 1_200,
                    "source": "binance_futures_batch",
                    "window_sec": 900,
                    "price": 110,
                    "oi_usd": 1_100_000,
                    "quote_volume": 6_000_000,
                    "funding_pct": 0.0123,
                },
                {
                    "symbol": "BTCUSDT",
                    "observed_at": 1_200,
                    "source": "market_flow_15m",
                    "window_sec": 900,
                    "spot_flow_usd": 250_000,
                    "futures_flow_usd": -125_000,
                },
            ])
            NewsEventStore(news_path).upsert_many([{
                "event_id": "btc-risk-1",
                "published_at": 1_190,
                "collected_at": 1_190,
                "source": "Binance",
                "source_type": "official_announcement",
                "title": "BTC 合约参数调整公告",
                "summary": "",
                "url": "https://www.binance.com/en/support/announcement/btc-risk-1",
                "symbols": ["BTCUSDT"],
                "importance": "high",
                "language": "zh",
                "cluster_id": "btc-risk-1",
                "event_kind": "risk",
                "rights_status": "link_only",
                "timestamp_quality": "source_time",
            }, {
                "event_id": "btc-plaza-risk",
                "published_at": 1_195,
                "collected_at": 1_195,
                "source": "@liquidation-lol.bsky.social",
                "source_type": "plaza",
                "title": "BTC liquidation social post",
                "summary": "",
                "url": "https://bsky.app/profile/liquidation-lol.bsky.social/post/test",
                "symbols": ["BTCUSDT"],
                "importance": "high",
                "language": "en",
                "cluster_id": "btc-plaza-risk",
                "event_kind": "risk",
                "rights_status": "link_only",
                "timestamp_quality": "source_time",
            }, {
                "event_id": "btc-other-official",
                "published_at": 1_198,
                "collected_at": 1_198,
                "source": "OtherExchange",
                "source_type": "official_announcement",
                "title": "BTC other exchange announcement",
                "summary": "",
                "url": "https://www.binance.com/en/support/announcement/other",
                "symbols": ["BTCUSDT"],
                "importance": "high",
                "language": "en",
                "cluster_id": "btc-other-official",
                "event_kind": "risk",
                "rights_status": "link_only",
                "timestamp_quality": "source_time",
            }])
            text = enrich_telegram_with_market_context(
                SimpleNamespace(
                    realtime_features_db_path=Path(tmp) / "missing-realtime.db",
                    market_snapshots_db_path=market_path,
                    news_events_db_path=news_path,
                ),
                "资金流雷达",
                "TG_FLOW_RADAR",
                [{"symbol": "BTCUSDT"}],
                now_ts=1_200,
            )
            summary_text = enrich_telegram_with_market_context(
                SimpleNamespace(
                    realtime_features_db_path=Path(tmp) / "missing-realtime.db",
                    market_snapshots_db_path=market_path,
                    news_events_db_path=news_path,
                ),
                "资金摘要",
                "TG_RADAR_SUMMARY",
                [{"symbol": "BTCUSDT"}],
                now_ts=1_200,
            )

        self.assertTrue(text.startswith("资金流雷达"))
        self.assertIn("↳ 15m", text)
        self.assertIn("现货主动成交净额 +$250.0K", text)
        self.assertIn("合约主动成交净额 -$125.0K", text)
        self.assertIn("OI +10.00%", text)
        self.assertIn("费率 +0.0123%", text)
        self.assertNotIn("24h 情报", text)
        self.assertNotIn("BTC 合约参数调整公告", text)
        self.assertNotIn("liquidation social post", text)
        self.assertNotIn("other exchange announcement", text)
        self.assertIn("BOT Binance 原生数据确认", summary_text)
        self.assertIn("不采用新闻、社交情报或 CoinGlass/Coinalyze", summary_text)

    def test_untrusted_snapshot_source_cannot_enrich_message(self) -> None:
        with TemporaryDirectory() as tmp:
            market_path = Path(tmp) / "market.db"
            MarketSnapshotStore(market_path).append_many([
                {
                    "symbol": "BTCUSDT",
                    "observed_at": 300,
                    "source": "news_derived",
                    "window_sec": 900,
                    "price": 100,
                    "oi_usd": 1_000_000,
                },
                {
                    "symbol": "BTCUSDT",
                    "observed_at": 1_200,
                    "source": "news_derived",
                    "window_sec": 900,
                    "price": 110,
                    "oi_usd": 1_100_000,
                    "spot_flow_usd": 250_000,
                    "futures_flow_usd": -125_000,
                    "funding_pct": 0.0123,
                },
            ])
            original = "资金流雷达"
            text = enrich_telegram_with_market_context(
                SimpleNamespace(
                    realtime_features_db_path=Path(tmp) / "missing-realtime.db",
                    market_snapshots_db_path=market_path,
                ),
                original,
                "TG_FLOW_RADAR",
                [{"symbol": "BTCUSDT"}],
                now_ts=1_200,
            )
        self.assertEqual(text, original)

    def test_one_hour_flow_snapshot_is_not_relabelled_as_fifteen_minutes(self) -> None:
        with TemporaryDirectory() as tmp:
            market_path = Path(tmp) / "market.db"
            MarketSnapshotStore(market_path).append_many([
                {
                    "symbol": "BTCUSDT",
                    "observed_at": 300,
                    "source": "flow_radar",
                    "window_sec": 3600,
                    "price": 100,
                    "oi_usd": 1_000_000,
                    "oi_change_pct": 5,
                },
                {
                    "symbol": "BTCUSDT",
                    "observed_at": 1_200,
                    "source": "flow_radar",
                    "window_sec": 3600,
                    "price": 110,
                    "oi_usd": 1_100_000,
                    "oi_change_pct": 10,
                },
            ])
            original = "资金流雷达"
            text = enrich_telegram_with_market_context(
                SimpleNamespace(
                    realtime_features_db_path=Path(tmp) / "missing-realtime.db",
                    market_snapshots_db_path=market_path,
                ),
                original,
                "TG_FLOW_RADAR",
                [{"symbol": "BTCUSDT"}],
                now_ts=1_200,
            )

        self.assertEqual(text, original)

    def test_missing_summary_facts_and_test_messages_are_never_enriched(self) -> None:
        settings = SimpleNamespace(realtime_features_db_path=Path("missing.db"))
        self.assertEqual(
            enrich_telegram_with_market_context(settings, "摘要", "TG_RADAR_SUMMARY", [{"symbol": "BTC"}]),
            "摘要",
        )
        self.assertEqual(
            enrich_telegram_with_market_context(settings, "测试", "TG_TEST_MESSAGE", [{"symbol": "BTC"}]),
            "测试",
        )


if __name__ == "__main__":
    unittest.main()
