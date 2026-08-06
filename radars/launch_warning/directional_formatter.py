from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from ..common import coin_link, tg_bold, tg_escape


DEFAULT_MAX_CHARS = 3500

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


def _readiness_text(signal: Mapping[str, Any], direction: str) -> str:
    direct = _finite(signal.get("readiness_score"))
    if direct is None and direction.startswith("bullish"):
        direct = _finite(signal.get("bullish_readiness"))
    if direct is None and direction.startswith("bearish"):
        direct = _finite(signal.get("bearish_readiness"))
    if direct is None:
        return "待确认"
    return f"{max(0, min(100, round(direct)))}/100"


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
    reasons = [
        str(value)
        for side in ("bullish_reasons", "bearish_reasons")
        for value in _sequence(adjustments.get(side))
    ]
    translated = [_EVIDENCE_TEXT[value] for value in reasons if value in _EVIDENCE_TEXT]
    if translated:
        return "；".join(dict.fromkeys(translated))
    funding = _finite(item.get("funding_rate_pct", item.get("funding_pct")))
    basis = _finite(item.get("basis_pct"))
    values: list[str] = []
    if funding is not None:
        values.append(f"资金费率 {funding:+.4f}%")
    if basis is not None:
        values.append(f"基差 {basis:+.3f}%")
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


def _optional_section(title: str, lines: list[str]) -> list[str]:
    return ["", tg_bold(title), *lines]


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
        f"⚠️ {tg_bold(f'{previous_text}观察周期已失效')}",
        _instrument_line(item),
        f"品类：{tg_escape(category)}",
        "",
        tg_bold("发生了什么"),
        f"• 失效原因：{tg_escape(reason_text)}",
        "",
        tg_bold("当前处理"),
        "• 原观察计划与原失效位停止沿用。",
        "• 本消息只记录旧周期结束，不代表立即开仓或反手。",
        f"• 数据完整性：{tg_escape(_data_text(signal))}",
        "",
        tg_bold("品类提醒"),
        f"• {tg_escape(category_risk)}",
        "",
        "仅作规则观察，不执行交易，不构成投资建议。",
    ]
    if reason == "direction_changed":
        lines[7:7] = [
            f"• 当前规则方向：{tg_escape(next_text)}",
            "• 先结束原方向周期，不在同一窗口自动反手。",
            "• 新方向必须等下一完整窗口重新通过多周期、主动买卖和风险门禁。",
        ]
    return "\n".join(lines)[: max(1600, min(4096, int(max_chars)))]


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
    icon, direction_label = _DIRECTION_TEXT[direction]
    stage = _stage_text(signal, direction)
    readiness = _readiness_text(signal, direction)
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
    rr = f"{rr_value:.2f}" if rr_value is not None else "待确认"

    divergence_watch = direction in {
        "bullish_divergence_watch",
        "bearish_divergence_watch",
    }
    futures_only = (
        str(signal.get("observation_mode") or "")
        == "futures_only_spot_pair_not_listed"
    )
    plan_available = bool(
        entry != "待确认"
        and invalidation != "待确认"
        and targets != "待确认"
        and rr != "待确认"
    )

    lines = [
        f"{icon} {tg_bold(f'{direction_label}｜{stage}')}",
        _instrument_line(item),
        f"品类：{tg_escape(category)}",
        f"准备度：{tg_escape(readiness)}（规则分，不是涨跌概率）",
        "",
    ]
    if divergence_watch:
        lines.extend([
            tg_bold("⚠️ 背离风险观察"),
            "• 当前只说明价格与主动成交方向不一致。",
            "• 背离尚未确认反转，不给出入场、止损或目标位。",
            "• 等待结构破位、放量和下一完整窗口再次确认。",
        ])
    elif futures_only:
        lines.extend([
            tg_bold("⚠️ 仅合约观察"),
            "• Binance 没有同名现货交易对，现货主动买卖数据不可用。",
            "• 本轮只展示合约侧候选，不确认看涨或看跌，也不调用AI。",
        ])
    elif plan_available:
        lines.extend([
            tg_bold("🎯 交易观察"),
            f"• 入场观察区：{tg_escape(entry)}",
            f"• 失效位置：{tg_escape(invalidation)}",
            f"• 目标：{tg_escape(targets)}",
            f"• 收益风险比：{tg_escape(rr)}",
        ])
    else:
        lines.extend([
            tg_bold("⏳ 观察条件"),
            "• 当前结构空间不足，暂不生成入场、失效和目标参考。",
            "• 等待新的完整收盘窗口形成可验证结构。",
        ])
    lines.extend(["", tg_bold("🧭 多周期"), *_timeframe_lines(item)])

    evidence = _mapping(signal.get("evidence"))
    if direction.startswith("bearish"):
        support = evidence.get("bearish", signal.get("supporting_evidence"))
        counter = evidence.get("bullish", signal.get("counter_evidence"))
    else:
        support = evidence.get("bullish", signal.get("supporting_evidence"))
        counter = evidence.get("bearish", signal.get("counter_evidence"))
    if divergence_watch:
        support = signal.get("divergence_evidence") or support
    sections = [
        _optional_section(
            "✅ 支持证据",
            _translated_lines(support, empty="暂无完整支持证据"),
        ),
        _optional_section(
            "⚠️ 反向证据",
            _translated_lines(counter, empty="暂无明显反向证据"),
        ),
        _optional_section(
            "📊 拥挤与数据",
            [
                f"• 资金拥挤：{tg_escape(_crowding_text(item, signal))}",
                f"• 数据完整性：{tg_escape(_data_text(signal))}",
            ],
        ),
    ]
    ai_text = _short(item.get("ai_interpretation"), limit=420)
    if ai_text:
        sections.append(_optional_section(
            "🤖 AI白话解读",
            [f"• {tg_escape(ai_text)}", "• AI只解释规则结果，不改变方向、分数和失效位"],
        ))
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

    max_chars = max(1600, min(4096, int(max_chars)))
    for section in sections:
        candidate = "\n".join([*lines, *section])
        if len(candidate) <= max_chars:
            lines.extend(section)
    footer = ["", "仅作规则观察，不执行交易，不构成投资建议。"]
    if len("\n".join([*lines, *footer])) <= max_chars:
        lines.extend(footer)
    return "\n".join(lines)


