from __future__ import annotations

import unittest

from paopao_radar.onchain_flow.controlled_alert_preview import (
    evaluate_controlled_alert_preview,
)


class ControlledAlertPreviewTests(unittest.TestCase):
    def evaluate(
        self,
        *,
        complete: bool = True,
        actionable: bool = True,
        baseline_status: str = "ready",
        anomaly: bool = True,
        multi_window: bool = False,
        market_signals: int = 1,
        enforced: bool = False,
    ) -> dict[str, object]:
        return evaluate_controlled_alert_preview(
            activity_complete=complete,
            analysis_complete=complete,
            existing_rule_gate_met=actionable,
            historical_baseline={
                "status": baseline_status,
                "anomaly": anomaly,
                "multi_window_anomaly": multi_window,
            },
            market_convergence={
                "level": "high" if multi_window else "medium",
                "market_signal_count": market_signals,
            },
            enforced=enforced,
        )

    def test_complete_convergent_anomaly_is_preview_eligible(self) -> None:
        result = self.evaluate()

        self.assertEqual(result["status"], "eligible")
        self.assertTrue(result["would_alert"])
        self.assertEqual(result["preview_level"], "medium")
        self.assertEqual(result["block_reasons"], [])
        self.assertTrue(result["dry_run_only"])
        self.assertFalse(result["notification_gate_changed"])
        self.assertEqual(result["telegram_calls"], 0)

    def test_enforced_policy_marks_notification_gate_changed(self) -> None:
        result = self.evaluate(enforced=True)

        self.assertTrue(result["would_alert"])
        self.assertTrue(result["enforced"])
        self.assertFalse(result["dry_run_only"])
        self.assertTrue(result["notification_gate_changed"])
        self.assertEqual(result["policy"], "controlled_anomaly_v1")
        self.assertEqual(result["telegram_calls"], 0)

    def test_multi_window_anomaly_is_high_preview_level(self) -> None:
        result = self.evaluate(multi_window=True)

        self.assertTrue(result["would_alert"])
        self.assertEqual(result["preview_level"], "high")

    def test_cold_start_and_no_market_context_fail_closed(self) -> None:
        result = self.evaluate(
            baseline_status="cold_start",
            anomaly=False,
            market_signals=0,
        )

        self.assertFalse(result["would_alert"])
        self.assertEqual(
            result["block_reasons"],
            [
                "historical_baseline_not_ready",
                "market_context_not_present",
            ],
        )

    def test_ready_baseline_without_anomaly_is_blocked(self) -> None:
        result = self.evaluate(anomaly=False)

        self.assertFalse(result["would_alert"])
        self.assertIn(
            "historical_anomaly_not_observed", result["block_reasons"]
        )

    def test_partial_and_non_actionable_scan_is_blocked(self) -> None:
        result = self.evaluate(complete=False, actionable=False)

        self.assertFalse(result["would_alert"])
        self.assertEqual(
            result["block_reasons"][:2],
            ["scan_incomplete", "existing_rule_gate_not_met"],
        )


if __name__ == "__main__":
    unittest.main()
