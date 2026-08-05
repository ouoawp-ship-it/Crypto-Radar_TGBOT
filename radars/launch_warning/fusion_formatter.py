from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from ..common import coin_link, tg_bold, tg_escape


_STAGES: dict[str, tuple[str, str]] = {
    "idle": ("⚪", "启动观察"),
    "watching": ("🟡", "启动观察"),
    "primed": ("🟠", "提前预警"),
    "breakout": ("🟠", "启动确认"),
    "launched": ("🔴", "启动加速"),
    "risk": ("⚠️", "结构转弱"),
    "cooling": ("🔵", "启动降温"),
    "failed": ("⚫", "本轮启动失效"),
}

_SUPPORT_TEXT = {
    "price_momentum_met": "价格动能达到门槛",
    "open_interest_growth_met": "持仓量增长达到门槛",
    "volume_expansion_met": "成交量明显放大",
    "breakout_structure_met": "收盘突破结构位",
    "spot_active_buying_met": "现货主动买入占优",
    "futures_active_buying_met": "合约主动买入占优",
    "price_still_quiet": "价格未大动但持仓或资金先异动",
}

_COUNTER_TEXT = {
    "price_up_oi_down": "价格上涨但持仓下降，可能由平仓推动",
    "price_down_oi_up": "价格下跌且持仓增加，新增空头候选",
    "active_selling_against_move": "主动卖出与价格上涨方向相反",
    "price_without_participation": "价格变化但持仓、成交量和主动资金未跟随",
    "spot_futures_divergence": "现货与合约主动资金方向不一致",
    "funding_overheated": "资金费率偏热，拥挤风险上升",
}

_ERROR_TEXT = {
    "launch_market_facts_input_invalid": "输入数据格式不完整",
    "launch_market_facts_kline_malformed": "价格数据格式异常",
    "launch_market_facts_oi_malformed": "持仓量数据格式异常",
    "launch_market_facts_kline_duplicate": "价格数据存在重复窗口",
    "launch_market_facts_oi_duplicate": "持仓量数据存在重复窗口",
    "launch_market_facts_kline_gap": "价格窗口不连续",
    "launch_market_facts_oi_gap": "持仓量窗口不连续",
    "launch_market_facts_boundary_mismatch": "数据未对齐同一收盘窗口",
    "launch_market_facts_series_misaligned": "价格与持仓量窗口未对齐",
    "launch_market_facts_insufficient_history": "完整历史窗口不足",
}

_CONCLUSIONS = {
    "idle": "仅记录异动，等待下一次完整收盘。",
    "watching": "进入观察，等待1小时完整收盘确认。",
    "primed": "多项异动已出现，仍需1小时结构确认。",
    "breakout": "结构达到确认阶段，继续观察资金能否延续。",
    "launched": "进入加速阶段；涨幅扩大也会增加回撤风险。",
    "risk": "结构转弱，优先观察反证是否继续扩大。",
    "cooling": "动能正在降温，暂未形成新的延续确认。",
    "failed": "本轮生命周期结束；重新满足条件后开启新一轮。",
}

_EVIDENCE_STRENGTH = {
    "low": "较弱",
    "medium": "中等",
    "strong": "较强",
}

_TRIGGER_PATH = {
    "momentum": "动量共振",
    "dark_current": "资金先行",
    "none": "证据不足",
}

_ACTIVE_FLOW_STATUS = {
    "spot_pair_not_listed": "该币无币安现货对",
    "window_incomplete": "本窗口未完整",
    "budget_exhausted": "本轮请求额度已用完",
    "binance_unavailable": "币安数据暂不可用",
    "no_trades": "本窗口无成交",
}

