from __future__ import annotations

import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.config import Settings
from paopao_radar.launch_lifecycle import LaunchLifecycleStore
from paopao_radar.radar import RadarEngine
from paopao_radar.signal_store import SignalEventStore
from paopao_radar.storage import JsonStore


def snapshot(
    *,
    symbol: str = "TESTUSDT",
    window_end_ts: int,
    score: int,
    price: float,
    oi: float,
    funding_pct: float = -0.1,
    funding_interval_hours: int = 8,
    breakout: bool = False,
    breakout_price: float = 0.0,
    quality_gate: str = "allow",
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "window_end_ts": window_end_ts,
        "score": score,
        "closed_price": price,
        "closed_oi_usd": oi,
        "closed_quote_volume": 1_000_000.0,
        "price_15m": 1.0,
        "price_1h": 2.0,
        "oi_15m": 1.5,
        "oi_1h": 3.0,
        "volume_ratio": 2.0,
        "funding_pct": funding_pct,
        "funding_interval_hours": funding_interval_hours,
        "breakout": breakout,
        "breakout_price": breakout_price,
        "data_quality_status": "confirmed",
        "data_quality_score": 100,
        "quality_gate": quality_gate,
        "reasons": ["test"],
    }


class LaunchLifecycleStoreTests(unittest.TestCase):
    def make_store(self, root: str) -> LaunchLifecycleStore:
        return LaunchLifecycleStore(Path(root) / "signals.db")

    def test_records_exact_first_and_previous_deltas_idempotently(self) -> None:
        with TemporaryDirectory() as tmp:
            store = self.make_store(tmp)
            first = store.record_observation(
                snapshot(window_end_ts=900, score=60, price=100, oi=1_000),
                stage="primed",
                observed_at=910,
            )
            second = store.record_observation(
                snapshot(
                    window_end_ts=1800,
                    score=75,
                    price=110,
                    oi=1_200,
                    funding_pct=-0.08,
                    funding_interval_hours=4,
                ),
                stage="breakout",
                observed_at=1810,
            )
            duplicate = store.record_observation(
                snapshot(
                    window_end_ts=1800,
                    score=75,
                    price=110,
                    oi=1_200,
                    funding_pct=-0.08,
                    funding_interval_hours=4,
                ),
                stage="breakout",
                observed_at=1811,
            )

            self.assertEqual(first["status"], "opened")
            self.assertEqual(second["status"], "active")
            self.assertEqual(second["cycle_no"], 1)
            self.assertEqual(second["observation_no"], 2)
            self.assertAlmostEqual(second["delta_from_first"]["price_pct"], 10.0)
            self.assertAlmostEqual(second["delta_from_first"]["oi_pct"], 20.0)
            self.assertAlmostEqual(second["delta_from_first"]["funding_pct_point"], 0.02)
            self.assertAlmostEqual(second["delta_from_first"]["funding_8h_pct_point"], -0.06)
            self.assertEqual(second["delta_from_first"]["funding_interval_hours"], -4)
            self.assertEqual(second["delta_from_first"]["score"], 15)
            self.assertEqual(duplicate["status"], "duplicate")
            self.assertEqual(len(store.list_observations(first["cycle_id"])), 2)

    def test_lifecycle_tables_coexist_with_existing_signal_store(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signals.db"
            SignalEventStore(db_path).stats(window_sec=3600)
            lifecycle = LaunchLifecycleStore(db_path)
            opened = lifecycle.record_observation(
                snapshot(window_end_ts=900, score=60, price=100, oi=1_000),
                stage="primed",
                observed_at=910,
            )

            self.assertEqual(opened["status"], "opened")
            self.assertEqual(SignalEventStore(db_path).stats(window_sec=3600)["total"], 0)

    def test_two_consecutive_low_score_windows_fail_cycle(self) -> None:
        with TemporaryDirectory() as tmp:
            store = self.make_store(tmp)
            opened = store.record_observation(
                snapshot(window_end_ts=900, score=60, price=100, oi=1_000),
                stage="primed",
                observed_at=910,
            )
            cooling = store.record_observation(
                snapshot(window_end_ts=1800, score=40, price=99, oi=990),
                stage="idle",
                observed_at=1810,
            )
            duplicate = store.record_observation(
                snapshot(window_end_ts=1800, score=40, price=99, oi=990),
                stage="idle",
                observed_at=1811,
            )
            failed = store.record_observation(
                snapshot(window_end_ts=2700, score=40, price=98, oi=980),
                stage="idle",
                observed_at=2710,
            )

            self.assertEqual(cooling["current_stage"], "cooling")
            self.assertEqual(cooling["invalid_window_count"], 1)
            self.assertEqual(duplicate["invalid_window_count"], 1)
            self.assertEqual(failed["status"], "failed")
            self.assertEqual(failed["cycle_status"], "failed")
            self.assertEqual(failed["end_reason"], "two_windows_below_watch_score")
            self.assertEqual(len(store.list_observations(opened["cycle_id"])), 3)

    def test_missing_or_blocked_window_freezes_instead_of_failing(self) -> None:
        with TemporaryDirectory() as tmp:
            store = self.make_store(tmp)
            opened = store.record_observation(
                snapshot(window_end_ts=900, score=60, price=100, oi=1_000),
                stage="primed",
                observed_at=910,
            )
            frozen = store.record_observation(
                snapshot(
                    window_end_ts=1800,
                    score=30,
                    price=99,
                    oi=990,
                    quality_gate="block",
                ),
                stage="idle",
                observed_at=1810,
            )
            after_gap = store.record_observation(
                snapshot(window_end_ts=2700, score=30, price=98, oi=980),
                stage="idle",
                observed_at=2710,
            )

            self.assertEqual(frozen["status"], "frozen")
            self.assertEqual(after_gap["cycle_status"], "active")
            self.assertEqual(after_gap["invalid_window_count"], 1)
            self.assertEqual(len(store.list_observations(opened["cycle_id"])), 2)

    def test_two_closes_below_confirmed_breakout_fail_cycle(self) -> None:
        with TemporaryDirectory() as tmp:
            store = self.make_store(tmp)
            opened = store.record_observation(
                snapshot(
                    window_end_ts=900,
                    score=75,
                    price=105,
                    oi=1_000,
                    breakout=True,
                    breakout_price=100,
                ),
                stage="breakout",
                observed_at=910,
            )
            first_below = store.record_observation(
                snapshot(window_end_ts=1800, score=70, price=99, oi=1_020),
                stage="primed",
                observed_at=1810,
            )
            failed = store.record_observation(
                snapshot(window_end_ts=2700, score=70, price=98, oi=1_030),
                stage="primed",
                observed_at=2710,
            )
            repeated_failed_window = store.record_observation(
                snapshot(window_end_ts=2700, score=70, price=98, oi=1_030),
                stage="primed",
                observed_at=2711,
            )

            self.assertEqual(first_below["breakout_below_count"], 1)
            self.assertEqual(failed["cycle_status"], "failed")
            self.assertEqual(failed["end_reason"], "two_closes_below_breakout")
            self.assertEqual(repeated_failed_window["status"], "duplicate")
            self.assertEqual(repeated_failed_window["cycle_no"], 1)
            self.assertEqual(len(store.list_observations(opened["cycle_id"])), 3)

    def test_new_trigger_after_failure_opens_next_cycle(self) -> None:
        with TemporaryDirectory() as tmp:
            store = self.make_store(tmp)
            store.record_observation(
                snapshot(window_end_ts=900, score=60, price=100, oi=1_000),
                stage="primed",
                observed_at=910,
            )
            store.record_observation(
                snapshot(window_end_ts=1800, score=30, price=99, oi=990),
                stage="idle",
                observed_at=1810,
            )
            store.record_observation(
                snapshot(window_end_ts=2700, score=30, price=98, oi=980),
                stage="idle",
                observed_at=2710,
            )
            reopened = store.record_observation(
                snapshot(window_end_ts=3600, score=65, price=101, oi=1_050),
                stage="primed",
                observed_at=3610,
            )

            self.assertEqual(reopened["status"], "opened")
            self.assertEqual(reopened["cycle_no"], 2)
            self.assertEqual(store.list_active_symbols(), ["TESTUSDT"])


class LaunchLifecycleRadarIntegrationTests(unittest.TestCase):
    def test_launch_analysis_exposes_absolute_closed_window_values(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = RadarEngine(Settings(data_dir=Path(tmp)), JsonStore(Path(tmp)))
            klines = [
                [0, "100", "102", "99", "100", "0", 0, "5000"]
                for _ in range(16)
            ]
            klines.append([0, "100", "102", "99", "101", "0", 0, "7500"])
            oi_history = [
                {"sumOpenInterestValue": "1000000"}
                for _ in range(16)
            ]
            oi_history.append({"sumOpenInterestValue": "1010000"})

            class Source:
                @staticmethod
                def klines(*_args: object, **_kwargs: object) -> list[list[object]]:
                    return klines

                @staticmethod
                def open_interest_hist(*_args: object, **_kwargs: object) -> list[dict[str, str]]:
                    return oi_history

            result = engine._analyze_launch_symbol(  # type: ignore[arg-type]
                Source(),
                {
                    "symbol": "TESTUSDT",
                    "coin": "TEST",
                    "quote_volume": 10_000_000,
                    "price_24h": 1.0,
                    "price": 101.0,
                    "funding_available": False,
                    "funding_pct": 0.0,
                    "funding_next_time_ms": 0,
                    "mcap": 100_000_000,
                    "mcap_source": "Binance",
                    "market_cap_tier": "低市值",
                    "liquidity_tier": "低流动性",
                },
            )

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result["closed_price"], 101.0)
            self.assertEqual(result["closed_oi_usd"], 1_010_000.0)
            self.assertEqual(result["closed_quote_volume"], 7_500.0)
            self.assertEqual(result["breakout_price"], 102.0)

    def test_active_legacy_symbol_is_scanned_before_higher_volume_candidate(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                signal_events_db_path=Path(tmp) / "signals.db",
                radar_min_quote_volume=1_000_000,
                launch_scan_limit=1,
                launch_lifecycle_v2_enable=True,
            )
            json_store = JsonStore(Path(tmp))
            json_store.save(
                settings.launch_state_path,
                {
                    "TESTUSDT": {
                        "stage": "primed",
                        "first_seen": int(time.time()) - 900,
                        "last_seen": int(time.time()),
                    }
                },
            )
            engine = RadarEngine(settings, json_store)
            analyzed_symbols: list[str] = []

            def fake_analyze(_source: object, item: dict[str, object]) -> dict[str, object]:
                analyzed_symbols.append(str(item["symbol"]))
                return {
                    **item,
                    "score": 65,
                    "closed_price": 1.0,
                    "closed_oi_usd": 1_000_000.0,
                    "closed_quote_volume": 10_000.0,
                    "price_15m": 1.0,
                    "price_1h": 2.0,
                    "oi_15m": 1.0,
                    "oi_1h": 3.0,
                    "volume_ratio": 2.0,
                    "breakout": False,
                    "breakout_price": 1.1,
                    "reasons": ["test"],
                    "window_end_ts": 900,
                    "funding_interval_hours": 8,
                }

            class Source:
                @staticmethod
                def ticker_24h() -> list[dict[str, str]]:
                    return [
                        {
                            "symbol": "HOTUSDT",
                            "quoteVolume": "100000000",
                            "priceChangePercent": "10",
                            "lastPrice": "2",
                        },
                        {
                            "symbol": "TESTUSDT",
                            "quoteVolume": "10",
                            "priceChangePercent": "0",
                            "lastPrice": "1",
                        },
                    ]

                @staticmethod
                def market_caps() -> dict[str, float]:
                    return {"HOT": 1_000_000_000, "TEST": 100_000_000}

            engine._analyze_launch_symbol = fake_analyze  # type: ignore[method-assign]
            result = engine.build_launch_alerts(Source())  # type: ignore[arg-type]

            self.assertEqual(analyzed_symbols, ["TESTUSDT"])
            self.assertEqual(result["diagnostics"]["lifecycle_v2"]["status"], "shadow")
            self.assertEqual(result["diagnostics"]["lifecycle_v2"]["opened"], 1)
            self.assertEqual(
                LaunchLifecycleStore(settings.signal_events_db_path).list_active_symbols(),
                ["TESTUSDT"],
            )


if __name__ == "__main__":
    unittest.main()
