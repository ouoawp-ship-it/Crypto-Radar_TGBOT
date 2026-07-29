from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import requests

from paopao_radar.onchain_flow.ai_client import (
    OarAiCache,
    OarAiError,
    OpenAiCompatibleOarClient,
    validate_ai_output,
)


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
            partial_input=False,
        )
        self.assertEqual(result["bias"], "neutral")
        self.assertFalse(session.calls[0]["allow_redirects"])
        self.assertEqual(session.calls[0]["timeout"], 2)

    def test_unknown_field_and_invalid_enum_are_rejected(self) -> None:
        extra = valid_output()
        extra["facts"] = {"transfer_count": 999}
        with self.assertRaises(OarAiError):
            validate_ai_output(extra, partial_input=False)
        invalid = valid_output(bias="certain")
        with self.assertRaises(OarAiError):
            validate_ai_output(invalid, partial_input=False)

    def test_array_length_and_markdown_are_rejected(self) -> None:
        oversized = valid_output()
        oversized["watch_signals"] = ["x"] * 6
        with self.assertRaises(OarAiError):
            validate_ai_output(oversized, partial_input=False)
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
            self.client(session).analyze({}, partial_input=False)

    def test_partial_input_forces_low_neutral_or_uncertain(self) -> None:
        with self.assertRaises(OarAiError):
            validate_ai_output(
                valid_output(bias="bullish", confidence="high"),
                partial_input=True,
            )
        accepted = validate_ai_output(
            valid_output(bias="uncertain", confidence="low"),
            partial_input=True,
        )
        self.assertEqual(accepted["confidence"], "low")

    def test_prohibited_trading_and_identity_claims_are_rejected(self) -> None:
        for phrase in ("目标价 2 美元", "建议开多", "已确认同一主力"):
            payload = valid_output()
            payload["primary_hypothesis"] = phrase
            with self.subTest(phrase=phrase):
                with self.assertRaises(OarAiError):
                    validate_ai_output(payload, partial_input=False)

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
        ).analyze({}, partial_input=False)
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
                partial_input=False,
            )
        self.assertEqual(caught.exception.code, "ai_timeout")
        self.assertNotIn("top-secret-key", str(caught.exception))
        self.assertEqual(len(session.calls), 2)

    def test_redirect_is_not_followed(self) -> None:
        session = FakeSession([FakeResponse(302, {})])
        with self.assertRaises(OarAiError) as caught:
            self.client(session).analyze({}, partial_input=False)
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
            cache.put("context-hash", "model", valid_output())
            hit = cache.get("context-hash", "model")
            self.assertEqual(hit.status, "hit")
            text = (root / "oar_ai_cache.json").read_text(
                encoding="utf-8"
            )
            self.assertNotIn("top-secret-key", text)
            self.assertNotIn("Authorization", text)
            self.assertNotIn("messages", text)
            now["value"] += 4000
            self.assertEqual(
                cache.get("context-hash", "model").status,
                "miss",
            )
            self.assertTrue(cache.reserve_call())


if __name__ == "__main__":
    unittest.main()
