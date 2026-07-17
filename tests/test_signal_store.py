from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.config import Settings
from paopao_radar.signal_store import SignalEventStore, append_from_push, signal_public_ref


class CountingSignalEventStore(SignalEventStore):
    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "connection_count", 0)

    @contextmanager
    def connect(self):
        object.__setattr__(self, "connection_count", self.connection_count + 1)
        with super().connect() as conn:
            yield conn


class SignalEventStoreTests(unittest.TestCase):
    def settings_for(self, tmp: str) -> Settings:
        return Settings(
            data_dir=Path(tmp),
            signal_events_path=Path(tmp) / "signal_events.json",
            signal_events_db_path=Path(tmp) / "signals.db",
        )

    def test_init_creates_table_indexes_and_compat_view(self) -> None:
        with TemporaryDirectory() as tmp:
            store = SignalEventStore(Path(tmp) / "signals.db")
            with store.connect():
                pass
            with closing(sqlite3.connect(Path(tmp) / "signals.db")) as conn:
                objects = {
                    row[1]: row[0]
                    for row in conn.execute(
                        "SELECT type, name FROM sqlite_master WHERE type IN ('table', 'index', 'view')"
                    ).fetchall()
                }

        self.assertEqual(objects["signals"], "table")
        self.assertEqual(objects["signal_events"], "view")
        self.assertEqual(objects["idx_signals_ts"], "index")
        self.assertEqual(objects["idx_signals_symbol_ts"], "index")
        self.assertEqual(objects["idx_signals_module_ts"], "index")
        self.assertEqual(objects["idx_signals_template_ts"], "index")
        self.assertEqual(objects["ux_signals_dedup_symbol"], "index")

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

    def test_symbol_extraction_ignores_encoded_tradingview_url(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            count = append_from_push(
                settings,
                template_id="TG_FLOW_RADAR",
                dedup_key="flow:url-artifact",
                status="sent",
                sent=True,
                text='<a href="https://tradingview.com/chart/?symbol=BINANCE%3ABTCUSDT.P">BTCUSDT</a> 75分',
                ts=1000,
            )
            items = SignalEventStore(settings.signal_events_db_path).list_signals()["items"]

        self.assertEqual(count, 1)
        self.assertEqual([item["symbol"] for item in items], ["BTCUSDT"])
        self.assertEqual(items[0]["score"], 75)
        self.assertEqual(items[0]["ingest_mode"], "text_fallback")

    def test_structured_records_persist_per_symbol_facts(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            append_from_push(
                settings,
                template_id="TG_LAUNCH_ALERT",
                dedup_key="launch:structured",
                status="sent",
                sent=True,
                text="BTCUSDT 75分 ETHUSDT 61分",
                ts=1000,
                structured_records=[
                    {"symbol": "BTCUSDT", "score": 75, "stage": "breakout", "price": 100.5},
                    {"symbol": "ETHUSDT", "score": 61, "stage": "watch", "price": 25.5},
                ],
            )
            items = SignalEventStore(settings.signal_events_db_path).list_signals(limit=10)["items"]

        by_symbol = {item["symbol"]: item for item in items}
        self.assertEqual(by_symbol["BTCUSDT"]["score"], 75)
        self.assertEqual(by_symbol["ETHUSDT"]["score"], 61)
        self.assertEqual(by_symbol["BTCUSDT"]["stage"], "breakout")
        self.assertEqual(by_symbol["BTCUSDT"]["ingest_mode"], "structured")
        self.assertEqual(by_symbol["BTCUSDT"]["quality_status"], "ready")
        self.assertEqual(by_symbol["BTCUSDT"]["payload"]["facts"]["price"], 100.5)

    def test_repair_legacy_signals_is_auditable_and_backed_up(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            store = SignalEventStore(settings.signal_events_db_path)
            store.append_from_push(
                template_id="TG_FLOW_RADAR",
                dedup_key="legacy:real",
                status="sent",
                sent=True,
                text='<a href="https://tradingview.com/?symbol=BINANCE%3ABTCUSDT.P">BTCUSDT</a> 82分',
                ts=1000,
            )
            with store.connect() as conn:
                conn.execute(
                    "UPDATE signals SET symbol = '3ABTCUSDT', coin = '3ABTC', score = NULL WHERE dedup_key = 'legacy:real'"
                )
                conn.execute(
                    "INSERT INTO signals (ts, time, module, template_id, signal_type, symbol, coin, dedup_key, status, sent, text_html) "
                    "VALUES (1001, '1970-01-01T00:16:41+00:00', 'flow', 'TG_FLOW_RADAR', 'flow', "
                    "'ETHUSDT', 'ETH', 'legacy:score', 'sent', 1, 'ETHUSDT 79分')"
                )
                conn.commit()

            dry_run = store.repair_legacy_signals()
            applied = store.repair_legacy_signals(apply=True)
            remaining = store.list_signals(limit=10)["items"]
            backup_exists = Path(applied["backup_path"]).exists()

        self.assertEqual(dry_run["artifact_rows"], 1)
        self.assertEqual(dry_run["recoverable_scores"], 1)
        self.assertFalse(dry_run["applied"])
        self.assertEqual(applied["deleted"], 1)
        self.assertEqual(applied["scores_recovered"], 1)
        self.assertTrue(backup_exists)
        self.assertEqual([item["symbol"] for item in remaining], ["ETHUSDT"])
        self.assertEqual(remaining[0]["score"], 79)

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

    def test_list_signals_supports_sort_and_time_range(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            for idx, symbol in enumerate(("BTCUSDT", "ETHUSDT", "SOLUSDT"), start=1):
                append_from_push(
                    settings,
                    template_id="TG_FLOW_RADAR",
                    dedup_key=f"range:{idx}",
                    status="sent",
                    sent=True,
                    text=symbol,
                    ts=1000 + idx,
                )
            store = SignalEventStore(settings.signal_events_db_path)
            asc = store.list_signals(limit=3, sort_field="ts", sort_direction="asc")
            ranged = store.list_signals(limit=10, start_ts=1002, end_ts=1002)

        self.assertEqual([item["symbol"] for item in asc["items"]], ["BTCUSDT", "ETHUSDT", "SOLUSDT"])
        self.assertEqual(ranged["count"], 1)
        self.assertEqual(ranged["items"][0]["symbol"], "ETHUSDT")

    def test_list_signals_supports_q_search(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            append_from_push(
                settings,
                template_id="TG_FLOW_RADAR",
                dedup_key="search:btc",
                status="sent",
                sent=True,
                text="BTCUSDT strong flow breakout",
                ts=1000,
            )
            append_from_push(
                settings,
                template_id="TG_FUNDING_RADAR",
                dedup_key="search:eth",
                status="sent",
                sent=True,
                text="ETHUSDT funding watch",
                ts=1001,
            )
            store = SignalEventStore(settings.signal_events_db_path)
            btc = store.list_signals(q="btc")
            funding = store.list_signals(q="funding")

        self.assertEqual(btc["count"], 1)
        self.assertEqual(btc["items"][0]["symbol"], "BTCUSDT")
        self.assertEqual(funding["count"], 1)
        self.assertEqual(funding["items"][0]["symbol"], "ETHUSDT")

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

    def test_symbol_queries_support_coin_detail_views(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            append_from_push(settings, template_id="TG_LAUNCH_ALERT", dedup_key="coin:a", status="sent", sent=True, text="BTCUSDT launch", ts=1000)
            append_from_push(settings, template_id="TG_FLOW_RADAR", dedup_key="coin:b", status="failed", sent=False, text="BTCUSDT flow", ts=1001)
            append_from_push(settings, template_id="TG_FLOW_RADAR", dedup_key="coin:c", status="sent", sent=True, text="ETHUSDT flow", ts=1002)
            store = SignalEventStore(settings.signal_events_db_path)
            active = store.search_symbols(limit=10, start_ts=999, end_ts=1003)
            btc_search = store.search_symbols(q="btc", limit=10, start_ts=999, end_ts=1003)
            btc_stats = store.stats_by_symbol("BTC", start_ts=999, end_ts=1003)
            first = store.list_by_symbol("BTCUSDT", limit=1, start_ts=999, end_ts=1003)
            older = store.list_by_symbol("BTC", limit=10, cursor=first["next_cursor"], start_ts=999, end_ts=1003)

        self.assertEqual(active[0]["symbol"], "BTCUSDT")
        self.assertEqual(active[0]["count"], 2)
        self.assertEqual(btc_search[0]["symbol"], "BTCUSDT")
        self.assertEqual(btc_stats["total"], 2)
        self.assertEqual(btc_stats["failed"], 1)
        self.assertEqual(btc_stats["by_module"]["flow"], 1)
        self.assertEqual(first["count"], 1)
        self.assertEqual(older["count"], 1)

    def test_timeline_queries_support_filters_and_special_search_text(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            append_from_push(settings, template_id="TG_LAUNCH_ALERT", dedup_key="timeline:a", status="sent", sent=True, text="BTCUSDT launch alpha", ts=1000)
            append_from_push(settings, template_id="TG_FLOW_RADAR", dedup_key="timeline:b", status="failed", sent=False, text="BTCUSDT flow beta", ts=1001)
            append_from_push(settings, template_id="TG_FLOW_RADAR", dedup_key="timeline:c", status="sent", sent=True, text="ETHUSDT flow alpha_%", ts=1002)
            store = SignalEventStore(settings.signal_events_db_path)
            btc = store.list_timeline(symbol="BTC", limit=10, start_ts=999, end_ts=1003)
            failed = store.list_timeline(status="failed", limit=10, start_ts=999, end_ts=1003)
            flow = store.list_timeline(module="flow", limit=10, start_ts=999, end_ts=1003)
            alpha = store.list_timeline(q="alpha", limit=10, start_ts=999, end_ts=1003)
            special = store.list_timeline(q="alpha_%", limit=10, start_ts=999, end_ts=1003)
            stats = store.timeline_stats(symbol="BTCUSDT", start_ts=999, end_ts=1003)

        self.assertEqual(btc["count"], 2)
        self.assertEqual(failed["count"], 1)
        self.assertEqual(failed["items"][0]["status"], "failed")
        self.assertEqual(flow["count"], 2)
        self.assertEqual(alpha["count"], 2)
        self.assertIsInstance(special["items"], list)
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["by_module"]["flow"], 1)

    def test_signal_events_view_matches_signals_table(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            append_from_push(
                settings,
                template_id="TG_TEST_MESSAGE",
                dedup_key="test:view",
                status="dry_run",
                sent=False,
                text="Telegram test message",
                ts=1000,
            )
            with closing(sqlite3.connect(settings.signal_events_db_path)) as conn:
                conn.row_factory = sqlite3.Row
                signals_count = conn.execute("SELECT COUNT(*) AS c FROM signals").fetchone()["c"]
                compat_count = conn.execute("SELECT COUNT(*) AS c FROM signal_events").fetchone()["c"]
                latest = conn.execute(
                    """
                    SELECT template_id, status, module, excerpt
                    FROM signal_events
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()

        self.assertEqual(signals_count, 1)
        self.assertEqual(compat_count, 1)
        self.assertEqual(latest["template_id"], "TG_TEST_MESSAGE")
        self.assertEqual(latest["status"], "dry_run")
        self.assertEqual(latest["module"], "test")
        self.assertIn("Telegram test message", latest["excerpt"])

    def test_existing_signals_database_gets_signal_events_view(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signals.db"
            store = SignalEventStore(db_path)
            with store.connect():
                pass
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute("DROP VIEW signal_events")
                conn.commit()
                missing = conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE name = 'signal_events'"
                ).fetchone()[0]
            self.assertEqual(missing, 0)

            with store.connect():
                pass

            with closing(sqlite3.connect(db_path)) as conn:
                row = conn.execute(
                    "SELECT type FROM sqlite_master WHERE name = 'signal_events'"
                ).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(row[0], "view")

    def test_current_schema_check_does_not_rewrite_metadata(self) -> None:
        with TemporaryDirectory() as tmp:
            store = SignalEventStore(Path(tmp) / "signals.db")
            with store.connect():
                pass

            with store.connect() as conn:
                changes = conn.total_changes

        self.assertEqual(changes, 0)

    def test_schema_upgrade_removes_records_from_retired_modules(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            for index in (1, 2):
                append_from_push(
                    settings,
                    template_id="TG_FLOW_RADAR",
                    dedup_key=f"migration:{index}",
                    status="sent",
                    sent=True,
                    text=f"BTCUSDT migration {index}",
                    ts=1000 + index,
                )
            with closing(sqlite3.connect(settings.signal_events_db_path)) as conn:
                conn.execute("UPDATE signals SET module = 'retired' WHERE dedup_key = 'migration:1'")
                conn.execute(
                    "UPDATE signal_store_meta SET value = '3' WHERE key = 'schema_version'"
                )
                conn.commit()

            store = SignalEventStore(settings.signal_events_db_path)
            with store.connect():
                pass
            items = store.list_signals(limit=10)["items"]

        self.assertEqual([item["dedup_key"] for item in items], ["migration:2"])

    def test_list_by_symbols_limits_each_symbol_in_one_query(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            for index, symbol in enumerate(("BTCUSDT", "BTCUSDT", "ETHUSDT", "ETHUSDT"), 1):
                append_from_push(
                    settings,
                    template_id="TG_TEST_MESSAGE",
                    dedup_key=f"batch:{index}",
                    status="sent",
                    sent=True,
                    text=f"{symbol} test {index}",
                    ts=1000 + index,
                )
            grouped = SignalEventStore(settings.signal_events_db_path).list_by_symbols(
                ["BTC", "ETHUSDT"],
                limit_per_symbol=1,
            )

        self.assertEqual(set(grouped), {"BTCUSDT", "ETHUSDT"})
        self.assertEqual([item["id"] for item in grouped["BTCUSDT"]], [2])
        self.assertEqual([item["id"] for item in grouped["ETHUSDT"]], [4])

    def test_compact_projection_preserves_shape_and_defers_large_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            append_from_push(
                settings,
                template_id="TG_FLOW_RADAR",
                dedup_key="compact:btc",
                status="sent",
                sent=True,
                text="BTCUSDT " + ("x" * 5000),
                ts=1000,
            )
            store = SignalEventStore(settings.signal_events_db_path)
            compact = store.list_signals(limit=1, compact=True)["items"][0]
            detail = store.signal_detail(int(compact["id"])) or {}

        self.assertEqual(set(compact), set(detail))
        self.assertEqual(compact["text_html"], "")
        self.assertEqual(compact["payload"], {})
        self.assertLessEqual(len(compact["excerpt"]), 260)
        self.assertGreater(len(detail["text_html"]), 5000)
        self.assertEqual(detail["payload"]["source"], "telegram_push")

    def test_stats_with_latest_uses_one_connection(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            append_from_push(
                settings,
                template_id="TG_FLOW_RADAR",
                dedup_key="stats:one-connection",
                status="sent",
                sent=True,
                text="BTCUSDT stats",
                ts=1000,
            )
            store = CountingSignalEventStore(settings.signal_events_db_path)
            payload = store.stats_with_latest(window_sec=10**10)

        self.assertEqual(store.connection_count, 1)
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["latest_sent"][0]["symbol"], "BTCUSDT")

    def test_signal_events_view_defaults_missing_legacy_columns(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signals.db"
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    """
                    CREATE TABLE signals (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts INTEGER NOT NULL,
                        time TEXT NOT NULL,
                        module TEXT NOT NULL,
                        template_id TEXT NOT NULL,
                        signal_type TEXT NOT NULL,
                        symbol TEXT NOT NULL DEFAULT '',
                        coin TEXT NOT NULL DEFAULT '',
                        stage TEXT NOT NULL DEFAULT '',
                        severity TEXT NOT NULL DEFAULT 'info',
                        score REAL,
                        title TEXT NOT NULL DEFAULT '',
                        excerpt TEXT NOT NULL DEFAULT '',
                        text_html TEXT NOT NULL DEFAULT '',
                        dedup_key TEXT NOT NULL,
                        status TEXT NOT NULL,
                        sent INTEGER NOT NULL DEFAULT 0,
                        topic_id TEXT NOT NULL DEFAULT '',
                        message_ids_json TEXT NOT NULL DEFAULT '[]',
                        reply_to_message_id INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO signals (
                        ts, time, module, template_id, signal_type, symbol, coin, stage, severity,
                        score, title, excerpt, text_html, dedup_key, status, sent, topic_id,
                        message_ids_json, reply_to_message_id
                    ) VALUES (
                        1000, '1970-01-01T00:16:40+00:00', 'test', 'TG_TEST_MESSAGE', '测试',
                        'BTCUSDT', 'BTC', '', 'info', NULL, 'title', 'excerpt', 'body',
                        'dedup', 'dry_run', 0, '', '[]', 0
                    )
                    """
                )
                conn.commit()

            store = SignalEventStore(db_path)
            with store.connect():
                pass

            with closing(sqlite3.connect(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                latest = conn.execute(
                    """
                    SELECT payload_json, error, public_ref
                    FROM signal_events
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()
            inserted_count = store.append_from_push(
                template_id="TG_TEST_MESSAGE",
                dedup_key="legacy:write",
                status="sent",
                sent=True,
                text="ETHUSDT legacy migration write",
                ts=1001,
            )
            with closing(sqlite3.connect(db_path)) as conn:
                migrated = conn.execute(
                    "SELECT payload_json, error FROM signals WHERE dedup_key = ?",
                    ("legacy:write",),
                ).fetchone()

        self.assertEqual(latest["payload_json"], "{}")
        self.assertEqual(latest["error"], "")
        self.assertEqual(latest["public_ref"], signal_public_ref("dedup", "BTCUSDT"))
        self.assertEqual(inserted_count, 1)
        self.assertIsNotNone(migrated)
        self.assertIn("telegram_push", migrated[0])
        self.assertEqual(migrated[1], "")


if __name__ == "__main__":
    unittest.main()
