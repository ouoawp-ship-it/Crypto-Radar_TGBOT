from __future__ import annotations

import argparse
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import paopao_radar.cli as main
from paopao_radar.config import Settings
from paopao_radar.radar import RadarEngine
from paopao_radar.storage import JsonStore
from paopao_radar.telegram import TelegramGateway


class MainCommandTests(unittest.TestCase):
    def make_runtime(self, tmp: str):
        settings = Settings(
            base_dir=Path(tmp),
            data_dir=Path(tmp),
            tg_push_history_path=Path(tmp) / "push_history.json",
            runtime_status_path=Path(tmp) / "runtime_status.json",
            radar_state_path=Path(tmp) / "radar_state.json",
            funding_snapshot_path=Path(tmp) / "funding_snapshot.json",
            launch_state_path=Path(tmp) / "launch_state.json",
            launch_watchlist_path=Path(tmp) / "launch_watchlist.json",
            launch_watch_history_path=Path(tmp) / "launch_watch_history.json",
            divergence_state_path=Path(tmp) / "oi_divergence_state.json",
            divergence_cooldown_path=Path(tmp) / "oi_divergence_cooldown.json",
        )
        store = JsonStore(Path(tmp))
        gateway = TelegramGateway(settings, store)
        return settings, store, None, gateway

    def test_telegram_test_defaults_to_dry_run(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(main, "make_runtime", side_effect=lambda: self.make_runtime(tmp)):
                with redirect_stdout(StringIO()) as output:
                    code = main.main(["telegram-test"])

        self.assertEqual(code, 0)
        self.assertIn("telegram_test: dry_run", output.getvalue())

    def test_telegram_test_blocks_real_send_without_confirmation(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(main, "make_runtime", side_effect=lambda: self.make_runtime(tmp)):
                with redirect_stdout(StringIO()) as output:
                    code = main.main(["telegram-test", "--send"])

        self.assertEqual(code, 2)
        self.assertIn("telegram_test: blocked", output.getvalue())

    def test_readiness_reports_wait_when_history_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(main, "make_runtime", side_effect=lambda: self.make_runtime(tmp)):
                with redirect_stdout(StringIO()) as output:
                    code = main.main(["readiness"])

        self.assertEqual(code, 1)
        self.assertIn("真实推送准备度", output.getvalue())
        self.assertIn("WAIT", output.getvalue())

    def test_readiness_rejects_invalid_telegram_token_even_with_history(self) -> None:
        with TemporaryDirectory() as tmp:
            settings, store, _engine, _gateway = self.make_runtime(tmp)
            settings = Settings(
                base_dir=settings.base_dir,
                data_dir=settings.data_dir,
                tg_bot_token="",
                tg_chat_id="-1001234567890",
                tg_push_history_path=settings.tg_push_history_path,
                runtime_status_path=settings.runtime_status_path,
                launch_watch_history_path=settings.launch_watch_history_path,
            )
            for idx in range(5):
                store.append_record(settings.launch_watch_history_path, {"top_score": 1, "scanned": 1, "alert_count": 0, "top_symbols": [f"T{idx}"]})

            with redirect_stdout(StringIO()) as output:
                code = main.print_readiness(settings, store)

        self.assertEqual(code, 1)
        self.assertIn("TG_BOT_TOKEN 缺失或格式无效", output.getvalue())

    def test_telegram_test_blocks_invalid_config_before_real_send(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(main, "make_runtime", side_effect=lambda: self.make_runtime(tmp)):
                with redirect_stdout(StringIO()) as output:
                    code = main.main(["telegram-test", "--send", "--confirm-real-send"])

        self.assertEqual(code, 2)
        self.assertIn("invalid Telegram config", output.getvalue())

    def test_live_requires_explicit_real_send_confirmation(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(main, "make_runtime", side_effect=lambda: self.make_runtime(tmp)):
                with redirect_stdout(StringIO()) as output:
                    code = main.main(["live"])

        self.assertEqual(code, 2)
        self.assertIn("真实推送已阻止", output.getvalue())

    def test_runtime_status_reports_empty_before_first_write(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(main, "make_runtime", side_effect=lambda: self.make_runtime(tmp)):
                with redirect_stdout(StringIO()) as output:
                    code = main.main(["runtime-status"])

        self.assertEqual(code, 0)
        self.assertIn('"status": "empty"', output.getvalue())

    def test_stable_check_prints_summary_and_returns_ready_code(self) -> None:
        snapshot = {
            "generated_at": "2026-07-04 08:00:00",
            "git": {"version": "v1.36.0", "branch": "main", "commit": "abc123"},
            "stability": {
                "status": "ready",
                "label": "达到稳定版标准",
                "summary": "核心服务正常",
                "checks": [{"label": "后台服务", "status": "ok", "detail": "全部运行中"}],
            },
            "release_readiness": {
                "status": "complete_candidate",
                "label": "完整稳定版候选",
                "summary": "当前快照达到长期运行候选标准",
                "score": 100,
                "ok_count": 6,
                "warn_count": 0,
                "fail_count": 0,
                "next_version_goal": "可以进入下一阶段",
                "checks": [{"label": "当前稳定版验收", "status": "ok", "detail": "当前 stable-check 已通过"}],
            },
            "release_trend": {
                "status": "improved",
                "label": "趋势变好",
                "summary": "长期运行就绪度比上一次验收更好。",
                "current_score": 100,
                "previous_score": 84,
                "score_delta": 16,
                "action": "继续观察。",
            },
            "recommendations": ["当前快照没有发现明显异常。"],
        }

        with patch("paopao_radar.web.ops_snapshot_payload", return_value=snapshot):
            with redirect_stdout(StringIO()) as output:
                code = main.main(["stable-check", "--no-save"])

        self.assertEqual(code, 0)
        text = output.getvalue()
        self.assertIn("泡泡雷达稳定版自检", text)
        self.assertIn("达到稳定版标准", text)
        self.assertIn("长期运行就绪度", text)
        self.assertIn("完整稳定版候选", text)
        self.assertIn("评分: 100/100", text)
        self.assertIn("下一目标: 可以进入下一阶段", text)
        self.assertIn("趋势变化", text)
        self.assertIn("趋势变好", text)
        self.assertIn("变化 16", text)
        self.assertIn("后台服务: 通过", text)
        self.assertIn("本次未保存", text)

    def test_stable_check_json_outputs_snapshot_and_blocked_code(self) -> None:
        snapshot = {
            "ok": True,
            "stability": {
                "status": "blocked",
                "label": "未达稳定版标准",
                "summary": "1 个阻断项",
                "checks": [],
            },
        }

        with patch("paopao_radar.web.ops_snapshot_payload", return_value=snapshot):
            with redirect_stdout(StringIO()) as output:
                code = main.main(["stable-check", "--json", "--no-save"])

        self.assertEqual(code, 2)
        self.assertEqual(__import__("json").loads(output.getvalue())["stability"]["status"], "blocked")

    def test_write_runtime_status_persists_payload(self) -> None:
        with TemporaryDirectory() as tmp:
            settings, store, _engine, _gateway = self.make_runtime(tmp)
            payload = main.write_runtime_status(settings, store, "test", "running", task="unit")
            saved = store.load(settings.runtime_status_path, {})

        self.assertEqual(payload["mode"], "test")
        self.assertEqual(saved["status"], "running")
        self.assertEqual(saved["task"], "unit")

    def test_structure_runtime_status_uses_separate_file(self) -> None:
        with TemporaryDirectory() as tmp:
            settings, store, _engine, _gateway = self.make_runtime(tmp)
            settings = replace(
                settings,
                structure_runtime_status_path=Path(tmp) / "structure_runtime_status.json",
            )

            main.write_runtime_status(settings, store, "structure-loop", "running", task="structure-loop")
            main_saved = store.load(settings.runtime_status_path, {})
            structure_saved = store.load(settings.structure_runtime_status_path, {})

        self.assertEqual(main_saved, {})
        self.assertEqual(structure_saved["task"], "structure-loop")

    def test_make_runtime_for_args_applies_scan_limit_overrides(self) -> None:
        with TemporaryDirectory() as tmp:
            args = argparse.Namespace(radar_scan_limit=4, launch_scan_limit=3, flow_scan_limit=2, funding_scan_limit=5)
            with patch.object(main, "make_runtime", side_effect=lambda: self.make_runtime(tmp)):
                settings, _store, _engine, _gateway = main.make_runtime_for_args(args)

        self.assertEqual(settings.radar_scan_limit, 4)
        self.assertEqual(settings.launch_scan_limit, 3)
        self.assertEqual(settings.flow_scan_limit, 2)
        self.assertEqual(settings.funding_alert_scan_limit, 5)

    def test_next_interval_epoch_aligns_hourly_jobs_to_top_of_hour(self) -> None:
        base = main.datetime(2026, 5, 26, 17, 46, 30).timestamp()
        expected = main.datetime(2026, 5, 26, 18, 0, 0).timestamp()

        self.assertEqual(main.next_interval_epoch(base, 3600), expected)

    def test_next_closed_window_epoch_adds_post_close_delay(self) -> None:
        from datetime import timedelta, timezone
        from paopao_radar.time_windows import next_closed_window_epoch

        tz = timezone(timedelta(hours=8))
        base = main.datetime(2026, 5, 26, 17, 46, 30, tzinfo=tz).timestamp()
        expected = main.datetime(2026, 5, 26, 18, 5, 0, tzinfo=tz).timestamp()

        self.assertEqual(
            next_closed_window_epoch(base, interval_sec=3600, delay_sec=300),
            expected,
        )

    def test_announcements_test_prints_diagnostics(self) -> None:
        with TemporaryDirectory() as tmp:
            settings, store, _engine, gateway = self.make_runtime(tmp)
            engine = RadarEngine(settings, store)
            with patch.object(main, "make_runtime", return_value=(settings, store, engine, gateway)):
                with patch.object(main.BinanceDataSource, "announcements", return_value=[]):
                    with patch.object(main.BinanceDataSource, "usdt_perp_symbols", return_value=[]):
                        with redirect_stdout(StringIO()) as output:
                            code = main.main(["announcements-test"])

        self.assertEqual(code, 0)
        self.assertIn("announcements_test: ok", output.getvalue())
        self.assertIn("articles_scanned", output.getvalue())


if __name__ == "__main__":
    unittest.main()
