from __future__ import annotations

import io
import json
import sqlite3
import time
import unittest
from contextlib import closing, contextmanager, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from unittest.mock import patch

from paopao_radar import cli
from paopao_radar.config import Settings
from paopao_radar.outcome_tracker import (
    OutcomeStore,
    calculate_outcome_metrics,
    outcome_result_label,
    scan_report_text,
    scan_outcomes,
    scan_signal_outcomes,
)
from paopao_radar.signal_store import append_from_push
from paopao_radar.web_services.outcomes import (
    PUBLIC_OUTCOME_COLUMNS,
    outcome_stats_payload,
    outcomes_payload,
    public_outcomes_payload,
    public_symbol_outcomes_payload,
    symbol_outcomes_payload,
)


def make_settings(tmp: str) -> Settings:
    base = Path(tmp)
    return Settings(
        data_dir=base,
        signal_events_path=base / "signal_events.json",
        signal_events_db_path=base / "signals.db",
        outcome_db_path=base / "outcomes.db",
        outcome_request_sleep_sec=0,
        outcome_scan_limit=100,
        outcome_backfill_days=7,
    )


def add_signal(settings: Settings, *, symbol: str = "BTCUSDT", status: str = "sent", ts: int | None = None) -> None:
    append_from_push(
        settings,
        template_id="TG_FLOW_RADAR",
        dedup_key=f"flow:{symbol}:{status}:{ts or int(time.time())}",
        status=status,
        sent=status == "sent",
        text=f"{symbol}\nScore: 82\n结构确认",
        ts=ts or int(time.time()),
    )


def fake_klines(_symbol: str, _start_ts: int, _end_ts: int, _interval: str, _timeout_sec: int) -> list[dict[str, float]]:
    return [
        {"high": 102.0, "low": 99.0, "close": 100.0},
        {"high": 110.0, "low": 95.0, "close": 104.0},
    ]


