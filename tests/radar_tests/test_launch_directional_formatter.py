from __future__ import annotations

import copy
import unittest

from radars.launch_warning.directional_formatter import (
    format_launch_directional_signal,
    launch_directional_topic_intro,
)
from shared.telegram import plain_fallback


def _item(**changes: object) -> dict[str, object]:
    item: dict[str, object] = {
        "symbol": "DEMOUSDT",
        "coin": "DEMO",
        "asset_category": "altcoin",
        "entry_zone": {"low": 1.02, "high": 1.06},
        "invalidation_price": 0.98,
        "targets": [1.22, 1.35],
        "risk_reward_ratio": 2.4,
        "funding_rate_pct": 0.0123,
        "basis_pct": 0.08,
        "directional_readiness": {
            "status": "多头候选",
            "direction": "bullish",
            "bullish_readiness": 78,
            "bearish_readiness": 21,
            "data_complete": True,
            "evidence": {
                "bullish": [
                    "price_up_oi_up",
                    "spot_cvd_buying",
                    "futures_cvd_buying",
                    "bullish_structure",
                ],
                "bearish": ["funding_crowded_in_direction"],
            },
            "limitations": [
                "rule_readiness_not_probability",
                "open_interest_does_not_identify_long_or_short_by_itself",
            ],
        },
        "multi_timeframe": {
            "role_groups": {
                "macro_direction": {"direction": "bullish"},
                "main_structure": {"direction": "bullish"},
                "confirmation": {"direction": "bullish"},
                "trigger": {"direction": "mixed"},
                "entry": {"direction": "neutral"},
            },
            "rolling_24h_background": {"direction": "bullish"},
        },
    }
    item.update(changes)
    return item


