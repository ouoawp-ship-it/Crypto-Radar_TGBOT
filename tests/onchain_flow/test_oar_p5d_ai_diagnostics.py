from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from paopao_radar.onchain_flow.ai_client import (
    OarAiError,
    OpenAiCompatibleOarClient,
    build_ai_request_diagnostics,
)
from paopao_radar.onchain_flow.cli import (
    _ai_network_command,
    _ai_request_check,
)
from tests.onchain_flow.support import make_settings
from tests.onchain_flow.test_oar_ai_client import (
    FakeResponse,
    FakeSession,
    envelope,
    valid_output,
)


class DeepSeekHttpDiagnosticsTests(unittest.TestCase):
    def client(
        self,
        response: FakeResponse,
    ) -> OpenAiCompatibleOarClient:
        return OpenAiCompatibleOarClient(
            base_url="https://provider.invalid/v1",
            api_key="private-api-key",
            model="deepseek-v4-pro",
            timeout_sec=60,
            max_retries=0,
            max_output_chars=8000,
            provider="deepseek",
            thinking_mode="enabled",
            session=FakeSession([response]),
        )

    def test_non_success_http_statuses_are_classified_exactly(self) -> None:
        cases = {
            400: "ai_invalid_request",
            402: "ai_insufficient_balance",
            404: "ai_endpoint_not_found",
            422: "ai_invalid_parameters",
            418: "ai_http_error",
            429: "ai_rate_limited",
            500: "ai_provider",
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                response = FakeResponse(
                    status,
                    {
                        "error": {
                            "message": "provider secret explanation",
                            "type": "invalid_request_error",
                            "code": "bad.parameter",
                            "param": "thinking",
                        }
                    },
                )
                with self.assertRaises(OarAiError) as caught:
                    self.client(response).analyze(
                        {"fact": 1},
                        restricted_input=False,
                    )
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(caught.exception.http_status, status)
                self.assertEqual(
                    caught.exception.provider_error_type,
                    "invalid_request_error",
                )
                public = json.dumps(
                    caught.exception.public_details(),
                    ensure_ascii=False,
                )
                self.assertNotIn("provider secret explanation", public)
                self.assertNotIn("private-api-key", public)
                self.assertNotIn("provider.invalid", public)

    def test_unsafe_provider_fields_and_message_are_discarded(self) -> None:
        response = FakeResponse(
            400,
            {
                "error": {
                    "message": "do not persist this message",
                    "type": "unsafe field with spaces",
                    "code": 1002,
                    "param": "x" * 81,
                }
            },
        )
        with self.assertRaises(OarAiError) as caught:
            self.client(response).analyze({}, restricted_input=False)
        details = caught.exception.public_details()
        self.assertNotIn("provider_error_type", details)
        self.assertEqual(details["provider_error_code"], 1002)
        self.assertNotIn("provider_error_param", details)
        self.assertNotIn("do not persist", str(caught.exception))
        self.assertNotIn("do not persist", json.dumps(details))

    def test_finish_reason_and_content_failures_are_distinct(self) -> None:
        cases = (
            (
                {
                    "choices": [{
                        "finish_reason": "length",
                        "message": {"content": "{}"},
                    }]
                },
                "ai_output_truncated",
            ),
            (
                {
                    "choices": [{
                        "finish_reason": "stop",
                        "message": {"content": None},
                    }]
                },
                "ai_empty_content",
            ),
            (
                {
                    "choices": [{
                        "finish_reason": "stop",
                        "message": {"content": ""},
                    }]
                },
                "ai_empty_content",
            ),
            (
                {
                    "choices": [{
                        "finish_reason": "stop",
                        "message": {"content": "{not-json"},
                    }]
                },
                "invalid_ai_output",
            ),
            (
                {
                    "choices": [{
                        "finish_reason": "stop",
                        "message": {},
                    }]
                },
                "invalid_ai_output",
            ),
        )
        for payload, expected in cases:
            with self.subTest(expected=expected):
                with self.assertRaises(OarAiError) as caught:
                    self.client(FakeResponse(200, payload)).analyze(
                        {},
                        restricted_input=False,
                    )
                self.assertEqual(caught.exception.code, expected)

    def test_success_diagnostics_are_content_free(self) -> None:
        client = self.client(
            FakeResponse(200, envelope(valid_output()))
        )
        context = {"private_context": "DO_NOT_EXPOSE_CONTEXT"}
        client.operator_prompt = "DO_NOT_EXPOSE_PROMPT"
        result = client.analyze(context, restricted_input=False)
        self.assertEqual(result["schema_version"], 1)
        serialized = json.dumps(
            client.last_diagnostics,
            ensure_ascii=False,
        )
        for forbidden in (
            "DO_NOT_EXPOSE_CONTEXT",
            "DO_NOT_EXPOSE_PROMPT",
            "private-api-key",
            "provider.invalid",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(client.last_diagnostics["http_attempts"], 1)
        self.assertEqual(client.last_diagnostics["http_status"], 200)
        self.assertIn("request_body_chars", client.last_diagnostics)

    def test_request_diagnostics_only_expose_sizes_and_controls(self) -> None:
        diagnostics = build_ai_request_diagnostics(
            {"secret_fact": "DO_NOT_EXPOSE_CONTEXT"},
            True,
            "deepseek-v4-pro",
            provider="deepseek",
            operator_prompt="DO_NOT_EXPOSE_PROMPT",
            operator_prompt_hash="safe-hash",
            thinking_mode="enabled",
            reasoning_effort="high",
            max_tokens=8192,
            timeout_sec=60,
        )
        serialized = json.dumps(diagnostics, ensure_ascii=False)
        self.assertTrue(diagnostics["restricted_input"])
        self.assertGreater(diagnostics["operator_prompt_chars"], 0)
        self.assertGreater(diagnostics["ai_context_chars"], 0)
        self.assertGreater(diagnostics["request_body_chars"], 0)
        self.assertNotIn("DO_NOT_EXPOSE_CONTEXT", serialized)
        self.assertNotIn("DO_NOT_EXPOSE_PROMPT", serialized)
        self.assertNotIn("safe-hash", serialized)


class AiDiagnosticCliTests(unittest.TestCase):
    def test_ai_failure_json_has_safe_details_without_message(self) -> None:
        error = OarAiError(
            "ai_invalid_parameters",
            "provider message must stay private",
            http_status=422,
            provider_error_type="invalid_request_error",
            provider_error_param="thinking",
            diagnostics={
                "request_body_chars": 1234,
                "http_attempts": 1,
            },
        )
        fake_client = SimpleNamespace(
            analyze=lambda *args, **kwargs: (_ for _ in ()).throw(error)
        )
        prompt = SimpleNamespace(content="private prompt", prompt_hash="hash")
        settings = make_settings(
            Path(tempfile.mkdtemp()),
            oar_ai_api_key="configured",
        )
        output = StringIO()
        with (
            patch(
                "paopao_radar.onchain_flow.cli._ai_client",
                return_value=fake_client,
            ),
            patch(
                "paopao_radar.onchain_flow.cli.OperatorPromptManager."
                "from_settings"
            ) as manager,
            redirect_stdout(output),
        ):
            manager.return_value.load_for_request.return_value = prompt
            code = _ai_network_command(
                settings,
                SimpleNamespace(
                    command="ai-smoke",
                    allow_network=True,
                ),
            )
        payload = json.loads(output.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(payload["error"], "ai_invalid_parameters")
        self.assertEqual(payload["http_status"], 422)
        self.assertEqual(payload["provider_error_param"], "thinking")
        serialized = json.dumps(payload)
        self.assertNotIn("provider message", serialized)
        self.assertNotIn("private prompt", serialized)

    def test_ai_request_check_builds_real_context_without_ai_or_secrets(
        self,
    ) -> None:
        analyzed = {
            "status": "ok",
            "complete": True,
            "analysis": {"status": "ok", "complete": True},
            "diagnostics": {"rpc_request_count": 17},
        }
        analysis_service = SimpleNamespace(execute=lambda query: analyzed)
        prompt = SimpleNamespace(
            content="DO_NOT_EXPOSE_PROMPT",
            prompt_hash="prompt-hash",
        )
        settings = make_settings(Path(tempfile.mkdtemp()))
        with (
            patch(
                "paopao_radar.onchain_flow.cli.TokenAnalysisService."
                "from_settings",
                return_value=analysis_service,
            ),
            patch(
                "paopao_radar.onchain_flow.cli.build_ai_context",
                return_value={
                    "context_hash": "context-hash",
                    "fact": "DO_NOT_EXPOSE_CONTEXT",
                },
            ),
            patch(
                "paopao_radar.onchain_flow.cli.OperatorPromptManager."
                "from_settings"
            ) as manager,
        ):
            manager.return_value.load_for_request.return_value = prompt
            code, payload = _ai_request_check(settings, object())
        self.assertEqual(code, 0)
        self.assertEqual(payload["ai_calls"], 0)
        self.assertEqual(payload["telegram_calls"], 0)
        self.assertEqual(payload["rpc_calls"], 17)
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("DO_NOT_EXPOSE_PROMPT", serialized)
        self.assertNotIn("DO_NOT_EXPOSE_CONTEXT", serialized)
        self.assertNotIn(settings.oar_ai_base_url, serialized)


if __name__ == "__main__":
    unittest.main()
