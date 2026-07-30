from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "ops" / "systemd" / "paopao-oar-watch.service"
INSTALLER = ROOT / "scripts" / "install_oar_watch.sh"
MENU = ROOT / "scripts" / "paopao_menu.sh"
RUNNER = ROOT / "scripts" / "run_oar_watch.sh"
BASH = Path(r"C:\Program Files\Git\bin\bash.exe")


class OarWatchServiceTests(unittest.TestCase):
    def _bash(self) -> str:
        candidate = str(BASH) if BASH.exists() else shutil.which("bash")
        if not candidate:
            self.skipTest("bash is unavailable")
        return candidate

    def _run_launcher(
        self,
        values: dict[str, str] | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            args_file = root / "args.txt"
            (root / "onchain_main.py").write_text(
                "#!/usr/bin/env bash\n"
                "printf '%s\\n' \"$@\" >\"$OAR_ARGS_FILE\"\n",
                encoding="utf-8",
                newline="\n",
            )
            env = {
                **os.environ,
                "PAOPAO_APP_DIR": root.as_posix(),
                "PAOPAO_PYTHON_BIN": "bash",
                "OAR_ARGS_FILE": args_file.as_posix(),
                **(values or {}),
            }
            result = subprocess.run(
                [self._bash(), RUNNER.as_posix()],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            args = (
                args_file.read_text(encoding="utf-8").splitlines()
                if args_file.exists()
                else []
            )
            return result, args

    def test_unit_uses_guarded_launcher(self) -> None:
        text = UNIT.read_text(encoding="utf-8")
        exec_line = next(
            line for line in text.splitlines() if line.startswith("ExecStart=")
        )
        self.assertEqual(
            exec_line,
            "ExecStart=/home/ubuntu/paopao-crypto-radar/"
            "scripts/run_oar_watch.sh",
        )
        for forbidden in (
            "--notify-dry-run",
            "--with-ai",
            "--send",
            "--confirm-real-send",
        ):
            self.assertNotIn(forbidden, exec_line)

    def test_unit_uses_private_onchain_environment_and_non_root_user(
        self,
    ) -> None:
        text = UNIT.read_text(encoding="utf-8")
        self.assertIn("User=ubuntu", text)
        self.assertIn(
            "EnvironmentFile=/home/ubuntu/paopao-crypto-radar/.env.onchain",
            text,
        )
        self.assertIn("Restart=on-failure", text)
        self.assertIn("KillSignal=SIGINT", text)
        self.assertIn("SuccessExitStatus=130", text)
        success_line = next(
            line for line in text.splitlines()
            if line.startswith("SuccessExitStatus=")
        )
        self.assertEqual(success_line, "SuccessExitStatus=130")
        self.assertIn("NoNewPrivileges=true", text)
        self.assertIn("PrivateTmp=true", text)
        self.assertIn("UMask=0077", text)

    def test_installer_is_oar_only_and_rejects_duplicate_writer(self) -> None:
        text = INSTALLER.read_text(encoding="utf-8")
        self.assertIn("duplicate_writer_risk", text)
        self.assertIn("pgrep -af '[o]nchain_main.py.*watch-live'", text)
        self.assertIn("systemctl daemon-reload", text)
        self.assertIn("run_oar_watch.sh", text)
        self.assertIn("service_started=false", text)
        self.assertNotIn("enable --now", text)
        self.assertNotIn("paopao-radar", text)
        self.assertNotIn("paopao-market-stream", text)

    def test_menu_has_enforcing_worker_guard(self) -> None:
        text = MENU.read_text(encoding="utf-8")
        self.assertIn("show_oar_workers()", text)
        self.assertIn("assert_no_conflicting_oar_worker()", text)
        self.assertIn("duplicate_writer_risk", text)
        self.assertNotIn("duplicate_worker_check()", text)

    def test_observe_is_default_and_adds_no_delivery_or_ai_flags(self) -> None:
        result, args = self._run_launcher()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(args, ["watch-live", "--allow-network"])

    def test_dry_run_adds_only_notify_dry_run(self) -> None:
        result, args = self._run_launcher({
            "OAR_WATCH_DELIVERY_MODE": "dry_run",
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            args,
            ["watch-live", "--allow-network", "--notify-dry-run"],
        )
        self.assertNotIn("--send", args)

    def test_real_mode_fails_closed_without_complete_gate(self) -> None:
        result, args = self._run_launcher({
            "OAR_WATCH_DELIVERY_MODE": "real",
        })
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(args, [])
        self.assertIn("real_send_gate_blocked", result.stderr)

    def test_complete_real_gate_adds_both_existing_send_flags(self) -> None:
        result, args = self._run_launcher({
            "OAR_WATCH_DELIVERY_MODE": "real",
            "ONCHAIN_REAL_SEND": "true",
            "OAR_WATCH_REAL_SEND_ACK": "发送真实链上提醒",
            "TG_BOT_TOKEN": "123456:safe_TOKEN-1",
            "TG_CHAT_ID": "-100123",
            "TG_ONCHAIN_FLOW_TOPIC_ID": "42",
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            args,
            [
                "watch-live",
                "--allow-network",
                "--send",
                "--confirm-real-send",
            ],
        )
        self.assertNotIn("--with-ai", args)

    def test_ai_flag_requires_watch_global_and_complete_config(self) -> None:
        base = {
            "OAR_WATCH_DELIVERY_MODE": "dry_run",
            "OAR_WATCH_WITH_AI": "true",
        }
        _, disabled = self._run_launcher({
            **base,
            "OAR_AI_ENABLE": "false",
        })
        self.assertNotIn("--with-ai", disabled)
        _, incomplete = self._run_launcher({
            **base,
            "OAR_AI_ENABLE": "true",
        })
        self.assertNotIn("--with-ai", incomplete)
        result, enabled = self._run_launcher({
            **base,
            "OAR_AI_ENABLE": "true",
            "OAR_AI_BASE_URL": "https://ai.invalid",
            "OAR_AI_API_KEY": "private-key",
            "OAR_AI_MODEL": "model",
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--with-ai", enabled)

    def test_observe_never_enables_ai_even_when_all_ai_gates_are_true(
        self,
    ) -> None:
        result, args = self._run_launcher({
            "OAR_WATCH_DELIVERY_MODE": "observe",
            "OAR_WATCH_WITH_AI": "true",
            "OAR_AI_ENABLE": "true",
            "OAR_AI_BASE_URL": "https://ai.invalid",
            "OAR_AI_API_KEY": "private-key",
            "OAR_AI_MODEL": "model",
        })
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("--with-ai", args)

    def test_unknown_mode_fails_closed(self) -> None:
        result, args = self._run_launcher({
            "OAR_WATCH_DELIVERY_MODE": "unknown",
        })
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(args, [])
        self.assertIn("watch_delivery_mode_invalid", result.stderr)

    def test_launcher_uses_fixed_array_without_eval_or_secret_output(
        self,
    ) -> None:
        text = RUNNER.read_text(encoding="utf-8")
        self.assertNotIn("eval", text)
        self.assertNotIn('printf \'%s\' "$OAR_AI_API_KEY"', text)
        self.assertNotIn('printf \'%s\' "$TG_BOT_TOKEN"', text)


if __name__ == "__main__":
    unittest.main()
