from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from config import Settings
from radars.consolidation_breakout.chart import (
    PNG_SIGNATURE,
    render_consolidation_chart_png,
)
from radars.consolidation_breakout.radar import (
    Candle,
    ConsolidationBreakoutRadar,
    _chart_payload,
    _detect_three_push_pattern,
    _latest_daily_close_time,
    _step_three_push_track,
    _three_push_quality,
    count_touch_clusters,
)
from shared.storage import JsonStore


DAY_MS = 86_400_000
BASE_MS = 1_700_000_000_000


def kline(
    index: int,
    *,
    high: float,
    low: float,
    close: float,
    volume: float = 100.0,
) -> list[Any]:
    open_time = BASE_MS + index * DAY_MS
    return [
        open_time,
        "100",
        str(high),
        str(low),
        str(close),
        str(volume),
        open_time + DAY_MS - 1,
        "0",
        100,
        "0",
        "0",
        "0",
    ]


def range_klines(count: int = 250) -> list[list[Any]]:
    rows: list[list[Any]] = []
    for index in range(count):
        phase = index % 4
        rows.append(kline(
            index,
            high=103.0 if phase == 0 else 101.0,
            low=97.0 if phase == 2 else 99.0,
            close=99.8 if index % 2 == 0 else 100.2,
        ))
    return rows


def breakout_up(index: int, *, volume: float = 100.0) -> list[Any]:
    return kline(index, high=104.0, low=100.0, close=103.6, volume=volume)


def breakout_down(index: int, *, volume: float = 100.0) -> list[Any]:
    return kline(index, high=100.0, low=96.0, close=96.4, volume=volume)


def utc_daily_range_rows(count: int = 510) -> list[list[Any]]:
    base = BASE_MS // DAY_MS * DAY_MS
    rows = range_klines(count)
    for index, row in enumerate(rows):
        row[0] = base + index * DAY_MS
        row[6] = base + (index + 1) * DAY_MS - 1
    return rows


def utc_daily_rows_with_breakout(base_bars: int = 510) -> list[list[Any]]:
    rows = utc_daily_range_rows(base_bars)
    base = int(rows[0][0])
    row = breakout_up(base_bars, volume=180.0)
    row[0] = base + base_bars * DAY_MS
    row[6] = base + (base_bars + 1) * DAY_MS - 1
    rows.append(row)
    return rows


def three_push_candles(
    *,
    bottom: bool = False,
    hold: int = 12,
    increasing_volume: bool = False,
) -> list[Candle]:
    closes = [100.0] * 60
    closes += [101, 105, 111, 117, 120]
    closes += [116, 112, 108, 105] + [105] * hold
    closes += [106, 108, 110, 112, 114, 116, 118, 121]
    closes += [117, 113, 110, 107] + [107] * hold
    closes += [108, 109, 110, 112, 114, 116, 118, 120, 122]
    closes += [118, 114, 114]
    if bottom:
        closes = [200.0 - value for value in closes]
    candles: list[Candle] = []
    for index, close in enumerate(closes):
        open_time = BASE_MS + index * DAY_MS
        candles.append(Candle(
            open_time=open_time,
            open=close,
            high=close + 0.4,
            low=close - 0.4,
            close=close,
            volume=(
                100.0 + index * 3.0
                if increasing_volume
                else max(100.0, 1_000.0 - index * 3.0)
            ),
            close_time=open_time + DAY_MS - 1,
        ))
    return candles


def rows_from_candles(candles: list[Candle]) -> list[list[Any]]:
    return [
        [
            candle.open_time,
            str(candle.open),
            str(candle.high),
            str(candle.low),
            str(candle.close),
            str(candle.volume),
            candle.close_time,
            "0",
            100,
            "0",
            "0",
            "0",
        ]
        for candle in candles
    ]


def top_pivot_indices(candles: list[Candle]) -> list[int]:
    return [
        index
        for index in range(2, len(candles) - 2)
        if all(
            candles[index].high > candles[neighbor].high
            for neighbor in range(index - 2, index + 3)
            if neighbor != index
        )
    ][-3:]


def bottom_pivot_indices(candles: list[Candle]) -> list[int]:
    return [
        index
        for index in range(2, len(candles) - 2)
        if all(
            candles[index].low < candles[neighbor].low
            for neighbor in range(index - 2, index + 3)
            if neighbor != index
        )
    ][-3:]


def macd_with_pivots(
    candle_count: int,
    peak_indices: list[int],
    peak_values: list[float],
) -> list[float]:
    baseline = -1.0 if peak_values and peak_values[0] < 0 else 1.0
    values = [baseline] * candle_count
    for index, value in zip(peak_indices, peak_values, strict=True):
        values[index] = value
    return values


