from __future__ import annotations

import unittest
from types import SimpleNamespace

from radars.launch_warning.fusion_formatter import (
    format_launch_fusion_incomplete,
    format_launch_fusion_package,
)
from shared.telegram import plain_fallback


class LaunchFusionFormatterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = SimpleNamespace(
            launch_watch_score=45,
            launch_primed_score=60,
            launch_breakout_score=75,
            launch_launched_score=90,
        )

    @staticmethod
    def item(**changes: object) -> dict[str, object]:
        item: dict[str, object] = {
            "symbol": "DEMOUSDT",
            "coin": "DEMO",
            "stage": "watching",
            "appear_count": 1,
            "asset_category_label": "山寨币",
            "evidence_strength": "medium",
            "funding_available": True,
            "funding_pct": 0.0123,
            "spot_active_net_usd": 42_000,
            "spot_active_ratio": 0.11,
            "futures_active_net_usd": 76_000,
            "futures_active_ratio": 0.08,
            "confirmation_1h": False,
            "launch_market_facts": {
                "status": "ok",
                "price_15m_pct": 3.6,
                "price_1h_pct": 4.2,
                "price_4h_pct": 5.8,
                "price_24h_rolling_pct": 11.3,
                "oi_15m_pct": 3.2,
                "oi_1h_pct": 3.9,
                "oi_4h_pct": 4.6,
                "oi_24h_closed_pct": 8.4,
                "oi_24h_status": "ok",
                "volume_ratio_15m": 2.1,
            },
            "launch_scoring": {
                "score": 54,
                "score_semantics": "rule_score_not_probability",
                "trigger_path": "momentum",
                "supporting_evidence": [
                    "price_momentum_met",
                    "open_interest_growth_met",
                    "volume_expansion_met",
                ],
                "counter_evidence": ["price_up_oi_down"],
            },
        }
        item.update(changes)
        return item

    def test_stage_labels_cover_the_lifecycle(self) -> None:
        labels = {
            "watching": "启动观察",
            "primed": "提前预警",
            "breakout": "启动确认",
            "launched": "启动加速",
            "risk": "结构转弱",
            "cooling": "启动降温",
            "failed": "本轮启动失效",
        }
        for stage, label in labels.items():
            with self.subTest(stage=stage):
                text = format_launch_fusion_package(
                    self.item(stage=stage), self.settings
                )
                self.assertIn(label, text)

    def test_formats_multiperiod_facts_and_telegram_links(self) -> None:
        text = format_launch_fusion_package(self.item(), self.settings)
        self.assertIn("• 15分钟：价格 +3.60%｜持仓 +3.20%", text)
        self.assertIn("• 1小时：价格 +4.20%｜持仓 +3.90%", text)
        self.assertIn("• 4小时：价格 +5.80%｜持仓 +4.60%", text)
        self.assertIn(
            "• 24小时：价格 +11.30%（滚动）",
            text,
        )
        self.assertIn("• 24小时持仓：+8.40%（严格闭合）", text)
        self.assertIn("触发：动量共振｜1小时：待确认", text)
        self.assertIn('<a href="https://www.tradingview.com/', text)
        self.assertIn('<a href="https://www.coinglass.com/', text)
        self.assertIn("<code>DEMOUSDT</code>", text)

    def test_translates_evidence_and_never_exposes_fixed_codes(self) -> None:
        text = format_launch_fusion_package(self.item(), self.settings)
        self.assertIn("价格动能达到门槛", text)
        self.assertIn("价格上涨但持仓下降，可能由平仓推动", text)
        self.assertNotIn("price_momentum_met", text)
        self.assertNotIn("price_up_oi_down", text)

    def test_missing_funds_are_not_rendered_as_zero(self) -> None:
        item = self.item(
            spot_active_net_usd=None,
            spot_active_ratio=None,
            futures_active_net_usd=None,
            futures_active_ratio=None,
            funding_available=False,
            funding_pct=0.0,
        )
        text = format_launch_fusion_package(item, self.settings)
        self.assertIn("• 现货：缺数据", text)
        self.assertIn("• 合约：缺数据", text)
        self.assertIn("资金费率 缺数据", text)
        self.assertNotIn("现货 +$0", text)
        self.assertNotIn("合约 +$0", text)
        self.assertNotIn("资金费率：+0.0000%", text)

    def test_active_fund_gap_explains_the_exact_binance_reason(self) -> None:
        text = format_launch_fusion_package(
            self.item(
                spot_active_net_usd=None,
                spot_active_ratio=None,
                spot_active_status="spot_pair_not_listed",
                futures_active_net_usd=None,
                futures_active_ratio=None,
                futures_active_status="window_incomplete",
            ),
            self.settings,
        )

        self.assertIn("• 现货：该币无币安现货对", text)
        self.assertIn("• 合约：本窗口未完整", text)
        self.assertNotIn("现货 +$0", text)
        self.assertNotIn("合约 +$0", text)

    def test_new_listing_explains_missing_strict_24h_oi_history(self) -> None:
        facts = dict(self.item()["launch_market_facts"])  # type: ignore[arg-type]
        facts["oi_24h_closed_pct"] = None
        facts["oi_24h_status"] = "insufficient_history"

        text = format_launch_fusion_package(
            self.item(launch_market_facts=facts),
            self.settings,
        )

        self.assertIn("• 24小时持仓：历史不足（严格闭合）", text)
        self.assertNotIn("持仓 +0.00%", text)

    def test_downward_one_hour_state_is_not_rendered_as_confirmed(self) -> None:
        item = self.item(
            confirmation_1h=None,
            price_action={
                "status": "confirmed_1h",
                "direction": "down",
            },
        )

        text = format_launch_fusion_package(item, self.settings)

        self.assertIn("触发：动量共振｜1小时：待确认", text)
        self.assertNotIn("1小时：已确认", text)

    def test_caption_is_compact_and_has_no_deterministic_promise(self) -> None:
        text = format_launch_fusion_package(
            self.item(stage="launched", confirmation_1h=True), self.settings
        )
        self.assertLessEqual(len(plain_fallback(text)), 1024)
        self.assertIn("规则分：54/100（不是概率）", text)
        for forbidden in ("必涨", "确定会涨", "稳赚", "庄家"):
            self.assertNotIn(forbidden, text)

    def test_mobile_sections_put_judgment_before_details(self) -> None:
        text = format_launch_fusion_package(self.item(), self.settings)

        expected = [
            "<b>当前判断</b>",
            "<b>🔥 核心变化</b>",
            "<b>💰 主动资金</b>",
            "<b>✅ 支持证据</b>",
            "<b>⚠️ 反向证据</b>",
            "<b>🔭 背景参考</b>",
        ]
        positions = [text.index(label) for label in expected]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("品类：山寨币｜本轮第1次｜数据：完整", text)

    def test_lifecycle_and_historical_results_are_reported_without_tuning(self) -> None:
        text = format_launch_fusion_package(
            self.item(
                launch_lifecycle={
                    "current_stage": "breakout",
                    "peak_stage": "breakout",
                    "duration_sec": 2700,
                    "cycle_status": "active",
                    "outcome_evaluation": {
                        "reliability": {
                            "rates_available": True,
                            "aggregation_scope": "asset_liquidity",
                            "completed_samples": 24,
                            "minimum_samples": 20,
                            "confirmed_rate_pct": 62.5,
                            "followed_through_rate_pct": 45.8,
                        }
                    },
                },
            ),
            self.settings,
        )

        self.assertIn("• 本轮：已跟踪 45分钟｜最高 启动确认", text)
        self.assertIn("• 历史：同类同流动性 24轮", text)
        self.assertIn("确认率 +62.50%", text)
        self.assertLessEqual(len(plain_fallback(text)), 1024)
        self.assertIn("不自动修改参数", format_launch_fusion_package(
            self.item(
                launch_lifecycle={
                    "peak_stage": "primed",
                    "duration_sec": 900,
                    "cycle_status": "active",
                    "outcome_evaluation": {
                        "reliability": {
                            "rates_available": False,
                            "completed_samples": 3,
                            "minimum_samples": 20,
                        }
                    },
                },
            ),
            self.settings,
        ))

    def test_incomplete_card_localizes_reason_and_keeps_missing_explicit(self) -> None:
        item = {
            "symbol": "DEMOUSDT",
            "coin": "DEMO",
            "launch_market_facts": {
                "status": "invalid",
                "error": "launch_market_facts_oi_gap",
                "price_15m_pct": 1.0,
            },
            "launch_scoring": {
                "data_availability": {
                    "price": True,
                    "open_interest": False,
                    "volume": False,
                    "active_funds": False,
                }
            },
        }
        text = format_launch_fusion_incomplete(item)
        self.assertIn("已取得：价格", text)
        self.assertIn("缺少：持仓量、成交量、主动资金", text)
        self.assertIn("• 原因：持仓量窗口不连续", text)
        self.assertIn("缺失项不会按0计算", text)
        self.assertNotIn("launch_market_facts_oi_gap", text)
        self.assertLessEqual(len(text), 1024)


if __name__ == "__main__":
    unittest.main()
