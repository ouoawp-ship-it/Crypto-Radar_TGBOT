from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.config import Settings
from paopao_radar.launch_lifecycle import LaunchLifecycleStore
from paopao_radar.launch_price_action import (
    advance_price_action_state,
    analyze_launch_price_action,
    required_15m_kline_limit,
)
from paopao_radar.radar import RadarEngine
from paopao_radar.storage import JsonStore
from paopao_radar.time_windows import closed_window


INTERVAL_MS = 15 * 60 * 1000


def kline(
    open_time_ms: int,
    open_price: float,
    high: float,
    low: float,
    close: float,
) -> list[object]:
    return [
        open_time_ms,
        str(open_price),
        str(high),
        str(low),
        str(close),
        "0",
        open_time_ms + INTERVAL_MS - 1,
        "100000",
    ]


def analysis(
    *,
    event_15m: str,
    end_15m: int,
    close_15m: float,
    high_15m: float,
    low_15m: float,
    open_15m: float,
    event_1h: str = "insufficient_history",
    end_1h: int = 0,
    close_1h: float = 0,
    high_1h: float = 0,
    low_1h: float = 0,
    open_1h: float = 0,
    event_4h: str = "insufficient_history",
    end_4h: int = 0,
    close_4h: float = 0,
    high_4h: float = 0,
    low_4h: float = 0,
    open_4h: float = 0,
) -> dict[str, object]:
    def frame(
        event: str,
        end: int,
        open_price: float,
        high: float,
        low: float,
        close: float,
    ) -> dict[str, object]:
        candle_range = high - low
        body = abs(close - open_price)
        upper_wick = high - max(open_price, close)
        lower_wick = min(open_price, close) - low
        denominator = body if body > 0 else max(0.01, candle_range * 0.01)
        return {
            "data_status": "ready",
            "event": event,
            "candle_end_ts": end,
            "open": open_price,
            "high": high,
            "low": low,
            "close": close,
            "body_ratio": body / candle_range if candle_range > 0 else 0.0,
            "upper_wick_body_ratio": upper_wick / denominator,
            "lower_wick_body_ratio": lower_wick / denominator,
            "box_high": 100.0,
            "box_low": 95.0,
        }

    return {
        "version": 1,
        "data_status": "ready",
        "min_body_ratio": 0.45,
        "wick_body_ratio": 1.5,
        "timeframes": {
            "15m": frame(
                event_15m,
                end_15m,
                open_15m,
                high_15m,
                low_15m,
                close_15m,
            ),
            "1h": frame(
                event_1h,
                end_1h,
                open_1h,
                high_1h,
                low_1h,
                close_1h,
            ),
            "4h": frame(
                event_4h,
                end_4h,
                open_4h,
                high_4h,
                low_4h,
                close_4h,
            ),
        },
    }


def lifecycle_snapshot(
    *,
    window_end_ts: int,
    price_action_analysis: dict[str, object],
) -> dict[str, object]:
    return {
        "symbol": "TESTUSDT",
        "window_end_ts": window_end_ts,
        "score": 75,
        "closed_price": 103.0,
        "closed_oi_usd": 1_000_000.0,
        "closed_quote_volume": 10_000_000.0,
        "price_15m": 2.0,
        "price_1h": 5.0,
        "oi_15m": 1.0,
        "oi_1h": 3.0,
        "volume_ratio": 2.0,
        "funding_pct": -0.01,
        "funding_interval_hours": 8,
        "breakout": True,
        "breakout_price": 100.0,
        "data_quality_status": "confirmed",
        "data_quality_score": 100,
        "quality_gate": "allow",
        "reasons": ["test"],
        "price_action_analysis": price_action_analysis,
    }


