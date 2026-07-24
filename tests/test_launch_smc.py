from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.launch_lifecycle import LaunchLifecycleStore
from paopao_radar.launch_price_action import advance_price_action_state
from paopao_radar.launch_smc import advance_smc_state, analyze_smc_frames


def candle(
    index: int,
    *,
    open_price: float,
    high: float,
    low: float,
    close: float,
    interval_sec: int = 900,
) -> dict[str, float | int]:
    return {
        "open_ts": index * interval_sec,
        "close_ts": (index + 1) * interval_sec,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
    }


def frames(
    candles_15m: list[dict[str, float | int]],
    candles_1h: list[dict[str, float | int]] | None = None,
    candles_4h: list[dict[str, float | int]] | None = None,
) -> dict[str, list[dict[str, float | int]]]:
    return {
        "15m": candles_15m,
        "1h": candles_1h or [],
        "4h": candles_4h or [],
    }


def bullish_break_series() -> list[dict[str, float | int]]:
    values = [
        (9.0, 10.0, 8.0, 9.5),
        (9.5, 11.0, 9.0, 10.2),
        (10.2, 12.0, 9.8, 11.0),
        (11.0, 15.0, 10.5, 13.0),
        (13.0, 13.5, 10.0, 11.0),
        (11.0, 11.8, 9.5, 10.5),
        (14.5, 16.0, 13.5, 14.0),
        (14.0, 16.5, 13.8, 16.0),
    ]
    return [
        candle(
            index,
            open_price=value[0],
            high=value[1],
            low=value[2],
            close=value[3],
        )
        for index, value in enumerate(values)
    ]


