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
    "rule_readiness_not_probability": "准备度是规则分，不是涨跌概率",
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
    "not_requested": "未调用，本卡片全部为规则结论",
    "not_eligible": "未调用（数据或信号未达到解读条件）",
    "not_eligible_smc_conflict": "未调用（1小时与4小时结构均反向）",
    "not_eligible_smc_neutral": "未调用（高周期结构尚未形成一致支持）",
    "not_eligible_smc_insufficient": "未调用（高周期闭合数据不足）",
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
    bullish = _finite(signal.get("bullish_readiness"))
    bearish = _finite(signal.get("bearish_readiness"))
    if bullish is None and bearish is None:
        return ""
    return "bearish" if (bearish or 0.0) > (bullish or 0.0) else "bullish"


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
    bullish = _finite(signal.get("bullish_readiness"))
    bearish = _finite(signal.get("bearish_readiness"))
    if bullish is None and bearish is None:
        return ["• 多空准备度：待确认（规则分，不是概率）"]
    bullish_text = str(int(round(bullish))) if bullish is not None else "待确认"
    bearish_text = str(int(round(bearish))) if bearish is not None else "待确认"
    comparison = f"• 看涨 {bullish_text}｜看跌 {bearish_text}"
    if bullish is not None and bearish is not None:
        lead = abs(int(round(bullish)) - int(round(bearish)))
        leading = "看涨" if bullish >= bearish else "看跌"
        comparison += f"｜{leading}领先 {lead}"
    lines = [comparison, "• 规则分不是概率"]
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
        final = _finite(signal.get(f"{side}_readiness"))
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
    delta_values = (
        _finite(delta.get("price_pct")),
        _finite(delta.get("oi_pct")),
        _finite(delta.get("score")),
    )
    if any(value is not None for value in delta_values):
        score_delta = _finite(delta.get("score"))
        score_text = (
            f"{int(round(score_delta)):+d}分"
            if score_delta is not None
            else "待确认"
        )
        lines.append(
            f"• 较首次：价格 {_pct(delta.get('price_pct'))}｜持仓量 {_pct(delta.get('oi_pct'))}｜"
            f"准备度 {score_text}"
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
            if key.replace("_", "").replace("-", "").isalnum() and key.isascii():
                if unknown_added:
                    continue
                text = "其他规则证据已记录"
                unknown_added = True
            else:
                text = _short(key, limit=100)
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
        return []
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
    explanation = {
        "supportive": "至少一个高周期同向，且没有确认的反向结构。",
        "neutral": "高周期没有形成一致结论，本轮只按观察处理。",
        "conflicting": "1小时和4小时均明确反向；新周期首条强提醒会被拦截。",
        "insufficient": "闭合历史不足或不连续；不参与拦截，也不调用AI。",
    }.get(status, "高周期数据不可用于确定性过滤。")
    return [
        f"{tg_bold('🧭 SMC二次过滤')}：{tg_escape(status_text)}",
        f"• 1小时：{tg_escape(one_hour)}｜4小时：{tg_escape(four_hour)}",
        f"• {tg_escape(explanation)}",
        "• SMC不修改15分钟触发、方向和规则分。",
    ]


def _optional_section(title: str, lines: list[str]) -> list[str]:
    return ["", tg_bold(title), *lines]


def _ai_participation_lines(item: Mapping[str, Any]) -> list[str]:
    ai_text = _short(item.get("ai_interpretation"), limit=160)
    status = _short(item.get("ai_interpretation_status"), limit=64)
    source = _short(item.get("ai_interpretation_source"), limit=16)
    if not status and ai_text:
        status = "available"
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
            f"{tg_bold('AI白话解读')}：{tg_escape(ai_text)}",
            "• 只有上一行解读由AI生成；方向、分数和失效位仍由规则决定。",
        ]
    if status == "available":
        status = "invalid_ai_output"
    detail = _AI_STATUS_TEXT.get(
        status,
        "未调用（当前卡片没有可用AI结果）",
    )
    return [f"{tg_bold('🤖 AI参与')}：{tg_escape(detail)}"]


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
    while len(safe_lines) > 2 and _visible_length("\n".join(safe_lines)) > max_chars:
        safe_lines.pop()
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
    category, category_risk = _asset_profile(item, signal)
    reason_text = {
        "direction_changed": "新一轮完整数据转为相反方向",
        "two_closes_below_invalidation": "连续两个完整窗口收盘跌破看涨失效位",
        "two_closes_above_invalidation": "连续两个完整窗口收盘升破看跌失效位",
        "two_windows_below_watch_score": "连续两个完整窗口准备度低于观察门槛",
    }.get(reason, "原方向继续成立的条件已经不足")
    lines = [
        f"⚫ {tg_bold(f'{previous_text}信号失效')}",
        _instrument_line(item),
        f"品类：{tg_escape(category)}",
        "",
        f"{tg_bold('失效原因')}：{tg_escape(reason_text)}",
        *_ai_participation_lines(item),
    ]
    if reason == "direction_changed":
        lines.extend([
            f"• 当前规则方向：{tg_escape(next_text)}",
            "• 先结束原方向周期，不在同一窗口自动反手。",
        ])
    lines.extend([
        *_lifecycle_lines(item),
        "",
        tg_bold("当前处理"),
        "• 原观察计划与原失效位停止沿用。",
        "• 本消息只记录旧周期结束，不代表立即开仓或反手。",
    ])
    sections = [
        _optional_section("📊 本轮结果", _outcome_lines(item)),
        _optional_section("📋 数据", _data_quality_lines(item, signal)),
        _optional_section("🧩 品类提醒", [f"• {tg_escape(category_risk)}"]),
    ]
    max_chars = max(512, min(1024, int(max_chars)))
    for section in sections:
        if len(section) <= 2:
            continue
        candidate = "\n".join([*lines, *section])
        if _visible_length(candidate) <= max_chars:
            lines.extend(section)
    footer = ["", "本轮结束；重新满足条件后开启新一轮。规则分不是概率。"]
    if _visible_length("\n".join([*lines, *footer])) <= max_chars:
        lines.extend(footer)
    return _bounded_card(
        lines,
        max_chars=max_chars,
        fallback_lines=[
            lines[0],
            lines[1],
            f"品类：{tg_escape(category)}",
            "",
            f"{tg_bold('失效原因')}：{tg_escape(reason_text)}",
            *_ai_participation_lines(item),
            "• 卡片内容异常或过长，已安全精简；旧观察计划停止沿用。",
        ],
    )


