from __future__ import annotations

import copy
import unittest

from radars.launch_warning.smc_overlay import build_smc_overlay


HOUR = 60 * 60
START = 1_800_000_000


def candle(
    index: int,
    *,
    open_price: float = 100.0,
    high: float = 101.0,
    low: float = 99.0,
    close: float = 100.0,
) -> dict[str, float | int]:
    return {
        "close_ts": START + index * HOUR,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
    }


class LaunchSmcOverlayTests(unittest.TestCase):
    def test_initial_high_candidate_does_not_change_zero_leg(self) -> None:
        rows = [candle(index) for index in range(6)]
        rows[0] = candle(0, high=110.0)

        result = build_smc_overlay(rows)

        self.assertEqual(result["pivots"], [])

    def test_pivot_uses_delayed_leg_confirmation_without_left_window(self) -> None:
        rows = [candle(index) for index in range(72)]
        rows[0] = candle(0, low=90.0)

        before_confirmation = build_smc_overlay(rows[:5])
        at_confirmation = build_smc_overlay(rows[:6])
        complete = build_smc_overlay(rows)

        self.assertEqual(before_confirmation["pivots"], [])
        internal = next(
            pivot
            for pivot in at_confirmation["pivots"]
            if pivot["kind"] == "internal" and pivot["side"] == "low"
        )
        swing = next(
            pivot
            for pivot in complete["pivots"]
            if pivot["kind"] == "swing" and pivot["side"] == "low"
        )
        self.assertEqual(internal["origin_ts"], rows[0]["close_ts"])
        self.assertEqual(internal["confirmed_at_ts"], rows[5]["close_ts"])
        self.assertEqual(swing["origin_ts"], rows[0]["close_ts"])
        self.assertEqual(swing["confirmed_at_ts"], rows[20]["close_ts"])

    def test_future_bars_do_not_revoke_an_already_confirmed_pivot(self) -> None:
        prefix = [candle(index) for index in range(6)]
        prefix[0] = candle(0, low=90.0)
        future = [candle(index, low=50.0 if index == 6 else 99.0) for index in range(6, 20)]

        short = build_smc_overlay(prefix)
        long = build_smc_overlay(prefix + future)

        short_pivot = short["pivots"][0]
        matching = next(
            pivot
            for pivot in long["pivots"]
            if pivot["kind"] == short_pivot["kind"]
            and pivot["side"] == short_pivot["side"]
            and pivot["origin_ts"] == short_pivot["origin_ts"]
        )
        self.assertEqual(
            matching["confirmed_at_ts"],
            short_pivot["confirmed_at_ts"],
        )

    def test_structure_break_requires_close_and_classifies_direction_turn(self) -> None:
        rows = [candle(index) for index in range(72)]
        rows[0] = candle(0, low=90.0)
        rows[6] = candle(6, open_price=100.0, high=112.0, low=88.0, close=89.0)
        rows[12] = candle(12, open_price=100.0, high=114.0, low=99.0, close=113.0)

        wick_only = copy.deepcopy(rows)
        wick_only[6] = candle(6, high=112.0, low=88.0, close=91.0)
        wick_result = build_smc_overlay(wick_only)
        result = build_smc_overlay(rows)

        self.assertFalse(any(
            event["direction"] == "bearish"
            and event["broken_at_ts"] == rows[6]["close_ts"]
            for event in wick_result["structure_events"]
        ))
        internal = [
            event for event in result["structure_events"]
            if event["kind"] == "internal"
        ]
        self.assertEqual(
            [(event["direction"], event["event"]) for event in internal[:2]],
            [("bearish", "continuation"), ("bullish", "structure_turn")],
        )
        self.assertEqual(internal[0]["origin_ts"], rows[0]["close_ts"])
        self.assertEqual(internal[0]["confirmed_at_ts"], rows[5]["close_ts"])
        self.assertEqual(internal[0]["broken_at_ts"], rows[6]["close_ts"])

    def test_order_block_uses_atr_parsed_extreme_and_keeps_only_active(self) -> None:
        rows = [
            candle(index, low=99.0 - index * 0.05)
            for index in range(50)
        ]
        rows[0] = candle(0, low=90.0)
        rows[6] = candle(6, high=110.0, low=98.7)
        rows[30] = candle(30, high=109.0, low=50.0)
        rows[40] = candle(
            40,
            open_price=100.0,
            high=112.0,
            low=96.0,
            close=111.0,
        )
        for index in range(41, len(rows)):
            rows[index] = candle(index, low=98.5)

        result = build_smc_overlay(rows)
        block = next(
            block
            for block in result["active_order_blocks"]
            if block["direction"] == "bullish"
        )

        self.assertEqual(block["origin_ts"], rows[39]["close_ts"])
        self.assertNotEqual(block["origin_ts"], rows[30]["close_ts"])
        self.assertEqual(block["zone_low"], rows[39]["low"])
        invalidated_rows = copy.deepcopy(rows)
        invalidated_rows[41] = candle(41, high=101.0, low=90.0, close=100.0)
        invalidated = build_smc_overlay(invalidated_rows)
        self.assertFalse(any(
            candidate["direction"] == "bullish"
            and candidate["origin_ts"] == rows[39]["close_ts"]
            for candidate in invalidated["active_order_blocks"]
        ))

    def test_bearish_order_block_is_invalidated_by_high_not_close(self) -> None:
        rows = [candle(index) for index in range(50)]
        rows[0] = candle(0, low=90.0)
        rows[6] = candle(6, high=110.0)
        rows[12] = candle(12, low=90.0)
        for index in range(13, 40):
            rows[index] = candle(index, high=101.0 + (index - 12) * 0.05)
        rows[30] = candle(30, high=109.0, low=91.0)
        rows[40] = candle(
            40,
            open_price=100.0,
            high=103.0,
            low=88.0,
            close=89.0,
        )
        for index in range(41, len(rows)):
            rows[index] = candle(index, high=102.0)

        result = build_smc_overlay(rows)
        block = next(
            block
            for block in result["active_order_blocks"]
            if block["direction"] == "bearish"
        )
        self.assertEqual(block["origin_ts"], rows[39]["close_ts"])
        invalidated_rows = copy.deepcopy(rows)
        invalidated_rows[41] = candle(41, high=120.0, low=99.0, close=100.0)

        invalidated = build_smc_overlay(invalidated_rows)

        self.assertFalse(any(
            candidate["direction"] == "bearish"
            and candidate["origin_ts"] == rows[39]["close_ts"]
            for candidate in invalidated["active_order_blocks"]
        ))

    def test_valuation_uses_latest_72_candles_and_narrow_context_bands(self) -> None:
        rows = [
            candle(index, high=110.0 + index, low=90.0 - index * 0.5)
            for index in range(80)
        ]

        result = build_smc_overlay(rows)
        valuation = result["valuation"]

        self.assertEqual(result["status"], "ready")
        self.assertEqual(valuation["data_status"], "complete")
        self.assertEqual(valuation["window_bars"], 72)
        self.assertEqual(valuation["start_ts"], rows[8]["close_ts"])
        self.assertEqual(valuation["end_ts"], rows[-1]["close_ts"])
        self.assertEqual(valuation["range_low"], rows[-1]["low"])
        self.assertEqual(valuation["range_high"], rows[-1]["high"])
        self.assertEqual(
            valuation["zones"]["low"]["high"],
            valuation["range_low"]
            + (valuation["range_high"] - valuation["range_low"]) * 0.05,
        )
        self.assertEqual(
            valuation["zones"]["mid"]["low"],
            valuation["range_low"]
            + (valuation["range_high"] - valuation["range_low"]) * 0.475,
        )
        self.assertEqual(
            valuation["zones"]["mid"]["high"],
            valuation["range_low"]
            + (valuation["range_high"] - valuation["range_low"]) * 0.525,
        )
        self.assertEqual(
            valuation["zones"]["high"]["low"],
            valuation["range_low"]
            + (valuation["range_high"] - valuation["range_low"]) * 0.95,
        )

    def test_output_is_deterministic_and_input_is_not_mutated(self) -> None:
        rows = [candle(index) for index in range(72)]
        original = copy.deepcopy(rows)

        first = build_smc_overlay(rows)
        second = build_smc_overlay(rows)

        self.assertEqual(first, second)
        self.assertEqual(rows, original)

    def test_accepts_closed_market_session_gaps_and_reports_them(self) -> None:
        rows = [candle(0), candle(8), candle(32)]

        result = build_smc_overlay(rows, allow_session_gaps=True)

        self.assertEqual(result["continuity"], {
            "session_gap_count": 2,
            "missing_session_hours": 30,
            "largest_gap_hours": 23,
        })

    def test_default_policy_rejects_gap_and_session_policy_stays_bounded(self) -> None:
        with self.assertRaisesRegex(ValueError, "candle_gap"):
            build_smc_overlay([candle(0), candle(8)])
        with self.assertRaisesRegex(ValueError, "candle_gap"):
            build_smc_overlay(
                [candle(0), candle(2)],
                allow_session_gaps=True,
            )
        with self.assertRaisesRegex(ValueError, "candle_gap"):
            build_smc_overlay(
                [candle(0), candle(168)],
                allow_session_gaps=True,
            )

    def test_rejects_non_hourly_timestamp_cadence(self) -> None:
        rows = [candle(0), candle(1)]
        rows[1]["close_ts"] = int(rows[1]["close_ts"]) + 60

        with self.assertRaisesRegex(ValueError, "cadence_invalid"):
            build_smc_overlay(rows)


if __name__ == "__main__":
    unittest.main()
