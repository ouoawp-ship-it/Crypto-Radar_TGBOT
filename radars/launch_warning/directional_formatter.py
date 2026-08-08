from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from html import unescape
from typing import Any

from ..common import coin_link, fmt_money, tg_bold, tg_escape


# Launch packages are sent as a Telegram photo caption when the 1h chart is
# enabled. Telegram caps parsed caption text at 1024 characters.
DEFAULT_MAX_CHARS = 1024

_DIRECTION_TEXT = {
    "bullish": ("🟢", "看涨准备"),
    "bullish_candidate": ("🟢", "看涨观察"),
    "bullish_divergence_watch": ("🟡", "看涨背离观察"),
    "bullish_rebound_only": ("🟠", "反弹观察"),
    "bearish": ("🔴", "看跌准备"),
    "bearish_candidate": ("🔴", "看跌观察"),
    "bearish_divergence_watch": ("🟡", "看跌背离观察"),
    "bearish_deleveraging_only": ("🟠", "去杠杆观察"),
    "bullish_overheated": ("🟠", "多头过热"),
    "none": ("⚪", "方向待确认"),
}

_STATUS_TEXT = {
    "多头确认": "条件满足",
    "多头候选": "等待回踩确认",
    "空头确认": "条件满足",
    "空头候选": "等待反弹受阻",
    "杠杆过热": "过热，不追涨",
    "挤空反弹": "只看反弹，不追涨",
    "多头踩踏": "去杠杆释放，不追空",
    "潜伏积累": "等待向上突破",
    "派发风险": "表面偏强，警惕派发",
    "冲突等待": "多空证据冲突",
    "数据不足": "等待完整收盘数据",
    "假强背离": "价格偏强但主动买盘转弱，仅作反转风险观察",
    "假弱背离": "价格偏弱但主动卖盘转弱，仅作反转风险观察",
    "confirmed": "条件满足",
    "candidate": "等待确认",
    "waiting_retest": "等待回踩确认",
    "waiting_rebound": "等待反弹受阻",
    "invalid": "本轮已失效",
}

_ASSET_PROFILE = {
    "core_crypto": ("主流加密资产", "资金费率、基差和清算可能造成杠杆反转。"),
    "large_crypto": ("大型加密资产", "仍需防范资金费率、基差和清算引起的反转。"),
    "altcoin": ("山寨币", "流动性与插针风险通常更高，必须等待回踩或反弹确认。"),
    "single_stock": ("股票代币", "美股休市、财报、开盘跳空及代币跟踪偏差会使信号失真。"),
    "tokenized_stock": ("股票代币", "美股休市、财报、开盘跳空及代币跟踪偏差会使信号失真。"),
    "broad_market_etf": ("指数代币", "交易时段、跟踪偏差和隔夜跳空风险需要额外确认。"),
    "regional_etf": ("指数代币", "交易时段、跟踪偏差和隔夜跳空风险需要额外确认。"),
    "leveraged_index_etf": ("杠杆指数代币", "除了跟踪偏差，还需防范每日复位和杠杆损耗。"),
    "inverse_index_etf": ("反向指数代币", "除了跟踪偏差，还需防范每日复位和杠杆损耗。"),
    "leveraged_sector_etf": ("杠杆行业指数代币", "除了跟踪偏差，还需防范每日复位和杠杆损耗。"),
    "inverse_sector_etf": ("反向行业指数代币", "除了跟踪偏差，还需防范每日复位和杠杆损耗。"),
    "etf": ("ETF代币", "交易时段、跟踪偏差和隔夜跳空风险需要额外确认。"),
    "precious_metal": ("贵金属代币", "注意现货交易时段、宏观数据和代币跟踪偏差。"),
    "industrial_metal": ("工业金属代币", "注意现货交易时段、宏观数据和代币跟踪偏差。"),
    "energy": ("能源代币", "注意交割月、库存数据、交易时段和跟踪偏差。"),
    "currency_pair": ("外汇代币", "注意各市场交易时段、宏观数据和跟踪偏差。"),
    "crypto_index": ("加密市场指数", "指数成分和权重可能调整，不宜当作单一币种理解。"),
    "other": ("大宗商品代币", "注意原生市场交易时段、宏观数据和代币跟踪偏差。"),
}

_EVIDENCE_TEXT = {
    "price_up_oi_up": "价格上涨，持仓量同步增加",
    "price_down_oi_up": "价格下跌，持仓量同步增加",
    "short_covering": "价格上涨但持仓下降，更像空头平仓反弹",
    "long_liquidation": "价格下跌且持仓下降，更像多头去杠杆",
    "quiet_price_oi_build": "价格横盘，但持仓量正在积累",
    "price_up_without_oi_confirmation": "价格上涨，但持仓量尚未确认",
    "price_down_without_oi_confirmation": "价格下跌，但持仓量尚未确认",
    "spot_cvd_buying": "现货主动买入占优",
    "spot_cvd_selling": "现货主动卖出占优",
    "futures_cvd_buying": "合约主动买入占优",
    "futures_cvd_selling": "合约主动卖出占优",
    "bullish_structure": "价格结构偏多",
    "bearish_structure": "价格结构偏空",
    "funding_extreme_in_direction": "资金费率与当前方向过度拥挤",
    "funding_crowded_in_direction": "资金费率显示当前方向偏拥挤",
    "basis_extreme_in_direction": "基差显示当前方向过度拥挤",
    "basis_crowded_in_direction": "基差显示当前方向偏拥挤",
    "BOS_up": "收盘向上突破原有结构",
    "BOS_down": "收盘向下跌破原有结构",
    "CHoCH_up": "原偏空结构开始向多转变",
    "CHoCH_down": "原偏多结构开始向空转变",
    "sweep_low": "向下扫过流动性后收回",
    "sweep_high": "向上扫过流动性后回落",
    "bullish_fvg": "上涨不平衡区仍可作为回踩参考",
    "bearish_fvg": "下跌不平衡区仍可作为反弹参考",
    "spot_and_futures_cvd_oppose_price_rise": "价格上涨，但现货和合约主动成交都偏卖出",
    "spot_and_futures_cvd_oppose_price_decline": "价格下跌，但现货和合约主动成交都偏买入",
    "risk_watch_not_confirmed_reversal": "背离只是风险提醒，尚未确认反转",
}

_LIMITATION_TEXT = {
    "rule_readiness_not_probability": "旧版准备度只是规则分，不是涨跌概率",
    "rule_score_not_probability": "方向证据分不是涨跌概率",
    "open_interest_does_not_identify_long_or_short_by_itself": "持仓量单独不能区分新增多头还是空头",
    "cvd_is_aggressive_trade_imbalance_not_capital_inflow": "主动买卖差代表成交主导方，不等于真实资金流入流出",
    "funding_and_basis_are_crowding_risk_not_direction_proof": "资金费率和基差只衡量拥挤，不能单独证明方向",
    "structure_labels_do_not_prove_institutional_activity": "价格结构只是行情证据，不能证明庄家或机构身份",
    "divergence_watch_does_not_confirm_reversal": "价格与主动成交背离不等于行情已经反转",
    "futures_only_observation_cannot_confirm_direction": "缺少同名现货数据时只能观察合约，不能确认方向",
}

_ROLE_LABELS = (
    ("macro_direction", "周线/日线"),
    ("main_structure", "12小时–4小时"),
    ("confirmation", "2小时/1小时"),
    ("trigger", "15分钟"),
    ("entry", "5分钟"),
)

_HEADLINE_TEXT = {
    "多头确认": ("🟢", "看涨条件满足", "等待回踩确认"),
    "多头候选": ("🟡", "看涨候选", "证据增强，尚未确认"),
    "空头确认": ("🔴", "看跌条件满足", "等待反弹确认"),
    "空头候选": ("🟠", "看跌候选", "证据增强，尚未确认"),
    "杠杆过热": ("♨️", "多头过热", "方向仍强，追涨风险升高"),
    "挤空反弹": ("⚡", "挤空反弹", "上涨主要来自空头退出"),
    "多头踩踏": ("📉", "多头踩踏", "下跌伴随持仓释放"),
    "潜伏积累": ("🟡", "潜伏观察", "等待向上突破"),
    "派发风险": ("🟠", "派发风险", "上涨乏力，等待转弱确认"),
    "冲突等待": ("🟣", "多空冲突", "等待方向统一"),
    "数据不足": ("⚪", "数据不足", "本轮不确认方向"),
    "假强背离": ("⚠️", "假强背离", "价格在涨，主动买盘没有跟上"),
    "假弱背离": ("⚠️", "假弱背离", "价格在跌，主动卖盘正在减弱"),
}

_SUMMARY_TEXT = {
    "多头确认": "价格、持仓量、主动买卖和主要结构共同偏多，规则条件已经满足。",
    "多头候选": "偏多证据正在增加，但仍有确认条件没有通过。",
    "空头确认": "价格、持仓量、主动买卖和主要结构共同偏空，规则条件已经满足。",
    "空头候选": "偏空证据正在增加，但仍有确认条件没有通过。",
    "杠杆过热": "方向尚未转空，但资金费率与基差显示同方向已经偏拥挤。",
    "挤空反弹": "价格上涨但持仓量下降，更像空头平仓推动，不是新增多头确认。",
    "多头踩踏": "价格和持仓量同步下降，更像多头退出，不是新增空头确认。",
    "潜伏积累": "价格尚未明显启动，但持仓或主动买卖已经先出现变化。",
    "派发风险": "价格表面偏强，但主动买卖和结构正在转弱，只作派发风险观察。",
    "冲突等待": "多空分差不足或不同周期互相冲突，本轮不形成方向结论。",
    "数据不足": "关键数据缺失，本轮只保留观察，不使用0补齐。",
    "假强背离": "价格与持仓量偏强，但现货和合约主动买卖共同偏弱。",
    "假弱背离": "价格与持仓量偏弱，但现货和合约主动卖出正在减弱。",
}

