from __future__ import annotations

import unittest
from dataclasses import fields
from pathlib import Path
from tempfile import TemporaryDirectory

from config import BASE_DIR, Settings


class BotOnlyEnhancementDefaultTests(unittest.TestCase):
    def test_all_new_enrichments_default_to_disabled(self) -> None:
        settings = Settings()
        self.assertFalse(settings.funding_flip_oi_enable)
        self.assertFalse(settings.launch_outcome_v2_enable)
        self.assertFalse(settings.launch_fusion_enable)
        self.assertEqual(settings.launch_same_stage_min_interval_sec, 1800)
        self.assertFalse(settings.redacted_status()["launch"]["fusion_active"])

    def test_retired_configuration_is_absent_from_settings_and_status(self) -> None:
        settings = Settings()
        for field_name in (
            "signal_events_path",
            "flow_candidate_pool",
            "funding_alert_reply_chain_enable",
            "launch_multi_exchange_funding_enable",
            "coinglass_api_key",
            "coinalyze_api_key",
            "news_events_db_path",
            "heat_context_enable",
            "binance_square_heat_enable",
            "announcement_enrichment_enable",
            "flow_model_comparison_enable",
            "launch_smc_v4_enable",
            "accumulation_quality_v2_enable",
            "tg_auto_create_topics",
            "tg_topic_intro_enable",
        ):
            self.assertFalse(hasattr(settings, field_name), field_name)

        status = settings.redacted_status()
        self.assertNotIn("signal_events_file", status["bot_data"])
        self.assertNotIn("legacy_candidate_pool_ignored", status["flow_radar"])
        self.assertNotIn("reply_chain_enable", status["funding_alert"])
        self.assertNotIn("multi_exchange_funding_enable", status["launch"])
        self.assertNotIn("derivatives_validation", status)
        self.assertNotIn("heat_context_enable", status["radar"])
        self.assertNotIn("model_comparison_enable", status["flow_radar"])
        self.assertNotIn("enrichment_enable", status["announcement_risk"])
        self.assertTrue(status["announcement_risk"]["standalone_push"])
        self.assertTrue(status["announcement_risk"]["launch_supporting_evidence"])
        self.assertNotIn("smc_v4_enable", status["launch"])

    def test_custom_data_directory_contains_every_default_runtime_path(self) -> None:
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            settings = Settings(data_dir=data_dir)

            for field in fields(Settings):
                default = field.default
                if not isinstance(default, Path):
                    continue
                try:
                    relative = default.relative_to(BASE_DIR / "data")
                except ValueError:
                    continue
                self.assertEqual(getattr(settings, field.name), data_dir / relative)

    def test_default_and_explicit_external_runtime_paths_are_preserved(self) -> None:
        self.assertEqual(
            Settings().launch_state_path,
            BASE_DIR / "data" / "launch_state.json",
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            external = root / "operator-owned" / "outbox.json"
            settings = Settings(
                data_dir=root / "runtime",
                tg_outbox_path=external,
            )

            self.assertEqual(settings.tg_outbox_path, external)
