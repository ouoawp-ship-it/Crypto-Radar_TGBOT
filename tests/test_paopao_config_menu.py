from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.paopao_config import (
    ConfigManager,
    ConfigManagerError,
    build_parser,
    main,
)


ROOT = Path(__file__).resolve().parents[1]
MENU = ROOT / "scripts" / "paopao_menu.sh"
INSTALL = ROOT / "scripts" / "install_server.sh"
UPDATE = ROOT / "scripts" / "update_server.sh"
SHORTCUTS = ROOT / "scripts" / "install_shortcuts.sh"
BASH = Path(r"C:\Program Files\Git\bin\bash.exe")


class ConfigManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manager = ConfigManager(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_non_allowlisted_key_is_rejected(self) -> None:
        with self.assertRaises(ConfigManagerError):
            self.manager.set("UNSAFE_UNKNOWN_KEY", "value")

    def test_unknown_fields_and_comments_are_preserved(self) -> None:
        path = self.root / ".env.onchain"
        path.write_text(
            "# keep this comment\nCUSTOM_KEEP=1\nOAR_AI_ENABLE=false\n",
            encoding="utf-8",
        )
        self.manager.set("OAR_AI_PROVIDER", "openai_compatible")
        text = path.read_text(encoding="utf-8")
        self.assertIn("# keep this comment", text)
        self.assertIn("CUSTOM_KEEP=1", text)
        self.assertIn("OAR_AI_PROVIDER=openai_compatible", text)

    def test_secret_is_read_from_stdin_and_not_returned(self) -> None:
        secret = "123456:abcdefghijklmnopqrstuvwxyz_ABC"
        output = StringIO()
        with patch("sys.stdin", StringIO(f"{secret}\n")):
            with redirect_stdout(output):
                code = main([
                    "--base-dir",
                    str(self.root),
                    "set",
                    "TG_BOT_TOKEN",
                ])
        self.assertEqual(code, 0)
        self.assertNotIn(secret, output.getvalue())
        self.assertIn(
            secret,
            (self.root / ".env.oi").read_text(encoding="utf-8"),
        )

    def test_secret_cannot_be_supplied_as_extra_argv(self) -> None:
        parser = build_parser()
        with self.assertRaises(SystemExit):
            parser.parse_args([
                "set",
                "TG_BOT_TOKEN",
                "must-not-appear-in-argv",
            ])

    def test_status_redacts_urls_ids_and_keys(self) -> None:
        (self.root / ".env.oi").write_text(
            "TG_BOT_TOKEN=token\nTG_CHAT_ID=-100123456\n",
            encoding="utf-8",
        )
        (self.root / ".env.onchain").write_text(
            "ONCHAIN_BASE_HTTP_RPC_URL=https://rpc.invalid/private\n"
            "TG_ONCHAIN_FLOW_TOPIC_ID=999999\n"
            "OAR_AI_API_KEY=ai-secret\n",
            encoding="utf-8",
        )
        serialized = json.dumps(self.manager.status())
        for secret in (
            "token",
            "-100123456",
            "rpc.invalid",
            "999999",
            "ai-secret",
        ):
            self.assertNotIn(secret, serialized)

    def test_modification_creates_backup_and_chmods_file(self) -> None:
        path = self.root / ".env.oi"
        path.write_text("TG_CHAT_ID=-1001\n", encoding="utf-8")
        result = self.manager.set("TG_CHAT_ID", "-1002")
        self.assertTrue(result["backup_created"])
        self.assertTrue(list(self.root.glob(".env.oi.bak.*")))
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_post_validation_failure_restores_exact_previous_file(self) -> None:
        path = self.root / ".env.onchain"
        path.write_text(
            "OAR_AI_ENABLE=true\n"
            "OAR_AI_PROVIDER=deepseek\n"
            "OAR_AI_BASE_URL=https://ai.invalid/v1\n"
            "OAR_AI_API_KEY=secret\n"
            "OAR_AI_MODEL=deepseek-v4-pro\n",
            encoding="utf-8",
        )
        original = path.read_bytes()
        with self.assertRaises(ConfigManagerError):
            self.manager.set("OAR_AI_MODEL", "deepseek-chat")
        self.assertEqual(path.read_bytes(), original)

    def test_boolean_and_integer_validation_is_strict(self) -> None:
        for key, value in (
            ("OAR_AI_ENABLE", "maybe"),
            ("OAR_AUTOMATION_ENABLE", "1"),
            ("OAR_AI_MAX_TOKENS", "511"),
            ("OAR_AI_MAX_TOKENS", "32769"),
            ("ONCHAIN_RPC_MAX_BLOCK_RANGE", "0"),
            ("ONCHAIN_RPC_MAX_BLOCK_RANGE", "10001"),
            ("ONCHAIN_RPC_MAX_BLOCK_RANGE", "10.5"),
        ):
            with self.subTest(key=key, value=value):
                with self.assertRaises(ConfigManagerError):
                    self.manager.set(key, value)

    def test_rpc_max_block_range_is_allowlisted_and_bounded(self) -> None:
        result = self.manager.set("ONCHAIN_RPC_MAX_BLOCK_RANGE", "10")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["value"], "10")
        self.assertIn(
            "ONCHAIN_RPC_MAX_BLOCK_RANGE=10",
            (self.root / ".env.onchain").read_text(encoding="utf-8"),
        )

    def test_endpoint_with_credentials_is_rejected(self) -> None:
        with self.assertRaises(ConfigManagerError):
            self.manager.set(
                "OAR_AI_BASE_URL",
                "https://user:pass@ai.invalid/v1",
            )

    def test_duplicate_key_is_rejected_without_rewrite(self) -> None:
        path = self.root / ".env.oi"
        path.write_text(
            "TG_CHAT_ID=1001\nTG_CHAT_ID=1002\n",
            encoding="utf-8",
        )
        original = path.read_bytes()
        with self.assertRaises(ConfigManagerError):
            self.manager.set("TG_CHAT_ID", "1003")
        self.assertEqual(path.read_bytes(), original)

    def test_configuration_rollback_restores_allowlisted_backup(self) -> None:
        path = self.root / ".env.oi"
        path.write_text("TG_CHAT_ID=-1001\n", encoding="utf-8")
        self.manager.set("TG_CHAT_ID", "-1002")
        version = self.manager.backups("oi")[0]["version"]
        result = self.manager.rollback("oi", str(version))
        self.assertEqual(result["status"], "ok")
        self.assertIn("TG_CHAT_ID=-1001", path.read_text(encoding="utf-8"))

    def test_deepseek_profile_is_atomic_and_does_not_enable_or_set_key(
        self,
    ) -> None:
        path = self.root / ".env.onchain"
        path.write_text(
            "# keep\nOAR_AI_API_KEY=private-key\nOAR_AI_ENABLE=false\n",
            encoding="utf-8",
        )
        result = self.manager.profile("deepseek-v4-pro")
        values = dict(
            line.split("=", 1)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#")
        )
        expected = {
            "OAR_AI_PROVIDER": "deepseek",
            "OAR_AI_BASE_URL": "https://api.deepseek.com",
            "OAR_AI_MODEL": "deepseek-v4-pro",
            "OAR_AI_THINKING_MODE": "enabled",
            "OAR_AI_REASONING_EFFORT": "high",
            "OAR_AI_MAX_TOKENS": "8192",
        }
        self.assertEqual(
            {key: values[key] for key in expected},
            expected,
        )
        self.assertEqual(values["OAR_AI_API_KEY"], "private-key")
        self.assertEqual(values["OAR_AI_ENABLE"], "false")
        self.assertEqual(
            result["configuration"]["OAR_AI_API_KEY"],
            "configured",
        )
        self.assertFalse(result["configuration"]["OAR_AI_ENABLE"])
        self.assertNotIn("private-key", json.dumps(result))

    def test_profile_validation_failure_restores_exact_file(self) -> None:
        onchain = self.root / ".env.onchain"
        onchain.write_text(
            "# exact original\nOAR_AI_ENABLE=false\n",
            encoding="utf-8",
        )
        (self.root / ".env.oi").write_text(
            "TG_CHAT_ID=not-an-integer\n",
            encoding="utf-8",
        )
        original = onchain.read_bytes()
        with self.assertRaises(ConfigManagerError):
            self.manager.profile("deepseek-v4-pro")
        self.assertEqual(onchain.read_bytes(), original)

    def test_fresh_deepseek_configuration_can_be_completed_offline(
        self,
    ) -> None:
        self.manager.profile("deepseek-v4-pro")
        self.manager.set("OAR_AI_API_KEY", "private-key-from-stdin")
        enabled = self.manager.set("OAR_AI_ENABLE", "true")
        validated = self.manager.validate(".env.onchain")
        self.assertTrue(enabled["value"])
        self.assertEqual(validated["status"], "ok")
        self.assertNotIn(
            "private-key-from-stdin",
            json.dumps(enabled, ensure_ascii=False),
        )

    def test_telegram_business_formats_are_validated_locally(self) -> None:
        for key, value in (
            ("TG_BOT_TOKEN", "not-a-bot-token"),
            ("TG_CHAT_ID", "0"),
            ("TG_CHAT_ID", "@channel"),
            ("TG_ONCHAIN_FLOW_TOPIC_ID", "-1"),
            ("TG_ONCHAIN_FLOW_TOPIC_ID", "topic"),
        ):
            with self.subTest(key=key, value=value):
                with self.assertRaises(ConfigManagerError):
                    self.manager.set(key, value)
        self.assertEqual(
            self.manager.set("TG_BOT_TOKEN", "123456:safe_TOKEN-1")[
                "status"
            ],
            "ok",
        )
        self.assertEqual(
            self.manager.set("TG_CHAT_ID", "-100123")["status"],
            "ok",
        )
        self.assertEqual(
            self.manager.set("TG_ONCHAIN_FLOW_TOPIC_ID", "42")["status"],
            "ok",
        )

    def test_cex_labels_path_is_safe_and_may_be_absent(self) -> None:
        result = self.manager.set(
            "ONCHAIN_CEX_LABELS_FILE",
            "config/onchain/private.csv",
        )
        self.assertEqual(
            result["validation"]["checks"]["cex_labels_file"],
            "not_present",
        )
        for value in (
            "../private.csv",
            str((self.root.parent / "private.csv").resolve()),
        ):
            with self.subTest(value=value):
                with self.assertRaises(ConfigManagerError):
                    self.manager.set("ONCHAIN_CEX_LABELS_FILE", value)

    @unittest.skipIf(os.name == "nt", "symlink creation is restricted")
    def test_cex_labels_symlink_cannot_escape_project(self) -> None:
        outside = self.root.parent / f"{self.root.name}-outside-labels.csv"
        self.addCleanup(outside.unlink, missing_ok=True)
        outside.write_text("header\n", encoding="utf-8")
        link = self.root / "labels.csv"
        link.symlink_to(outside)
        with self.assertRaises(ConfigManagerError):
            self.manager.set("ONCHAIN_CEX_LABELS_FILE", "labels.csv")

    def test_backups_are_bounded_without_deleting_unrelated_files(self) -> None:
        path = self.root / ".env.oi"
        path.write_text("TG_CHAT_ID=1\n", encoding="utf-8")
        unrelated = self.root / ".env.oi.backup.keep"
        unrelated.write_text("keep", encoding="utf-8")
        for value in range(2, 40):
            self.manager.set("TG_CHAT_ID", str(value))
        self.assertLessEqual(
            len(list(self.root.glob(".env.oi.bak.*"))),
            30,
        )
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "keep")


class ChineseMenuTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not BASH.exists() and shutil.which("bash") is None:
            raise unittest.SkipTest("bash is unavailable")

    def test_shell_scripts_pass_syntax_check(self) -> None:
        for path in (
            MENU,
            INSTALL,
            UPDATE,
            SHORTCUTS,
        ):
            with self.subTest(path=path.name):
                result = subprocess.run(
                    [
                        str(BASH if BASH.exists() else shutil.which("bash")),
                        "-n",
                        path.as_posix(),
                    ],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_non_tty_no_argument_shows_help_without_menu_actions(self) -> None:
        env = {
            **os.environ,
            "PAOPAO_APP_DIR": str(ROOT),
            "PAOPAO_PYTHON_BIN": sys.executable,
        }
        result = subprocess.run(
            [
                str(BASH if BASH.exists() else shutil.which("bash")),
                MENU.as_posix(),
            ],
            cwd=ROOT,
            env=env,
            input="",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("非 TTY 环境不会打开全屏菜单", result.stdout)
        self.assertNotIn("请选择：", result.stdout)

    def test_existing_direct_commands_remain_registered(self) -> None:
        text = MENU.read_text(encoding="utf-8")
        for command in (
            "status)",
            "logs)",
            "restart)",
            "doctor)",
            "readiness)",
            "stable-check)",
            "providers|provider-check)",
            "backup|database-backup)",
            "telegram-test)",
            "cleanup)",
            "check-update|check)",
            "update)",
            "version)",
        ):
            self.assertIn(command, text)

    def test_tty_entry_and_all_ten_main_sections_exist(self) -> None:
        text = MENU.read_text(encoding="utf-8")
        self.assertIn("[ -t 0 ] && [ -t 1 ]", text)
        self.assertIn("interactive_menu", text)
        for title in (
            "总览与健康检查",
            "服务管理",
            "检查更新与版本",
            "API、Token 与密钥",
            "AI 模型与提示词",
            "链上活动雷达",
            "Telegram 设置与测试",
            "数据库、备份与清理",
            "日志与故障诊断",
            "高级运维",
        ):
            self.assertIn(title, text)

    def test_menu_opening_is_local_only(self) -> None:
        text = MENU.read_text(encoding="utf-8")
        start = text.index("print_local_overview()")
        end = text.index("system_resources()", start)
        overview = text[start:end]
        for forbidden in (
            "git fetch",
            "--allow-network",
            "telegram-test",
            "provider-check",
            "stable-check",
        ):
            self.assertNotIn(forbidden, overview)

    def test_risky_operations_require_full_chinese_phrases(self) -> None:
        text = MENU.read_text(encoding="utf-8")
        for phrase in (
            'confirm_phrase "停止主BOT"',
            'confirm_phrase "重启主服务"',
            'confirm_phrase "执行安全更新"',
            'confirm_phrase "发送真实测试"',
            'confirm_phrase "恢复数据库"',
            'confirm_phrase "回滚配置"',
            'confirm_phrase "清理AI缓存"',
            'confirm_phrase "恢复提示词"',
            'confirm_phrase "禁用Registry"',
            'confirm_phrase "接受Symbol不一致"',
        ):
            self.assertIn(phrase, text)

    def test_menu_exposes_complete_deepseek_and_registry_choices(self) -> None:
        text = MENU.read_text(encoding="utf-8")
        for expected in (
            "profile deepseek-v4-pro",
            "设置 AI Base URL",
            "设置 Max Tokens",
            "ai-cache status",
            "ai-cache clear-results",
            "验证并设为 Primary",
            "仅验证为 Secondary",
            "--set-primary",
            "--accept-symbol-mismatch",
        ):
            self.assertIn(expected, text)
        self.assertNotIn(
            'rm -f "${APP_DIR}/data/onchain/oar_ai_cache.json"',
            text,
        )

    def test_menu_exposes_bounded_base_rpc_range_setting(self) -> None:
        text = MENU.read_text(encoding="utf-8")
        self.assertIn("设置 Base RPC 最大区块范围（高级）", text)
        self.assertIn(
            "config_set ONCHAIN_RPC_MAX_BLOCK_RANGE",
            text,
        )

    def test_menu_uses_config_manager_instead_of_sed(self) -> None:
        text = MENU.read_text(encoding="utf-8")
        self.assertIn("scripts/paopao_config.py", text)
        self.assertNotIn("sed -i", text)

    def test_installer_adds_pp_without_touching_bashrc(self) -> None:
        install = INSTALL.read_text(encoding="utf-8")
        update = UPDATE.read_text(encoding="utf-8")
        shortcuts = SHORTCUTS.read_text(encoding="utf-8")
        self.assertIn("scripts/install_shortcuts.sh", install)
        self.assertIn("/usr/local/bin", shortcuts)
        self.assertIn('TARGET_DIR}/paopao', shortcuts)
        self.assertIn('TARGET_DIR}/pp', shortcuts)
        self.assertIn("ln -sfn", shortcuts)
        for text in (install, update, shortcuts):
            self.assertNotIn(".bashrc", text)
        self.assertNotIn("interactive_menu", shortcuts)

    def test_private_prompt_and_diagnostic_exports_are_git_ignored(self) -> None:
        ignored_paths = (
            "data/onchain/config/oar_ai_operator_prompt.txt",
            "reports/onchain/paopao-diagnostic-test.txt",
        )
        result = subprocess.run(
            ["git", "check-ignore", *ignored_paths],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(result.stdout.splitlines()), set(ignored_paths))


if __name__ == "__main__":
    unittest.main()