_GATE_TEXT = {
    "complete_data": "数据不完整",
    "macro_direction_aligned": "周线/日线未同向",
    "main_structure_aligned": "12小时–4小时结构未同向",
    "confirmation_group_aligned": "2小时/1小时确认组未同向",
    "confirmed_2h": "2小时未确认",
    "confirmed_1h": "1小时未确认",
    "four_hour_not_opposed": "4小时方向相反",
    "trigger_15m_aligned": "15分钟触发未同向",
    "entry_5m_aligned": "5分钟入场确认未通过",
    "spot_cvd_aligned": "现货主动买卖未同向",
    "futures_cvd_aligned": "合约主动买卖未同向",
    "liquidity": "流动性不足",
    "risk_reward": "收益风险参考不足2",
}

_MISSING_FIELD_TEXT = {
    "price_change_pct": "1小时价格",
    "oi_change_pct": "1小时持仓量",
    "spot_cvd_ratio": "现货主动买卖",
    "futures_cvd_ratio": "合约主动买卖",
    "funding_rate_pct": "资金费率",
    "basis_pct": "基差",
    "structure": "价格结构",
    "macro_direction": "周线/日线方向",
    "main_structure": "主要结构",
    "confirmation": "确认周期",
    "trigger": "15分钟触发",
    "entry": "5分钟确认",
    "timeframe_2h": "2小时结构",
    "timeframe_1h": "1小时结构",
    "timeframe_4h": "4小时结构",
    "timeframe_15m": "15分钟结构",
    "timeframe_5m": "5分钟结构",
    "liquidity": "流动性",
}

_FLOW_STATUS_TEXT = {
    "available": "可用",
    "no_trades": "本窗口无成交",
    "spot_pair_not_listed": "没有同名现货对",
    "window_incomplete": "窗口不完整",
    "budget_exhausted": "本轮请求额度已用完",
    "binance_unavailable": "Binance数据暂不可用",
    "unknown": "缺数据",
    "": "缺数据",
}

_AI_STATUS_TEXT = {
    "disabled": "未开启，本卡片全部为规则结论",
    "on_demand": "未自动调用；需要时点击消息下方“AI解读”",
    "on_demand_route_not_configured": "未自动调用（私聊按钮尚未配置完整）",
    "not_requested": "未调用，本卡片全部为规则结论",
    "not_eligible": "未调用（数据或信号未达到解读条件）",
    "not_eligible_smc_conflict": "未调用（1小时与4小时结构均反向）",
    "not_eligible_smc_neutral": "未调用（高周期结构尚未形成一致支持）",
    "not_eligible_smc_insufficient": "未调用（高周期闭合数据不足）",
    "not_eligible_directional_incomplete": "未调用（方向判断所需数据不完整）",
    "not_eligible_phase_missing": "未调用（位置与时机检查不可用）",
    "not_eligible_phase_low_volume": "未调用（1小时成交量未达到确认要求）",
    "not_eligible_phase_low_flow_scale": "未调用（主动买卖规模未达到确认要求）",
    "not_eligible_phase_crowding": "未调用（同方向已经过度拥挤）",
    "not_eligible_phase_extended": "未调用（行情已经延伸，当前位置不追价）",
    "not_eligible_phase_insufficient": "未调用（位置、成交量或主动买卖数据不足）",
    "not_eligible_phase_timing": "未调用（当前时机尚未达到解读条件）",
    "not_configured": "未调用（密钥、接口或模型配置不完整）",
    "deferred_cycle_limit": "本轮顺延（每轮最多解读一个信号）",
    "invalid_configuration": "未调用（AI配置无效），已使用规则结论",
    "ai_output_truncated": "已调用，但输出被截断；已使用规则结论",
    "ai_empty_content": "已调用，但没有返回可用正文；已使用规则结论",
    "invalid_ai_output": "已调用，但结果格式未通过校验；已使用规则结论",
    "ai_rule_conflict": "已调用，但结果与规则方向冲突；已使用规则结论",
    "ai_policy_violation": "已调用，但结果未通过安全校验；已使用规则结论",
    "ai_timeout": "调用超时；已使用规则结论",
    "ai_rate_limited": "服务限流；已使用规则结论",
    "ai_auth_failed": "鉴权失败；已使用规则结论",
    "ai_insufficient_balance": "服务额度不足；已使用规则结论",
    "ai_endpoint_not_found": "接口不可用；已使用规则结论",
    "ai_invalid_request": "请求不兼容；已使用规则结论",
    "ai_invalid_parameters": "参数不兼容；已使用规则结论",
    "ai_provider_unavailable": "服务暂不可用；已使用规则结论",
    "ai_dns_failed": "网络解析失败；已使用规则结论",
    "ai_tls_failed": "安全连接失败；已使用规则结论",
    "ai_connection_failed": "网络连接失败；已使用规则结论",
    "ai_client_unavailable": "本地客户端不可用；已使用规则结论",
    "ai_redirect_rejected": "接口重定向被拒绝；已使用规则结论",
    "ai_http_error": "接口请求失败；已使用规则结论",
    "ai_client_error": "本地调用失败；已使用规则结论",
}

_LIFECYCLE_STAGE_TEXT = {
    "idle": "观察",
    "watching": "观察",
    "primed": "提前预警",
    "breakout": "条件满足",
    "launched": "启动加速",
    "risk": "风险",
    "cooling": "减弱",
    "failed": "失效",
}

_PHASE_POSITION_TEXT = {
    "high": "高位",
    "high_extended": "高位延伸",
    "upper": "偏高",
    "middle": "中部",
    "mid": "中部",
    "lower": "偏低",
    "low": "低位",
    "low_extended": "低位延伸",
    "insufficient": "缺数据",
    "unknown": "待确认",
    "": "待确认",
}

_PHASE_VOLUME_TEXT = {
    "sufficient": "达到确认要求",
    "expanding": "放量",
    "expanded": "放量",
    "high": "放量",
    "normal": "正常",
    "stable": "正常",
    "contracting": "缩量",
    "contracted": "缩量",
    "low": "缩量",
    "insufficient": "缺数据",
    "unavailable": "缺数据",
    "unknown": "待确认",
    "": "待确认",
}

_PHASE_EXECUTION_TEXT = {
    "ready": "条件已齐",
    "observe": "仅观察",
    "observe_only": "仅观察",
    "waiting": "等待确认",
    "waiting_retest": "等待回踩",
    "waiting_rebound": "等待反弹受阻",
    "retest_ready": "回踩/反弹时机已出现",
    "blocked_data": "数据不完整，暂缓确认",
    "blocked_extension": "行情已延伸，不追价",
    "blocked_volume": "成交量未达到确认要求",
    "blocked_flow_scale": "主动买卖规模不足",
    "blocked_crowding": "同方向过度拥挤，暂缓",
    "wait_new_positioning": "等待新增持仓确认",
    "wait_direction": "等待方向形成",
    "wait_confirmation": "等待完整确认",
    "blocked": "暂缓",
    "no_chase": "不追价",
    "invalidated": "停止沿用旧计划",
    "insufficient": "数据不足",
    "unknown": "规则观察",
    "": "规则观察",
}

_PHASE_MECHANISM_TEXT = {
    "new_longs": "新增多头推动",
    "new_shorts": "新增空头推动",
    "short_covering": "空头平仓推动",
    "long_liquidation": "多头去杠杆推动",
    "spot_led": "现货主动买入推动",
    "futures_led": "合约主动成交推动",
    "mixed": "多种力量共同推动",
    "unclear": "推动机制待确认",
    "unknown": "推动机制待确认",
    "": "推动机制待确认",
}

_PHASE_REASON_TEXT = {
    **_GATE_TEXT,
    "data_incomplete": "关键数据不完整",
    "smc_conflict": "1小时和4小时结构均与当前方向相反",
    "smc_neutral": "高周期结构尚未形成一致支持",
    "smc_insufficient": "高周期闭合数据不足",
    "wait_retest": "等待回踩确认",
    "wait_rebound": "等待反弹受阻",
    "extended_no_chase": "行情已经延伸，当前位置不追价",
    "high_position_no_chase": "已在72小时高位，不追涨",
    "low_position_no_chase": "已在72小时低位，不追空",
    "timing_not_ready": "时机条件尚未完成",
    "directional_cycle_failed": "原方向继续成立的条件已经不足",
    "directional_data_incomplete": "方向判断所需数据不完整",
    "position_data_insufficient": "72小时位置数据不足",
    "volume_data_insufficient": "成交量数据不足",
    "volume_below_confirmation_floor": "成交量未达到确认要求",
    "active_flow_scale_insufficient": "主动买卖规模不足",
    "active_flow_below_confirmation_floor": "主动买卖未达到确认要求",
    "bullish_72h_high_extended": "已在72小时高位延伸，不追涨",
    "bearish_72h_low_extended": "已在72小时低位延伸，不追空",
    "long_side_overcrowded": "多头方向过度拥挤",
    "short_side_overcrowded": "空头方向过度拥挤",
    "short_covering_not_new_long_positioning": "上涨主要来自空头平仓，不是新增多头",
    "long_liquidation_not_new_short_positioning": "下跌主要来自多头去杠杆，不是新增空头",
    "directional_hard_gates_incomplete": "方向确认条件尚未全部通过",
    "direction_not_resolved": "多空方向尚未形成一致证据",
    "phase_local_error": "位置与时机检查暂不可用",
    "none": "无；仍需遵守失效参考",
}


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _sequence(value: object) -> list[object]:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return list(value)
    return []


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _pct(value: object) -> str:
    number = _finite(value)
    if number is None:
        return "缺数据"
    if abs(number) > 1_000_000:
        return "数值异常"
    return f"{number:+.2f}%"