def format_launch_directional_signal(
    item: Mapping[str, Any],
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """Format one bounded Telegram HTML card without making trading claims."""

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
    direction = _direction_key(signal)
    icon, headline, subtitle = _headline(signal, direction)
    smc_filter = item.get("smc_filter")
    smc_status = (
        str(smc_filter.get("status") or "")
        if isinstance(smc_filter, Mapping)
        else ""
    )
    display_summary = _summary(signal)
    if smc_status == "neutral":
        icon, headline, subtitle = "🟡", "方向观察", "高周期暂未一致"
        display_summary = "15分钟候选仍保留，但高周期暂未一致，不作为强信号。"
    elif smc_status == "insufficient":
        icon, headline, subtitle = "🟡", "方向观察", "高周期数据不足"
        display_summary = "高周期闭合数据不足，本轮只保留规则观察。"
    elif smc_status == "conflicting":
        icon, headline, subtitle = "🟠", "结构冲突", "仅保留已有周期跟踪"
        display_summary = "1小时和4小时均明确反向；新周期强提醒会被拦截。"
    status = str(signal.get("status") or "").strip()
    category, category_risk = _asset_profile(item, signal)
    category = _short(item.get("asset_category_label"), limit=80) or category

    entry = _price_text(
        item.get("entry_zone", signal.get("entry_zone"))
    ) or "待确认"
    invalidation = _price_text(
        item.get("invalidation_price", item.get("invalidation", signal.get("invalidation")))
    ) or "待确认"
    targets = _targets_text(
        item.get("targets", signal.get("targets"))
    ) or "待确认"
    rr_value = _finite(
        item.get("risk_reward_ratio", signal.get("risk_reward_ratio"))
    )
    rr = (
        f"{rr_value:.2f}"
        if rr_value is not None and 0 <= rr_value <= 1_000_000
        else "待确认"
    )

    divergence_watch = direction in {
        "bullish_divergence_watch",
        "bearish_divergence_watch",
    }
    futures_only = (
        str(signal.get("observation_mode") or "")
        == "futures_only_spot_pair_not_listed"
    )
    active_side = _active_side(signal, direction)
    hard_gates = _mapping(signal.get("hard_gates"))
    active_gates = _mapping(hard_gates.get(active_side)) if active_side else {}
    minimum_rr = _finite(hard_gates.get("minimum_risk_reward_ratio"))
    if minimum_rr is None or not 0 <= minimum_rr <= 1_000_000:
        minimum_rr = 2.0
    minimum_rr = max(2.0, minimum_rr)
    confirmed_side = (
        "bullish"
        if status == "多头确认"
        else "bearish"
        if status == "空头确认"
        else ""
    )
    hard_gates_passed = bool(
        active_gates
        and all(value is True for value in active_gates.values())
        and hard_gates.get(f"{active_side}_passed") is True
    )
    smc_supportive = bool(
        isinstance(smc_filter, Mapping)
        and str(smc_filter.get("status") or "") == "supportive"
    )
    plan_available = bool(
        status in {"多头确认", "空头确认"}
        and active_side == confirmed_side
        and not divergence_watch
        and not futures_only
        and signal.get("data_complete") is True
        and hard_gates_passed
        and smc_supportive
        and entry != "待确认"
        and invalidation != "待确认"
        and targets != "待确认"
        and rr_value is not None
        and rr_value >= minimum_rr
        and rr != "待确认"
    )

    lines = [
        f"{icon} {tg_bold(f'{headline}｜{subtitle}')}",
        _instrument_line(item),
        f"品类：{tg_escape(category)}",
        "",
        f"{tg_bold('当前结论')}：{tg_escape(display_summary)}",
        *_smc_filter_lines(item),
        *_ai_participation_lines(item),
        "",
        tg_bold("📊 信号强度"),
        *_score_lines(signal, direction),
    ]
    strength_lines = _market_strength_lines(item, signal)
    if strength_lines:
        lines.extend(["", tg_bold("🔥 实际数据"), *strength_lines])
    lines.extend([
        "",
        tg_bold("🚦 可靠度与风险"),
        *_gate_lines(signal, direction),
        *_data_quality_lines(item, signal),
        f"• 拥挤：{tg_escape(_crowding_text(item, signal))}",
    ])

    if divergence_watch:
        lines.extend([
            "",
            tg_bold("⚠️ 背离风险观察"),
            "• 背离尚未确认反转，不给出观察区、失效位或目标。",
        ])
    elif futures_only:
        lines.extend([
            "",
            tg_bold("⚠️ 仅合约观察"),
            "• 本轮只展示合约侧候选，不确认看涨或看跌，也不调用AI。",
        ])
    elif plan_available:
        lines.extend([
            "",
            tg_bold("📍 观察计划"),
            f"• 观察区：{tg_escape(entry)}｜失效参考：{tg_escape(invalidation)}",
            f"• 目标参考：{tg_escape(targets)}｜收益风险参考：{tg_escape(rr)}",
        ])
    else:
        lines.extend([
            "",
            tg_bold("⏳ 观察条件"),
            "• 尚未满足完整确认；等待下一完整收盘窗口，不沿用旧计划。",
        ])

    evidence = _mapping(signal.get("evidence"))
    if direction.startswith("bearish"):
        support = evidence.get("bearish", signal.get("supporting_evidence"))
        counter = evidence.get("bullish", signal.get("counter_evidence"))
    else:
        support = evidence.get("bullish", signal.get("supporting_evidence"))
        counter = evidence.get("bearish", signal.get("counter_evidence"))
    if divergence_watch:
        support = signal.get("divergence_evidence") or support
    sections: list[list[str]] = [
        _optional_section("🧭 多周期", _timeframe_lines(item)),
        _optional_section("🔭 背景与规模", [
            *_background_lines(item),
            *_market_size_lines(item),
        ]),
        _optional_section(
            "✅ 支持证据",
            _translated_lines(support, empty="暂无完整支持证据", limit=2),
        ),
        _optional_section(
            "⚠️ 反向证据",
            _translated_lines(counter, empty="暂无明显反向证据", limit=1),
        ),
        _optional_section("⏱️ 生命周期", _lifecycle_lines(item)),
    ]
    limitations = signal.get("limitations")
    limitation_lines = _translated_lines(
        limitations,
        empty="价格结构和资金数据只是观察证据",
        limit=3,
    )
    sections.extend([
        _optional_section("🧩 品类提醒", [f"• {tg_escape(category_risk)}"]),
        _optional_section("🛡️ 限制", limitation_lines),
    ])

    max_chars = max(512, min(1024, int(max_chars)))
    for section in sections:
        if len(section) <= 2:
            continue
        candidate = "\n".join([*lines, *section])
        if _visible_length(candidate) <= max_chars:
            lines.extend(section)
    footer = ["", "规则分不是概率；仅作观察，不执行交易，不构成投资建议。"]
    if _visible_length("\n".join([*lines, *footer])) <= max_chars:
        lines.extend(footer)
    return _bounded_card(
        lines,
        max_chars=max_chars,
        fallback_lines=[
            lines[0],
            lines[1],
            f"品类：{tg_escape(category)}",
            "",
            f"{tg_bold('当前结论')}：{tg_escape(display_summary)}",
            *_smc_filter_lines(item),
            *_ai_participation_lines(item),
            "• 卡片内容异常或过长，已安全精简；等待下一完整窗口。",
            "• 规则分不是概率，不执行交易。",
        ],
    )


def launch_directional_topic_intro() -> str:
    """Return the detailed plain-Chinese introduction for the launch topic."""

    return "\n".join([
        "📌 <b>启动预警话题说明</b>",
        "",
        "这里先用原来的15分钟规则发现异动，再对重要候选做多周期深查。新版会把看涨、看跌、风险和数据可靠度分开显示，不再只给一个看不懂的总分。",
        "",
        "<b>📱 一张卡片怎么看</b>",
        "• 当前结论：先用一句白话说明这轮属于看涨、看跌、背离、过热还是等待。",
        "• 信号强度：看涨和看跌分别打分，并列出价格与持仓、主动买卖、结构、执行质量四部分。",
        "• 实际数据：显示1小时价格与持仓变化、1小时现货/合约主动买卖、15分钟发现数据和本品类门槛。",
        "• 可靠度：显示确认条件通过几项、缺哪项、数据是否完整，以及资金费率和基差的拥挤风险。",
        "• 背景与跟踪：显示4小时、24小时、市值、成交额、当前持仓和本轮生命周期。",
        "",
        "<b>🧭 各周期怎么用</b>",
        "• 1周/1天：过滤大方向。",
        "• 12小时/8小时/4小时：判断主要结构。",
        "• 2小时/1小时：确认方向和资金是否跟随。",
        "• 15分钟：保留现有异动触发。",
        "• 5分钟：只优化入场时机，不能推翻大周期。",
        "• 滚动24小时只是背景，不与日线重复计分。",
        "",
        "<b>🧭 SMC二次过滤怎么用</b>",
        "• 15分钟仍负责发现异动；SMC只读取已收线的1小时和4小时结构，负责过滤高周期明确反向的假启动。",
        "• 同向支持：至少一个高周期同向且没有反向结构；中性观察：周期结论混合；数据不足：历史缺口或闭合K线不足。",
        "• 只有1小时和4小时都完整、都明确反向时，才拦截尚未发布的新周期首条强提醒。已有周期的失效与安全更新不会被拦截。",
        "• SMC不加分、不扣分、不改变15分钟方向；数据不足不会被误写成冲突。",
        "",
        "<b>📊 分数怎么来</b>",
        "• 四组规则分：价格与持仓30分、主动买卖25分、多周期结构25分、执行质量20分。",
        "• 看涨和看跌分开计算；资金费率和基差过度拥挤时只扣分，不会凭空加分。",
        "• 分数表示证据准备程度，不是胜率。即使分数较高，确认门槛没有全部通过也只会继续观察。",
        "",
        "<b>🔥 实际数据是什么意思</b>",
        "• 持仓量增加只说明新仓进入，必须再结合价格和主动买卖判断方向。",
        "• 主动买卖是成交主导方，不等于钱包资金真实流入或流出。",
        "• 24小时价格是滚动背景；24小时持仓量只有在连续闭合窗口齐全时才显示“严格闭合”。",
        "• 数据缺失不会按0计算，也不会用较旧的15分钟主动买卖冒充1小时数据。",
        "",
        "<b>🚦 会出现哪些状态</b>",
        "• 看涨/看跌候选：证据正在增强，但仍有条件未通过。",
        "• 看涨/看跌条件满足：完整数据和硬门槛都通过，才显示观察区、失效参考和目标参考。",
        "• 假强/假弱背离：价格与主动买卖不一致，只提示反转风险，不代表已经反转。",
        "• 过热、挤空、踩踏、派发、冲突：只作风险观察，不追涨、不追空。",
        "• 信号失效：结束旧周期；不会在同一窗口自动反手。",
        "",
        "<b>⚠️ 背离与数据不足</b>",
        "• 假强：价格和持仓上升，但现货、合约主动成交都偏卖出。",
        "• 假弱：价格和持仓下降，但现货、合约主动成交都偏买入。",
        "• 没有同名现货对时只做合约观察，不确认方向，也不调用AI。",
        "",
        "<b>⏱️ 生命周期与历史</b>",
        "• 同一轮会记录第几次完整观察、持续多久、最高到过哪个阶段，以及相对首次的价格、持仓和分数变化。",
        "• 第一次信号单独发送；同一币种后续监控会回复上一条成功消息，历史卡片不自动删除。",
        "• 如果上一条被人工删除，下一次会安全改为独立发送，并从新消息继续串联。",
        "• 历史统计严格区分看涨和看跌；样本不足时只显示积累进度，不展示容易误导的百分比。",
        "• 确认率和跟随率只是规则历史记录，不是胜率，也不会自动修改参数。",
        "",
        "<b>🤖 AI做什么</b>",
        "• 每张卡都会明确显示AI是已完成、复用缓存、本轮顺延、未调用还是调用失败。",
        "• AI只把已计算的数据和规则翻译成白话；只有“AI白话解读”后面的文字由AI生成，其他数据和结论都来自确定性规则。",
        "• AI不改方向、不改分数、不改失效位。输出被截断或调用失败时会显示中文原因并使用规则结论；同一版本的同一个观察最多调用一次，仅旧版已截断结果在本次升级后允许一次修复尝试。",
        "• 只有SMC二次过滤通过且其他数据完整时才调用AI；中性、冲突或数据不足均保持AI零调用。",
        "",
        "<b>🛡️ 重要边界</b>",
        "• 规则分不是涨跌概率；观察区、失效位和目标也不是交易指令。",
        "• 数据缺失、多周期冲突或流动性不足时只降级观察，不强行给方向。",
        "• 多周期方向雷达和AI解读默认关闭，必须由运维人员明确启用。",
        "• 不自动交易，不保证结果，不构成投资建议。",
        "",
        "品类会分为主流币、山寨币、股票/指数代币及大宗商品代币，并使用各自的风险提醒。",
    ])


__all__ = [
    "DEFAULT_MAX_CHARS",
    "format_launch_directional_signal",
    "launch_directional_topic_intro",
]
