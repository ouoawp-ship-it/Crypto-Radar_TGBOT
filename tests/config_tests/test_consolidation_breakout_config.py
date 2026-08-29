from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from config import Settings


class ConsolidationBreakoutConfigTests(unittest.TestCase):
    def test_defaults_are_opt_in_and_cover_long_ranges(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))

        self.assertFalse(settings.consolidation_breakout_enable)
        self.assertFalse(settings.consolidation_breakout_three_push_enable)
        self.assertEqual(
            settings.consolidation_breakout_timeframes,
            ("4h", "1d", "1w"),
        )
        self.assertEqual(settings.consolidation_breakout_interval_sec, 300)
        self.assertEqual(settings.consolidation_breakout_scan_limit, 40)
        self.assertEqual(settings.consolidation_breakout_min_quote_volume, 0)
        self.assertEqual(
            settings.consolidation_breakout_state_path.parent,
            Path(tmp),
        )
        self.assertFalse(settings.consolidation_daily_product_enable)
        self.assertTrue(settings.consolidation_daily_shadow_mode)
        self.assertFalse(settings.consolidation_daily_digest_enable)
        self.assertFalse(
            settings.consolidation_daily_boundary_events_enable
        )
        self.assertEqual(settings.consolidation_daily_history_bars, 620)
        self.assertEqual(settings.consolidation_daily_digest_max_items, 20)
        self.assertEqual(settings.consolidation_daily_retry_rounds, 2)
        self.assertEqual(settings.consolidation_daily_max_wait_sec, 10800)
        self.assertEqual(
            settings.consolidation_daily_state_path,
            Path(tmp) / "consolidation_daily_product_state.json",
        )
        self.assertEqual(
            settings.consolidation_daily_digest_state_path,
            Path(tmp) / "consolidation_daily_digest_state.json",
        )
        self.assertNotEqual(
            settings.consolidation_daily_state_path,
            settings.consolidation_daily_digest_state_path,
        )

    def test_loads_topic_and_scanner_configuration(self) -> None:
        values = {
            "TG_CONSOLIDATION_BREAKOUT_TOPIC_ID": "77",
            "CONSOLIDATION_BREAKOUT_ENABLE": "true",
            "CONSOLIDATION_BREAKOUT_TIMEFRAMES": "4h,1d,1w",
            "CONSOLIDATION_BREAKOUT_SCAN_LIMIT": "12",
            "CONSOLIDATION_BREAKOUT_REQUIRE_STRONG_VOLUME": "true",
            "CONSOLIDATION_BREAKOUT_THREE_PUSH_ENABLE": "true",
            "CONSOLIDATION_DAILY_PRODUCT_ENABLE": "true",
            "CONSOLIDATION_DAILY_SHADOW_MODE": "false",
            "CONSOLIDATION_DAILY_DIGEST_ENABLE": "true",
            "CONSOLIDATION_DAILY_BOUNDARY_EVENTS_ENABLE": "true",
            "CONSOLIDATION_DAILY_HISTORY_BARS": "840",
            "CONSOLIDATION_DAILY_DIGEST_MAX_ITEMS": "35",
            "CONSOLIDATION_DAILY_RETRY_ROUNDS": "4",
            "CONSOLIDATION_DAILY_MAX_WAIT_SEC": "14400",
            "CONSOLIDATION_DAILY_STATE_FILE": "daily-product.json",
            "CONSOLIDATION_DAILY_DIGEST_STATE_FILE": "daily-digest.json",
        }
        with patch.dict(os.environ, values, clear=True), patch(
            "config.settings.load_env_file",
            return_value=values,
        ):
            settings = Settings.load()

        self.assertTrue(settings.consolidation_breakout_enable)
        self.assertEqual(settings.tg_consolidation_breakout_topic_id, "77")
        self.assertEqual(settings.consolidation_breakout_scan_limit, 12)
        self.assertEqual(
            settings.consolidation_breakout_timeframes,
            ("4h", "1d", "1w"),
        )
        self.assertTrue(
            settings.consolidation_breakout_require_strong_volume
        )
        self.assertTrue(settings.consolidation_breakout_three_push_enable)
        self.assertTrue(settings.consolidation_daily_product_enable)
        self.assertFalse(settings.consolidation_daily_shadow_mode)
        self.assertTrue(settings.consolidation_daily_digest_enable)
        self.assertTrue(
            settings.consolidation_daily_boundary_events_enable
        )
        self.assertEqual(settings.consolidation_daily_history_bars, 840)
        self.assertEqual(settings.consolidation_daily_digest_max_items, 35)
        self.assertEqual(settings.consolidation_daily_retry_rounds, 4)
        self.assertEqual(settings.consolidation_daily_max_wait_sec, 14400)
        self.assertEqual(
            settings.consolidation_daily_state_path.name,
            "daily-product.json",
        )
        self.assertEqual(
            settings.consolidation_daily_digest_state_path.name,
            "daily-digest.json",
        )
        status = settings.redacted_status()
        self.assertTrue(
            status["telegram"]["topic_routes_configured"][
                "consolidation_breakout"
            ]
        )
        self.assertNotIn("77", str(status))
        daily = status["consolidation_breakout"]["daily_product"]
        self.assertEqual(
            daily,
            {
                "enabled": True,
                "shadow_mode": False,
                "digest_enabled": True,
                "boundary_events_enabled": True,
                "history_bars": 840,
                "digest_max_items": 35,
                "retry_rounds": 4,
                "max_wait_sec": 14400,
                "state_file": str(settings.consolidation_daily_state_path),
                "digest_state_file": str(
                    settings.consolidation_daily_digest_state_path
                ),
            },
        )

    def test_daily_boolean_controls_reload_from_file_values(self) -> None:
        stale_environment = {
            "CONSOLIDATION_DAILY_PRODUCT_ENABLE": "false",
            "CONSOLIDATION_DAILY_SHADOW_MODE": "true",
            "CONSOLIDATION_DAILY_DIGEST_ENABLE": "false",
            "CONSOLIDATION_DAILY_BOUNDARY_EVENTS_ENABLE": "false",
        }
        current_file = {
            "CONSOLIDATION_DAILY_PRODUCT_ENABLE": "true",
            "CONSOLIDATION_DAILY_SHADOW_MODE": "false",
            "CONSOLIDATION_DAILY_DIGEST_ENABLE": "true",
            "CONSOLIDATION_DAILY_BOUNDARY_EVENTS_ENABLE": "true",
        }
        with patch.dict(os.environ, stale_environment, clear=True), patch(
            "config.settings.load_env_file",
            return_value=current_file,
        ):
            settings = Settings.load()

        self.assertTrue(settings.consolidation_daily_product_enable)
        self.assertFalse(settings.consolidation_daily_shadow_mode)
        self.assertTrue(settings.consolidation_daily_digest_enable)
        self.assertTrue(
            settings.consolidation_daily_boundary_events_enable
        )

    def test_daily_numeric_controls_fall_back_when_out_of_bounds(self) -> None:
        values = {
            "CONSOLIDATION_DAILY_HISTORY_BARS": "619",
            "CONSOLIDATION_DAILY_DIGEST_MAX_ITEMS": "61",
            "CONSOLIDATION_DAILY_RETRY_ROUNDS": "6",
            "CONSOLIDATION_DAILY_MAX_WAIT_SEC": "599",
        }
        with patch.dict(os.environ, values, clear=True), patch(
            "config.settings.load_env_file",
            return_value=values,
        ):
            settings = Settings.load()

        self.assertEqual(settings.consolidation_daily_history_bars, 620)
        self.assertEqual(settings.consolidation_daily_digest_max_items, 20)
        self.assertEqual(settings.consolidation_daily_retry_rounds, 2)
        self.assertEqual(settings.consolidation_daily_max_wait_sec, 10800)

    def test_example_keeps_daily_product_disabled_and_shadowed(self) -> None:
        example_path = (
            Path(__file__).resolve().parents[2] / "config" / ".env.oi.example"
        )
        values = dict(
            line.split("=", 1)
            for line in example_path.read_text(encoding="utf-8").splitlines()
            if line and not line.startswith("#") and "=" in line
        )

        self.assertEqual(values["CONSOLIDATION_DAILY_PRODUCT_ENABLE"], "false")
        self.assertEqual(values["CONSOLIDATION_DAILY_SHADOW_MODE"], "true")
        self.assertEqual(values["CONSOLIDATION_DAILY_DIGEST_ENABLE"], "false")
        self.assertEqual(
            values["CONSOLIDATION_DAILY_BOUNDARY_EVENTS_ENABLE"],
            "false",
        )
        self.assertNotEqual(
            values["CONSOLIDATION_DAILY_STATE_FILE"],
            values["CONSOLIDATION_DAILY_DIGEST_STATE_FILE"],
        )


if __name__ == "__main__":
    unittest.main()
