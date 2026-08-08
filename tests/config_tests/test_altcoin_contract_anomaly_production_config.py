from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from config import Settings
from radars.altcoin_contract_anomaly.configuration import (
    ALTCOIN_PRODUCTION_SEND_CONFIRMATION,
    AltcoinAnomalyConfigError,
    AltcoinAnomalyProductionConfig,
)


class AltcoinAnomalyProductionConfigTests(unittest.TestCase):
    def _settings(self, root: Path, **changes: object) -> Settings:
        base = Settings(
            data_dir=root,
            altcoin_contract_anomaly_enable=True,
            altcoin_contract_anomaly_cmc_api_key="fake-cmc-key",
            altcoin_contract_anomaly_realtime_enable=True,
            altcoin_contract_anomaly_production_enable=True,
        )
        return replace(base, **changes)

    def test_defaults_are_disabled_and_isolated(self) -> None:
        settings = Settings()

        self.assertFalse(settings.altcoin_contract_anomaly_production_enable)
        self.assertFalse(settings.altcoin_contract_anomaly_production_send_enable)
        self.assertEqual(
            settings.altcoin_contract_anomaly_production_manifest_refresh_sec,
            1800,
        )
        self.assertEqual(
            settings.altcoin_contract_anomaly_production_manifest_max_age_sec,
            2400,
        )
        self.assertEqual(
            settings.altcoin_contract_anomaly_production_oi_budget_window_sec,
            300,
        )
        paths = {
            settings.altcoin_contract_anomaly_production_observation_state_path,
            settings.altcoin_contract_anomaly_production_observation_event_path,
            settings.altcoin_contract_anomaly_production_state_path,
            settings.altcoin_contract_anomaly_production_outbox_path,
            settings.altcoin_contract_anomaly_production_status_path,
            settings.altcoin_contract_anomaly_realtime_lock_path,
            settings.altcoin_contract_anomaly_realtime_state_path,
            settings.altcoin_contract_anomaly_realtime_event_path,
        }
        self.assertEqual(len(paths), 8)

    def test_environment_values_load_and_confirmation_is_redacted(self) -> None:
        values = {
            "ALTCOIN_CONTRACT_ANOMALY_ENABLE": "true",
            "ALTCOIN_CONTRACT_ANOMALY_CMC_API_KEY": "fake-cmc-key",
            "ALTCOIN_CONTRACT_ANOMALY_REALTIME_ENABLE": "true",
            "ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_ENABLE": "true",
            "ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_SEND_ENABLE": "false",
            "ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_MANIFEST_REFRESH_SEC": "1800",
            "ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_MANIFEST_RETRY_SEC": "60",
            "ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_MANIFEST_MAX_AGE_SEC": "2400",
            "ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_OI_BUDGET_WINDOW_SEC": "600",
        }
        with patch.dict(os.environ, values, clear=True), patch(
            "config.settings.load_env_file",
            return_value={},
        ):
            settings = Settings.load()

        self.assertTrue(settings.altcoin_contract_anomaly_production_enable)
        self.assertEqual(
            settings.altcoin_contract_anomaly_production_manifest_refresh_sec,
            1800,
        )
        self.assertEqual(
            settings.altcoin_contract_anomaly_production_oi_budget_window_sec,
            600,
        )
        rendered = repr(settings.redacted_status())
        self.assertNotIn("fake-cmc-key", rendered)
        self.assertNotIn(ALTCOIN_PRODUCTION_SEND_CONFIRMATION, rendered)

    def test_production_requires_all_three_feature_gates_and_cmc_key(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cases = (
                replace(self._settings(root), altcoin_contract_anomaly_enable=False),
                replace(self._settings(root), altcoin_contract_anomaly_realtime_enable=False),
                replace(self._settings(root), altcoin_contract_anomaly_production_enable=False),
                replace(self._settings(root), altcoin_contract_anomaly_cmc_api_key=""),
            )
            for settings in cases:
                with self.subTest(settings=settings), self.assertRaises(
                    AltcoinAnomalyConfigError
                ):
                    AltcoinAnomalyProductionConfig.from_settings(settings)

    def test_real_send_requires_explicit_confirmation_and_prebuilt_topic(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base = self._settings(
                root,
                altcoin_contract_anomaly_production_send_enable=True,
                tg_bot_token="123:fake-token",
                tg_chat_id="-100123",
            )
            with self.assertRaisesRegex(AltcoinAnomalyConfigError, "确认短语"):
                AltcoinAnomalyProductionConfig.from_settings(
                    base,
                    real_send_requested=True,
                )

            confirmed = replace(
                base,
                altcoin_contract_anomaly_production_send_confirm=(
                    ALTCOIN_PRODUCTION_SEND_CONFIRMATION
                ),
            )
            with self.assertRaisesRegex(AltcoinAnomalyConfigError, "Topic ID"):
                AltcoinAnomalyProductionConfig.from_settings(
                    confirmed,
                    real_send_requested=True,
                )

            ready = replace(
                confirmed,
                tg_altcoin_contract_anomaly_topic_id="321",
            )
            config = AltcoinAnomalyProductionConfig.from_settings(
                ready,
                real_send_requested=True,
            )
            self.assertTrue(config.send_enabled)
            self.assertTrue(config.send_confirmed)

    def test_refresh_age_and_every_production_path_fail_closed(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base = self._settings(root)
            with self.assertRaisesRegex(AltcoinAnomalyConfigError, "刷新间隔"):
                AltcoinAnomalyProductionConfig.from_settings(
                    replace(
                        base,
                        altcoin_contract_anomaly_production_manifest_refresh_sec=2400,
                        altcoin_contract_anomaly_production_manifest_max_age_sec=2400,
                    )
                )

            protected = base.tg_outbox_path
            path_fields = (
                "altcoin_contract_anomaly_production_observation_state_path",
                "altcoin_contract_anomaly_production_observation_event_path",
                "altcoin_contract_anomaly_production_state_path",
                "altcoin_contract_anomaly_production_outbox_path",
                "altcoin_contract_anomaly_production_status_path",
                "altcoin_contract_anomaly_realtime_lock_path",
            )
            for field in path_fields:
                with self.subTest(field=field), self.assertRaises(
                    AltcoinAnomalyConfigError
                ):
                    AltcoinAnomalyProductionConfig.from_settings(
                        replace(base, **{field: protected})
                    )

            config = AltcoinAnomalyProductionConfig.from_settings(base)
            self.assertEqual(config.oi_budget_window_sec, 300)
            with self.assertRaisesRegex(
                AltcoinAnomalyConfigError,
                "five minutes",
            ):
                AltcoinAnomalyProductionConfig.from_settings(
                    replace(
                        base,
                        altcoin_contract_anomaly_production_oi_budget_window_sec=301,
                    )
                )
            self.assertNotEqual(config.preview_state_path, config.state_path)
            self.assertNotEqual(config.preview_outbox_path, config.outbox_path)
            self.assertNotEqual(config.preview_state_path, config.preview_outbox_path)
            for preview_path in (
                config.preview_state_path,
                config.preview_outbox_path,
            ):
                for protected_path in (
                    preview_path,
                    preview_path.with_name(f"{preview_path.name}.lock"),
                ):
                    with self.subTest(
                        protected_path=protected_path
                    ), self.assertRaises(AltcoinAnomalyConfigError):
                        AltcoinAnomalyProductionConfig.from_settings(
                            replace(base, tg_push_history_path=protected_path)
                        )


if __name__ == "__main__":
    unittest.main()
