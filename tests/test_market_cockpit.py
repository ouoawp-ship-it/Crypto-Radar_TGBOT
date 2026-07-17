from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.config import Settings
from paopao_radar.market_cockpit import (
    MarketSnapshotStore,
    build_market_cockpit,
    collect_binance_market_rows,
    persist_flow_market_rows,
    persist_market_batch,
)
from paopao_radar.web_services.public import (
    public_market_overview_payload,
    public_radar_boards_payload,
)


class FakeBinanceSource:
    def usdt_perp_symbols(self):
        return [
            {"symbol": "BTCUSDT"},
            {"symbol": "ETHUSDT"},
            {"symbol": "XAUUSDT"},
        ]

    def premium_index(self):
        return [
            {"symbol": "BTCUSDT", "lastFundingRate": "0.0001"},
            {"symbol": "ETHUSDT", "lastFundingRate": "-0.0002"},
        ]

    def ticker_24h(self):
        return [
            {"symbol": "BTCUSDT", "lastPrice": "110", "priceChangePercent": "10", "quoteVolume": "100000000"},
            {"symbol": "ETHUSDT", "lastPrice": "180", "priceChangePercent": "-10", "quoteVolume": "80000000"},
            {"symbol": "XAUUSDT", "lastPrice": "2000", "priceChangePercent": "1", "quoteVolume": "90000000"},
        ]


