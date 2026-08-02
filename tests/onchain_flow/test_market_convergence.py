from __future__ import annotations

import unittest

from paopao_radar.onchain_flow.market_convergence import (
    evaluate_market_convergence,
)


class MarketConvergenceTests(unittest.TestCase):
    @staticmethod
    def signals() -> list[dict[str, object]]:
        return [
            {
                "module": "launch",
                "score": 80,
                "age_sec": 60,
                "private_payload": "must-not-copy",
            },
            {
                "module": "funding",
                "score": 70,
                "age_sec": 120,
            },
        ]

    def test_no_market_context_is_not_convergence(self) -> None:
        result = evaluate_market_convergence(
            [],
            {"status": "ready", "anomaly": True},
            onchain_actionable=True,
            behavior_score=70,
            max_wallet_group_score=45,
        )
        self.assertEqual(result["status"], "no_market_context")
        self.assertEqual(result["level"], "none")
        self.assertEqual(result["rule_score"], 0)

    def test_multi_window_cooccurrence_is_high_but_not_directional(self) -> None:
        result = evaluate_market_convergence(
            self.signals(),
            {
                "status": "ready",
                "anomaly": True,
                "multi_window_anomaly": True,
            },
            onchain_actionable=True,
            behavior_score=70,
            max_wallet_group_score=45,
        )
        self.assertEqual(
            result["status"], "multi_window_anomaly_cooccurrence"
        )
        self.assertEqual(result["level"], "high")
        self.assertEqual(result["direction_alignment"], "not_evaluated")
        self.assertEqual(result["rule_score"], 100)
        self.assertFalse(result["notification_gate_changed"])
        self.assertNotIn("private_payload", str(result))

    def test_unknown_modules_are_not_accepted_as_market_evidence(self) -> None:
        result = evaluate_market_convergence(
            [{"module": "untrusted", "score": 100}],
            {"status": "cold_start", "anomaly": False},
            onchain_actionable=False,
            behavior_score=0,
            max_wallet_group_score=0,
        )
        self.assertEqual(result["market_signal_count"], 0)
        self.assertEqual(result["status"], "no_market_context")

    def test_market_context_alone_is_low_confidence_context(self) -> None:
        result = evaluate_market_convergence(
            self.signals()[:1],
            {"status": "cold_start", "anomaly": False},
            onchain_actionable=False,
            behavior_score=0,
            max_wallet_group_score=0,
        )
        self.assertEqual(result["status"], "market_context_only")
        self.assertEqual(result["level"], "low")
        self.assertIn("cooccurrence_not_causation", result["limitations"])

    def test_structured_directions_can_align_without_changing_gate(self) -> None:
        signals = self.signals()
        signals[0]["direction"] = "long"
        signals[1]["direction"] = "long"
        result = evaluate_market_convergence(
            signals,
            {"status": "ready", "anomaly": True},
            onchain_actionable=True,
            behavior_type="accumulation_candidate",
            behavior_score=80,
            max_wallet_group_score=50,
        )
        self.assertEqual(result["market_direction"], "long")
        self.assertEqual(result["onchain_direction"], "long")
        self.assertEqual(result["direction_alignment"], "aligned")
        self.assertEqual(result["structured_direction_signal_count"], 2)
        self.assertTrue(result["directional_hypothesis_only"])
        self.assertFalse(result["notification_gate_changed"])

    def test_opposed_and_mixed_directions_are_explicit(self) -> None:
        opposed = evaluate_market_convergence(
            [{"module": "funding", "direction": "short"}],
            {"status": "ready", "anomaly": True},
            onchain_actionable=True,
            behavior_type="accumulation_candidate",
            behavior_score=80,
            max_wallet_group_score=50,
        )
        self.assertEqual(opposed["direction_alignment"], "opposed")
        self.assertIn(
            "market_onchain_direction_opposed", opposed["evidence"]
        )

        mixed = evaluate_market_convergence(
            [
                {"module": "launch", "direction": "long"},
                {"module": "funding", "direction": "short"},
            ],
            {"status": "ready", "anomaly": True},
            onchain_actionable=True,
            behavior_type="distribution_candidate",
            behavior_score=80,
            max_wallet_group_score=50,
        )
        self.assertEqual(mixed["market_direction"], "mixed")
        self.assertEqual(mixed["direction_alignment"], "mixed")
        self.assertIn(
            "market_signal_direction_conflicted", mixed["limitations"]
        )

    def test_non_actionable_behavior_has_no_onchain_direction(self) -> None:
        result = evaluate_market_convergence(
            [{"module": "launch", "direction": "long"}],
            {"status": "ready", "anomaly": False},
            onchain_actionable=False,
            behavior_type="accumulation_candidate",
            behavior_score=20,
            max_wallet_group_score=10,
        )
        self.assertEqual(result["onchain_direction"], "not_structured")
        self.assertEqual(result["direction_alignment"], "not_evaluated")


if __name__ == "__main__":
    unittest.main()