def launch_directional_topic_intro() -> str:
    """Return the detailed plain-Chinese introduction for the launch topic."""

    return "\n".join([
        "📌 <b>启动预警话题说明</b>",
        "",
        "这里先沿用原来的15分钟异动发现逻辑，再对少量重要候选做多周期深查，分成看涨、看跌或继续观察；背离单独作为风险提醒。",
        "",
        "<b>🔎 看哪些数据</b>",
        "• 价格、持仓量、成交量、现货主动买卖、合约主动买卖。",
        "• 资金费率和基差只用来判断多空是否过度拥挤。",
        "• 价格行为和智能资金概念只识别结构突破、结构转变、流动性扫单和不平衡区，不猜测“庄家”身份。",
        "",
        "<b>🧭 各周期怎么用</b>",
        "• 1周/1天：过滤大方向。",
        "• 12小时/8小时/4小时：判断主要结构。",
        "• 2小时/1小时：确认方向和资金是否跟随。",
        "• 15分钟：保留现有异动触发。",
        "• 5分钟：只优化入场时机，不能推翻大周期。",
        "• 滚动24小时只是背景，不与日线重复计分。",
        "",
        "<b>📊 准备度怎么来</b>",
        "五组时间周期只是分工，真正评分只有四组：价格与持仓参与、现货/合约主动买卖、多周期结构、执行质量。看涨和看跌分别计算，分数不是概率。",
        "",
        "<b>🚦 信号状态</b>",
        "• 方向观察：异动已出现，证据还不够。",
        "• 等待确认：等待回踩站稳，或等待反弹受阻。",
        "• 条件满足：数据、结构、流动性和收益风险比通过规则门槛。",
        "• 过热/去杠杆：可能是挤空或踩踏释放，不追涨、不追空。",
        "• 失效：关键位破坏后结束本轮跟踪。",
        "",
        "<b>⚠️ 背离与缺数据</b>",
        "• 假强：价格和持仓上升，但现货、合约主动成交都偏卖出。",
        "• 假弱：价格和持仓下降，但现货、合约主动成交都偏买入。",
        "• 背离只提示反转风险，不代表已经反转，不提供交易计划。",
        "• 没有同名现货对时只做合约观察，不确认方向，也不调用AI。",
        "",
        "<b>🤖 AI做什么</b>",
        "AI只把已计算的数据和规则翻译成白话；不改方向、不改分数、不改失效位。同一个观察最多调用一次，重复生成时复用已校验结果；AI失败也不影响规则卡片。",
        "",
        "<b>🛡️ 重要边界</b>",
        "• 准备度是规则分，不是涨跌概率。",
        "• 持仓量上升不能单独说明是多头还是空头。",
        "• 主动买卖差是成交主导方，不等于钱包或资金真实流入流出。",
        "• 数据缺失或多周期冲突时只降级观察，不强行给方向。",
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
