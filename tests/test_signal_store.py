from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.config import Settings
from paopao_radar.signal_store import SignalEventStore, append_from_push


class SignalEventStoreTests(unittest.TestCase):
    def settings_for(self, tmp: str) -> Settings:
        return Settings(
            data_dir=Path(tmp),
            signal_events_path=Path(tmp) / "signal_events.json",
            signal_events_db_path=Path(tmp) / "signals.db",
        )

    def test_init_creates_table_and_indexes(self) -> None:
        with TemporaryDirectory() as tmp:
            store = SignalEventStore(Path(tmp) / "signals.db")
            with store.connect():
                pass
            with closing(sqlite3.connect(Path(tmp) / "signals.db")) as conn:
                names = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
                    ).fetchall()
                }

        self.assertIn("signals", names)
        self.assertIn("idx_signals_ts", names)
        self.assertIn("idx_signals_symbol_ts", names)
        self.assertIn("idx_signals_module_ts", names)
        self.assertIn("idx_signals_template_ts", names)
        self.assertIn("ux_signals_dedup_symbol", names)

    def test_append_from_push_extracts_multiple_symbols(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            count = append_from_push(
                settings,
                template_id="TG_LAUNCH_ALERT",
                dedup_key="launch:multi",
                status="sent",
                sent=True,
                text="Launch BTCUSDT and ETHUSDT\nScore: 88",
                ts=1000,
                topic_id="12",
                message_ids=[101, 102],
            )
            items = SignalEventStore(settings.signal_events_db_path).list_signals()["items"]

        self.assertEqual(count, 2)
        self.assertEqual({item["symbol"] for item in items}, {"BTCUSDT", "ETHUSDT"})
        self.assertTrue(all(item["module"] == "launch" for item in items))
        self.assertTrue(all(item["message_ids"] == [101, 102] for item in items))

    def test_append_from_push_without_symbol_writes_empty_symbol_event(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            count = append_from_push(
                settings,
                template_id="TG_RADAR_SUMMARY",
                dedup_key="summary:no-symbol",
                status="dry_run",
                sent=False,
                text="推送摘要：本轮没有具体币种。",
                ts=1000,
            )
            items = SignalEventStore(settings.signal_events_db_path).list_signals()["items"]

        self.assertEqual(count, 1)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["symbol"], "")
        self.assertEqual(items[0]["status"], "dry_run")

    def test_duplicate_dedup_key_and_symbol_is_upserted(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            for score in (70, 90):
                append_from_push(
                    settings,
                    template_id="TG_LAUNCH_ALERT",
                    dedup_key="launch:BTC",
                    status="sent",
                    sent=True,
                    text=f"BTCUSDT\n分数: {score}",
                    ts=score,
                    message_ids=[score],
                )
            items = SignalEventStore(settings.signal_events_db_path).list_signals()["items"]

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["symbol"], "BTCUSDT")
        self.assertEqual(items[0]["score"], 90)
        self.assertEqual(items[0]["message_ids"], [90])

    def test_list_signals_supports_limit_cursor_symbol_and_status(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            for idx, (symbol, status) in enumerate((("BTCUSDT", "sent"), ("ETHUSDT", "failed"), ("BTCUSDT", "dry_run")), start=1):
                append_from_push(
                    settings,
                    template_id="TG_FLOW_RADAR",
                    dedup_key=f"flow:{idx}",
                    status=status,
                    sent=status == "sent",
                    text=f"{symbol}\n分数: {idx}",
                    ts=1000 + idx,
                )
            store = SignalEventStore(settings.signal_events_db_path)
            first = store.list_signals(limit=1)
            older = store.list_signals(limit=10, cursor=first["next_cursor"])
            btc = store.list_signals(symbol="BTCUSDT")
            failed = store.list_signals(status="failed")

        self.assertEqual(first["count"], 1)
        self.assertEqual(older["count"], 2)
        self.assertEqual([item["symbol"] for item in btc["items"]], ["BTCUSDT", "BTCUSDT"])
        self.assertEqual(failed["items"][0]["symbol"], "ETHUSDT")

    def test_stats_returns_status_module_and_top_symbols(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            append_from_push(settings, template_id="TG_LAUNCH_ALERT", dedup_key="a", status="sent", sent=True, text="BTCUSDT", ts=1000)
            append_from_push(settings, template_id="TG_FLOW_RADAR", dedup_key="b", status="failed", sent=False, text="BTCUSDT", ts=1001)
            append_from_push(settings, template_id="TG_FLOW_RADAR", dedup_key="c", status="dry_run", sent=False, text="ETHUSDT", ts=1002)
            stats = SignalEventStore(settings.signal_events_db_path).stats(window_sec=10**10)

        self.assertEqual(stats["sent"], 1)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["dry_run"], 1)
        self.assertEqual(stats["by_status"]["sent"], 1)
        self.assertEqual(stats["by_module"]["flow"], 2)
        self.assertEqual(stats["top_symbols"][0]["symbol"], "BTCUSDT")


if __name__ == "__main__":
    unittest.main()
