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


def format_alert(alert: OnchainAlert) -> str:
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