class LaunchSmcTests(unittest.TestCase):
    def test_swing_waits_for_right_side_closed_candles(self) -> None:
        values = [
            (9, 10, 8, 9.5),
            (9.5, 11, 9, 10),
            (10, 12, 9, 11),
            (11, 11.5, 9.5, 10),
            (10, 12, 9, 11),
            (11, 15, 10, 13),
            (13, 13.5, 9.5, 11),
            (11, 11.5, 9, 10),
        ]
        rows = [
            candle(
                index,
                open_price=value[0],
                high=value[1],
                low=value[2],
                close=value[3],
            )
            for index, value in enumerate(values)
        ]

        before = analyze_smc_frames(frames(rows[:7]), swing_length=2)
        after = analyze_smc_frames(frames(rows), swing_length=2)
        target_ts = rows[5]["close_ts"]

        self.assertNotIn(
            target_ts,
            [item["ts"] for item in before["timeframes"]["15m"]["swings"]],
        )
        confirmed = [
            item
            for item in after["timeframes"]["15m"]["swings"]
            if item["ts"] == target_ts
        ]
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0]["confirmed_ts"], rows[7]["close_ts"])

    def test_structure_break_requires_body_close_not_wick(self) -> None:
        rows = bullish_break_series()

        wick_only = analyze_smc_frames(frames(rows[:7]), swing_length=2)
        close_break = analyze_smc_frames(frames(rows), swing_length=2)

        self.assertEqual(
            wick_only["timeframes"]["15m"]["structures"],
            [],
        )
        structures = close_break["timeframes"]["15m"]["structures"]
        self.assertEqual(len(structures), 1)
        self.assertEqual(structures[0]["type"], "BOS")
        self.assertEqual(structures[0]["direction"], "up")
        self.assertEqual(structures[0]["event_ts"], rows[-1]["close_ts"])

    def test_gap_degrades_without_publishing_structure(self) -> None:
        rows = bullish_break_series()
        rows[5]["close_ts"] = int(rows[5]["close_ts"]) + 900

        result = analyze_smc_frames(frames(rows), swing_length=2)
        frame_15m = result["timeframes"]["15m"]

        self.assertEqual(frame_15m["data_status"], "gap")
        self.assertEqual(frame_15m["structures"], [])
        self.assertEqual(frame_15m["events"], [])

    def test_equal_high_pool_records_long_wick_liquidity_sweep(self) -> None:
        values = [
            (9, 10, 8, 9.5),
            (10, 15, 9, 12),
            (12, 12.5, 7, 9),
            (9, 15.05, 8.5, 12),
            (12, 12.5, 8, 10),
            (10, 16, 9, 14),
            (14, 14.5, 10, 12),
        ]
        rows = [
            candle(
                index,
                open_price=value[0],
                high=value[1],
                low=value[2],
                close=value[3],
            )
            for index, value in enumerate(values)
        ]

        result = analyze_smc_frames(
            frames(rows),
            swing_length=1,
            equal_tolerance_atr=0.2,
        )

        swept = [
            pool
            for pool in result["timeframes"]["15m"]["liquidity"]
            if pool["type"] == "BSL" and pool["status"] == "swept"
        ]
        self.assertEqual(len(swept), 1)
        self.assertEqual(swept[0]["direction"], "down")
        self.assertEqual(swept[0]["event_ts"], rows[5]["close_ts"])

    def test_fvg_mitigation_order_block_breaker_and_dealing_range(self) -> None:
        rows = bullish_break_series()
        rows.extend([
            candle(8, open_price=15.8, high=16.2, low=14.2, close=15.5),
            candle(9, open_price=15.4, high=15.6, low=12.5, close=13.0),
            candle(10, open_price=13.0, high=14.2, low=12.4, close=12.8),
            candle(11, open_price=12.8, high=14.0, low=12.5, close=13.5),
            candle(12, open_price=13.5, high=15.5, low=13.0, close=15.0),
            candle(13, open_price=15.0, high=16.0, low=14.5, close=15.5),
        ])

        result = analyze_smc_frames(
            frames(rows),
            swing_length=2,
            displacement_body_atr=0.5,
        )
        frame_15m = result["timeframes"]["15m"]

        self.assertTrue(frame_15m["fvgs"])
        self.assertTrue(frame_15m["order_blocks"])
        bullish_blocks = [
            block
            for block in frame_15m["order_blocks"]
            if block["direction"] == "up"
        ]
        self.assertTrue(bullish_blocks)
        self.assertEqual(bullish_blocks[0]["status"], "invalidated")
        self.assertTrue(frame_15m["mitigation_blocks"])
        self.assertTrue(frame_15m["breaker_blocks"])
        self.assertEqual(
            frame_15m["breaker_blocks"][0]["direction"],
            "down",
        )
        self.assertTrue(frame_15m["dealing_range"])

    def test_higher_timeframe_bias_and_execution_alignment(self) -> None:
        rows = bullish_break_series()
        rows_1h = [
            {
                **item,
                "open_ts": index * 3600,
                "close_ts": (index + 1) * 3600,
            }
            for index, item in enumerate(rows)
        ]
        rows_4h = [
            {
                **item,
                "open_ts": index * 14400,
                "close_ts": (index + 1) * 14400,
            }
            for index, item in enumerate(rows)
        ]
        result = analyze_smc_frames(
            frames(rows, rows_1h, rows_4h),
            swing_length=2,
        )

        self.assertEqual(result["htf_bias"]["direction"], "up")
        self.assertEqual(result["htf_bias"]["alignment"], "aligned")
        self.assertEqual(
            result["htf_bias"]["execution_alignment"],
            "aligned",
        )

    def test_state_advances_full_smc_sequence_idempotently(self) -> None:
        events = [
            {
                "key": "sweep",
                "event_type": "liquidity_sweep",
                "label": "SSL SWEEP",
                "direction": "up",
                "event_ts": 900,
                "priority": 10,
            },
            {
                "key": "mss",
                "event_type": "structure",
                "label": "MSS",
                "direction": "up",
                "event_ts": 1800,
                "priority": 20,
            },
            {
                "key": "fvg",
                "event_type": "displacement_fvg",
                "label": "FVG",
                "direction": "up",
                "event_ts": 1800,
                "priority": 30,
            },
            {
                "key": "retest",
                "event_type": "mitigation",
                "label": "MB",
                "direction": "up",
                "event_ts": 2700,
                "priority": 40,
            },
            {
                "key": "bos",
                "event_type": "structure",
                "label": "BOS",
                "direction": "up",
                "event_ts": 3600,
                "priority": 20,
            },
        ]
        analysis = {
            "enabled": True,
            "data_status": "ready",
            "events": events,
            "htf_bias": {"direction": "up"},
            "timeframes": {},
        }

        state = advance_smc_state(None, analysis)
        repeated = advance_smc_state(state, analysis)

        self.assertEqual(state["status"], "bos_confirmed")
        self.assertEqual(state["direction"], "up")
        self.assertEqual(state["event_key"], "bos")
        self.assertEqual(len(state["processed_event_keys"]), 5)
        self.assertEqual(repeated["processed_event_keys"], state["processed_event_keys"])
        self.assertEqual(repeated["event_key"], "bos")

    def test_price_action_state_embeds_smc_and_lifecycle_checkpoints_change(self) -> None:
        smc_analysis = {
            "enabled": True,
            "data_status": "ready",
            "events": [{
                "key": "smc-sweep",
                "event_type": "liquidity_sweep",
                "label": "SSL SWEEP",
                "direction": "up",
                "event_ts": 900,
                "priority": 10,
            }],
            "htf_bias": {"direction": "up"},
            "timeframes": {},
        }
        price_analysis = {
            "data_status": "ready",
            "lookback": 16,
            "min_body_ratio": 0.45,
            "wick_body_ratio": 1.5,
            "timeframes": {
                "15m": {
                    "event": "inside",
                    "candle_end_ts": 900,
                    "open": 100,
                    "high": 101,
                    "low": 99,
                    "close": 100.5,
                },
            },
            "smc_analysis": smc_analysis,
        }

        state = advance_price_action_state(None, price_analysis)

        self.assertEqual(state["smc"]["status"], "liquidity_sweep")
        self.assertEqual(state["smc_event_key"], "smc-sweep")
        with TemporaryDirectory() as tmp:
            store = LaunchLifecycleStore(
                Path(tmp) / "signals.db",
                package_enabled=True,
                price_action_enabled=True,
            )
            common = {
                "checkpoint_no": None,
                "lifecycle_stage": "breakout",
                "score": 75,
                "closed_price": 100,
                "closed_oi_usd": 1_000_000,
                "funding_interval_hours": 8,
                "funds_direction": "unknown",
            }
            reasons = store._publication_reasons(  # type: ignore[attr-defined]
                {
                    **common,
                    "price_action_json": json.dumps(state),
                },
                {
                    **common,
                    "price_action_json": json.dumps({
                        "enabled": True,
                        "smc_event_key": "",
                    }),
                },  # type: ignore[arg-type]
            )

        self.assertIn("smc_changed", reasons)


if __name__ == "__main__":
    unittest.main()
