from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[2]


class AltcoinProductionDeploymentTests(unittest.TestCase):
    def test_market_stream_runner_defaults_to_legacy_and_requires_explicit_mode(self) -> None:
        script = (ROOT / "scripts" / "run_market_stream.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn(
            'PRODUCTION_ENABLE="${ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_ENABLE:-false}"',
            script,
        )
        self.assertIn('exec "$PYTHON_BIN" "${APP_DIR}/main.py" market-stream', script)
        self.assertIn('"--altcoin-production"', script)
        self.assertIn('EXPECTED_CONFIRM="ENABLE_ALTCOIN_ANOMALY_REAL_SEND"', script)
        self.assertIn('args+=("--send" "--confirm-real-send")', script)
        self.assertIn("altcoin_production_real_send_gate_blocked", script)
        self.assertIn("TG_BOT_TOKEN", script)
        self.assertIn("TG_CHAT_ID", script)
        self.assertIn("TG_ALTCOIN_CONTRACT_ANOMALY_TOPIC_ID", script)
        self.assertNotIn("telegram-topic-setup", script)
        self.assertNotIn("createForumTopic", script)

        production_case = script.index("  true)")
        explicit_flag = script.index('"--altcoin-production"')
        send_flags = script.index('args+=("--send" "--confirm-real-send")')
        confirmation_check = script.index('SEND_CONFIRM" != "$EXPECTED_CONFIRM')
        topic_check = script.index("TG_ALTCOIN_CONTRACT_ANOMALY_TOPIC_ID")
        self.assertGreater(explicit_flag, production_case)
        self.assertGreater(send_flags, confirmation_check)
        self.assertGreater(send_flags, topic_check)

    def test_market_stream_systemd_unit_uses_wrapper_and_stops_restart_loop_on_gate_failure(self) -> None:
        script = (ROOT / "scripts" / "install_market_stream_service.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("ExecStart=${APP_DIR}/scripts/run_market_stream.sh", script)
        self.assertIn("EnvironmentFile=-${APP_DIR}/config/.env.oi", script)
        self.assertIn("Restart=on-failure", script)
        self.assertIn("RestartPreventExitStatus=2", script)
        self.assertIn("KillSignal=SIGINT", script)
        self.assertIn("NoNewPrivileges=true", script)
        self.assertIn("PrivateTmp=true", script)
        self.assertIn("UMask=0077", script)
        self.assertIn("market_stream_env_permissions_must_be_600", script)
        self.assertIn("market_stream_env_symlink_rejected", script)
        self.assertIn("cp --preserve=mode,ownership,timestamps", script)
        self.assertNotIn("ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_SEND_CONFIRM=", script)
        self.assertNotIn("TG_BOT_TOKEN=", script)

    def test_install_and_update_paths_both_install_the_hardened_market_stream_unit(self) -> None:
        install = (ROOT / "scripts" / "install_server.sh").read_text(
            encoding="utf-8"
        )
        update = (ROOT / "scripts" / "update_server.sh").read_text(
            encoding="utf-8"
        )

        for name, script in (("install", install), ("update", update)):
            with self.subTest(script=name):
                self.assertIn("install_market_stream_service.sh", script)
                self.assertIn("START_MARKET_STREAM=0", script)
                self.assertIn("MARKET_STREAM_MEMORY_HIGH", script)
                self.assertIn("MARKET_STREAM_MEMORY_MAX", script)
        self.assertIn(
            'systemctl restart "$MARKET_STREAM_SERVICE_NAME"',
            update,
        )

    @unittest.skipUnless(
        os.name == "posix" and shutil.which("bash"),
        "a native POSIX bash is not available",
    )
    def test_new_shell_entrypoints_pass_shell_syntax_validation(self) -> None:
        for script in (
            ROOT / "scripts" / "run_market_stream.sh",
            ROOT / "scripts" / "install_market_stream_service.sh",
        ):
            with self.subTest(script=script.name):
                completed = subprocess.run(
                    ["bash", "-n", str(script)],
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)

    @unittest.skipUnless(
        os.name == "posix" and shutil.which("bash"),
        "a native POSIX bash is not available",
    )
    def test_market_stream_runner_enforces_mode_and_real_send_gates(self) -> None:
        runner = ROOT / "scripts" / "run_market_stream.sh"
        with TemporaryDirectory() as directory:
            app = Path(directory)
            python = app / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            log = app / "arguments.txt"
            python.write_text(
                "#!/usr/bin/env bash\nprintf '%s\\n' \"$@\" >\"$TEST_ARGUMENT_LOG\"\n",
                encoding="utf-8",
            )
            python.chmod(0o700)
            base_env = {
                **os.environ,
                "PAOPAO_APP_DIR": str(app),
                "TEST_ARGUMENT_LOG": str(log),
            }

            legacy = subprocess.run(
                ["bash", str(runner)],
                env={
                    **base_env,
                    "ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_ENABLE": "false",
                    "ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_SEND_ENABLE": "false",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(legacy.returncode, 0, legacy.stderr)
            self.assertEqual(
                log.read_text(encoding="utf-8").splitlines(),
                [str(app / "main.py"), "market-stream"],
            )

            log.unlink()
            preview = subprocess.run(
                ["bash", str(runner)],
                env={
                    **base_env,
                    "ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_ENABLE": "true",
                    "ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_SEND_ENABLE": "false",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(preview.returncode, 0, preview.stderr)
            self.assertEqual(
                log.read_text(encoding="utf-8").splitlines(),
                [str(app / "main.py"), "market-stream", "--altcoin-production"],
            )

            log.unlink()
            blocked = subprocess.run(
                ["bash", str(runner)],
                env={
                    **base_env,
                    "ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_ENABLE": "true",
                    "ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_SEND_ENABLE": "true",
                    "ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_SEND_CONFIRM": "wrong",
                    "TG_BOT_TOKEN": "123:fake-token",
                    "TG_CHAT_ID": "-100123",
                    "TG_ALTCOIN_CONTRACT_ANOMALY_TOPIC_ID": "321",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(blocked.returncode, 2)
            self.assertFalse(log.exists())

            allowed = subprocess.run(
                ["bash", str(runner)],
                env={
                    **base_env,
                    "ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_ENABLE": "true",
                    "ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_SEND_ENABLE": "true",
                    "ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_SEND_CONFIRM": (
                        "ENABLE_ALTCOIN_ANOMALY_REAL_SEND"
                    ),
                    "TG_BOT_TOKEN": "123:fake-token",
                    "TG_CHAT_ID": "-100123",
                    "TG_ALTCOIN_CONTRACT_ANOMALY_TOPIC_ID": "321",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(allowed.returncode, 0, allowed.stderr)
            self.assertEqual(
                log.read_text(encoding="utf-8").splitlines(),
                [
                    str(app / "main.py"),
                    "market-stream",
                    "--altcoin-production",
                    "--send",
                    "--confirm-real-send",
                ],
            )


if __name__ == "__main__":
    unittest.main()
