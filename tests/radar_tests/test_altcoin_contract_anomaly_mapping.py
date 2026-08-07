from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from radars.altcoin_contract_anomaly.mapping import (
    CmcIdentityResolver,
    load_mapping_overrides,
    normalize_contract_asset,
)


def cmc_entry(
    cmc_id: int,
    symbol: str,
    *,
    address: str = "",
    active: bool = True,
) -> dict[str, object]:
    return {
        "id": cmc_id,
        "name": f"{symbol} Token",
        "symbol": symbol,
        "slug": f"{symbol.lower()}-token",
        "is_active": active,
        "platform": {"token_address": address} if address else {},
    }


class ContractAssetNormalizationTests(unittest.TestCase):
    def test_multiplier_contracts_are_normalized_once(self) -> None:
        self.assertEqual(normalize_contract_asset("1000PEPE"), ("PEPE", 1_000))
        self.assertEqual(normalize_contract_asset("1000000MOG"), ("MOG", 1_000_000))
        self.assertEqual(normalize_contract_asset("1MBABYDOGE"), ("BABYDOGE", 1_000_000))
        self.assertEqual(normalize_contract_asset("COTI"), ("COTI", 1))


class CmcIdentityResolverTests(unittest.TestCase):
    def test_manual_override_is_loaded_and_wins_as_formal_identity(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "mapping-overrides.json"
            path.write_text(
                json.dumps({
                    "schema_version": 1,
                    "overrides": [{
                        "binance_symbol": "1000PEPEUSDT",
                        "cmc_id": 24478,
                        "normalized_asset": "PEPE",
                        "note": "reviewed identity",
                    }],
                }),
                encoding="utf-8",
            )
            overrides = load_mapping_overrides(path)

        resolver = CmcIdentityResolver(
            [cmc_entry(24478, "PEPE")],
            overrides=overrides,
            verified_at="2026-08-07T00:00:00+00:00",
        )
        record = resolver.resolve_one(
            {"symbol": "1000PEPEUSDT", "baseAsset": "1000PEPE"},
            {"cmc_id": 999999, "mapper_name": "WRONG"},
        )

        self.assertTrue(record.is_formal)
        self.assertEqual(record.mapping_method, "verified_override")
        self.assertEqual(record.cmc_id, 24478)
        self.assertEqual(record.normalized_asset, "PEPE")
        self.assertEqual(record.contract_multiplier, 1_000)
        self.assertIn("manual_override", record.mapping_evidence)

    def test_manual_override_can_resolve_conflicting_binance_marketing_rows(self) -> None:
        resolver = CmcIdentityResolver(
            [cmc_entry(1, "ABC"), cmc_entry(2, "ABC")],
            overrides={"ABCUSDT": {"cmc_id": 1, "normalized_asset": "ABC"}},
        )

        summary = resolver.resolve_many(
            [{"symbol": "ABCUSDT", "baseAsset": "ABC"}],
            [
                {"symbol": "ABCUSDT", "base_asset": "ABC", "cmc_id": 1},
                {"symbol": "ABCUSDT", "base_asset": "ABC", "cmc_id": 2},
            ],
        )

        self.assertTrue(summary.records[0].is_formal)
        self.assertEqual(summary.records[0].cmc_id, 1)

    def test_exact_contract_address_is_a_formal_match(self) -> None:
        resolver = CmcIdentityResolver([
            cmc_entry(321, "ABC", address="0xAbCdEf"),
        ])

        record = resolver.resolve_one(
            {"symbol": "ABCUSDT", "baseAsset": "ABC"},
            {"token_address": "0xabcdef"},
        )

        self.assertTrue(record.is_formal)
        self.assertEqual(record.mapping_method, "contract_address_match")
        self.assertEqual(record.cmc_id, 321)
        self.assertIn("exact_token_address", record.mapping_evidence)

    def test_contract_address_conflicting_with_binance_identity_is_rejected(self) -> None:
        resolver = CmcIdentityResolver([
            cmc_entry(1, "WRONG", address="0xabc"),
            cmc_entry(2, "ABC"),
        ])

        record = resolver.resolve_one(
            {"symbol": "ABCUSDT", "baseAsset": "ABC"},
            {
                "token_address": "0xabc",
                "cmc_id": 2,
                "mapper_name": "ABC",
            },
        )

        self.assertFalse(record.is_formal)
        self.assertEqual(record.mapping_method, "ambiguous")
        self.assertEqual(record.rejection_reason, "ambiguous_symbol")

    def test_existing_binance_cmc_anchor_is_preserved_as_formal_match(self) -> None:
        resolver = CmcIdentityResolver([cmc_entry(24478, "PEPE")])

        record = resolver.resolve_one(
            {"symbol": "1000PEPEUSDT", "baseAsset": "1000PEPE"},
            {"cmc_id": 24478, "mapper_name": "PEPE"},
        )

        self.assertTrue(record.is_formal)
        self.assertEqual(record.mapping_method, "existing_verified_anchor")
        self.assertEqual(record.cmc_id, 24478)
        self.assertEqual(record.contract_multiplier, 1_000)
        self.assertIn("binance_cmc_unique_id", record.mapping_evidence)
        self.assertIn("multiplier_mapper_name_consistent", record.mapping_evidence)

    def test_unique_symbol_only_is_diagnostic_and_never_formal(self) -> None:
        resolver = CmcIdentityResolver([cmc_entry(123, "ONLY")])

        record = resolver.resolve_one(
            {"symbol": "ONLYUSDT", "baseAsset": "ONLY"},
        )

        self.assertFalse(record.is_formal)
        self.assertEqual(record.mapping_method, "unique_symbol_diagnostic")
        self.assertEqual(record.mapping_confidence, "diagnostic")
        self.assertEqual(record.cmc_id, 123)
        self.assertEqual(record.rejection_reason, "missing_cmc_id")

    def test_same_symbol_collision_is_rejected_instead_of_guessed(self) -> None:
        resolver = CmcIdentityResolver([
            cmc_entry(101, "DUP"),
            cmc_entry(202, "DUP"),
        ])

        record = resolver.resolve_one(
            {"symbol": "DUPUSDT", "baseAsset": "DUP"},
        )

        self.assertFalse(record.is_formal)
        self.assertEqual(record.mapping_method, "ambiguous")
        self.assertIsNone(record.cmc_id)
        self.assertEqual(record.rejection_reason, "ambiguous_symbol")

    def test_mapping_summary_records_conflict_and_missing_reasons(self) -> None:
        resolver = CmcIdentityResolver([
            cmc_entry(101, "DUP"),
            cmc_entry(202, "DUP"),
        ])

        summary = resolver.resolve_many(
            [
                {"symbol": "DUPUSDT", "baseAsset": "DUP"},
                {"symbol": "NEWUSDT", "baseAsset": "NEW"},
            ],
            [],
        )

        self.assertEqual(summary.conflict_count, 1)
        self.assertEqual(summary.unmapped_count, 1)
        self.assertEqual(summary.trusted_count, 0)
        self.assertEqual(summary.reason_counts, {
            "ambiguous_symbol": 1,
            "missing_cmc_id": 1,
        })
        reasons = {record.binance_symbol: record.rejection_reason for record in summary.records}
        self.assertEqual(reasons["DUPUSDT"], "ambiguous_symbol")
        self.assertEqual(reasons["NEWUSDT"], "missing_cmc_id")

    def test_conflicting_duplicate_binance_identity_rows_are_rejected(self) -> None:
        resolver = CmcIdentityResolver([cmc_entry(1, "ABC"), cmc_entry(2, "ABC")])

        summary = resolver.resolve_many(
            [{"symbol": "ABCUSDT", "baseAsset": "ABC"}],
            [
                {"symbol": "ABCUSDT", "cmc_id": 1, "mapper_name": "ABC"},
                {"symbol": "ABCUSDT", "cmc_id": 2, "mapper_name": "ABC"},
            ],
        )

        self.assertEqual(summary.conflict_count, 1)
        self.assertEqual(summary.records[0].rejection_reason, "ambiguous_symbol")

    def test_unique_binance_mapper_anchor_can_bridge_a_missing_contract_row(self) -> None:
        resolver = CmcIdentityResolver([cmc_entry(24478, "PEPE")])

        summary = resolver.resolve_many(
            [{"symbol": "1000PEPEUSDT", "baseAsset": "1000PEPE"}],
            [{
                "symbol": "PEPEUSDC",
                "base_asset": "PEPE",
                "cmc_id": 24478,
                "mapper_name": "PEPE",
            }],
        )

        self.assertTrue(summary.records[0].is_formal)
        self.assertIn(
            "binance_unique_mapper_anchor",
            summary.records[0].mapping_evidence,
        )

    def test_mapper_bridge_rejects_a_source_row_with_different_base_identity(self) -> None:
        resolver = CmcIdentityResolver([cmc_entry(1, "FOO")])

        summary = resolver.resolve_many(
            [{"symbol": "FOOUSDT", "baseAsset": "FOO"}],
            [{
                "symbol": "BARUSDC",
                "base_asset": "BAR",
                "cmc_id": 1,
                "mapper_name": "FOO",
            }],
        )

        self.assertFalse(summary.records[0].is_formal)
        self.assertEqual(
            summary.records[0].mapping_method,
            "unique_symbol_diagnostic",
        )


if __name__ == "__main__":
    unittest.main()
