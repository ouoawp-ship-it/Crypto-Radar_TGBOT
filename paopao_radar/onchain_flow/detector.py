from __future__ import annotations

from decimal import Decimal
from typing import Iterable

from .config import OnchainSettings
from .constants import (
    ALGORITHM_VERSION,
    DIRECTIONAL_FLOW_TYPES,
    THRESHOLD_VERSION,
    WINDOW_15M_SEC,
    WINDOW_60M_SEC,
)
from .models import (
    ClassifiedFlow,
    DetectedFlow,
    DetectedRollingFlow,
    FlowWindow,
    RollingFlowSnapshot,
    TokenMetadata,
)


def _value(value: Decimal | None) -> Decimal:
    return value if value is not None else Decimal("0")


def _single_threshold(
    settings: OnchainSettings, metadata: TokenMetadata
) -> Decimal:
    return max(
        settings.single_large_floor_usd,
        _value(metadata.historical_single_p99_usd),
        _value(metadata.volume_24h_usd) * settings.single_volume_ratio,
    )


def _window_threshold(
    settings: OnchainSettings,
    metadata: TokenMetadata,
    duration: int,
) -> Decimal:
    if duration == WINDOW_15M_SEC:
        floor = settings.batch_15m_floor_usd
        historical = _value(metadata.historical_15m_p99_usd)
        ratio = settings.batch_volume_ratio
    else:
        floor = settings.continuous_60m_floor_usd
        historical = _value(metadata.historical_60m_p99_usd)
        ratio = settings.continuous_volume_ratio
    robust_baseline = _value(metadata.historical_window_median_usd) + (
        settings.baseline_mad_multiplier
        * _value(metadata.historical_window_mad_usd)
    )
    return max(
        floor,
        historical,
        robust_baseline,
        _value(metadata.volume_24h_usd) * ratio,
    )


def detect_flows(
    flows: Iterable[ClassifiedFlow],
    windows: Iterable[FlowWindow],
    metadata: dict[tuple[int, str], TokenMetadata],
    settings: OnchainSettings,
) -> list[DetectedFlow]:
    detected: list[DetectedFlow] = []
    for flow in flows:
        token = metadata.get((flow.chain_id, flow.token_address))
        if (
            token is None
            or flow.flow_type not in DIRECTIONAL_FLOW_TYPES
            or flow.amount_usd is None
            or flow.price_status != "available"
            or flow.label_confidence < settings.min_label_confidence
        ):
            continue
        threshold = _single_threshold(settings, token)
        if flow.amount_usd < threshold:
            continue
        exchange = tuple([flow.exchange]) if flow.exchange else ()
        single = FlowWindow(
            window_key=f"event:{flow.event_id}",
            chain_id=flow.chain_id,
            token_address=flow.token_address,
            symbol=flow.symbol,
            direction=flow.flow_type,
            window_start=flow.block_time,
            window_end=flow.block_time,
            duration_sec=0,
            total_usd=flow.amount_usd,
            tx_count=1,
            distinct_counterparties=1,
            exchanges=exchange,
            active_15m_buckets=1,
            min_label_confidence=flow.label_confidence,
            algorithm_version=ALGORITHM_VERSION,
            threshold_version=THRESHOLD_VERSION,
        )
        detected.append(
            DetectedFlow(
                window=single,
                detection_types=("single_large",),
                threshold_usd=threshold,
                source_event_ids=(flow.event_id,),
            )
        )

    for window in windows:
        token = metadata.get((window.chain_id, window.token_address))
        if token is None:
            continue
        threshold = _window_threshold(settings, token, window.duration_sec)
        detection_types: list[str] = []
        if (
            window.duration_sec == WINDOW_15M_SEC
            and window.tx_count >= 5
            and window.distinct_counterparties >= 3
            and window.total_usd >= threshold
        ):
            detection_types.append("batch_flow")
        if window.duration_sec == WINDOW_60M_SEC:
            if (
                window.active_15m_buckets >= 3
                and window.tx_count >= 8
                and window.total_usd >= threshold
            ):
                detection_types.append("continuous_flow")
            if len(window.exchanges) >= 2 and window.total_usd >= threshold:
                detection_types.append("multi_exchange")
        if detection_types:
            detected.append(
                DetectedFlow(
                    window=window,
                    detection_types=tuple(detection_types),
                    threshold_usd=threshold,
                )
            )
    return sorted(
        detected,
        key=lambda item: (
            item.window.chain_id,
            item.window.token_address,
            item.window.window_start,
            item.window.duration_sec,
            item.detection_types,
        ),
    )


def detect_rolling_flows(
    snapshots: Iterable[RollingFlowSnapshot],
    metadata: dict[tuple[int, str], TokenMetadata],
    settings: OnchainSettings,
) -> list[DetectedRollingFlow]:
    detected: list[DetectedRollingFlow] = []
    for snapshot in snapshots:
        token = metadata.get((snapshot.chain_id, snapshot.token_address))
        if (
            token is None
            or token.metadata_status not in {"verified", "verified_erc20"}
            or snapshot.direction == "balanced"
            or snapshot.min_label_confidence < settings.min_label_confidence
            or snapshot.net_dominance < settings.net_dominance_min
        ):
            continue
        threshold = _window_threshold(
            settings, token, snapshot.duration_sec
        )
        directional_tx_count = (
            snapshot.inflow_tx_count
            if snapshot.direction == "inflow"
            else snapshot.outflow_tx_count
        )
        directional_counterparties = (
            snapshot.distinct_inbound_counterparties
            if snapshot.direction == "inflow"
            else snapshot.distinct_outbound_counterparties
        )
        detection_types: list[str] = []
        if (
            snapshot.duration_sec == WINDOW_15M_SEC
            and directional_tx_count >= 5
            and directional_counterparties >= 3
            and abs(snapshot.net_flow_usd) >= threshold
        ):
            detection_types.append("batch_flow")
        if (
            snapshot.duration_sec == WINDOW_60M_SEC
            and directional_tx_count >= 8
            and snapshot.active_15m_buckets >= 3
            and abs(snapshot.net_flow_usd) >= threshold
        ):
            detection_types.append("continuous_flow")
        if (
            snapshot.duration_sec == WINDOW_60M_SEC
            and len(snapshot.exchanges) >= 2
            and abs(snapshot.net_flow_usd) >= threshold
        ):
            detection_types.append("multi_exchange")
        if detection_types:
            detected.append(
                DetectedRollingFlow(
                    snapshot=snapshot,
                    detection_types=tuple(detection_types),
                    threshold_usd=threshold,
                )
            )
    return detected
