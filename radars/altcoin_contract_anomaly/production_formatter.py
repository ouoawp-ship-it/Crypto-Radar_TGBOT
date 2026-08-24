from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any, Iterable, Mapping


TELEGRAM_TEMPLATE_ID = "TG_ALTCOIN_CONTRACT_ANOMALY"

_EVENT_TYPE_ORDER = (
    "short_fuel_building",
    "short_squeeze_ignition",
    "high_leverage_anomaly",
    "long_crowding_risk",
    "anomaly_weakening",
    "candidate_condition_invalidated",
)
_EVENT_NAMES_CN = {
    "short_fuel_building": "空头燃料堆积",
    "short_squeeze_ignition": "逼空启动",
    "high_leverage_anomaly": "高杠杆异动",
    "long_crowding_risk": "多头拥挤风险",
    "anomaly_weakening": "异动减弱",
    "candidate_condition_invalidated": "候选条件失效",
}
_FACTOR_ORDER = (
    "price_momentum",
    "volume_expansion",
    "aggressive_flow",
    "open_interest",
    "funding",
    "liquidation",
)
_FACTOR_NAMES_CN = {
    "price_momentum": "价格动量",
    "volume_expansion": "成交量放大",
    "aggressive_flow": "主动买卖与CVD",
    "open_interest": "OI变化",
    "funding": "资金费率变化",
    "liquidation": "多空爆仓",
}
_CANDIDATE_TAG_NAMES_CN = {
    "short_squeeze_candidate": "潜在逼空",
    "high_leverage_candidate": "高合约杠杆",
}
_NOTIFICATION_TITLES = {
    "first_confirmation": "🚨【山寨合约异动｜首次确认】",
    "new_round": "🚨【山寨合约异动｜新一轮异动】",
    "signal_expired": "⚠️【山寨合约异动｜信号过期】",
    "candidate_invalidated": "⚠️【山寨合约异动｜候选失效】",
}
_BEIJING_TIME = timezone(timedelta(hours=8))


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _money(value: Any, *, signed: bool = False) -> str:
    number = _number(value)
    if number is None:
        return "缺数据"
    if number < 0 and not signed:
        return "缺数据"
    sign = "+" if signed and number > 0 else "-" if number < 0 else ""
    absolute = abs(number)
    if absolute >= 1_000_000_000:
        return f"{sign}${absolute / 1_000_000_000:.2f}B"
    if absolute >= 1_000_000:
        return f"{sign}${absolute / 1_000_000:.2f}M"
    if absolute >= 1_000:
        return f"{sign}${absolute / 1_000:.0f}K"
    return f"{sign}${absolute:.2f}"


def _percent(value: Any, *, decimals: int, signed: bool = False) -> str:
    number = _number(value)
    if number is None:
        return "缺数据"
    percent = number * 100
    sign = "+" if signed and percent > 0 else ""
    return f"{sign}{percent:.{decimals}f}%"


def _multiple(value: Any) -> str:
    number = _number(value)
    return "缺数据" if number is None else f"基线的{number:.1f}倍"


