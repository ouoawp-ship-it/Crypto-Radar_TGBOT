from __future__ import annotations

import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.onchain_flow.aggregator import build_rolling_snapshots
from paopao_radar.onchain_flow.classifier import classify_transfer
from paopao_radar.onchain_flow.detector import detect_rolling_flows
from paopao_radar.onchain_flow.formatter import format_alert
from paopao_radar.onchain_flow.labels import LabelRegistry
from paopao_radar.onchain_flow.models import (
    AddressLabel,
    ClassifiedFlow,
    DetectedRollingFlow,
    NormalizedTransfer,
    RollingFlowSnapshot,
    TokenMetadata,
)
from paopao_radar.onchain_flow.scorer import score_rolling_detection

from .support import make_settings


TOKEN = "0x9999999999999999999999999999999999999999"
CEX = "0x1111111111111111111111111111111111111111"
ZERO = "0x0000000000000000000000000000000000000000"
OUTSIDE = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def flow(
    name: str,
    flow_type: str,
    amount: str,
    block_time: int,
    *,
    exchange: str = "Binance",
    counterparty: str = OUTSIDE,
    price_status: str = "available",
) -> ClassifiedFlow:
    return ClassifiedFlow(
        event_id=name,
        chain_id=8453,
        token_address=TOKEN,
        symbol="ABC",
        block_time=block_time,
        flow_type=flow_type,
        exchange_from=exchange if flow_type == "outflow" else None,
        exchange_to=exchange if flow_type == "inflow" else None,
        counterparty_address=counterparty,
        amount=Decimal(amount),
        amount_usd=Decimal(amount),
        label_confidence=0.95,
        price_status=price_status,
        block_number=100,
        price_source="static",
        price_observed_at=2000,
    )


def metadata() -> TokenMetadata:
    return TokenMetadata(
        chain_id=8453,
        token_address=TOKEN,
        symbol="ABC",
        name="ABC",
        decimals=6,
        token_kind="erc20",
        metadata_status="verified_erc20",
        updated_at=2000,
        price_usd=Decimal("1"),
        volume_24h_usd=Decimal("1000000"),
        price_source="static",
        price_observed_at=2000,
    )


