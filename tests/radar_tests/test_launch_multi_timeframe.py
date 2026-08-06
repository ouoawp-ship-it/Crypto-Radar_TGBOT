from __future__ import annotations

import inspect
import unittest

from radars.launch_warning.multi_timeframe import (
    ROLE_GROUPS,
    TIMEFRAME_INTERVAL_MS,
    analyze_multi_timeframe,
    expand_timeframe_klines,
)


def kline(
    open_time_ms: int,
    interval_ms: int,
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
        open_time_ms + interval_ms - 1,
        "1000",
    ]


def rising_rows(timeframe: str, *, count: int = 7) -> list[list[object]]:
    interval_ms = TIMEFRAME_INTERVAL_MS[timeframe]
    return [
        kline(
            index * interval_ms,
            interval_ms,
            100 + index,
            102 + index,
            99 + index,
            101.5 + index,
        )
        for index in range(count)
    ]


def falling_rows(timeframe: str, *, count: int = 7) -> list[list[object]]:
    interval_ms = TIMEFRAME_INTERVAL_MS[timeframe]
    return [
        kline(
            index * interval_ms,
            interval_ms,
            110 - index,
            111 - index,
            108 - index,
            108.5 - index,
        )
        for index in range(count)
    ]


class LaunchMultiTimeframeTests(unittest.TestCase):
    def test_expands_five_native_requests_into_all_nine_timeframes(self) -> None:
        day = TIMEFRAME_INTERVAL_MS["1d"]
        monday_offset = 4 * day
        base = {
            "5m": rising_rows("5m", count=8),
            "15m": rising_rows("15m", count=8),
            "1h": rising_rows("1h", count=16),
            "4h": rising_rows("4h", count=24),
            "1d": [
                kline(
                    monday_offset + index * day,
                    day,
                    100 + index,
                    102 + index,
                    99 + index,
                    101 + index,
                )
                for index in range(14)
            ],
        }

        result = expand_timeframe_klines(
            base,
            window_end_ms=monday_offset + 14 * day,
        )

        self.assertEqual(set(result), set(TIMEFRAME_INTERVAL_MS))
        self.assertEqual(len(result["2h"]), 8)
        self.assertEqual(len(result["8h"]), 12)
        self.assertEqual(len(result["12h"]), 8)
        self.assertEqual(len(result["1w"]), 2)

    def test_supports_all_requested_timeframes_and_role_groups(self) -> None:
        rows = {
            timeframe: rising_rows(timeframe)
            for timeframe in TIMEFRAME_INTERVAL_MS
        }
        boundary = max(
            7 * interval_ms for interval_ms in TIMEFRAME_INTERVAL_MS.values()
        )

        result = analyze_multi_timeframe(rows, window_end_ms=boundary)

        self.assertEqual(set(result["timeframes"]), set(TIMEFRAME_INTERVAL_MS))
        self.assertEqual(set(result["role_groups"]), {row[0] for row in ROLE_GROUPS})
        self.assertEqual(result["status"], "ok")

    def test_strictly_excludes_unclosed_kline(self) -> None:
        timeframe = "15m"
        interval_ms = TIMEFRAME_INTERVAL_MS[timeframe]
        rows = rising_rows(timeframe, count=6)
        rows.append(kline(6 * interval_ms, interval_ms, 500, 510, 490, 505))

        result = analyze_multi_timeframe(
            {timeframe: rows},
            window_end_ms=6 * interval_ms,
        )
        frame = result["timeframes"][timeframe]

        self.assertEqual(frame["closed_candles"], 6)
        self.assertEqual(frame["excluded_unclosed_candles"], 1)
        self.assertEqual(frame["last_closed_end_ms"], 6 * interval_ms)
        self.assertNotEqual(frame["reference_high"], 510)

    def test_clock_is_injected_when_boundary_is_not_supplied(self) -> None:
        interval_ms = TIMEFRAME_INTERVAL_MS["5m"]
        calls = 0

        def clock() -> float:
            nonlocal calls
            calls += 1
            return 6 * interval_ms / 1000

        result = analyze_multi_timeframe(
            {"5m": rising_rows("5m", count=6)},
            clock=clock,
        )

        self.assertEqual(calls, 1)
        self.assertEqual(result["window_end_ms"], 6 * interval_ms)

    def test_same_role_timeframes_contribute_only_one_vote(self) -> None:
        boundary = 7 * TIMEFRAME_INTERVAL_MS["12h"]
        result = analyze_multi_timeframe(
            {
                "12h": rising_rows("12h"),
                "8h": rising_rows("8h"),
                "4h": rising_rows("4h"),
            },
            window_end_ms=boundary,
        )

        group = result["role_groups"]["main_structure"]
        self.assertEqual(group["direction"], "bullish")
        self.assertEqual(group["vote"], 1)
        self.assertEqual(group["max_vote_contribution"], 1)
        self.assertEqual(result["vote_summary"]["net_group_vote"], 1)

    def test_conflicting_timeframes_in_one_role_do_not_vote(self) -> None:
        boundary = 7 * TIMEFRAME_INTERVAL_MS["2h"]
        result = analyze_multi_timeframe(
            {
                "2h": rising_rows("2h"),
                "1h": falling_rows("1h"),
            },
            window_end_ms=boundary,
        )

        group = result["role_groups"]["confirmation"]
        self.assertEqual(group["direction"], "mixed")
        self.assertEqual(group["vote"], 0)

    def test_rolling_24h_is_background_and_never_changes_vote(self) -> None:
        rows = {"15m": rising_rows("15m")}
        boundary = 7 * TIMEFRAME_INTERVAL_MS["15m"]
        bullish_background = analyze_multi_timeframe(
            rows,
            window_end_ms=boundary,
            rolling_24h={"price_pct": 50, "direction": "bullish"},
        )
        bearish_background = analyze_multi_timeframe(
            rows,
            window_end_ms=boundary,
            rolling_24h={"price_pct": -50, "direction": "bearish"},
        )

        self.assertEqual(
            bullish_background["vote_summary"],
            bearish_background["vote_summary"],
        )
        self.assertFalse(
            bullish_background["rolling_24h_background"]["counts_toward_vote"]
        )

    def test_reports_hh_hl_and_bos_without_identity_inference(self) -> None:
        timeframe = "1h"
        interval_ms = TIMEFRAME_INTERVAL_MS[timeframe]
        rows = rising_rows(timeframe, count=6)
        rows[-1] = kline(5 * interval_ms, interval_ms, 105, 112, 104, 111)

        result = analyze_multi_timeframe(
            {timeframe: rows},
            window_end_ms=6 * interval_ms,
        )
        frame = result["timeframes"][timeframe]

        self.assertEqual(frame["structure"]["high"], "HH")
        self.assertEqual(frame["structure"]["low"], "HL")
        self.assertEqual(frame["structure_event"], "BOS_up")
        self.assertEqual(frame["identity_inference"], "not_performed")

    def test_reports_choch_after_bearish_prior_structure_breaks_up(self) -> None:
        timeframe = "1h"
        interval_ms = TIMEFRAME_INTERVAL_MS[timeframe]
        rows = falling_rows(timeframe, count=6)
        rows[-1] = kline(5 * interval_ms, interval_ms, 105, 116, 104, 115)

        result = analyze_multi_timeframe(
            {timeframe: rows},
            window_end_ms=6 * interval_ms,
        )

        self.assertEqual(
            result["timeframes"][timeframe]["structure_event"],
            "CHoCH_up",
        )

    def test_reports_liquidity_sweep_and_fvg(self) -> None:
        timeframe = "15m"
        interval_ms = TIMEFRAME_INTERVAL_MS[timeframe]
        sweep_rows = [
            kline(index * interval_ms, interval_ms, 100, 102, 99, 101)
            for index in range(5)
        ]
        sweep_rows.append(kline(5 * interval_ms, interval_ms, 101, 105, 100, 101.5))
        fvg_rows = [
            kline(0, interval_ms, 100, 101, 99, 100),
            kline(interval_ms, interval_ms, 101, 105, 100, 104),
            kline(2 * interval_ms, interval_ms, 104, 106, 102, 105),
            kline(3 * interval_ms, interval_ms, 105, 107, 103, 106),
            kline(4 * interval_ms, interval_ms, 106, 108, 104, 107),
            kline(5 * interval_ms, interval_ms, 107, 109, 105, 108),
        ]

        sweep = analyze_multi_timeframe(
            {timeframe: sweep_rows},
            window_end_ms=6 * interval_ms,
        )
        fvg = analyze_multi_timeframe(
            {timeframe: fvg_rows},
            window_end_ms=6 * interval_ms,
        )

        self.assertEqual(
            sweep["timeframes"][timeframe]["liquidity_sweep"],
            "high",
        )
        self.assertEqual(fvg["timeframes"][timeframe]["fvg"]["status"], "bullish")

    def test_insufficient_history_degrades_explicitly_and_cannot_vote(self) -> None:
        timeframe = "5m"
        interval_ms = TIMEFRAME_INTERVAL_MS[timeframe]
        result = analyze_multi_timeframe(
            {timeframe: rising_rows(timeframe, count=5)},
            window_end_ms=5 * interval_ms,
        )
        frame = result["timeframes"][timeframe]

        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(frame["data_status"], "insufficient_history")
        self.assertEqual(frame["vote"], 0)
        self.assertEqual(
            result["role_groups"]["entry"]["data_status"],
            "unavailable",
        )

    def test_malformed_extra_row_is_safely_degraded(self) -> None:
        timeframe = "5m"
        interval_ms = TIMEFRAME_INTERVAL_MS[timeframe]
        rows: list[object] = list(rising_rows(timeframe, count=6))
        rows.append({"not": "a Binance kline"})

        result = analyze_multi_timeframe(  # type: ignore[arg-type]
            {timeframe: rows},
            window_end_ms=6 * interval_ms,
        )

        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["timeframes"][timeframe]["data_status"], "degraded")
        self.assertEqual(result["timeframes"][timeframe]["invalid_rows"], 1)
        self.assertEqual(result["role_groups"]["entry"]["data_status"], "degraded")

    def test_module_has_no_network_client_dependency(self) -> None:
        source = inspect.getsource(analyze_multi_timeframe)
        self.assertNotIn("requests", source)
        self.assertNotIn("urllib", source)
        self.assertNotIn("http", source.lower())


if __name__ == "__main__":
    unittest.main()