def _beijing_time(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "缺数据"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return "缺数据"
    if parsed.tzinfo is None:
        return "缺数据"
    return parsed.astimezone(_BEIJING_TIME).strftime("%Y-%m-%d %H:%M:%S（北京时间）")


def _event_sort_key(event: Mapping[str, Any]) -> tuple[int, str]:
    event_type = str(event.get("event_type") or "")
    try:
        order = _EVENT_TYPE_ORDER.index(event_type)
    except ValueError:
        order = len(_EVENT_TYPE_ORDER)
    return order, str(event.get("event_id") or "")


def group_production_events(
    events: Iterable[Mapping[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Group deterministic P2 events by symbol and closed window."""

    grouped: dict[tuple[str, str, str], dict[str, dict[str, Any]]] = {}
    for source in events:
        if not isinstance(source, Mapping):
            raise ValueError("production event must be a mapping")
        event = dict(source)
        symbol = str(event.get("symbol") or "").strip().upper()
        window_start = str(event.get("window_start") or "").strip()
        window_end = str(event.get("window_end") or "").strip()
        event_id = str(event.get("event_id") or "").strip()
        if not symbol or not window_end or not event_id:
            raise ValueError("production event identity is incomplete")
        grouped.setdefault(
            (symbol, window_start, window_end),
            {},
        ).setdefault(event_id, event)
    return [
        sorted(grouped[key].values(), key=_event_sort_key)
        for key in sorted(grouped, key=lambda item: (item[2], item[0], item[1]))
    ]


def _notification_kind(
    events: list[dict[str, Any]],
    context: Mapping[str, Any] | None = None,
) -> str:
    explicit = {
        str(event.get("notification_kind") or "").strip()
        for event in events
        if str(event.get("notification_kind") or "").strip()
    }
    context_kind = str((context or {}).get("notification_kind") or "").strip()
    if context_kind:
        explicit.add(context_kind)
    if len(explicit) > 1:
        raise ValueError("conflicting notification kinds in one event window")
    if explicit:
        kind = next(iter(explicit))
        if kind not in _NOTIFICATION_TITLES:
            raise ValueError("unsupported production notification kind")
        return kind
    types = {str(event.get("event_type") or "") for event in events}
    if "candidate_condition_invalidated" in types:
        return "candidate_invalidated"
    if "anomaly_weakening" in types:
        return "signal_expired"
    return "first_confirmation"


def _consistent_mapping(
    events: list[dict[str, Any]],
    field: str,
) -> Mapping[str, Any]:
    values = [event.get(field) for event in events]
    if all(value is None for value in values):
        return {}
    if any(value is None for value in values):
        raise ValueError(f"conflicting {field} in one event window")
    if not all(isinstance(value, Mapping) for value in values):
        raise ValueError(f"{field} must be a mapping")
    first = dict(values[0])
    if any(dict(value) != first for value in values[1:]):
        raise ValueError(f"conflicting {field} in one event window")
    return first


def _consistent_quality(events: list[dict[str, Any]]) -> str:
    values = {
        str(event.get("data_quality") or "").strip()
        for event in events
    }
    if len(values) != 1:
        raise ValueError("conflicting data quality in one event window")
    value = next(iter(values))
    return {
        "complete": "完整",
        "partial": "部分缺失",
        "stale": "已过期",
        "insufficient_history": "历史不足",
        "subscription_degraded": "订阅降级",
        "manifest_degraded": "候选池降级",
        "capacity_degraded": "容量降级",
    }.get(value, "缺数据")


def _candidate_labels(events: list[dict[str, Any]]) -> list[str]:
    tags = {
        str(tag)
        for event in events
        for tag in (event.get("candidate_tags") or [])
    }
    unknown = tags.difference(_CANDIDATE_TAG_NAMES_CN)
    if unknown:
        raise ValueError("unsupported candidate tag")
    return [
        label for tag, label in _CANDIDATE_TAG_NAMES_CN.items()
        if tag in tags
    ]


def _factor_labels(events: list[dict[str, Any]]) -> list[str]:
    factors = {
        str(factor)
        for event in events
        for factor in (event.get("confirmed_factor_families") or [])
    }
    unknown = factors.difference(_FACTOR_NAMES_CN)
    if unknown:
        raise ValueError("unsupported confirmation factor family")
    return [
        _FACTOR_NAMES_CN[factor]
        for factor in _FACTOR_ORDER
        if factor in factors
    ]


def _event_labels(events: list[dict[str, Any]]) -> list[str]:
    labels: list[str] = []
    for event in events:
        event_type = str(event.get("event_type") or "")
        if event_type not in _EVENT_NAMES_CN:
            raise ValueError("unsupported production event type")
        label = str(event.get("event_name_cn") or _EVENT_NAMES_CN.get(event_type) or event_type)
        if label and label not in labels:
            labels.append(label)
    return labels


def _message_lines(
    events: list[dict[str, Any]],
    context: Mapping[str, Any] | None = None,
) -> list[str]:
    first = events[0]
    symbol = str(first["symbol"]).strip().upper()
    factor_values = _consistent_mapping(events, "factor_values")
    candidate = _consistent_mapping(events, "candidate_snapshot")
    candidate_labels = _candidate_labels(events)
    factor_labels = _factor_labels(events)
    event_labels = _event_labels(events)
    title = _NOTIFICATION_TITLES[_notification_kind(events, context)]
    return [
        f"<b>{escape(title)}</b>",
        "",
        f"<b>{escape(symbol)}</b>",
        f"异动类型：{escape(' + '.join(event_labels) if event_labels else '缺数据')}",
        f"候选依据：{escape(' + '.join(candidate_labels) if candidate_labels else '缺数据')}",
        f"实时确认：{len(factor_labels)}项",
        f"确认依据：{escape('｜'.join(factor_labels) if factor_labels else '无（候选状态事件）')}",
        "",
        f"市值：{_money(factor_values.get('market_cap_usd'))}",
        f"Binance OI：{_money(factor_values.get('oi_value_usd'))}",
        f"OI/市值：{_percent(factor_values.get('oi_market_cap_ratio'), decimals=1)}",
        f"资金费率：{_percent(factor_values.get('funding_rate'), decimals=4)}",
        "",
        f"1分钟价格：{_percent(factor_values.get('price_change_1m'), decimals=1, signed=True)}",
        f"5分钟价格：{_percent(factor_values.get('price_change_5m'), decimals=1, signed=True)}",
        f"5分钟OI：{_percent(factor_values.get('oi_change_5m'), decimals=1, signed=True)}",
        f"主动买入占比：{_percent(factor_values.get('aggressive_buy_ratio_5m'), decimals=1)}",
        f"成交量：{_multiple(factor_values.get('volume_anomaly_multiple'))}",
        f"5分钟CVD：{_money(factor_values.get('cvd_5m_usd'), signed=True)}",
        f"空头爆仓：{_money(factor_values.get('short_liquidation_5m_usd'))}",
        f"多头爆仓：{_money(factor_values.get('long_liquidation_5m_usd'))}",
        "",
        f"数据时间：{_beijing_time(first.get('window_end'))}",
        f"数据完整度：{escape(_consistent_quality(events))}",
    ]


def _paginate(lines: list[str], *, max_chars: int) -> list[str]:
    limit = int(max_chars)
    if limit < 256:
        raise ValueError("Telegram page limit is too small")
    full = "\n".join(lines)
    if len(full) <= limit:
        return [full]

    prefix = lines[:3]
    body = lines[3:]
    reserve = len("\n".join([*prefix, "第999/999页"])) + 1
    pages: list[list[str]] = []
    current: list[str] = []
    for line in body:
        candidate = [*current, line]
        if current and reserve + len("\n".join(candidate)) > limit:
            pages.append(current)
            current = [line]
        else:
            current = candidate
        if reserve + len("\n".join(current)) > limit:
            raise ValueError("one Telegram message line exceeds page limit")
    if current:
        pages.append(current)
    total = len(pages)
    rendered = [
        "\n".join([*prefix, f"第{index}/{total}页", *page])
        for index, page in enumerate(pages, start=1)
    ]
    if any(len(page) > limit for page in rendered):
        raise ValueError("Telegram pagination invariant failed")
    return rendered


def render_production_event_group(
    events: Iterable[Mapping[str, Any]],
    context: Mapping[str, Any] | None = None,
    *,
    max_chars: int = 4000,
) -> list[str]:
    """Render one symbol/window event group into bounded HTML pages."""

    groups = group_production_events(events)
    if len(groups) != 1:
        raise ValueError("exactly one symbol/window event group is required")
    return _paginate(_message_lines(groups[0], context), max_chars=max_chars)


__all__ = [
    "TELEGRAM_TEMPLATE_ID",
    "group_production_events",
    "render_production_event_group",
]