class RollingFlowTests(unittest.TestCase):
    def settings(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return replace(
            make_settings(Path(temporary.name)),
            batch_15m_floor_usd=Decimal("50"),
            continuous_60m_floor_usd=Decimal("50"),
            batch_volume_ratio=Decimal("0"),
            continuous_volume_ratio=Decimal("0"),
            net_dominance_min=Decimal("0.60"),
        )

    def test_signed_net_and_gross_values_for_15m_and_60m(self) -> None:
        flows = [
            flow("in", "inflow", "100", 1950),
            flow("out", "outflow", "40", 1960),
        ]
        snapshots = build_rolling_snapshots(
            flows,
            evaluation_time=2000,
            evaluation_block=123,
            min_label_confidence=0.8,
            price_max_age_sec=300,
        )
        self.assertEqual({item.duration_sec for item in snapshots}, {900, 3600})
        for snapshot in snapshots:
            self.assertEqual(snapshot.gross_inflow_usd, Decimal("100"))
            self.assertEqual(snapshot.gross_outflow_usd, Decimal("40"))
            self.assertEqual(snapshot.net_flow_usd, Decimal("60"))
            self.assertEqual(snapshot.direction, "inflow")
            self.assertEqual(snapshot.net_dominance, Decimal("0.6"))

    def test_negative_net_means_exchange_outflow(self) -> None:
        snapshot = build_rolling_snapshots(
            [
                flow("in", "inflow", "20", 1950),
                flow("out", "outflow", "100", 1960),
            ],
            evaluation_time=2000,
            evaluation_block=123,
            min_label_confidence=0.8,
            price_max_age_sec=300,
        )[0]
        self.assertEqual(snapshot.net_flow_usd, Decimal("-80"))
        self.assertEqual(snapshot.direction, "outflow")

    def test_balanced_opposite_flow_suppresses_directional_alert(self) -> None:
        flows = [
            flow(f"in-{index}", "inflow", "20", 1950 + index)
            for index in range(5)
        ] + [
            flow(f"out-{index}", "outflow", "18", 1950 + index)
            for index in range(5)
        ]
        snapshots = build_rolling_snapshots(
            flows,
            evaluation_time=2000,
            evaluation_block=123,
            min_label_confidence=0.8,
            price_max_age_sec=300,
        )
        detected = detect_rolling_flows(
            snapshots, {(8453, TOKEN): metadata()}, self.settings()
        )
        self.assertEqual(detected, [])

    def test_rolling_boundary_is_inclusive_and_stale_price_is_excluded(self) -> None:
        boundary = flow("boundary", "inflow", "100", 1100)
        stale = replace(
            flow("stale", "inflow", "100", 1999),
            price_observed_at=1000,
        )
        snapshots = build_rolling_snapshots(
            [boundary, stale],
            evaluation_time=2000,
            evaluation_block=123,
            min_label_confidence=0.8,
            price_max_age_sec=300,
        )
        fifteen = next(item for item in snapshots if item.duration_sec == 900)
        self.assertEqual(fifteen.gross_inflow_usd, Decimal("100"))

    def test_multi_exchange_rolling_detection_and_copy(self) -> None:
        flows = []
        for index in range(8):
            exchange = "Binance" if index < 4 else "OKX"
            flows.append(
                flow(
                    f"in-{index}",
                    "inflow",
                    "20",
                    2000 - (index * 500),
                    exchange=exchange,
                    counterparty=f"outside-{index}",
                )
            )
        snapshots = build_rolling_snapshots(
            flows,
            evaluation_time=2000,
            evaluation_block=123456,
            min_label_confidence=0.8,
            price_max_age_sec=300,
        )
        detected = detect_rolling_flows(
            snapshots, {(8453, TOKEN): metadata()}, self.settings()
        )
        sixty = next(
            item for item in detected if item.snapshot.duration_sec == 3600
        )
        self.assertIn("continuous_flow", sixty.detection_types)
        self.assertIn("multi_exchange", sixty.detection_types)
        rendered = format_alert(score_rolling_detection(sixty))
        self.assertIn("合约：" + TOKEN, rendered)
        self.assertIn("总流入交易所", rendered)
        self.assertIn("总从交易所流出", rendered)
        self.assertIn("净流量（流入-流出）", rendered)
        self.assertIn("Base finalized block：123456", rendered)
        self.assertIn("评分不是概率", rendered)
        self.assertIn("不保证价格必然上涨或下跌", rendered)

    def test_unpriced_flow_never_enters_rolling_snapshot(self) -> None:
        snapshots = build_rolling_snapshots(
            [flow("missing", "inflow", "100", 1999, price_status="missing")],
            evaluation_time=2000,
            evaluation_block=123,
            min_label_confidence=0.8,
            price_max_age_sec=300,
        )
        self.assertEqual(snapshots, [])

    def test_severity_escalation_uses_a_new_audited_alert_key(self) -> None:
        base = RollingFlowSnapshot(
            snapshot_key="stable-snapshot",
            chain_id=8453,
            token_address=TOKEN,
            symbol="ABC",
            evaluation_time=2000,
            duration_sec=3600,
            gross_inflow_usd=Decimal("100"),
            gross_outflow_usd=Decimal("0"),
            net_flow_usd=Decimal("100"),
            inflow_tx_count=8,
            outflow_tx_count=0,
            distinct_inbound_counterparties=8,
            distinct_outbound_counterparties=0,
            exchanges=("Binance",),
            active_15m_buckets=3,
            min_label_confidence=0.95,
            price_source="static",
            price_observed_at=2000,
            evaluation_block=123,
            algorithm_version="p3.1-test",
        )
        medium = score_rolling_detection(
            DetectedRollingFlow(
                snapshot=base,
                detection_types=("continuous_flow",),
                threshold_usd=Decimal("100"),
            )
        )
        high = score_rolling_detection(
            DetectedRollingFlow(
                snapshot=replace(base, exchanges=("Binance", "OKX")),
                detection_types=("continuous_flow", "multi_exchange"),
                threshold_usd=Decimal("100"),
            )
        )
        self.assertEqual(medium.confidence, "medium")
        self.assertEqual(high.confidence, "high")
        self.assertNotEqual(medium.alert_key, high.alert_key)


class LifecycleClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = LabelRegistry(
            [
                AddressLabel(
                    chain_id=8453,
                    address=CEX,
                    entity_name="Binance",
                    entity_type="cex",
                    address_type="hot",
                    source="test",
                    confidence=0.95,
                )
            ]
        )

    def classify(self, from_address, to_address):
        transfer = NormalizedTransfer.create(
            chain_id=8453,
            chain_name="Base",
            block_number=1,
            block_hash="0xblock",
            block_time=2000,
            tx_hash="0xtx",
            log_index=0,
            token_address=TOKEN,
            from_address=from_address,
            to_address=to_address,
            amount_raw=1,
        )
        return classify_transfer(transfer, metadata(), self.registry)

    def test_mint_and_burn_are_non_directional_lifecycle_events(self) -> None:
        self.assertEqual(self.classify(ZERO, CEX).flow_type, "mint")
        self.assertEqual(self.classify(CEX, ZERO).flow_type, "burn")


if __name__ == "__main__":
    unittest.main()
