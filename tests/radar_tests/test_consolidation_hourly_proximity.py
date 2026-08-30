from __future__ import annotations

import copy
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest

from config import Settings
from radars.consolidation_breakout.hourly_proximity import (
    ConsolidationHourlyProximityRadar,
    _box_id,
)
from radars.consolidation_breakout.radar import HORIZONS
from shared.storage import JsonStore


MINUTE_MS = 60_000
FIFTEEN_MINUTE_MS = 15 * MINUTE_MS
HOUR_MS = 60 * MINUTE_MS
END_CLOSE = (
    (1_700_006_399_999 + 1) // HOUR_MS * HOUR_MS
) - 1


def kline_row(
    open_time: int,
    duration_ms: int,
    close: float,
    *,
    high: float | None = None,
    low: float | None = None,
    volume: float = 100.0,
) -> list[Any]:
    return [
        open_time,
        str(close),
        str(close + 0.02 if high is None else high),
        str(close - 0.02 if low is None else low),
        str(close),
        str(volume),
        open_time + duration_ms - 1,
        "0",
        100,
        "0",
        "0",
        "0",
    ]


def hourly_rows(
    *,
    count: int = 250,
    end_close: int = END_CLOSE,
    final_closes: list[float] | None = None,
) -> list[list[Any]]:
    closes = [100.0] * count
    if final_closes:
        closes[-len(final_closes):] = final_closes
    first_open = end_close + 1 - count * HOUR_MS
    return [
        kline_row(
            first_open + index * HOUR_MS,
            HOUR_MS,
            close,
            volume=100.0 + index % 7,
        )
        for index, close in enumerate(closes)
    ]


def fifteen_minute_rows(
    final_closes: list[float],
    *,
    end_close: int = END_CLOSE,
    count: int = 84,
    final_high: float | None = None,
    final_low: float | None = None,
) -> list[list[Any]]:
    closes = [100.4] * count
    closes[-len(final_closes):] = final_closes
    first_open = end_close + 1 - count * FIFTEEN_MINUTE_MS
    rows = [
        kline_row(
            first_open + index * FIFTEEN_MINUTE_MS,
            FIFTEEN_MINUTE_MS,
            close,
            volume=80.0 + index,
        )
        for index, close in enumerate(closes)
    ]
    if final_high is not None:
        rows[-1][2] = str(final_high)
    if final_low is not None:
        rows[-1][3] = str(final_low)
    return rows


class IntervalSource:
    def __init__(
        self,
        rows_1h: list[list[Any]],
        rows_15m: list[list[Any]],
        *,
        symbol: str = "TESTUSDT",
        ticker: float = 100.9,
    ) -> None:
        self.symbol = symbol
        self.rows = {"1h": rows_1h, "15m": rows_15m}
        self.ticker = ticker
        self.calls: list[tuple[str, str, int]] = []

    def usdt_perp_symbols(self) -> list[dict[str, str]]:
        return [{"symbol": self.symbol}]

    def ticker_24h(self) -> list[dict[str, str]]:
        return [{
            "symbol": self.symbol,
            "lastPrice": str(self.ticker),
            "quoteVolume": "100000000",
        }]

    def klines(
        self,
        symbol: str,
        interval: str,
        limit: int,
    ) -> list[list[Any]]:
        self.calls.append((symbol, interval, limit))
        return copy.deepcopy(self.rows[interval][-limit:])


class MultiIntervalSource:
    def __init__(
        self,
        rows: dict[tuple[str, str], list[list[Any]]],
        tickers: dict[str, float],
        *,
        failures: set[tuple[str, str]] | None = None,
    ) -> None:
        self.rows = rows
        self.tickers = tickers
        self.failures = failures or set()
        self.calls: list[tuple[str, str, int]] = []

    def usdt_perp_symbols(self) -> list[dict[str, str]]:
        return [
            {"symbol": symbol}
            for symbol in sorted(self.tickers)
        ]

    def ticker_24h(self) -> list[dict[str, str]]:
        return [
            {
                "symbol": symbol,
                "lastPrice": str(price),
                "quoteVolume": "100000000",
            }
            for symbol, price in sorted(self.tickers.items())
        ]

    def klines(
        self,
        symbol: str,
        interval: str,
        limit: int,
    ) -> list[list[Any]]:
        self.calls.append((symbol, interval, limit))
        if (symbol, interval) in self.failures:
            raise TimeoutError(f"{symbol}:{interval}")
        return copy.deepcopy(self.rows.get((symbol, interval), [])[-limit:])


