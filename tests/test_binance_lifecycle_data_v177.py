from __future__ import annotations

import threading
import time
import unittest
from collections import Counter
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable

import requests

from paopao_radar.binance_lifecycle_data import (
    MAX_SPOT_AGG_TRADES,
    BinanceLifecycleDataClient,
    futures_cvd_from_taker_rows,
    kline_snapshot,
)
from paopao_radar.config import Settings
from paopao_radar.data_sources import BinanceDataSource, DataQuality, HttpClient


class FakeResponse:
    def __init__(self, status_code: int, payload: Any = None):
        self.status_code = status_code
        self.payload = payload

    def json(self) -> Any:
        return self.payload


class FakeSession:
    def __init__(self, post_payload: Any = None):
        self.post_payload = post_payload if post_payload is not None else []
        self.post_calls: list[dict[str, Any]] = []

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.post_calls.append({"url": url, **kwargs})
        return FakeResponse(200, self.post_payload)

    def close(self) -> None:
        return None


class FakeHttp:
    def __init__(self, responder: Callable[[str, dict[str, Any]], Any], post_payload: Any = None):
        self.responder = responder
        self.calls: list[dict[str, Any]] = []
        self.session = FakeSession(post_payload)

    def get_json(self, url: str, params: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        normalized = dict(params or {})
        self.calls.append({"url": url, "params": normalized, **kwargs})
        return self.responder(url, normalized)

    def close(self) -> None:
        return None


class FakeSource:
    def __init__(self, http: FakeHttp):
        self.http = http

    @staticmethod
    def endpoint(path: str) -> str:
        return f"https://fapi.binance.test{path}"

    @staticmethod
    def spot_endpoint(path: str) -> str:
        return f"https://spot.binance.test{path}"

    @staticmethod
    def coinpaprika_market_caps() -> dict[str, float]:
        return {"BTC": 1_000_000.0}

    @staticmethod
    def market_caps() -> dict[str, float]:
        return {}


def settings_for(path: str, **kwargs: Any) -> Settings:
    values: dict[str, Any] = {
        "data_dir": Path(path),
        "lifecycle_db_path": Path(path) / "lifecycle.db",
        "lifecycle_binance_cache_ttl_sec": 300,
        "lifecycle_http_timeout_sec": 3,
        "lifecycle_active_max_symbols": 80,
        "http_retry": 1,
        "http_cache_enable": True,
        "http_cache_ttl_sec": 10,
    }
    values.update(kwargs)
    return Settings(**values)


def complete_responder(url: str, params: dict[str, Any]) -> Any:
    if "/fapi/v1/klines" in url:
        return [[0, "100", "110", "90", "105", "12", 999, "1260"]]
    if "/api/v3/klines" in url:
        return [[0, "99", "109", "89", "104", "15", 998, "1560"]]
    if "/fapi/v1/openInterest" in url:
        return {"openInterest": "20", "time": 123456}
    if "openInterestHist" in url:
        return [
            {"sumOpenInterest": "18", "sumOpenInterestValue": "1890", "timestamp": 100},
            {"sumOpenInterest": "20", "sumOpenInterestValue": "2100", "timestamp": 200},
        ]
    if "takerlongshortRatio" in url:
        return [{"buyVol": "8", "sellVol": "4", "timestamp": 200}]
    if "/api/v3/aggTrades" in url:
        return [
            {"p": "10", "q": "2", "m": False, "T": 150},
            {"p": "10", "q": "1", "m": True, "T": 160},
        ]
    if "/fapi/v1/fundingRate" in url:
        return [{"fundingRate": "0.0001", "fundingTime": 123, "markPrice": "105"}]
    if "okx.com/api/v5/market/ticker" in url:
        return {"data": [{"last": "106"}]}
    if "okx.com/api/v5/public/funding-rate" in url:
        return {"data": [{"fundingRate": "0.0002"}]}
    if "bybit.com/v5/market/tickers" in url:
        return {"result": {"list": [{"lastPrice": "103", "fundingRate": "0.00005"}]}}
    return None


class BinanceLifecycleDataV177Tests(unittest.TestCase):
    def test_kline_has_complete_ohlcv_fields(self) -> None:
        result = kline_snapshot([[0, "100", "110", "90", "105", "12", 999, "1260"]])

        self.assertEqual(result["open"], 100.0)
        self.assertEqual(result["high"], 110.0)
        self.assertEqual(result["low"], 90.0)
        self.assertEqual(result["close"], 105.0)
        self.assertEqual(result["price"], 105.0)
        self.assertEqual(result["volume"], 12.0)
        self.assertEqual(result["quote_volume"], 1260.0)
        self.assertEqual(result["close_time"], 999)

    def test_snapshot_collects_futures_spot_oi_cvd_funding_and_side_context(self) -> None:
        hyperliquid = [
            {"universe": [{"name": "BTC"}]},
            [{"markPx": "107", "funding": "0.0003"}],
        ]
        with TemporaryDirectory() as tmp:
            http = FakeHttp(complete_responder, hyperliquid)
            client = BinanceLifecycleDataClient(settings_for(tmp), source=FakeSource(http))  # type: ignore[arg-type]
            snapshot = client.snapshot("BTCUSDT", "15m")

        self.assertEqual(snapshot["open"], 100.0)
        self.assertEqual(snapshot["close"], 105.0)
        self.assertEqual(snapshot["spot_price"], 104.0)
        self.assertEqual(snapshot["spot_kline"]["open"], 99.0)
        self.assertEqual(snapshot["openInterest"], 20.0)
        self.assertEqual(snapshot["open_interest_time"], 123456)
        self.assertEqual(snapshot["timestamp"], 123456)
        self.assertEqual(snapshot["time"], 123456)
        self.assertEqual(snapshot["sum_open_interest"], 20.0)
        self.assertEqual(snapshot["sum_open_interest_value_usdt"], 2100.0)
        self.assertEqual(snapshot["sumOpenInterest"], 20.0)
        self.assertEqual(snapshot["sumOpenInterestValue"], 2100.0)
        self.assertEqual(snapshot["futures_cvd_delta"], 4.0)
        self.assertEqual(snapshot["spot_cvd_delta"], 10.0)
        self.assertEqual(snapshot["funding_rate"], 0.0001)
        self.assertEqual(snapshot["fundingRate"], 0.0001)
        self.assertEqual(snapshot["fundingTime"], 123)
        self.assertEqual(snapshot["markPrice"], 105.0)

        items = snapshot["exchange_context"]["items"]
        self.assertEqual([item["exchange"] for item in items], ["OKX", "Bybit", "Hyperliquid"])
        self.assertAlmostEqual(items[0]["price_deviation_vs_binance_pct"], (106 - 105) / 105 * 100, places=6)
        self.assertEqual(items[0]["funding_deviation_vs_binance"], 0.0001)
        allowed = {
            "exchange",
            "current_price",
            "funding_rate",
            "price_deviation_vs_binance_pct",
            "funding_deviation_vs_binance",
            "status",
        }
        for item in items:
            self.assertEqual(set(item), allowed)
            self.assertFalse({"oi", "open_interest", "cvd", "score"} & set(item))

    def test_24h_oi_history_falls_back_from_1d_to_six_4h_rows(self) -> None:
        periods: list[tuple[str, int]] = []

        def responder(url: str, params: dict[str, Any]) -> Any:
            if "openInterestHist" not in url:
                return None
            periods.append((str(params["period"]), int(params["limit"])))
            if params["period"] == "1d":
                return []
            if params["period"] == "4h":
                return [
                    {
                        "sumOpenInterest": str(10 + index),
                        "sumOpenInterestValue": str(1000 + index),
                        "timestamp": index,
                    }
                    for index in range(6)
                ]
            raise AssertionError("1h fallback should not run after a successful 4h response")

        with TemporaryDirectory() as tmp:
            client = BinanceLifecycleDataClient(settings_for(tmp), source=FakeSource(FakeHttp(responder)))  # type: ignore[arg-type]
            result = client.historical_open_interest("BTCUSDT", "24h")

        self.assertEqual(periods, [("1d", 2), ("4h", 6)])
        self.assertEqual(result["oi_history_source_period"], "4h")
        self.assertEqual(result["oi_history_aggregation"], "6x4h")
        self.assertEqual(result["oi_history_sample_count"], 6)
        self.assertEqual(result["sum_open_interest"], 15.0)

    def test_24h_oi_history_uses_24x1h_as_final_fallback(self) -> None:
        periods: list[tuple[str, int]] = []

        def responder(url: str, params: dict[str, Any]) -> Any:
            if "openInterestHist" not in url:
                return None
            periods.append((str(params["period"]), int(params["limit"])))
            if params["period"] in {"1d", "4h"}:
                return []
            return [{"sumOpenInterest": "21", "sumOpenInterestValue": "2200", "timestamp": 300}]

        with TemporaryDirectory() as tmp:
            client = BinanceLifecycleDataClient(settings_for(tmp), source=FakeSource(FakeHttp(responder)))  # type: ignore[arg-type]
            result = client.historical_open_interest("BTCUSDT", "24h")

        self.assertEqual(periods, [("1d", 2), ("4h", 6), ("1h", 24)])
        self.assertEqual(result["oi_history_source_period"], "1h")
        self.assertEqual(result["oi_history_aggregation"], "24x1h")

    def test_futures_taker_uses_one_period_and_zero_sell_has_finite_tiny_ratio(self) -> None:
        calls: list[dict[str, Any]] = []

        def responder(url: str, params: dict[str, Any]) -> Any:
            calls.append(params)
            return [{"buyVol": "2", "sellVol": "0"}]

        with TemporaryDirectory() as tmp:
            client = BinanceLifecycleDataClient(settings_for(tmp), source=FakeSource(FakeHttp(responder)))  # type: ignore[arg-type]
            result = client.futures_taker_ratio("BTCUSDT", "1h")

        self.assertEqual(calls[0]["limit"], 1)
        self.assertEqual(calls[0]["period"], "1h")
        self.assertEqual(result["futures_cvd_delta"], 2.0)
        self.assertEqual(result["futures_cvd_ratio"], 2_000_000_000_000.0)
        self.assertEqual(futures_cvd_from_taker_rows([{"buyVol": "1", "sellVol": "0"}])["sample_count"], 1)

    def test_spot_cvd_marks_sample_and_truncation_without_pagination(self) -> None:
        rows = [
            {"p": "1", "q": "1", "m": bool(index % 2), "T": index}
            for index in range(MAX_SPOT_AGG_TRADES)
        ]
        http = FakeHttp(lambda _url, _params: rows)
        with TemporaryDirectory() as tmp:
            client = BinanceLifecycleDataClient(settings_for(tmp), source=FakeSource(http))  # type: ignore[arg-type]
            result = client.spot_cvd("BTCUSDT", "4h")

        self.assertTrue(result["spot_cvd_sampled"])
        self.assertTrue(result["spot_cvd_truncated"])
        self.assertEqual(result["spot_cvd_trade_count"], MAX_SPOT_AGG_TRADES)
        self.assertEqual(result["spot_cvd_sample_start_ms"], 0)
        self.assertEqual(result["spot_cvd_sample_end_ms"], MAX_SPOT_AGG_TRADES - 1)
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(http.calls[0]["params"]["limit"], MAX_SPOT_AGG_TRADES)

    def test_funding_uses_shared_http_without_legacy_budget(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = settings_for(tmp, funding_history_budget=0)
            source = BinanceDataSource(settings)
            http = FakeHttp(lambda _url, _params: [{"fundingRate": "0.0002", "fundingTime": 10, "markPrice": "100"}])
            source.http = http  # type: ignore[assignment]
            client = BinanceLifecycleDataClient(settings, source=source)
            result = client.funding("BTCUSDT")

        self.assertEqual(result["funding_rate"], 0.0002)
        self.assertEqual(source.budget.used.get("funding_history", 0), 0)
        self.assertEqual(http.calls[0]["timeout"], 3)

    def test_spot_component_failure_does_not_block_binance_core(self) -> None:
        def responder(url: str, params: dict[str, Any]) -> Any:
            if "spot.binance.test" in url:
                raise requests.Timeout("spot timeout")
            return complete_responder(url, params)

        with TemporaryDirectory() as tmp:
            client = BinanceLifecycleDataClient(settings_for(tmp), source=FakeSource(FakeHttp(responder)))  # type: ignore[arg-type]
            result = client.snapshot("BTCUSDT", "15m")

        self.assertEqual(result["data_source_status"], "ok")
        self.assertEqual(result["price"], 105.0)
        self.assertEqual(result["oi"], 20.0)
        self.assertEqual(result["funding_rate"], 0.0001)
        self.assertEqual(result["spot_price_status"], "unavailable")
        self.assertEqual(result["spot_cvd_status"], "unavailable")

    def test_real_http_418_429_and_timeout_degrade_to_unavailable(self) -> None:
        class StatusSession:
            def __init__(self, result: int | BaseException):
                self.result = result

            def get(self, *_args: Any, **_kwargs: Any) -> FakeResponse:
                if isinstance(self.result, BaseException):
                    raise self.result
                return FakeResponse(self.result, {})

            def close(self) -> None:
                return None

        with TemporaryDirectory() as tmp:
            for result in (418, 429, requests.Timeout("timeout")):
                with self.subTest(result=result):
                    settings = settings_for(tmp)
                    source = BinanceDataSource(settings)
                    source.http = HttpClient(settings, DataQuality(), session=StatusSession(result))  # type: ignore[arg-type]
                    client = BinanceLifecycleDataClient(settings, source=source)
                    value = client.current_open_interest("BTCUSDT")
                    self.assertEqual(value["oi_status"], "unavailable")

    def test_unavailable_results_use_short_cache_ttl(self) -> None:
        with TemporaryDirectory() as tmp:
            client = BinanceLifecycleDataClient(settings_for(tmp), source=FakeSource(FakeHttp(lambda _u, _p: None)))  # type: ignore[arg-type]
            client._store("failure", {"data_source_status": "unavailable"})
            client._store("success", {"data_source_status": "ok"})

        self.assertLessEqual(client.cache["failure"][1], 30)
        self.assertEqual(client.cache["success"][1], 300)

    def test_snapshot_many_deduplicates_pairs_and_has_bounded_worker_count(self) -> None:
        with TemporaryDirectory() as tmp:
            client = BinanceLifecycleDataClient(settings_for(tmp), source=FakeSource(FakeHttp(lambda _u, _p: None)))  # type: ignore[arg-type]
            lock = threading.Lock()
            active = 0
            peak = 0
            calls: Counter[tuple[str, str]] = Counter()

            def slow_snapshot(symbol: str, timeframe: str = "15m") -> dict[str, Any]:
                nonlocal active, peak
                pair = (symbol, timeframe)
                with lock:
                    calls[pair] += 1
                    active += 1
                    peak = max(peak, active)
                time.sleep(0.015)
                with lock:
                    active -= 1
                return {"symbol": symbol, "timeframe": timeframe, "data_source_status": "ok"}

            client.snapshot = slow_snapshot  # type: ignore[method-assign]
            unique = [(f"COIN{index}USDT", "15m") for index in range(12)]
            result = client.snapshot_many(unique + unique[:5], max_workers=4)

        self.assertEqual(len(result), 12)
        self.assertTrue(all(count == 1 for count in calls.values()))
        self.assertGreater(peak, 1)
        self.assertLessEqual(peak, 4)
        self.assertEqual(client.last_batch_metrics["max_workers"], 4)
        self.assertEqual(client.last_batch_metrics["peak_concurrency"], peak)


if __name__ == "__main__":
    unittest.main()
