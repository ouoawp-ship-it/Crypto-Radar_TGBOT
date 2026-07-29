from __future__ import annotations

import json
import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.onchain_flow.behavior import (
    BehaviorAnalyzer,
    build_nested_windows,
)

from .analysis_support import CEX, activity, fixture_case, record
from .support import make_settings


def wallet(number: int) -> str:
    return f"0x{number:040x}"


class TokenBehaviorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = make_settings(Path(self.temp.name))
        self.analyzer = BehaviorAnalyzer(self.settings)
        self.to_time = 1_700_000_000

    def test_nested_windows_follow_query_window_order(self) -> None:
        expected = {
            "15m": ["15m"],
            "1h": ["15m", "1h"],
            "4h": ["15m", "1h", "4h"],
            "24h": ["15m", "1h", "4h", "24h"],
        }
        for query_window, names in expected.items():
            with self.subTest(window=query_window):
                windows = build_nested_windows(
                    activity(window=query_window, to_time=self.to_time)
                )
                self.assertEqual([item.name for item in windows], names)

    def test_windows_use_query_to_time_and_exact_boundaries(self) -> None:
        facts = activity(
            window="1h",
            to_time=self.to_time,
            transfers=[
                record(
                    1,
                    block_time=self.to_time - 3600,
                    from_address=wallet(1),
                    to_address=wallet(2),
                    amount="1",
                    flow_type="non_cex",
                ),
                record(
                    2,
                    block_time=self.to_time,
                    from_address=wallet(2),
                    to_address=wallet(3),
                    amount="1",
                    flow_type="non_cex",
                ),
                record(
                    3,
                    block_time=self.to_time - 3601,
                    from_address=wallet(3),
                    to_address=wallet(4),
                    amount="1",
                    flow_type="non_cex",
                ),
            ],
        )
        windows = build_nested_windows(facts)
        one_hour = windows[-1]
        self.assertEqual(one_hour.window_end, self.to_time)
        self.assertEqual(one_hour.window_start, self.to_time - 3600)
        self.assertEqual(len(one_hour.records), 2)

    def test_decimal_window_totals_do_not_lose_precision(self) -> None:
        facts = activity(
            transfers=[
                record(
                    1,
                    block_time=self.to_time - 120,
                    from_address=wallet(1),
                    to_address=wallet(2),
                    amount="0.100000000000000001",
                    flow_type="non_cex",
                ),
                record(
                    2,
                    block_time=self.to_time - 60,
                    from_address=wallet(2),
                    to_address=wallet(3),
                    amount="0.200000000000000002",
                    flow_type="non_cex",
                ),
            ],
            to_time=self.to_time,
        )
        result = self.analyzer.analyze(facts)
        self.assertEqual(
            result["windows"]["1h"]["total_token_amount"],
            "0.300000000000000003",
        )

    def test_zero_activity_and_isolated_fixtures(self) -> None:
        no_activity = self.analyzer.analyze(fixture_case("no_activity"))
        isolated = self.analyzer.analyze(fixture_case("isolated"))
        self.assertEqual(no_activity["status"], "no_activity")
        self.assertEqual(
            no_activity["primary_behavior"]["type"], "no_activity"
        )
        self.assertEqual(isolated["status"], "ok")
        self.assertEqual(
            isolated["primary_behavior"]["type"], "isolated"
        )

    def test_accumulation_requires_repeated_multi_bucket_outflow(self) -> None:
        result = self.analyzer.analyze(fixture_case("accumulation"))
        primary = result["primary_behavior"]
        self.assertEqual(primary["type"], "accumulation_candidate")
        self.assertGreaterEqual(primary["score"], 55)
        self.assertIn("multiple_15m_buckets", primary["supporting_evidence"])
        self.assertEqual(
            primary["score_semantics"], "rule_score_not_probability"
        )

    def test_single_outflow_does_not_trigger_accumulation(self) -> None:
        result = self.analyzer.analyze(
            activity(
                transfers=[
                    record(
                        1,
                        block_time=self.to_time - 120,
                        from_address=CEX,
                        to_address=wallet(2),
                        amount="100",
                        flow_type="outflow",
                    )
                ],
                to_time=self.to_time,
            )
        )
        self.assertNotIn(
            "accumulation_candidate",
            result["coexisting_behavior_types"],
        )

    def test_repeated_outflow_to_one_wallet_can_trigger(self) -> None:
        destination = wallet(200)
        transfers = [
            record(
                index,
                block_time=self.to_time - minutes * 60,
                from_address=CEX,
                to_address=destination,
                amount="10",
                flow_type="outflow",
            )
            for index, minutes in enumerate((50, 25, 5), start=1)
        ]
        result = self.analyzer.analyze(
            activity(transfers=transfers, to_time=self.to_time)
        )
        self.assertIn(
            "accumulation_candidate",
            result["coexisting_behavior_types"],
        )

    def test_opposing_inflow_reduces_accumulation_score(self) -> None:
        transfers = [
            record(
                index,
                block_time=self.to_time - minutes * 60,
                from_address=CEX,
                to_address=wallet(10 + index),
                amount="10",
                flow_type="outflow",
            )
            for index, minutes in enumerate((55, 35, 20, 5), start=1)
        ]
        transfers.append(
            record(
                9,
                block_time=self.to_time - 10 * 60,
                from_address=wallet(99),
                to_address=CEX,
                amount="100",
                flow_type="inflow",
            )
        )
        result = self.analyzer.analyze(
            activity(transfers=transfers, to_time=self.to_time)
        )
        candidate = next(
            item
            for item in result["behavior_candidates"]
            if item["type"] == "accumulation_candidate"
        )
        self.assertIn(
            "opposite_cex_flow_material", candidate["counter_evidence"]
        )
        self.assertLess(candidate["score"], 90)

    def test_distribution_requires_repeated_multi_bucket_inflow(self) -> None:
        result = self.analyzer.analyze(fixture_case("distribution"))
        primary = result["primary_behavior"]
        self.assertEqual(primary["type"], "distribution_candidate")
        self.assertGreaterEqual(primary["score"], 55)

    def test_single_inflow_does_not_trigger_distribution(self) -> None:
        result = self.analyzer.analyze(
            activity(
                transfers=[
                    record(
                        1,
                        block_time=self.to_time - 60,
                        from_address=wallet(1),
                        to_address=CEX,
                        amount="100",
                        flow_type="inflow",
                    )
                ],
                to_time=self.to_time,
            )
        )
        self.assertNotIn(
            "distribution_candidate",
            result["coexisting_behavior_types"],
        )

    def test_internal_and_cross_cex_are_not_directional_evidence(self) -> None:
        transfers = [
            record(
                index,
                block_time=self.to_time - minutes * 60,
                from_address=CEX,
                to_address=wallet(90 + index),
                amount="10",
                flow_type=flow_type,
                from_cex=True,
                to_cex=True,
            )
            for index, (minutes, flow_type) in enumerate(
                ((50, "internal"), (25, "cross_cex"), (5, "internal")),
                start=1,
            )
        ]
        result = self.analyzer.analyze(
            activity(transfers=transfers, to_time=self.to_time)
        )
        self.assertEqual(
            result["primary_behavior"]["type"], "inconclusive_activity"
        )
        self.assertEqual(result["status"], "insufficient_evidence")
        observed = {
            item["type"] for item in result["observed_patterns"]
        }
        self.assertIn("cex_internal_activity_observed", observed)
        self.assertIn("cross_cex_activity_observed", observed)

    def test_missing_labels_block_formal_cex_direction(self) -> None:
        facts = fixture_case("accumulation")
        facts["labels"]["status"] = "missing"
        result = self.analyzer.analyze(facts)
        self.assertNotIn(
            "accumulation_candidate",
            result["coexisting_behavior_types"],
        )

    def test_wallet_consolidation_and_fanout_fixtures(self) -> None:
        consolidation = self.analyzer.analyze(
            fixture_case("wallet_consolidation")
        )
        fanout = self.analyzer.analyze(fixture_case("fanout"))
        self.assertEqual(
            consolidation["primary_behavior"]["type"],
            "wallet_consolidation_candidate",
        )
        self.assertEqual(
            fanout["primary_behavior"]["type"], "fanout_candidate"
        )

    def test_isolated_requires_one_or_two_single_bucket_records(self) -> None:
        cases = {
            "one": [
                record(
                    1,
                    block_time=self.to_time - 60,
                    from_address=wallet(1),
                    to_address=wallet(2),
                    amount="1",
                    flow_type="non_cex",
                )
            ],
            "two": [
                record(
                    index,
                    block_time=self.to_time - seconds,
                    from_address=wallet(index),
                    to_address=wallet(index + 10),
                    amount="1",
                    flow_type="non_cex",
                )
                for index, seconds in ((1, 90), (2, 30))
            ],
        }
        for name, transfers in cases.items():
            with self.subTest(name=name):
                result = self.analyzer.analyze(
                    activity(
                        transfers=transfers,
                        to_time=self.to_time,
                    )
                )
                self.assertEqual(
                    result["primary_behavior"]["type"], "isolated"
                )
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["behavior_candidates"], [])

    def test_activity_beyond_isolated_boundary_is_inconclusive(self) -> None:
        cases = {
            "three_single_bucket": [
                record(
                    index,
                    block_time=self.to_time - index * 20,
                    from_address=wallet(index),
                    to_address=wallet(index + 10),
                    amount="1",
                    flow_type="non_cex",
                )
                for index in range(1, 4)
            ],
            "two_buckets": [
                record(
                    1,
                    block_time=self.to_time - 20 * 60,
                    from_address=wallet(1),
                    to_address=wallet(11),
                    amount="1",
                    flow_type="unclassified",
                ),
                record(
                    2,
                    block_time=self.to_time - 60,
                    from_address=wallet(2),
                    to_address=wallet(12),
                    amount="1",
                    flow_type="unclassified",
                ),
            ],
            "many_dispersed": [
                record(
                    index,
                    block_time=self.to_time - (index % 10) * 30,
                    from_address=wallet(index),
                    to_address=wallet(index + 100),
                    amount="1",
                    flow_type="non_cex",
                )
                for index in range(1, 51)
            ],
        }
        for name, transfers in cases.items():
            with self.subTest(name=name):
                result = self.analyzer.analyze(
                    activity(
                        transfers=transfers,
                        to_time=self.to_time,
                    )
                )
                self.assertEqual(
                    result["primary_behavior"]["type"],
                    "inconclusive_activity",
                )
                self.assertEqual(
                    result["status"], "insufficient_evidence"
                )
                self.assertEqual(result["behavior_candidates"], [])

    def test_fallback_never_displaces_formal_candidate(self) -> None:
        result = self.analyzer.analyze(fixture_case("distribution"))
        self.assertEqual(
            result["primary_behavior"]["type"],
            "distribution_candidate",
        )
        self.assertNotIn(
            "isolated", result["coexisting_behavior_types"]
        )
        self.assertNotIn(
            "inconclusive_activity",
            result["coexisting_behavior_types"],
        )

    def test_identical_nested_events_do_not_add_direction_evidence(
        self,
    ) -> None:
        result = self.analyzer.analyze(fixture_case("accumulation"))
        directional = [
            item
            for item in result["behavior_candidates"]
            if item["type"] == "accumulation_candidate"
        ]
        self.assertTrue(directional)
        self.assertTrue(
            all(
                "repeated_across_nested_windows"
                not in item["supporting_evidence"]
                for item in directional
            )
        )

    def test_older_event_and_new_bucket_add_direction_evidence(self) -> None:
        facts = fixture_case("accumulation")
        facts["transfers"].append(
            record(
                20,
                block_time=self.to_time - 2 * 3600,
                from_address=CEX,
                to_address=wallet(200),
                amount="100",
                flow_type="outflow",
            )
        )
        result = self.analyzer.analyze(facts)
        primary = result["primary_behavior"]
        self.assertEqual(primary["window"], "4h")
        self.assertIn(
            "repeated_across_nested_windows",
            primary["supporting_evidence"],
        )

    def test_older_event_in_existing_bucket_adds_no_direction_evidence(
        self,
    ) -> None:
        transfers = [
            record(
                index,
                block_time=timestamp,
                from_address=CEX,
                to_address=wallet(index),
                amount="10",
                flow_type="outflow",
            )
            for index, timestamp in (
                (1, self.to_time - 3590),
                (2, self.to_time - 1800),
                (3, self.to_time - 300),
                (4, self.to_time - 3610),
            )
        ]
        result = self.analyzer.analyze(
            activity(
                window="4h",
                transfers=transfers,
                to_time=self.to_time,
            )
        )
        self.assertTrue(
            all(
                "repeated_across_nested_windows"
                not in item["supporting_evidence"]
                for item in result["behavior_candidates"]
            )
        )

    def test_identical_nested_structure_events_are_counted_once(
        self,
    ) -> None:
        facts = fixture_case("wallet_consolidation")
        facts["query"]["window"] = "4h"
        facts["query"]["window_seconds"] = 14400
        facts["query"]["from_time"] = self.to_time - 14400
        result = self.analyzer.analyze(facts)
        candidate = next(
            item
            for item in result["behavior_candidates"]
            if item["type"] == "wallet_consolidation_candidate"
        )
        self.assertNotIn(
            "repeated_across_nested_windows",
            candidate["supporting_evidence"],
        )

    def test_older_structure_event_and_bucket_add_repeated_evidence(
        self,
    ) -> None:
        facts = fixture_case("wallet_consolidation")
        facts["query"]["window"] = "4h"
        facts["query"]["window_seconds"] = 14400
        facts["query"]["from_time"] = self.to_time - 14400
        facts["transfers"].append(
            record(
                20,
                block_time=self.to_time - 2 * 3600,
                from_address=wallet(1),
                to_address=wallet(0xDD),
                amount="10",
                flow_type="unclassified",
            )
        )
        target = facts["transfers"][0]["to"]["address"]
        facts["transfers"][-1]["to"]["address"] = target
        result = self.analyzer.analyze(facts)
        candidate = next(
            item
            for item in result["behavior_candidates"]
            if item["type"] == "wallet_consolidation_candidate"
        )
        self.assertIn(
            "repeated_across_nested_windows",
            candidate["supporting_evidence"],
        )

    def test_general_wallet_structures_exclude_cex_touching_flows(
        self,
    ) -> None:
        target = wallet(500)
        outflows = [
            record(
                index,
                block_time=self.to_time - minutes * 60,
                from_address=CEX,
                to_address=target,
                amount="10",
                flow_type="outflow",
            )
            for index, minutes in enumerate((50, 25, 5), start=1)
        ]
        inflows = [
            record(
                index + 10,
                block_time=self.to_time - minutes * 60,
                from_address=wallet(600),
                to_address=wallet(700 + index),
                amount="10",
                flow_type="inflow",
                to_cex=True,
            )
            for index, minutes in enumerate((50, 25, 5), start=1)
        ]
        for name, transfers, forbidden in (
            (
                "outflow",
                outflows,
                "wallet_consolidation_candidate",
            ),
            ("inflow", inflows, "fanout_candidate"),
        ):
            with self.subTest(name=name):
                result = self.analyzer.analyze(
                    activity(
                        transfers=transfers,
                        to_time=self.to_time,
                    )
                )
                self.assertNotIn(
                    forbidden, result["coexisting_behavior_types"]
                )

    def test_non_cex_and_unclassified_wallet_structures_remain_valid(
        self,
    ) -> None:
        for flow_type in ("non_cex", "unclassified"):
            with self.subTest(flow_type=flow_type):
                target = wallet(800)
                transfers = [
                    record(
                        index,
                        block_time=self.to_time - minutes * 60,
                        from_address=wallet(index),
                        to_address=target,
                        amount="10",
                        flow_type=flow_type,
                    )
                    for index, minutes in enumerate(
                        (50, 25, 5), start=1
                    )
                ]
                result = self.analyzer.analyze(
                    activity(
                        transfers=transfers,
                        to_time=self.to_time,
                    )
                )
                self.assertIn(
                    "wallet_consolidation_candidate",
                    result["coexisting_behavior_types"],
                )

    def test_large_structure_candidates_are_low_confidence(self) -> None:
        target = wallet(900)
        consolidation = [
            record(
                index,
                block_time=self.to_time - (index % 10) * 30,
                from_address=wallet(index),
                to_address=target,
                amount="1",
                flow_type="non_cex",
            )
            for index in range(1, 22)
        ]
        fanout = [
            record(
                index + 30,
                block_time=self.to_time - (index % 10) * 30,
                from_address=target,
                to_address=wallet(index),
                amount="1",
                flow_type="unclassified",
            )
            for index in range(1, 22)
        ]
        for name, transfers, candidate_type in (
            (
                "consolidation",
                consolidation,
                "wallet_consolidation_candidate",
            ),
            ("fanout", fanout, "fanout_candidate"),
        ):
            with self.subTest(name=name):
                result = self.analyzer.analyze(
                    activity(
                        transfers=transfers,
                        to_time=self.to_time,
                    )
                )
                candidate = next(
                    item
                    for item in result["behavior_candidates"]
                    if item["type"] == candidate_type
                )
                self.assertLessEqual(candidate["score"], 69)
                self.assertEqual(
                    candidate["confidence_level"], "low"
                )
                self.assertIn(
                    "batch_or_airdrop_possible",
                    candidate["limitations"],
                )

    def test_behavior_output_is_input_order_independent(self) -> None:
        facts = fixture_case("wallet_consolidation")
        reversed_facts = fixture_case("wallet_consolidation")
        reversed_facts["transfers"].reverse()
        self.assertEqual(
            self.analyzer.analyze(facts),
            self.analyzer.analyze(reversed_facts),
        )

    def test_cex_anchor_is_not_general_wallet_pattern(self) -> None:
        distribution = self.analyzer.analyze(
            fixture_case("distribution")
        )
        accumulation = self.analyzer.analyze(
            fixture_case("accumulation")
        )
        self.assertNotIn(
            "wallet_consolidation_candidate",
            distribution["coexisting_behavior_types"],
        )
        self.assertNotIn(
            "fanout_candidate",
            accumulation["coexisting_behavior_types"],
        )

    def test_partial_input_keeps_observations_but_no_formal_candidate(
        self,
    ) -> None:
        facts = fixture_case("accumulation")
        facts["complete"] = False
        facts["status"] = "partial"
        facts["truncated"] = True
        result = self.analyzer.analyze(facts)
        self.assertEqual(result["status"], "partial_input")
        self.assertFalse(result["complete"])
        self.assertEqual(
            result["primary_behavior"]["type"], "insufficient_data"
        )
        self.assertEqual(result["behavior_candidates"], [])
        self.assertIn("query_incomplete", result["limitations"])
        self.assertTrue(result["observed_patterns"])

    def test_price_missing_does_not_block_token_amount_analysis(self) -> None:
        facts = fixture_case("accumulation")
        facts["price"]["status"] = "missing"
        result = self.analyzer.analyze(facts)
        self.assertEqual(
            result["primary_behavior"]["type"], "accumulation_candidate"
        )
        self.assertEqual(result["valuation_basis"], "token_amount")
        self.assertIn("price_unavailable", result["limitations"])

    def test_current_price_changes_only_valuation_basis(self) -> None:
        facts = fixture_case("distribution")
        baseline = self.analyzer.analyze(facts)
        facts["price"]["status"] = "available"
        facts["price"]["price_usd"] = "2"
        priced = self.analyzer.analyze(facts)
        self.assertEqual(
            baseline["primary_behavior"]["type"],
            priced["primary_behavior"]["type"],
        )
        self.assertEqual(
            priced["valuation_basis"], "current_usd_estimate"
        )

    def test_primary_selection_is_score_then_evidence_then_type_order(
        self,
    ) -> None:
        facts = fixture_case("distribution")
        target = wallet(777)
        for index, minutes in enumerate((50, 25, 5), start=20):
            facts["transfers"].append(
                record(
                    index,
                    block_time=self.to_time - minutes * 60,
                    from_address=wallet(index),
                    to_address=target,
                    amount="20",
                    flow_type="non_cex",
                )
            )
        result = self.analyzer.analyze(facts)
        self.assertEqual(
            result["primary_behavior"]["type"],
            "distribution_candidate",
        )
        self.assertIn(
            "wallet_consolidation_candidate",
            result["coexisting_behavior_types"],
        )

    def test_outputs_never_claim_confirmed_control_or_certain_trade(
        self,
    ) -> None:
        payload = json.dumps(
            self.analyzer.analyze(fixture_case("accumulation")),
            ensure_ascii=False,
        )
        for forbidden in (
            "已确认属于同一主力",
            "已确认同一机构控制",
            "已经完成吸筹",
            "一定上涨",
            "已经卖出",
            "必然下跌",
        ):
            self.assertNotIn(forbidden, payload)

    def test_configurable_threshold_can_disable_direction_candidate(
        self,
    ) -> None:
        analyzer = BehaviorAnalyzer(
            replace(self.settings, oar_behavior_min_tx=10)
        )
        result = analyzer.analyze(fixture_case("accumulation"))
        self.assertNotIn(
            "accumulation_candidate",
            result["coexisting_behavior_types"],
        )

    def test_window_directional_dominance_is_decimal_string(self) -> None:
        result = self.analyzer.analyze(fixture_case("mixed_opposing_flows"))
        value = result["windows"]["1h"]["directional_dominance"]
        self.assertIsInstance(value, str)
        self.assertEqual(Decimal(value), Decimal("0.5"))


if __name__ == "__main__":
    unittest.main()
