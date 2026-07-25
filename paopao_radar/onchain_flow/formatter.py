from __future__ import annotations

from decimal import Decimal

from .models import OnchainAlert


DETECTION_LABELS = {
    "single_large": "单笔大额",
    "batch_flow": "15m 批量",
    "continuous_flow": "60m 持续",
    "multi_exchange": "多交易所同步",
}


def _usd(value: Decimal) -> str:
    absolute = abs(value)
    if absolute >= Decimal("1000000"):
        return f"${value / Decimal('1000000'):.2f}M"
    if absolute >= Decimal("1000"):
        return f"${value / Decimal('1000'):.2f}K"
    return f"${value:.2f}"


def _signed_usd(value: Decimal) -> str:
    prefix = "+" if value > 0 else ""
    return prefix + _usd(value)


def format_alert(alert: OnchainAlert) -> str:
    if alert.source == "arkham":
        return _format_arkham_alert(alert)
    if alert.net_flow_usd is not None:
        return _format_rolling_alert(alert)
    if alert.chain_name and (
        alert.gross_inflow_usd is not None
        or alert.gross_outflow_usd is not None
    ):
        return _format_live_single_alert(alert)
    bearish = alert.direction == "inflow"
    marker = "[流入]" if bearish else "[流出]"
    direction_text = "偏空" if bearish else "偏多"
    flow_text = "流入交易所" if bearish else "从交易所流出"
    confidence = {
        "high": "高",
        "medium": "中",
        "low": "低",
    }.get(alert.confidence, alert.confidence)
    exchange_text = "、".join(alert.exchanges) or "单一交易所"
    detection_text = "、".join(
        DETECTION_LABELS.get(kind, kind) for kind in alert.detection_types
    )
    reason_lines = "\n".join(f"- {reason}" for reason in alert.reasons)
    return "\n".join(
        [
            f"{marker} ${alert.symbol} 异常资金{flow_text}",
            "",
            f"判断：未来 {alert.horizon} {direction_text}",
            f"方向评分：{alert.score:+d} / 100（不是概率）",
            f"置信度：{confidence}",
            "",
            "链上资金流：",
            f"- 金额：{_usd(alert.total_usd)}",
            f"- 交易：{alert.tx_count} 笔",
            f"- 交易所：{exchange_text}",
            f"- 触发：{detection_text}",
            "",
            "解释：",
            reason_lines,
            "",
            (
                "交易所资金流仅代表方向性倾向，不保证价格必然上涨或下跌；"
                "请结合市场结构独立判断。"
            ),
            f"链 ID：{alert.chain_id}",
            f"标签最低置信度：{alert.label_confidence:.2f}",
        ]
    )


def _format_arkham_alert(alert: OnchainAlert) -> str:
    flow_name = {
        "inflow": "流入交易所",
        "outflow": "从交易所流出",
    }.get(alert.direction, "非方向性资金流")
    signal_type = (
        "单笔信号"
        if alert.duration_sec <= 0
        else f"{alert.duration_sec // 60} 分钟滚动信号"
    )
    exchange_text = "、".join(alert.exchanges) or "已识别交易所"
    attribution = {
        "arkham_entity": "Arkham 实体",
        "arkham_label": "Arkham 标签",
        "unlabeled": "未标注",
    }.get(alert.attribution_quality, "未知")
    confidence = {
        "high": "高",
        "medium": "中",
        "low": "低",
        "context": "背景信息",
    }.get(alert.confidence, alert.confidence)
    gross_inflow = alert.gross_inflow_usd or Decimal("0")
    gross_outflow = alert.gross_outflow_usd or Decimal("0")
    net_flow = (
        alert.net_flow_usd
        if alert.net_flow_usd is not None
        else gross_inflow - gross_outflow
    )
    lines = [
        f"[Arkham] 链上资金流｜${alert.symbol}｜{flow_name}",
        f"Token 标识：{alert.token_address}",
        f"链：{alert.chain_name or alert.chain_id}",
        f"信号类型：{signal_type}",
        f"交易所：{exchange_text}",
        f"事件发生时美元价值：{_usd(alert.total_usd)}",
        f"总流入：{_usd(gross_inflow)}",
        f"总流出：{_usd(gross_outflow)}",
    ]
    if alert.duration_sec > 0:
        lines.append(f"净流量（流入-流出）：{_signed_usd(net_flow)}")
        lines.append(
            "交易笔数："
            f"流入 {alert.inflow_tx_count}｜流出 {alert.outflow_tx_count}"
        )
    lines.extend(
        [
            f"Arkham 归因质量：{attribution}",
            f"方向评分：{alert.score:+d} / 100（评分不是概率）",
            f"置信等级：{confidence}",
            "说明：Arkham 实体归因为概率性情报，并非确定事实。",
        ]
    )
    if alert.signal_context == "market_liquidity_context":
        lines.extend(
            [
                "解读：稳定币流向只作为市场流动性背景。",
                "该信号不预测稳定币自身价格上涨或下跌。",
            ]
        )
    else:
        prior = (
            "可能增加交易所内的潜在可售供应"
            if alert.direction == "inflow"
            else "可能代表提币或交易所余额转出"
        )
        lines.extend(
            [
                f"方向性先验：{prior}。",
                "这只是资金流背景，不保证价格方向，也不代表价格概率。",
            ]
        )
    if alert.excluded_unpriced_count:
        lines.append(
            "未计入美元汇总的无价格事件："
            f"{alert.excluded_unpriced_count} 条"
        )
    lines.append("数据来源：Arkham /transfers 的 historicalUSD")
    return "\n".join(lines)


