from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from paopao_radar.onchain_flow.cli import build_parser, main as cli_main

from tests.onchain_flow.support import make_settings


CONTRACT = "0x1111111111111111111111111111111111111111"


class OarP4CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = make_settings(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_cli(self, args: list[str]) -> tuple[int, dict[str, object]]:
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli_main(args, settings=self.settings)
        return code, json.loads(output.getvalue())

    def test_p4_commands_are_registered(self) -> None:
        commands = (
            [
                "registry-add",
                "--market-symbol",
                "AAAUSDT",
                "--chain",
                "base",
                "--contract",
                CONTRACT,
            ],
            ["registry-verify", "--token-key", f"8453:{CONTRACT}"],
            ["registry-list"],
            ["registry-disable", "--token-key", f"8453:{CONTRACT}"],
            ["watch-add", "--token-key", f"8453:{CONTRACT}"],
            ["watch-list"],
            ["watch-remove", "--token-key", f"8453:{CONTRACT}"],
            ["bridge-once"],
            ["watch-once"],
            ["watch-live", "--duration-minutes", "0"],
        )
        for arguments in commands:
            with self.subTest(command=arguments[0]):
                self.assertEqual(
                    build_parser().parse_args(arguments).command,
                    arguments[0],
                )

    def test_watch_once_without_network_has_zero_side_effects(self) -> None:
        with patch(
            "paopao_radar.onchain_flow.watch_scanner.SignalBridge.run_once",
            side_effect=AssertionError("bridge ran"),
        ), patch(
            "paopao_radar.onchain_flow.watch_scanner."
            "TokenAnalysisService.from_settings",
            side_effect=AssertionError("RPC service created"),
        ), patch(
            "paopao_radar.onchain_flow.watch_scanner.ReportNotifier",
            side_effect=AssertionError("Gateway created"),
        ):
            code, payload = self.run_cli(["watch-once"])
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason"], "allow_network_required")
        self.assertFalse(payload["network_activity"])
        self.assertFalse(payload["database_writes"])
        self.assertFalse(payload["telegram_calls"])
        self.assertFalse(payload["ai_calls"])
        self.assertFalse(self.settings.data_dir.exists())

    def test_watch_live_disabled_has_zero_side_effects(self) -> None:
        code, payload = self.run_cli(
            ["watch-live", "--allow-network", "--duration-minutes", "0"]
        )
        self.assertEqual(code, 1)
        self.assertEqual(payload["reason"], "automation_disabled")
        self.assertFalse(payload["network_activity"])
        self.assertFalse(payload["database_writes"])
        self.assertFalse(self.settings.data_dir.exists())

    def test_registry_list_and_watch_list_do_not_initialize_database(self) -> None:
        for command in ("registry-list", "watch-list"):
            with self.subTest(command=command):
                code, payload = self.run_cli([command])
                self.assertEqual(code, 0)
                self.assertEqual(payload["status"], "not_initialized")
                self.assertFalse(self.settings.data_dir.exists())

    def test_registry_add_is_pending_and_offline(self) -> None:
        code, payload = self.run_cli(
            [
                "registry-add",
                "--market-symbol",
                "AAAUSDT",
                "--chain",
                "base",
                "--contract",
                CONTRACT,
            ]
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["token"]["status"], "pending")

    def test_registry_verify_requires_network_before_client(self) -> None:
        self.run_cli(
            [
                "registry-add",
                "--market-symbol",
                "AAAUSDT",
                "--chain",
                "base",
                "--contract",
                CONTRACT,
            ]
        )
        with patch(
            "paopao_radar.onchain_flow.cli.RegistryService",
            side_effect=AssertionError("service created"),
        ):
            code, payload = self.run_cli(
                ["registry-verify", "--token-key", f"8453:{CONTRACT}"]
            )
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"], "allow_network_required")
        self.assertFalse(payload["network_activity"])

    def test_registry_verify_reports_primary_and_reconciliation(self) -> None:
        token_key = f"8453:{CONTRACT}"
        service = Mock()
        service.verify.return_value = {
            "token_key": token_key,
            "status": "verified",
            "is_primary": 1,
            "verification": {
                "was_primary": False,
                "is_primary": True,
                "primary_changed": True,
            },
            "reconciliation": {
                "status": "ok",
                "examined": 1,
                "resolved": 1,
                "expired": 0,
                "remaining_open": 0,
                "watch_created": 1,
                "watch_refreshed": 0,
            },
        }
        with patch(
            "paopao_radar.onchain_flow.cli.RegistryService",
            return_value=service,
        ):
            code, payload = self.run_cli(
                [
                    "registry-verify",
                    "--token-key",
                    token_key,
                    "--allow-network",
                    "--set-primary",
                ]
            )
        self.assertEqual(code, 0)
        self.assertTrue(payload["verification"]["primary_changed"])
        self.assertEqual(payload["reconciliation"]["resolved"], 1)
        self.assertNotIn("reconciliation", payload["token"])

    def test_bridge_missing_main_db_never_creates_it(self) -> None:
        code, payload = self.run_cli(["bridge-once"])
        self.assertEqual(code, 0)
        self.assertEqual(payload["source_status"], "source_not_initialized")
        self.assertFalse(self.settings.main_signal_db_path.exists())
        self.assertFalse(payload["network_activity"])
        self.assertFalse(payload["telegram_calls"])
        self.assertFalse(payload["ai_calls"])

    def test_status_and_doctor_are_offline(self) -> None:
        for command in ("status", "doctor"):
            with self.subTest(command=command):
                code, payload = self.run_cli([command])
                self.assertEqual(code, 0)
                self.assertNotIn("configured-secret", json.dumps(payload))
                self.assertFalse(self.settings.main_signal_db_path.exists())


if __name__ == "__main__":
    unittest.main()
