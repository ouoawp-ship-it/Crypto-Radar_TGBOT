from __future__ import annotations


# Source group: test_structure_radar.py

import argparse
import json
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import paopao_radar.cli as cli
from paopao_radar.config import Settings
from paopao_radar.storage import JsonStore
from paopao_radar.structure_radar import (
    SIGNAL_PRE_BREAKDOWN_NEAR,
    SIGNAL_PRE_BREAKOUT_NEAR,
    StructureRadarEngine,
    calculate_atr_pct,
    calculate_bb_width_pct,
    calculate_box,
    calculate_oi_changes,
    calculate_volume_ratio,
    next_structure_confirm_epoch,
    next_structure_pre_epoch,
    normalize_candles,
    score_level,
)
from paopao_radar.telegram import TelegramGateway
from paopao_radar.telegram import PushResult


def kline(
    idx: int,
    close: float,
    *,
    high: float | None = None,
    low: float | None = None,
    quote_volume: float = 1000,
    taker_ratio: float = 0.55,
) -> list[object]:
    open_time = 1_700_000_000_000 + idx * 900_000
    high = high if high is not None else close + 0.4
    low = low if low is not None else close - 0.4
    volume = quote_volume / close
    return [
        open_time,
        str(close),
        str(high),
        str(low),
        str(close),
        str(volume),
        open_time + 899_999,
        str(quote_volume),
        100,
        str(volume * taker_ratio),
        str(quote_volume * taker_ratio),
        "0",
    ]


def pre_breakout_klines() -> list[list[object]]:
    rows: list[list[object]] = []
    for idx in range(44):
        close = 100 + (idx % 4) * 0.12
        high = 100.8
        low = 99.3
        if idx in {6, 16, 27, 36}:
            high = 105.0
        if idx in {9, 24, 33}:
            low = 98.8
        rows.append(kline(idx, close, high=high, low=low, quote_volume=1000, taker_ratio=0.56))
    rows.append(kline(45, 104.4, high=104.8, low=103.8, quote_volume=2600, taker_ratio=0.66))
    return rows


def pre_breakdown_klines() -> list[list[object]]:
    rows = pre_breakout_klines()
    rows[-1] = kline(45, 99.0, high=99.7, low=98.85, quote_volume=2600, taker_ratio=0.34)
    return rows


class FakeSource:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.budget = type("Budget", (), {"used": {"klines": 0, "open_interest_hist": 0}, "limits": {"klines": 120, "open_interest_hist": 80}})()

    def usdt_perp_symbols(self):
        return [{"symbol": "TESTUSDT", "contractType": "PERPETUAL", "quoteAsset": "USDT", "status": "TRADING"}]

    def ticker_24h(self):
        return [{"symbol": "TESTUSDT", "quoteVolume": "20000000", "lastPrice": "104.4", "priceChangePercent": "2.2"}]

    def premium_index(self):
        return [{"symbol": "TESTUSDT", "lastFundingRate": "0.0001"}]

    def klines(self, symbol, interval="15m", limit=120, start_time=None, end_time=None):
        self.budget.used["klines"] += 1
        return pre_breakout_klines()[-limit:]

    def open_interest_hist(self, symbol, period="15m", limit=36, start_time=None, end_time=None):
        self.budget.used["open_interest_hist"] += 1
        return [
            {"timestamp": idx, "sumOpenInterest": str(1000 + idx * 8)}
            for idx in range(max(2, limit))
        ]

    def diagnostics(self):
        return {
            "budget": {
                "klines": {"used": self.budget.used["klines"], "limit": self.budget.limits["klines"]},
                "open_interest_hist": {"used": self.budget.used["open_interest_hist"], "limit": self.budget.limits["open_interest_hist"]},
            },
            "quality": {"successes": {}, "failures": {}, "warnings": [], "fused": {}},
        }


