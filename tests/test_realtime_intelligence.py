from __future__ import annotations

import unittest

from paopao_radar.realtime_intelligence import (
    build_open_interest_anomaly_events,
    build_realtime_intelligence,
    build_realtime_intelligence_radar_boards,
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
    def test_builds_ranked_open_interest_events_from_snapshot_history(self) -> None:
        rows: list[dict[str, object]] = []
        for index in range(9):
            observed_at = (index + 1) * 900
            rows.append({
                "symbol": "BTCUSDT",
                "observed_at": observed_at,
                "oi_usd": 1_000_000 + index * 10_000 + (250_000 if index == 8 else 0),
            })
            rows.append({
                "symbol": "ETHUSDT",
                "observed_at": observed_at,
                "oi_usd": 800_000 - index * 8_000 - (180_000 if index == 8 else 0),
            })

        events = build_open_interest_anomaly_events(rows, now_ts=8_100, limit=20)

        self.assertTrue(events)
        self.assertNotIn("5m", {event["window"] for event in events})
        self.assertIn("oi_up", {event["event_type"] for event in events})
        self.assertIn("oi_down", {event["event_type"] for event in events})
        self.assertTrue(all(event["metric"] == "oi" for event in events))
        self.assertTrue(any(event["rankings"]["self"].get("available") for event in events))
        self.assertTrue(any(event["rankings"]["market_absolute"].get("available") for event in events))

    def test_builds_five_minute_oi_events_with_mercu_display_contract(self) -> None:
        rows: list[dict[str, object]] = []
        for index in range(12):
            observed_at = (index + 1) * 300
            rows.extend([
                {
                    "symbol": "BTCUSDT",
                    "observed_at": observed_at,
                    "oi_usd": 10_000_000 + index * 10_000 + (800_000 if index == 11 else 0),
                },
                {
                    "symbol": "ETHUSDT",
                    "observed_at": observed_at,
                    "oi_usd": 8_000_000 - index * 8_000 - (640_000 if index == 11 else 0),
                },
            ])

        events = build_open_interest_anomaly_events(rows, now_ts=3_600, limit=30)
        five_minute = [event for event in events if event["window"] == "5m"]

        self.assertEqual({event["label"] for event in five_minute}, {"OI 暴涨", "OI 暴跌"})
        self.assertTrue(all(event["detail"].startswith("5 分钟内 oi ") for event in five_minute))
        self.assertTrue(any("万 (+" in event["detail"] for event in five_minute))
        self.assertTrue(any("万 (-" in event["detail"] for event in five_minute))

    def test_volume_event_uses_mercu_label_and_displays_price_change(self) -> None:
        rows = [
            feature_row(
                "BTCUSDT",
                minute,
                buy=2_000 if minute >= 5 else 500,
                sell=1_000 if minute >= 5 else 500,
                open_price=100 if minute < 5 else 100 + (minute - 5) * 0.5,
                close_price=100 if minute < 5 else 100 + (minute - 4) * 0.5,
            )
            for minute in range(10)
        ]

        payload = build_realtime_intelligence(rows, now_ts=600, limit=10)
        event = next(item for item in payload["anomaly_events"] if item["event_type"] == "volume_spike")

        self.assertEqual(event["label"], "Vol 爆发")
        self.assertAlmostEqual(event["change_pct"], 2.5)
        self.assertEqual(event["detail"], "5 分钟内 成交量 2万 (+2.5%)")

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
        self.assertEqual(by_symbol["SOLUSDT"]["lifecycle"]["state"], "continuing")
        self.assertEqual(by_symbol["SOLUSDT"]["lifecycle"]["rule"], "ambush")
        self.assertEqual(by_symbol["SOLUSDT"]["lifecycle"]["direction"], "long")
        self.assertGreaterEqual(by_symbol["SOLUSDT"]["lifecycle"]["age_sec"], 600)
        self.assertEqual(by_symbol["BTCUSDT"]["resonance"]["direction"], "long")
        self.assertGreaterEqual(by_symbol["BTCUSDT"]["resonance"]["active_count"], 2)
        self.assertEqual(
            [window["key"] for window in by_symbol["BTCUSDT"]["resonance"]["windows"]],
            ["15m", "30m", "1h", "4h", "1d"],
        )
        self.assertGreaterEqual(by_symbol["BTCUSDT"]["anomaly_24h"]["count"], 1)
        self.assertTrue(by_symbol["BTCUSDT"]["rankings"]["market_strength"]["available"])
        self.assertTrue(payload["anomaly_events"])
        event_types = {event["event_type"] for event in payload["anomaly_events"]}
        self.assertIn("perp_inflow", event_types)
        self.assertIn("perp_outflow", event_types)
        ranked_events = [
            event for event in payload["anomaly_events"]
            if event["rankings"]["market_strength"].get("available")
        ]
        self.assertTrue(ranked_events)
        self.assertTrue(all("self" in event["rankings"] for event in payload["anomaly_events"]))
        self.assertTrue(payload["boards"][0]["items"])
        self.assertTrue(payload["boards"][1]["items"])
        self.assertTrue(payload["boards"][2]["items"])
        radar_boards = build_realtime_intelligence_radar_boards(payload, limit=10)
        self.assertEqual(
            [board["key"] for board in radar_boards],
            ["realtime_surge", "realtime_ambush", "realtime_total"],
        )
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


if __name__ == "__main__":
    unittest.main()
