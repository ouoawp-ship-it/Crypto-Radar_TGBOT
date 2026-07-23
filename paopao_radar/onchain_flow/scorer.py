from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from .constants import P3_1_SEVERITY_VERSION, SEVERITY_VERSION
from .models import (
    ClassifiedFlow,
    DetectedFlow,
    DetectedRollingFlow,
    OnchainAlert,
)


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
        reasons.append("流入交易所增加潜在可售供应")
    else:
        reasons.append("从交易所流出代表潜在提币或积累")
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


def score_rolling_detection(detected: DetectedRollingFlow) -> OnchainAlert:
    snapshot = detected.snapshot
    absolute = max(TYPE_SCORES[kind] for kind in detected.detection_types)
    if "multi_exchange" in detected.detection_types:
        absolute += TYPE_SCORES["multi_exchange"]
    if detected.threshold_usd > 0:
        ratio = abs(snapshot.net_flow_usd) / detected.threshold_usd
        absolute += min(10, max(0, int((ratio - Decimal("1")) * 10)))
    absolute = min(100, absolute)
    score = -absolute if snapshot.direction == "inflow" else absolute
    reasons = [
        (
            "净流入交易所形成偏空方向先验"
            if snapshot.direction == "inflow"
            else "净流出交易所形成偏多方向先验"
        ),
        (
            f"净流占主方向总流量比例 "
            f"{snapshot.net_dominance * Decimal('100'):.1f}%"
        ),
    ]
    if "batch_flow" in detected.detection_types:
        reasons.append("滚动 15 分钟多地址资金流达到阈值")
    if "continuous_flow" in detected.detection_types:
        reasons.append("滚动 60 分钟至少三个 15 分钟桶持续活跃")
    if "multi_exchange" in detected.detection_types:
        reasons.append("至少两家交易所出现同方向净流")
    confidence = (
        "high"
        if absolute >= 70 and snapshot.min_label_confidence >= 0.90
        else "medium"
    )
    return OnchainAlert(
        alert_key=(
            f"{snapshot.snapshot_key}:{snapshot.direction}:"
            f"{P3_1_SEVERITY_VERSION}:{confidence}"
        ),
        chain_id=snapshot.chain_id,
        token_address=snapshot.token_address,
        symbol=snapshot.symbol,
        direction=snapshot.direction,
        score=score,
        horizon="1h-4h",
        confidence=confidence,
        reasons=tuple(reasons),
        detection_types=detected.detection_types,
        window_start=snapshot.evaluation_time - snapshot.duration_sec,
        window_end=snapshot.evaluation_time,
        total_usd=abs(snapshot.net_flow_usd),
        tx_count=snapshot.inflow_tx_count + snapshot.outflow_tx_count,
        exchanges=snapshot.exchanges,
        label_confidence=snapshot.min_label_confidence,
        price_status="available",
        created_at=snapshot.evaluation_time,
        severity_version=P3_1_SEVERITY_VERSION,
        gross_inflow_usd=snapshot.gross_inflow_usd,
        gross_outflow_usd=snapshot.gross_outflow_usd,
        net_flow_usd=snapshot.net_flow_usd,
        duration_sec=snapshot.duration_sec,
        inflow_tx_count=snapshot.inflow_tx_count,
        outflow_tx_count=snapshot.outflow_tx_count,
        distinct_inbound_counterparties=(
            snapshot.distinct_inbound_counterparties
        ),
        distinct_outbound_counterparties=(
            snapshot.distinct_outbound_counterparties
        ),
        evaluation_block=snapshot.evaluation_block,
        price_source=snapshot.price_source,
        price_observed_at=snapshot.price_observed_at,
        chain_name="Base",
    )


def score_live_single_detection(
    detected: DetectedFlow, flow: ClassifiedFlow
) -> OnchainAlert:
    alert = score_detection(detected)
    return replace(
        alert,
        alert_key=(
            f"{flow.chain_id}:{flow.token_address}:{flow.flow_type}:"
            f"{flow.block_number}:{flow.block_hash}:"
            f"{P3_1_SEVERITY_VERSION}:{flow.event_id}"
        ),
        severity_version=P3_1_SEVERITY_VERSION,
        gross_inflow_usd=(
            flow.amount_usd
            if flow.flow_type == "inflow"
            else Decimal("0")
        ),
        gross_outflow_usd=(
            flow.amount_usd
            if flow.flow_type == "outflow"
            else Decimal("0")
        ),
        evaluation_block=flow.block_number,
        price_source=flow.price_source,
        price_observed_at=flow.price_observed_at,
        chain_name="Base",
    )
