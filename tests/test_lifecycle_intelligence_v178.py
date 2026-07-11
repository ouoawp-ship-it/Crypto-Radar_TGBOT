from __future__ import annotations

import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.config import Settings
from paopao_radar.lifecycle_analytics import (
    DEFAULT_ANALYTICS_CACHE_KEY,
    build_lifecycle_analytics,
    capital_confirmation_statistics,
    first_signal_level_statistics,
    generate_lifecycle_analytics,
    module_statistics,
    risk_warning_performance,
    upgrade_path_statistics,
)
from paopao_radar.lifecycle_intelligence import (
    INTELLIGENCE_MODEL_VERSION,
    build_upgrade_path,
    evaluate_lifecycle,
    generate_intelligence,
    identify_lifecycle_stage,
)
from paopao_radar.lifecycle_intelligence_store import IntelligenceStore
from paopao_radar.lifecycle_similarity import (
    SIMILARITY_MODEL_VERSION,
    find_similar_lifecycles,
    similarity_score,
)
from paopao_radar.lifecycle_store import LifecycleStore


def lifecycle(**overrides: object) -> dict:
    item = {
        "id": 1,
        "symbol": "BTCUSDT",
        "first_signal_at": "2026-07-01T00:00:00+00:00",
        "first_signal_level": "15m",
        "first_signal_module": "launch",
        "highest_level": "4h",
        "highest_level_rank": 3,
        "current_state": "upgraded_4h",
        "is_active": 1,
        "lifecycle_score": 90,
        "risk_score": 10,
        "price_change_from_first_pct": 12,
        "oi_change_from_first_pct": 15,
        "spot_cvd_change_from_first": 1200,
        "futures_cvd_change_from_first": 900,
        "latest_funding_rate": 0.0001,
        "metrics": {"volume_multiplier": 2.5},
        "updated_at": "2026-07-02T00:00:00+00:00",
    }
    item.update(overrides)
    return item


def events() -> list[dict]:
    return [
        {"id": 1, "lifecycle_id": 1, "event_time": "2026-07-01T00:00:00+00:00", "event_type": "first_signal"},
        {"id": 2, "lifecycle_id": 1, "event_time": "2026-07-01T01:00:00+00:00", "event_type": "timeframe_upgrade_1h"},
        {"id": 3, "lifecycle_id": 1, "event_time": "2026-07-01T04:00:00+00:00", "event_type": "spot_cvd_confirmed"},
        {"id": 4, "lifecycle_id": 1, "event_time": "2026-07-01T08:00:00+00:00", "event_type": "timeframe_upgrade_4h"},
    ]


def analytics_record(identifier: int, **overrides: object) -> dict:
    item = {
        "lifecycle_id": identifier,
        "symbol": f"C{identifier}USDT",
        "first_signal_level": "15m",
        "first_signal_module": "launch",
        "highest_level": "4h",
        "upgrade_path": "15m → 1h → 4h",
        "is_active": 0,
        "intelligence_score": 82,
        "capital_confirmation_label": "现货与合约同步确认",
        "duration_sec": 20000,
        "time_to_1h_sec": 3600,
        "time_to_4h_sec": 14400,
        "risk_event_count": 1,
        "oi_change_from_first_pct": 12,
        "latest_funding_rate": 0.0001,
        "final_return_pct": 5,
        "max_price_gain_pct": 9,
        "max_drawdown_pct": -2,
        "result_label": "success",
        "outcome_status": "linked",
    }
    item.update(overrides)
    return item


