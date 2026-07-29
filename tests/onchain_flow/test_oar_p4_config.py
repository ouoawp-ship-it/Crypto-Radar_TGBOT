from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from paopao_radar.onchain_flow.config import (
    OnchainSettings,
    SettingsValidationError,
)

from tests.onchain_flow.support import make_settings


class OarP4ConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_automation_defaults_are_safe_and_disabled(self) -> None:
        settings = OnchainSettings.load(base_dir=self.root, environ={})
        settings.validate()
        self.assertFalse(settings.oar_automation_enable)
        self.assertEqual(settings.oar_bridge_max_signals_per_cycle, 100)
        self.assertEqual(settings.oar_watch_max_active_tokens, 50)
        self.assertEqual(settings.oar_watch_max_tokens_per_cycle, 5)
        self.assertEqual(settings.oar_watch_query_window, "4h")
        self.assertFalse(settings.oar_watch_notify_partial)
        self.assertEqual(
            settings.oar_automation_db_path,
            self.root / "data" / "onchain" / "oar_automation.db",
        )

    def test_allowed_modules_must_be_reviewed_unique_subset(self) -> None:
        for value in ("", "launch,launch", "launch,onchain"):
            with self.subTest(value=value):
                settings = OnchainSettings.load(
                    base_dir=self.root,
                    environ={"OAR_BRIDGE_ALLOWED_MODULES": value},
                )
                with self.assertRaises(SettingsValidationError):
                    settings.validate()

    def test_hard_caps_and_minimum_intervals_are_enforced(self) -> None:
        cases = {
            "OAR_BRIDGE_MAX_SIGNALS_PER_CYCLE": "501",
            "OAR_WATCH_MAX_ACTIVE_TOKENS": "201",
            "OAR_WATCH_MAX_TOKENS_PER_CYCLE": "21",
            "OAR_WATCH_SCAN_INTERVAL_SEC": "59",
            "OAR_WATCH_LIVE_POLL_SEC": "9",
            "OAR_WATCH_LEASE_SEC": "59",
            "OAR_WATCH_MANUAL_TTL_SEC": str(366 * 86400),
            "OAR_WATCH_FLOW_TTL_SEC": str(31 * 86400),
        }
        for name, value in cases.items():
            with self.subTest(name=name):
                settings = OnchainSettings.load(
                    base_dir=self.root, environ={name: value}
                )
                with self.assertRaises(SettingsValidationError):
                    settings.validate()

    def test_automatic_query_budget_cannot_exceed_p1_limits(self) -> None:
        settings = make_settings(
            self.root,
            oar_watch_max_events_per_token=5001,
        )
        with self.assertRaises(SettingsValidationError):
            settings.validate()

    def test_enabled_automation_budget_cannot_exceed_local_p1_budget(
        self,
    ) -> None:
        settings = make_settings(
            self.root,
            oar_automation_enable=True,
            token_activity_max_events=100,
            oar_watch_max_events_per_token=101,
        )
        with self.assertRaisesRegex(
            SettingsValidationError, "Token Activity limits"
        ):
            settings.validate()

    def test_zero_bridge_overlap_is_valid(self) -> None:
        settings = make_settings(self.root, oar_bridge_overlap_sec=0)
        settings.validate()

    def test_main_signal_db_cannot_point_into_onchain_writable_dir(self) -> None:
        settings = make_settings(self.root)
        unsafe = replace(
            settings,
            main_signal_db_path=settings.data_dir / "signals.db",
        )
        with self.assertRaisesRegex(
            SettingsValidationError, "outside the on-chain"
        ):
            unsafe.validate()

    def test_diagnostics_expose_only_non_secret_automation_metadata(self) -> None:
        settings = OnchainSettings.load(
            base_dir=self.root,
            environ={"OAR_AI_API_KEY": "hidden-secret"},
        )
        diagnostics = settings.diagnostic()
        self.assertFalse(diagnostics["oar_automation"]["enabled"])
        self.assertNotIn("hidden-secret", str(diagnostics))


if __name__ == "__main__":
    unittest.main()
