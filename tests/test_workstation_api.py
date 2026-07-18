from __future__ import annotations

import unittest
from unittest.mock import patch

from paopao_radar.web_services.public import (
    public_workstation_funds_open_interest_payload,
    public_workstation_radar_momentum_payload,
    public_workstation_radar_momentum_windows_payload,
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