def _rate_pct(value: object) -> str:
    number = _finite(value)
    if number is None:
        return "缺数据"
    if not 0 <= number <= 100:
        return "数值异常"
    return f"{number:.1f}%"


def _multiple(value: object) -> str:
    number = _finite(value)
    if number is None:
        return "缺数据"
    if not 0 <= number <= 1_000_000:
        return "数值异常"
    return f"{number:.2f}倍"


def _money(value: object, *, signed: bool = False) -> str:
    number = _finite(value)
    if number is None:
        return "缺数据"
    if abs(number) > 1_000_000_000_000_000_000:
        return "数值异常"
    text = fmt_money(abs(number))
    if not signed:
        return text
    return f"{'+' if number >= 0 else '-'}{text}"


def _duration(value: object) -> str:
    parsed = _finite(value)
    if parsed is None:
        return "待确认"
    if parsed > 10 * 366 * 24 * 3600:
        return "数值异常"
    seconds = max(0, int(parsed))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    return f"{hours}小时{minutes:02d}分" if hours else f"{minutes}分钟"


def _active_side(signal: Mapping[str, Any], direction: str) -> str:
    if direction.startswith("bearish"):
        return "bearish"
    if direction.startswith("bullish"):
        return "bullish"
    bullish = _side_evidence_score(signal, "bullish")
    bearish = _side_evidence_score(signal, "bearish")
    if bullish is None and bearish is None:
        return ""
    return "bearish" if (bearish or 0.0) > (bullish or 0.0) else "bullish"


def _side_evidence_score(
    signal: Mapping[str, Any],
    side: str,
) -> float | None:
    canonical = signal.get(f"{side}_evidence_score")
    if canonical is not None:
        return _finite(canonical)
    return _finite(signal.get(f"{side}_readiness"))


def _headline(signal: Mapping[str, Any], direction: str) -> tuple[str, str, str]:
    status = str(signal.get("status") or "").strip()
    if status in _HEADLINE_TEXT:
        return _HEADLINE_TEXT[status]
    icon, label = _DIRECTION_TEXT[direction]
    return icon, label, _stage_text(signal, direction)


def _summary(signal: Mapping[str, Any]) -> str:
    status = str(signal.get("status") or "").strip()
    return _SUMMARY_TEXT.get(
        status,
        "当前证据只用于规则观察，等待下一完整收盘窗口确认。",
    )


def _score_lines(signal: Mapping[str, Any], direction: str) -> list[str]:
    bullish = _side_evidence_score(signal, "bullish")
    bearish = _side_evidence_score(signal, "bearish")
    if bullish is None and bearish is None:
        return ["• 多空证据分：待确认（不是涨跌概率）"]
    bullish_text = str(int(round(bullish))) if bullish is not None else "待确认"
    bearish_text = str(int(round(bearish))) if bearish is not None else "待确认"
    comparison = f"• 看涨 {bullish_text}｜看跌 {bearish_text}"
    if bullish is not None and bearish is not None:
        lead = abs(int(round(bullish)) - int(round(bearish)))
        leading = "看涨" if bullish >= bearish else "看跌"
        comparison += f"｜{leading}领先 {lead}"
    lines = [comparison, "• 证据分不是涨跌概率"]
    side = _active_side(signal, direction)
    groups = _mapping(signal.get(f"{side}_group_scores")) if side else {}
    caps = _mapping(signal.get("group_caps"))
    if groups:
        def component(name: str) -> str:
            value = _finite(groups.get(name))
            return "?" if value is None else str(int(round(value)))

        def cap(name: str, fallback: int) -> str:
            value = _finite(caps.get(name))
            return str(int(round(value))) if value is not None else str(fallback)

        lines.append(
            "• 构成："
            f"价与持仓 {component('price_oi_participation')}/{cap('price_oi_participation', 30)}｜"
            f"主动买卖 {component('active_funds')}/{cap('active_funds', 25)}｜"
            f"结构 {component('structure')}/{cap('structure', 25)}｜"
            f"执行 {component('execution_quality')}/{cap('execution_quality', 20)}"
        )
    if side:
        raw = _finite(signal.get(f"{side}_raw_score"))
        adjustment = _finite(_mapping(signal.get("risk_adjustments")).get(side))
        final = _side_evidence_score(signal, side)
        if raw is not None and adjustment is not None and final is not None:
            lines.append(
                f"• 当前方向：原始 {int(round(raw))}｜拥挤 {int(round(adjustment or 0)):+d}｜"
                f"最终 {int(round(final))}"
            )
    return lines


def _gate_lines(signal: Mapping[str, Any], direction: str) -> list[str]:
    side = _active_side(signal, direction)
    hard_gates = _mapping(signal.get("hard_gates"))
    gates = _mapping(hard_gates.get(side)) if side else {}
    if not gates:
        return ["• 确认条件：待确认"]
    passed_count = sum(value is True for value in gates.values())
    total_count = len(gates)
    failed = [
        _GATE_TEXT.get(str(key), "其他确认条件未通过")
        for key, passed in gates.items()
        if passed is not True
    ]
    if not failed:
        return [f"• 确认门槛：{passed_count}/{total_count}｜全部通过"]
    unique_failed = list(dict.fromkeys(failed))
    rendered = "、".join(unique_failed[:3])
    if len(unique_failed) > 3:
        rendered += f"等{len(unique_failed)}项"
    return [
        f"• 确认门槛：{passed_count}/{total_count}｜未过："
        f"{rendered}"
    ]


def _flow_line(label: str, value: object) -> str:
    flow = _mapping(value)
    status = str(flow.get("status") or "")
    if status == "no_trades":
        return f"• {label}：本窗口无成交"
    net = _finite(flow.get("net_usd"))
    ratio = _finite(flow.get("ratio"))
    if net is None:
        return f"• {label}：{_FLOW_STATUS_TEXT.get(status, '缺数据')}"
    ratio_text = (
        "缺数据"
        if ratio is None
        else "数值异常"
        if abs(ratio) > 1
        else f"{ratio * 100:+.1f}%"
    )
    return f"• {label}：{_money(net, signed=True)}｜主动占比 {ratio_text}"


def _market_strength_lines(
    item: Mapping[str, Any],
    signal: Mapping[str, Any],
) -> list[str]:
    metrics = (
        item.get("price_15m"),
        item.get("oi_15m"),
        item.get("price_1h"),
        item.get("oi_1h"),
        item.get("volume_ratio"),
    )
    spot = item.get("spot_cvd_1h")
    futures = item.get("futures_cvd_1h")
    if not any(_finite(value) is not None for value in metrics) and not (
        isinstance(spot, Mapping) or isinstance(futures, Mapping)
    ):
        return []
    thresholds = _mapping(signal.get("thresholds"))
    price_threshold = _finite(thresholds.get("price_change_pct"))
    oi_threshold = _finite(thresholds.get("oi_change_pct"))
    cvd_threshold = _finite(thresholds.get("cvd_ratio"))
    lines = [
        f"• 1小时：价格 {_pct(item.get('price_1h'))}｜持仓量 {_pct(item.get('oi_1h'))}",
        _flow_line("现货1小时", spot),
        _flow_line("合约1小时", futures),
        f"• 15分钟发现：价格 {_pct(item.get('price_15m'))}｜持仓量 {_pct(item.get('oi_15m'))}｜"
        f"成交量 {_multiple(item.get('volume_ratio'))}",
    ]
    threshold_parts: list[str] = []
    if price_threshold is not None:
        threshold_parts.append(f"价格±{price_threshold:.1f}%")
    if oi_threshold is not None:
        threshold_parts.append(f"持仓+{oi_threshold:.1f}%")
    if cvd_threshold is not None:
        threshold_parts.append(f"主动占比±{cvd_threshold * 100:.1f}%")
    if threshold_parts:
        lines.append(f"• 本品类门槛：{'｜'.join(threshold_parts)}")
    return lines


def _background_lines(item: Mapping[str, Any]) -> list[str]:
    values = (
        item.get("price_4h"),
        item.get("oi_4h"),
        item.get("price_24h"),
        item.get("oi_24h"),
    )
    if not any(_finite(value) is not None for value in values):
        return []
    oi_24h = _finite(item.get("oi_24h"))
    oi_24h_status = str(item.get("oi_24h_status") or "")
    oi_24h_text = (
        f"{oi_24h:+.2f}%（严格闭合）"
        if oi_24h is not None
        else {
            "insufficient_history": "历史不足",
            "gap": "窗口不连续",
            "boundary_missing": "闭合点缺失",
            "invalid": "数据异常",
            "core_invalid": "核心窗口异常",
        }.get(oi_24h_status, "缺数据")
    )
    return [
        f"• 4小时：价格 {_pct(item.get('price_4h'))}｜持仓量 {_pct(item.get('oi_4h'))}",
        f"• 24小时：价格 {_pct(item.get('price_24h'))}（滚动）｜持仓量 {oi_24h_text}",
    ]


