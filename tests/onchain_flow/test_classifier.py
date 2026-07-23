from __future__ import annotations

import unittest
from decimal import Decimal

from paopao_radar.onchain_flow.classifier import classify_transfer
from paopao_radar.onchain_flow.labels import LabelRegistry
from paopao_radar.onchain_flow.models import (
    AddressLabel,
    NormalizedTransfer,
    TokenMetadata,
)


HOT_A = "0x1111111111111111111111111111111111111111"
COLD_A = "0x2222222222222222222222222222222222222222"
HOT_B = "0x3333333333333333333333333333333333333333"
DEPOSIT_A = "0x4444444444444444444444444444444444444444"
LOW = "0x5555555555555555555555555555555555555555"
OUTSIDE_A = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
OUTSIDE_B = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
TOKEN = "0x9999999999999999999999999999999999999999"


def label(
    address: str,
    entity: str,
    address_type: str,
    confidence: float = 0.95,
) -> AddressLabel:
    return AddressLabel(
        chain_id=8453,
        address=address,
        entity_name=entity,
        entity_type="cex",
        address_type=address_type,
        source="test",
        confidence=confidence,
    )


def transfer(from_address: str, to_address: str) -> NormalizedTransfer:
    return NormalizedTransfer.create(
        chain_id=8453,
        chain_name="Base",
        block_number=1,
        block_hash="0xblock",
        block_time=1700000000,
        tx_hash=f"0x{from_address[-4:]}{to_address[-4:]}",
        log_index=0,
        token_address=TOKEN,
        from_address=from_address,
        to_address=to_address,
        amount_raw="1000000",
    )


class FlowClassifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = LabelRegistry(
            [
                label(HOT_A, "A", "hot", 0.98),
                label(COLD_A, "A", "cold", 0.97),
                label(HOT_B, "B", "hot", 0.96),
                label(DEPOSIT_A, "A", "deposit", 0.92),
                label(LOW, "Low", "hot", 0.50),
            ]
        )
        self.metadata = TokenMetadata(
            chain_id=8453,
            token_address=TOKEN,
            symbol="ABC",
            name="ABC",
            decimals=6,
            token_kind="erc20",
            metadata_status="verified",
            updated_at=1700000000,
            price_usd=Decimal("2"),
        )

    def classify(self, from_address: str, to_address: str):
        return classify_transfer(
            transfer(from_address, to_address),
            self.metadata,
            self.registry,
        )

    def test_all_classification_types(self) -> None:
        cases = (
            (HOT_A, COLD_A, "internal"),
            (HOT_A, HOT_B, "cross_cex"),
            (OUTSIDE_A, HOT_A, "inflow"),
            (HOT_A, OUTSIDE_A, "outflow"),
            (DEPOSIT_A, HOT_A, "consolidation"),
            (OUTSIDE_A, OUTSIDE_B, "non_cex"),
        )
        for from_address, to_address, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(
                    self.classify(from_address, to_address).flow_type,
                    expected,
                )

    def test_integer_raw_amount_uses_decimal_accounting(self) -> None:
        flow = self.classify(OUTSIDE_A, HOT_A)
        self.assertEqual(flow.amount, Decimal("1"))
        self.assertEqual(flow.amount_usd, Decimal("2"))

    def test_missing_price_and_metadata_cannot_be_directional_candidates(self) -> None:
        missing_price = TokenMetadata(
            **{
                **self.metadata.__dict__,
                "price_usd": None,
            }
        )
        flow = classify_transfer(
            transfer(OUTSIDE_A, HOT_A),
            missing_price,
            self.registry,
        )
        self.assertEqual(flow.flow_type, "inflow")
        self.assertIsNone(flow.amount_usd)
        self.assertEqual(flow.price_status, "missing")

    def test_low_confidence_label_is_preserved_for_detector_filter(self) -> None:
        flow = self.classify(OUTSIDE_A, LOW)
        self.assertEqual(flow.flow_type, "inflow")
        self.assertEqual(flow.label_confidence, 0.50)


if __name__ == "__main__":
    unittest.main()
