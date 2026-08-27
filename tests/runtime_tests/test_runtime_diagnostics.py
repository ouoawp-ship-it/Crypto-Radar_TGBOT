from __future__ import annotations

from datetime import datetime
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from config import Settings
from runtime.diagnostics import (
    build_market_radar_runtime_status,
)
from shared.storage import JsonStore


DEFAULT_ENABLED_RADARS = (
    "launch_alert",
    "radar_summary",
    "announcement_risk",
    "funding_alert",
    "flow_radar",
)


class MarketRadarRuntimeStatusTests(unittest.TestCase):
    def runtime(self, root: Path) -> tuple[Settings, JsonStore]:
        settings = Settings(
            base_dir=root,
            data_dir=root,
            runtime_status_path=root / "runtime_status.json",
            flow_candidate_state_path=root / "flow_candidates.json",
            funding_alert_state_path=root / "funding_alert.json",
            health_runtime_max_age_sec=600,
        )
        return settings, JsonStore(root)

    def test_missing_runtime_is_local_safe_and_not_initialized(self) -> None:
        with TemporaryDirectory() as tmp:
            settings, store = self.runtime(Path(tmp))
            result = build_market_radar_runtime_status(
                settings, store, now=2_000_000_000
            )

        self.assertEqual(result["status"], "not_initialized")
        self.assertEqual(result["delivery_mode"], "dry_run")
        self.assertEqual(result["telegram_http_policy"], "zero_by_dry_run")
        self.assertFalse(result["network_activity"])
        self.assertEqual(result["telegram_calls"], 0)
        self.assertTrue(all(
            result["radars"][name]["state"] == "not_running"
            for name in DEFAULT_ENABLED_RADARS
        ))
        self.assertEqual(
            result["radars"]["consolidation_breakout"]["state"],
            "disabled",
        )

    def test_five_radars_report_fresh_runtime_and_dry_run_block(self) -> None:
        now = 2_000_000_000
        stamp = datetime.fromtimestamp(now - 10).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        with TemporaryDirectory() as tmp:
            settings, store = self.runtime(Path(tmp))
            store.save(settings.runtime_status_path, {
                "updated_at": stamp,
                "mode": "loop",
                "task": "loop",
                "status": "running",
                "real_send": False,
                "last_summary_at": stamp,
                "next_summary_at": "later",
                "summary_push": "dry_run",
                "last_launch_at": stamp,
                "next_launch_at": "soon",
                "pulse_cycle_status": "ok",
                "last_flow_at": stamp,
                "next_flow_at": "later",
                "flow_push": "dry_run",
                "last_funding_alert_at": stamp,
                "next_funding_alert_at": "soon",
                "funding_alert_push": "dry_run",
                "last_announcement_at": stamp,
                "next_announcement_at": "later",
                "announcement_risk_push": "dry_run",
                "announcement_risk_candidate_count": 1,
                "announcement_risk_scanned_count": 25,
                "radar_scan_limit": 120,
                "diagnostics": {"pulse": {"simple": {"scanned": 80}}},
            })
            store.save(settings.data_dir / "simple_alert_state.json", {
                "BTCUSDT": {"template": "health_up"},
                "ETHUSDT": {"template": "false_strong"},
                "SOLUSDT": {"template": "health_down"},
            })
            store.save(settings.flow_candidate_state_path, {
                "updated_at": stamp,
                "total_candidates": 147,
                "selected_count": 24,
            })
            store.save(settings.funding_alert_state_path, {
                "updated_at": stamp,
                "last_scanned": 60,
                "last_alert_count": 2,
            })
            result = build_market_radar_runtime_status(
                settings, store, now=now
            )

        self.assertEqual(result["status"], "running")
        self.assertEqual(result["runtime_heartbeat_age_sec"], 10)
        for name in DEFAULT_ENABLED_RADARS:
            item = result["radars"][name]
            self.assertEqual(item["state"], "running")
            self.assertEqual(item["delivery_mode"], "dry_run")
            self.assertEqual(
                item["delivery_block_reason"], "main_bot_dry_run"
            )
            self.assertEqual(item["telegram_http_calls"], 0)
        self.assertEqual(
            result["radars"]["consolidation_breakout"]["state"],
            "disabled",
        )
        self.assertEqual(
            result["radars"]["launch_alert"]["candidate_count"], 3
        )
        self.assertEqual(
            result["radars"]["flow_radar"]["candidate_count"], 147
        )
        self.assertEqual(
            result["radars"]["flow_radar"]["scanned_count"], 24
        )
        self.assertEqual(
            result["radars"]["funding_alert"]["candidate_count"], 2
        )
        self.assertEqual(
            result["radars"]["announcement_risk"]["candidate_count"], 1
        )

    def test_live_real_send_loop_reports_running(self) -> None:
        now = 2_000_000_000
        stamp = datetime.fromtimestamp(now - 10).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        with TemporaryDirectory() as tmp:
            settings, store = self.runtime(Path(tmp))
            store.save(settings.runtime_status_path, {
                "updated_at": stamp,
                "mode": "live",
                "task": "loop",
                "status": "running",
                "real_send": True,
                "last_summary_at": stamp,
                "last_launch_at": stamp,
                "last_flow_at": stamp,
                "last_funding_alert_at": stamp,
                "last_announcement_at": stamp,
            })
            result = build_market_radar_runtime_status(
                settings, store, now=now
            )

        self.assertEqual(result["status"], "running")
        self.assertEqual(result["runtime_mode"], "live")
        self.assertEqual(result["delivery_mode"], "real")
        self.assertTrue(all(
            result["radars"][name]["state"] == "running"
            for name in DEFAULT_ENABLED_RADARS
        ))
        self.assertEqual(
            result["radars"]["consolidation_breakout"]["state"],
            "disabled",
        )

    def test_consolidation_status_separates_universe_from_current_batch(self) -> None:
        now = 2_000_000_000
        stamp = datetime.fromtimestamp(now - 10).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        with TemporaryDirectory() as tmp:
            settings, store = self.runtime(Path(tmp))
            settings = replace(
                settings,
                consolidation_breakout_enable=True,
            )
            store.save(settings.runtime_status_path, {
                "updated_at": stamp,
                "mode": "live",
                "task": "loop",
                "status": "running",
                "real_send": True,
                "last_consolidation_breakout_at": stamp,
                "next_consolidation_breakout_at": "later",
                "consolidation_breakout_cycle_status": "ok",
                "diagnostics": {
                    "consolidation_breakout": {
                        "candidate_count": 524,
                        "scanned_symbol_count": 40,
                        "scanned_pairs": 120,
                    },
                },
            })

            result = build_market_radar_runtime_status(
                settings,
                store,
                now=now,
            )

        radar = result["radars"]["consolidation_breakout"]
        self.assertEqual(radar["state"], "running")
        self.assertEqual(radar["candidate_count"], 524)
        self.assertEqual(radar["scanned_count"], 40)

    def test_stale_heartbeat_does_not_claim_running(self) -> None:
        now = 2_000_000_000
        with TemporaryDirectory() as tmp:
            settings, store = self.runtime(Path(tmp))
            store.save(settings.runtime_status_path, {
                "updated_at": datetime.fromtimestamp(now - 601).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "mode": "loop",
                "task": "loop",
                "status": "running",
                "real_send": False,
            })
            result = build_market_radar_runtime_status(
                settings, store, now=now
            )

        self.assertEqual(result["status"], "stale")
        self.assertFalse(result["runtime_heartbeat_fresh"])
        self.assertTrue(all(
            result["radars"][name]["state"] == "not_running"
            for name in DEFAULT_ENABLED_RADARS
        ))
        self.assertEqual(
            result["radars"]["consolidation_breakout"]["state"],
            "disabled",
        )

    def test_overdue_radar_is_stale_while_loop_heartbeat_is_fresh(self) -> None:
        now = 2_000_000_000
        fresh = datetime.fromtimestamp(now - 5).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        overdue = datetime.fromtimestamp(now - 301).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        with TemporaryDirectory() as tmp:
            settings, store = self.runtime(Path(tmp))
            store.save(settings.runtime_status_path, {
                "updated_at": fresh,
                "mode": "loop",
                "task": "loop",
                "status": "running",
                "real_send": False,
                "last_flow_at": fresh,
                "next_flow_at": overdue,
            })
            result = build_market_radar_runtime_status(
                settings, store, now=now
            )

        flow = result["radars"]["flow_radar"]
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["reason"], "one_or_more_radars_degraded")
        self.assertEqual(flow["state"], "stale")
        self.assertEqual(flow["state_reason"], "scheduled_cycle_overdue")
        self.assertTrue(flow["schedule_overdue"])

    def test_disabled_radar_does_not_report_schedule_overdue(self) -> None:
        now = 2_000_000_000
        stamp = datetime.fromtimestamp(now - 5).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        overdue = datetime.fromtimestamp(now - 600).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        with TemporaryDirectory() as tmp:
            settings, store = self.runtime(Path(tmp))
            store.save(settings.runtime_status_path, {
                "updated_at": stamp,
                "mode": "loop",
                "task": "loop",
                "status": "running",
                "real_send": False,
                "no_flow": True,
                "last_flow_at": stamp,
                "next_flow_at": overdue,
            })
            result = build_market_radar_runtime_status(
                settings, store, now=now
            )

        flow = result["radars"]["flow_radar"]
        self.assertEqual(flow["state"], "disabled")
        self.assertFalse(flow["schedule_overdue"])

    def test_one_failed_cycle_stays_degraded_without_error_body(self) -> None:
        now = 2_000_000_000
        secret = "private endpoint body must not leak"
        with TemporaryDirectory() as tmp:
            settings, store = self.runtime(Path(tmp))
            store.save(settings.runtime_status_path, {
                "updated_at": datetime.fromtimestamp(now).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "mode": "loop",
                "task": "loop",
                "status": "running",
                "real_send": False,
                "last_summary_at": datetime.fromtimestamp(now).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "summary_cycle_status": "failed",
                "summary_error_code": "TimeoutError",
                "last_error": secret,
            })
            result = build_market_radar_runtime_status(
                settings, store, now=now
            )

        summary = result["radars"]["radar_summary"]
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(summary["state"], "degraded")
        self.assertEqual(summary["last_error_code"], "TimeoutError")
        self.assertNotIn(secret, json.dumps(result))

    def test_runtime_diagnostics_never_echo_unrelated_secrets(self) -> None:
        secret = "secret-bot-token-and-private-url"
        with TemporaryDirectory() as tmp:
            settings, store = self.runtime(Path(tmp))
            store.save(settings.runtime_status_path, {
                "updated_at": datetime.fromtimestamp(2_000_000_000).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "mode": "loop",
                "task": "loop",
                "status": "running",
                "real_send": False,
                "mode": secret,
                "task": secret,
                "last_summary_at": secret,
                "next_summary_at": secret,
                "radar_scan_limit": secret,
                "diagnostics": {"provider_body": secret},
            })
            result = build_market_radar_runtime_status(
                settings, store, now=2_000_000_000
            )

        self.assertNotIn(secret, json.dumps(result))
        self.assertEqual(result["runtime_mode"], "unknown")
        self.assertEqual(result["runtime_task"], "unknown")
        self.assertEqual(
            result["radars"]["radar_summary"]["last_run_at"], ""
        )
        self.assertFalse(result["credentials_exposed"])


if __name__ == "__main__":
    unittest.main()
