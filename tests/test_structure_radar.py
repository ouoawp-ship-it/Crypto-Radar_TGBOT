from __future__ import annotations

import argparse
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