def _format_live_single_alert(alert: OnchainAlert) -> str:
    bearish = alert.direction == "inflow"
    marker = "[流入]" if bearish else "[流出]"
    direction_text = "偏空" if bearish else "偏多"
    gross = (
        alert.gross_inflow_usd
        if bearish
        else alert.gross_outflow_usd
    ) or Decimal("0")
    price_age = max(0, alert.created_at - alert.price_observed_at)
    reason_lines = "\n".join(f"- {reason}" for reason in alert.reasons)
    return "\n".join(
        [
            f"{marker} ${alert.symbol} Base 单笔交易所资金流",
            f"合约：{alert.token_address}",
            "",
            f"判断：未来 {alert.horizon} {direction_text}",
            f"方向评分：{alert.score:+d} / 100（评分不是概率）",
            f"置信度：{alert.confidence}",
            "",
            f"单笔总流量：{_usd(gross)}（单笔总额，不是净流量）",
            f"交易所：{'、'.join(alert.exchanges) or '单一交易所'}",
            f"价格：{alert.price_source or 'unknown'} / {price_age}s 前",
            f"标签最低置信度：{alert.label_confidence:.2f}",
            f"Base finalized block：{alert.evaluation_block}",
            "",
            "解释：",
            reason_lines,
            "",
            (
                "交易所资金流仅代表方向性倾向，不保证价格必然上涨或下跌；"
                "请结合市场结构独立判断。"
            ),
        ]
    )


def _format_rolling_alert(alert: OnchainAlert) -> str:
    bearish = alert.direction == "inflow"
    marker = "[净流入]" if bearish else "[净流出]"
    direction_text = "偏空" if bearish else "偏多"
    confidence = {
        "high": "高",
        "medium": "中",
        "low": "低",
    }.get(alert.confidence, alert.confidence)
    exchange_text = "、".join(alert.exchanges) or "单一交易所"
    detection_text = "、".join(
        DETECTION_LABELS.get(kind, kind) for kind in alert.detection_types
    )
    reason_lines = "\n".join(f"- {reason}" for reason in alert.reasons)
    price_age = max(0, alert.created_at - alert.price_observed_at)
    return "\n".join(
        [
            f"{marker} ${alert.symbol} Base 交易所滚动资金流",
            f"合约：{alert.token_address}",
            "",
            f"判断：未来 {alert.horizon} {direction_text}",
            f"方向评分：{alert.score:+d} / 100（评分不是概率）",
            f"置信度：{confidence}",
            "",
            f"滚动窗口：{alert.duration_sec // 60}m",
            f"- 总流入交易所：{_usd(alert.gross_inflow_usd or Decimal('0'))}",
            f"- 总从交易所流出：{_usd(alert.gross_outflow_usd or Decimal('0'))}",
            f"- 净流量（流入-流出）：{_signed_usd(alert.net_flow_usd)}",
            (
                f"- 交易：流入 {alert.inflow_tx_count} 笔 / "
                f"流出 {alert.outflow_tx_count} 笔"
            ),
            (
                f"- 对手方：流入 {alert.distinct_inbound_counterparties} / "
                f"流出 {alert.distinct_outbound_counterparties}"
            ),
            f"- 交易所：{exchange_text}",
            f"- 触发：{detection_text}",
            f"- 价格：{alert.price_source or 'unknown'} / {price_age}s 前",
            f"- 标签最低置信度：{alert.label_confidence:.2f}",
            f"- Base finalized block：{alert.evaluation_block}",
            "",
            "解释：",
            reason_lines,
            "",
            (
                "交易所资金流仅代表方向性倾向，不保证价格必然上涨或下跌；"
                "请结合市场结构独立判断。"
            ),
        ]
    )