def settings_for(
    root: Path,
    *,
    shadow: bool = False,
    **overrides: Any,
) -> Settings:
    values: dict[str, Any] = {
        "data_dir": root,
        "excluded_base_assets": (),
        "consolidation_hourly_proximity_enable": True,
        "consolidation_hourly_proximity_shadow_mode": shadow,
        "consolidation_hourly_proximity_discovery_limit": 20,
        "consolidation_hourly_proximity_monitor_limit": 20,
        "consolidation_hourly_proximity_max_signals_per_scan": 4,
        "consolidation_hourly_proximity_state_path": root / "proximity.json",
        "consolidation_breakout_state_path": root / "legacy.json",
        "consolidation_daily_state_path": root / "daily.json",
        "consolidation_breakout_close_delay_sec": 90,
    }
    values.update(overrides)
    return Settings(**values)


def box_for(
    spec: Any,
    *,
    last_close: int,
    upper: float = 101.0,
    lower: float = 99.0,
    atr: float = 1.0,
) -> dict[str, Any]:
    return {
        "upper": upper,
        "lower": lower,
        "atr": atr,
        "width_atr": (upper - lower) / atr,
        "width_pct": (upper - lower) / ((upper + lower) / 2.0) * 100.0,
        "efficiency": 0.1,
        "upper_touches": 3,
        "lower_touches": 3,
        "formed_close_time": last_close - 5 * HOUR_MS,
        "window_start_close_time": last_close - spec.length * HOUR_MS,
        "active_bars": 5,
        "base_bars": spec.length,
    }


def active_track(box: dict[str, Any], last_close: int) -> dict[str, Any]:
    return {
        "box": copy.deepcopy(box),
        "breakout": None,
        "cooldown_until": 0,
        "last_close_time": last_close,
        "structure_active_bars": int(box.get("active_bars") or 0),
    }


