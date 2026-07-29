from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from paopao_radar.onchain_flow.constants import (
    OAR_AI_OPERATOR_PROMPT_HISTORY_LIMIT,
    OAR_AI_OPERATOR_PROMPT_MAX_CHARS,
)
from paopao_radar.onchain_flow.ai_client import (
    AI_OUTPUT_CONTRACT,
    build_ai_system_prompt,
)
from paopao_radar.onchain_flow.prompt_manager import (
    OperatorPromptError,
    OperatorPromptManager,
)


class OperatorPromptManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data_dir = self.root / "data" / "onchain"
        self.path = self.data_dir / "config" / "operator.txt"
        self.default = self.root / "config" / "default.txt"
        self.default.parent.mkdir(parents=True)
        self.default.write_text("默认提示词\n", encoding="utf-8")
        ticks = iter(range(100, 1000))
        self.manager = OperatorPromptManager(
            data_dir=self.data_dir,
            prompt_path=self.path,
            default_path=self.default,
            clock_ns=lambda: next(ticks),
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_first_use_installs_default_with_stable_hash(self) -> None:
        prompt = self.manager.install_default()
        self.assertEqual(prompt.content, "默认提示词\n")
        self.assertEqual(
            prompt.prompt_hash,
            self.manager.hash_text("默认提示词\n"),
        )
        self.assertEqual(self.path.read_text(encoding="utf-8"), prompt.content)

    def test_private_file_and_parent_use_restricted_permissions(self) -> None:
        self.manager.install_default()
        if os.name != "nt":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                self.path.parent.stat().st_mode & 0o777,
                0o700,
            )

    def test_save_is_atomic_and_keeps_no_temporary_file(self) -> None:
        self.manager.install_default()
        prompt = self.manager.save("新提示词")
        self.assertEqual(prompt.content, "新提示词")
        self.assertFalse(list(self.path.parent.glob("*.tmp")))

    def test_history_and_rollback_restore_prior_content(self) -> None:
        self.manager.install_default()
        self.manager.save("版本二")
        self.manager.save("版本三")
        history = self.manager.history()
        self.assertEqual(len(history), 2)
        prior = next(
            item for item in history
            if item["prompt_hash"] == self.manager.hash_text("版本二")
        )
        restored = self.manager.rollback(prior["version"])
        self.assertEqual(restored.content, "版本二")

    def test_history_is_bounded_to_twenty_versions(self) -> None:
        self.manager.install_default()
        for index in range(OAR_AI_OPERATOR_PROMPT_HISTORY_LIMIT + 5):
            self.manager.save(f"版本 {index}")
        self.assertEqual(
            len(self.manager.history()),
            OAR_AI_OPERATOR_PROMPT_HISTORY_LIMIT,
        )

    def test_restore_default_records_current_and_restores_template(self) -> None:
        self.manager.install_default()
        self.manager.save("自定义")
        restored = self.manager.restore_default()
        self.assertEqual(restored.content, "默认提示词\n")
        self.assertTrue(self.manager.history())

    def test_install_default_never_overwrites_existing_private_prompt(
        self,
    ) -> None:
        self.manager.install_default()
        self.manager.save("保留现有私有 Prompt")
        self.default.write_text("升级后的公共模板\n", encoding="utf-8")
        loaded = self.manager.install_default()
        self.assertEqual(loaded.content, "保留现有私有 Prompt")

    def test_nul_and_length_are_rejected_without_changing_file(self) -> None:
        self.manager.install_default()
        original = self.path.read_bytes()
        for invalid in (
            "bad\x00prompt",
            "x" * (OAR_AI_OPERATOR_PROMPT_MAX_CHARS + 1),
        ):
            with self.subTest(length=len(invalid)):
                with self.assertRaises(OperatorPromptError):
                    self.manager.save(invalid)
                self.assertEqual(self.path.read_bytes(), original)

    def test_invalid_utf8_is_rejected(self) -> None:
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"\xff\xfe")
        with self.assertRaises(OperatorPromptError):
            self.manager.validate()

    def test_runtime_path_cannot_escape_data_onchain(self) -> None:
        with self.assertRaises(OperatorPromptError):
            OperatorPromptManager(
                data_dir=self.data_dir,
                prompt_path=self.root / "outside.txt",
                default_path=self.default,
            )

    def test_status_never_includes_full_private_prompt(self) -> None:
        self.manager.save("不要输出这段完整业务提示词")
        serialized = str(self.manager.status())
        self.assertNotIn("不要输出这段完整业务提示词", serialized)
        self.assertIn(self.manager.hash(), serialized)

    def test_public_default_prompt_has_all_twelve_analysis_sections(
        self,
    ) -> None:
        path = (
            Path(__file__).resolve().parents[2]
            / "config"
            / "onchain"
            / "oar_ai_operator_prompt.default.txt"
        )
        prompt = path.read_text(encoding="utf-8")
        for section in (
            "一、数据质量优先",
            "二、交易所流向",
            "三、行为候选",
            "四、钱包关联",
            "五、市场关联信号",
            "六、主要假设与备选假设",
            "七、可能的下一步动作",
            "八、继续观察条件",
            "九、失效条件",
            "十、Bias",
            "十一、Confidence",
            "十二、输出风格",
        ):
            self.assertIn(section, prompt)
        for field in (
            "linked_market_signals",
            "likely_next_actions",
            "watch_signals",
            "invalidation_conditions",
        ):
            self.assertIn(field, prompt)
        self.assertLessEqual(
            len(prompt),
            OAR_AI_OPERATOR_PROMPT_MAX_CHARS,
        )

    def test_core_prompt_precedes_operator_and_contract_is_unchanged(
        self,
    ) -> None:
        operator = "仅补充表达偏好；不得改变输出结构。"
        prompt = build_ai_system_prompt(operator)
        self.assertLess(
            prompt.index("完整输出契约"),
            prompt.index("<operator_policy>"),
        )
        self.assertLess(
            prompt.index("不能覆盖核心安全规则和输出契约"),
            prompt.index(operator),
        )
        self.assertEqual(
            tuple(AI_OUTPUT_CONTRACT["required"]),
            (
                "schema_version",
                "bias",
                "confidence",
                "primary_hypothesis",
                "alternative_hypotheses",
                "likely_next_actions",
                "watch_signals",
                "invalidation_conditions",
                "risk_notes",
            ),
        )


if __name__ == "__main__":
    unittest.main()
