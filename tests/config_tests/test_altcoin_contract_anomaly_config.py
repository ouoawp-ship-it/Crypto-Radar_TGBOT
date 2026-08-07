from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from config import BASE_DIR, Settings
from radars.altcoin_contract_anomaly.configuration import (
    AltcoinAnomalyConfig,
    AltcoinAnomalyConfigError,
)


class AltcoinContractAnomalyConfigTests(unittest.TestCase):
    def test_defaults_are_safe_and_use_decimal_ratios(self) -> None:
        settings = Settings()

        self.assertFalse(settings.altcoin_contract_anomaly_enable)
        self.assertEqual(settings.altcoin_contract_anomaly_cmc_api_key, "")
        self.assertEqual(settings.altcoin_contract_anomaly_cmc_batch_size, 100)
        self.assertEqual(settings.altcoin_contract_anomaly_cmc_retry, 2)
        self.assertEqual(settings.altcoin_contract_anomaly_cmc_backoff_base_sec, 0.5)
        self.assertEqual(
            settings.altcoin_contract_anomaly_cmc_min_request_interval_sec,
            2.0,
        )
        self.assertEqual(settings.altcoin_contract_anomaly_oi_workers, 8)
        self.assertEqual(settings.altcoin_contract_anomaly_oi_request_budget, 600)
        self.assertEqual(
            settings.altcoin_contract_anomaly_market_cap_max_usd,
            30_000_000,
        )
        self.assertEqual(
            settings.altcoin_contract_anomaly_short_squeeze_min_oi_market_cap_ratio,
            0.20,
        )
        self.assertEqual(
            settings.altcoin_contract_anomaly_high_leverage_min_oi_market_cap_ratio,
            0.50,
        )
        self.assertEqual(
            settings.altcoin_contract_anomaly_mapping_overrides_path,
            BASE_DIR / "config" / "altcoin_contract_anomaly_overrides.json",
        )

    def test_environment_values_load_through_central_settings(self) -> None:
        secret = "fake-cmc-key-never-log"
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            values = {
                "ALTCOIN_CONTRACT_ANOMALY_ENABLE": "true",
                "ALTCOIN_CONTRACT_ANOMALY_CMC_API_KEY": secret,
                "ALTCOIN_CONTRACT_ANOMALY_CMC_CONNECT_TIMEOUT_SEC": "7",
                "ALTCOIN_CONTRACT_ANOMALY_CMC_READ_TIMEOUT_SEC": "23",
                "ALTCOIN_CONTRACT_ANOMALY_CMC_RETRY": "3",
                "ALTCOIN_CONTRACT_ANOMALY_CMC_BACKOFF_BASE_SEC": "1.25",
                "ALTCOIN_CONTRACT_ANOMALY_CMC_MIN_REQUEST_INTERVAL_SEC": "2.5",
                "ALTCOIN_CONTRACT_ANOMALY_CMC_BATCH_SIZE": "50",
                "ALTCOIN_CONTRACT_ANOMALY_CMC_CACHE_TTL_SEC": "600",
                "ALTCOIN_CONTRACT_ANOMALY_CMC_MAX_DATA_AGE_SEC": "1200",
                "ALTCOIN_CONTRACT_ANOMALY_CMC_CACHE_FILE": "cmc-test.json",
                "ALTCOIN_CONTRACT_ANOMALY_CANDIDATE_SNAPSHOT_FILE": "pool-test.json",
                "ALTCOIN_CONTRACT_ANOMALY_MAPPING_OVERRIDES_FILE": str(
                    root / "overrides.json"
                ),
                "ALTCOIN_CONTRACT_ANOMALY_MARKET_CAP_MAX_USD": "25000000",
                "ALTCOIN_CONTRACT_ANOMALY_SHORT_SQUEEZE_MIN_OI_MARKET_CAP_RATIO": "0.25",
                "ALTCOIN_CONTRACT_ANOMALY_SHORT_SQUEEZE_MAX_FUNDING_RATE": "-0.00001",
                "ALTCOIN_CONTRACT_ANOMALY_HIGH_LEVERAGE_MIN_OI_MARKET_CAP_RATIO": "0.55",
                "ALTCOIN_CONTRACT_ANOMALY_CANDIDATE_REFRESH_SEC": "420",
                "ALTCOIN_CONTRACT_ANOMALY_BINANCE_OI_MAX_AGE_SEC": "240",
                "ALTCOIN_CONTRACT_ANOMALY_FUNDING_MAX_AGE_SEC": "300",
                "ALTCOIN_CONTRACT_ANOMALY_TELEGRAM_PREVIEW_PAGE_CHARS": "3600",
                "ALTCOIN_CONTRACT_ANOMALY_OI_WORKERS": "6",
                "ALTCOIN_CONTRACT_ANOMALY_OI_REQUEST_BUDGET": "500",
            }
            with patch.dict(os.environ, values, clear=True), patch(
                "config.settings.load_env_file",
                return_value={},
            ):
                settings = Settings.load()

        self.assertTrue(settings.altcoin_contract_anomaly_enable)
        self.assertEqual(settings.altcoin_contract_anomaly_cmc_api_key, secret)
        self.assertEqual(settings.altcoin_contract_anomaly_cmc_connect_timeout_sec, 7)
        self.assertEqual(settings.altcoin_contract_anomaly_cmc_read_timeout_sec, 23)
        self.assertEqual(settings.altcoin_contract_anomaly_cmc_retry, 3)
        self.assertEqual(settings.altcoin_contract_anomaly_cmc_backoff_base_sec, 1.25)
        self.assertEqual(
            settings.altcoin_contract_anomaly_cmc_min_request_interval_sec,
            2.5,
        )
        self.assertEqual(settings.altcoin_contract_anomaly_cmc_batch_size, 50)
        self.assertEqual(settings.altcoin_contract_anomaly_cmc_cache_ttl_sec, 600)
        self.assertEqual(settings.altcoin_contract_anomaly_cmc_max_data_age_sec, 1200)
        self.assertEqual(settings.altcoin_contract_anomaly_cmc_cache_path.name, "cmc-test.json")
        self.assertEqual(
            settings.altcoin_contract_anomaly_candidate_snapshot_path.name,
            "pool-test.json",
        )
        self.assertEqual(
            settings.altcoin_contract_anomaly_mapping_overrides_path,
            root / "overrides.json",
        )
        self.assertEqual(settings.altcoin_contract_anomaly_market_cap_max_usd, 25_000_000)
        self.assertEqual(
            settings.altcoin_contract_anomaly_short_squeeze_min_oi_market_cap_ratio,
            0.25,
        )
        self.assertEqual(
            settings.altcoin_contract_anomaly_short_squeeze_max_funding_rate,
            -0.00001,
        )
        self.assertEqual(
            settings.altcoin_contract_anomaly_high_leverage_min_oi_market_cap_ratio,
            0.55,
        )
        self.assertEqual(settings.altcoin_contract_anomaly_candidate_refresh_sec, 420)
        self.assertEqual(settings.altcoin_contract_anomaly_binance_oi_max_age_sec, 240)
        self.assertEqual(settings.altcoin_contract_anomaly_funding_max_age_sec, 300)
        self.assertEqual(settings.altcoin_contract_anomaly_telegram_preview_page_chars, 3600)
        self.assertEqual(settings.altcoin_contract_anomaly_oi_workers, 6)
        self.assertEqual(settings.altcoin_contract_anomaly_oi_request_budget, 500)

    def test_bounded_operational_values_fall_back_to_safe_defaults(self) -> None:
        values = {
            "ALTCOIN_CONTRACT_ANOMALY_CMC_BATCH_SIZE": "101",
            "ALTCOIN_CONTRACT_ANOMALY_CMC_RETRY": "99",
            "ALTCOIN_CONTRACT_ANOMALY_OI_WORKERS": "0",
            "ALTCOIN_CONTRACT_ANOMALY_TELEGRAM_PREVIEW_PAGE_CHARS": "5000",
        }
        with patch.dict(os.environ, values, clear=True), patch(
            "config.settings.load_env_file",
            return_value={},
        ):
            settings = Settings.load()

        self.assertEqual(settings.altcoin_contract_anomaly_cmc_batch_size, 100)
        self.assertEqual(settings.altcoin_contract_anomaly_cmc_retry, 2)
        self.assertEqual(settings.altcoin_contract_anomaly_oi_workers, 8)
        self.assertEqual(
            settings.altcoin_contract_anomaly_telegram_preview_page_chars,
            3800,
        )

    def test_runtime_paths_follow_a_custom_data_directory(self) -> None:
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            settings = Settings(data_dir=data_dir)

            self.assertEqual(
                settings.altcoin_contract_anomaly_cmc_cache_path,
                data_dir / "altcoin_contract_anomaly_cmc_cache.json",
            )
            self.assertEqual(
                settings.altcoin_contract_anomaly_candidate_snapshot_path,
                data_dir / "altcoin_contract_anomaly_candidate_pool.json",
            )
            self.assertEqual(
                settings.altcoin_contract_anomaly_mapping_overrides_path,
                BASE_DIR / "config" / "altcoin_contract_anomaly_overrides.json",
            )

    def test_redacted_status_never_echoes_the_cmc_key(self) -> None:
        secret = "fake-cmc-key-never-log"
        status = Settings(
            altcoin_contract_anomaly_cmc_api_key=secret,
        ).redacted_status()

        module = status["altcoin_contract_anomaly"]
        self.assertTrue(module["cmc_api_key_configured"])
        self.assertNotIn(secret, json.dumps(status, ensure_ascii=False))

    def test_example_keeps_the_cmc_key_blank(self) -> None:
        text = (BASE_DIR / "config" / ".env.oi.example").read_text(
            encoding="utf-8"
        )

        self.assertIn("ALTCOIN_CONTRACT_ANOMALY_ENABLE=false", text)
        self.assertIn("ALTCOIN_CONTRACT_ANOMALY_CMC_API_KEY=\n", text)
        self.assertNotIn("COINGLASS_ENABLE=", text)

    def test_explicit_p1_validation_rejects_raw_invalid_environment_value(self) -> None:
        with patch.dict(
            os.environ,
            {"ALTCOIN_CONTRACT_ANOMALY_CMC_BATCH_SIZE": "not-a-number"},
            clear=True,
        ):
            with self.assertRaises(AltcoinAnomalyConfigError) as caught:
                AltcoinAnomalyConfig.from_settings(
                    Settings(
                        altcoin_contract_anomaly_enable=True,
                        altcoin_contract_anomaly_cmc_api_key="fake-key",
                    )
                )

        self.assertIn("CMC_BATCH_SIZE", str(caught.exception))
        self.assertNotIn("not-a-number", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
