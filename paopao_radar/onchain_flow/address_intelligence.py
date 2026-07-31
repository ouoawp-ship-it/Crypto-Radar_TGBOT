from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path
import time
from typing import Any, Callable, Iterable, Protocol, Sequence
from urllib.parse import urlsplit

import requests

from paopao_radar.atomic_json import (
    _atomic_write_text_unlocked,
    _file_lock,
    _read_json_unlocked,
    _write_json_unlocked,
)

from .arkham_intelligence import (
    ArkhamIntelligenceClient,
    ArkhamIntelligenceError,
)
from .config import OnchainSettings
from .label_candidates import candidate_from_arkham
from .labels import (
    AUDIT_COLUMNS,
    REQUIRED_COLUMNS,
    LabelValidationError,
    is_approved_label,
    load_labels_csv,
    normalize_evm_address,
)


ADDRESS_INTELLIGENCE_SCHEMA_VERSION = 1
BASE_CHAIN_ID = 8453
ZERO_ADDRESS = "0x" + ("0" * 40)
PROVIDER_NAMES = (
    "local_approved",
    "dune_cex",
    "dune_cex_deposit",
    "oli",
    "basescan_manual",
    "arkham_optional",
    "behavior_inference",
)
NETWORK_PROVIDER_NAMES = {
    "dune_cex",
    "dune_cex_deposit",
    "arkham_optional",
}
PRODUCTION_ROLES = {
    "deposit",
    "collector",
    "hot",
    "cold",
    "cex_wallet",
    "treasury",
    "bridge",
    "contract",
    "wallet",
}
GENERIC_ROLES = {"cex_wallet"}
ROLE_CONFLICTS = {
    frozenset(("hot", "cold")),
    frozenset(("deposit", "hot")),
    frozenset(("deposit", "cold")),
    frozenset(("collector", "cold")),
}
BEHAVIOR_ROLES = {
    "deposit_candidate",
    "collector_candidate",
    "hot_wallet_candidate",
    "fanout_candidate",
    "treasury_candidate",
    "bridge_candidate",
    "contract_candidate",
}
FINAL_STATUSES = {"approved", "rejected", "expired"}
CANDIDATE_STATUSES = {
    "pending",
    "approved",
    "rejected",
    "expired",
    "conflicted",
}


class AddressIntelligenceError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class AddressLabelProvider(Protocol):
    provider_name: str
    source_priority: int
    network_required: bool

    @property
    def configured(self) -> bool: ...

    def provider_check(self) -> dict[str, object]: ...

    def discover(
        self,
        addresses: Sequence[dict[str, object]],
    ) -> list[dict[str, object]]: ...


