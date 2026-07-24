from __future__ import annotations

from decimal import Decimal

from .constants import ZERO_ADDRESS
from .labels import LabelRegistry
from .models import ClassifiedFlow, NormalizedTransfer, TokenMetadata


def _is_cex(label: object) -> bool:
    return bool(label is not None and getattr(label, "entity_type", "") == "cex")


def classify_transfer(
    transfer: NormalizedTransfer,
    metadata: TokenMetadata | None,
    registry: LabelRegistry,
) -> ClassifiedFlow:
    from_label = registry.lookup(
        transfer.chain_id, transfer.from_address, transfer.block_time
    )
    to_label = registry.lookup(
        transfer.chain_id, transfer.to_address, transfer.block_time
    )
    from_cex = _is_cex(from_label)
    to_cex = _is_cex(to_label)

    if transfer.from_address == ZERO_ADDRESS:
        flow_type = "mint"
    elif transfer.to_address == ZERO_ADDRESS:
        flow_type = "burn"
    elif (
        from_cex
        and to_cex
        and from_label is not None
        and to_label is not None
        and from_label.entity_name == to_label.entity_name
        and from_label.address_type == "deposit"
        and to_label.address_type in {"hot", "collector"}
    ):
        flow_type = "consolidation"
    elif (
        from_cex
        and to_cex
        and from_label is not None
        and to_label is not None
        and from_label.entity_name == to_label.entity_name
    ):
        flow_type = "internal"
    elif from_cex and to_cex:
        flow_type = "cross_cex"
    elif to_cex:
        flow_type = "inflow"
    elif from_cex:
        flow_type = "outflow"
    else:
        flow_type = "non_cex"

    labels = [label for label in (from_label, to_label) if label is not None]
    label_confidence = min(
        (label.confidence for label in labels),
        default=0.0,
    )
    amount: Decimal | None = None
    amount_usd: Decimal | None = None
    price_status = "missing"
    symbol = "UNKNOWN"
    if metadata is not None:
        symbol = metadata.symbol
        metadata_usable = (
            metadata.token_kind == "erc20"
            and metadata.metadata_status in {"verified", "verified_erc20"}
            and metadata.decimals is not None
        )
        if metadata_usable:
            amount = Decimal(transfer.amount_raw) / (
                Decimal(10) ** int(metadata.decimals)
            )
            if metadata.price_usd is not None:
                amount_usd = amount * metadata.price_usd
                price_status = "available"
            else:
                price_status = "missing"
        else:
            price_status = "metadata_missing"

    exchange_from = from_label.entity_name if from_cex and from_label else None
    exchange_to = to_label.entity_name if to_cex and to_label else None
    if flow_type == "inflow":
        counterparty = transfer.from_address
    elif flow_type == "outflow":
        counterparty = transfer.to_address
    else:
        counterparty = transfer.to_address

    return ClassifiedFlow(
        event_id=transfer.event_id,
        chain_id=transfer.chain_id,
        token_address=transfer.token_address,
        symbol=symbol,
        block_time=transfer.block_time,
        flow_type=flow_type,
        exchange_from=exchange_from,
        exchange_to=exchange_to,
        counterparty_address=counterparty,
        amount=amount,
        amount_usd=amount_usd,
        label_confidence=label_confidence,
        price_status=price_status,
        block_number=transfer.block_number,
        block_hash=transfer.block_hash,
        price_source=metadata.price_source if metadata is not None else "",
        price_observed_at=(
            metadata.price_observed_at if metadata is not None else 0
        ),
    )
