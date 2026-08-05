from __future__ import annotations

import unittest

from radars.launch_warning.market_facts import (
    ERROR_BOUNDARY_MISMATCH,
    ERROR_INSUFFICIENT_HISTORY,
    ERROR_KLINE_DUPLICATE,
    ERROR_KLINE_GAP,
    ERROR_KLINE_MALFORMED,
    ERROR_OI_DUPLICATE,
    ERROR_OI_GAP,
    ERROR_OI_MALFORMED,
    INTERVAL_MS,
    OI_24H_REQUIRED_POINTS,
    build_launch_market_facts,
    closed_kline_active_flow,
    closed_24h_open_interest_change,
    normalize_binance_15m_klines,
    normalize_binance_15m_open_interest,
    price_oi_quadrant,
)


def kline(index: int, *, close: float | None = None, volume: float = 100.0) -> list[object]:
    open_time_ms = index * INTERVAL_MS
    close_price = float(100 + index if close is None else close)
    return [
        open_time_ms,
        str(close_price - 0.5),
        str(close_price + 1.0),
        str(close_price - 1.0),
        str(close_price),
        "0",
        open_time_ms + INTERVAL_MS - 1,
        str(volume),
    ]


def oi(index: int, *, value: float | None = None) -> dict[str, object]:
    return {
        "timestamp": (index + 1) * INTERVAL_MS,
        "sumOpenInterestValue": str(1_000 + index * 10 if value is None else value),
    }


class LaunchMarketFactsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.window_end_ms = 17 * INTERVAL_MS
        self.klines = [kline(index) for index in range(17)]
        self.oi_rows = [oi(index) for index in range(17)]

    def build(self, **changes: object) -> dict[str, object]:
        return build_launch_market_facts(
            changes.get("klines", self.klines),  # type: ignore[arg-type]
            changes.get("oi_rows", self.oi_rows),  # type: ignore[arg-type]
            window_end_ms=int(changes.get("window_end_ms", self.window_end_ms)),
            ticker_24h=changes.get("ticker_24h"),  # type: ignore[arg-type]
            oi_24h_rows=changes.get("oi_24h_rows"),  # type: ignore[arg-type]
        )

    def test_normalizes_unsorted_rows_and_derives_aligned_facts(self) -> None:
        klines = list(reversed(self.klines))
        oi_rows = list(reversed(self.oi_rows))
        klines[0] = kline(16, volume=400.0)

        result = self.build(
            klines=klines,
            oi_rows=oi_rows,
            ticker_24h={"priceChangePercent": "12.5"},
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["aligned_points"], 17)
        self.assertEqual(result["closed_price"], 116.0)
        self.assertAlmostEqual(result["price_15m_pct"], (116 / 115 - 1) * 100)
        self.assertAlmostEqual(result["price_1h_pct"], (116 / 112 - 1) * 100)
        self.assertAlmostEqual(result["price_4h_pct"], 16.0)
        self.assertAlmostEqual(result["oi_15m_pct"], (1160 / 1150 - 1) * 100)
        self.assertAlmostEqual(result["oi_1h_pct"], (1160 / 1120 - 1) * 100)
        self.assertAlmostEqual(result["oi_4h_pct"], 16.0)
        self.assertAlmostEqual(result["volume_ratio_15m"], 4.0)
        self.assertGreater(result["recent_volatility_pct"], 0.0)
        self.assertEqual(result["price_24h_rolling_pct"], 12.5)
        self.assertEqual(
            result["price_24h_semantics"],
            "rolling_24h_not_closed_window",
        )
        self.assertEqual(result["quadrants"]["1h"]["key"], "price_up_oi_up")  # type: ignore[index]

    def test_public_normalizers_return_sorted_closed_boundaries(self) -> None:
        klines = normalize_binance_15m_klines(
            list(reversed(self.klines)),
            window_end_ms=self.window_end_ms,
        )
        oi_rows = normalize_binance_15m_open_interest(
            list(reversed(self.oi_rows)),
            window_end_ms=self.window_end_ms,
        )

        self.assertEqual(klines[0].period_end_ms, INTERVAL_MS)
        self.assertEqual(klines[-1].period_end_ms, self.window_end_ms)
        self.assertEqual(oi_rows[0].period_end_ms, INTERVAL_MS)
        self.assertEqual(oi_rows[-1].period_end_ms, self.window_end_ms)

    def test_missing_rolling_ticker_is_none_not_zero(self) -> None:
        result = self.build()

        self.assertIsNone(result["price_24h_rolling_pct"])
        self.assertEqual(result["status"], "ok")

    def test_strict_24h_oi_uses_97_continuous_closed_points(self) -> None:
        window_end_ms = OI_24H_REQUIRED_POINTS * INTERVAL_MS
        recent_klines = [kline(index) for index in range(80, 97)]
        recent_oi = [oi(index) for index in range(80, 97)]
        full_oi = [oi(index) for index in range(OI_24H_REQUIRED_POINTS)]

        result = self.build(
            klines=recent_klines,
            oi_rows=recent_oi,
            oi_24h_rows=full_oi,
            window_end_ms=window_end_ms,
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["oi_24h_status"], "ok")
        self.assertEqual(result["oi_24h_points"], OI_24H_REQUIRED_POINTS)
        self.assertAlmostEqual(result["oi_24h_closed_pct"], 96.0)
        self.assertEqual(
            result["oi_24h_semantics"],
            "closed_15m_boundaries_24h",
        )

    def test_short_24h_history_does_not_invalidate_recent_core_window(self) -> None:
        window_end_ms = OI_24H_REQUIRED_POINTS * INTERVAL_MS
        result = self.build(
            klines=[kline(index) for index in range(80, 97)],
            oi_rows=[oi(index) for index in range(80, 97)],
            oi_24h_rows=[oi(index) for index in range(1, 97)],
            window_end_ms=window_end_ms,
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["oi_24h_status"], "insufficient_history")
        self.assertIsNone(result["oi_24h_closed_pct"])

    def test_gap_in_24h_background_does_not_invalidate_recent_core_window(self) -> None:
        window_end_ms = 98 * INTERVAL_MS
        full_oi = [oi(index) for index in range(98) if index != 20]
        result = self.build(
            klines=[kline(index) for index in range(81, 98)],
            oi_rows=[oi(index) for index in range(81, 98)],
            oi_24h_rows=full_oi,
            window_end_ms=window_end_ms,
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["oi_24h_status"], "gap")
        self.assertIsNone(result["oi_24h_closed_pct"])

    def test_closed_24h_oi_requires_the_current_closed_boundary(self) -> None:
        result = closed_24h_open_interest_change(
            [oi(index) for index in range(OI_24H_REQUIRED_POINTS)],
            window_end_ms=(OI_24H_REQUIRED_POINTS + 1) * INTERVAL_MS,
        )

        self.assertEqual(result["status"], "boundary_missing")
        self.assertIsNone(result["value_pct"])

    def test_closed_kline_active_flow_uses_exact_window_and_never_fills_zero(self) -> None:
        row = [
            16 * INTERVAL_MS,
            "100",
            "101",
            "99",
            "100",
            "10",
            17 * INTERVAL_MS - 1,
            "1000",
            20,
            "7",
            "700",
            "0",
        ]

        available = closed_kline_active_flow(
            row,
            window_end_ms=17 * INTERVAL_MS,
        )
        stale = closed_kline_active_flow(
            row,
            window_end_ms=18 * INTERVAL_MS,
        )

        self.assertEqual(available["status"], "available")
        self.assertEqual(available["net_usd"], 400.0)
        self.assertEqual(available["ratio"], 0.4)
        self.assertEqual(stale["status"], "window_incomplete")
        self.assertIsNone(stale["net_usd"])

    def test_zero_reference_volume_is_none_not_zero(self) -> None:
        rows = [kline(index, volume=0.0) for index in range(17)]

        result = self.build(klines=rows)

        self.assertEqual(result["status"], "ok")
        self.assertIsNone(result["volume_ratio_15m"])

    def test_rejects_insufficient_history(self) -> None:
        result = self.build(klines=self.klines[-16:], oi_rows=self.oi_rows[-16:])

        self.assertEqual(result["error"], ERROR_INSUFFICIENT_HISTORY)
        self.assertIsNone(result["price_15m_pct"])
        self.assertIsNone(result["recent_volatility_pct"])

    def test_rejects_duplicate_kline_boundary(self) -> None:
        result = self.build(klines=[*self.klines, self.klines[-1]])

        self.assertEqual(result["error"], ERROR_KLINE_DUPLICATE)

    def test_rejects_duplicate_oi_boundary(self) -> None:
        result = self.build(oi_rows=[*self.oi_rows, self.oi_rows[-1]])

        self.assertEqual(result["error"], ERROR_OI_DUPLICATE)

    def test_rejects_kline_gap_in_recent_window(self) -> None:
        result = self.build(klines=[*self.klines[:8], *self.klines[9:]])

        self.assertEqual(result["error"], ERROR_INSUFFICIENT_HISTORY)
        extended = [
            *[kline(index) for index in range(8)],
            *[kline(index) for index in range(9, 18)],
        ]
        self.assertEqual(
            self.build(
                klines=extended,
                oi_rows=[oi(index + 1) for index in range(17)],
                window_end_ms=18 * INTERVAL_MS,
            )["error"],
            ERROR_KLINE_GAP,
        )

    def test_rejects_oi_gap_in_recent_window(self) -> None:
        rows = [
            *[oi(index) for index in range(8)],
            *[oi(index) for index in range(9, 18)],
        ]
        self.assertEqual(
            self.build(
                klines=[kline(index + 1) for index in range(17)],
                oi_rows=rows,
                window_end_ms=18 * INTERVAL_MS,
            )["error"],
            ERROR_OI_GAP,
        )

    def test_rejects_unclosed_or_wrong_latest_boundary(self) -> None:
        future = [*self.klines, kline(17)]
        self.assertEqual(self.build(klines=future)["error"], ERROR_BOUNDARY_MISMATCH)
        old = self.build(
            klines=[kline(index) for index in range(17)],
            oi_rows=[oi(index) for index in range(16)],
            window_end_ms=18 * INTERVAL_MS,
        )
        self.assertEqual(old["error"], ERROR_BOUNDARY_MISMATCH)

    def test_rejects_malformed_kline_and_oi(self) -> None:
        malformed_kline = [*self.klines]
        malformed_kline[-1] = [self.klines[-1][0], "bad"]
        malformed_oi = [*self.oi_rows]
        malformed_oi[-1] = {
            "timestamp": self.window_end_ms,
            "sumOpenInterestValue": "nan",
        }

        self.assertEqual(self.build(klines=malformed_kline)["error"], ERROR_KLINE_MALFORMED)
        self.assertEqual(self.build(oi_rows=malformed_oi)["error"], ERROR_OI_MALFORMED)

    def test_rejects_kline_oi_boundary_misalignment(self) -> None:
        oi_rows = [
            {
                "timestamp": (index + 2) * INTERVAL_MS,
                "sumOpenInterestValue": str(1_000 + index * 10),
            }
            for index in range(17)
        ]
        result = self.build(
            klines=[kline(index + 1) for index in range(17)],
            oi_rows=oi_rows,
            window_end_ms=18 * INTERVAL_MS,
        )
        self.assertEqual(result["status"], "ok")

        oi_rows[0]["timestamp"] = INTERVAL_MS
        result = self.build(
            klines=[kline(index + 1) for index in range(17)],
            oi_rows=oi_rows,
            window_end_ms=18 * INTERVAL_MS,
        )
        self.assertEqual(result["error"], ERROR_OI_GAP)

        self.assertEqual(result["status"], "invalid")

    def test_four_quadrants_use_cautious_chinese_semantics(self) -> None:
        cases = {
            (1.0, 1.0): ("price_up_oi_up", False),
            (1.0, -1.0): ("price_up_oi_down", True),
            (-1.0, 1.0): ("price_down_oi_up", True),
            (-1.0, -1.0): ("price_down_oi_down", True),
            (0.0, 1.0): ("neutral", False),
        }
        for changes, expected in cases.items():
            with self.subTest(changes=changes):
                result = price_oi_quadrant(*changes)
                self.assertEqual(result["key"], expected[0])
                self.assertEqual(result["counter_evidence"], expected[1])
                self.assertNotIn("必然", str(result["meaning"]))

        missing = price_oi_quadrant(None, 1.0)
        self.assertEqual(missing["key"], "insufficient_data")


if __name__ == "__main__":
    unittest.main()
