from __future__ import annotations

import asyncio
import os
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from paopao_radar.config import Settings
from paopao_radar.data_sources import DataQuality, HttpClient
from paopao_radar.funding_sources import MultiExchangeFundingClient, funding_interval_transition


CST = timezone(timedelta(hours=8))


def ms_at(hour: int) -> int:
    return int(datetime(2026, 7, 1, hour, 0, 0, tzinfo=CST).timestamp() * 1000)


class FakeHttp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get_json(self, url: str, params=None, **_kwargs):  # type: ignore[no-untyped-def]
        params = dict(params or {})
        self.calls.append((url, params))
        if "premiumIndex" in url:
            return {"symbol": "BTCUSDT", "lastFundingRate": "-0.0200", "nextFundingTime": ms_at(17)}
        if "fapi/v1/fundingRate" in url:
            return [
                {"fundingTime": ms_at(8), "fundingRate": "-0.001"},
                {"fundingTime": ms_at(12), "fundingRate": "-0.002"},
                {"fundingTime": ms_at(16), "fundingRate": "-0.004"},
            ]
        if "okx.com" in url and "funding-rate-history" not in url:
            return {
                "data": [{
                    "instId": "BTC-USDT-SWAP",
                    "fundingRate": "-0.0100",
                    "prevFundingTime": str(ms_at(16)),
                    "fundingTime": str(ms_at(17)),
                }]
            }
        if "okx.com" in url:
            return {"data": [{"fundingTime": str(ms_at(16)), "fundingRate": "-0.004"}]}
        if "bybit.com" in url and "tickers" in url:
            return {
                "result": {
                    "list": [{
                        "symbol": "BTCUSDT",
                        "fundingRate": "-0.006",
                        "nextFundingTime": str(ms_at(17)),
                        "fundingIntervalHour": "1",
                    }]
                }
            }
        if "bybit.com" in url:
            return {
                "result": {
                    "list": [
                        {"fundingRateTimestamp": str(ms_at(16)), "fundingRate": "-0.004"},
                        {"fundingRateTimestamp": str(ms_at(12)), "fundingRate": "-0.002"},
                        {"fundingRateTimestamp": str(ms_at(8)), "fundingRate": "-0.001"},
                    ]
                }
            }
        if "current-fund-rate" in url:
            return {
                "data": [{
                    "symbol": "BTCUSDT",
                    "fundingRate": "-0.005",
                    "fundingRateInterval": "1",
                    "nextUpdate": str(ms_at(17)),
                }]
            }
        if "history-fund-rate" in url:
            return {"data": [{"fundingTime": str(ms_at(16)), "fundingRate": "-0.004"}]}
        if "contracts/BTC_USDT" in url:
            return {
                "name": "BTC_USDT",
                "funding_rate": "-0.003",
                "funding_interval": 3600,
                "funding_next_apply": int(ms_at(17) / 1000),
            }
        if "funding_rate" in url:
            return [{"t": int(ms_at(16) / 1000), "r": "-0.004"}]
        return {}


class BatchHttp:
    def __init__(self, delay_sec: float = 0.01, fail_exchange: str = "") -> None:
        self.delay_sec = delay_sec
        self.fail_exchange = fail_exchange
        self.lock = threading.Lock()
        self.active = 0
        self.peak_active = 0
        self.calls = 0
        self.timeouts: list[float] = []

    @staticmethod
    def _exchange(url: str) -> str:
        if "binance" in url or "premiumIndex" in url:
            return "BINANCE"
        if "okx.com" in url:
            return "OKX"
        if "bybit.com" in url:
            return "BYBIT"
        if "bitget.com" in url:
            return "BITGET"
        return "GATE"

    def get_json(self, url: str, params=None, **kwargs):  # type: ignore[no-untyped-def]
        params = dict(params or {})
        exchange = self._exchange(url)
        with self.lock:
            self.calls += 1
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)
            self.timeouts.append(float(kwargs.get("timeout", 0)))
        try:
            time.sleep(self.delay_sec)
            if exchange == self.fail_exchange:
                if exchange == "OKX":
                    raise TimeoutError("simulated exchange timeout")
                raise RuntimeError("simulated exchange failure")
            if exchange == "BINANCE":
                symbol = str(params.get("symbol") or "BTCUSDT")
                return {"symbol": symbol, "lastFundingRate": "0.0001", "nextFundingTime": ms_at(17)}
            if exchange == "OKX":
                return {"data": [{"fundingRate": "0.0001", "fundingTime": str(ms_at(17))}]}
            if exchange == "BYBIT":
                return {"result": {"list": [{"fundingRate": "0.0001", "nextFundingTime": str(ms_at(17))}]}}
            if exchange == "BITGET":
                return {"data": [{"fundingRate": "0.0001", "nextUpdate": str(ms_at(17))}]}
            return {"funding_rate": "0.0001", "funding_interval": 3600, "funding_next_apply": int(ms_at(17) / 1000)}
        finally:
            with self.lock:
                self.active -= 1


