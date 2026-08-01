from __future__ import annotations

import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.binance_confirmation import apply_binance_confirmation
from paopao_radar.config import Settings
from paopao_radar.flow_candidates import build_candidate_list, format_candidate_list
from paopao_radar.flow_radar import (
    coinglass_tv_url,
    binance_oi_stats,
    FlowRadarEngine,
    flow_category,
    flow_classification,
    flow_net_ratio_pct,
    fmt_cvd,
    kline_cvd_delta_info,
    kline_cvd_flow_info,
    series_delta_info,
)
from paopao_radar.storage import JsonStore
from paopao_radar.time_windows import ClosedWindow, closed_window


class FlowRadarTests(unittest.TestCase):
    def test_coin_link_defaults_to_coinglass_binance_tv_page(self) -> None:
        self.assertEqual(
            coinglass_tv_url("BTC"),
            "https://www.coinglass.com/tv/zh/Binance_BTCUSDT",
        )

    def test_series_delta_reports_missing_data(self) -> None:
        delta, ready, count = series_delta_info({"data": [{"cvd": 100}]})

        self.assertEqual(delta, 0.0)
        self.assertFalse(ready)
        self.assertEqual(count, 1)

    def test_binance_oi_stats_calculates_fallback_change(self) -> None:
        class Source:
            def open_interest_hist(
                self,
                symbol: str,
                period: str = "1h",
                limit: int = 25,
                start_time: int | None = None,
                end_time: int | None = None,
            ):
                self.args = (symbol, period, limit, start_time, end_time)
                return [
                    {"sumOpenInterestValue": "100"},
                    {"sumOpenInterestValue": "115"},
                ]

        source = Source()
        pct, last, ready, points = binance_oi_stats(source, "BTCUSDT")

        self.assertEqual(source.args, ("BTCUSDT", "1h", 25, None, None))
        self.assertEqual(pct, 15.0)
        self.assertEqual(last, 115.0)
        self.assertTrue(ready)
        self.assertEqual(points, 2)

    def test_binance_oi_stats_uses_exact_closed_window(self) -> None:
        start_ms = 1_771_965_600_000
        end_ms = start_ms + 3_600_000

        class Source:
            def open_interest_hist(
                self,
                symbol: str,
                period: str = "1h",
                limit: int = 25,
                start_time: int | None = None,
                end_time: int | None = None,
            ):
                self.args = (symbol, period, limit, start_time, end_time)
                return [
                    {"timestamp": start_ms - 3_600_000, "sumOpenInterestValue": "100"},
                    {"timestamp": start_ms, "sumOpenInterestValue": "110"},
                    {"timestamp": end_ms, "sumOpenInterestValue": "132"},
                ]

        window = ClosedWindow(
            start=datetime.fromtimestamp(start_ms / 1000, timezone.utc),
            end=datetime.fromtimestamp(end_ms / 1000, timezone.utc),
            interval_sec=3600,
            delay_sec=300,
        )
        source = Source()
        change, last, ready, points = binance_oi_stats(source, "BTCUSDT", window=window)

        self.assertEqual(source.args[3:], (start_ms, end_ms))
        self.assertTrue(ready)
        self.assertEqual(points, 2)
        self.assertEqual(last, 132.0)
        self.assertAlmostEqual(change, 20.0)

    def test_series_delta_filters_to_closed_window_timestamps(self) -> None:
        data = {
            "data": [
                {"time": 1_771_965_600_000, "cvd": 10},
                {"time": 1_771_969_200_000, "cvd": 30},
                {"time": 1_771_972_800_000, "cvd": 99},
            ]
        }

        delta, ready, points = series_delta_info(
            data,
            start_ms=1_771_965_600_000,
            end_ms=1_771_969_200_000,
        )

        self.assertEqual(delta, 20.0)
        self.assertTrue(ready)
        self.assertEqual(points, 2)

    def test_kline_cvd_uses_taker_buy_quote_volume(self) -> None:
        klines = [
            [
                1_771_965_600_000,
                "1",
                "1",
                "1",
                "1",
                "100",
                1_771_969_199_999,
                "1000",
                10,
                "55",
                "650",
                "0",
            ]
        ]

        delta, ready, points = kline_cvd_delta_info(klines)

        self.assertEqual(delta, 300.0)
        self.assertTrue(ready)
        self.assertEqual(points, 1)

        gross_delta, inflow, outflow, gross_ready, gross_points = kline_cvd_flow_info(klines)
        self.assertEqual(gross_delta, 300.0)
        self.assertEqual(inflow, 650.0)
        self.assertEqual(outflow, 350.0)
        self.assertTrue(gross_ready)
        self.assertEqual(gross_points, 1)

    def test_candidate_symbols_keeps_binance_funding_percent_once(self) -> None:
        class Source:
            def usdt_perp_symbols(self):
                return [{"symbol": "BTCUSDT"}]

            def premium_index(self):
                return [{"symbol": "BTCUSDT", "lastFundingRate": "0.0001"}]

            def ticker_24h(self):
                return [{"symbol": "BTCUSDT", "quoteVolume": "10000000", "priceChangePercent": "2"}]

        candidates = FlowRadarEngine(Settings(radar_min_quote_volume=1))._candidate_symbols(Source())

        self.assertEqual(candidates[0]["funding_pct"], 0.01)

    def test_closed_window_waits_for_delay_before_using_latest_hour(self) -> None:
        from datetime import datetime, timedelta, timezone

        window = closed_window(
            now=datetime(2026, 5, 26, 18, 4, 0, tzinfo=timezone(timedelta(hours=8))),
            interval_sec=3600,
            delay_sec=300,
        )

        self.assertEqual(window.label(), "05-26 16:00-17:00 CST")

    def test_push_body_excludes_static_legend_and_calculation_copy(self) -> None:
        window = closed_window(
            now=datetime(2026, 5, 26, 18, 5, 0, tzinfo=timezone.utc),
            interval_sec=3600,
            delay_sec=300,
        )

        text = FlowRadarEngine(Settings())._format([], [], [], window)

        self.assertIn("本轮统计", text)
        self.assertIn("全市场候选: 0（无固定数量上限）", text)
        self.assertIn("本轮优先轮换: 0/0", text)
        self.assertNotIn("📖 图例", text)
        self.assertNotIn("📐 数据与计算口径", text)
        self.assertNotIn("市场边界: 仅代表 Binance", text)
        self.assertNotIn("数据规则: 整点收线后延迟", text)
        self.assertNotIn("主动成交净额 = taker主动买入报价额", text)

    def test_true_launch_category_scores_multi_factor_confirmation(self) -> None:
        category, score, _reason = flow_category({
            "price_24h": 6.0,
            "oi_24h": 8.0,
            "spot_cvd_delta": 1_000_000,
            "spot_inflow_usd": 3_000_000,
            "spot_outflow_usd": 2_000_000,
            "futures_cvd_delta": 800_000,
            "futures_inflow_usd": 2_400_000,
            "futures_outflow_usd": 1_600_000,
            "funding_pct": 0.02,
            "quote_volume": 80_000_000,
        })

        self.assertEqual(category, "真启动候选")
        self.assertGreaterEqual(score, 90)

    def test_neutral_cvd_does_not_trigger_distribution(self) -> None:
        category, _score, _reason = flow_category({
            "price_24h": -1.0,
            "oi_24h": -1.0,
            "spot_cvd_delta": 0.0,
            "futures_cvd_delta": 0.0,
            "spot_cvd_ready": True,
            "futures_cvd_ready": True,
            "funding_pct": 0.2,
            "quote_volume": 100_000_000,
        })

        self.assertNotEqual(category, "诱多/派发")

    def test_fmt_cvd_distinguishes_missing_neutral_and_signed_values(self) -> None:
        self.assertEqual(fmt_cvd(0.0, True), "近0")
        self.assertEqual(fmt_cvd(0.25, True), "近0")
        self.assertEqual(fmt_cvd(1_250_000, True), "+$1.2M")
        self.assertEqual(fmt_cvd(-2_500, True), "-$2.5K")
        self.assertEqual(fmt_cvd(0.0, False), "缺失")

    def test_missing_cvd_does_not_create_fake_distribution_signal(self) -> None:
        category, score, reason = flow_category({
            "price_24h": 20.0,
            "oi_24h": 0.0,
            "spot_cvd_delta": 0.0,
            "futures_cvd_delta": 0.0,
            "spot_cvd_ready": False,
            "futures_cvd_ready": False,
            "funding_pct": 0.1,
            "quote_volume": 100_000_000,
        })

        self.assertEqual(category, "数据不足")
        self.assertEqual(score, 0)
        self.assertIn("Binance 主动成交数据缺失", reason)

    def test_missing_funding_is_not_treated_as_zero(self) -> None:
        category, score, reason = flow_category({
            "price_24h": 6.0,
            "oi_24h": 8.0,
            "spot_cvd_delta": 1_000_000,
            "futures_cvd_delta": 800_000,
            "funding_pct": 0.0,
            "funding_ready": False,
            "quote_volume": 80_000_000,
        })

        self.assertEqual(category, "数据不足")
        self.assertEqual(score, 0)
        self.assertIn("资金费率缺失", reason)

    def test_active_net_ratio_requires_absolute_and_relative_thresholds(self) -> None:
        settings = Settings(
            flow_spot_net_min_usd=10_000,
            flow_spot_net_ratio_min_pct=3,
            flow_futures_net_min_usd=25_000,
            flow_futures_net_ratio_min_pct=2,
        )
        low_absolute = self._flow_item(
            spot_cvd_delta=5_000,
            spot_inflow_usd=52_500,
            spot_outflow_usd=47_500,
            futures_cvd_delta=0,
            futures_inflow_usd=500_000,
            futures_outflow_usd=500_000,
        )
        low_ratio = self._flow_item(
            spot_cvd_delta=20_000,
            spot_inflow_usd=510_000,
            spot_outflow_usd=490_000,
            futures_cvd_delta=0,
            futures_inflow_usd=500_000,
            futures_outflow_usd=500_000,
        )

        self.assertFalse(flow_classification(low_absolute, settings)["eligible"])
        self.assertFalse(flow_classification(low_ratio, settings)["eligible"])
        self.assertAlmostEqual(flow_net_ratio_pct(20_000, 510_000, 490_000), 2.0)

    def test_p0_categories_require_complete_core_gates(self) -> None:
        for missing_field in (
            "price_ready",
            "oi_ready",
            "spot_cvd_ready",
            "futures_cvd_ready",
            "funding_ready",
        ):
            with self.subTest(missing_field=missing_field):
                item = self._flow_item()
                item[missing_field] = False
                result = flow_classification(item)
                self.assertEqual(result["category"], "数据不足")
                self.assertFalse(result["eligible"])

    def test_p0_strict_category_fixtures(self) -> None:
        cases = {
            "真启动候选": self._flow_item(),
            "吸筹观察": self._flow_item(
                price_24h=0.4,
                futures_cvd_delta=0,
                futures_inflow_usd=500_000,
                futures_outflow_usd=500_000,
            ),
            "空头燃料": self._flow_item(
                price_24h=-0.2,
                spot_cvd_delta=0,
                spot_inflow_usd=500_000,
                spot_outflow_usd=500_000,
                futures_cvd_delta=-120_000,
                futures_inflow_usd=440_000,
                futures_outflow_usd=560_000,
                funding_pct=-0.06,
            ),
            "合约拉盘": self._flow_item(
                spot_cvd_delta=0,
                spot_inflow_usd=500_000,
                spot_outflow_usd=500_000,
                funding_pct=0.01,
            ),
            "挤空/止损": self._flow_item(
                oi_1h=-3.0,
                oi_24h=-3.0,
                spot_cvd_delta=0,
                spot_inflow_usd=500_000,
                spot_outflow_usd=500_000,
                funding_pct=-0.02,
            ),
            "诱多/派发": self._flow_item(
                oi_1h=0.5,
                oi_24h=0.5,
                spot_cvd_delta=-100_000,
                spot_inflow_usd=450_000,
                spot_outflow_usd=550_000,
                funding_pct=0.04,
            ),
            "恐慌下跌": self._flow_item(
                price_24h=-3.0,
                spot_cvd_delta=-100_000,
                spot_inflow_usd=450_000,
                spot_outflow_usd=550_000,
                futures_cvd_delta=-120_000,
                futures_inflow_usd=440_000,
                futures_outflow_usd=560_000,
                funding_pct=-0.01,
            ),
        }

        for expected, item in cases.items():
            with self.subTest(category=expected):
                result = flow_classification(item)
                self.assertEqual(result["category"], expected)
                self.assertTrue(result["eligible"])
                self.assertGreaterEqual(result["score"], 60)

    def test_p0_score_is_monotonic_for_stronger_same_category_evidence(self) -> None:
        weak = flow_classification(self._flow_item(
            price_24h=1.1,
            oi_1h=2.1,
            oi_24h=2.1,
            spot_cvd_delta=40_000,
            spot_inflow_usd=520_000,
            spot_outflow_usd=480_000,
            futures_cvd_delta=30_000,
            futures_inflow_usd=515_000,
            futures_outflow_usd=485_000,
            quote_volume=10_000_000,
        ))
        strong = flow_classification(self._flow_item(
            price_24h=4.0,
            oi_1h=8.0,
            oi_24h=8.0,
            spot_cvd_delta=300_000,
            spot_inflow_usd=650_000,
            spot_outflow_usd=350_000,
            futures_cvd_delta=400_000,
            futures_inflow_usd=700_000,
            futures_outflow_usd=300_000,
            quote_volume=150_000_000,
        ))

        self.assertEqual(weak["category"], "真启动候选")
        self.assertEqual(strong["category"], "真启动候选")
        self.assertGreaterEqual(strong["score"], weak["score"])

    def test_candidate_pool_diversifies_liquidity_movers_and_funding(self) -> None:
        class Source:
            def usdt_perp_symbols(self):
                return [
                    {"symbol": symbol}
                    for symbol in ("LIQUSDT", "MOVEUSDT", "FUNDUSDT", "OTHERUSDT")
                ]

            def premium_index(self):
                return [
                    {"symbol": "LIQUSDT", "lastFundingRate": "0.0001"},
                    {"symbol": "MOVEUSDT", "lastFundingRate": "0.0002"},
                    {"symbol": "FUNDUSDT", "lastFundingRate": "-0.01"},
                    {"symbol": "OTHERUSDT", "lastFundingRate": "0"},
                ]

            def ticker_24h(self):
                return [
                    {"symbol": "LIQUSDT", "quoteVolume": "1000000000", "priceChangePercent": "1"},
                    {"symbol": "MOVEUSDT", "quoteVolume": "10000000", "priceChangePercent": "30"},
                    {"symbol": "FUNDUSDT", "quoteVolume": "9000000", "priceChangePercent": "2"},
                    {"symbol": "OTHERUSDT", "quoteVolume": "8000000", "priceChangePercent": "3"},
                ]

        settings = Settings(
            radar_min_quote_volume=1,
            flow_candidate_pool=4,
            flow_scan_limit=3,
        )
        candidates = FlowRadarEngine(settings)._candidate_symbols(Source())

        self.assertEqual(
            {item["symbol"] for item in candidates[:3]},
            {"LIQUSDT", "MOVEUSDT", "FUNDUSDT"},
        )
        self.assertEqual(len(candidates), 4)

    def test_legacy_candidate_pool_no_longer_truncates_full_market(self) -> None:
        class Source:
            def usdt_perp_symbols(self):
                return [{"symbol": f"C{index}USDT"} for index in range(8)]

            def premium_index(self):
                return [
                    {"symbol": f"C{index}USDT", "lastFundingRate": str(index / 10000)}
                    for index in range(8)
                ]

            def ticker_24h(self):
                return [
                    {
                        "symbol": f"C{index}USDT",
                        "quoteVolume": str(1_000_000 - index),
                        "priceChangePercent": str(index),
                        "lastPrice": "1",
                    }
                    for index in range(8)
                ]

        settings = Settings(radar_min_quote_volume=1, flow_candidate_pool=2)
        candidates = FlowRadarEngine(settings)._candidate_symbols(Source())

        self.assertEqual(len(candidates), 8)
        self.assertEqual({item["priority_rank"] for item in candidates}, set(range(1, 9)))

    def test_rotation_covers_full_market_before_repeating(self) -> None:
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            settings = Settings(data_dir=data_dir, flow_scan_limit=24)
            candidates = [
                {
                    "symbol": f"C{index:02d}USDT",
                    "coin": f"C{index:02d}",
                    "price": 1.0,
                    "price_24h": float(index),
                    "quote_volume": 1_000_000.0 - index,
                    "funding_pct": 0.0,
                    "funding_ready": True,
                    "selection_reasons": ["liquidity"],
                    "priority_rank": index + 1,
                }
                for index in range(48)
            ]
            engine = FlowRadarEngine(settings, JsonStore(data_dir))

            first, state = engine._rotation_candidates(candidates)
            engine._save_candidate_state(
                candidates,
                state,
                selected_symbols={str(item["symbol"]) for item in first},
                observed_at=100,
            )
            second, _state = FlowRadarEngine(settings, JsonStore(data_dir))._rotation_candidates(candidates)

            self.assertEqual(len(first), 24)
            self.assertEqual(len(second), 24)
            self.assertTrue({item["symbol"] for item in first}.isdisjoint(
                {item["symbol"] for item in second}
            ))
            self.assertEqual(
                {item["symbol"] for item in first + second},
                {item["symbol"] for item in candidates},
            )

    def test_candidate_state_and_complete_cli_list_are_local_and_bounded(self) -> None:
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            settings = Settings(data_dir=data_dir, flow_scan_limit=2)
            candidates = [
                {
                    "symbol": f"C{index}USDT",
                    "coin": f"C{index}",
                    "price": 1.0,
                    "price_24h": float(index),
                    "quote_volume": 1_000_000.0,
                    "funding_pct": 0.0,
                    "funding_ready": True,
                    "selection_reasons": ["price_mover"],
                    "priority_rank": index + 1,
                }
                for index in range(3)
            ]
            store = JsonStore(data_dir)
            engine = FlowRadarEngine(settings, store)
            selected, state = engine._rotation_candidates(candidates)
            result = engine._save_candidate_state(
                candidates,
                state,
                selected_symbols={str(item["symbol"]) for item in selected},
                observed_at=123,
            )
            output = StringIO()
            with redirect_stdout(output):
                list_result = build_candidate_list(
                    settings,
                    store,
                    show_all=True,
                    limit=1,
                )
                print(format_candidate_list(list_result))

            payload = store.load(settings.flow_candidate_state_path, {})
            self.assertEqual(result["status"], "ok")
            self.assertEqual(payload["pool_mode"], "unlimited")
            self.assertEqual(payload["total_candidates"], 3)
            self.assertEqual(list_result["status"], "ok")
            self.assertIn("候选总数: 3", output.getvalue())
            for index in range(3):
                self.assertIn(f"C{index}USDT", output.getvalue())
            self.assertIn("网络请求: 0", output.getvalue())

    def test_model_comparison_records_old_and_new_without_affecting_primary(self) -> None:
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            settings = Settings(
                data_dir=data_dir,
                flow_model_comparison_path=data_dir / "comparison.json",
                flow_model_comparison_history_limit=2,
            )
            engine = FlowRadarEngine(settings, JsonStore(data_dir))
            window = ClosedWindow(
                start=datetime(2026, 7, 27, 0, 0, tzinfo=timezone.utc),
                end=datetime(2026, 7, 27, 1, 0, tzinfo=timezone.utc),
                interval_sec=3600,
                delay_sec=300,
            )
            item = self._flow_item(symbol="BTCUSDT")
            item.update({
                "category": "观察",
                "score": 0,
                "flow_model_eligible": False,
                "legacy_category": "吸筹观察",
                "legacy_score": 75,
                "quality_gate": "allow",
                "category_margin": 0,
            })

            status = engine._record_model_comparison([item], window)

            self.assertEqual(status["status"], "recorded")
            record = JsonStore(data_dir).load(settings.flow_model_comparison_path, [])[0]
            self.assertEqual(record["legacy_eligible_count"], 1)
            self.assertEqual(record["p0_eligible_count"], 0)
            self.assertEqual(record["legacy_suppressed_count"], 1)
            self.assertEqual(record["items"][0]["p0_category"], "观察")

    @staticmethod
    def _flow_item(**overrides: object) -> dict[str, object]:
        item: dict[str, object] = {
            "symbol": "TESTUSDT",
            "price_24h": 3.0,
            "price_ready": True,
            "oi_1h": 5.0,
            "oi_24h": 5.0,
            "oi_ready": True,
            "spot_cvd_delta": 100_000,
            "spot_inflow_usd": 550_000,
            "spot_outflow_usd": 450_000,
            "spot_cvd_ready": True,
            "futures_cvd_delta": 120_000,
            "futures_inflow_usd": 560_000,
            "futures_outflow_usd": 440_000,
            "futures_cvd_ready": True,
            "funding_pct": 0.01,
            "funding_ready": True,
            "quote_volume": 80_000_000,
        }
        item.update(overrides)
        return item

    def test_binance_confirmation_requires_every_declared_input(self) -> None:
        item: dict[str, object] = {}

        confirmation = apply_binance_confirmation(
            item,
            {"价格": True, "OI": True, "费率": False},
            scope="Binance USDⓈ-M Futures",
            window="1h闭合窗口",
            observed_at=1000,
        )

        self.assertEqual(confirmation["status"], "incomplete")
        self.assertEqual(confirmation["missing"], ["费率"])
        self.assertEqual(item["quality_gate"], "block")
        self.assertEqual(item["primary_data_source"], "binance_native")


if __name__ == "__main__":
    unittest.main()
