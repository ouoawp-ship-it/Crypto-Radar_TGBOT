from __future__ import annotations

from typing import Any


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def clamp(value: float, low: float = 0.0, high: float = 100.0) -> float:
    return max(low, min(high, value))


def risk_level(score: float) -> str:
    if score >= 70:
        return "HIGH"
    if score >= 40:
        return "MEDIUM"
    return "LOW"


def calculate_wash_risk(metrics: dict[str, Any]) -> dict[str, Any]:
    """Estimate whether a launch signal is mostly mechanical volume noise.

    Inputs are plain numeric metrics so the API and scorer can use this module
    without coupling it to a specific exchange client.
    """
    score = 0.0
    reasons: list[str] = []

    volume_ratio = to_float(metrics.get("volume_ratio"), 1.0)
    price_change_pct = to_float(metrics.get("price_change_pct"))
    oi_change_pct = to_float(metrics.get("oi_change_pct"))
    taker_buy_sell_ratio = to_float(metrics.get("taker_buy_sell_ratio"), 1.0)
    trade_count_ratio = to_float(metrics.get("trade_count_ratio"), 1.0)
    avg_trade_usd = to_float(metrics.get("avg_trade_usd"))
    avg_trade_usd_baseline = to_float(metrics.get("avg_trade_usd_baseline"))
    cross_exchange_confirmed = bool(metrics.get("cross_exchange_confirmed"))
    volume_marketcap_ratio = to_float(metrics.get("volume_marketcap_ratio"))
    oi_marketcap_ratio = to_float(metrics.get("oi_marketcap_ratio"))
    price_1h_change_pct = to_float(metrics.get("price_1h_change_pct"))
    binance_anomaly = bool(metrics.get("binance_anomaly", True))

    if volume_ratio >= 3 and abs(price_change_pct) <= 1.2:
        score += 20
        reasons.append("成交额暴增但价格位移很小")
    elif volume_ratio >= 2.2 and abs(price_change_pct) <= 0.8:
        score += 12
        reasons.append("成交额放大但价格响应偏弱")

    if oi_change_pct >= 10 and abs(taker_buy_sell_ratio - 1.0) <= 0.08:
        score += 18
        reasons.append("OI暴增但主动买卖比接近1")
    elif oi_change_pct >= 6 and abs(taker_buy_sell_ratio - 1.0) <= 0.05:
        score += 10
        reasons.append("OI上升但主动方向不清晰")

    tiny_trade = avg_trade_usd > 0 and avg_trade_usd < 80
    much_smaller_than_baseline = (
        avg_trade_usd > 0
        and avg_trade_usd_baseline > 0
        and avg_trade_usd <= avg_trade_usd_baseline * 0.35
    )
    if trade_count_ratio >= 3 and (tiny_trade or much_smaller_than_baseline):
        score += 14
        reasons.append("交易笔数异常但平均单笔金额过小")

    if binance_anomaly and not cross_exchange_confirmed:
        score += 14
        reasons.append("Binance异动暂未获得OKX/Bybit等交易所确认")

    if volume_marketcap_ratio >= 3:
        score += 20
        reasons.append("成交额/市值过高")
    elif volume_marketcap_ratio >= 1.5:
        score += 12
        reasons.append("成交额/市值偏高")

    if oi_marketcap_ratio >= 0.5:
        score += 16
        reasons.append("OI/市值过高")
    elif oi_marketcap_ratio >= 0.25:
        score += 10
        reasons.append("OI/市值偏高")

    if abs(price_1h_change_pct) >= 12 and oi_change_pct >= 10:
        score += 12
        reasons.append("价格已大幅运行后才出现OI暴增")

    score = round(clamp(score), 1)
    return {
        "wash_risk_score": score,
        "risk_level": risk_level(score),
        "risk_reasons": reasons,
    }
