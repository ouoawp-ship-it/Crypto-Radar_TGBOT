from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from paopao_radar.config import Settings
from paopao_radar.data_sources import CoinglassDataSource


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

    def test_orderbook_heatmap_uses_spot_orderbook_history_endpoint(self) -> None:
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
        self.assertEqual(calls[0][0], "/api/spot/orderbook/history")
        self.assertEqual(calls[0][1]["symbol"], "BTCUSDT")
        self.assertEqual(calls[0][1]["interval"], "1h")


if __name__ == "__main__":
    unittest.main()
