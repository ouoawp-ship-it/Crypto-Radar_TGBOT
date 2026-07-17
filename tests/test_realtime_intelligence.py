from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from paopao_radar.realtime_intelligence import (
    build_realtime_intelligence,
    build_realtime_intelligence_radar_boards,
)
from paopao_radar.realtime_market import RealtimeFeatureStore
from paopao_radar.web_services.public import (
    public_radar_boards_payload,
    public_realtime_intelligence_payload,
)


def feature_row(
    symbol: str,
    minute: int,
    *,
    buy: float,
    sell: float,
    open_price: float,
    close_price: float,
    long_liquidation: float = 0,
    short_liquidation: float = 0,
) -> dict[str, object]:
    return {
        "exchange": "binance",
        "market": "futures",
        "symbol": symbol,
        "bucket_start": minute * 60,
        "bucket_sec": 60,
        "trade_buy_usd": buy,
        "trade_sell_usd": sell,
        "cvd_usd": buy - sell,
        "trade_count": 10,
        "price_open": open_price,
        "price_high": max(open_price, close_price),
        "price_low": min(open_price, close_price),
        "price_close": close_price,
        "long_liquidation_usd": long_liquidation,
        "short_liquidation_usd": short_liquidation,
        "liquidation_count": int(bool(long_liquidation or short_liquidation)),
        "last_event_ms": (minute * 60 + 59) * 1000,
    }