class OutcomeTrackerTests(unittest.TestCase):
    def test_schema_indexes_and_unique_constraint(self) -> None:
        with TemporaryDirectory() as tmp:
            db = Path(tmp) / "outcomes.db"
            store = OutcomeStore(db)
            store.ensure_schema()
            with closing(sqlite3.connect(db)) as conn:
                objects = {
                    row[1]: row[0]
                    for row in conn.execute(
                        "SELECT type, name FROM sqlite_master WHERE type IN ('table', 'index')"
                    ).fetchall()
                }
                self.assertEqual(objects["signal_outcomes"], "table")
                self.assertEqual(objects["idx_signal_outcomes_symbol"], "index")
                self.assertEqual(objects["idx_signal_outcomes_horizon"], "index")
                self.assertEqual(objects["idx_signal_outcomes_due_time"], "index")
                self.assertEqual(objects["idx_signal_outcomes_status"], "index")
                self.assertEqual(objects["idx_signal_outcomes_decision"], "index")

                conn.execute(
                    """
                    INSERT INTO signal_outcomes (
                        signal_id, symbol, coin, signal_time, horizon, horizon_sec,
                        due_time, direction, data_status, created_at, updated_at
                    ) VALUES (1, 'BTCUSDT', 'BTC', '2026-01-01T00:00:00+00:00', '1h', 3600,
                        '2026-01-01T01:00:00+00:00', 'long', 'pending', 'now', 'now')
                    """
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(
                        """
                        INSERT INTO signal_outcomes (
                            signal_id, symbol, coin, signal_time, horizon, horizon_sec,
                            due_time, direction, data_status, created_at, updated_at
                        ) VALUES (1, 'BTCUSDT', 'BTC', '2026-01-01T00:00:00+00:00', '1h', 3600,
                            '2026-01-01T01:00:00+00:00', 'long', 'pending', 'now', 'now')
                        """
                    )

    def test_pending_creation_filters_signal_status_symbol_and_duplicates(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            now = int(time.time())
            add_signal(settings, symbol="BTCUSDT", status="sent", ts=now)
            add_signal(settings, symbol="ETHUSDT", status="failed", ts=now)
            append_from_push(
                settings,
                template_id="TG_RADAR_SUMMARY",
                dedup_key="summary:no-symbol",
                status="sent",
                sent=True,
                text="全局摘要，没有币种",
                ts=now,
            )

            first = scan_outcomes(settings=settings, now_ts=now, price_fetcher=fake_klines)
            second = scan_outcomes(settings=settings, now_ts=now, price_fetcher=fake_klines)
            rows = OutcomeStore(settings.outcome_db_path).list_outcomes(limit=20)["items"]

        self.assertEqual(first["counts"]["new_pending"], 4)
        self.assertEqual(second["counts"]["new_pending"], 0)
        self.assertEqual(len(rows), 4)
        self.assertEqual({row["symbol"] for row in rows}, {"BTCUSDT"})
        self.assertTrue(all(row["data_status"] == "pending" for row in rows))

    def test_due_outcome_calculates_success_unavailable_and_error(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            now = int(time.time())
            add_signal(settings, symbol="BTCUSDT", status="sent", ts=now - 7200)
            success = scan_outcomes(settings=settings, horizon="1h", now_ts=now, price_fetcher=fake_klines)
            row = OutcomeStore(settings.outcome_db_path).list_outcomes(horizon="1h", symbol="BTCUSDT")["items"][0]

            add_signal(settings, symbol="ETHUSDT", status="sent", ts=now - 7200)
            unavailable = scan_outcomes(settings=settings, horizon="1h", symbol="ETHUSDT", now_ts=now, price_fetcher=lambda *_args: [])
            eth = OutcomeStore(settings.outcome_db_path).list_outcomes(horizon="1h", symbol="ETHUSDT")["items"][0]

            add_signal(settings, symbol="SOLUSDT", status="sent", ts=now - 7200)

            def broken(*_args):
                raise RuntimeError("network failed")

            error = scan_outcomes(settings=settings, horizon="1h", symbol="SOLUSDT", now_ts=now, price_fetcher=broken)
            sol = OutcomeStore(settings.outcome_db_path).list_outcomes(horizon="1h", symbol="SOLUSDT")["items"][0]

        self.assertEqual(success["counts"]["success"], 1)
        self.assertEqual(row["data_status"], "success")
        self.assertAlmostEqual(row["final_return_pct"], 4.0)
        self.assertAlmostEqual(row["max_gain_pct"], 10.0)
        self.assertAlmostEqual(row["max_drawdown_pct"], -5.0)
        self.assertEqual(row["result_label"], "表现较强")
        self.assertEqual(unavailable["counts"]["unavailable"], 1)
        self.assertEqual(eth["data_status"], "unavailable")
        self.assertEqual(error["counts"]["error"], 1)
        self.assertEqual(sol["data_status"], "error")

    def test_http_400_invalid_symbol_is_unavailable_with_summary(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            now = int(time.time())
            add_signal(settings, symbol="LABUSDT", status="sent", ts=now - 7200)

            def invalid_symbol(*_args):
                raise RuntimeError("HTTP Error 400: Bad Request")

            result = scan_outcomes(settings=settings, horizon="1h", symbol="LABUSDT", now_ts=now, price_fetcher=invalid_symbol)
            row = OutcomeStore(settings.outcome_db_path).list_outcomes(horizon="1h", symbol="LABUSDT")["items"][0]
            stats = OutcomeStore(settings.outcome_db_path).stats(horizon="1h", symbol="LABUSDT")
            report = scan_report_text(result)

        self.assertEqual(result["counts"]["unavailable"], 1)
        self.assertEqual(result["counts"]["error"], 0)
        self.assertEqual(row["data_status"], "unavailable")
        self.assertEqual(row["result_label"], "数据不足")
        self.assertIn("价格源不支持该交易对", row["error"])
        self.assertEqual(stats["unavailable_count"], 1)
        self.assertEqual(stats["error_count"], 0)
        self.assertIn("数据不足 / 价格源不可用摘要", report)
        self.assertIn("LABUSDT 1h", report)

    def test_timeout_remains_retryable_error_instead_of_unavailable(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            now = int(time.time())
            add_signal(settings, symbol="BTCUSDT", status="sent", ts=now - 7200)

            def timed_out(*_args):
                raise TimeoutError("provider timed out")

            result = scan_outcomes(
                settings=settings,
                horizon="1h",
                symbol="BTCUSDT",
                now_ts=now,
                price_fetcher=timed_out,
            )
            row = OutcomeStore(settings.outcome_db_path).list_outcomes(
                horizon="1h",
                symbol="BTCUSDT",
            )["items"][0]

        self.assertEqual(result["counts"]["error"], 1)
        self.assertEqual(result["counts"]["unavailable"], 0)
        self.assertEqual(row["data_status"], "error")
        self.assertIn("timed out", row["error"])

    def test_invalid_symbol_cache_skips_repeated_horizon_fetches(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            now = int(time.time())
            add_signal(settings, symbol="EVAAUSDT", status="sent", ts=now - 20000)
            calls = {"count": 0}

            def invalid_symbol(*_args):
                calls["count"] += 1
                raise RuntimeError("HTTP Error 400: Bad Request")

            result = scan_outcomes(settings=settings, now_ts=now, price_fetcher=invalid_symbol)
            rows = OutcomeStore(settings.outcome_db_path).list_outcomes(symbol="EVAAUSDT", limit=10)["items"]

        self.assertEqual(calls["count"], 1)
        self.assertGreaterEqual(result["counts"]["unavailable"], 2)
        self.assertEqual(result["counts"]["error"], 0)
        self.assertTrue(all(row["data_status"] == "unavailable" for row in rows if row["horizon"] in {"1h", "4h"}))

    def test_scan_reuses_price_windows_and_decision_per_symbol(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            now = int(time.time())
            signal_ts = now - 300000
            add_signal(settings, symbol="BTCUSDT", status="sent", ts=signal_ts)
            calls: list[tuple[str, int, int, str]] = []

            def timestamped_klines(symbol: str, start_ts: int, end_ts: int, interval: str, _timeout: int) -> list[dict[str, float]]:
                calls.append((symbol, start_ts, end_ts, interval))
                offsets = (0, 3600, 14400) if interval == "1m" else (0, 86400, 259200)
                return [
                    {
                        "open_time": float(start_ts + offset),
                        "high": 101.0 + index,
                        "low": 99.0,
                        "close": 100.0 + index,
                    }
                    for index, offset in enumerate(offsets)
                ]

            decision = {
                "decision_code": "wait",
                "decision_label": "等待",
                "decision_confidence": 60,
                "risk_level": "low",
            }
            with patch("paopao_radar.outcome_tracker._decision_snapshot", return_value=decision) as decision_snapshot:
                result = scan_outcomes(settings=settings, limit=10, now_ts=now, price_fetcher=timestamped_klines)

            rows = OutcomeStore(settings.outcome_db_path).list_outcomes(symbol="BTCUSDT", limit=10)["items"]

        self.assertEqual(result["counts"]["success"], 4)
        self.assertEqual(len(calls), 2)
        self.assertEqual({call[3] for call in calls}, {"1m", "5m"})
        self.assertEqual(decision_snapshot.call_count, 1)
        self.assertTrue(all(row["decision_code"] == "wait" for row in rows))

    def test_completed_horizons_are_skipped_without_fetch_or_decision(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            now = int(time.time())
            add_signal(settings, symbol="BTCUSDT", status="sent", ts=now - 7200)
            first = scan_outcomes(settings=settings, horizon="1h", now_ts=now, price_fetcher=fake_klines)

            with patch("paopao_radar.outcome_tracker._decision_snapshot") as decision_snapshot:
                second = scan_outcomes(
                    settings=settings,
                    horizon="1h",
                    now_ts=now,
                    price_fetcher=lambda *_args: self.fail("completed horizon must not refetch prices"),
                )

        self.assertEqual(first["counts"]["success"], 1)
        self.assertEqual(second["counts"]["due"], 0)
        decision_snapshot.assert_not_called()

    def test_explicit_signal_batch_backfills_only_requested_ids(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            now = int(time.time())
            signal = {
                "id": 777,
                "ts": now - 7200,
                "time": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 7200)),
                "symbol": "BTCUSDT",
                "module": "flow",
                "signal_type": "启动",
                "score": 80,
            }
            result = scan_signal_outcomes(
                [signal], settings=settings, horizon="1h", now_ts=now, price_fetcher=fake_klines,
            )
            store = OutcomeStore(settings.outcome_db_path)
            rows = store.list_by_signal_ids([777, 999], horizons=["1h"])

            second = scan_signal_outcomes(
                [signal],
                settings=settings,
                horizon="1h",
                now_ts=now,
                price_fetcher=lambda *_args: self.fail("successful explicit outcome must be skipped"),
            )

        self.assertEqual(result["counts"]["candidate_signals"], 1)
        self.assertEqual(result["counts"]["success"], 1)
        self.assertEqual([(row["signal_id"], row["horizon"]) for row in rows], [(777, "1h")])
        self.assertEqual(second["counts"]["due"], 0)

    def test_batch_update_rolls_back_every_row_on_failure(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            now = int(time.time())
            store = OutcomeStore(settings.outcome_db_path)
            store.create_pending(
                [{"id": 1, "ts": now - 7200, "time": "2026-01-01T00:00:00+00:00", "symbol": "BTCUSDT"}],
                {"1h": 3600, "4h": 14400},
            )
            rows = store.list_outcomes(limit=10)["items"]

            with self.assertRaises(ValueError):
                store.update_outcomes([
                    (int(rows[0]["id"]), {"data_status": "success"}),
                    ("not-an-id", {"data_status": "success"}),  # type: ignore[list-item]
                ])

            after = store.list_outcomes(limit=10)["items"]

        self.assertTrue(all(row["data_status"] == "pending" for row in after))

    def test_1000_prefix_symbol_marks_unavailable_without_fetch(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            now = int(time.time())
            add_signal(settings, symbol="1000BONKUSDT", status="sent", ts=now - 7200)

            def should_not_fetch(*_args):
                raise AssertionError("1000 prefix should not call spot price source")

            result = scan_outcomes(settings=settings, horizon="1h", symbol="1000BONKUSDT", now_ts=now, price_fetcher=should_not_fetch)
            row = OutcomeStore(settings.outcome_db_path).list_outcomes(horizon="1h", symbol="1000BONKUSDT")["items"][0]

        self.assertEqual(result["counts"]["unavailable"], 1)
        self.assertEqual(result["counts"]["error"], 0)
        self.assertEqual(row["data_status"], "unavailable")
        self.assertIn("1000 前缀交易对", row["error"])

    def test_historical_http_400_errors_repair_to_unavailable(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            now = int(time.time())
            store = OutcomeStore(settings.outcome_db_path)
            store.ensure_schema()
            with store.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO signal_outcomes (
                        signal_id, symbol, coin, signal_time, horizon, horizon_sec,
                        due_time, direction, result_label, result_tone, data_status,
                        data_source, error, created_at, updated_at
                    ) VALUES (99, 'LABUSDT', 'LAB', ?, '1h', 3600, ?, 'long',
                        '数据不足', 'muted', 'error', 'binance',
                        'HTTPError: HTTP Error 400: Bad Request', 'now', 'now')
                    """,
                    (time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 7200)), time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 3600))),
                )

            result = scan_outcomes(settings=settings, horizon="1h", symbol="LABUSDT", now_ts=now, price_fetcher=fake_klines)
            row = OutcomeStore(settings.outcome_db_path).list_outcomes(horizon="1h", symbol="LABUSDT")["items"][0]

        self.assertEqual(result["counts"]["repaired_unavailable"], 1)
        self.assertEqual(row["data_status"], "unavailable")
        self.assertEqual(row["error"], "价格源不支持该交易对或暂无 K 线数据")

    def test_metric_and_label_helpers(self) -> None:
        metrics = calculate_outcome_metrics(fake_klines("BTCUSDT", 0, 0, "1m", 10))
        self.assertEqual(metrics["final_return_pct"], 4.0)
        self.assertEqual(metrics["max_gain_pct"], 10.0)
        self.assertEqual(metrics["max_drawdown_pct"], -5.0)
        self.assertEqual(outcome_result_label(final_return_pct=-4, max_gain_pct=1, max_drawdown_pct=-6)["result_label"], "明显回撤")
        self.assertEqual(outcome_result_label(final_return_pct=0.2, max_gain_pct=0.5, max_drawdown_pct=-0.4)["result_label"], "震荡")

    def test_outcome_service_payloads_and_public_redaction(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            now = int(time.time())
            add_signal(settings, symbol="BTCUSDT", status="sent", ts=now - 7200)
            scan_outcomes(settings=settings, horizon="1h", now_ts=now, price_fetcher=fake_klines)

            listing = outcomes_payload(settings=settings, limit=5)
            public_listing = public_outcomes_payload(settings=settings, limit=5)
            stats = outcome_stats_payload(settings=settings, horizon="1h")
            symbol = symbol_outcomes_payload("BTC", settings=settings)
            public_symbol = public_symbol_outcomes_payload("BTC", settings=settings)

        self.assertTrue(listing["ok"])
        self.assertTrue(public_listing["ok"])
        self.assertTrue(stats["ok"])
        self.assertTrue(symbol["ok"])
        self.assertTrue(public_symbol["ok"])
        self.assertEqual(public_listing["items"][0]["symbol"], "BTCUSDT")
        self.assertNotIn("error", public_listing["items"][0])
        serialized = json.dumps(public_listing, ensure_ascii=False)
        for forbidden in ("payload_json", "text_html", "dedup_key", "message_ids", "topic_id", "reply_to_message_id", "WEB_ADMIN_TOKEN", "Cookie"):
            self.assertNotIn(forbidden, serialized)

    def test_outcome_list_reuses_connection_and_projects_public_columns(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            now = int(time.time())
            add_signal(settings, symbol="BTCUSDT", status="sent", ts=now - 7200)
            scan_outcomes(settings=settings, horizon="1h", now_ts=now, price_fetcher=fake_klines)
            original_connect = OutcomeStore.connect
            original_list = OutcomeStore.list_outcomes
            calls = {"connections": 0, "selects": 0}
            captured: dict[str, object] = {}

            @contextmanager
            def counted_connect(store: OutcomeStore):
                calls["connections"] += 1
                with original_connect(store) as connection:
                    connection.set_trace_callback(
                        lambda statement: calls.__setitem__(
                            "selects",
                            calls["selects"] + int(statement.lstrip().upper().startswith(("SELECT", "WITH"))),
                        )
                    )
                    yield connection

            def observed_list(store: OutcomeStore, *args, **kwargs):
                captured["columns"] = kwargs.get("columns")
                captured["connection"] = kwargs.get("connection")
                return original_list(store, *args, **kwargs)

            with patch.object(OutcomeStore, "connect", new=counted_connect), patch.object(OutcomeStore, "list_outcomes", new=observed_list):
                payload = public_outcomes_payload(settings=settings, limit=5)

        self.assertTrue(payload["ok"])
        self.assertEqual(calls["connections"], 1)
        self.assertEqual(calls["selects"], 2)
        self.assertEqual(captured["columns"], PUBLIC_OUTCOME_COLUMNS)
        self.assertIsNotNone(captured["connection"])
        self.assertEqual(set(payload["items"][0]), set(PUBLIC_OUTCOME_COLUMNS))

    def test_cli_outcome_scan_dry_run_uses_tracker(self) -> None:
        with TemporaryDirectory() as tmp:
            args = SimpleNamespace(limit=10, horizon="1h", symbol="BTCUSDT", dry_run=True, backfill_days=7)
            result = {
                "ok": True,
                "counts": {"new_pending": 1, "due": 0, "success": 0, "unavailable": 0, "error": 0, "dry_run": True},
                "errors": [],
            }
            output = io.StringIO()
            with patch("paopao_radar.cli.Settings.load", return_value=make_settings(tmp)), patch("paopao_radar.cli.scan_outcomes", return_value=result) as scan, redirect_stdout(output):
                code = cli.run_outcome_scan(args)

        self.assertEqual(code, 0)
        scan.assert_called_once()
        self.assertIn("信号结果追踪扫描", output.getvalue())
