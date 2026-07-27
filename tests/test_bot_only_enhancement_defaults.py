from __future__ import annotations

import unittest

from paopao_radar.config import Settings


class BotOnlyEnhancementDefaultTests(unittest.TestCase):
    def test_all_new_enrichments_default_to_disabled(self) -> None:
        settings = Settings()
        self.assertFalse(settings.funding_flip_oi_enable)
        self.assertFalse(settings.accumulation_quality_v2_enable)
        self.assertFalse(settings.heat_context_enable)
        self.assertFalse(settings.binance_square_heat_enable)
        self.assertFalse(settings.announcement_enrichment_enable)
        self.assertFalse(settings.launch_outcome_v2_enable)
