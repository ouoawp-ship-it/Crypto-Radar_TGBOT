from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.config import Settings
from paopao_radar.heat_context import (
    HeatContextEnricher,
    five_day_volume_context,
)
from paopao_radar.storage import JsonStore


NOW_TS = 1_800_000_000
DAY_MS = 86_400_000


def closed_daily_volumes(volumes: list[float]) -> list[list[object]]:
    start = NOW_TS * 1000 - len(volumes) * DAY_MS
    return [
        [
            start + index * DAY_MS,
            "1",
            "1",
            "1",
            "1",
            "1",
            start + (index + 1) * DAY_MS - 1,
            str(volume),
        ]
        for index, volume in enumerate(volumes)
    ]


class _HeatHttp:
    def __init__(self, trending: object = None, square: object = None):
        self.trending = trending
        self.square = square
        self.calls: list[str] = []

    def get_json(self, url: str, **_kwargs: object) -> object:
        self.calls.append(url)
        if "search/trending" in url:
            return self.trending
        return self.square


class HeatContextTests(unittest.TestCase):
    def test_volume_surge_uses_five_complete_days(self) -> None:
        result = five_day_volume_context(
            closed_daily_volumes([10, 10, 10, 10, 10]),
            current_24h_quote_volume=25,
            now_ms=NOW_TS * 1000,
        )
        self.assertTrue(result["ready"])
        self.assertTrue(result["volume_surge"])
        self.assertEqual(result["volume_ratio"], 2.5)

    def test_disabled_has_no_network_or_cache_write(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(
                data_dir=root,
                heat_context_enable=False,
                heat_context_cache_path=root / "heat.json",
            )
            http = _HeatHttp()
            items = [{"coin": "BTC"}]
            enriched, diagnostics = HeatContextEnricher(
                settings,
                JsonStore(root),
                http,
            ).enrich(items, now_ts=NOW_TS)
            self.assertIs(enriched, items)
            self.assertEqual(diagnostics["status"], "disabled")
            self.assertEqual(http.calls, [])
            self.assertFalse(settings.heat_context_cache_path.exists())

    def test_trending_is_context_only_and_cache_is_reused(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(
                data_dir=root,
                heat_context_enable=True,
                heat_context_cache_path=root / "heat.json",
                heat_context_cache_ttl_sec=900,
            )
            http = _HeatHttp({
                "coins": [{"item": {"symbol": "BTC"}}],
            })
            item = {
                "coin": "BTC",
                "volume_context": {
                    "ready": True,
                    "volume_ratio": 3.0,
                    "volume_surge": True,
                },
            }
            first, first_diag = HeatContextEnricher(
                settings,
                JsonStore(root),
                http,
            ).enrich([item], now_ts=NOW_TS)
            second, second_diag = HeatContextEnricher(
                settings,
                JsonStore(root),
                http,
            ).enrich([item], now_ts=NOW_TS + 60)
            self.assertTrue(first[0]["heat_context"]["coingecko_trending"])
            self.assertTrue(first[0]["heat_context"]["context_only"])
            self.assertEqual(first_diag["network_calls"], 1)
            self.assertEqual(second_diag["network_calls"], 0)
            self.assertEqual(len(http.calls), 1)

    def test_coingecko_failure_uses_stale_cache_and_degrades(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(
                data_dir=root,
                heat_context_enable=True,
                heat_context_cache_path=root / "heat.json",
                heat_context_cache_ttl_sec=60,
            )
            store = JsonStore(root)
            store.save(settings.heat_context_cache_path, {
                "coingecko_trending": {
                    "symbols": ["ETH"],
                    "fetched_at": NOW_TS - 3600,
                },
            })
            http = _HeatHttp(None)
            enriched, diagnostics = HeatContextEnricher(
                settings,
                store,
                http,
            ).enrich([{"coin": "ETH"}], now_ts=NOW_TS)
            self.assertTrue(enriched[0]["heat_context"]["coingecko_trending"])
            self.assertTrue(enriched[0]["heat_context"]["degraded"])
            self.assertEqual(diagnostics["status"], "degraded")

    def test_square_failure_does_not_remove_other_context(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(
                data_dir=root,
                heat_context_enable=True,
                binance_square_heat_enable=True,
                heat_context_cache_path=root / "heat.json",
            )
            http = _HeatHttp(
                {"coins": [{"item": {"symbol": "SOL"}}]},
                None,
            )
            enriched, diagnostics = HeatContextEnricher(
                settings,
                JsonStore(root),
                http,
            ).enrich([{"coin": "SOL"}], now_ts=NOW_TS)
            self.assertTrue(enriched[0]["heat_context"]["coingecko_trending"])
            self.assertTrue(enriched[0]["heat_context"]["degraded"])
            self.assertEqual(diagnostics["square_status"], "degraded")
