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
from paopao_radar.workstation_funds import (
    build_cross_exchange_open_interest,
    build_funds_series_analytics,
    build_volume_profile,
    collect_cross_exchange_open_interest,
)


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
        self.assertEqual([item["coin"] for item in amount], ["QTUM", "PHA", "BANK"])
        self.assertEqual(amount[0]["board_count"], 3)
        self.assertEqual(amount[0]["N"], 3)
        self.assertEqual(amount[0]["side"], "in")
        self.assertEqual(amount[0]["direction"], "positive")
        self.assertEqual(amount[1]["direction"], "negative")
        self.assertEqual(amount[2]["board_count"], 2)
        self.assertTrue(amount[2]["divergent"])

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

    def test_momentum_windows_marks_same_board_rank_and_direction_membership(self) -> None:
        settings = type(
            "TestSettings",
            (),
            {"cockpit_v2_mode": "enabled", "market_snapshots_db_path": "test-market.sqlite3"},
        )()

        def side(*symbols: str) -> dict:
            return {"items": [{"symbol": symbol, "coin": symbol.removesuffix("USDT")} for symbol in symbols]}

        sources = {}
        for window_sec in (900, 1800, 3600, 14400, 86400):
            amount_symbols = ("BTCUSDT",) if window_sec in {900, 3600, 86400} else ("ETHUSDT",)
            strength_symbols = ("BTCUSDT",) if window_sec in {900, 1800} else ("SOLUSDT",)
            sources[window_sec] = {
                "data_status": "ready",
                "boards": [{
                    "key": "price",
                    "amount_positive": side(*amount_symbols),
                    "amount_negative": side(),
                    "strength_positive": side(*strength_symbols),
                    "strength_negative": side(),
                }],
            }

        with patch(
            "paopao_radar.web_services.public.load_market_cockpit_windows",
            return_value=sources,
        ):
            payload = public_workstation_radar_momentum_windows_payload(
                settings=settings,
                now_ts=1,
            )

        board = payload["data"]["windows"]["15m"]["boards"][0]
        amount_btc = board["amount_positive"]["items"][0]
        strength_btc = board["strength_positive"]["items"][0]
        self.assertEqual(
            amount_btc["window_states"],
            {"15m": True, "30m": False, "1h": True, "4h": False, "1d": True},
        )
        self.assertEqual(
            strength_btc["window_states"],
            {"15m": True, "30m": True, "1h": False, "4h": False, "1d": False},
        )

    def test_momentum_windows_schedules_market_warmup_when_live_data_is_unavailable(self) -> None:
        settings = type(
            "WarmupSettings",
            (),
            {"cockpit_v2_mode": "enabled", "market_snapshots_db_path": "warmup-market.sqlite3"},
        )()
        sources = {
            window_sec: {"data_status": "unavailable", "warnings": [], "boards": []}
            for window_sec in (900, 1800, 3600, 14400, 86400)
        }
        with patch(
            "paopao_radar.web_services.public.load_market_cockpit_windows",
            return_value=sources,
        ), patch(
            "paopao_radar.web_services.public._schedule_market_warmup",
            return_value=True,
        ) as schedule:
            payload = public_workstation_radar_momentum_windows_payload(
                board_limit=6,
                settings=settings,
            )

        self.assertTrue(payload["ok"])
        schedule.assert_called_once_with(settings)
        self.assertIn("后台预热", payload["data"]["windows"]["15m"]["warnings"][0])

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

    def test_anomaly_endpoint_can_return_one_hundred_server_ranked_events(self) -> None:
        events = [
            {
                "id": f"evt-{index}",
                "symbol": "BTCUSDT",
                "coin": "BTC",
                "observed_at": "2026-07-21T00:00:00Z",
                "window": "5m",
                "event_type": "oi_up",
                "label": "OI 暴涨",
                "direction": "long",
                "value_usd": index,
            }
            for index in range(105)
        ]
        source = {
            "schema_version": "test-realtime-v1",
            "generated_at": "2026-07-21T00:00:00Z",
            "observed_at": "2026-07-21T00:00:00Z",
            "data_status": "ready",
            "coverage": {"symbols": 1, "anomaly_events": len(events)},
            "items": [],
            "anomaly_events": events,
            "boards": [],
        }
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), cockpit_v2_mode="enabled")
            with patch(
                "paopao_radar.web_services.public.RealtimeFeatureStore.recent_rows",
                return_value=[],
            ), patch(
                "paopao_radar.web_services.public.build_realtime_intelligence",
                return_value=source,
            ) as builder:
                payload = public_workstation_radar_anomalies_payload(
                    limit=100,
                    settings=settings,
                    now_ts=1,
                )

        self.assertTrue(payload["ok"])
        self.assertEqual(len(payload["data"]["items"]), 100)
        self.assertEqual(payload["data"]["coverage"]["events"], 100)
        self.assertEqual(builder.call_args.kwargs["limit"], 30)
        self.assertEqual(builder.call_args.kwargs["event_limit"], 100)

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

    def test_funds_flow_analytics_report_continuity_and_honest_next_bucket_hit_rate(self) -> None:
        analytics = build_funds_series_analytics(
            [
                {"price": 100, "spot_flow_usd": 10},
                {"price": 101, "spot_flow_usd": 20},
                {"price": 100, "spot_flow_usd": -30},
                {"price": 99, "spot_flow_usd": -40},
            ],
            metric="spot_flow_usd",
            interval_sec=900,
        )

        self.assertEqual(analytics["latest_direction"], "outflow")
        self.assertEqual(analytics["duration_sec"], 1_800)
        self.assertEqual(analytics["hit_samples"], 3)
        self.assertEqual(analytics["hit_rate_pct"], 66.6667)
        self.assertEqual(analytics["price"]["change_pct"], -1)

    def test_funds_series_endpoint_includes_flow_analytics_for_selected_window(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), cockpit_v2_mode="enabled")
            store = MarketSnapshotStore(settings.market_snapshots_db_path)
            store.append_many([
                {"symbol": "BTCUSDT", "observed_at": 900, "source": "test", "spot_flow_usd": 10, "price": 100},
                {"symbol": "BTCUSDT", "observed_at": 1_800, "source": "test", "spot_flow_usd": 20, "price": 101},
                {"symbol": "BTCUSDT", "observed_at": 2_700, "source": "test", "spot_flow_usd": -30, "price": 100},
            ])
            response = public_workstation_funds_series_payload(
                "BTC",
                kind="spot_flow",
                interval="15m",
                bars=24,
                settings=settings,
                now_ts=2_700,
            )

        self.assertTrue(response["ok"])
        self.assertEqual(response["data"]["analytics"]["metric"], "spot_flow_usd")
        self.assertEqual(response["data"]["analytics"]["hit_samples"], 2)
        self.assertEqual(response["data"]["analytics"]["duration_sec"], 900)

    def test_volume_profile_returns_poc_and_seventy_percent_value_area(self) -> None:
        profile = build_volume_profile([
            {
                "high": 101 + index,
                "low": 99 + index,
                "close": 100 + index,
                "quote_volume": 100_000 if index == 12 else 1_000,
            }
            for index in range(24)
        ])

        self.assertEqual(profile["data_status"], "ready")
        self.assertLessEqual(profile["val"], profile["poc"])
        self.assertLessEqual(profile["poc"], profile["vah"])
        self.assertEqual(profile["value_area_ratio"], 0.7)

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

    def test_cross_exchange_oi_collector_builds_its_real_http_client(self) -> None:
        class FakeHttpClient:
            closed = False

            def __init__(self, _settings, quality) -> None:
                self.quality = quality

            def get_json(self, url, *_args, **_kwargs):
                if url.endswith("/premiumIndex"):
                    return {"markPrice": "50000"}
                if url.endswith("/openInterest"):
                    return {"openInterest": "10"}
                if "bybit" in url:
                    return {"result": {"list": [{"openInterest": "4"}]}}
                return {"data": [{"oiUsd": "300000"}]}

            def close(self) -> None:
                self.closed = True

        with patch("paopao_radar.workstation_funds.HttpClient", FakeHttpClient):
            payload = collect_cross_exchange_open_interest(Settings.load(), "BTCUSDT")

        self.assertEqual(payload["data_status"], "ready")
        self.assertEqual(payload["coverage"], {"exchanges": 3, "target": 3})
        self.assertEqual(payload["total_oi_usd"], 1_000_000)

    def test_cross_exchange_oi_endpoint_validates_symbol_and_wraps_collector(self) -> None:
        bad = public_workstation_funds_open_interest_payload("--")
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
