from __future__ import annotations

import json
import unittest
from argparse import Namespace
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from paopao_radar import cli, web
from paopao_radar.config import BASE_DIR, Settings
from paopao_radar.lifecycle_intelligence_store import IntelligenceStore
from paopao_radar.lifecycle_store import LifecycleStore
from paopao_radar.web_services import jobs
from paopao_radar.web_services.lifecycle_intelligence import (
    public_lifecycle_intelligence_detail_payload,
    public_lifecycle_intelligence_list_payload,
    public_lifecycle_replay_frames_payload,
    public_lifecycle_replay_payload,
    public_lifecycle_similar_payload,
)


FRONTEND = BASE_DIR / "frontend"


def make_settings(tmp: str, **overrides: object) -> Settings:
    base = Path(tmp)
    values: dict[str, object] = {
        "data_dir": base,
        "signal_events_db_path": base / "signals.db",
        "outcome_db_path": base / "outcomes.db",
        "lifecycle_db_path": base / "lifecycle.db",
        "web_jobs_db_path": base / "jobs.db",
    }
    values.update(overrides)
    return Settings(**values)


def seed_intelligence(settings: Settings) -> None:
    lifecycle_store = LifecycleStore(settings.lifecycle_db_path)
    lifecycle, _ = lifecycle_store.create_lifecycle({
        "symbol": "BTCUSDT",
        "first_signal_id": 101,
        "first_signal_at": "2026-07-09T00:00:00+00:00",
        "first_signal_module": "structure",
        "first_signal_level": "15m",
        "first_signal_level_rank": 1,
        "current_state": "upgraded_1h",
        "highest_level": "1h",
        "highest_level_rank": 2,
        "lifecycle_score": 76,
        "risk_score": 31,
        "price_change_from_first_pct": 4.2,
        "oi_change_from_first_pct": 8.5,
    })
    lifecycle_id = int(lifecycle["id"])
    store = IntelligenceStore(settings)
    store.upsert_intelligence({
        "lifecycle_id": lifecycle_id,
        "symbol": "BTCUSDT",
        "intelligence_score": 82,
        "quality_label": "高质量启动",
        "stage_label": "周期升级",
        "momentum_label": "趋势增强",
        "capital_confirmation_label": "现货与合约同步确认",
        "risk_label": "中风险",
        "maturity_label": "1H 周期确认",
        "confidence_label": "可参考",
        "summary": "BOT_TOKEN=private，生命周期增强。",
        "strengths": ["OI 与 CVD 同步"],
        "risks": ["chat_id=private"],
        "watch_points": ["topic_id=private"],
        "factors": {"safe": 1, "message_id": "private", "payload_json": "private"},
        "model_version": "lifecycle-intelligence-v1",
        "source_signature": "internal-fingerprint",
    })
    store.upsert_replay({
        "lifecycle_id": lifecycle_id,
        "symbol": "BTCUSDT",
        "replay_version": "lifecycle-replay-v1",
        "frame_count": 1,
        "upgrade_path": "15m → 1h",
        "highest_level": "1h",
        "final_return_pct": 4.2,
        "result_label": "success",
        "outcome_status": "linked",
        "source_signature": "internal-replay-fingerprint",
        "summary": {"safe": "ok", "dedup_key": "private"},
    }, frames=[{
        "event_time": "2026-07-09T01:00:00+00:00",
        "event_type": "timeframe_upgrade_1h",
        "event_label": "升级到 1H",
        "state_before": "launching",
        "state_after": "upgraded_1h",
        "signal_level": "1h",
        "price_change_from_first_pct": 4.2,
        "oi_change_from_first_pct": 8.5,
        "intelligence_score": 82,
        "summary": "1h 周期确认",
        "metrics": {"payload_json": "private"},
    }])


