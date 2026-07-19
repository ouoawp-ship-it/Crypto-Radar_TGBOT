from __future__ import annotations

import unittest
from unittest.mock import patch

from paopao_radar.web_services.public import (
    public_workstation_funds_open_interest_payload,
    public_workstation_radar_anomalies_payload,
    public_workstation_radar_briefs_payload,
    public_workstation_radar_momentum_payload,
    public_workstation_radar_momentum_windows_payload,
    public_workstation_radar_rank_payload,
    public_workstation_radar_surge_payload,
)
from paopao_radar.workstation_funds import build_cross_exchange_open_interest


class WorkstationRadarApiTests(unittest.TestCase):
    def test_momentum_projects_only_core_boards_for_selected_window(self) -> None:
        source_payload = {
            "ok": True,
            "data": {
                "schema_version": "source-v1",
                "generated_at": "2026-07-18T00:00:00Z",
                "data_status": "ready",
                "warnings": [],
                "coverage": {"assets": 100},
                "readiness": {"status": "ready"},
                "boards": [
                    {"key": "price", "title": "Price"},
                    {"key": "oi", "title": "OI"},
                    {"key": "futures_flow", "title": "Futures flow"},
                    {"key": "spot_flow", "title": "Spot flow"},
                    {"key": "realtime_surge", "title": "Surge"},
                ],
                "methodology": {"strength": "empirical percentile"},
            },
        }
        with patch(
            "paopao_radar.web_services.public.public_radar_boards_payload",
            return_value=source_payload,
        ) as source:
            payload = public_workstation_radar_momentum_payload(window="30m", board_limit=6)

        self.assertTrue(payload["ok"])
        data = payload["data"]
        self.assertEqual(data["schema_version"], "workstation.radar.momentum.v1")
        self.assertEqual(data["window"], "30m")
        self.assertEqual(data["window_sec"], 1800)
        self.assertEqual(
            [item["key"] for item in data["boards"]],
            ["price", "oi", "futures_flow", "spot_flow"],
        )
        self.assertTrue(data["methodology"]["closed_window"])
        source.assert_called_once_with(
            window_sec=1800,
            board_limit=6,
            settings=None,
            now_ts=None,
        )

    def test_momentum_rejects_unknown_window_without_loading_source(self) -> None:
        with patch("paopao_radar.web_services.public.public_radar_boards_payload") as source:
            payload = public_workstation_radar_momentum_payload(window="2h")

        self.assertFalse(payload["ok"])
        self.assertEqual(payload["code"], "invalid_window")
        source.assert_not_called()

    def test_momentum_returns_server_owned_cross_board_confluence(self) -> None:
        def side(*coins: str) -> dict:
            return {
                "items": [
                    {"symbol": f"{coin}USDT", "coin": coin, "value": index + 1, "strength_percentile": 99 - index}
                    for index, coin in enumerate(coins)
                ]
            }

        source_payload = {
            "ok": True,
            "data": {
                "boards": [
                    {"key": "price", "amount_positive": side("QTUM")},
                    {"key": "oi", "amount_positive": side("QTUM"), "amount_negative": side("PHA")},
                    {"key": "futures_flow", "amount_positive": side("QTUM", "BANK"), "amount_negative": side("PHA")},
                    {"key": "spot_flow", "amount_positive": side("QTUM"), "amount_negative": side("PHA", "BANK")},
                ]
            },
        }
        with patch(
            "paopao_radar.web_services.public.public_radar_boards_payload",
            return_value=source_payload,
        ):
            payload = public_workstation_radar_momentum_payload(window="15m")

        amount = payload["data"]["confluence"]["amount"]
        self.assertEqual([item["coin"] for item in amount], ["QTUM", "PHA"])
        self.assertEqual(amount[0]["board_count"], 3)
        self.assertEqual(amount[0]["direction"], "positive")
        self.assertEqual(amount[1]["direction"], "negative")
        self.assertFalse(any(item["coin"] == "BANK" for item in amount))

    def test_momentum_windows_loads_all_windows_from_one_history_scan(self) -> None:
        settings = type(
            "TestSettings",
            (),
            {"cockpit_v2_mode": "enabled", "market_snapshots_db_path": "test-market.sqlite3"},
        )()
        sources = {
            window_sec: {
                "generated_at": "2026-07-18T00:00:00Z",
                "data_status": "ready",
                "coverage": {"assets": 2},
                "readiness": {"status": "ready"},
                "boards": [
                    {"key": "price"},
                    {"key": "oi"},
                    {"key": "futures_flow"},
                    {"key": "spot_flow"},
                    {"key": "funding"},
                ],
            }
            for window_sec in (900, 1800, 3600, 14400, 86400)
        }
        with patch(
            "paopao_radar.web_services.public.load_market_cockpit_windows",
            return_value=sources,
        ) as loader:
            payload = public_workstation_radar_momentum_windows_payload(
                board_limit=6,
                settings=settings,
                now_ts=1,
            )

        self.assertTrue(payload["ok"])
        self.assertEqual(list(payload["data"]["windows"]), ["15m", "30m", "1h", "4h", "1d"])
        self.assertEqual(
            [board["key"] for board in payload["data"]["windows"]["1h"]["boards"]],
            ["price", "oi", "futures_flow", "spot_flow"],
        )
        loader.assert_called_once_with(
            settings,
            window_secs=(900, 1800, 3600, 14400, 86400),
            board_limit=6,
            now_ts=1,
            live_rows=[],
        )

    def test_workstation_realtime_endpoints_project_independent_contracts(self) -> None:
        source = {
            "ok": True,
            "data": {
                "schema_version": "source-v1",
                "generated_at": "2026-07-18T00:00:00Z",
                "observed_at": "2026-07-18T00:00:00Z",
                "data_status": "ready",
                "coverage": {"symbols": 2},
                "anomaly_events": [
                    {"id": "evt-1", "symbol": "BTCUSDT", "coin": "BTC", "label": "OI 暴涨", "window": "5m", "rankings": {"self": {"rank": 1}}}
                ],
                "items": [
                    {"symbol": "BTCUSDT", "coin": "BTC", "surge": {"triggered": True, "score": 91}, "ambush": {"triggered": False}, "anomaly_24h": {"count": 12}},
                    {"symbol": "ETHUSDT", "coin": "ETH", "surge": {"triggered": False}, "ambush": {"triggered": True, "score": 82}, "anomaly_24h": {"count": 8}},
                ],
            },
        }
        source["data"]["anomaly_events"] = [
            {"id": f"evt-{index}", "symbol": "BTCUSDT", "coin": "BTC", "label": "OI move", "window": "5m", "detail": "5 分钟内 oi +55万 (+0.7%)", "rankings": {"self": {"rank": 1}}}
            for index in range(1, 106)
        ]
        with patch(
            "paopao_radar.web_services.public.public_realtime_intelligence_payload",
            return_value=source,
        ):
            anomalies = public_workstation_radar_anomalies_payload(limit=100)
            surge = public_workstation_radar_surge_payload()
            rank = public_workstation_radar_rank_payload()
            briefs = public_workstation_radar_briefs_payload()

        self.assertEqual(anomalies["data"]["items"][0]["id"], "evt-1")
        self.assertEqual(anomalies["data"]["items"][0]["detail"], "5 分钟内 oi +55万 (+0.7%)")
        self.assertEqual(len(anomalies["data"]["items"]), 100)
        self.assertEqual([item["coin"] for item in surge["data"]["items"]], ["BTC"])
        self.assertEqual([item["coin"] for item in rank["data"]["total"]], ["BTC", "ETH"])
        self.assertEqual([item["coin"] for item in rank["data"]["ambush"]], ["ETH"])
        self.assertEqual(rank["data"]["universe"], source["data"]["items"])
        self.assertEqual(briefs["data"]["items"][0]["title"], "BTC OI move")

    def test_cross_exchange_oi_normalizes_usd_and_excludes_missing_venues(self) -> None:
        payload = build_cross_exchange_open_interest(
            "BTCUSDT",
            mark_price=50_000,
            payloads={
                "binance": {"openInterest": "10"},
                "bybit": {"result": {"list": [{"openInterest": "4"}]}},
                "okx": {"data": [{"oiUsd": "300000"}]},
            },
        )

        self.assertEqual(payload["data_status"], "ready")
        self.assertEqual(payload["total_oi_usd"], 1_000_000)
        self.assertEqual(payload["coverage"], {"exchanges": 3, "target": 3})
        self.assertEqual(sum(item["share_pct"] for item in payload["exchanges"]), 100)

    def test_cross_exchange_oi_endpoint_validates_symbol_and_wraps_collector(self) -> None:
        bad = public_workstation_funds_open_interest_payload("not-a-symbol")
        self.assertFalse(bad["ok"])
        expected = {
            "schema_version": "workstation.funds.open-interest.v1",
            "symbol": "BTCUSDT",
            "data_status": "ready",
            "exchanges": [],
        }
        collector = lambda _settings, _symbol: expected
        payload = public_workstation_funds_open_interest_payload(
            "BTC",
            settings=object(),
            collector=collector,
            now_ts=1,
        )
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"], expected)


if __name__ == "__main__":
    unittest.main()