class KlineSource:
    def __init__(self, rows: list[list[Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, str, int]] = []

    @staticmethod
    def usdt_perp_symbols() -> list[dict[str, str]]:
        return [
            {"symbol": "TESTUSDT"},
            {"symbol": "XAUUSDT"},
        ]

    @staticmethod
    def ticker_24h() -> list[dict[str, str]]:
        return [
            {"symbol": "TESTUSDT", "quoteVolume": "100000000"},
            {"symbol": "XAUUSDT", "quoteVolume": "900000000"},
        ]

    def klines(self, symbol: str, interval: str, limit: int) -> list[list[Any]]:
        self.calls.append((symbol, interval, limit))
        return list(self.rows[-limit:])


class UniverseSource:
    def __init__(
        self,
        symbols: list[str],
        rows_by_symbol: dict[str, list[list[Any]]],
        *,
        quote_volumes: dict[str, float] | None = None,
    ) -> None:
        self.symbols = list(symbols)
        self.rows_by_symbol = rows_by_symbol
        self.quote_volumes = quote_volumes or {}
        self.calls: list[tuple[str, str, int]] = []

    def usdt_perp_symbols(self) -> list[dict[str, str]]:
        return [{"symbol": symbol} for symbol in self.symbols]

    def ticker_24h(self) -> list[dict[str, str]]:
        return [
            {
                "symbol": symbol,
                "quoteVolume": str(self.quote_volumes.get(symbol, 0.0)),
            }
            for symbol in self.symbols
        ]

    def klines(self, symbol: str, interval: str, limit: int) -> list[list[Any]]:
        self.calls.append((symbol, interval, limit))
        return list(self.rows_by_symbol[symbol][-limit:])

    @property
    def called_symbols(self) -> list[str]:
        return [symbol for symbol, _interval, _limit in self.calls]


class IntervalSource(UniverseSource):
    def __init__(
        self,
        symbols: list[str],
        rows_by_interval: dict[tuple[str, str], list[list[Any]]],
    ) -> None:
        super().__init__(symbols, {})
        self.rows_by_interval = rows_by_interval

    def klines(self, symbol: str, interval: str, limit: int) -> list[list[Any]]:
        self.calls.append((symbol, interval, limit))
        return list(self.rows_by_interval[(symbol, interval)][-limit:])


def settings_for(root: Path, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "data_dir": root,
        "consolidation_breakout_enable": True,
        "consolidation_breakout_timeframes": ("1d",),
        "consolidation_breakout_scan_limit": 5,
        "consolidation_breakout_min_quote_volume": 1_000_000,
        "consolidation_breakout_close_delay_sec": 90,
        "consolidation_breakout_strong_volume_ratio": 1.20,
        "consolidation_breakout_require_strong_volume": False,
        "consolidation_breakout_three_push_enable": False,
        "consolidation_breakout_max_signals_per_scan": 8,
        "excluded_base_assets": ("XAU", "XAG"),
    }
    values.update(overrides)
    return Settings(**values)


def closed_now(rows: list[list[Any]], delay_sec: int = 90) -> int:
    return int(rows[-1][6]) + delay_sec * 1000


class ConsolidationBreakoutRadarTests(unittest.TestCase):
    def assert_chart_payload(
        self,
        result: dict[str, Any],
        event: dict[str, Any],
        *,
        cutoff_ms: int,
    ) -> None:
        event_id = str(event["event_id"])
        payload = result["chart_payloads"][event_id]
        chart_candles = payload["candles"]
        close_times = [int(candle["close_time"]) for candle in chart_candles]

        self.assertTrue(chart_candles)
        self.assertLessEqual(len(chart_candles), 264)
        self.assertEqual(len(payload["macd"]), len(chart_candles))
        self.assertEqual(close_times, sorted(set(close_times)))
        self.assertTrue(all(close_time <= cutoff_ms for close_time in close_times))
        self.assertEqual(close_times[-1], int(event["close_time"]))
        for field in ("candles", "macd", "chart_payload", "chart_payloads"):
            self.assertNotIn(field, event)

    def test_latest_daily_close_time_respects_utc_boundary(self) -> None:
        close_time = BASE_MS // DAY_MS * DAY_MS - 1

        self.assertEqual(_latest_daily_close_time(close_time), close_time)
        self.assertEqual(_latest_daily_close_time(close_time + 1), close_time)
        self.assertEqual(
            _latest_daily_close_time(close_time + DAY_MS),
            close_time + DAY_MS,
        )

    def test_daily_product_adds_1d_and_only_expands_its_history(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = utc_daily_rows_with_breakout()
            settings = settings_for(
                root,
                consolidation_breakout_timeframes=("4h",),
                consolidation_breakout_min_quote_volume=0,
                excluded_base_assets=(),
                consolidation_daily_product_enable=True,
                consolidation_daily_shadow_mode=True,
                consolidation_daily_history_bars=620,
            )
            source = UniverseSource(
                ["TESTUSDT"],
                {"TESTUSDT": rows},
            )

            result = ConsolidationBreakoutRadar(
                settings,
                JsonStore(root),
            ).build(source, now_ms=closed_now(rows))  # type: ignore[arg-type]

        calls = {(interval, limit) for _symbol, interval, limit in source.calls}
        self.assertEqual(calls, {("4h", 264), ("1d", 620)})
        self.assertEqual(result["diagnostics"]["timeframes"], ["4h", "1d"])
        self.assertFalse(any(
            event["timeframe"] == "1d"
            and event["detector_profile"] == "legacy_fixed.v1"
            for event in result["events"]
        ))
        self.assertEqual(
            result["diagnostics"]["history_by_timeframe"]["1d"]["max_bars"],
            len(rows),
        )

    def test_daily_box_uses_4h_closed_candle_as_explicit_trigger(self) -> None:
        interval_ms = 4 * 60 * 60 * 1000
        candles: list[Candle] = []
        for index in range(80):
            phase = index % 4
            open_time = BASE_MS + index * interval_ms
            candles.append(Candle(
                open_time=open_time,
                open=100.0,
                high=103.0 if phase == 0 else 101.0,
                low=97.0 if phase == 2 else 99.0,
                close=99.8 if index % 2 == 0 else 100.2,
                volume=100.0,
                close_time=open_time + interval_ms - 1,
            ))
        open_time = BASE_MS + 80 * interval_ms
        candles.append(Candle(
            open_time=open_time,
            open=100.0,
            high=104.0,
            low=100.0,
            close=103.6,
            volume=180.0,
            close_time=open_time + interval_ms - 1,
        ))
        daily_box = {
            "detector_profile": "daily_adaptive.v1",
            "upper": 103.0,
            "lower": 97.0,
            "atr": 2.0,
            "width_atr": 3.0,
            "width_pct": 6.0,
            "efficiency": 0.10,
            "candle_coverage": 0.96,
            "close_coverage": 0.99,
            "upper_touches": 3,
            "lower_touches": 3,
            "base_bars": 500,
            "active_bars": 10,
            "formed_close_time": BASE_MS - DAY_MS,
            "window_start_close_time": BASE_MS - 500 * DAY_MS,
            "quality_label": "strong",
            "quality_label_zh": "强",
            "quality_reasons": [
                {"factor": "touch_clusters", "upper": 3, "lower": 3},
                {"factor": "close_coverage", "value": 0.99},
            ],
        }
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            radar = ConsolidationBreakoutRadar(
                settings_for(root),
                JsonStore(root),
            )
            result = radar._build_daily_boundary_pair(
                symbol="TESTUSDT",
                candles=candles,
                daily_tracks={
                    "TESTUSDT|1d|long": {"box": daily_box},
                },
                strong_ratio=1.20,
                require_strong=False,
                emit_events=True,
            )

        self.assertEqual(len(result["events"]), 1)
        event = result["events"][0]
        self.assertEqual(event["structure_timeframe"], "1d")
        self.assertEqual(event["trigger_timeframe"], "4h")
        self.assertEqual(event["trigger_kind"], "intraday_closed_candle")
        self.assertEqual(event["box_age"], 510)
        self.assertEqual(event["detector_profile"], "daily_adaptive.v1")
        self.assertNotIn("score", event)
        self.assertIn("结构周期 1D ｜ 触发周期 4H", event["text"])
        monitor_updates = [
            update
            for update in result["state_updates"]
            if update["key"].endswith("monitor_4h")
        ]
        self.assertEqual(len(monitor_updates), 2)
        checkpoint = next(
            update
            for update in monitor_updates
            if not update["required_event_ids"]
        )
        self.assertEqual(
            checkpoint["state"]["pending_event"]["event_id"],
            event["event_id"],
        )
        accepted_update = next(
            update
            for update in monitor_updates
            if update["required_event_ids"]
        )
        self.assertEqual(
            accepted_update["required_event_ids"],
            [event["event_id"]],
        )

    def test_unaccepted_4h_trigger_replays_after_daily_box_disappears(self) -> None:
        interval_ms = 4 * 60 * 60 * 1000
        candles: list[Candle] = []
        for index in range(80):
            phase = index % 4
            open_time = BASE_MS + index * interval_ms
            candles.append(Candle(
                open_time=open_time,
                open=100.0,
                high=103.0 if phase == 0 else 101.0,
                low=97.0 if phase == 2 else 99.0,
                close=99.8 if index % 2 == 0 else 100.2,
                volume=100.0,
                close_time=open_time + interval_ms - 1,
            ))
        open_time = BASE_MS + 80 * interval_ms
        candles.append(Candle(
            open_time=open_time,
            open=100.0,
            high=104.0,
            low=100.0,
            close=103.6,
            volume=180.0,
            close_time=open_time + interval_ms - 1,
        ))
        daily_box = {
            "detector_profile": "daily_adaptive.v1",
            "upper": 103.0,
            "lower": 97.0,
            "atr": 2.0,
            "width_atr": 3.0,
            "width_pct": 6.0,
            "efficiency": 0.10,
            "candle_coverage": 0.96,
            "close_coverage": 0.99,
            "upper_touches": 3,
            "lower_touches": 3,
            "base_bars": 500,
            "active_bars": 10,
            "formed_close_time": BASE_MS - DAY_MS,
            "window_start_close_time": BASE_MS - 500 * DAY_MS,
            "quality_label": "strong",
            "quality_label_zh": "强",
            "quality_reasons": [],
        }
        monitor_key = "TESTUSDT|1d|long|monitor_4h"
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for(root)
            store = JsonStore(root)
            radar = ConsolidationBreakoutRadar(settings, store)
            first = radar._build_daily_boundary_pair(
                symbol="TESTUSDT",
                candles=candles,
                daily_tracks={
                    "TESTUSDT|1d|long": {"box": daily_box},
                },
                strong_ratio=1.20,
                require_strong=False,
                emit_events=True,
            )
            event_id = str(first["events"][0]["event_id"])
            rejected = radar.commit(
                {"daily_state_updates": first["state_updates"]},
                [],
            )
            pending_state = store.load(
                settings.consolidation_daily_state_path,
                {},
            )
            pending_tracks = pending_state["tracks"]

            shadow = radar._build_daily_boundary_pair(
                symbol="TESTUSDT",
                candles=candles,
                daily_tracks=pending_tracks,
                strong_ratio=1.20,
                require_strong=False,
                emit_events=False,
            )
            replay = radar._build_daily_boundary_pair(
                symbol="TESTUSDT",
                candles=candles,
                daily_tracks=pending_tracks,
                strong_ratio=1.20,
                require_strong=False,
                emit_events=True,
            )
            before_failed_replay = store.load(
                settings.consolidation_daily_state_path,
                {},
            )
            failed_replay = radar.commit(
                {"daily_state_updates": replay["state_updates"]},
                [],
            )
            after_failed_replay = store.load(
                settings.consolidation_daily_state_path,
                {},
            )
            accepted = radar.commit(
                {"daily_state_updates": replay["state_updates"]},
                [event_id],
            )
            accepted_state = store.load(
                settings.consolidation_daily_state_path,
                {},
            )
            final = radar._build_daily_boundary_pair(
                symbol="TESTUSDT",
                candles=candles,
                daily_tracks=accepted_state["tracks"],
                strong_ratio=1.20,
                require_strong=False,
                emit_events=True,
            )

        self.assertEqual(rejected["daily_deferred"], 1)
        self.assertEqual(
            pending_tracks[monitor_key]["pending_event"]["event_id"],
            event_id,
        )
        self.assertEqual(shadow["events"], [])
        self.assertEqual(shadow["state_updates"], [])
        self.assertEqual([event["event_id"] for event in replay["events"]], [event_id])
        self.assertEqual(failed_replay["daily_deferred"], 1)
        self.assertEqual(after_failed_replay, before_failed_replay)
        self.assertEqual(accepted["daily_deferred"], 0)
        self.assertNotIn("pending_event", accepted_state["tracks"][monitor_key])
        self.assertEqual(final["events"], [])

    def test_4h_monitor_serializes_two_events_across_scans(self) -> None:
        interval_ms = 4 * 60 * 60 * 1000
        candles: list[Candle] = []
        for index, close in enumerate((100.0, 103.6, 100.0)):
            open_time = BASE_MS + index * interval_ms
            candles.append(Candle(
                open_time=open_time,
                open=100.0,
                high=max(101.0, close + 0.4),
                low=min(99.0, close - 0.4),
                close=close,
                volume=100.0,
                close_time=open_time + interval_ms - 1,
            ))
        daily_box = {
            "detector_profile": "daily_adaptive.v1",
            "upper": 103.0,
            "lower": 97.0,
            "atr": 2.0,
            "width_atr": 3.0,
            "width_pct": 6.0,
            "efficiency": 0.10,
            "candle_coverage": 0.96,
            "close_coverage": 0.99,
            "upper_touches": 3,
            "lower_touches": 3,
            "base_bars": 500,
            "active_bars": 10,
            "formed_close_time": BASE_MS - DAY_MS,
            "window_start_close_time": BASE_MS - 500 * DAY_MS,
            "quality_label": "strong",
            "quality_reasons": [],
            "upper_sweep_sent": False,
            "lower_sweep_sent": False,
        }
        monitor_key = "TESTUSDT|1d|long|monitor_4h"
        source_id = (
            f"{daily_box['formed_close_time']}:"
            f"{daily_box['lower']:.12g}:{daily_box['upper']:.12g}"
        )
        initial_tracks = {
            "TESTUSDT|1d|long": {"box": daily_box},
            monitor_key: {
                "box": daily_box,
                "breakout": None,
                "cooldown_until": 0,
                "source_box_id": source_id,
                "last_close_time": candles[0].close_time,
            },
        }
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for(root)
            store = JsonStore(root)
            radar = ConsolidationBreakoutRadar(settings, store)
            first = radar._build_daily_boundary_pair(
                symbol="TESTUSDT",
                candles=candles,
                daily_tracks=initial_tracks,
                strong_ratio=1.20,
                require_strong=False,
                emit_events=True,
            )
            first_id = str(first["events"][0]["event_id"])
            radar.commit(
                {"daily_state_updates": first["state_updates"]},
                [first_id],
            )
            after_first = store.load(
                settings.consolidation_daily_state_path,
                {},
            )["tracks"]
            second = radar._build_daily_boundary_pair(
                symbol="TESTUSDT",
                candles=candles,
                daily_tracks={
                    **after_first,
                    "TESTUSDT|1d|long": {
                        "box": None,
                        "last_close_time": candles[-1].close_time,
                    },
                },
                strong_ratio=1.20,
                require_strong=False,
                emit_events=True,
            )
            second_id = str(second["events"][0]["event_id"])
            radar.commit(
                {"daily_state_updates": second["state_updates"]},
                [],
            )
            after_rejection = store.load(
                settings.consolidation_daily_state_path,
                {},
            )["tracks"]
            replay = radar._build_daily_boundary_pair(
                symbol="TESTUSDT",
                candles=candles,
                daily_tracks=after_rejection,
                strong_ratio=1.20,
                require_strong=False,
                emit_events=True,
            )

        self.assertEqual(
            [event["event"] for event in first["events"]],
            ["breakout_up"],
        )
        self.assertEqual(
            after_first[monitor_key]["last_close_time"],
            candles[1].close_time,
        )
        self.assertNotIn("pending_event", after_first[monitor_key])
        self.assertEqual(
            [event["event"] for event in second["events"]],
            ["fake_breakout"],
        )
        self.assertEqual(second["events"][0]["box_age"], 510)
        self.assertEqual(
            after_rejection[monitor_key]["pending_event"]["event_id"],
            second_id,
        )
        self.assertEqual(
            [event["event_id"] for event in replay["events"]],
            [second_id],
        )

    def test_4h_monitor_freezes_retirement_cutoff_across_scans(self) -> None:
        interval_ms = 4 * 60 * 60 * 1000
        closes = (100.0, 103.6, 104.0, 100.0)
        candles = [
            Candle(
                open_time=BASE_MS + index * interval_ms,
                open=104.0 if index == 2 else 100.0,
                high=max(101.0, close + 0.4),
                low=103.4 if index == 2 else min(99.0, close - 0.4),
                close=close,
                volume=100.0,
                close_time=BASE_MS + (index + 1) * interval_ms - 1,
            )
            for index, close in enumerate(closes)
        ]
        box = {
            "detector_profile": "daily_adaptive.v1",
            "upper": 103.0,
            "lower": 97.0,
            "atr": 2.0,
            "width_atr": 3.0,
            "width_pct": 6.0,
            "efficiency": 0.10,
            "candle_coverage": 0.96,
            "close_coverage": 0.99,
            "upper_touches": 3,
            "lower_touches": 3,
            "base_bars": 500,
            "active_bars": 10,
            "formed_close_time": BASE_MS - DAY_MS,
            "window_start_close_time": BASE_MS - 500 * DAY_MS,
            "quality_label": "strong",
            "quality_reasons": [],
        }
        monitor_key = "TESTUSDT|1d|long|monitor_4h"
        source_id = (
            f"{box['formed_close_time']}:"
            f"{box['lower']:.12g}:{box['upper']:.12g}"
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for(root)
            store = JsonStore(root)
            radar = ConsolidationBreakoutRadar(settings, store)
            first = radar._build_daily_boundary_pair(
                symbol="TESTUSDT",
                candles=candles,
                daily_tracks={
                    "TESTUSDT|1d|long": {
                        "box": None,
                        "last_close_time": candles[2].close_time,
                    },
                    monitor_key: {
                        "box": box,
                        "breakout": None,
                        "cooldown_until": 0,
                        "source_box_id": source_id,
                        "last_close_time": candles[0].close_time,
                    },
                },
                strong_ratio=1.20,
                require_strong=False,
                emit_events=True,
            )
            first_id = str(first["events"][0]["event_id"])
            radar.commit(
                {"daily_state_updates": first["state_updates"]},
                [first_id],
            )
            after_first = store.load(
                settings.consolidation_daily_state_path,
                {},
            )["tracks"]
            second = radar._build_daily_boundary_pair(
                symbol="TESTUSDT",
                candles=candles,
                daily_tracks={
                    **after_first,
                    "TESTUSDT|1d|long": {
                        "box": None,
                        "last_close_time": candles[3].close_time,
                    },
                },
                strong_ratio=1.20,
                require_strong=False,
                emit_events=True,
            )

        self.assertEqual([event["event"] for event in first["events"]], ["breakout_up"])
        self.assertEqual(
            after_first[monitor_key]["retirement"]["cutoff_close_time"],
            candles[2].close_time,
        )
        self.assertEqual(second["events"], [])
        final_update = [
            update
            for update in second["state_updates"]
            if update["key"] == monitor_key
        ][-1]
        self.assertIsNone(final_update["state"]["box"])
        self.assertEqual(
            final_update["state"]["last_close_time"],
            candles[2].close_time,
        )

    def test_4h_monitor_persists_retirement_when_trigger_data_lags(self) -> None:
        interval_ms = 4 * 60 * 60 * 1000
        candle = Candle(
            open_time=BASE_MS,
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=100.0,
            close_time=BASE_MS + interval_ms - 1,
        )
        box_a = {
            "detector_profile": "daily_adaptive.v1",
            "upper": 103.0,
            "lower": 97.0,
            "atr": 2.0,
            "width_atr": 3.0,
            "width_pct": 6.0,
            "efficiency": 0.10,
            "candle_coverage": 0.96,
            "close_coverage": 0.99,
            "upper_touches": 3,
            "lower_touches": 3,
            "base_bars": 500,
            "active_bars": 10,
            "formed_close_time": BASE_MS - DAY_MS,
            "window_start_close_time": BASE_MS - 500 * DAY_MS,
            "quality_label": "strong",
            "quality_reasons": [],
        }
        box_b = {
            **box_a,
            "upper": 110.0,
            "lower": 100.0,
            "formed_close_time": BASE_MS,
        }
        box_c = {
            **box_a,
            "upper": 120.0,
            "lower": 108.0,
            "formed_close_time": BASE_MS + DAY_MS,
        }
        monitor_key = "TESTUSDT|1d|long|monitor_4h"
        source_id_a = (
            f"{box_a['formed_close_time']}:"
            f"{box_a['lower']:.12g}:{box_a['upper']:.12g}"
        )
        cutoff_b = candle.close_time + 2 * interval_ms
        cutoff_c = candle.close_time + 3 * interval_ms
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for(root)
            store = JsonStore(root)
            radar = ConsolidationBreakoutRadar(settings, store)
            first = radar._build_daily_boundary_pair(
                symbol="TESTUSDT",
                candles=[candle],
                daily_tracks={
                    "TESTUSDT|1d|long": {
                        "box": box_b,
                        "last_close_time": cutoff_b,
                    },
                    monitor_key: {
                        "box": box_a,
                        "breakout": None,
                        "cooldown_until": 0,
                        "source_box_id": source_id_a,
                        "last_close_time": candle.close_time,
                    },
                },
                strong_ratio=1.20,
                require_strong=False,
                emit_events=True,
            )
            radar.commit(
                {"daily_state_updates": first["state_updates"]},
                [],
            )
            after_first = store.load(
                settings.consolidation_daily_state_path,
                {},
            )["tracks"]
            second = radar._build_daily_boundary_pair(
                symbol="TESTUSDT",
                candles=[candle],
                daily_tracks={
                    **after_first,
                    "TESTUSDT|1d|long": {
                        "box": box_c,
                        "last_close_time": cutoff_c,
                    },
                },
                strong_ratio=1.20,
                require_strong=False,
                emit_events=True,
            )

        retirement = after_first[monitor_key]["retirement"]
        self.assertEqual(first["events"], [])
        self.assertEqual(retirement["cutoff_close_time"], cutoff_b)
        self.assertEqual(retirement["replacement_box"]["upper"], 110.0)
        self.assertEqual(second["events"], [])
        self.assertEqual(second["state_updates"], [])

    def test_4h_trigger_chart_uses_real_1d_structure_history(self) -> None:
        daily_rows = utc_daily_range_rows(511)
        target_close_time = int(daily_rows[-1][6])
        interval_ms = 4 * 60 * 60 * 1000
        trigger_close_time = target_close_time + interval_ms
        first_open = trigger_close_time - 81 * interval_ms + 1
        trigger_rows: list[list[Any]] = []
        for index in range(81):
            phase = index % 4
            open_time = first_open + index * interval_ms
            breakout = index == 80
            trigger_rows.append([
                open_time,
                "100",
                "104" if breakout else "103" if phase == 0 else "101",
                "100" if breakout else "97" if phase == 2 else "99",
                "103.6" if breakout else "99.8" if index % 2 == 0 else "100.2",
                "180" if breakout else "100",
                open_time + interval_ms - 1,
                "0",
                100,
                "0",
                "0",
                "0",
            ])
        box_start_close_time = target_close_time - 500 * DAY_MS
        daily_box = {
            "detector_profile": "daily_adaptive.v1",
            "upper": 103.0,
            "lower": 97.0,
            "atr": 2.0,
            "width_atr": 3.0,
            "width_pct": 6.0,
            "efficiency": 0.10,
            "candle_coverage": 0.96,
            "close_coverage": 0.99,
            "upper_touches": 3,
            "lower_touches": 3,
            "base_bars": 500,
            "active_bars": 10,
            "formed_close_time": target_close_time - DAY_MS,
            "window_start_close_time": box_start_close_time,
            "quality_label": "strong",
            "quality_reasons": ["上下沿触碰 3/3", "收盘覆盖 99%"],
        }
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for(
                root,
                consolidation_breakout_timeframes=("4h",),
                consolidation_breakout_min_quote_volume=0,
                excluded_base_assets=(),
                consolidation_daily_product_enable=True,
                consolidation_daily_shadow_mode=False,
                consolidation_daily_boundary_events_enable=True,
            )
            store = JsonStore(root)
            daily_tracks = {
                "TESTUSDT|1d|short": {
                    "box": None,
                    "last_close_time": target_close_time,
                },
                "TESTUSDT|1d|medium": {
                    "box": None,
                    "last_close_time": target_close_time,
                },
                "TESTUSDT|1d|long": {
                    "box": daily_box,
                    "last_close_time": target_close_time,
                },
                "TESTUSDT|1d|observation": {
                    "last_close_time": target_close_time,
                    "observation": {
                        "symbol": "TESTUSDT",
                        "target_close_time": target_close_time,
                        "status": "success",
                        "structures": [],
                        "gate_failures": [],
                    },
                },
            }
            store.save(settings.consolidation_daily_state_path, {
                "schema_version": 1,
                "detector_profile": "daily_adaptive.v1",
                "tracks": daily_tracks,
            })
            source = IntervalSource(
                ["TESTUSDT"],
                {
                    ("TESTUSDT", "4h"): trigger_rows,
                    ("TESTUSDT", "1d"): daily_rows,
                },
            )
            result = ConsolidationBreakoutRadar(settings, store).build(
                source,
                now_ms=trigger_close_time + 90_000,
            )  # type: ignore[arg-type]

        event = next(
            event
            for event in result["events"]
            if event["structure_timeframe"] == "1d"
            and event["trigger_timeframe"] == "4h"
        )
        payload = result["chart_payloads"][event["event_id"]]
        self.assertEqual(
            {(interval, limit) for _symbol, interval, limit in source.calls},
            {("4h", 264), ("1d", 620)},
        )
        self.assertGreater(len(payload["candles"]), 264)
        self.assertLessEqual(
            int(payload["candles"][0]["close_time"]),
            box_start_close_time,
        )
        self.assertEqual(payload["structure_timeframe"], "1d")
        self.assertEqual(payload["trigger_timeframe"], "4h")
        self.assertEqual(
            payload["trigger_marker"]["close_time"],
            trigger_close_time,
        )
        png = render_consolidation_chart_png(event=event, chart_payload=payload)
        self.assertTrue(png.startswith(PNG_SIGNATURE))

    def test_live_daily_product_owns_1d_event_and_builds_long_chart(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = utc_daily_rows_with_breakout()
            settings = settings_for(
                root,
                consolidation_breakout_min_quote_volume=0,
                excluded_base_assets=(),
                consolidation_daily_product_enable=True,
                consolidation_daily_shadow_mode=False,
                consolidation_daily_boundary_events_enable=True,
                consolidation_daily_history_bars=620,
            )
            store = JsonStore(root)
            radar = ConsolidationBreakoutRadar(settings, store)
            result = radar.build(
                UniverseSource(["TESTUSDT"], {"TESTUSDT": rows}),
                now_ms=closed_now(rows),
            )  # type: ignore[arg-type]

            self.assertEqual(len(result["events"]), 1)
            event = result["events"][0]
            self.assertEqual(event["detector_profile"], "daily_adaptive.v1")
            self.assertEqual(event["horizon"], "long")
            self.assertEqual(event["box_base_bars"], 500)
            self.assertEqual(event["structure_timeframe"], "1d")
            self.assertEqual(event["trigger_timeframe"], "1d")
            self.assertNotIn("score", event)
            self.assertNotIn("/100", event["text"])
            self.assertGreater(
                len(result["chart_payloads"][event["event_id"]]["candles"]),
                264,
            )
            daily_diag = result["diagnostics"]["daily_product"]
            self.assertEqual(daily_diag["event_count"], 1)
            self.assertGreater(daily_diag["legacy_daily_events_suppressed"], 0)
            self.assertNotIn("TESTUSDT", daily_diag["detector_summary"])
            self.assertLess(len(json.dumps(daily_diag)), 12_000)

            deferred = radar.commit(result, [])
            self.assertEqual(deferred["daily_deferred"], 1)
            retry_source = UniverseSource(
                ["TESTUSDT"],
                {"TESTUSDT": rows},
            )
            retry_result = radar.build(
                retry_source,
                now_ms=closed_now(rows),
            )  # type: ignore[arg-type]
            self.assertEqual(
                {interval for _symbol, interval, _limit in retry_source.calls},
                {"1d", "4h"},
            )
            self.assertIn(
                event["event_id"],
                {retry_event["event_id"] for retry_event in retry_result["events"]},
            )
            committed = radar.commit(result, [event["event_id"]])
            self.assertGreaterEqual(committed["daily_applied"], 3)
            daily_state = store.load(settings.consolidation_daily_state_path, {})
            cached_source = UniverseSource(
                ["TESTUSDT"],
                {"TESTUSDT": rows},
            )
            cached_radar = ConsolidationBreakoutRadar(
                replace(
                    settings,
                    consolidation_daily_boundary_events_enable=False,
                ),
                store,
            )
            cached_result = cached_radar.build(
                cached_source,
                now_ms=closed_now(rows),
            )  # type: ignore[arg-type]

        self.assertEqual(daily_state["detector_profile"], "daily_adaptive.v1")
        self.assertIn("TESTUSDT|1d|long", daily_state["tracks"])
        self.assertEqual(cached_source.calls, [])
        self.assertEqual(
            cached_result["diagnostics"]["daily_product"]["cached_pair_count"],
            1,
        )
        self.assertEqual(
            cached_result["daily_digest_batch"]["observations"][0]["status"],
            "success",
        )

    def test_shadow_daily_product_keeps_legacy_sender_and_emits_digest_batch(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = utc_daily_rows_with_breakout()
            settings = settings_for(
                root,
                consolidation_breakout_min_quote_volume=0,
                excluded_base_assets=(),
                consolidation_daily_product_enable=True,
                consolidation_daily_shadow_mode=True,
                consolidation_daily_boundary_events_enable=True,
                consolidation_daily_history_bars=620,
            )
            result = ConsolidationBreakoutRadar(
                settings,
                JsonStore(root),
            ).build(
                UniverseSource(["TESTUSDT"], {"TESTUSDT": rows}),
                now_ms=closed_now(rows),
            )  # type: ignore[arg-type]

        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["events"][0]["detector_profile"], "legacy_fixed.v1")
        daily_diag = result["diagnostics"]["daily_product"]
        self.assertEqual(daily_diag["shadow_event_count"], 1)
        self.assertEqual(daily_diag["legacy_daily_events_suppressed"], 0)
        batch = result["daily_digest_batch"]
        self.assertEqual(batch["target_close_time"], int(rows[-1][6]))
        self.assertEqual(batch["expected_symbols"], ["TESTUSDT"])
        self.assertEqual(len(batch["observations"]), 1)
        observation = batch["observations"][0]
        self.assertEqual(observation["status"], "success")
        self.assertTrue(observation["structures"])
        self.assertTrue(all(
            structure["structure_quality"] in {"strong", "standard", "watch"}
            for structure in observation["structures"]
        ))

    def test_zero_volume_floor_does_not_depend_on_ticker_snapshot(self) -> None:
        class MissingTickerSource(UniverseSource):
            def ticker_24h(self) -> list[dict[str, str]]:
                raise AssertionError("ticker snapshot must not gate full-market mode")

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = range_klines()
            settings = settings_for(
                root,
                consolidation_breakout_min_quote_volume=0,
                excluded_base_assets=(),
            )
            source = MissingTickerSource(
                ["NEWUSDT"],
                {"NEWUSDT": rows},
            )

            result = ConsolidationBreakoutRadar(
                settings,
                JsonStore(root),
            ).build(source, now_ms=closed_now(rows))  # type: ignore[arg-type]

            self.assertEqual(source.called_symbols, ["NEWUSDT"])
            self.assertEqual(result["diagnostics"]["universe_count"], 1)

    def test_full_sweep_finishes_tail_before_wrapping(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            symbols = [f"S{index:02d}USDT" for index in range(5)]
            settings = settings_for(
                root,
                consolidation_breakout_scan_limit=2,
                consolidation_breakout_min_quote_volume=0,
                excluded_base_assets=(),
            )
            store = JsonStore(root)
            batches: list[list[str]] = []
            diagnostics: list[dict[str, Any]] = []
            for _index in range(4):
                source = UniverseSource(
                    symbols,
                    {symbol: [] for symbol in symbols},
                )
                radar = ConsolidationBreakoutRadar(settings, store)
                result = radar.build(source, now_ms=BASE_MS)  # type: ignore[arg-type]
                commit = radar.commit(result, [])
                batches.append(source.called_symbols)
                diagnostics.append(result["diagnostics"])
                self.assertTrue(commit["rotation_advanced"])

            self.assertEqual(
                batches,
                [
                    ["S00USDT", "S01USDT"],
                    ["S02USDT", "S03USDT"],
                    ["S04USDT"],
                    ["S00USDT", "S01USDT"],
                ],
            )
            self.assertTrue(diagnostics[2]["round_completed"])
            self.assertEqual(diagnostics[2]["remaining_in_round"], 0)
            self.assertEqual(diagnostics[3]["rotation_round"], 2)

    def test_batch_is_clamped_to_remaining_kline_budget(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            symbols = [f"S{index:02d}USDT" for index in range(5)]
            settings = settings_for(
                root,
                consolidation_breakout_scan_limit=100,
                consolidation_breakout_min_quote_volume=0,
                consolidation_breakout_timeframes=("4h", "1d", "1w"),
                excluded_base_assets=(),
                kline_budget=6,
            )
            source = UniverseSource(
                symbols,
                {symbol: [] for symbol in symbols},
            )

            result = ConsolidationBreakoutRadar(
                settings,
                JsonStore(root),
            ).build(source, now_ms=BASE_MS)  # type: ignore[arg-type]

            self.assertEqual(len(source.calls), 6)
            self.assertEqual(sorted(set(source.called_symbols)), symbols[:2])
            self.assertEqual(result["diagnostics"]["candidate_count"], 5)
            self.assertEqual(result["diagnostics"]["attempted_symbol_count"], 2)
            self.assertEqual(result["diagnostics"]["scanned_symbol_count"], 0)
            self.assertEqual(result["diagnostics"]["status"], "degraded")
            self.assertEqual(result["diagnostics"]["configured_batch_size"], 100)
            self.assertEqual(result["diagnostics"]["effective_batch_size"], 2)

    def test_full_market_rotation_is_alphabetical_and_survives_restart(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            symbols = ["DDDUSDT", "BBBUSDT", "AAAUSDT", "CCCUSDT"]
            rows_by_symbol = {symbol: range_klines() for symbol in symbols}
            # Deliberately make alphabetical order the reverse of liquidity order.
            quote_volumes = {
                "AAAUSDT": 1.0,
                "BBBUSDT": 2.0,
                "CCCUSDT": 3.0,
                "DDDUSDT": 4.0,
            }
            settings = settings_for(
                root,
                consolidation_breakout_scan_limit=2,
                consolidation_breakout_min_quote_volume=0,
                excluded_base_assets=(),
            )

            first_source = UniverseSource(
                symbols,
                rows_by_symbol,
                quote_volumes=quote_volumes,
            )
            first_radar = ConsolidationBreakoutRadar(settings, JsonStore(root))
            first = first_radar.build(
                first_source,
                now_ms=closed_now(rows_by_symbol["AAAUSDT"]),
            )  # type: ignore[arg-type]
            first_radar.commit(first, [])

            second_source = UniverseSource(
                list(reversed(symbols)),
                rows_by_symbol,
                quote_volumes={
                    "AAAUSDT": 4.0,
                    "BBBUSDT": 3.0,
                    "CCCUSDT": 2.0,
                    "DDDUSDT": 1.0,
                },
            )
            # A new engine and store model a daemon restart between batches.
            second_radar = ConsolidationBreakoutRadar(settings, JsonStore(root))
            second = second_radar.build(
                second_source,
                now_ms=closed_now(rows_by_symbol["AAAUSDT"]),
            )  # type: ignore[arg-type]
            second_radar.commit(second, [])

            self.assertEqual(first_source.called_symbols, ["AAAUSDT", "BBBUSDT"])
            self.assertEqual(second_source.called_symbols, ["CCCUSDT", "DDDUSDT"])
            self.assertEqual(
                set(first_source.called_symbols + second_source.called_symbols),
                set(symbols),
            )

    def test_default_zero_volume_floor_includes_low_volume_contract(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = range_klines()
            settings = Settings(
                data_dir=root,
                consolidation_breakout_enable=True,
                consolidation_breakout_timeframes=("1d",),
                excluded_base_assets=(),
            )
            source = UniverseSource(
                ["TINYUSDT"],
                {"TINYUSDT": rows},
                quote_volumes={"TINYUSDT": 1.0},
            )

            result = ConsolidationBreakoutRadar(
                settings,
                JsonStore(root),
            ).build(source, now_ms=closed_now(rows))  # type: ignore[arg-type]

            self.assertEqual(settings.consolidation_breakout_min_quote_volume, 0)
            self.assertEqual(source.called_symbols, ["TINYUSDT"])
            self.assertEqual(result["diagnostics"]["candidate_count"], 1)

    def test_unaccepted_event_advances_rotation_but_replays_on_next_sweep(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            event_rows = [*range_klines(), breakout_up(250)]
            quiet_rows = range_klines()
            symbols = ["AAAUSDT", "BBBUSDT"]
            rows_by_symbol = {
                "AAAUSDT": event_rows,
                "BBBUSDT": quiet_rows,
            }
            settings = settings_for(
                root,
                consolidation_breakout_scan_limit=1,
                consolidation_breakout_min_quote_volume=0,
                excluded_base_assets=(),
            )
            now_ms = closed_now(event_rows)

            first_source = UniverseSource(symbols, rows_by_symbol)
            first_radar = ConsolidationBreakoutRadar(settings, JsonStore(root))
            first = first_radar.build(first_source, now_ms=now_ms)  # type: ignore[arg-type]
            first_event_id = first["events"][0]["event_id"]
            first_radar.commit(first, [])

            second_source = UniverseSource(symbols, rows_by_symbol)
            second_radar = ConsolidationBreakoutRadar(settings, JsonStore(root))
            second = second_radar.build(second_source, now_ms=now_ms)  # type: ignore[arg-type]
            second_radar.commit(second, [])

            replay_source = UniverseSource(symbols, rows_by_symbol)
            replay = ConsolidationBreakoutRadar(
                settings,
                JsonStore(root),
            ).build(replay_source, now_ms=now_ms)  # type: ignore[arg-type]

            self.assertEqual(first_source.called_symbols, ["AAAUSDT"])
            self.assertEqual(second_source.called_symbols, ["BBBUSDT"])
            self.assertEqual(replay_source.called_symbols, ["AAAUSDT"])
            self.assertEqual(replay["events"][0]["event_id"], first_event_id)

    def test_empty_universe_does_not_reset_persisted_rotation(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            symbols = ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
            rows_by_symbol = {symbol: range_klines() for symbol in symbols}
            settings = settings_for(
                root,
                consolidation_breakout_scan_limit=1,
                consolidation_breakout_min_quote_volume=0,
                excluded_base_assets=(),
            )
            now_ms = closed_now(rows_by_symbol["AAAUSDT"])

            first_source = UniverseSource(symbols, rows_by_symbol)
            first_radar = ConsolidationBreakoutRadar(settings, JsonStore(root))
            first = first_radar.build(first_source, now_ms=now_ms)  # type: ignore[arg-type]
            first_radar.commit(first, [])

            empty_source = UniverseSource([], {})
            empty_radar = ConsolidationBreakoutRadar(settings, JsonStore(root))
            empty = empty_radar.build(empty_source, now_ms=now_ms)  # type: ignore[arg-type]
            empty_radar.commit(empty, [])

            recovered_source = UniverseSource(symbols, rows_by_symbol)
            recovered = ConsolidationBreakoutRadar(settings, JsonStore(root)).build(
                recovered_source,
                now_ms=now_ms,
            )  # type: ignore[arg-type]

            self.assertEqual(first_source.called_symbols, ["AAAUSDT"])
            self.assertEqual(empty_source.called_symbols, [])
            self.assertEqual(empty["diagnostics"]["candidate_count"], 0)
            self.assertEqual(recovered_source.called_symbols, ["BBBUSDT"])
            self.assertEqual(recovered["diagnostics"]["candidate_count"], 3)

    def test_rotation_safely_absorbs_listing_and_delisting_changes(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            initial_symbols = ["AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT"]
            changed_symbols = ["AA0USDT", "AAAUSDT", "BBBUSDT", "DDDUSDT"]
            rows_by_symbol = {
                symbol: range_klines()
                for symbol in set(initial_symbols + changed_symbols)
            }
            settings = settings_for(
                root,
                consolidation_breakout_scan_limit=2,
                consolidation_breakout_min_quote_volume=0,
                excluded_base_assets=(),
            )
            now_ms = closed_now(rows_by_symbol["AAAUSDT"])

            first_source = UniverseSource(initial_symbols, rows_by_symbol)
            first_radar = ConsolidationBreakoutRadar(settings, JsonStore(root))
            first = first_radar.build(first_source, now_ms=now_ms)  # type: ignore[arg-type]
            first_radar.commit(first, [])

            second_source = UniverseSource(changed_symbols, rows_by_symbol)
            second_radar = ConsolidationBreakoutRadar(settings, JsonStore(root))
            second = second_radar.build(second_source, now_ms=now_ms)  # type: ignore[arg-type]
            second_radar.commit(second, [])

            third_source = UniverseSource(changed_symbols, rows_by_symbol)
            third = ConsolidationBreakoutRadar(settings, JsonStore(root)).build(
                third_source,
                now_ms=now_ms,
            )  # type: ignore[arg-type]

            self.assertEqual(first_source.called_symbols, ["AAAUSDT", "BBBUSDT"])
            self.assertEqual(second_source.called_symbols, ["DDDUSDT"])
            self.assertEqual(third_source.called_symbols, ["AA0USDT", "AAAUSDT"])
            self.assertNotIn(
                "CCCUSDT",
                second_source.called_symbols + third_source.called_symbols,
            )
            self.assertIn("AA0USDT", third_source.called_symbols)

    def test_rotation_metadata_preserves_existing_v1_tracks(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for(
                root,
                consolidation_breakout_scan_limit=1,
                consolidation_breakout_min_quote_volume=0,
                excluded_base_assets=(),
            )
            store = JsonStore(root)
            legacy_track = {
                "last_close_time": 123,
                "box": {"upper": 10.0, "lower": 8.0},
                "breakout": None,
                "cooldown_until": 0,
            }
            legacy_key = "LEGACYUSDT|1d|long"
            store.save(settings.consolidation_breakout_state_path, {
                "schema_version": 1,
                "tracks": {legacy_key: legacy_track},
                "updated_at": 100,
            })
            rows = range_klines()
            source = UniverseSource(["AAAUSDT"], {"AAAUSDT": rows})
            radar = ConsolidationBreakoutRadar(settings, store)

            result = radar.build(source, now_ms=closed_now(rows))  # type: ignore[arg-type]
            radar.commit(result, [])
            saved = store.load(settings.consolidation_breakout_state_path, {})

            self.assertIn(legacy_key, saved["tracks"])
            self.assertEqual(saved["tracks"][legacy_key], legacy_track)

    def test_detects_breakout_from_240_day_range_and_prefers_long_horizon(self) -> None:
        with TemporaryDirectory() as tmp:
            rows = [*range_klines(), breakout_up(250)]
            settings = settings_for(Path(tmp))
            result = ConsolidationBreakoutRadar(
                settings,
                JsonStore(Path(tmp)),
            ).build(KlineSource(rows), now_ms=closed_now(rows))  # type: ignore[arg-type]

            self.assertEqual(result["template_id"], "TG_CONSOLIDATION_BREAKOUT")
            self.assertEqual(len(result["events"]), 1)
            event = result["events"][0]
            self.assertEqual(event["event"], "breakout_up")
            self.assertEqual(event["horizon"], "long")
            self.assertEqual(event["structure_timeframe"], "1d")
            self.assertEqual(event["trigger_timeframe"], "1d")
            self.assertEqual(event["trigger_kind"], "closed_candle")
            self.assertGreaterEqual(event["box_age"], 240)
            self.assertAlmostEqual(event["box_upper"], 103.0)
            self.assertAlmostEqual(event["box_lower"], 97.0)
            self.assertEqual(event["width_pct"], event["box_width_pct"])
            self.assertGreater(event["breakout_distance_pct"], 0)
            self.assertEqual(
                event["breakout_distance_basis"],
                "signed_directional_edge_pct",
            )
            self.assertIn("长期箱体", event["text"])
            self.assertIn("结构周期 1D", event["text"])
            self.assertIn("触发周期 1D", event["text"])
            self.assertIn("结构质量", event["text"])
            self.assertNotIn("评分", event["text"])
            self.assertNotIn("/100", event["text"])
            self.assertIn("未来3根K线", event["text"])
            self.assertGreaterEqual(result["diagnostics"]["suppressed_horizon_events"], 2)
            self.assertEqual(set(result["chart_payloads"]), {event["event_id"]})
            expected_box_start_index = (
                len(rows) - 1 - int(event["box_age"])
            )
            self.assertEqual(
                result["chart_payloads"][event["event_id"]][
                    "box_start_close_time"
                ],
                int(rows[expected_box_start_index][6]),
            )
            self.assert_chart_payload(
                result,
                event,
                cutoff_ms=int(rows[-1][6]),
            )
            chart = render_consolidation_chart_png(
                event=event,
                chart_payload=result["chart_payloads"][event["event_id"]],
            )
            self.assertTrue(chart.startswith(PNG_SIGNATURE))
            self.assertLess(len(chart), 10 * 1024 * 1024)

    def test_fake_breakout_is_detected_with_frozen_endpoints(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for(root)
            store = JsonStore(root)
            radar = ConsolidationBreakoutRadar(settings, store)
            rows = [*range_klines(), breakout_up(250)]
            first = radar.build(KlineSource(rows), now_ms=closed_now(rows))  # type: ignore[arg-type]
            radar.commit(first, [first["events"][0]["event_id"]])

            rows.append(kline(251, high=104.0, low=102.0, close=102.5))
            second = radar.build(KlineSource(rows), now_ms=closed_now(rows))  # type: ignore[arg-type]

            self.assertEqual(len(second["events"]), 1)
            event = second["events"][0]
            self.assertEqual(event["event"], "fake_breakout")
            self.assertEqual(event["horizon"], "long")
            self.assertAlmostEqual(event["box_upper"], 103.0)
            self.assertAlmostEqual(event["box_lower"], 97.0)
            self.assertLess(event["breakout_distance_pct"], 0)
            self.assertIn("原向上突破失效", event["text"])

    def test_fake_breakdown_uses_the_same_three_bar_reentry_logic(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for(root)
            store = JsonStore(root)
            radar = ConsolidationBreakoutRadar(settings, store)
            rows = [*range_klines(), breakout_down(250)]
            first = radar.build(KlineSource(rows), now_ms=closed_now(rows))  # type: ignore[arg-type]
            self.assertEqual(first["events"][0]["event"], "breakout_down")
            radar.commit(first, [first["events"][0]["event_id"]])

            rows.append(kline(251, high=98.5, low=96.0, close=97.6))
            second = radar.build(KlineSource(rows), now_ms=closed_now(rows))  # type: ignore[arg-type]

            self.assertEqual(len(second["events"]), 1)
            event = second["events"][0]
            self.assertEqual(event["event"], "fake_breakdown")
            self.assertEqual(event["horizon"], "long")
            self.assertIn("原向下跌破失效", event["text"])

    def test_direct_opposite_break_wins_over_fakeout_inside_three_bars(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for(root)
            radar = ConsolidationBreakoutRadar(settings, JsonStore(root))
            rows = [*range_klines(), breakout_up(250)]
            first = radar.build(KlineSource(rows), now_ms=closed_now(rows))  # type: ignore[arg-type]
            radar.commit(first, [first["events"][0]["event_id"]])

            rows.append(kline(251, high=103.0, low=95.5, close=96.0))
            reversal = radar.build(KlineSource(rows), now_ms=closed_now(rows))  # type: ignore[arg-type]

            self.assertEqual(len(reversal["events"]), 1)
            self.assertEqual(reversal["events"][0]["event"], "breakout_down")
            self.assertNotEqual(reversal["events"][0]["event"], "fake_breakout")

    def test_retest_only_pushes_once_and_keeps_fakeout_observation_alive(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for(root)
            radar = ConsolidationBreakoutRadar(settings, JsonStore(root))
            rows = [*range_klines(), breakout_up(250)]
            first = radar.build(KlineSource(rows), now_ms=closed_now(rows))  # type: ignore[arg-type]
            radar.commit(first, [first["events"][0]["event_id"]])

            rows.append(kline(251, high=104.0, low=103.0, close=103.5))
            retest = radar.build(KlineSource(rows), now_ms=closed_now(rows))  # type: ignore[arg-type]
            self.assertEqual(retest["events"][0]["event"], "retest_up")
            self.assertEqual(retest["events"][0]["bars_since_breakout"], 1)
            radar.commit(retest, [retest["events"][0]["event_id"]])

            rows.append(kline(252, high=104.0, low=103.0, close=103.5))
            repeated_retest = radar.build(KlineSource(rows), now_ms=closed_now(rows))  # type: ignore[arg-type]
            self.assertEqual(repeated_retest["events"], [])
            radar.commit(repeated_retest, [])

            rows.append(kline(253, high=103.0, low=102.0, close=102.5))
            fakeout = radar.build(KlineSource(rows), now_ms=closed_now(rows))  # type: ignore[arg-type]
            self.assertEqual(fakeout["events"][0]["event"], "fake_breakout")
            self.assertEqual(fakeout["events"][0]["bars_since_breakout"], 3)

    def test_sweep_is_one_shot_and_double_sweep_enters_cooldown_silently(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for(root)
            store = JsonStore(root)
            radar = ConsolidationBreakoutRadar(settings, store)
            rows = range_klines()
            formed = radar.build(KlineSource(rows), now_ms=closed_now(rows))  # type: ignore[arg-type]
            self.assertEqual(formed["events"], [])
            radar.commit(formed, [])

            rows.append(kline(250, high=104.0, low=99.0, close=102.8))
            first_sweep = radar.build(KlineSource(rows), now_ms=closed_now(rows))  # type: ignore[arg-type]
            self.assertEqual(first_sweep["events"][0]["event"], "upper_sweep")
            radar.commit(first_sweep, [first_sweep["events"][0]["event_id"]])

            rows.append(kline(251, high=104.0, low=99.0, close=102.8))
            duplicate_sweep = radar.build(KlineSource(rows), now_ms=closed_now(rows))  # type: ignore[arg-type]
            self.assertEqual(duplicate_sweep["events"], [])
            radar.commit(duplicate_sweep, [])

            rows.append(kline(252, high=104.0, low=96.0, close=100.0))
            double_sweep = radar.build(KlineSource(rows), now_ms=closed_now(rows))  # type: ignore[arg-type]
            self.assertEqual(double_sweep["events"], [])
            radar.commit(double_sweep, [])

            state = store.load(settings.consolidation_breakout_state_path, {})
            long_track = state["tracks"]["TESTUSDT|1d|long"]
            self.assertIsNone(long_track["box"])
            self.assertGreater(long_track["cooldown_until"], int(rows[-1][6]))

    def test_touch_count_debounces_adjacent_bars(self) -> None:
        candles = [
            Candle(1, 100, 103, 99, 100, 1, 2),
            Candle(2, 100, 103, 99, 100, 1, 3),
            Candle(3, 100, 101, 97, 100, 1, 4),
            Candle(4, 100, 103, 97, 100, 1, 5),
            Candle(5, 100, 101, 99, 100, 1, 6),
            Candle(6, 100, 101, 97, 100, 1, 7),
        ]

        upper, lower = count_touch_clusters(
            candles,
            upper=103,
            lower=97,
            tolerance=0.1,
        )

        self.assertEqual(upper, 2)
        self.assertEqual(lower, 2)

    def test_three_push_top_uses_three_confirmed_price_and_macd_peaks(self) -> None:
        candles = three_push_candles()

        self.assertIsNone(
            _detect_three_push_pattern(candles, len(candles) - 2)
        )
        pattern = _detect_three_push_pattern(candles, len(candles) - 1)

        self.assertIsNotNone(pattern)
        assert pattern is not None
        self.assertEqual(pattern["structure"], "top")
        self.assertEqual(pattern["push_prices"], [120.4, 121.4, 122.4])
        self.assertGreater(pattern["push_macd"][0], pattern["push_macd"][1])
        self.assertGreater(pattern["push_macd"][1], pattern["push_macd"][2])
        self.assertGreater(pattern["neckline"], 0)
        self.assertGreater(pattern["invalidation"], pattern["push_prices"][-1])

    def test_three_push_bottom_is_the_symmetric_closed_bar_pattern(self) -> None:
        candles = three_push_candles(bottom=True, increasing_volume=True)

        pattern = _detect_three_push_pattern(candles, len(candles) - 1)

        self.assertIsNotNone(pattern)
        assert pattern is not None
        self.assertEqual(pattern["structure"], "bottom")
        self.assertEqual(pattern["push_prices"], [79.6, 78.6, 77.6])
        self.assertLess(pattern["push_macd"][0], pattern["push_macd"][1])
        self.assertLess(pattern["push_macd"][1], pattern["push_macd"][2])
        self.assertLess(pattern["invalidation"], pattern["push_prices"][-1])
        self.assertFalse(pattern["volume_progressive_weakening"])

    def test_three_push_rejects_price_only_pattern_without_progressive_macd(self) -> None:
        candles = three_push_candles(hold=4)
        pivots = top_pivot_indices(candles)
        non_progressive_macd = macd_with_pivots(
            len(candles),
            pivots,
            [100.0, 100.0, 90.0],
        )

        with patch(
            "radars.consolidation_breakout.radar._macd_line",
            return_value=non_progressive_macd,
        ):
            pattern = _detect_three_push_pattern(candles, len(candles) - 1)

        self.assertIsNone(pattern)

    def test_three_push_rejects_descending_macd_samples_without_three_macd_peaks(self) -> None:
        candles = three_push_candles()
        sampled_macd = [20.0 - index * 0.05 for index in range(len(candles))]

        with patch(
            "radars.consolidation_breakout.radar._macd_line",
            return_value=sampled_macd,
        ):
            pattern = _detect_three_push_pattern(candles, len(candles) - 1)

        self.assertIsNone(pattern)

    def test_three_push_price_steps_require_at_least_one_tenth_atr_each(self) -> None:
        candles = three_push_candles()
        pivots = top_pivot_indices(candles)
        macd = macd_with_pivots(len(candles), pivots, [100.0, 90.0, 80.0])

        exact = list(candles)
        below = list(candles)
        for index, exact_high, below_high in zip(
            pivots,
            [130.0, 131.0, 132.0],
            [130.0, 130.99, 131.98],
            strict=True,
        ):
            exact[index] = replace(exact[index], high=exact_high)
            below[index] = replace(below[index], high=below_high)

        with (
            patch(
                "radars.consolidation_breakout.radar._macd_line",
                return_value=macd,
            ),
            patch(
                "radars.consolidation_breakout.radar._atr",
                return_value=10.0,
            ),
        ):
            exact_pattern = _detect_three_push_pattern(exact, len(exact) - 1)
            below_pattern = _detect_three_push_pattern(below, len(below) - 1)

        self.assertIsNotNone(exact_pattern)
        self.assertIsNone(below_pattern)

        bottom_candles = three_push_candles(bottom=True)
        bottom_pivots = bottom_pivot_indices(bottom_candles)
        bottom_macd = macd_with_pivots(
            len(bottom_candles),
            bottom_pivots,
            [-100.0, -90.0, -80.0],
        )
        exact_bottom = list(bottom_candles)
        below_bottom = list(bottom_candles)
        for index, exact_low, below_low in zip(
            bottom_pivots,
            [70.0, 69.0, 68.0],
            [70.0, 69.01, 68.02],
            strict=True,
        ):
            exact_bottom[index] = replace(exact_bottom[index], low=exact_low)
            below_bottom[index] = replace(below_bottom[index], low=below_low)

        with (
            patch(
                "radars.consolidation_breakout.radar._macd_line",
                return_value=bottom_macd,
            ),
            patch(
                "radars.consolidation_breakout.radar._atr",
                return_value=10.0,
            ),
        ):
            exact_bottom_pattern = _detect_three_push_pattern(
                exact_bottom,
                len(exact_bottom) - 1,
            )
            below_bottom_pattern = _detect_three_push_pattern(
                below_bottom,
                len(below_bottom) - 1,
            )

        self.assertIsNotNone(exact_bottom_pattern)
        self.assertIsNone(below_bottom_pattern)

    def test_three_push_macd_steps_require_five_percent_of_first_peak_each(self) -> None:
        cases = (
            ("exact boundary", [100.0, 95.0, 90.0], True),
            ("first step below", [100.0, 95.01, 89.0], False),
            ("second step below", [100.0, 94.0, 89.01], False),
        )

        for bottom in (False, True):
            candles = three_push_candles(bottom=bottom)
            pivots = (
                bottom_pivot_indices(candles)
                if bottom
                else top_pivot_indices(candles)
            )
            for label, unsigned_values, expected in cases:
                peak_values = (
                    [-value for value in unsigned_values]
                    if bottom
                    else unsigned_values
                )
                with self.subTest(
                    structure="bottom" if bottom else "top",
                    label=label,
                ):
                    macd = macd_with_pivots(
                        len(candles),
                        pivots,
                        peak_values,
                    )
                    with patch(
                        "radars.consolidation_breakout.radar._macd_line",
                        return_value=macd,
                    ):
                        pattern = _detect_three_push_pattern(
                            candles,
                            len(candles) - 1,
                        )

                    self.assertEqual(pattern is not None, expected)

    def test_three_push_volume_weakening_affects_quality_but_is_not_pattern_gate(self) -> None:
        weakening = _detect_three_push_pattern(
            three_push_candles(),
            len(three_push_candles()) - 1,
        )
        rising = _detect_three_push_pattern(
            three_push_candles(increasing_volume=True),
            len(three_push_candles(increasing_volume=True)) - 1,
        )

        self.assertIsNotNone(weakening)
        self.assertIsNotNone(rising)
        assert weakening is not None and rising is not None
        weakening["event"] = "three_push_top_forming"
        rising["event"] = "three_push_top_forming"
        self.assertEqual(
            _three_push_quality(weakening)["structure_quality"],
            "normal",
        )
        self.assertEqual(
            _three_push_quality(rising)["structure_quality"],
            "weak",
        )

    def test_three_push_forms_then_confirms_once_after_neckline_close(self) -> None:
        candles = three_push_candles()
        track, forming = _step_three_push_track(
            {},
            candles,
            len(candles) - 1,
        )

        self.assertIsNotNone(forming)
        assert forming is not None
        self.assertEqual(forming["event"], "three_push_top_forming")
        neckline = float(forming["neckline"])
        atr = float(forming["atr"])
        index = len(candles)
        open_time = BASE_MS + index * DAY_MS
        candles.append(Candle(
            open_time=open_time,
            open=neckline,
            high=neckline + 0.4,
            low=neckline - atr,
            close=neckline,
            volume=100.0,
            close_time=open_time + DAY_MS - 1,
        ))

        wick_track, wick_only = _step_three_push_track(
            track,
            candles,
            len(candles) - 1,
        )
        self.assertIsNone(wick_only)
        self.assertIsNotNone(wick_track["setup"])

        index += 1
        open_time = BASE_MS + index * DAY_MS
        candles.append(Candle(
            open_time=open_time,
            open=neckline,
            high=neckline + 0.4,
            low=neckline - atr - 0.4,
            close=neckline - atr,
            volume=100.0,
            close_time=open_time + DAY_MS - 1,
        ))
        confirmed_track, confirmed = _step_three_push_track(
            wick_track,
            candles,
            len(candles) - 1,
        )

        self.assertIsNotNone(confirmed)
        assert confirmed is not None
        self.assertEqual(confirmed["event"], "three_push_top_confirmed")
        self.assertIsNone(confirmed_track["setup"])

        index += 1
        open_time = BASE_MS + index * DAY_MS
        candles.append(Candle(
            open_time=open_time,
            open=neckline - 1.0,
            high=neckline,
            low=neckline - 2.0,
            close=neckline - 1.5,
            volume=100.0,
            close_time=open_time + DAY_MS - 1,
        ))
        _finished, repeated = _step_three_push_track(
            confirmed_track,
            candles,
            len(candles) - 1,
        )
        self.assertIsNone(repeated)

    def test_three_push_bottom_confirms_on_buffered_neckline_close(self) -> None:
        candles = three_push_candles(bottom=True, increasing_volume=True)
        track, forming = _step_three_push_track(
            {},
            candles,
            len(candles) - 1,
        )
        self.assertIsNotNone(forming)
        assert forming is not None
        self.assertEqual(forming["event"], "three_push_bottom_forming")
        neckline = float(forming["neckline"])
        atr = float(forming["atr"])
        index = len(candles)
        open_time = BASE_MS + index * DAY_MS
        close = neckline + atr
        candles.append(Candle(
            open_time=open_time,
            open=candles[-1].close,
            high=close + 0.4,
            low=min(candles[-1].close, close) - 0.4,
            close=close,
            volume=2_000.0,
            close_time=open_time + DAY_MS - 1,
        ))

        _confirmed_track, confirmed = _step_three_push_track(
            track,
            candles,
            len(candles) - 1,
        )

        self.assertIsNotNone(confirmed)
        assert confirmed is not None
        self.assertEqual(confirmed["event"], "three_push_bottom_confirmed")

    def test_pending_three_push_top_is_discarded_on_any_higher_high(self) -> None:
        candles = three_push_candles()
        track, forming = _step_three_push_track(
            {},
            candles,
            len(candles) - 1,
        )
        self.assertIsNotNone(forming)
        assert forming is not None

        index = len(candles)
        open_time = BASE_MS + index * DAY_MS
        neckline = float(forming["neckline"])
        atr = float(forming["atr"])
        candles.append(Candle(
            open_time=open_time,
            open=candles[-1].close,
            high=float(forming["push_prices"][-1]) + 0.01,
            low=neckline - atr - 0.4,
            close=neckline - atr,
            volume=100.0,
            close_time=open_time + DAY_MS - 1,
        ))

        discarded, event = _step_three_push_track(
            track,
            candles,
            len(candles) - 1,
        )

        self.assertIsNone(event)
        self.assertIsNone(discarded["setup"])

    def test_pending_three_push_bottom_is_discarded_on_any_lower_low(self) -> None:
        candles = three_push_candles(bottom=True, increasing_volume=True)
        track, forming = _step_three_push_track(
            {},
            candles,
            len(candles) - 1,
        )
        self.assertIsNotNone(forming)
        assert forming is not None

        index = len(candles)
        open_time = BASE_MS + index * DAY_MS
        neckline = float(forming["neckline"])
        atr = float(forming["atr"])
        candles.append(Candle(
            open_time=open_time,
            open=candles[-1].close,
            high=neckline + atr + 0.4,
            low=float(forming["push_prices"][-1]) - 0.01,
            close=neckline + atr,
            volume=100.0,
            close_time=open_time + DAY_MS - 1,
        ))

        discarded, event = _step_three_push_track(
            track,
            candles,
            len(candles) - 1,
        )

        self.assertIsNone(event)
        self.assertIsNone(discarded["setup"])

    def test_legacy_three_push_setup_without_rule_version_never_confirms(self) -> None:
        candles = three_push_candles()
        track, forming = _step_three_push_track(
            {},
            candles,
            len(candles) - 1,
        )
        self.assertIsNotNone(forming)
        assert forming is not None
        legacy = dict(track)
        legacy["setup"] = dict(track["setup"])
        legacy["setup"].pop("rule_version", None)

        index = len(candles)
        open_time = BASE_MS + index * DAY_MS
        neckline = float(forming["neckline"])
        atr = float(forming["atr"])
        candles.append(Candle(
            open_time=open_time,
            open=candles[-1].close,
            high=candles[-1].high,
            low=neckline - atr - 0.4,
            close=neckline - atr,
            volume=100.0,
            close_time=open_time + DAY_MS - 1,
        ))

        migrated, event = _step_three_push_track(
            legacy,
            candles,
            len(candles) - 1,
        )

        self.assertIsNone(event)
        self.assertIsNone(migrated["setup"])
        self.assertEqual(migrated["rule_version"], 2)

    def test_three_push_build_waits_for_closed_confirmation_bar(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            candles = three_push_candles()
            rows = rows_from_candles(candles)
            settings = settings_for(
                root,
                consolidation_breakout_three_push_enable=True,
            )
            radar = ConsolidationBreakoutRadar(settings, JsonStore(root))
            before_close = (
                int(rows[-1][6])
                + settings.consolidation_breakout_close_delay_sec * 1000
                - 1
            )

            open_result = radar.build(
                KlineSource(rows),
                now_ms=before_close,
            )  # type: ignore[arg-type]
            radar.commit(open_result, [])
            closed_result = radar.build(
                KlineSource(rows),
                now_ms=closed_now(rows),
            )  # type: ignore[arg-type]

            self.assertEqual(open_result["events"], [])
            three_push_events = [
                event
                for event in closed_result["events"]
                if event["event"].startswith("three_push_")
            ]
            self.assertEqual(len(three_push_events), 1)
            self.assertEqual(
                three_push_events[0]["event"],
                "three_push_top_forming",
            )
            self.assertIn("右侧2根闭合K线", three_push_events[0]["text"])
            self.assert_chart_payload(
                closed_result,
                three_push_events[0],
                cutoff_ms=int(rows[-1][6]),
            )
            chart = render_consolidation_chart_png(
                event=three_push_events[0],
                chart_payload=closed_result["chart_payloads"][
                    three_push_events[0]["event_id"]
                ],
            )
            self.assertTrue(chart.startswith(PNG_SIGNATURE))
            self.assertLess(len(chart), 10 * 1024 * 1024)

    def test_three_push_text_uses_explainable_quality_without_score(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            candles = three_push_candles()
            rows = rows_from_candles(candles)
            settings = settings_for(
                root,
                consolidation_breakout_three_push_enable=True,
            )

            result = ConsolidationBreakoutRadar(
                settings,
                JsonStore(root),
            ).build(KlineSource(rows), now_ms=closed_now(rows))  # type: ignore[arg-type]
            event = next(
                event
                for event in result["events"]
                if event["event"] == "three_push_top_forming"
            )
            text = event["text"]

            self.assertNotIn("评分", text)
            self.assertNotIn("/100", text)
            expected_reasons = {
                "结构质量": "一般",
                "价格推进": "通过",
                "MACD三峰": "通过",
                "量能确认": "通过",
                "箱体位置": "未通过",
                "颈线状态": "形成中",
            }
            for label, reason in expected_reasons.items():
                with self.subTest(label=label):
                    line = next(
                        line for line in text.splitlines() if label in line
                    )
                    self.assertIn(reason, line)
                    if label == "箱体位置":
                        self.assertIn("共振", line)

    def test_weak_three_push_is_suppressed_and_checkpointed_once(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            candles = three_push_candles(increasing_volume=True)
            rows = rows_from_candles(candles)
            settings = settings_for(
                root,
                consolidation_breakout_three_push_enable=True,
            )
            store = JsonStore(root)
            radar = ConsolidationBreakoutRadar(settings, store)

            first = radar.build(
                KlineSource(rows),
                now_ms=closed_now(rows),
            )  # type: ignore[arg-type]
            self.assertFalse(any(
                event["event"].startswith("three_push_")
                for event in first["events"]
            ))
            self.assertEqual(
                first["diagnostics"]["three_push_weak_suppressed_count"],
                1,
            )

            radar.commit(first, [])
            key = "TESTUSDT|1d|three_push"
            saved = store.load(settings.consolidation_breakout_state_path, {})
            track = saved["tracks"][key]
            self.assertEqual(track["last_close_time"], candles[-1].close_time)
            self.assertEqual(track["setup"]["structure_quality"], "weak")
            self.assertTrue(track["last_pattern_id"])

            repeated = radar.build(
                KlineSource(rows),
                now_ms=closed_now(rows),
            )  # type: ignore[arg-type]
            self.assertFalse(any(
                event["event"].startswith("three_push_")
                for event in repeated["events"]
            ))
            self.assertEqual(
                repeated["diagnostics"]["three_push_weak_suppressed_count"],
                0,
            )

            setup = track["setup"]
            neckline = float(setup["neckline"])
            atr = float(setup["atr"])
            index = len(candles)
            open_time = BASE_MS + index * DAY_MS
            close = neckline - atr
            candles.append(Candle(
                open_time=open_time,
                open=candles[-1].close,
                high=candles[-1].high,
                low=close - 0.4,
                close=close,
                volume=100.0,
                close_time=open_time + DAY_MS - 1,
            ))
            rows = rows_from_candles(candles)
            confirmed = radar.build(
                KlineSource(rows),
                now_ms=closed_now(rows),
            )  # type: ignore[arg-type]
            self.assertFalse(any(
                event["event"].startswith("three_push_")
                for event in confirmed["events"]
            ))
            self.assertEqual(
                confirmed["diagnostics"]["three_push_weak_suppressed_count"],
                1,
            )

            radar.commit(confirmed, [])
            finished = store.load(settings.consolidation_breakout_state_path, {})
            self.assertIsNone(finished["tracks"][key]["setup"])
            final_repeat = radar.build(
                KlineSource(rows),
                now_ms=closed_now(rows),
            )  # type: ignore[arg-type]
            self.assertEqual(
                final_repeat["diagnostics"]["three_push_weak_suppressed_count"],
                0,
            )

    def test_three_push_delivery_replays_forming_and_confirmed_events(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            candles = three_push_candles()
            rows = rows_from_candles(candles)
            settings = settings_for(
                root,
                consolidation_breakout_three_push_enable=True,
            )
            radar = ConsolidationBreakoutRadar(settings, JsonStore(root))

            forming = radar.build(
                KlineSource(rows),
                now_ms=closed_now(rows),
            )  # type: ignore[arg-type]
            forming_event = next(
                event
                for event in forming["events"]
                if event["event"] == "three_push_top_forming"
            )
            radar.commit(forming, [])
            replay = radar.build(
                KlineSource(rows),
                now_ms=closed_now(rows),
            )  # type: ignore[arg-type]
            replay_event = next(
                event
                for event in replay["events"]
                if event["event"] == "three_push_top_forming"
            )
            self.assertEqual(replay_event["event_id"], forming_event["event_id"])

            radar.commit(replay, [replay_event["event_id"]])
            accepted = radar.build(
                KlineSource(rows),
                now_ms=closed_now(rows),
            )  # type: ignore[arg-type]
            self.assertFalse(any(
                event["event"].startswith("three_push_")
                for event in accepted["events"]
            ))

            neckline = float(forming_event["neckline"])
            atr = float(forming_event["atr"])
            index = len(candles)
            open_time = BASE_MS + index * DAY_MS
            close = neckline - atr
            candles.append(Candle(
                open_time=open_time,
                open=candles[-1].close,
                high=max(candles[-1].close, close) + 0.4,
                low=close - 0.4,
                close=close,
                volume=100.0,
                close_time=open_time + DAY_MS - 1,
            ))
            rows = rows_from_candles(candles)
            confirmed = radar.build(
                KlineSource(rows),
                now_ms=closed_now(rows),
            )  # type: ignore[arg-type]
            confirmed_event = next(
                event
                for event in confirmed["events"]
                if event["event"] == "three_push_top_confirmed"
            )
            radar.commit(confirmed, [])
            confirmed_replay = radar.build(
                KlineSource(rows),
                now_ms=closed_now(rows),
            )  # type: ignore[arg-type]
            replayed_confirmation = next(
                event
                for event in confirmed_replay["events"]
                if event["event"] == "three_push_top_confirmed"
            )
            self.assertEqual(
                replayed_confirmation["event_id"],
                confirmed_event["event_id"],
            )

            radar.commit(
                confirmed_replay,
                [replayed_confirmation["event_id"]],
            )
            finished = radar.build(
                KlineSource(rows),
                now_ms=closed_now(rows),
            )  # type: ignore[arg-type]
            self.assertFalse(any(
                event["event"].startswith("three_push_")
                for event in finished["events"]
            ))
            state = JsonStore(root).load(
                settings.consolidation_breakout_state_path,
                {},
            )
            track = state["tracks"]["TESTUSDT|1d|three_push"]
            self.assertIsNone(track["setup"])
            self.assertEqual(
                track["last_pattern_id"],
                forming_event["pattern_id"],
            )

    def test_unaccepted_first_three_push_replays_after_a_new_closed_bar(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            candles = three_push_candles()
            rows = rows_from_candles(candles)
            settings = settings_for(
                root,
                consolidation_breakout_three_push_enable=True,
            )
            store = JsonStore(root)
            radar = ConsolidationBreakoutRadar(settings, store)

            first = radar.build(
                KlineSource(rows),
                now_ms=closed_now(rows),
            )  # type: ignore[arg-type]
            first_event = next(
                event
                for event in first["events"]
                if event["event"] == "three_push_top_forming"
            )
            radar.commit(first, [])
            saved = store.load(settings.consolidation_breakout_state_path, {})
            self.assertEqual(
                saved["tracks"]["TESTUSDT|1d|three_push"]["last_close_time"],
                candles[-2].close_time,
            )

            index = len(candles)
            open_time = BASE_MS + index * DAY_MS
            candles.append(Candle(
                open_time=open_time,
                open=114.0,
                high=114.4,
                low=113.6,
                close=114.0,
                volume=100.0,
                close_time=open_time + DAY_MS - 1,
            ))
            rows = rows_from_candles(candles)
            replay = radar.build(
                KlineSource(rows),
                now_ms=closed_now(rows),
            )  # type: ignore[arg-type]
            replayed_event = next(
                event
                for event in replay["events"]
                if event["event"] == "three_push_top_forming"
            )

            self.assertEqual(replayed_event["event_id"], first_event["event_id"])

    def test_catch_up_drops_forming_event_superseded_later_in_same_batch(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            candles = three_push_candles()
            prior_close_time = candles[-2].close_time
            index = len(candles)
            open_time = BASE_MS + index * DAY_MS
            candles.append(Candle(
                open_time=open_time,
                open=114.0,
                high=123.0,
                low=113.6,
                close=114.0,
                volume=100.0,
                close_time=open_time + DAY_MS - 1,
            ))
            rows = rows_from_candles(candles)
            settings = settings_for(
                root,
                consolidation_breakout_three_push_enable=True,
            )
            store = JsonStore(root)
            key = "TESTUSDT|1d|three_push"
            store.save(settings.consolidation_breakout_state_path, {
                "schema_version": 1,
                "tracks": {
                    key: {
                        "rule_version": 2,
                        "last_close_time": prior_close_time,
                        "setup": None,
                        "last_pattern_id": "",
                        "last_third_pivot_close_time": 0,
                        "pending_context": None,
                    },
                },
                "rotation": {"after_symbol": "", "round": 1},
            })
            radar = ConsolidationBreakoutRadar(settings, store)

            result = radar.build(
                KlineSource(rows),
                now_ms=closed_now(rows),
            )  # type: ignore[arg-type]

            self.assertFalse(any(
                event["event"].startswith("three_push_")
                for event in result["events"]
            ))
            committed = radar.commit(result, [])
            self.assertEqual(committed["deferred"], 0)
            saved = store.load(settings.consolidation_breakout_state_path, {})
            track = saved["tracks"][key]
            self.assertEqual(track["last_close_time"], candles[-1].close_time)
            self.assertIsNone(track["setup"])

    def test_corrupted_three_push_setup_is_discarded_without_stopping_scan(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            candles = three_push_candles()
            index = len(candles)
            open_time = BASE_MS + index * DAY_MS
            candles.append(Candle(
                open_time=open_time,
                open=114.0,
                high=123.0,
                low=113.6,
                close=114.0,
                volume=100.0,
                close_time=open_time + DAY_MS - 1,
            ))
            rows = rows_from_candles(candles)
            settings = settings_for(
                root,
                consolidation_breakout_three_push_enable=True,
            )
            store = JsonStore(root)
            key = "TESTUSDT|1d|three_push"
            store.save(settings.consolidation_breakout_state_path, {
                "schema_version": 1,
                "tracks": {
                    key: {
                        "last_close_time": candles[-2].close_time,
                        "setup": {
                            "structure": "top",
                            "bars_since": "damaged",
                            "neckline": 106.6,
                            "invalidation": 123.0,
                            "atr": 1.0,
                        },
                    },
                },
                "rotation": {"after_symbol": "", "round": 1},
            })
            radar = ConsolidationBreakoutRadar(settings, store)

            result = radar.build(
                KlineSource(rows),
                now_ms=closed_now(rows),
            )  # type: ignore[arg-type]
            committed = radar.commit(result, [])
            saved = store.load(settings.consolidation_breakout_state_path, {})

            self.assertEqual(result["events"], [])
            self.assertEqual(result["diagnostics"]["status"], "degraded")
            self.assertEqual(
                result["diagnostics"]["three_push_state_recoveries"],
                1,
            )
            self.assertEqual(committed["status"], "ok")
            self.assertIsNone(saved["tracks"][key]["setup"])

    def test_three_push_replay_freezes_box_context_quality_and_text(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            candles = three_push_candles()
            settings = settings_for(
                root,
                consolidation_breakout_three_push_enable=True,
            )
            store = JsonStore(root)
            store.save(settings.consolidation_breakout_state_path, {
                "schema_version": 1,
                "tracks": {
                    "TESTUSDT|1d|long": {
                        "last_close_time": candles[-2].close_time,
                        "box": {
                            "upper": 122.0,
                            "lower": 100.0,
                            "atr": 1.0,
                            "width_atr": 22.0,
                            "width_pct": 19.82,
                            "efficiency": 0.1,
                            "upper_touches": 2,
                            "lower_touches": 2,
                            "formed_close_time": candles[-2].close_time,
                            "active_bars": 0,
                            "base_bars": 240,
                            "upper_sweep_sent": False,
                            "lower_sweep_sent": False,
                        },
                        "breakout": None,
                        "cooldown_until": 0,
                    },
                },
                "rotation": {"after_symbol": "", "round": 1},
            })
            radar = ConsolidationBreakoutRadar(settings, store)
            rows = rows_from_candles(candles)
            result = radar.build(
                KlineSource(rows),
                now_ms=closed_now(rows),
            )  # type: ignore[arg-type]
            first = next(
                event
                for event in result["events"]
                if event["event"] == "three_push_top_forming"
            )
            self.assertNotIn("score", first)
            expected = (
                first["event_id"],
                first["structure_quality"],
                first["structure_quality_label"],
                first["text"],
                first["box_horizon"],
                first["box_age"],
                first["box_start_close_time"],
            )
            self.assertEqual(
                result["chart_payloads"][first["event_id"]][
                    "box_start_close_time"
                ],
                first["box_start_close_time"],
            )
            third_push_index = next(
                index
                for index, candle in enumerate(candles)
                if candle.close_time == first["push_close_times"][2]
            )
            candles[third_push_index] = replace(
                candles[third_push_index],
                volume=first["push_volumes"][0] * 2.0,
            )

            for _attempt in range(2):
                radar.commit(result, [])
                index = len(candles)
                open_time = BASE_MS + index * DAY_MS
                candles.append(Candle(
                    open_time=open_time,
                    open=114.0,
                    high=114.4,
                    low=113.6,
                    close=114.0,
                    volume=100.0,
                    close_time=open_time + DAY_MS - 1,
                ))
                rows = rows_from_candles(candles)
                result = radar.build(
                    KlineSource(rows),
                    now_ms=closed_now(rows),
                )  # type: ignore[arg-type]
                replay = next(
                    event
                    for event in result["events"]
                    if event["event"] == "three_push_top_forming"
                )
                self.assertEqual(
                    (
                        replay["event_id"],
                        replay["structure_quality"],
                        replay["structure_quality_label"],
                        replay["text"],
                        replay["box_horizon"],
                        replay["box_age"],
                        replay["box_start_close_time"],
                    ),
                    expected,
                )

    def test_range_and_three_push_events_commit_independently(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            candles = three_push_candles()
            last = candles[-1]
            candles[-1] = Candle(
                open_time=last.open_time,
                open=last.open,
                high=116.0,
                low=last.low,
                close=last.close,
                volume=last.volume,
                close_time=last.close_time,
            )
            rows = rows_from_candles(candles)
            settings = settings_for(
                root,
                consolidation_breakout_three_push_enable=True,
            )
            store = JsonStore(root)
            store.save(settings.consolidation_breakout_state_path, {
                "schema_version": 1,
                "tracks": {
                    "TESTUSDT|1d|long": {
                        "last_close_time": candles[-2].close_time,
                        "box": {
                            "upper": 115.0,
                            "lower": 100.0,
                            "atr": 1.0,
                            "width_atr": 15.0,
                            "width_pct": 13.95,
                            "efficiency": 0.1,
                            "upper_touches": 2,
                            "lower_touches": 2,
                            "formed_close_time": candles[-2].close_time,
                            "active_bars": 0,
                            "base_bars": 240,
                            "upper_sweep_sent": False,
                            "lower_sweep_sent": False,
                        },
                        "breakout": None,
                        "cooldown_until": 0,
                    },
                },
                "rotation": {"after_symbol": "", "round": 1},
            })
            radar = ConsolidationBreakoutRadar(settings, store)

            first = radar.build(
                KlineSource(rows),
                now_ms=closed_now(rows),
            )  # type: ignore[arg-type]
            by_name = {event["event"]: event for event in first["events"]}
            self.assertIn("upper_sweep", by_name)
            self.assertIn("three_push_top_forming", by_name)
            self.assertEqual(
                [event["event"] for event in first["events"][:2]],
                ["three_push_top_forming", "upper_sweep"],
            )
            self.assertEqual(sum(
                event["event"].startswith("three_push_")
                for event in first["events"]
            ), 1)

            capped_settings = settings_for(
                root,
                consolidation_breakout_three_push_enable=True,
                consolidation_breakout_max_signals_per_scan=1,
            )
            capped_radar = ConsolidationBreakoutRadar(capped_settings, store)
            capped = capped_radar.build(
                KlineSource(rows),
                now_ms=closed_now(rows),
            )  # type: ignore[arg-type]
            self.assertEqual(
                [event["event"] for event in capped["events"]],
                ["three_push_top_forming"],
            )
            self.assertEqual(capped["diagnostics"]["withheld_event_count"], 1)

            three_push_id = capped["events"][0]["event_id"]
            capped_radar.commit(capped, [three_push_id])
            second = capped_radar.build(
                KlineSource(rows),
                now_ms=closed_now(rows),
            )  # type: ignore[arg-type]
            self.assertEqual(
                [event["event"] for event in second["events"]],
                ["upper_sweep"],
            )

            capped_radar.commit(second, [second["events"][0]["event_id"]])
            finished = capped_radar.build(
                KlineSource(rows),
                now_ms=closed_now(rows),
            )  # type: ignore[arg-type]
            self.assertEqual(finished["events"], [])

    def test_open_candle_is_strictly_excluded(self) -> None:
        with TemporaryDirectory() as tmp:
            rows = [*range_klines(), breakout_up(250, volume=300)]
            settings = settings_for(Path(tmp))
            cutoff_before_breakout_close = int(rows[-1][6]) - 1
            now_ms = cutoff_before_breakout_close + settings.consolidation_breakout_close_delay_sec * 1000

            result = ConsolidationBreakoutRadar(
                settings,
                JsonStore(Path(tmp)),
            ).build(KlineSource(rows), now_ms=now_ms)  # type: ignore[arg-type]

            self.assertEqual(result["events"], [])
            self.assertEqual(result["diagnostics"]["closed_candles"], 250)

    def test_dry_run_commit_does_not_consume_event_state(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [*range_klines(), breakout_up(250)]
            settings = settings_for(root)
            radar = ConsolidationBreakoutRadar(settings, JsonStore(root))

            first = radar.build(KlineSource(rows), now_ms=closed_now(rows))  # type: ignore[arg-type]
            event_id = first["events"][0]["event_id"]
            dry_commit = radar.commit(first, [])
            replay = radar.build(KlineSource(rows), now_ms=closed_now(rows))  # type: ignore[arg-type]

            self.assertGreater(dry_commit["deferred"], 0)
            self.assertEqual(replay["events"][0]["event_id"], event_id)

            accepted = radar.commit(replay, [event_id])
            after_accept = radar.build(KlineSource(rows), now_ms=closed_now(rows))  # type: ignore[arg-type]
            self.assertEqual(accepted["status"], "ok")
            self.assertEqual(after_accept["events"], [])

    def test_signal_cap_commits_checkpoint_and_retries_later_event(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for(
                root,
                consolidation_breakout_max_signals_per_scan=1,
            )
            radar = ConsolidationBreakoutRadar(settings, JsonStore(root))
            rows = range_klines()
            formed = radar.build(KlineSource(rows), now_ms=closed_now(rows))  # type: ignore[arg-type]
            radar.commit(formed, [])

            rows.extend([
                breakout_up(250),
                kline(251, high=103.0, low=95.5, close=96.0),
            ])
            with patch(
                "radars.consolidation_breakout.radar._chart_payload",
                wraps=_chart_payload,
            ) as chart_builder:
                capped = radar.build(
                    KlineSource(rows),
                    now_ms=closed_now(rows),
                )  # type: ignore[arg-type]
            self.assertEqual(chart_builder.call_count, 1)
            self.assertEqual(len(capped["events"]), 1)
            self.assertEqual(capped["events"][0]["event"], "breakout_up")
            self.assertEqual(capped["diagnostics"]["withheld_event_count"], 1)
            self.assertEqual(
                set(capped["chart_payloads"]),
                {capped["events"][0]["event_id"]},
            )
            radar.commit(capped, [capped["events"][0]["event_id"]])

            retried = radar.build(KlineSource(rows), now_ms=closed_now(rows))  # type: ignore[arg-type]
            self.assertEqual(len(retried["events"]), 1)
            self.assertEqual(retried["events"][0]["event"], "breakout_down")
            self.assertEqual(
                set(retried["chart_payloads"]),
                {retried["events"][0]["event_id"]},
            )
            self.assert_chart_payload(
                retried,
                retried["events"][0],
                cutoff_ms=int(rows[-1][6]),
            )


if __name__ == "__main__":
    unittest.main()
