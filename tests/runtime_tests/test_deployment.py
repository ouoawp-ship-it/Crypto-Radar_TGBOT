from __future__ import annotations

import argparse
from datetime import datetime
import importlib.util
import json
import os
import subprocess
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import runtime.cli as main
from config import Settings
from runtime.radar_engine import RadarEngine
from shared.storage import JsonStore
from shared.telegram import PushResult, TelegramGateway


ROOT = Path(__file__).resolve().parents[2]


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
        self.assertTrue(is_ignored("data/backups/20260723T033000Z/manifest.json"))
        self.assertTrue(is_ignored("data/altcoin/state/production.json"))
        self.assertTrue(is_ignored("data/altcoin/db/realtime.db-wal"))

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
                "TG_BOT_TOKEN=\nTG_CHAT_ID=\n"
                "RADAR_SUMMARY_MIN_INTERVAL_SEC=21600\n",
                encoding="utf-8",
            )
            result = module.sync_env(env, example)
            text = env.read_text(encoding="utf-8")

        self.assertIn("TG_BOT_TOKEN=secret", text)
        self.assertIn("TG_CHAT_ID=-1001234567890", text)
        self.assertNotIn("COINALYZE_API_KEY", text)
        self.assertIn("RADAR_SUMMARY_MIN_INTERVAL_SEC=21600", text)
        self.assertIn("CUSTOM_KEEP=1", text)
        self.assertNotIn("WEB_PORT", text)
        self.assertIn("COINALYZE_API_KEY", result["removed"])
        self.assertIn("WEB_PORT", result["removed"])

    def test_sync_migrates_only_the_legacy_binance_market_stream_url(self) -> None:
        module = load_sync_module()
        with TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env.oi"
            example = Path(tmp) / ".env.oi.example"
            env.write_text(
                "BINANCE_FUTURES_WS_URL=wss://fstream.binance.com/ws\n",
                encoding="utf-8",
            )
            example.write_text(
                "BINANCE_FUTURES_WS_URL=wss://fstream.binance.com/market/ws\n",
                encoding="utf-8",
            )
            module.sync_env(env, example)
            text = env.read_text(encoding="utf-8")

        self.assertIn("BINANCE_FUTURES_WS_URL=wss://fstream.binance.com/market/ws", text)

    def test_sync_extends_default_signal_history_for_p2_calibration(self) -> None:
        module = load_sync_module()
        with TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env.oi"
            example = Path(tmp) / ".env.oi.example"
            env.write_text(
                "SIGNAL_EVENTS_LIMIT=5000\n"
                "SIGNAL_EVENTS_RETENTION_DAYS=60\n"
                "DATABASE_BACKUP_DIR=custom-backups\n",
                encoding="utf-8",
            )
            example.write_text(
                "SIGNAL_EVENTS_LIMIT=20000\n"
                "SIGNAL_EVENTS_RETENTION_DAYS=365\n"
                "DATABASE_BACKUP_DIR=backups\n",
                encoding="utf-8",
            )

            result = module.sync_env(env, example)
            text = env.read_text(encoding="utf-8")

        self.assertIn("SIGNAL_EVENTS_LIMIT=20000", text)
        self.assertIn("SIGNAL_EVENTS_RETENTION_DAYS=365", text)
        self.assertIn("DATABASE_BACKUP_DIR=custom-backups", text)
        self.assertEqual(
            sorted(result["updated"]),
            ["SIGNAL_EVENTS_LIMIT", "SIGNAL_EVENTS_RETENTION_DAYS"],
        )

    def test_sync_removes_retired_bot_configuration_keys(self) -> None:
        module = load_sync_module()
        retired = {
            "SIGNAL_EVENTS_FILE": "signal_events.json",
            "REALTIME_BYBIT_ENABLE": "false",
            "REALTIME_OKX_ENABLE": "false",
            "BYBIT_PUBLIC_REST_URL": "https://example.invalid",
            "BYBIT_LINEAR_WS_URL": "wss://example.invalid/ws",
            "OKX_PUBLIC_REST_URL": "https://example.invalid",
            "OKX_PUBLIC_WS_URL": "wss://example.invalid/ws",
            "ACCUMULATION_QUALITY_V2_ENABLE": "false",
            "TELEGRAM_ANNOUNCEMENT_ALERT_TOPIC_ID": "13",
            "TG_AUTO_CREATE_TOPICS": "true",
            "TG_TOPIC_INTRO_ENABLE": "true",
            "FLOW_CANDIDATE_POOL": "60",
            "FUNDING_ALERT_REPLY_CHAIN_ENABLE": "true",
            "LAUNCH_MULTI_EXCHANGE_FUNDING_ENABLE": "false",
            "LAUNCH_SMC_V4_ENABLE": "true",
            "LAUNCH_SMC_HISTORY_BARS": "400",
            "LAUNCH_SMC_SWING_LENGTH": "2",
            "LAUNCH_SMC_EQUAL_TOLERANCE_ATR": "0.15",
            "LAUNCH_SMC_DISPLACEMENT_BODY_ATR": "1.0",
            "LAUNCH_SMC_MAX_ZONE_AGE_BARS": "96",
            "LAUNCH_AI_AUTO_ENABLE": "false",
            "TG_BOT_USERNAME": "legacy_bot",
            "AI_API_KEY": "legacy-secret",
            "AI_BASE_URL": "https://example.invalid/v1",
            "AI_MODEL": "legacy-model",
            "AI_OPERATOR_PROMPT": "legacy prompt",
            "AI_ON_DEMAND_DAILY_LIMIT": "20",
        }
        with TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env.oi"
            example = Path(tmp) / ".env.oi.example"
            env.write_text(
                "\n".join(f"{key}={value}" for key, value in retired.items()) + "\n",
                encoding="utf-8",
            )
            example.write_text("SIGNAL_EVENTS_DB_FILE=signals.db\n", encoding="utf-8")

            result = module.sync_env(env, example)
            text = env.read_text(encoding="utf-8")

        for key in retired:
            self.assertNotIn(f"{key}=", text)
            self.assertIn(key, result["removed"])
            self.assertIn(key, module.RETIRED_KEYS)
        self.assertNotIn("SIGNAL_EVENTS_FILE", module.PRESERVE_KEYS)
        self.assertIn("TG_ANNOUNCEMENT_ALERT_TOPIC_ID=13", text)

    def test_sync_preserves_announcement_risk_topic(self) -> None:
        module = load_sync_module()
        with TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env.oi"
            example = Path(tmp) / ".env.oi.example"
            env.write_text(
                "TG_ANNOUNCEMENT_ALERT_TOPIC_ID=13\n",
                encoding="utf-8",
            )
            example.write_text(
                "TG_ANNOUNCEMENT_ALERT_TOPIC_ID=\n",
                encoding="utf-8",
            )

            module.sync_env(env, example)

            self.assertIn(
                "TG_ANNOUNCEMENT_ALERT_TOPIC_ID=13",
                env.read_text(encoding="utf-8"),
            )

    def test_sync_migrates_old_launch_switch_to_pulse_and_removes_old_keys(self) -> None:
        module = load_sync_module()
        with TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env.oi"
            example = Path(tmp) / ".env.oi.example"
            env.write_text(
                "LAUNCH_ALERT_ENABLE=false\n"
                "LAUNCH_SCAN_LIMIT=80\n"
                "LAUNCH_FUSION_ENABLE=true\n",
                encoding="utf-8",
            )
            example.write_text(
                "PULSE_RADAR_ENABLE=true\n"
                "SIMPLE_ALERT_SCAN_LIMIT=120\n"
                "DIVERGENCE_SCAN_LIMIT=200\n",
                encoding="utf-8",
            )

            result = module.sync_env(env, example)
            text = env.read_text(encoding="utf-8")

        self.assertIn("PULSE_RADAR_ENABLE=false", text)
        self.assertIn("SIMPLE_ALERT_SCAN_LIMIT=80", text)
        self.assertIn("DIVERGENCE_SCAN_LIMIT=200", text)
        self.assertNotIn("LAUNCH_", text)
        self.assertIn("LAUNCH_ALERT_ENABLE", result["removed"])
        self.assertIn("LAUNCH_SCAN_LIMIT", result["removed"])
        self.assertIn("LAUNCH_FUSION_ENABLE", result["removed"])

    def test_sync_backs_up_and_atomically_replaces_environment_file(self) -> None:
        module = load_sync_module()
        with TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env.oi"
            example = Path(tmp) / ".env.oi.example"
            original = b"TG_BOT_TOKEN=test-secret\nRADAR_SUMMARY_MIN_INTERVAL_SEC=1800\n"
            env.write_bytes(original)
            example.write_text(
                "TG_BOT_TOKEN=\nRADAR_SUMMARY_MIN_INTERVAL_SEC=21600\n",
                encoding="utf-8",
            )

            module.sync_env(env, example)

            backups = list(env.parent.glob(".env.oi.bak.*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original)
            self.assertIn(
                "RADAR_SUMMARY_MIN_INTERVAL_SEC=21600",
                env.read_text(encoding="utf-8"),
            )
            self.assertTrue(env.with_name(".env.oi.lock").exists())
            self.assertEqual(list(env.parent.glob("..env.oi.*.tmp")), [])
            if os.name == "posix":
                self.assertEqual(env.stat().st_mode & 0o777, 0o600)
                self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
                self.assertEqual(
                    env.with_name(".env.oi.lock").stat().st_mode & 0o777,
                    0o600,
                )

    def test_sync_failure_preserves_original_environment_file(self) -> None:
        module = load_sync_module()
        with TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env.oi"
            example = Path(tmp) / ".env.oi.example"
            original = b"TG_BOT_TOKEN=test-secret\nRADAR_SUMMARY_MIN_INTERVAL_SEC=1800\n"
            env.write_bytes(original)
            example.write_text(
                "TG_BOT_TOKEN=\nRADAR_SUMMARY_MIN_INTERVAL_SEC=21600\n",
                encoding="utf-8",
            )
            real_replace = module.os.replace

            def fail_target_replace(source: object, destination: object) -> None:
                if Path(destination) == env:
                    raise OSError("simulated atomic replace failure")
                real_replace(source, destination)

            with patch.object(module.os, "replace", side_effect=fail_target_replace):
                with self.assertRaises(OSError):
                    module.sync_env(env, example)

            self.assertEqual(env.read_bytes(), original)
            backups = list(env.parent.glob(".env.oi.bak.*"))
            self.assertEqual(len(backups), 1)
            self.assertEqual(backups[0].read_bytes(), original)
            self.assertEqual(list(env.parent.glob("..env.oi.*.tmp")), [])

    def test_sync_cli_reports_only_key_names_and_counts(self) -> None:
        module = load_sync_module()
        with TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env.oi"
            example = Path(tmp) / ".env.oi.example"
            env.write_text(
                "TG_BOT_TOKEN=test-secret\nRADAR_SUMMARY_MIN_INTERVAL_SEC=1800\n",
                encoding="utf-8",
            )
            example.write_text(
                "TG_BOT_TOKEN=\nRADAR_SUMMARY_MIN_INTERVAL_SEC=21600\n",
                encoding="utf-8",
            )
            output = StringIO()
            with (
                patch.object(
                    module.sys,
                    "argv",
                    [
                        "sync_env.py",
                        "--env",
                        str(env),
                        "--example",
                        str(example),
                    ],
                ),
                redirect_stdout(output),
            ):
                code = module.main()

        self.assertEqual(code, 0)
        self.assertIn("RADAR_SUMMARY_MIN_INTERVAL_SEC", output.getvalue())
        self.assertNotIn("test-secret", output.getvalue())


class BotOnlyDeploymentTests(unittest.TestCase):
    def test_server_scripts_install_only_bot_runtime_services(self) -> None:
        install = (ROOT / "scripts" / "install_server.sh").read_text(encoding="utf-8")
        update = (ROOT / "scripts" / "update_server.sh").read_text(encoding="utf-8")
        main_installer = (
            ROOT / "scripts" / "install_main_bot_service.sh"
        ).read_text(encoding="utf-8")
        main_runner = (
            ROOT / "scripts" / "run_main_bot.sh"
        ).read_text(encoding="utf-8")
        market_stream_installer = (
            ROOT / "scripts" / "install_market_stream_service.sh"
        ).read_text(encoding="utf-8")
        market_stream_runner = (
            ROOT / "scripts" / "run_market_stream.sh"
        ).read_text(encoding="utf-8")
        combined = install + "\n" + update
        service_installers = main_installer + "\n" + market_stream_installer

        self.assertIn("install_main_bot_service.sh", combined)
        self.assertNotIn(
            '"live --send --confirm-real-send"',
            combined,
        )
        self.assertIn(
            "ExecStart=${APP_DIR}/scripts/run_main_bot.sh",
            main_installer,
        )
        self.assertIn('args+=("loop")', main_runner)
        self.assertIn(
            'args+=("live" "--send" "--confirm-real-send")',
            main_runner,
        )
        self.assertIn("install_market_stream_service.sh", combined)
        self.assertIn(
            "ExecStart=${APP_DIR}/scripts/run_market_stream.sh",
            market_stream_installer,
        )
        self.assertIn('"${APP_DIR}/main.py" market-stream', market_stream_runner)
        self.assertIn("paopao-radar", combined)
        self.assertIn("paopao-market-stream", combined)
        self.assertIn("paopao-health", combined)
        self.assertIn("paopao-backup", combined)
        self.assertIn("database-backup", combined)
        self.assertIn("MemoryHigh=", service_installers)
        self.assertIn("MemoryMax=", service_installers)
        self.assertIn("LimitNOFILE=65536", service_installers)
        self.assertIn("systemd_health_check.sh", combined)
        self.assertIn("OnUnitActiveSec=5min", combined)
        self.assertIn("OnCalendar=*-*-* 03:30:00 UTC", combined)
        self.assertNotIn("paopao-frontend", install)
        self.assertNotIn("paopao-web", install)
        self.assertNotIn("paopao-ai", install)
        self.assertIn("paopao-frontend", update)
        self.assertIn("paopao-web", update)
        self.assertIn("paopao-ai", update)
        self.assertNotIn("npm ", combined)
        self.assertNotIn("proxy_pass", combined)

    def test_systemd_health_wrapper_only_fails_for_blocking_or_unexpected_exit(self) -> None:
        script = (ROOT / "scripts" / "systemd_health_check.sh").read_text(encoding="utf-8")

        self.assertIn("stable-check --json --no-save", script)
        self.assertIn("0|1)", script)
        self.assertIn("2)", script)
        self.assertIn('exit "$code"', script)

    def test_update_script_keeps_safe_fast_forward_and_validation_gates(self) -> None:
        script = (ROOT / "scripts" / "update_server.sh").read_text(encoding="utf-8")

        self.assertIn("git pull --ff-only", script)
        self.assertIn("python -m unittest discover", script)
        self.assertIn("main.py stable-check", script)
        self.assertIn('if [ "$code" -ge 2 ]', script)
        self.assertIn("bot_stable_check_attention_non_blocking", script)
        self.assertIn("retire_legacy_services", script)

    def test_update_script_reexecutes_after_pull_to_load_new_deployment_logic(self) -> None:
        script = (ROOT / "scripts" / "update_server.sh").read_text(encoding="utf-8")

        pull_index = script.index('git pull --ff-only "$REMOTE" "$BRANCH"')
        reexec_index = script.index("export PAOPAO_UPDATE_REEXEC=1")
        self.assertGreater(reexec_index, pull_index)
        self.assertIn('exec bash "${APP_DIR}/scripts/update_server.sh" --yes', script)

    def test_cli_no_longer_exposes_web_or_ai_commands(self) -> None:
        parser = main.build_parser()
        command_action = next(action for action in parser._actions if action.dest == "command")

        self.assertNotIn("web", command_action.choices)
        self.assertNotIn("admin-password", command_action.choices)
        self.assertNotIn("ai-assistant", command_action.choices)
        self.assertNotIn("price-alerts", command_action.choices)
        self.assertNotIn("provider-check", command_action.choices)
        self.assertNotIn("migrate-state", command_action.choices)
        self.assertIn("database-backup", command_action.choices)


class PulseReadinessTests(unittest.TestCase):
    def test_readiness_accepts_direct_pulse_configuration(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(
                base_dir=root,
                data_dir=root,
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcd",
                tg_chat_id="-1001234567890",
                tg_radar_summary_topic_id="11",
                tg_launch_alert_topic_id="12",
                tg_announcement_alert_topic_id="15",
                tg_flow_radar_topic_id="13",
                tg_topic_routes_path=root / "topic_routes.json",
            )
            store = JsonStore(root)
            store.save(settings.tg_topic_routes_path, {
                "routes": {
                    "TG_FUNDING_ALERT": {"topic_id": "14"},
                }
            })

            with patch.object(main, "runtime_health_checks", return_value=[]):
                with redirect_stdout(StringIO()) as output:
                    code = main.print_readiness(settings, store)

            self.assertEqual(code, 0)
            self.assertIn("脉冲雷达已启用", output.getvalue())
            self.assertIn("脉冲扫描上限 15m=120，2h=200", output.getvalue())
            self.assertNotIn("shadow", output.getvalue().lower())

    def test_readiness_fails_when_a_production_topic_is_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(
                base_dir=root,
                data_dir=root,
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcd",
                tg_chat_id="-1001234567890",
                tg_radar_summary_topic_id="11",
                tg_launch_alert_topic_id="12",
                tg_announcement_alert_topic_id="14",
                tg_flow_radar_topic_id="13",
                tg_topic_routes_path=root / "topic_routes.json",
            )
            store = JsonStore(root)

            with patch.object(main, "runtime_health_checks", return_value=[]):
                with redirect_stdout(StringIO()) as output:
                    code = main.print_readiness(settings, store)

            self.assertEqual(code, 1)
            text = output.getvalue()
            self.assertIn("⏳ 待处理 资金费率警报专属话题", text)
            self.assertIn("资金费率警报专属话题未配置", text)
            self.assertNotIn("telegram_topic_test_message", text)


class MainCommandTests(unittest.TestCase):
    @staticmethod
    def make_runtime(tmp: str, *, configured: bool = False):
        settings = Settings(
            base_dir=Path(tmp),
            data_dir=Path(tmp),
            tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcd" if configured else "",
            tg_chat_id="-1001234567890" if configured else "",
            tg_push_history_path=Path(tmp) / "push_history.json",
            tg_topic_routes_path=Path(tmp) / "topic_routes.json",
            runtime_status_path=Path(tmp) / "runtime_status.json",
            radar_state_path=Path(tmp) / "radar_state.json",
            funding_snapshot_path=Path(tmp) / "funding_snapshot.json",
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
        self.assertIn(
            "Telegram 测试：安全演练，未发送真实消息",
            output.getvalue(),
        )
        self.assertNotIn("send_flag_not_set", output.getvalue())

    def test_telegram_test_blocks_real_send_without_confirmation(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(main, "make_runtime", side_effect=lambda: self.make_runtime(tmp)):
                with redirect_stdout(StringIO()) as output:
                    code = main.main(["telegram-test", "--send"])

        self.assertEqual(code, 2)
        self.assertIn("Telegram 测试：已阻止", output.getvalue())
        self.assertIn("未完成真实发送的第二重确认", output.getvalue())
        self.assertNotIn("missing_confirm_real_send", output.getvalue())

    def test_telegram_test_translates_hourly_limit_without_changing_code(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = self.make_runtime(tmp, configured=True)
            gateway = runtime[3]
            result = PushResult(
                "skipped",
                "global_hourly_limit",
                False,
            )
            with (
                patch.object(main, "make_runtime", return_value=runtime),
                patch.object(
                    gateway,
                    "send",
                    return_value=result,
                ),
                redirect_stdout(StringIO()) as output,
            ):
                code = main.main(
                    ["telegram-test", "--send", "--confirm-real-send"]
                )

        self.assertEqual(code, 0)
        self.assertIn("Telegram 测试：已跳过", output.getvalue())
        self.assertIn("本小时发送额度已用完", output.getvalue())
        self.assertNotIn("global_hourly_limit", output.getvalue())
        self.assertEqual(result.status, "skipped")
        self.assertEqual(result.reason, "global_hourly_limit")

    def test_telegram_topic_setup_cli_requires_both_real_send_flags(self) -> None:
        with TemporaryDirectory() as tmp:
            runtime = self.make_runtime(tmp, configured=True)
            with (
                patch.object(main, "make_runtime", return_value=runtime),
                patch.object(main, "make_runtime_for_args", return_value=runtime),
                patch.object(TelegramGateway, "_create_forum_topic") as create_mock,
                patch.object(TelegramGateway, "_send_real_message_ids") as send_mock,
            ):
                for argv, reason in (
                    (
                        [
                            "telegram-topic-setup",
                            "--topic-template",
                            "TG_RADAR_SUMMARY",
                        ],
                        "send_flag_not_set",
                    ),
                    (
                        [
                            "telegram-topic-setup",
                            "--topic-template",
                            "TG_RADAR_SUMMARY",
                            "--send",
                        ],
                        "missing_confirm_real_send",
                    ),
                ):
                    with self.subTest(argv=argv):
                        with redirect_stdout(StringIO()) as output:
                            code = main.main(argv)
                        self.assertEqual(code, 2)
                        self.assertIn(reason, output.getvalue())

            create_mock.assert_not_called()
            send_mock.assert_not_called()

    def test_readiness_reports_wait_when_history_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(main, "make_runtime", side_effect=lambda: self.make_runtime(tmp)):
                with redirect_stdout(StringIO()) as output:
                    code = main.main(["readiness"])

        self.assertEqual(code, 1)
        self.assertIn("真实推送准备度", output.getvalue())
        self.assertIn("⏳ 待处理", output.getvalue())

    def test_live_bootstrap_refreshes_stale_snapshot_without_telegram(self) -> None:
        with TemporaryDirectory() as tmp:
            settings, store, _engine, _gateway = self.make_runtime(tmp)
            source = MagicMock()
            with patch.object(
                main,
                "runtime_health_checks",
                return_value=[{
                    "name": "market_snapshots_freshness",
                    "status": "fail",
                }],
            ), patch.object(
                main,
                "BinanceDataSource",
                return_value=source,
            ), patch.object(
                main,
                "persist_market_batch",
                return_value={"status": "saved", "count": 80},
            ) as persist:
                result = main.bootstrap_live_market_snapshot(settings, store)

        self.assertEqual(result["status"], "saved")
        self.assertEqual(result["count"], 80)
        self.assertEqual(result["telegram_calls"], 0)
        persist.assert_called_once_with(settings, source=source, force=True)
        source.close.assert_called_once_with()

    def test_live_bootstrap_skips_when_snapshot_is_fresh(self) -> None:
        with TemporaryDirectory() as tmp:
            settings, store, _engine, _gateway = self.make_runtime(tmp)
            with patch.object(
                main,
                "runtime_health_checks",
                return_value=[{
                    "name": "market_snapshots_freshness",
                    "status": "ok",
                }],
            ), patch.object(main, "BinanceDataSource") as source:
                result = main.bootstrap_live_market_snapshot(settings, store)

        self.assertEqual(result, {"status": "not_needed"})
        source.assert_not_called()

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

    def test_loop_runtime_status_merges_independent_radar_updates(self) -> None:
        with TemporaryDirectory() as tmp:
            settings, store, _engine, _gateway = self.make_runtime(tmp)
            main.write_runtime_status(
                settings,
                store,
                "loop",
                "running",
                task="loop",
                last_summary_at="summary-time",
                summary_push="dry_run",
                launch_pushes=[{"status": "sent"}],
                launch_scan_limit=80,
                launch_cycle_status="ok",
                launch_error_code="",
                launch_interval_sec=180,
                diagnostics={
                    "summary": {"status": "ok"},
                    "launch": {"status": "legacy"},
                },
            )
            main.write_runtime_status(
                settings,
                store,
                "loop",
                "running",
                task="loop",
                last_launch_at="launch-time",
                pulse_cycle_status="ok",
                diagnostics={"pulse": {"status": "ok"}},
            )
            saved = store.load(settings.runtime_status_path, {})

        self.assertEqual(saved["last_summary_at"], "summary-time")
        self.assertEqual(saved["summary_push"], "dry_run")
        self.assertEqual(saved["last_launch_at"], "launch-time")
        self.assertEqual(saved["diagnostics"]["summary"]["status"], "ok")
        self.assertEqual(saved["diagnostics"]["pulse"]["status"], "ok")
        self.assertNotIn("launch", saved["diagnostics"])
        for legacy_key in (
            "launch_pushes",
            "launch_scan_limit",
            "launch_cycle_status",
            "launch_error_code",
            "launch_interval_sec",
        ):
            self.assertNotIn(legacy_key, saved)

    def test_live_loop_heartbeat_preserves_radar_schedule_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            settings, store, _engine, _gateway = self.make_runtime(tmp)
            main.write_runtime_status(
                settings,
                store,
                "live",
                "running",
                task="loop",
                last_summary_at="summary-time",
                next_summary_at="next-summary-time",
            )
            main.write_runtime_status(
                settings,
                store,
                "live",
                "running",
                task="loop",
                real_send=True,
            )
            saved = store.load(settings.runtime_status_path, {})

        self.assertEqual(saved["last_summary_at"], "summary-time")
        self.assertEqual(saved["next_summary_at"], "next-summary-time")
        self.assertTrue(saved["real_send"])

    def test_live_summary_run_preserves_loop_schedule_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            settings, store, _engine, _gateway = self.make_runtime(tmp)
            store.save(settings.runtime_status_path, {
                "mode": "live",
                "task": "loop",
                "status": "running",
                "next_flow_at": "next-flow-time",
                "next_funding_alert_at": "next-funding-time",
            })
            engine = MagicMock()
            engine.run_once.return_value = {
                "summary": {
                    "text": "summary",
                    "template_id": "TG_RADAR_SUMMARY",
                    "dedup_key": "summary:test",
                    "context_records": [],
                },
                "diagnostics": {},
            }
            gateway = MagicMock()
            gateway.send.return_value.status = "sent"
            gateway.send.return_value.reason = "ok"
            args = argparse.Namespace(
                command="live",
                send=True,
                confirm_real_send=True,
                no_launch=True,
                no_announcements=True,
                no_flow=True,
                no_funding_alert=True,
            )
            with (
                patch.object(
                    main,
                    "make_runtime_for_args",
                    return_value=(settings, store, engine, gateway),
                ),
                patch.object(
                    main,
                    "refresh_signal_effectiveness",
                    return_value={"status": "ok"},
                ) as refresh,
                redirect_stdout(StringIO()),
            ):
                code = main.run_once(args)
            saved = store.load(settings.runtime_status_path, {})

        self.assertEqual(code, 0)
        self.assertEqual(saved["mode"], "live")
        self.assertEqual(saved["task"], "loop")
        self.assertEqual(saved["status"], "running")
        self.assertEqual(saved["next_flow_at"], "next-flow-time")
        self.assertEqual(
            saved["next_funding_alert_at"], "next-funding-time"
        )
        refresh.assert_called_once_with(settings)

    def test_non_loop_runtime_write_does_not_inherit_loop_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            settings, store, _engine, _gateway = self.make_runtime(tmp)
            main.write_runtime_status(
                settings,
                store,
                "loop",
                "running",
                task="loop",
                last_summary_at="old",
            )
            main.write_runtime_status(
                settings,
                store,
                "once",
                "completed",
                task="once",
            )
            saved = store.load(settings.runtime_status_path, {})

        self.assertNotIn("last_summary_at", saved)

    def test_radar_status_cli_is_local_and_redacted(self) -> None:
        with TemporaryDirectory() as tmp:
            settings, store, _engine, gateway = self.make_runtime(tmp)
            store.save(settings.runtime_status_path, {
                "updated_at": datetime.now().strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "mode": "loop",
                "task": "loop",
                "status": "running",
                "real_send": False,
                "diagnostics": {"secret": "must-not-appear"},
            })
            with patch.object(
                main,
                "make_runtime",
                return_value=(settings, store, None, gateway),
            ), redirect_stdout(StringIO()) as output:
                code = main.main(["radar-status"])

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "running")
        self.assertEqual(payload["telegram_http_policy"], "zero_by_dry_run")
        self.assertFalse(payload["network_activity"])
        self.assertNotIn("must-not-appear", output.getvalue())

    def test_make_runtime_for_args_applies_scan_limit_overrides(self) -> None:
        with TemporaryDirectory() as tmp:
            args = argparse.Namespace(radar_scan_limit=4, pulse_scan_limit=3, flow_scan_limit=2, funding_scan_limit=5)
            with patch.object(main, "make_runtime", side_effect=lambda: self.make_runtime(tmp)):
                settings, _store, _engine, _gateway = main.make_runtime_for_args(args)

        self.assertEqual(settings.radar_scan_limit, 4)
        self.assertEqual(settings.pulse_simple_scan_limit, 3)
        self.assertEqual(settings.pulse_divergence_scan_limit, 3)
        self.assertEqual(settings.flow_scan_limit, 2)
        self.assertEqual(settings.funding_alert_scan_limit, 5)

    def test_standalone_announcements_command_is_removed(self) -> None:
        parser = main.build_parser()
        command_action = next(
            action for action in parser._actions if action.dest == "command"
        )

        self.assertNotIn("announcements-test", command_action.choices)


if __name__ == "__main__":
    unittest.main()
