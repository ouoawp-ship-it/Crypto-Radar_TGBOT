from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Mapping

from .arkham_models import (
    ArkhamEntitySnapshot,
    ArkhamProcessedEvent,
    ArkhamRawEvent,
)
from .models import ClassifiedFlow, NormalizedTransfer, TokenMetadata
from .token_policy import STABLECOIN, TokenPolicy


ARKHAM_CHAIN_IDS = {
    "ethereum": 1,
    "optimism": 10,
    "bsc": 56,
    "polygon": 137,
    "base": 8453,
    "arbitrum": 42161,
    "arbitrum_one": 42161,
    "avalanche": 43114,
    "linea": 59144,
    "scroll": 534352,
    "zksync_era": 324,
}


class ArkhamNormalizationError(ValueError):
    pass


@dataclass(frozen=True)
class ArkhamParty:
    address: str
    entity_id: str
    entity_name: str
    entity_type: str
    label_name: str

    @property
    def identified(self) -> bool:
        return bool(self.entity_id and self.entity_type)

    @property
    def is_cex(self) -> bool:
        return self.identified and self.entity_type == "cex"


def arkham_chain_id(chain: str) -> int:
    normalized = chain.strip().lower()
    if normalized in ARKHAM_CHAIN_IDS:
        return ARKHAM_CHAIN_IDS[normalized]
    digest = sha256(normalized.encode("utf-8")).hexdigest()
    return -int(digest[:7], 16) - 1


def _string(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, dict) else {}


def _entity(value: object) -> tuple[str, str, str]:
    data = _mapping(value)
    entity_id = _string(
        data.get("id")
        or data.get("entityId")
        or data.get("arkhamEntityId")
    )
    entity_name = _string(
        data.get("name")
        or data.get("entityName")
        or data.get("label")
    )
    entity_type = _string(
        data.get("type") or data.get("entityType")
    ).lower()
    return entity_id, entity_name, entity_type


