from __future__ import annotations

import sqlite3
import time
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from paopao_radar.config import Settings
from paopao_radar.lifecycle_engine import (
    LifecycleEngine,
    calculate_lifecycle_scores,
    candidate_lifecycle_signals,
    extract_signal_level,
    lifecycle_state_from_scores,
    scan_lifecycles,
)
from paopao_radar.lifecycle_store import LifecycleStore
from paopao_radar.signal_store import SignalEventStore


def settings_for(tmp: str) -> Settings:
    root = Path(tmp)
    return Settings(
        data_dir=root,
        signal_events_path=root / "signals.json",
        signal_events_db_path=root / "signals.db",
        lifecycle_db_path=root / "lifecycle.db",
        tg_push_history_path=root / "push_history.json",
    )


def make_signal(signal_id: int, *, level: str = "15m", text: str = "", ts: int | None = None) -> dict:
    timestamp = int(time.time()) if ts is None else int(ts)
    return {
        "id": signal_id,
        "symbol": "BTCUSDT",
        "status": "sent",
        "module": "launch",
        "template_id": "TG_LAUNCH_ALERT",
        "timeframe": level,
        "signal_type": "launch",
        "stage": "启动确认",
        "score": 80,
        "excerpt": text or f"BTCUSDT {level} lifecycle signal",
        "time": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(timestamp)),
        "ts": timestamp,
    }


def market_metrics(
    *,
    price: float = 100,
    volume: float = 100,
    oi: float = 100,
    futures_cvd: float | None = 10,
    spot_cvd: float | None = 10,
    funding: float = 0.0001,
) -> dict:
    return {
        "symbol": "BTCUSDT",
        "timeframe": "15m",
        "price": price,
        "volume": volume,
        "quote_volume": volume * price,
        "oi": oi,
        "oi_value_usdt": oi * price,
        "futures_cvd_delta": futures_cvd,
        "spot_cvd_delta": spot_cvd,
        "funding_rate": funding,
        "market_cap_usd": 100_000_000,
        "data_source": "binance",
        "data_source_status": "ok",
        "exchange_context": {"items": []},
    }


