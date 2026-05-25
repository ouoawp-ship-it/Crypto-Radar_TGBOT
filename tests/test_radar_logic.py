from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.config import Settings
from paopao_radar.radar import RadarEngine, score_funding
from paopao_radar.storage import JsonStore


class _FakeBudget:
    used = {"open_interest_hist": 1, "klines": 2}
    limits = {"open_interest_hist": 80, "klines": 120}


class _FakeQuality:
    failures: dict[str, int] = {}


class _FakeSource:
    budget = _FakeBudget()
    quality = _FakeQuality()


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

    def test_summary_uses_html_links_quotes_and_score_notes(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = RadarEngine(Settings(data_dir=Path(tmp)), JsonStore(Path(tmp)))
            item = {
                "symbol": "TESTUSDT",
                "coin": "TEST",
                "funding_pct": -0.12,
                "funding_trend": "🔥加速",
                "price_24h": 5.2,
                "mcap": 42_000_000,
                "price": 0.1234,
                "combined_score": 88,
                "ambush_score": 80,
                "momentum_score": 70,
                "new_score": 65,
                "sideways_days": 96,
                "oi_6h": 12.3,
                "quote_volume": 55_000_000,
                "history_days": 12,
                "divergence": 7.1,
                "level": "🟡中",
                "status_text": "🆕 首次出现",
            }

            text = engine._format_summary(
                "05-25 22:00 CST",
                [item],
                [item],
                [item],
                [item],
                [item],
                [item],
                [item],
                _FakeSource(),
                {"first": 1, "continued": 0, "enhanced": 0, "reappeared": 0},
            )

            self.assertIn("<blockquote><b>📊 综合榜（评分=费率25 + 市值25 + 横盘25 + OI25）</b></blockquote>", text)
            self.assertIn('href="https://www.coinglass.com/tv/Binance_TESTUSDT"', text)
            self.assertIn("<b>TEST</b>", text)
            self.assertIn("</a>\n 88分", text)
            self.assertNotIn("<code>", text)
            self.assertNotIn("&nbsp;", text)
            self.assertIn("链接 = 点击币种打开 CoinGlass Binance K线", text)

    def test_launch_alert_translates_state_and_explains_score(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = RadarEngine(Settings(data_dir=Path(tmp)), JsonStore(Path(tmp)))
            text = engine._format_launch_alert({
                "symbol": "TESTUSDT",
                "coin": "TEST",
                "stage": "primed",
                "previous_stage": "idle",
                "score": 63,
                "appear_count": 2,
                "price_15m": 4.5,
                "price_1h": 6.0,
                "oi_15m": 3.2,
                "oi_1h": 6.8,
                "volume_ratio": 2.4,
                "breakout": False,
            })

            self.assertIn("状态</b>: 未触发 -> 提前预警", text)
            self.assertIn("分数图例", text)
            self.assertIn("05-", text)
            self.assertIn("CST", text)

    def test_risk_announcement_uses_chinese_state(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = RadarEngine(Settings(data_dir=Path(tmp)), JsonStore(Path(tmp)))
            text = engine._format_announcement({
                "kind": "risk",
                "symbol": "TEST",
                "title": "Binance Will Delist TEST",
                "url": "https://www.binance.com/example",
            })

            self.assertIn("风险提醒", text)
            self.assertIn("标记为 风险", text)
            self.assertNotIn("risk", text)


if __name__ == "__main__":
    unittest.main()