def _label_name(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    data = _mapping(value)
    return _string(data.get("name") or data.get("label"))


def _party(value: object) -> ArkhamParty:
    data = _mapping(value)
    entity_id, entity_name, entity_type = _entity(
        data.get("arkhamEntity")
    )
    return ArkhamParty(
        address=_string(data.get("address")).lower(),
        entity_id=entity_id,
        entity_name=entity_name,
        entity_type=entity_type,
        label_name=_label_name(data.get("arkhamLabel")),
    )


def _positive_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return parsed


def _non_negative_decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or parsed < 0:
        return None
    return parsed


def _timestamp_ms(value: object) -> int:
    if isinstance(value, bool):
        raise ArkhamNormalizationError(
            "Arkham blockTimestamp is malformed"
        )
    if isinstance(value, (int, float)):
        parsed = int(value)
        return parsed if parsed >= 10**12 else parsed * 1000
    text = _string(value)
    if not text:
        raise ArkhamNormalizationError(
            "Arkham blockTimestamp is missing"
        )
    if text.isdigit():
        parsed = int(text)
        return parsed if parsed >= 10**12 else parsed * 1000
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ArkhamNormalizationError(
            "Arkham blockTimestamp is malformed"
        ) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


def _classification(
    from_party: ArkhamParty, to_party: ArkhamParty
) -> tuple[str, str | None, str | None, str]:
    if not from_party.identified or not to_party.identified:
        return "unidentified", None, None, "unlabeled"
    quality = "arkham_entity"
    if from_party.is_cex and to_party.is_cex:
        if from_party.entity_id == to_party.entity_id:
            return (
                "internal",
                from_party.entity_name or from_party.entity_id,
                to_party.entity_name or to_party.entity_id,
                quality,
            )
        return (
            "cross_cex",
            from_party.entity_name or from_party.entity_id,
            to_party.entity_name or to_party.entity_id,
            quality,
        )
    if not from_party.is_cex and to_party.is_cex:
        return (
            "inflow",
            None,
            to_party.entity_name or to_party.entity_id,
            quality,
        )
    if from_party.is_cex and not to_party.is_cex:
        return (
            "outflow",
            from_party.entity_name or from_party.entity_id,
            None,
            quality,
        )
    return "non_cex", None, None, quality


def _fallback_identity(
    transfer: Mapping[str, object],
    *,
    chain: str,
    tx_hash: str,
) -> str:
    discriminator = {
        "chain": chain,
        "transactionHash": tx_hash,
        "blockNumber": transfer.get("blockNumber"),
        "tokenAddress": transfer.get("tokenAddress"),
        "tokenId": transfer.get("tokenId"),
        "from": _mapping(transfer.get("fromAddress")).get("address"),
        "to": _mapping(transfer.get("toAddress")).get("address"),
        "unitValue": transfer.get("unitValue"),
        "historicalUSD": transfer.get("historicalUSD"),
    }
    digest = sha256(
        json.dumps(
            discriminator,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return f"fallback:{digest}"


def normalize_arkham_transfer(
    payload: Mapping[str, object],
    *,
    token_policy: TokenPolicy,
    received_at: int,
    received_via: str = "rest",
) -> ArkhamProcessedEvent:
    nested = payload.get("transfer")
    transfer = nested if isinstance(nested, dict) else payload
    chain = _string(transfer.get("chain")).lower()
    if not chain:
        raise ArkhamNormalizationError("Arkham chain is missing")
    timestamp_ms = _timestamp_ms(transfer.get("blockTimestamp"))
    block_time = timestamp_ms // 1000
    tx_hash = _string(transfer.get("transactionHash")).lower()
    transfer_id = _string(transfer.get("id"))
    if not transfer_id:
        transfer_id = _fallback_identity(
            transfer, chain=chain, tx_hash=tx_hash
        )
    event_id = f"arkham:{transfer_id}"
    if not tx_hash:
        tx_hash = f"arkham:{sha256(event_id.encode()).hexdigest()}"
    discriminator = int(
        sha256(event_id.encode("utf-8")).hexdigest()[:15], 16
    )
    from_party = _party(transfer.get("fromAddress"))
    to_party = _party(transfer.get("toAddress"))
    flow_type, exchange_from, exchange_to, quality = _classification(
        from_party, to_party
    )
    if quality == "unlabeled" and (
        from_party.label_name or to_party.label_name
    ):
        quality = "arkham_label"

    token_address = _string(transfer.get("tokenAddress")).lower()
    token_id = _string(transfer.get("tokenId")).lower()
    if token_address:
        token_identity = token_address
    elif token_id:
        token_identity = f"arkham-token:{token_id}"
    else:
        token_identity = (
            "arkham-token-unknown:"
            + sha256(f"{chain}:{event_id}".encode()).hexdigest()
        )
    policy = token_policy.classify(
        chain=chain,
        token_id=token_id,
        token_address=token_address,
    )
    signal_context = (
        "market_liquidity_context"
        if policy == STABLECOIN
        else "token_directional"
    )
    decimals_value = transfer.get("tokenDecimals")
    decimals = (
        int(decimals_value)
        if isinstance(decimals_value, int)
        and not isinstance(decimals_value, bool)
        and 0 <= decimals_value <= 36
        else None
    )
    unit_value = _non_negative_decimal(transfer.get("unitValue"))
    amount_raw = 0
    if unit_value is not None and decimals is not None:
        amount_raw = int(unit_value * (Decimal(10) ** decimals))
    historical_usd = _positive_decimal(
        transfer.get("historicalUSD")
    )
    symbol = (
        _string(transfer.get("tokenSymbol"))
        or token_id
        or "UNKNOWN"
    )
    name = _string(transfer.get("tokenName")) or symbol
    chain_id = arkham_chain_id(chain)
    normalized = NormalizedTransfer(
        event_id=event_id,
        chain_id=chain_id,
        chain_name=chain,
        block_number=(
            int(transfer.get("blockNumber"))
            if isinstance(transfer.get("blockNumber"), int)
            and not isinstance(transfer.get("blockNumber"), bool)
            else 0
        ),
        block_hash=_string(transfer.get("blockHash")).lower(),
        block_time=block_time,
        tx_hash=tx_hash,
        log_index=discriminator,
        token_address=token_identity,
        from_address=from_party.address,
        to_address=to_party.address,
        amount_raw=amount_raw,
        removed=False,
        confirmation_status="finalized",
        source="arkham",
    )
    counterparty = (
        from_party.address
        if flow_type == "inflow"
        else to_party.address
        if flow_type == "outflow"
        else to_party.address or from_party.address
    )
    flow = ClassifiedFlow(
        event_id=event_id,
        chain_id=chain_id,
        token_address=token_identity,
        symbol=symbol,
        block_time=block_time,
        flow_type=flow_type,
        exchange_from=exchange_from,
        exchange_to=exchange_to,
        counterparty_address=counterparty,
        amount=unit_value,
        amount_usd=historical_usd,
        label_confidence=0.0,
        price_status=(
            "available" if historical_usd is not None else "missing"
        ),
        block_number=normalized.block_number,
        block_hash=normalized.block_hash,
        price_source=(
            "arkham_historical_usd"
            if historical_usd is not None
            else ""
        ),
        price_observed_at=(
            block_time if historical_usd is not None else 0
        ),
        source="arkham",
        attribution_quality=quality,
        token_policy=policy,
        signal_context=signal_context,
    )
    metadata = TokenMetadata(
        chain_id=chain_id,
        token_address=token_identity,
        symbol=symbol,
        name=name,
        decimals=decimals,
        token_kind=policy,
        metadata_status="verified",
        updated_at=received_at,
        price_source=flow.price_source,
        price_observed_at=flow.price_observed_at,
    )
    entities = tuple(
        ArkhamEntitySnapshot(
            chain=chain,
            address=party.address,
            entity_id=party.entity_id,
            entity_name=party.entity_name,
            entity_type=party.entity_type,
            label_name=party.label_name,
            source="arkham",
            first_seen=block_time,
            last_seen=block_time,
        )
        for party in (from_party, to_party)
        if party.address
        and (party.entity_id or party.label_name or party.entity_type)
    )
    payload_json = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    if historical_usd is None:
        processed_status = "unpriced"
    elif flow_type not in {"inflow", "outflow"}:
        processed_status = "non_directional"
    elif policy not in {"normal_token", "stablecoin"}:
        processed_status = "policy_suppressed"
    else:
        processed_status = "processed"
    raw = ArkhamRawEvent(
        transfer_id=transfer_id,
        payload_json=payload_json,
        payload_hash=sha256(payload_json.encode("utf-8")).hexdigest(),
        received_via=received_via,
        received_at=received_at,
        processed_status=processed_status,
    )
    return ArkhamProcessedEvent(
        raw=raw,
        transfer=normalized,
        metadata=metadata,
        flow=flow,
        entities=entities,
        timestamp_ms=timestamp_ms,
    )
