from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.config import Settings
from paopao_radar.signal_store import append_from_push
from paopao_radar.web_services.coins import (
    coin_detail_payload,
    coin_search_payload,
    coin_timeline_payload,
    normalize_coin_query,
)


class CoinDetailTests(unittest.TestCase):
    def settings_for(self, tmp: str) -> Settings:
        return Settings(
            data_dir=Path(tmp),
            signal_events_path=Path(tmp) / "signal_events.json",
            signal_events_db_path=Path(tmp) / "signals.db",
        )

    def test_normalize_coin_query_accepts_coin_and_symbol(self) -> None:
        self.assertEqual(normalize_coin_query("btc")["symbol"], "BTCUSDT")
        self.assertEqual(normalize_coin_query("BTCUSDT")["coin"], "BTC")
        self.assertFalse(normalize_coin_query("")["ok"])

    def test_coin_detail_without_signals_returns_empty_ok_payload(self) -> None:
        with TemporaryDirectory() as tmp:
            payload = coin_detail_payload("BTC", settings=self.settings_for(tmp), window_sec=10**10)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["symbol"], "BTCUSDT")
        self.assertEqual(payload["summary"]["total"], 0)
        self.assertEqual(payload["timeline"], [])
        self.assertEqual(payload["latest"], [])

    def test_coin_detail_aggregates_summary_distribution_timeline_and_telegram(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            now = int(time.time()) - 10
            append_from_push(
                settings,
                template_id="TG_LAUNCH_ALERT",
                dedup_key="coin-detail:a",
                status="sent",
                sent=True,
                text="BTCUSDT launch signal",
                ts=now,
                topic_id="12",
                message_ids=[101],
            )
            append_from_push(
                settings,
                template_id="TG_FLOW_RADAR",
                dedup_key="coin-detail:b",
                status="failed",
                sent=False,
                text="BTCUSDT flow failed",
                ts=now + 1,
                topic_id="12",
                message_ids=[102],
            )
            append_from_push(
                settings,
                template_id="TG_FLOW_RADAR",
                dedup_key="coin-detail:c",
                status="sent",
                sent=True,
                text="ETHUSDT flow signal",
                ts=now + 2,
            )

            payload = coin_detail_payload("BTC", settings=settings, window_sec=10**10)
            search = coin_search_payload("", settings=settings, window_sec=10**10)
            btc_search = coin_search_payload("btc", settings=settings, window_sec=10**10)
            timeline = coin_timeline_payload("BTCUSDT", settings=settings)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["summary"]["total"], 2)
        self.assertEqual(payload["summary"]["failed"], 1)
        self.assertEqual(payload["summary"]["health"], "risk")
        self.assertTrue(payload["module_counts"])
        self.assertTrue(payload["status_counts"])
        self.assertTrue(payload["timeline"])
        self.assertEqual(payload["telegram"]["latest_message_ids"], [102, 101])
        self.assertIn("display", payload["latest"][0])
        self.assertTrue(search["items"])
        self.assertEqual(btc_search["items"][0]["symbol"], "BTCUSDT")
        self.assertTrue(timeline["ok"])
        self.assertEqual(timeline["symbol"], "BTCUSDT")
        self.assertTrue(timeline["items"])

    def test_coin_outputs_do_not_expose_sensitive_field_names(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            append_from_push(
                settings,
                template_id="TG_TEST_MESSAGE",
                dedup_key="coin-detail:sensitive",
                status="dry_run",
                sent=False,
                text="BTCUSDT token=123456:abcdefghijklmnopqrstuvwxyz sk-secret-value",
                ts=1000,
            )
            payload = coin_detail_payload("BTC", settings=settings, window_sec=10**10)
            text = json.dumps(payload, ensure_ascii=False)

        self.assertNotIn("123456:abcdefghijklmnopqrstuvwxyz", text)
        self.assertNotIn("sk-secret-value", text)


if __name__ == "__main__":
    unittest.main()