def _chmod_private(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass


def _bounded(value: object, limit: int = 160) -> str:
    if value is None or not isinstance(value, (str, int, float)):
        return ""
    return " ".join(str(value).replace("\x00", "").split())[:limit]


def _normalize_confidence(value: object, default: float) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        result = default
    return max(0.0, min(result, 1.0))


def _stable_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _candidate_id(value: dict[str, object]) -> str:
    return _stable_hash({
        "chain_id": value["chain_id"],
        "address": value["address"],
        "provider": value["provider"],
        "entity_name": _canonical_entity_name(value["entity_name"]),
        "entity_type": _canonical_entity_type(value["entity_type"]),
        "address_role": value["address_role"],
    })


def _canonical_entity_name(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _canonical_entity_type(value: object) -> str:
    normalized = " ".join(str(value or "").casefold().split())
    return "cex" if normalized in {"cex", "exchange"} else normalized


def _stable_source_ref(
    provider: str,
    address: str,
    entity_name: str,
    address_role: str,
) -> str:
    return (
        f"{provider}:base:{address}:"
        f"{_canonical_entity_name(entity_name)}:{address_role}"
    )


def build_candidate(
    *,
    chain_id: int,
    address: str,
    entity_name: str,
    entity_type: str,
    address_role: str,
    provider: str,
    source_ref: str,
    source_confidence: float,
    evidence_type: str,
    evidence: dict[str, object],
    observed_at: int,
    expires_at: int | None = None,
    status: str = "pending",
) -> dict[str, object]:
    if provider not in PROVIDER_NAMES:
        raise AddressIntelligenceError("label_provider_invalid")
    if status not in CANDIDATE_STATUSES:
        raise AddressIntelligenceError("label_candidate_status_invalid")
    try:
        normalized = normalize_evm_address(address)
    except LabelValidationError as exc:
        raise AddressIntelligenceError("label_candidate_address_invalid") from exc
    if int(chain_id) != BASE_CHAIN_ID:
        raise AddressIntelligenceError("label_candidate_chain_invalid")
    safe_evidence = {
        str(key)[:60]: _bounded(value, 160)
        for key, value in sorted(evidence.items())
        if isinstance(value, (str, int, float, bool)) or value is None
    }
    candidate: dict[str, object] = {
        "candidate_id": "",
        "chain_id": BASE_CHAIN_ID,
        "address": normalized,
        "entity_name": _bounded(entity_name),
        "entity_type": _bounded(entity_type, 80).lower(),
        "address_role": _bounded(address_role, 80).lower(),
        "provider": provider,
        "source_ref": _bounded(source_ref, 240),
        "source_confidence": _normalize_confidence(
            source_confidence, 0.0
        ),
        "evidence_type": _bounded(evidence_type, 100),
        "evidence_hash": _stable_hash(safe_evidence),
        "first_seen_at": int(observed_at),
        "last_seen_at": int(observed_at),
        "expires_at": (
            None if expires_at is None else int(expires_at)
        ),
        "status": status,
        "conflict_status": "none",
        "review_status": (
            "approved" if status == "approved" else "unreviewed"
        ),
        "reviewed_at": (
            int(observed_at) if status == "approved" else None
        ),
        "review_note": "",
        "evidence_source_count": 1,
        "corroborated": False,
        "corroborates_approved": False,
        "preferred_address_role": _bounded(address_role, 80).lower(),
        "approval_eligible": False,
        "approval_block_reason": "candidate_not_evaluated",
    }
    candidate["candidate_id"] = _candidate_id(candidate)
    return candidate


def _empty_store() -> dict[str, object]:
    return {
        "schema_version": ADDRESS_INTELLIGENCE_SCHEMA_VERSION,
        "unknown_addresses": [],
        "candidates": [],
    }


def _validate_store(value: object) -> dict[str, object]:
    if (
        not isinstance(value, dict)
        or value.get("schema_version")
        != ADDRESS_INTELLIGENCE_SCHEMA_VERSION
        or not isinstance(value.get("unknown_addresses"), list)
        or not isinstance(value.get("candidates"), list)
    ):
        raise AddressIntelligenceError("address_intelligence_store_invalid")
    return value


def _sort_unknown(item: dict[str, object]) -> tuple[object, ...]:
    try:
        amount = Decimal(str(item.get("cumulative_token_amount") or "0"))
    except (InvalidOperation, ValueError):
        amount = Decimal(0)
    return (
        -int(item.get("trigger_signal_count") or 0),
        -int(item.get("window_count") or 0),
        -amount,
        -int(item.get("associated_wallet_count") or 0),
        str(item.get("address") or ""),
    )


class AddressIntelligenceStore:
    def __init__(
        self,
        path: Path,
        approved_labels_path: Path | None = None,
    ):
        self.path = path
        self.approved_labels_path = approved_labels_path

    @classmethod
    def from_settings(
        cls, settings: OnchainSettings
    ) -> "AddressIntelligenceStore":
        return cls(
            settings.address_intelligence_path,
            approved_labels_path=settings.labels_path,
        )

    def _prepare(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _chmod_private(self.path.parent, 0o700)

    def _read_unlocked(self) -> dict[str, object]:
        return _validate_store(
            _read_json_unlocked(self.path, _empty_store())
        )

    def status(self, *, now: int | None = None) -> dict[str, object]:
        timestamp = int(time.time()) if now is None else int(now)
        self.expire(now=timestamp)
        with _file_lock(self.path):
            data = self._read_unlocked()
        candidates = [
            item for item in data["candidates"] if isinstance(item, dict)
        ]
        return {
            "status": "ok" if self.path.exists() else "not_initialized",
            "unknown_address_count": len(data["unknown_addresses"]),
            "pending_candidate_count": sum(
                item.get("status") == "pending" for item in candidates
            ),
            "conflicted_candidate_count": sum(
                item.get("status") == "conflicted" for item in candidates
            ),
            "approved_candidate_count": sum(
                item.get("status") == "approved" for item in candidates
            ),
            "network_activity": False,
        }

    def observe_complete_scan(
        self,
        payload: dict[str, object],
        *,
        observed_at: int | None = None,
    ) -> dict[str, int]:
        analysis = payload.get("analysis")
        analysis = analysis if isinstance(analysis, dict) else {}
        if not (
            payload.get("complete") is True
            and analysis.get("complete") is True
        ):
            return {"observed": 0, "created": 0, "updated": 0}
        transfers = payload.get("transfers")
        query = payload.get("query")
        query = query if isinstance(query, dict) else {}
        if not isinstance(transfers, list):
            return {"observed": 0, "created": 0, "updated": 0}
        try:
            contract = normalize_evm_address(
                str(query.get("contract") or "")
            )
        except LabelValidationError:
            return {"observed": 0, "created": 0, "updated": 0}
        timestamp = (
            int(time.time()) if observed_at is None else int(observed_at)
        )
        window_id = _stable_hash({
            "chain_id": BASE_CHAIN_ID,
            "contract": contract,
            "from_time": int(query.get("from_time") or 0),
            "to_time": int(query.get("to_time") or 0),
        })
        primary = analysis.get("primary_behavior")
        primary = primary if isinstance(primary, dict) else {}
        behavior_type = str(primary.get("type") or "")
        aggregates: dict[str, dict[str, object]] = {}
        excluded = {ZERO_ADDRESS, contract}
        for transfer in transfers:
            if not isinstance(transfer, dict):
                continue
            try:
                amount = Decimal(str(transfer.get("amount") or "0"))
            except (InvalidOperation, ValueError):
                amount = Decimal(0)
            endpoints: dict[str, dict[str, object]] = {}
            for side in ("from", "to"):
                endpoint = transfer.get(side)
                if not isinstance(endpoint, dict):
                    continue
                try:
                    address = normalize_evm_address(
                        str(endpoint.get("address") or "")
                    )
                except LabelValidationError:
                    continue
                endpoints[side] = {**endpoint, "address": address}
            for side, endpoint in endpoints.items():
                address = str(endpoint["address"])
                if (
                    address in excluded
                    or endpoint.get("known") is True
                ):
                    continue
                peer_side = "to" if side == "from" else "from"
                peer = endpoints.get(peer_side, {})
                entry = aggregates.setdefault(address, {
                    "occurrence_count": 0,
                    "amount": Decimal(0),
                    "peers": set(),
                    "known_cex_adjacent": False,
                    "bridge_adjacent": False,
                    "contract_observed": False,
                    "roles": set(),
                })
                entry["occurrence_count"] = (
                    int(entry["occurrence_count"]) + 1
                )
                entry["amount"] = Decimal(entry["amount"]) + amount
                if peer.get("address"):
                    cast_peers = entry["peers"]
                    assert isinstance(cast_peers, set)
                    cast_peers.add(str(peer["address"]))
                if (
                    peer.get("classification_eligible") is True
                    and str(peer.get("entity_type") or "").lower() == "cex"
                ):
                    entry["known_cex_adjacent"] = True
                    cast_roles = entry["roles"]
                    assert isinstance(cast_roles, set)
                    cast_roles.add(
                        "deposit_candidate"
                        if side == "from"
                        else "hot_wallet_candidate"
                    )
                if str(peer.get("entity_type") or "").lower() == "bridge":
                    entry["bridge_adjacent"] = True
                    cast_roles = entry["roles"]
                    assert isinstance(cast_roles, set)
                    cast_roles.add("bridge_candidate")
                if (
                    endpoint.get("is_contract") is True
                    or str(endpoint.get("address_type") or "").lower()
                    == "contract"
                ):
                    entry["contract_observed"] = True
                    cast_roles = entry["roles"]
                    assert isinstance(cast_roles, set)
                    cast_roles.add("contract_candidate")
                roles = entry["roles"]
                assert isinstance(roles, set)
                if behavior_type == "wallet_consolidation_candidate":
                    roles.add("collector_candidate")
                elif behavior_type == "fanout_candidate":
                    roles.add("fanout_candidate")
        if not aggregates:
            return {"observed": 0, "created": 0, "updated": 0}
        amount_ranked = sorted(
            aggregates,
            key=lambda address: (
                -Decimal(aggregates[address]["amount"]),
                address,
            ),
        )
        large_addresses = set(
            amount_ranked[: min(20, max(1, len(amount_ranked) // 5))]
        )
        aggregates = {
            address: value
            for address, value in aggregates.items()
            if (
                int(value["occurrence_count"]) >= 2
                or bool(value["known_cex_adjacent"])
                or bool(value["roles"])
                or address in large_addresses
            )
        }
        if not aggregates:
            return {"observed": 0, "created": 0, "updated": 0}

        self._prepare()
        created = 0
        updated = 0
        with _file_lock(self.path):
            data = self._read_unlocked()
            current = {
                str(item.get("address")): item
                for item in data["unknown_addresses"]
                if isinstance(item, dict) and item.get("address")
            }
            for address, aggregate in aggregates.items():
                item = current.get(address)
                was_new = item is None
                if item is None:
                    item = {
                        "chain_id": BASE_CHAIN_ID,
                        "address": address,
                        "trigger_signal_count": 0,
                        "window_count": 0,
                        "cumulative_token_amount": "0",
                        "associated_wallet_count": 0,
                        "associated_wallets": [],
                        "behavior_roles": [],
                        "known_cex_adjacent": False,
                        "bridge_adjacent": False,
                        "contract_observed": False,
                        "first_seen_at": timestamp,
                        "last_seen_at": timestamp,
                        "window_ids": [],
                        "query_status": "pending",
                    }
                    current[address] = item
                    created += 1
                windows = [
                    str(value)
                    for value in item.get("window_ids", [])
                    if isinstance(value, str)
                ]
                if window_id not in windows:
                    windows.append(window_id)
                    windows = windows[-20:]
                    item["window_ids"] = windows
                    item["window_count"] = len(windows)
                    item["trigger_signal_count"] = (
                        int(item.get("trigger_signal_count") or 0)
                        + int(aggregate["occurrence_count"])
                    )
                    total = Decimal(
                        str(item.get("cumulative_token_amount") or "0")
                    )
                    item["cumulative_token_amount"] = format(
                        total + Decimal(aggregate["amount"]), "f"
                    )
                    peers = set(
                        str(value)
                        for value in item.get("associated_wallets", [])
                    )
                    peers.update(aggregate["peers"])
                    item["associated_wallets"] = sorted(peers)[:50]
                    item["associated_wallet_count"] = len(peers)
                    roles = set(
                        str(value)
                        for value in item.get("behavior_roles", [])
                    )
                    roles.update(aggregate["roles"])
                    item["behavior_roles"] = sorted(roles)
                    item["known_cex_adjacent"] = bool(
                        item.get("known_cex_adjacent")
                        or aggregate["known_cex_adjacent"]
                    )
                    item["bridge_adjacent"] = bool(
                        item.get("bridge_adjacent")
                        or aggregate["bridge_adjacent"]
                    )
                    item["contract_observed"] = bool(
                        item.get("contract_observed")
                        or aggregate["contract_observed"]
                    )
                    item["last_seen_at"] = timestamp
                    if not was_new:
                        updated += 1
            data["unknown_addresses"] = sorted(
                current.values(), key=_sort_unknown
            )[:1000]
            _write_json_unlocked(self.path, data)
            _chmod_private(self.path, 0o600)
        return {
            "observed": len(aggregates),
            "created": created,
            "updated": updated,
        }

    def unknown_queue(
        self, *, limit: int = 50
    ) -> list[dict[str, object]]:
        with _file_lock(self.path):
            data = self._read_unlocked()
        return [
            dict(item)
            for item in sorted(
                (
                    item for item in data["unknown_addresses"]
                    if isinstance(item, dict)
                ),
                key=_sort_unknown,
            )[: max(0, min(int(limit), 100))]
        ]

    def merge_candidates(
        self,
        candidates: Sequence[dict[str, object]],
        *,
        now: int | None = None,
    ) -> dict[str, int]:
        timestamp = int(time.time()) if now is None else int(now)
        self._prepare()
        created = 0
        refreshed = 0
        with _file_lock(self.path):
            data = self._read_unlocked()
            current = {
                str(item.get("candidate_id")): item
                for item in data["candidates"]
                if isinstance(item, dict) and item.get("candidate_id")
            }
            for candidate in candidates:
                normalized = self._validate_candidate(candidate)
                candidate_id = str(normalized["candidate_id"])
                existing = current.get(candidate_id)
                if existing is None:
                    current[candidate_id] = normalized
                    created += 1
                elif existing.get("status") not in FINAL_STATUSES:
                    existing["last_seen_at"] = max(
                        int(existing.get("last_seen_at") or 0),
                        int(normalized["last_seen_at"]),
                    )
                    existing["expires_at"] = normalized.get("expires_at")
                    existing["evidence_hash"] = normalized["evidence_hash"]
                    existing["source_ref"] = normalized["source_ref"]
                    existing["source_confidence"] = normalized[
                        "source_confidence"
                    ]
                    existing["evidence_type"] = normalized["evidence_type"]
                    refreshed += 1
            values = list(current.values())
            self._expire_values(values, timestamp)
            anchors, anchor_error = self._approved_anchors(timestamp)
            self._mark_conflicts(
                values,
                approved_anchors=anchors,
                anchor_error=anchor_error,
            )
            data["candidates"] = sorted(
                values,
                key=lambda item: str(item.get("candidate_id") or ""),
            )[:5000]
            _write_json_unlocked(self.path, data)
            _chmod_private(self.path, 0o600)
        return {"created": created, "refreshed": refreshed}

    @staticmethod
    def _validate_candidate(
        candidate: dict[str, object]
    ) -> dict[str, object]:
        required = {
            "candidate_id",
            "chain_id",
            "address",
            "entity_name",
            "entity_type",
            "address_role",
            "provider",
            "source_ref",
            "source_confidence",
            "evidence_type",
            "evidence_hash",
            "first_seen_at",
            "last_seen_at",
            "expires_at",
            "status",
            "conflict_status",
            "review_status",
            "reviewed_at",
            "review_note",
        }
        if not required.issubset(candidate):
            raise AddressIntelligenceError("label_candidate_schema_invalid")
        value = dict(candidate)
        value.setdefault("evidence_source_count", 1)
        value.setdefault("corroborated", False)
        value.setdefault("corroborates_approved", False)
        value.setdefault(
            "preferred_address_role",
            str(value.get("address_role") or ""),
        )
        value.setdefault("approval_eligible", False)
        value.setdefault(
            "approval_block_reason", "candidate_not_evaluated"
        )
        if (
            int(value["chain_id"]) != BASE_CHAIN_ID
            or normalize_evm_address(str(value["address"]))
            != value["address"]
            or value["provider"] not in PROVIDER_NAMES
            or value["status"] not in CANDIDATE_STATUSES
            or value["candidate_id"] != _candidate_id(value)
            or not isinstance(value["evidence_hash"], str)
            or len(str(value["evidence_hash"])) != 64
        ):
            raise AddressIntelligenceError("label_candidate_schema_invalid")
        return value

    @staticmethod
    def _expire_values(
        candidates: list[dict[str, object]], now: int
    ) -> None:
        for item in candidates:
            expires_at = item.get("expires_at")
            if (
                item.get("status") in {"pending", "conflicted"}
                and expires_at is not None
                and int(expires_at) <= now
            ):
                item["status"] = "expired"
                item["review_status"] = "expired"
                item["conflict_status"] = "none"
                item["approval_eligible"] = False
                item["approval_block_reason"] = "candidate_expired"

    def _approved_anchors(
        self,
        now: int,
    ) -> tuple[dict[tuple[int, str], object], str | None]:
        if self.approved_labels_path is None:
            return {}, None
        if not self.approved_labels_path.exists():
            return {}, "approved_label_anchor_unavailable"
        try:
            labels = load_labels_csv(self.approved_labels_path)
        except (LabelValidationError, OSError):
            return {}, "approved_label_anchor_unavailable"
        return {
            (label.chain_id, label.address): label
            for label in labels
            if label.chain_id == BASE_CHAIN_ID
            and label.active_at(now)
            and is_approved_label(label)
        }, None

    @staticmethod
    def _mark_conflicts(
        candidates: list[dict[str, object]],
        *,
        approved_anchors: dict[tuple[int, str], object] | None = None,
        anchor_error: str | None = None,
    ) -> None:
        approved_anchors = approved_anchors or {}
        groups: dict[tuple[int, str], list[dict[str, object]]] = {}
        for item in candidates:
            if item.get("status") not in {"pending", "conflicted"}:
                continue
            item.setdefault("evidence_source_count", 1)
            item.setdefault("corroborated", False)
            item["corroborates_approved"] = False
            if item.get("review_status") in {
                "corroborates_approved",
                "role_refinement_candidate",
            }:
                item["review_status"] = "unreviewed"
            item.setdefault(
                "preferred_address_role",
                str(item.get("address_role") or ""),
            )
            item["approval_eligible"] = False
            item["approval_block_reason"] = "candidate_not_eligible"
            if not str(item.get("entity_name") or ""):
                item["conflict_status"] = "none"
                item["approval_block_reason"] = "identity_required"
                if item.get("status") == "conflicted":
                    item["status"] = "pending"
                continue
            groups.setdefault(
                (int(item["chain_id"]), str(item["address"])), []
            ).append(item)
        for key, items in groups.items():
            if anchor_error is not None:
                for item in items:
                    item["status"] = "conflicted"
                    item["conflict_status"] = anchor_error
                    item["approval_block_reason"] = anchor_error
                continue
            identity_groups: dict[
                tuple[str, str], list[dict[str, object]]
            ] = {}
            for item in items:
                identity_groups.setdefault((
                    _canonical_entity_name(item.get("entity_name")),
                    _canonical_entity_type(item.get("entity_type")),
                ), []).append(item)
            if len(identity_groups) > 1:
                for item in items:
                    item["conflict_status"] = "conflicted"
                    item["status"] = "conflicted"
                    item["approval_block_reason"] = "identity_conflict"
                continue
            identity_items = next(iter(identity_groups.values()))
            sources = {
                str(item.get("provider") or "")
                for item in identity_items
            }
            roles = {
                str(item.get("address_role") or "")
                for item in identity_items
            }
            specific_roles = roles - GENERIC_ROLES
            role_conflict = any(
                pair.issubset(specific_roles) for pair in ROLE_CONFLICTS
            )
            preferred_role = (
                next(iter(specific_roles))
                if len(specific_roles) == 1
                else (
                    "cex_wallet"
                    if not specific_roles
                    else sorted(specific_roles)[0]
                )
            )
            for item in identity_items:
                item["evidence_source_count"] = len(sources)
                item["corroborated"] = len(sources) > 1
                item["preferred_address_role"] = preferred_role
            if role_conflict:
                for item in identity_items:
                    item["conflict_status"] = "conflicted"
                    item["status"] = "conflicted"
                    item["approval_block_reason"] = "role_conflict"
                continue
            anchor = approved_anchors.get(key)
            if anchor is not None:
                anchor_name = _canonical_entity_name(
                    getattr(anchor, "entity_name", "")
                )
                anchor_type = _canonical_entity_type(
                    getattr(anchor, "entity_type", "")
                )
                anchor_role = _bounded(
                    getattr(anchor, "address_type", ""), 80
                ).lower()
                for item in identity_items:
                    if (
                        _canonical_entity_name(item.get("entity_name"))
                        != anchor_name
                        or _canonical_entity_type(item.get("entity_type"))
                        != anchor_type
                    ):
                        item["status"] = "conflicted"
                        item["conflict_status"] = (
                            "conflicted_with_approved"
                        )
                        item["approval_block_reason"] = (
                            "approved_identity_conflict"
                        )
                        continue
                    candidate_role = str(
                        item.get("address_role") or ""
                    )
                    if frozenset(
                        (anchor_role, candidate_role)
                    ) in ROLE_CONFLICTS:
                        item["status"] = "conflicted"
                        item["conflict_status"] = (
                            "conflicted_with_approved_role"
                        )
                        item["approval_block_reason"] = (
                            "approved_role_conflict"
                        )
                    elif (
                        candidate_role == anchor_role
                        or (
                            anchor_role not in GENERIC_ROLES
                            and candidate_role in GENERIC_ROLES
                        )
                    ):
                        item["status"] = "pending"
                        item["conflict_status"] = (
                            "corroborates_approved"
                        )
                        item["corroborates_approved"] = True
                        item["review_status"] = (
                            "corroborates_approved"
                        )
                        item["approval_block_reason"] = (
                            "approved_label_already_covers_role"
                        )
                    else:
                        item["status"] = "pending"
                        item["conflict_status"] = (
                            "role_refinement_candidate"
                        )
                        item["review_status"] = (
                            "role_refinement_candidate"
                        )
                        item["approval_block_reason"] = (
                            "approved_role_refinement_requires_revoke"
                        )
                continue
            for item in identity_items:
                item["conflict_status"] = "none"
                item["status"] = "pending"
                if (
                    specific_roles
                    and str(item.get("address_role") or "")
                    in GENERIC_ROLES
                ):
                    item["approval_block_reason"] = (
                        "more_specific_role_candidate_available"
                    )
                else:
                    item["approval_eligible"] = True
                    item["approval_block_reason"] = ""

    def expire(self, *, now: int | None = None) -> int:
        if not self.path.exists():
            return 0
        timestamp = int(time.time()) if now is None else int(now)
        with _file_lock(self.path):
            data = self._read_unlocked()
            values = [
                item for item in data["candidates"]
                if isinstance(item, dict)
            ]
            before = sum(item.get("status") == "expired" for item in values)
            self._expire_values(values, timestamp)
            changed = (
                sum(item.get("status") == "expired" for item in values)
                - before
            )
            if changed:
                _write_json_unlocked(self.path, data)
                _chmod_private(self.path, 0o600)
        return changed

    def list_candidates(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        self.expire()
        with _file_lock(self.path):
            data = self._read_unlocked()
        values = [
            dict(item)
            for item in data["candidates"]
            if isinstance(item, dict)
            and (status is None or item.get("status") == status)
        ]
        values.sort(key=lambda item: (
            -int(item.get("last_seen_at") or 0),
            str(item.get("candidate_id") or ""),
        ))
        return values[: max(0, min(int(limit), 500))]

    def reject(
        self,
        candidate_id: str,
        *,
        note: str = "manual_review",
        reviewed_at: int | None = None,
    ) -> dict[str, object]:
        return self._review(
            candidate_id,
            status="rejected",
            review_status="rejected",
            note=note,
            reviewed_at=reviewed_at,
        )

    def defer(
        self,
        candidate_id: str,
        *,
        note: str = "manual_defer",
        reviewed_at: int | None = None,
    ) -> dict[str, object]:
        return self._review(
            candidate_id,
            status="pending",
            review_status="deferred",
            note=note,
            reviewed_at=reviewed_at,
        )

    def _review(
        self,
        candidate_id: str,
        *,
        status: str,
        review_status: str,
        note: str,
        reviewed_at: int | None,
    ) -> dict[str, object]:
        timestamp = (
            int(time.time()) if reviewed_at is None else int(reviewed_at)
        )
        with _file_lock(self.path):
            data = self._read_unlocked()
            selected = next(
                (
                    item for item in data["candidates"]
                    if isinstance(item, dict)
                    and item.get("candidate_id") == candidate_id
                ),
                None,
            )
            if selected is None:
                raise AddressIntelligenceError("label_candidate_not_found")
            if selected.get("status") not in {"pending", "conflicted"}:
                raise AddressIntelligenceError(
                    "label_candidate_already_reviewed"
                )
            selected["status"] = status
            selected["review_status"] = review_status
            selected["reviewed_at"] = timestamp
            selected["review_note"] = _bounded(note, 200)
            anchors, anchor_error = self._approved_anchors(timestamp)
            self._mark_conflicts([
                item for item in data["candidates"]
                if isinstance(item, dict)
            ], approved_anchors=anchors, anchor_error=anchor_error)
            _write_json_unlocked(self.path, data)
            _chmod_private(self.path, 0o600)
            return dict(selected)

    def approve(
        self,
        candidate_id: str,
        *,
        labels_path: Path,
        reviewed_at: int | None = None,
    ) -> dict[str, object]:
        timestamp = (
            int(time.time()) if reviewed_at is None else int(reviewed_at)
        )
        if (
            labels_path.name == "cex_addresses.example.csv"
            or labels_path.is_symlink()
        ):
            raise AddressIntelligenceError("private_labels_file_required")
        self._prepare()
        with _file_lock(self.path):
            data = self._read_unlocked()
            values = [
                item
                for item in data["candidates"]
                if isinstance(item, dict)
            ]
            anchors, anchor_error = self._approved_anchors(timestamp)
            self._mark_conflicts(
                values,
                approved_anchors=anchors,
                anchor_error=anchor_error,
            )
            if anchor_error is not None:
                raise AddressIntelligenceError(anchor_error)
            selected = next(
                (
                    item
                    for item in values
                    if item.get("candidate_id") == candidate_id
                ),
                None,
            )
            if selected is None:
                raise AddressIntelligenceError("label_candidate_not_found")
            expires_at = selected.get("expires_at")
            if (
                expires_at is not None
                and int(expires_at) <= timestamp
            ):
                selected["status"] = "expired"
                selected["review_status"] = "expired"
                selected["conflict_status"] = "none"
                selected["approval_eligible"] = False
                selected["approval_block_reason"] = "candidate_expired"
                selected["reviewed_at"] = timestamp
                selected["review_note"] = "expired_before_approval"
                _write_json_unlocked(self.path, data)
                _chmod_private(self.path, 0o600)
                raise AddressIntelligenceError(
                    "label_candidate_expired"
                )
            if (
                selected.get("status") != "pending"
                or selected.get("conflict_status") != "none"
            ):
                raise AddressIntelligenceError(
                    "label_candidate_conflict_or_reviewed"
                )
            if selected.get("provider") == "behavior_inference":
                raise AddressIntelligenceError(
                    "behavior_candidate_not_production_identity"
                )
            if selected.get("approval_eligible") is not True:
                raise AddressIntelligenceError(
                    "label_candidate_not_preferred_role"
                )
            entity_name = _bounded(selected.get("entity_name"))
            entity_type = _bounded(
                selected.get("entity_type"), 80
            ).lower()
            address_role = _bounded(
                selected.get("address_role"), 80
            ).lower()
            if (
                not entity_name
                or not entity_type
                or address_role not in PRODUCTION_ROLES
                or float(selected.get("source_confidence") or 0) <= 0
            ):
                raise AddressIntelligenceError(
                    "label_candidate_not_production_eligible"
                )
            labels_path.parent.mkdir(parents=True, exist_ok=True)
            _chmod_private(labels_path.parent, 0o700)
            with _file_lock(labels_path):
                existed = labels_path.exists()
                if self.approved_labels_path is not None:
                    if not existed:
                        raise AddressIntelligenceError(
                            "approved_label_anchor_unavailable"
                        )
                    try:
                        load_labels_csv(labels_path)
                    except (LabelValidationError, OSError) as exc:
                        raise AddressIntelligenceError(
                            "approved_label_anchor_unavailable"
                        ) from exc
                original = labels_path.read_bytes() if existed else b""
                backup: Path | None = None
                if existed:
                    stamp = datetime.now(timezone.utc).strftime(
                        "%Y%m%dT%H%M%S%fZ"
                    )
                    backup = labels_path.with_name(
                        f"{labels_path.name}.bak.{stamp}"
                    )
                    backup.write_bytes(original)
                    _chmod_private(backup, 0o600)
                try:
                    rows = _read_label_rows(labels_path)
                    address = str(selected["address"])
                    if any(
                        int(row["chain_id"]) == BASE_CHAIN_ID
                        and normalize_evm_address(row["address"]) == address
                        for row in rows
                    ):
                        raise AddressIntelligenceError(
                            "production_label_duplicate"
                        )
                    rows.append({
                        "chain_id": str(BASE_CHAIN_ID),
                        "address": address,
                        "entity_name": entity_name,
                        "entity_type": (
                            "cex"
                            if entity_type in {"exchange", "cex"}
                            else entity_type
                        ),
                        "address_type": address_role,
                        "source": (
                            f"{selected['provider']}+manual_review"
                        ),
                        "confidence": str(
                            selected["source_confidence"]
                        ),
                        "valid_from": str(timestamp),
                        "valid_to": (
                            ""
                            if selected.get("expires_at") is None
                            else str(selected["expires_at"])
                        ),
                        "evidence_hash": str(
                            selected["evidence_hash"]
                        ),
                        "review_status": "approved",
                    })
                    _atomic_write_text_unlocked(
                        labels_path, _render_label_rows(rows)
                    )
                    _chmod_private(labels_path, 0o600)
                    load_labels_csv(labels_path)
                    selected["status"] = "approved"
                    selected["review_status"] = "approved"
                    selected["reviewed_at"] = timestamp
                    selected["review_note"] = "manual_review"
                    _write_json_unlocked(self.path, data)
                    _chmod_private(self.path, 0o600)
                except Exception:
                    if existed:
                        _atomic_write_text_unlocked(
                            labels_path,
                            original.decode("utf-8-sig"),
                        )
                        _chmod_private(labels_path, 0o600)
                    else:
                        labels_path.unlink(missing_ok=True)
                    raise
        return {
            "candidate_id": candidate_id,
            "candidate_status": "approved",
            "backup_created": backup is not None,
        }

    def revoke(
        self,
        candidate_id: str,
        *,
        labels_path: Path,
        reviewed_at: int | None = None,
    ) -> dict[str, object]:
        timestamp = (
            int(time.time()) if reviewed_at is None else int(reviewed_at)
        )
        with _file_lock(self.path):
            data = self._read_unlocked()
            selected = next(
                (
                    item for item in data["candidates"]
                    if isinstance(item, dict)
                    and item.get("candidate_id") == candidate_id
                ),
                None,
            )
            if selected is None:
                raise AddressIntelligenceError("label_candidate_not_found")
            if selected.get("status") != "approved":
                raise AddressIntelligenceError(
                    "label_candidate_not_approved"
                )
            with _file_lock(labels_path):
                original = labels_path.read_bytes()
                stamp = datetime.now(timezone.utc).strftime(
                    "%Y%m%dT%H%M%S%fZ"
                )
                backup = labels_path.with_name(
                    f"{labels_path.name}.bak.{stamp}"
                )
                backup.write_bytes(original)
                _chmod_private(backup, 0o600)
                try:
                    rows = _read_label_rows(labels_path)
                    kept = [
                        row for row in rows
                        if not (
                            int(row["chain_id"]) == BASE_CHAIN_ID
                            and normalize_evm_address(row["address"])
                            == selected["address"]
                            and row.get("evidence_hash", "")
                            == selected["evidence_hash"]
                        )
                    ]
                    if len(kept) == len(rows):
                        raise AddressIntelligenceError(
                            "production_label_not_found"
                        )
                    _atomic_write_text_unlocked(
                        labels_path, _render_label_rows(kept)
                    )
                    _chmod_private(labels_path, 0o600)
                    load_labels_csv(labels_path)
                except Exception:
                    _atomic_write_text_unlocked(
                        labels_path,
                        original.decode("utf-8-sig"),
                    )
                    _chmod_private(labels_path, 0o600)
                    raise
            selected["status"] = "expired"
            selected["review_status"] = "revoked"
            selected["reviewed_at"] = timestamp
            selected["review_note"] = "source_revoked"
            _write_json_unlocked(self.path, data)
            _chmod_private(self.path, 0o600)
        return {
            "candidate_id": candidate_id,
            "candidate_status": "expired",
            "backup_created": True,
        }


def _read_label_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    load_labels_csv(path)
    fields = list(REQUIRED_COLUMNS) + list(AUDIT_COLUMNS)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            {field: str(row.get(field) or "") for field in fields}
            for row in csv.DictReader(handle)
        ]


def _render_label_rows(rows: list[dict[str, str]]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=list(REQUIRED_COLUMNS) + list(AUDIT_COLUMNS),
        lineterminator="\n",
        extrasaction="ignore",
    )
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


class LocalApprovedProvider:
    provider_name = "local_approved"
    source_priority = 100
    network_required = False

    def __init__(self, labels_path: Path):
        self.labels_path = labels_path

    @property
    def configured(self) -> bool:
        if not self.labels_path.exists():
            return False
        try:
            return any(
                is_approved_label(label)
                and label.source.strip().lower() != "synthetic_fixture"
                for label in load_labels_csv(self.labels_path)
            )
        except (LabelValidationError, OSError):
            return False

    def provider_check(self) -> dict[str, object]:
        return {
            "provider": self.provider_name,
            "status": "ok" if self.configured else "not_configured",
            "configured": self.configured,
            "network_activity": False,
        }

    def discover(
        self,
        addresses: Sequence[dict[str, object]],
    ) -> list[dict[str, object]]:
        if not self.configured:
            return []
        wanted = {str(item.get("address") or "") for item in addresses}
        now = int(time.time())
        results = []
        for label in load_labels_csv(self.labels_path):
            if (
                label.address not in wanted
                or not label.active_at(now)
                or not is_approved_label(label)
                or label.source.strip().lower() == "synthetic_fixture"
            ):
                continue
            results.append(build_candidate(
                chain_id=label.chain_id,
                address=label.address,
                entity_name=label.entity_name,
                entity_type=label.entity_type,
                address_role=label.address_type,
                provider=self.provider_name,
                source_ref=label.source,
                source_confidence=label.confidence,
                evidence_type="local_approved_label",
                evidence={
                    "evidence_hash": label.evidence_hash,
                    "review_status": label.review_status,
                },
                observed_at=now,
                expires_at=label.valid_to,
                status="approved",
            ))
        return results


class BehaviorInferenceProvider:
    provider_name = "behavior_inference"
    source_priority = 10
    network_required = False
    configured = True

    def provider_check(self) -> dict[str, object]:
        return {
            "provider": self.provider_name,
            "status": "ok",
            "configured": True,
            "network_activity": False,
        }

    def discover(
        self,
        addresses: Sequence[dict[str, object]],
    ) -> list[dict[str, object]]:
        now = int(time.time())
        results: list[dict[str, object]] = []
        for item in addresses:
            roles = [
                str(role)
                for role in item.get("behavior_roles", [])
                if str(role) in BEHAVIOR_ROLES
            ]
            if not roles:
                occurrence = int(item.get("trigger_signal_count") or 0)
                windows = int(item.get("window_count") or 0)
                if occurrence >= 5 and windows >= 2:
                    roles = ["hot_wallet_candidate"]
            if (
                int(item.get("associated_wallet_count") or 0) >= 5
                and int(item.get("window_count") or 0) >= 2
                and not bool(item.get("known_cex_adjacent"))
            ):
                roles.append("treasury_candidate")
            if item.get("bridge_adjacent") is True:
                roles.append("bridge_candidate")
            if item.get("contract_observed") is True:
                roles.append("contract_candidate")
            for role in sorted(set(roles)):
                results.append(build_candidate(
                    chain_id=BASE_CHAIN_ID,
                    address=str(item["address"]),
                    entity_name="",
                    entity_type="",
                    address_role=role,
                    provider=self.provider_name,
                    source_ref="complete_oar_scan",
                    source_confidence=0.50,
                    evidence_type="deterministic_behavior_role",
                    evidence={
                        "role": role,
                        "trigger_signal_count": int(
                            item.get("trigger_signal_count") or 0
                        ),
                        "window_count": int(
                            item.get("window_count") or 0
                        ),
                        "associated_wallet_count": int(
                            item.get("associated_wallet_count") or 0
                        ),
                    },
                    observed_at=int(item.get("last_seen_at") or now),
                    expires_at=now + 30 * 86400,
                ))
        return results


class ManualCsvProvider:
    network_required = False

    def __init__(self, provider_name: str):
        if provider_name not in {
            "dune_cex",
            "dune_cex_deposit",
            "basescan_manual",
        }:
            raise AddressIntelligenceError("label_provider_invalid")
        self.provider_name = provider_name
        self.source_priority = {
            "dune_cex_deposit": 80,
            "dune_cex": 70,
            "basescan_manual": 60,
        }[provider_name]
        self.configured = True

    def provider_check(self) -> dict[str, object]:
        return {
            "provider": self.provider_name,
            "status": "manual_import",
            "configured": True,
            "network_activity": False,
        }

    def discover(
        self,
        addresses: Sequence[dict[str, object]],
    ) -> list[dict[str, object]]:
        del addresses
        return []

    def import_csv(
        self,
        path: Path,
        *,
        observed_at: int | None = None,
        row_limit: int = 5000,
    ) -> list[dict[str, object]]:
        timestamp = (
            int(time.time()) if observed_at is None else int(observed_at)
        )
        file_hash = _file_sha256(path)
        results: list[dict[str, object]] = []
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            for index, row in enumerate(reader, start=2):
                if index - 1 > row_limit:
                    raise AddressIntelligenceError(
                        "label_import_row_limit"
                    )
                candidate = self._candidate_from_csv_row(
                    row,
                    index=index,
                    file_hash=file_hash,
                    timestamp=timestamp,
                )
                if candidate is not None:
                    results.append(candidate)
        return results

    def _candidate_from_csv_row(
        self,
        row: dict[str, object],
        *,
        index: int,
        file_hash: str,
        timestamp: int,
    ) -> dict[str, object] | None:
        blockchain = _bounded(
            row.get("blockchain")
            or row.get("chain")
            or row.get("chain_id")
        ).lower()
        if blockchain not in {"base", "8453", "eip155:8453"}:
            return None
        try:
            address = normalize_evm_address(
                str(row.get("address") or "")
            )
        except LabelValidationError:
            return None
        entity_name = _bounded(
            row.get("cex_name")
            or row.get("entity_name")
            or row.get("name")
            or row.get("label")
        )
        if not entity_name:
            return None
        if self.provider_name == "basescan_manual":
            entity_type = _bounded(
                row.get("entity_type") or "entity"
            ).lower()
            role = _bounded(
                row.get("address_role")
                or row.get("address_type")
                or "wallet"
            ).lower()
            if entity_type in {"cex", "exchange"}:
                if not (
                    row.get("address_role") or row.get("address_type")
                ):
                    return None
                if not (row.get("source") or row.get("source_ref")):
                    return None
        else:
            entity_type = _bounded(
                row.get("entity_type") or "cex"
            ).lower()
            role = _bounded(
                row.get("address_role")
                or row.get("address_type")
                or (
                    "deposit"
                    if self.provider_name == "dune_cex_deposit"
                    else "cex_wallet"
                )
            ).lower()
        source_ref = _stable_source_ref(
            self.provider_name,
            address,
            entity_name,
            role,
        )
        confidence = _normalize_confidence(
            row.get("confidence"),
            0.95
            if self.provider_name == "dune_cex_deposit"
            else 0.90,
        )
        return build_candidate(
            chain_id=BASE_CHAIN_ID,
            address=address,
            entity_name=entity_name,
            entity_type=entity_type,
            address_role=role,
            provider=self.provider_name,
            source_ref=source_ref,
            source_confidence=confidence,
            evidence_type="manual_csv_exact_address",
            evidence={
                "file_hash": file_hash,
                "row": index,
                "provider": self.provider_name,
                "source": _bounded(
                    row.get("source") or row.get("source_ref"),
                    160,
                ),
            },
            observed_at=timestamp,
            expires_at=_optional_timestamp(row.get("expires_at")),
        )


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _optional_timestamp(value: object) -> int | None:
    text = _bounded(value)
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


class DuneAddressProvider:
    network_required = True

    def __init__(
        self,
        *,
        provider_name: str,
        api_key: str,
        base_url: str = "https://api.dune.com/api",
        timeout_sec: int = 15,
        max_retries: int = 1,
        max_requests: int = 10,
        poll_interval_sec: float = 4.0,
        execution_timeout_sec: int = 30,
        max_rows: int = 100,
        session: Any | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        if provider_name not in {"dune_cex", "dune_cex_deposit"}:
            raise AddressIntelligenceError("label_provider_invalid")
        self.provider_name = provider_name
        self.source_priority = (
            80 if provider_name == "dune_cex_deposit" else 70
        )
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.timeout_sec = int(timeout_sec)
        self.max_retries = max(0, min(int(max_retries), 2))
        if not 4 <= int(max_requests) <= 40:
            raise AddressIntelligenceError(
                "dune_max_requests_invalid"
            )
        if not 0.2 <= float(poll_interval_sec) <= 10:
            raise AddressIntelligenceError(
                "dune_poll_interval_invalid"
            )
        if not 5 <= int(execution_timeout_sec) <= 120:
            raise AddressIntelligenceError(
                "dune_execution_timeout_invalid"
            )
        self.max_requests = int(max_requests)
        self.poll_interval_sec = float(poll_interval_sec)
        self.execution_timeout_sec = int(execution_timeout_sec)
        if (
            (self.max_requests - 2) * self.poll_interval_sec
            < self.execution_timeout_sec
        ):
            raise AddressIntelligenceError(
                "dune_poll_budget_inconsistent"
            )
        self.max_rows = max(1, min(int(max_rows), 500))
        self.session = session or requests.Session()
        self.clock = clock
        self.monotonic = monotonic
        self.sleeper = sleeper
        self.request_count = 0

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def provider_check(self) -> dict[str, object]:
        if not self.configured:
            return {
                "provider": self.provider_name,
                "status": "optional_disabled",
                "configured": False,
                "network_activity": False,
                "request_count": 0,
            }
        self._execute_sql("SELECT 1 AS ok")
        return {
            "provider": self.provider_name,
            "status": "ok",
            "configured": True,
            "network_activity": True,
            "request_count": self.request_count,
        }

    def discover(
        self,
        addresses: Sequence[dict[str, object]],
    ) -> list[dict[str, object]]:
        if not self.configured:
            return []
        normalized = sorted({
            normalize_evm_address(str(item["address"]))
            for item in addresses
        })[: self.max_rows]
        if not normalized:
            return []
        varbinary_literals = ", ".join(normalized)
        if self.provider_name == "dune_cex_deposit":
            table = "cex.deposit_addresses"
            name_columns = "cex_name"
        else:
            table = "cex.addresses"
            name_columns = "cex_name"
        sql = (
            "SELECT blockchain, address, "
            f"{name_columns} FROM {table} "
            "WHERE blockchain = 'base' "
            f"AND address IN ({varbinary_literals}) "
            "ORDER BY address, cex_name "
            f"LIMIT {self.max_rows}"
        )
        rows = self._execute_sql(sql)
        now = int(self.clock())
        results = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            blockchain = _bounded(row.get("blockchain")).lower()
            if blockchain != "base":
                continue
            try:
                address = normalize_evm_address(
                    str(row.get("address") or "")
                )
            except LabelValidationError:
                continue
            if address not in normalized:
                continue
            name = _bounded(
                row.get("cex_name") or row.get("name")
            )
            if not name:
                continue
            results.append(build_candidate(
                chain_id=BASE_CHAIN_ID,
                address=address,
                entity_name=name,
                entity_type="cex",
                address_role=(
                    "deposit"
                    if self.provider_name == "dune_cex_deposit"
                    else "cex_wallet"
                ),
                provider=self.provider_name,
                source_ref=_stable_source_ref(
                    self.provider_name,
                    address,
                    name,
                    (
                        "deposit"
                        if self.provider_name == "dune_cex_deposit"
                        else "cex_wallet"
                    ),
                ),
                source_confidence=(
                    0.95
                    if self.provider_name == "dune_cex_deposit"
                    else 0.90
                ),
                evidence_type="dune_exact_address",
                evidence={
                    "table": table,
                    "address": address,
                    "entity_name": name,
                },
                observed_at=now,
                expires_at=now + 30 * 86400,
            ))
        return results

    def _execute_sql(self, sql: str) -> list[dict[str, object]]:
        response = self._request(
            "POST",
            "/v1/sql/execute",
            json={"sql": sql, "performance": "small"},
        )
        payload = self._json(response)
        execution_id = _bounded(
            payload.get("execution_id")
            if isinstance(payload, dict)
            else None,
            100,
        )
        if not execution_id:
            raise AddressIntelligenceError("dune_invalid_response")
        deadline = self.monotonic() + self.execution_timeout_sec
        while True:
            remaining = deadline - self.monotonic()
            if remaining <= 0:
                raise AddressIntelligenceError(
                    "dune_execution_timeout"
                )
            if self.request_count >= self.max_requests - 1:
                raise AddressIntelligenceError(
                    "dune_request_budget_exhausted"
                )
            self.sleeper(min(self.poll_interval_sec, remaining))
            if self.monotonic() >= deadline:
                raise AddressIntelligenceError(
                    "dune_execution_timeout"
                )
            status_response = self._request(
                "GET",
                f"/v1/execution/{execution_id}/status",
                _reserve_requests=1,
            )
            state = self._execution_state(
                self._json(status_response)
            )
            if state == "QUERY_STATE_COMPLETED":
                result_response = self._request(
                    "GET",
                    f"/v1/execution/{execution_id}/results",
                )
                rows = self._result_rows(self._json(result_response))
                if rows is None:
                    raise AddressIntelligenceError(
                        "dune_invalid_response"
                    )
                return rows[: self.max_rows]
            if state in {
                "QUERY_STATE_FAILED",
                "QUERY_STATE_CANCELLED",
                "FAILED",
                "CANCELLED",
            }:
                raise AddressIntelligenceError("dune_query_failed")
            if state not in {
                "QUERY_STATE_PENDING",
                "QUERY_STATE_EXECUTING",
                "PENDING",
                "EXECUTING",
            }:
                raise AddressIntelligenceError(
                    "dune_invalid_response"
                )

    @staticmethod
    def _execution_state(payload: dict[str, object]) -> str:
        execution = payload.get("execution")
        source = execution if isinstance(execution, dict) else payload
        state = _bounded(source.get("state"), 80).upper()
        if not state:
            raise AddressIntelligenceError("dune_invalid_response")
        return state

    def _request(self, method: str, path: str, **kwargs: object) -> Any:
        reserve_requests = int(kwargs.pop("_reserve_requests", 0))
        last_code = "dune_connection_failed"
        for attempt in range(self.max_retries + 1):
            if (
                self.request_count
                >= self.max_requests - reserve_requests
            ):
                raise AddressIntelligenceError(
                    "dune_request_budget_exhausted"
                )
            self.request_count += 1
            try:
                response = self.session.request(
                    method,
                    f"{self.base_url}{path}",
                    headers={
                        "X-Dune-API-Key": self.api_key,
                        "Content-Type": "application/json",
                    },
                    timeout=self.timeout_sec,
                    allow_redirects=False,
                    **kwargs,
                )
            except requests.Timeout as exc:
                last_code = "dune_timeout"
                if attempt >= self.max_retries:
                    raise AddressIntelligenceError(last_code) from exc
                continue
            except requests.RequestException as exc:
                last_code = "dune_connection_failed"
                if attempt >= self.max_retries:
                    raise AddressIntelligenceError(last_code) from exc
                continue
            status = int(response.status_code)
            if 200 <= status < 300:
                return response
            if status in {401, 403}:
                code = "dune_auth_failed"
            elif status == 402:
                code = "dune_credit_or_subscription_required"
            elif status == 429:
                code = "dune_rate_limited"
            elif status >= 500:
                code = "dune_provider_unavailable"
            else:
                code = "dune_http_error"
            last_code = code
            if (
                code not in {
                    "dune_rate_limited",
                    "dune_provider_unavailable",
                }
                or attempt >= self.max_retries
            ):
                raise AddressIntelligenceError(code)
        raise AddressIntelligenceError(last_code)

    @staticmethod
    def _json(response: Any) -> dict[str, object]:
        try:
            payload = response.json()
        except (TypeError, ValueError) as exc:
            raise AddressIntelligenceError("dune_invalid_response") from exc
        if not isinstance(payload, dict):
            raise AddressIntelligenceError("dune_invalid_response")
        return payload

    @staticmethod
    def _result_rows(
        payload: dict[str, object],
    ) -> list[dict[str, object]] | None:
        result = payload.get("result")
        result = result if isinstance(result, dict) else payload
        rows = result.get("rows")
        if rows is None:
            return None
        if not isinstance(rows, list):
            raise AddressIntelligenceError("dune_invalid_response")
        return [row for row in rows if isinstance(row, dict)]


class OliParquetProvider:
    provider_name = "oli"
    source_priority = 50
    network_required = False
    configured = True

    def provider_check(self) -> dict[str, object]:
        return {
            "provider": self.provider_name,
            "status": "manual_import",
            "configured": True,
            "network_activity": False,
        }

    def discover(
        self,
        addresses: Sequence[dict[str, object]],
    ) -> list[dict[str, object]]:
        del addresses
        return []

    def import_parquet(
        self,
        path: Path,
        *,
        row_reader: Callable[[Path], Iterable[dict[str, object]]] | None = None,
        observed_at: int | None = None,
        row_limit: int = 10000,
    ) -> list[dict[str, object]]:
        if row_reader is None:
            row_reader = self._pyarrow_rows
        timestamp = (
            int(time.time()) if observed_at is None else int(observed_at)
        )
        results: list[dict[str, object]] = []
        count = 0
        for row in row_reader(path):
            count += 1
            if count > row_limit:
                raise AddressIntelligenceError("label_import_row_limit")
            chain = _bounded(
                row.get("chain_id")
                or row.get("chain")
                or row.get("caip2")
            ).lower()
            if chain not in {"8453", "base", "eip155:8453"}:
                continue
            try:
                address = normalize_evm_address(
                    str(
                        row.get("address")
                        or row.get("account")
                        or row.get("subject")
                        or ""
                    )
                )
            except LabelValidationError:
                continue
            entity_name = _bounded(
                row.get("entity_name")
                or row.get("name")
                or row.get("label")
                or row.get("tag_value")
            )
            raw_type = _bounded(
                row.get("entity_type")
                or row.get("category")
                or row.get("tag_id")
                or row.get("tag")
            ).lower()
            explicit_cex = any(
                token in raw_type.replace("-", "_").split("_")
                for token in ("cex", "exchange")
            )
            entity_type = "cex" if explicit_cex else (
                raw_type or "protocol"
            )
            role = _bounded(
                row.get("address_role")
                or row.get("address_type")
                or ("cex_wallet" if explicit_cex else "contract")
            ).lower()
            attester = _bounded(
                row.get("attester")
                or row.get("attester_name")
                or row.get("source"),
                160,
            )
            if not entity_name or not attester:
                continue
            confidence = _normalize_confidence(
                row.get("confidence")
                or row.get("source_confidence"),
                0.70,
            )
            results.append(build_candidate(
                chain_id=BASE_CHAIN_ID,
                address=address,
                entity_name=entity_name,
                entity_type=entity_type,
                address_role=role,
                provider=self.provider_name,
                source_ref=attester,
                source_confidence=confidence,
                evidence_type=(
                    "oli_attested_cex_label"
                    if explicit_cex
                    else "oli_attested_entity_label"
                ),
                evidence={
                    "attester": attester,
                    "category": raw_type,
                    "entity_name": entity_name,
                },
                observed_at=timestamp,
                expires_at=_optional_timestamp(row.get("expires_at")),
            ))
        return results

    @staticmethod
    def _pyarrow_rows(path: Path) -> Iterable[dict[str, object]]:
        try:
            import pyarrow.parquet as parquet  # type: ignore[import-not-found]
        except ImportError as exc:
            raise AddressIntelligenceError(
                "oli_parquet_dependency_missing"
            ) from exc
        parquet_file = parquet.ParquetFile(path)
        for batch in parquet_file.iter_batches(batch_size=512):
            for row in batch.to_pylist():
                if isinstance(row, dict):
                    yield row


class ArkhamOptionalProvider:
    provider_name = "arkham_optional"
    source_priority = 40
    network_required = True

    def __init__(
        self,
        settings: OnchainSettings,
        *,
        client_factory: Callable[..., ArkhamIntelligenceClient] = (
            ArkhamIntelligenceClient
        ),
        clock: Callable[[], float] = time.time,
    ):
        self.settings = settings
        self.client_factory = client_factory
        self.clock = clock
        self.request_count = 0

    @property
    def configured(self) -> bool:
        return bool(self.settings.arkham_api_key)

    def _client(self) -> ArkhamIntelligenceClient:
        return self.client_factory(
            base_url=self.settings.arkham_api_base_url,
            api_key=self.settings.arkham_api_key,
            timeout_sec=self.settings.arkham_api_timeout_sec,
            max_retries=self.settings.arkham_api_max_retries,
        )

    def provider_check(self) -> dict[str, object]:
        if not self.configured:
            return {
                "provider": self.provider_name,
                "status": "optional_disabled",
                "configured": False,
                "network_activity": False,
                "request_count": 0,
            }
        client = self._client()
        result = client.provider_check()
        self.request_count = client.request_count
        return {
            "provider": self.provider_name,
            "status": result.get("status", "ok"),
            "configured": True,
            "network_activity": True,
            "request_count": result.get("arkham_request_count", 0),
        }

    def discover(
        self,
        addresses: Sequence[dict[str, object]],
    ) -> list[dict[str, object]]:
        if not self.configured:
            return []
        normalized = [
            normalize_evm_address(str(item["address"]))
            for item in addresses
        ][: self.settings.oar_label_candidate_max_addresses]
        client = self._client()
        payloads = client.address_intelligence(normalized)
        self.request_count = client.request_count
        now = int(self.clock())
        results: list[dict[str, object]] = []
        for address in normalized:
            legacy = candidate_from_arkham(
                payloads.get(address),
                expected_address=address,
                observed_at=now,
            )
            if legacy is None:
                continue
            results.append(build_candidate(
                chain_id=BASE_CHAIN_ID,
                address=address,
                entity_name=str(legacy["provider_entity_name"]),
                entity_type=str(legacy["provider_entity_type"]),
                address_role=str(legacy["proposed_address_type"]),
                provider=self.provider_name,
                source_ref=str(
                    legacy.get("provider_entity_id") or "arkham_exact"
                ),
                source_confidence=0.95,
                evidence_type=str(legacy["evidence_type"]),
                evidence={
                    "legacy_evidence_hash": legacy["evidence_hash"],
                    "provider_entity_id": legacy["provider_entity_id"],
                    "address": address,
                },
                observed_at=now,
                expires_at=now + 30 * 86400,
            ))
        return results


def validate_optional_provider_url(value: str, name: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.query)
        or bool(parsed.fragment)
    ):
        raise AddressIntelligenceError(
            f"{name.lower()}_base_url_invalid"
        )


class AddressIntelligenceService:
    def __init__(
        self,
        settings: OnchainSettings,
        *,
        providers: Sequence[AddressLabelProvider] | None = None,
        store: AddressIntelligenceStore | None = None,
    ):
        self.settings = settings
        self.store = store or AddressIntelligenceStore.from_settings(
            settings
        )
        self.providers = list(
            providers
            if providers is not None
            else self._default_providers()
        )

    def _default_providers(self) -> list[AddressLabelProvider]:
        return [
            LocalApprovedProvider(self.settings.labels_path),
            DuneAddressProvider(
                provider_name="dune_cex",
                api_key=self.settings.dune_api_key,
                base_url=self.settings.dune_api_base_url,
                timeout_sec=self.settings.dune_api_timeout_sec,
                max_retries=self.settings.dune_api_max_retries,
                max_requests=self.settings.dune_api_max_requests,
                poll_interval_sec=float(
                    self.settings.dune_api_poll_interval_sec
                ),
                execution_timeout_sec=(
                    self.settings.dune_api_execution_timeout_sec
                ),
                max_rows=self.settings.dune_api_max_rows,
            ),
            DuneAddressProvider(
                provider_name="dune_cex_deposit",
                api_key=self.settings.dune_api_key,
                base_url=self.settings.dune_api_base_url,
                timeout_sec=self.settings.dune_api_timeout_sec,
                max_retries=self.settings.dune_api_max_retries,
                max_requests=self.settings.dune_api_max_requests,
                poll_interval_sec=float(
                    self.settings.dune_api_poll_interval_sec
                ),
                execution_timeout_sec=(
                    self.settings.dune_api_execution_timeout_sec
                ),
                max_rows=self.settings.dune_api_max_rows,
            ),
            OliParquetProvider(),
            ManualCsvProvider("basescan_manual"),
            ArkhamOptionalProvider(self.settings),
            BehaviorInferenceProvider(),
        ]

    def provider_status(self) -> dict[str, object]:
        statuses = []
        for provider in self.providers:
            status = "configured" if provider.configured else (
                "not_configured"
                if provider.provider_name == "local_approved"
                else "optional_disabled"
            )
            statuses.append({
                "provider": provider.provider_name,
                "status": status,
                "configured": bool(provider.configured),
                "network_required": provider.network_required,
                "source_priority": provider.source_priority,
            })
        return {
            "status": "ok",
            "providers": statuses,
            "network_activity": False,
            "arkham_required": False,
            "core_available": True,
        }

    def discover(
        self,
        *,
        provider_names: Sequence[str],
        allow_network: bool,
        limit: int,
    ) -> dict[str, object]:
        if not 1 <= int(limit) <= min(
            100, self.settings.oar_label_candidate_max_addresses
        ):
            raise AddressIntelligenceError(
                "candidate_address_limit_invalid"
            )
        requested = set(provider_names)
        if "all" in requested:
            requested = {
                provider.provider_name for provider in self.providers
                if provider.provider_name != "local_approved"
            }
        unknown = self.store.unknown_queue(limit=limit)
        if not unknown:
            return {
                "status": "ok",
                "addresses_examined": 0,
                "candidates_found": 0,
                "created": 0,
                "refreshed": 0,
                "providers": [],
                "provider_request_count": 0,
                "network_activity": False,
                "core_services_affected": False,
                "telegram_calls": 0,
                "ai_calls": 0,
            }
        all_candidates: list[dict[str, object]] = []
        statuses: list[dict[str, object]] = []
        network_calls = 0
        for provider in sorted(
            (
                item for item in self.providers
                if item.provider_name in requested
            ),
            key=lambda item: (-item.source_priority, item.provider_name),
        ):
            if not provider.configured:
                statuses.append({
                    "provider": provider.provider_name,
                    "status": "optional_disabled",
                    "candidates": 0,
                })
                continue
            if provider.network_required and not allow_network:
                statuses.append({
                    "provider": provider.provider_name,
                    "status": "allow_network_required",
                    "candidates": 0,
                })
                continue
            try:
                discovered = provider.discover(unknown)
                all_candidates.extend(discovered)
                network_calls += int(
                    getattr(provider, "request_count", 0)
                )
                statuses.append({
                    "provider": provider.provider_name,
                    "status": "ok",
                    "candidates": len(discovered),
                })
            except (AddressIntelligenceError, ArkhamIntelligenceError) as exc:
                statuses.append({
                    "provider": provider.provider_name,
                    "status": "failed",
                    "error": getattr(exc, "code", "provider_failed"),
                    "candidates": 0,
                })
        merged = self.store.merge_candidates(all_candidates)
        return {
            "status": "ok",
            "addresses_examined": len(unknown),
            "candidates_found": len(all_candidates),
            "created": merged["created"],
            "refreshed": merged["refreshed"],
            "providers": statuses,
            "provider_request_count": network_calls,
            "network_activity": bool(network_calls),
            "core_services_affected": False,
            "telegram_calls": 0,
            "ai_calls": 0,
        }


__all__ = [
    "ADDRESS_INTELLIGENCE_SCHEMA_VERSION",
    "AddressIntelligenceError",
    "AddressIntelligenceService",
    "AddressIntelligenceStore",
    "AddressLabelProvider",
    "ArkhamOptionalProvider",
    "BehaviorInferenceProvider",
    "DuneAddressProvider",
    "LocalApprovedProvider",
    "ManualCsvProvider",
    "OliParquetProvider",
    "PROVIDER_NAMES",
    "build_candidate",
    "validate_optional_provider_url",
]
