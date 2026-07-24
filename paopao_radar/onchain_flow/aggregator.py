from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from hashlib import sha256
from typing import Iterable, Mapping

from .constants import (
    ALGORITHM_VERSION,
    DIRECTIONAL_FLOW_TYPES,
    P3_1_ALGORITHM_VERSION,
    THRESHOLD_VERSION,
    WINDOW_15M_SEC,
    WINDOW_60M_SEC,
)
from .models import (
    ClassifiedFlow,
    FlowWindow,
    PriceQuote,
    RollingFlowSnapshot,
)


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
    quotes: Mapping[tuple[int, str], PriceQuote] | None = None,
) -> list[RollingFlowSnapshot]:
    flow_list = list(flows)
    snapshots: list[RollingFlowSnapshot] = []
    for duration in (WINDOW_15M_SEC, WINDOW_60M_SEC):
        minimum_time = evaluation_time - duration
        eligible = [
            flow
            for flow in flow_list
            if flow.flow_type in DIRECTIONAL_FLOW_TYPES
            and flow.amount is not None
            and flow.label_confidence >= min_label_confidence
            and minimum_time <= flow.block_time <= evaluation_time
        ]
        grouped: dict[tuple[int, str], list[ClassifiedFlow]] = defaultdict(list)
        for flow in eligible:
            grouped[(flow.chain_id, flow.token_address)].append(flow)
        for (chain_id, token_address), records in sorted(grouped.items()):
            quote = (
                quotes.get((chain_id, token_address))
                if quotes is not None
                else None
            )
            if quote is None and quotes is None:
                latest = max(
                    records, key=lambda flow: flow.price_observed_at
                )
                if (
                    latest.amount is not None
                    and latest.amount != 0
                    and latest.amount_usd is not None
                    and latest.price_observed_at > 0
                ):
                    quote = PriceQuote(
                        chain_id=chain_id,
                        token_address=token_address,
                        price_usd=latest.amount_usd / latest.amount,
                        volume_24h_usd=None,
                        source=latest.price_source,
                        observed_at=latest.price_observed_at,
                        market_observed_at=latest.price_observed_at,
                        fetched_at=latest.price_observed_at,
                    )
            if (
                quote is None
                or evaluation_time - quote.freshness_timestamp
                > price_max_age_sec
            ):
                continue
            inflows = [flow for flow in records if flow.flow_type == "inflow"]
            outflows = [flow for flow in records if flow.flow_type == "outflow"]
            gross_inflow = sum(
                (
                    (flow.amount or Decimal("0")) * quote.price_usd
                    for flow in inflows
                ),
                Decimal("0"),
            )
            gross_outflow = sum(
                (
                    (flow.amount or Decimal("0")) * quote.price_usd
                    for flow in outflows
                ),
                Decimal("0"),
            )
            net_flow = gross_inflow - gross_outflow
            directional = inflows if net_flow >= 0 else outflows
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
            inflow_exchanges = tuple(
                sorted(
                    {
                        flow.exchange
                        for flow in inflows
                        if flow.exchange is not None
                    }
                )
            )
            outflow_exchanges = tuple(
                sorted(
                    {
                        flow.exchange
                        for flow in outflows
                        if flow.exchange is not None
                    }
                )
            )
            exchanges = tuple(
                sorted(set(inflow_exchanges) | set(outflow_exchanges))
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
                    price_source=quote.source,
                    price_observed_at=quote.freshness_timestamp,
                    evaluation_block=evaluation_block,
                    algorithm_version=P3_1_ALGORITHM_VERSION,
                    inflow_exchanges=inflow_exchanges,
                    outflow_exchanges=outflow_exchanges,
                    valuation_price_usd=quote.price_usd,
                    price_market_observed_at=quote.freshness_timestamp,
                    price_fetched_at=quote.fetched_at,
                )
            )
    return snapshots