def _market_size_lines(item: Mapping[str, Any]) -> list[str]:
    market_cap = _finite(item.get("mcap"))
    quote_volume = _finite(item.get("quote_volume"))
    open_interest = _finite(item.get("closed_oi_usd"))
    if not any(value is not None and value > 0 for value in (market_cap, quote_volume, open_interest)):
        return []
    parts = []
    if market_cap is not None and market_cap > 0:
        parts.append(f"市值 {_money(market_cap)}")
    if quote_volume is not None and quote_volume > 0:
        parts.append(f"24小时成交额 {_money(quote_volume)}")
    if open_interest is not None and open_interest > 0:
        parts.append(f"当前持仓 {_money(open_interest)}")
    tier = _short(item.get("liquidity_tier"), limit=24)
    lines = [f"• {'｜'.join(parts)}"]
    if tier:
        lines.append(f"• 流动性：{tg_escape(tier)}")
    return lines


def _data_quality_lines(item: Mapping[str, Any], signal: Mapping[str, Any]) -> list[str]:
    confirmation = _mapping(item.get("data_confirmation"))
    lines: list[str] = []
    ready = int(_finite(confirmation.get("ready_count")) or 0)
    total = int(_finite(confirmation.get("total_count")) or 0)
    if total > 0:
        status = "完整" if confirmation.get("status") == "confirmed" else "缺项"
        lines.append(f"• Binance原生：{ready}/{total}｜{status}｜仅使用已收盘数据")
        missing = [
            tg_escape(_short(value, limit=24))
            for value in _sequence(confirmation.get("missing"))
        ]
        if missing:
            lines.append(f"• 缺少：{'、'.join(missing[:4])}")
    else:
        lines.append(f"• {_data_text(signal)}")
    missing_fields = list(dict.fromkeys([
        _MISSING_FIELD_TEXT.get(str(value), "其他必要数据")
        for value in _sequence(signal.get("missing_fields"))
    ]))
    if missing_fields:
        rendered = "、".join(missing_fields[:4])
        if len(missing_fields) > 4:
            rendered += f"等{len(missing_fields)}项"
        lines.append(f"• 方向模型缺少：{rendered}")
    return lines


def _lifecycle_lines(item: Mapping[str, Any]) -> list[str]:
    lifecycle = _mapping(item.get("launch_lifecycle"))
    if not lifecycle:
        return []
    cycle_no = int(_finite(lifecycle.get("cycle_no")) or 0)
    observation_no = int(_finite(lifecycle.get("observation_no")) or 0)
    peak = _LIFECYCLE_STAGE_TEXT.get(
        str(lifecycle.get("peak_stage") or ""),
        "待确认",
    )
    cycle_text = f"第{cycle_no}轮" if cycle_no > 0 else "周期待确认"
    observation_text = (
        f"第{observation_no}次完整观察"
        if observation_no > 0
        else "观察次数待确认"
    )
    lines = [
        f"• {cycle_text}｜{observation_text}｜"
        f"持续 {_duration(lifecycle.get('duration_sec'))}｜最高 {peak}",
    ]
    delta = _mapping(lifecycle.get("delta_from_first"))
    score_delta_value = (
        delta.get("evidence_score")
        if delta.get("evidence_score") is not None
        else delta.get("score")
    )
    delta_values = (
        _finite(delta.get("price_pct")),
        _finite(delta.get("oi_pct")),
        _finite(score_delta_value),
    )
    if any(value is not None for value in delta_values):
        score_delta = _finite(score_delta_value)
        score_text = (
            f"{int(round(score_delta)):+d}分"
            if score_delta is not None
            else "待确认"
        )
        lines.append(
            f"• 较首次：价格 {_pct(delta.get('price_pct'))}｜持仓量 {_pct(delta.get('oi_pct'))}｜"
            f"证据分 {score_text}"
        )
    outcome = _mapping(lifecycle.get("outcome_evaluation"))
    reliability = _mapping(outcome.get("reliability"))
    samples = int(_finite(reliability.get("completed_samples")) or 0)
    minimum = max(1, int(_finite(reliability.get("minimum_samples")) or 20))
    direction_filtered = reliability.get("direction_filtered") is True
    if reliability.get("rates_available") is True and direction_filtered:
        threshold = _finite(reliability.get("follow_through_threshold_pct"))
        summary = (
            f"• 同方向历史：确认率 {_rate_pct(reliability.get('confirmed_rate_pct'))}｜"
            f"跟随率 {_rate_pct(reliability.get('followed_through_rate_pct'))}｜n={samples}"
        )
        if threshold is not None:
            summary += f"｜跟随门槛 {threshold:.1f}%"
        lines.append(summary)
    elif reliability and direction_filtered:
        scope = (
            "同品类同流动性"
            if reliability.get("aggregation_scope") == "asset_liquidity"
            else "同方向"
        )
        lines.append(f"• {scope}样本：积累中 {samples}/{minimum}（不展示胜率）")
    elif reliability:
        lines.append("• 历史方向未识别：暂不展示比例")
    return lines


def _outcome_lines(item: Mapping[str, Any]) -> list[str]:
    lifecycle = _mapping(item.get("launch_lifecycle"))
    outcome = _mapping(lifecycle.get("outcome_evaluation"))
    progress = _mapping(outcome.get("outcome")) or _mapping(outcome.get("progress"))
    if not progress:
        return []
    return [
        f"• 最大有利变化：{_pct(progress.get('max_favorable_return_pct'))}",
        f"• 最大不利变化：{_pct(progress.get('max_adverse_return_pct'))}",
        f"• 结束时变化：{_pct(progress.get('end_return_pct'))}",
        f"• 持仓量最大增加：{_pct(progress.get('max_oi_increase_pct'))}",
    ]


def _short(value: object, *, limit: int = 120) -> str:
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def _number_text(value: object) -> str:
    number = _finite(value)
    if number is None:
        return ""
    absolute = abs(number)
    if absolute >= 1_000_000_000 or (0 < absolute < 0.0000001):
        return f"{number:.6g}"
    if absolute >= 1000:
        return f"{number:,.2f}".rstrip("0").rstrip(".")
    if absolute >= 1:
        return f"{number:.4f}".rstrip("0").rstrip(".")
    return f"{number:.8f}".rstrip("0").rstrip(".")


def _price_text(value: object) -> str:
    if isinstance(value, Mapping):
        low = _number_text(value.get("low", value.get("min")))
        high = _number_text(value.get("high", value.get("max")))
        if low and high:
            return f"{low}–{high}"
        return low or high
    values = _sequence(value)
    if len(values) >= 2:
        low, high = _number_text(values[0]), _number_text(values[1])
        if low and high:
            return f"{low}–{high}"
    return _short(value, limit=60)


def _targets_text(value: object) -> str:
    values = _sequence(value)
    if values:
        rendered = [_number_text(item) or _short(item, limit=32) for item in values[:3]]
        return " / ".join(item for item in rendered if item)
    return _short(value, limit=90)


def _direction_key(signal: Mapping[str, Any]) -> str:
    value = str(signal.get("direction") or "none").strip().lower()
    return value if value in _DIRECTION_TEXT else "none"


def _launch_phase(item: Mapping[str, Any]) -> Mapping[str, Any]:
    value = item.get("launch_phase")
    return value if isinstance(value, Mapping) else {}


def _phase_side(phase: Mapping[str, Any], signal: Mapping[str, Any]) -> str:
    bias = str(phase.get("bias") or "").strip().lower()
    if bias in {"bullish", "bull", "long", "up"}:
        return "bullish"
    if bias in {"bearish", "bear", "short", "down"}:
        return "bearish"
    direction = _direction_key(signal)
    if direction.startswith("bullish"):
        return "bullish"
    if direction.startswith("bearish"):
        return "bearish"
    return ""


def _current_lifecycle_stage(item: Mapping[str, Any]) -> str:
    lifecycle = _mapping(item.get("launch_lifecycle"))
    return str(
        lifecycle.get("current_stage")
        or lifecycle.get("stage")
        or item.get("stage")
        or ""
    ).strip().lower()


def _confirmation_consistent(
    item: Mapping[str, Any],
    signal: Mapping[str, Any],
    phase: Mapping[str, Any],
    side: str,
) -> bool:
    phase_bias = str(phase.get("bias") or "").strip().lower()
    signal_direction = _direction_key(signal)
    signal_side = (
        "bullish"
        if signal_direction.startswith("bullish")
        else "bearish"
        if signal_direction.startswith("bearish")
        else ""
    )
    if phase and phase_bias in {"bullish", "bearish"} and signal_side and phase_bias != signal_side:
        return False
    timing = str(phase.get("timing_stage") or "").strip().lower()
    status = str(signal.get("status") or "").strip()
    claimed = timing in {"confirmed", "retest_ready", "continuation"} or status in {
        "多头确认",
        "空头确认",
    }
    if not claimed:
        return True
    if signal.get("data_complete") is False:
        return False
    hard_gates = _mapping(signal.get("hard_gates"))
    gates = _mapping(hard_gates.get(side)) if side else {}
    if gates and not all(value is True for value in gates.values()):
        return False
    if hard_gates.get(f"{side}_passed") is False:
        return False
    if phase and timing in {"confirmed", "retest_ready", "continuation"}:
        return bool(
            signal.get("data_complete") is True
            and gates
            and hard_gates.get(f"{side}_passed") is True
        )
    return True


