from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from config import Settings
from runtime.cli import build_parser, run_private_control
from shared.storage import JsonStore


ROOT = Path(__file__).resolve().parents[2]
RUNNER = ROOT / "scripts" / "run_private_control.sh"
INSTALLER = ROOT / "scripts" / "install_private_control_service.sh"
INSTALL_SERVER = ROOT / "scripts" / "install_server.sh"
UPDATE_SERVER = ROOT / "scripts" / "update_server.sh"
MENU = ROOT / "scripts" / "paopao_menu.sh"


@unittest.skipIf(os.name == "nt", "POSIX shell execution is required")
class PrivateControlRunnerTests(unittest.TestCase):
    def _run(self, **updates: str) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            python_bin = root / ".venv" / "bin" / "python"
            python_bin.parent.mkdir(parents=True)
            calls = root / "calls"
            lock = root / "worker.lock"
            python_bin.write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$@\" >\"$CALLS_FILE\"\n",
                encoding="utf-8",
            )
            python_bin.chmod(0o755)
            (root / "main.py").write_text("", encoding="utf-8")
            env = os.environ.copy()
            for key in (
                "TG_PRIVATE_CONTROL_ENABLE",
                "TG_PRIVATE_CONTROL_ADMIN_USER_ID",
                "TG_BOT_TOKEN",
            ):
                env.pop(key, None)
            env.update(
                {
                    "PAOPAO_APP_DIR": str(root),
                    "CALLS_FILE": str(calls),
                    "TG_PRIVATE_CONTROL_LOCK_FILE": str(lock),
                }
            )
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

    def test_disabled_by_default_does_not_start_python(self) -> None:
        result, arguments = self._run()

        self.assertEqual(result.returncode, 2)
        self.assertEqual(arguments, [])
        self.assertEqual(result.stderr.strip(), "private_control_disabled")

    def test_enabled_uses_only_fixed_private_control_command(self) -> None:
        result, arguments = self._run(
            TG_PRIVATE_CONTROL_ENABLE="true",
            TG_PRIVATE_CONTROL_ADMIN_USER_ID="123456789",
            TG_BOT_TOKEN="123456:fake-token",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(arguments[1:], ["private-control"])
        serialized = " ".join(arguments)
        self.assertNotIn("123456789", serialized)
        self.assertNotIn("fake-token", serialized)
        self.assertNotIn("--send", serialized)


class PrivateControlUnitTests(unittest.TestCase):
    def test_cli_registers_private_control_and_disabled_mode_is_offline(self) -> None:
        self.assertEqual(
            build_parser().parse_args(["private-control"]).command,
            "private-control",
        )
        with tempfile.TemporaryDirectory() as raw, patch(
            "requests.Session",
        ) as session:
            root = Path(raw)
            settings = Settings(
                base_dir=root,
                data_dir=root,
                tg_private_control_enable=False,
            )

            result = run_private_control(settings, JsonStore(root))

        self.assertEqual(result, 2)
        session.assert_not_called()

    def test_runner_is_fixed_array_without_eval_or_secret_output(self) -> None:
        text = RUNNER.read_text(encoding="utf-8")

        self.assertIn(
            'args=("$PYTHON_BIN" "${APP_DIR}/main.py" "private-control")',
            text,
        )
        self.assertIn('exec "${args[@]}"', text)
        self.assertIn("flock -n 9", text)
        self.assertNotIn("eval", text)
        self.assertNotIn('echo "$TG_BOT_TOKEN"', text)
        self.assertNotIn('echo "$TG_PRIVATE_CONTROL_ADMIN_USER_ID"', text)
        self.assertNotIn("--send", text)

    def test_installer_is_isolated_and_safe_by_default(self) -> None:
        text = INSTALLER.read_text(encoding="utf-8")

        for expected in (
            "ExecStart=/bin/bash ${APP_DIR}/scripts/run_private_control.sh",
            "Restart=on-failure",
            "RestartPreventExitStatus=2",
            "SuccessExitStatus=130",
            "KillSignal=SIGINT",
            "TimeoutStopSec=30",
            "NoNewPrivileges=true",
            "PrivateTmp=true",
            "UMask=0077",
            "RuntimeDirectory=paopao-private-control",
        ):
            self.assertIn(expected, text)
        self.assertIn('START_PRIVATE_CONTROL="${START_PRIVATE_CONTROL:-0}"', text)
        self.assertIn('ENABLE_PRIVATE_CONTROL="${ENABLE_PRIVATE_CONTROL:-0}"', text)
        self.assertNotIn("paopao-radar", text)
        self.assertNotIn("paopao-market-stream", text)
        self.assertNotIn("Restart=always", text)

    def test_server_scripts_install_but_do_not_start_private_control(self) -> None:
        install_text = INSTALL_SERVER.read_text(encoding="utf-8")
        update_text = UPDATE_SERVER.read_text(encoding="utf-8")

        self.assertIn("install_private_control_service.sh", install_text)
        self.assertIn("install_private_control_service.sh", update_text)
        self.assertNotIn("PRIVATE_CONTROL_SERVICE_NAME", install_text)
        self.assertIn("private_control_was_active=0", update_text)
        self.assertIn(
            'if [ "$private_control_was_active" = "1" ]; then',
            update_text,
        )
        self.assertIn(
            'systemctl restart "$PRIVATE_CONTROL_SERVICE_NAME"',
            update_text,
        )
        restart_list = (
            'systemctl restart "$MARKET_STREAM_SERVICE_NAME" '
            '"$SERVICE_NAME" "${HEALTH_SERVICE_NAME}.timer" '
            '"${BACKUP_SERVICE_NAME}.timer"'
        )
        self.assertIn(restart_list, update_text)

    def test_finalshell_menu_keeps_binding_and_service_control_visible(self) -> None:
        text = MENU.read_text(encoding="utf-8")

        for expected in (
            "管理员私聊菜单",
            "config_set TG_PRIVATE_CONTROL_ADMIN_USER_ID",
            "set_private_control_admin",
            "run_config enable TG_PRIVATE_CONTROL_ENABLE",
            "run_config disable TG_PRIVATE_CONTROL_ENABLE",
            "启用管理员私聊菜单",
            "关闭管理员私聊菜单",
            "journalctl -u \"$PRIVATE_CONTROL_SERVICE_NAME\"",
        ):
            self.assertIn(expected, text)
        self.assertNotIn("read -s", text)

    def test_admin_change_disables_old_worker_before_atomic_write(self) -> None:
        text = MENU.read_text(encoding="utf-8")
        function = text.split("set_private_control_admin() {", 1)[1].split(
            "\n}",
            1,
        )[0]

        disable_at = function.index(
            'systemctl disable --now "$PRIVATE_CONTROL_SERVICE_NAME"'
        )
        write_at = function.index(
            "config_set TG_PRIVATE_CONTROL_ADMIN_USER_ID"
        )
        enable_at = function.index(
            'systemctl enable "$PRIVATE_CONTROL_SERVICE_NAME"'
        )
        start_at = function.index(
            'systemctl start "$PRIVATE_CONTROL_SERVICE_NAME"'
        )
        self.assertLess(disable_at, write_at)
        self.assertLess(write_at, enable_at)
        self.assertLess(write_at, start_at)
        self.assertLess(enable_at, start_at)
        self.assertIn("保持停止且不随开机启动", function)


if __name__ == "__main__":
    unittest.main()