class LifecycleSchemaV177Tests(unittest.TestCase):
    def test_rank_defaults_and_exact_indexes_are_idempotent(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "lifecycle.db"
            store = LifecycleStore(db)
            store.ensure_schema()
            store.ensure_schema()
            with closing(sqlite3.connect(db)) as conn:
                defaults = {
                    (table, row[1]): row[4]
                    for table in ("signal_lifecycles", "lifecycle_events")
                    for row in conn.execute(f"PRAGMA table_info({table})")
                    if row[1].endswith("level_rank")
                }
                expected_indexes = {
                    "idx_signal_lifecycles_state": ("current_state", "is_active"),
                    "idx_signal_lifecycles_updated": ("updated_at",),
                    "idx_lifecycle_events_symbol_time": ("symbol", "event_time"),
                    "idx_lifecycle_events_type": ("event_type", "event_time"),
                    "idx_lifecycle_snapshots_symbol_tf_time": ("symbol", "timeframe", "snapshot_time"),
                }
                actual_indexes = {
                    name: tuple(row[2] for row in conn.execute(f'PRAGMA index_info("{name}")'))
                    for name in expected_indexes
                }

        self.assertEqual(defaults[("signal_lifecycles", "first_signal_level_rank")], "0")
        self.assertEqual(defaults[("signal_lifecycles", "highest_level_rank")], "0")
        self.assertEqual(defaults[("lifecycle_events", "event_level_rank")], "0")
        self.assertEqual(actual_indexes, expected_indexes)

    def test_wrong_legacy_index_definition_is_repaired(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "lifecycle.db"
            LifecycleStore(db).ensure_schema()
            with closing(sqlite3.connect(db)) as conn:
                conn.execute("DROP INDEX idx_signal_lifecycles_state")
                conn.execute("CREATE INDEX idx_signal_lifecycles_state ON signal_lifecycles(current_state)")
                conn.commit()
            LifecycleStore(db).ensure_schema()
            with closing(sqlite3.connect(db)) as conn:
                columns = tuple(row[2] for row in conn.execute('PRAGMA index_info("idx_signal_lifecycles_state")'))
        self.assertEqual(columns, ("current_state", "is_active"))

    def test_begin_immediate_retries_locked_writer_with_bounded_backoff(self) -> None:
        class FakeConnection:
            def __init__(self) -> None:
                self.calls = 0

            def execute(self, _sql: str):
                self.calls += 1
                if self.calls < 3:
                    raise sqlite3.OperationalError("database is locked")
                return None

        fake = FakeConnection()
        with patch("paopao_radar.lifecycle_store.time.sleep") as sleep:
            LifecycleStore._begin_immediate_with_retry(fake, attempts=4, base_delay_sec=0.01)  # type: ignore[arg-type]
        self.assertEqual(fake.calls, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [0.01, 0.02])


class LifecycleEngineV177Tests(unittest.TestCase):
    def test_level_resolution_falls_through_each_priority_layer(self) -> None:
        self.assertEqual(extract_signal_level({"timeframe": "n/a", "metadata": {"timeframe": "1H"}, "excerpt": "4h"}), ("1h", 2))
        self.assertEqual(extract_signal_level({"timeframe": "n/a", "signal_type": "4小时启动", "excerpt": "24h"}), ("4h", 3))
        self.assertEqual(extract_signal_level({"timeframe": "n/a", "excerpt": "日线确认"}), ("24h", 4))
        self.assertEqual(extract_signal_level({"payload": {"metadata": {"interval": "15min"}}}), ("15m", 1))

    def test_candidate_signal_pagination_reads_more_than_200_without_loss(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = settings_for(tmp)
            signal_store = SignalEventStore(settings.signal_events_db_path)
            now = int(time.time())
            with signal_store.connect() as conn:
                conn.executemany(
                    """
                    INSERT INTO signals (
                        ts, time, module, template_id, signal_type, symbol, dedup_key, status, sent, excerpt
                    ) VALUES (?, ?, 'launch', 'TG_LAUNCH_ALERT', 'launch', 'BTCUSDT', ?, 'sent', 1, 'BTCUSDT 15m')
                    """,
                    [
                        (now - 500 + index, f"2026-07-10T00:{index % 60:02d}:00+00:00", f"page:{index}")
                        for index in range(450)
                    ],
                )

            rows = candidate_lifecycle_signals(settings=settings, lookback_hours=24, limit=450)

        self.assertEqual(len(rows), 450)
        self.assertEqual(len({row["id"] for row in rows}), 450)
        self.assertEqual([row["id"] for row in rows], sorted(row["id"] for row in rows))

    def test_scoring_uses_only_actual_upgrades_and_cannot_skip_to_trend(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = settings_for(tmp)
            direct_4h, _, _ = calculate_lifecycle_scores(
                signal={"score": None},
                first_level="4h",
                highest_level="4h",
                metrics={"upgrade_levels": []},
                previous=None,
                settings=settings,
            )
            upgraded, _, _ = calculate_lifecycle_scores(
                signal={"score": None},
                first_level="15m",
                highest_level="4h",
                metrics={"upgrade_levels": ["4h"]},
                previous={},
                settings=settings,
            )
            unknown, _, _ = calculate_lifecycle_scores(
                signal={"score": None},
                first_level="unknown",
                highest_level="unknown",
                metrics={"upgrade_levels": []},
                previous=None,
                settings=settings,
            )
            state = lifecycle_state_from_scores(
                current_state="warming",
                lifecycle_score=100,
                risk_score=0,
                metrics={"price_change_from_first_pct": 3},
                signal={"timeframe": "15m"},
                settings=settings,
            )

        self.assertEqual(direct_4h, 30)
        self.assertEqual(upgraded, 25)
        self.assertEqual(unknown, 0)
        self.assertEqual(state, "launching")

    def test_one_signal_can_record_primary_and_multiple_metric_events(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = settings_for(tmp)
            queue = [market_metrics(), market_metrics(price=105, volume=250, oi=120, futures_cvd=25, spot_cvd=30)]
            engine = LifecycleEngine(settings, metrics_provider=lambda _symbol, _timeframe: queue.pop(0))
            engine.process_signal(make_signal(1))
            result = engine.process_signal(make_signal(2))
            event_types = {event["event_type"] for event in result["events"]}

        self.assertIn("same_level_confirm", event_types)
        self.assertIn("volume_expansion", event_types)
        self.assertIn("oi_accumulation", event_types)
        self.assertIn("futures_cvd_confirmed", event_types)
        self.assertIn("spot_cvd_confirmed", event_types)
        self.assertEqual(result["events_inserted"], len(event_types))

    def test_major_weakening_and_price_failure_close_lifecycle(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = settings_for(tmp)
            queue = [market_metrics(), market_metrics(price=101), market_metrics(price=90)]
            engine = LifecycleEngine(settings, metrics_provider=lambda _symbol, _timeframe: queue.pop(0))
            engine.process_signal(make_signal(1, level="4h"))
            weakening = engine.process_signal(make_signal(2, level="4h", text="BTCUSDT 4h 走弱"))
            failed = engine.process_signal(make_signal(3, level="4h"))
            lifecycle = LifecycleStore(settings.lifecycle_db_path).get_lifecycle("BTCUSDT") or {}

        self.assertEqual(weakening["event"]["event_type"], "major_timeframe_weakening")
        self.assertEqual(failed["event"]["event_type"], "launch_failed")
        self.assertEqual(lifecycle["current_state"], "failed")
        self.assertEqual(lifecycle["is_active"], 0)
        self.assertTrue(lifecycle["closed_at"])

    def test_peak_pullback_and_negative_cvd_with_falling_oi_cool(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = settings_for(tmp)
            queue = [
                market_metrics(price=100, oi=100),
                market_metrics(price=120, oi=110, futures_cvd=20),
                market_metrics(price=113, oi=90, futures_cvd=-10),
            ]
            engine = LifecycleEngine(settings, metrics_provider=lambda _symbol, _timeframe: queue.pop(0))
            engine.process_signal(make_signal(1))
            engine.process_signal(make_signal(2))
            cooled = engine.process_signal(make_signal(3))
            lifecycle = LifecycleStore(settings.lifecycle_db_path).get_lifecycle("BTCUSDT") or {}

        self.assertIn("short_term_weakening", {event["event_type"] for event in cooled["events"]})
        self.assertEqual(lifecycle["current_state"], "cooling")
        self.assertLessEqual(lifecycle["metrics"]["pullback_from_peak_pct"], -5)

    def test_signal_density_adds_risk(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = settings_for(tmp)
            engine = LifecycleEngine(settings, metrics_provider=lambda _symbol, _timeframe: market_metrics())
            now = int(time.time())
            for signal_id in range(1, 5):
                engine.process_signal(make_signal(signal_id, ts=now - 5 + signal_id))
            lifecycle = LifecycleStore(settings.lifecycle_db_path).get_lifecycle("BTCUSDT") or {}

        self.assertEqual(lifecycle["metrics"]["signal_density_1h"], 4)
        self.assertGreaterEqual(lifecycle["risk_score"], 10)

    def test_unknown_to_15m_never_creates_generic_upgrade(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = settings_for(tmp)
            engine = LifecycleEngine(settings, metrics_provider=lambda _symbol, _timeframe: market_metrics())
            engine.process_signal(make_signal(1, level="unknown", text="BTCUSDT lifecycle signal"))
            result = engine.process_signal(make_signal(2, level="15m"))

        self.assertEqual(result["event"]["event_type"], "same_level_confirm")
        self.assertNotIn("timeframe_upgrade", {event["event_type"] for event in result["events"]})

    def test_scan_refreshes_active_lifecycle_without_new_signal_in_one_batch(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = settings_for(tmp)
            LifecycleEngine(settings, metrics_provider=lambda _symbol, _timeframe: market_metrics()).process_signal(make_signal(1))
            provider_calls: list[tuple[str, str]] = []

            def provider(symbol: str, timeframe: str) -> dict:
                provider_calls.append((symbol, timeframe))
                return market_metrics(price=106, volume=250, oi=120, futures_cvd=25, spot_cvd=30)

            with patch("paopao_radar.lifecycle_engine.candidate_lifecycle_signals", return_value=[]):
                result = scan_lifecycles(settings=settings, metrics_provider=provider, limit_symbols=80)
            lifecycle = LifecycleStore(settings.lifecycle_db_path).get_lifecycle("BTCUSDT") or {}

        self.assertEqual(provider_calls, [("BTCUSDT", "15m")])
        self.assertEqual(result["counts"]["active_refresh"], 1)
        self.assertEqual(result["counts"]["refreshed"], 1)
        self.assertGreaterEqual(result["counts"]["events"], 2)
        self.assertEqual(lifecycle["latest_price"], 106)


if __name__ == "__main__":
    unittest.main()
