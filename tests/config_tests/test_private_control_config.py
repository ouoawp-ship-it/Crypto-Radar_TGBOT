from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from config import Settings
from scripts.paopao_config import ConfigManager, ConfigManagerError


class PrivateControlSettingsTests(unittest.TestCase):
    def test_defaults_are_disabled(self) -> None:
        settings = Settings()

        self.assertFalse(settings.tg_private_control_enable)
        self.assertEqual(settings.tg_private_control_admin_user_id, "")
        self.assertEqual(
            settings.tg_private_control_state_path.name,
            "telegram_private_control_state.json",
        )

    def test_loads_enabled_private_control_without_exposing_admin(self) -> None:
        values = {
            "TG_PRIVATE_CONTROL_ENABLE": "true",
            "TG_PRIVATE_CONTROL_ADMIN_USER_ID": "123456789",
        }
        with patch.dict(os.environ, values, clear=True), patch(
            "config.settings.load_env_file",
            return_value={},
        ):
            settings = Settings.load()

        self.assertTrue(settings.tg_private_control_enable)
        self.assertEqual(
            settings.tg_private_control_admin_user_id,
            "123456789",
        )
        self.assertNotIn(
            "123456789",
            str(settings.redacted_status()),
        )


class PrivateControlConfigManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "config").mkdir()
        self.manager = ConfigManager(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_defaults_are_disabled_and_admin_is_redacted(self) -> None:
        status = self.manager.status()

        self.assertFalse(status["TG_PRIVATE_CONTROL_ENABLE"])
        self.assertEqual(
            status["TG_PRIVATE_CONTROL_ADMIN_USER_ID"],
            "not_configured",
        )

    def test_enable_requires_bot_token_and_admin(self) -> None:
        with self.assertRaisesRegex(
            ConfigManagerError,
            "private_control_gate_blocked",
        ):
            self.manager.set("TG_PRIVATE_CONTROL_ENABLE", "true")

        self.assertFalse(
            (self.root / "config" / ".env.oi").exists()
        )

    def test_admin_id_must_be_a_positive_bounded_integer(self) -> None:
        for value in ("0", "-1", "+1", "abc", "1.5", "9" * 20):
            with self.subTest(value=value):
                with self.assertRaises(ConfigManagerError):
                    self.manager.set(
                        "TG_PRIVATE_CONTROL_ADMIN_USER_ID",
                        value,
                    )

    def test_valid_configuration_is_atomic_and_redacted(self) -> None:
        self.manager.set("TG_BOT_TOKEN", "123456:fake-secret")
        result = self.manager.set(
            "TG_PRIVATE_CONTROL_ADMIN_USER_ID",
            "123456789",
        )
        enabled = self.manager.set(
            "TG_PRIVATE_CONTROL_ENABLE",
            "true",
        )

        self.assertEqual(result["value"], "configured")
        self.assertNotIn("123456789", str(result))
        self.assertTrue(enabled["value"])
        checks = self.manager.validate()["checks"]
        self.assertTrue(checks["telegram_private_control_enable"])
        self.assertEqual(
            checks["telegram_private_control_admin"],
            "configured",
        )

    def test_failed_enable_restores_existing_file(self) -> None:
        self.manager.set(
            "TG_PRIVATE_CONTROL_ADMIN_USER_ID",
            "123456789",
        )
        path = self.root / "config" / ".env.oi"
        before = path.read_bytes()

        with self.assertRaisesRegex(
            ConfigManagerError,
            "private_control_gate_blocked",
        ):
            self.manager.set("TG_PRIVATE_CONTROL_ENABLE", "true")

        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
