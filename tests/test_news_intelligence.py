from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.config import Settings
from paopao_radar.news_intelligence import NewsEventStore, normalize_binance_articles
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

    def test_public_contract_exposes_unavailable_channels_without_fabricating_content(self) -> None:
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
        self.assertEqual(data["schema_version"], "2026-07-17")
        self.assertEqual(len(data["items"]), 2)
        channels = {item["key"]: item for item in data["channels"]}
        self.assertEqual(channels["authorized_zh"]["status"], "unavailable")
        self.assertEqual(channels["sentiment"]["count"], 0)
        serialized = json.dumps(response, ensure_ascii=False).lower()
        self.assertNotIn("password", serialized)
        self.assertNotIn("bot_token", serialized)

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
