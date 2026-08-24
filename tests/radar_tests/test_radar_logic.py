from __future__ import annotations

import unittest
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from config import Settings
from radars.common import (
    CST,
    funding_interval_transition,
    score_funding,
    score_mcap,
)
from runtime.radar_engine import RadarEngine
from shared.storage import JsonStore
from shared.time_windows import closed_window


class _FakeBudget:
    used = {"open_interest_hist": 1, "klines": 2}
    limits = {"open_interest_hist": 80, "klines": 120}


class _FakeQuality:
    failures: dict[str, int] = {}


class _FakeSource:
    budget = _FakeBudget()
    quality = _FakeQuality()


class _FakeAnnouncementSource:
    def __init__(self, articles: list[dict[str, object]], contract_bases: list[str]):
        self._articles = articles
        self._contract_bases = contract_bases

    def announcements(self, page_size: int = 20) -> list[dict[str, object]]:
        return self._articles[:page_size]

    def usdt_perp_symbols(self) -> list[dict[str, str]]:
        return [{"symbol": f"{base}USDT"} for base in self._contract_bases]


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

    def test_announcement_evidence_indexes_only_real_contract_symbols(self) -> None:
        with TemporaryDirectory() as tmp:
            today = datetime.now(CST).strftime("%Y-%m-%d")
            root = Path(tmp)
            engine = RadarEngine(
                Settings(data_dir=root, announcement_state_path=root / "announcement_state.json"),
                JsonStore(root),
            )
            source = _FakeAnnouncementSource(
                [{
                    "title": f"Binance Alpha Will Remove REX, XO, PHY ({today})",
                    "code": "risk-today",
                    "releaseDate": int(time.time() * 1000),
                }],
                ["REX", "PHY"],
            )

            result = engine.refresh_announcement_evidence(source)  # type: ignore[arg-type]
            state = engine.store.load(engine.settings.announcement_state_path, {})

            self.assertEqual(result["standalone_pushes"], 0)
            self.assertEqual(set(state["evidence_by_symbol"]), {"REXUSDT", "PHYUSDT"})
            self.assertNotIn("XOUSDT", state["evidence_by_symbol"])

    def test_announcement_skips_symbol_less_opportunity(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = RadarEngine(Settings(data_dir=Path(tmp)), JsonStore(Path(tmp)))
            source = _FakeAnnouncementSource(
                [{
                    "title": "Binance Wallet Launches Prediction Markets Trial Protection Campaign - Phase 2",
                    "code": "generic-campaign",
                    "releaseDate": int(time.time() * 1000),
                }],
                [],
            )

            result = engine.refresh_announcement_evidence(source)  # type: ignore[arg-type]

            self.assertEqual(result["evidence_count"], 0)

    def test_announcement_activity_keywords_with_symbol_are_opportunity(self) -> None:
        with TemporaryDirectory() as tmp:
            engine = RadarEngine(Settings(data_dir=Path(tmp)), JsonStore(Path(tmp)))
            source = _FakeAnnouncementSource(
                [{
                    "title": "Binance Launches ABC Trading Tournament With Token Vouchers and Rewards",
                    "code": "activity-abc",
                    "releaseDate": int(time.time() * 1000),
                }],
                ["ABC"],
            )

            result = engine.refresh_announcement_evidence(source)  # type: ignore[arg-type]
            state = engine.store.load(engine.settings.announcement_state_path, {})

            self.assertEqual(result["evidence_count"], 1)
            self.assertEqual(
                state["evidence_by_symbol"]["ABCUSDT"][0]["kind"],
                "opportunity",
            )

    def test_announcement_symbol_already_ending_in_usdt_is_not_duplicated(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(
                data_dir=root,
                announcement_state_path=root / "announcement_state.json",
            )
            engine = RadarEngine(settings, JsonStore(root))
            source = _FakeAnnouncementSource(
                [{
                    "title": "Binance Will List ABCUSDT",
                    "code": "listing-abc-usdt",
                    "releaseDate": int(time.time() * 1000),
                }],
                ["ABC"],
            )

            result = engine.refresh_announcement_evidence(source)  # type: ignore[arg-type]
            state = engine.store.load(settings.announcement_state_path, {})

            self.assertEqual(result["evidence_count"], 1)
            self.assertEqual(set(state["evidence_by_symbol"]), {"ABCUSDT"})

    def test_announcement_schema_preserves_release_alias(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = JsonStore(root)
            engine = RadarEngine(
                Settings(
                    data_dir=root,
                    announcement_state_path=root / "announcement_state.json",
                ),
                store,
            )
            release_ms = int(time.time() * 1000)
            source = _FakeAnnouncementSource(
                [{
                    "title": "Binance Will List ABC",
                    "code": "listing-abc",
                    "releaseDate": release_ms,
                }],
                ["ABC"],
            )

            article = source.announcements()[0]
            alert = engine._classify_announcement(article, {"ABC"})
            self.assertIsNotNone(alert)
            assert alert is not None

            self.assertEqual(alert["release_ts"], alert["announcement_release_ts"])
            self.assertEqual(alert["release_ts"], int(release_ms / 1000))
            for key in (
                "kind",
                "code",
                "title",
                "symbol",
                "symbols",
                "contract_symbols",
                "non_contract_symbols",
                "url",
                "release_ts",
                "expires_at",
                "priority",
                "reason",
            ):
                self.assertIn(key, alert)

            store.save(
                engine.settings.announcement_state_path,
                {"seen": {"legacy": {"message_ids": [123]}}},
            )
            engine.refresh_announcement_evidence(source)  # type: ignore[arg-type]
            saved = store.load(engine.settings.announcement_state_path, {})
            self.assertEqual(saved["seen"]["legacy"]["message_ids"], [123])
            self.assertEqual(
                saved["evidence_by_symbol"]["ABCUSDT"][0]["release_ts"],
                alert["release_ts"],
            )

    def test_announcement_skips_past_dated_article_after_reinstall(self) -> None:
        with TemporaryDirectory() as tmp:
            old_date = (datetime.now(CST) - timedelta(days=1)).strftime("%Y-%m-%d")
            engine = RadarEngine(Settings(data_dir=Path(tmp)), JsonStore(Path(tmp)))
            source = _FakeAnnouncementSource(
                [{
                    "title": f"Binance Alpha Will Remove OLD ({old_date})",
                    "code": "old-risk",
                    "releaseDate": int(time.time() * 1000),
                }],
                ["OLD"],
            )

            result = engine.refresh_announcement_evidence(source)  # type: ignore[arg-type]

            self.assertEqual(result["evidence_count"], 0)

    def test_announcement_refresh_failure_preserves_previous_evidence(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(
                data_dir=root,
                announcement_state_path=root / "announcement_state.json",
            )
            store = JsonStore(root)
            store.save(settings.announcement_state_path, {
                "evidence_updated_at": int(time.time()),
                "evidence_by_symbol": {"ABCUSDT": [{"expires_at": int(time.time()) + 60}]},
            })
            engine = RadarEngine(settings, store)
            source = _FakeAnnouncementSource([], [])

            result = engine.refresh_announcement_evidence(source)  # type: ignore[arg-type]
            state = store.load(settings.announcement_state_path, {})

            self.assertEqual(result["status"], "degraded")
            self.assertIn("ABCUSDT", state["evidence_by_symbol"])


class RadarScoringTests(unittest.TestCase):
    def test_negative_funding_scores_higher(self) -> None:
        self.assertGreater(score_funding(-0.5), score_funding(-0.01))
        self.assertEqual(score_funding(0.01), 0)

    def test_funding_interval_transition_detects_shorter_cycle(self) -> None:
        def ms_at(hour: int) -> int:
            return int(datetime(2026, 7, 1, hour, 0, 0, tzinfo=CST).timestamp() * 1000)

        transition = funding_interval_transition([
            {"fundingTime": ms_at(12), "fundingRate": "-0.001"},
            {"fundingTime": ms_at(16), "fundingRate": "-0.006"},
            {"fundingTime": ms_at(17), "fundingRate": "-0.020"},
        ])

        self.assertEqual(transition["previous_interval_hours"], 4)
        self.assertEqual(transition["current_interval_hours"], 1)
        self.assertIn("2026-07-01 16:00:00 4H结算一次", transition["transition_text"])
        self.assertIn("2026-07-01 17:00:00 1H结算一次", transition["transition_text"])

    def test_funding_interval_transition_detects_next_cycle_shortening(self) -> None:
        def ms_at(hour: int) -> int:
            return int(datetime(2026, 7, 1, hour, 0, 0, tzinfo=CST).timestamp() * 1000)

        transition = funding_interval_transition([
            {"fundingTime": ms_at(8), "fundingRate": "-0.001"},
            {"fundingTime": ms_at(12), "fundingRate": "-0.002"},
            {"fundingTime": ms_at(16), "fundingRate": "-0.004"},
        ], next_time_ms=ms_at(17))

        self.assertEqual(transition["previous_interval_hours"], 4)
        self.assertEqual(transition["current_interval_hours"], 1)
        self.assertIn("2026-07-01 16:00:00 4H结算一次", transition["transition_text"])
        self.assertIn("2026-07-01 17:00:00 1H结算一次", transition["transition_text"])

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
                "price_window": 4.8,
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
                closed_window(
                    now=datetime(2026, 5, 25, 22, 5, 0, tzinfo=timezone(timedelta(hours=8))),
                    interval_sec=21600,
                    delay_sec=300,
                ),
            )

            self.assertIn("<blockquote><b>📊 综合榜（评分=费率25 + 市值25 + 横盘25 + OI25）</b></blockquote>", text)
            self.assertIn('href="https://www.coinglass.com/tv/zh/Binance_TESTUSDT"', text)
            self.assertIn("<b>TEST</b>", text)
            self.assertIn("📋 <code>TESTUSDT</code>", text)
            self.assertIn('href="https://www.tradingview.com/chart/?symbol=BINANCE%3ATESTUSDT.P"', text)
            self.assertIn(">TV</a> · 📋 <code>TESTUSDT</code> · ", text)
            self.assertIn(">CG</a>\n 88分", text)
            self.assertNotIn("&nbsp;", text)
            self.assertNotIn("📖 图例", text)
            self.assertNotIn("负费率 = 空头拥挤，可能形成反向燃料", text)

    def test_signal_uses_binance_native_confirmation(self) -> None:
        item = {
            "score": 95,
            "oi_1h": 8.0,
            "price_1h": 6.0,
            "reasons": ["1h OI +8.0%"],
        }

        from shared.binance_confirmation import apply_binance_confirmation

        apply_binance_confirmation(
            item,
            {"价格": True, "OI": True, "成交量": True},
            scope="Binance USDⓈ-M Futures",
            window="15m闭合窗口",
            observed_at=1000,
        )

        self.assertEqual(item["score"], 95)
        self.assertEqual(item["quality_gate"], "allow")
        self.assertEqual(item["primary_data_source"], "binance_native")

    def test_summary_oi_is_labeled_as_binance_native(self) -> None:
        item = {"symbol": "BTCUSDT", "oi_6h": 8.0}

        from shared.binance_confirmation import apply_binance_confirmation

        apply_binance_confirmation(
            item,
            {"OI窗口": True},
            scope="Binance USDⓈ-M Futures",
            window="6h闭合窗口",
            observed_at=1000,
        )

        self.assertEqual(item["data_quality_status"], "confirmed")
        self.assertEqual(item["quality_gate"], "allow")
        self.assertTrue(RadarEngine._summary_oi_allowed(item))
        self.assertEqual(RadarEngine._summary_oi_quality_badge(item), "币安")

    def test_missing_market_cap_never_receives_small_cap_points(self) -> None:
        self.assertEqual(score_mcap(0), 0)
        self.assertEqual(score_mcap(-1), 0)

    def test_summary_oi_change_uses_window_start_not_prefetch_baseline(self) -> None:
        start_ms = 1_000_000
        end_ms = start_ms + 6 * 3_600_000
        rows = [
            {"timestamp": start_ms - 3_600_000, "sumOpenInterestValue": "100"},
            {"timestamp": start_ms, "sumOpenInterestValue": "110"},
            {"timestamp": end_ms, "sumOpenInterestValue": "132"},
        ]

        change, latest, ready = RadarEngine._oi_window_change(
            rows,
            start_ms=start_ms,
            end_ms=end_ms,
        )

        self.assertTrue(ready)
        self.assertEqual(latest["sumOpenInterestValue"], "132")
        self.assertAlmostEqual(change, 20.0)

if __name__ == "__main__":
    unittest.main()
