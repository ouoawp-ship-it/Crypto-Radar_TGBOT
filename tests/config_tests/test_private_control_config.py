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
        self.assertFalse(settings.tg_private_control_alert_enable)
        self.assertEqual(settings.tg_private_control_alert_cooldown_sec, 3600)
        self.assertTrue(settings.pulse_radar_enable)
        self.assertTrue(settings.radar_summary_enable)
        self.assertTrue(settings.funding_alert_enable)
        self.assertTrue(settings.flow_radar_enable)
        self.assertTrue(settings.announcement_risk_enable)

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

    def test_file_backed_switches_override_stale_process_environment(self) -> None:
        file_values = {
            "PULSE_RADAR_ENABLE": "false",
            "RADAR_SUMMARY_ENABLE": "false",
            "FUNDING_ALERT_ENABLE": "false",
            "FLOW_RADAR_ENABLE": "false",
            "ANNOUNCEMENT_RISK_ENABLE": "false",
            "TG_PRIVATE_CONTROL_ALERT_ENABLE": "true",
            "TG_PRIVATE_CONTROL_ALERT_COOLDOWN_SEC": "7200",
        }
        process_values = {key: "true" for key in file_values}
        process_values["TG_PRIVATE_CONTROL_ALERT_COOLDOWN_SEC"] = "3600"
        with patch.dict(os.environ, process_values, clear=True), patch(
            "config.settings.load_env_file",
            return_value=file_values,
        ):
            settings = Settings.load()

        self.assertFalse(settings.pulse_radar_enable)
        self.assertFalse(settings.radar_summary_enable)
        self.assertFalse(settings.funding_alert_enable)
        self.assertFalse(settings.flow_radar_enable)
        self.assertFalse(settings.announcement_risk_enable)
        self.assertTrue(settings.tg_private_control_alert_enable)
        self.assertEqual(settings.tg_private_control_alert_cooldown_sec, 7200)

    def test_new_pulse_switch_takes_precedence_over_legacy_alias(self) -> None:
        with patch.dict(
            os.environ,
            {"PULSE_RADAR_ENABLE": "true", "LAUNCH_ALERT_ENABLE": "invalid"},
            clear=True,
        ), patch(
            "config.settings.load_env_file",
            return_value={
                "PULSE_RADAR_ENABLE": "false",
                "LAUNCH_ALERT_ENABLE": "invalid",
            },
        ):
            settings = Settings.load()

        self.assertFalse(settings.pulse_radar_enable)


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
        self.assertFalse(status["TG_PRIVATE_CONTROL_ALERT_ENABLE"])
        self.assertTrue(status["PULSE_RADAR_ENABLE"])
        self.assertTrue(status["RADAR_SUMMARY_ENABLE"])
        self.assertTrue(status["FUNDING_ALERT_ENABLE"])
        self.assertTrue(status["FLOW_RADAR_ENABLE"])
        self.assertTrue(status["ANNOUNCEMENT_RISK_ENABLE"])

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
        alerts = self.manager.set(
            "TG_PRIVATE_CONTROL_ALERT_ENABLE",
            "true",
        )

        self.assertEqual(result["value"], "configured")
        self.assertNotIn("123456789", str(result))
        self.assertTrue(enabled["value"])
        self.assertTrue(alerts["value"])
        checks = self.manager.validate()["checks"]
        self.assertTrue(checks["telegram_private_control_enable"])
        self.assertTrue(checks["telegram_private_control_alert_enable"])
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

    def test_fault_alert_requires_private_control_and_valid_cooldown(self) -> None:
        with self.assertRaisesRegex(
            ConfigManagerError,
            "private_control_alert_gate_blocked",
        ):
            self.manager.set("TG_PRIVATE_CONTROL_ALERT_ENABLE", "true")
        for value in ("299", "86401", "not-a-number"):
            with self.subTest(value=value), self.assertRaises(ConfigManagerError):
                self.manager.set(
                    "TG_PRIVATE_CONTROL_ALERT_COOLDOWN_SEC",
                    value,
                )

    def test_five_radar_switches_are_atomic_allowlisted_booleans(self) -> None:
        keys = (
            "PULSE_RADAR_ENABLE",
            "RADAR_SUMMARY_ENABLE",
            "FUNDING_ALERT_ENABLE",
            "FLOW_RADAR_ENABLE",
            "ANNOUNCEMENT_RISK_ENABLE",
        )
        for key in keys:
            with self.subTest(key=key):
                result = self.manager.set(key, "false")
                self.assertFalse(result["value"])
                self.assertFalse(self.manager.status()[key])
        path = self.root / "config" / ".env.oi"
        self.assertTrue(path.exists())
        if os.name == "posix":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_invalid_radar_switch_rolls_back(self) -> None:
        self.manager.set("PULSE_RADAR_ENABLE", "true")
        path = self.root / "config" / ".env.oi"
        before = path.read_bytes()
        with self.assertRaises(ConfigManagerError):
            self.manager.set("PULSE_RADAR_ENABLE", "sometimes")
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
