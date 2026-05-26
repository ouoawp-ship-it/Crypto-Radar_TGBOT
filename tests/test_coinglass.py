from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from paopao_radar.config import Settings
from paopao_radar.data_sources import BinanceDataSource, CoinglassDataSource


class CoinglassDataSourceTests(unittest.TestCase):
    def test_disabled_without_key(self) -> None:
        with TemporaryDirectory() as tmp:
            source = CoinglassDataSource(Settings(data_dir=Path(tmp)))

            self.assertFalse(source.enabled)
            self.assertIsNone(source.open_interest_exchange_list("BTC"))
            self.assertIn("coinglassOpenInterestExchangeList", source.diagnostics()["quality"]["failures"])

    def test_endpoint_uses_configured_base_url(self) -> None:
        with TemporaryDirectory() as tmp:
            source = CoinglassDataSource(Settings(
                data_dir=Path(tmp),
                coinglass_base_url="https://example.test",
            ))

            self.assertEqual(source.endpoint("/api/test"), "https://example.test/api/test")

    def test_unwraps_common_response_shapes(self) -> None:
        self.assertEqual(CoinglassDataSource.unwrap_data({"data": [1]}), [1])
        self.assertEqual(CoinglassDataSource.unwrap_data({"result": {"ok": True}}), {"ok": True})
        self.assertEqual(CoinglassDataSource.unwrap_data([1, 2]), [1, 2])

    def test_liquidation_heatmap_uses_v4_model1_endpoint(self) -> None:
        with TemporaryDirectory() as tmp:
            source = CoinglassDataSource(Settings(
                data_dir=Path(tmp),
                coinglass_enable=True,
                coinglass_api_key="key",
            ))
            calls = []

            def fake_get_json(path, params=None, quality_key="coinglass", timeout_sec=None):
                calls.append((path, params or {}, quality_key))
                return {"data": [{"price": 100}]}

            with patch.object(source, "get_json", side_effect=fake_get_json):
                data = source.liquidation_heatmap("Binance", "BTCUSDT", "24h")

        self.assertEqual(data, [{"price": 100}])
        self.assertEqual(calls[0][0], "/api/futures/liquidation/heatmap/model1")
        self.assertEqual(calls[0][1]["symbol"], "BTCUSDT")
        self.assertEqual(calls[0][1]["exchange"], "Binance")

    def test_orderbook_heatmap_uses_futures_orderbook_history_endpoint(self) -> None:
        with TemporaryDirectory() as tmp:
            source = CoinglassDataSource(Settings(
                data_dir=Path(tmp),
                coinglass_enable=True,
                coinglass_api_key="key",
            ))
            calls = []

            def fake_get_json(path, params=None, quality_key="coinglass", timeout_sec=None):
                calls.append((path, params or {}, quality_key))
                return {"data": [{"price": 100}]}

            with patch.object(source, "get_json", side_effect=fake_get_json):
                data = source.orderbook_heatmap("Binance", "BTCUSDT", "24h")

        self.assertEqual(data, [{"price": 100}])
        self.assertEqual(calls[0][0], "/api/futures/orderbook/history")
        self.assertEqual(calls[0][1]["symbol"], "BTCUSDT")
        self.assertEqual(calls[0][1]["interval"], "1h")

    def test_binance_announcements_fetches_multiple_pages(self) -> None:
        with TemporaryDirectory() as tmp:
            source = BinanceDataSource(Settings(data_dir=Path(tmp)))
            calls = []

            def fake_get_json(url, params=None, cache_key=None, quality_key="http", timeout=None, retries=None):
                params = params or {}
                calls.append(params)
                if params.get("pageNo") == 1:
                    return {"data": {"catalogs": [{"articles": [{"code": f"{params['catalogId']}-a"}]}]}}
                if params.get("pageNo") == 2:
                    return {"data": {"catalogs": [{"articles": [{"code": f"{params['catalogId']}-b"}]}]}}
                return {"data": {"catalogs": [{"articles": []}]}}

            with patch.object(source.http, "get_json", side_effect=fake_get_json):
                articles = source.announcements(page_size=50)

        self.assertEqual(len(articles), 6)
        self.assertIn(2, {call["pageNo"] for call in calls})
        self.assertTrue(all(call["pageSize"] == 50 for call in calls))


if __name__ == "__main__":
    unittest.main()
