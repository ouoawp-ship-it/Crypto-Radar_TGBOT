from __future__ import annotations

from decimal import Decimal

from .constants import SEVERITY_VERSION
from .models import DetectedFlow, OnchainAlert


TYPE_SCORES = {
    "single_large": 45,
    "batch_flow": 55,
    "continuous_flow": 65,
    "multi_exchange": 10,
}


def score_detection(detected: DetectedFlow) -> OnchainAlert:
    window = detected.window
    magnitude_bonus = 0
    if detected.threshold_usd > 0:
        ratio = window.total_usd / detected.threshold_usd
        magnitude_bonus = min(10, max(0, int((ratio - Decimal("1")) * 10)))
    absolute = max(TYPE_SCORES[kind] for kind in detected.detection_types)
    if "multi_exchange" in detected.detection_types:
        absolute += TYPE_SCORES["multi_exchange"]
    absolute = min(100, absolute + magnitude_bonus)
    score = -absolute if window.direction == "inflow" else absolute

    reasons: list[str] = []
    if window.direction == "inflow":
        reasons.append("交易所净流入增加潜在可售供应")
    else:
        reasons.append("交易所净流出代表潜在提币或积累")
    if "single_large" in detected.detection_types:
        reasons.append("单笔金额超过保守动态阈值")
    if "batch_flow" in detected.detection_types:
        reasons.append("15 分钟内多地址同方向批量流动")
    if "continuous_flow" in detected.detection_types:
        reasons.append("60 分钟内至少三个 15 分钟桶持续活跃")
    if "multi_exchange" in detected.detection_types:
        reasons.append("至少两家交易所出现同方向流动")

    if absolute >= 70 and window.min_label_confidence >= 0.90:
        confidence = "high"
    elif absolute >= 45:
        confidence = "medium"
    else:
        confidence = "low"
    event_suffix = (
        f":{detected.source_event_ids[0]}"
        if detected.source_event_ids
        else ""
    )
    alert_key = (
        f"{window.chain_id}:{window.token_address}:{window.direction}:"
        f"{window.window_start}:{window.duration_sec}:{SEVERITY_VERSION}"
        f"{event_suffix}"
    )
    return OnchainAlert(
        alert_key=alert_key,
        chain_id=window.chain_id,
        token_address=window.token_address,
        symbol=window.symbol,
        direction=window.direction,
        score=score,
        horizon="1h-4h",
        confidence=confidence,
        reasons=tuple(reasons),
        detection_types=detected.detection_types,
        window_start=window.window_start,
        window_end=window.window_end,
        total_usd=window.total_usd,
        tx_count=window.tx_count,
        exchanges=window.exchanges,
        label_confidence=window.min_label_confidence,
        price_status="available",
        created_at=window.window_end,
        severity_version=SEVERITY_VERSION,
    )
