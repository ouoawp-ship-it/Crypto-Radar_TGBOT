from __future__ import annotations

import copy
import unittest
from unittest.mock import patch

from radars.launch_warning.smc_filter import (
    closed_candles_from_binance,
    evaluate_smc_filter,
)
from radars.launch_warning.smc_overlay import build_smc_overlay


def _candle(close: float = 100.0) -> dict[str, float | int]:
    return {
        "close_ts": 1_000_000,
        "open": close - 0.5,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
    }


def _overlay(
    direction: str = "neutral",
    *,
    status: str = "ready",
    gap_count: int = 0,
    opposing_block: bool = False,
) -> dict[str, object]:
    events: list[dict[str, object]] = []
    if direction in {"bullish", "bearish"}:
        events.append({
            "kind": "swing",
            "direction": direction,
            "event": "continuation",
            "level": 99.0,
            "confirmed_at_ts": 950_000,
            "broken_at_ts": 960_000,
        })
    blocks: list[dict[str, object]] = []
    if opposing_block:
        blocks.append({
            "kind": "swing",
            "side": "supply",
            "zone_low": 99.0,
            "zone_high": 101.0,
        })
    return {
        "status": status,
        "continuity": {"session_gap_count": gap_count},
        "structure_events": events,
        "active_order_blocks": blocks,
        "valuation": {"end_ts": 1_000_000},
    }


class LaunchSmcFilterTests(unittest.TestCase):
    def _evaluate(
        self,
        one_hour: dict[str, object],
        four_hour: dict[str, object],
        *,
        direction: str = "bullish",
    ) -> dict[str, object]:
        with patch(
            "radars.launch_warning.smc_filter.build_smc_overlay",
            side_effect=[one_hour, four_hour],
        ):
            return evaluate_smc_filter(
                signal={"direction": direction, "bullish_readiness": 82},
                one_hour_candles=[_candle()],
                four_hour_candles=[_candle()],
            )

    def test_both_aligned_are_supportive_and_ai_eligible(self) -> None:
        result = self._evaluate(_overlay("bullish"), _overlay("bullish"))
        self.assertEqual(result["status"], "supportive")
        self.assertFalse(result["blocks_publication"])
        self.assertTrue(result["ai_eligible"])
        self.assertEqual(result["score_adjustment"], 0)

    def test_one_aligned_and_one_neutral_is_supportive(self) -> None:
        result = self._evaluate(_overlay("bullish"), _overlay())
        self.assertEqual(result["status"], "supportive")

    def test_both_opposed_are_conflicting(self) -> None:
        result = self._evaluate(_overlay("bearish"), _overlay("bearish"))
        self.assertEqual(result["status"], "conflicting")
        self.assertTrue(result["blocks_publication"])
        self.assertFalse(result["ai_eligible"])

    def test_mixed_or_single_opposition_is_neutral(self) -> None:
        mixed = self._evaluate(_overlay("bullish"), _overlay("bearish"))
        single = self._evaluate(_overlay("bearish"), _overlay())
        self.assertEqual(mixed["status"], "neutral")
        self.assertEqual(single["status"], "neutral")
        self.assertFalse(mixed["blocks_publication"])
        self.assertFalse(mixed["ai_eligible"])

    def test_opposing_zone_is_counter_evidence_not_a_hard_block(self) -> None:
        result = self._evaluate(
            _overlay("bullish", opposing_block=True),
            _overlay("bullish"),
        )
        self.assertEqual(result["status"], "neutral")
        self.assertFalse(result["blocks_publication"])

    def test_incomplete_or_gapped_frame_fails_open(self) -> None:
        incomplete = self._evaluate(
            _overlay("bullish", status="insufficient_history"),
            _overlay("bullish"),
        )
        gapped = self._evaluate(
            _overlay("bullish", gap_count=1),
            _overlay("bullish"),
        )
        for result in (incomplete, gapped):
            self.assertEqual(result["status"], "insufficient")
            self.assertFalse(result["blocks_publication"])
            self.assertFalse(result["ai_eligible"])

    def test_filter_does_not_mutate_directional_score(self) -> None:
        signal = {"direction": "bearish", "bearish_readiness": 87}
        original = copy.deepcopy(signal)
        with patch(
            "radars.launch_warning.smc_filter.build_smc_overlay",
            side_effect=[_overlay("bearish"), _overlay("bearish")],
        ):
            evaluate_smc_filter(
                signal=signal,
                one_hour_candles=[_candle()],
                four_hour_candles=[_candle()],
            )
        self.assertEqual(signal, original)

    def test_binance_converter_excludes_unclosed_row(self) -> None:
        rows = [
            [0, "99", "101", "98", "100", "1", 3_599_999],
            [3_600_000, "100", "102", "99", "101", "1", 7_199_999],
        ]
        result = closed_candles_from_binance(
            rows,
            window_end_ms=7_199_999,
            interval_ms=3_600_000,
        )
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["close_ts"], 3_600)

    def test_missing_last_closed_boundary_is_insufficient(self) -> None:
        one_hour = [_candle()]
        four_hour = [_candle()]
        with patch(
            "radars.launch_warning.smc_filter.build_smc_overlay",
        ) as builder:
            result = evaluate_smc_filter(
                signal={"direction": "bullish"},
                one_hour_candles=one_hour,
                four_hour_candles=four_hour,
                window_end_ms=7_200_000,
            )

        self.assertEqual(result["status"], "insufficient")
        self.assertIn("last_closed_window_missing", result["reasons"][0])
        self.assertFalse(result["blocks_publication"])
        builder.assert_not_called()

    def test_overlay_accepts_strict_four_hour_cadence(self) -> None:
        rows = []
        for index in range(72):
            close = 100.0 + index * 0.1
            rows.append({
                "close_ts": (index + 1) * 4 * 3_600,
                "open": close - 0.05,
                "high": close + 0.2,
                "low": close - 0.2,
                "close": close,
            })
        result = build_smc_overlay(
            rows,
            timeframe="4h",
            interval_sec=4 * 3_600,
        )
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["timeframe"], "4h")


if __name__ == "__main__":
    unittest.main()
