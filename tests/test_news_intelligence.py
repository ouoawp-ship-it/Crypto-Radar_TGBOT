from __future__ import annotations

import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from paopao_radar.config import Settings
from paopao_radar.info_sources import normalize_bluesky_feed, normalize_rss_feed
from paopao_radar.market_cockpit import MarketSnapshotStore
from paopao_radar.news_intelligence import NewsEventStore, ingest_binance_announcements, normalize_binance_articles
from paopao_radar.web_services.public import public_info_feed_payload


class NewsIntelligenceTest(unittest.TestCase):
    def articles(self) -> list[dict[str, object]]:
        return [
            {
                "code": "btc-listing",
                "title": "Binance Will List Example Token (ABC)",
                "releaseDate": 1_720_000_000_000,
            },
            {
                "code": "risk-delisting",
                "title": "Binance Will Delist XYZ on 2026-07-18",
                "releaseDate": 1_720_000_100_000,
            },
        ]

    def test_normalization_is_bounded_traceable_and_rights_safe(self) -> None:
        events = normalize_binance_articles(self.articles(), collected_at=1_720_000_200)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["rights_status"], "official_link_only")
        self.assertEqual(events[0]["symbols"], ["ABCUSDT"])
        self.assertEqual(events[0]["importance"], "high")
        self.assertEqual(events[0]["ai_analysis"]["status"], "ready")
        self.assertIn("规则推断", events[0]["ai_analysis"]["fact_inference_boundary"])
        self.assertTrue(events[0]["url"].startswith("https://www.binance.com/"))

    def test_external_or_malformed_source_url_is_not_indexed(self) -> None:
        events = normalize_binance_articles([
            {"code": "bad", "title": "<b>Unsafe (ABC)</b>", "url": "https://attacker.example/story"},
        ], collected_at=100)
        self.assertEqual(events, [])

    def test_store_clusters_duplicates_and_filters_by_symbol(self) -> None:
        with TemporaryDirectory() as tmp:
            store = NewsEventStore(Path(tmp) / "news.db")
            events = normalize_binance_articles(self.articles(), collected_at=1_720_000_200)
            duplicate = dict(events[0])
            duplicate["event_id"] = "binance_duplicate"
            duplicate["source_links"] = [
                {"source": "Binance", "url": events[0]["url"] + "?lang=en", "rights_status": "official_link_only"}
            ]
            store.upsert_many([*events, duplicate])
            feed = store.list_feed(symbol="ABC", page=1, page_size=20)

        self.assertEqual(feed["pagination"]["total"], 1)
        self.assertEqual(feed["items"][0]["cluster_size"], 2)
        self.assertEqual(feed["items"][0]["symbols"], ["ABCUSDT"])
        self.assertEqual(len(feed["items"][0]["source_links"]), 2)

    def test_plaza_rankings_aggregate_real_posts_sentiment_and_engagement(self) -> None:
        now = 1_720_000_300
        with TemporaryDirectory() as tmp:
            store = NewsEventStore(Path(tmp) / "news.db")
            store.upsert_many([
                {
                    "event_id": "plaza_btc_long",
                    "published_at": now - 600,
                    "collected_at": now - 590,
                    "source": "@market.bsky.social",
                    "source_type": "plaza",
                    "title": "$BTC spot demand expands",
                    "summary": "BTC public discussion turns constructive.",
                    "url": "https://bsky.app/profile/market.bsky.social/post/btc-long",
                    "symbols": ["BTCUSDT"],
                    "event_kind": "opportunity",
                    "ai_analysis": {"engagement": {"likes": 10, "reposts": 3, "replies": 2, "score": 18}},
                    "rights_status": "public_social_link",
                },
                {
                    "event_id": "plaza_btc_risk",
                    "published_at": now - 1_800,
                    "collected_at": now - 1_790,
                    "source": "@risk.bsky.social",
                    "source_type": "plaza",
                    "title": "$BTC leverage is crowded",
                    "summary": "BTC leverage risk is rising.",
                    "url": "https://bsky.app/profile/risk.bsky.social/post/btc-risk",
                    "symbols": ["BTCUSDT"],
                    "event_kind": "risk",
                    "ai_analysis": {"engagement": {"likes": 4, "reposts": 1, "replies": 1}},
                    "rights_status": "public_social_link",
                },
                {
                    "event_id": "plaza_eth_old",
                    "published_at": now - 18_000,
                    "collected_at": now - 17_990,
                    "source": "@market.bsky.social",
                    "source_type": "plaza",
                    "title": "$ETH public activity rises",
                    "summary": "ETH remains active on the 24h board.",
                    "url": "https://bsky.app/profile/market.bsky.social/post/eth-old",
                    "symbols": ["ETHUSDT"],
                    "event_kind": "opportunity",
                    "rights_status": "public_social_link",
                },
            ])
            rankings = store.plaza_rankings(now_ts=now, windows=(14_400, 86_400), limit=10)

        self.assertEqual([item["coin"] for item in rankings[14_400]], ["BTC"])
        self.assertEqual([item["coin"] for item in rankings[86_400]], ["BTC", "ETH"])
        btc = rankings[14_400][0]
        self.assertEqual(btc["posts"], 2)
        self.assertEqual(btc["recent_1h_posts"], 2)
        self.assertEqual(btc["previous_1h_posts"], 0)
        self.assertIsNone(btc["recent_ratio"])
        self.assertTrue(btc["is_new"])
        self.assertEqual(btc["positive_pct"], 50)
        self.assertEqual(btc["sentiment"], "neutral")
        self.assertEqual(btc["engagement"], 25)

    def test_public_plaza_contract_exposes_server_owned_rankings(self) -> None:
        now = 1_720_000_300
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            NewsEventStore(settings.news_events_db_path).upsert_many([{
                "event_id": "plaza_contract_btc",
                "published_at": now - 300,
                "collected_at": now - 290,
                "source": "@market.bsky.social",
                "source_type": "plaza",
                "title": "$BTC public activity rises",
                "summary": "BTC public activity rises.",
                "url": "https://bsky.app/profile/market.bsky.social/post/btc-contract",
                "symbols": ["BTCUSDT"],
                "event_kind": "opportunity",
                "ai_analysis": {"engagement": {"likes": 8, "reposts": 2, "replies": 1}},
                "rights_status": "public_social_link",
            }])
            response = public_info_feed_payload(
                settings=settings,
                now_ts=now,
                refresh=False,
                source_type="plaza",
                page_size=10,
            )

        self.assertTrue(response["ok"])
        rankings = response["data"]["plaza_rankings"]
        self.assertEqual(rankings["schema_version"], "workstation.info.plaza.v3")
        self.assertEqual(rankings["provider"]["id"], "bluesky_crypto_plaza")
        self.assertEqual(rankings["provider"]["rights_status"], "public_social_link")
        self.assertEqual(rankings["active_4h"][0]["coin"], "BTC")
        self.assertEqual(rankings["total_24h"][0]["posts"], 1)
        self.assertIsNone(rankings["total_24h"][0]["price_change_pct"])
        self.assertIsNone(rankings["total_24h"][0]["futures_long_pct"])
        self.assertIsNone(rankings["total_24h"][0]["futures_short_pct"])

    def test_public_plaza_contract_uses_gross_futures_flow_for_long_short_share(self) -> None:
        now = 1_720_000_300
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            NewsEventStore(settings.news_events_db_path).upsert_many([{
                "event_id": "plaza_flow_share_btc",
                "published_at": now - 300,
                "collected_at": now - 290,
                "source": "@market.bsky.social",
                "source_type": "plaza",
                "title": "$BTC public activity rises",
                "summary": "BTC public activity rises.",
                "url": "https://bsky.app/profile/market.bsky.social/post/btc-flow-share",
                "symbols": ["BTCUSDT"],
                "event_kind": "opportunity",
                "ai_analysis": {"engagement": {"likes": 8, "reposts": 2, "replies": 1}},
                "rights_status": "public_social_link",
            }])
            MarketSnapshotStore(settings.market_snapshots_db_path).append_many([{
                "symbol": "BTCUSDT",
                "observed_at": now,
                "source": "test_flow_share",
                "window_sec": 900,
                "price": 100.0,
                "price_change_pct": 1.0,
                "change_window_sec": 86_400,
                "quote_volume": 10_000_000.0,
                "futures_inflow_usd": 600_000.0,
                "futures_outflow_usd": 400_000.0,
                "futures_flow_usd": 200_000.0,
                "coverage": {"price": True, "futures_flow": True},
            }])
            response = public_info_feed_payload(
                settings=settings,
                now_ts=now,
                refresh=False,
                source_type="plaza",
                page_size=10,
            )

        self.assertTrue(response["ok"])
        ranking = response["data"]["plaza_rankings"]["total_24h"][0]
        self.assertEqual(ranking["futures_long_pct"], 60)
        self.assertEqual(ranking["futures_short_pct"], 40)
        self.assertEqual(ranking["futures_flow_usd"], 200_000.0)

    def test_public_contract_exposes_real_channel_status_without_fabricating_content(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            NewsEventStore(settings.news_events_db_path).upsert_many(
                normalize_binance_articles(self.articles(), collected_at=1_720_000_200)
            )
            response = public_info_feed_payload(
                settings=settings,
                now_ts=1_720_000_300,
                refresh=False,
                page_size=10,
            )

        self.assertTrue(response["ok"])
        data = response["data"]
        self.assertEqual(data["schema_version"], "2026-07-18")
        self.assertEqual(len(data["items"]), 2)
        channels = {item["key"]: item for item in data["channels"]}
        self.assertEqual(channels["news_zh"]["status"], "empty")
        self.assertEqual(channels["news_en"]["count"], 0)
        self.assertEqual(channels["kol"]["count"], 0)
        self.assertEqual(channels["plaza"]["count"], 0)
        serialized = json.dumps(response, ensure_ascii=False).lower()
        self.assertNotIn("password", serialized)
        self.assertNotIn("bot_token", serialized)

    def test_public_cold_start_schedules_news_refresh_without_blocking_on_upstream(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            with (
                patch("paopao_radar.web_services.public.ingest_public_info_sources", side_effect=AssertionError("request path must not ingest")),
                patch("paopao_radar.web_services.public._schedule_news_refresh", return_value=True) as schedule,
            ):
                response = public_info_feed_payload(settings=settings)

        self.assertTrue(response["ok"])
        self.assertEqual(response["data"]["data_status"], "degraded")
        self.assertEqual(response["data"]["ingestion"]["status"], "refreshing")
        self.assertIn("后台更新", response["data"]["warnings"][0])
        schedule.assert_called_once_with(settings)

    def test_news_background_refresh_is_single_flight_and_persists_events(self) -> None:
        from paopao_radar.web_services import public as public_service

        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            started = threading.Event()
            release = threading.Event()
            finished = threading.Event()

            def ingest(*_args, **_kwargs):
                started.set()
                release.wait(timeout=2)
                NewsEventStore(settings.news_events_db_path).upsert_many(
                    normalize_binance_articles(self.articles(), collected_at=int(time.time()))
                )
                finished.set()
                return {"written": 2}

            with patch.object(public_service, "ingest_public_info_sources", side_effect=ingest):
                self.assertTrue(public_service._schedule_news_refresh(settings))
                self.assertTrue(started.wait(timeout=1))
                self.assertFalse(public_service._schedule_news_refresh(settings))
                release.set()
                self.assertTrue(finished.wait(timeout=2))

            feed = NewsEventStore(settings.news_events_db_path).list_feed(page=1, page_size=10)

        self.assertEqual(feed["pagination"]["total"], 2)

    def test_news_ingestion_closes_internally_owned_source(self) -> None:
        class OwnedSource:
            def __init__(self, *, fail: bool = False) -> None:
                self.fail = fail
                self.closed = False
                self.http = type("Http", (), {"close": lambda owner: setattr(self, "closed", True)})()

            def announcements(self, **_kwargs):
                if self.fail:
                    raise RuntimeError("announcement source failed")
                return self_articles

        self_articles = self.articles()
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            successful = OwnedSource()
            with patch("paopao_radar.news_intelligence.BinanceDataSource", return_value=successful):
                result = ingest_binance_announcements(settings, now_ts=int(time.time()))
            failed = OwnedSource(fail=True)
            with patch("paopao_radar.news_intelligence.BinanceDataSource", return_value=failed):
                with self.assertRaisesRegex(RuntimeError, "announcement source failed"):
                    ingest_binance_announcements(settings, now_ts=int(time.time()))

        self.assertEqual(result["written"], 2)
        self.assertTrue(successful.closed)
        self.assertTrue(failed.closed)

    def test_public_rss_and_social_normalizers_produce_distinct_channels(self) -> None:
        rss = """<?xml version="1.0"?><rss><channel><item><guid>zh-1</guid><title>比特币资金流入创出新高</title><description>BTC 市场活跃度上升</description><link>https://www.panewslab.com/zh/articles/1</link><pubDate>Sat, 18 Jul 2026 10:00:00 GMT</pubDate></item></channel></rss>"""
        news = normalize_rss_feed(
            rss,
            source_id="panews_zh",
            source_name="PANews",
            language="zh",
            collected_at=1_721_300_000,
        )
        social = normalize_bluesky_feed({"feed": [{"post": {
            "uri": "at://did:plc:test/app.bsky.feed.post/abc",
            "author": {"handle": "analyst.bsky.social", "displayName": "Analyst"},
            "record": {"text": "$ETH breakout looks bullish", "createdAt": "2026-07-18T10:05:00Z"},
            "likeCount": 120,
            "repostCount": 10,
            "replyCount": 5,
        }}]}, source_type="kol", collected_at=1_721_300_000)

        self.assertEqual(news[0]["source_type"], "news")
        self.assertEqual(news[0]["language"], "zh")
        self.assertIn("BTCUSDT", news[0]["symbols"])
        self.assertEqual(social[0]["source_type"], "kol")
        self.assertEqual(social[0]["event_kind"], "opportunity")
        self.assertIn("ETHUSDT", social[0]["symbols"])
        self.assertEqual(social[0]["ai_analysis"]["engagement"]["score"], 145)

    def test_retention_prunes_old_events_and_symbol_index(self) -> None:
        with TemporaryDirectory() as tmp:
            store = NewsEventStore(Path(tmp) / "news.db")
            old = normalize_binance_articles(self.articles(), collected_at=100)
            for item in old:
                item["published_at"] = 100
            store.upsert_many(old)
            result = store.prune(now_ts=100 + 100 * 86_400, retention_days=90, limit=5000)
            feed = store.list_feed(page=1, page_size=20)

        self.assertEqual(result["removed"], 2)
        self.assertEqual(feed["items"], [])


if __name__ == "__main__":
    unittest.main()
