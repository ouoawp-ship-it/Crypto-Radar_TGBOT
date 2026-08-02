from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from paopao_radar.onchain_flow.ai_client import OarAiError
from paopao_radar.onchain_flow.ai_context import build_ai_context
from paopao_radar.onchain_flow.constants import OAR_AI_PROMPT_VERSION
from paopao_radar.onchain_flow.report import TokenReportService
from paopao_radar.onchain_flow.token_activity import TokenActivityQuery
from paopao_radar.onchain_flow.token_analysis import TokenAnalysisService

from tests.onchain_flow.analysis_support import fixture_case
from tests.onchain_flow.support import make_settings


class StaticActivity:
    def __init__(self, payload: dict[str, object]):
        self.payload = payload

    def execute(self, query: object) -> dict[str, object]:
        del query
        return deepcopy(self.payload)


def analyzed_payload(
    settings: object,
    name: str,
) -> dict[str, object]:
    activity = fixture_case(name)
    return TokenAnalysisService(
        settings,
        StaticActivity(activity),
    ).execute(object())


class FakeAi:
    def __init__(self):
        self.calls = 0

    def analyze(
        self,
        context: dict[str, object],
        *,
        restricted_input: bool,
    ) -> dict[str, object]:
        del context, restricted_input
        self.calls += 1
        return {
            "schema_version": 1,
            "bias": "neutral",
            "confidence": "low",
            "primary_hypothesis": "链上活动仍需继续确认。",
            "alternative_hypotheses": [],
            "likely_next_actions": ["继续观察同方向事件是否延续。"],
            "watch_signals": [],
            "invalidation_conditions": [],
            "risk_notes": [],
        }


class ConfiguredAi:
    def __init__(self, result: dict[str, object]):
        self.result = deepcopy(result)
        self.calls = 0
        self.restricted_inputs: list[bool] = []

    def analyze(
        self,
        context: dict[str, object],
        *,
        restricted_input: bool,
    ) -> dict[str, object]:
        del context
        self.calls += 1
        self.restricted_inputs.append(restricted_input)
        return deepcopy(self.result)


def ai_output(
    *,
    bias: str = "neutral",
    confidence: str = "low",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "bias": bias,
        "confidence": confidence,
        "primary_hypothesis": "链上活动仍需继续确认。",
        "alternative_hypotheses": [],
        "likely_next_actions": [],
        "watch_signals": [],
        "invalidation_conditions": [],
        "risk_notes": [],
    }


class MemoryCache:
    def __init__(self):
        self.result: dict[str, object] | None = None
        self.reservations = 0
        self.get_prompt_versions: list[str] = []
        self.put_prompt_versions: list[str] = []
        self.get_cache_controls: list[dict[str, str]] = []
        self.put_cache_controls: list[dict[str, str]] = []

    def get(
        self,
        context_hash: str,
        model: str,
        prompt_version: str,
        **controls: str,
    ) -> object:
        del context_hash, model
        self.get_prompt_versions.append(prompt_version)
        self.get_cache_controls.append(dict(controls))
        return type("CacheResult", (), {"result": self.result})()

    def reserve_call(self) -> bool:
        self.reservations += 1
        return True

    def put(
        self,
        context_hash: str,
        model: str,
        prompt_version: str,
        result: dict[str, object],
        **controls: str,
    ) -> None:
        del context_hash, model
        self.put_prompt_versions.append(prompt_version)
        self.put_cache_controls.append(dict(controls))
        self.result = deepcopy(result)


class FailingAi:
    def analyze(self, context: object, *, restricted_input: bool) -> object:
        del context, restricted_input
        raise RuntimeError("provider exploded with secret")


class DiagnosticFailingAi:
    def analyze(self, context: object, *, restricted_input: bool) -> object:
        del context, restricted_input
        raise OarAiError(
            "ai_invalid_parameters",
            "provider message must not be returned",
            http_status=422,
            provider_error_code="invalid_parameter",
            provider_error_param="thinking",
            diagnostics={
                "request_body_chars": 4096,
                "http_attempts": 1,
            },
        )


class OarReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = make_settings(self.root)
        self.query = TokenActivityQuery.create(
            self.settings,
            chain="base",
            contract="0x9999999999999999999999999999999999999999",
            window="4h",
            max_events=None,
            max_rpc_requests=None,
            top_n=None,
            with_price=False,
            min_usd=None,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def ai_settings(self) -> object:
        return replace(
            self.settings,
            oar_ai_enable=True,
            oar_ai_base_url="https://ai.invalid/v1",
            oar_ai_api_key="configured-secret",
            oar_ai_model="fixture-model",
            oar_ai_cache_path=self.settings.data_dir / "ai.json",
        )

    def analyzed_case(self, name: str) -> dict[str, object]:
        return analyzed_payload(self.settings, name)

    def test_context_uses_only_whitelisted_bounded_facts(self) -> None:
        payload = analyzed_payload(self.settings, "accumulation")
        payload["rpc_url"] = "https://secret.invalid/key"
        payload["api_key"] = "secret"
        payload["private_labels_path"] = "C:/private/labels.csv"
        payload["largest_transfers"] = (
            list(payload["largest_transfers"]) * 8
        )
        analysis = payload["analysis"]
        analysis["wallet_groups"] = [
            {
                "group_id": f"group-{index}",
                "group_type": "shared_target",
                "score": index,
                "level": "弱关联",
                "wallets": [
                    f"0x{wallet:040x}" for wallet in range(30)
                ],
                "supporting_evidence": ["shared_target"],
                "counter_evidence": [],
                "limitations": [],
                "source_event_ids": [],
            }
            for index in range(15)
        ]
        context = build_ai_context(payload, max_chars=30000)
        serialized = json.dumps(context, ensure_ascii=False)
        self.assertNotIn("secret.invalid", serialized)
        self.assertNotIn("secret", serialized)
        self.assertNotIn("labels.csv", serialized)
        self.assertLessEqual(len(context["largest_transfers"]), 20)
        self.assertLessEqual(len(context["wallet_groups"]), 10)
        self.assertTrue(
            all(
                len(group["wallets"]) <= 20
                for group in context["wallet_groups"]
            )
        )

    def test_context_hash_is_stable_when_input_order_changes(self) -> None:
        payload = analyzed_payload(self.settings, "wallet_consolidation")
        reordered = deepcopy(payload)
        reordered["largest_transfers"] = list(
            reversed(reordered["largest_transfers"])
        )
        reordered["analysis"]["wallet_groups"] = list(
            reversed(reordered["analysis"]["wallet_groups"])
        )
        reordered["analysis"]["behavior_candidates"] = list(
            reversed(reordered["analysis"]["behavior_candidates"])
        )
        first = build_ai_context(payload, max_chars=30000)
        second = build_ai_context(reordered, max_chars=30000)
        self.assertEqual(first["context_hash"], second["context_hash"])
        self.assertEqual(first, second)

    def test_partial_context_has_explicit_limitations(self) -> None:
        payload = analyzed_payload(self.settings, "partial_input")
        context = build_ai_context(payload, max_chars=30000)
        self.assertFalse(context["query"]["complete"])
        self.assertIn("query_incomplete", context["data_limitations"])
        self.assertIn("analysis_incomplete", context["data_limitations"])

    def test_ai_closed_still_generates_complete_rule_report(self) -> None:
        analysis = TokenAnalysisService(
            self.settings,
            StaticActivity(fixture_case("distribution")),
        )
        result = TokenReportService(
            self.settings,
            analysis,
        ).execute(self.query, with_ai=False)
        report = result["report"]
        self.assertEqual(report["ai"]["status"], "not_requested")
        self.assertEqual(report["ai"]["calls"], 0)
        self.assertIn("流入交易所不等于已经卖出", report["rule_summary_text"])
        self.assertIn(
            "从交易所提出不等于已经买入",
            report["rule_summary_text"],
        )
        self.assertIn("高分不等于确认属于同一主力", report["rule_summary_text"])

    def test_with_ai_while_disabled_makes_no_call(self) -> None:
        fake = FakeAi()
        result = TokenReportService(
            self.settings,
            TokenAnalysisService(
                self.settings,
                StaticActivity(fixture_case("isolated")),
            ),
            ai_client=fake,
        ).execute(self.query, with_ai=True)
        self.assertEqual(result["report"]["ai"]["status"], "disabled")
        self.assertEqual(fake.calls, 0)

    def test_enabled_fake_ai_is_validated_and_cached(self) -> None:
        settings = replace(
            self.settings,
            oar_ai_enable=True,
            oar_ai_base_url="https://ai.invalid/v1",
            oar_ai_api_key="configured-secret",
            oar_ai_model="fixture-model",
            oar_ai_cache_path=self.settings.data_dir / "ai.json",
        )
        fake = FakeAi()
        cache = MemoryCache()
        service = TokenReportService(
            settings,
            TokenAnalysisService(
                settings,
                StaticActivity(fixture_case("isolated")),
            ),
            ai_client=fake,
            ai_cache=cache,
        )
        first = service.execute(self.query, with_ai=True)
        second = service.execute(self.query, with_ai=True)
        self.assertEqual(first["report"]["ai"]["status"], "available")
        self.assertEqual(second["report"]["ai"]["status"], "cached")
        self.assertEqual(
            first["report"]["content_hash"],
            second["report"]["content_hash"],
        )
        self.assertEqual(fake.calls, 1)
        self.assertEqual(cache.reservations, 1)
        self.assertEqual(
            cache.get_prompt_versions,
            [OAR_AI_PROMPT_VERSION, OAR_AI_PROMPT_VERSION],
        )
        self.assertEqual(
            cache.put_prompt_versions,
            [OAR_AI_PROMPT_VERSION],
        )

    def test_ai_failure_does_not_block_rule_summary(self) -> None:
        settings = replace(
            self.settings,
            oar_ai_enable=True,
            oar_ai_base_url="https://ai.invalid/v1",
            oar_ai_api_key="configured-secret",
            oar_ai_model="fixture-model",
            oar_ai_cache_path=self.settings.data_dir / "ai.json",
        )
        result = TokenReportService(
            settings,
            TokenAnalysisService(
                settings,
                StaticActivity(fixture_case("isolated")),
            ),
            ai_client=FailingAi(),
            ai_cache=MemoryCache(),
        ).execute(self.query, with_ai=True)
        self.assertEqual(result["report"]["ai"]["status"], "failed")
        self.assertEqual(
            result["report"]["ai"]["error"],
            "ai_client_error",
        )
        self.assertIn("规则分数", result["report"]["rule_summary_text"])
        self.assertNotIn(
            "configured-secret",
            json.dumps(result, ensure_ascii=False),
        )

    def test_ai_failure_exposes_only_safe_provider_diagnostics(self) -> None:
        settings = replace(
            self.settings,
            oar_ai_enable=True,
            oar_ai_base_url="https://ai.invalid/v1",
            oar_ai_api_key="configured-secret",
            oar_ai_model="fixture-model",
            oar_ai_cache_path=self.settings.data_dir / "ai.json",
        )
        result = TokenReportService(
            settings,
            TokenAnalysisService(
                settings,
                StaticActivity(fixture_case("isolated")),
            ),
            ai_client=DiagnosticFailingAi(),
            ai_cache=MemoryCache(),
        ).execute(self.query, with_ai=True)
        ai = result["report"]["ai"]
        self.assertEqual(ai["error"], "ai_invalid_parameters")
        self.assertEqual(ai["http_status"], 422)
        self.assertEqual(ai["provider_error_param"], "thinking")
        self.assertEqual(ai["diagnostics"]["http_attempts"], 1)
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("provider message", serialized)
        self.assertNotIn("configured-secret", serialized)

    def test_no_activity_does_not_invent_behavior(self) -> None:
        result = TokenReportService(
            self.settings,
            TokenAnalysisService(
                self.settings,
                StaticActivity(fixture_case("no_activity")),
            ),
        ).execute(self.query, with_ai=False)
        primary = result["report"]["rule_summary"]["primary_behavior"]
        self.assertEqual(primary["type"], "no_activity")
        self.assertEqual(primary["score"], 0)

    def test_price_absence_does_not_block_report(self) -> None:
        result = TokenReportService(
            self.settings,
            TokenAnalysisService(
                self.settings,
                StaticActivity(fixture_case("accumulation")),
            ),
        ).execute(self.query, with_ai=False)
        self.assertEqual(result["report"]["status"], "ok")
        self.assertEqual(result["analysis"]["valuation_basis"], "token_amount")

    def test_low_evidence_behaviors_reject_directional_high_ai(self) -> None:
        cases = ("no_activity", "isolated")
        for name in cases:
            with self.subTest(name=name):
                payload = self.analyzed_case(name)
                fake = ConfiguredAi(
                    ai_output(bias="bullish", confidence="high")
                )
                result = TokenReportService(
                    self.ai_settings(),
                    StaticActivity(payload),
                    ai_client=fake,
                    ai_cache=MemoryCache(),
                ).execute(self.query, with_ai=True)
                self.assertEqual(result["report"]["ai"]["status"], "invalid")
                self.assertEqual(fake.restricted_inputs, [True])

    def test_low_evidence_behaviors_accept_low_neutral_ai(self) -> None:
        for name, bias in (
            ("no_activity", "uncertain"),
            ("isolated", "neutral"),
        ):
            with self.subTest(name=name):
                fake = ConfiguredAi(
                    ai_output(bias=bias, confidence="low")
                )
                result = TokenReportService(
                    self.ai_settings(),
                    StaticActivity(self.analyzed_case(name)),
                    ai_client=fake,
                    ai_cache=MemoryCache(),
                ).execute(self.query, with_ai=True)
                self.assertEqual(
                    result["report"]["ai"]["status"],
                    "available",
                )
                self.assertEqual(fake.restricted_inputs, [True])

    def test_inconclusive_is_restricted_but_formal_behavior_is_not(self) -> None:
        inconclusive = self.analyzed_case("isolated")
        inconclusive["analysis"]["status"] = "insufficient_evidence"
        inconclusive["analysis"]["primary_behavior"]["type"] = (
            "inconclusive_activity"
        )
        restricted_ai = ConfiguredAi(
            ai_output(bias="bearish", confidence="high")
        )
        restricted = TokenReportService(
            self.ai_settings(),
            StaticActivity(inconclusive),
            ai_client=restricted_ai,
            ai_cache=MemoryCache(),
        ).execute(self.query, with_ai=True)
        self.assertEqual(restricted["report"]["ai"]["status"], "invalid")
        self.assertEqual(restricted_ai.restricted_inputs, [True])

        formal_ai = ConfiguredAi(
            ai_output(bias="bullish", confidence="high")
        )
        formal = TokenReportService(
            self.ai_settings(),
            StaticActivity(self.analyzed_case("accumulation")),
            ai_client=formal_ai,
            ai_cache=MemoryCache(),
        ).execute(self.query, with_ai=True)
        self.assertEqual(formal["report"]["ai"]["status"], "available")
        self.assertEqual(formal_ai.restricted_inputs, [False])

    def test_insufficient_cex_coverage_forces_cautious_ai(self) -> None:
        payload = self.analyzed_case("accumulation")
        payload["labels"] = {
            "status": "insufficient_cex_coverage",
            "identity_label_count": 0,
            "classification_eligible_cex_count": 0,
        }
        fake = ConfiguredAi(
            ai_output(bias="bullish", confidence="high")
        )

        result = TokenReportService(
            self.ai_settings(),
            StaticActivity(payload),
            ai_client=fake,
            ai_cache=MemoryCache(),
        ).execute(self.query, with_ai=True)

        ai = result["report"]["ai"]
        self.assertEqual(ai["status"], "invalid")
        self.assertTrue(ai["restricted_input"])
        self.assertEqual(
            ai["restriction_reasons"], ["insufficient_cex_coverage"]
        )
        self.assertIn(
            "insufficient_cex_coverage",
            result["report"]["ai_context"]["data_limitations"],
        )
        self.assertEqual(fake.restricted_inputs, [True])

    def test_unstructured_linked_market_direction_forces_cautious_ai(self) -> None:
        payload = self.analyzed_case("accumulation")
        fake = ConfiguredAi(
            ai_output(bias="bearish", confidence="high")
        )
        service = TokenReportService(
            self.ai_settings(),
            StaticActivity(payload),
            ai_client=fake,
            ai_cache=MemoryCache(),
        )

        result = service.build_from_analysis(
            payload,
            with_ai=True,
            linked_market_signals=[
                {
                    "public_ref": "funding:fixture",
                    "module": "funding",
                    "symbol": "AAAUSDT",
                    "score": 80,
                    "summary": "must-not-be-treated-as-direction",
                }
            ],
        )

        ai = result["report"]["ai"]
        self.assertEqual(ai["status"], "invalid")
        self.assertEqual(
            ai["restriction_reasons"],
            ["market_direction_not_structured"],
        )
        self.assertEqual(fake.restricted_inputs, [True])

    def test_restricted_input_rejects_stale_richer_cache(self) -> None:
        cache = MemoryCache()
        cache.result = ai_output(bias="bullish", confidence="high")
        fake = ConfiguredAi(
            ai_output(bias="uncertain", confidence="low")
        )
        result = TokenReportService(
            self.ai_settings(),
            StaticActivity(self.analyzed_case("no_activity")),
            ai_client=fake,
            ai_cache=cache,
        ).execute(self.query, with_ai=True)
        self.assertEqual(result["report"]["ai"]["status"], "available")
        self.assertEqual(fake.calls, 1)
        self.assertEqual(cache.reservations, 1)


if __name__ == "__main__":
    unittest.main()
