from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from config import Settings
from radars.launch_warning.lifecycle import LaunchLifecycleStore
from runtime.radar_engine import RadarEngine
from runtime.signal_effectiveness import SignalOutcomeTracker
from shared.signal_store import SignalEventStore
from shared.storage import JsonStore
from shared.telegram import plain_fallback


def snapshot(
    *,
    symbol: str = "TESTUSDT",
    window_end_ts: int,
    score: int,
    price: float,
    oi: float,
    asset_class: str = "",
    liquidity_tier: str = "",
    trigger_path: str = "",
    price_action_analysis: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "window_end_ts": window_end_ts,
        "score": score,
        "closed_price": price,
        "closed_oi_usd": oi,
        "closed_quote_volume": 1_000_000,
        "price_15m": 1.0,
        "price_1h": 2.0,
        "oi_15m": 1.0,
        "oi_1h": 2.0,
        "volume_ratio": 2.0,
        "funding_pct": -0.01,
        "funding_interval_hours": 4,
        "data_quality_status": "confirmed",
        "data_quality_score": 100,
        "quality_gate": "allow",
        "primary_data_source": "binance_native",
        "asset_class": asset_class,
        "liquidity_tier": liquidity_tier,
        "trigger_path": trigger_path,
        "price_action_analysis": price_action_analysis,
    }


def record(
    store: LaunchLifecycleStore,
    *,
    symbol: str,
    window_end_ts: int,
    score: int,
    price: float,
    oi: float,
    stage: str,
    asset_class: str = "",
    liquidity_tier: str = "",
    trigger_path: str = "",
    price_action_analysis: dict[str, object] | None = None,
) -> dict[str, object]:
    return store.record_observation(
        snapshot(
            symbol=symbol,
            window_end_ts=window_end_ts,
            score=score,
            price=price,
            oi=oi,
            asset_class=asset_class,
            liquidity_tier=liquidity_tier,
            trigger_path=trigger_path,
            price_action_analysis=price_action_analysis,
        ),
        stage=stage,
        observed_at=window_end_ts + 10,
    )


def finish_cycle(
    store: LaunchLifecycleStore,
    *,
    symbol: str,
    start: int,
    confirmed: bool,
    follow_through: bool,
    asset_class: str = "",
    liquidity_tier: str = "",
    trigger_path: str = "",
) -> dict[str, object]:
    record(
        store,
        symbol=symbol,
        window_end_ts=start,
        score=60,
        price=100,
        oi=1_000,
        stage="primed",
        asset_class=asset_class,
        liquidity_tier=liquidity_tier,
        trigger_path=trigger_path,
    )
    next_window = start + 900
    if confirmed:
        record(
            store,
            symbol=symbol,
            window_end_ts=next_window,
            score=75,
            price=104 if follow_through else 101,
            oi=1_100,
            stage="breakout",
            asset_class=asset_class,
            liquidity_tier=liquidity_tier,
            trigger_path=trigger_path,
        )
        next_window += 900
    record(
        store,
        symbol=symbol,
        window_end_ts=next_window,
        score=40,
        price=102 if follow_through else 99,
        oi=950,
        stage="idle",
        asset_class=asset_class,
        liquidity_tier=liquidity_tier,
        trigger_path=trigger_path,
    )
    return record(
        store,
        symbol=symbol,
        window_end_ts=next_window + 900,
        score=40,
        price=101 if follow_through else 98,
        oi=900,
        stage="idle",
        asset_class=asset_class,
        liquidity_tier=liquidity_tier,
        trigger_path=trigger_path,
    )


def price_action_breakout(window_end_ts: int) -> dict[str, object]:
    return {
        "data_status": "ready",
        "lookback": 2,
        "min_body_ratio": 0.45,
        "wick_body_ratio": 1.5,
        "timeframes": {
            "15m": {
                "data_status": "ready",
                "event": "breakout_up",
                "candle_end_ts": window_end_ts,
                "box_high": 100.0,
                "box_low": 95.0,
            },
            "1h": {"data_status": "insufficient_history"},
        },
    }


