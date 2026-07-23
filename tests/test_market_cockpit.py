from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from paopao_radar.config import Settings
from paopao_radar.market_cockpit import (
    MarketSnapshotStore,
    build_market_cockpit,
    collect_binance_market_rows,
    collect_market_flow_facts,
    load_market_cockpit_windows,
    persist_flow_market_rows,
    persist_market_batch,
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


class FakeFactSource(FakeBinanceSource):
    def open_interest_hist(self, symbol, period="5m", limit=2):
        base = 1_000_000 if symbol == "BTCUSDT" else 2_000_000
        return [
            {"sumOpenInterestValue": str(base)},
            {"sumOpenInterestValue": str(base * 1.1)},
        ]

    @staticmethod
    def _klines(taker_buy: float):
        return [[3_000, "1", "1", "1", "1", "1", "1", "1000", 0, "0", str(taker_buy)]]

    def spot_klines(self, *_args, **_kwargs):
        return self._klines(650)

    def klines(self, *_args, **_kwargs):
        return self._klines(400)


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
        self.assertEqual(boards["oi"]["amount_positive"]["items"][0]["magnitude_usd"], 100_000)
        self.assertEqual(boards["oi"]["amount_negative"]["items"][0]["magnitude_usd"], 200_000)
        self.assertEqual(payload["overview"]["oi_net_change_usd"], -100_000)
        self.assertAlmostEqual(payload["overview"]["spot_positive_ratio"], 80_000 / 150_000, places=6)
        self.assertAlmostEqual(payload["overview"]["futures_positive_ratio"], 120_000 / 210_000, places=6)
        self.assertAlmostEqual(payload["overview"]["oi_positive_ratio"], 100_000 / 300_000, places=6)
        comparison = payload["overview"]["comparison"]
        self.assertEqual(comparison["previous"]["spot_net_flow_usd"], 5_000)
        self.assertEqual(comparison["previous"]["futures_net_flow_usd"], 20_000)
        self.assertEqual(comparison["delta"]["spot_net_flow_usd"], 5_000)
        self.assertEqual(comparison["delta"]["futures_net_flow_usd"], 10_000)
        self.assertIsNone(comparison["previous"]["oi_net_change_usd"])
        self.assertIsNone(boards["price"]["positive"]["items"][0]["magnitude_usd"])
        self.assertIn("CVD", payload["methodology"]["flow"])

    def test_board_items_preserve_or_infer_non_crypto_asset_type(self) -> None:
        latest = [
            {"symbol": "BABAUSDT", "price": 101, "price_change_pct": 1.0, "observed_at": 1_000},
            {"symbol": "XAUTUSDT", "price": 2_400, "price_change_pct": 0.5, "observed_at": 1_000},
            {"symbol": "BTCUSDT", "asset_type": "主流币", "price": 100, "price_change_pct": 0.2, "observed_at": 1_000},
        ]

        payload = build_market_cockpit(latest, {}, now_ts=1_000, window_sec=900)
        items = {item["symbol"]: item for item in payload["boards"][0]["positive"]["items"]}

        self.assertEqual(items["BABAUSDT"]["asset_type"], "美股")
        self.assertEqual(items["XAUTUSDT"]["asset_type"], "黄金")
        self.assertEqual(items["BTCUSDT"]["asset_type"], "主流币")

    def test_gross_flow_preserves_valid_zero_side(self) -> None:
        with TemporaryDirectory() as tmp:
            store = MarketSnapshotStore(Path(tmp) / "market.db")
            store.append_many([{
                "symbol": "BTCUSDT",
                "observed_at": 1_000,
                "source": "zero_side",
                "price": 100,
                "spot_inflow_usd": 500,
                "spot_outflow_usd": 0,
                "spot_flow_usd": 500,
                "futures_inflow_usd": 800,
                "futures_outflow_usd": 0,
                "futures_flow_usd": 800,
            }])
            series = store.symbol_series("BTCUSDT", start_ts=0, end_ts=1_000)
            latest, baselines = store.comparison(now_ts=1_000, window_sec=900)
            cockpit = build_market_cockpit(latest, baselines, now_ts=1_000, window_sec=900)

        self.assertEqual(series[0]["spot_outflow_usd"], 0)
        self.assertEqual(series[0]["futures_outflow_usd"], 0)
        self.assertEqual(cockpit["assets"][0]["spot_outflow_usd"], 0)
        self.assertEqual(cockpit["assets"][0]["futures_outflow_usd"], 0)

    def test_comparison_aggregates_canonical_flow_facts_per_requested_window(self) -> None:
        with TemporaryDirectory() as tmp:
            store = MarketSnapshotStore(Path(tmp) / "market.db")
            store.append_many([{
                "symbol": "BTCUSDT",
                "observed_at": observed_at,
                "source": "market_flow_15m",
                "window_sec": 900,
                "price": 100 + index,
                "quote_volume": 1_000_000,
                "spot_inflow_usd": 60,
                "spot_outflow_usd": 50,
                "spot_flow_usd": 10,
                "futures_inflow_usd": 100,
                "futures_outflow_usd": 80,
                "futures_flow_usd": 20,
            } for index, observed_at in enumerate(range(900, 7_201, 900))])

            windows = store.comparisons(now_ts=7_200, window_secs=(900, 1_800, 3_600))

        for window_sec, multiplier in ((900, 1), (1_800, 2), (3_600, 4)):
            latest, baselines = windows[window_sec]
            self.assertEqual(latest[0]["spot_flow_usd"], 10 * multiplier)
            self.assertEqual(latest[0]["spot_inflow_usd"], 60 * multiplier)
            self.assertEqual(latest[0]["futures_flow_usd"], 20 * multiplier)
            self.assertEqual(latest[0]["_flow_window_quality"], "aggregated_15m")
            self.assertEqual(baselines["BTCUSDT"]["spot_flow_usd"], 10 * multiplier)
            self.assertEqual(baselines["BTCUSDT"]["_flow_window_quality"], "aggregated_15m")

    def test_flow_strength_uses_same_window_history(self) -> None:
        values = (10, 10, 10, 100, 10, 10, 10, 100, 10, 10, 10, 50)
        flow_rows = [{
            "observed_at": (index + 1) * 900,
            "source": "market_flow_15m",
            "window_sec": 900,
            "spot_flow_usd": value,
            "futures_flow_usd": value * 2,
        } for index, value in enumerate(values)]
        history = [{
            "observed_at": (index + 1) * 900,
            "price": 100 + index,
            "oi_usd": 1_000_000 + index * 1_000,
            "funding_pct": 0.01,
        } for index in range(len(values))]
        baseline = history[0]

        strengths: dict[int, dict[str, float]] = {}
        for window_sec in (900, 3_600):
            flow, quality = MarketSnapshotStore._window_flow(
                flow_rows,
                end_ts=10_800,
                window_sec=window_sec,
            )
            latest = {**history[-1], **flow, "_flow_window_quality": quality}
            strengths[window_sec] = MarketSnapshotStore._historical_strength(
                history,
                flow_rows=flow_rows,
                latest=latest,
                baseline=baseline,
                window_sec=window_sec,
            )

        self.assertEqual(strengths[900]["spot_flow_usd"], 83.3)
        self.assertEqual(strengths[3_600]["spot_flow_usd"], 11.1)

    def test_window_flow_rejects_stale_exact_fact(self) -> None:
        flow, quality = MarketSnapshotStore._window_flow(
            [{
                "observed_at": 1_000,
                "source": "legacy_exact",
                "window_sec": 900,
                "spot_flow_usd": 50,
            }],
            end_ts=5_000,
            window_sec=900,
        )

        self.assertEqual(quality, "insufficient")
        self.assertIsNone(flow["spot_flow_usd"])

    def test_oi_amount_and_strength_rankings_use_distinct_metrics(self) -> None:
        baselines = {
            "BTCUSDT": {"symbol": "BTCUSDT", "price": 100, "oi_usd": 100_000_000},
            "ETHUSDT": {"symbol": "ETHUSDT", "price": 100, "oi_usd": 1_000_000},
        }
        latest = [
            {"symbol": "BTCUSDT", "price": 101, "oi_usd": 101_000_000, "observed_at": 4_600},
            {"symbol": "ETHUSDT", "price": 101, "oi_usd": 1_200_000, "observed_at": 4_600},
        ]

        payload = build_market_cockpit(latest, baselines, now_ts=4_600, window_sec=3_600)
        oi_board = {board["key"]: board for board in payload["boards"]}["oi"]

        self.assertEqual(oi_board["amount_positive"]["items"][0]["symbol"], "BTCUSDT")
        self.assertEqual(oi_board["strength_positive"]["items"][0]["symbol"], "ETHUSDT")
        self.assertEqual(oi_board["amount_unit"], "usd")
        amount_items = {item["symbol"]: item for item in oi_board["amount_positive"]["items"]}
        self.assertEqual(amount_items["BTCUSDT"]["score"], 0.02)
        self.assertEqual(amount_items["ETHUSDT"]["score"], 0.004)
        self.assertEqual(oi_board["amount_score_cap"], 50_000_000.0)

    def test_radar_amount_scores_use_fixed_dimension_caps(self) -> None:
        baselines = {
            "BTCUSDT": {"symbol": "BTCUSDT", "price": 100, "oi_usd": 100_000_000},
        }
        latest = [{
            "symbol": "BTCUSDT",
            "price": 120,
            "oi_usd": 160_000_000,
            "spot_flow_usd": 5_000_000,
            "futures_flow_usd": -25_000_000,
            "observed_at": 4_600,
        }]

        payload = build_market_cockpit(latest, baselines, now_ts=4_600, window_sec=3_600)
        boards = {board["key"]: board for board in payload["boards"]}

        self.assertEqual(boards["price"]["amount_positive"]["items"][0]["score"], 1.0)
        self.assertEqual(boards["oi"]["amount_positive"]["items"][0]["score"], 1.0)
        self.assertEqual(boards["spot_flow"]["amount_positive"]["items"][0]["score"], 0.25)
        self.assertEqual(boards["futures_flow"]["amount_negative"]["items"][0]["score"], 1.0)

    def test_window_strength_uses_each_symbols_own_history(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings(tmp)
            store = MarketSnapshotStore(settings.market_snapshots_db_path)
            btc_oi = 100_000_000.0
            eth_oi = 1_000_000.0
            rows: list[dict[str, object]] = []
            for index in range(10):
                if index:
                    btc_oi *= 1.01 if index == 9 else 1.10
                    eth_oi *= 1.05 if index == 9 else 1.001
                observed_at = (index + 1) * 900
                rows.extend([
                    {
                        "symbol": "BTCUSDT", "observed_at": observed_at, "source": "test",
                        "price": 100 + index, "quote_volume": 100_000_000, "oi_usd": btc_oi,
                        "spot_flow_usd": 20_000, "futures_flow_usd": 30_000,
                    },
                    {
                        "symbol": "ETHUSDT", "observed_at": observed_at, "source": "test",
                        "price": 50 + index, "quote_volume": 80_000_000, "oi_usd": eth_oi,
                        "spot_flow_usd": -15_000, "futures_flow_usd": -10_000,
                    },
                ])
            store.append_many(rows)

            payload = load_market_cockpit_windows(
                settings,
                window_secs=(900,),
                now_ts=9_000,
                store=store,
            )[900]

        oi_board = {board["key"]: board for board in payload["boards"]}["oi"]
        self.assertEqual(oi_board["amount_positive"]["items"][0]["symbol"], "BTCUSDT")
        self.assertEqual(oi_board["strength_positive"]["items"][0]["symbol"], "ETHUSDT")
        btc_strength = next(item for item in oi_board["strength_positive"]["items"] if item["symbol"] == "BTCUSDT")
        self.assertLess(btc_strength["strength_percentile"], 50)
        self.assertIn("同币", payload["methodology"]["strength"])

    def test_comparisons_select_recent_liquid_symbols_before_history_scan(self) -> None:
        with TemporaryDirectory() as tmp:
            store = MarketSnapshotStore(Path(tmp) / "market.db")
            rows: list[dict[str, object]] = []
            for index, coin in enumerate(("AAA", "BBB", "CCC")):
                for observed_at in (900, 1_800):
                    rows.append({
                        "symbol": f"{coin}USDT",
                        "observed_at": observed_at,
                        "source": "test",
                        "price": 10 + index,
                        "quote_volume": (index + 1) * 1_000_000,
                    })
            store.append_many(rows)

            latest, _baselines = store.comparison(
                now_ts=1_800,
                window_sec=900,
                max_symbols=2,
            )

        self.assertEqual({row["symbol"] for row in latest}, {"BBBUSDT", "CCCUSDT"})

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

    def test_batch_collector_closes_internally_owned_source_on_success_and_failure(self) -> None:
        class OwnedSource(FakeBinanceSource):
            def __init__(self, *, fail: bool = False) -> None:
                self.fail = fail
                self.closed = False
                self.http = type("Http", (), {"close": lambda owner: setattr(self, "closed", True)})()

            def usdt_perp_symbols(self):
                if self.fail:
                    raise RuntimeError("upstream failed")
                return super().usdt_perp_symbols()

        with TemporaryDirectory() as tmp:
            settings = self.settings(tmp)
            successful = OwnedSource()
            with patch("paopao_radar.market_cockpit.BinanceDataSource", return_value=successful):
                self.assertTrue(collect_binance_market_rows(settings, now_ts=1_000))
            failed = OwnedSource(fail=True)
            with patch("paopao_radar.market_cockpit.BinanceDataSource", return_value=failed):
                with self.assertRaisesRegex(RuntimeError, "upstream failed"):
                    collect_binance_market_rows(settings, now_ts=1_000)

        self.assertTrue(successful.closed)
        self.assertTrue(failed.closed)

    def test_batch_collector_rotates_and_enriches_open_interest(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings(tmp)
            settings = Settings(**{**settings.__dict__, "market_snapshot_oi_limit": 1, "market_snapshot_workers": 2})
            source = FakeFactSource()
            first = collect_binance_market_rows(settings, source=source, oi_source=source, now_ts=1_000)
            second = collect_binance_market_rows(settings, source=source, oi_source=source, now_ts=1_300)

        first_oi = {row["symbol"] for row in first if row.get("oi_usd")}
        second_oi = {row["symbol"] for row in second if row.get("oi_usd")}
        self.assertEqual(len(first_oi), 1)
        self.assertEqual(len(second_oi), 1)
        self.assertNotEqual(first_oi, second_oi)
        self.assertTrue(all(row.get("oi_change_pct") == 10 for row in first if row.get("oi_usd")))

    def test_closed_market_flow_facts_are_independent_of_push_frequency(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings(tmp)
            settings = Settings(**{
                **settings.__dict__,
                "market_flow_fact_interval_sec": 900,
                "market_flow_fact_limit": 2,
                "market_snapshot_workers": 2,
            })
            observed_at, rows = collect_market_flow_facts(
                settings,
                source=FakeFactSource(),
                symbols=["BTCUSDT", "ETHUSDT"],
                now_ts=4_600,
            )

        self.assertEqual(observed_at, 3_600)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["spot_flow_usd"], 300)
        self.assertEqual(rows[0]["futures_flow_usd"], -200)
        self.assertEqual(rows[0]["window_sec"], 900)

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

    def test_readiness_distinguishes_warmup_from_stale_and_reports_progress(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings(tmp)
            settings = Settings(**{**settings.__dict__, "market_readiness_target_days": 30})
            store = MarketSnapshotStore(settings.market_snapshots_db_path)
            store.append_many(self.baseline_rows(1_000))
            store.append_many(self.latest_rows(2_800))

            warming = store.readiness_summary(settings, now_ts=2_800, requested_window_sec=3_600)
            stale = store.readiness_summary(settings, now_ts=20_000, requested_window_sec=900)

        self.assertEqual(warming["status"], "warming_up")
        self.assertGreater(warming["warmup_progress_pct"], 0)
        self.assertLess(warming["warmup_progress_pct"], 1)
        self.assertEqual(warming["freshness"]["status"], "fresh")
        self.assertEqual(stale["status"], "stale")

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
