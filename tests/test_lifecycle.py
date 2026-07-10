from __future__ import annotations

import json
import sqlite3
import time
import unittest
from contextlib import closing, contextmanager, redirect_stdout
from io import BytesIO, StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from paopao_radar import cli, web
from paopao_radar.binance_lifecycle_data import (
    BinanceLifecycleDataClient,
    futures_cvd_from_taker_rows,
    spot_cvd_from_agg_trades,
)
from paopao_radar.config import Settings
from paopao_radar.lifecycle_engine import (
    LifecycleEngine,
    build_lifecycle_telegram_message,
    extract_signal_level,
    scan_lifecycles,
)
from paopao_radar.lifecycle_store import LifecycleStore
from paopao_radar.signal_store import append_from_push
from paopao_radar.web_services.lifecycle import (
    lifecycle_detail_payload,
    lifecycle_list_payload,
    public_lifecycle_detail_payload,
    public_lifecycle_list_payload,
    public_lifecycle_summary_payload,
)


def make_settings(tmp: str) -> Settings:
    base = Path(tmp)
    return Settings(
        data_dir=base,
        signal_events_path=base / "signals.json",
        signal_events_db_path=base / "signals.db",
        lifecycle_db_path=base / "lifecycle.db",
        tg_push_history_path=base / "push_history.json",
    )


def signal(signal_id: int, symbol: str = "BTCUSDT", *, level: str = "15m", ts: int | None = None, text: str = "") -> dict:
    now = int(time.time()) if ts is None else ts
    return {
        "id": signal_id,
        "symbol": symbol,
        "status": "sent",
        "module": "launch",
        "template_id": "TG_LAUNCH_ALERT",
        "timeframe": level,
        "signal_type": "launch",
        "stage": "启动确认",
        "score": 82,
        "excerpt": text or f"{symbol} {level} lifecycle signal",
        "time": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now)),
        "ts": now,
    }


def metrics(
    price: float = 100,
    oi: float = 100,
    futures_cvd: float | None = 10,
    spot_cvd: float | None = 10,
    funding: float = 0.0001,
    volume: float = 100,
) -> dict:
    return {
        "symbol": "BTCUSDT",
        "timeframe": "15m",
        "price": price,
        "volume": volume,
        "quote_volume": volume * price,
        "oi": oi,
        "oi_value_usdt": oi * price,
        "futures_cvd_delta": futures_cvd,
        "spot_cvd_delta": spot_cvd,
        "funding_rate": funding,
        "market_cap_usd": 100_000_000,
        "data_source": "binance",
        "data_source_status": "ok",
        "exchange_context": {"items": [{"exchange": "okx", "status": "side_observation"}]},
    }


