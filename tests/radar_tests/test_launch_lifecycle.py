from __future__ import annotations

import time
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from config import Settings
from radars.launch_warning.chart import PNG_SIGNATURE
from radars.launch_warning.lifecycle import LaunchLifecycleStore
from runtime.radar_engine import RadarEngine
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
    funding_pct: float = -0.1,
    funding_interval_hours: int = 8,
    breakout: bool = False,
    breakout_price: float = 0.0,
    quality_gate: str = "allow",
    spot_active_net_usd: float | None = None,
    futures_active_net_usd: float | None = None,
    funds_direction: str = "unknown",
    price_action_analysis: dict[str, object] | None = None,
    asset_class: str = "altcoin",
    liquidity_tier: str = "中流动性",
    trigger_path: str = "momentum",
    price_oi_quadrant: str = "price_up_oi_up",
    counter_evidence: list[str] | None = None,
    directional_readiness: dict[str, object] | None = None,
    invalidation_price: float | None = None,
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
        "spot_active_net_usd": spot_active_net_usd,
        "futures_active_net_usd": futures_active_net_usd,
        "funds_direction": funds_direction,
        "price_action_analysis": price_action_analysis,
        "asset_class": asset_class,
        "liquidity_tier": liquidity_tier,
        "trigger_path": trigger_path,
        "price_oi_quadrant": price_oi_quadrant,
        "counter_evidence": list(counter_evidence or []),
        "directional_readiness": directional_readiness,
        "invalidation_price": invalidation_price,
    }


