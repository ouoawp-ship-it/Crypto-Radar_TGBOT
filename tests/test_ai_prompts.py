from __future__ import annotations

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from paopao_radar import ai_prompts
from paopao_radar.ai_prompts import DEFAULT_ANALYST_PROMPT, DEFAULT_ASSISTANT_PROMPT, load_ai_prompts, reset_ai_prompts, save_ai_prompts
from paopao_radar.config import Settings


class AiPromptsTests(unittest.TestCase):
    def test_load_uses_defaults_when_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), ai_prompts_path=Path(tmp) / "ai_prompts.json")

            payload = load_ai_prompts(settings)

        self.assertTrue(payload["ok"])
        self.assertFalse(payload["exists"])
        self.assertEqual(payload["prompts"]["assistant_prompt"], DEFAULT_ASSISTANT_PROMPT)
        self.assertEqual(payload["prompts"]["analyst_prompt"], DEFAULT_ANALYST_PROMPT)

    def test_default_assistant_prompt_supports_playful_style_and_expert_routing(self) -> None:
        self.assertIn("有一点皮", DEFAULT_ASSISTANT_PROMPT)
        self.assertIn("生活问题", DEFAULT_ASSISTANT_PROMPT)
        self.assertIn("专业分析师模式", DEFAULT_ASSISTANT_PROMPT)
        self.assertIn("不能用自然语言直接创建", DEFAULT_ASSISTANT_PROMPT)

    def test_save_and_reset_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prompt_path = Path(tmp) / "ai_prompts.json"
            settings = Settings(data_dir=Path(tmp), ai_prompts_path=prompt_path)

            saved = save_ai_prompts(
                {
                    "assistant_prompt": "普通助手",
                    "analyst_prompt": "专业分析师",
                },
                settings,
            )
            loaded = load_ai_prompts(settings)
            reset = reset_ai_prompts(settings)
            restored = load_ai_prompts(settings)

        self.assertTrue(saved["ok"])
        self.assertEqual(set(saved["changed"]), {"assistant_prompt", "analyst_prompt"})
        self.assertEqual(loaded["prompts"]["assistant_prompt"], "普通助手")
        self.assertEqual(loaded["prompts"]["analyst_prompt"], "专业分析师")
        self.assertTrue(reset["ok"])
        self.assertEqual(restored["prompts"]["assistant_prompt"], DEFAULT_ASSISTANT_PROMPT)
        self.assertEqual(restored["prompts"]["analyst_prompt"], DEFAULT_ANALYST_PROMPT)

    def test_save_rejects_empty_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), ai_prompts_path=Path(tmp) / "ai_prompts.json")

            result = save_ai_prompts({"assistant_prompt": "", "analyst_prompt": "x"}, settings)

        self.assertFalse(result["ok"])
        self.assertIn("不能为空", result["error"])

    def test_save_preserves_other_value_from_legacy_json_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prompt_path = Path(tmp) / "ai_prompts.json"
            prompt_path.write_text(
                json.dumps(
                    {
                        "assistant_prompt": "旧助手提示词",
                        "analyst_prompt": "旧分析师提示词",
                        "updated_at": "legacy",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            settings = Settings(data_dir=Path(tmp), ai_prompts_path=prompt_path)

            result = save_ai_prompts({"assistant_prompt": "新助手提示词"}, settings)
            loaded = load_ai_prompts(settings)

        self.assertTrue(result["ok"])
        self.assertEqual(result["changed"], ["assistant_prompt"])
        self.assertEqual(loaded["prompts"]["assistant_prompt"], "新助手提示词")
        self.assertEqual(loaded["prompts"]["analyst_prompt"], "旧分析师提示词")

    def test_concurrent_partial_prompt_updates_do_not_lose_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prompt_path = Path(tmp) / "ai_prompts.json"
            settings = Settings(data_dir=Path(tmp), ai_prompts_path=prompt_path)
            save_ai_prompts(
                {"assistant_prompt": "初始助手", "analyst_prompt": "初始分析师"},
                settings,
            )
            barrier = threading.Barrier(2)
            real_update = ai_prompts.locked_update_json

            def synchronized_update(*args, **kwargs):
                barrier.wait(timeout=5)
                return real_update(*args, **kwargs)

            with patch.object(ai_prompts, "locked_update_json", side_effect=synchronized_update) as update_mock:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [
                        executor.submit(save_ai_prompts, {"assistant_prompt": "并发助手"}, settings),
                        executor.submit(save_ai_prompts, {"analyst_prompt": "并发分析师"}, settings),
                    ]
                    results = [future.result(timeout=10) for future in futures]

            payload = json.loads(prompt_path.read_text(encoding="utf-8"))

        self.assertTrue(all(result["ok"] for result in results))
        self.assertEqual(update_mock.call_count, 2)
        self.assertEqual(payload["assistant_prompt"], "并发助手")
        self.assertEqual(payload["analyst_prompt"], "并发分析师")


if __name__ == "__main__":
    unittest.main()
