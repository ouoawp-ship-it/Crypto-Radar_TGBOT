from __future__ import annotations

import csv
import re
from pathlib import Path

from .models import AddressLabel


EVM_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
REQUIRED_COLUMNS = (
    "chain_id",
    "address",
    "entity_name",
    "entity_type",
    "address_type",
    "source",
    "confidence",
    "valid_from",
    "valid_to",
)


class LabelValidationError(ValueError):
    pass


def normalize_evm_address(address: str) -> str:
    value = address.strip()
    if not EVM_ADDRESS_RE.fullmatch(value):
        raise LabelValidationError(f"invalid EVM address: {value}")
    return value.lower()


def _optional_int(value: str, field: str, row_number: int) -> int | None:
    if not value.strip():
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise LabelValidationError(
            f"row {row_number}: {field} must be a unix timestamp"
        ) from exc


def load_labels_csv(path: Path) -> list[AddressLabel]:
    if not path.exists():
        raise LabelValidationError(f"label file not found: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = [column for column in REQUIRED_COLUMNS if column not in (reader.fieldnames or [])]
        if missing:
            raise LabelValidationError(
                f"label file missing columns: {', '.join(missing)}"
            )
        labels: list[AddressLabel] = []
        seen: set[tuple[int, str]] = set()
        for row_number, row in enumerate(reader, start=2):
            try:
                chain_id = int(str(row["chain_id"]).strip())
                confidence = float(str(row["confidence"]).strip())
            except (TypeError, ValueError) as exc:
                raise LabelValidationError(
                    f"row {row_number}: invalid chain_id or confidence"
                ) from exc
            if chain_id <= 0:
                raise LabelValidationError(
                    f"row {row_number}: chain_id must be positive"
                )
            if not 0 <= confidence <= 1:
                raise LabelValidationError(
                    f"row {row_number}: confidence must be in [0, 1]"
                )
            address = normalize_evm_address(str(row["address"]))
            key = (chain_id, address)
            if key in seen:
                raise LabelValidationError(
                    f"row {row_number}: duplicate label for {chain_id}:{address}"
                )
            seen.add(key)
            entity_name = str(row["entity_name"]).strip()
            entity_type = str(row["entity_type"]).strip().lower()
            address_type = str(row["address_type"]).strip().lower()
            source = str(row["source"]).strip()
            if not entity_name or not entity_type or not address_type or not source:
                raise LabelValidationError(
                    f"row {row_number}: label fields cannot be blank"
                )
            valid_from = _optional_int(
                str(row.get("valid_from") or ""), "valid_from", row_number
            )
            valid_to = _optional_int(
                str(row.get("valid_to") or ""), "valid_to", row_number
            )
            if (
                valid_from is not None
                and valid_to is not None
                and valid_from > valid_to
            ):
                raise LabelValidationError(
                    f"row {row_number}: valid_from is after valid_to"
                )
            labels.append(
                AddressLabel(
                    chain_id=chain_id,
                    address=address,
                    entity_name=entity_name,
                    entity_type=entity_type,
                    address_type=address_type,
                    source=source,
                    confidence=confidence,
                    valid_from=valid_from,
                    valid_to=valid_to,
                )
            )
    return labels


class LabelRegistry:
    def __init__(self, labels: list[AddressLabel]):
        self._labels = {
            (label.chain_id, label.address): label for label in labels
        }

    def lookup(
        self, chain_id: int, address: str, timestamp: int
    ) -> AddressLabel | None:
        label = self._labels.get((int(chain_id), address.lower()))
        if label is None or not label.active_at(timestamp):
            return None
        return label
