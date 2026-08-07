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
        "discovery_score": 80,
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
        "smc_filter": {
            "status": "supportive",
            "one_hour_structure": "bullish",
            "four_hour_structure": "bullish",
            "ai_eligible": True,
        },
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


def _phase(
    *,
    bias: str = "bullish",
    timing_stage: str = "forming",
    execution_status: str = "wait_confirmation",
    position_status: str = "middle",
    volume_status: str = "normal",
    primary_block_reason: str = "directional_hard_gates_incomplete",
    plan_eligible: bool = False,
) -> dict[str, object]:
    return {
        "bias": bias,
        "timing_stage": timing_stage,
        "execution_status": execution_status,
        "position_status": position_status,
        "volume_status": volume_status,
        "primary_block_reason": primary_block_reason,
        "plan_eligible": plan_eligible,
    }


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
            "<b>📊 发现分与方向证据分（都不是概率）</b>",
            "发现分 80/100｜只负责发现异动",
            "看涨 88｜看跌 19｜看涨领先 69",
            "构成：价与持仓 30/30｜主动买卖 23/25｜结构 22/25｜执行 18/20",
            "<b>🔥 已收盘数据</b>",
            "1小时：价格 +4.80%｜持仓量 +6.20%",
            "现货1小时：+$92K｜主动占比 +18.4%",
            "合约1小时：+$214K｜主动占比 +12.1%",
            "15分钟发现：价格 +1.40%｜持仓量 +2.10%｜成交量 1.80倍",
            "<b>🚦 位置、阶段与执行</b>",
            "<b>📍 观察计划</b>",
            "观察区：1.02–1.06｜失效参考：0.98",
            "目标参考：1.22 / 1.35｜收益风险参考：2.40",
            "确认门槛：13/13｜全部通过",
        ]
        positions = [text.index(value) for value in expected]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("发现分/证据分都不是概率", text)
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
        self.assertIn("主要阻断：5分钟入场确认未通过", text)
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

    def test_unknown_evidence_and_block_reason_are_not_echoed(self) -> None:
        item = _item()
        signal = copy.deepcopy(item["directional_readiness"])
        assert isinstance(signal, dict)
        signal["evidence"] = {
            "bullish": ["未知证据 https://private.invalid/path"],
            "bearish": [],
        }
        item["directional_readiness"] = signal
        item["launch_phase"] = _phase(
            primary_block_reason="内部路径 C:\\private\\secret"
        )

        text = format_launch_directional_signal(item)

        self.assertIn("其他规则证据已记录", text)
        self.assertIn("其他关键条件尚未通过", text)
        self.assertNotIn("private.invalid", text)
        self.assertNotIn("C:\\private", text)

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
            "smc_filter": {
                "status": "supportive",
                "one_hour_structure": "bullish",
                "four_hour_structure": "bullish",
            },
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

    def test_ai_participation_and_generated_text_are_explicit(self) -> None:
        live = format_launch_directional_signal(_item(
            ai_interpretation_status="available",
            ai_interpretation_source="provider",
            ai_interpretation="规则证据偏多，但仍要等待结构保持。",
        ))
        cached = format_launch_directional_signal(_item(
            ai_interpretation_status="available",
            ai_interpretation_source="cache",
            ai_interpretation="复用已校验的白话解读。",
        ))

        self.assertIn("AI参与</b>：已完成（本轮调用）", live)
        self.assertIn("AI白话解读</b>：规则证据偏多", live)
        self.assertIn("仅本行由AI生成；规则结论不变", live)
        self.assertLess(live.index("🔥 已收盘数据"), live.index("AI参与"))
        self.assertIn("AI参与</b>：已完成（复用已校验结果）", cached)

    def test_ai_failure_and_deferred_status_never_disappear(self) -> None:
        truncated = format_launch_directional_signal(_item(
            ai_interpretation_status="ai_output_truncated",
            ai_interpretation_source="provider",
        ))
        deferred = format_launch_directional_signal(_item(
            ai_interpretation_status="deferred_cycle_limit",
        ))

        self.assertIn("已调用，但输出被截断", truncated)
        self.assertNotIn("AI白话解读</b>", truncated)
        self.assertIn("本轮顺延（每轮最多解读一个信号）", deferred)

    def test_ai_status_survives_caption_pressure_and_invalidation(self) -> None:
        pressured = format_launch_directional_signal(
            _item(
                symbol=f"{'X' * 24}USDT",
                asset_category_label="很长的品类说明" * 30,
                ai_interpretation_status="ai_timeout",
            ),
            max_chars=512,
        )
        invalidated = format_launch_directional_signal(_item(
            ai_interpretation_status="not_eligible",
            directional_cycle_invalidated={
                "reason": "direction_changed",
                "previous_direction": "bullish",
            },
        ))

        self.assertIn("AI参与", pressured)
        self.assertIn("调用超时", pressured)
        self.assertLessEqual(len(plain_fallback(pressured)), 512)
        self.assertIn("AI参与</b>：未调用", invalidated)

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
        self.assertIn("多空证据分：待确认", text)
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
            line for line in text.splitlines() if line.startswith("• 拥挤参考：")
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

        self.assertIn("确认信息冲突｜本轮降级观察", text)
        self.assertIn("已安全降级", text)
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
        self.assertIn("📌 当前处理", text)
        self.assertIn("背离尚未确认反转", text)
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
        self.assertIn("不确认方向、不生成计划，也不调用AI", text)
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
            "two_windows_below_watch_score": "证据分低于观察门槛",
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
        self.assertIn("较首次：价格 +3.60%｜持仓量 +5.80%｜证据分 +12分", accumulating)
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

    def test_smc_filter_is_visible_and_only_supportive_allows_plan(self) -> None:
        item = _item()
        signal = copy.deepcopy(item["directional_readiness"])
        signal["status"] = "多头确认"
        signal["hard_gates"]["bullish_passed"] = True
        for key in signal["hard_gates"]["bullish"]:
            signal["hard_gates"]["bullish"][key] = True
        item["directional_readiness"] = signal
        item["smc_filter"] = {
            "status": "supportive",
            "one_hour_structure": "bullish",
            "four_hour_structure": "neutral",
        }

        supportive = format_launch_directional_signal(item)

        self.assertIn("SMC二次过滤</b>：同向支持", supportive)
        self.assertIn("1小时偏多｜4小时中性", supportive)
        self.assertIn("📍 观察计划", supportive)
        self.assertIn("只过滤，不改发现分和方向证据分", supportive)

        item["smc_filter"] = {
            "status": "neutral",
            "one_hour_structure": "bullish",
            "four_hour_structure": "bearish",
        }
        neutral = format_launch_directional_signal(item)
        self.assertIn("看涨条件满足｜等待回踩确认", neutral)
        self.assertIn("SMC二次过滤</b>：中性观察", neutral)
        self.assertNotIn("📍 观察计划", neutral)
        self.assertIn("📌 当前处理", neutral)

        missing = _item()
        missing["directional_readiness"] = signal
        missing.pop("smc_filter")
        missing_text = format_launch_directional_signal(missing)
        self.assertNotIn("📍 观察计划", missing_text)

    def test_smc_insufficient_cannot_be_presented_as_conflict(self) -> None:
        text = format_launch_directional_signal(_item(smc_filter={
            "status": "insufficient",
            "one_hour_structure": "unavailable",
            "four_hour_structure": "unavailable",
        }))

        self.assertIn("SMC二次过滤</b>：数据不足", text)
        self.assertIn("看涨候选｜证据增强，尚未确认", text)
        self.assertIn("不改发现分和方向证据分", text)
        self.assertNotIn("高周期冲突", text)

    def test_bullish_high_extension_is_tracking_not_a_fresh_candidate(self) -> None:
        text = format_launch_directional_signal(_item(
            launch_phase=_phase(
                bias="bullish",
                timing_stage="extended_no_chase",
                execution_status="blocked_extension",
                position_status="high_extended",
                volume_status="sufficient",
                primary_block_reason="bullish_72h_high_extended",
            ),
            smc_filter={
                "status": "conflicting",
                "one_hour_structure": "bearish",
                "four_hour_structure": "bearish",
            },
        ))

        self.assertIn("上涨已延伸｜高位不追涨", text)
        self.assertIn("72小时位置：高位延伸｜成交量：达到确认要求", text)
        self.assertIn("SMC二次过滤</b>：高周期冲突", text)
        self.assertNotIn("看涨候选", text)
        self.assertNotIn("📍 观察计划", text)
        for code in (
            "extended_no_chase",
            "blocked_extension",
            "high_extended",
            "bullish_72h_high_extended",
        ):
            self.assertNotIn(code, text)

    def test_bearish_low_extension_is_tracking_not_a_fresh_candidate(self) -> None:
        item = _item(
            price_15m=-0.31,
            oi_15m=0.01,
            price_1h=-1.05,
            oi_1h=0.04,
            spot_cvd_1h={"status": "available", "net_usd": -103_000, "ratio": -0.108},
            futures_cvd_1h={"status": "available", "net_usd": -1_000_000, "ratio": -0.229},
            launch_phase=_phase(
                bias="bearish",
                timing_stage="extended_no_chase",
                execution_status="blocked_extension",
                position_status="low_extended",
                volume_status="low",
                primary_block_reason="bearish_72h_low_extended",
            ),
        )
        signal = copy.deepcopy(item["directional_readiness"])
        assert isinstance(signal, dict)
        signal.update({
            "status": "空头候选",
            "direction": "bearish",
            "bullish_readiness": 10,
            "bearish_readiness": 60,
            "bearish_raw_score": 60,
            "risk_adjustments": {"bullish": 0, "bearish": 0},
            "hard_gates": {
                "bearish": {"complete_data": True, "entry_5m_aligned": False},
                "bearish_passed": False,
            },
            "evidence": {
                "bullish": [],
                "bearish": ["spot_cvd_selling", "futures_cvd_selling"],
            },
        })
        signal["bearish_group_scores"] = {
            "price_oi_participation": 0,
            "active_funds": 25,
            "structure": 25,
            "execution_quality": 10,
        }
        item["directional_readiness"] = signal

        text = format_launch_directional_signal(item)

        self.assertIn("下跌已延伸｜低位不追空", text)
        self.assertIn("72小时位置：低位延伸｜成交量：缩量", text)
        self.assertNotIn("看跌候选", text)
        self.assertNotIn("📍 观察计划", text)

    def test_lifecycle_stage_owns_title_and_smc_stays_secondary(self) -> None:
        cases = {
            "launched": "看涨加速",
            "risk": "看涨结构转弱",
            "cooling": "看涨降温",
        }
        for current_stage, expected in cases.items():
            with self.subTest(current_stage=current_stage):
                item = _item(
                    launch_phase=_phase(),
                    smc_filter={
                        "status": "neutral",
                        "one_hour_structure": "bullish",
                        "four_hour_structure": "bearish",
                    },
                )
                lifecycle = copy.deepcopy(item["launch_lifecycle"])
                assert isinstance(lifecycle, dict)
                lifecycle["current_stage"] = current_stage
                item["launch_lifecycle"] = lifecycle

                text = format_launch_directional_signal(item)

                self.assertIn(expected, text.splitlines()[0])
                self.assertIn("SMC二次过滤</b>：中性观察", text)

    def test_phase_and_ai_internal_status_codes_are_never_exposed(self) -> None:
        statuses = {
            "not_eligible_directional_incomplete": "方向判断所需数据不完整",
            "not_eligible_phase_missing": "位置与时机检查不可用",
            "not_eligible_phase_low_volume": "1小时成交量未达到确认要求",
            "not_eligible_phase_low_flow_scale": "主动买卖规模未达到确认要求",
            "not_eligible_phase_crowding": "同方向已经过度拥挤",
        }
        for status, expected in statuses.items():
            with self.subTest(status=status):
                text = format_launch_directional_signal(_item(
                    launch_phase=_phase(),
                    ai_interpretation_status=status,
                ))
                self.assertIn(expected, text)
                self.assertNotIn(status, text)

    def test_caption_pressure_keeps_lifecycle_ai_and_fixed_footer(self) -> None:
        text = format_launch_directional_signal(
            _item(
                symbol=f"{'LONG' * 20}USDT",
                asset_category_label="超长品类" * 30,
                launch_phase=_phase(),
                ai_interpretation_status="available",
                ai_interpretation_source="provider",
                ai_interpretation="已核对规则事实。" * 100,
            ),
            max_chars=512,
        )

        self.assertLessEqual(len(plain_fallback(text)), 512)
        self.assertIn("AI参与", text)
        self.assertIn("生命周期", text)
        self.assertIn("发现分/证据分都不是概率", text)
        self.assertIn("不构成投资建议", text)

    def test_topic_intro_is_detailed_plain_chinese_and_sets_ai_boundary(self) -> None:
        text = launch_directional_topic_intro()

        for expected in (
            "提前发现可能启动或转弱的币，并持续跟踪",
            "15分钟继续使用原规则发现第一批异动",
            "发现分：只负责衡量15分钟异动有多明显",
            "方向证据分：看涨和看跌分开计算",
            "行情阶段：区分初步发现、形成中、确认、延续",
            "执行状态：明确写出等待确认",
            "方向证据强，不代表当前位置适合追",
            "上涨已延伸",
            "下跌已延伸",
            "确认信息冲突",
            "主动买卖只表示成交主导方",
            "缺失项不按0补",
            "SMC只做二次过滤",
            "不会接管卡片主标题",
            "AI只负责白话解读",
            "只有“AI白话解读”一行由AI生成",
            "不能改方向、发现分、方向证据分、阶段、观察区或失效位",
            "延伸不追价、时机未到、SMC不支持或数据不足时不调用AI",
            "首次预警单独发送",
            "历史消息不自动删除",
            "上一条被人工删除",
            "没有同名现货对时只做合约观察",
            "主流币、山寨币、股票/指数代币和大宗商品代币",
        ):
            self.assertIn(expected, text)
        self.assertNotIn("AI决定", text)
        self.assertNotIn("确定上涨", text)
        self.assertLessEqual(len(plain_fallback(text)), 4096)


if __name__ == "__main__":
    unittest.main()