class MarketCockpitTests(unittest.TestCase):
    @staticmethod
    def settings(tmp: str) -> Settings:
        return Settings(
            data_dir=Path(tmp),
            signal_events_db_path=Path(tmp) / "signals.db",
            market_snapshots_db_path=Path(tmp) / "market_snapshots.db",
            market_snapshot_interval_sec=300,
            market_snapshot_retention_days=7,
            market_snapshot_limit=160,
        )

    @staticmethod
    def baseline_rows(ts: int) -> list[dict[str, object]]:
        return [
            {
                "symbol": "BTCUSDT", "observed_at": ts, "source": "binance_futures_batch",
                "price": 100, "quote_volume": 90_000_000, "oi_usd": 1_000_000,
                "spot_flow_usd": 20_000, "futures_flow_usd": 30_000, "funding_pct": 0.01,
                "coverage": {"price": True, "volume": True, "oi": True, "spot_flow": True, "futures_flow": True},
            },
            {
                "symbol": "ETHUSDT", "observed_at": ts, "source": "binance_futures_batch",
                "price": 200, "quote_volume": 70_000_000, "oi_usd": 2_000_000,
                "spot_flow_usd": -15_000, "futures_flow_usd": -10_000, "funding_pct": -0.02,
                "coverage": {"price": True, "volume": True, "oi": True, "spot_flow": True, "futures_flow": True},
            },
        ]

    @staticmethod
    def latest_rows(ts: int) -> list[dict[str, object]]:
        return [
            {
                "symbol": "BTCUSDT", "observed_at": ts, "source": "flow_radar", "window_sec": 3600,
                "price": 110, "quote_volume": 100_000_000, "oi_usd": 1_100_000,
                "spot_flow_usd": 80_000, "futures_flow_usd": 120_000, "funding_pct": 0.015,
                "coverage": {"price": True, "volume": True, "oi": True, "spot_flow": True, "futures_flow": True},
            },
            {
                "symbol": "ETHUSDT", "observed_at": ts, "source": "flow_radar", "window_sec": 3600,
                "price": 180, "quote_volume": 80_000_000, "oi_usd": 1_800_000,
                "spot_flow_usd": -70_000, "futures_flow_usd": -90_000, "funding_pct": -0.025,
                "coverage": {"price": True, "volume": True, "oi": True, "spot_flow": True, "futures_flow": True},
            },
        ]

    def test_store_builds_window_comparison_and_all_primary_boards(self) -> None:
        with TemporaryDirectory() as tmp:
            store = MarketSnapshotStore(Path(tmp) / "market.db")
            self.assertEqual(store.append_many(self.baseline_rows(1_000)), 2)
            self.assertEqual(store.append_many(self.latest_rows(4_600)), 2)
            latest, baselines = store.comparison(now_ts=4_600, window_sec=3_600)
            payload = build_market_cockpit(latest, baselines, now_ts=4_600, window_sec=3_600, board_limit=5)

        self.assertEqual(payload["data_status"], "ready")
        self.assertEqual(payload["coverage"]["assets"], 2)
        self.assertEqual(payload["overview"]["advancing"], 1)
        self.assertEqual(payload["overview"]["declining"], 1)
        boards = {board["key"]: board for board in payload["boards"]}
        self.assertEqual(boards["price"]["positive"]["items"][0]["symbol"], "BTCUSDT")
        self.assertEqual(boards["price"]["negative"]["items"][0]["symbol"], "ETHUSDT")
        self.assertTrue(boards["oi"]["available"])
        self.assertTrue(boards["futures_flow"]["available"])
        self.assertTrue(boards["spot_flow"]["available"])
        self.assertIn("CVD", payload["methodology"]["flow"])

    def test_missing_history_is_explicitly_degraded_and_uses_ticker_fallback(self) -> None:
        latest = [{
            "symbol": "BTCUSDT", "observed_at": 2_000, "source": "binance_futures_batch",
            "price": 110, "price_change_pct": 8.5, "change_window_sec": 86400,
            "quote_volume": 100_000_000, "funding_pct": 0.01,
            "coverage": {"price": True, "volume": True, "funding": True},
        }]
        payload = build_market_cockpit(latest, {}, now_ts=2_000, window_sec=3_600)

        self.assertEqual(payload["data_status"], "degraded")
        self.assertEqual(payload["assets"][0]["price_change_window_sec"], 86400)
        self.assertEqual(payload["assets"][0]["quality"]["price_change_pct"], "ticker_fallback")
        self.assertFalse({board["key"]: board for board in payload["boards"]}["oi"]["available"])
        self.assertTrue(payload["warnings"])

    def test_batch_collector_filters_excluded_assets_and_persists_on_interval(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings(tmp)
            source = FakeBinanceSource()
            rows = collect_binance_market_rows(settings, source=source, now_ts=1_000)
            self.assertEqual([row["symbol"] for row in rows], ["BTCUSDT", "ETHUSDT"])
            self.assertAlmostEqual(rows[0]["funding_pct"], 0.01)

            store = MarketSnapshotStore(settings.market_snapshots_db_path)
            saved = persist_market_batch(settings, source=source, store=store, now_ts=1_000)
            skipped = persist_market_batch(settings, source=source, store=store, now_ts=1_100)

        self.assertEqual(saved["status"], "saved")
        self.assertEqual(saved["count"], 2)
        self.assertEqual(skipped["status"], "skipped")

    def test_flow_rows_are_persisted_with_declared_cvd_semantics(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings(tmp)
            store = MarketSnapshotStore(settings.market_snapshots_db_path)
            count = persist_flow_market_rows(settings, {
                "observed_at": 4_600,
                "window_sec": 3_600,
                "snapshots": [{
                    "symbol": "BTCUSDT", "price": 110, "quote_volume": 100_000_000,
                    "oi_usd": 1_100_000, "oi_24h": 10,
                    "spot_cvd_delta": 80_000, "spot_inflow_usd": 540_000,
                    "spot_outflow_usd": 460_000, "spot_cvd_ready": True,
                    "futures_cvd_delta": 120_000, "futures_inflow_usd": 810_000,
                    "futures_outflow_usd": 690_000, "futures_cvd_ready": True,
                    "price_ready": True, "oi_ready": True, "funding_pct": 0.015,
                }],
            }, store=store)
            latest, _ = store.comparison(now_ts=4_600, window_sec=3_600)

        self.assertEqual(count, 1)
        self.assertEqual(latest[0]["spot_flow_usd"], 80_000)
        self.assertEqual(latest[0]["spot_inflow_usd"], 540_000)
        self.assertEqual(latest[0]["spot_outflow_usd"], 460_000)
        self.assertEqual(latest[0]["futures_flow_usd"], 120_000)

    def test_public_overview_and_boards_are_bounded_and_redacted(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings(tmp)
            store = MarketSnapshotStore(settings.market_snapshots_db_path)
            store.append_many(self.baseline_rows(1_000))
            store.append_many(self.latest_rows(4_600))

            overview = public_market_overview_payload(window_sec=3_600, settings=settings, now_ts=4_600)
            boards = public_radar_boards_payload(window_sec=3_600, board_limit=3, settings=settings, now_ts=4_600)

        self.assertTrue(overview["ok"])
        self.assertTrue(boards["ok"])
        self.assertEqual(overview["data"]["data_status"], "ready")
        self.assertEqual(len(boards["data"]["boards"]), 5)
        serialized = json.dumps({"overview": overview, "boards": boards}, ensure_ascii=False).lower()
        for forbidden in ("bot_token", "api_key", "password", "coverage_json"):
            self.assertNotIn(forbidden, serialized)

    def test_legacy_snapshot_database_is_migrated_without_data_loss(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.db"
            with sqlite3.connect(path) as conn:
                conn.execute(
                    """
                    CREATE TABLE market_snapshots(
                        id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
                        observed_at INTEGER NOT NULL, source TEXT NOT NULL, window_sec INTEGER NOT NULL DEFAULT 0,
                        price REAL, price_change_pct REAL, change_window_sec INTEGER NOT NULL DEFAULT 0,
                        quote_volume REAL, oi_usd REAL, oi_change_pct REAL, spot_flow_usd REAL,
                        futures_flow_usd REAL, funding_pct REAL, coverage_json TEXT NOT NULL DEFAULT '{}',
                        created_at INTEGER NOT NULL
                    )
                    """
                )
                conn.execute("INSERT INTO market_snapshots(symbol, observed_at, source, price, created_at) VALUES('BTCUSDT', 1000, 'legacy', 60000, 1000)")
            conn.close()
            store = MarketSnapshotStore(path)
            store.append_many(self.latest_rows(4_600))
            latest, _ = store.comparison(now_ts=4_600, window_sec=3_600)
            with sqlite3.connect(path) as conn:
                columns = {row[1] for row in conn.execute("PRAGMA table_info(market_snapshots)")}
            conn.close()

        self.assertIn("spot_inflow_usd", columns)
        self.assertIn("market_cap", columns)
        self.assertTrue(any(item["symbol"] == "BTCUSDT" for item in latest))


if __name__ == "__main__":
    unittest.main()
