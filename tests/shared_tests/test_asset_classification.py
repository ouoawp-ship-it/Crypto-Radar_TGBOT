from __future__ import annotations

import unittest

from radars.pulse.simple_alert import _asset_tier
from shared.asset_classification import (
    classify_binance_instrument,
    crypto_contract_eligibility,
    is_stable_crypto_asset,
)


class AssetClassificationTests(unittest.TestCase):
    def test_spy_is_index_etf_perpetual_not_crypto_or_tokenized_stock(self) -> None:
        result = classify_binance_instrument("SPYUSDT", {"baseAsset": "SPY"})

        self.assertEqual(result["asset_family"], "tradfi")
        self.assertEqual(result["asset_class"], "etf_index")
        self.assertEqual(result["asset_subclass"], "broad_market_etf")
        self.assertEqual(result["instrument_type"], "usdt_perpetual")
        self.assertIn("标普500", result["asset_category_label"])
        self.assertNotIn("代币化股票", result["asset_category_label"])

    def test_reviewed_commodity_and_equity_products_are_distinct(self) -> None:
        gold = classify_binance_instrument("XAUUSDT")
        oil = classify_binance_instrument("CLUSDT")
        equity = classify_binance_instrument("NVDAUSDT")

        self.assertEqual(gold["asset_subclass"], "precious_metal")
        self.assertIn("黄金", gold["asset_category_label"])
        self.assertEqual(oil["asset_subclass"], "energy")
        self.assertIn("原油", oil["asset_category_label"])
        self.assertEqual(equity["asset_class"], "equity")
        self.assertIn("半导体", equity["asset_category_label"])

    def test_mrna_is_reviewed_equity_not_altcoin(self) -> None:
        result = classify_binance_instrument("MRNAUSDT")

        self.assertEqual(result["asset_family"], "tradfi")
        self.assertEqual(result["asset_class"], "equity")

    def test_crypto_tier_and_exchange_theme_are_both_preserved(self) -> None:
        bitcoin = classify_binance_instrument(
            "BTCUSDT",
            {"baseAsset": "BTC", "underlyingSubType": ["PoW"]},
        )
        solana = classify_binance_instrument("SOLUSDT", {"baseAsset": "SOL"})
        alt = classify_binance_instrument(
            "TESTUSDT",
            {"baseAsset": "TEST", "underlyingSubType": ["AI", "Meme"]},
        )

        self.assertEqual(bitcoin["asset_subclass"], "core_crypto")
        self.assertIn("核心主流", bitcoin["asset_category_label"])
        self.assertEqual(solana["asset_subclass"], "large_crypto")
        self.assertIn("主流加密", solana["asset_category_label"])
        self.assertEqual(alt["asset_subclass"], "altcoin")
        self.assertEqual(alt["asset_theme_tags"], ["AI", "Meme"])

    def test_reviewed_stablecoin_registry_is_central_and_non_disruptive(self) -> None:
        for base_asset in ("USDC", "FDUSD", "USDE", "DAI", "TUSD"):
            with self.subTest(base_asset=base_asset):
                self.assertTrue(is_stable_crypto_asset(base_asset))
                result = classify_binance_instrument(
                    f"{base_asset}USDT",
                    {"baseAsset": base_asset},
                )

                self.assertEqual(result["asset_family"], "crypto")
                self.assertEqual(result["asset_class"], "crypto")
                self.assertEqual(result["asset_subclass"], "altcoin")

    def test_exchange_metadata_can_identify_new_tradfi_or_tokenized_products(self) -> None:
        stock = classify_binance_instrument(
            "NEWUSDT",
            {"baseAsset": "NEW", "underlyingType": "EQUITY"},
        )
        tokenized = classify_binance_instrument(
            "NEWUSD",
            {"baseAsset": "NEW", "underlyingType": "TOKENIZED_STOCK"},
        )

        self.assertEqual(stock["asset_class"], "equity")
        self.assertEqual(stock["asset_category_source"], "binance_exchange_info")
        self.assertEqual(tokenized["instrument_type"], "tokenized_equity")

    def test_strict_contract_gate_accepts_reviewed_crypto_and_quarantines_unknown(self) -> None:
        eligible, reason, classification = crypto_contract_eligibility(
            "TESTUSDT",
            {
                "symbol": "TESTUSDT",
                "baseAsset": "TEST",
                "quoteAsset": "USDT",
                "status": "TRADING",
                "contractType": "PERPETUAL",
                "underlyingType": "COIN",
            },
        )
        unknown, unknown_reason, _ = crypto_contract_eligibility(
            "MYSTERYUSDT",
            {
                "symbol": "MYSTERYUSDT",
                "baseAsset": "MYSTERY",
                "quoteAsset": "USDT",
                "status": "TRADING",
                "contractType": "PERPETUAL",
            },
        )

        self.assertTrue(eligible)
        self.assertEqual(reason, "eligible_crypto")
        self.assertEqual(classification["asset_category_source"], "binance_exchange_info")
        self.assertFalse(unknown)
        self.assertEqual(unknown_reason, "unknown_asset")

    def test_strict_contract_gate_rejects_tradfi_contract_type_and_stablecoin(self) -> None:
        tradfi, tradfi_reason, _ = crypto_contract_eligibility(
            "MRNAUSDT",
            {
                "symbol": "MRNAUSDT",
                "baseAsset": "MRNA",
                "quoteAsset": "USDT",
                "status": "TRADING",
                "contractType": "TRADIFI_PERPETUAL",
                "underlyingType": "EQUITY",
            },
        )
        stable, stable_reason, _ = crypto_contract_eligibility(
            "USDCUSDT",
            {
                "symbol": "USDCUSDT",
                "baseAsset": "USDC",
                "quoteAsset": "USDT",
                "status": "TRADING",
                "contractType": "PERPETUAL",
                "underlyingType": "COIN",
            },
        )

        self.assertFalse(tradfi)
        self.assertEqual(tradfi_reason, "non_crypto_contract_type")
        self.assertFalse(stable)
        self.assertEqual(stable_reason, "stablecoin")

    def test_pulse_threshold_tiers_use_shared_asset_classification(self) -> None:
        self.assertEqual(_asset_tier("BTCUSDT"), "core")
        self.assertEqual(_asset_tier("SOLUSDT"), "large")
        self.assertEqual(_asset_tier("TESTUSDT"), "alt")
        self.assertEqual(_asset_tier("SPYUSDT"), "unknown")


if __name__ == "__main__":
    unittest.main()