class StructureRadarTests(unittest.TestCase):
    def test_box_edges_are_identified(self) -> None:
        candles = normalize_candles(pre_breakout_klines()[:-1])
        box = calculate_box(candles[-36:], 104.4, tolerance_pct=1.5)

        self.assertIsNotNone(box)
        self.assertAlmostEqual(box.box_high, 105.0)
        self.assertAlmostEqual(box.box_low, 98.8)
        self.assertGreaterEqual(box.touch_high_count, 3)
        self.assertGreaterEqual(box.touch_low_count, 2)

    def test_pre_breakout_near_signal(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                radar_min_quote_volume=1,
                structure_state_path=Path(tmp) / "structure_state.json",
                structure_history_path=Path(tmp) / "structure_history.json",
                structure_chart_dir=Path(tmp) / "charts",
                structure_min_score=50,
            )
            engine = StructureRadarEngine(settings, JsonStore(Path(tmp)))
            result = engine.build(FakeSource(settings), mode="pre", save_charts=False)

        signals = result["signal_objects"]
        self.assertTrue(signals)
        self.assertEqual(signals[0].signal_type, SIGNAL_PRE_BREAKOUT_NEAR)

    def test_pre_breakdown_near_signal(self) -> None:
        class DownSource(FakeSource):
            def klines(self, symbol, interval="15m", limit=120, start_time=None, end_time=None):
                self.budget.used["klines"] += 1
                return pre_breakdown_klines()[-limit:]

        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                radar_min_quote_volume=1,
                structure_state_path=Path(tmp) / "structure_state.json",
                structure_history_path=Path(tmp) / "structure_history.json",
                structure_chart_dir=Path(tmp) / "charts",
                structure_min_score=50,
            )
            engine = StructureRadarEngine(settings, JsonStore(Path(tmp)))
            result = engine.build(DownSource(settings), mode="pre", save_charts=False)

        self.assertTrue(result["signal_objects"])
        self.assertEqual(result["signal_objects"][0].signal_type, SIGNAL_PRE_BREAKDOWN_NEAR)

    def test_atr_bb_and_volume_metrics(self) -> None:
        candles = normalize_candles(pre_breakout_klines())
        atr_pct, atr_compressed = calculate_atr_pct(candles)
        bb_width_pct, bb_compressed = calculate_bb_width_pct(candles)
        volume_ratio = calculate_volume_ratio(candles)

        self.assertIsNotNone(atr_pct)
        self.assertIsInstance(atr_compressed, bool)
        self.assertIsNotNone(bb_width_pct)
        self.assertIsInstance(bb_compressed, bool)
        self.assertGreater(volume_ratio or 0, 2.0)

    def test_oi_missing_does_not_crash(self) -> None:
        self.assertEqual(calculate_oi_changes([], "15m"), (None, None))

    def test_score_levels(self) -> None:
        self.assertEqual(score_level(86), "S")
        self.assertEqual(score_level(70), "A")
        self.assertEqual(score_level(60), "B")
        self.assertEqual(score_level(50), "C")

    def test_structure_schedule_times(self) -> None:
        tz = timezone(timedelta(hours=8))
        base = datetime(2026, 5, 26, 17, 46, 30, tzinfo=tz).timestamp()
        pre = datetime(2026, 5, 26, 17, 55, 0, tzinfo=tz).timestamp()
        confirm = datetime(2026, 5, 26, 18, 5, 0, tzinfo=tz).timestamp()

        self.assertEqual(next_structure_pre_epoch(base, 55), pre)
        self.assertEqual(next_structure_confirm_epoch(base, 300), confirm)

    def test_structure_radar_cli_dry_run(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                radar_min_quote_volume=1,
                tg_push_history_path=Path(tmp) / "tg_push_history.json",
                runtime_status_path=Path(tmp) / "runtime_status.json",
                structure_state_path=Path(tmp) / "structure_state.json",
                structure_history_path=Path(tmp) / "structure_history.json",
                structure_chart_dir=Path(tmp) / "charts",
                structure_min_score=50,
            )
            store = JsonStore(Path(tmp))
            runtime = (settings, store, None, TelegramGateway(settings, store))

            with patch.object(cli, "make_runtime", side_effect=lambda: runtime):
                with patch.object(cli, "BinanceDataSource", FakeSource):
                    with redirect_stdout(StringIO()) as output:
                        code = cli.main(["structure-radar", "--min-score", "50", "--save-charts"])

        self.assertEqual(code, 0)
        self.assertIn("structure_push: dry_run", output.getvalue())

    def test_structure_signal_replies_to_same_symbol_last_message(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                radar_min_quote_volume=1,
                tg_push_history_path=Path(tmp) / "tg_push_history.json",
                runtime_status_path=Path(tmp) / "runtime_status.json",
                structure_state_path=Path(tmp) / "structure_state.json",
                structure_history_path=Path(tmp) / "structure_history.json",
                structure_review_path=Path(tmp) / "structure_review.json",
                structure_chart_dir=Path(tmp) / "charts",
                structure_min_score=50,
                structure_review_enable=False,
            )
            store = JsonStore(Path(tmp))
            store.save(settings.structure_state_path, {"TESTUSDT": {"last_message_id": 123}})
            runtime = (settings, store, None, TelegramGateway(settings, store))

            with patch.object(cli, "make_runtime", side_effect=lambda: runtime):
                with patch.object(cli, "BinanceDataSource", FakeSource):
                    with redirect_stdout(StringIO()):
                        code = cli.main(["structure-radar", "--min-score", "50"])

            history = store.load(settings.tg_push_history_path, [])

        self.assertEqual(code, 0)
        self.assertEqual(history[0]["reply_to_message_id"], 123)

    def test_structure_signal_does_not_reply_to_other_symbol(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                radar_min_quote_volume=1,
                tg_push_history_path=Path(tmp) / "tg_push_history.json",
                runtime_status_path=Path(tmp) / "runtime_status.json",
                structure_state_path=Path(tmp) / "structure_state.json",
                structure_history_path=Path(tmp) / "structure_history.json",
                structure_review_path=Path(tmp) / "structure_review.json",
                structure_chart_dir=Path(tmp) / "charts",
                structure_min_score=50,
                structure_review_enable=False,
            )
            store = JsonStore(Path(tmp))
            store.save(settings.structure_state_path, {"OTHERUSDT": {"last_message_id": 456}})
            runtime = (settings, store, None, TelegramGateway(settings, store))

            with patch.object(cli, "make_runtime", side_effect=lambda: runtime):
                with patch.object(cli, "BinanceDataSource", FakeSource):
                    with redirect_stdout(StringIO()):
                        code = cli.main(["structure-radar", "--min-score", "50"])

            history = store.load(settings.tg_push_history_path, [])

        self.assertEqual(code, 0)
        self.assertEqual(history[0]["reply_to_message_id"], 0)

    def test_delete_chart_after_success_only_for_real_sent_photo(self) -> None:
        with TemporaryDirectory() as tmp:
            chart = Path(tmp) / "chart.png"
            settings = Settings(data_dir=Path(tmp), structure_delete_chart_after_send=True)

            chart.write_bytes(b"\x89PNG\r\n\x1a\n")
            deleted = cli.delete_chart_after_success(settings, PushResult("sent", "telegram_api", True), str(chart))
            self.assertTrue(deleted["deleted"])
            self.assertFalse(chart.exists())

            chart.write_bytes(b"\x89PNG\r\n\x1a\n")
            dry_run = cli.delete_chart_after_success(settings, PushResult("dry_run", "send_flag_not_set", False), str(chart))
            self.assertEqual(dry_run["reason"], "not_sent")
            self.assertTrue(chart.exists())

            failed = cli.delete_chart_after_success(settings, PushResult("failed", "telegram_api_failed", False), str(chart))
            self.assertEqual(failed["reason"], "not_sent")
            self.assertTrue(chart.exists())


