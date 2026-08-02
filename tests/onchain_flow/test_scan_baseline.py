from __future__ import annotations

import unittest
from decimal import Decimal

from paopao_radar.onchain_flow.scan_baseline import (
    HistoricalScanBaseline,
    scan_metrics,
)


class HistoricalScanBaselineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.analyzer = HistoricalScanBaseline(
            min_samples=4,
            max_samples=8,
            mad_multiplier=Decimal("3.5"),
        )

    @staticmethod
    def metrics(value: int) -> dict[str, object]:
        return {
            "transfer_count": value,
            "total_token_amount": str(value * 100),
            "unique_senders": value,
            "unique_receivers": value,
            "behavior_score": value,
            "max_wallet_group_score": value,
        }

    def test_cold_start_never_claims_anomaly(self) -> None:
        result = self.analyzer.analyze(
            self.metrics(100),
            [self.metrics(1), self.metrics(2), self.metrics(3)],
        )
        self.assertEqual(result["status"], "cold_start")
        self.assertFalse(result["anomaly"])
        self.assertEqual(result["sample_count"], 3)

    def test_flat_history_detects_strict_increase(self) -> None:
        result = self.analyzer.analyze(
            self.metrics(20), [self.metrics(10) for _ in range(4)]
        )
        self.assertEqual(result["status"], "ready")
        self.assertTrue(result["anomaly"])
        self.assertIn("transfer_count", result["anomalous_metrics"])
        metric = result["metrics"]["transfer_count"]
        self.assertTrue(metric["flat_baseline"])
        self.assertIsNone(metric["robust_mad_ratio"])

    def test_stable_observation_is_not_anomalous(self) -> None:
        result = self.analyzer.analyze(
            self.metrics(12),
            [self.metrics(value) for value in (9, 10, 10, 11, 11, 12)],
        )
        self.assertFalse(result["anomaly"])

    def test_history_is_bounded_to_latest_samples(self) -> None:
        result = self.analyzer.analyze(
            self.metrics(10),
            [self.metrics(1000), *[self.metrics(10) for _ in range(8)]],
        )
        self.assertEqual(result["sample_count"], 8)
        self.assertFalse(result["anomaly"])

    def test_scan_metrics_sanitizes_invalid_values(self) -> None:
        result = scan_metrics(
            {
                "transfer_count": 2,
                "total_token_amount": "not-a-number",
                "unique_senders": 1,
                "unique_receivers": 2,
            },
            behavior_score=5,
            max_wallet_group_score=6,
        )
        self.assertEqual(result["total_token_amount"], "0")
        self.assertEqual(result["transfer_count"], 2)


if __name__ == "__main__":
    unittest.main()
