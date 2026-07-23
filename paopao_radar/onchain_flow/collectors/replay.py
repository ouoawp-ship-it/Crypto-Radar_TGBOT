from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

from ..constants import FIXTURE_VERSION
from ..labels import normalize_evm_address
from ..models import NormalizedTransfer, TokenMetadata
from .base import TransferCollector


class FixtureValidationError(ValueError):
    pass


def _optional_decimal(value: object) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation as exc:
        raise FixtureValidationError(f"invalid decimal value: {value}") from exc


@dataclass(frozen=True)
class FixtureData:
    name: str
    metadata: tuple[TokenMetadata, ...]
    transfers: tuple[NormalizedTransfer, ...]


class ReplayCollector(TransferCollector):
    def __init__(self, fixture_path: Path):
        self.fixture_path = fixture_path
        self.data = self._load()

    def collect(self) -> tuple[NormalizedTransfer, ...]:
        return self.data.transfers

    def _load(self) -> FixtureData:
        if not self.fixture_path.exists():
            raise FixtureValidationError(
                f"fixture not found: {self.fixture_path}"
            )
        name = self.fixture_path.name
        metadata: list[TokenMetadata] = []
        transfers: list[NormalizedTransfer] = []
        manifest_seen = False
        for line_number, raw_line in enumerate(
            self.fixture_path.read_text(encoding="utf-8").splitlines(),
            start=1,
        ):
            line = raw_line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise FixtureValidationError(
                    f"line {line_number}: invalid JSON"
                ) from exc
            if not isinstance(record, dict):
                raise FixtureValidationError(
                    f"line {line_number}: record must be an object"
                )
            if record.get("fixture_version") != FIXTURE_VERSION:
                raise FixtureValidationError(
                    f"line {line_number}: fixture_version must be {FIXTURE_VERSION}"
                )
            record_type = str(record.get("record_type") or "")
            if record_type == "manifest":
                if manifest_seen:
                    raise FixtureValidationError("duplicate fixture manifest")
                manifest_seen = True
                name = str(record.get("name") or name)
            elif record_type == "token_metadata":
                metadata.append(self._metadata(record, line_number))
            elif record_type == "transfer":
                transfers.append(self._transfer(record, line_number))
            else:
                raise FixtureValidationError(
                    f"line {line_number}: unknown record_type {record_type!r}"
                )
        if not manifest_seen:
            raise FixtureValidationError("fixture manifest is required")
        if not metadata:
            raise FixtureValidationError("fixture token metadata is required")
        if not transfers:
            raise FixtureValidationError("fixture transfers are required")
        return FixtureData(
            name=name,
            metadata=tuple(metadata),
            transfers=tuple(transfers),
        )

    @staticmethod
    def _metadata(record: dict[str, object], line_number: int) -> TokenMetadata:
        try:
            chain_id = int(record["chain_id"])
            token_address = normalize_evm_address(str(record["token_address"]))
            decimals_value = record.get("decimals")
            decimals = (
                int(decimals_value)
                if decimals_value is not None and str(decimals_value) != ""
                else None
            )
            return TokenMetadata(
                chain_id=chain_id,
                token_address=token_address,
                symbol=str(record["symbol"]).upper(),
                name=str(record["name"]),
                decimals=decimals,
                token_kind=str(record["token_kind"]),
                metadata_status=str(record["metadata_status"]),
                updated_at=int(record["updated_at"]),
                price_usd=_optional_decimal(record.get("price_usd")),
                volume_24h_usd=_optional_decimal(record.get("volume_24h_usd")),
                historical_single_p99_usd=_optional_decimal(
                    record.get("historical_single_p99_usd")
                ),
                historical_15m_p99_usd=_optional_decimal(
                    record.get("historical_15m_p99_usd")
                ),
                historical_60m_p99_usd=_optional_decimal(
                    record.get("historical_60m_p99_usd")
                ),
                historical_window_median_usd=_optional_decimal(
                    record.get("historical_window_median_usd")
                ),
                historical_window_mad_usd=_optional_decimal(
                    record.get("historical_window_mad_usd")
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise FixtureValidationError(
                f"line {line_number}: invalid token metadata"
            ) from exc

    @staticmethod
    def _transfer(record: dict[str, object], line_number: int) -> NormalizedTransfer:
        try:
            token_address = normalize_evm_address(str(record["token_address"]))
            from_address = normalize_evm_address(str(record["from_address"]))
            to_address = normalize_evm_address(str(record["to_address"]))
            return NormalizedTransfer.create(
                chain_id=int(record["chain_id"]),
                chain_name=str(record["chain_name"]),
                block_number=int(record["block_number"]),
                block_hash=str(record["block_hash"]),
                block_time=int(record["block_time"]),
                tx_hash=str(record["tx_hash"]),
                log_index=int(record["log_index"]),
                token_address=token_address,
                from_address=from_address,
                to_address=to_address,
                amount_raw=str(record["amount_raw"]),
                removed=bool(record.get("removed", False)),
                confirmation_status=str(
                    record.get("confirmation_status", "finalized")
                ),
                source="replay",
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise FixtureValidationError(
                f"line {line_number}: invalid transfer"
            ) from exc
