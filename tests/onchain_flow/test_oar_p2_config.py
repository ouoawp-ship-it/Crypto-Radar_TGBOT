from __future__ import annotations

import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.onchain_flow.config import (
    OnchainSettings,
    SettingsValidationError,
)

from .support import make_settings


class OarP2ConfigTests(unittest.TestCase):
    def test_safe_defaults_match_documented_thresholds(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
        self.assertEqual(settings.oar_behavior_min_tx, 3)
        self.assertEqual(
            settings.oar_behavior_dominance_min, Decimal("0.67")
        )
        self.assertEqual(settings.oar_behavior_min_active_buckets_1h, 2)
        self.assertEqual(settings.oar_behavior_min_active_buckets_long, 3)
        self.assertEqual(settings.oar_pattern_min_wallets, 3)
        self.assertEqual(settings.oar_pattern_min_tx, 3)
        self.assertEqual(
            settings.oar_pattern_min_amount_share, Decimal("0.10")
        )
        self.assertEqual(settings.oar_wallet_sync_window_sec, 300)
        self.assertEqual(
            settings.oar_wallet_amount_similarity_tolerance,
            Decimal("0.10"),
        )
        self.assertEqual(settings.oar_max_analyzed_wallets, 100)
        self.assertEqual(settings.oar_max_wallet_groups, 20)
        self.assertEqual(settings.oar_max_source_event_ids, 50)

    def test_environment_overrides_are_loaded_and_diagnostic_is_safe(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            settings = OnchainSettings.load(
                base_dir=Path(tmp),
                environ={
                    "OAR_BEHAVIOR_MIN_TX": "4",
                    "OAR_BEHAVIOR_DOMINANCE_MIN": "0.75",
                    "OAR_PATTERN_MIN_WALLETS": "4",
                    "OAR_WALLET_SYNC_WINDOW_SEC": "420",
                    "OAR_MAX_WALLET_GROUPS": "12",
                },
            )
        settings.validate()
        self.assertEqual(settings.oar_behavior_min_tx, 4)
        self.assertEqual(
            settings.oar_behavior_dominance_min, Decimal("0.75")
        )
        self.assertEqual(settings.oar_pattern_min_wallets, 4)
        self.assertEqual(settings.oar_wallet_sync_window_sec, 420)
        self.assertEqual(settings.oar_max_wallet_groups, 12)
        diagnostic = settings.diagnostic()["token_analysis"]
        self.assertEqual(diagnostic["behavior_min_tx"], 4)
        self.assertEqual(diagnostic["behavior_dominance_min"], "0.75")
        self.assertEqual(diagnostic["wallet_sync_window_sec"], 420)
        self.assertNotIn("rpc", str(diagnostic).lower())

    def test_integer_hard_caps_are_enforced(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
        cases = {
            "oar_behavior_min_tx": 1001,
            "oar_behavior_min_active_buckets_1h": 97,
            "oar_behavior_min_active_buckets_long": 97,
            "oar_pattern_min_wallets": 201,
            "oar_pattern_min_tx": 1001,
            "oar_wallet_sync_window_sec": 1801,
            "oar_max_analyzed_wallets": 201,
            "oar_max_wallet_groups": 51,
            "oar_max_source_event_ids": 201,
        }
        for field, value in cases.items():
            with self.subTest(field=field):
                with self.assertRaises(SettingsValidationError):
                    replace(settings, **{field: value}).validate()

    def test_decimal_ranges_and_non_finite_values_are_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
        cases = (
            ("oar_behavior_dominance_min", Decimal("0.49")),
            ("oar_behavior_dominance_min", Decimal("1.01")),
            ("oar_pattern_min_amount_share", Decimal("-0.01")),
            ("oar_pattern_min_amount_share", Decimal("1.01")),
            (
                "oar_wallet_amount_similarity_tolerance",
                Decimal("0.51"),
            ),
            ("oar_behavior_dominance_min", Decimal("NaN")),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value):
                with self.assertRaises(SettingsValidationError):
                    replace(settings, **{field: value}).validate()


if __name__ == "__main__":
    unittest.main()