def price_action_confirmed(window_end_ts: int) -> dict[str, object]:
    return {
        "data_status": "ready",
        "lookback": 2,
        "min_body_ratio": 0.45,
        "wick_body_ratio": 1.5,
        "timeframes": {
            "15m": {
                "data_status": "ready",
                "event": "inside",
                "candle_end_ts": window_end_ts,
                "open": 102.0,
                "high": 106.0,
                "low": 101.0,
                "close": 105.0,
                "body_ratio": 0.6,
            },
            "1h": {
                "data_status": "ready",
                "event": "inside",
                "candle_end_ts": window_end_ts,
                "open": 100.0,
                "high": 106.0,
                "low": 99.0,
                "close": 105.0,
                "body_ratio": 0.7,
                "upper_wick_body_ratio": 0.2,
                "lower_wick_body_ratio": 0.2,
            },
        },
    }


class LaunchOutcomeTests(unittest.TestCase):
    def test_bearish_directional_cycle_normalizes_favorable_return(self) -> None:
        with TemporaryDirectory() as tmp:
            store = LaunchLifecycleStore(
                Path(tmp) / "signals.db",
                outcome_enabled=True,
                invalid_windows_required=2,
            )
            result: dict[str, object] = {}
            for window, score, price in (
                (900, 70, 100.0),
                (1800, 70, 90.0),
                (2700, 0, 92.0),
                (3600, 0, 90.0),
            ):
                result = record(
                    store,
                    symbol="BEARUSDT",
                    window_end_ts=window,
                    score=score,
                    price=price,
                    oi=1_000,
                    stage="primed" if score else "idle",
                    trigger_path="directional:bearish",
                )

            evaluation = result["outcome_evaluation"]
            assert isinstance(evaluation, dict)
            outcome = evaluation["outcome"]
            assert isinstance(outcome, dict)
            self.assertAlmostEqual(outcome["max_favorable_return_pct"], 10.0)
            self.assertAlmostEqual(outcome["max_adverse_return_pct"], 0.0)
            self.assertAlmostEqual(outcome["end_return_pct"], 10.0)

    def test_completed_cycle_is_one_exact_idempotent_outcome(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signals.db"
            store = LaunchLifecycleStore(
                db_path,
                outcome_enabled=True,
                outcome_follow_through_pct=3.0,
            )
            record(
                store,
                symbol="TESTUSDT",
                window_end_ts=900,
                score=60,
                price=100,
                oi=1_000,
                stage="primed",
            )
            record(
                store,
                symbol="TESTUSDT",
                window_end_ts=1800,
                score=75,
                price=104,
                oi=1_200,
                stage="breakout",
            )
            record(
                store,
                symbol="TESTUSDT",
                window_end_ts=2700,
                score=40,
                price=102,
                oi=1_100,
                stage="idle",
            )
            failed = record(
                store,
                symbol="TESTUSDT",
                window_end_ts=3600,
                score=40,
                price=101,
                oi=900,
                stage="idle",
            )
            duplicate = record(
                store,
                symbol="TESTUSDT",
                window_end_ts=3600,
                score=40,
                price=101,
                oi=900,
                stage="idle",
            )

            evaluation = failed["outcome_evaluation"]
            outcome = evaluation["outcome"]
            self.assertEqual(evaluation["status"], "evaluated")
            self.assertEqual(outcome["label"], "confirmed_follow_through")
            self.assertEqual(outcome["observation_count"], 4)
            self.assertAlmostEqual(outcome["max_favorable_return_pct"], 4.0)
            self.assertAlmostEqual(outcome["max_adverse_return_pct"], 0.0)
            self.assertAlmostEqual(outcome["end_return_pct"], 1.0)
            self.assertAlmostEqual(outcome["max_oi_increase_pct"], 20.0)
            self.assertAlmostEqual(outcome["max_oi_decrease_pct"], -10.0)
            self.assertEqual(outcome["time_to_confirm_sec"], 900)
            self.assertIsNone(outcome["time_to_launch_sec"])
            self.assertEqual(
                evaluation["reliability"]["completed_samples"],
                0,
            )
            self.assertEqual(duplicate["status"], "duplicate")
            with closing(sqlite3.connect(db_path)) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM launch_lifecycle_outcomes"
                    ).fetchone()[0],
                    1,
                )

    def test_fusion_outcome_requires_independent_closed_one_hour_confirmation(self) -> None:
        with TemporaryDirectory() as tmp:
            store = LaunchLifecycleStore(
                Path(tmp) / "signals.db",
                fusion_enabled=True,
                price_action_enabled=True,
                outcome_enabled=True,
            )
            record(
                store,
                symbol="UNCONFIRMEDUSDT",
                window_end_ts=900,
                score=95,
                price=100,
                oi=1_000,
                stage="launched",
                price_action_analysis=price_action_breakout(900),
            )
            record(
                store,
                symbol="UNCONFIRMEDUSDT",
                window_end_ts=1_800,
                score=95,
                price=104,
                oi=1_100,
                stage="launched",
                price_action_analysis=price_action_breakout(900),
            )
            record(
                store,
                symbol="UNCONFIRMEDUSDT",
                window_end_ts=2_700,
                score=40,
                price=102,
                oi=1_050,
                stage="idle",
            )
            unconfirmed = record(
                store,
                symbol="UNCONFIRMEDUSDT",
                window_end_ts=3_600,
                score=40,
                price=101,
                oi=1_000,
                stage="idle",
            )

            record(
                store,
                symbol="CONFIRMEDUSDT",
                window_end_ts=900,
                score=95,
                price=100,
                oi=1_000,
                stage="launched",
                price_action_analysis=price_action_breakout(900),
            )
            record(
                store,
                symbol="CONFIRMEDUSDT",
                window_end_ts=1_800,
                score=95,
                price=104,
                oi=1_100,
                stage="launched",
                price_action_analysis=price_action_confirmed(1_800),
            )
            record(
                store,
                symbol="CONFIRMEDUSDT",
                window_end_ts=2_700,
                score=40,
                price=102,
                oi=1_050,
                stage="idle",
            )
            confirmed = record(
                store,
                symbol="CONFIRMEDUSDT",
                window_end_ts=3_600,
                score=40,
                price=101,
                oi=1_000,
                stage="idle",
            )

            self.assertFalse(
                unconfirmed["outcome_evaluation"]["outcome"]["confirmed"]
            )
            self.assertIsNone(
                unconfirmed["outcome_evaluation"]["outcome"]["confirmed_at"]
            )
            self.assertEqual(
                unconfirmed["outcome_evaluation"]["outcome"]["first_stage"],
                "primed",
            )
            self.assertTrue(
                confirmed["outcome_evaluation"]["outcome"]["confirmed"]
            )
            self.assertEqual(
                confirmed["outcome_evaluation"]["outcome"]["confirmed_at"],
                1_800,
            )

    def test_reliability_uses_asset_liquidity_cohort_at_minimum_samples(self) -> None:
        with TemporaryDirectory() as tmp:
            store = LaunchLifecycleStore(
                Path(tmp) / "signals.db",
                outcome_enabled=True,
                outcome_min_samples=2,
            )
            finish_cycle(
                store,
                symbol="ALTLOWAUSDT",
                start=900,
                confirmed=True,
                follow_through=True,
                asset_class="altcoin",
                liquidity_tier="low",
            )
            finish_cycle(
                store,
                symbol="ALTLOWBUSDT",
                start=900,
                confirmed=False,
                follow_through=False,
                asset_class="altcoin",
                liquidity_tier="low",
            )
            finish_cycle(
                store,
                symbol="COREHIGHUSDT",
                start=900,
                confirmed=False,
                follow_through=False,
                asset_class="core_crypto",
                liquidity_tier="high",
            )
            current = record(
                store,
                symbol="CURRENTUSDT",
                window_end_ts=5_400,
                score=60,
                price=100,
                oi=1_000,
                stage="primed",
                asset_class="altcoin",
                liquidity_tier="low",
            )

            reliability = current["outcome_evaluation"]["reliability"]
            self.assertEqual(reliability["aggregation_scope"], "asset_liquidity")
            self.assertEqual(reliability["cohort_status"], "used")
            self.assertEqual(reliability["completed_samples"], 2)
            self.assertEqual(reliability["global_completed_samples"], 3)
            self.assertEqual(reliability["confirmed_rate_pct"], 50.0)

    def test_reliability_marks_global_fallback_when_cohort_is_too_small(self) -> None:
        with TemporaryDirectory() as tmp:
            store = LaunchLifecycleStore(
                Path(tmp) / "signals.db",
                outcome_enabled=True,
                outcome_min_samples=2,
            )
            finish_cycle(
                store,
                symbol="ALTLOWUSDT",
                start=900,
                confirmed=True,
                follow_through=True,
                asset_class="altcoin",
                liquidity_tier="low",
            )
            finish_cycle(
                store,
                symbol="COREHIGHUSDT",
                start=900,
                confirmed=False,
                follow_through=False,
                asset_class="core_crypto",
                liquidity_tier="high",
            )
            current = record(
                store,
                symbol="CURRENTUSDT",
                window_end_ts=5_400,
                score=60,
                price=100,
                oi=1_000,
                stage="primed",
                asset_class="altcoin",
                liquidity_tier="low",
            )

            reliability = current["outcome_evaluation"]["reliability"]
            self.assertEqual(reliability["aggregation_scope"], "same_rule_global")
            self.assertEqual(reliability["cohort_status"], "fallback_global")
            self.assertEqual(reliability["cohort_completed_samples"], 1)
            self.assertEqual(reliability["completed_samples"], 2)
            self.assertTrue(reliability["rates_available"])

    def test_directional_reliability_excludes_opposite_direction_cycles(self) -> None:
        with TemporaryDirectory() as tmp:
            store = LaunchLifecycleStore(
                Path(tmp) / "signals.db",
                outcome_enabled=True,
                outcome_min_samples=1,
                directional_enabled=True,
            )
            finish_cycle(
                store,
                symbol="BULLUSDT",
                start=900,
                confirmed=True,
                follow_through=True,
                asset_class="altcoin",
                liquidity_tier="low",
                trigger_path="directional:bullish",
            )
            finish_cycle(
                store,
                symbol="BEARUSDT",
                start=900,
                confirmed=False,
                follow_through=False,
                asset_class="altcoin",
                liquidity_tier="low",
                trigger_path="directional:bearish",
            )
            current = record(
                store,
                symbol="CURRENTUSDT",
                window_end_ts=5_400,
                score=60,
                price=100,
                oi=1_000,
                stage="primed",
                asset_class="altcoin",
                liquidity_tier="low",
                trigger_path="directional:bullish",
            )

            reliability = current["outcome_evaluation"]["reliability"]
            self.assertTrue(reliability["direction_filtered"])
            self.assertEqual(reliability["direction"], "bullish")
            self.assertEqual(reliability["completed_samples"], 1)
            self.assertEqual(reliability["global_completed_samples"], 1)
            self.assertEqual(reliability["confirmed_rate_pct"], 100.0)
            self.assertEqual(reliability["followed_through_rate_pct"], 100.0)

    def test_existing_outcome_table_receives_additive_cohort_columns(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signals.db"
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    """
                    CREATE TABLE launch_lifecycle_outcomes (
                        cycle_id INTEGER PRIMARY KEY,
                        rule_key TEXT NOT NULL,
                        symbol TEXT NOT NULL,
                        evaluated_at INTEGER NOT NULL
                    )
                    """
                )
                conn.commit()

            store = LaunchLifecycleStore(db_path)
            with store.connect() as conn:
                columns = {
                    str(row["name"])
                    for row in conn.execute(
                        "PRAGMA table_info(launch_lifecycle_outcomes)"
                    ).fetchall()
                }

            self.assertTrue(
                {"asset_class", "liquidity_tier", "trigger_path"}.issubset(columns)
            )

    def test_reliability_hides_rates_until_same_rule_minimum_is_met(self) -> None:
        with TemporaryDirectory() as tmp:
            store = LaunchLifecycleStore(
                Path(tmp) / "signals.db",
                outcome_enabled=True,
                outcome_follow_through_pct=3.0,
                outcome_min_samples=2,
            )
            finish_cycle(
                store,
                symbol="AAAUSDT",
                start=900,
                confirmed=True,
                follow_through=True,
            )
            finish_cycle(
                store,
                symbol="BBBUSDT",
                start=900,
                confirmed=False,
                follow_through=False,
            )
            current = record(
                store,
                symbol="CCCUSDT",
                window_end_ts=900,
                score=60,
                price=100,
                oi=1_000,
                stage="primed",
            )

            reliability = current["outcome_evaluation"]["reliability"]
            self.assertTrue(reliability["rates_available"])
            self.assertEqual(reliability["completed_samples"], 2)
            self.assertEqual(reliability["confirmed_count"], 1)
            self.assertEqual(reliability["followed_through_count"], 1)
            self.assertEqual(reliability["confirmed_rate_pct"], 50.0)
            self.assertEqual(reliability["followed_through_rate_pct"], 50.0)
            self.assertEqual(reliability["symbol_completed_samples"], 0)

    def test_rule_change_does_not_relabel_historical_cycles(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signals.db"
            original = LaunchLifecycleStore(
                db_path,
                outcome_enabled=True,
                outcome_follow_through_pct=3.0,
                outcome_min_samples=1,
            )
            finish_cycle(
                original,
                symbol="OLDUSDT",
                start=900,
                confirmed=True,
                follow_through=True,
            )
            changed = LaunchLifecycleStore(
                db_path,
                outcome_enabled=True,
                outcome_follow_through_pct=5.0,
                outcome_min_samples=1,
            )
            refreshed = changed.refresh_outcomes(evaluated_at=5_000)
            current = record(
                changed,
                symbol="NEWUSDT",
                window_end_ts=4_500,
                score=60,
                price=100,
                oi=1_000,
                stage="primed",
            )

            self.assertEqual(refreshed["evaluated"], 0)
            self.assertEqual(refreshed["same_rule_samples"], 0)
            self.assertEqual(
                current["outcome_evaluation"]["reliability"]["completed_samples"],
                0,
            )
            with closing(sqlite3.connect(db_path)) as conn:
                rule_key = conn.execute(
                    """
                    SELECT rule_key FROM launch_lifecycle_outcomes
                    WHERE symbol = 'OLDUSDT'
                    """
                ).fetchone()[0]
            self.assertIn("follow=3", rule_key)

    def test_active_cycle_keeps_its_opening_thresholds_after_config_change(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signals.db"
            original = LaunchLifecycleStore(
                db_path,
                watch_score=45,
                start_score=60,
                invalid_windows_required=2,
                outcome_enabled=True,
            )
            first = record(
                original,
                symbol="HOLDUSDT",
                window_end_ts=900,
                score=60,
                price=100,
                oi=1_000,
                stage="primed",
            )
            changed = LaunchLifecycleStore(
                db_path,
                watch_score=70,
                start_score=80,
                invalid_windows_required=1,
                outcome_enabled=True,
            )
            second = record(
                changed,
                symbol="HOLDUSDT",
                window_end_ts=1_800,
                score=60,
                price=101,
                oi=1_010,
                stage="idle",
            )

            self.assertEqual(second["cycle_id"], first["cycle_id"])
            self.assertEqual(second["cycle_status"], "active")
            self.assertEqual(second["current_stage"], "primed")
            self.assertEqual(second["invalid_window_count"], 0)
            self.assertIn(
                "watch=45",
                second["outcome_evaluation"]["reliability"]["rule_key"],
            )

    def test_refresh_backfills_cycles_completed_before_p24_enablement(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signals.db"
            disabled = LaunchLifecycleStore(db_path, outcome_enabled=False)
            finish_cycle(
                disabled,
                symbol="OLDUSDT",
                start=900,
                confirmed=False,
                follow_through=False,
            )
            with closing(sqlite3.connect(db_path)) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM launch_lifecycle_outcomes"
                    ).fetchone()[0],
                    0,
                )

            enabled = LaunchLifecycleStore(db_path, outcome_enabled=True)
            first = enabled.refresh_outcomes(evaluated_at=5_000)
            second = enabled.refresh_outcomes(evaluated_at=5_100)

            self.assertEqual(first["evaluated"], 1)
            self.assertEqual(first["completed_cycles"], 1)
            self.assertEqual(first["same_rule_samples"], 1)
            self.assertFalse(first["rates_available"])
            self.assertEqual(second["evaluated"], 0)

    def test_retention_prunes_only_expired_completed_cycles(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signals.db"
            store = LaunchLifecycleStore(db_path, outcome_enabled=True)
            finish_cycle(
                store,
                symbol="OLDUSDT",
                start=900,
                confirmed=False,
                follow_through=False,
            )
            active = record(
                store,
                symbol="LIVEUSDT",
                window_end_ts=3_600,
                score=60,
                price=100,
                oi=1_000,
                stage="primed",
            )

            result = SignalEventStore(db_path).prune(
                before_ts=3_000,
                max_rows=100,
            )

            self.assertEqual(result["launch_cycles_expired"], 1)
            self.assertEqual(store.list_active_symbols(), ["LIVEUSDT"])
            with closing(sqlite3.connect(db_path)) as conn:
                self.assertEqual(
                    conn.execute(
                        "SELECT COUNT(*) FROM launch_lifecycle_outcomes"
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    conn.execute(
                        """
                        SELECT COUNT(*) FROM launch_lifecycle_observations
                        WHERE cycle_id = ?
                        """,
                        (active["cycle_id"],),
                    ).fetchone()[0],
                    1,
                )

    def test_latest_message_explains_progress_and_sample_gate(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                launch_message_package_v2_enable=True,
                launch_outcome_v2_enable=True,
                launch_outcome_min_samples=20,
            )
            engine = RadarEngine(settings, JsonStore(Path(tmp)))
            point = {
                "observation_id": 1,
                "observation_no": 1,
                "checkpoint_no": 1,
                "window_end_ts": 900,
                "stage": "primed",
                "status": "active",
                "score": 60,
                "price": 100.0,
                "oi_usd": 1_000.0,
                "funding_pct": -0.01,
                "funding_interval_hours": 4,
                "funds_direction": "both_buy",
            }
            lifecycle = {
                "cycle_id": 1,
                "cycle_no": 1,
                "cycle_status": "active",
                "current_stage": "primed",
                "peak_stage": "primed",
                "duration_sec": 900,
                "outcome_evaluation": {
                    "enabled": True,
                    "status": "tracking",
                    "progress": {
                        "cycle_status": "active",
                        "observation_count": 2,
                        "duration_sec": 900,
                        "max_favorable_return_pct": 2.5,
                        "max_adverse_return_pct": -0.5,
                        "end_return_pct": 2.0,
                        "max_oi_increase_pct": 6.0,
                        "max_oi_decrease_pct": 0.0,
                        "peak_score": 65,
                        "confirmed": False,
                        "launched": False,
                        "followed_through": False,
                        "confirmed_at": None,
                        "launched_at": None,
                        "time_to_confirm_sec": None,
                        "time_to_launch_sec": None,
                    },
                    "outcome": None,
                    "reliability": {
                        "status": "accumulating",
                        "completed_samples": 2,
                        "minimum_samples": 20,
                        "rates_available": False,
                        "confirmed_count": 1,
                        "launched_count": 0,
                        "followed_through_count": 1,
                        "follow_through_threshold_pct": 3.0,
                    },
                },
            }
            text = engine._format_launch_alert({
                "symbol": "TESTUSDT",
                "coin": "TEST",
                "score": 60,
                "stage": "primed",
                "launch_message_package_v2": True,
                "launch_lifecycle": lifecycle,
                "launch_package": {
                    "checkpoint_no": 1,
                    "checkpoint_reasons": ["cycle_opened"],
                    "first": point,
                    "previous_published": point,
                    "current": point,
                    "checkpoints": [],
                },
                "data_confirmation": {
                    "confirmed_count": 5,
                    "expected_count": 5,
                    "status": "complete",
                },
            })

            self.assertIn("本轮进展", text)
            self.assertIn("最高/最低收盘变动: +2.50% / -0.50%", text)
            self.assertIn("首次达到启动确认: 尚未达到", text)
            self.assertIn("首次达到启动瞬间: 尚未达到", text)
            self.assertIn("样本积累中｜同口径已完成 2/20 轮", text)
            self.assertIn("样本未达门槛，不展示比例", text)
            self.assertNotIn("启动确认率", text)
            self.assertLessEqual(len(plain_fallback(text)), 1024)

    def test_stage_delay_copy_distinguishes_immediate_and_elapsed_reach(self) -> None:
        self.assertEqual(
            RadarEngine._launch_package_stage_delay(0),
            "首次信号即达到",
        )
        self.assertEqual(
            RadarEngine._launch_package_stage_delay(15 * 60),
            "15分钟后",
        )
        self.assertEqual(
            RadarEngine._launch_package_stage_delay(60 * 60),
            "1小时00分钟后",
        )

    def test_completed_message_explains_final_cycle_result(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = LaunchLifecycleStore(
                root / "signals.db",
                package_enabled=True,
                outcome_enabled=True,
            )
            failed = finish_cycle(
                store,
                symbol="DONEUSDT",
                start=900,
                confirmed=True,
                follow_through=True,
            )
            settings = Settings(
                data_dir=root,
                launch_message_package_v2_enable=True,
                launch_outcome_v2_enable=True,
            )
            text = RadarEngine(settings, JsonStore(root))._format_launch_alert({
                "symbol": "DONEUSDT",
                "coin": "DONE",
                "score": 40,
                "stage": "failed",
                "launch_message_package_v2": True,
                "launch_lifecycle": failed,
                "launch_package": failed["publication"],
                "data_confirmation": {
                    "confirmed_count": 5,
                    "expected_count": 5,
                    "status": "complete",
                },
            })

            self.assertIn("本轮结果", text)
            self.assertIn("达到启动确认且价格完成跟随", text)
            self.assertIn("结束收益: +1.00%", text)
            self.assertIn("本轮结束", text)
            self.assertIn(
                "失效原因: 连续两个15分钟闭合窗口低于观察阈值，"
                "本轮启动跟踪结束",
                text,
            )
            self.assertNotIn("two_windows_below_watch_score", text)
            self.assertLessEqual(len(plain_fallback(text)), 1024)

    def test_package_messages_are_not_double_counted_as_generic_outcomes(self) -> None:
        with TemporaryDirectory() as tmp:
            signal_db = Path(tmp) / "signals.db"
            store = SignalEventStore(signal_db)
            store.append_from_push(
                template_id="TG_LAUNCH_ALERT",
                dedup_key="launch-package:1:2",
                status="sent",
                sent=True,
                text="TESTUSDT lifecycle package",
                ts=1_000,
                structured_records=[{
                    "symbol": "TESTUSDT",
                    "stage": "breakout",
                    "score": 75,
                    "price": 100,
                    "quality_gate": "allow",
                    "launch_message_package_v2": True,
                    "launch_cycle_id": 1,
                    "launch_observation_id": 2,
                }],
            )
            with closing(sqlite3.connect(signal_db)) as conn:
                signal_id = conn.execute(
                    "SELECT id FROM signals WHERE dedup_key = 'launch-package:1:2'"
                ).fetchone()[0]
                conn.execute(
                    """
                    INSERT INTO signal_outcomes (
                        signal_id, horizon, horizon_sec, due_at, status, direction
                    ) VALUES (?, '15m', 900, 1900, 'pending', 'long')
                    """,
                    (signal_id,),
                )
                conn.commit()

            result = SignalOutcomeTracker(
                signal_db,
                Path(tmp) / "market.db",
            ).refresh(now_ts=2_000)
            self.assertEqual(result["signals_tracked"], 0)
            self.assertEqual(result["lifecycle_package_outcomes_removed"], 1)
            with closing(sqlite3.connect(signal_db)) as conn:
                self.assertEqual(
                    conn.execute("SELECT COUNT(*) FROM signal_outcomes").fetchone()[0],
                    0,
                )


if __name__ == "__main__":
    unittest.main()
