from __future__ import annotations

import json
import unittest
from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import replace
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from paopao_radar.onchain_flow.cli import build_parser, main
from paopao_radar.onchain_flow.token_activity import (
    TokenActivityQuery,
    TokenActivityQueryService,
)
from paopao_radar.onchain_flow.token_analysis import TokenAnalysisService

from .analysis_support import fixture_case
from .support import make_settings


TOKEN = "0x9999999999999999999999999999999999999999"


class StaticActivityService:
    def __init__(self, payload: dict[str, object]):
        self.payload = payload
        self.calls = 0

    def execute(self, _query):
        self.calls += 1
        return deepcopy(self.payload)


class TokenAnalysisTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = make_settings(self.root)
        self.query = TokenActivityQuery.create(
            self.settings,
            chain="base",
            contract=TOKEN,
            window="1h",
            max_events=None,
            max_rpc_requests=None,
            top_n=None,
            with_price=False,
            min_usd=None,
        )

    def analyze(
        self,
        payload: dict[str, object],
        *,
        settings=None,
    ) -> dict[str, object]:
        service = StaticActivityService(payload)
        result = TokenAnalysisService(
            settings or self.settings, service
        ).execute(self.query)
        self.assertEqual(service.calls, 1)
        return result

    def test_cli_registers_token_analysis_without_send_flags(self) -> None:
        args = build_parser().parse_args(
            [
                "token-analysis",
                "--chain",
                "base",
                "--contract",
                TOKEN,
                "--window",
                "24h",
            ]
        )
        self.assertEqual(args.command, "token-analysis")
        self.assertFalse(hasattr(args, "send"))
        self.assertFalse(hasattr(args, "confirm_real_send"))

    def test_cli_rejects_send_arguments(self) -> None:
        with self.assertRaises(SystemExit):
            build_parser().parse_args(
                [
                    "token-analysis",
                    "--chain",
                    "base",
                    "--contract",
                    TOKEN,
                    "--window",
                    "24h",
                    "--send",
                ]
            )

    def test_missing_allow_network_has_zero_factories_and_side_effects(
        self,
    ) -> None:
        output = StringIO()
        with (
            patch.object(
                TokenAnalysisService, "from_settings"
            ) as analysis_factory,
            patch.object(
                TokenActivityQueryService, "from_settings"
            ) as activity_factory,
            redirect_stdout(output),
        ):
            code = main(
                [
                    "token-analysis",
                    "--chain",
                    "base",
                    "--contract",
                    TOKEN,
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
        analysis_factory.assert_not_called()
        activity_factory.assert_not_called()

    def test_schema_contains_analysis_and_preserves_all_activity_facts(
        self,
    ) -> None:
        facts = fixture_case("accumulation")
        original = deepcopy(facts)
        result = self.analyze(facts)
        for key, value in original.items():
            self.assertEqual(result[key], value)
        analysis = result["analysis"]
        self.assertEqual(analysis["schema_version"], 1)
        self.assertEqual(analysis["algorithm_version"], "oar-behavior-v1")
        self.assertEqual(
            analysis["wallet_group_algorithm_version"],
            "oar-wallet-group-v1",
        )
        self.assertEqual(
            analysis["score_semantics"], "rule_score_not_probability"
        )

    def test_partial_activity_keeps_top_level_partial_and_limits_analysis(
        self,
    ) -> None:
        facts = fixture_case("partial_input")
        result = self.analyze(facts)
        self.assertEqual(result["status"], "partial")
        self.assertFalse(result["complete"])
        self.assertEqual(result["analysis"]["status"], "partial_input")
        self.assertFalse(result["analysis"]["complete"])
        self.assertEqual(
            result["analysis"]["primary_behavior"]["type"],
            "insufficient_data",
        )
        self.assertTrue(
            all(
                group["score"] <= 39
                for group in result["analysis"]["wallet_groups"]
            )
        )

    def test_analysis_budget_does_not_change_activity_completeness(
        self,
    ) -> None:
        settings = replace(
            self.settings,
            oar_max_analyzed_wallets=3,
        )
        facts = fixture_case("wallet_consolidation")
        result = self.analyze(facts, settings=settings)
        self.assertTrue(result["complete"])
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["analysis"]["complete"])
        self.assertEqual(
            result["analysis"]["status"], "partial_analysis"
        )
        self.assertIn(
            "analysis_budget_exhausted",
            result["analysis"]["limitations"],
        )
        self.assertNotEqual(
            result["analysis"]["primary_behavior"]["confidence_level"],
            "high",
        )
        self.assertTrue(
            all(
                group["score"] <= 59
                for group in result["analysis"]["wallet_groups"]
            )
        )

    def test_no_price_still_produces_behavior(self) -> None:
        result = self.analyze(fixture_case("distribution"))
        self.assertEqual(
            result["analysis"]["primary_behavior"]["type"],
            "distribution_candidate",
        )
        self.assertEqual(
            result["analysis"]["valuation_basis"], "token_amount"
        )

    def test_output_contains_no_probability_field_or_control_claim(
        self,
    ) -> None:
        result = self.analyze(fixture_case("wallet_consolidation"))
        text = json.dumps(result, ensure_ascii=False)
        self.assertNotIn('"probability"', text)
        self.assertNotIn("已确认属于同一主力", text)
        self.assertNotIn("已确认同一机构控制", text)
        self.assertNotIn("钱包已合并", text)

    def test_service_does_not_create_store_notifier_or_telegram(self) -> None:
        facts = fixture_case("accumulation")
        with (
            patch(
                "paopao_radar.onchain_flow.db.OnchainStore",
                side_effect=AssertionError("database must not be created"),
            ) as store,
            patch(
                "paopao_radar.onchain_flow.notifier.OnchainNotifier",
                side_effect=AssertionError("notifier must not be created"),
            ) as notifier,
            patch(
                "paopao_radar.telegram.TelegramGateway",
                side_effect=AssertionError("telegram must not be created"),
            ) as telegram,
        ):
            result = self.analyze(facts)
        self.assertEqual(result["analysis"]["status"], "ok")
        store.assert_not_called()
        notifier.assert_not_called()
        telegram.assert_not_called()

    def test_token_activity_cli_output_remains_exactly_compatible(
        self,
    ) -> None:
        facts = fixture_case("isolated")

        class ActivityService:
            def execute(self, _query):
                return deepcopy(facts)

        output = StringIO()
        with (
            patch.object(
                TokenActivityQueryService,
                "from_settings",
                return_value=ActivityService(),
            ),
            redirect_stdout(output),
        ):
            code = main(
                [
                    "token-activity",
                    "--chain",
                    "base",
                    "--contract",
                    TOKEN,
                    "--window",
                    "1h",
                    "--allow-network",
                ],
                settings=self.settings,
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue()), facts)
        self.assertNotIn("analysis", json.loads(output.getvalue()))

    def test_token_analysis_cli_uses_shared_safe_output_file(self) -> None:
        facts = fixture_case("isolated")

        class AnalysisService:
            def execute(self, _query):
                result = deepcopy(facts)
                result["analysis"] = {"status": "ok", "complete": True}
                return result

        output_path = self.root / "reports" / "onchain" / "analysis.json"
        output_path.parent.mkdir(parents=True)
        output = StringIO()
        with (
            patch.object(
                TokenAnalysisService,
                "from_settings",
                return_value=AnalysisService(),
            ),
            redirect_stdout(output),
        ):
            code = main(
                [
                    "token-analysis",
                    "--chain",
                    "base",
                    "--contract",
                    TOKEN,
                    "--window",
                    "1h",
                    "--allow-network",
                    "--output-file",
                    str(output_path),
                ],
                settings=self.settings,
            )
        summary = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(summary["output_file"], str(output_path))
        self.assertEqual(
            json.loads(output_path.read_text(encoding="utf-8"))["analysis"],
            {"status": "ok", "complete": True},
        )

    def test_cli_returns_partial_exit_for_incomplete_analysis(self) -> None:
        facts = fixture_case("wallet_consolidation")
        result = self.analyze(
            facts,
            settings=replace(
                self.settings,
                oar_max_analyzed_wallets=3,
            ),
        )

        class AnalysisService:
            def execute(self, _query):
                return result

        output = StringIO()
        with (
            patch.object(
                TokenAnalysisService,
                "from_settings",
                return_value=AnalysisService(),
            ),
            redirect_stdout(output),
        ):
            code = main(
                [
                    "token-analysis",
                    "--chain",
                    "base",
                    "--contract",
                    TOKEN,
                    "--window",
                    "1h",
                    "--allow-network",
                ],
                settings=self.settings,
            )
        self.assertEqual(code, 2)
        self.assertEqual(
            json.loads(output.getvalue())["analysis"]["status"],
            "partial_analysis",
        )

    def test_analysis_output_does_not_expose_credentials_or_private_paths(
        self,
    ) -> None:
        settings = replace(
            self.settings,
            base_http_rpc_url="https://private.example/secret-key",
            tg_bot_token="telegram-secret",
        )
        result = self.analyze(
            fixture_case("accumulation"), settings=settings
        )
        text = json.dumps(result, ensure_ascii=False)
        for secret in (
            "private.example",
            "secret-key",
            "telegram-secret",
            str(settings.labels_path),
            "Authorization",
        ):
            self.assertNotIn(secret, text)

    def test_malformed_activity_fails_with_structured_query_error(self) -> None:
        facts = fixture_case("isolated")
        facts["query"]["to_time"] = "invalid"
        with self.assertRaisesRegex(ValueError, "analyzed safely") as raised:
            self.analyze(facts)
        self.assertEqual(raised.exception.code, "analysis_failed")


if __name__ == "__main__":
    unittest.main()
