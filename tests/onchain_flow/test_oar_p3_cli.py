from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from paopao_radar.onchain_flow.cli import build_parser, main as cli_main
from paopao_radar.onchain_flow.config import (
    OnchainSettings,
    SettingsValidationError,
)

from tests.onchain_flow.support import make_settings


CONTRACT = "0x9999999999999999999999999999999999999999"


class OarP3CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = make_settings(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_report_and_notify_commands_are_registered(self) -> None:
        for command in ("token-report", "token-notify"):
            with self.subTest(command=command):
                args = build_parser().parse_args(
                    [
                        command,
                        "--chain",
                        "base",
                        "--contract",
                        CONTRACT,
                        "--window",
                        "24h",
                    ]
                )
                self.assertEqual(args.command, command)

    def test_token_report_rejects_send_flags(self) -> None:
        with self.assertRaises(SystemExit):
            build_parser().parse_args(
                [
                    "token-report",
                    "--chain",
                    "base",
                    "--contract",
                    CONTRACT,
                    "--window",
                    "24h",
                    "--send",
                ]
            )

    def test_no_allow_network_has_zero_report_or_notifier_factories(self) -> None:
        for command in ("token-report", "token-notify"):
            output = io.StringIO()
            with self.subTest(command=command):
                with patch(
                    "paopao_radar.onchain_flow.cli."
                    "TokenReportService.from_settings",
                    side_effect=AssertionError("network service created"),
                ), patch(
                    "paopao_radar.onchain_flow.cli.ReportNotifier",
                    side_effect=AssertionError("Telegram gateway created"),
                ):
                    with redirect_stdout(output):
                        code = cli_main(
                            [
                                command,
                                "--chain",
                                "base",
                                "--contract",
                                CONTRACT,
                                "--window",
                                "24h",
                            ],
                            settings=self.settings,
                        )
                payload = json.loads(output.getvalue())
                self.assertEqual(code, 1)
                self.assertFalse(payload["network_activity"])
                self.assertFalse(payload["database_writes"])
                self.assertFalse(payload["telegram_calls"])
                self.assertFalse(payload["ai_calls"])
                self.assertFalse(self.settings.data_dir.exists())
                output.seek(0)
                output.truncate(0)

    def test_ai_defaults_are_disabled_and_diagnostics_redacted(self) -> None:
        settings = OnchainSettings.load(
            environ={},
            base_dir=self.root,
        )
        self.assertFalse(settings.oar_ai_enable)
        self.assertFalse(settings.oar_replace_complete_card_with_partial)
        self.assertFalse(
            settings.oar_replace_rich_ai_card_with_rule_only
        )
        diagnostic = settings.diagnostic()["oar_reporting"]
        self.assertFalse(diagnostic["ai_enabled"])
        self.assertFalse(diagnostic["ai_api_key_configured"])
        self.assertNotIn("OAR_AI_API_KEY", json.dumps(diagnostic))

    def test_token_report_with_network_authority_never_builds_gateway(
        self,
    ) -> None:
        payload = {
            "schema_version": 1,
            "status": "ok",
            "complete": True,
            "truncated": False,
            "truncation_reason": None,
            "summary": {"transfer_count": 0},
            "analysis": {"complete": True},
            "report": {"ai": {"status": "not_requested", "calls": 0}},
        }
        fake_service = type(
            "FakeReportService",
            (),
            {"execute": lambda self, query, with_ai: payload},
        )()
        output = io.StringIO()
        with patch(
            "paopao_radar.onchain_flow.cli."
            "TokenReportService.from_settings",
            return_value=fake_service,
        ), patch(
            "paopao_radar.onchain_flow.cli.ReportNotifier",
            side_effect=AssertionError("Telegram gateway created"),
        ):
            with redirect_stdout(output):
                code = cli_main(
                    [
                        "token-report",
                        "--chain",
                        "base",
                        "--contract",
                        CONTRACT,
                        "--window",
                        "24h",
                        "--allow-network",
                    ],
                    settings=self.settings,
                )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "ok")

    def test_configured_ai_secret_is_never_in_diagnostics(self) -> None:
        settings = OnchainSettings.load(
            environ={
                "OAR_AI_ENABLE": "true",
                "OAR_AI_BASE_URL": "https://ai.invalid/v1",
                "OAR_AI_API_KEY": "super-secret-value",
                "OAR_AI_MODEL": "fixture-model",
            },
            base_dir=self.root,
        )
        settings.validate()
        diagnostic = json.dumps(
            settings.diagnostic(),
            ensure_ascii=False,
        )
        self.assertNotIn("super-secret-value", diagnostic)
        self.assertIn('"ai_api_key_configured": true', diagnostic)

    def test_ai_base_url_requires_https_except_for_loopback(self) -> None:
        allowed = (
            "https://remote.example/v1",
            "http://localhost:11434/v1",
            "http://127.0.0.1:11434/v1",
            "http://[::1]:11434/v1",
        )
        for base_url in allowed:
            with self.subTest(base_url=base_url):
                settings = OnchainSettings.load(
                    environ={
                        "OAR_AI_ENABLE": "true",
                        "OAR_AI_BASE_URL": base_url,
                        "OAR_AI_API_KEY": "configured-secret",
                        "OAR_AI_MODEL": "fixture-model",
                    },
                    base_dir=self.root,
                )
                settings.validate()

        settings = OnchainSettings.load(
            environ={
                "OAR_AI_ENABLE": "true",
                "OAR_AI_BASE_URL": "http://remote.example/v1",
                "OAR_AI_API_KEY": "configured-secret",
                "OAR_AI_MODEL": "fixture-model",
            },
            base_dir=self.root,
        )
        with self.assertRaises(SettingsValidationError) as caught:
            settings.validate()
        self.assertEqual(
            str(caught.exception),
            "OAR_AI_BASE_URL must use HTTPS unless it targets loopback",
        )
        self.assertNotIn("configured-secret", str(caught.exception))

    def test_ai_base_url_rejects_credentials_query_and_fragment(self) -> None:
        rejected = (
            "https://user:pass@remote.example/v1",
            "https://remote.example/v1?mode=test",
            "https://remote.example/v1#fragment",
        )
        for base_url in rejected:
            with self.subTest(base_url=base_url):
                settings = OnchainSettings.load(
                    environ={
                        "OAR_AI_ENABLE": "true",
                        "OAR_AI_BASE_URL": base_url,
                        "OAR_AI_API_KEY": "configured-secret",
                        "OAR_AI_MODEL": "fixture-model",
                    },
                    base_dir=self.root,
                )
                with self.assertRaises(SettingsValidationError) as caught:
                    settings.validate()
                self.assertNotIn(
                    "configured-secret",
                    str(caught.exception),
                )


if __name__ == "__main__":
    unittest.main()
