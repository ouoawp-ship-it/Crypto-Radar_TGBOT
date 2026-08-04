from __future__ import annotations

import unittest
from decimal import Decimal

from paopao_radar.onchain_flow.domain import (
    ProjectRelationship,
    SingleTransferRiskContext,
    TokenSnapshot,
)
from paopao_radar.onchain_flow.models import NormalizedTransfer
from paopao_radar.onchain_flow.single_transfer_risk import (
    SingleTransferRiskEngine,
    SingleTransferRiskThresholds,
)
from paopao_radar.onchain_flow.token_snapshots import (
    EvmTokenSnapshotProvider,
)


TOKEN = "0x" + "a" * 40
SENDER = "0x" + "b" * 40
CEX = "0x" + "c" * 40


def transfer(*, chain_id: int = 8453, amount_raw: int = 90_000_000) -> NormalizedTransfer:
    return NormalizedTransfer.create(
        chain_id=chain_id,
        chain_name="Base" if chain_id == 8453 else "BSC",
        block_number=100,
        block_hash="0x" + "1" * 64,
        block_time=1_700_000_000,
        tx_hash="0x" + "2" * 64,
        log_index=1,
        token_address=TOKEN,
        from_address=SENDER,
        to_address=CEX,
        amount_raw=amount_raw,
        confirmation_status="finalized",
        source="fixture",
    )


def snapshot(
    *,
    chain_id: int = 8453,
    before: Decimal | None = Decimal("100"),
    after: Decimal | None = Decimal("10"),
    total: Decimal | None = Decimal("1000"),
    circulating: Decimal | None = None,
    balance_status: str = "ok",
    supply_status: str = "ok",
) -> TokenSnapshot:
    return TokenSnapshot(
        chain_id=chain_id,
        token_address=TOKEN,
        block_number=100,
        decimals=6,
        sender_balance_before=before,
        sender_balance_after=after,
        total_supply=total,
        circulating_supply=circulating,
        balance_status=balance_status,
        supply_status=supply_status,
    )


def context(
    *,
    chain_id: int = 8453,
    amount: Decimal = Decimal("90"),
    usd: Decimal | None = Decimal("2000000"),
    before: Decimal | None = Decimal("100"),
    after: Decimal | None = Decimal("10"),
    total: Decimal | None = Decimal("1000"),
    circulating: Decimal | None = None,
    classification: str = "inflow",
    source_role: str = "unclassified",
    destination_role: str = "deposit",
    complete: bool = True,
    finalized: bool = True,
    identity_coverage: str = "reviewed",
    relationship: ProjectRelationship | None = None,
    counterflow: Decimal | None = None,
    internal_evidence: tuple[str, ...] = (),
    balance_status: str = "ok",
    supply_status: str = "ok",
) -> SingleTransferRiskContext:
    return SingleTransferRiskContext(
        transfer=transfer(chain_id=chain_id, amount_raw=int(amount * 1_000_000)),
        amount_token=amount,
        usd_value=usd,
        snapshot=snapshot(
            chain_id=chain_id,
            before=before,
            after=after,
            total=total,
            circulating=circulating,
            balance_status=balance_status,
            supply_status=supply_status,
        ),
        source_role=source_role,
        destination_role=destination_role,
        classification=classification,
        query_complete=complete,
        finalized=finalized,
        identity_coverage=identity_coverage,
        project_relationship=relationship,
        historical_single_transfer_anomaly=Decimal("4"),
        same_window_cex_outflow_counter_evidence=counterflow,
        internal_transfer_probability_evidence=internal_evidence,
        liquidity_impact="unknown",
    )


class SingleTransferRiskTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = SingleTransferRiskEngine(
            SingleTransferRiskThresholds(enabled=True)
        )

    def types(self, value: SingleTransferRiskContext) -> set[str]:
        return {signal.signal_type for signal in self.engine.evaluate(value)}

    def test_default_disabled(self) -> None:
        engine = SingleTransferRiskEngine(SingleTransferRiskThresholds())
        self.assertEqual(engine.evaluate(context()), ())

    def test_wallet_to_cex_deposit_and_hot(self) -> None:
        self.assertIn("large_cex_inflow", self.types(context()))
        self.assertIn(
            "large_cex_inflow",
            self.types(context(destination_role="hot")),
        )

    def test_cex_outflow(self) -> None:
        result = self.types(
            context(
                classification="outflow",
                source_role="hot",
                destination_role="unclassified",
            )
        )
        self.assertEqual(result, {"large_cex_outflow"})

    def test_internal_and_cross_cex_are_not_direction_signals(self) -> None:
        self.assertEqual(self.engine.evaluate(context(classification="internal")), ())
        self.assertEqual(
            self.engine.evaluate(context(classification="cross_cex")), ()
        )

    def test_supply_share_can_gate_without_price(self) -> None:
        signals = self.engine.evaluate(context(usd=None, amount=Decimal("10")))
        self.assertTrue(signals)
        self.assertIn("high_supply_share", signals[0].support_evidence)
        self.assertIn("price_unavailable", signals[0].limitations)

    def test_exit_boundaries_are_deterministic(self) -> None:
        high = self.engine.evaluate(
            context(amount=Decimal("80"), after=Decimal("20"))
        )
        self.assertNotIn("near_full_exit_to_cex", {row.signal_type for row in high})
        self.assertIn("sender_high_exit", high[0].support_evidence)

        near = self.engine.evaluate(
            context(amount=Decimal("95"), after=Decimal("5"))
        )
        self.assertIn("near_full_exit_to_cex", {row.signal_type for row in near})

        full = self.engine.evaluate(
            context(amount=Decimal("99"), after=Decimal("1"))
        )
        self.assertIn("full_exit_to_cex", {row.signal_type for row in full})

    def test_sender_balance_inconsistent_is_not_actionable(self) -> None:
        signals = self.engine.evaluate(
            context(amount=Decimal("101"), before=Decimal("100"), after=Decimal("0"))
        )
        self.assertTrue(signals)
        self.assertTrue(all(not row.actionable for row in signals))
        self.assertIn("sender_balance_inconsistent", signals[0].limitations)

    def test_balance_or_supply_failure_degrades(self) -> None:
        balance = self.engine.evaluate(
            context(balance_status="rpc_failed", before=None, after=None)
        )
        supply = self.engine.evaluate(
            context(supply_status="rpc_failed", total=None)
        )
        self.assertTrue(balance and supply)
        self.assertTrue(all(row.data_completeness == "partial" for row in balance))
        self.assertTrue(all(row.data_completeness == "partial" for row in supply))
        self.assertTrue(all(not row.actionable for row in (*balance, *supply)))

    def test_missing_circulating_supply_is_explicit_not_zero(self) -> None:
        signal = self.engine.evaluate(context(circulating=None))[0]
        self.assertIsNone(signal.facts["circulating_supply_share"])

    def test_identity_coverage_and_counter_evidence_are_separate(self) -> None:
        signal = self.engine.evaluate(
            context(
                identity_coverage="insufficient",
                counterflow=Decimal("50"),
                internal_evidence=("shared_exchange_cluster",),
            )
        )[0]
        self.assertIn("identity_coverage_insufficient", signal.limitations)
        self.assertIn("same_window_cex_outflow", signal.counter_evidence)
        self.assertEqual(signal.score_semantics, "rule_score_not_probability")

    def test_project_and_unlock_relationships(self) -> None:
        project = ProjectRelationship(
            chain_id=8453,
            address=SENDER,
            relationship_type="treasury",
            evidence_type="deployment_graph",
            confidence="deterministic",
        )
        self.assertIn(
            "project_related_cex_inflow",
            self.types(context(relationship=project)),
        )
        unlock = ProjectRelationship(
            chain_id=8453,
            address=SENDER,
            relationship_type="vesting",
            evidence_type="reviewed_contract_relation",
            confidence="reviewed",
            reviewed=True,
        )
        result = self.types(context(relationship=unlock))
        self.assertIn("project_related_cex_inflow", result)
        self.assertIn("unlock_related_cex_inflow", result)

    def test_partial_reorg_and_non_finalized_never_signal(self) -> None:
        self.assertEqual(self.engine.evaluate(context(complete=False)), ())
        self.assertEqual(self.engine.evaluate(context(finalized=False)), ())
        orphan = context()
        object.__setattr__(orphan.transfer, "removed", True)
        self.assertEqual(self.engine.evaluate(orphan), ())

    def test_malformed_transfer_is_rejected(self) -> None:
        value = context()
        object.__setattr__(value.transfer, "amount_raw", -1)
        with self.assertRaisesRegex(ValueError, "malformed_transfer"):
            self.engine.evaluate(value)

    def test_base_and_bsc_share_the_same_domain_engine(self) -> None:
        base = self.types(context(chain_id=8453))
        bsc = self.types(context(chain_id=56))
        self.assertEqual(base, bsc)


class FakeRpc:
    def __init__(self, values: list[int]):
        self.values = list(values)
        self.calls: list[tuple[str, str, str]] = []

    def eth_call(self, address: str, data: str, *, block_tag: str = "latest") -> str:
        self.calls.append((address, data, block_tag))
        if not self.values:
            raise AssertionError("unexpected RPC call")
        return hex(self.values.pop(0))


class SnapshotProviderTests(unittest.TestCase):
    def test_exact_blocks_and_supply_share_facts(self) -> None:
        rpc = FakeRpc([100_000_000, 10_000_000, 1_000_000_000])
        provider = EvmTokenSnapshotProvider(rpc, ttl_sec=300)
        result = provider.snapshot_for_transfer(transfer(), decimals=6)
        self.assertEqual(result.sender_balance_before, Decimal("100"))
        self.assertEqual(result.sender_balance_after, Decimal("10"))
        self.assertEqual(result.total_supply, Decimal("1000"))
        self.assertEqual([item[2] for item in rpc.calls], ["0x63", "0x64", "0x64"])

    def test_budget_exhaustion_is_bounded_and_degraded(self) -> None:
        rpc = FakeRpc([100_000_000, 1_000_000_000])
        provider = EvmTokenSnapshotProvider(
            rpc,
            max_balance_calls=1,
            max_supply_calls=1,
        )
        result = provider.snapshot_for_transfer(transfer(), decimals=6)
        self.assertEqual(len(rpc.calls), 2)
        self.assertEqual(result.balance_status, "budget_exhausted")
        self.assertEqual(result.supply_status, "ok")


if __name__ == "__main__":
    unittest.main()
