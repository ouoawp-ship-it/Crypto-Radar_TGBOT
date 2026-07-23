from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Iterable

from .constants import (
    ALGORITHM_VERSION,
    DIRECTIONAL_FLOW_TYPES,
    THRESHOLD_VERSION,
    WINDOW_15M_SEC,
    WINDOW_60M_SEC,
)
from .models import ClassifiedFlow, FlowWindow


def _bucket(timestamp: int, duration: int) -> int:
    return timestamp - (timestamp % duration)


def build_windows(
    flows: Iterable[ClassifiedFlow],
    *,
    min_label_confidence: float,
) -> list[FlowWindow]:
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