def _primary_stage(
    item: Mapping[str, Any],
    signal: Mapping[str, Any],
    phase: Mapping[str, Any],
    *,
    confirmation_consistent: bool,
) -> str:
    timing = str(phase.get("timing_stage") or "").strip().lower()
    lifecycle_stage = _current_lifecycle_stage(item)
    status = str(signal.get("status") or "").strip()
    if timing == "invalidated" or lifecycle_stage == "failed":
        return "invalidated"
    if timing == "insufficient" or status == "数据不足":
        return "insufficient"
    if not confirmation_consistent:
        return "confirmation_conflict"
    if timing == "extended_no_chase":
        return "extended_no_chase"
    if timing == "exhausted":
        return "exhausted"
    if timing == "conflicting":
        return "confirmation_conflict"
    if lifecycle_stage == "risk":
        return "risk"
    if lifecycle_stage == "cooling":
        return "cooling"
    if lifecycle_stage == "launched":
        return "launched"
    if timing in {"forming", "confirmed", "retest_ready", "continuation"}:
        return timing
    if timing in {"discovered", "crowding_watch", "mechanism_watch"}:
        return timing
    return "legacy"


def _phase_title(
    item: Mapping[str, Any],
    signal: Mapping[str, Any],
    phase: Mapping[str, Any],
    side: str,
    stage: str,
) -> tuple[str, str, str]:
    direction_text = "看涨" if side == "bullish" else "看跌" if side == "bearish" else "方向"
    position = str(phase.get("position_status") or "").strip().lower()
    if stage == "confirmation_conflict":
        return "⚪", "确认信息冲突", "本轮降级观察"
    if stage == "insufficient":
        return "⚪", "数据不足", "本轮不确认方向"
    if stage == "invalidated":
        return "⚫", f"{direction_text}信号失效", "本轮周期结束"
    if stage == "extended_no_chase":
        if side == "bearish":
            return "🧊", "下跌已延伸", "低位不追空"
        return "♨️", "上涨已延伸", "高位不追涨"
    if stage == "exhausted":
        return "⚠️", f"{direction_text}动能衰竭", "等待新的完整结构"
    if stage == "crowding_watch":
        subtitle = "高位不追涨" if side == "bullish" else "低位不追空"
        return "⚠️", f"{direction_text}方向拥挤", subtitle
    if stage == "mechanism_watch":
        if side == "bearish":
            return "🟠", "下跌释放观察", "不是新增空头确认"
        return "🟡", "反弹释放观察", "不是新增多头确认"
    if stage == "discovered":
        return "🔎", f"{direction_text}异动发现", "等待方向证据形成"
    if stage == "risk":
        return "⚠️", f"{direction_text}结构转弱", "优先观察失效位"
    if stage == "cooling":
        return "🔵", f"{direction_text}降温", "动能减弱，暂停追加"
    if stage == "launched":
        subtitle = "趋势延续，谨防回撤" if side == "bullish" else "趋势延续，谨防反抽"
        return ("🟢" if side == "bullish" else "🔴"), f"{direction_text}加速", subtitle
    if stage == "continuation":
        if side == "bearish" and position in {"low", "lower", "low_extended"}:
            return "🔴", "下跌延续", "低位不追空"
        if side == "bullish" and position in {"high", "upper", "high_extended"}:
            return "🟢", "上涨延续", "高位不追涨"
        return ("🟢" if side == "bullish" else "🔴"), f"{direction_text}延续", "结构仍有效"
    if stage == "retest_ready":
        if side == "bearish":
            return "🔴", "看跌反弹受阻", "观察时机已出现"
        return "🟢", "看涨回踩确认", "观察时机已出现"
    if stage == "confirmed":
        if side == "bearish":
            return "🔴", "看跌确认", "等待反弹，不追空"
        return "🟢", "看涨确认", "等待回踩，不追价"
    if stage == "forming":
        if side == "bearish":
            return "🟠", "看跌预警", "证据形成中"
        if side == "bullish":
            return "🟡", "看涨预警", "证据形成中"
        return "⚪", "方向观察", "证据形成中"
    return _headline(signal, _direction_key(signal))


def _phase_conclusion(
    signal: Mapping[str, Any],
    phase: Mapping[str, Any],
    side: str,
    stage: str,
) -> str:
    if stage == "confirmation_conflict":
        return "状态声称已经确认，但完整数据或确认门槛不一致；已安全降级。"
    if stage == "insufficient":
        return "关键数据不足，本轮只记录事实，不补0、不确认方向。"
    if stage == "invalidated":
        return "原方向继续成立的条件已经不足；旧计划停止沿用。"
    if stage == "extended_no_chase":
        return "方向尚未反转，但行情已经延伸；当前位置不追涨、不追空。"
    if stage == "exhausted":
        return "当前动能明显减弱，尚未形成反向确认。"
    if stage == "crowding_watch":
        return "方向证据仍在，但同方向已经拥挤；本轮只观察，不追价。"
    if stage == "mechanism_watch":
        return "当前变化主要来自旧仓退出，不作为新增方向仓位确认。"
    if stage == "discovered":
        return "15分钟已经发现异动，但方向、位置或执行条件仍需完整窗口确认。"
    if stage in {"risk", "cooling"}:
        return "原方向动能减弱，先观察失效位，不追加风险。"
    mechanism = _PHASE_MECHANISM_TEXT.get(
        str(phase.get("mechanism") or "").strip().lower(),
        "推动机制待确认",
    )
    if stage in {"launched", "continuation"}:
        return f"{('偏多' if side == 'bullish' else '偏空')}结构仍在延续；{mechanism}。"
    if stage in {"confirmed", "retest_ready"}:
        return f"方向条件已经通过；{mechanism}，仍需遵守失效参考。"
    return _summary(signal)


def _flow_brief(label: str, value: object) -> str:
    flow = _mapping(value)
    status = str(flow.get("status") or "")
    if status == "no_trades":
        return f"{label}无成交"
    net = _finite(flow.get("net_usd"))
    ratio = _finite(flow.get("ratio"))
    if net is None:
        return f"{label}{_FLOW_STATUS_TEXT.get(status, '缺数据')}"
    ratio_text = "缺占比" if ratio is None else "占比异常" if abs(ratio) > 1 else f"{ratio * 100:+.1f}%"
    return f"{label}{_money(net, signed=True)}/{ratio_text}"


def _compact_market_lines(item: Mapping[str, Any]) -> list[str]:
    return [
        f"• 15分钟：价 {_pct(item.get('price_15m'))}｜持仓 {_pct(item.get('oi_15m'))}｜量 {_multiple(item.get('volume_ratio'))}",
        f"• 1小时：价 {_pct(item.get('price_1h'))}｜持仓 {_pct(item.get('oi_1h'))}",
        "• 主动买卖："
        f"{_flow_brief('现货', item.get('spot_cvd_1h'))}｜"
        f"{_flow_brief('合约', item.get('futures_cvd_1h'))}",
    ]


def _compact_score_lines(
    signal: Mapping[str, Any],
    direction: str,
    *,
    discovery_score: object = None,
) -> list[str]:
    score_lines = _score_lines(signal, direction)
    comparison = score_lines[0] if score_lines else "• 多空证据分：待确认"
    comparison = comparison.replace("看涨 ", "看涨 ").replace("看跌 ", "看跌 ")
    component = next((line for line in score_lines if line.startswith("• 构成：")), "")
    discovery = _finite(discovery_score)
    discovery_line = (
        f"• 发现分 {int(round(discovery))}/100｜只负责发现异动"
        if discovery is not None
        else "• 发现分：待确认｜只负责发现异动"
    )
    return [discovery_line, comparison, *([component] if component else [])]


def _data_status_line(item: Mapping[str, Any], signal: Mapping[str, Any]) -> str:
    confirmation = _mapping(item.get("data_confirmation"))
    ready = int(_finite(confirmation.get("ready_count")) or 0)
    total = int(_finite(confirmation.get("total_count")) or 0)
    if total > 0:
        status = "完整" if confirmation.get("status") == "confirmed" else "缺项"
        return f"• 数据：Binance {ready}/{total}｜{status}｜仅用已收盘数据"
    return f"• 数据：{_data_text(signal)}"


