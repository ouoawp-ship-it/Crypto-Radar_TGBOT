from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

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


if __name__ == "__main__":
    unittest.main()
