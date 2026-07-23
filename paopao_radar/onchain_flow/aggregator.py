from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from hashlib import sha256
from typing import Iterable

from .constants import (
    ALGORITHM_VERSION,
    DIRECTIONAL_FLOW_TYPES,
    P3_1_ALGORITHM_VERSION,
    THRESHOLD_VERSION,
    WINDOW_15M_SEC,
    WINDOW_60M_SEC,
)
from .models import ClassifiedFlow, FlowWindow, RollingFlowSnapshot


def _bucket(timestamp: int, duration: int) -> int:
    return timestamp - (timestamp % duration)


def build_windows(
    flows: Iterable[ClassifiedFlow],
    *,
    min_label_confidence: float,
) -> list[FlowWindow]:
    # P3.0 aggregates gross directions independently. Net flow belongs to P3.1.
    eligible = [
        flow
        for flow in flows
        if flow.flow_type in DIRECTIONAL_FLOW_TYPES
        and flow.amount_usd is not None
        and flow.price_status == "available"
        and flow.label_confidence >= min_label_confidence
    ]
    grouped: dict[
        tuple[int, str, str, int, int], list[ClassifiedFlow]
    ] = defaultdict(list)
    for flow in eligible:
        for duration in (WINDOW_15M_SEC, WINDOW_60M_SEC):
            start = _bucket(flow.block_time, duration)
            grouped[
                (
                    flow.chain_id,
                    flow.token_address,
                    flow.flow_type,
                    start,
                    duration,
                )
            ].append(flow)

    windows: list[FlowWindow] = []
    for key, records in sorted(grouped.items()):
        chain_id, token_address, direction, start, duration = key
        exchanges = tuple(
            sorted({flow.exchange for flow in records if flow.exchange})
        )
        counterparties = {
            flow.counterparty_address for flow in records
        }
        active_buckets = {
            _bucket(flow.block_time, WINDOW_15M_SEC) for flow in records
        }
        total = sum(
            (flow.amount_usd or Decimal("0") for flow in records),
            Decimal("0"),
        )
        windows.append(
            FlowWindow(
                window_key=(
                    f"{chain_id}:{token_address}:{direction}:{start}:{duration}"
                ),
                chain_id=chain_id,
                token_address=token_address,
                symbol=records[0].symbol,
                direction=direction,
                window_start=start,
                window_end=start + duration,
                duration_sec=duration,
                total_usd=total,
                tx_count=len(records),
                distinct_counterparties=len(counterparties),
                exchanges=exchanges,
                active_15m_buckets=len(active_buckets),
                min_label_confidence=min(
                    flow.label_confidence for flow in records
                ),
                algorithm_version=ALGORITHM_VERSION,
                threshold_version=THRESHOLD_VERSION,
            )
        )
    return windows


def build_rolling_snapshots(
    flows: Iterable[ClassifiedFlow],
    *,
    evaluation_time: int,
    evaluation_block: int,
    min_label_confidence: float,
    price_max_age_sec: int,
) -> list[RollingFlowSnapshot]:
    flow_list = list(flows)
    snapshots: list[RollingFlowSnapshot] = []
    for duration in (WINDOW_15M_SEC, WINDOW_60M_SEC):
        minimum_time = evaluation_time - duration
        eligible = [
            flow
            for flow in flow_list
            if flow.flow_type in DIRECTIONAL_FLOW_TYPES
            and flow.amount_usd is not None
            and flow.price_status == "available"
            and flow.label_confidence >= min_label_confidence
            and minimum_time <= flow.block_time <= evaluation_time
            and flow.price_observed_at > 0
            and evaluation_time - flow.price_observed_at <= price_max_age_sec
        ]
        grouped: dict[tuple[int, str], list[ClassifiedFlow]] = defaultdict(list)
        for flow in eligible:
            grouped[(flow.chain_id, flow.token_address)].append(flow)
        for (chain_id, token_address), records in sorted(grouped.items()):
            inflows = [flow for flow in records if flow.flow_type == "inflow"]
            outflows = [flow for flow in records if flow.flow_type == "outflow"]
            gross_inflow = sum(
                (flow.amount_usd or Decimal("0") for flow in inflows),
                Decimal("0"),
            )
            gross_outflow = sum(
                (flow.amount_usd or Decimal("0") for flow in outflows),
                Decimal("0"),
            )
            net_flow = gross_inflow - gross_outflow
            directional = inflows if net_flow >= 0 else outflows
            latest_price = max(
                records, key=lambda flow: flow.price_observed_at
            )
            source_fingerprint = sha256(
                "|".join(
                    sorted(
                        (
                            f"{flow.event_id}:{flow.block_number}:"
                            f"{flow.block_hash}:{flow.block_time}"
                        )
                        for flow in records
                    )
                ).encode("utf-8")
            ).hexdigest()[:16]
            exchanges = tuple(
                sorted(
                    {
                        flow.exchange
                        for flow in records
                        if flow.exchange is not None
                    }
                )
            )
            snapshots.append(
                RollingFlowSnapshot(
                    snapshot_key=(
                        f"{chain_id}:{token_address}:{evaluation_time}:"
                        f"{duration}:{P3_1_ALGORITHM_VERSION}:"
                        f"{source_fingerprint}"
                    ),
                    chain_id=chain_id,
                    token_address=token_address,
                    symbol=records[0].symbol,
                    evaluation_time=evaluation_time,
                    duration_sec=duration,
                    gross_inflow_usd=gross_inflow,
                    gross_outflow_usd=gross_outflow,
                    net_flow_usd=net_flow,
                    inflow_tx_count=len(inflows),
                    outflow_tx_count=len(outflows),
                    distinct_inbound_counterparties=len(
                        {flow.counterparty_address for flow in inflows}
                    ),
                    distinct_outbound_counterparties=len(
                        {flow.counterparty_address for flow in outflows}
                    ),
                    exchanges=exchanges,
                    active_15m_buckets=len(
                        {
                            _bucket(flow.block_time, WINDOW_15M_SEC)
                            for flow in directional
                        }
                    ),
                    min_label_confidence=min(
                        flow.label_confidence for flow in records
                    ),
                    price_source=latest_price.price_source,
                    price_observed_at=latest_price.price_observed_at,
                    evaluation_block=evaluation_block,
                    algorithm_version=P3_1_ALGORITHM_VERSION,
                )
            )
    return snapshots