def seed_hourly_boxes(
    store: JsonStore,
    settings: Settings,
    *,
    last_close: int = END_CLOSE,
    horizons: tuple[str, ...] = ("long",),
    upper: float = 101.0,
    lower: float = 99.0,
    atr: float = 1.0,
    monitor: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    tracks: dict[str, Any] = {}
    selected_box: dict[str, Any] = {}
    selected_box_id = ""
    for spec in HORIZONS:
        if spec.name not in horizons:
            continue
        box = box_for(
            spec,
            last_close=last_close,
            upper=upper,
            lower=lower,
            atr=atr,
        )
        tracks[f"TESTUSDT|1h|{spec.name}"] = active_track(box, last_close)
        if not selected_box or spec.length > int(selected_box["base_bars"]):
            selected_box = box
            selected_box_id = _box_id("TESTUSDT", spec.name, box)
        if monitor is not None:
            tracks[f"TESTUSDT|1h|{spec.name}|proximity"] = copy.deepcopy(
                monitor
            )
    store.save(settings.consolidation_hourly_proximity_state_path, {
        "schema_version": 1,
        "tracks": tracks,
        "rotation": {"after_symbol": "", "round": 1},
    })
    return selected_box, selected_box_id


def now_for(rows: list[list[Any]]) -> int:
    return int(rows[-1][6]) + 90_000


class ConsolidationHourlyProximityTests(unittest.TestCase):
    def test_disabled_makes_no_market_requests(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for(
                root,
                consolidation_hourly_proximity_enable=False,
            )
            source = IntervalSource(
                hourly_rows(),
                fifteen_minute_rows([100.7, 100.75, 100.82, 100.9]),
            )

            result = ConsolidationHourlyProximityRadar(settings).build(source)

        self.assertEqual(result["events"], [])
        self.assertEqual(result["diagnostics"]["status"], "disabled")
        self.assertEqual(source.calls, [])

    def test_15m_upper_preview_uses_only_longest_box_and_chart(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for(root)
            store = JsonStore(root)
            seed_hourly_boxes(
                store,
                settings,
                horizons=("short", "medium", "long"),
            )
            rows_15m = fifteen_minute_rows(
                [100.70, 100.75, 100.82, 100.90]
            )
            source = IntervalSource(
                hourly_rows(final_closes=[100.9]),
                rows_15m,
            )

            result = ConsolidationHourlyProximityRadar(
                settings,
                store,
            ).build(source, now_ms=now_for(rows_15m))

        self.assertEqual(len(result["events"]), 1)
        event = result["events"][0]
        self.assertEqual(event["event"], "proximity_upper")
        self.assertEqual(event["horizon"], "long")
        self.assertEqual(event["structure_timeframe"], "1h")
        self.assertEqual(event["trigger_timeframe"], "15m")
        self.assertGreater(event["volume_ratio"], 0)
        self.assertIn("临界预警，不是突破信号", event["text"])
        self.assertIn("无（仅1H结构）", event["text"])
        self.assertIn("结构依据｜上下沿触碰", event["text"])
        self.assertNotIn("/100", event["text"])
        self.assertNotIn("胜率", event["text"])
        payload = result["chart_payloads"][event["event_id"]]
        self.assertEqual(payload["structure_timeframe"], "1h")
        self.assertEqual(payload["trigger_timeframe"], "15m")
        self.assertEqual(
            payload["trigger_marker"]["close_time"],
            event["close_time"],
        )
        self.assertEqual(result["diagnostics"]["confluence_event_count"], 0)
        self.assertGreaterEqual(
            result["diagnostics"]["p95_decision_latency_sec"],
            90.0,
        )

    def test_lower_edge_is_symmetric(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for(root)
            store = JsonStore(root)
            seed_hourly_boxes(store, settings)
            rows_15m = fifteen_minute_rows(
                [99.30, 99.25, 99.18, 99.10]
            )
            source = IntervalSource(
                hourly_rows(),
                rows_15m,
                ticker=99.1,
            )

            result = ConsolidationHourlyProximityRadar(
                settings,
                store,
            ).build(source, now_ms=now_for(rows_15m))

        self.assertEqual(len(result["events"]), 1)
        event = result["events"][0]
        self.assertEqual(event["event"], "proximity_lower")
        self.assertEqual(event["direction"], "down")
        self.assertEqual(event["proximity_edge"], "lower")

    def test_wick_cross_and_unclosed_near_bar_are_suppressed(self) -> None:
        with self.subTest(case="wick_cross"):
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                settings = settings_for(root)
                store = JsonStore(root)
                seed_hourly_boxes(store, settings)
                rows_15m = fifteen_minute_rows(
                    [100.70, 100.75, 100.82, 100.90],
                    final_high=101.1,
                )
                result = ConsolidationHourlyProximityRadar(
                    settings,
                    store,
                ).build(
                    IntervalSource(hourly_rows(), rows_15m),
                    now_ms=now_for(rows_15m),
                )
            self.assertEqual(result["events"], [])
            self.assertGreater(
                result["diagnostics"]["suppression_counts"].get(
                    "wick_or_close_crossed",
                    0,
                ),
                0,
            )

        with self.subTest(case="unclosed_bar"):
            with TemporaryDirectory() as tmp:
                root = Path(tmp)
                settings = settings_for(root)
                store = JsonStore(root)
                seed_hourly_boxes(store, settings)
                closed = fifteen_minute_rows([100.4, 100.4, 100.4, 100.4])
                next_open = int(closed[-1][6]) + 1
                unclosed = kline_row(
                    next_open,
                    FIFTEEN_MINUTE_MS,
                    100.9,
                )
                rows_15m = [*closed, unclosed]
                result = ConsolidationHourlyProximityRadar(
                    settings,
                    store,
                ).build(
                    IntervalSource(hourly_rows(), rows_15m),
                    now_ms=now_for(closed),
                )
            self.assertEqual(result["events"], [])

    def test_newer_15m_sweep_cannot_fall_back_to_older_1h(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for(root)
            store = JsonStore(root)
            seed_hourly_boxes(store, settings)
            rows_15m = fifteen_minute_rows(
                [100.70, 100.75, 100.82, 100.90],
                end_close=END_CLOSE + FIFTEEN_MINUTE_MS,
                final_high=101.20,
            )
            result = ConsolidationHourlyProximityRadar(
                settings,
                store,
            ).build(
                IntervalSource(
                    hourly_rows(final_closes=[100.4, 100.82]),
                    rows_15m,
                ),
                now_ms=now_for(rows_15m),
            )

        self.assertEqual(result["events"], [])
        self.assertGreaterEqual(
            result["diagnostics"]["suppression_counts"].get(
                "wick_or_close_crossed",
                0,
            ),
            1,
        )
        self.assertGreaterEqual(
            result["diagnostics"]["suppression_counts"].get(
                "newer_15m_than_1h",
                0,
            ),
            1,
        )

    def test_1h_is_fallback_when_15m_approach_gate_does_not_pass(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for(root)
            store = JsonStore(root)
            seed_hourly_boxes(store, settings)
            rows_15m = fifteen_minute_rows(
                [100.80, 100.82, 100.81, 100.80]
            )
            source = IntervalSource(hourly_rows(), rows_15m, ticker=100.8)

            result = ConsolidationHourlyProximityRadar(
                settings,
                store,
            ).build(source, now_ms=now_for(rows_15m))

        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(result["events"][0]["trigger_timeframe"], "1h")
        self.assertEqual(
            result["events"][0]["proximity_rule"],
            "hourly_progress",
        )

    def test_hot_refreshes_stale_non_discovery_box_before_proximity(self) -> None:
        for transition in ("breakout", "sweep"):
            with self.subTest(transition=transition):
                with TemporaryDirectory() as tmp:
                    root = Path(tmp)
                    settings = settings_for(
                        root,
                        consolidation_hourly_proximity_discovery_limit=1,
                    )
                    store = JsonStore(root)
                    seed_hourly_boxes(
                        store,
                        settings,
                        last_close=END_CLOSE - 2 * HOUR_MS,
                    )
                    rows_15m = fifteen_minute_rows(
                        [100.70, 100.75, 100.82, 100.90]
                    )
                    if transition == "breakout":
                        rows_15m[-5][1] = "101.2"
                        rows_15m[-5][2] = "101.22"
                        rows_15m[-5][3] = "101.18"
                        rows_15m[-5][4] = "101.2"
                    else:
                        rows_15m[-5][2] = "101.2"
                    source = MultiIntervalSource(
                        {
                            ("AAAUSDT", "1h"): hourly_rows(),
                            ("BBBUSDT", "1h"): hourly_rows(),
                            ("TESTUSDT", "15m"): rows_15m,
                        },
                        {
                            "AAAUSDT": 200.0,
                            "BBBUSDT": 200.0,
                            "TESTUSDT": 100.9,
                        },
                    )
                    radar = ConsolidationHourlyProximityRadar(
                        settings,
                        store,
                    )

                    first = radar.build(
                        source,
                        now_ms=now_for(rows_15m),
                    )
                    radar.commit(first, set())
                    second = radar.build(
                        source,
                        now_ms=now_for(rows_15m),
                    )

                    saved = store.load(
                        settings.consolidation_hourly_proximity_state_path,
                        {},
                    )
                    saved_track = saved["tracks"]["TESTUSDT|1h|long"]

                self.assertEqual(first["events"], [])
                self.assertEqual(second["events"], [])
                self.assertGreaterEqual(
                    first["diagnostics"]["hot_structure_transition_count"],
                    1,
                )
                self.assertEqual(saved_track["last_close_time"], END_CLOSE)
                if transition == "breakout":
                    self.assertIsNone(saved_track["box"])
                else:
                    self.assertIsInstance(saved_track["box"], dict)
                    monitor = saved["tracks"][
                        "TESTUSDT|1h|long|proximity"
                    ]
                    self.assertEqual(
                        monitor["last_15m_close_time"],
                        END_CLOSE,
                    )
                    self.assertGreaterEqual(
                        first["diagnostics"]["suppression_counts"].get(
                            "formal_structure_transition",
                            0,
                        ),
                        1,
                    )

    def test_direct_1h_fallback_when_15m_request_fails(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for(
                root,
                consolidation_hourly_proximity_discovery_limit=1,
            )
            store = JsonStore(root)
            seed_hourly_boxes(store, settings)
            direct_hourly = hourly_rows(final_closes=[100.50, 100.80])
            source = MultiIntervalSource(
                {
                    ("AAAUSDT", "1h"): hourly_rows(),
                    ("TESTUSDT", "1h"): direct_hourly,
                },
                {"AAAUSDT": 200.0, "TESTUSDT": 100.8},
                failures={("TESTUSDT", "15m")},
            )
            radar = ConsolidationHourlyProximityRadar(settings, store)

            result = radar.build(
                source,
                now_ms=now_for(direct_hourly),
            )
            event = result["events"][0]
            radar.commit(result, {str(event["event_id"])})
            saved = store.load(
                settings.consolidation_hourly_proximity_state_path,
                {},
            )
            monitor = saved["tracks"]["TESTUSDT|1h|long|proximity"]

        self.assertEqual(len(result["events"]), 1)
        self.assertEqual(event["trigger_timeframe"], "1h")
        self.assertEqual(event["proximity_rule"], "hourly_progress")
        self.assertIn(("TESTUSDT", "15m", 84), source.calls)
        self.assertIn(("TESTUSDT", "1h", 264), source.calls)
        self.assertEqual(
            result["diagnostics"]["direct_hourly_fallback_attempt_count"],
            1,
        )
        self.assertEqual(
            result["diagnostics"]["direct_hourly_fallback_success_count"],
            1,
        )
        self.assertEqual(monitor["last_15m_close_time"], 0)
        self.assertEqual(monitor["last_1h_close_time"], END_CLOSE)

    def test_structure_gap_forces_full_1h_refresh_before_proximity(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for(
                root,
                consolidation_hourly_proximity_discovery_limit=1,
            )
            store = JsonStore(root)
            seed_hourly_boxes(
                store,
                settings,
                last_close=END_CLOSE - 30 * HOUR_MS,
            )
            rows_15m = fifteen_minute_rows(
                [100.70, 100.75, 100.82, 100.90]
            )
            direct_hourly = hourly_rows()
            direct_hourly[-26][1] = "101.2"
            direct_hourly[-26][2] = "101.22"
            direct_hourly[-26][3] = "101.18"
            direct_hourly[-26][4] = "101.2"
            source = MultiIntervalSource(
                {
                    ("AAAUSDT", "1h"): hourly_rows(),
                    ("TESTUSDT", "15m"): rows_15m,
                    ("TESTUSDT", "1h"): direct_hourly,
                },
                {"AAAUSDT": 200.0, "TESTUSDT": 100.9},
            )

            result = ConsolidationHourlyProximityRadar(
                settings,
                store,
            ).build(source, now_ms=now_for(rows_15m))

        self.assertEqual(result["events"], [])
        self.assertIn(("TESTUSDT", "1h", 264), source.calls)
        self.assertEqual(
            result["diagnostics"]["structure_history_gap_detected_count"],
            1,
        )
        self.assertEqual(
            result["diagnostics"]["structure_history_gap_unresolved_count"],
            0,
        )
        self.assertGreaterEqual(
            result["diagnostics"]["hot_structure_transition_count"],
            1,
        )

    def test_discovery_gap_rebuilds_instead_of_bridging_stale_box(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for(root)
            store = JsonStore(root)
            seed_hourly_boxes(
                store,
                settings,
                last_close=END_CLOSE - 300 * HOUR_MS,
            )
            rows_1h = hourly_rows(count=350)
            for index, row in enumerate(rows_1h):
                close = 99.2 + 1.7 * index / (len(rows_1h) - 1)
                row[1] = str(close)
                row[2] = str(close + 0.02)
                row[3] = str(close - 0.02)
                row[4] = str(close)
            # This real breakout is older than the 264-bar discovery response
            # and demonstrates why the missing interval cannot be bridged.
            rows_1h[-282][1] = "101.2"
            rows_1h[-282][2] = "101.22"
            rows_1h[-282][3] = "101.18"
            rows_1h[-282][4] = "101.2"
            rows_15m = fifteen_minute_rows(
                [100.70, 100.75, 100.82, 100.90]
            )
            radar = ConsolidationHourlyProximityRadar(settings, store)

            result = radar.build(
                IntervalSource(rows_1h, rows_15m),
                now_ms=now_for(rows_15m),
            )
            radar.commit(result, set())
            saved = store.load(
                settings.consolidation_hourly_proximity_state_path,
                {},
            )

        self.assertEqual(result["events"], [])
        self.assertEqual(
            result["diagnostics"]["discovery_history_gap_reset_count"],
            1,
        )
        self.assertIsNone(saved["tracks"]["TESTUSDT|1h|long"]["box"])

    def test_failed_delivery_replays_same_logical_event_id(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for(root)
            store = JsonStore(root)
            seed_hourly_boxes(store, settings)
            rows_15m = fifteen_minute_rows(
                [100.70, 100.75, 100.82, 100.90]
            )
            source = IntervalSource(hourly_rows(), rows_15m)
            radar = ConsolidationHourlyProximityRadar(settings, store)

            first = radar.build(source, now_ms=now_for(rows_15m))
            first_id = first["events"][0]["event_id"]
            deferred = radar.commit(first, set())

            newer_rows = fifteen_minute_rows(
                [100.72, 100.78, 100.84, 100.92],
                end_close=END_CLOSE + FIFTEEN_MINUTE_MS,
            )
            source.rows["15m"] = newer_rows
            source.ticker = 100.92
            second = radar.build(source, now_ms=now_for(newer_rows))
            second_id = second["events"][0]["event_id"]

            radar.commit(second, {second_id})
            third = radar.build(source, now_ms=now_for(newer_rows))

        self.assertGreater(deferred["deferred"], 0)
        self.assertEqual(first_id, second_id)
        self.assertEqual(third["events"], [])

    def test_shadow_observation_does_not_consume_future_live_alert(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            shadow_settings = settings_for(root, shadow=True)
            store = JsonStore(root)
            seed_hourly_boxes(store, shadow_settings)
            rows_15m = fifteen_minute_rows(
                [100.70, 100.75, 100.82, 100.90]
            )
            source = IntervalSource(
                hourly_rows(final_closes=[100.9]),
                rows_15m,
            )
            shadow_radar = ConsolidationHourlyProximityRadar(
                shadow_settings,
                store,
            )
            shadow_result = shadow_radar.build(
                source,
                now_ms=now_for(rows_15m),
            )
            shadow_id = shadow_result["events"][0]["event_id"]
            shadow_radar.commit(shadow_result, {shadow_id})

            live_settings = settings_for(root, shadow=False)
            live_result = ConsolidationHourlyProximityRadar(
                live_settings,
                store,
            ).build(source, now_ms=now_for(rows_15m))

        self.assertEqual(len(live_result["events"]), 1)
        self.assertEqual(live_result["events"][0]["event_id"], shadow_id)
        self.assertFalse(live_result["events"][0]["shadow_mode"])

    def test_two_hour_retreat_rearms_and_changes_epoch(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for(root)
            store = JsonStore(root)
            spec = next(item for item in HORIZONS if item.name == "long")
            box = box_for(spec, last_close=END_CLOSE)
            box_id = _box_id("TESTUSDT", "long", box)
            monitor = {
                "source_box_id": box_id,
                "last_15m_close_time": END_CLOSE,
                "last_1h_close_time": END_CLOSE,
                "edges": {
                    "upper": {
                        "live_sent": True,
                        "shadow_seen": False,
                        "rearm_count": 0,
                        "last_event_id": "old",
                        "last_event_close_time": END_CLOSE - HOUR_MS,
                        "last_trigger_timeframe": "15m",
                        "last_rearm_close_time": 0,
                    },
                    "lower": {
                        "live_sent": False,
                        "shadow_seen": False,
                        "rearm_count": 0,
                        "last_event_id": "",
                        "last_event_close_time": 0,
                        "last_trigger_timeframe": "",
                        "last_rearm_close_time": 0,
                    },
                },
            }
            seed_hourly_boxes(store, settings, monitor=monitor)
            far_1h = hourly_rows(final_closes=[100.0, 100.0])
            far_15m = fifteen_minute_rows([100.0, 100.0, 100.0, 100.0])
            radar = ConsolidationHourlyProximityRadar(settings, store)
            retreat = radar.build(
                IntervalSource(far_1h, far_15m, ticker=100.0),
                now_ms=now_for(far_15m),
            )
            radar.commit(retreat, set())

            saved = store.load(settings.consolidation_hourly_proximity_state_path, {})
            upper_state = saved["tracks"][
                "TESTUSDT|1h|long|proximity"
            ]["edges"]["upper"]

            near_rows = fifteen_minute_rows(
                [100.70, 100.75, 100.82, 100.90],
                end_close=END_CLOSE + FIFTEEN_MINUTE_MS,
            )
            near = radar.build(
                IntervalSource(far_1h, near_rows, ticker=100.9),
                now_ms=now_for(near_rows),
            )

        self.assertEqual(upper_state["rearm_count"], 1)
        self.assertFalse(upper_state["live_sent"])
        self.assertEqual(len(near["events"]), 1)
        self.assertEqual(near["events"][0]["rearm_count"], 1)
        self.assertTrue(near["events"][0]["event_id"].endswith(":r1"))

    def test_higher_timeframe_confluence_is_same_side_and_high_to_low(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for(root)
            store = JsonStore(root)
            seed_hourly_boxes(store, settings)
            legacy_tracks: dict[str, Any] = {}
            prices = {"4h": 101.10, "1d": 101.15, "1w": 100.95}
            long_spec = next(item for item in HORIZONS if item.name == "long")
            for timeframe, upper in prices.items():
                box = box_for(
                    long_spec,
                    last_close=END_CLOSE,
                    upper=upper,
                    lower=90.0,
                    atr=2.0,
                )
                legacy_tracks[f"TESTUSDT|{timeframe}|long"] = active_track(
                    box,
                    END_CLOSE,
                )
            # This opposite-side boundary is close to the hourly upper, but its
            # own upper is far away and therefore must not create 4H confluence.
            mismatch = box_for(
                long_spec,
                last_close=END_CLOSE,
                upper=110.0,
                lower=101.0,
                atr=2.0,
            )
            legacy_tracks["TESTUSDT|4h|medium"] = active_track(
                mismatch,
                END_CLOSE,
            )
            store.save(settings.consolidation_breakout_state_path, {
                "schema_version": 1,
                "tracks": legacy_tracks,
            })
            rows_15m = fifteen_minute_rows(
                [100.70, 100.75, 100.82, 100.90]
            )
            result = ConsolidationHourlyProximityRadar(
                settings,
                store,
            ).build(
                IntervalSource(hourly_rows(), rows_15m),
                now_ms=now_for(rows_15m),
            )

        event = result["events"][0]
        self.assertEqual(
            event["higher_tf_confluence_timeframes"],
            "1w,1d,4h",
        )
        self.assertEqual(
            [item["timeframe"] for item in event["higher_tf_confluence"]],
            ["1w", "1d", "4h"],
        )
        summary = event["higher_tf_confluence_summary"]
        self.assertLess(summary.index("1W"), summary.index("1D"))
        self.assertLess(summary.index("1D"), summary.index("4H"))


if __name__ == "__main__":
    unittest.main()
