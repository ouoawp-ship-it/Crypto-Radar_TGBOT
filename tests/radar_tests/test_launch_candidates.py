from __future__ import annotations

import unittest

from radars.launch_warning.candidates import select_launch_candidates


def _candidate(symbol: str, tier: str, quote_volume: float) -> dict[str, object]:
    return {
        "symbol": symbol,
        "liquidity_tier": tier,
        "quote_volume": quote_volume,
    }


class LaunchCandidateSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.candidates = [
            _candidate("H1USDT", "high", 500_000_000),
            _candidate("H2USDT", "high", 400_000_000),
            _candidate("H3USDT", "high", 300_000_000),
            _candidate("M1USDT", "medium", 80_000_000),
            _candidate("M2USDT", "medium", 60_000_000),
            _candidate("L1USDT", "low", 10_000_000),
            _candidate("L2USDT", "low", 8_000_000),
        ]

    def test_active_lifecycle_symbols_are_selected_first(self) -> None:
        result = select_launch_candidates(
            self.candidates,
            active_symbols=["L2USDT", "M2USDT"],
            limit=4,
            closed_window_end_ts=900,
        )

        self.assertEqual(
            [item["symbol"] for item in result["selected"][:2]],
            ["L2USDT", "M2USDT"],
        )
        self.assertEqual(result["stats"]["active_selected"], 2)
        self.assertEqual(result["stats"]["selected_count"], 4)

    def test_limit_and_deduplication_are_strict(self) -> None:
        result = select_launch_candidates(
            [*self.candidates, _candidate("h1usdt", "low", 1)],
            active_symbols=["H1USDT", "H1USDT"],
            limit=3,
            closed_window_end_ts=1_800,
        )

        symbols = [item["symbol"] for item in result["selected"]]
        self.assertEqual(len(symbols), 3)
        self.assertEqual(len(set(symbols)), 3)
        self.assertEqual(result["stats"]["duplicate_candidates_removed"], 1)

    def test_same_closed_window_is_deterministic_across_input_order(self) -> None:
        first = select_launch_candidates(
            self.candidates,
            active_symbols=[],
            limit=5,
            closed_window_end_ts=3_600,
        )
        second = select_launch_candidates(
            list(reversed(self.candidates)),
            active_symbols=[],
            limit=5,
            closed_window_end_ts=3_600,
        )

        self.assertEqual(
            [item["symbol"] for item in first["selected"]],
            [item["symbol"] for item in second["selected"]],
        )
        self.assertEqual(first["stats"], second["stats"])

    def test_low_liquidity_pool_is_not_starved(self) -> None:
        observed: set[str] = set()
        for window_index in range(10):
            result = select_launch_candidates(
                self.candidates,
                active_symbols=[],
                limit=1,
                closed_window_end_ts=(window_index + 1) * 900,
            )
            observed.add(str(result["selected"][0]["liquidity_tier"]))

        self.assertIn("low", observed)
        self.assertIn("medium", observed)
        self.assertIn("high", observed)

    def test_every_low_liquidity_candidate_eventually_rotates_in(self) -> None:
        candidates = [
            _candidate("HIGHUSDT", "high", 500_000_000),
            _candidate("MEDIUMUSDT", "medium", 50_000_000),
            *[
                _candidate(f"LOW{index}USDT", "low", 10_000_000 - index)
                for index in range(10)
            ],
        ]
        observed_low: set[str] = set()
        for window_index in range(50):
            result = select_launch_candidates(
                candidates,
                active_symbols=[],
                limit=1,
                closed_window_end_ts=(window_index + 1) * 900,
            )
            selected = result["selected"][0]
            if selected["liquidity_tier"] == "low":
                observed_low.add(str(selected["symbol"]))

        self.assertEqual(len(observed_low), 10)

    def test_rotation_moves_with_closed_window(self) -> None:
        selected = []
        for window_index in range(3):
            result = select_launch_candidates(
                self.candidates,
                active_symbols=[],
                limit=4,
                closed_window_end_ts=(window_index + 1) * 900,
            )
            selected.append(tuple(item["symbol"] for item in result["selected"]))

        self.assertGreater(len(set(selected)), 1)

    def test_module_does_not_apply_an_extra_eligibility_threshold(self) -> None:
        result = select_launch_candidates(
            [_candidate("TINYUSDT", "low", 1)],
            active_symbols=[],
            limit=1,
            closed_window_end_ts=900,
        )

        self.assertEqual(result["selected"][0]["symbol"], "TINYUSDT")

    def test_nonfinite_volume_does_not_break_deterministic_sorting(self) -> None:
        candidates = [
            _candidate("MISSINGUSDT", "high", float("nan")),
            _candidate("REALUSDT", "high", 1_000_000),
        ]

        result = select_launch_candidates(
            candidates,
            active_symbols=[],
            limit=2,
            closed_window_end_ts=900,
        )

        self.assertEqual(
            [item["symbol"] for item in result["selected"]],
            ["REALUSDT", "MISSINGUSDT"],
        )

    def test_zero_limit_returns_safe_empty_statistics(self) -> None:
        result = select_launch_candidates(
            self.candidates,
            active_symbols=[],
            limit=0,
            closed_window_end_ts=900,
        )

        self.assertEqual(result["selected"], [])
        self.assertEqual(result["stats"]["status"], "disabled")
        self.assertEqual(result["stats"]["selected_count"], 0)


if __name__ == "__main__":
    unittest.main()
