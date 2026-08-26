from __future__ import annotations

import argparse
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from config import Settings
from runtime.cli import (
    effective_radar_switches,
    last_known_settings_reader,
    radar_runtime_flags,
    refresh_shared_market_snapshot,
    reload_loop_settings,
)
from runtime.diagnostics import build_market_radar_runtime_status
from shared.storage import JsonStore


def args(**updates: bool) -> argparse.Namespace:
    values = {
        "no_launch": False,
        "no_announcements": False,
        "no_flow": False,
        "no_funding_alert": False,
        "no_consolidation_breakout": False,
    }
    values.update(updates)
    return argparse.Namespace(**values)


class RadarSwitchTests(unittest.TestCase):
    def test_existing_switches_stay_enabled_and_new_radar_is_opt_in(self) -> None:
        switches = effective_radar_switches(Settings(), args())

        self.assertFalse(switches["consolidation_breakout"])
        self.assertTrue(all(
            enabled
            for key, enabled in switches.items()
            if key != "consolidation_breakout"
        ))
        self.assertEqual(
            radar_runtime_flags(switches),
            {
                "no_launch": False,
                "no_summary": False,
                "no_funding_alert": False,
                "no_flow": False,
                "no_consolidation_breakout": True,
                "no_announcements": False,
            },
        )

    def test_each_config_switch_is_independent(self) -> None:
        fields = {
            "pulse_radar_enable": "launch_alert",
            "radar_summary_enable": "radar_summary",
            "funding_alert_enable": "funding_alert",
            "flow_radar_enable": "flow_radar",
            "consolidation_breakout_enable": "consolidation_breakout",
            "announcement_risk_enable": "announcement_risk",
        }
        enabled_settings = {
            field: True
            for field in fields
        }
        for field, radar in fields.items():
            with self.subTest(radar=radar):
                settings = Settings(**{
                    **enabled_settings,
                    field: False,
                })
                switches = effective_radar_switches(settings, args())
                self.assertFalse(switches[radar])
                self.assertTrue(
                    all(
                        enabled
                        for key, enabled in switches.items()
                        if key != radar
                    )
                )

    def test_existing_runtime_flags_remain_stronger(self) -> None:
        switches = effective_radar_switches(
            Settings(consolidation_breakout_enable=True),
            args(
                no_launch=True,
                no_announcements=True,
                no_flow=True,
                no_funding_alert=True,
                no_consolidation_breakout=True,
            ),
        )

        self.assertFalse(switches["launch_alert"])
        self.assertFalse(switches["announcement_risk"])
        self.assertFalse(switches["flow_radar"])
        self.assertFalse(switches["funding_alert"])
        self.assertFalse(switches["consolidation_breakout"])
        self.assertTrue(switches["radar_summary"])

    def test_config_disabled_radar_is_not_reported_stale(self) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            settings = Settings(
                base_dir=root,
                data_dir=root,
                pulse_radar_enable=False,
                radar_summary_enable=False,
                funding_alert_enable=False,
                flow_radar_enable=False,
                announcement_risk_enable=False,
                health_runtime_max_age_sec=600,
            )
            store = JsonStore(root)
            store.save(
                settings.runtime_status_path,
                {
                    "updated_at": 1000,
                    "mode": "loop",
                    "task": "loop",
                    "status": "pulse_failed",
                    "pulse_cycle_status": "failed",
                    "real_send": False,
                },
            )

            result = build_market_radar_runtime_status(
                settings,
                store,
                now=1000,
            )

        self.assertEqual(result["status"], "running")
        for item in result["radars"].values():
            self.assertEqual(item["state"], "disabled")
            self.assertEqual(item["state_reason"], "disabled_by_config")
            self.assertFalse(item["schedule_overdue"])

    @patch("runtime.cli.persist_market_batch")
    @patch("runtime.cli.BinanceDataSource")
    def test_shared_snapshot_refresh_does_not_depend_on_radar_switches(
        self,
        source_factory: Mock,
        persist: Mock,
    ) -> None:
        source = source_factory.return_value
        persist.return_value = {"status": "ok", "rows": 5}
        settings = Settings(
            pulse_radar_enable=False,
            radar_summary_enable=False,
            funding_alert_enable=False,
            flow_radar_enable=False,
            announcement_risk_enable=False,
        )

        result = refresh_shared_market_snapshot(settings)

        self.assertEqual(result["status"], "ok")
        persist.assert_called_once_with(settings, source=source)
        source.close.assert_called_once_with()

    @patch("runtime.cli.persist_market_batch", side_effect=RuntimeError("private"))
    def test_shared_snapshot_failure_is_a_fixed_degraded_result(
        self,
        persist: Mock,
    ) -> None:
        source = Mock()

        result = refresh_shared_market_snapshot(Settings(), source=source)

        self.assertEqual(
            result,
            {
                "status": "failed",
                "error": "market_snapshot_refresh_failed",
            },
        )
        persist.assert_called_once()
        source.close.assert_not_called()

    @patch("runtime.cli.Settings.load", side_effect=ValueError("private"))
    def test_invalid_hot_reload_keeps_last_known_settings(
        self,
        _load: Mock,
    ) -> None:
        current = Settings(pulse_radar_enable=True)

        loaded, error = reload_loop_settings(current, args())

        self.assertIs(loaded, current)
        self.assertEqual(error, "settings_reload_failed")

    def test_private_reader_falls_back_to_latest_valid_settings(self) -> None:
        initial = Settings(tg_private_control_alert_enable=True)
        disabled = Settings(tg_private_control_alert_enable=False)
        reader = last_known_settings_reader(initial)

        with patch(
            "runtime.cli.Settings.load",
            side_effect=(disabled, ValueError("private")),
        ):
            self.assertIs(reader(), disabled)
            self.assertIs(reader(), disabled)


if __name__ == "__main__":
    unittest.main()