class LifecycleIntelligenceV178Tests(unittest.TestCase):
    def test_intelligence_score_is_bounded_and_high_quality_scores_high(self) -> None:
        result = evaluate_lifecycle(lifecycle(), events())
        self.assertGreaterEqual(result["intelligence_score"], 80)
        self.assertLessEqual(result["intelligence_score"], 100)
        self.assertIn(result["quality_label"], {"高质量启动", "强趋势确认"})
        self.assertEqual(result["model_version"], INTELLIGENCE_MODEL_VERSION)

    def test_oi_up_price_down_applies_risk_penalty(self) -> None:
        healthy = evaluate_lifecycle(lifecycle(price_change_from_first_pct=5), events())
        divergent = evaluate_lifecycle(
            lifecycle(price_change_from_first_pct=-4, oi_change_from_first_pct=20),
            events(),
        )
        self.assertLess(divergent["intelligence_score"], healthy["intelligence_score"])
        self.assertIn("oi_up_price_down", divergent["factors"]["risk_penalties"])

    def test_spot_and_futures_confirmation_raise_score(self) -> None:
        confirmed = evaluate_lifecycle(lifecycle(), events())
        absent = evaluate_lifecycle(
            lifecycle(spot_cvd_change_from_first=-10, futures_cvd_change_from_first=-10),
            [events()[0]],
        )
        self.assertGreater(confirmed["intelligence_score"], absent["intelligence_score"])
        self.assertEqual(confirmed["capital_confirmation_label"], "现货与合约同步确认")

    def test_funding_overheat_reduces_score(self) -> None:
        normal = evaluate_lifecycle(lifecycle(latest_funding_rate=0.0001), events())
        hot = evaluate_lifecycle(lifecycle(latest_funding_rate=0.0012), events())
        self.assertLess(hot["intelligence_score"], normal["intelligence_score"])
        self.assertIn("funding_overheated", hot["factors"]["risk_penalties"])

    def test_stage_identification_uses_state_events_and_metrics(self) -> None:
        self.assertEqual(identify_lifecycle_stage(lifecycle(current_state="failed"), events())[0], "failure")
        self.assertEqual(identify_lifecycle_stage(lifecycle(current_state="cooling"), events())[0], "cooling")
        self.assertEqual(identify_lifecycle_stage(lifecycle(current_state="risk_warning"), events())[0], "distribution_risk")
        self.assertEqual(identify_lifecycle_stage(lifecycle(), events())[0], "trend_expansion")
        self.assertEqual(
            identify_lifecycle_stage(
                lifecycle(highest_level="1h", highest_level_rank=2, price_change_from_first_pct=0),
                events()[:2],
            )[0],
            "timeframe_upgrade",
        )

    def test_replay_peak_drawdown_affects_active_stage(self) -> None:
        item = lifecycle(current_state="upgraded_4h", is_active=1, risk_score=10)
        result = evaluate_lifecycle(item, events(), replay={"max_drawdown_pct": -11})
        self.assertEqual(result["stage"], "distribution_risk")
        self.assertEqual(result["momentum_label"], "动能走弱")

    def test_upgrade_path_follows_event_time(self) -> None:
        shuffled = list(reversed(events()))
        self.assertEqual(build_upgrade_path(lifecycle(), shuffled), "15m → 1h → 4h")

    def test_dry_run_does_not_add_intelligence_tables(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "lifecycle.db"
            store = LifecycleStore(path)
            store.create_lifecycle({
                "symbol": "BTCUSDT",
                "first_signal_at": "2026-07-01T00:00:00+00:00",
                "first_signal_level": "15m",
                "current_state": "warming",
                "is_active": 1,
                "highest_level": "15m",
            })
            settings = Settings(data_dir=Path(tmp), lifecycle_db_path=path)
            result = generate_intelligence(settings=settings, dry_run=True, all_active=True)
            with closing(sqlite3.connect(path)) as conn:
                names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(result["processed"], 1)
        self.assertNotIn("lifecycle_intelligence", names)

    def test_short_symbol_is_normalized_for_single_lifecycle(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "lifecycle.db"
            lifecycle_store = LifecycleStore(path)
            lifecycle_store.create_lifecycle({
                "symbol": "BTCUSDT",
                "first_signal_at": "2026-07-01T00:00:00+00:00",
                "first_signal_level": "15m",
                "current_state": "warming",
                "is_active": 1,
            })
            result = generate_intelligence(
                settings=Settings(data_dir=Path(tmp), lifecycle_db_path=path),
                symbol="BTC",
                dry_run=True,
            )
        self.assertEqual(result["processed"], 1)
        self.assertEqual(result["items"][0]["symbol"], "BTCUSDT")

    def test_batch_generation_is_idempotent_when_sources_do_not_change(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "lifecycle.db"
            lifecycle_store = LifecycleStore(path)
            created, _ = lifecycle_store.create_lifecycle({
                "symbol": "BTCUSDT",
                "first_signal_at": "2026-07-01T00:00:00+00:00",
                "first_signal_level": "15m",
                "first_signal_module": "launch",
                "current_state": "warming",
                "highest_level": "15m",
                "lifecycle_score": 65,
                "risk_score": 10,
                "is_active": 1,
            })
            lifecycle_store.insert_event({
                "lifecycle_id": created["id"],
                "symbol": "BTCUSDT",
                "event_time": "2026-07-01T00:00:00+00:00",
                "event_type": "first_signal",
                "event_level": "15m",
                "dedup_key": "intelligence-test:first",
            })
            settings = Settings(data_dir=Path(tmp), lifecycle_db_path=path)
            extension_store = IntelligenceStore(path)
            extension_store.put_analytics_cache(DEFAULT_ANALYTICS_CACHE_KEY, {"stale": True})
            first = generate_intelligence(settings=settings, all_active=True)
            second = generate_intelligence(settings=settings, all_active=True)
            stored = IntelligenceStore(path).get_intelligence(symbol="BTCUSDT")
            cached_after_update = extension_store.get_analytics_cache(DEFAULT_ANALYTICS_CACHE_KEY)
            analytics = generate_lifecycle_analytics(
                settings=settings,
                force=True,
                report_path=Path(tmp) / "analytics.json",
            )
        self.assertEqual(first["processed"], 1)
        self.assertEqual(second["processed"], 0)
        self.assertEqual(second["skipped"], 1)
        self.assertIsNone(cached_after_update)
        self.assertIsNotNone(stored)
        self.assertEqual(stored["stage"], "discovery")
        self.assertEqual(analytics["data"]["summary"]["total_lifecycle_count"], 1)

    def test_default_batch_includes_closed_lifecycles(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), lifecycle_db_path=Path(tmp) / "lifecycle.db")
            lifecycle_store = LifecycleStore(settings.lifecycle_db_path)
            lifecycle_store.create_lifecycle({
                "symbol": "BTCUSDT",
                "first_signal_at": "2026-07-01T00:00:00+00:00",
                "first_signal_level": "15m",
                "current_state": "warming",
                "is_active": 1,
            })
            lifecycle_store.create_lifecycle({
                "symbol": "ETHUSDT",
                "first_signal_at": "2026-07-01T00:00:00+00:00",
                "first_signal_level": "1h",
                "current_state": "closed",
                "is_active": 0,
                "closed_at": "2026-07-02T00:00:00+00:00",
            })
            result = generate_intelligence(settings=settings, limit=10)
        self.assertEqual(result["processed"], 2)
        self.assertEqual({item["symbol"] for item in result["items"]}, {"BTCUSDT", "ETHUSDT"})


class LifecycleAnalyticsV178Tests(unittest.TestCase):
    def test_first_level_statistics_does_not_invent_zero_rates(self) -> None:
        stats = first_signal_level_statistics([analytics_record(1)])
        empty = next(item for item in stats if item["first_signal_level"] == "24h")
        populated = next(item for item in stats if item["first_signal_level"] == "15m")
        self.assertIsNone(empty["success_rate"])
        self.assertEqual(populated["upgrade_4h_ratio"], 1.0)

    def test_upgrade_path_and_module_statistics(self) -> None:
        rows = [
            analytics_record(1),
            analytics_record(2, first_signal_module="structure", upgrade_path="1h → 4h", first_signal_level="1h"),
        ]
        paths = upgrade_path_statistics(rows)
        modules = module_statistics(rows)
        self.assertEqual({item["upgrade_path"] for item in paths}, {"15m → 1h → 4h", "1h → 4h"})
        first_path = next(item for item in paths if item["upgrade_path"] == "15m → 1h → 4h")
        self.assertEqual(first_path["avg_upgrade_time_sec"], 14400.0)
        self.assertEqual({item["module"] for item in modules}, {"launch", "structure"})

    def test_capital_confirmation_statistics(self) -> None:
        rows = [
            analytics_record(1),
            analytics_record(2, capital_confirmation_label="仅现货 CVD 确认"),
        ]
        stats = capital_confirmation_statistics(rows)
        self.assertEqual(len(stats), 2)
        self.assertTrue(all(item["sample_count"] == 1 for item in stats))

    def test_risk_warning_after_performance(self) -> None:
        risk_events = [{
            "lifecycle_id": 1,
            "event_time": "2026-07-01T00:00:00+00:00",
            "event_type": "risk_warning",
            "price": 100,
        }]
        frames = [
            {"lifecycle_id": 1, "frame_index": 1, "event_time": "2026-07-01T01:00:00+00:00", "price": 98},
            {"lifecycle_id": 1, "frame_index": 2, "event_time": "2026-07-01T04:00:00+00:00", "price": 95},
            {"lifecycle_id": 1, "frame_index": 3, "event_time": "2026-07-02T00:00:00+00:00", "price": 90},
        ]
        result = risk_warning_performance(risk_events, frames)
        self.assertEqual(result["avg_return_1h_pct"], -2.0)
        self.assertEqual(result["avg_return_24h_pct"], -10.0)
        self.assertEqual(result["hit_rate"], 1.0)

    def test_risk_horizon_does_not_reuse_a_far_future_frame(self) -> None:
        risk_events = [{
            "lifecycle_id": 1,
            "event_time": "2026-07-01T00:00:00+00:00",
            "event_type": "risk_warning",
            "price": 100,
        }]
        frames = [{
            "lifecycle_id": 1,
            "frame_index": 1,
            "event_time": "2026-07-02T00:00:00+00:00",
            "price": 90,
        }]
        result = risk_warning_performance(risk_events, frames)
        self.assertIsNone(result["avg_return_1h_pct"])
        self.assertIsNone(result["avg_return_4h_pct"])
        self.assertEqual(result["avg_return_24h_pct"], -10.0)

    def test_build_analytics_includes_all_dimensions(self) -> None:
        data = build_lifecycle_analytics([analytics_record(1)])
        self.assertEqual(data["summary"]["total_lifecycle_count"], 1)
        self.assertIn("first_level", data)
        self.assertIn("upgrade_path", data)
        self.assertIn("module", data)
        self.assertIn("capital_confirmation", data)
        self.assertEqual(data["result_distribution"]["success"], 1)
        self.assertIn("spot_futures_cvd", data["factor_effects"])
        self.assertIn("oi_confirmation", data["factor_effects"])
        self.assertIn("funding_overheat", data["factor_effects"])
        self.assertIn("model_data_warnings", data)

    def test_snapshot_only_result_is_not_counted_as_outcome(self) -> None:
        snapshot_only = analytics_record(
            1,
            is_active=1,
            outcome_status="insufficient_data",
            outcome_count=0,
            result_label="success",
            final_return_pct=6,
        )
        data = build_lifecycle_analytics([snapshot_only])
        self.assertEqual(data["summary"]["outcome_linked_count"], 0)
        self.assertEqual(data["summary"]["resolved_outcome_count"], 0)
        populated = next(item for item in data["first_level"] if item["first_signal_level"] == "15m")
        self.assertIsNone(populated["success_rate"])

    def test_cache_hit_avoids_recomputation(self) -> None:
        cached = {"model_version": "cached", "summary": {"total_lifecycle_count": 7}}

        class CacheOnlyStore:
            def get_analytics_cache(self, _key: str) -> dict:
                return cached

            def connect(self):
                raise AssertionError("cache hit must not query database")

        result = generate_lifecycle_analytics(store=CacheOnlyStore())
        self.assertTrue(result["cache_hit"])
        self.assertEqual(result["data"], cached)

    def test_report_is_sanitized_and_dry_run_does_not_write(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "lifecycle.db"
            lifecycle_store = LifecycleStore(db)
            lifecycle_store.ensure_schema()
            store = IntelligenceStore(db)
            report = Path(tmp) / "analytics.json"
            rows = [analytics_record(
                1,
                chat_id="secret",
                payload_json="private",
                api_key="private",
                server_path="/home/ubuntu/private",
            )]
            generated = generate_lifecycle_analytics(
                store=store,
                records=rows,
                events=[],
                frames=[],
                force=True,
                report_path=report,
            )
            content = report.read_text(encoding="utf-8")
            dry_path = Path(tmp) / "dry.json"
            generate_lifecycle_analytics(
                store=store,
                records=rows,
                events=[],
                frames=[],
                dry_run=True,
                report_path=dry_path,
            )
        self.assertTrue(generated["ok"])
        self.assertNotIn("chat_id", content)
        self.assertNotIn("payload_json", content)
        self.assertNotIn("api_key", content)
        self.assertNotIn("server_path", content)
        self.assertFalse(dry_path.exists())

    def test_analytics_dry_run_with_missing_database_creates_nothing(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "missing" / "lifecycle.db"
            settings = Settings(data_dir=Path(tmp), lifecycle_db_path=db)
            report = Path(tmp) / "dry-report.json"
            result = generate_lifecycle_analytics(
                settings=settings,
                dry_run=True,
                report_path=report,
            )
            self.assertTrue(result["ok"])
            self.assertFalse(db.exists())
            self.assertFalse(report.exists())


class LifecycleSimilarityV178Tests(unittest.TestCase):
    def test_missing_market_features_do_not_receive_confirmation_points(self) -> None:
        sparse = {
            "first_signal_level": "15m",
            "highest_level": "1h",
            "upgrade_path": "15m → 1h",
        }
        self.assertEqual(similarity_score(sparse, dict(sparse)), 50.0)

    def test_similarity_is_bounded_and_same_path_scores_higher(self) -> None:
        current = analytics_record(100, is_active=1)
        same = analytics_record(1)
        different = analytics_record(
            2,
            first_signal_level="24h",
            highest_level="24h",
            upgrade_path="24h",
            oi_change_from_first_pct=-10,
            capital_confirmation_label="无资金确认",
            price_change_from_first_pct=-10,
        )
        same_score = similarity_score(current, same)
        different_score = similarity_score(current, different)
        self.assertGreater(same_score, different_score)
        self.assertGreaterEqual(different_score, 0)
        self.assertLessEqual(same_score, 100)

    def test_insufficient_samples_has_no_fake_statistics(self) -> None:
        result = find_similar_lifecycles(analytics_record(100, is_active=1), [analytics_record(1)], min_samples=5)
        self.assertEqual(result["status"], "insufficient_samples")
        self.assertIsNone(result["positive_ratio"])
        self.assertEqual(result["samples"], [])

    def test_similarity_returns_top_samples_and_model_version(self) -> None:
        current = analytics_record(100, is_active=1)
        candidates = [analytics_record(index) for index in range(1, 7)]
        result = find_similar_lifecycles(current, candidates, limit=3, min_samples=5)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["model_version"], SIMILARITY_MODEL_VERSION)
        self.assertEqual(len(result["samples"]), 3)
        self.assertEqual(result["positive_ratio"], 1.0)

    def test_active_snapshot_only_candidate_is_not_historical_outcome(self) -> None:
        current = analytics_record(100, is_active=1)
        snapshot_only = analytics_record(
            1,
            is_active=1,
            outcome_status="insufficient_data",
            outcome_count=0,
            result_label="success",
        )
        result = find_similar_lifecycles(current, [snapshot_only], min_samples=1)
        self.assertEqual(result["status"], "insufficient_samples")
        self.assertEqual(result["similar_count"], 0)


if __name__ == "__main__":
    unittest.main()