class RealtimeIntelligenceTests(unittest.TestCase):
    def test_multi_exchange_flow_uses_unique_time_coverage_and_one_price_source(self) -> None:
        rows: list[dict[str, object]] = []
        for minute in range(10):
            binance = feature_row(
                "BTCUSDT", minute, buy=1_000, sell=500,
                open_price=100 + minute, close_price=101 + minute,
            )
            bybit = feature_row(
                "BTCUSDT", minute, buy=500, sell=250,
                open_price=200 + minute, close_price=201 + minute,
            )
            bybit["exchange"] = "bybit"
            rows.extend([binance, bybit])

        payload = build_realtime_intelligence(rows, now_ts=600, limit=10)
        window = payload["items"][0]["windows"]["5m"]

        self.assertEqual(window["coverage_ratio"], 1)
        self.assertEqual(window["time_bucket_count"], 5)
        self.assertEqual(window["exchanges"], ["binance", "bybit"])
        self.assertEqual(window["price_source_exchange"], "binance")
        self.assertEqual(window["price_open"], 105)
        self.assertEqual(window["price_close"], 110)

    def test_builds_surge_ambush_directional_resonance_and_rankings(self) -> None:
        rows: list[dict[str, object]] = []
        for minute in range(20):
            btc_current = minute >= 15
            rows.append(feature_row(
                "BTCUSDT", minute,
                buy=2_000 if btc_current else 700,
                sell=500 if btc_current else 800,
                open_price=100 + max(0, minute - 15) * 0.2,
                close_price=100 + max(0, minute - 14) * 0.2,
                short_liquidation=200 if btc_current else 0,
            ))
            eth_current = minute >= 15
            rows.append(feature_row(
                "ETHUSDT", minute,
                buy=400 if eth_current else 800,
                sell=1_800 if eth_current else 700,
                open_price=50 - max(0, minute - 15) * 0.1,
                close_price=50 - max(0, minute - 14) * 0.1,
                long_liquidation=100 if eth_current else 0,
            ))
            rows.append(feature_row(
                "SOLUSDT", minute,
                buy=1_200,
                sell=800,
                open_price=10,
                close_price=10.01,
            ))

        payload = build_realtime_intelligence(rows, now_ts=1_200, limit=10)
        by_symbol = {item["symbol"]: item for item in payload["items"]}

        self.assertEqual(payload["data_status"], "ready")
        self.assertTrue(by_symbol["BTCUSDT"]["surge"]["triggered"])
        self.assertEqual(by_symbol["BTCUSDT"]["surge"]["direction"], "long")
        self.assertTrue(by_symbol["ETHUSDT"]["surge"]["triggered"])
        self.assertEqual(by_symbol["ETHUSDT"]["surge"]["direction"], "short")
        self.assertTrue(by_symbol["SOLUSDT"]["ambush"]["triggered"])
        self.assertEqual(by_symbol["SOLUSDT"]["ambush"]["direction"], "long")
        self.assertEqual(by_symbol["BTCUSDT"]["resonance"]["direction"], "long")
        self.assertGreaterEqual(by_symbol["BTCUSDT"]["resonance"]["active_count"], 2)
        self.assertTrue(by_symbol["BTCUSDT"]["rankings"]["market_strength"]["available"])
        self.assertTrue(payload["boards"][0]["items"])
        self.assertTrue(payload["boards"][1]["items"])
        radar_boards = build_realtime_intelligence_radar_boards(payload, limit=10)
        self.assertEqual([board["key"] for board in radar_boards], ["realtime_surge", "realtime_ambush"])
        self.assertTrue(radar_boards[0]["positive"]["items"])
        self.assertTrue(radar_boards[0]["negative"]["items"])

    def test_backtest_reports_small_samples_as_insufficient(self) -> None:
        rows: list[dict[str, object]] = []
        price = 100.0
        for minute in range(40):
            accelerating = 10 <= minute < 15
            if minute >= 15:
                price += 0.2
            rows.append(feature_row(
                "BTCUSDT", minute,
                buy=2_000 if accelerating else 800,
                sell=400 if accelerating else 800,
                open_price=price,
                close_price=price + (0.05 if accelerating else 0),
            ))

        payload = build_realtime_intelligence(rows, now_ts=2_400, limit=10, include_backtest=True)
        backtest = payload["backtest"]

        self.assertEqual(backtest["status"], "insufficient")
        self.assertGreaterEqual(backtest["horizons"]["5m"]["sample_size"], 1)
        self.assertLess(backtest["horizons"]["5m"]["sample_size"], backtest["minimum_sample_size"])
        self.assertIn("不构成", backtest["disclaimer"])

    def test_public_endpoint_reads_persisted_features_and_keeps_backtest_explicit(self) -> None:
        rows = [
            feature_row(
                "BTCUSDT", minute,
                buy=2_000 if minute >= 15 else 700,
                sell=500 if minute >= 15 else 800,
                open_price=100,
                close_price=100 + max(0, minute - 14) * 0.2,
            )
            for minute in range(20)
        ]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "realtime.db"
            RealtimeFeatureStore(path).replace_many(rows)
            payload = public_realtime_intelligence_payload(
                settings=SimpleNamespace(realtime_features_db_path=path),
                now_ts=1_200,
                include_backtest=True,
            )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["data_status"], "ready")
        self.assertTrue(payload["data"]["items"][0]["surge"]["triggered"])
        self.assertEqual(payload["data"]["backtest"]["status"], "insufficient")

    def test_radar_boards_append_realtime_intelligence_when_windows_are_ready(self) -> None:
        rows = [
            feature_row(
                "BTCUSDT", minute,
                buy=2_000 if minute >= 15 else 700,
                sell=500 if minute >= 15 else 800,
                open_price=100,
                close_price=100 + max(0, minute - 14) * 0.2,
            )
            for minute in range(20)
        ]
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "realtime.db"
            RealtimeFeatureStore(path).replace_many(rows)
            settings = SimpleNamespace(cockpit_v2_mode="enabled", realtime_features_db_path=path)
            cockpit = {
                "schema_version": "test", "generated_at": "", "window_sec": 3600,
                "data_status": "ready", "warnings": [], "coverage": {"assets": 1},
                "readiness": {}, "boards": [{"key": "price"}], "methodology": {},
            }
            with patch("paopao_radar.web_services.public._market_cockpit_raw", return_value=cockpit):
                payload = public_radar_boards_payload(settings=settings, now_ts=1_200)

        keys = [board["key"] for board in payload["data"]["boards"]]
        self.assertIn("realtime_surge", keys)
        self.assertIn("realtime_ambush", keys)
        self.assertEqual(payload["data"]["coverage"]["realtime_intelligence"], 1)


if __name__ == "__main__":
    unittest.main()
