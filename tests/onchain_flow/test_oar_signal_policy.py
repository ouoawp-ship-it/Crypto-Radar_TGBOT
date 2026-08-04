from __future__ import annotations

import unittest

from paopao_radar.onchain_flow.signal_policy import DefaultSignalPolicy


class SignalPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = DefaultSignalPolicy(
            min_behavior_score=55,
            min_wallet_score=60,
            single_transfer_enabled=True,
            min_single_transfer_score=60,
        )

    def evaluate(self, **overrides: object) -> bool:
        values: dict[str, object] = {
            "payload_complete": True,
            "analysis_complete": True,
            "analysis_status": "ok",
            "behavior_type": "insufficient_data",
            "behavior_score": 0,
            "max_wallet_score": 0,
            "single_transfer_signals": (),
        }
        values.update(overrides)
        return self.policy.actionable(**values)  # type: ignore[arg-type]

    def test_preserves_behavior_and_wallet_gates(self) -> None:
        self.assertTrue(
            self.evaluate(
                behavior_type="distribution_candidate",
                behavior_score=55,
            )
        )
        self.assertTrue(self.evaluate(max_wallet_score=60))

    def test_single_transfer_is_an_explicit_or_gate(self) -> None:
        self.assertTrue(
            self.evaluate(
                single_transfer_signals=(
                    {
                        "actionable": True,
                        "rule_score": 60,
                        "data_completeness": "complete",
                    },
                )
            )
        )

    def test_partial_inputs_and_partial_event_facts_never_pass(self) -> None:
        event = (
            {
                "actionable": True,
                "rule_score": 90,
                "data_completeness": "partial",
            },
        )
        self.assertFalse(self.evaluate(payload_complete=False, single_transfer_signals=event))
        self.assertFalse(self.evaluate(analysis_complete=False, single_transfer_signals=event))
        self.assertFalse(self.evaluate(single_transfer_signals=event))

    def test_disabled_event_engine_does_not_change_p2(self) -> None:
        policy = DefaultSignalPolicy(
            min_behavior_score=55,
            min_wallet_score=60,
            single_transfer_enabled=False,
            min_single_transfer_score=60,
        )
        self.assertFalse(
            policy.actionable(
                payload_complete=True,
                analysis_complete=True,
                analysis_status="ok",
                behavior_type="insufficient_data",
                behavior_score=0,
                max_wallet_score=0,
                single_transfer_signals=(
                    {
                        "actionable": True,
                        "rule_score": 100,
                        "data_completeness": "complete",
                    },
                ),
            )
        )


if __name__ == "__main__":
    unittest.main()
