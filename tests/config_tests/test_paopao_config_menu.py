from __future__ import annotations

from io import StringIO
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.paopao_config import (
    ConfigManager,
    ConfigManagerError,
    _read_value,
    build_parser,
)


ROOT = Path(__file__).resolve().parents[2]
MENU = ROOT / "scripts" / "paopao_menu.sh"


def _env_values(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
    )


class ConfigManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "config").mkdir()
        self.manager = ConfigManager(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_defaults_are_safe_and_redacted(self) -> None:
        status = self.manager.status()
        self.assertEqual(status["MAIN_BOT_DELIVERY_MODE"], "dry_run")
        self.assertFalse(status["MAIN_BOT_REAL_SEND"])
        self.assertEqual(
            status["MAIN_BOT_REAL_SEND_ACK"], "not_configured"
        )
        self.assertEqual(status["TG_BOT_TOKEN"], "not_configured")
        self.assertTrue(status["PULSE_RADAR_ENABLE"])

    def test_non_allowlisted_key_is_rejected(self) -> None:
        with self.assertRaises(ConfigManagerError):
            self.manager.set("ONCHAIN_BASE_HTTP_RPC_URL", "https://example.test")

    def test_legacy_root_config_blocks_new_writes(self) -> None:
        (self.root / ".env.oi").write_text(
            "TG_BOT_TOKEN=123456:legacy-secret\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            ConfigManagerError,
            "env_layout_migration_required",
        ):
            self.manager.set("TG_CHAT_ID", "-1001")

        self.assertFalse((self.root / "config" / ".env.oi").exists())
        self.assertEqual(
            self.manager.status()["TG_BOT_TOKEN"],
            "configured",
        )

    def test_comments_and_unknown_fields_are_preserved(self) -> None:
        path = self.root / "config" / ".env.oi"
        path.write_text(
            "# keep this comment\nCUSTOM_KEEP=1\n",
            encoding="utf-8",
        )
        self.manager.set("TG_BOT_TOKEN", "123456:fake-secret")
        text = path.read_text(encoding="utf-8")
        self.assertIn("# keep this comment", text)
        self.assertIn("CUSTOM_KEEP=1", text)
        self.assertIn("TG_BOT_TOKEN=123456:fake-secret", text)

    def test_secret_status_does_not_echo_value(self) -> None:
        secret = "123456:fake-secret-for-test"
        result = self.manager.set("TG_BOT_TOKEN", secret)
        self.assertEqual(result["value"], "configured")
        self.assertNotIn(secret, str(result))
        self.assertEqual(
            self.manager.status()["TG_BOT_TOKEN"], "configured"
        )

    def test_nested_config_backup_and_rollback(self) -> None:
        path = self.root / "config" / ".env.oi"
        first = "123456:first-fake-token"
        second = "123456:second-fake-token"
        self.manager.set("TG_BOT_TOKEN", first)
        self.manager.set("TG_BOT_TOKEN", second)

        backups = self.manager.backups("oi")
        self.assertEqual(len(backups), 1)
        self.manager.rollback("oi", str(backups[0]["version"]))

        self.assertIn(
            f"TG_BOT_TOKEN={first}",
            path.read_text(encoding="utf-8"),
        )

    def test_tty_input_uses_visible_input(self) -> None:
        with patch("sys.stdin.isatty", return_value=True), patch(
            "builtins.input", return_value="visible-secret"
        ) as visible:
            self.assertEqual(
                _read_value("TG_BOT_TOKEN"), "visible-secret"
            )
        visible.assert_called_once_with("请输入 TG_BOT_TOKEN: ")

    def test_non_tty_input_reads_stdin_without_argv(self) -> None:
        fake = StringIO("piped-value\n")
        with patch("sys.stdin", fake):
            self.assertEqual(_read_value("TG_CHAT_ID"), "piped-value")
        with self.assertRaises(SystemExit):
            build_parser().parse_args(
                ["set", "TG_CHAT_ID", "must-not-be-an-argv-value"]
            )

    def test_dry_run_profile_is_atomic_and_safe(self) -> None:
        path = self.root / "config" / ".env.oi"
        path.write_text(
            "MAIN_BOT_DELIVERY_MODE=real\n"
            "MAIN_BOT_REAL_SEND=true\n"
            "MAIN_BOT_REAL_SEND_ACK=发送真实主BOT提醒\n"
            "TG_BOT_TOKEN=123456:fake-token\n"
            "TG_CHAT_ID=-100123456\n",
            encoding="utf-8",
        )
        result = self.manager.main_bot_delivery("dry-run")
        values = _env_values(path)
        self.assertEqual(values["MAIN_BOT_DELIVERY_MODE"], "dry_run")
        self.assertEqual(values["MAIN_BOT_REAL_SEND"], "false")
        self.assertEqual(values["MAIN_BOT_REAL_SEND_ACK"], "")
        self.assertEqual(result["configuration"]["MAIN_BOT_REAL_SEND_ACK"], "not_configured")
        self.assertTrue(result["backup_created"])

    def test_real_profile_fails_closed_without_telegram(self) -> None:
        with self.assertRaisesRegex(
            ConfigManagerError, "main_bot_real_send_gate_blocked"
        ):
            self.manager.main_bot_delivery("real")
        self.assertFalse((self.root / "config" / ".env.oi").exists())

    def test_real_profile_requires_complete_gate(self) -> None:
        path = self.root / "config" / ".env.oi"
        path.write_text(
            "TG_BOT_TOKEN=123456:fake-token\n"
            "TG_CHAT_ID=-100123456\n",
            encoding="utf-8",
        )
        result = self.manager.main_bot_delivery("real")
        values = _env_values(path)
        self.assertEqual(values["MAIN_BOT_DELIVERY_MODE"], "real")
        self.assertEqual(values["MAIN_BOT_REAL_SEND"], "true")
        self.assertEqual(
            values["MAIN_BOT_REAL_SEND_ACK"], "发送真实主BOT提醒"
        )
        self.assertEqual(
            result["configuration"]["MAIN_BOT_REAL_SEND_ACK"],
            "configured",
        )

    def test_invalid_ack_and_boolean_are_rejected(self) -> None:
        with self.assertRaises(ConfigManagerError):
            self.manager.set("MAIN_BOT_REAL_SEND_ACK", "almost")
        with self.assertRaises(ConfigManagerError):
            self.manager.set("MAIN_BOT_REAL_SEND", "yes")

    def test_retired_launch_algorithm_and_ai_keys_are_rejected(self) -> None:
        for key in (
            "LAUNCH_FUSION_ENABLE",
            "LAUNCH_DIRECTIONAL_ENABLE",
            "LAUNCH_AI_INTERPRETER_ENABLE",
            "LAUNCH_SAME_STAGE_MIN_INTERVAL_SEC",
            "AI_API_KEY",
        ):
            with self.subTest(key=key):
                with self.assertRaises(ConfigManagerError):
                    self.manager.set(key, "true")

    @unittest.skipIf(os.name == "nt", "POSIX permission bits required")
    def test_environment_file_permission_is_600(self) -> None:
        self.manager.set("TG_BOT_TOKEN", "123456:fake-secret")
        mode = (self.root / "config" / ".env.oi").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


class MainOnlyMenuTests(unittest.TestCase):
    def test_menu_contains_only_market_radar_surfaces(self) -> None:
        text = MENU.read_text(encoding="utf-8")
        for expected in (
            "五雷达状态",
            "radar-status",
            "MAIN_BOT_DELIVERY_MODE",
            "PULSE_RADAR_ENABLE",
            "脉冲雷达",
            "CONSOLIDATION_DAILY_PRODUCT_ENABLE",
            "1H箱体临界预警",
            "1H临界预警影子模式",
            "日线盘整影子模式",
            "日线盘整日报",
            "日线边界事件",
        ):
            self.assertIn(expected, text)
        for removed in (
            "onchain_main.py",
            "paopao-oar-watch",
            "链上活动雷达",
            "OAR_AI_ENABLE",
            "ONCHAIN_BASE_HTTP_RPC_URL",
            "LAUNCH_ALERT_ENABLE",
            "LAUNCH_DIRECTIONAL_ENABLE",
            "LAUNCH_AI_INTERPRETER_ENABLE",
            "设置 AI API Key",
            "启动预警",
        ):
            self.assertNotIn(removed, text)

    def test_no_hidden_input_primitive(self) -> None:
        text = MENU.read_text(encoding="utf-8")
        self.assertNotIn("read -s", text)
        self.assertNotIn("stty -echo", text)

    def test_menu_uses_plain_chinese_for_common_runtime_states(self) -> None:
        text = MENU.read_text(encoding="utf-8")

        for expected in (
            "主 BOT 自动诊断",
            "市场数据服务最近日志",
            "服务启动配置内容",
            "运行中",
            "安全演练（不发送）",
        ):
            self.assertIn(expected, text)
        self.assertIn("stable-check --no-save", text)
        self.assertNotIn("stable-check --json --no-save", text)


if __name__ == "__main__":
    unittest.main()