def _position_timing_lines(
    phase: Mapping[str, Any],
    stage: str,
    side: str,
) -> list[str]:
    position = _PHASE_POSITION_TEXT.get(
        str(phase.get("position_status") or "").strip().lower(),
        "待确认",
    )
    volume = _PHASE_VOLUME_TEXT.get(
        str(phase.get("volume_status") or "").strip().lower(),
        "待确认",
    )
    execution = _PHASE_EXECUTION_TEXT.get(
        str(phase.get("execution_status") or "").strip().lower(),
        "规则观察",
    )
    timing = {
        "forming": "形成中",
        "confirmed": "已确认",
        "retest_ready": "回踩/反弹确认",
        "continuation": "趋势延续",
        "extended_no_chase": "行情已延伸",
        "exhausted": "动能衰竭",
        "risk": "结构转弱",
        "cooling": "动能降温",
        "launched": "启动加速",
        "invalidated": "已失效",
        "insufficient": "数据不足",
        "confirmation_conflict": "确认信息冲突",
        "crowding_watch": "方向拥挤",
        "mechanism_watch": "旧仓释放",
        "discovered": "初步发现",
    }.get(stage, "规则观察")
    if stage == "continuation" and side == "bearish" and position in {"低位", "偏低"}:
        execution = "低位不追空"
    if stage in {"continuation", "extended_no_chase"} and side == "bullish" and position in {"高位", "偏高"}:
        execution = "高位不追涨"
    return [
        f"• 72小时位置：{position}｜成交量：{volume}",
        f"• 时机：{timing}｜处理：{execution}",
    ]


def _reason_text(value: object) -> str:
    key = str(value or "").strip()
    if not key:
        return ""
    if key in _PHASE_REASON_TEXT:
        return _PHASE_REASON_TEXT[key]
    return "其他关键条件尚未通过"


def _primary_block_reason(
    item: Mapping[str, Any],
    signal: Mapping[str, Any],
    phase: Mapping[str, Any],
    side: str,
    stage: str,
) -> str:
    explicit = _reason_text(phase.get("primary_block_reason"))
    if explicit:
        return explicit
    if stage == "confirmation_conflict":
        if signal.get("data_complete") is not True:
            return "关键数据不完整"
    hard_gates = _mapping(signal.get("hard_gates"))
    gates = _mapping(hard_gates.get(side)) if side else {}
    for key, passed in gates.items():
        if passed is not True:
            return _GATE_TEXT.get(str(key), "其他确认条件尚未通过")
    smc = _mapping(item.get("smc_filter"))
    smc_status = str(smc.get("status") or "")
    if smc_status == "conflicting":
        return _PHASE_REASON_TEXT["smc_conflict"]
    if smc_status == "neutral":
        return _PHASE_REASON_TEXT["smc_neutral"]
    if smc_status in {"insufficient", ""}:
        return _PHASE_REASON_TEXT["smc_insufficient"]
    if stage == "extended_no_chase":
        return "已在72小时低位，不追空" if side == "bearish" else "已在72小时高位，不追涨"
    if stage in {"risk", "cooling", "exhausted"}:
        return "当前动能减弱，等待新的完整结构"
    if stage == "invalidated":
        return "原方向继续成立的条件已经不足"
    if stage in {"confirmed", "retest_ready", "continuation", "launched"}:
        return "无；仍需遵守失效参考"
    return "等待下一完整收盘窗口"


def _translated_inline(value: object, *, empty: str, limit: int) -> str:
    lines = _translated_lines(value, empty=empty, limit=limit)
    return "；".join(line.removeprefix("• ") for line in lines)


def _compact_evidence_lines(signal: Mapping[str, Any], direction: str) -> list[str]:
    evidence = _mapping(signal.get("evidence"))
    if direction.startswith("bearish"):
        support = evidence.get("bearish", signal.get("supporting_evidence"))
        counter = evidence.get("bullish", signal.get("counter_evidence"))
    else:
        support = evidence.get("bullish", signal.get("supporting_evidence"))
        counter = evidence.get("bearish", signal.get("counter_evidence"))
    if direction in {"bullish_divergence_watch", "bearish_divergence_watch"}:
        support = signal.get("divergence_evidence") or support
    return [
        f"✅ 支持：{_translated_inline(support, empty='暂无明确支持证据', limit=2)}",
        f"⚠️ 反证：{_translated_inline(counter, empty='暂无明显反向证据', limit=1)}",
    ]


def _compact_lifecycle_line(item: Mapping[str, Any]) -> str:
    lifecycle = _mapping(item.get("launch_lifecycle"))
    if not lifecycle:
        return "⏱️ 生命周期：本轮首次观察"
    cycle_no = int(_finite(lifecycle.get("cycle_no")) or 0)
    observation_no = int(_finite(lifecycle.get("observation_no")) or 0)
    current_key = _current_lifecycle_stage(item)
    current = _LIFECYCLE_STAGE_TEXT.get(current_key, "待确认")
    peak = _LIFECYCLE_STAGE_TEXT.get(str(lifecycle.get("peak_stage") or ""), "待确认")
    prefix = f"第{cycle_no}轮" if cycle_no > 0 else "本轮"
    observation = f"第{observation_no}次完整观察" if observation_no > 0 else "首次观察"
    return (
        f"⏱️ 生命周期：{prefix}｜{observation}｜持续 {_duration(lifecycle.get('duration_sec'))}｜"
        f"当前{current}｜最高{peak}"
    )


def _compact_ai_lines(item: Mapping[str, Any]) -> list[str]:
    ai_text = _short(item.get("ai_interpretation"), limit=100)
    status = _short(item.get("ai_interpretation_status"), limit=64)
    source = _short(item.get("ai_interpretation_source"), limit=16)
    if not status and ai_text:
        status = "available"
    if status == "on_demand":
        return [
            f"{tg_bold('🤖 AI按需解读')}：需要时点击消息下方按钮；本卡片仍是规则结论"
        ]
    if status == "available" and ai_text:
        origin = (
            "已完成（复用已校验结果）"
            if source == "cache"
            else "已完成（本轮调用）"
            if source == "provider"
            else "已完成"
        )
        return [
            f"{tg_bold('🤖 AI参与')}：{origin}",
            f"{tg_bold('AI白话解读')}：{tg_escape(ai_text)}（仅本行由AI生成；规则结论不变）",
        ]
    if status == "available":
        status = "invalid_ai_output"
    detail = _AI_STATUS_TEXT.get(status, "未调用（当前卡片没有可用AI结果）")
    return [f"{tg_bold('🤖 AI参与')}：{tg_escape(detail)}"]


def _compact_plan_lines(
    item: Mapping[str, Any],
    signal: Mapping[str, Any],
    phase: Mapping[str, Any],
    side: str,
    stage: str,
) -> list[str]:
    if phase and phase.get("plan_eligible") is not True:
        return []
    if stage not in {"confirmed", "retest_ready", "legacy"}:
        return []
    if stage == "legacy" and str(signal.get("status") or "") not in {"多头确认", "空头确认"}:
        return []
    hard_gates = _mapping(signal.get("hard_gates"))
    gates = _mapping(hard_gates.get(side)) if side else {}
    if not (
        signal.get("data_complete") is True
        and gates
        and all(value is True for value in gates.values())
        and hard_gates.get(f"{side}_passed") is True
    ):
        return []
    entry = _price_text(item.get("entry_zone", signal.get("entry_zone")))
    invalidation = _price_text(
        item.get("invalidation_price", item.get("invalidation", signal.get("invalidation")))
    )
    targets = _targets_text(item.get("targets", signal.get("targets")))
    rr = _finite(item.get("risk_reward_ratio", signal.get("risk_reward_ratio")))
    if not (entry and invalidation and targets and rr is not None and rr >= 2.0):
        return []
    if str(_mapping(item.get("smc_filter")).get("status") or "") != "supportive":
        return []
    return [
        tg_bold("📍 观察计划"),
        f"• 观察区：{tg_escape(entry)}｜失效参考：{tg_escape(invalidation)}",
        f"• 目标参考：{tg_escape(targets)}｜收益风险参考：{rr:.2f}",
    ]


def _stage_text(signal: Mapping[str, Any], direction: str) -> str:
    raw = str(signal.get("stage") or signal.get("status") or "").strip()
    if raw in _STATUS_TEXT:
        return _STATUS_TEXT[raw]
    if direction == "bullish":
        return "等待回踩确认"
    if direction == "bearish":
        return "等待反弹受阻"
    return "等待更多证据"


def _direction_text(value: object) -> str:
    if isinstance(value, Mapping):
        value = value.get("direction", value.get("status"))
    normalized = str(value or "").strip().lower()
    return {
        "bullish": "偏多",
        "bull": "偏多",
        "up": "偏多",
        "bearish": "偏空",
        "bear": "偏空",
        "down": "偏空",
        "neutral": "震荡",
        "range": "震荡",
        "mixed": "方向分歧",
        "conflict": "方向分歧",
        "unavailable": "缺数据",
        "degraded": "数据不完整",
        "unknown": "待确认",
        "": "待确认",
    }.get(normalized, "待确认")


def _timeframe_lines(item: Mapping[str, Any]) -> list[str]:
    multi = _mapping(item.get("multi_timeframe"))
    groups = _mapping(multi.get("role_groups"))
    lines: list[str] = []
    for key, label in _ROLE_LABELS:
        value = groups.get(key, item.get(key))
        lines.append(f"• {label}：{_direction_text(value)}")
    background = _mapping(multi.get("rolling_24h_background"))
    if background:
        lines.append("• 滚动24小时：仅作背景，不重复计分")
    return lines


def _translated_lines(value: object, *, empty: str, limit: int = 5) -> list[str]:
    values = _sequence(value)
    if not values:
        return [f"• {empty}"]
    rendered: list[str] = []
    unknown_added = False
    for item in values:
        key = str(item or "").strip()
        text = _EVIDENCE_TEXT.get(key) or _LIMITATION_TEXT.get(key)
        if text is None:
            if unknown_added:
                continue
            text = "其他规则证据已记录"
            unknown_added = True
        if text:
            rendered.append(f"• {tg_escape(text)}")
        if len(rendered) >= limit:
            break
    return rendered or [f"• {empty}"]


