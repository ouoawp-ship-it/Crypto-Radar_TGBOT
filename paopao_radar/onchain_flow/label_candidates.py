from __future__ import annotations

import csv
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import io
import json
from pathlib import Path
import time
from typing import Any, Callable

from paopao_radar.atomic_json import (
    _atomic_write_text_unlocked,
    _file_lock,
    _write_json_unlocked,
)

from .arkham_intelligence import (
    ArkhamIntelligenceClient,
    ArkhamIntelligenceError,
)
from .config import OnchainSettings
from .labels import (
    REQUIRED_COLUMNS,
    LabelValidationError,
    load_labels_csv,
    normalize_evm_address,
    validate_live_labels,
)
from .token_activity import TokenActivityQuery, TokenActivityQueryService


LABEL_CANDIDATE_SCHEMA_VERSION = 1
ZERO_ADDRESS = "0x" + ("0" * 40)
_CEX_ENTITY_TYPES = {"cex", "exchange"}
_ROLE_WORDS = (
    ("deposit", "deposit"),
    ("collector", "collector"),
    ("hot", "hot"),
    ("cold", "cold"),
)


class LabelCandidateError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _bounded_text(value: object, *, limit: int = 200) -> str:
    if not isinstance(value, (str, int)):
        return ""
    return " ".join(str(value).replace("\x00", "").split())[:limit]


def _is_base(value: object) -> bool:
    return isinstance(value, str) and value.strip().lower() == "base"


def _explicit_cex_tag(payload: dict[str, object]) -> bool:
    values: list[str] = []
    label = payload.get("arkhamLabel")
    if isinstance(label, dict):
        values.append(_bounded_text(label.get("name")).lower())
    for key in ("tags", "populatedTags"):
        raw = payload.get(key)
        if not isinstance(raw, list):
            continue
        for item in raw:
            if isinstance(item, dict):
                values.extend(
                    _bounded_text(item.get(field)).lower()
                    for field in ("name", "label", "type")
                )
            else:
                values.append(_bounded_text(item).lower())
    tokens = {
        token
        for value in values
        for token in value.replace("-", " ").replace("_", " ").split()
    }
    return bool(tokens & {"cex", "exchange"})


def _address_type(payload: dict[str, object]) -> str:
    label = payload.get("arkhamLabel")
    label_text = (
        _bounded_text(label.get("name")).lower()
        if isinstance(label, dict)
        else ""
    )
    for word, role in _ROLE_WORDS:
        if word in label_text:
            return role
    return "cex_wallet"