if __name__ == "__main__":
    unittest.main()


# Source group: test_structure_review.py

import argparse
import json
import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import paopao_radar.cli as cli
from paopao_radar.config import Settings
from paopao_radar.liquidity_context import LiquidityContext
from paopao_radar.storage import JsonStore
from paopao_radar.structure_radar import (
    SIGNAL_PRE_BREAKDOWN_NEAR,
    SIGNAL_PRE_BREAKOUT_NEAR,
    StructureSignal,
)
from paopao_radar.structure_review import StructureReviewEngine
from paopao_radar.telegram import TelegramGateway


def make_signal(
    symbol: str = "TESTUSDT",
    signal_type: str = SIGNAL_PRE_BREAKOUT_NEAR,
    level: str = "A",
    price: float = 100.0,
) -> StructureSignal:
    return StructureSignal(
        symbol=symbol,
        interval="15m",
        signal_type=signal_type,
        level=level,
        score=75,
        price=price,
        box_high=105,
        box_low=95,
        box_width_pct=10,
        position_in_box=80,
        distance_to_high_pct=1.0,
        distance_to_low_pct=8.0,
        touch_high_count=3,
        touch_low_count=3,
        atr_pct=1.2,
        atr_compressed=True,
        bb_width_pct=3.5,
        bb_compressed=True,
        volume_ratio=1.6,
        oi_change_pct_1h=5,
        oi_change_pct_4h=9,
        taker_buy_ratio=0.58,
        reason_lines=["test"],
    )