def directional_signal(direction: str, *, confirmed: bool = True) -> dict[str, object]:
    return {
        "status": "confirmed" if confirmed else "candidate",
        "direction": direction,
        "data_complete": True,
        "hard_gates": {
            "bullish_passed": confirmed and direction.startswith("bullish"),
            "bearish_passed": confirmed and direction.startswith("bearish"),
        },
    }


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

    def test_fusion_score_cannot_replace_closed_one_hour_confirmation(self) -> None:
        with TemporaryDirectory() as tmp:
            store = LaunchLifecycleStore(
                Path(tmp) / "signals.db",
                fusion_enabled=True,
                price_action_enabled=True,
                launched_score=90,
            )
            opened = store.record_observation(
                snapshot(
                    window_end_ts=2700,
                    score=95,
                    price=103,
                    oi=1_050,
                    price_action_analysis=price_action_breakout(2700),
                ),
                stage="launched",
                observed_at=2710,
            )
            confirmed = store.record_observation(
                snapshot(
                    window_end_ts=3600,
                    score=85,
                    price=105,
                    oi=1_100,
                    price_action_analysis=price_action_confirmed(3600),
                ),
                stage="breakout",
                observed_at=3610,
            )
            accelerated = store.record_observation(
                snapshot(
                    window_end_ts=4500,
                    score=95,
                    price=108,
                    oi=1_180,
                    price_action_analysis=price_action_confirmed(3600),
                ),
                stage="launched",
                observed_at=4510,
            )

            self.assertEqual(opened["current_stage"], "primed")
            self.assertEqual(opened["confirmation_status"], "awaiting_1h")
            self.assertEqual(confirmed["current_stage"], "breakout")
            self.assertEqual(confirmed["confirmation_status"], "confirmed_1h")
            self.assertEqual(accelerated["current_stage"], "launched")
            self.assertEqual(accelerated["peak_stage"], "launched")

    def test_directional_bearish_cycle_confirms_and_invalidates_above_level(self) -> None:
        with TemporaryDirectory() as tmp:
            store = LaunchLifecycleStore(
                Path(tmp) / "signals.db",
                fusion_enabled=True,
                directional_enabled=True,
                invalid_windows_required=2,
            )
            opened = store.record_observation(
                snapshot(
                    window_end_ts=900,
                    score=80,
                    price=100,
                    oi=1_000,
                    trigger_path="directional:bearish",
                    directional_readiness=directional_signal("bearish"),
                    invalidation_price=105,
                ),
                stage="breakout",
                observed_at=910,
            )
            first_breach = store.record_observation(
                snapshot(
                    window_end_ts=1800,
                    score=80,
                    price=106,
                    oi=1_020,
                    trigger_path="directional:bearish",
                    directional_readiness=directional_signal("bearish"),
                    invalidation_price=105,
                ),
                stage="breakout",
                observed_at=1810,
            )
            failed = store.record_observation(
                snapshot(
                    window_end_ts=2700,
                    score=80,
                    price=107,
                    oi=1_030,
                    trigger_path="directional:bearish",
                    directional_readiness=directional_signal("bearish"),
                    invalidation_price=105,
                ),
                stage="breakout",
                observed_at=2710,
            )

            self.assertEqual(opened["current_stage"], "breakout")
            self.assertEqual(opened["confirmation_status"], "directional_confirmed")
            self.assertEqual(first_breach["cycle_status"], "active")
            self.assertEqual(failed["cycle_status"], "failed")
            self.assertEqual(failed["end_reason"], "two_closes_above_invalidation")

    def test_directional_bullish_cycle_invalidates_below_level(self) -> None:
        with TemporaryDirectory() as tmp:
            store = LaunchLifecycleStore(
                Path(tmp) / "signals.db",
                fusion_enabled=True,
                directional_enabled=True,
                invalid_windows_required=2,
            )
            for window, price in ((900, 100), (1800, 94), (2700, 93)):
                result = store.record_observation(
                    snapshot(
                        window_end_ts=window,
                        score=80,
                        price=price,
                        oi=1_000,
                        trigger_path="directional:bullish",
                        directional_readiness=directional_signal("bullish"),
                        invalidation_price=95,
                    ),
                    stage="breakout",
                    observed_at=window + 10,
                )

            self.assertEqual(result["cycle_status"], "failed")
            self.assertEqual(result["end_reason"], "two_closes_below_invalidation")

    def test_direction_change_closes_old_cycle_before_new_side_opens(self) -> None:
        with TemporaryDirectory() as tmp:
            store = LaunchLifecycleStore(
                Path(tmp) / "signals.db",
                fusion_enabled=True,
                directional_enabled=True,
            )
            first = store.record_observation(
                snapshot(
                    window_end_ts=900,
                    score=80,
                    price=100,
                    oi=1_000,
                    trigger_path="directional:bullish",
                    directional_readiness=directional_signal("bullish"),
                    invalidation_price=95,
                ),
                stage="breakout",
                observed_at=910,
            )
            flipped = store.record_observation(
                snapshot(
                    window_end_ts=1800,
                    score=80,
                    price=99,
                    oi=1_050,
                    trigger_path="directional:bearish",
                    directional_readiness=directional_signal("bearish"),
                    invalidation_price=104,
                ),
                stage="breakout",
                observed_at=1810,
            )
            reopened = store.record_observation(
                snapshot(
                    window_end_ts=2700,
                    score=80,
                    price=98,
                    oi=1_080,
                    trigger_path="directional:bearish",
                    directional_readiness=directional_signal("bearish"),
                    invalidation_price=104,
                ),
                stage="breakout",
                observed_at=2710,
            )

            self.assertEqual(first["cycle_no"], 1)
            self.assertEqual(flipped["cycle_status"], "failed")
            self.assertEqual(flipped["end_reason"], "direction_changed")
            self.assertEqual(reopened["status"], "opened")
            self.assertEqual(reopened["cycle_no"], 2)
            profile = store.active_symbol_profiles()["TESTUSDT"]
            self.assertTrue(profile["directional"])
            self.assertEqual(profile["direction"], "bearish")

    def test_divergence_risk_watch_can_open_below_direction_confirmation_score(self) -> None:
        with TemporaryDirectory() as tmp:
            store = LaunchLifecycleStore(
                Path(tmp) / "signals.db",
                fusion_enabled=True,
                directional_enabled=True,
                package_enabled=True,
                watch_score=45,
                start_score=60,
            )

            opened = store.record_observation(
                snapshot(
                    window_end_ts=900,
                    score=59,
                    price=100,
                    oi=1_000,
                    trigger_path="directional:bearish_divergence_watch",
                    directional_readiness=directional_signal(
                        "bearish_divergence_watch",
                        confirmed=False,
                    ),
                ),
                stage="watching",
                observed_at=910,
            )

            self.assertEqual(opened["status"], "opened")
            self.assertEqual(opened["current_stage"], "watching")
            self.assertEqual(opened["confirmation_status"], "not_applicable")
            self.assertTrue(opened["publication"]["publish_required"])
            self.assertIn(
                "cycle_opened",
                opened["publication"]["checkpoint_reasons"],
            )

    def test_legacy_cycle_keeps_legacy_semantics_after_fusion_is_enabled(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signals.db"
            legacy = LaunchLifecycleStore(db_path, fusion_enabled=False)
            opened = legacy.record_observation(
                snapshot(window_end_ts=900, score=80, price=100, oi=1_000),
                stage="breakout",
                observed_at=910,
            )

            fusion = LaunchLifecycleStore(db_path, fusion_enabled=True)
            continued = fusion.record_observation(
                snapshot(window_end_ts=1800, score=82, price=101, oi=1_020),
                stage="breakout",
                observed_at=1810,
            )

            self.assertEqual(opened["current_stage"], "breakout")
            self.assertEqual(fusion.active_symbol_modes(), {"TESTUSDT": False})
            self.assertEqual(continued["current_stage"], "breakout")
            self.assertEqual(continued["confirmation_status"], "not_required")

    def test_fusion_cycle_keeps_confirmation_gate_after_fusion_is_disabled(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signals.db"
            fusion = LaunchLifecycleStore(
                db_path,
                fusion_enabled=True,
                price_action_enabled=True,
                launched_score=90,
            )
            opened = fusion.record_observation(
                snapshot(
                    window_end_ts=900,
                    score=95,
                    price=100,
                    oi=1_000,
                    price_action_analysis=price_action_breakout(900),
                ),
                stage="launched",
                observed_at=910,
            )

            legacy = LaunchLifecycleStore(
                db_path,
                fusion_enabled=False,
                price_action_enabled=False,
                launched_score=90,
            )
            waiting = legacy.record_observation(
                snapshot(window_end_ts=1800, score=95, price=101, oi=1_020),
                stage="launched",
                observed_at=1810,
            )
            confirmed = legacy.record_observation(
                snapshot(
                    window_end_ts=2700,
                    score=95,
                    price=103,
                    oi=1_060,
                    price_action_analysis=price_action_confirmed(2700),
                ),
                stage="launched",
                observed_at=2710,
            )

            self.assertEqual(opened["current_stage"], "primed")
            self.assertEqual(legacy.active_symbol_modes(), {"TESTUSDT": True})
            self.assertEqual(waiting["current_stage"], "primed")
            self.assertEqual(waiting["confirmation_status"], "awaiting_1h")
            self.assertEqual(confirmed["current_stage"], "launched")
            self.assertEqual(confirmed["confirmation_status"], "confirmed_1h")

    def test_blank_pre_fusion_rule_key_migrates_as_legacy(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signals.db"
            legacy = LaunchLifecycleStore(db_path, fusion_enabled=False)
            opened = legacy.record_observation(
                snapshot(window_end_ts=900, score=80, price=100, oi=1_000),
                stage="breakout",
                observed_at=910,
            )
            with legacy.connect() as conn:
                conn.execute(
                    "UPDATE launch_lifecycle_cycles SET outcome_rule_key = '' WHERE id = ?",
                    (opened["cycle_id"],),
                )

            fusion = LaunchLifecycleStore(
                db_path,
                fusion_enabled=True,
                outcome_enabled=True,
            )
            fusion.refresh_outcomes(evaluated_at=920)

            self.assertEqual(fusion.active_symbol_modes(), {"TESTUSDT": False})
            with fusion.connect() as conn:
                rule_key = str(
                    conn.execute(
                        "SELECT outcome_rule_key FROM launch_lifecycle_cycles WHERE id = ?",
                        (opened["cycle_id"],),
                    ).fetchone()[0]
                )
            self.assertTrue(rule_key.endswith(":fusion=0"))

    def test_fusion_unconfirmed_breakout_price_does_not_end_cycle(self) -> None:
        with TemporaryDirectory() as tmp:
            store = LaunchLifecycleStore(
                Path(tmp) / "signals.db",
                fusion_enabled=True,
                price_action_enabled=True,
            )
            opened = store.record_observation(
                snapshot(
                    window_end_ts=900,
                    score=80,
                    price=105,
                    oi=1_000,
                    breakout=True,
                    breakout_price=100,
                ),
                stage="breakout",
                observed_at=910,
            )
            first = store.record_observation(
                snapshot(window_end_ts=1800, score=70, price=99, oi=1_010),
                stage="primed",
                observed_at=1810,
            )
            second = store.record_observation(
                snapshot(window_end_ts=2700, score=70, price=98, oi=1_020),
                stage="primed",
                observed_at=2710,
            )

            self.assertEqual(opened["current_stage"], "primed")
            self.assertEqual(first["breakout_below_count"], 0)
            self.assertEqual(second["cycle_status"], "active")

    def test_fusion_same_stage_changes_are_throttled_for_thirty_minutes(self) -> None:
        with TemporaryDirectory() as tmp:
            store = LaunchLifecycleStore(
                Path(tmp) / "signals.db",
                package_enabled=True,
                fusion_enabled=True,
                same_stage_min_interval_sec=1800,
            )
            first = store.record_observation(
                snapshot(window_end_ts=900, score=60, price=100, oi=1_000),
                stage="primed",
                observed_at=910,
            )
            store.commit_package(
                cycle_id=first["cycle_id"],
                observation_id=first["observation_id"],
                message_ids=[101],
                checkpoint_reasons=["cycle_opened"],
                published_at=920,
            )
            too_soon = store.record_observation(
                snapshot(window_end_ts=1800, score=76, price=104, oi=1_070),
                stage="breakout",
                observed_at=1810,
            )
            after_interval = store.record_observation(
                snapshot(window_end_ts=2700, score=77, price=105, oi=1_080),
                stage="breakout",
                observed_at=2710,
            )

            self.assertFalse(too_soon["publication"]["publish_required"])
            self.assertTrue(after_interval["publication"]["publish_required"])
            self.assertIn(
                "score_delta",
                after_interval["publication"]["checkpoint_reasons"],
            )

    def test_fusion_hard_counter_moves_active_cycle_to_risk(self) -> None:
        with TemporaryDirectory() as tmp:
            store = LaunchLifecycleStore(
                Path(tmp) / "signals.db",
                package_enabled=True,
                fusion_enabled=True,
                outcome_enabled=True,
            )

            result = store.record_observation(
                snapshot(
                    window_end_ts=900,
                    score=80,
                    price=100,
                    oi=1_000,
                    counter_evidence=["price_down_oi_up"],
                ),
                stage="breakout",
                observed_at=910,
            )

            self.assertEqual(result["cycle_status"], "active")
            self.assertEqual(result["current_stage"], "risk")
            self.assertFalse(result["outcome_evaluation"]["progress"]["confirmed"])

    def test_fusion_quadrant_change_waits_for_same_stage_interval(self) -> None:
        with TemporaryDirectory() as tmp:
            store = LaunchLifecycleStore(
                Path(tmp) / "signals.db",
                package_enabled=True,
                fusion_enabled=True,
                same_stage_min_interval_sec=1800,
            )
            first = store.record_observation(
                snapshot(
                    window_end_ts=900,
                    score=65,
                    price=100,
                    oi=1_000,
                    price_oi_quadrant="price_up_oi_up",
                ),
                stage="primed",
                observed_at=910,
            )
            store.commit_package(
                cycle_id=first["cycle_id"],
                observation_id=first["observation_id"],
                message_ids=[101],
                checkpoint_reasons=["cycle_opened"],
                published_at=920,
            )
            too_soon = store.record_observation(
                snapshot(
                    window_end_ts=1800,
                    score=65,
                    price=100,
                    oi=1_000,
                    price_oi_quadrant="price_up_oi_down",
                ),
                stage="primed",
                observed_at=1810,
            )
            after_interval = store.record_observation(
                snapshot(
                    window_end_ts=2700,
                    score=65,
                    price=100,
                    oi=1_000,
                    price_oi_quadrant="price_up_oi_down",
                ),
                stage="primed",
                observed_at=2710,
            )

            self.assertFalse(too_soon["publication"]["publish_required"])
            self.assertEqual(
                after_interval["publication"]["checkpoint_reasons"],
                ["quadrant_changed"],
            )

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

    def test_package_checkpoints_only_publish_significant_changes(self) -> None:
        with TemporaryDirectory() as tmp:
            store = LaunchLifecycleStore(
                Path(tmp) / "signals.db",
                package_enabled=True,
            )
            opened = store.record_observation(
                snapshot(window_end_ts=900, score=60, price=100, oi=1_000),
                stage="primed",
                observed_at=910,
            )
            self.assertTrue(opened["publication"]["publish_required"])
            self.assertEqual(
                opened["publication"]["checkpoint_reasons"],
                ["cycle_opened"],
            )
            committed = store.commit_package(
                cycle_id=opened["cycle_id"],
                observation_id=opened["observation_id"],
                message_ids=[101],
                checkpoint_reasons=opened["publication"]["checkpoint_reasons"],
                published_at=920,
            )
            self.assertEqual(committed["status"], "committed")
            self.assertEqual(committed["checkpoint_no"], 1)

            silent = store.record_observation(
                snapshot(window_end_ts=1800, score=65, price=101, oi=1_020),
                stage="primed",
                observed_at=1810,
            )
            self.assertFalse(silent["publication"]["publish_required"])

            significant = store.record_observation(
                snapshot(window_end_ts=2700, score=75, price=101, oi=1_020),
                stage="breakout",
                observed_at=2710,
            )
            self.assertTrue(significant["publication"]["publish_required"])
            self.assertIn("stage_changed", significant["publication"]["checkpoint_reasons"])
            self.assertIn("score_delta", significant["publication"]["checkpoint_reasons"])

    def test_package_commit_retains_old_message_as_reply_head_without_cleanup(self) -> None:
        with TemporaryDirectory() as tmp:
            store = LaunchLifecycleStore(
                Path(tmp) / "signals.db",
                package_enabled=True,
            )
            first = store.record_observation(
                snapshot(window_end_ts=900, score=60, price=100, oi=1_000),
                stage="primed",
                observed_at=910,
            )
            store.commit_package(
                cycle_id=first["cycle_id"],
                observation_id=first["observation_id"],
                message_ids=[101, 102],
                checkpoint_reasons=["cycle_opened"],
                published_at=920,
            )
            second = store.record_observation(
                snapshot(window_end_ts=1800, score=75, price=104, oi=1_060),
                stage="breakout",
                observed_at=1810,
            )
            self.assertEqual(
                second["publication"]["reply_message_ids"],
                [101, 102],
            )
            committed = store.commit_package(
                cycle_id=second["cycle_id"],
                observation_id=second["observation_id"],
                message_ids=[201],
                checkpoint_reasons=second["publication"]["checkpoint_reasons"],
                published_at=1820,
            )
            self.assertEqual(committed["delete_message_ids"], [])
            self.assertEqual(store.list_pending_cleanups(), [])
            with store.connect() as conn:
                row = conn.execute(
                    """
                    SELECT latest_message_ids_json,
                           cleanup_pending_message_ids_json
                    FROM launch_lifecycle_cycles
                    WHERE id = ?
                    """,
                    (first["cycle_id"],),
                ).fetchone()
            self.assertEqual(row["latest_message_ids_json"], "[201]")
            self.assertEqual(row["cleanup_pending_message_ids_json"], "[]")

    def test_active_cycle_republishes_when_its_latest_message_was_deleted(self) -> None:
        with TemporaryDirectory() as tmp:
            store = LaunchLifecycleStore(
                Path(tmp) / "signals.db",
                package_enabled=True,
            )
            first = store.record_observation(
                snapshot(window_end_ts=900, score=60, price=100, oi=1_000),
                stage="primed",
                observed_at=910,
            )
            store.commit_package(
                cycle_id=first["cycle_id"],
                observation_id=first["observation_id"],
                message_ids=[101],
                checkpoint_reasons=["cycle_opened"],
                published_at=920,
            )
            store.reconcile_topic_message_cleanup(
                deleted_ids=[101],
                updated_at=930,
            )

            recovered = store.record_observation(
                snapshot(window_end_ts=1800, score=62, price=100.5, oi=1_010),
                stage="primed",
                observed_at=1810,
            )

            self.assertTrue(recovered["publication"]["publish_required"])
            self.assertEqual(
                recovered["publication"]["checkpoint_reasons"],
                ["active_message_missing"],
            )

    def test_failed_package_history_is_retained_for_next_cycle_reply(self) -> None:
        with TemporaryDirectory() as tmp:
            store = LaunchLifecycleStore(
                Path(tmp) / "signals.db",
                package_enabled=True,
                invalid_windows_required=2,
            )
            first = store.record_observation(
                snapshot(window_end_ts=900, score=60, price=100, oi=1_000),
                stage="primed",
                observed_at=910,
            )
            store.commit_package(
                cycle_id=first["cycle_id"],
                observation_id=first["observation_id"],
                message_ids=[101, 102],
                checkpoint_reasons=["cycle_opened"],
                published_at=920,
            )
            store.record_observation(
                snapshot(window_end_ts=1800, score=20, price=99, oi=980),
                stage="idle",
                observed_at=1810,
            )
            failed = store.record_observation(
                snapshot(window_end_ts=2700, score=20, price=98, oi=960),
                stage="idle",
                observed_at=2710,
            )
            self.assertEqual(failed["cycle_status"], "failed")

            self.assertEqual(store.list_pending_cleanups(
                now_ts=2800,
                max_age_sec=47 * 3600,
            ), [])
            restarted = LaunchLifecycleStore(
                Path(tmp) / "signals.db",
                package_enabled=True,
                invalid_windows_required=2,
            )
            next_cycle = restarted.record_observation(
                snapshot(window_end_ts=3600, score=60, price=101, oi=1_010),
                stage="primed",
                observed_at=3610,
            )
            self.assertNotEqual(next_cycle["cycle_id"], first["cycle_id"])
            self.assertEqual(
                next_cycle["publication"]["reply_message_ids"],
                [101, 102],
            )

            other_symbol = restarted.record_observation(
                snapshot(
                    symbol="OTHERUSDT",
                    window_end_ts=3600,
                    score=60,
                    price=10,
                    oi=100,
                ),
                stage="primed",
                observed_at=3610,
            )
            self.assertEqual(
                other_symbol["publication"]["reply_message_ids"],
                [],
            )

    def test_topic_cleanup_reconciles_latest_and_pending_message_ids(self) -> None:
        with TemporaryDirectory() as tmp:
            store = LaunchLifecycleStore(
                Path(tmp) / "signals.db",
                package_enabled=True,
            )
            first = store.record_observation(
                snapshot(window_end_ts=900, score=60, price=100, oi=1_000),
                stage="primed",
                observed_at=910,
            )
            store.commit_package(
                cycle_id=first["cycle_id"],
                observation_id=first["observation_id"],
                message_ids=[101],
                checkpoint_reasons=["cycle_opened"],
                published_at=920,
            )
            second = store.record_observation(
                snapshot(window_end_ts=1800, score=75, price=104, oi=1_060),
                stage="breakout",
                observed_at=1810,
            )
            store.commit_package(
                cycle_id=second["cycle_id"],
                observation_id=second["observation_id"],
                message_ids=[201],
                checkpoint_reasons=second["publication"]["checkpoint_reasons"],
                published_at=1820,
            )

            result = store.reconcile_topic_message_cleanup(
                deleted_ids=[101, 201],
                updated_at=1830,
            )

            self.assertEqual(result["cycles_updated"], 1)
            self.assertEqual(result["message_ids_removed"], 1)
            with store.connect() as conn:
                row = conn.execute(
                    """
                    SELECT latest_message_ids_json,
                           cleanup_pending_message_ids_json
                    FROM launch_lifecycle_cycles
                    WHERE id = ?
                    """,
                    (first["cycle_id"],),
                ).fetchone()
            self.assertEqual(row["latest_message_ids_json"], "[]")
            self.assertEqual(row["cleanup_pending_message_ids_json"], "[]")

    def test_funds_direction_divergence_is_a_package_trigger(self) -> None:
        with TemporaryDirectory() as tmp:
            store = LaunchLifecycleStore(
                Path(tmp) / "signals.db",
                package_enabled=True,
            )
            first = store.record_observation(
                snapshot(
                    window_end_ts=900,
                    score=60,
                    price=100,
                    oi=1_000,
                    spot_active_net_usd=20_000,
                    futures_active_net_usd=30_000,
                    funds_direction="both_buy",
                ),
                stage="primed",
                observed_at=910,
            )
            store.commit_package(
                cycle_id=first["cycle_id"],
                observation_id=first["observation_id"],
                message_ids=[101],
                checkpoint_reasons=["cycle_opened"],
                published_at=920,
            )
            divergence = store.record_observation(
                snapshot(
                    window_end_ts=1800,
                    score=62,
                    price=100.5,
                    oi=1_010,
                    spot_active_net_usd=25_000,
                    futures_active_net_usd=-40_000,
                    funds_direction="divergence_spot_buy_futures_sell",
                ),
                stage="primed",
                observed_at=1810,
            )
            self.assertEqual(
                divergence["publication"]["checkpoint_reasons"],
                ["funds_divergence"],
            )

    def test_direction_change_is_an_immediate_package_trigger(self) -> None:
        with TemporaryDirectory() as tmp:
            store = LaunchLifecycleStore(
                Path(tmp) / "signals.db",
                package_enabled=True,
                fusion_enabled=True,
                same_stage_min_interval_sec=1800,
            )
            opened = store.record_observation(
                snapshot(
                    window_end_ts=900,
                    score=75,
                    price=100,
                    oi=1_000,
                    trigger_path="directional:bullish",
                ),
                stage="breakout",
                observed_at=910,
            )
            store.commit_package(
                cycle_id=int(opened["cycle_id"]),
                observation_id=int(opened["observation_id"]),
                message_ids=[101],
                checkpoint_reasons=["cycle_opened"],
                published_at=920,
            )

            changed = store.record_observation(
                snapshot(
                    window_end_ts=1800,
                    score=75,
                    price=100,
                    oi=1_000,
                    trigger_path="directional:bearish",
                ),
                stage="breakout",
                observed_at=1810,
            )

            self.assertTrue(changed["publication"]["publish_required"])
            self.assertIn(
                "direction_changed",
                changed["publication"]["checkpoint_reasons"],
            )

    def test_existing_p21_tables_receive_additive_package_columns(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signals.db"
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    """
                    CREATE TABLE launch_lifecycle_cycles (
                        id INTEGER PRIMARY KEY,
                        symbol TEXT NOT NULL,
                        status TEXT NOT NULL,
                        last_window_end INTEGER NOT NULL
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE launch_lifecycle_observations (
                        id INTEGER PRIMARY KEY,
                        cycle_id INTEGER NOT NULL,
                        window_end_ts INTEGER NOT NULL
                    )
                    """
                )
                conn.commit()
            finally:
                conn.close()

            LaunchLifecycleStore(db_path).list_active_symbols()

            conn = sqlite3.connect(db_path)
            try:
                cycle_columns = {
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(launch_lifecycle_cycles)"
                    )
                }
                observation_columns = {
                    row[1]
                    for row in conn.execute(
                        "PRAGMA table_info(launch_lifecycle_observations)"
                    )
                }
                outcome_table = conn.execute(
                    """
                    SELECT 1 FROM sqlite_master
                    WHERE type = 'table' AND name = 'launch_lifecycle_outcomes'
                    """
                ).fetchone()
            finally:
                conn.close()
            self.assertIn("latest_message_ids_json", cycle_columns)
            self.assertIn("cleanup_pending_message_ids_json", cycle_columns)
            self.assertIn("last_published_observation_id", cycle_columns)
            self.assertIn("outcome_rule_key", cycle_columns)
            self.assertIn("checkpoint_no", observation_columns)
            self.assertIn("funds_direction", observation_columns)
            self.assertIsNotNone(outcome_table)


class LaunchLifecycleRadarIntegrationTests(unittest.TestCase):
    def test_topic_cleanup_reconciles_launch_state_message_ids(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                signal_events_db_path=Path(tmp) / "signals.db",
            )
            json_store = JsonStore(Path(tmp))
            json_store.save(settings.launch_state_path, {
                "AUSDT": {
                    "message_ids": [101],
                    "last_message_ids": [101],
                    "last_message_id": 101,
                },
                "BUSDT": {
                    "message_ids": [202],
                    "last_message_ids": [202],
                    "last_message_id": 202,
                },
            })
            engine = RadarEngine(settings, json_store)

            result = engine.reconcile_launch_topic_messages(
                deleted_ids=[101],
                updated_at=1_000,
            )

            state = json_store.load(settings.launch_state_path, {})
            self.assertEqual(result["state_records_updated"], 1)
            self.assertEqual(state["AUSDT"]["message_ids"], [])
            self.assertEqual(state["AUSDT"]["last_message_id"], 0)
            self.assertEqual(state["BUSDT"]["message_ids"], [202])

    def test_launch_chart_fetches_cycle_window_and_stays_in_memory(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                launch_chart_v2_enable=True,
            )
            engine = RadarEngine(settings, JsonStore(Path(tmp)))
            captured: dict[str, int | str] = {}

            class Source:
                @staticmethod
                def klines(
                    _symbol: str,
                    *,
                    interval: str,
                    limit: int,
                    start_time: int,
                    end_time: int,
                ) -> list[list[object]]:
                    captured.update({
                        "interval": interval,
                        "limit": limit,
                        "start_time": start_time,
                        "end_time": end_time,
                    })
                    last_closed_boundary = end_time // 3_600_000 * 3_600_000
                    return [
                        [
                            last_closed_boundary - (limit - index) * 3_600_000,
                            "100",
                            "102",
                            "99",
                            str(100 + index * 0.1),
                            "10",
                            last_closed_boundary - (limit - index - 1) * 3_600_000 - 1,
                            str(100_000 + index * 1000),
                        ]
                        for index in range(limit)
                    ]

            alert = {
                "symbol": "TESTUSDT",
                "launch_lifecycle": {
                    "cycle_no": 1,
                    "first_window_end": 1_700_000_000,
                    "window_end_ts": 1_700_100_000,
                },
                "launch_package": {
                    "checkpoint_no": 2,
                    "checkpoints": [{
                        "checkpoint_no": 1,
                        "window_end_ts": 1_700_000_000,
                        "stage": "primed",
                    }],
                    "current": {
                        "window_end_ts": 1_700_100_000,
                        "stage": "breakout",
                        "price_action": {
                            "enabled": True,
                            "status": "confirmed_1h",
                            "direction": "up",
                            "lookback": 16,
                            "box_high": 102.0,
                            "box_low": 99.0,
                            "level": 102.0,
                            "box_start_ts": 1_699_996_400,
                            "box_end_ts": 1_699_999_100,
                            "trigger_window_end_ts": 1_700_000_000,
                            "event_window_end_ts": 1_700_100_000,
                            "confirmation_ends": {
                                "15m": 1_700_000_000,
                                "1h": 1_700_100_000,
                            },
                            "timeframes": {},
                        },
                    },
                },
            }

            ready = engine._attach_launch_chart(Source(), alert)  # type: ignore[arg-type]

            self.assertTrue(ready)
            self.assertTrue(alert["chart_png_bytes"].startswith(PNG_SIGNATURE))
            self.assertTrue(alert["chart_generated_in_memory"])
            self.assertEqual(alert["chart_checkpoint_count"], 2)
            self.assertEqual(alert["chart_candle_count"], 72)
            self.assertEqual(alert["chart_timeframe"], "1h")
            self.assertEqual(alert["chart_trigger_timeframe"], "15m")
            self.assertEqual(alert["chart_price_action_status"], "confirmed_1h")
            self.assertEqual(captured["interval"], "1h")
            self.assertGreaterEqual(captured["limit"], 72)
            self.assertLessEqual(captured["limit"], 240)
            self.assertEqual(captured["end_time"], 1_700_100_000_000 - 1)
            self.assertEqual(list(Path(tmp).rglob("*.png")), [])

    def test_launch_package_message_contains_cycle_deltas_and_event_axis(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                launch_message_package_v2_enable=True,
            )
            engine = RadarEngine(settings, JsonStore(Path(tmp)))
            first = {
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
            current = {
                **first,
                "observation_id": 2,
                "observation_no": 2,
                "checkpoint_no": None,
                "window_end_ts": 1800,
                "stage": "breakout",
                "score": 75,
                "price": 104.0,
                "oi_usd": 1_060.0,
                "funding_pct": -0.005,
                "funding_interval_hours": 0,
                "funds_direction": "divergence_spot_buy_futures_sell",
            }
            lifecycle = {
                "cycle_id": 1,
                "cycle_no": 2,
                "cycle_status": "active",
                "current_stage": "breakout",
                "peak_stage": "breakout",
                "duration_sec": 900,
            }
            publication = {
                "checkpoint_no": 2,
                "checkpoint_reasons": ["stage_changed", "oi_delta"],
                "first": first,
                "previous_published": first,
                "current": current,
                "checkpoints": [first],
            }
            text = engine._format_launch_alert({
                "symbol": "TESTUSDT",
                "coin": "TEST",
                "score": 75,
                "stage": "breakout",
                "mcap": 53_000_000,
                "mcap_source": "CoinPaprika",
                "quote_volume": 286_000_000,
                "launch_message_package_v2": True,
                "launch_lifecycle": lifecycle,
                "launch_package": publication,
                "data_confirmation": {
                    "confirmed_count": 5,
                    "expected_count": 5,
                    "status": "complete",
                },
            })

            self.assertIn("第2轮启动跟踪", text)
            self.assertIn("事件02", text)
            self.assertIn("OI: +6.00%", text)
            self.assertIn("生命周期阶段变化", text)
            self.assertIn("事件轴", text)
            self.assertIn("现货主动买入、合约主动卖出", text)
            self.assertIn(
                "资金费率: -0.010%/4H → -0.0050%/本次未确认（上次确认4H）",
                text,
            )
            self.assertIn("市场概况", text)
            self.assertIn("市值: $53M（低市值，来源 CoinPaprika）", text)
            self.assertIn("流动性: $286M/24h（高流动性）", text)
            self.assertIn(
                'href="https://www.coinglass.com/tv/zh/Binance_TESTUSDT"',
                text,
            )
            self.assertIn("<code>TESTUSDT</code>", text)
            self.assertIn(
                'href="https://www.tradingview.com/chart/?symbol=BINANCE%3ATESTUSDT.P"',
                text,
            )
            self.assertLessEqual(len(plain_fallback(text)), 1024)
            self.assertNotIn("图片只在内存中生成", text)
            self.assertNotIn("每次新消息确认发送", text)

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

    def test_active_lifecycle_verifies_funding_interval_even_when_score_falls(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = RadarEngine(Settings(data_dir=Path(tmp)), JsonStore(Path(tmp)))
            klines = [
                [0, "100", "102", "99", "100", "0", 0, "5000"]
                for _ in range(17)
            ]
            oi_history = [
                {"sumOpenInterestValue": "1000000"}
                for _ in range(17)
            ]

            class Source:
                funding_calls = 0

                @staticmethod
                def klines(*_args: object, **_kwargs: object) -> list[list[object]]:
                    return klines

                @staticmethod
                def open_interest_hist(*_args: object, **_kwargs: object) -> list[dict[str, str]]:
                    return oi_history

                @classmethod
                def funding_rate(
                    cls,
                    *_args: object,
                    **_kwargs: object,
                ) -> list[dict[str, object]]:
                    cls.funding_calls += 1
                    return [
                        {"fundingTime": 1_000_000_000, "fundingRate": "-0.0008"},
                        {"fundingTime": 1_014_400_000, "fundingRate": "-0.0007"},
                        {"fundingTime": 1_028_800_000, "fundingRate": "-0.0006"},
                    ]

            result = engine._analyze_launch_symbol(  # type: ignore[arg-type]
                Source(),
                {
                    "symbol": "TESTUSDT",
                    "coin": "TEST",
                    "quote_volume": 10_000_000,
                    "price_24h": 0.0,
                    "price": 100.0,
                    "funding_available": True,
                    "funding_pct": -0.0729,
                    "funding_next_time_ms": 0,
                    "launch_lifecycle_active": True,
                    "mcap": 100_000_000,
                    "mcap_source": "Binance",
                    "market_cap_tier": "低市值",
                    "liquidity_tier": "低流动性",
                },
            )

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result["score"], 0)
            self.assertEqual(result["funding_interval_hours"], 4)
            self.assertEqual(Source.funding_calls, 1)

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
