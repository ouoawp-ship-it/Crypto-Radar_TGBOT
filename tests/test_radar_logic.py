from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.config import Settings
from paopao_radar.radar import RadarEngine, score_funding
from paopao_radar.storage import JsonStore


class RadarAnnouncementTests(unittest.TestCase):
    def test_extracts_multiple_listing_symbols(self) -> None:
        title = "Binance Will List Genius Terminal (GENIUS) and OpenGradient (OPG) with Seed Tag Applied"

        self.assertEqual(RadarEngine._extract_symbols(title), ["GENIUS", "OPG"])

    def test_chain_context_parentheses_are_not_reported_as_symbols(self) -> None:
        title = (
            "Binance Alpha Will Remove REX, XO, Ghibli (SOL), "
            "Ghibli (BSC), PHY (2026-04-30)"
        )

        self.assertEqual(RadarEngine._extract_symbols(title), ["REX", "XO", "PHY"])

    def test_real_token_parentheses_are_kept(self) -> None:
        self.assertEqual(RadarEngine._extract_symbols("Binance Will List Solana (SOL)"), ["SOL"])


class RadarScoringTests(unittest.TestCase):
    def test_launch_stage_thresholds(self) -> None:
        self.assertEqual(RadarEngine.launch_stage_for_score(44), "idle")
        self.assertEqual(RadarEngine.launch_stage_for_score(45), "watching")
        self.assertEqual(RadarEngine.launch_stage_for_score(60), "primed")
        self.assertEqual(RadarEngine.launch_stage_for_score(75), "breakout")
        self.assertEqual(RadarEngine.launch_stage_for_score(90), "launched")

    def test_launch_stage_thresholds_are_configurable(self) -> None:
        self.assertEqual(
            RadarEngine.launch_stage_for_score(70, watching=30, primed=50, breakout=70, launched=85),
            "breakout",
        )
        self.assertEqual(
            RadarEngine.launch_stage_for_score(84, watching=30, primed=50, breakout=70, launched=85),
            "breakout",
        )
        self.assertEqual(
            RadarEngine.launch_stage_for_score(85, watching=30, primed=50, breakout=70, launched=85),
            "launched",
        )

    def test_negative_funding_scores_higher(self) -> None:
        self.assertGreater(score_funding(-0.5), score_funding(-0.01))
        self.assertEqual(score_funding(0.01), 0)

    def test_excluded_base_assets_filter_non_crypto_symbols(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), excluded_base_assets=("XAU", "XAG"))
            engine = RadarEngine(settings, JsonStore(Path(tmp)))

            self.assertTrue(engine._is_excluded_symbol("XAUUSDT"))
            self.assertTrue(engine._is_excluded_symbol("XAGUSDT"))
            self.assertFalse(engine._is_excluded_symbol("BTCUSDT"))


if __name__ == "__main__":
    unittest.main()