def _candidate_id(candidate: dict[str, object]) -> str:
    stable = {
        "chain_id": candidate["chain_id"],
        "address": candidate["address"],
        "provider": candidate["provider"],
        "provider_entity_id": candidate["provider_entity_id"],
        "evidence_type": candidate["evidence_type"],
    }
    encoded = json.dumps(
        stable,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _evidence_hash(candidate: dict[str, object]) -> str:
    evidence = {
        "address": candidate["address"],
        "chain": "base",
        "entity_id": candidate["provider_entity_id"],
        "entity_name": candidate["provider_entity_name"],
        "entity_type": candidate["provider_entity_type"],
        "label": candidate["provider_label"],
        "deposit_exchange_id_present": candidate[
            "deposit_exchange_id_present"
        ],
        "address_type": candidate["proposed_address_type"],
        "evidence_type": candidate["evidence_type"],
    }
    return hashlib.sha256(
        json.dumps(
            evidence,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _validate_persisted_candidate(candidate: dict[str, object]) -> None:
    try:
        address = normalize_evm_address(str(candidate["address"]))
        chain_id = int(candidate["chain_id"])
        entity_name = _bounded_text(candidate["provider_entity_name"])
        entity_type = _bounded_text(
            candidate["provider_entity_type"]
        ).lower()
        provider_label = _bounded_text(candidate["provider_label"]).lower()
        address_type = str(candidate["proposed_address_type"])
        evidence_type = str(candidate["evidence_type"])
    except (KeyError, TypeError, ValueError, LabelValidationError) as exc:
        raise LabelCandidateError(
            "candidate_evidence_insufficient"
        ) from exc
    if (
        address != candidate["address"]
        or chain_id != 8453
        or candidate.get("provider") != "arkham"
        or not entity_name
        or candidate.get("candidate_id") != _candidate_id(candidate)
        or candidate.get("evidence_hash") != _evidence_hash(candidate)
    ):
        raise LabelCandidateError("candidate_evidence_insufficient")
    if evidence_type == "exact_deposit_exchange_id":
        valid = (
            candidate.get("deposit_exchange_id_present") is True
            and address_type == "deposit"
        )
    elif evidence_type == "exact_cex_entity":
        label_tokens = set(
            provider_label.replace("-", " ").replace("_", " ").split()
        )
        valid = (
            entity_type in _CEX_ENTITY_TYPES
            and (
                candidate.get("service") is True
                or bool(label_tokens & {"cex", "exchange"})
            )
            and address_type
            in {"hot", "cold", "deposit", "collector", "cex_wallet"}
        )
    else:
        valid = False
    if not valid:
        raise LabelCandidateError("candidate_evidence_insufficient")


def candidate_from_arkham(
    payload: object,
    *,
    expected_address: str,
    observed_at: int,
) -> dict[str, object] | None:
    """Return a bounded exact-evidence candidate, never predicted evidence."""

    if not isinstance(payload, dict) or not _is_base(payload.get("chain")):
        return None
    try:
        expected = normalize_evm_address(expected_address)
        actual = normalize_evm_address(str(payload.get("address") or ""))
    except LabelValidationError:
        return None
    if actual != expected:
        return None

    entity = payload.get("arkhamEntity")
    entity_dict = entity if isinstance(entity, dict) else {}
    entity_name = _bounded_text(entity_dict.get("name"))
    entity_id = _bounded_text(entity_dict.get("id"))
    entity_type = _bounded_text(entity_dict.get("type")).lower()
    label = payload.get("arkhamLabel")
    provider_label = (
        _bounded_text(label.get("name"))
        if isinstance(label, dict)
        else ""
    )
    deposit_id = _bounded_text(payload.get("depositServiceID"))

    if deposit_id:
        evidence_type = "exact_deposit_exchange_id"
        proposed_type = "deposit"
        if not entity_name:
            entity_name = deposit_id
        if not entity_id:
            entity_id = deposit_id
    elif (
        entity_type in _CEX_ENTITY_TYPES
        and entity_id
        and (
            entity_dict.get("service") is True
            or payload.get("service") is True
            or _explicit_cex_tag(payload)
        )
        and entity_name
    ):
        evidence_type = "exact_cex_entity"
        proposed_type = _address_type(payload)
    else:
        return None

    candidate: dict[str, object] = {
        "candidate_id": "",
        "chain_id": 8453,
        "address": actual,
        "provider": "arkham",
        "provider_entity_id": entity_id,
        "provider_entity_name": entity_name,
        "provider_entity_type": entity_type,
        "provider_label": provider_label,
        "service": bool(
            entity_dict.get("service") is True
            or payload.get("service") is True
        ),
        "deposit_exchange_id_present": bool(deposit_id),
        "proposed_address_type": proposed_type,
        "evidence_type": evidence_type,
        "evidence_hash": "",
        "observed_at": int(observed_at),
        "status": "pending",
        "reviewed_at": None,
        "review_note": "",
    }
    candidate["evidence_hash"] = _evidence_hash(candidate)
    candidate["candidate_id"] = _candidate_id(candidate)
    return candidate


def _chmod_private(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass


class LabelCandidateStore:
    def __init__(self, path: Path):
        self.path = path

    @classmethod
    def from_settings(
        cls,
        settings: OnchainSettings,
    ) -> "LabelCandidateStore":
        return cls(settings.label_candidates_path)

    @staticmethod
    def _empty() -> dict[str, object]:
        return {
            "schema_version": LABEL_CANDIDATE_SCHEMA_VERSION,
            "candidates": [],
        }

    def _read_unlocked(self) -> dict[str, object]:
        if not self.path.exists():
            return self._empty()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise LabelCandidateError("candidate_store_invalid") from exc
        if (
            not isinstance(value, dict)
            or value.get("schema_version")
            != LABEL_CANDIDATE_SCHEMA_VERSION
            or not isinstance(value.get("candidates"), list)
        ):
            raise LabelCandidateError("candidate_store_invalid")
        return value

    def _prepare_private_path(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _chmod_private(self.path.parent, 0o700)

    def list(
        self,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        with _file_lock(self.path):
            data = self._read_unlocked()
        candidates = [
            dict(item)
            for item in data["candidates"]
            if isinstance(item, dict)
            and (status is None or item.get("status") == status)
        ]
        candidates.sort(
            key=lambda item: (
                -int(item.get("observed_at") or 0),
                str(item.get("candidate_id") or ""),
            )
        )
        return candidates[: max(0, min(int(limit), 100))]

    def merge(self, candidates: list[dict[str, object]]) -> dict[str, int]:
        self._prepare_private_path()
        with _file_lock(self.path):
            data = self._read_unlocked()
            current = {
                str(item.get("candidate_id")): item
                for item in data["candidates"]
                if isinstance(item, dict) and item.get("candidate_id")
            }
            created = 0
            refreshed = 0
            for candidate in candidates:
                candidate_id = str(candidate["candidate_id"])
                existing = current.get(candidate_id)
                if existing is None:
                    current[candidate_id] = dict(candidate)
                    created += 1
                elif existing.get("status") == "pending":
                    current[candidate_id] = {
                        **candidate,
                        "status": "pending",
                        "reviewed_at": None,
                        "review_note": "",
                    }
                    refreshed += 1
            data["candidates"] = sorted(
                current.values(),
                key=lambda item: str(item.get("candidate_id") or ""),
            )
            _write_json_unlocked(self.path, data)
            _chmod_private(self.path, 0o600)
        return {"created": created, "refreshed": refreshed}

    def reject(
        self,
        candidate_id: str,
        *,
        reviewed_at: int | None = None,
    ) -> dict[str, object]:
        return self._review(
            candidate_id,
            status="rejected",
            review_note="manual_review",
            reviewed_at=reviewed_at,
        )

    def _review(
        self,
        candidate_id: str,
        *,
        status: str,
        review_note: str,
        reviewed_at: int | None,
    ) -> dict[str, object]:
        now = int(time.time()) if reviewed_at is None else int(reviewed_at)
        with _file_lock(self.path):
            data = self._read_unlocked()
            selected: dict[str, object] | None = None
            for item in data["candidates"]:
                if (
                    isinstance(item, dict)
                    and item.get("candidate_id") == candidate_id
                ):
                    selected = item
                    break
            if selected is None:
                raise LabelCandidateError("candidate_not_found")
            if selected.get("status") != "pending":
                raise LabelCandidateError("candidate_already_reviewed")
            selected["status"] = status
            selected["reviewed_at"] = now
            selected["review_note"] = review_note[:200]
            _write_json_unlocked(self.path, data)
            _chmod_private(self.path, 0o600)
            return dict(selected)

    def approve(
        self,
        candidate_id: str,
        *,
        labels_path: Path,
        min_confidence: float,
        reviewed_at: int | None = None,
    ) -> dict[str, object]:
        now = int(time.time()) if reviewed_at is None else int(reviewed_at)
        if (
            labels_path.name == "cex_addresses.example.csv"
            or labels_path.is_symlink()
        ):
            raise LabelCandidateError("private_labels_file_required")
        self._prepare_private_path()
        with _file_lock(self.path):
            data = self._read_unlocked()
            selected: dict[str, object] | None = None
            for item in data["candidates"]:
                if (
                    isinstance(item, dict)
                    and item.get("candidate_id") == candidate_id
                ):
                    selected = item
                    break
            if selected is None:
                raise LabelCandidateError("candidate_not_found")
            if selected.get("status") != "pending":
                raise LabelCandidateError("candidate_already_reviewed")
            _validate_persisted_candidate(selected)

            labels_path.parent.mkdir(parents=True, exist_ok=True)
            with _file_lock(labels_path):
                existed = labels_path.exists()
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
                    rows = self._read_label_rows(labels_path)
                    key = (
                        int(selected["chain_id"]),
                        normalize_evm_address(str(selected["address"])),
                    )
                    if any(
                        (
                            int(str(row["chain_id"])),
                            normalize_evm_address(str(row["address"])),
                        )
                        == key
                        for row in rows
                    ):
                        raise LabelCandidateError(
                            "candidate_label_duplicate"
                        )
                    rows.append({
                        "chain_id": "8453",
                        "address": key[1],
                        "entity_name": str(
                            _bounded_text(
                                selected["provider_entity_name"]
                            )
                        ),
                        "entity_type": "cex",
                        "address_type": str(
                            selected["proposed_address_type"]
                        ),
                        "source": "arkham_api_exact+manual_review",
                        "confidence": "0.95",
                        "valid_from": str(now),
                        "valid_to": "",
                    })
                    text = self._render_rows(rows)
                    _atomic_write_text_unlocked(labels_path, text)
                    _chmod_private(labels_path, 0o600)
                    labels = load_labels_csv(labels_path)
                    validate_live_labels(
                        labels,
                        min_confidence=min_confidence,
                        chain_id=8453,
                        timestamp=now,
                    )
                    selected["status"] = "approved"
                    selected["reviewed_at"] = now
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
            "candidate": dict(selected),
            "backup_created": backup is not None,
        }

    @staticmethod
    def _read_label_rows(path: Path) -> list[dict[str, str]]:
        if not path.exists():
            return []
        load_labels_csv(path)
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [
                {key: str(row.get(key) or "") for key in REQUIRED_COLUMNS}
                for row in csv.DictReader(handle)
            ]

    @staticmethod
    def _render_rows(rows: list[dict[str, str]]) -> str:
        buffer = io.StringIO(newline="")
        writer = csv.DictWriter(
            buffer,
            fieldnames=list(REQUIRED_COLUMNS),
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(rows)
        return buffer.getvalue()


def _aggregate_addresses(
    activity: dict[str, object],
    *,
    contract: str,
    limit: int,
) -> list[dict[str, object]]:
    transfers = activity.get("transfers")
    if not isinstance(transfers, list):
        raise LabelCandidateError("token_activity_invalid")
    aggregates: dict[str, dict[str, object]] = {}
    excluded = {ZERO_ADDRESS, normalize_evm_address(contract)}
    for transfer in transfers:
        if not isinstance(transfer, dict):
            continue
        try:
            amount = Decimal(str(transfer.get("amount") or "0"))
        except (InvalidOperation, ValueError):
            amount = Decimal(0)
        block_time = int(transfer.get("block_time") or 0)
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
            if address in excluded:
                continue
            entry = aggregates.setdefault(
                address,
                {"count": 0, "amount": Decimal(0), "latest": 0},
            )
            entry["count"] = int(entry["count"]) + 1
            entry["amount"] = Decimal(entry["amount"]) + amount
            entry["latest"] = max(int(entry["latest"]), block_time)
    ordered = sorted(
        aggregates,
        key=lambda address: (
            -int(aggregates[address]["count"]),
            -Decimal(aggregates[address]["amount"]),
            address,
        ),
    )
    return [
        {
            "address": address,
            "source_transfer_count": int(aggregates[address]["count"]),
            "source_token_amount": str(aggregates[address]["amount"]),
            "source_latest_at": int(aggregates[address]["latest"]),
        }
        for address in ordered[:limit]
    ]


def _seed_address_payloads(
    transfers: list[dict[str, object]],
) -> list[tuple[str, dict[str, object]]]:
    results: list[tuple[str, dict[str, object]]] = []
    seen: set[str] = set()
    for transfer in transfers:
        for key in ("fromAddress", "toAddress"):
            value = transfer.get(key)
            if not isinstance(value, dict):
                continue
            try:
                address = normalize_evm_address(
                    str(value.get("address") or "")
                )
            except LabelValidationError:
                continue
            if address in seen:
                continue
            seen.add(address)
            results.append((address, value))
    return results


class LabelCandidateDiscovery:
    def __init__(
        self,
        settings: OnchainSettings,
        *,
        client_factory: Callable[..., ArkhamIntelligenceClient] = (
            ArkhamIntelligenceClient
        ),
        activity_runner: Callable[
            [TokenActivityQuery], dict[str, object]
        ] | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.settings = settings
        self.client_factory = client_factory
        self.activity_runner = activity_runner
        self.clock = clock

    def _client(self) -> ArkhamIntelligenceClient:
        return self.client_factory(
            base_url=self.settings.arkham_api_base_url,
            api_key=self.settings.arkham_api_key,
            timeout_sec=self.settings.arkham_api_timeout_sec,
            max_retries=self.settings.arkham_api_max_retries,
        )

    def provider_check(self) -> dict[str, object]:
        return self._client().provider_check()

    def discover(
        self,
        *,
        chain: str,
        contract: str,
        window: str,
        max_addresses: int,
    ) -> dict[str, object]:
        if chain != "base" or window != "4h":
            raise LabelCandidateError("candidate_query_invalid")
        if not 1 <= max_addresses <= min(
            100,
            self.settings.oar_label_candidate_max_addresses,
        ):
            raise LabelCandidateError("candidate_address_limit_invalid")
        if not self.settings.arkham_api_key:
            raise ArkhamIntelligenceError("arkham_not_configured")
        query = TokenActivityQuery.create(
            self.settings,
            chain=chain,
            contract=contract,
            window=window,
            max_events=None,
            max_rpc_requests=None,
            top_n=min(max_addresses, self.settings.token_activity_top_n),
            with_price=False,
            min_usd=None,
        )
        activity = (
            self.activity_runner(query)
            if self.activity_runner is not None
            else TokenActivityQueryService.from_settings(
                self.settings, query
            ).execute(query)
        )
        if (
            activity.get("status") != "ok"
            or activity.get("complete") is not True
        ):
            raise LabelCandidateError("token_activity_incomplete")
        address_observations = _aggregate_addresses(
            activity,
            contract=query.contract,
            limit=max_addresses,
        )
        addresses = [
            str(item["address"]) for item in address_observations
        ]
        observation_by_address = {
            str(item["address"]): item for item in address_observations
        }
        client = self._client()
        intelligence = client.address_intelligence(addresses)
        observed_at = int(self.clock())
        candidates = [
            {
                **candidate,
                **{
                    key: value
                    for key, value in observation_by_address[address].items()
                    if key != "address"
                },
                "source_kind": "token_activity",
            }
            for address in addresses
            if (
                candidate := candidate_from_arkham(
                    intelligence.get(address),
                    expected_address=address,
                    observed_at=observed_at,
                )
            )
            is not None
        ]
        seed_used = False
        if not candidates:
            seed_used = True
            seeds = client.seed_cex_transfers()
            candidates = [
                {
                    **candidate,
                    "source_transfer_count": 0,
                    "source_token_amount": "0",
                    "source_latest_at": 0,
                    "source_kind": "arkham_cex_seed",
                }
                for address, payload in _seed_address_payloads(seeds)
                if (
                    candidate := candidate_from_arkham(
                        payload,
                        expected_address=address,
                        observed_at=observed_at,
                    )
                )
                is not None
            ]
        unique = {
            str(candidate["candidate_id"]): candidate
            for candidate in candidates
        }
        store_result = LabelCandidateStore.from_settings(
            self.settings
        ).merge(list(unique.values()))
        diagnostics = activity.get("diagnostics")
        return {
            "status": "ok",
            "provider": "arkham",
            "chain": "base",
            "token_activity_complete": True,
            "rpc_request_count": (
                int(diagnostics.get("rpc_request_count") or 0)
                if isinstance(diagnostics, dict)
                else 0
            ),
            "transfer_count": (
                int(diagnostics.get("transfer_count") or 0)
                if isinstance(diagnostics, dict)
                else len(activity.get("transfers") or [])
            ),
            "addresses_examined": len(addresses),
            "candidates_found": len(unique),
            "created": store_result["created"],
            "refreshed": store_result["refreshed"],
            "seed_queries_used": seed_used,
            "arkham_request_count": client.request_count,
            "network_activity": True,
            "telegram_calls": 0,
            "ai_calls": 0,
        }


def label_readiness(
    path: Path,
    *,
    min_confidence: float,
    chain_id: int = 8453,
    now: int | None = None,
) -> dict[str, object]:
    labels = load_labels_csv(path)
    timestamp = int(time.time()) if now is None else int(now)
    synthetic = sum(
        label.source.strip().lower() == "synthetic_fixture"
        for label in labels
    )
    eligible = [
        label
        for label in labels
        if label.chain_id == chain_id
        and label.entity_type == "cex"
        and label.confidence >= min_confidence
        and label.active_at(timestamp)
        and label.source.strip().lower() != "synthetic_fixture"
    ]
    mode = (
        format(path.stat().st_mode & 0o777, "03o")
        if path.exists()
        else ""
    )
    return {
        "status": "ok",
        "total_labels": len(labels),
        "classification_eligible_cex_count": len(eligible),
        "synthetic_fixture": synthetic,
        "invalid_address": 0,
        "duplicate_address": 0,
        "permissions": mode,
    }


__all__ = [
    "LabelCandidateDiscovery",
    "LabelCandidateError",
    "LabelCandidateStore",
    "candidate_from_arkham",
    "label_readiness",
]