def review_kline(signal_ts: int, idx: int, close: float, high: float | None = None, low: float | None = None) -> list[object]:
    open_time = signal_ts * 1000 + idx * 900_000 + 1
    high = close if high is None else high
    low = close if low is None else low
    return [
        open_time,
        str(close),
        str(high),
        str(low),
        str(close),
        "100",
        open_time + 899_999,
        "10000",
        "0",
        "55",
        "5500",
        "0",
    ]


class ReviewSource:
    def __init__(self, rows: list[list[object]]):
        self.rows = rows

    def klines(self, symbol, interval="15m", limit=120, start_time=None, end_time=None):
        return self.rows[-limit:]


class StructureReviewTests(unittest.TestCase):
    def test_records_structure_signal_into_review_file(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                structure_review_path=Path(tmp) / "structure_review.json",
            )
            store = JsonStore(Path(tmp))
            engine = StructureReviewEngine(settings, store)
            added = engine.record_signals(
                [make_signal()],
                mode="pre",
                window={"end_ms": 1_700_000_000_000},
                push_status="dry_run",
            )

            records = store.load(settings.structure_review_path, [])
            self.assertEqual(added, 1)
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["symbol"], "TESTUSDT")

    def test_records_liquidity_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                structure_review_path=Path(tmp) / "structure_review.json",
            )
            store = JsonStore(Path(tmp))
            signal = make_signal()
            signal.base_score = 70
            signal.liquidity_score_delta = 8
            signal.final_score = 78
            signal.score = 78
            signal.liquidity_context = LiquidityContext(
                symbol=signal.symbol,
                available=True,
                source="BinanceOrderBook",
                liquidation_bias="up",
                orderbook_bias="neutral",
            )

            StructureReviewEngine(settings, store).record_signals(
                [signal],
                mode="pre",
                window={"end_ms": 1_700_000_000_000},
                push_status="dry_run",
            )
            records = store.load(settings.structure_review_path, [])

        self.assertEqual(records[0]["base_score"], 70)
        self.assertEqual(records[0]["liquidity_score_delta"], 8)
        self.assertEqual(records[0]["final_score"], 78)
        self.assertEqual(records[0]["liquidation_bias"], "up")
        self.assertEqual(records[0]["liquidity_source"], "BinanceOrderBook")
        self.assertTrue(records[0]["liquidity_available"])

    def test_review_price_changes_and_valid_breakout(self) -> None:
        with TemporaryDirectory() as tmp:
            now = int(time.time())
            signal_ts = now - 5 * 3600
            settings = Settings(
                data_dir=Path(tmp),
                structure_review_path=Path(tmp) / "structure_review.json",
                structure_stats_path=Path(tmp) / "structure_stats.json",
                structure_review_report_path=Path(tmp) / "structure_review_report.txt",
                structure_review_min_age_minutes=1,
                structure_review_forward_hours=4,
            )
            store = JsonStore(Path(tmp))
            engine = StructureReviewEngine(settings, store)
            engine.record_signals([make_signal()], mode="pre", window={"end_ms": signal_ts * 1000})
            rows = [
                review_kline(signal_ts, 0, 104, high=104, low=99),
                review_kline(signal_ts, 1, 106, high=108, low=103),
                review_kline(signal_ts, 3, 110, high=112, low=106),
                review_kline(signal_ts, 15, 112, high=115, low=109),
            ]
            result = engine.update(ReviewSource(rows), lookback_hours=24)

            record = result["records"][0]
            self.assertEqual(record["outcome"], "valid_breakout")
            self.assertAlmostEqual(record["metrics"]["price_change_15m"], 4.0)
            self.assertAlmostEqual(record["metrics"]["price_change_1h"], 10.0)
            self.assertAlmostEqual(record["metrics"]["price_change_4h"], 12.0)
            self.assertGreater(record["metrics"]["mfe_pct"], 14)

    def test_down_signal_valid_breakdown(self) -> None:
        with TemporaryDirectory() as tmp:
            now = int(time.time())
            signal_ts = now - 5 * 3600
            settings = Settings(
                data_dir=Path(tmp),
                structure_review_path=Path(tmp) / "structure_review.json",
                structure_stats_path=Path(tmp) / "structure_stats.json",
                structure_review_report_path=Path(tmp) / "structure_review_report.txt",
                structure_review_min_age_minutes=1,
            )
            store = JsonStore(Path(tmp))
            engine = StructureReviewEngine(settings, store)
            engine.record_signals(
                [make_signal(signal_type=SIGNAL_PRE_BREAKDOWN_NEAR, price=100)],
                mode="pre",
                window={"end_ms": signal_ts * 1000},
            )
            rows = [
                review_kline(signal_ts, 0, 98, high=100, low=96),
                review_kline(signal_ts, 1, 92, high=97, low=90),
                review_kline(signal_ts, 15, 88, high=91, low=86),
            ]
            result = engine.update(ReviewSource(rows), lookback_hours=24)

            self.assertEqual(result["records"][0]["outcome"], "valid_breakdown")
            self.assertTrue(result["records"][0]["metrics"]["broke_box_low"])

    def test_fake_breakout_detection(self) -> None:
        with TemporaryDirectory() as tmp:
            now = int(time.time())
            signal_ts = now - 5 * 3600
            settings = Settings(
                data_dir=Path(tmp),
                structure_review_path=Path(tmp) / "structure_review.json",
                structure_stats_path=Path(tmp) / "structure_stats.json",
                structure_review_report_path=Path(tmp) / "structure_review_report.txt",
                structure_review_min_age_minutes=1,
            )
            store = JsonStore(Path(tmp))
            engine = StructureReviewEngine(settings, store)
            engine.record_signals([make_signal()], mode="pre", window={"end_ms": signal_ts * 1000})
            rows = [
                review_kline(signal_ts, 0, 106, high=108, low=104),
                review_kline(signal_ts, 1, 101, high=103, low=100),
                review_kline(signal_ts, 15, 99, high=101, low=98),
            ]
            result = engine.update(ReviewSource(rows), lookback_hours=24)

            self.assertEqual(result["records"][0]["outcome"], "fake_breakout")
            self.assertTrue(result["records"][0]["metrics"]["fake_breakout"])

    def test_aggregate_levels_and_sample_suggestion(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                structure_review_min_sample=10,
                structure_review_path=Path(tmp) / "structure_review.json",
                structure_stats_path=Path(tmp) / "structure_stats.json",
            )
            engine = StructureReviewEngine(settings, JsonStore(Path(tmp)))
            records = [
                {"signal_type": SIGNAL_PRE_BREAKOUT_NEAR, "level": "S", "direction": "up", "symbol": "A", "interval": "15m", "status": "completed", "outcome": "valid_breakout", "metrics": {"mfe_pct": 5}},
                {"signal_type": SIGNAL_PRE_BREAKOUT_NEAR, "level": "A", "direction": "up", "symbol": "B", "interval": "15m", "status": "completed", "outcome": "fake_breakout", "metrics": {"mfe_pct": 2}},
                {"signal_type": SIGNAL_PRE_BREAKOUT_NEAR, "level": "B", "direction": "up", "symbol": "C", "interval": "15m", "status": "pending", "outcome": "pending", "metrics": {}},
            ]
            stats = engine.aggregate(records)

            self.assertEqual(stats["summary"]["total"], 3)
            self.assertEqual(stats["by_level"]["S"]["valid_breakouts"], 1)
            self.assertEqual(stats["by_level"]["A"]["fake_breakouts"], 1)
            self.assertEqual(engine.suggestions(stats), ["样本不足，暂不建议调整参数。"])

    def test_structure_review_cli_generates_report_and_send_gate_blocks_without_confirm(self) -> None:
        with TemporaryDirectory() as tmp:
            now = int(time.time())
            signal_ts = now - 5 * 3600
            settings = Settings(
                data_dir=Path(tmp),
                tg_bot_token="123456:abcdefghijklmnopqrstuvwxyzABC",
                tg_chat_id="-1001234567890",
                tg_push_history_path=Path(tmp) / "tg_push_history.json",
                runtime_status_path=Path(tmp) / "runtime_status.json",
                structure_review_path=Path(tmp) / "structure_review.json",
                structure_stats_path=Path(tmp) / "structure_stats.json",
                structure_review_report_path=Path(tmp) / "structure_review_report.txt",
                structure_review_min_age_minutes=1,
            )
            store = JsonStore(Path(tmp))
            store.save(settings.structure_review_path, [{
                "id": "x",
                "symbol": "TESTUSDT",
                "interval": "15m",
                "signal_type": SIGNAL_PRE_BREAKOUT_NEAR,
                "direction": "up",
                "level": "A",
                "score": 75,
                "price": 100,
                "box_high": 105,
                "box_low": 95,
                "signal_ts": signal_ts,
                "status": "pending",
                "outcome": "pending",
                "metrics": {},
            }])
            runtime = (settings, store, None, TelegramGateway(settings, store))
            rows = [review_kline(signal_ts, 0, 106, high=108, low=104), review_kline(signal_ts, 15, 110, high=111, low=106)]

            with patch.object(cli, "make_runtime", side_effect=lambda: runtime):
                with patch.object(cli, "BinanceDataSource", lambda _settings: ReviewSource(rows)):
                    with redirect_stdout(StringIO()) as output:
                        code = cli.main(["structure-review", "--send"])

            self.assertEqual(code, 0)
            self.assertTrue(settings.structure_review_report_path.exists())
            self.assertIn("structure_review_push: blocked", output.getvalue())


