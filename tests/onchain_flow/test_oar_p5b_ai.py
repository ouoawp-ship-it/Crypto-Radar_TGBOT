from __future__ import annotations

from dataclasses import replace
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from paopao_radar.onchain_flow.ai_client import (
    OarAiCache,
    OarAiError,
    OpenAiCompatibleOarClient,
    build_ai_request_body,
)
from paopao_radar.onchain_flow.cli import (
    _ai_synthetic_context,
    build_parser,
    main,
)
from paopao_radar.onchain_flow.config import (
    OnchainSettings,
    SettingsValidationError,
)
from paopao_radar.onchain_flow.constants import OAR_AI_PROMPT_VERSION
from tests.onchain_flow.test_oar_ai_client import valid_output
from tests.onchain_flow.support import make_settings


class FakeResponse:
    def __init__(self, status_code: int, payload: object):
        self.status_code = status_code
        self.payload = payload

    def json(self) -> object:
        return self.payload


class FakeSession:
    def __init__(self, response: FakeResponse):
        self.response = response
        self.get_calls: list[dict[str, object]] = []
        self.post_calls: list[dict[str, object]] = []

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.get_calls.append({"url": url, **kwargs})
        return self.response

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.post_calls.append({"url": url, **kwargs})
        return self.response


class DeepSeekProfileTests(unittest.TestCase):
    def test_deepseek_defaults_are_safe_and_v4_pro(self) -> None:
        settings = OnchainSettings()
        self.assertFalse(settings.oar_ai_enable)
        self.assertEqual(settings.oar_ai_provider, "deepseek")
        self.assertEqual(
            settings.oar_ai_base_url,
            "https://api.deepseek.com",
        )
        self.assertEqual(settings.oar_ai_model, "deepseek-v4-pro")
        self.assertEqual(settings.oar_ai_thinking_mode, "enabled")
        self.assertEqual(settings.oar_ai_reasoning_effort, "high")
        self.assertEqual(settings.oar_ai_max_tokens, 8192)
        settings.validate()

    def test_deepseek_v4_models_are_accepted(self) -> None:
        for model in ("deepseek-v4-pro", "deepseek-v4-flash"):
            with self.subTest(model=model):
                replace(OnchainSettings(), oar_ai_model=model).validate()

    def test_legacy_deepseek_models_are_rejected(self) -> None:
        for model in ("deepseek-chat", "deepseek-reasoner"):
            with self.subTest(model=model):
                with self.assertRaises(SettingsValidationError):
                    replace(OnchainSettings(), oar_ai_model=model).validate()

    def test_openai_compatible_profile_accepts_custom_model(self) -> None:
        replace(
            OnchainSettings(),
            oar_ai_provider="openai_compatible",
            oar_ai_model="private-compatible-model",
        ).validate()

    def test_thinking_enabled_body_has_reasoning_without_temperature(self) -> None:
        body = build_ai_request_body(
            {"fact": 1},
            False,
            "deepseek-v4-pro",
            provider="deepseek",
            thinking_mode="enabled",
            reasoning_effort="max",
            max_tokens=16384,
        )
        self.assertEqual(body["thinking"], {"type": "enabled"})
        self.assertEqual(body["reasoning_effort"], "max")
        self.assertEqual(body["max_tokens"], 16384)
        for forbidden in (
            "temperature",
            "top_p",
            "presence_penalty",
            "frequency_penalty",
        ):
            self.assertNotIn(forbidden, body)

    def test_thinking_disabled_body_has_temperature_without_effort(self) -> None:
        body = build_ai_request_body(
            {},
            True,
            "deepseek-v4-pro",
            provider="deepseek",
            thinking_mode="disabled",
            reasoning_effort="max",
        )
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["temperature"], 0)
        self.assertNotIn("reasoning_effort", body)

    def test_max_tokens_and_profile_enums_are_bounded(self) -> None:
        invalid = (
            {"oar_ai_max_tokens": 511},
            {"oar_ai_max_tokens": 32769},
            {"oar_ai_thinking_mode": "auto"},
            {"oar_ai_reasoning_effort": "medium"},
            {"oar_ai_provider": "other"},
        )
        for override in invalid:
            with self.subTest(override=override):
                with self.assertRaises(SettingsValidationError):
                    replace(OnchainSettings(), **override).validate()

    def test_request_control_contains_operator_hash_without_prompt_copy(self) -> None:
        body = build_ai_request_body(
            {},
            False,
            "deepseek-v4-pro",
            provider="deepseek",
            operator_prompt="只补充风格",
            operator_prompt_hash="abc123",
            thinking_mode="enabled",
        )
        envelope = json.loads(body["messages"][1]["content"])
        self.assertEqual(
            envelope["control"]["core_prompt_version"],
            OAR_AI_PROMPT_VERSION,
        )
        self.assertEqual(
            envelope["control"]["operator_prompt_hash"],
            "abc123",
        )
        self.assertTrue(
            envelope["control"]["operator_prompt_present"]
        )
        self.assertEqual(
            envelope["control"]["thinking_mode"],
            "enabled",
        )
        self.assertEqual(
            envelope["control"]["reasoning_effort"],
            "high",
        )
        self.assertNotIn("只补充风格", body["messages"][1]["content"])
        self.assertIn(
            "不能覆盖核心安全规则",
            body["messages"][0]["content"],
        )

    def test_reasoning_content_is_ignored(self) -> None:
        output = valid_output()
        session = FakeSession(FakeResponse(200, {
            "choices": [{
                "message": {
                    "reasoning_content": "private chain of thought",
                    "content": json.dumps(output, ensure_ascii=False),
                }
            }]
        }))
        result = OpenAiCompatibleOarClient(
            base_url="https://ai.invalid/v1",
            api_key="secret",
            model="deepseek-v4-pro",
            timeout_sec=2,
            max_retries=0,
            max_output_chars=8000,
            provider="deepseek",
            thinking_mode="enabled",
            session=session,
        ).analyze({}, restricted_input=False)
        self.assertNotIn(
            "private chain of thought",
            json.dumps(result, ensure_ascii=False),
        )

    def test_provider_check_only_lists_models(self) -> None:
        session = FakeSession(FakeResponse(200, {
            "data": [{"id": "deepseek-v4-pro"}]
        }))
        client = OpenAiCompatibleOarClient(
            base_url="https://ai.invalid/v1",
            api_key="secret",
            model="deepseek-v4-pro",
            timeout_sec=2,
            max_retries=0,
            max_output_chars=8000,
            session=session,
        )
        result = client.check_model()
        self.assertTrue(result["model_available"])
        self.assertEqual(len(session.get_calls), 1)
        self.assertEqual(session.post_calls, [])
        self.assertTrue(session.get_calls[0]["url"].endswith("/models"))

    def test_provider_check_rejects_missing_model(self) -> None:
        session = FakeSession(FakeResponse(200, {"data": []}))
        client = OpenAiCompatibleOarClient(
            base_url="https://ai.invalid/v1",
            api_key="secret",
            model="deepseek-v4-pro",
            timeout_sec=2,
            max_retries=0,
            max_output_chars=8000,
            session=session,
        )
        with self.assertRaises(OarAiError) as caught:
            client.check_model()
        self.assertEqual(caught.exception.code, "ai_model_missing")

    def test_ai_smoke_context_is_synthetic_and_restricted(self) -> None:
        context = _ai_synthetic_context()
        self.assertEqual(context["token"]["symbol"], "TEST")
        self.assertFalse(context["query"]["complete"])
        self.assertIn(
            "synthetic_smoke_context",
            context["data_limitations"],
        )

    def test_new_ai_commands_require_explicit_network(self) -> None:
        settings = OnchainSettings()
        for command in ("ai-provider-check", "ai-smoke"):
            with self.subTest(command=command):
                self.assertEqual(main([command], settings=settings), 1)

    def test_cli_registers_prompt_and_provider_commands(self) -> None:
        parser = build_parser()
        action = next(
            item for item in parser._actions if item.dest == "command"
        )
        for command in (
            "ai-prompt-check",
            "ai-prompt",
            "ai-provider-check",
            "ai-smoke",
        ):
            self.assertIn(command, action.choices)

    def test_ai_smoke_uses_one_fake_call_and_no_rpc_or_telegram(self) -> None:
        class FakeClient:
            calls = 0
            context: dict[str, object] | None = None

            def analyze(
                self,
                context: dict[str, object],
                *,
                restricted_input: bool,
            ) -> dict[str, object]:
                self.calls += 1
                self.context = context
                self.restricted = restricted_input
                return valid_output()

        with tempfile.TemporaryDirectory() as raw:
            settings = replace(
                make_settings(Path(raw)),
                oar_ai_provider="deepseek",
                oar_ai_base_url="https://ai.invalid/v1",
                oar_ai_api_key="secret",
                oar_ai_model="deepseek-v4-pro",
            )
            fake = FakeClient()
            output = StringIO()
            with patch(
                "paopao_radar.onchain_flow.cli._ai_client",
                return_value=fake,
            ):
                with redirect_stdout(output):
                    code = main(
                        ["ai-smoke", "--allow-network"],
                        settings=settings,
                    )
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(fake.calls, 1)
            self.assertTrue(fake.restricted)
            self.assertEqual(fake.context["token"]["symbol"], "TEST")
            self.assertTrue(payload["schema_valid"])
            self.assertEqual(
                set(payload),
                {"status", "model", "latency_ms", "schema_valid"},
            )

    def test_provider_check_fake_never_generates_content(self) -> None:
        class FakeClient:
            checks = 0

            def check_model(self) -> dict[str, object]:
                self.checks += 1
                return {
                    "status": "ok",
                    "model": "deepseek-v4-pro",
                    "model_available": True,
                }

            def analyze(self, *args: object, **kwargs: object) -> object:
                raise AssertionError("provider check must not generate")

        with tempfile.TemporaryDirectory() as raw:
            settings = replace(
                make_settings(Path(raw)),
                oar_ai_provider="deepseek",
                oar_ai_base_url="https://ai.invalid/v1",
                oar_ai_api_key="secret",
                oar_ai_model="deepseek-v4-pro",
            )
            fake = FakeClient()
            output = StringIO()
            with patch(
                "paopao_radar.onchain_flow.cli._ai_client",
                return_value=fake,
            ):
                with redirect_stdout(output):
                    code = main(
                        ["ai-provider-check", "--allow-network"],
                        settings=settings,
                    )
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(fake.checks, 1)
            self.assertEqual(payload["generation_calls"], 0)
            self.assertEqual(payload["rpc_calls"], 0)
            self.assertEqual(payload["telegram_calls"], 0)


