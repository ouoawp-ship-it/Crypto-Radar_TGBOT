from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from config import Settings
from radars.consolidation_breakout.radar import (
    Candle,
    ConsolidationBreakoutRadar,
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
        "consolidation_breakout_max_signals_per_scan": 8,
        "excluded_base_assets": ("XAU", "XAG"),
    }
    values.update(overrides)
    return Settings(**values)


def closed_now(rows: list[list[Any]], delay_sec: int = 90) -> int:
    return int(rows[-1][6]) + delay_sec * 1000


class ConsolidationBreakoutRadarTests(unittest.TestCase):
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
            self.assertIn("未来3根K线", event["text"])
            self.assertGreaterEqual(result["diagnostics"]["suppressed_horizon_events"], 2)

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
            capped = radar.build(KlineSource(rows), now_ms=closed_now(rows))  # type: ignore[arg-type]
            self.assertEqual(len(capped["events"]), 1)
            self.assertEqual(capped["events"][0]["event"], "breakout_up")
            self.assertEqual(capped["diagnostics"]["withheld_event_count"], 1)
            radar.commit(capped, [capped["events"][0]["event_id"]])

            retried = radar.build(KlineSource(rows), now_ms=closed_now(rows))  # type: ignore[arg-type]
            self.assertEqual(len(retried["events"]), 1)
            self.assertEqual(retried["events"][0]["event"], "breakout_down")


if __name__ == "__main__":
    unittest.main()