if __name__ == "__main__":
    unittest.main()


# Source group: test_symbol_dossier.py

import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.config import Settings
from paopao_radar.storage import JsonStore
from paopao_radar.symbol_dossier import (
    append_signal_events_from_push,
    build_symbol_dossier,
    extract_symbol_from_query,
    extract_symbols_from_text,
    format_symbol_dossier_report,
    is_symbol_dossier_request,
)


class FakeDossierSource:
    def ticker_24h(self):  # type: ignore[no-untyped-def]
        return [{
            "symbol": "TESTUSDT",
            "lastPrice": "116",
            "priceChangePercent": "12.5",
            "quoteVolume": "65000000",
        }]

    def premium_index(self):  # type: ignore[no-untyped-def]
        return [{
            "symbol": "TESTUSDT",
            "lastFundingRate": "-0.008",
            "nextFundingTime": "1783008000000",
        }]

    def klines(self, symbol: str, interval: str = "15m", limit: int = 64, **_kwargs):  # type: ignore[no-untyped-def]
        rows = []
        base = 100.0
        for idx in range(limit):
            close = base + idx * 0.25
            if idx == limit - 1:
                close = 116.0
            high = close * 1.01
            low = close * 0.99
            rows.append([
                idx * 900000,
                str(close * 0.995),
                str(high),
                str(low),
                str(close),
                "1000",
                idx * 900000 + 899999,
                str(1_000_000 + idx * 10_000),
                100,
                "600",
                "600000",
            ])
        return rows

    def open_interest_hist(self, symbol: str, period: str = "15m", limit: int = 17, **_kwargs):  # type: ignore[no-untyped-def]
        return [
            {"sumOpenInterestValue": str(1_000_000 + idx * 40_000)}
            for idx in range(limit)
        ]

    def market_caps(self) -> dict[str, float]:
        return {"TEST": 123_000_000}

    def coinpaprika_market_caps(self) -> dict[str, float]:
        return {}

    def diagnostics(self) -> dict[str, object]:
        return {"quality": {"warnings": []}}


