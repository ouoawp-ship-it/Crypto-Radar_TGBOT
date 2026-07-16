from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from paopao_radar.config import Settings
from paopao_radar.data_sources import BinanceDataSource, DataQuality, HttpClient


class MarketCapSourceTests(unittest.TestCase):
    def test_http_client_reuses_owned_session(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), http_cache_enable=False)
            with patch("requests.Session") as session_factory:
                session = session_factory.return_value
                response = session.get.return_value
                response.status_code = 200
                response.json.return_value = {"ok": True}
                client = HttpClient(settings, DataQuality())
                client.get_json("https://example.test/one")
                client.get_json("https://example.test/two")
                client.close()

        session_factory.assert_called_once_with()
        self.assertEqual(session.get.call_count, 2)
        session.close.assert_called_once_with()

    def test_http_client_does_not_close_injected_session(self) -> None:
        with TemporaryDirectory() as tmp:
            session = Mock()
            client = HttpClient(Settings(data_dir=Path(tmp)), DataQuality(), session=session)
            client.close()

        session.close.assert_not_called()

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
