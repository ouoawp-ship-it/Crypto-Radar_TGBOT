from __future__ import annotations

import unittest
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.config import Settings
from paopao_radar.radar import CST, RadarEngine, score_funding
from paopao_radar.storage import JsonStore
from paopao_radar.time_windows import closed_window


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

    def test_announcement_formats_each_symbol_and_skips_fake_links(self) -> None:
        with TemporaryDirectory() as tmp:
            today = datetime.now(CST).strftime("%Y-%m-%d")
            engine = RadarEngine(Settings(data_dir=Path(tmp)), JsonStore(Path(tmp)))
            source = _FakeAnnouncementSource(
                [{
                    "title": f"Binance Alpha Will Remove REX, XO, PHY ({today})",
                    "code": "risk-today",
                    "releaseDate": int(time.time() * 1000),
                }],
                ["REX", "PHY"],
            )

            result = engine.build_announcement_alerts(source)  # type: ignore[arg-type]
            text = result["messages"][0]

            self.assertIn('href="https://www.coinglass.com/tv/zh/Binance_REXUSDT"', text)
            self.assertIn('href="https://www.coinglass.com/tv/zh/Binance_PHYUSDT"', text)
            self.assertIn("<b>XO</b>（无合约）", text)
            self.assertNotIn("UNKNOWN", text)
            self.assertNotIn("Binance_REX,%20XO", text)

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

            result = engine.build_announcement_alerts(source)  # type: ignore[arg-type]

            self.assertEqual(result["messages"], [])
            self.assertEqual(result["alerts"], [])

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

            result = engine.build_announcement_alerts(source)  # type: ignore[arg-type]

            self.assertEqual(result["alerts"][0]["kind"], "opportunity")
            self.assertEqual(result["alerts"][0]["symbols"], ["ABC"])

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

            result = engine.build_announcement_alerts(source)  # type: ignore[arg-type]

            self.assertEqual(result["messages"], [])
            self.assertEqual(result["alerts"], [])

    def test_expired_announcement_cleanup_deletes_messages_and_state(self) -> None:
        with TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp))
            settings = Settings(data_dir=Path(tmp), announcement_state_path=Path(tmp) / "announcement_state.json")
            store.save(settings.announcement_state_path, {
                "seen": {
                    "expired": {
                        "title": "old",
                        "seen_at": int(time.time()),
                        "expires_at": int(time.time()) - 1,
                        "message_ids": [101, 102],
                    },
                    "active": {
                        "title": "new",
                        "seen_at": int(time.time()),
                        "expires_at": int(time.time()) + 3600,
                        "message_ids": [201],
                    },
                }
            })
            engine = RadarEngine(settings, store)
            deleted: list[int] = []

            result = engine.cleanup_expired_announcements(lambda ids: deleted.extend(ids) or len(ids))

            self.assertEqual(result, {"expired": 1, "deleted_messages": 2})
            self.assertEqual(deleted, [101, 102])
            state = store.load(settings.announcement_state_path, {})
            self.assertEqual(list(state["seen"].keys()), ["active"])

    def test_expired_announcement_cleanup_keeps_message_ids_until_real_delete(self) -> None:
        with TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp))
            settings = Settings(data_dir=Path(tmp), announcement_state_path=Path(tmp) / "announcement_state.json")
            store.save(settings.announcement_state_path, {
                "seen": {
                    "expired": {
                        "title": "old",
                        "seen_at": int(time.time()),
                        "expires_at": int(time.time()) - 1,
                        "message_ids": [101],
                    },
                }
            })
            engine = RadarEngine(settings, store)

            result = engine.cleanup_expired_announcements()

            self.assertEqual(result, {"expired": 0, "deleted_messages": 0})
            state = store.load(settings.announcement_state_path, {})
            self.assertTrue(state["seen"]["expired"]["delete_pending"])


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
            self.assertRegex(text, r"\d{2}-\d{2} \d{2}:\d{2} CST")

    def test_launch_alert_replies_to_previous_symbol_message(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), radar_min_quote_volume=1)
            store = JsonStore(Path(tmp))
            store.save(settings.launch_state_path, {
                "TESTUSDT": {
                    "stage": "primed",
                    "last_message_id": 123,
                    "last_pushed": 0,
                    "last_seen": int(time.time()),
                    "appear_count": 1,
                }
            })
            engine = RadarEngine(settings, store)

            def fake_analyze(_source: object, item: dict[str, object]) -> dict[str, object]:
                return {
                    **item,
                    "score": 95,
                    "price_15m": 5.0,
                    "price_1h": 8.0,
                    "oi_15m": 4.0,
                    "oi_1h": 8.0,
                    "volume_ratio": 2.5,
                    "breakout": True,
                    "reasons": ["测试"],
                }

            class Source:
                @staticmethod
                def ticker_24h() -> list[dict[str, str]]:
                    return [{
                        "symbol": "TESTUSDT",
                        "quoteVolume": "10000000",
                        "priceChangePercent": "10",
                        "lastPrice": "1",
                    }]

            engine._analyze_launch_symbol = fake_analyze  # type: ignore[method-assign]

            result = engine.build_launch_alerts(Source())  # type: ignore[arg-type]

            self.assertEqual(result["alerts"][0]["reply_to_message_id"], 123)

    def test_mark_launch_pushed_stores_message_id_for_reply_chain(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            store = JsonStore(Path(tmp))
            store.save(settings.launch_state_path, {"TESTUSDT": {"stage": "launched"}})
            engine = RadarEngine(settings, store)

            engine.mark_launch_pushed([{
                "symbol": "TESTUSDT",
                "stage": "launched",
                "message_ids": [456],
            }])

            state = store.load(settings.launch_state_path, {})
            self.assertEqual(state["TESTUSDT"]["last_message_id"], 456)
            self.assertEqual(state["TESTUSDT"]["last_message_ids"], [456])

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
