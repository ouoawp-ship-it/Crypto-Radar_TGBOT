from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from paopao_radar.config import Settings
from paopao_radar.market_cockpit import MarketSnapshotStore
from paopao_radar.web_services.public import (
    public_workstation_funds_open_interest_payload,
    public_workstation_funds_overview_payload,
    public_workstation_funds_series_payload,
    public_workstation_info_briefs_payload,
    public_workstation_info_dashboard_payload,
    public_workstation_info_feed_payload,
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

    def test_funds_overview_uses_one_multi_window_scan_for_sectors_and_assets(self) -> None:
        settings = type(
            "TestSettings",
            (),
            {"cockpit_v2_mode": "enabled", "market_snapshots_db_path": Path("test-market.sqlite3")},
        )()
        sector_source = {"generated_at": "2026-07-19T00:00:00Z", "window_sec": 3600}
        asset_source = {"generated_at": "2026-07-19T00:00:00Z", "window_sec": 900}
        sector_payload = {
            "data_status": "ready", "coverage": {"assets": 2}, "warnings": [],
            "summary": {"net_flow_usd": 10}, "catalog": [],
            "sectors": [{"sector_id": "defi", "label": "DeFi"}],
            "methodology": {"flow": "sector-flow"},
        }
        asset_payload = {
            "data_status": "ready", "coverage": {"assets": 2}, "warnings": [],
            "distribution": {"oi_total_usd": 100}, "filters": {},
            "sort": {"key": "net_flow_usd", "direction": "desc"},
            "pagination": {"page": 1, "page_size": 20, "page_count": 1, "total": 1},
            "items": [{"symbol": "BTCUSDT", "net_flow_usd": 10}],
            "methodology": {"flow": "asset-flow"},
        }
        with patch(
            "paopao_radar.web_services.public.load_market_cockpit_windows",
            return_value={3600: sector_source, 900: asset_source},
        ) as loader, patch(
            "paopao_radar.web_services.public.build_funds_sectors",
            return_value=sector_payload,
        ) as sectors, patch(
            "paopao_radar.web_services.public.build_funds_assets",
            return_value=asset_payload,
        ) as assets:
            response = public_workstation_funds_overview_payload(
                sector_window_sec=3600,
                asset_window_sec=900,
                market_type="spot",
                settings=settings,
                now_ts=1,
            )

        self.assertTrue(response["ok"])
        data = response["data"]
        self.assertEqual(data["schema_version"], "workstation.funds.overview.v1")
        self.assertEqual(data["sector_window_sec"], 3600)
        self.assertEqual(data["asset_window_sec"], 900)
        self.assertEqual(data["assets"][0]["symbol"], "BTCUSDT")
        loader.assert_called_once_with(
            settings,
            window_secs=(3600, 900),
            board_limit=8,
            now_ts=1,
            live_rows=[],
        )
        sectors.assert_called_once_with(sector_source, market_type="spot")
        self.assertIs(assets.call_args.args[0], asset_source)

    def test_funds_series_returns_selected_bucket_oi_amount_and_percent_change(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), cockpit_v2_mode="enabled")
            store = MarketSnapshotStore(settings.market_snapshots_db_path)
            store.append_many([
                {"symbol": "BTCUSDT", "observed_at": 900, "source": "test", "oi_usd": 1_000, "price": 100},
                {"symbol": "BTCUSDT", "observed_at": 1_800, "source": "test", "oi_usd": 1_250, "price": 101},
            ])
            response = public_workstation_funds_series_payload(
                "BTC",
                kind="oi",
                interval="15m",
                bars=24,
                settings=settings,
                now_ts=1_800,
            )

        self.assertTrue(response["ok"])
        data = response["data"]
        self.assertEqual(data["schema_version"], "workstation.funds.series.v1")
        self.assertEqual(data["metric"], "oi_usd")
        self.assertEqual(data["points"][-1]["oi_change_usd"], 250)
        self.assertEqual(data["points"][-1]["oi_change_pct"], 25)
        invalid = public_workstation_funds_series_payload("BTC", kind="prediction")
        self.assertFalse(invalid["ok"])
        self.assertEqual(invalid["code"], "invalid_kind")

    def test_info_workstation_contracts_map_channels_and_keep_rule_ai_boundary(self) -> None:
        source = {
            "ok": True,
            "data": {
                "schema_version": "source-v1",
                "generated_at": "2026-07-19T00:00:00Z",
                "data_status": "ready",
                "coverage": {"events": 1},
                "warnings": [],
                "pagination": {"total": 9},
                "summary": {"high_importance": 1},
                "channels": [{"key": "news_en", "count": 9}],
                "items": [{
                    "event_id": "evt-1", "title": "Market update", "summary": "Observed facts",
                    "importance": "high", "source": "Public source", "url": "https://example.com/event",
                    "ai_analysis": {"generated_by": "rules", "fact_summary": "Rule summary"},
                }],
                "methodology": {"rights": "linked"},
            },
        }
        with patch(
            "paopao_radar.web_services.public.public_info_feed_payload",
            return_value=source,
        ) as base_feed:
            feed = public_workstation_info_feed_payload(channel="en", now_ts=1)
            dashboard = public_workstation_info_dashboard_payload(now_ts=1)

        self.assertTrue(feed["ok"])
        self.assertEqual(feed["data"]["channel"], "en")
        self.assertEqual(feed["data"]["schema_version"], "workstation.info.feed.v1")
        self.assertEqual(base_feed.call_args_list[0].kwargs["source_type"], "news")
        self.assertEqual(base_feed.call_args_list[0].kwargs["language"], "en")
        self.assertEqual(dashboard["data"]["coverage"]["events"], 9)

        def channel_feed(**kwargs):
            channel = kwargs["channel"]
            return {"ok": True, "data": {**source["data"], "channel": channel}}

        with patch(
            "paopao_radar.web_services.public.public_workstation_info_feed_payload",
            side_effect=channel_feed,
        ):
            briefs = public_workstation_info_briefs_payload(now_ts=1)

        self.assertTrue(briefs["ok"])
        self.assertEqual(briefs["data"]["coverage"]["ready_channels"], 4)
        self.assertEqual(briefs["data"]["items"][0]["summary"], "Rule summary")
        self.assertFalse(briefs["data"]["items"][0]["model_generated"])

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
