from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from config import Settings
from scripts.paopao_config import ConfigManager, ConfigManagerError


class LaunchDirectionalSettingsTests(unittest.TestCase):
    def test_safe_defaults(self) -> None:
        settings = Settings()

        self.assertFalse(settings.launch_directional_enable)
        self.assertEqual(settings.launch_directional_max_candidates, 6)
        self.assertFalse(settings.launch_ai_interpreter_enable)
        self.assertEqual(settings.ai_api_key, "")
        self.assertEqual(settings.ai_base_url, "")
        self.assertEqual(settings.ai_model, "")
        self.assertEqual(settings.ai_timeout_sec, 60)

    def test_loads_valid_values(self) -> None:
        values = {
            "LAUNCH_DIRECTIONAL_ENABLE": "true",
            "LAUNCH_DIRECTIONAL_MAX_CANDIDATES": "4",
            "LAUNCH_AI_INTERPRETER_ENABLE": "true",
            "AI_API_KEY": "fake-ai-secret",
            "AI_BASE_URL": "https://ai.example.test/v1/",
            "AI_MODEL": "fake-model",
            "AI_TIMEOUT_SEC": "90",
        }
        with patch.dict(os.environ, values, clear=True), patch(
            "config.settings.load_env_file",
            return_value={},
        ):
            settings = Settings.load()

        self.assertTrue(settings.launch_directional_enable)
        self.assertEqual(settings.launch_directional_max_candidates, 4)
        self.assertTrue(settings.launch_ai_interpreter_enable)
        self.assertEqual(settings.ai_api_key, "fake-ai-secret")
        self.assertEqual(settings.ai_base_url, "https://ai.example.test/v1")
        self.assertEqual(settings.ai_model, "fake-model")
        self.assertEqual(settings.ai_timeout_sec, 90)

    def test_invalid_bounded_environment_values_fall_back_safely(self) -> None:
        values = {
            "LAUNCH_DIRECTIONAL_MAX_CANDIDATES": "7",
            "AI_TIMEOUT_SEC": "181",
        }
        with patch.dict(os.environ, values, clear=True), patch(
            "config.settings.load_env_file",
            return_value={},
        ):
            settings = Settings.load()

        self.assertEqual(settings.launch_directional_max_candidates, 6)
        self.assertEqual(settings.ai_timeout_sec, 60)

    def test_runtime_switches_reload_from_managed_file_values(self) -> None:
        environment = {
            "LAUNCH_DIRECTIONAL_ENABLE": "false",
            "LAUNCH_AI_INTERPRETER_ENABLE": "false",
        }
        managed_values = {
            "LAUNCH_DIRECTIONAL_ENABLE": "true",
            "LAUNCH_AI_INTERPRETER_ENABLE": "true",
        }
        with patch.dict(os.environ, environment, clear=True), patch(
            "config.settings.load_env_file",
            return_value=managed_values,
        ):
            settings = Settings.load()

        self.assertTrue(settings.launch_directional_enable)
        self.assertTrue(settings.launch_ai_interpreter_enable)


class LaunchDirectionalConfigManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "config").mkdir()
        self.manager = ConfigManager(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_defaults_are_safe_and_secrets_are_redacted(self) -> None:
        status = self.manager.status()

        self.assertFalse(status["LAUNCH_DIRECTIONAL_ENABLE"])
        self.assertEqual(status["LAUNCH_DIRECTIONAL_MAX_CANDIDATES"], "6")
        self.assertFalse(status["LAUNCH_AI_INTERPRETER_ENABLE"])
        self.assertEqual(status["AI_API_KEY"], "not_configured")
        self.assertEqual(status["AI_BASE_URL"], "not_configured")
        self.assertEqual(status["AI_MODEL"], "not_configured")
        self.assertEqual(status["AI_TIMEOUT_SEC"], "60")

    def test_all_new_keys_are_allowlisted_and_validate(self) -> None:
        values = {
            "LAUNCH_DIRECTIONAL_ENABLE": "true",
            "LAUNCH_DIRECTIONAL_MAX_CANDIDATES": "6",
            "LAUNCH_AI_INTERPRETER_ENABLE": "true",
            "AI_API_KEY": "fake-ai-secret",
            "AI_BASE_URL": "https://ai.example.test/v1",
            "AI_MODEL": "fake-model",
            "AI_TIMEOUT_SEC": "60",
        }
        results = {
            key: self.manager.set(key, value)
            for key, value in values.items()
        }

        self.assertEqual(results["AI_API_KEY"]["value"], "configured")
        self.assertEqual(results["AI_BASE_URL"]["value"], "configured")
        self.assertNotIn("fake-ai-secret", str(results))
        self.assertNotIn("https://ai.example.test/v1", str(results))
        checks = self.manager.validate()["checks"]
        self.assertTrue(checks["launch_directional_enable"])
        self.assertEqual(checks["launch_directional_max_candidates"], 6)
        self.assertTrue(checks["launch_ai_interpreter_enable"])
        self.assertEqual(checks["ai_api_key"], "configured")
        self.assertEqual(checks["ai_base_url"], "configured")
        self.assertEqual(checks["ai_model"], "configured")
        self.assertEqual(checks["ai_timeout_sec"], 60)

    def test_integer_ranges_are_enforced(self) -> None:
        cases = (
            ("LAUNCH_DIRECTIONAL_MAX_CANDIDATES", "0"),
            ("LAUNCH_DIRECTIONAL_MAX_CANDIDATES", "7"),
            ("AI_TIMEOUT_SEC", "4"),
            ("AI_TIMEOUT_SEC", "181"),
        )
        for key, value in cases:
            with self.subTest(key=key, value=value):
                with self.assertRaises(ConfigManagerError):
                    self.manager.set(key, value)

    def test_ai_base_url_requires_safe_https_endpoint(self) -> None:
        invalid_values = (
            "http://ai.example.test/v1",
            "https://user:password@ai.example.test/v1",
            "https://ai.example.test/v1?secret=value",
            "https://ai.example.test/v1#fragment",
            "https:///v1",
        )
        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(ConfigManagerError):
                    self.manager.set("AI_BASE_URL", value)

    def test_invalid_value_does_not_modify_existing_file(self) -> None:
        self.manager.set("AI_TIMEOUT_SEC", "60")
        path = self.root / "config" / ".env.oi"
        before = path.read_bytes()

        with self.assertRaises(ConfigManagerError):
            self.manager.set("AI_TIMEOUT_SEC", "181")

        self.assertEqual(path.read_bytes(), before)

    def test_finalshell_menu_exposes_visible_directional_ai_settings(self) -> None:
        menu = (
            Path(__file__).resolve().parents[2]
            / "scripts"
            / "paopao_menu.sh"
        ).read_text(encoding="utf-8")

        for key in (
            "AI_API_KEY",
            "AI_BASE_URL",
            "AI_MODEL",
            "AI_TIMEOUT_SEC",
            "LAUNCH_DIRECTIONAL_ENABLE",
            "LAUNCH_AI_INTERPRETER_ENABLE",
            "LAUNCH_DIRECTIONAL_MAX_CANDIDATES",
        ):
            self.assertIn(f"config_set {key}", menu)
        self.assertNotIn("read -s", menu)


if __name__ == "__main__":
    unittest.main()
