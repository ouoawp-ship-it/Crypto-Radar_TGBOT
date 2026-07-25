from __future__ import annotations

from dataclasses import dataclass

from .models import ClassifiedFlow, NormalizedTransfer, TokenMetadata


@dataclass(frozen=True)
class ArkhamEntitySnapshot:
    chain: str
    address: str
    entity_id: str
    entity_name: str
    entity_type: str
    label_name: str
    source: str
    first_seen: int
    last_seen: int


@dataclass(frozen=True)
class ArkhamRawEvent:
    transfer_id: str
    payload_json: str
    payload_hash: str
    immutable_fingerprint: str
    received_via: str
    received_at: int
    processed_status: str
    error_type: str = ""


@dataclass(frozen=True)
class ArkhamProcessedEvent:
    raw: ArkhamRawEvent
    transfer: NormalizedTransfer
    metadata: TokenMetadata
    flow: ClassifiedFlow
    entities: tuple[ArkhamEntitySnapshot, ...]
    timestamp_ms: int


@dataclass(frozen=True)
class ArkhamSyncState:
    stream_name: str
    last_timestamp_ms: int
    last_event_id: str
    last_success_at: int
    status: str
    query_upper_ms: int = 0
    backlog_remaining: int = 0
