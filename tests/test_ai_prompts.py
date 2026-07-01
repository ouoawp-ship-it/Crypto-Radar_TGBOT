from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