def _asset_profile(item: Mapping[str, Any], signal: Mapping[str, Any]) -> tuple[str, str]:
    key = str(
        item.get("asset_subclass")
        or item.get("asset_category")
        or signal.get("asset_profile")
        or ""
    ).strip().lower()
    if key == "tradfi":
        key = str(item.get("asset_subclass") or "").strip().lower()
    return _ASSET_PROFILE.get(key, ("未分类资产", "品类尚未确认，使用保守门槛并等待更完整的数据。"))


def _instrument_line(item: Mapping[str, Any]) -> str:
    symbol = _short(item.get("symbol") or item.get("coin"), limit=24)
    if not symbol:
        return "币种：待确认"
    return coin_link({"symbol": symbol})


def _crowding_text(item: Mapping[str, Any], signal: Mapping[str, Any]) -> str:
    adjustments = _mapping(signal.get("risk_adjustments"))
    side = _active_side(signal, _direction_key(signal))
    reasons = [
        str(value)
        for value in _sequence(adjustments.get(f"{side}_reasons"))
    ]
    translated = [_EVIDENCE_TEXT[value] for value in reasons if value in _EVIDENCE_TEXT]
    funding = _finite(item.get("funding_rate_pct", item.get("funding_pct")))
    basis = _finite(item.get("basis_pct"))
    values: list[str] = []
    if funding is not None:
        values.append(
            "资金费率 数值异常"
            if abs(funding) > 1_000_000
            else f"资金费率 {funding:+.4f}%"
        )
    if basis is not None:
        values.append(
            "基差 数值异常"
            if abs(basis) > 1_000_000
            else f"基差 {basis:+.3f}%"
        )
    if translated:
        values.append("；".join(dict.fromkeys(translated)))
    return "｜".join(values) if values else "待确认"


def _data_text(signal: Mapping[str, Any]) -> str:
    complete = signal.get("data_complete")
    if complete is True:
        return "完整，仅使用已收盘数据"
    if str(signal.get("observation_mode") or "") == "futures_only_spot_pair_not_listed":
        return "同名现货对不存在，仅作合约观察；不确认方向、不调用AI"
    missing = [_short(value, limit=32) for value in _sequence(signal.get("missing_fields"))]
    if complete is False or missing:
        return "不完整，本轮降级为观察"
    return "待确认"


