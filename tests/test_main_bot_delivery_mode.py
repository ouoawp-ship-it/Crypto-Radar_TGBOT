from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from paopao_radar.config import Settings
from paopao_radar.storage import JsonStore
from paopao_radar.telegram import TelegramGateway
from scripts.paopao_config import ConfigManager, ConfigManagerError


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "run_main_bot.sh"
INSTALLER = ROOT / "scripts" / "install_main_bot_service.sh"
MENU = ROOT / "scripts" / "paopao_menu.sh"


def _env_values(path: Path) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line and not line.startswith("#") and "=" in line
    )


class MainBotConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manager = ConfigManager(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_missing_keys_default_to_safe_dry_run(self) -> None:
        checks = self.manager.validate()["checks"]
        status = self.manager.status()
        self.assertEqual(checks["main_bot_delivery_mode"], "dry_run")
        self.assertFalse(checks["main_bot_real_send"])
        self.assertEqual(
            checks["main_bot_real_send_ack"],
            "not_configured",
        )
        self.assertEqual(
            status["MAIN_BOT_DELIVERY_MODE"],
            "dry_run",
        )
        self.assertFalse(status["MAIN_BOT_REAL_SEND"])

    def test_dry_run_profile_closes_all_real_gates_atomically(self) -> None:
        path = self.root / ".env.oi"
        path.write_text(
            "MAIN_BOT_DELIVERY_MODE=real\n"
            "MAIN_BOT_REAL_SEND=true\n"
            "MAIN_BOT_REAL_SEND_ACK=发送真实主BOT提醒\n",
            encoding="utf-8",
        )

        result = self.manager.main_bot_delivery("dry-run")
        values = _env_values(path)

        self.assertEqual(values["MAIN_BOT_DELIVERY_MODE"], "dry_run")
        self.assertEqual(values["MAIN_BOT_REAL_SEND"], "false")
        self.assertEqual(values["MAIN_BOT_REAL_SEND_ACK"], "")
        self.assertFalse(result["configuration"]["MAIN_BOT_REAL_SEND"])
        self.assertEqual(
            result["configuration"]["MAIN_BOT_REAL_SEND_ACK"],
            "not_configured",
        )

    def test_real_profile_requires_telegram_and_rolls_back(self) -> None:
        path = self.root / ".env.oi"
        path.write_text(
            "# keep\nMAIN_BOT_DELIVERY_MODE=dry_run\n",
            encoding="utf-8",
        )
        original = path.read_bytes()

        with self.assertRaisesRegex(
            ConfigManagerError,
            "main_bot_real_send_gate_blocked",
        ):
            self.manager.main_bot_delivery("real")

        self.assertEqual(path.read_bytes(), original)

    def test_real_profile_sets_only_fixed_gate_and_redacts_ack(self) -> None:
        path = self.root / ".env.oi"
        path.write_text(
            "TG_BOT_TOKEN=123456:safe_TOKEN-1\n"
            "TG_CHAT_ID=-100123\n",
            encoding="utf-8",
        )

        result = self.manager.main_bot_delivery("real")
        values = _env_values(path)
        serialized = json.dumps(result, ensure_ascii=False)

        self.assertEqual(values["MAIN_BOT_DELIVERY_MODE"], "real")
        self.assertEqual(values["MAIN_BOT_REAL_SEND"], "true")
        self.assertEqual(
            values["MAIN_BOT_REAL_SEND_ACK"],
            "发送真实主BOT提醒",
        )
        self.assertNotIn("发送真实主BOT提醒", serialized)
        self.assertEqual(
            result["configuration"]["MAIN_BOT_REAL_SEND_ACK"],
            "configured",
        )

    def test_invalid_mode_ack_and_inconsistent_dry_run_are_rejected(
        self,
    ) -> None:
        for key, value in (
            ("MAIN_BOT_DELIVERY_MODE", "observe"),
            ("MAIN_BOT_REAL_SEND_ACK", "almost"),
            ("MAIN_BOT_REAL_SEND", "yes"),
        ):
            with self.subTest(key=key):
                with self.assertRaises(ConfigManagerError):
                    self.manager.set(key, value)
        path = self.root / ".env.oi"
        path.write_text(
            "MAIN_BOT_DELIVERY_MODE=dry_run\n"
            "MAIN_BOT_REAL_SEND=true\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(
            ConfigManagerError,
            "main_bot_dry_run_gate_inconsistent",
        ):
            self.manager.validate()


@unittest.skipIf(os.name == "nt", "POSIX shell execution is required")
class MainBotRunnerTests(unittest.TestCase):
    def _run(
        self,
        **updates: str,
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            python_bin = root / ".venv" / "bin" / "python"
            python_bin.parent.mkdir(parents=True)
            calls = root / "calls"
            python_bin.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$@\" >\"$CALLS_FILE\"\n",
                encoding="utf-8",
            )
            python_bin.chmod(0o755)
            (root / "main.py").write_text("", encoding="utf-8")
            env = os.environ.copy()
            env.update({
                "PAOPAO_APP_DIR": str(root),
                "CALLS_FILE": str(calls),
            })
            for key in (
                "MAIN_BOT_DELIVERY_MODE",
                "MAIN_BOT_REAL_SEND",
                "MAIN_BOT_REAL_SEND_ACK",
                "TG_BOT_TOKEN",
                "TG_CHAT_ID",
            ):
                env.pop(key, None)
            env.update(updates)
            result = subprocess.run(
                ["bash", str(RUNNER)],
                check=False,
                capture_output=True,
                text=True,
                env=env,
            )
            arguments = (
                calls.read_text(encoding="utf-8").splitlines()
                if calls.exists()
                else []
            )
            return result, arguments

    def test_default_dry_run_executes_only_main_loop(self) -> None:
        result, arguments = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(arguments[1:], ["loop"])
        self.assertNotIn("--send", arguments)
        self.assertNotIn("--confirm-real-send", arguments)

    def test_real_gate_incomplete_blocks_before_python(self) -> None:
        result, arguments = self._run(
            MAIN_BOT_DELIVERY_MODE="real",
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(arguments, [])
        self.assertEqual(
            result.stderr.strip(),
            "main_bot_real_send_gate_blocked",
        )

    def test_real_gate_complete_uses_existing_dual_cli_gate(self) -> None:
        result, arguments = self._run(
            MAIN_BOT_DELIVERY_MODE="real",
            MAIN_BOT_REAL_SEND="true",
            MAIN_BOT_REAL_SEND_ACK="发送真实主BOT提醒",
            TG_BOT_TOKEN="123456:safe_TOKEN-1",
            TG_CHAT_ID="-100123",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            arguments[1:],
            ["live", "--send", "--confirm-real-send"],
        )

    def test_real_gate_rejects_zero_chat_id(self) -> None:
        result, arguments = self._run(
            MAIN_BOT_DELIVERY_MODE="real",
            MAIN_BOT_REAL_SEND="true",
            MAIN_BOT_REAL_SEND_ACK="发送真实主BOT提醒",
            TG_BOT_TOKEN="123456:safe_TOKEN-1",
            TG_CHAT_ID="-000",
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(arguments, [])
        self.assertEqual(
            result.stderr.strip(),
            "main_bot_real_send_gate_blocked",
        )

    def test_unknown_mode_fails_closed_before_python(self) -> None:
        result, arguments = self._run(
            MAIN_BOT_DELIVERY_MODE="unknown",
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(arguments, [])
        self.assertEqual(
            result.stderr.strip(),
            "main_bot_delivery_mode_invalid",
        )


class MainBotUnitAndMenuTests(unittest.TestCase):
    def test_runner_is_fixed_array_without_eval_or_secret_output(self) -> None:
        text = RUNNER.read_text(encoding="utf-8")
        self.assertIn('args=("$PYTHON_BIN" "${APP_DIR}/main.py")', text)
        self.assertIn('args+=("loop")', text)
        self.assertIn(
            'args+=("live" "--send" "--confirm-real-send")',
            text,
        )
        self.assertIn('exec "${args[@]}"', text)
        self.assertNotIn("eval", text)
        for forbidden in (
            'echo "$TG_BOT_TOKEN"',
            'echo "$TG_CHAT_ID"',
            'echo "$MAIN_BOT_REAL_SEND_ACK"',
        ):
            self.assertNotIn(forbidden, text)

    def test_installer_writes_guarded_systemd_semantics_only(self) -> None:
        text = INSTALLER.read_text(encoding="utf-8")
        self.assertIn(
            "ExecStart=${APP_DIR}/scripts/run_main_bot.sh",
            text,
        )
        self.assertIn("Restart=on-failure", text)
        self.assertIn("RestartPreventExitStatus=2", text)
        self.assertIn("SuccessExitStatus=130", text)
        self.assertIn("KillSignal=SIGINT", text)
        self.assertIn("TimeoutStopSec=30", text)
        self.assertIn("NoNewPrivileges=true", text)
        self.assertIn("PrivateTmp=true", text)
        self.assertIn("UMask=0077", text)
        self.assertNotIn("paopao-market-stream", text)
        self.assertNotIn("paopao-oar-watch", text)

    def test_menu_exposes_mode_profiles_and_real_confirmations(self) -> None:
        text = MENU.read_text(encoding="utf-8")
        for expected in (
            "主 BOT 运行模式",
            "main-bot-delivery dry-run",
            "main-bot-delivery real",
            'confirm_phrase "启用真实主BOT提醒"',
            'confirm_phrase "重启真实主BOT"',
            'confirm_phrase "重启主BOT"',
        ):
            self.assertIn(expected, text)


class TelegramDryRunRedactionTests(unittest.TestCase):
    def test_dry_run_prints_route_presence_without_route_ids(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            settings = Settings(
                base_dir=root,
                data_dir=root,
                tg_bot_token="123456:safe_TOKEN-1",
                tg_chat_id="-100123",
                tg_topic_id="987654",
                tg_push_history_path=root / "push_history.json",
            )
            gateway = TelegramGateway(settings, JsonStore(root))
            output = StringIO()
            with redirect_stdout(output):
                result = gateway.send(
                    "dry-run body",
                    "TG_TEST_MESSAGE",
                    "safe-dedup",
                    send=False,
                    confirm_real_send=False,
                    reply_to_message_id=456789,
                )

        text = output.getvalue()
        self.assertEqual(result.status, "dry_run")
        self.assertIn("template_id: TG_TEST_MESSAGE", text)
        self.assertIn("dedup_key: safe-dedup", text)
        self.assertIn("topic_configured: true", text)
        self.assertIn("reply_target_configured: true", text)
        self.assertNotIn("987654", text)
        self.assertNotIn("456789", text)
        self.assertNotIn("topic_id:", text)
        self.assertNotIn("reply_to_message_id:", text)


if __name__ == "__main__":
    unittest.main()