class SymbolDossierTests(unittest.TestCase):
    def test_extracts_symbol_from_signal_and_query(self) -> None:
        text = "🚀 启动雷达 [GWEI](https://www.coinglass.com/tv/zh/Binance_GWEIUSDT)"

        self.assertEqual(extract_symbols_from_text(text), ["GWEIUSDT"])
        self.assertEqual(extract_symbol_from_query("GWEI 怎么看"), "GWEIUSDT")
        self.assertTrue(is_symbol_dossier_request("SOL 可以做多吗"))

    def test_append_signal_events_from_push_writes_symbol_index(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                signal_events_path=Path(tmp) / "signal_events.json",
            )
            store = JsonStore(Path(tmp))

            count = append_signal_events_from_push(
                settings,
                store,
                template_id="TG_LAUNCH_ALERT",
                dedup_key="launch:TEST",
                status="sent",
                sent=True,
                text="🚀 启动雷达 [TEST](https://www.coinglass.com/tv/zh/Binance_TESTUSDT)\n分数: 90",
                ts=int(time.time()),
                message_ids=[321],
            )
            events = store.load(settings.signal_events_path, [])

        self.assertEqual(count, 1)
        self.assertEqual(events[0]["symbol"], "TESTUSDT")
        self.assertEqual(events[0]["signal_type"], "启动雷达")
        self.assertEqual(events[0]["message_ids"], [321])

    def test_build_symbol_dossier_combines_history_snapshot_and_verdict(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                signal_events_path=Path(tmp) / "signal_events.json",
                launch_state_path=Path(tmp) / "launch_state.json",
                launch_watch_history_path=Path(tmp) / "launch_watch_history.json",
                structure_review_path=Path(tmp) / "structure_review.json",
                structure_history_path=Path(tmp) / "structure_history.json",
                funding_alert_state_path=Path(tmp) / "funding_alert_state.json",
            )
            store = JsonStore(Path(tmp))
            store.save(settings.signal_events_path, [{
                "source": "telegram_push",
                "ts": 1000,
                "symbol": "TESTUSDT",
                "signal_type": "启动雷达",
                "template_id": "TG_LAUNCH_ALERT",
                "excerpt": "启动雷达 TEST 分数 90",
            }])
            store.save(settings.structure_review_path, [{
                "symbol": "TESTUSDT",
                "signal_ts": 1100,
                "signal_type": "BREAKOUT_CONFIRMED",
                "level": "A",
                "score": 82,
                "outcome": "valid_breakout",
                "status": "completed",
                "metrics": {"price_change_1h": 4.2},
            }])

            dossier = build_symbol_dossier(settings, "TEST 怎么看", store=store, source=FakeDossierSource())  # type: ignore[arg-type]
            report = format_symbol_dossier_report(dossier)

        self.assertEqual(dossier["symbol"], "TESTUSDT")
        self.assertGreaterEqual(len(dossier["history"]), 2)
        self.assertEqual(dossier["snapshot"]["market_cap_tier"], "低市值")
        self.assertIn(dossier["verdict"]["stance"], {"偏多", "高风险观望", "观望"})
        self.assertIn("TESTUSDT 币种雷达档案", report)
        self.assertIn("历史雷达信号", report)
        self.assertIn("本地规则结论", report)


if __name__ == "__main__":
    unittest.main()
