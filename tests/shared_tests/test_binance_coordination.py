from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from config import Settings
from shared.binance_coordination import (
    GlobalBinanceCoordinator,
    binance_request_buckets,
)
from shared.binance_data import DataQuality, HttpClient


class BinanceRequestCostTests(unittest.TestCase):
    def test_futures_300_kline_and_oi_use_separate_official_buckets(self) -> None:
        kline = binance_request_buckets(
            "https://fapi.binance.com/fapi/v1/klines",
            {"limit": 300},
            futures_weight_per_minute=1200,
            spot_weight_per_minute=3000,
            futures_data_requests_per_5m=800,
        )
        oi = binance_request_buckets(
            "https://fapi.binance.com/futures/data/openInterestHist",
            {"limit": 300},
            futures_weight_per_minute=1200,
            spot_weight_per_minute=3000,
            futures_data_requests_per_5m=800,
        )

        self.assertEqual((kline[0].name, kline[0].cost), ("futures_weight_1m", 2.0))
        self.assertEqual((oi[0].name, oi[0].cost), ("futures_data_5m", 1.0))


class GlobalBinanceCoordinatorTests(unittest.TestCase):
    def test_separate_instances_share_one_token_ledger(self) -> None:
        with TemporaryDirectory() as tmp:
            now = [100.0]
            kwargs = {
                "limiter_enabled": True,
                "shared_cache_enabled": True,
                "futures_weight_per_minute": 10,
                "spot_weight_per_minute": 10,
                "futures_data_requests_per_5m": 10,
                "max_wait_sec": 0,
                "clock": lambda: now[0],
            }
            first = GlobalBinanceCoordinator(Path(tmp) / "coord.db", **kwargs)
            second = GlobalBinanceCoordinator(Path(tmp) / "coord.db", **kwargs)
            url = "https://fapi.binance.com/fapi/v1/klines"

            for _index in range(5):
                self.assertTrue(first.acquire(url, {"limit": 300}))
            self.assertFalse(second.acquire(url, {"limit": 300}))

            now[0] += 12.0
            self.assertTrue(second.acquire(url, {"limit": 300}))
            diagnostics = first.diagnostics()["buckets"]["futures_weight_1m"]
            self.assertEqual(diagnostics["granted"], 6)
            self.assertGreaterEqual(diagnostics["denied"], 1)

    def test_shared_cache_is_visible_to_another_instance_and_expires(self) -> None:
        with TemporaryDirectory() as tmp:
            now = [100.0]
            path = Path(tmp) / "coord.db"
            first = GlobalBinanceCoordinator(path, clock=lambda: now[0])
            second = GlobalBinanceCoordinator(path, clock=lambda: now[0])

            first.cache_put("ticker", {"symbol": "BTCUSDT"})
            self.assertEqual(
                second.cache_get("ticker", 20),
                {"symbol": "BTCUSDT"},
            )
            now[0] = 121.0
            self.assertIsNone(second.cache_get("ticker", 20))

    def test_observed_binance_weight_tightens_other_process_capacity(self) -> None:
        with TemporaryDirectory() as tmp:
            now = [100.0]
            kwargs = {
                "futures_weight_per_minute": 10,
                "spot_weight_per_minute": 10,
                "futures_data_requests_per_5m": 10,
                "max_wait_sec": 0,
                "clock": lambda: now[0],
            }
            first = GlobalBinanceCoordinator(Path(tmp) / "coord.db", **kwargs)
            second = GlobalBinanceCoordinator(Path(tmp) / "coord.db", **kwargs)
            url = "https://fapi.binance.com/fapi/v1/klines"

            self.assertTrue(first.acquire(url, {"limit": 300}))
            first.observe_response(
                url,
                {"limit": 300},
                {"X-MBX-USED-WEIGHT-1M": "9"},
            )

            self.assertFalse(second.acquire(url, {"limit": 300}))


class SharedHttpCacheTests(unittest.TestCase):
    def test_two_http_clients_reuse_public_binance_payload(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), http_cache_ttl_sec=10)
            first_session = Mock()
            first_session.get.return_value.status_code = 200
            first_session.get.return_value.headers = {}
            first_session.get.return_value.json.return_value = [{"symbol": "BTCUSDT"}]
            second_session = Mock()
            first = HttpClient(settings, DataQuality(), session=first_session)
            second = HttpClient(settings, DataQuality(), session=second_session)

            payload = first.get_json(
                "https://fapi.binance.com/fapi/v1/ticker/24hr",
                cache_key="fapi:ticker24hr",
                quality_key="ticker24hr",
            )
            cached = second.get_json(
                "https://fapi.binance.com/fapi/v1/ticker/24hr",
                cache_key="fapi:ticker24hr",
                quality_key="ticker24hr",
            )

        self.assertEqual(payload, cached)
        first_session.get.assert_called_once()
        second_session.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