class LaunchDirectionalFormatterTests(unittest.TestCase):
    def test_bullish_mobile_first_screen_has_required_fields_in_order(self) -> None:
        text = format_launch_directional_signal(_item())

        expected = [
            "看涨准备｜等待回踩确认",
            "准备度：78/100（规则分，不是涨跌概率）",
            "入场观察区：1.02–1.06",
            "失效位置：0.98",
            "目标：1.22 / 1.35",
            "收益风险比：2.40",
            "<b>🧭 多周期</b>",
            "周线/日线：偏多",
            "12小时–4小时：偏多",
            "2小时/1小时：偏多",
            "15分钟：方向分歧",
            "5分钟：震荡",
        ]
        positions = [text.index(value) for value in expected]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("规则分，不是涨跌概率", text)

    def test_bearish_card_uses_its_own_score_and_evidence(self) -> None:
        item = _item(
            entry_zone=[0.94, 0.97],
            invalidation_price=1.01,
            targets=[0.86, 0.79],
            directional_readiness={
                "status": "空头候选",
                "direction": "bearish",
                "bullish_readiness": 18,
                "bearish_readiness": 81,
                "data_complete": True,
                "evidence": {
                    "bullish": ["spot_cvd_buying"],
                    "bearish": [
                        "price_down_oi_up",
                        "spot_cvd_selling",
                        "bearish_structure",
                    ],
                },
            },
        )

        text = format_launch_directional_signal(item)

        self.assertIn("看跌准备｜等待反弹受阻", text)
        self.assertIn("准备度：81/100", text)
        self.assertIn("价格下跌，持仓量同步增加", text)
        self.assertIn("现货主动卖出占优", text)
        self.assertIn("反向证据", text)
        self.assertIn("现货主动买入占优", text)

    def test_asset_profiles_have_distinct_plain_language_risks(self) -> None:
        cases = {
            "core_crypto": ("主流加密资产", "资金费率、基差和清算"),
            "altcoin": ("山寨币", "插针风险"),
            "tokenized_stock": ("股票代币", "财报、开盘跳空"),
            "broad_market_etf": ("指数代币", "隔夜跳空风险"),
            "precious_metal": ("贵金属代币", "宏观数据"),
            "energy": ("能源代币", "交割月"),
        }
        for key, (label, risk) in cases.items():
            with self.subTest(asset_category=key):
                text = format_launch_directional_signal(_item(asset_category=key))
                self.assertIn(f"品类：{label}", text)
                self.assertIn(risk, text)

    def test_internal_structure_and_evidence_codes_are_translated(self) -> None:
        signal = copy.deepcopy(_item()["directional_readiness"])
        assert isinstance(signal, dict)
        signal["evidence"] = {
            "bullish": ["BOS_up", "CHoCH_up", "unknown_rule_code"],
            "bearish": ["sweep_high"],
        }

        text = format_launch_directional_signal(
            _item(directional_readiness=signal)
        )

        self.assertIn("收盘向上突破原有结构", text)
        self.assertIn("原偏空结构开始向多转变", text)
        self.assertIn("其他规则证据已记录", text)
        self.assertIn("向上扫过流动性后回落", text)
        for code in ("BOS_up", "CHoCH_up", "unknown_rule_code", "sweep_high"):
            self.assertNotIn(code, text)

    def test_user_controlled_text_is_html_escaped(self) -> None:
        text = format_launch_directional_signal(_item(
            entry_zone="<b>1 & 2</b>",
            ai_interpretation="<script>bad()</script> & still text",
        ))

        self.assertNotIn("<script>", text)
        self.assertNotIn("<b>1 & 2</b>", text)
        self.assertIn("&lt;b&gt;1 &amp; 2&lt;/b&gt;", text)
        self.assertIn("&lt;script&gt;bad()&lt;/script&gt; &amp; still text", text)

    def test_missing_fields_are_explicit_and_never_filled_with_zero(self) -> None:
        text = format_launch_directional_signal({
            "symbol": "DEMOUSDT",
            "asset_category": "unknown",
            "directional_readiness": {
                "status": "数据不足",
                "direction": "none",
                "data_complete": False,
                "missing_fields": ["basis_pct"],
            },
        })

        self.assertIn("方向待确认｜等待完整收盘数据", text)
        self.assertIn("准备度：待确认", text)
        self.assertIn("当前结构空间不足", text)
        self.assertNotIn("入场观察区：0", text)
        self.assertNotIn("目标：0", text)
        self.assertIn("本轮降级为观察", text)

    def test_divergence_is_risk_watch_without_trade_plan(self) -> None:
        text = format_launch_directional_signal(_item(
            directional_readiness={
                "status": "假强背离",
                "direction": "bearish_divergence_watch",
                "bullish_readiness": 30,
                "bearish_readiness": 59,
                "data_complete": True,
                "divergence_evidence": [
                    "spot_and_futures_cvd_oppose_price_rise",
                ],
                "limitations": [
                    "divergence_watch_does_not_confirm_reversal",
                ],
            },
        ))

        self.assertIn("看跌背离观察", text)
        self.assertIn("背离风险观察", text)
        self.assertIn("尚未确认反转", text)
        self.assertIn("现货和合约主动成交都偏卖出", text)
        self.assertNotIn("入场观察区", text)
        self.assertNotIn("收益风险比", text)

    def test_futures_only_candidate_explains_degraded_boundary(self) -> None:
        text = format_launch_directional_signal(_item(
            directional_readiness={
                "status": "多头候选",
                "direction": "bullish_candidate",
                "bullish_readiness": 55,
                "bearish_readiness": 20,
                "data_complete": False,
                "observation_ready": True,
                "observation_mode": "futures_only_spot_pair_not_listed",
                "spot_cvd_status": "spot_pair_not_listed",
                "limitations": [
                    "futures_only_observation_cannot_confirm_direction",
                ],
            },
        ))

        self.assertIn("仅合约观察", text)
        self.assertIn("没有同名现货交易对", text)
        self.assertIn("不确认看涨或看跌，也不调用AI", text)
        self.assertNotIn("入场观察区", text)

    def test_direction_flip_closes_old_cycle_without_presenting_new_trade_plan(self) -> None:
        text = format_launch_directional_signal(_item(
            directional_cycle_invalidated={
                "reason": "direction_changed",
                "previous_direction": "bullish",
                "next_direction": "bearish",
            },
            directional_readiness={
                "status": "空头确认",
                "direction": "bearish",
                "bearish_readiness": 82,
                "bullish_readiness": 15,
                "data_complete": True,
            },
        ))

        self.assertIn("看涨观察周期已失效", text)
        self.assertIn("先结束原方向周期", text)
        self.assertIn("下一完整窗口", text)
        self.assertNotIn("入场观察区", text)
        self.assertNotIn("收益风险比", text)

    def test_all_directional_lifecycle_failures_render_as_invalidated(self) -> None:
        cases = {
            "two_closes_below_invalidation": "跌破看涨失效位",
            "two_closes_above_invalidation": "升破看跌失效位",
            "two_windows_below_watch_score": "准备度低于观察门槛",
        }
        for reason, expected in cases.items():
            with self.subTest(reason=reason):
                text = format_launch_directional_signal(_item(
                    directional_cycle_invalidated={
                        "reason": reason,
                        "previous_direction": (
                            "bearish"
                            if "above" in reason
                            else "bullish"
                        ),
                    },
                ))
                self.assertIn("观察周期已失效", text)
                self.assertIn(expected, text)
                self.assertNotIn("入场观察区", text)

    def test_output_is_bounded_even_with_large_external_text(self) -> None:
        signal = copy.deepcopy(_item()["directional_readiness"])
        assert isinstance(signal, dict)
        signal["evidence"] = {
            "bullish": ["支持证据" * 100 for _ in range(30)],
            "bearish": ["反向证据" * 100 for _ in range(30)],
        }

        text = format_launch_directional_signal(
            _item(
                directional_readiness=signal,
                ai_interpretation="AI解读" * 1000,
            ),
            max_chars=2200,
        )

        self.assertLessEqual(len(text), 2200)
        self.assertLessEqual(len(plain_fallback(text)), 2200)
        self.assertIn("入场观察区", text)
        self.assertIn("多周期", text)

    def test_formatter_is_deterministic_and_does_not_mutate_input(self) -> None:
        item = _item()
        before = copy.deepcopy(item)

        self.assertEqual(
            format_launch_directional_signal(item),
            format_launch_directional_signal(item),
        )
        self.assertEqual(item, before)

    def test_topic_intro_is_detailed_plain_chinese_and_sets_ai_boundary(self) -> None:
        text = launch_directional_topic_intro()

        for expected in (
            "价格、持仓量、成交量、现货主动买卖、合约主动买卖",
            "1周/1天：过滤大方向",
            "15分钟：保留现有异动触发",
            "5分钟：只优化入场时机",
            "滚动24小时只是背景",
            "AI只把已计算的数据和规则翻译成白话",
            "不改方向、不改分数、不改失效位",
            "同一个观察最多调用一次",
            "背离只提示反转风险",
            "没有同名现货对时只做合约观察",
            "准备度是规则分，不是涨跌概率",
            "山寨币、股票/指数代币及大宗商品代币",
        ):
            self.assertIn(expected, text)
        self.assertNotIn("AI决定", text)
        self.assertNotIn("确定上涨", text)
        self.assertLessEqual(len(plain_fallback(text)), 4096)


if __name__ == "__main__":
    unittest.main()
