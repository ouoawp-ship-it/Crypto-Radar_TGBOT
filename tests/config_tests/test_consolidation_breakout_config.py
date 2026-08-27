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

    def test_loads_topic_and_scanner_configuration(self) -> None:
        values = {
            "TG_CONSOLIDATION_BREAKOUT_TOPIC_ID": "77",
            "CONSOLIDATION_BREAKOUT_ENABLE": "true",
            "CONSOLIDATION_BREAKOUT_TIMEFRAMES": "4h,1d,1w",
            "CONSOLIDATION_BREAKOUT_SCAN_LIMIT": "12",
            "CONSOLIDATION_BREAKOUT_REQUIRE_STRONG_VOLUME": "true",
            "CONSOLIDATION_BREAKOUT_THREE_PUSH_ENABLE": "true",
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
        status = settings.redacted_status()
        self.assertTrue(
            status["telegram"]["topic_routes_configured"][
                "consolidation_breakout"
            ]
        )
        self.assertNotIn("77", str(status))


if __name__ == "__main__":
    unittest.main()
