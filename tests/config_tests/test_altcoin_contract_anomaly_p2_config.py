from __future__ import annotations

import json
from math import ceil
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from config import BASE_DIR, Settings
from radars.altcoin_contract_anomaly.configuration import (
    AltcoinAnomalyConfig,
    AltcoinAnomalyConfigError,
)


P2_ENV_NAMES = (
    "ALTCOIN_CONTRACT_ANOMALY_REALTIME_ENABLE",
    "ALTCOIN_CONTRACT_ANOMALY_MANIFEST_POLL_SEC",
    "ALTCOIN_CONTRACT_ANOMALY_MANIFEST_MAX_AGE_SEC",
    "ALTCOIN_CONTRACT_ANOMALY_SUBSCRIPTION_BATCH_SIZE",
    "ALTCOIN_CONTRACT_ANOMALY_SUBSCRIPTION_MIN_INTERVAL_SEC",
    "ALTCOIN_CONTRACT_ANOMALY_SUBSCRIPTION_ACK_TIMEOUT_SEC",
    "ALTCOIN_CONTRACT_ANOMALY_MAX_STREAMS",
    "ALTCOIN_CONTRACT_ANOMALY_REALTIME_DATA_MAX_AGE_SEC",
    "ALTCOIN_CONTRACT_ANOMALY_FUNDING_MAX_GAP_SEC",
    "ALTCOIN_CONTRACT_ANOMALY_OI_REFRESH_SEC",
    "ALTCOIN_CONTRACT_ANOMALY_REALTIME_OI_MAX_AGE_SEC",
    "ALTCOIN_CONTRACT_ANOMALY_REALTIME_OI_WORKERS",
    "ALTCOIN_CONTRACT_ANOMALY_REALTIME_OI_REQUEST_BUDGET",
    "ALTCOIN_CONTRACT_ANOMALY_FEATURE_1M_WINDOW_SEC",
    "ALTCOIN_CONTRACT_ANOMALY_FEATURE_5M_WINDOW_SEC",
    "ALTCOIN_CONTRACT_ANOMALY_VOLUME_BASELINE_BUCKETS",
    "ALTCOIN_CONTRACT_ANOMALY_VOLUME_MIN_SAMPLES",
    "ALTCOIN_CONTRACT_ANOMALY_VOLUME_MIN_COVERAGE",
    "ALTCOIN_CONTRACT_ANOMALY_PRICE_1M_MOVE_RATIO",
    "ALTCOIN_CONTRACT_ANOMALY_PRICE_5M_MOVE_RATIO",
    "ALTCOIN_CONTRACT_ANOMALY_VOLUME_EXPANSION_RATIO",
    "ALTCOIN_CONTRACT_ANOMALY_AGGRESSIVE_BUY_RATIO",
    "ALTCOIN_CONTRACT_ANOMALY_AGGRESSIVE_SELL_RATIO",
    "ALTCOIN_CONTRACT_ANOMALY_OPEN_INTEREST_MOVE_RATIO",
    "ALTCOIN_CONTRACT_ANOMALY_FUNDING_POSITIVE_RATE",
    "ALTCOIN_CONTRACT_ANOMALY_FUNDING_CHANGE_RATIO",
    "ALTCOIN_CONTRACT_ANOMALY_LIQUIDATION_MIN_USD",
    "ALTCOIN_CONTRACT_ANOMALY_PRICE_STALL_RATIO",
    "ALTCOIN_CONTRACT_ANOMALY_WEAKENING_VOLUME_RATIO",
    "ALTCOIN_CONTRACT_ANOMALY_WEAKENING_WINDOWS",
    "ALTCOIN_CONTRACT_ANOMALY_REALTIME_STATE_FILE",
    "ALTCOIN_CONTRACT_ANOMALY_REALTIME_EVENT_FILE",
    "ALTCOIN_CONTRACT_ANOMALY_SMOKE_DURATION_SEC",
)