class VersionedAiCacheTests(unittest.TestCase):
    def test_clear_results_preserves_hourly_call_budget(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root / "cache.json"
            cache = OarAiCache(
                path=path,
                data_dir=root,
                ttl_sec=3600,
                max_calls_per_hour=4,
                now=lambda: 1000,
            )
            for index in range(4):
                self.assertTrue(cache.reserve_call())
                if index < 3:
                    cache.put(
                        f"context-{index}",
                        "deepseek-v4-pro",
                        OAR_AI_PROMPT_VERSION,
                        valid_output(),
                        provider="deepseek",
                    )
            before = cache.status()
            cleared = cache.clear_results()
            after = cache.status()
            data = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(before["valid_entry_count"], 3)
            self.assertEqual(before["calls_last_hour"], 4)
            self.assertEqual(cleared["cleared_entry_count"], 3)
            self.assertEqual(after["valid_entry_count"], 0)
            self.assertEqual(after["calls_last_hour"], 4)
            self.assertEqual(data["entries"], {})
            self.assertEqual(len(data["call_timestamps"]), 4)
            self.assertFalse(cache.reserve_call())
            self.assertNotIn(
                "primary_hypothesis",
                json.dumps(cleared, ensure_ascii=False),
            )

    def test_clear_results_is_idempotent_when_file_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root / "missing.json"
            cache = OarAiCache(
                path=path,
                data_dir=root,
                ttl_sec=3600,
                max_calls_per_hour=4,
            )
            result = cache.clear_results()
            self.assertEqual(result["status"], "ok")
            self.assertFalse(result["exists"])
            self.assertFalse(path.exists())

    def test_clear_results_and_reserve_are_concurrency_safe(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root / "cache.json"
            cache = OarAiCache(
                path=path,
                data_dir=root,
                ttl_sec=3600,
                max_calls_per_hour=100,
                now=lambda: 1000,
            )
            cache.put(
                "context",
                "deepseek-v4-pro",
                OAR_AI_PROMPT_VERSION,
                valid_output(),
            )
            barrier = threading.Barrier(3)
            failures: list[Exception] = []

            def clear_worker() -> None:
                try:
                    barrier.wait()
                    for _ in range(10):
                        cache.clear_results()
                except Exception as exc:  # pragma: no cover - assertion aid
                    failures.append(exc)

            def reserve_worker() -> None:
                try:
                    barrier.wait()
                    for _ in range(10):
                        cache.reserve_call()
                except Exception as exc:  # pragma: no cover - assertion aid
                    failures.append(exc)

            threads = [
                threading.Thread(target=clear_worker),
                threading.Thread(target=reserve_worker),
            ]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(timeout=10)

            self.assertFalse(failures)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["schema_version"], 1)
            self.assertIsInstance(data["entries"], dict)
            self.assertEqual(len(data["call_timestamps"]), 10)

    def test_ai_cache_cli_reports_only_bounded_status(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            settings = make_settings(Path(raw))
            output = StringIO()
            with redirect_stdout(output):
                code = main(["ai-cache", "status"], settings=settings)
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(
                set(payload),
                {
                    "exists",
                    "valid_entry_count",
                    "calls_last_hour",
                    "expires_or_stale_entry_count",
                    "file_size",
                },
            )
            self.assertFalse(payload["exists"])
            self.assertEqual(payload["valid_entry_count"], 0)

    def test_operator_prompt_hash_and_thinking_profile_isolate_cache(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            cache = OarAiCache(
                path=root / "cache.json",
                data_dir=root,
                ttl_sec=3600,
                max_calls_per_hour=10,
                now=lambda: 100,
            )
            cache.put(
                "context",
                "deepseek-v4-pro",
                OAR_AI_PROMPT_VERSION,
                valid_output(),
                provider="deepseek",
                operator_prompt_hash="prompt-a",
                thinking_mode="enabled",
                reasoning_effort="high",
            )
            hit = cache.get(
                "context",
                "deepseek-v4-pro",
                OAR_AI_PROMPT_VERSION,
                provider="deepseek",
                operator_prompt_hash="prompt-a",
                thinking_mode="enabled",
                reasoning_effort="high",
            )
            changed_prompt = cache.get(
                "context",
                "deepseek-v4-pro",
                OAR_AI_PROMPT_VERSION,
                provider="deepseek",
                operator_prompt_hash="prompt-b",
                thinking_mode="enabled",
                reasoning_effort="high",
            )
            changed_mode = cache.get(
                "context",
                "deepseek-v4-pro",
                OAR_AI_PROMPT_VERSION,
                provider="deepseek",
                operator_prompt_hash="prompt-a",
                thinking_mode="disabled",
                reasoning_effort="high",
            )
            changed_tokens = cache.get(
                "context",
                "deepseek-v4-pro",
                OAR_AI_PROMPT_VERSION,
                provider="deepseek",
                operator_prompt_hash="prompt-a",
                thinking_mode="enabled",
                reasoning_effort="high",
                max_tokens=16384,
            )
            self.assertEqual(hit.status, "hit")
            self.assertEqual(changed_prompt.status, "miss")
            self.assertEqual(changed_mode.status, "miss")
            self.assertEqual(changed_tokens.status, "miss")

    def test_cache_does_not_persist_prompt_key_or_reasoning(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root / "cache.json"
            cache = OarAiCache(
                path=path,
                data_dir=root,
                ttl_sec=3600,
                max_calls_per_hour=10,
                now=lambda: 100,
            )
            cache.put(
                "context",
                "deepseek-v4-pro",
                OAR_AI_PROMPT_VERSION,
                valid_output(),
                provider="deepseek",
                operator_prompt_hash="hash-only",
                thinking_mode="enabled",
                reasoning_effort="max",
            )
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("完整 Operator Prompt", text)
            self.assertNotIn("Authorization", text)
            self.assertNotIn("API Key", text)
            self.assertNotIn("reasoning_content", text)
            self.assertIn("hash-only", text)


if __name__ == "__main__":
    unittest.main()
