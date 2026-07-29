from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.onchain_flow.wallet_groups import WalletGroupAnalyzer

from .analysis_support import CEX, activity, fixture_case, record
from .support import make_settings


def wallet(number: int) -> str:
    return f"0x{number:040x}"


class WalletGroupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = make_settings(Path(self.temp.name))
        self.analyzer = WalletGroupAnalyzer(self.settings)
        self.to_time = 1_700_000_000

    @staticmethod
    def group(
        result: dict[str, object], group_type: str
    ) -> dict[str, object]:
        return next(
            item
            for item in result["groups"]
            if item["group_type"] == group_type
        )

    def test_shared_target_and_shared_source_groups(self) -> None:
        shared_target = self.analyzer.analyze(
            fixture_case("wallet_consolidation")
        )
        shared_source = self.analyzer.analyze(fixture_case("fanout"))
        self.assertEqual(
            self.group(shared_target, "shared_target")["level"],
            "中等概率关联",
        )
        self.assertEqual(
            self.group(shared_source, "shared_source")["level"],
            "中等概率关联",
        )

    def test_synchronized_cex_inflow_group_is_capped_when_cex_is_only_link(
        self,
    ) -> None:
        result = self.analyzer.analyze(
            fixture_case("synchronized_cex_cohort")
        )
        group = self.group(result, "synchronized_cex_inflow")
        self.assertLessEqual(group["score"], 39)
        self.assertEqual(group["level"], "弱关联")
        self.assertIn(
            "coordinated_deposit_not_control_proof",
            group["limitations"],
        )
        self.assertIn(
            "same_cex_may_be_only_common_factor",
            group["counter_evidence"],
        )

    def test_synchronized_cex_outflow_group(self) -> None:
        transfers = [
            record(
                index,
                block_time=self.to_time - seconds,
                from_address=CEX,
                to_address=wallet(index),
                amount="10",
                flow_type="outflow",
            )
            for index, seconds in enumerate((240, 120, 30), start=1)
        ]
        result = self.analyzer.analyze(
            activity(transfers=transfers, to_time=self.to_time)
        )
        group = self.group(result, "synchronized_cex_outflow")
        self.assertLessEqual(group["score"], 39)
        self.assertIn("cex_batch_withdrawal_possible", group["limitations"])

    def test_time_similarity_and_direct_transfer_add_named_evidence(
        self,
    ) -> None:
        target = wallet(90)
        members = (wallet(1), wallet(2), wallet(3))
        transfers = [
            record(
                index,
                block_time=self.to_time - seconds,
                from_address=member,
                to_address=target,
                amount=amount,
                flow_type="non_cex",
            )
            for index, (member, seconds, amount) in enumerate(
                zip(members, (240, 120, 30), ("100", "102", "99")),
                start=1,
            )
        ]
        transfers.append(
            record(
                9,
                block_time=self.to_time - 20,
                from_address=members[0],
                to_address=members[1],
                amount="1",
                flow_type="non_cex",
            )
        )
        result = self.analyzer.analyze(
            activity(transfers=transfers, to_time=self.to_time)
        )
        group = self.group(result, "shared_target")
        self.assertIn("time_synchronized", group["supporting_evidence"])
        self.assertIn("amounts_similar", group["supporting_evidence"])
        self.assertNotIn(
            "repeated_across_nested_windows",
            group["supporting_evidence"],
        )
        self.assertIn(
            "direct_token_transfer_between_members",
            group["supporting_evidence"],
        )

    def test_fewer_than_three_events_caps_as_insufficient_evidence(
        self,
    ) -> None:
        analyzer = WalletGroupAnalyzer(
            replace(
                self.settings,
                oar_pattern_min_wallets=2,
                oar_pattern_min_tx=2,
            )
        )
        target = wallet(90)
        transfers = [
            record(
                index,
                block_time=self.to_time - seconds,
                from_address=wallet(index),
                to_address=target,
                amount="10",
                flow_type="non_cex",
            )
            for index, seconds in enumerate((120, 30), start=1)
        ]
        result = analyzer.analyze(
            activity(transfers=transfers, to_time=self.to_time)
        )
        group = self.group(result, "shared_target")
        self.assertLessEqual(group["score"], 19)
        self.assertEqual(group["level"], "证据不足")

    def test_single_evidence_type_is_at_most_weak(self) -> None:
        target = wallet(90)
        transfers = [
            record(
                index,
                block_time=self.to_time - minutes * 60,
                from_address=wallet(index),
                to_address=target,
                amount=amount,
                flow_type="non_cex",
            )
            for index, (minutes, amount) in enumerate(
                ((14, "1"), (7, "100"), (1, "10000")), start=1
            )
        ]
        result = self.analyzer.analyze(
            activity(
                window="15m",
                transfers=transfers,
                to_time=self.to_time,
            )
        )
        group = self.group(result, "shared_target")
        self.assertLessEqual(group["score"], 39)
        self.assertIn("single_evidence_type", group["limitations"])

    def test_partial_input_caps_every_group_as_weak(self) -> None:
        facts = fixture_case("wallet_consolidation")
        facts["complete"] = False
        facts["status"] = "partial"
        result = self.analyzer.analyze(facts)
        self.assertTrue(result["groups"])
        self.assertTrue(
            all(group["score"] <= 39 for group in result["groups"])
        )
        self.assertTrue(
            all(
                "query_incomplete" in group["limitations"]
                for group in result["groups"]
            )
        )

    def test_more_than_twenty_members_is_at_most_weak(self) -> None:
        target = wallet(999)
        transfers = [
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
        result = self.analyzer.analyze(
            activity(transfers=transfers, to_time=self.to_time)
        )
        group = self.group(result, "shared_target")
        self.assertLessEqual(group["score"], 39)
        self.assertIn("batch_or_airdrop_possible", group["limitations"])

    def test_identical_nested_event_sets_do_not_increase_group_score(
        self,
    ) -> None:
        target = wallet(999)
        transfers = [
            record(
                index,
                block_time=self.to_time - seconds,
                from_address=wallet(index),
                to_address=target,
                amount="10",
                flow_type="non_cex",
            )
            for index, seconds in enumerate((240, 120, 30), start=1)
        ]
        result = self.analyzer.analyze(
            activity(
                window="4h",
                transfers=transfers,
                to_time=self.to_time,
            )
        )
        groups = [
            item
            for item in result["groups"]
            if item["group_type"] == "shared_target"
        ]
        self.assertTrue(groups)
        self.assertTrue(
            all(
                "repeated_across_nested_windows"
                not in item["supporting_evidence"]
                for item in groups
            )
        )

    def test_older_independent_bucket_increases_group_evidence(self) -> None:
        target = wallet(999)
        transfers = [
            record(
                index,
                block_time=self.to_time - seconds,
                from_address=wallet(index),
                to_address=target,
                amount="10",
                flow_type="non_cex",
            )
            for index, seconds in enumerate((240, 120, 30), start=1)
        ]
        transfers.append(
            record(
                10,
                block_time=self.to_time - 2 * 3600,
                from_address=wallet(1),
                to_address=target,
                amount="10",
                flow_type="non_cex",
            )
        )
        result = self.analyzer.analyze(
            activity(
                window="4h",
                transfers=transfers,
                to_time=self.to_time,
            )
        )
        four_hour = next(
            item
            for item in result["groups"]
            if item["group_type"] == "shared_target"
            and item["window"] == "4h"
        )
        self.assertIn(
            "repeated_across_nested_windows",
            four_hour["supporting_evidence"],
        )

    def test_older_event_in_existing_bucket_does_not_repeat_group(
        self,
    ) -> None:
        target = wallet(999)
        transfers = [
            record(
                index,
                block_time=timestamp,
                from_address=wallet(index),
                to_address=target,
                amount="10",
                flow_type="non_cex",
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
                for item in result["groups"]
            )
        )

    def test_cex_addresses_never_enter_shared_wallet_groups(self) -> None:
        target = wallet(999)
        outflows = [
            record(
                index,
                block_time=self.to_time - seconds,
                from_address=CEX,
                to_address=target,
                amount="10",
                flow_type="outflow",
            )
            for index, seconds in enumerate((240, 120, 30), start=1)
        ]
        inflows = [
            record(
                index + 10,
                block_time=self.to_time - seconds,
                from_address=target,
                to_address=CEX,
                amount="10",
                flow_type="inflow",
            )
            for index, seconds in enumerate((240, 120, 30), start=1)
        ]
        for transfers in (outflows, inflows):
            result = self.analyzer.analyze(
                activity(
                    transfers=transfers,
                    to_time=self.to_time,
                )
            )
            for group in result["groups"]:
                if group["group_type"] in {
                    "shared_target",
                    "shared_source",
                }:
                    self.assertNotIn(CEX, group["wallets"])

    def test_no_transitive_or_permanent_wallet_merge(self) -> None:
        target = wallet(90)
        source = wallet(91)
        shared = wallet(2)
        transfers = [
            record(
                index,
                block_time=self.to_time - index * 30,
                from_address=member,
                to_address=target,
                amount="10",
                flow_type="non_cex",
            )
            for index, member in enumerate(
                (wallet(1), shared, wallet(3)), start=1
            )
        ]
        transfers.extend(
            record(
                10 + index,
                block_time=self.to_time - index * 40,
                from_address=source,
                to_address=member,
                amount="10",
                flow_type="non_cex",
            )
            for index, member in enumerate(
                (shared, wallet(4), wallet(5)), start=1
            )
        )
        result = self.analyzer.analyze(
            activity(transfers=transfers, to_time=self.to_time)
        )
        target_group = self.group(result, "shared_target")
        source_group = self.group(result, "shared_source")
        self.assertIn(shared, target_group["wallets"])
        self.assertIn(shared, source_group["wallets"])
        self.assertEqual(len(target_group["wallets"]), 3)
        self.assertEqual(len(source_group["wallets"]), 3)
        self.assertNotEqual(target_group["group_id"], source_group["group_id"])

    def test_group_id_is_stable_and_anchor_sensitive(self) -> None:
        facts = fixture_case("wallet_consolidation")
        first = self.analyzer.analyze(facts)
        second = self.analyzer.analyze(facts)
        self.assertEqual(
            [item["group_id"] for item in first["groups"]],
            [item["group_id"] for item in second["groups"]],
        )
        changed = fixture_case("wallet_consolidation")
        for item in changed["transfers"]:
            item["to"]["address"] = wallet(991)
        third = self.analyzer.analyze(changed)
        self.assertNotEqual(
            self.group(first, "shared_target")["group_id"],
            self.group(third, "shared_target")["group_id"],
        )

    def test_group_and_wallet_order_is_deterministic(self) -> None:
        facts = fixture_case("wallet_consolidation")
        reversed_facts = fixture_case("wallet_consolidation")
        reversed_facts["transfers"].reverse()
        first = self.analyzer.analyze(facts)["groups"]
        second = self.analyzer.analyze(reversed_facts)["groups"]
        self.assertEqual(first, second)
        for group in first:
            self.assertEqual(group["wallets"], sorted(group["wallets"]))

    def test_source_event_ids_are_bounded(self) -> None:
        analyzer = WalletGroupAnalyzer(
            replace(self.settings, oar_max_source_event_ids=2)
        )
        result = analyzer.analyze(fixture_case("wallet_consolidation"))
        group = self.group(result, "shared_target")
        self.assertEqual(len(group["source_event_ids"]), 2)
        self.assertTrue(group["source_events_truncated"])

    def test_shared_wallet_pattern_requires_minimum_amount_share(
        self,
    ) -> None:
        target = wallet(90)
        transfers = [
            record(
                index,
                block_time=self.to_time - index * 30,
                from_address=wallet(index),
                to_address=target,
                amount="1",
                flow_type="non_cex",
            )
            for index in range(1, 4)
        ]
        transfers.append(
            record(
                9,
                block_time=self.to_time - 10,
                from_address=wallet(50),
                to_address=wallet(51),
                amount="1000",
                flow_type="non_cex",
            )
        )
        result = self.analyzer.analyze(
            activity(transfers=transfers, to_time=self.to_time)
        )
        self.assertFalse(
            any(
                group["group_type"] == "shared_target"
                for group in result["groups"]
            )
        )

    def test_analysis_wallet_budget_is_separate_from_activity_complete(
        self,
    ) -> None:
        analyzer = WalletGroupAnalyzer(
            replace(self.settings, oar_max_analyzed_wallets=3)
        )
        facts = fixture_case("wallet_consolidation")
        result = analyzer.analyze(facts)
        self.assertTrue(facts["complete"])
        self.assertFalse(result["complete"])
        self.assertTrue(result["truncated"])
        self.assertIn("analysis_budget_exhausted", result["limitations"])
        self.assertTrue(
            all(group["score"] <= 59 for group in result["groups"])
        )

    def test_mint_and_burn_do_not_enter_wallet_groups(self) -> None:
        facts = activity(
            transfers=[
                record(
                    1,
                    block_time=self.to_time - 60,
                    from_address=wallet(0),
                    to_address=wallet(1),
                    amount="10",
                    flow_type="mint",
                ),
                record(
                    2,
                    block_time=self.to_time - 30,
                    from_address=wallet(1),
                    to_address=wallet(0),
                    amount="10",
                    flow_type="burn",
                ),
            ],
            to_time=self.to_time,
        )
        self.assertEqual(self.analyzer.analyze(facts)["groups"], [])

    def test_output_has_rule_score_not_probability(self) -> None:
        result = self.analyzer.analyze(
            fixture_case("wallet_consolidation")
        )
        payload = json.dumps(result, ensure_ascii=False)
        for group in result["groups"]:
            self.assertEqual(
                group["score_semantics"], "rule_score_not_probability"
            )
            self.assertNotIn("probability", group)
        self.assertNotIn("已确认属于同一主力", payload)
        self.assertNotIn("钱包已合并", payload)


if __name__ == "__main__":
    unittest.main()
