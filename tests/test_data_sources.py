from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from paopao_radar.config import Settings
from paopao_radar.data_sources import BinanceDataSource


class MarketCapSourceTests(unittest.TestCase):
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
