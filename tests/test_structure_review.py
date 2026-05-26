from __future__ import annotations

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
from paopao_radar.coinglass_liquidity import LiquidityContext
from paopao_radar.config import Settings
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

    def test_records_coinglass_liquidity_fields(self) -> None:
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
                source="unit",
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
        self.assertTrue(records[0]["coinglass_available"])

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