_OI_24H_STATUS = {
    "insufficient_history": "历史不足",
    "gap": "窗口不连续",
    "boundary_missing": "最新闭合点缺失",
    "invalid": "数据异常",
    "core_invalid": "核心窗口异常",
}


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _facts(item: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in ("launch_market_facts", "market_facts"):
        value = item.get(key)
        if isinstance(value, Mapping):
            return value
    return item


def _scoring(item: Mapping[str, Any]) -> Mapping[str, Any]:
    for key in (
        "launch_scoring",
        "fusion_analysis",
        "fusion_score",
        "scoring",
    ):
        value = item.get(key)
        if isinstance(value, Mapping):
            return value
    return item


def _optional_number(*values: object) -> float | None:
    for value in values:
        if value is None or value == "":
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            return number
    return None


def _metric(mapping: Mapping[str, Any], *keys: str) -> float | None:
    return _optional_number(*(mapping.get(key) for key in keys))


def _pct(value: float | None) -> str:
    return "缺数据" if value is None else f"{value:+.2f}%"


def _ratio(value: float | None) -> str:
    return "缺数据" if value is None else f"{value:.2f}倍均值"


def _signed_money(value: float | None) -> str:
    if value is None:
        return "缺数据"
    absolute = abs(value)
    if absolute >= 1_000_000_000:
        amount = f"${absolute / 1_000_000_000:.1f}B"
    elif absolute >= 1_000_000:
        amount = f"${absolute / 1_000_000:.1f}M"
    elif absolute >= 1_000:
        amount = f"${absolute / 1_000:.1f}K"
    else:
        amount = f"${absolute:.0f}"
    return f"{'+' if value >= 0 else '-'}{amount}"


def _flow_text(value: float | None, ratio: float | None, status: object = "") -> str:
    if value is None:
        return _ACTIVE_FLOW_STATUS.get(str(status or ""), "缺数据")
    suffix = f"（主动占比{ratio * 100:+.1f}%）" if ratio is not None else ""
    return f"{_signed_money(value)}{suffix}"


def _oi_24h_text(value: float | None, status: object = "") -> str:
    if value is not None:
        return _pct(value)
    return _OI_24H_STATUS.get(str(status or ""), "缺数据")


def _short_text(value: object, *, limit: int = 24) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return tg_escape(text)


def _stage(item: Mapping[str, Any], settings: object) -> str:
    lifecycle = _mapping(item.get("launch_lifecycle"))
    stage = str(item.get("stage") or lifecycle.get("stage") or "").strip()
    if stage in _STAGES:
        return stage
    score = int(_optional_number(_scoring(item).get("score"), item.get("score")) or 0)
    if score >= int(getattr(settings, "launch_launched_score", 90)):
        return "launched"
    if score >= int(getattr(settings, "launch_breakout_score", 75)):
        return "breakout"
    if score >= int(getattr(settings, "launch_primed_score", 60)):
        return "primed"
    if score >= int(getattr(settings, "launch_watch_score", 45)):
        return "watching"
    return "idle"


def _confirmation_1h(item: Mapping[str, Any], facts: Mapping[str, Any]) -> str:
    explicit = item.get("confirmation_1h", facts.get("confirmation_1h"))
    if explicit is True or str(explicit).lower() in {"confirmed", "ok", "true"}:
        return "已确认"
    if explicit is False or str(explicit).lower() in {"pending", "false"}:
        return "待确认"
    lifecycle = _mapping(item.get("launch_lifecycle"))
    lifecycle_confirmation = str(lifecycle.get("confirmation_status") or "")
    if lifecycle_confirmation in {"confirmed_1h", "confirmed_4h"}:
        return "已确认"
    if lifecycle_confirmation in {"awaiting_1h", "rejected"}:
        return "待确认"
    price_action = _mapping(
        lifecycle.get("price_action")
        or item.get("price_action")
        or item.get("price_action_analysis")
    )
    status = str(price_action.get("status") or "")
    direction = str(price_action.get("direction") or "")
    if status in {"confirmed_1h", "confirmed_4h"} and direction == "up":
        return "已确认"
    if _metric(facts, "price_1h_pct", "price_1h") is not None:
        return "待确认"
    return "缺数据"


def _evidence_lines(
    values: object,
    labels: Mapping[str, str],
    *,
    empty_text: str,
) -> list[str]:
    if not isinstance(values, (list, tuple)):
        return [f"• {empty_text}"]
    translated = [labels[str(value)] for value in values if str(value) in labels]
    return [f"• {text}" for text in translated[:3]] or [f"• {empty_text}"]


def _funding_text(item: Mapping[str, Any]) -> str:
    available = item.get("funding_available")
    value = _optional_number(item.get("funding_pct"))
    if available is False or value is None:
        return "缺数据"
    return f"{value:+.4f}%"


def _quadrant_text(
    item: Mapping[str, Any],
    facts: Mapping[str, Any],
) -> str:
    quadrants = _mapping(facts.get("quadrants"))
    current = _mapping(quadrants.get("15m"))
    label = str(current.get("label") or "").strip()
    if label:
        return _short_text(label, limit=20)
    return {
        "price_up_oi_up": "价格上涨、持仓增加",
        "price_up_oi_down": "价格上涨、持仓减少",
        "price_down_oi_up": "价格下跌、持仓增加",
        "price_down_oi_down": "价格下跌、持仓减少",
        "neutral": "价格或持仓基本不变",
    }.get(str(item.get("price_oi_quadrant") or ""), "暂无法判断")


def _duration_text(value: object) -> str:
    seconds = max(0, int(_optional_number(value) or 0))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    return f"{hours}小时{minutes:02d}分" if hours else f"{minutes}分钟"


def _outcome_lines(item: Mapping[str, Any]) -> list[str]:
    lifecycle = _mapping(item.get("launch_lifecycle"))
    if not lifecycle:
        return []
    peak_key = str(lifecycle.get("peak_stage") or "idle")
    peak_label = _STAGES.get(peak_key, ("", "未知"))[1]
    lines = [
        f"本轮：已跟踪 {_duration_text(lifecycle.get('duration_sec'))}｜"
        f"最高 {peak_label}"
    ]
    evaluation = _mapping(lifecycle.get("outcome_evaluation"))
    reliability = _mapping(evaluation.get("reliability"))
    if reliability:
        samples = int(_optional_number(reliability.get("completed_samples")) or 0)
        minimum = int(_optional_number(reliability.get("minimum_samples")) or 20)
        if reliability.get("rates_available"):
            scope = (
                "同类同流动性"
                if reliability.get("aggregation_scope") == "asset_liquidity"
                else "同规则"
            )
            lines.extend([
                f"历史：{scope} {samples}轮",
                f"确认率 {_pct(_metric(reliability, 'confirmed_rate_pct'))}｜"
                f"跟随率 {_pct(_metric(reliability, 'followed_through_rate_pct'))}",
            ])
        else:
            lines.append(
                f"历史：样本积累中 {samples}/{minimum}轮（不自动修改参数）"
            )
    progress = _mapping(evaluation.get("progress"))
    if str(lifecycle.get("cycle_status") or "") == "failed" and progress:
        lines.append(
            "结案：有利/不利收盘变动 "
            f"{_pct(_metric(progress, 'max_favorable_return_pct'))} / "
            f"{_pct(_metric(progress, 'max_adverse_return_pct'))}"
        )
    return lines


def format_launch_fusion_package(item: Mapping[str, Any], settings: object) -> str:
    """Format one deterministic launch lifecycle snapshot for Telegram HTML."""

    facts = _facts(item)
    scoring = _scoring(item)
    stage = _stage(item, settings)
    icon, stage_label = _STAGES[stage]
    score = int(_optional_number(scoring.get("score"), item.get("score")) or 0)
    appear_count = max(1, int(_optional_number(item.get("appear_count")) or 1))
    category = _short_text(item.get("asset_category_label") or "未分类")

    price_15m = _metric(facts, "price_15m_pct", "price_15m")
    price_1h = _metric(facts, "price_1h_pct", "price_1h")
    price_4h = _metric(facts, "price_4h_pct", "price_4h")
    price_24h = _metric(
        facts, "price_24h_rolling_pct", "price_24h_pct", "price_24h"
    )
    oi_15m = _metric(facts, "oi_15m_pct", "oi_15m")
    oi_1h = _metric(facts, "oi_1h_pct", "oi_1h")
    oi_4h = _metric(facts, "oi_4h_pct", "oi_4h")
    oi_24h = _metric(
        facts, "oi_24h_closed_pct", "oi_24h_pct", "oi_24h"
    )
    volume_ratio = _metric(facts, "volume_ratio_15m", "volume_ratio")

    spot_flow = _optional_number(
        item.get("spot_active_net_usd"), item.get("spot_flow_usd")
    )
    futures_flow = _optional_number(
        item.get("futures_active_net_usd"), item.get("futures_flow_usd")
    )
    spot_ratio = _optional_number(item.get("spot_active_ratio"))
    futures_ratio = _optional_number(item.get("futures_active_ratio"))

    support_lines = _evidence_lines(
        scoring.get("supporting_evidence"),
        _SUPPORT_TEXT,
        empty_text="暂无明确支持证据",
    )
    counter_lines = _evidence_lines(
        scoring.get("counter_evidence"),
        _COUNTER_TEXT,
        empty_text="暂无明显反向证据",
    )
    data_status = "完整" if str(facts.get("status") or "ok") == "ok" else "不完整"
    evidence_strength = _EVIDENCE_STRENGTH.get(
        str(item.get("evidence_strength") or ""),
        "待积累",
    )
    trigger_path = _TRIGGER_PATH.get(
        str(scoring.get("trigger_path") or item.get("trigger_path") or "none"),
        "证据不足",
    )

    outcome_lines = _outcome_lines(item)
    outcome_section = (
        ["", tg_bold("📍 跟踪结果"), *[f"• {line}" for line in outcome_lines]]
        if outcome_lines
        else []
    )
    return "\n".join([
        f"{icon} {tg_bold(stage_label)}",
        coin_link(dict(item)),
        f"{tg_bold('当前判断')}：{_CONCLUSIONS[stage]}",
        f"规则分：{score}/100（不是概率）｜证据：{evidence_strength}",
        f"触发：{trigger_path}｜1小时：{_confirmation_1h(item, facts)}",
        f"品类：{category}｜本轮第{appear_count}次｜数据：{data_status}",
        "",
        tg_bold("🔥 核心变化"),
        f"• 15分钟：价格 {_pct(price_15m)}｜持仓 {_pct(oi_15m)}",
        f"• 1小时：价格 {_pct(price_1h)}｜持仓 {_pct(oi_1h)}",
        f"• 成交量：{_ratio(volume_ratio)}｜资金费率 {_funding_text(item)}",
        "",
        tg_bold("💰 主动资金"),
        f"• 现货：{_flow_text(spot_flow, spot_ratio, item.get('spot_active_status'))}",
        f"• 合约：{_flow_text(futures_flow, futures_ratio, item.get('futures_active_status'))}",
        "",
        tg_bold("✅ 支持证据"),
        *support_lines,
        "",
        tg_bold("⚠️ 反向证据"),
        *counter_lines,
        "",
        tg_bold("🔭 背景参考"),
        f"• 4小时：价格 {_pct(price_4h)}｜持仓 {_pct(oi_4h)}",
        f"• 24小时：价格 {_pct(price_24h)}（滚动）",
        f"• 24小时持仓：{_oi_24h_text(oi_24h, facts.get('oi_24h_status') or item.get('oi_24h_status'))}（严格闭合）",
        f"• 价量结构：{_quadrant_text(item, facts)}",
        *outcome_section,
        "",
        f"{tg_bold('数据说明')}：缺失项不会按0计算。",
    ])


def format_launch_fusion_incomplete(item: Mapping[str, Any]) -> str:
    """Format a safe degraded card without leaking internal error details."""

    facts = _facts(item)
    scoring = _scoring(item)
    availability = _mapping(scoring.get("data_availability"))
    checks = {
        "价格": bool(availability.get("price")) or any(
            _metric(facts, key) is not None
            for key in ("price_15m_pct", "price_1h_pct", "price_4h_pct")
        ),
        "持仓量": bool(availability.get("open_interest")) or any(
            _metric(facts, key) is not None
            for key in ("oi_15m_pct", "oi_1h_pct", "oi_4h_pct")
        ),
        "成交量": bool(availability.get("volume"))
        or _metric(facts, "volume_ratio_15m", "volume_ratio") is not None,
        "主动资金": bool(availability.get("active_funds"))
        or _optional_number(
            item.get("spot_active_net_usd"), item.get("futures_active_net_usd")
        )
        is not None,
    }
    ready = "、".join(name for name, value in checks.items() if value) or "暂未确认"
    missing = "、".join(name for name, value in checks.items() if not value) or "无"
    error = str(
        facts.get("error")
        or item.get("analysis_error")
        or "launch_market_facts_input_invalid"
    )
    reason = _ERROR_TEXT.get(error, "数据校验未通过")
    return "\n".join([
        f"⚪ {tg_bold('启动数据不足')}",
        coin_link(dict(item)),
        f"{tg_bold('当前判断')}：本轮不升级，等待完整收盘数据。",
        "确认：15分钟发现暂停｜1小时不升级",
        "",
        tg_bold("🧾 数据状态"),
        f"• 已取得：{ready}",
        f"• 缺少：{missing}",
        f"• 原因：{reason}",
        "",
        "处理：缺失项不会按0计算，也不会形成高确定性判断。",
    ])


__all__ = [
    "format_launch_fusion_incomplete",
    "format_launch_fusion_package",
]