class LifecycleStoreTests(unittest.TestCase):
    def test_schema_is_idempotent_and_tables_exist(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "lifecycle.db"
            store = LifecycleStore(db)
            store.ensure_schema()
            store.ensure_schema()
            with closing(sqlite3.connect(db)) as conn:
                objects = {
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
                    ).fetchall()
                }

        self.assertIn("signal_lifecycles", objects)
        self.assertIn("lifecycle_events", objects)
        self.assertIn("lifecycle_metric_snapshots", objects)
        self.assertIn("idx_signal_lifecycles_state", objects)
        self.assertIn("idx_lifecycle_events_symbol", objects)

    def test_create_lifecycle_is_unique_by_symbol_and_event_dedupes(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            engine = LifecycleEngine(settings, metrics_provider=lambda _symbol, _level: metrics())
            first = engine.process_signal(signal(1, level="15m"))
            second = engine.process_signal(signal(2, level="15m"))
            duplicate = engine.process_signal(signal(2, level="15m"))
            store = LifecycleStore(settings.lifecycle_db_path)
            listed = store.list_lifecycles(limit=10)["items"]
            events = store.list_events(symbol="BTCUSDT", limit=10)

        self.assertTrue(first["created"])
        self.assertTrue(second["event_inserted"])
        self.assertFalse(duplicate["event_inserted"])
        self.assertEqual(len(listed), 1)
        self.assertEqual(len(events), 2)

    def test_lifecycle_batch_rolls_back_lifecycle_event_and_snapshot_together(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            candidates = [signal(1, level="15m"), signal(2, level="15m")]
            original_insert_snapshot = LifecycleStore.insert_snapshot
            snapshot_calls = 0

            def fail_second_snapshot(store, values, *, dry_run=False, conn=None):
                nonlocal snapshot_calls
                snapshot_calls += 1
                if snapshot_calls == 2:
                    raise RuntimeError("snapshot write failed")
                return original_insert_snapshot(store, values, dry_run=dry_run, conn=conn)

            with patch("paopao_radar.lifecycle_engine.candidate_lifecycle_signals", return_value=candidates):
                with patch.object(LifecycleStore, "insert_snapshot", new=fail_second_snapshot):
                    with self.assertRaisesRegex(RuntimeError, "snapshot write failed"):
                        scan_lifecycles(settings=settings, metrics_provider=lambda _symbol, _level: metrics())

            with closing(sqlite3.connect(settings.lifecycle_db_path)) as conn:
                counts = {
                    table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                    for table in ("signal_lifecycles", "lifecycle_events", "lifecycle_metric_snapshots")
                }

        self.assertEqual(counts, {"signal_lifecycles": 0, "lifecycle_events": 0, "lifecycle_metric_snapshots": 0})


class LifecycleEngineTests(unittest.TestCase):
    def test_extract_signal_level_supports_core_timeframes(self) -> None:
        cases = [
            ("15m", "15m", 1),
            ("1h", "1h", 2),
            ("4H", "4h", 3),
            ("日线", "24h", 4),
            ("unknown text", "unknown", 0),
        ]
        for text, expected, rank in cases:
            with self.subTest(text=text):
                self.assertEqual(extract_signal_level({"excerpt": text}), (expected, rank))

    def test_first_signal_and_timeframe_upgrades_update_state(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            engine = LifecycleEngine(settings, metrics_provider=lambda _symbol, _level: metrics())
            engine.process_signal(signal(1, level="15m"))
            one_hour = engine.process_signal(signal(2, level="1h"))
            four_hour = engine.process_signal(signal(3, level="4h"))
            daily = engine.process_signal(signal(4, level="24h"))
            store = LifecycleStore(settings.lifecycle_db_path)
            lifecycle = store.get_lifecycle("BTCUSDT") or {}
            event_types = [item["event_type"] for item in store.list_events(symbol="BTCUSDT", limit=10)]

        self.assertEqual(one_hour["event"]["event_type"], "timeframe_upgrade_1h")
        self.assertEqual(four_hour["event"]["event_type"], "timeframe_upgrade_4h")
        self.assertEqual(daily["event"]["event_type"], "timeframe_upgrade_24h")
        self.assertEqual(lifecycle["highest_level"], "24h")
        self.assertEqual(lifecycle["current_state"], "trend_confirmed")
        self.assertIn("timeframe_upgrade_1h", event_types)
        self.assertIn("timeframe_upgrade_4h", event_types)
        self.assertIn("timeframe_upgrade_24h", event_types)

    def test_same_level_confirm_and_risk_warning_from_oi_price_divergence(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            metric_queue = [metrics(price=100, oi=100), metrics(price=104, oi=112), metrics(price=95, oi=130, spot_cvd=-5)]
            engine = LifecycleEngine(settings, metrics_provider=lambda _symbol, _level: metric_queue.pop(0))
            engine.process_signal(signal(1, level="1h"))
            confirm = engine.process_signal(signal(2, level="1h"))
            risk = engine.process_signal(signal(3, level="1h"))
            lifecycle = LifecycleStore(settings.lifecycle_db_path).get_lifecycle("BTCUSDT") or {}

        self.assertEqual(confirm["event"]["event_type"], "same_level_confirm")
        self.assertEqual(risk["event"]["event_type"], "oi_price_divergence")
        self.assertEqual(lifecycle["current_state"], "risk_warning")

    def test_spot_cvd_unavailable_does_not_block_lifecycle(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            engine = LifecycleEngine(settings, metrics_provider=lambda _symbol, _level: metrics(spot_cvd=None))
            result = engine.process_signal(signal(1, level="15m"))
            lifecycle = LifecycleStore(settings.lifecycle_db_path).get_lifecycle("BTCUSDT") or {}

        self.assertTrue(result["ok"])
        self.assertEqual(lifecycle["symbol"], "BTCUSDT")
        self.assertIsNone(lifecycle["first_spot_cvd_15m"])

    def test_funding_crowded_increases_risk_score(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            metric_queue = [metrics(price=100, oi=100), metrics(price=110, oi=105, funding=0.001)]
            engine = LifecycleEngine(settings, metrics_provider=lambda _symbol, _level: metric_queue.pop(0))
            engine.process_signal(signal(1, level="15m"))
            result = engine.process_signal(signal(2, level="15m"))
            lifecycle = LifecycleStore(settings.lifecycle_db_path).get_lifecycle("BTCUSDT") or {}

        self.assertEqual(result["event"]["event_type"], "funding_crowded")
        self.assertGreaterEqual(float(lifecycle["risk_score"]), 20)

    def test_lifecycle_scan_dry_run_does_not_create_lifecycle_db(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            append_from_push(
                settings,
                template_id="TG_LAUNCH_ALERT",
                dedup_key="lifecycle:dry-run",
                status="sent",
                sent=True,
                text="BTCUSDT 15m 启动观察",
                ts=int(time.time()),
            )
            result = scan_lifecycles(settings=settings, dry_run=True, lookback_hours=24, limit_symbols=20)

        self.assertTrue(result["ok"])
        self.assertFalse(Path(tmp, "lifecycle.db").exists())

    def test_scan_reuses_metrics_per_symbol_timeframe(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            candidates = [
                signal(1, level="15m"),
                signal(2, level="15m"),
                signal(3, level="1h"),
            ]
            provider_calls: list[tuple[str, str]] = []

            def provider(symbol: str, timeframe: str) -> dict:
                provider_calls.append((symbol, timeframe))
                return metrics()

            with patch("paopao_radar.lifecycle_engine.candidate_lifecycle_signals", return_value=candidates):
                result = scan_lifecycles(settings=settings, metrics_provider=provider)

        self.assertEqual(result["counts"]["events"], 3)
        self.assertEqual(provider_calls, [("BTCUSDT", "15m"), ("BTCUSDT", "1h")])

    def test_processed_unavailable_signal_is_skipped_before_provider_call(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            candidates = [signal(1, level="15m")]
            provider_calls = 0

            def unavailable_provider(_symbol: str, _timeframe: str) -> dict:
                nonlocal provider_calls
                provider_calls += 1
                raise TimeoutError("market data timeout")

            with patch("paopao_radar.lifecycle_engine.candidate_lifecycle_signals", return_value=candidates):
                first = scan_lifecycles(settings=settings, metrics_provider=unavailable_provider)
                second = scan_lifecycles(settings=settings, metrics_provider=unavailable_provider)

            events = LifecycleStore(settings.lifecycle_db_path).list_events(symbol="BTCUSDT")

        self.assertEqual(first["counts"]["events"], 1)
        self.assertEqual(second["counts"]["events"], 0)
        self.assertEqual(second["counts"]["skipped"], 1)
        self.assertEqual(provider_calls, 1)
        self.assertEqual(events[0]["metrics"]["data_source_status"], "unavailable")

    def test_dry_run_with_selected_signal_does_not_create_database(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            with patch("paopao_radar.lifecycle_engine.candidate_lifecycle_signals", return_value=[signal(1)]):
                result = scan_lifecycles(settings=settings, dry_run=True)

            self.assertTrue(result["ok"])
            self.assertFalse(settings.lifecycle_db_path.exists())

    def test_cli_lifecycle_commands_use_engine_helpers(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch("paopao_radar.cli.Settings.load", return_value=make_settings(tmp)):
                with patch("paopao_radar.cli.backfill_lifecycles", return_value={"ok": True, "counts": {"created": 0}}):
                    with redirect_stdout(StringIO()) as output:
                        code = cli.main(["lifecycle-backfill", "--lookback-hours", "168", "--dry-run"])
            self.assertEqual(code, 0)
            self.assertIn("生命周期", output.getvalue())


class BinanceLifecycleDataTests(unittest.TestCase):
    def test_futures_and_spot_cvd_helpers(self) -> None:
        futures = futures_cvd_from_taker_rows([
            {"buyVol": "8", "sellVol": "5"},
            {"buyVol": "4", "sellVol": "1"},
        ])
        spot = spot_cvd_from_agg_trades([
            {"p": "10", "q": "2", "m": False},
            {"p": "10", "q": "1", "m": True},
        ])

        self.assertEqual(futures["futures_cvd_delta"], 6.0)
        self.assertEqual(futures["futures_cvd_status"], "主动买入增强")
        self.assertEqual(spot["spot_cvd_delta"], 10.0)
        self.assertEqual(spot["spot_cvd_status"], "现货买盘跟随")

    def test_binance_timeout_or_429_degrades_to_unavailable(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            client = BinanceLifecycleDataClient(settings)
            with patch.object(client.source, "http", side_effect=TimeoutError("429 too many requests")):
                snapshot = client.snapshot("BTCUSDT", "15m")

        self.assertEqual(snapshot["data_source_status"], "unavailable")
        self.assertIn("Binance", snapshot["data_source_reason"])

    def test_market_cap_map_is_parsed_once_for_multiple_symbols(self) -> None:
        with TemporaryDirectory() as tmp:
            client = BinanceLifecycleDataClient(make_settings(tmp))
            with patch.object(
                client.source,
                "coinpaprika_market_caps",
                return_value={"BTC": 1_000_000, "ETH": 500_000},
            ) as market_caps:
                btc = client.market_cap("BTCUSDT")
                eth = client.market_cap("ETHUSDT")

        self.assertEqual(btc["market_cap_usd"], 1_000_000)
        self.assertEqual(eth["market_cap_usd"], 500_000)
        market_caps.assert_called_once_with()


class LifecycleApiTests(unittest.TestCase):
    def test_public_payloads_are_redacted_and_private_payloads_keep_detail(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            engine = LifecycleEngine(settings, metrics_provider=lambda _symbol, _level: metrics())
            engine.process_signal(signal(1, level="15m", text="token=hidden chat_id=hidden topic_id=42"))

            public_summary = public_lifecycle_summary_payload(settings=settings)
            public_list = public_lifecycle_list_payload(settings=settings)
            public_detail = public_lifecycle_detail_payload("BTCUSDT", settings=settings)
            private_detail = lifecycle_detail_payload("BTCUSDT", settings=settings)

        self.assertTrue(public_summary["ok"])
        self.assertTrue(public_list["ok"])
        self.assertTrue(public_detail["ok"])
        self.assertTrue(private_detail["ok"])
        text = repr(public_detail) + repr(public_list) + repr(public_summary)
        for forbidden in (
            "dedup_key",
            "topic_id",
            "message_id",
            "chat_id",
            "payload_json",
            "text_html",
            "WEB_ADMIN_TOKEN",
            "WEB_SESSION_SECRET",
            "Authorization",
            "Cookie",
        ):
            self.assertNotIn(forbidden, text)
        self.assertIn("BTCUSDT", text)

    def test_web_routes_public_lifecycle_and_private_401(self) -> None:
        def make_handler(path: str):
            statuses: list[int] = []
            headers: list[tuple[str, str]] = []
            handler = object.__new__(web.WebHandler)
            handler.path = path
            handler.headers = {}
            handler.server = type("Server", (), {"admin_token": "secret", "settings": Settings(web_auth_mode="password")})()
            handler.wfile = BytesIO()
            handler.send_response = lambda status: statuses.append(status)
            handler.send_header = lambda key, value: headers.append((key, value))
            handler.end_headers = lambda: None
            return handler, statuses

        public, public_statuses = make_handler("/public-api/lifecycle/summary")
        with patch("paopao_radar.web.public_lifecycle_summary_payload", return_value={"ok": True, "data": {"summary": {}}}):
            web.WebHandler.do_GET(public)
        self.assertEqual(public_statuses[-1], 200)
        self.assertTrue(__import__("json").loads(public.wfile.getvalue().decode("utf-8"))["ok"])

        private, private_statuses = make_handler("/api/lifecycle/summary")
        web.WebHandler.do_GET(private)
        self.assertEqual(private_statuses[-1], 401)

    def test_payload_filters_and_empty_detail_are_safe(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            empty = public_lifecycle_detail_payload("BTCUSDT", settings=settings)
            listed = lifecycle_list_payload(symbol="BTC", limit=5, settings=settings)

        self.assertTrue(empty["ok"])
        self.assertEqual(empty["symbol"], "BTCUSDT")
        self.assertEqual(listed["filters"]["symbol"], "BTCUSDT")

    def test_list_and_summary_use_compact_projection_while_detail_stays_full(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            large_metrics = metrics()
            large_metrics["large_blob"] = "x" * 20_000
            large_metrics["exchange_context"] = {"items": [], "note": "y" * 20_000}
            engine = LifecycleEngine(settings, metrics_provider=lambda _symbol, _level: large_metrics)
            engine.process_signal(signal(1, text="z" * 20_000))

            listed = lifecycle_list_payload(settings=settings)
            summary = public_lifecycle_summary_payload(settings=settings)
            detail = lifecycle_detail_payload("BTCUSDT", settings=settings)

        list_item = listed["items"][0]
        summary_item = summary["items"][0]
        self.assertEqual(list_item["metrics"], {})
        self.assertEqual(list_item["exchange_context"], {})
        self.assertEqual(list_item["reasons"], [])
        self.assertEqual(list_item["first_signal_excerpt"], "")
        self.assertEqual(summary_item["metrics"], {})
        self.assertIn("large_blob", detail["lifecycle"]["metrics"])
        self.assertGreater(len(json.dumps(detail)), len(json.dumps(listed)) * 5)

    def test_detail_reuses_one_database_connection(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            LifecycleEngine(settings, metrics_provider=lambda _symbol, _level: metrics()).process_signal(signal(1))
            original_connect = LifecycleStore.connect
            connect_calls = 0

            @contextmanager
            def counted_connect(store):
                nonlocal connect_calls
                connect_calls += 1
                with original_connect(store) as conn:
                    yield conn

            with patch.object(LifecycleStore, "connect", new=counted_connect):
                detail = lifecycle_detail_payload("BTCUSDT", settings=settings)

        self.assertTrue(detail["ok"])
        self.assertEqual(connect_calls, 1)


class LifecycleTelegramTests(unittest.TestCase):
    def test_lifecycle_telegram_message_is_redacted(self) -> None:
        lifecycle = {
            "symbol": "BTCUSDT",
            "state_label": "升级到 1H",
            "first_signal_level": "15m",
            "first_signal_at": "2026-07-09T15:05:00+00:00",
            "price_change_from_first_pct": 6.2,
            "oi_change_from_first_pct": 12.5,
            "futures_cvd_status": "主动买入增强",
            "spot_cvd_status": "现货买盘跟随",
            "funding_status": "未明显拥挤",
            "reasons": ["资金跟随较完整"],
        }
        event = {
            "event_label": "升级到 1H",
            "event_time": "2026-07-09T16:05:00+00:00",
            "source_excerpt": "WEB_ADMIN_TOKEN=secret chat_id=123456 topic_id=42",
        }
        message = build_lifecycle_telegram_message(lifecycle, event)

        self.assertIn("生命周期跟随", message)
        self.assertIn("不构成投资建议", message)
        self.assertNotIn("WEB_ADMIN_TOKEN", message)
        self.assertNotIn("chat_id", message)
        self.assertNotIn("topic_id", message)


if __name__ == "__main__":
    unittest.main()
