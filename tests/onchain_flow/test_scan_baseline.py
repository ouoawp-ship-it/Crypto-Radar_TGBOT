from __future__ import annotations

import unittest
from decimal import Decimal

from paopao_radar.onchain_flow.scan_baseline import (
    HistoricalScanBaseline,
    analyze_nested_windows,
    build_rolling_observation,
    nested_window_metrics,
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

    def test_nested_windows_are_compared_independently(self) -> None:
        current = {
            "15m": {
                "transfer_count": 20,
                "total_token_amount": "200",
                "unique_senders": 20,
                "unique_receivers": 20,
                "inflow_count": 0,
                "outflow_count": 0,
                "unclassified_count": 20,
                "active_15m_buckets": 1,
            },
            "1h": {
                "transfer_count": 20,
                "total_token_amount": "200",
                "unique_senders": 20,
                "unique_receivers": 20,
                "inflow_count": 0,
                "outflow_count": 0,
                "unclassified_count": 20,
                "active_15m_buckets": 4,
            },
        }
        prior = {
            name: {
                **metrics,
                "transfer_count": 2,
                "total_token_amount": "20",
                "unique_senders": 2,
                "unique_receivers": 2,
                "unclassified_count": 2,
            }
            for name, metrics in current.items()
        }
        history = [{"window_metrics": prior} for _ in range(4)]
        result = analyze_nested_windows(
            current,
            history,
            min_samples=4,
            max_samples=8,
            mad_multiplier=Decimal("3.5"),
        )
        self.assertEqual(result["ready_windows"], ["15m", "1h"])
        self.assertEqual(result["anomalous_windows"], ["15m", "1h"])
        self.assertTrue(result["multi_window_anomaly"])

    def test_nested_window_metrics_only_exposes_aggregate_facts(self) -> None:
        result = nested_window_metrics(
            {
                "windows": {
                    "15m": {
                        "transfer_count": 2,
                        "total_token_amount": "4.2",
                        "unique_senders": 1,
                        "unique_receivers": 2,
                        "inflow_count": 0,
                        "outflow_count": 0,
                        "unclassified_count": 2,
                        "active_15m_buckets": 1,
                        "private_transfer_payload": "must-not-copy",
                    }
                }
            }
        )
        self.assertEqual(result["15m"]["total_token_amount"], "4.2")
        self.assertNotIn("private_transfer_payload", result["15m"])

    def test_rolling_observation_hashes_private_event_and_wallet_ids(self) -> None:
        tx_hash = "0x" + "ab" * 32
        sender = "0x" + "11" * 20
        receiver = "0x" + "22" * 20
        observation = build_rolling_observation(
            {
                "complete": True,
                "query": {"from_time": 1000, "to_time": 1900},
                "transfers": [
                    {
                        "event_id": f"8453:{tx_hash}:7",
                        "tx_hash": tx_hash,
                        "log_index": 7,
                        "block_time": 1500,
                        "amount": "12.5",
                        "from": {"address": sender},
                        "to": {"address": receiver},
                        "flow_type": "inflow",
                        "explorer_url": f"https://example.invalid/{tx_hash}",
                    }
                ],
            }
        )

        rendered = str(observation).lower()
        self.assertNotIn(tx_hash, rendered)
        self.assertNotIn(sender, rendered)
        self.assertNotIn(receiver, rendered)
        self.assertNotIn("example.invalid", rendered)
        event = observation["events"][0]
        self.assertRegex(event["event_hash"], r"^[0-9a-f]{64}$")
        self.assertRegex(event["from_hash"], r"^[0-9a-f]{64}$")
        self.assertRegex(event["to_hash"], r"^[0-9a-f]{64}$")

    def test_rolling_observation_rejects_incomplete_or_out_of_range(self) -> None:
        with self.assertRaises(ValueError):
            build_rolling_observation(
                {
                    "complete": False,
                    "query": {"from_time": 1000, "to_time": 1900},
                    "transfers": [],
                }
            )
        with self.assertRaises(ValueError):
            build_rolling_observation(
                {
                    "complete": True,
                    "query": {"from_time": 1000, "to_time": 1900},
                    "transfers": [
                        {
                            "event_id": "event",
                            "block_time": 999,
                            "amount": "1",
                            "from": {"address": "0x" + "11" * 20},
                            "to": {"address": "0x" + "22" * 20},
                            "flow_type": "unclassified",
                        }
                    ],
                }
            )


if __name__ == "__main__":
    unittest.main()
