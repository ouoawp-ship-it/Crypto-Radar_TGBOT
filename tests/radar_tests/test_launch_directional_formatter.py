from __future__ import annotations

import copy
import unittest

from radars.launch_warning.directional_formatter import (
    format_launch_directional_signal,
    launch_directional_topic_intro,
)
from shared.telegram import TelegramGateway, plain_fallback


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
        "price_15m": 1.4,
        "oi_15m": 2.1,
        "price_1h": 4.8,
        "oi_1h": 6.2,
        "price_4h": 7.6,
        "oi_4h": 9.4,
        "price_24h": 18.2,
        "oi_24h": 12.1,
        "oi_24h_status": "ok",
        "volume_ratio": 1.8,
        "spot_cvd_1h": {
            "status": "available",
            "net_usd": 92_000,
            "ratio": 0.184,
        },
        "futures_cvd_1h": {
            "status": "available",
            "net_usd": 214_000,
            "ratio": 0.121,
        },
        "mcap": 43_000_000,
        "quote_volume": 86_000_000,
        "closed_oi_usd": 24_000_000,
        "liquidity_tier": "中流动性",
        "data_confirmation": {
            "status": "confirmed",
            "ready_count": 9,
            "total_count": 9,
            "missing": [],
        },
        "launch_lifecycle": {
            "cycle_no": 3,
            "observation_no": 3,
            "duration_sec": 2700,
            "peak_stage": "breakout",
            "delta_from_first": {
                "price_pct": 3.6,
                "oi_pct": 5.8,
                "score": 17,
            },
            "outcome_evaluation": {
                "reliability": {
                    "rates_available": False,
                    "direction_filtered": True,
                    "completed_samples": 8,
                    "minimum_samples": 20,
                },
            },
        },
        "directional_readiness": {
            "status": "多头候选",
            "direction": "bullish",
            "bullish_readiness": 88,
            "bearish_readiness": 19,
            "bullish_raw_score": 93,
            "bearish_raw_score": 19,
            "group_caps": {
                "price_oi_participation": 30,
                "active_funds": 25,
                "structure": 25,
                "execution_quality": 20,
            },
            "bullish_group_scores": {
                "price_oi_participation": 30,
                "active_funds": 23,
                "structure": 22,
                "execution_quality": 18,
            },
            "bearish_group_scores": {
                "price_oi_participation": 0,
                "active_funds": 0,
                "structure": 0,
                "execution_quality": 19,
            },
            "risk_adjustments": {
                "bullish": -5,
                "bearish": 0,
                "bullish_reasons": ["funding_crowded_in_direction"],
                "bearish_reasons": [],
            },
            "hard_gates": {
                "bullish": {
                    "complete_data": True,
                    "macro_direction_aligned": True,
                    "main_structure_aligned": True,
                    "confirmation_group_aligned": True,
                    "confirmed_2h": True,
                    "confirmed_1h": True,
                    "four_hour_not_opposed": True,
                    "trigger_15m_aligned": True,
                    "entry_5m_aligned": False,
                    "spot_cvd_aligned": True,
                    "futures_cvd_aligned": True,
                    "liquidity": True,
                    "risk_reward": True,
                },
                "bullish_passed": False,
                "bearish_passed": False,
            },
            "thresholds": {
                "price_change_pct": 2.0,
                "oi_change_pct": 2.5,
                "cvd_ratio": 0.08,
            },
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
    def test_every_directional_status_has_a_plain_chinese_headline(self) -> None:
        cases = {
            "多头确认": ("bullish", "看涨条件满足"),
            "多头候选": ("bullish_candidate", "看涨候选"),
            "空头确认": ("bearish", "看跌条件满足"),
            "空头候选": ("bearish_candidate", "看跌候选"),
            "杠杆过热": ("bullish_overheated", "多头过热"),
            "挤空反弹": ("bullish_rebound_only", "挤空反弹"),
            "多头踩踏": ("bearish_deleveraging_only", "多头踩踏"),
            "潜伏积累": ("bullish_candidate", "潜伏观察"),
            "派发风险": ("bearish_candidate", "派发风险"),
            "冲突等待": ("none", "多空冲突"),
            "数据不足": ("none", "数据不足"),
            "假强背离": ("bearish_divergence_watch", "假强背离"),
            "假弱背离": ("bullish_divergence_watch", "假弱背离"),
        }
        for status, (direction, headline) in cases.items():
            with self.subTest(status=status):
                text = format_launch_directional_signal({
                    "symbol": "DEMOUSDT",
                    "asset_category": "altcoin",
                    "directional_readiness": {
                        "status": status,
                        "direction": direction,
                        "bullish_readiness": 55,
                        "bearish_readiness": 45,
                        "data_complete": status != "数据不足",
                    },
                })
                self.assertIn(headline, text)

    def test_bullish_mobile_first_screen_has_strength_facts_and_gate_in_order(self) -> None:
        item = _item()
        signal = copy.deepcopy(item["directional_readiness"])
        assert isinstance(signal, dict)
        signal["status"] = "多头确认"
        hard_gates = signal["hard_gates"]
        assert isinstance(hard_gates, dict)
        bullish_gates = hard_gates["bullish"]
        assert isinstance(bullish_gates, dict)
        bullish_gates["entry_5m_aligned"] = True
        hard_gates["bullish_passed"] = True
        item["directional_readiness"] = signal

        text = format_launch_directional_signal(item)

        expected = [
            "看涨条件满足｜等待回踩确认",
            "<b>当前结论</b>",
            "<b>📊 信号强度</b>",
            "看涨 88｜看跌 19｜看涨领先 69",
            "构成：价与持仓 30/30｜主动买卖 23/25｜结构 22/25｜执行 18/20",
            "当前方向：原始 93｜拥挤 -5｜最终 88",
            "<b>🔥 实际数据</b>",
            "1小时：价格 +4.80%｜持仓量 +6.20%",
            "现货1小时：+$92K｜主动占比 +18.4%",
            "合约1小时：+$214K｜主动占比 +12.1%",
            "15分钟发现：价格 +1.40%｜持仓量 +2.10%｜成交量 1.80倍",
            "本品类门槛：价格±2.0%｜持仓+2.5%｜主动占比±8.0%",
            "<b>🚦 可靠度与风险</b>",
            "确认门槛：13/13｜全部通过",
            "<b>📍 观察计划</b>",
            "观察区：1.02–1.06｜失效参考：0.98",
            "目标参考：1.22 / 1.35｜收益风险参考：2.40",
        ]
        positions = [text.index(value) for value in expected]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("规则分不是概率", text)
        self.assertNotIn("price_oi_participation", text)
        self.assertNotIn("entry_5m_aligned", text)

    def test_bearish_card_uses_its_own_score_and_evidence(self) -> None:
        item = _item(
            price_15m=-1.8,
            oi_15m=2.9,
            price_1h=-4.9,
            oi_1h=5.7,
            spot_cvd_1h={
                "status": "available",
                "net_usd": -66_000,
                "ratio": -0.172,
            },
            futures_cvd_1h={
                "status": "available",
                "net_usd": -181_000,
                "ratio": -0.146,
            },
            entry_zone=[0.94, 0.97],
            invalidation_price=1.01,
            targets=[0.86, 0.79],
            directional_readiness={
                "status": "空头候选",
                "direction": "bearish",
                "bullish_readiness": 18,
                "bearish_readiness": 81,
                "bearish_raw_score": 86,
                "group_caps": {
                    "price_oi_participation": 30,
                    "active_funds": 25,
                    "structure": 25,
                    "execution_quality": 20,
                },
                "bearish_group_scores": {
                    "price_oi_participation": 30,
                    "active_funds": 24,
                    "structure": 22,
                    "execution_quality": 10,
                },
                "risk_adjustments": {"bearish": -5},
                "hard_gates": {
                    "bearish": {
                        "complete_data": True,
                        "confirmed_1h": True,
                        "entry_5m_aligned": False,
                    },
                },
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

        self.assertIn("看跌候选｜证据增强，尚未确认", text)
        self.assertIn("看涨 18｜看跌 81｜看跌领先 63", text)
        self.assertIn("价与持仓 30/30｜主动买卖 24/25", text)
        self.assertIn("1小时：价格 -4.90%｜持仓量 +5.70%", text)
        self.assertIn("现货1小时：-$66K｜主动占比 -17.2%", text)
        self.assertIn("合约1小时：-$181K｜主动占比 -14.6%", text)
        self.assertIn("未过：5分钟入场确认未通过", text)
        self.assertNotIn("观察区：0.94", text)

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
                text = format_launch_directional_signal({
                    "symbol": "DEMOUSDT",
                    "asset_category": key,
                    "directional_readiness": {
                        "status": "多头候选",
                        "direction": "bullish_candidate",
                        "bullish_readiness": 55,
                        "bearish_readiness": 20,
                        "data_complete": True,
                    },
                })
                self.assertIn(f"品类：{label}", text)
                self.assertIn(risk, text)

    def test_internal_structure_and_evidence_codes_are_translated(self) -> None:
        signal = copy.deepcopy(_item()["directional_readiness"])
        assert isinstance(signal, dict)
        signal["evidence"] = {
            "bullish": ["BOS_up", "CHoCH_up", "unknown_rule_code"],
            "bearish": ["sweep_high"],
        }

        text = format_launch_directional_signal({
            "symbol": "DEMOUSDT",
            "asset_category": "altcoin",
            "directional_readiness": signal,
        })

        self.assertIn("收盘向上突破原有结构", text)
        self.assertIn("原偏空结构开始向多转变", text)
        self.assertIn("向上扫过流动性后回落", text)
        for code in ("BOS_up", "CHoCH_up", "unknown_rule_code", "sweep_high"):
            self.assertNotIn(code, text)

    def test_user_controlled_text_is_html_escaped(self) -> None:
        signal = {
            "status": "多头确认",
            "direction": "bullish",
            "bullish_readiness": 78,
            "bearish_readiness": 21,
            "data_complete": True,
            "hard_gates": {
                "bullish": {"complete_data": True},
                "bullish_passed": True,
                "minimum_risk_reward_ratio": 2.0,
            },
        }
        plan_text = format_launch_directional_signal({
            "symbol": "DEMOUSDT",
            "asset_category": "altcoin",
            "directional_readiness": signal,
            "entry_zone": "<b>1 & 2</b>",
            "invalidation_price": 0.98,
            "targets": [1.22, 1.35],
            "risk_reward_ratio": 2.4,
        })
        ai_text = format_launch_directional_signal({
            "symbol": "DEMOUSDT",
            "asset_category": "altcoin",
            "directional_readiness": signal,
            "ai_interpretation": "<script>bad()</script> & still text",
        })

        self.assertNotIn("<script>", ai_text)
        self.assertNotIn("<b>1 & 2</b>", plan_text)
        self.assertIn("&lt;b&gt;1 &amp; 2&lt;/b&gt;", plan_text)
        self.assertIn("&lt;script&gt;bad()&lt;/script&gt; &amp; still text", ai_text)

    def test_missing_fields_are_explicit_and_never_filled_with_zero(self) -> None:
        text = format_launch_directional_signal({
            "symbol": "DEMOUSDT",
            "asset_category": "unknown",
            "price_1h": None,
            "oi_1h": None,
            "spot_cvd_1h": {
                "status": "binance_unavailable",
                "net_usd": None,
                "ratio": None,
            },
            "futures_cvd_1h": {
                "status": "window_incomplete",
                "net_usd": None,
                "ratio": None,
            },
            "directional_readiness": {
                "status": "数据不足",
                "direction": "none",
                "data_complete": False,
                "missing_fields": [
                    "price_change_pct",
                    "oi_change_pct",
                    "spot_cvd_ratio",
                    "basis_pct",
                ],
            },
        })

        self.assertIn("数据不足｜本轮不确认方向", text)
        self.assertIn("多空准备度：待确认", text)
        self.assertIn("1小时：价格 缺数据｜持仓量 缺数据", text)
        self.assertIn("现货1小时：Binance数据暂不可用", text)
        self.assertIn("合约1小时：窗口不完整", text)
        self.assertIn("方向模型缺少：1小时价格、1小时持仓量、现货主动买卖、基差", text)
        self.assertIn("本轮降级为观察", text)
        self.assertNotIn("+$0", text)
        self.assertNotIn("-$0", text)
        self.assertNotIn("价格 +0.00%", text)
        self.assertNotIn("持仓量 +0.00%", text)
        self.assertNotIn("观察区：0", text)
        self.assertNotIn("目标参考：0", text)

    def test_no_trade_window_is_not_presented_as_zero_active_flow(self) -> None:
        text = format_launch_directional_signal(_item(
            spot_cvd_1h={
                "status": "no_trades",
                "net_usd": 0,
                "ratio": 0,
            },
        ))

        self.assertIn("现货1小时：本窗口无成交", text)
        self.assertNotIn("现货1小时：+$0", text)

    def test_crowding_text_uses_only_the_active_direction_adjustment(self) -> None:
        signal = copy.deepcopy(_item()["directional_readiness"])
        assert isinstance(signal, dict)
        signal["risk_adjustments"] = {
            "bullish": 0,
            "bearish": -7,
            "bullish_reasons": [],
            "bearish_reasons": ["funding_crowded_in_direction"],
        }
        signal["evidence"] = {"bullish": [], "bearish": []}

        text = format_launch_directional_signal(_item(
            funding_rate_pct=-0.10,
            basis_pct=0.0,
            directional_readiness=signal,
        ))

        crowding_line = next(
            line for line in text.splitlines() if line.startswith("• 拥挤：")
        )
        self.assertIn("资金费率 -0.1000%", crowding_line)
        self.assertNotIn("当前方向偏拥挤", crowding_line)

    def test_contradictory_confirmed_payload_cannot_publish_a_trade_plan(self) -> None:
        signal = copy.deepcopy(_item()["directional_readiness"])
        assert isinstance(signal, dict)
        signal["status"] = "多头确认"
        signal["data_complete"] = False
        signal["hard_gates"] = {
            "bullish": {
                "complete_data": False,
                "confirmed_1h": False,
            },
            "bullish_passed": False,
            "minimum_risk_reward_ratio": 2.0,
        }

        text = format_launch_directional_signal(_item(
            directional_readiness=signal,
            risk_reward_ratio=0.5,
        ))

        self.assertIn("看涨条件满足", text)
        self.assertIn("尚未满足完整确认", text)
        self.assertNotIn("<b>📍 观察计划</b>", text)
        self.assertNotIn("目标参考：", text)

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

        self.assertIn("假强背离｜价格在涨，主动买盘没有跟上", text)
        self.assertIn("背离风险观察", text)
        self.assertIn("尚未确认反转", text)
        self.assertIn("现货和合约主动成交都偏卖出", text)
        self.assertNotIn("观察区：", text)
        self.assertNotIn("收益风险参考：", text)

    def test_futures_only_candidate_explains_degraded_boundary(self) -> None:
        text = format_launch_directional_signal(_item(
            spot_cvd_1h={
                "status": "spot_pair_not_listed",
                "net_usd": None,
                "ratio": None,
            },
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
        self.assertIn("现货1小时：没有同名现货对", text)
        self.assertIn("不确认看涨或看跌，也不调用AI", text)
        self.assertNotIn("观察区：", text)

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

        self.assertIn("看涨信号失效", text)
        self.assertIn("先结束原方向周期", text)
        self.assertIn("本消息只记录旧周期结束", text)
        self.assertNotIn("观察区：", text)
        self.assertNotIn("收益风险参考：", text)

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
                self.assertIn("信号失效", text)
                self.assertIn(expected, text)
                self.assertNotIn("观察区：", text)

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
            max_chars=1024,
        )

        self.assertLessEqual(len(plain_fallback(text)), 1024)
        self.assertEqual(
            TelegramGateway._photo_validation_error(  # noqa: SLF001
                b"\x89PNG\r\n\x1a\nfixture",
                text,
            ),
            "",
        )
        self.assertIn("看涨 88｜看跌 19", text)
        self.assertIn("1小时：价格 +4.80%｜持仓量 +6.20%", text)
        self.assertNotIn("AI解读" * 100, text)

    def test_confirmed_card_base_fields_cannot_overflow_photo_caption(self) -> None:
        item = _item(
            symbol=f"{'X' * 24}USDT",
            coin="Y" * 24,
            asset_category_label="Z" * 80,
            entry_zone=f"<b>{'1' * 200}</b>",
            targets=["9" * 200] * 3,
        )
        signal = copy.deepcopy(item["directional_readiness"])
        assert isinstance(signal, dict)
        signal["status"] = "多头确认"
        hard_gates = signal["hard_gates"]
        assert isinstance(hard_gates, dict)
        bullish_gates = hard_gates["bullish"]
        assert isinstance(bullish_gates, dict)
        bullish_gates["entry_5m_aligned"] = True
        hard_gates["bullish_passed"] = True
        item["directional_readiness"] = signal

        text = format_launch_directional_signal(item)

        self.assertLessEqual(len(plain_fallback(text)), 1024)
        self.assertEqual(
            TelegramGateway._photo_validation_error(  # noqa: SLF001
                b"\x89PNG\r\n\x1a\nfixture",
                text,
            ),
            "",
        )
        self.assertIn("<b>📍 观察计划</b>", text)

    def test_extreme_finite_numbers_degrade_to_a_safe_bounded_card(self) -> None:
        item = _item(
            price_15m=1e308,
            oi_15m=1e308,
            price_1h=1e308,
            oi_1h=1e308,
            volume_ratio=1e308,
            mcap=1e308,
            quote_volume=1e308,
            closed_oi_usd=1e308,
        )
        signal = copy.deepcopy(item["directional_readiness"])
        assert isinstance(signal, dict)
        signal["bullish_readiness"] = 1e308
        signal["bearish_readiness"] = 1e308
        signal["bullish_raw_score"] = 1e308
        signal["bullish_group_scores"] = {
            "price_oi_participation": 1e308,
            "active_funds": 1e308,
            "structure": 1e308,
            "execution_quality": 1e308,
        }
        item["directional_readiness"] = signal

        text = format_launch_directional_signal(item)

        self.assertLessEqual(len(plain_fallback(text)), 1024)
        self.assertEqual(
            TelegramGateway._photo_validation_error(  # noqa: SLF001
                b"\x89PNG\r\n\x1a\nfixture",
                text,
            ),
            "",
        )

    def test_nested_one_hour_flows_are_used_instead_of_legacy_15m_fields(self) -> None:
        text = format_launch_directional_signal(_item(
            spot_active_net_usd=-999_000,
            spot_active_ratio=-0.99,
            futures_active_net_usd=-888_000,
            futures_active_ratio=-0.88,
        ))

        self.assertIn("现货1小时：+$92K｜主动占比 +18.4%", text)
        self.assertIn("合约1小时：+$214K｜主动占比 +12.1%", text)
        self.assertNotIn("-$999K", text)
        self.assertNotIn("-$888K", text)
        self.assertNotIn("-99.0%", text)
        self.assertNotIn("-88.0%", text)

    def test_background_and_market_scale_keep_window_semantics(self) -> None:
        text = format_launch_directional_signal({
            "symbol": "DEMOUSDT",
            "asset_category": "altcoin",
            "price_4h": 7.6,
            "oi_4h": 9.4,
            "price_24h": 18.2,
            "oi_24h": 12.1,
            "oi_24h_status": "ok",
            "mcap": 43_000_000,
            "quote_volume": 86_000_000,
            "closed_oi_usd": 24_000_000,
            "liquidity_tier": "中流动性",
            "directional_readiness": {
                "status": "多头候选",
                "direction": "bullish_candidate",
                "bullish_readiness": 55,
                "bearish_readiness": 20,
                "data_complete": True,
            },
        })

        self.assertIn("🔭 背景与规模", text)
        self.assertIn("4小时：价格 +7.60%｜持仓量 +9.40%", text)
        self.assertIn(
            "24小时：价格 +18.20%（滚动）｜持仓量 +12.10%（严格闭合）",
            text,
        )
        self.assertIn("市值 $43M｜24小时成交额 $86M｜当前持仓 $24M", text)
        self.assertIn("流动性：中流动性", text)

    def test_lifecycle_hides_rates_until_same_direction_sample_gate(self) -> None:
        base_signal = {
            "status": "多头候选",
            "direction": "bullish_candidate",
            "bullish_readiness": 72,
            "bearish_readiness": 31,
            "data_complete": True,
        }
        accumulating = format_launch_directional_signal({
            "symbol": "DEMOUSDT",
            "asset_category": "altcoin",
            "directional_readiness": base_signal,
            "launch_lifecycle": {
                "cycle_no": 4,
                "observation_no": 6,
                "duration_sec": 4500,
                "peak_stage": "primed",
                "delta_from_first": {
                    "price_pct": 3.6,
                    "oi_pct": 5.8,
                    "score": 12,
                },
                "outcome_evaluation": {
                    "reliability": {
                        "rates_available": False,
                        "direction_filtered": True,
                        "completed_samples": 8,
                        "minimum_samples": 20,
                    },
                },
            },
        })

        self.assertIn("第4轮｜第6次完整观察｜持续 1小时15分", accumulating)
        self.assertIn("较首次：价格 +3.60%｜持仓量 +5.80%｜准备度 +12分", accumulating)
        self.assertIn("同方向样本：积累中 8/20（不展示胜率）", accumulating)
        self.assertNotIn("确认率", accumulating)
        self.assertNotIn("跟随率", accumulating)

        ready_lifecycle = copy.deepcopy({
            "cycle_no": 4,
            "observation_no": 6,
            "duration_sec": 4500,
            "peak_stage": "breakout",
            "outcome_evaluation": {
                "reliability": {
                    "rates_available": True,
                    "direction_filtered": True,
                    "completed_samples": 24,
                    "minimum_samples": 20,
                    "confirmed_rate_pct": 62.5,
                    "followed_through_rate_pct": 45.8,
                    "follow_through_threshold_pct": 3.0,
                },
            },
        })
        ready = format_launch_directional_signal({
            "symbol": "DEMOUSDT",
            "asset_category": "altcoin",
            "directional_readiness": base_signal,
            "launch_lifecycle": ready_lifecycle,
        })

        self.assertIn(
            "同方向历史：确认率 62.5%｜跟随率 45.8%｜n=24｜跟随门槛 3.0%",
            ready,
        )

        legacy_unknown_direction = format_launch_directional_signal({
            "symbol": "DEMOUSDT",
            "asset_category": "altcoin",
            "directional_readiness": base_signal,
            "launch_lifecycle": {
                "cycle_no": 2,
                "observation_no": 2,
                "duration_sec": 1_800,
                "peak_stage": "primed",
                "outcome_evaluation": {
                    "reliability": {
                        "rates_available": True,
                        "direction_filtered": False,
                        "completed_samples": 25,
                        "minimum_samples": 20,
                        "confirmed_rate_pct": 80.0,
                    },
                },
            },
        })
        self.assertIn(
            "历史方向未识别：暂不展示比例",
            legacy_unknown_direction,
        )
        self.assertNotIn("确认率 80.0%", legacy_unknown_direction)

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
            "信号强度：看涨和看跌分别打分",
            "实际数据：显示1小时价格与持仓变化",
            "四组规则分：价格与持仓30分、主动买卖25分、多周期结构25分、执行质量20分",
            "1周/1天：过滤大方向",
            "15分钟：保留现有异动触发",
            "5分钟：只优化入场时机",
            "滚动24小时只是背景",
            "数据缺失不会按0计算",
            "历史统计严格区分看涨和看跌",
            "第一次信号单独发送",
            "历史卡片不自动删除",
            "上一条被人工删除",
            "样本不足时只显示积累进度",
            "AI只把已计算的数据和规则翻译成白话",
            "不改方向、不改分数、不改失效位",
            "同一个观察最多调用一次",
            "没有同名现货对时只做合约观察",
            "山寨币、股票/指数代币及大宗商品代币",
        ):
            self.assertIn(expected, text)
        self.assertNotIn("AI决定", text)
        self.assertNotIn("确定上涨", text)
        self.assertLessEqual(len(plain_fallback(text)), 4096)


if __name__ == "__main__":
    unittest.main()