def _signal(item: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in (
        "directional_readiness",
        "launch_directional_readiness",
        "directional_signal",
    ):
        value = item.get(key)
        if isinstance(value, Mapping):
            return value
    return item


def _smc_filter_lines(item: Mapping[str, Any]) -> list[str]:
    result = item.get("smc_filter")
    if not isinstance(result, Mapping):
        return [f"{tg_bold('🧭 SMC二次过滤')}：数据不足｜不改发现分和方向证据分"]
    status = str(result.get("status") or "insufficient")
    status_text = {
        "supportive": "同向支持",
        "neutral": "中性观察",
        "conflicting": "高周期冲突",
        "insufficient": "数据不足",
    }.get(status, "数据不足")
    structure_text = {
        "bullish": "偏多",
        "bearish": "偏空",
        "neutral": "中性",
        "unavailable": "不可用",
    }
    one_hour = structure_text.get(
        str(result.get("one_hour_structure") or "unavailable"),
        "不可用",
    )
    four_hour = structure_text.get(
        str(result.get("four_hour_structure") or "unavailable"),
        "不可用",
    )
    return [
        f"{tg_bold('🧭 SMC二次过滤')}：{tg_escape(status_text)}｜"
        f"1小时{tg_escape(one_hour)}｜4小时{tg_escape(four_hour)}"
        "（只过滤，不改发现分和方向证据分）"
    ]


def _visible_length(text: str) -> int:
    return len(unescape(re.sub(r"<[^>]*>", "", text)))


def _bounded_card(
    lines: Sequence[str],
    *,
    max_chars: int,
    fallback_lines: Sequence[str],
) -> str:
    rendered = "\n".join(lines)
    if _visible_length(rendered) <= max_chars:
        return rendered
    safe_lines = list(fallback_lines)
    while len(safe_lines) > 7 and _visible_length("\n".join(safe_lines)) > max_chars:
        # Preserve the fixed lifecycle/safety tail.  Optional middle facts are
        # safer to remove than the user-facing end state or disclaimer.
        safe_lines.pop(-5)
    return "\n".join(safe_lines)


def _invalidated_cycle_card(
    item: Mapping[str, Any],
    signal: Mapping[str, Any],
    invalidated: Mapping[str, Any],
    *,
    max_chars: int,
) -> str:
    previous = str(invalidated.get("previous_direction") or "")
    previous_text = "看涨" if previous == "bullish" else "看跌" if previous == "bearish" else "原方向"
    reason = str(invalidated.get("reason") or "directional_cycle_failed")
    next_direction = _direction_key(signal)
    next_text = _DIRECTION_TEXT[next_direction][1]
    category, _category_risk = _asset_profile(item, signal)
    category = _short(item.get("asset_category_label"), limit=48) or category
    reason_text = {
        "direction_changed": "新一轮完整数据转为相反方向",
        "two_closes_below_invalidation": "连续两个完整窗口收盘跌破看涨失效位",
        "two_closes_above_invalidation": "连续两个完整窗口收盘升破看跌失效位",
        "two_windows_below_watch_score": "连续两个完整窗口证据分低于观察门槛",
    }.get(reason, "原方向继续成立的条件已经不足")
    phase = _launch_phase(item)
    score_direction = "bullish" if previous == "bullish" else "bearish" if previous == "bearish" else _direction_key(signal)
    lines = [
        f"⚫ {tg_bold(f'{previous_text}信号失效｜本轮周期结束')}",
        _instrument_line(item),
        f"品类：{tg_escape(category)}",
        "",
        f"{tg_bold('当前结论')}：原方向继续成立的条件已经不足；旧计划停止沿用。",
        "",
        tg_bold("🔥 核心数据"),
        *_compact_market_lines(item),
        "",
        tg_bold("📊 发现分与方向证据分（都不是概率）"),
        *_compact_score_lines(
            signal,
            score_direction,
            discovery_score=item.get("discovery_score"),
        ),
        "",
        tg_bold("🚦 行情位置与时机"),
        *_position_timing_lines(phase, "invalidated", previous),
        f"• 唯一主要阻断：{tg_escape(reason_text)}",
        _data_status_line(item, signal),
        *_smc_filter_lines(item),
    ]
    if reason == "direction_changed":
        lines.extend([
            f"• 当前规则方向：{tg_escape(next_text)}",
            "• 先结束原方向周期，不在同一窗口自动反手。",
        ])
    lines.extend([
        *_compact_evidence_lines(signal, score_direction),
        "",
        tg_bold("📌 当前处理"),
        "• 原观察计划与原失效位停止沿用。",
        "• 本消息只记录旧周期结束，不代表立即开仓或反手。",
        _compact_lifecycle_line(item),
        *_compact_ai_lines(item),
        "",
        "发现分/证据分都不是概率｜缺失不按0｜仅作观察，不构成投资建议。",
    ])
    max_chars = max(512, min(1024, int(max_chars)))
    return _bounded_card(
        lines,
        max_chars=max_chars,
        fallback_lines=[
            lines[0],
            lines[1],
            f"品类：{tg_escape(category)}",
            "",
            f"{tg_bold('失效原因')}：{tg_escape(reason_text)}",
            *_compact_market_lines(item),
            *_compact_score_lines(
                signal,
                score_direction,
                discovery_score=item.get("discovery_score"),
            )[:2],
            f"• 唯一主要阻断：{tg_escape(reason_text)}",
            _data_status_line(item, signal),
            *_smc_filter_lines(item),
            _compact_lifecycle_line(item),
            *_compact_ai_lines(item),
            "发现分/证据分都不是概率｜缺失不按0｜仅作观察，不构成投资建议。",
        ],
    )


def format_launch_directional_signal(
    item: Mapping[str, Any],
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """Format one bounded Telegram HTML card without making trading claims.

    Direction evidence, timing/position and execution are deliberately shown
    as separate layers.  SMC is a secondary filter and never owns the title.
    """

    if not isinstance(item, Mapping):
        item = {}
    signal = _signal(item)
    invalidated = item.get("directional_cycle_invalidated")
    if isinstance(invalidated, Mapping):
        return _invalidated_cycle_card(
            item,
            signal,
            invalidated,
            max_chars=max_chars,
        )

    max_chars = max(512, min(1024, int(max_chars)))
    direction = _direction_key(signal)
    phase = _launch_phase(item)
    side = _phase_side(phase, signal)
    confirmation_consistent = _confirmation_consistent(item, signal, phase, side)
    stage = _primary_stage(
        item,
        signal,
        phase,
        confirmation_consistent=confirmation_consistent,
    )
    icon, headline, subtitle = _phase_title(item, signal, phase, side, stage)
    conclusion = _phase_conclusion(signal, phase, side, stage)
    category, category_risk = _asset_profile(item, signal)
    category = _short(item.get("asset_category_label"), limit=80) or category

    score_lines = _compact_score_lines(
        signal,
        direction,
        discovery_score=item.get("discovery_score"),
    )
    market_lines = _market_strength_lines(item, signal)
    core_market = market_lines[:4]
    threshold_lines = market_lines[4:5]
    plan_lines = _compact_plan_lines(item, signal, phase, side, stage)
    crowding = _crowding_text(item, signal)
    if not plan_lines:
        if direction in {"bullish_divergence_watch", "bearish_divergence_watch"}:
            plan_lines = [
                tg_bold("📌 当前处理"),
                "• 背离尚未确认反转；不生成观察区、失效位或目标。",
            ]
        elif str(signal.get("observation_mode") or "") == "futures_only_spot_pair_not_listed":
            plan_lines = [
                tg_bold("📌 当前处理"),
                "• 仅合约观察；不确认方向、不生成计划，也不调用AI。",
            ]
        else:
            plan_lines = [
                tg_bold("📌 当前处理"),
                f"• {_primary_block_reason(item, signal, phase, side, stage)}。",
            ]

    required_head = [
        f"{icon} {tg_bold(f'{headline}｜{subtitle}')}",
        _instrument_line(item),
        f"品类：{tg_escape(category)}",
        "",
        f"{tg_bold('当前结论')}：{tg_escape(conclusion)}",
        "",
        tg_bold("📊 发现分与方向证据分（都不是概率）"),
        *score_lines,
    ]
    if core_market:
        required_head.extend(["", tg_bold("🔥 已收盘数据"), *core_market])
    required_head.extend([
        "",
        tg_bold("🚦 位置、阶段与执行"),
        *_position_timing_lines(phase, stage, side),
        f"• 主要阻断：{tg_escape(_primary_block_reason(item, signal, phase, side, stage))}",
        _data_status_line(item, signal),
        *_data_quality_lines(item, signal)[1:],
        *(
            [f"• 拥挤参考：{tg_escape(crowding)}"]
            if crowding != "待确认"
            else []
        ),
        *_smc_filter_lines(item),
        "",
        *plan_lines,
        *_compact_evidence_lines(signal, direction),
    ])

    ai_lines = _compact_ai_lines(item)
    lifecycle_line = _compact_lifecycle_line(item)
    footer = "发现分/证据分都不是概率｜不追涨不追空｜仅作观察，不构成投资建议。"
    required_tail = ["", *ai_lines, lifecycle_line, footer]

    optional_sections: list[list[str]] = []
    background = [*_background_lines(item), *_market_size_lines(item)]
    if background:
        optional_sections.append(["", tg_bold("🔭 背景与规模"), *background])
    gate_lines = _gate_lines(signal, direction)
    if gate_lines:
        optional_sections.append(gate_lines)
    if threshold_lines:
        optional_sections.append(threshold_lines)
    lifecycle_details = _lifecycle_lines(item)[1:]
    if lifecycle_details:
        optional_sections.append(lifecycle_details)
    optional_sections.append([f"🧩 品类提醒：{tg_escape(category_risk)}"])

    lines = list(required_head)
    for section in optional_sections:
        candidate = "\n".join([*lines, *section, *required_tail])
        non_empty_lines = sum(
            bool(re.sub(r"<[^>]*>", "", value).strip())
            for value in [*lines, *section, *required_tail]
        )
        if _visible_length(candidate) <= max_chars and non_empty_lines <= 32:
            lines.extend(section)
    rendered = "\n".join([*lines, *required_tail])
    if _visible_length(rendered) <= max_chars:
        return rendered

    # Even under unusually long user-controlled labels, keep the stage,
    # deterministic facts, AI status, lifecycle and fixed safety footer.
    fallback = [
        required_head[0],
        required_head[1],
        f"品类：{tg_escape(_short(category, limit=32))}",
        f"{tg_bold('当前结论')}：{tg_escape(_short(conclusion, limit=72))}",
        tg_bold("📊 发现分与方向证据分（都不是概率）"),
        *score_lines[:2],
        *core_market[:2],
        tg_bold("🚦 位置、阶段与执行"),
        *_position_timing_lines(phase, stage, side),
        f"• 主要阻断：{tg_escape(_short(_primary_block_reason(item, signal, phase, side, stage), limit=48))}",
        *(
            [f"• 拥挤参考：{tg_escape(crowding)}"]
            if crowding != "待确认"
            else []
        ),
        *_smc_filter_lines(item),
        *_compact_evidence_lines(signal, direction),
        *required_tail,
    ]
    while len(fallback) > 8 and _visible_length("\n".join(fallback)) > max_chars:
        # Remove optional middle facts, never the title or mandatory tail.
        removable = len(fallback) - len(required_tail) - 1
        if removable <= 5:
            break
        fallback.pop(removable)
    return "\n".join(fallback)


def launch_directional_topic_intro() -> str:
    """Return the detailed plain-Chinese introduction for the launch topic."""

    return "\n".join([
        "📌 <b>启动预警话题说明</b>",
        "",
        "这里的目标是提前发现可能启动或转弱的币，并持续跟踪后续变化；不是等涨跌结束后才补发结论，也不是自动交易工具。",
        "",
        "<b>🔎 雷达怎么工作</b>",
        "• 15分钟继续使用原规则发现第一批异动，不改变原触发算法。",
        "• 1小时现货/合约主动买卖、价格、持仓量和成交量共同核对；更大周期只提供方向背景。",
        "• 每次只使用已经收盘的数据；缺失项不按0补，也不会拿旧窗口冒充当前窗口。",
        "",
        "<b>🧱 卡片分成四层，不能混着看</b>",
        "1. 发现分：只负责衡量15分钟异动有多明显，用来找币和排序，不代表方向。",
        "2. 方向证据分：看涨和看跌分开计算；只表示哪边证据更多，不是上涨或下跌概率。",
        "3. 行情阶段：区分初步发现、形成中、确认、延续、拥挤、衰竭、失效，以及72小时内所处位置。",
        "4. 执行状态：明确写出等待确认、等待回踩/反弹、数据不足，或高位不追涨、低位不追空。",
        "看涨、看跌、风险和数据可靠度分开显示，避免把方向证据误读成进场概率。",
        "方向证据强，不代表当前位置适合追；阶段和执行门禁会单独拦截。",
        "",
        "<b>🚦 重点状态怎么理解</b>",
        "• 看涨/看跌预警：提前证据正在形成，仍需下一个完整窗口确认。",
        "• 看涨/看跌确认：方向与硬门槛一致，才可能给出观察区、失效和目标参考。",
        "• 上涨已延伸：已靠近72小时高位且离结构参考过远，只跟踪，不追涨。",
        "• 下跌已延伸：已靠近72小时低位且跌幅已经延伸，只跟踪，不追空。",
        "• 确认信息冲突：文字状态与完整数据或硬门槛不一致，自动降级，不给计划。",
        "• 失效：旧方向周期结束；同一窗口不会自动反手。",
        "",
        "<b>📊 数据怎么看</b>",
        "• 价格↑、持仓↑、主动买入↑：更接近新增多头推动；价格↓、持仓↑、主动卖出↑：更接近新增空头推动。",
        "• 价格上涨但持仓下降，多数先按空头平仓观察；价格下跌且持仓下降，多数先按多头去杠杆观察。",
        "• 主动买卖只表示成交主导方，不等于钱包真实资金流入流出；持仓量本身也不能区分多头或空头。",
        "• 1小时缩量、主动买卖规模太小、方向过度拥挤或数据不完整，都不能升级成确认。",
        "",
        "<b>🧭 SMC只做二次过滤</b>",
        "• SMC读取已收线的1小时和4小时结构，用来过滤明显反向的假启动。",
        "• 它不会接管卡片主标题，不改15分钟发现结果，也不改发现分或方向证据分。",
        "• 中性、冲突或数据不足会单独写在“SMC二次过滤”一行，不能伪装成主信号状态。",
        "",
        "<b>🤖 AI改为需要时主动解读</b>",
        "• 雷达扫描默认不调用AI；先发送确定性规则卡片，需要时再点击消息下方“AI解读这条信号”。",
        "• 按钮只对已绑定的管理员私聊生效，并读取这张卡片发出当时的安全快照，不会拿后来的行情冒充当时数据。",
        "• 同一快照、模型和提示词重复点击会复用已通过校验的结果，不重复产生AI调用；每日调用还有独立上限。",
        "• AI只把已计算的数据和规则翻译成白话，不能改方向、发现分、方向证据分、阶段、观察区或失效位。",
        "",
        "<b>⏱️ 同币种如何持续跟踪</b>",
        "• 首次预警单独发送；同一币种同一轮的后续变化会回复上一条成功消息。",
        "• 历史消息不自动删除；如果上一条被人工删除，下一次会改为独立发送并重新建立回复链。",
        "• 生命周期会记录第几轮、第几次完整观察、持续时间、当前阶段和历史最高阶段。",
        "",
        "<b>🛡️ 重要边界</b>",
        "• 主流币、山寨币、股票/指数代币和大宗商品代币使用不同风险提醒。",
        "• 没有同名现货对时只做合约观察，不确认方向，也不调用AI。",
        "• 发现分和方向证据分都不是概率；观察区、失效和目标不是交易指令；不自动交易，不构成投资建议。",
    ])


__all__ = [
    "DEFAULT_MAX_CHARS",
    "format_launch_directional_signal",
    "launch_directional_topic_intro",
]
