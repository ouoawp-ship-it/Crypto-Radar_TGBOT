from __future__ import annotations

import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from config import Settings
from scripts.paopao_config import ConfigManager, ConfigManagerError


class FailOncePromptWriteManager(ConfigManager):
    def __init__(self, base_dir: Path) -> None:
        super().__init__(base_dir)
        self.fail_next_prompt_write = False

    def _atomic_write(self, path: Path, text: str) -> None:
        if self.fail_next_prompt_write and path.name == ".launch_ai_prompt":
            self.fail_next_prompt_write = False
            raise OSError("private write detail must not escape")
        super()._atomic_write(path, text)


class LaunchAiPromptConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "config").mkdir()
        self.manager = ConfigManager(self.root)
        self.prompt_path = self.root / "config" / ".launch_ai_prompt"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_prompt_is_written_atomically_as_private_file_not_environment(self) -> None:
        prompt = "请先说明数据完整性。\n再用两句白话解释主要风险。"

        result = self.manager.set_ai_prompt(prompt)

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["value"], "configured")
        self.assertEqual(self.prompt_path.read_text("utf-8").rstrip("\n"), prompt)
        self.assertEqual(
            self.manager.status()["AI_OPERATOR_PROMPT"],
            "configured",
        )
        env_path = self.root / "config" / ".env.oi"
        self.assertFalse(env_path.exists())
        self.assertEqual(list(self.prompt_path.parent.glob(".*.tmp")), [])
        self.assertEqual(
            list(self.prompt_path.parent.glob("..launch_ai_prompt.*.tmp")),
            [],
        )
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.prompt_path.stat().st_mode), 0o600)

    def test_replacing_prompt_creates_private_backup_and_clear_restores_default(self) -> None:
        first = "第一版补充提示词"
        second = "第二版补充提示词"
        self.manager.set_ai_prompt(first)

        replaced = self.manager.set_ai_prompt(second)
        backups_after_replace = sorted(
            self.prompt_path.parent.glob(".launch_ai_prompt.bak.*")
        )

        self.assertTrue(replaced["backup_created"])
        self.assertEqual(self.prompt_path.read_text("utf-8").rstrip("\n"), second)
        self.assertTrue(backups_after_replace)
        self.assertIn(
            first,
            [path.read_text("utf-8").rstrip("\n") for path in backups_after_replace],
        )
        if os.name == "posix":
            self.assertTrue(
                all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in backups_after_replace)
            )

        cleared = self.manager.clear_ai_prompt()

        self.assertEqual(cleared["status"], "ok")
        self.assertEqual(cleared["value"], "default")
        self.assertEqual(self.prompt_path.read_text("utf-8"), "")
        self.assertEqual(self.manager.status()["AI_OPERATOR_PROMPT"], "default")

    def test_invalid_prompt_is_rejected_without_changing_previous_value(self) -> None:
        original = "保留这份有效提示词"
        self.manager.set_ai_prompt(original)
        before = self.prompt_path.read_bytes()
        invalid_values = (
            "",
            "\x00",
            "x" * 3501,
        )

        for value in invalid_values:
            with self.subTest(length=len(value)), self.assertRaises(ConfigManagerError):
                self.manager.set_ai_prompt(value)
            self.assertEqual(self.prompt_path.read_bytes(), before)

    def test_write_failure_restores_old_prompt_and_removes_temporary_file(self) -> None:
        manager = FailOncePromptWriteManager(self.root)
        manager.set_ai_prompt("原有提示词")
        before = self.prompt_path.read_bytes()
        manager.fail_next_prompt_write = True

        with self.assertRaises(OSError):
            manager.set_ai_prompt("不应留下的新提示词")

        self.assertEqual(self.prompt_path.read_bytes(), before)
        self.assertFalse(
            any(
                path.name.endswith(".tmp")
                for path in self.prompt_path.parent.iterdir()
            )
        )

    def test_write_failure_restores_invalid_old_prompt_bytes_exactly(self) -> None:
        manager = FailOncePromptWriteManager(self.root)
        invalid_original = b"\xff\xfeprivate-old-bytes"
        self.prompt_path.write_bytes(invalid_original)
        self.prompt_path.chmod(0o600)
        manager.fail_next_prompt_write = True

        with self.assertRaises(OSError):
            manager.set_ai_prompt("新的有效提示词")

        self.assertEqual(self.prompt_path.read_bytes(), invalid_original)

    def test_prompt_content_never_appears_in_result_or_environment_backup(self) -> None:
        secret_marker = "PRIVATE_PROMPT_MARKER_NEVER_ECHO"
        self.manager.set("AI_MODEL", "fake-model")

        result = self.manager.set_ai_prompt(secret_marker)

        self.assertNotIn(secret_marker, str(result))
        env_path = self.root / "config" / ".env.oi"
        self.assertNotIn(secret_marker, env_path.read_text("utf-8"))
        env_backups = list(env_path.parent.glob(".env.oi.bak.*"))
        self.assertTrue(
            all(secret_marker not in path.read_text("utf-8") for path in env_backups)
        )

    def test_settings_hot_reload_prefers_managed_ai_values_and_loads_prompt(self) -> None:
        prompt_path = self.root / "config" / ".launch_ai_prompt"
        prompt_path.write_text("部署者补充解读要求", encoding="utf-8")
        prompt_path.chmod(0o600)
        managed = {
            "AI_API_KEY": "new-managed-key",
            "AI_BASE_URL": "https://new-ai.example.test/v1/",
            "AI_MODEL": "new-managed-model",
        }
        stale_process_environment = {
            "AI_API_KEY": "old-process-key",
            "AI_BASE_URL": "https://old-ai.example.test/v1",
            "AI_MODEL": "old-process-model",
        }

        with patch.dict(os.environ, stale_process_environment, clear=True), patch(
            "config.settings.load_env_file",
            return_value=managed,
        ), patch(
            "config.settings.AI_OPERATOR_PROMPT_FILE",
            prompt_path,
        ):
            settings = Settings.load()

        self.assertEqual(settings.ai_api_key, "new-managed-key")
        self.assertEqual(settings.ai_base_url, "https://new-ai.example.test/v1")
        self.assertEqual(settings.ai_model, "new-managed-model")
        self.assertEqual(settings.ai_operator_prompt, "部署者补充解读要求")

    def test_invalid_prompt_file_is_reported_and_not_loaded(self) -> None:
        self.prompt_path.write_text("x" * 3501, encoding="utf-8")
        self.prompt_path.chmod(0o600)

        self.assertEqual(self.manager.ai_prompt_status(), "invalid")
        with patch(
            "config.settings.AI_OPERATOR_PROMPT_FILE",
            self.prompt_path,
        ):
            settings = Settings.load()
        self.assertEqual(settings.ai_operator_prompt, "")

    def test_ai_endpoint_rejects_whitespace_and_oversized_values(self) -> None:
        for value in (
            "https://provider.invalid/v1 path",
            "https://provider.invalid/" + "x" * 2049,
        ):
            with self.subTest(length=len(value)), self.assertRaises(
                ConfigManagerError
            ):
                self.manager.set("AI_BASE_URL", value)

    def test_missing_prompt_file_uses_immutable_default_prompt_only(self) -> None:
        missing = self.root / "config" / ".launch_ai_prompt"
        with patch.dict(os.environ, {}, clear=True), patch(
            "config.settings.load_env_file",
            return_value={},
        ), patch(
            "config.settings.AI_OPERATOR_PROMPT_FILE",
            missing,
        ):
            settings = Settings.load()

        self.assertEqual(settings.ai_operator_prompt, "")


if __name__ == "__main__":
    unittest.main()