class AltcoinContractAnomalyP2ConfigTests(unittest.TestCase):
    def test_defaults_are_complete_disabled_and_isolated(self) -> None:
        settings = Settings()

        self.assertFalse(settings.altcoin_contract_anomaly_realtime_enable)
        expected = {
            "manifest_poll_sec": 5,
            "manifest_max_age_sec": 1200,
            "subscription_batch_size": 50,
            "subscription_min_interval_sec": 1.0,
            "subscription_ack_timeout_sec": 10,
            "max_streams": 300,
            "realtime_data_max_age_sec": 120,
            "funding_max_gap_sec": 15,
            "oi_refresh_sec": 300,
            "realtime_oi_max_age_sec": 600,
            "realtime_oi_workers": 4,
            "realtime_oi_request_budget": 50,
            "feature_1m_window_sec": 60,
            "feature_5m_window_sec": 300,
            "volume_baseline_buckets": 5,
            "volume_min_samples": 4,
            "volume_min_coverage": 0.8,
            "price_1m_move_ratio": 0.01,
            "price_5m_move_ratio": 0.02,
            "volume_expansion_ratio": 2.0,
            "aggressive_buy_ratio": 0.60,
            "aggressive_sell_ratio": 0.40,
            "open_interest_move_ratio": 0.03,
            "funding_positive_rate": 0.0005,
            "funding_change_ratio": 0.0001,
            "liquidation_min_usd": 100_000,
            "price_stall_ratio": 0.003,
            "weakening_volume_ratio": 1.2,
            "weakening_windows": 2,
            "smoke_duration_sec": 900,
        }
        for suffix, value in expected.items():
            self.assertEqual(
                getattr(settings, f"altcoin_contract_anomaly_{suffix}"),
                value,
                suffix,
            )
        self.assertEqual(
            settings.altcoin_contract_anomaly_realtime_state_path,
            BASE_DIR / "data" / "altcoin_contract_anomaly_p2_state.json",
        )
        self.assertEqual(
            settings.altcoin_contract_anomaly_realtime_event_path,
            BASE_DIR / "data" / "altcoin_contract_anomaly_p2_events.jsonl",
        )

    def test_all_environment_values_load_through_central_settings(self) -> None:
        values = {
            "ALTCOIN_CONTRACT_ANOMALY_ENABLE": "true",
            "ALTCOIN_CONTRACT_ANOMALY_CMC_API_KEY": "fake-key",
            "ALTCOIN_CONTRACT_ANOMALY_REALTIME_ENABLE": "true",
            "ALTCOIN_CONTRACT_ANOMALY_MANIFEST_POLL_SEC": "7",
            "ALTCOIN_CONTRACT_ANOMALY_MANIFEST_MAX_AGE_SEC": "2000",
            "ALTCOIN_CONTRACT_ANOMALY_SUBSCRIPTION_BATCH_SIZE": "40",
            "ALTCOIN_CONTRACT_ANOMALY_SUBSCRIPTION_MIN_INTERVAL_SEC": "0.5",
            "ALTCOIN_CONTRACT_ANOMALY_SUBSCRIPTION_ACK_TIMEOUT_SEC": "20",
            "ALTCOIN_CONTRACT_ANOMALY_MAX_STREAMS": "200",
            "ALTCOIN_CONTRACT_ANOMALY_REALTIME_DATA_MAX_AGE_SEC": "90",
            "ALTCOIN_CONTRACT_ANOMALY_FUNDING_MAX_GAP_SEC": "20",
            "ALTCOIN_CONTRACT_ANOMALY_OI_REFRESH_SEC": "300",
            "ALTCOIN_CONTRACT_ANOMALY_REALTIME_OI_MAX_AGE_SEC": "720",
            "ALTCOIN_CONTRACT_ANOMALY_REALTIME_OI_WORKERS": "3",
            "ALTCOIN_CONTRACT_ANOMALY_REALTIME_OI_REQUEST_BUDGET": "40",
            "ALTCOIN_CONTRACT_ANOMALY_FEATURE_1M_WINDOW_SEC": "60",
            "ALTCOIN_CONTRACT_ANOMALY_FEATURE_5M_WINDOW_SEC": "300",
            "ALTCOIN_CONTRACT_ANOMALY_VOLUME_BASELINE_BUCKETS": "30",
            "ALTCOIN_CONTRACT_ANOMALY_VOLUME_MIN_SAMPLES": "12",
            "ALTCOIN_CONTRACT_ANOMALY_VOLUME_MIN_COVERAGE": "0.9",
            "ALTCOIN_CONTRACT_ANOMALY_PRICE_1M_MOVE_RATIO": "0.015",
            "ALTCOIN_CONTRACT_ANOMALY_PRICE_5M_MOVE_RATIO": "0.025",
            "ALTCOIN_CONTRACT_ANOMALY_VOLUME_EXPANSION_RATIO": "2.5",
            "ALTCOIN_CONTRACT_ANOMALY_AGGRESSIVE_BUY_RATIO": "0.65",
            "ALTCOIN_CONTRACT_ANOMALY_AGGRESSIVE_SELL_RATIO": "0.35",
            "ALTCOIN_CONTRACT_ANOMALY_OPEN_INTEREST_MOVE_RATIO": "0.04",
            "ALTCOIN_CONTRACT_ANOMALY_FUNDING_POSITIVE_RATE": "0.0006",
            "ALTCOIN_CONTRACT_ANOMALY_FUNDING_CHANGE_RATIO": "0.0002",
            "ALTCOIN_CONTRACT_ANOMALY_LIQUIDATION_MIN_USD": "200000",
            "ALTCOIN_CONTRACT_ANOMALY_PRICE_STALL_RATIO": "0.004",
            "ALTCOIN_CONTRACT_ANOMALY_WEAKENING_VOLUME_RATIO": "1.1",
            "ALTCOIN_CONTRACT_ANOMALY_WEAKENING_WINDOWS": "3",
            "ALTCOIN_CONTRACT_ANOMALY_REALTIME_STATE_FILE": "p2-state-test.json",
            "ALTCOIN_CONTRACT_ANOMALY_REALTIME_EVENT_FILE": "p2-events-test.jsonl",
            "ALTCOIN_CONTRACT_ANOMALY_SMOKE_DURATION_SEC": "1200",
        }
        with patch.dict(os.environ, values, clear=True), patch(
            "config.settings.load_env_file",
            return_value={},
        ):
            settings = Settings.load()
            config = AltcoinAnomalyConfig.from_settings(settings, realtime=True)

        self.assertTrue(config.realtime_enabled)
        self.assertEqual(config.manifest_poll_sec, 7)
        self.assertEqual(config.subscription_min_interval_sec, 0.5)
        self.assertEqual(config.max_streams, 200)
        self.assertEqual(config.funding_max_gap_sec, 20)
        self.assertEqual(config.feature_1m_window_sec, 60)
        self.assertEqual(config.feature_5m_window_sec, 300)
        self.assertEqual(config.volume_min_samples, 12)
        self.assertEqual(config.aggressive_buy_ratio, 0.65)
        self.assertEqual(config.liquidation_min_usd, 200_000)
        self.assertEqual(config.weakening_windows, 3)
        self.assertEqual(config.realtime_state_path.name, "p2-state-test.json")
        self.assertEqual(config.realtime_event_path.name, "p2-events-test.jsonl")
        self.assertEqual(config.smoke_duration_sec, 1200)

    def test_default_volume_baseline_can_complete_during_cold_start_smoke(self) -> None:
        settings = Settings()
        required = max(
            settings.altcoin_contract_anomaly_volume_min_samples,
            ceil(
                settings.altcoin_contract_anomaly_volume_baseline_buckets
                * settings.altcoin_contract_anomaly_volume_min_coverage
            ),
        )

        self.assertEqual(required, 4)
        self.assertLessEqual(
            settings.altcoin_contract_anomaly_volume_baseline_buckets,
            5,
            "12到15分钟冷启动至少应能形成当前订阅代次的前置闭合桶窗口",
        )

    def test_realtime_gate_and_raw_values_are_strict(self) -> None:
        base = Settings(
            altcoin_contract_anomaly_enable=True,
            altcoin_contract_anomaly_cmc_api_key="fake-key",
        )
        with self.assertRaisesRegex(AltcoinAnomalyConfigError, "P2实时确认未启用"):
            AltcoinAnomalyConfig.from_settings(base, realtime=True)

        invalid_values = (
            ("ALTCOIN_CONTRACT_ANOMALY_REALTIME_ENABLE", "maybe"),
            ("ALTCOIN_CONTRACT_ANOMALY_MAX_STREAMS", "0"),
            ("ALTCOIN_CONTRACT_ANOMALY_OI_REFRESH_SEC", "180"),
            ("ALTCOIN_CONTRACT_ANOMALY_VOLUME_MIN_COVERAGE", "nan"),
        )
        for name, value in invalid_values:
            with self.subTest(name=name), patch.dict(
                os.environ,
                {name: value},
                clear=True,
            ):
                with self.assertRaises(AltcoinAnomalyConfigError) as caught:
                    AltcoinAnomalyConfig.from_settings(base)
            self.assertIn(name, str(caught.exception))
            self.assertNotIn(value, str(caught.exception))

    def test_realtime_mode_consumes_manifest_without_requiring_cmc_key(self) -> None:
        settings = Settings(
            altcoin_contract_anomaly_enable=True,
            altcoin_contract_anomaly_realtime_enable=True,
            altcoin_contract_anomaly_cmc_api_key="",
        )

        with patch.dict(os.environ, {}, clear=True):
            config = AltcoinAnomalyConfig.from_settings(settings, realtime=True)
            with self.assertRaisesRegex(AltcoinAnomalyConfigError, "CMC API Key"):
                AltcoinAnomalyConfig.from_settings(settings)

        self.assertTrue(config.realtime_enabled)
        self.assertEqual(config.cmc_api_key, "")

    def test_cross_field_validation_rejects_incoherent_windows(self) -> None:
        settings = Settings(
            altcoin_contract_anomaly_volume_baseline_buckets=20,
            altcoin_contract_anomaly_volume_min_samples=21,
        )

        with self.assertRaisesRegex(AltcoinAnomalyConfigError, "样本数"):
            AltcoinAnomalyConfig.from_settings(settings, cache_only=True)

        unsupported_bucket = Settings(
            altcoin_contract_anomaly_feature_1m_window_sec=45,
            altcoin_contract_anomaly_feature_5m_window_sec=225,
        )
        with self.assertRaisesRegex(AltcoinAnomalyConfigError, "60秒"):
            AltcoinAnomalyConfig.from_settings(
                unsupported_bucket,
                cache_only=True,
            )

    def test_manifest_freshness_cannot_expire_before_smoke_finishes(self) -> None:
        settings = Settings(
            altcoin_contract_anomaly_manifest_max_age_sec=899,
            altcoin_contract_anomaly_smoke_duration_sec=900,
        )

        with self.assertRaisesRegex(AltcoinAnomalyConfigError, "不能短于"):
            AltcoinAnomalyConfig.from_settings(settings, cache_only=True)

    def test_realtime_state_paths_cannot_overwrite_existing_runtime_files(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            sentinel = root / "tg_outbox.json"
            sentinel.write_text('{"production":"keep"}', encoding="utf-8")
            settings = Settings(
                altcoin_contract_anomaly_enable=True,
                altcoin_contract_anomaly_realtime_enable=True,
                tg_outbox_path=sentinel,
                altcoin_contract_anomaly_realtime_state_path=sentinel,
                altcoin_contract_anomaly_realtime_event_path=root / "p2-events.jsonl",
            )

            with self.assertRaisesRegex(AltcoinAnomalyConfigError, "tg_outbox_path"):
                AltcoinAnomalyConfig.from_settings(settings, realtime=True)

            self.assertEqual(
                sentinel.read_text(encoding="utf-8"),
                '{"production":"keep"}',
            )

    def test_realtime_state_paths_require_isolated_file_types(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            common = {
                "altcoin_contract_anomaly_enable": True,
                "altcoin_contract_anomaly_realtime_enable": True,
            }
            invalid = (
                Settings(
                    **common,
                    altcoin_contract_anomaly_realtime_state_path=root / "state.db",
                    altcoin_contract_anomaly_realtime_event_path=root / "events.jsonl",
                ),
                Settings(
                    **common,
                    altcoin_contract_anomaly_realtime_state_path=root / "state.json",
                    altcoin_contract_anomaly_realtime_event_path=root / "events.json",
                ),
            )

            for settings in invalid:
                with self.subTest(settings=settings), self.assertRaises(
                    AltcoinAnomalyConfigError
                ):
                    AltcoinAnomalyConfig.from_settings(settings, realtime=True)

    def test_redacted_status_and_example_expose_only_safe_p2_configuration(self) -> None:
        secret = "fake-cmc-key-never-log"
        status = Settings(
            altcoin_contract_anomaly_cmc_api_key=secret,
        ).redacted_status()
        realtime = status["altcoin_contract_anomaly"]["realtime"]

        self.assertFalse(realtime["enabled"])
        self.assertEqual(realtime["max_streams"], 300)
        self.assertEqual(realtime["smoke_duration_sec"], 900)
        self.assertNotIn(secret, json.dumps(status, ensure_ascii=False))

        example = (BASE_DIR / "config" / ".env.oi.example").read_text(
            encoding="utf-8"
        )
        for name in P2_ENV_NAMES:
            self.assertIn(f"{name}=", example, name)
        self.assertIn("ALTCOIN_CONTRACT_ANOMALY_REALTIME_ENABLE=false", example)
        self.assertIn("ALTCOIN_CONTRACT_ANOMALY_CMC_API_KEY=\n", example)
        self.assertNotIn("COINGLASS_ENABLE=", example)
        gitignore = (BASE_DIR / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("data/*.jsonl", gitignore)


if __name__ == "__main__":
    unittest.main()
