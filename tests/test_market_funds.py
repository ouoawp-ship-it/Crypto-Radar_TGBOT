from __future__ import annotations

import json
import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.asset_catalog import ASSET_CATALOG_VERSION, asset_sector_view
from paopao_radar.config import Settings
from paopao_radar.market_cockpit import MarketSnapshotStore, build_market_cockpit
from paopao_radar.market_funds import build_funds_assets, build_funds_sectors
from paopao_radar.web_services.public import (
    public_funds_assets_payload,
    public_funds_sectors_payload,
)


class MarketFundsTests(unittest.TestCase):
    @staticmethod
    def latest(ts: int) -> list[dict[str, object]]:
        return [
            {
                "symbol": "ETHUSDT", "observed_at": ts, "source": "flow_radar",
                "price": 210, "quote_volume": 120_000_000, "market_cap": 300_000_000_000,
                "oi_usd": 3_000_000, "spot_inflow_usd": 900_000,
                "spot_outflow_usd": 600_000, "spot_flow_usd": 300_000,
                "futures_inflow_usd": 1_400_000, "futures_outflow_usd": 1_000_000,
                "futures_flow_usd": 400_000, "funding_pct": 0.01,
                "coverage": {"price": True, "spot_flow": True, "futures_flow": True, "oi": True},
            },
            {
                "symbol": "ARBUSDT", "observed_at": ts, "source": "flow_radar",
                "price": 1.1, "quote_volume": 80_000_000, "market_cap": 4_000_000_000,
                "oi_usd": 2_000_000, "spot_inflow_usd": 400_000,
                "spot_outflow_usd": 650_000, "spot_flow_usd": -250_000,
                "futures_inflow_usd": 600_000, "futures_outflow_usd": 750_000,
                "futures_flow_usd": -150_000, "funding_pct": -0.02,
                "coverage": {"price": True, "spot_flow": True, "futures_flow": True, "oi": True},
            },
            {
                "symbol": "PEPEUSDT", "observed_at": ts, "source": "binance_futures_batch",
                "price": 0.00001, "quote_volume": 60_000_000, "funding_pct": 0.03,
                "coverage": {"price": True, "funding": True},
            },
        ]

    @staticmethod
    def baselines(ts: int) -> dict[str, dict[str, object]]:
        return {
            "ETHUSDT": {
                "price": 200, "quote_volume": 100_000_000, "oi_usd": 2_500_000,
                "spot_inflow_usd": 600_000, "spot_outflow_usd": 400_000,
                "futures_inflow_usd": 1_000_000, "futures_outflow_usd": 1_000_000,
                "spot_flow_usd": 200_000, "futures_flow_usd": 500_000, "observed_at": ts,
            },
            "ARBUSDT": {
                "price": 1, "quote_volume": 75_000_000, "oi_usd": 2_100_000,
                "spot_inflow_usd": 500_000, "spot_outflow_usd": 500_000,
                "futures_inflow_usd": 800_000, "futures_outflow_usd": 400_000,
                "spot_flow_usd": -500_000, "futures_flow_usd": -100_000, "observed_at": ts,
            },
            "PEPEUSDT": {"price": 0.000011, "quote_volume": 55_000_000, "observed_at": ts},
        }

    def cockpit(self) -> dict[str, object]:
        return build_market_cockpit(
            self.latest(4_600), self.baselines(1_000), now_ts=4_600, window_sec=3_600,
        )

    def test_catalog_is_versioned_and_has_one_primary_aggregation_sector(self) -> None:
        eth = asset_sector_view("ETHUSDT")
        self.assertEqual(eth["catalog_version"], ASSET_CATALOG_VERSION)
        self.assertEqual(eth["primary_sector_id"], "layer1")
        self.assertIn("staking", eth["sector_ids"])

    def test_sector_aggregation_does_not_double_count_multi_sector_assets(self) -> None:
        payload = build_funds_sectors(self.cockpit(), market_type="spot")
        by_sector = {item["sector_id"]: item for item in payload["sectors"]}

        self.assertEqual(payload["summary"]["net_flow_usd"], 50_000)
        self.assertEqual(payload["summary"]["inflow_usd"], 1_300_000)
        self.assertEqual(payload["summary"]["outflow_usd"], 1_250_000)
        self.assertEqual(by_sector["layer1"]["net_flow_usd"], 300_000)
        self.assertNotIn("staking", by_sector)
        self.assertEqual(by_sector["layer2"]["net_flow_usd"], -250_000)
        self.assertEqual(by_sector["meme"]["data_status"], "unavailable")

    def test_assets_support_filters_sorting_pagination_and_explicit_missing_values(self) -> None:
        payload = build_funds_assets(
            self.cockpit(), market_type="spot", sector="meme", sort_key="net_flow_usd",
            direction="desc", page=1, page_size=10,
        )
        self.assertEqual(payload["pagination"]["total"], 1)
        item = payload["items"][0]
        self.assertEqual(item["symbol"], "PEPEUSDT")
        self.assertIsNone(item["net_flow_usd"])
        self.assertIsNone(item["inflow_usd"])
        self.assertEqual(item["data_status"], "unavailable")

        searched = build_funds_assets(self.cockpit(), market_type="futures", search="arb")
        self.assertEqual([item["symbol"] for item in searched["items"]], ["ARBUSDT"])
        self.assertEqual(searched["items"][0]["net_flow_usd"], -150_000)
        self.assertEqual(searched["items"][0]["net_flow_change_pct"], -50)

        eth_spot = build_funds_assets(self.cockpit(), market_type="spot", search="eth")["items"][0]
        eth_futures = build_funds_assets(self.cockpit(), market_type="futures", search="eth")["items"][0]
        self.assertEqual(eth_spot["volume_usd"], 1_500_000)
        self.assertEqual(eth_spot["volume_change_pct"], 50)
        self.assertEqual(eth_spot["sources"]["volume"], "binance_spot_klines")
        self.assertEqual(eth_futures["volume_usd"], 2_400_000)
        self.assertEqual(eth_futures["volume_change_pct"], 20)
        self.assertEqual(eth_futures["sources"]["volume"], "binance_futures_klines")

        sorted_by_flow_change = build_funds_assets(
            self.cockpit(), market_type="spot", sort_key="net_flow_change_pct", direction="desc",
        )
        self.assertEqual([item["symbol"] for item in sorted_by_flow_change["items"][:2]], ["ETHUSDT", "ARBUSDT"])
        self.assertEqual(sorted_by_flow_change["items"][0]["net_flow_change_pct"], 50)

    def test_oi_distribution_uses_complete_filtered_universe_not_current_page(self) -> None:
        cockpit = deepcopy(self.cockpit())
        template = cockpit["assets"][0]
        cockpit["assets"] = [
            {**template, "symbol": f"T{i:02d}USDT", "coin": f"T{i:02d}", "oi_usd": float(i)}
            for i in range(1, 26)
        ]

        payload = build_funds_assets(cockpit, market_type="futures", page=2, page_size=10)

        self.assertEqual(len(payload["items"]), 10)
        self.assertEqual(payload["distribution"]["oi_covered_assets"], 25)
        self.assertEqual(payload["distribution"]["oi_total_usd"], 325.0)
        self.assertAlmostEqual(payload["distribution"]["top_10_oi_share_pct"], sum(range(16, 26)) / 325 * 100)
        self.assertEqual(payload["distribution"]["top_50_oi_share_pct"], 100.0)

    def test_snapshot_store_migrates_and_returns_traceable_gross_flow_series(self) -> None:
        with TemporaryDirectory() as tmp:
            store = MarketSnapshotStore(Path(tmp) / "market.db")
            store.append_many(self.latest(4_600))
            points = store.symbol_series("ETHUSDT", start_ts=1_000, end_ts=5_000)

        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["spot_inflow_usd"], 900_000)
        self.assertEqual(points[0]["spot_outflow_usd"], 600_000)
        self.assertEqual(points[0]["sources"], ["flow_radar"])

    def test_public_funds_contracts_are_bounded_versioned_and_redacted(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                signal_events_db_path=Path(tmp) / "signals.db",
                market_snapshots_db_path=Path(tmp) / "market.db",
            )
            store = MarketSnapshotStore(settings.market_snapshots_db_path)
            store.append_many([
                {"symbol": symbol, "source": "binance_futures_batch", **row}
                for symbol, row in self.baselines(1_000).items()
            ])
            store.append_many(self.latest(4_600))
            sectors = public_funds_sectors_payload(
                window_sec=3_600, market_type="spot", settings=settings, now_ts=4_600,
            )
            assets = public_funds_assets_payload(
                window_sec=3_600, market_type="spot", page_size=10,
                settings=settings, now_ts=4_600,
            )

        self.assertTrue(sectors["ok"])
        self.assertTrue(assets["ok"])
        self.assertEqual(sectors["data"]["catalog_version"], ASSET_CATALOG_VERSION)
        self.assertLessEqual(len(assets["data"]["items"]), 10)
        serialized = json.dumps({"sectors": sectors, "assets": assets}, ensure_ascii=False).lower()
        for forbidden in ("bot_token", "api_key", "password", "coverage_json"):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