class CacheResponse:
    status_code = 200

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol

    def json(self) -> dict[str, object]:
        return {"symbol": self.symbol, "lastFundingRate": "0.0001", "nextFundingTime": ms_at(17)}


class CacheSession:
    def __init__(self) -> None:
        self.calls = 0

    def get(self, _url: str, params=None, **_kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        return CacheResponse(str((params or {}).get("symbol") or "BTCUSDT"))


class FundingSourceTests(unittest.TestCase):
    def test_settings_load_funding_performance_controls(self) -> None:
        with patch.dict(os.environ, {
            "FUNDING_SCAN_CONCURRENCY": "7",
            "FUNDING_REQUEST_TIMEOUT_SEC": "6",
            "FUNDING_MAX_SYMBOLS_PER_BATCH": "99",
        }):
            settings = Settings.load()

        self.assertEqual(settings.funding_scan_concurrency, 7)
        self.assertEqual(settings.funding_request_timeout_sec, 6)
        self.assertEqual(settings.funding_max_symbols_per_batch, 99)

    def test_transition_uses_next_settlement_time(self) -> None:
        transition = funding_interval_transition(
            [
                {"time_ms": ms_at(8), "rate_pct": -0.1},
                {"time_ms": ms_at(12), "rate_pct": -0.2},
                {"time_ms": ms_at(16), "rate_pct": -0.4},
            ],
            next_time_ms=ms_at(17),
        )

        self.assertEqual(transition["previous_interval_hours"], 4)
        self.assertEqual(transition["current_interval_hours"], 1)
        self.assertIn("2026-07-01 16:00:00 4H结算一次", transition["transition_text"])
        self.assertIn("2026-07-01 17:00:00 1H结算一次", transition["transition_text"])

    def test_snapshot_normalizes_five_exchange_funding(self) -> None:
        settings = Settings(
            data_dir=Path("."),
            launch_funding_exchanges=("BINANCE", "OKX", "BYBIT", "BITGET", "GATE"),
            launch_funding_history_limit=3,
        )
        rows = MultiExchangeFundingClient(settings, FakeHttp()).snapshot("BTCUSDT")  # type: ignore[arg-type]

        self.assertEqual([row["exchange"] for row in rows], ["Binance", "OKX", "Bybit", "Bitget", "Gate"])
        self.assertEqual(rows[0]["funding_pct"], -2.0)
        self.assertEqual(rows[0]["interval_hours"], 1)
        self.assertEqual(rows[0]["last_funding_time"], "2026-07-01 16:00:00")
        self.assertEqual(rows[0]["current_interval_hours"], 1)
        self.assertEqual(rows[0]["previous_interval_hours"], 4)
        self.assertEqual(rows[0]["extreme_label"], "极负")
        self.assertIn("4H结算一次", rows[0]["funding_interval_transition"])
        self.assertTrue(all(row["next_funding_time"].endswith("17:00:00") for row in rows))

    def test_snapshot_many_uses_bounded_workers(self) -> None:
        http = BatchHttp(delay_sec=0.02)
        settings = Settings(
            data_dir=Path("."),
            launch_funding_exchanges=("BINANCE", "OKX", "BYBIT", "BITGET", "GATE"),
            funding_scan_concurrency=6,
            funding_max_symbols_per_batch=120,
        )
        client = MultiExchangeFundingClient(settings, http)  # type: ignore[arg-type]

        result = client.snapshot_many(
            ["BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT"],
            include_history=False,
        )

        self.assertEqual(len(result), 4)
        self.assertEqual(http.calls, 20)
        self.assertGreater(http.peak_active, 1)
        self.assertLessEqual(http.peak_active, 6)
        self.assertGreaterEqual(client.last_batch_metrics["peak_concurrency"], http.peak_active)
        self.assertLessEqual(client.last_batch_metrics["peak_concurrency"], 6)
        self.assertEqual(client.last_batch_metrics["exchange_requests"], 20)
        self.assertEqual(client.last_batch_metrics["succeeded"], 20)

    def test_snapshot_many_caps_symbol_batch(self) -> None:
        http = BatchHttp(delay_sec=0)
        settings = Settings(
            data_dir=Path("."),
            launch_funding_exchanges=("BINANCE",),
            funding_max_symbols_per_batch=2,
        )
        client = MultiExchangeFundingClient(settings, http)  # type: ignore[arg-type]

        result = client.snapshot_many(["BTCUSDT", "ETHUSDT", "SOLUSDT"], include_history=False)

        self.assertEqual(list(result), ["BTCUSDT", "ETHUSDT"])
        self.assertEqual(http.calls, 2)

    def test_timeout_fallback_keeps_other_exchange_result(self) -> None:
        http = BatchHttp(delay_sec=0, fail_exchange="OKX")
        settings = Settings(
            data_dir=Path("."),
            launch_funding_exchanges=("BINANCE", "OKX"),
            funding_request_timeout_sec=3,
        )
        client = MultiExchangeFundingClient(settings, http)  # type: ignore[arg-type]

        rows = client.snapshot("BTCUSDT", include_history=False)

        self.assertEqual([row["exchange"] for row in rows], ["Binance"])
        self.assertEqual(len(http.timeouts), 2)
        self.assertTrue(all(2.9 <= timeout <= 3.0 for timeout in http.timeouts))
        self.assertEqual(client.last_batch_metrics["succeeded"], 1)
        self.assertEqual(client.last_batch_metrics["failed"], 1)

    def test_exchange_job_shares_one_timeout_budget_with_history(self) -> None:
        http = BatchHttp(delay_sec=0.07)
        settings = Settings(
            data_dir=Path("."),
            launch_funding_exchanges=("BINANCE",),
            funding_request_timeout_sec=0.1,  # type: ignore[arg-type]
        )
        client = MultiExchangeFundingClient(settings, http)  # type: ignore[arg-type]

        rows = client.snapshot("BTCUSDT", include_history=True)

        self.assertEqual([row["exchange"] for row in rows], ["Binance"])
        self.assertEqual(http.calls, 1)
        self.assertEqual(len(http.timeouts), 1)
        self.assertLessEqual(http.timeouts[0], 0.1)

    def test_exchange_failure_is_isolated(self) -> None:
        http = BatchHttp(delay_sec=0, fail_exchange="BYBIT")
        settings = Settings(
            data_dir=Path("."),
            launch_funding_exchanges=("BINANCE", "OKX", "BYBIT"),
        )

        rows = MultiExchangeFundingClient(settings, http).snapshot("BTCUSDT", include_history=False)  # type: ignore[arg-type]

        self.assertEqual([row["exchange"] for row in rows], ["Binance", "OKX"])

    def test_snapshot_batch_reuses_http_cache(self) -> None:
        settings = Settings(
            data_dir=Path("."),
            launch_funding_exchanges=("BINANCE",),
            http_cache_enable=True,
            http_cache_ttl_sec=60,
        )
        session = CacheSession()
        http = HttpClient(settings, DataQuality(), session=session)  # type: ignore[arg-type]
        client = MultiExchangeFundingClient(settings, http)

        first = client.snapshot_many(["BTCUSDT", "BTCUSDT"], include_history=False)
        second = client.snapshot("BTCUSDT", include_history=False)

        self.assertEqual(list(first), ["BTCUSDT"])
        self.assertEqual(len(second), 1)
        self.assertEqual(session.calls, 1)

    def test_sync_snapshot_remains_usable_inside_event_loop(self) -> None:
        settings = Settings(
            data_dir=Path("."),
            launch_funding_exchanges=("BINANCE",),
        )
        client = MultiExchangeFundingClient(settings, BatchHttp(delay_sec=0))  # type: ignore[arg-type]

        async def call_sync_api() -> list[dict[str, object]]:
            return client.snapshot("BTCUSDT", include_history=False)

        rows = asyncio.run(call_sync_api())

        self.assertEqual([row["exchange"] for row in rows], ["Binance"])

    def test_sync_batch_inside_event_loop_points_to_async_api(self) -> None:
        settings = Settings(
            data_dir=Path("."),
            launch_funding_exchanges=("BINANCE",),
        )
        client = MultiExchangeFundingClient(settings, BatchHttp(delay_sec=0))  # type: ignore[arg-type]

        async def call_sync_batch() -> None:
            with self.assertRaisesRegex(RuntimeError, "snapshot_many_async"):
                client.snapshot_many(["BTCUSDT"], include_history=False)

        asyncio.run(call_sync_batch())


if __name__ == "__main__":
    unittest.main()
