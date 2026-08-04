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

from paopao_radar.onchain_flow.config import OnchainSettings

from scripts.paopao_config import (
    ConfigManager,
    ConfigManagerError,
    _read_value,
    build_parser,
    main,
)


ROOT = Path(__file__).resolve().parents[1]
MENU = ROOT / "scripts" / "paopao_menu.sh"
INSTALL = ROOT / "scripts" / "install_server.sh"
UPDATE = ROOT / "scripts" / "update_server.sh"
SHORTCUTS = ROOT / "scripts" / "install_shortcuts.sh"
OAR_RUNNER = ROOT / "scripts" / "run_oar_watch.sh"
BASH = Path(r"C:\Program Files\Git\bin\bash.exe")


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

    def test_tty_value_uses_visible_input_and_redacts_saved_result(
        self,
    ) -> None:
        secret = "fake-visible-ai-key"
        with patch("sys.stdin.isatty", return_value=True):
            with patch("builtins.input", return_value=secret) as visible:
                self.assertEqual(_read_value("OAR_AI_API_KEY"), secret)
        visible.assert_called_once_with("请输入 OAR_AI_API_KEY: ")
        result = self.manager.set("OAR_AI_API_KEY", secret)
        self.assertEqual(result["value"], "configured")
        self.assertNotIn(secret, json.dumps(result, ensure_ascii=False))

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

    def test_shared_telegram_identity_wins_over_stale_onchain_copy(
        self,
    ) -> None:
        (self.root / ".env.oi").write_text(
            "TG_BOT_TOKEN=123456:shared-token\n"
            "TG_CHAT_ID=-1001234567890\n",
            encoding="utf-8",
        )
        (self.root / ".env.onchain").write_text(
            "TG_BOT_TOKEN=999999:stale-token\n"
            "TG_CHAT_ID=-1009999999999\n",
            encoding="utf-8",
        )
        values = self.manager._effective_values()
        self.assertEqual(values["TG_BOT_TOKEN"], "123456:shared-token")
        self.assertEqual(values["TG_CHAT_ID"], "-1001234567890")

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

    def test_p7_single_transfer_defaults_are_disabled_and_bounded(self) -> None:
        settings = OnchainSettings.load(base_dir=self.root, environ={})
        self.assertFalse(settings.oar_single_transfer_risk_enable)
        self.assertEqual(settings.oar_single_transfer_min_score, 60)
        self.assertEqual(settings.oar_single_transfer_critical_score, 80)
        for key, value in (
            ("OAR_SINGLE_TRANSFER_MIN_SCORE", "60"),
            ("OAR_SINGLE_TRANSFER_CRITICAL_SCORE", "80"),
            ("OAR_SENDER_EXIT_HIGH", "0.80"),
            ("OAR_SENDER_EXIT_NEAR_FULL", "0.95"),
            ("OAR_SENDER_EXIT_FULL", "0.99"),
            ("OAR_SUPPLY_SHARE_WATCH", "0.001"),
            ("OAR_SUPPLY_SHARE_HIGH", "0.005"),
            ("OAR_SINGLE_TRANSFER_MAX_BALANCE_CALLS", "20"),
            ("OAR_SINGLE_TRANSFER_MAX_SUPPLY_CALLS", "5"),
            ("OAR_SINGLE_TRANSFER_SNAPSHOT_TTL_SEC", "300"),
        ):
            with self.subTest(key=key):
                self.assertEqual(self.manager.set(key, value)["status"], "ok")

    def test_p7_threshold_combination_failure_rolls_back(self) -> None:
        path = self.root / ".env.onchain"
        path.write_text(
            "OAR_SENDER_EXIT_HIGH=0.80\n"
            "OAR_SENDER_EXIT_NEAR_FULL=0.95\n"
            "OAR_SENDER_EXIT_FULL=0.99\n",
            encoding="utf-8",
        )
        before = path.read_bytes()
        with self.assertRaisesRegex(
            ConfigManagerError, "strictly increasing"
        ):
            self.manager.set("OAR_SENDER_EXIT_HIGH", "0.96")
        self.assertEqual(path.read_bytes(), before)

    def test_bsc_configuration_is_optional_bounded_and_redacted(self) -> None:
        self.assertEqual(
            self.manager.set("OAR_BSC_HTTP_RPC_URL", "https://bsc.invalid/key")[
                "value"
            ],
            "configured",
        )
        self.manager.set("OAR_BSC_CONFIRMATION_DEPTH", "15")
        self.manager.set("OAR_BSC_REORG_LOOKBACK_BLOCKS", "64")
        self.manager.set("OAR_BSC_ENABLE", "true")
        serialized = json.dumps(self.manager.status())
        self.assertNotIn("bsc.invalid", serialized)
        self.assertNotIn("/key", serialized)

        empty_root = self.root / "empty"
        empty_root.mkdir()
        manager = ConfigManager(empty_root)
        with self.assertRaisesRegex(ConfigManagerError, "enabled BSC"):
            manager.set("OAR_BSC_ENABLE", "true")
        self.assertFalse((empty_root / ".env.onchain").exists())

    def test_additional_evm_chains_are_optional_and_fail_closed(self) -> None:
        pairs = (
            ("OAR_ETHEREUM_ENABLE", "OAR_ETHEREUM_HTTP_RPC_URL"),
            ("OAR_ARBITRUM_ENABLE", "OAR_ARBITRUM_HTTP_RPC_URL"),
            ("OAR_OPTIMISM_ENABLE", "OAR_OPTIMISM_HTTP_RPC_URL"),
            ("OAR_POLYGON_ENABLE", "OAR_POLYGON_HTTP_RPC_URL"),
            ("OAR_AVALANCHE_ENABLE", "OAR_AVALANCHE_HTTP_RPC_URL"),
        )
        for enable_key, rpc_key in pairs:
            with self.subTest(enable_key=enable_key):
                empty_root = self.root / enable_key.lower()
                empty_root.mkdir()
                manager = ConfigManager(empty_root)
                with self.assertRaisesRegex(
                    ConfigManagerError,
                    "enabled EVM chain requires",
                ):
                    manager.set(enable_key, "true")
                self.assertFalse((empty_root / ".env.onchain").exists())

                endpoint = f"https://{rpc_key.lower()}.invalid/private"
                result = manager.set(rpc_key, endpoint)
                self.assertEqual(result["value"], "configured")
                manager.set(enable_key, "true")
                serialized = json.dumps(manager.status())
                self.assertNotIn(endpoint, serialized)
                self.assertNotIn("/private", serialized)

    def test_arkham_configuration_is_allowlisted_bounded_and_redacted(
        self,
    ) -> None:
        key = "fake-visible-arkham-key"
        result = self.manager.set("ARKHAM_API_KEY", key)
        self.assertEqual(result["value"], "configured")
        self.assertNotIn(key, json.dumps(result))
        self.manager.set("ARKHAM_API_BASE_URL", "https://api.arkm.com")
        self.manager.set("ARKHAM_API_TIMEOUT_SEC", "15")
        self.manager.set("ARKHAM_API_MAX_RETRIES", "1")
        self.manager.set("OAR_LABEL_CANDIDATE_MAX_ADDRESSES", "50")
        status = json.dumps(self.manager.status())
        self.assertNotIn(key, status)
        self.assertIn("configured", status)
        for key_name, value in (
            ("ARKHAM_API_BASE_URL", "http://api.arkm.com"),
            ("ARKHAM_API_BASE_URL", "https://u:p@api.arkm.com"),
            ("ARKHAM_API_BASE_URL", "https://api.arkm.com?q=1"),
            ("ARKHAM_API_TIMEOUT_SEC", "0"),
            ("ARKHAM_API_TIMEOUT_SEC", "61"),
            ("ARKHAM_API_MAX_RETRIES", "3"),
            ("OAR_LABEL_CANDIDATE_MAX_ADDRESSES", "101"),
        ):
            with self.subTest(key=key_name, value=value):
                with self.assertRaises(ConfigManagerError):
                    self.manager.set(key_name, value)

    def test_arkham_key_tty_input_remains_visible(self) -> None:
        key = "fake-visible-arkham-key"
        with patch("sys.stdin.isatty", return_value=True):
            with patch("builtins.input", return_value=key) as visible:
                self.assertEqual(_read_value("ARKHAM_API_KEY"), key)
        visible.assert_called_once_with("请输入 ARKHAM_API_KEY: ")

    def test_watch_delivery_defaults_and_values_are_strict(self) -> None:
        validation = self.manager.validate()
        checks = validation["checks"]
        self.assertEqual(checks["oar_watch_delivery_mode"], "observe")
        self.assertFalse(checks["oar_watch_with_ai"])
        self.assertFalse(checks["oar_watch_real_send"])
        for key, value in (
            ("OAR_WATCH_DELIVERY_MODE", "unknown"),
            ("OAR_WATCH_WITH_AI", "yes"),
            ("OAR_WATCH_REAL_SEND_ACK", "almost"),
        ):
            with self.subTest(key=key):
                with self.assertRaises(ConfigManagerError):
                    self.manager.set(key, value)

    def test_observe_profile_closes_real_and_ai_gates_atomically(self) -> None:
        path = self.root / ".env.onchain"
        path.write_text(
            "OAR_WATCH_DELIVERY_MODE=dry_run\n"
            "OAR_WATCH_WITH_AI=true\n"
            "ONCHAIN_REAL_SEND=true\n"
            "OAR_WATCH_REAL_SEND_ACK=发送真实链上提醒\n",
            encoding="utf-8",
        )
        result = self.manager.watch_delivery("observe")
        values = _env_values(path)
        self.assertEqual(values["OAR_WATCH_DELIVERY_MODE"], "observe")
        self.assertEqual(values["OAR_WATCH_WITH_AI"], "false")
        self.assertEqual(values["ONCHAIN_REAL_SEND"], "false")
        self.assertEqual(values["OAR_WATCH_REAL_SEND_ACK"], "")
        self.assertFalse(result["configuration"]["OAR_WATCH_WITH_AI"])
        self.assertEqual(
            result["configuration"]["OAR_WATCH_REAL_SEND_ACK"],
            "not_configured",
        )

    def test_dry_run_does_not_enable_real_send_or_change_ai_choice(
        self,
    ) -> None:
        self.manager.watch_delivery("enable-ai")
        self.manager.watch_delivery("dry-run")
        values = _env_values(self.root / ".env.onchain")
        self.assertEqual(values["OAR_WATCH_DELIVERY_MODE"], "dry_run")
        self.assertEqual(values["OAR_WATCH_WITH_AI"], "true")
        self.assertEqual(values["ONCHAIN_REAL_SEND"], "false")
        self.assertEqual(values["OAR_WATCH_REAL_SEND_ACK"], "")

    def test_real_delivery_requires_complete_telegram_gate_and_rolls_back(
        self,
    ) -> None:
        path = self.root / ".env.onchain"
        path.write_text(
            "# exact\nOAR_WATCH_DELIVERY_MODE=observe\n",
            encoding="utf-8",
        )
        original = path.read_bytes()
        with self.assertRaisesRegex(
            ConfigManagerError,
            "real_send_gate_blocked",
        ):
            self.manager.watch_delivery("real")
        self.assertEqual(path.read_bytes(), original)

    def test_real_delivery_sets_only_guard_values_when_complete(self) -> None:
        (self.root / ".env.oi").write_text(
            "TG_BOT_TOKEN=123456:safe_TOKEN-1\n"
            "TG_CHAT_ID=-100123\n",
            encoding="utf-8",
        )
        (self.root / ".env.onchain").write_text(
            "TG_ONCHAIN_FLOW_TOPIC_ID=42\n"
            "OAR_WATCH_WITH_AI=false\n"
            "OAR_AI_ENABLE=false\n",
            encoding="utf-8",
        )
        result = self.manager.watch_delivery("real")
        values = _env_values(self.root / ".env.onchain")
        self.assertEqual(values["OAR_WATCH_DELIVERY_MODE"], "real")
        self.assertEqual(values["ONCHAIN_REAL_SEND"], "true")
        self.assertEqual(
            values["OAR_WATCH_REAL_SEND_ACK"],
            "发送真实链上提醒",
        )
        self.assertEqual(values["OAR_WATCH_WITH_AI"], "false")
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("发送真实链上提醒", serialized)

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
            "OAR_AI_TIMEOUT_SEC": "60",
            "OAR_AI_MAX_RETRIES": "0",
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

    def test_ai_timeout_and_retry_values_are_allowlisted_and_bounded(
        self,
    ) -> None:
        self.assertEqual(
            self.manager.set("OAR_AI_TIMEOUT_SEC", "60")["status"],
            "ok",
        )
        self.assertEqual(
            self.manager.set("OAR_AI_MAX_RETRIES", "0")["status"],
            "ok",
        )
        for key, value in (
            ("OAR_AI_TIMEOUT_SEC", "4"),
            ("OAR_AI_TIMEOUT_SEC", "181"),
            ("OAR_AI_MAX_RETRIES", "-1"),
            ("OAR_AI_MAX_RETRIES", "3"),
        ):
            with self.subTest(key=key, value=value):
                with self.assertRaises(ConfigManagerError):
                    self.manager.set(key, value)

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

    def test_watch_baseline_settings_are_allowlisted_and_atomic(self) -> None:
        self.assertEqual(
            self.manager.set("OAR_WATCH_BASELINE_MIN_SAMPLES", "8")[
                "status"
            ],
            "ok",
        )
        self.assertEqual(
            self.manager.set("OAR_WATCH_BASELINE_MAX_SAMPLES", "64")[
                "status"
            ],
            "ok",
        )
        self.assertEqual(
            self.manager.set(
                "OAR_WATCH_BASELINE_MAD_MULTIPLIER", "3.5"
            )["status"],
            "ok",
        )
        before = (self.root / ".env.onchain").read_text(encoding="utf-8")
        with self.assertRaises(ConfigManagerError):
            self.manager.set("OAR_WATCH_BASELINE_MIN_SAMPLES", "80")
        self.assertEqual(
            (self.root / ".env.onchain").read_text(encoding="utf-8"),
            before,
        )

    def test_controlled_alert_gate_defaults_off_and_is_atomic(self) -> None:
        self.assertEqual(
            self.manager.status()["OAR_WATCH_CONTROLLED_ALERT_ENABLE"],
            False,
        )
        result = self.manager.set(
            "OAR_WATCH_CONTROLLED_ALERT_ENABLE", "true"
        )
        self.assertEqual(result["status"], "ok")
        self.assertTrue(
            result["validation"]["checks"][
                "oar_watch_controlled_alert_enable"
            ]
        )
        with self.assertRaises(ConfigManagerError):
            self.manager.set(
                "OAR_WATCH_CONTROLLED_ALERT_ENABLE", "enabled"
            )

    def test_watch_rolling_coverage_cadence_is_atomic(self) -> None:
        self.assertEqual(
            self.manager.status()["OAR_WATCH_SCAN_INTERVAL_SEC"], "900"
        )
        self.assertEqual(
            self.manager.status()["OAR_WATCH_QUERY_WINDOW"], "4h"
        )
        self.assertEqual(
            self.manager.set("OAR_WATCH_SCAN_INTERVAL_SEC", "840")[
                "status"
            ],
            "ok",
        )
        self.assertEqual(
            self.manager.set("OAR_WATCH_QUERY_WINDOW", "15m")["status"],
            "ok",
        )
        before = (self.root / ".env.onchain").read_text(encoding="utf-8")
        with self.assertRaisesRegex(
            ConfigManagerError, "oar_watch_rolling_coverage_inconsistent"
        ):
            self.manager.set("OAR_WATCH_SCAN_INTERVAL_SEC", "900")
        self.assertEqual(
            (self.root / ".env.onchain").read_text(encoding="utf-8"),
            before,
        )

    def test_watch_query_window_enum_is_enforced(self) -> None:
        with self.assertRaises(ConfigManagerError):
            self.manager.set("OAR_WATCH_QUERY_WINDOW", "30m")

    def test_watch_baseline_setting_ranges_are_enforced(self) -> None:
        for key, value in (
            ("OAR_WATCH_BASELINE_MIN_SAMPLES", "3"),
            ("OAR_WATCH_BASELINE_MAX_SAMPLES", "101"),
            ("OAR_WATCH_BASELINE_MAD_MULTIPLIER", "0.9"),
            ("OAR_WATCH_BASELINE_MAD_MULTIPLIER", "10.1"),
        ):
            with self.subTest(key=key, value=value):
                with self.assertRaises(ConfigManagerError):
                    self.manager.set(key, value)

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
            OAR_RUNNER,
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
            "radar-status)",
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
            'confirm_phrase "批准CEX标签"',
        ):
            self.assertIn(phrase, text)

    def test_menu_exposes_complete_deepseek_and_registry_choices(self) -> None:
        text = MENU.read_text(encoding="utf-8")
        for expected in (
            "profile deepseek-v4-pro",
            "设置 AI Base URL",
            "设置 Max Tokens",
            "设置 AI Timeout",
            "设置 AI Max Retries",
            "config_set OAR_AI_TIMEOUT_SEC",
            "config_set OAR_AI_MAX_RETRIES",
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

    def test_menu_exposes_full_market_flow_candidate_list(self) -> None:
        text = MENU.read_text(encoding="utf-8")
        self.assertIn("五因子资金流雷达候选清单", text)
        self.assertIn("查看全市场完整候选清单", text)
        self.assertIn("run_flow_candidates --all", text)
        self.assertIn("查看清单不会访问网络", text)

    def test_menu_exposes_local_four_radar_status(self) -> None:
        text = MENU.read_text(encoding="utf-8")
        self.assertIn("四个市场雷达运行与推送状态", text)
        self.assertIn("run_main radar-status", text)
        self.assertIn(
            "paopao radar-status   查看四个市场雷达运行与投递状态",
            text,
        )

    def test_menu_exposes_bounded_base_rpc_range_setting(self) -> None:
        text = MENU.read_text(encoding="utf-8")
        self.assertIn("设置 Base RPC 最大区块范围（高级）", text)
        self.assertIn(
            "config_set ONCHAIN_RPC_MAX_BLOCK_RANGE",
            text,
        )

    def test_menu_exposes_reviewed_arkham_candidate_workflow(self) -> None:
        text = MENU.read_text(encoding="utf-8")
        for expected in (
            "设置 Arkham API Key",
            "config_set ARKHAM_API_KEY",
            "CEX 标签候选",
            "label-candidates provider-check --allow-network",
            "label-candidates discover",
            "label-candidates list",
            "--status pending --limit 100",
            "label-candidates approve",
            "label-candidates reject",
            'confirm_phrase "批准CEX标签"',
        ):
            self.assertIn(expected, text)
        self.assertNotRegex(text, r"read\s+[^\n]*-s")

    def test_menu_exposes_guarded_watch_delivery_modes(self) -> None:
        text = MENU.read_text(encoding="utf-8")
        for expected in (
            "Watch 通知模式",
            "watch-delivery observe",
            "watch-delivery dry-run",
            "watch-delivery real",
            "watch-delivery enable-ai",
            "watch-delivery disable-ai",
            'confirm_phrase "启用链上DryRun"',
            'confirm_phrase "启用真实链上提醒"',
            'confirm_phrase "启用自动AI分析"',
            "受控自动预警门禁",
            "run_config enable OAR_WATCH_CONTROLLED_ALERT_ENABLE",
            "run_config disable OAR_WATCH_CONTROLLED_ALERT_ENABLE",
            'confirm_phrase "启用受控自动预警"',
        ):
            self.assertIn(expected, text)

    def test_menu_bootstraps_topic_from_shared_group(self) -> None:
        text = MENU.read_text(encoding="utf-8")
        self.assertIn("自动识别群并创建/修复链上话题", text)
        self.assertIn(
            "run_onchain telegram-topic bootstrap --allow-network",
            text,
        )
        self.assertIn("将直接复用主 BOT 的 Token 和群", text)
        self.assertIn("Bot/群配置来源: 主 BOT 共享 .env.oi", text)
        self.assertNotIn("config_set TG_ONCHAIN_FLOW_TOPIC_ID", text)
        self.assertNotIn("从消息链接绑定链上话题", text)

    def test_menu_exposes_guarded_topic_intro_publish(self) -> None:
        text = MENU.read_text(encoding="utf-8")
        self.assertIn("发送并置顶链上话题说明", text)
        self.assertIn(
            "run_onchain telegram-topic intro \\",
            text,
        )
        self.assertIn("--allow-network --send --confirm-real-send", text)
        self.assertIn(
            'confirm_phrase "发送并置顶链上话题说明"',
            text,
        )

    def test_production_inputs_never_disable_terminal_echo(self) -> None:
        config_text = (
            ROOT / "scripts" / "paopao_config.py"
        ).read_text(encoding="utf-8")
        menu_text = MENU.read_text(encoding="utf-8")
        production_text = "\n".join(
            path.read_text(encoding="utf-8")
            for directory in (ROOT / "scripts", ROOT / "paopao_radar")
            for path in directory.rglob("*")
            if path.is_file() and path.suffix in {".py", ".sh"}
        )
        self.assertNotIn("import getpass", config_text)
        self.assertNotIn("getpass.getpass", production_text)
        self.assertNotRegex(production_text, r"\bread\s+[^\n]*-s(?:\s|$)")
        self.assertNotIn("stty -echo", production_text)
        self.assertNotIn("termios", production_text)
        self.assertIn("return input(prompt)", config_text)

    def test_all_sensitive_menu_fields_use_the_visible_config_reader(
        self,
    ) -> None:
        text = MENU.read_text(encoding="utf-8")
        for key in (
            "TG_BOT_TOKEN",
            "TG_CHAT_ID",
            "COINGLASS_API_KEY",
            "COINALYZE_API_KEY",
            "ONCHAIN_BASE_HTTP_RPC_URL",
            "OAR_AI_API_KEY",
            "OAR_AI_BASE_URL",
            "ARKHAM_API_KEY",
            "ARKHAM_API_BASE_URL",
        ):
            self.assertIn(f"config_set {key}", text)
        self.assertNotIn("history -w", text)
        self.assertNotIn("history -a", text)

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