class LaunchPriceActionTests(unittest.TestCase):
    def test_detects_body_confirmed_breakout_from_closed_15m_box(self) -> None:
        rows = [
            kline(index * INTERVAL_MS, 100.0, 102.0, 99.0, 101.0)
            for index in range(16)
        ]
        rows.append(kline(16 * INTERVAL_MS, 101.0, 104.0, 100.5, 103.5))

        result = analyze_launch_price_action(
            rows,
            window_end_ms=17 * INTERVAL_MS,
        )

        frame = result["timeframes"]["15m"]  # type: ignore[index]
        self.assertEqual(frame["data_status"], "ready")
        self.assertEqual(frame["event"], "breakout_up")
        self.assertEqual(frame["box_high"], 102.0)
        self.assertGreater(frame["body_ratio"], 0.45)

    def test_detects_long_upper_wick_liquidity_sweep(self) -> None:
        rows = [
            kline(index * INTERVAL_MS, 100.0, 102.0, 99.0, 101.0)
            for index in range(16)
        ]
        rows.append(kline(16 * INTERVAL_MS, 101.0, 105.0, 100.5, 101.2))

        result = analyze_launch_price_action(
            rows,
            window_end_ms=17 * INTERVAL_MS,
        )

        frame = result["timeframes"]["15m"]  # type: ignore[index]
        self.assertEqual(frame["event"], "sweep_high")
        self.assertGreater(frame["upper_wick_body_ratio"], 1.5)
        self.assertLess(frame["close"], frame["box_high"])

    def test_higher_timeframe_ignores_partial_candle(self) -> None:
        rows = [
            kline(index * INTERVAL_MS, 100.0, 110.0, 99.0, 100.5 + index)
            for index in range(5)
        ]

        result = analyze_launch_price_action(
            rows,
            window_end_ms=5 * INTERVAL_MS,
            lookback=2,
        )

        frame_1h = result["timeframes"]["1h"]  # type: ignore[index]
        self.assertEqual(frame_1h["candle_end_ts"], 3600)
        self.assertEqual(frame_1h["close"], 103.5)

    def test_full_smc_analysis_is_opt_in_and_uses_closed_timeframes(self) -> None:
        rows = [
            kline(index * INTERVAL_MS, 100.0, 102.0, 99.0, 101.0)
            for index in range(400)
        ]

        disabled = analyze_launch_price_action(
            rows,
            window_end_ms=400 * INTERVAL_MS,
        )
        enabled = analyze_launch_price_action(
            rows,
            window_end_ms=400 * INTERVAL_MS,
            smc_enable=True,
        )

        self.assertNotIn("smc_analysis", disabled)
        smc = enabled["smc_analysis"]
        self.assertEqual(smc["timeframes"]["15m"]["candle_count"], 400)
        self.assertEqual(smc["timeframes"]["1h"]["candle_count"], 100)
        self.assertEqual(smc["timeframes"]["4h"]["candle_count"], 25)
        self.assertEqual(smc["data_status"], "ready")

    def test_state_advances_15m_to_1h_to_4h_on_same_frozen_level(self) -> None:
        first = advance_price_action_state(
            None,
            analysis(
                event_15m="breakout_up",
                end_15m=900,
                open_15m=98,
                high_15m=104,
                low_15m=97,
                close_15m=103,
            ),
        )
        second = advance_price_action_state(
            first,
            analysis(
                event_15m="inside",
                end_15m=1800,
                open_15m=102,
                high_15m=104,
                low_15m=101,
                close_15m=103,
                event_1h="inside",
                end_1h=3600,
                open_1h=99,
                high_1h=106,
                low_1h=98,
                close_1h=105,
            ),
        )
        third = advance_price_action_state(
            second,
            analysis(
                event_15m="inside",
                end_15m=4500,
                open_15m=104,
                high_15m=106,
                low_15m=103,
                close_15m=105,
                event_1h="inside",
                end_1h=3600,
                open_1h=99,
                high_1h=106,
                low_1h=98,
                close_1h=105,
                event_4h="inside",
                end_4h=14400,
                open_4h=98,
                high_4h=110,
                low_4h=97,
                close_4h=108,
            ),
        )

        self.assertEqual(first["status"], "breakout_15m")
        self.assertEqual(first["level"], 100.0)
        self.assertEqual(first["lookback"], 16)
        self.assertEqual(first["event_window_end_ts"], 900)
        self.assertEqual(first["box_high"], 100.0)
        self.assertEqual(first["box_low"], 95.0)
        self.assertEqual(second["status"], "confirmed_1h")
        self.assertEqual(second["event_window_end_ts"], 3600)
        self.assertEqual(third["status"], "confirmed_4h")
        self.assertEqual(third["event_window_end_ts"], 14400)
        self.assertEqual(third["confirmed_timeframes"], ["15m", "1h", "4h"])

    def test_reentry_with_long_wick_marks_false_breakout(self) -> None:
        first = advance_price_action_state(
            None,
            analysis(
                event_15m="breakout_up",
                end_15m=900,
                open_15m=98,
                high_15m=104,
                low_15m=97,
                close_15m=103,
            ),
        )
        failed = advance_price_action_state(
            first,
            analysis(
                event_15m="inside",
                end_15m=1800,
                open_15m=101,
                high_15m=105,
                low_15m=98,
                close_15m=99,
            ),
        )

        self.assertEqual(failed["status"], "false_breakout_15m")
        self.assertEqual(failed["event_window_end_ts"], 1800)
        self.assertIn("false_breakout_15m", failed["event_key"])

    def test_lifecycle_persists_and_publishes_price_action_change(self) -> None:
        with TemporaryDirectory() as tmp:
            store = LaunchLifecycleStore(
                Path(tmp) / "signals.db",
                package_enabled=True,
                price_action_enabled=True,
            )
            opened = store.record_observation(
                lifecycle_snapshot(
                    window_end_ts=900,
                    price_action_analysis=analysis(
                        event_15m="breakout_up",
                        end_15m=900,
                        open_15m=98,
                        high_15m=104,
                        low_15m=97,
                        close_15m=103,
                    ),
                ),
                stage="breakout",
                observed_at=910,
            )
            store.commit_package(
                cycle_id=opened["cycle_id"],
                observation_id=opened["observation_id"],
                message_ids=[101],
                checkpoint_reasons=["cycle_opened"],
                published_at=920,
            )
            confirmed = store.record_observation(
                lifecycle_snapshot(
                    window_end_ts=1800,
                    price_action_analysis=analysis(
                        event_15m="inside",
                        end_15m=1800,
                        open_15m=102,
                        high_15m=104,
                        low_15m=101,
                        close_15m=103,
                        event_1h="inside",
                        end_1h=3600,
                        open_1h=99,
                        high_1h=106,
                        low_1h=98,
                        close_1h=105,
                    ),
                ),
                stage="breakout",
                observed_at=1810,
            )

            self.assertEqual(opened["price_action"]["status"], "breakout_15m")
            self.assertEqual(confirmed["price_action"]["status"], "confirmed_1h")
            self.assertIn(
                "price_action_changed",
                confirmed["publication"]["checkpoint_reasons"],
            )
            observation = store.list_observations(opened["cycle_id"])[-1]
            self.assertIn("confirmed_1h", observation["price_action_json"])

    def test_active_cycle_requests_only_enough_history_for_1h_box(self) -> None:
        self.assertEqual(required_15m_kline_limit(16, follow_up=False), 17)
        self.assertEqual(required_15m_kline_limit(16, follow_up=True), 72)
        self.assertEqual(
            required_15m_kline_limit(
                16,
                follow_up=True,
                smc_history_bars=400,
            ),
            400,
        )
        self.assertEqual(
            required_15m_kline_limit(
                16,
                follow_up=False,
                smc_history_bars=400,
            ),
            17,
        )

    def test_active_radar_analysis_keeps_legacy_score_on_latest_17_bars(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                launch_price_action_v3_enable=True,
            )
            engine = RadarEngine(settings, JsonStore(Path(tmp)))
            window = closed_window(
                interval_sec=15 * 60,
                delay_sec=settings.launch_close_delay_sec,
            )
            start_ms = window.end_ms - 72 * INTERVAL_MS
            rows = [
                kline(start_ms + index * INTERVAL_MS, 100, 1000, 99, 100)
                for index in range(55)
            ]
            rows.extend(
                kline(start_ms + index * INTERVAL_MS, 100, 102, 99, 100)
                for index in range(55, 71)
            )
            rows.append(
                kline(start_ms + 71 * INTERVAL_MS, 100, 104, 99, 103)
            )
            rows[-1][7] = "300000"
            oi_history = [
                {"sumOpenInterestValue": str(1_000_000 + index * 1000)}
                for index in range(17)
            ]

            class Source:
                kline_limit = 0

                @classmethod
                def klines(cls, *_args: object, **kwargs: object) -> list[list[object]]:
                    cls.kline_limit = int(kwargs["limit"])
                    return rows

                @staticmethod
                def open_interest_hist(
                    *_args: object,
                    **_kwargs: object,
                ) -> list[dict[str, str]]:
                    return oi_history

            result = engine._analyze_launch_symbol(  # type: ignore[arg-type]
                Source(),
                {
                    "symbol": "TESTUSDT",
                    "coin": "TEST",
                    "quote_volume": 10_000_000,
                    "price_24h": 1.0,
                    "price": 103.0,
                    "funding_available": False,
                    "funding_pct": 0.0,
                    "funding_next_time_ms": 0,
                    "launch_lifecycle_active": True,
                    "mcap": 100_000_000,
                    "mcap_source": "Binance",
                    "market_cap_tier": "低市值",
                    "liquidity_tier": "低流动性",
                },
            )

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(Source.kline_limit, 72)
            self.assertEqual(result["breakout_price"], 102.0)
            self.assertTrue(result["breakout"])
            self.assertEqual(result["kline_points"], 72)

    def test_active_smc_cycle_reuses_one_kline_request_with_deeper_history(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                launch_price_action_v3_enable=True,
                launch_smc_v4_enable=True,
                launch_smc_history_bars=400,
            )
            engine = RadarEngine(settings, JsonStore(Path(tmp)))
            window = closed_window(
                interval_sec=15 * 60,
                delay_sec=settings.launch_close_delay_sec,
            )
            start_ms = window.end_ms - 400 * INTERVAL_MS
            rows = [
                kline(
                    start_ms + index * INTERVAL_MS,
                    100,
                    102,
                    99,
                    101,
                )
                for index in range(400)
            ]
            oi_history = [
                {"sumOpenInterestValue": str(1_000_000 + index * 1000)}
                for index in range(17)
            ]

            class Source:
                kline_limit = 0

                @classmethod
                def klines(
                    cls,
                    *_args: object,
                    **kwargs: object,
                ) -> list[list[object]]:
                    cls.kline_limit = int(kwargs["limit"])
                    return rows

                @staticmethod
                def open_interest_hist(
                    *_args: object,
                    **_kwargs: object,
                ) -> list[dict[str, str]]:
                    return oi_history

            result = engine._analyze_launch_symbol(  # type: ignore[arg-type]
                Source(),
                {
                    "symbol": "TESTUSDT",
                    "coin": "TEST",
                    "quote_volume": 10_000_000,
                    "price_24h": 1.0,
                    "price": 101.0,
                    "funding_available": False,
                    "funding_pct": 0.0,
                    "funding_next_time_ms": 0,
                    "launch_lifecycle_active": True,
                    "mcap": 100_000_000,
                    "mcap_source": "Binance",
                    "market_cap_tier": "低市值",
                    "liquidity_tier": "低流动性",
                },
            )

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(Source.kline_limit, 400)
            self.assertEqual(result["kline_points"], 400)
            self.assertIn("smc_analysis", result["price_action_analysis"])

    def test_structure_status_is_formatted_for_existing_launch_package(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = RadarEngine(
                Settings(data_dir=Path(tmp)),
                JsonStore(Path(tmp)),
            )
            lines = engine._launch_price_action_lines({
                "enabled": True,
                "status": "confirmed_1h",
                "direction": "up",
                "level": 100.0,
                "confirmed_timeframes": ["15m", "1h"],
                "timeframes": {"1h": {}},
            })

            text = "\n".join(lines)
            self.assertIn("1h实体收盘确认，等待4h确认", text)
            self.assertIn("结构位 $100", text)
            self.assertIn("已确认 15m→1h", text)

    def test_full_smc_status_is_formatted_for_existing_launch_package(self) -> None:
        lines = RadarEngine._launch_smc_lines({
            "enabled": True,
            "status": "bos_confirmed",
            "direction": "up",
            "htf_bias": {"direction": "up"},
            "latest_event": {"label": "BOS"},
            "snapshot": {
                "timeframes": {
                    "15m": {
                        "fvgs": [{"status": "active"}],
                        "order_blocks": [{"status": "mitigated"}],
                    },
                },
            },
        })

        text = "\n".join(lines)
        self.assertIn("SMC 结构", text)
        self.assertIn("完整链确认", text)
        self.assertIn("高周期 偏多", text)
        self.assertIn("活动FVG 1 / OB 1", text)


if __name__ == "__main__":
    unittest.main()
