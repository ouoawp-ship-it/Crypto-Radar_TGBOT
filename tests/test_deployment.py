from __future__ import annotations

import argparse
import importlib.util
import json
import subprocess
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import paopao_radar.cli as main
from paopao_radar.config import Settings
from paopao_radar.radar import RadarEngine
from paopao_radar.storage import JsonStore
from paopao_radar.telegram import TelegramGateway


ROOT = Path(__file__).resolve().parents[1]


def is_ignored(path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--quiet", "--", path],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


class GitIgnoreHardeningTests(unittest.TestCase):
    def test_runtime_env_backups_are_ignored(self) -> None:
        self.assertTrue(is_ignored(".env.oi.bak.20260710_000000"))
        self.assertTrue(is_ignored("runtime-config.bak"))
        self.assertTrue(is_ignored("data/tg_push_history.json.lock"))

    def test_example_env_files_remain_trackable(self) -> None:
        self.assertFalse(is_ignored(".env.oi.example"))


def load_sync_module():
    path = ROOT / "scripts" / "sync_env.py"
    spec = importlib.util.spec_from_file_location("sync_env", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EnvSyncTests(unittest.TestCase):
    def test_sync_updates_defaults_preserves_secrets_and_removes_web_keys(self) -> None:
        module = load_sync_module()
        with TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env.oi"
            example = Path(tmp) / ".env.oi.example"
            env.write_text(
                "TG_BOT_TOKEN=secret\nTG_CHAT_ID=-1001234567890\n"
                "COINALYZE_API_KEY=ca-secret\n"
                "RADAR_SUMMARY_MIN_INTERVAL_SEC=1800\nWEB_PORT=8080\nCUSTOM_KEEP=1\n",
                encoding="utf-8",
            )
            example.write_text(
                "TG_BOT_TOKEN=\nTG_CHAT_ID=\nCOINALYZE_API_KEY=\n"
                "RADAR_SUMMARY_MIN_INTERVAL_SEC=21600\n",
                encoding="utf-8",
            )
            result = module.sync_env(env, example)
            text = env.read_text(encoding="utf-8")

        self.assertIn("TG_BOT_TOKEN=secret", text)
        self.assertIn("TG_CHAT_ID=-1001234567890", text)
        self.assertIn("COINALYZE_API_KEY=ca-secret", text)
        self.assertIn("RADAR_SUMMARY_MIN_INTERVAL_SEC=21600", text)
        self.assertIn("CUSTOM_KEEP=1", text)
        self.assertNotIn("WEB_PORT", text)
        self.assertIn("WEB_PORT", result["removed"])

    def test_sync_migrates_only_the_legacy_binance_market_stream_url(self) -> None:
        module = load_sync_module()
        with TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env.oi"
            example = Path(tmp) / ".env.oi.example"
            env.write_text(
                "BINANCE_FUTURES_WS_URL=wss://fstream.binance.com/ws\n"
                "BYBIT_LINEAR_WS_URL=wss://custom.example/ws\n",
                encoding="utf-8",
            )
            example.write_text(
                "BINANCE_FUTURES_WS_URL=wss://fstream.binance.com/market/ws\n"
                "BYBIT_LINEAR_WS_URL=wss://stream.bybit.com/v5/public/linear\n",
                encoding="utf-8",
            )
            module.sync_env(env, example)
            text = env.read_text(encoding="utf-8")

        self.assertIn("BINANCE_FUTURES_WS_URL=wss://fstream.binance.com/market/ws", text)
        self.assertIn("BYBIT_LINEAR_WS_URL=wss://custom.example/ws", text)


class BotOnlyDeploymentTests(unittest.TestCase):
    def test_server_scripts_install_only_bot_runtime_services(self) -> None:
        install = (ROOT / "scripts" / "install_server.sh").read_text(encoding="utf-8")
        update = (ROOT / "scripts" / "update_server.sh").read_text(encoding="utf-8")
        combined = install + "\n" + update

        self.assertIn('"live --send --confirm-real-send"', combined)
        self.assertIn('"market-stream"', combined)
        self.assertIn("main.py ${command}", combined)
        self.assertIn("paopao-radar", combined)
        self.assertIn("paopao-market-stream", combined)
        self.assertIn("paopao-health", combined)
        self.assertIn("MemoryHigh=", combined)
        self.assertIn("MemoryMax=", combined)
        self.assertIn("LimitNOFILE=65536", combined)
        self.assertIn("main.py stable-check --json --no-save", combined)
        self.assertIn("OnUnitActiveSec=5min", combined)
        self.assertNotIn("paopao-frontend", install)
        self.assertNotIn("paopao-web", install)
        self.assertNotIn("paopao-ai", install)
        self.assertIn("paopao-frontend", update)
        self.assertIn("paopao-web", update)
        self.assertIn("paopao-ai", update)
        self.assertNotIn("npm ", combined)
        self.assertNotIn("proxy_pass", combined)

    def test_update_script_keeps_safe_fast_forward_and_validation_gates(self) -> None:
        script = (ROOT / "scripts" / "update_server.sh").read_text(encoding="utf-8")

        self.assertIn("git pull --ff-only", script)
        self.assertIn("python -m unittest discover", script)
        self.assertIn("main.py stable-check", script)
        self.assertIn("retire_legacy_services", script)

    def test_cli_no_longer_exposes_web_or_ai_commands(self) -> None:
        parser = main.build_parser()
        command_action = next(action for action in parser._actions if action.dest == "command")

        self.assertNotIn("web", command_action.choices)
        self.assertNotIn("admin-password", command_action.choices)
        self.assertNotIn("ai-assistant", command_action.choices)
        self.assertNotIn("price-alerts", command_action.choices)


class LaunchReportTests(unittest.TestCase):
    def test_launch_report_summarizes_scores_and_buckets(self) -> None:
        settings = Settings(base_dir=Path("."), data_dir=Path("data"))
        report = main.build_launch_report(
            [
                {
                    "top_score": 20,
                    "scanned": 2,
                    "alert_count": 0,
                    "buckets": {"idle": 2, "watching": 0},
                    "top_symbols": ["BTCUSDT", "ETHUSDT"],
                },
                {
                    "top_score": 60,
                    "scanned": 2,
                    "alert_count": 1,
                    "buckets": {"idle": 1, "primed": 1},
                    "top_symbols": ["ETHUSDT", "BTCUSDT"],
                },
            ],
            settings,
        )

        self.assertEqual(report["records"], 2)
        self.assertEqual(report["total_scanned"], 4)
        self.assertEqual(report["total_alerts"], 1)
        self.assertEqual(report["max_top_score"], 60)
        self.assertEqual(report["avg_top_score"], 40)
        self.assertEqual(report["buckets"]["primed"], 1)
        self.assertEqual(report["top_symbols"][0], ("BTCUSDT", 2))

    def test_launch_report_ignores_excluded_symbols(self) -> None:
        settings = Settings(excluded_base_assets=("XAU", "XAG"))
        report = main.build_launch_report(
            [{"top_score": 10, "scanned": 3, "alert_count": 0, "buckets": {}, "top_symbols": ["XAUUSDT", "BTCUSDT"]}],
            settings,
        )

        self.assertEqual(report["top_symbols"], [("BTCUSDT", 1)])


class MainCommandTests(unittest.TestCase):
    @staticmethod
    def make_runtime(tmp: str, *, configured: bool = False):
        settings = Settings(
            base_dir=Path(tmp),
            data_dir=Path(tmp),
            tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcd" if configured else "",
            tg_chat_id="-1001234567890" if configured else "",
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

    def test_stable_check_reports_ready_bot_only_scope(self) -> None:
        with TemporaryDirectory() as tmp:
            settings, store, _engine, gateway = self.make_runtime(tmp, configured=True)
            (settings.base_dir / "VERSION").write_text("v2.0.0\n", encoding="utf-8")
            for path in (
                settings.runtime_status_path,
                settings.signal_events_db_path,
                settings.market_snapshots_db_path,
                settings.realtime_features_db_path,
            ):
                path.touch()
            with patch.object(main, "make_runtime", return_value=(settings, store, None, gateway)), \
                    patch.object(main, "runtime_health_checks", return_value=[]):
                with redirect_stdout(StringIO()) as output:
                    code = main.main(["stable-check", "--no-save"])

        self.assertEqual(code, 0)
        self.assertIn("BOT-only", output.getvalue())
        self.assertIn("达到稳定版标准", output.getvalue())
        self.assertIn("本次未保存", output.getvalue())

    def test_stable_check_json_blocks_invalid_telegram_config(self) -> None:
        with TemporaryDirectory() as tmp:
            settings, store, _engine, gateway = self.make_runtime(tmp)
            with patch.object(main, "make_runtime", return_value=(settings, store, None, gateway)):
                with redirect_stdout(StringIO()) as output:
                    code = main.main(["stable-check", "--json", "--no-save"])

        self.assertEqual(code, 2)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["scope"], "telegram-bot-only")
        self.assertEqual(payload["stability"]["status"], "blocked")

    def test_write_runtime_status_persists_payload(self) -> None:
        with TemporaryDirectory() as tmp:
            settings, store, _engine, _gateway = self.make_runtime(tmp)
            payload = main.write_runtime_status(settings, store, "test", "running", task="unit")
            saved = store.load(settings.runtime_status_path, {})

        self.assertEqual(payload["mode"], "test")
        self.assertEqual(saved["status"], "running")

    def test_make_runtime_for_args_applies_scan_limit_overrides(self) -> None:
        with TemporaryDirectory() as tmp:
            args = argparse.Namespace(radar_scan_limit=4, launch_scan_limit=3, flow_scan_limit=2, funding_scan_limit=5)
            with patch.object(main, "make_runtime", side_effect=lambda: self.make_runtime(tmp)):
                settings, _store, _engine, _gateway = main.make_runtime_for_args(args)

        self.assertEqual(settings.radar_scan_limit, 4)
        self.assertEqual(settings.launch_scan_limit, 3)
        self.assertEqual(settings.flow_scan_limit, 2)
        self.assertEqual(settings.funding_alert_scan_limit, 5)

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


if __name__ == "__main__":
    unittest.main()
