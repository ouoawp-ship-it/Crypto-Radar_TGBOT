from __future__ import annotations

import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.config import Settings
from paopao_radar.signal_store import SignalEventStore, append_from_push
from paopao_radar.web_services.timeline import (
    group_timeline_by_day,
    timeline_event_display,
    timeline_payload,
    timeline_summary,
)


class TimelineServiceTests(unittest.TestCase):
    def settings_for(self, tmp: str) -> Settings:
        return Settings(
            data_dir=Path(tmp),
            signal_events_path=Path(tmp) / "signal_events.json",
            signal_events_db_path=Path(tmp) / "signals.db",
        )

    def seed(self, settings: Settings) -> None:
        now = int(time.time()) - 100
        append_from_push(settings, template_id="TG_LAUNCH_ALERT", dedup_key="timeline-service:a", status="sent", sent=True, text="BTCUSDT launch signal", ts=now, message_ids=[11], topic_id="2")
        append_from_push(settings, template_id="TG_FLOW_RADAR", dedup_key="timeline-service:b", status="failed", sent=False, text="BTCUSDT flow failed", ts=now + 1)
        append_from_push(settings, template_id="TG_FLOW_RADAR", dedup_key="timeline-service:c", status="sent", sent=True, text="ETHUSDT flow signal", ts=now + 2)

    def test_timeline_event_display_group_and_summary(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            self.seed(settings)
            item = SignalEventStore(settings.signal_events_db_path).list_timeline(symbol="BTC", limit=1)["items"][0]

        event = timeline_event_display(item)
        groups = group_timeline_by_day([item])
        summary = timeline_summary([item])

        self.assertEqual(event["symbol"], "BTCUSDT")
        self.assertIn("module_label", event)
        self.assertIn("status_label", event)
        self.assertIn(event["tone"], {"good", "warn", "bad", "info", "neutral"})
        self.assertTrue(event["telegram"]["has_message"] or isinstance(event["telegram"]["message_ids"], list))
        self.assertEqual(groups[0]["count"], 1)
        self.assertEqual(summary["total"], 1)
        self.assertEqual(summary["module_count"], 1)

    def test_timeline_payload_filters_symbol_module_status_and_q(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            self.seed(settings)
            btc = timeline_payload(symbol="BTC", settings=settings, window_sec=10**10)
            failed = timeline_payload(symbol="BTCUSDT", status="failed", settings=settings, window_sec=10**10)
            flow = timeline_payload(module="flow", settings=settings, window_sec=10**10)
            q = timeline_payload(q="launch", settings=settings, window_sec=10**10)
            empty = timeline_payload(symbol="DOGE", settings=settings, window_sec=10**10)

        self.assertTrue(btc["ok"])
        self.assertEqual(btc["symbol"], "BTCUSDT")
        self.assertEqual(btc["summary"]["total"], 2)
        self.assertTrue(btc["groups"])
        self.assertTrue(btc["items"])
        self.assertEqual(failed["summary"]["failed"], 1)
        self.assertEqual(failed["items"][0]["status"], "failed")
        self.assertEqual(flow["summary"]["total"], 2)
        self.assertEqual(q["items"][0]["module"], "launch")
        self.assertTrue(empty["ok"])
        self.assertEqual(empty["summary"]["total"], 0)
        self.assertEqual(empty["groups"], [])
        self.assertEqual(empty["items"], [])


if __name__ == "__main__":
    unittest.main()
