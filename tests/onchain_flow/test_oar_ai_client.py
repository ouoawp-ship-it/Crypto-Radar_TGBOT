from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import requests

from paopao_radar.onchain_flow.ai_client import (
    AI_OUTPUT_KEYS,
    OarAiCache,
    OarAiError,
    OpenAiCompatibleOarClient,
    build_ai_request_body,
    validate_ai_output,
)
from paopao_radar.onchain_flow.constants import OAR_AI_PROMPT_VERSION
from paopao_radar.storage import JsonStore


def valid_output(
    *,
    bias: str = "neutral",
    confidence: str = "low",
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "bias": bias,
        "confidence": confidence,
        "primary_hypothesis": "需要继续观察链上事件。",
        "alternative_hypotheses": [],
        "likely_next_actions": [],
        "watch_signals": [],
        "invalidation_conditions": [],
        "risk_notes": [],
    }


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload: object | None = None,
    ):
        self.status_code = status_code
        self.payload = payload

    def json(self) -> object:
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeSession:
    def __init__(self, responses: list[object]):
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def envelope(output: dict[str, object]) -> dict[str, object]:
    return {
        "choices": [
            {"message": {"content": json.dumps(output, ensure_ascii=False)}}
        ]
    }


class OarAiClientTests(unittest.TestCase):
    def client(
        self,
        session: FakeSession,
        *,
        retries: int = 1,
        sleep: list[float] | None = None,
    ) -> OpenAiCompatibleOarClient:
        return OpenAiCompatibleOarClient(
            base_url="https://ai.invalid/v1",
            api_key="top-secret-key",
            model="test-model",
            timeout_sec=2,
            max_retries=retries,
            max_output_chars=8000,
            session=session,
            sleep=(sleep.append if sleep is not None else lambda _: None),
        )

    def test_valid_strict_json_is_accepted(self) -> None:
        session = FakeSession([FakeResponse(200, envelope(valid_output()))])
        result = self.client(session).analyze(
            {"schema_version": 1},
            restricted_input=False,
        )
        self.assertEqual(result["bias"], "neutral")
        self.assertFalse(session.calls[0]["allow_redirects"])
        self.assertEqual(session.calls[0]["timeout"], 2)
        request_body = session.calls[0]["json"]
        self.assertNotIn(
            "top-secret-key",
            json.dumps(request_body, ensure_ascii=False),
        )

    def test_unknown_field_and_invalid_enum_are_rejected(self) -> None:
        missing = valid_output()
        missing.pop("risk_notes")
        with self.assertRaises(OarAiError):
            validate_ai_output(missing, restricted_input=False)
        extra = valid_output()
        extra["facts"] = {"transfer_count": 999}
        with self.assertRaises(OarAiError):
            validate_ai_output(extra, restricted_input=False)
        invalid = valid_output(bias="certain")
        with self.assertRaises(OarAiError):
            validate_ai_output(invalid, restricted_input=False)

    def test_provider_missing_or_extra_fields_are_rejected(self) -> None:
        missing = valid_output()
        missing.pop("risk_notes")
        extra = valid_output()
        extra["unexpected"] = "not allowed"
        for payload in (missing, extra):
            with self.subTest(keys=sorted(payload)):
                session = FakeSession(
                    [FakeResponse(200, envelope(payload))]
                )
                with self.assertRaises(OarAiError) as caught:
                    self.client(session).analyze(
                        {},
                        restricted_input=False,
                    )
                self.assertEqual(
                    caught.exception.code,
                    "invalid_ai_output",
                )

    def test_request_body_exposes_strict_contract_and_controls(self) -> None:
        body = build_ai_request_body(
            {"z_fact": 2, "a_fact": 1},
            True,
            "fixture-model",
        )
        system_prompt = body["messages"][0]["content"]
        for field in sorted(AI_OUTPUT_KEYS):
            with self.subTest(field=field):
                self.assertIn(field, system_prompt)
        self.assertIn('"additionalProperties":false', system_prompt)
        self.assertIn('"maxItems":5', system_prompt)
        self.assertIn("Markdown code fence", system_prompt)
        self.assertIn("neutral 或 uncertain", system_prompt)
        self.assertIn("confidence 必须为 low", system_prompt)
        envelope = json.loads(body["messages"][1]["content"])
        self.assertEqual(
            envelope["control"]["prompt_version"],
            OAR_AI_PROMPT_VERSION,
        )
        self.assertTrue(envelope["control"]["restricted_input"])
        self.assertEqual(
            envelope["facts"],
            {"a_fact": 1, "z_fact": 2},
        )
        self.assertEqual(
            body["response_format"],
            {"type": "json_object"},
        )

    def test_request_body_is_deterministic_and_has_no_credentials(self) -> None:
        first = build_ai_request_body(
            {"z_fact": 2, "a_fact": 1},
            False,
            "fixture-model",
        )
        second = build_ai_request_body(
            {"a_fact": 1, "z_fact": 2},
            False,
            "fixture-model",
        )
        self.assertEqual(first, second)
        envelope = json.loads(first["messages"][1]["content"])
        self.assertFalse(envelope["control"]["restricted_input"])
        serialized = json.dumps(first, ensure_ascii=False)
        for secret in (
            "top-secret-key",
            "telegram-bot-token",
            "https://private-rpc.invalid",
            "Authorization",
        ):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, serialized)

    def test_array_length_and_markdown_are_rejected(self) -> None:
        oversized = valid_output()
        oversized["watch_signals"] = ["x"] * 6
        with self.assertRaises(OarAiError):
            validate_ai_output(oversized, restricted_input=False)
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    {
                        "choices": [
                            {"message": {"content": "```json\n{}\n```"}}
                        ]
                    },
                )
            ]
        )
        with self.assertRaises(OarAiError):
            self.client(session).analyze({}, restricted_input=False)

    def test_partial_input_forces_low_neutral_or_uncertain(self) -> None:
        with self.assertRaises(OarAiError):
            validate_ai_output(
                valid_output(bias="bullish", confidence="high"),
                restricted_input=True,
            )
        accepted = validate_ai_output(
            valid_output(bias="uncertain", confidence="low"),
            restricted_input=True,
        )
        self.assertEqual(accepted["confidence"], "low")

    def test_prohibited_trading_and_identity_claims_are_rejected(self) -> None:
        for phrase in ("目标价 2 美元", "建议开多", "已确认同一主力"):
            payload = valid_output()
            payload["primary_hypothesis"] = phrase
            with self.subTest(phrase=phrase):
                with self.assertRaises(OarAiError):
                    validate_ai_output(payload, restricted_input=False)

    def test_429_and_5xx_retry_is_bounded(self) -> None:
        sleeps: list[float] = []
        session = FakeSession(
            [
                FakeResponse(429, {}),
                FakeResponse(503, {}),
                FakeResponse(200, envelope(valid_output())),
            ]
        )
        result = self.client(
            session,
            retries=2,
            sleep=sleeps,
        ).analyze({}, restricted_input=False)
        self.assertEqual(result["bias"], "neutral")
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(sleeps, [1, 2])

    def test_timeout_retry_is_bounded_and_secret_is_redacted(self) -> None:
        session = FakeSession(
            [requests.Timeout("top-secret-key")] * 2
        )
        with self.assertRaises(OarAiError) as caught:
            self.client(session, retries=1).analyze(
                {},
                restricted_input=False,
            )
        self.assertEqual(caught.exception.code, "ai_timeout")
        self.assertNotIn("top-secret-key", str(caught.exception))
        self.assertEqual(len(session.calls), 2)

    def test_redirect_is_not_followed(self) -> None:
        session = FakeSession([FakeResponse(302, {})])
        with self.assertRaises(OarAiError) as caught:
            self.client(session).analyze({}, restricted_input=False)
        self.assertEqual(caught.exception.code, "ai_redirect_rejected")
        self.assertEqual(len(session.calls), 1)

    def test_cache_and_hourly_budget_are_bounded_without_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            now = {"value": 1000}
            cache = OarAiCache(
                path=root / "oar_ai_cache.json",
                data_dir=root,
                ttl_sec=3600,
                max_calls_per_hour=1,
                now=lambda: now["value"],
            )
            self.assertTrue(cache.reserve_call())
            self.assertFalse(cache.reserve_call())
            cache.put(
                "context-hash",
                "model",
                OAR_AI_PROMPT_VERSION,
                valid_output(),
            )
            hit = cache.get(
                "context-hash",
                "model",
                OAR_AI_PROMPT_VERSION,
            )
            self.assertEqual(hit.status, "hit")
            text = (root / "oar_ai_cache.json").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("top-secret-key", text)
            self.assertNotIn("Authorization", text)
            self.assertNotIn("messages", text)
            self.assertNotIn("完整输出契约", text)
            self.assertIn(OAR_AI_PROMPT_VERSION, text)
            now["value"] += 4000
            self.assertEqual(
                cache.get(
                    "context-hash",
                    "model",
                    OAR_AI_PROMPT_VERSION,
                ).status,
                "miss",
            )
            self.assertTrue(cache.reserve_call())

    def test_cache_misses_legacy_and_other_prompt_versions(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root / "oar_ai_cache.json"
            cache = OarAiCache(
                path=path,
                data_dir=root,
                ttl_sec=3600,
                max_calls_per_hour=10,
                now=lambda: 1000,
            )
            legacy_key = (
                f"model:{OAR_AI_PROMPT_VERSION}:legacy-context"
            )
            JsonStore(root).save(path, {
                "schema_version": 1,
                "entries": {
                    legacy_key: {
                        "context_hash": "legacy-context",
                        "model": "model",
                        "result": valid_output(),
                        "expires_at": 2000,
                    }
                },
            })
            self.assertEqual(
                cache.get(
                    "legacy-context",
                    "model",
                    OAR_AI_PROMPT_VERSION,
                ).status,
                "miss",
            )

            cache.put(
                "versioned-context",
                "model",
                "older-prompt-version",
                valid_output(),
            )
            self.assertEqual(
                cache.get(
                    "versioned-context",
                    "model",
                    OAR_AI_PROMPT_VERSION,
                ).status,
                "miss",
            )
            cache.put(
                "versioned-context",
                "model",
                OAR_AI_PROMPT_VERSION,
                valid_output(),
            )
            self.assertEqual(
                cache.get(
                    "versioned-context",
                    "model",
                    OAR_AI_PROMPT_VERSION,
                ).status,
                "hit",
            )


if __name__ == "__main__":
    unittest.main()