class LifecycleIntelligenceSurfaceConfigTests(unittest.TestCase):
    def test_settings_and_example_defaults(self) -> None:
        settings = Settings()
        self.assertTrue(settings.lifecycle_intelligence_enable)
        self.assertEqual(settings.lifecycle_intelligence_interval_sec, 900)
        self.assertEqual(settings.lifecycle_replay_interval_sec, 3600)
        self.assertEqual(settings.lifecycle_analytics_interval_sec, 21600)
        self.assertEqual(settings.lifecycle_similarity_min_samples, 5)
        text = (BASE_DIR / ".env.oi.example").read_text(encoding="utf-8")
        for item in (
            "LIFECYCLE_INTELLIGENCE_ENABLE=true",
            "LIFECYCLE_INTELLIGENCE_INTERVAL_SEC=900",
            "LIFECYCLE_REPLAY_INTERVAL_SEC=3600",
            "LIFECYCLE_ANALYTICS_INTERVAL_SEC=21600",
            "LIFECYCLE_SIMILARITY_MIN_SAMPLES=5",
        ):
            self.assertIn(item, text)

    def test_cli_parser_exposes_all_research_commands_and_options(self) -> None:
        parser = cli.build_parser()
        for command in (
            "lifecycle-intelligence", "lifecycle-replay", "lifecycle-replay-backfill",
            "lifecycle-analytics", "lifecycle-similar",
        ):
            args = parser.parse_args([command, "--dry-run", "--pretty", "--force-rebuild"])
            self.assertEqual(args.command, command)
            self.assertTrue(args.dry_run)
            self.assertTrue(args.pretty)
            self.assertTrue(args.force_rebuild)

    def test_cli_intelligence_dry_run_is_forwarded(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            args = Namespace(symbol="BTCUSDT", all_active=False, dry_run=True, force_rebuild=True, limit=20, pretty=False)
            with patch("paopao_radar.cli.Settings.load", return_value=settings):
                with patch("paopao_radar.lifecycle_intelligence.generate_intelligence", return_value={"ok": True}) as generate:
                    self.assertEqual(cli.run_lifecycle_intelligence(args), 0)
        self.assertTrue(generate.call_args.kwargs["dry_run"])
        self.assertTrue(generate.call_args.kwargs["force"])

    def test_cli_replay_requires_symbol_or_lifecycle_id(self) -> None:
        args = Namespace(
            symbol="", lifecycle_id=None, dry_run=True, force_rebuild=False,
            limit=None, pretty=False,
        )
        with patch("paopao_radar.cli.Settings.load", return_value=Settings()):
            self.assertEqual(cli.run_lifecycle_replay(args), 1)


class LifecycleIntelligenceJobTests(unittest.TestCase):
    def test_job_specs_are_guarded_and_return_job_id(self) -> None:
        expected = {"lifecycle-intelligence", "lifecycle-replay", "lifecycle-analytics", "lifecycle-replay-rebuild"}
        self.assertTrue(expected.issubset(jobs.JOB_SPECS))
        self.assertTrue(expected.issubset(jobs.CONCURRENT_GUARD_JOB_TYPES))
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            first = jobs.create_job_payload("lifecycle-intelligence", settings=settings, start=False)
            second = jobs.create_job_payload("lifecycle-intelligence", settings=settings, start=False)
        self.assertTrue(first["ok"])
        self.assertGreater(first["job_id"], 0)
        self.assertEqual(first["job_id"], second["job_id"])
        self.assertTrue(second["reused"])

    def test_research_jobs_share_guard_and_honor_target_arguments(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            first = jobs.create_job_payload(
                "lifecycle-intelligence",
                {"symbol": "btc"},
                settings=settings,
                start=False,
            )
            blocked = jobs.create_job_payload(
                "lifecycle-replay",
                {"lifecycle_id": 12},
                settings=settings,
                start=False,
            )
            store = jobs.store_for_settings(settings)
            store.finish_job(first["job_id"], status="success", returncode=0)
            replay = jobs.create_job_payload(
                "lifecycle-replay",
                {"lifecycle_id": 12},
                settings=settings,
                start=False,
            )
        self.assertEqual(first["job"]["command"][-2:], ["--symbol", "BTCUSDT"])
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["code"], "lifecycle_research_busy")
        self.assertEqual(blocked["job_id"], first["job_id"])
        self.assertEqual(replay["job"]["command"][-2:], ["--lifecycle-id", "12"])

    def test_scheduler_submits_at_most_one_job_per_tick_in_priority_order(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            first = jobs.lifecycle_intelligence_scheduler_tick(settings=settings, now=100_000, start=False)
            store = jobs.store_for_settings(settings)
            store.finish_job(first["jobs"][0]["job_id"], status="success", returncode=0)
            second = jobs.lifecycle_intelligence_scheduler_tick(settings=settings, now=100_060, start=False)
            store.finish_job(second["jobs"][0]["job_id"], status="success", returncode=0)
            third = jobs.lifecycle_intelligence_scheduler_tick(settings=settings, now=100_120, start=False)
        self.assertEqual(first["submitted"], ["lifecycle-intelligence"])
        self.assertEqual(second["submitted"], ["lifecycle-replay"])
        self.assertEqual(third["submitted"], ["lifecycle-analytics"])
        self.assertEqual(len(first["jobs"]), 1)

    def test_scheduler_does_not_overlap_research_dimensions(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            first = jobs.lifecycle_intelligence_scheduler_tick(settings=settings, now=100_000, start=False)
            blocked = jobs.lifecycle_intelligence_scheduler_tick(settings=settings, now=100_060, start=False)
        self.assertEqual(first["submitted"], ["lifecycle-intelligence"])
        self.assertEqual(blocked["submitted"], [])
        self.assertTrue(blocked["ok"])
        self.assertEqual(blocked["jobs"][0]["code"], "lifecycle_research_busy")


class LifecycleIntelligenceApiTests(unittest.TestCase):
    def test_empty_similarity_returns_insufficient_samples_without_stack(self) -> None:
        with TemporaryDirectory() as tmp:
            payload = public_lifecycle_similar_payload("BTCUSDT", settings=make_settings(tmp))
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["status"], "insufficient_mature_samples")
        self.assertNotIn("Traceback", json.dumps(payload, ensure_ascii=False))

    def test_public_payloads_are_projected_paginated_and_redacted(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            seed_intelligence(settings)
            listed = public_lifecycle_intelligence_list_payload(settings=settings, limit=10)
            detail = public_lifecycle_intelligence_detail_payload("BTCUSDT", settings=settings)
            replay = public_lifecycle_replay_payload("BTCUSDT", settings=settings)
            frames = public_lifecycle_replay_frames_payload("BTCUSDT", settings=settings, limit=100)
        self.assertTrue(listed["ok"])
        self.assertNotIn("items", listed)
        self.assertEqual(listed["data"]["items"][0]["upgrade_path"], "15m → 1h")
        self.assertEqual(frames["data"]["items"][0]["frame_index"], 1)
        self.assertNotIn("metrics", frames["data"]["items"][0])
        self.assertEqual(frames["data"]["pagination"]["total"], 1)
        text = json.dumps({"detail": detail, "replay": replay, "frames": frames}, ensure_ascii=False)
        for forbidden in (
            "BOT_TOKEN", "chat_id", "topic_id", "message_id", "dedup_key", "payload_json",
            "source_signature", "internal-fingerprint",
        ):
            self.assertNotIn(forbidden, text)

    def test_intelligence_list_filters_base_state_level_and_risk(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            seed_intelligence(settings)
            matched = public_lifecycle_intelligence_list_payload(
                settings=settings,
                state="upgraded_1h",
                level="1h",
                risk="中",
            )
            missing = public_lifecycle_intelligence_list_payload(
                settings=settings,
                state="failed",
            )
        self.assertEqual(matched["data"]["total"], 1)
        self.assertEqual(matched["data"]["items"][0]["symbol"], "BTCUSDT")
        self.assertEqual(missing["data"]["total"], 0)

    def test_public_route_and_private_route_auth_boundary(self) -> None:
        def handler(path: str):
            statuses: list[int] = []
            instance = object.__new__(web.WebHandler)
            instance.path = path
            instance.headers = {}
            instance.server = type("Server", (), {"admin_token": "secret", "settings": Settings(web_auth_mode="password")})()
            instance.wfile = BytesIO()
            instance.send_response = lambda status: statuses.append(status)
            instance.send_header = lambda _key, _value: None
            instance.end_headers = lambda: None
            return instance, statuses

        public, public_status = handler("/public-api/lifecycle/intelligence/summary")
        with patch("paopao_radar.web.public_lifecycle_intelligence_summary_payload", return_value={"ok": True, "data": {}}):
            web.WebHandler.do_GET(public)
        self.assertEqual(public_status[-1], 200)

        private, private_status = handler("/api/lifecycle/intelligence/summary")
        web.WebHandler.do_GET(private)
        self.assertEqual(private_status[-1], 401)


class LifecycleIntelligenceFrontendTests(unittest.TestCase):
    def test_required_pages_and_markers_exist(self) -> None:
        lifecycle = (FRONTEND / "app/lifecycle/page.tsx").read_text(encoding="utf-8")
        replay = (FRONTEND / "app/lifecycle/replay/page.tsx").read_text(encoding="utf-8")
        coin = (FRONTEND / "app/coin/[symbol]/page.tsx").read_text(encoding="utf-8")
        home = (FRONTEND / "components/HomeDashboard.tsx").read_text(encoding="utf-8")
        for marker in ("生命周期智能排行", "智能评分", "当前阶段", "升级路径", "风险评分", "强趋势确认", "高质量启动", "模型诊断"):
            self.assertIn(marker, lifecycle)
        for marker in ("生命周期回放", "首次信号", "升级路径", "时间轴", "资金确认", "最终结果"):
            self.assertIn(marker, replay)
        self.assertIn('URLSearchParams(window.location.search).get("symbol")', replay)
        for marker in ("生命周期智能评价", "智能评分", "当前阶段", "历史相似生命周期", "打开回放"):
            self.assertIn(marker, coin)
        self.assertIn("生命周期智能榜", home)
        self.assertIn("slice(0, 5)", home)


if __name__ == "__main__":
    unittest.main()
