from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .models import MappingRecord


OVERRIDE_SCHEMA_VERSION = 1
MULTIPLIER_PREFIXES = (1_000_000, 100_000, 10_000, 1_000)
MULTIPLIER_ALIASES = (("1M", 1_000_000),)


class MappingConfigError(ValueError):
    pass


def normalize_contract_asset(base_asset: str) -> tuple[str, int]:
    base = str(base_asset or "").strip().upper()
    prefixes = tuple((str(value), value) for value in MULTIPLIER_PREFIXES) + MULTIPLIER_ALIASES
    for prefix, multiplier in prefixes:
        if base.startswith(prefix):
            remainder = base[len(prefix):]
            if remainder and remainder[0].isalpha():
                return remainder, multiplier
    return base, 1


def load_mapping_overrides(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MappingConfigError("mapping override file is unreadable") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != OVERRIDE_SCHEMA_VERSION:
        raise MappingConfigError("unsupported mapping override schema")
    raw_overrides = payload.get("overrides")
    if not isinstance(raw_overrides, list):
        raise MappingConfigError("mapping overrides must be a list")
    result: dict[str, dict[str, Any]] = {}
    for raw in raw_overrides:
        if not isinstance(raw, dict):
            raise MappingConfigError("invalid mapping override entry")
        if set(raw) - {
            "binance_symbol",
            "cmc_id",
            "normalized_asset",
            "token_address",
            "note",
        }:
            raise MappingConfigError("mapping override contains unsupported fields")
        symbol = str(raw.get("binance_symbol") or "").strip().upper()
        try:
            cmc_id = int(raw.get("cmc_id") or 0)
        except (TypeError, ValueError) as exc:
            raise MappingConfigError("mapping override has invalid cmc_id") from exc
        if not symbol.endswith("USDT") or cmc_id <= 0 or symbol in result:
            raise MappingConfigError("mapping override identity is invalid or duplicated")
        result[symbol] = {
            "binance_symbol": symbol,
            "cmc_id": cmc_id,
            "normalized_asset": str(raw.get("normalized_asset") or "").strip().upper() or None,
            "token_address": str(raw.get("token_address") or "").strip() or None,
            "note": str(raw.get("note") or "").strip()[:200] or None,
        }
    return result


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _positive_id(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _active(item: Any) -> bool:
    value = _value(item, "is_active", _value(item, "active", True))
    return value not in {False, 0, "0"}


def _address(item: Any) -> str:
    direct = str(_value(item, "token_address", "") or "").strip()
    if direct:
        return direct.lower()
    platform = _value(item, "platform", {})
    if isinstance(platform, Mapping):
        return str(platform.get("token_address") or "").strip().lower()
    return ""


def _platform_tokens(item: Any) -> set[str]:
    values = {
        str(_value(item, "platform_name", "") or "").strip().lower(),
        str(_value(item, "platform_symbol", "") or "").strip().lower(),
        str(_value(item, "platform_slug", "") or "").strip().lower(),
    }
    platform = _value(item, "platform", {})
    if isinstance(platform, Mapping):
        values.update({
            str(platform.get("name") or "").strip().lower(),
            str(platform.get("symbol") or "").strip().lower(),
            str(platform.get("slug") or "").strip().lower(),
        })
    return {value for value in values if value}


@dataclass(frozen=True)
class MappingSummary:
    records: tuple[MappingRecord, ...]
    trusted_count: int
    diagnostic_count: int
    conflict_count: int
    unmapped_count: int
    reason_counts: dict[str, int]


class CmcIdentityResolver:
    def __init__(
        self,
        cmc_entries: Iterable[Any],
        *,
        overrides: Mapping[str, Mapping[str, Any]] | None = None,
        verified_at: str | None = None,
    ) -> None:
        self.entries = tuple(cmc_entries)
        self.by_id = {
            cmc_id: item
            for item in self.entries
            if (cmc_id := _positive_id(_value(item, "cmc_id", _value(item, "id")))) is not None
        }
        self.by_symbol: dict[str, list[Any]] = {}
        self.by_address: dict[str, list[Any]] = {}
        for item in self.entries:
            symbol = str(_value(item, "symbol", "") or "").strip().upper()
            if symbol:
                self.by_symbol.setdefault(symbol, []).append(item)
            address = _address(item)
            if address:
                self.by_address.setdefault(address, []).append(item)
        self.overrides = {str(key).upper(): dict(value) for key, value in (overrides or {}).items()}
        self.verified_at = verified_at or datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _identity(item: Any) -> tuple[int | None, str | None, str | None, str | None, str | None]:
        cmc_id = _positive_id(_value(item, "cmc_id", _value(item, "id")))
        name = str(_value(item, "name", "") or "").strip() or None
        symbol = str(_value(item, "symbol", "") or "").strip().upper() or None
        slug = str(_value(item, "slug", "") or "").strip().lower() or None
        token_address = _address(item) or None
        return cmc_id, name, symbol, slug, token_address

    def _record(
        self,
        *,
        symbol: str,
        base_asset: str,
        normalized_asset: str,
        multiplier: int,
        item: Any | None,
        method: str,
        confidence: str,
        evidence: Iterable[str] = (),
        reason: str | None = None,
    ) -> MappingRecord:
        identity = self._identity(item) if item is not None else (None, None, None, None, None)
        return MappingRecord(
            binance_symbol=symbol,
            base_asset=base_asset,
            normalized_asset=normalized_asset,
            contract_multiplier=multiplier,
            cmc_id=identity[0],
            cmc_name=identity[1],
            cmc_symbol=identity[2],
            cmc_slug=identity[3],
            token_address=identity[4],
            mapping_method=method,
            mapping_confidence=confidence,
            mapping_evidence=tuple(str(value) for value in evidence if value),
            verified_at=self.verified_at if confidence == "high" else None,
            rejection_reason=reason,
        )

    def resolve_one(
        self,
        contract: Mapping[str, Any],
        marketing: Mapping[str, Any] | None = None,
    ) -> MappingRecord:
        symbol = str(contract.get("symbol") or "").strip().upper()
        base_asset = str(contract.get("baseAsset") or "").strip().upper()
        normalized_asset, multiplier = normalize_contract_asset(base_asset)
        marketing = marketing if isinstance(marketing, Mapping) else {}

        override = self.overrides.get(symbol)
        if override:
            expected = str(override.get("normalized_asset") or "").upper()
            item = self.by_id.get(_positive_id(override.get("cmc_id")) or -1)
            if expected and expected != normalized_asset:
                return self._record(
                    symbol=symbol, base_asset=base_asset, normalized_asset=normalized_asset,
                    multiplier=multiplier, item=item, method="ambiguous", confidence="none",
                    evidence=("manual_override",), reason="override_asset_mismatch",
                )
            if item is None or not _active(item):
                return self._record(
                    symbol=symbol, base_asset=base_asset, normalized_asset=normalized_asset,
                    multiplier=multiplier, item=item, method="unmapped", confidence="none",
                    evidence=("manual_override",), reason="inactive_cmc_asset",
                )
            expected_address = str(override.get("token_address") or "").strip().lower()
            if expected_address and _address(item) != expected_address:
                return self._record(
                    symbol=symbol, base_asset=base_asset, normalized_asset=normalized_asset,
                    multiplier=multiplier, item=item, method="ambiguous", confidence="none",
                    evidence=("manual_override", "token_address_mismatch"),
                    reason="override_address_mismatch",
                )
            return self._record(
                symbol=symbol, base_asset=base_asset, normalized_asset=normalized_asset,
                multiplier=multiplier, item=item, method="verified_override", confidence="high",
                evidence=(
                    "manual_override",
                    "cmc_id_exists_in_active_map",
                    "token_address_consistent" if expected_address else "",
                ),
            )

        if marketing.get("_identity_conflict"):
            return self._record(
                symbol=symbol, base_asset=base_asset, normalized_asset=normalized_asset,
                multiplier=multiplier, item=None, method="ambiguous", confidence="none",
                evidence=("duplicate_binance_marketing_identity",), reason="ambiguous_symbol",
            )

        anchor_id = _positive_id(marketing.get("cmc_id"))
        mapper_name = str(marketing.get("mapper_name") or "").strip().upper()
        contract_address = str(marketing.get("token_address") or "").strip().lower()
        address_matches = self.by_address.get(contract_address, []) if contract_address else []
        if len(address_matches) == 1:
            address_item = address_matches[0]
            address_id, _name, address_symbol, _slug, _token = self._identity(address_item)
            identity_consistent = (
                _active(address_item)
                and address_symbol == normalized_asset
                and (anchor_id is None or anchor_id == address_id)
                and (not mapper_name or mapper_name == normalized_asset)
            )
            marketing_platform = _platform_tokens(marketing)
            cmc_platform = _platform_tokens(address_item)
            platform_consistent = (
                not marketing_platform
                or not cmc_platform
                or bool(marketing_platform & cmc_platform)
            )
            if not identity_consistent or not platform_consistent:
                return self._record(
                    symbol=symbol, base_asset=base_asset,
                    normalized_asset=normalized_asset, multiplier=multiplier,
                    item=address_item, method="ambiguous", confidence="none",
                    evidence=("exact_token_address", "identity_conflict"),
                    reason="ambiguous_symbol",
                )
            return self._record(
                symbol=symbol, base_asset=base_asset, normalized_asset=normalized_asset,
                multiplier=multiplier, item=address_item, method="contract_address_match",
                confidence="high", evidence=(
                    "exact_token_address",
                    "active_cmc_map",
                    "normalized_symbol_consistent",
                    "binance_cmc_id_consistent" if anchor_id is not None else "",
                    "platform_consistent" if marketing_platform and cmc_platform else "",
                ),
            )
        if len(address_matches) > 1:
            return self._record(
                symbol=symbol, base_asset=base_asset, normalized_asset=normalized_asset,
                multiplier=multiplier, item=None, method="ambiguous", confidence="none",
                evidence=("duplicate_token_address",), reason="ambiguous_symbol",
            )

        if anchor_id is not None:
            item = self.by_id.get(anchor_id)
            if item is None or not _active(item):
                return self._record(
                    symbol=symbol, base_asset=base_asset, normalized_asset=normalized_asset,
                    multiplier=multiplier, item=item, method="unmapped", confidence="none",
                    evidence=("binance_cmc_id",), reason="inactive_cmc_asset",
                )
            _cmc_id, _name, cmc_symbol, _slug, _token = self._identity(item)
            mapper_consistent = not mapper_name or mapper_name == normalized_asset
            symbol_consistent = cmc_symbol == normalized_asset
            if mapper_consistent and symbol_consistent:
                evidence = ["binance_cmc_unique_id", "active_cmc_map_id", "normalized_symbol_consistent"]
                if marketing.get("_matched_by_mapper"):
                    evidence.append("binance_unique_mapper_anchor")
                if multiplier > 1 and mapper_name == normalized_asset:
                    evidence.append("multiplier_mapper_name_consistent")
                return self._record(
                    symbol=symbol, base_asset=base_asset, normalized_asset=normalized_asset,
                    multiplier=multiplier, item=item, method="existing_verified_anchor",
                    confidence="high", evidence=evidence,
                )
            return self._record(
                symbol=symbol, base_asset=base_asset, normalized_asset=normalized_asset,
                multiplier=multiplier, item=item, method="ambiguous", confidence="none",
                evidence=("binance_cmc_unique_id", "identity_mismatch"), reason="ambiguous_symbol",
            )

        symbol_matches = [item for item in self.by_symbol.get(normalized_asset, []) if _active(item)]
        if len(symbol_matches) == 1:
            return self._record(
                symbol=symbol, base_asset=base_asset, normalized_asset=normalized_asset,
                multiplier=multiplier, item=symbol_matches[0], method="unique_symbol_diagnostic",
                confidence="diagnostic", evidence=("unique_symbol_only",), reason="missing_cmc_id",
            )
        if len(symbol_matches) > 1:
            return self._record(
                symbol=symbol, base_asset=base_asset, normalized_asset=normalized_asset,
                multiplier=multiplier, item=None, method="ambiguous", confidence="none",
                evidence=("multiple_active_symbol_matches",), reason="ambiguous_symbol",
            )
        return self._record(
            symbol=symbol, base_asset=base_asset, normalized_asset=normalized_asset,
            multiplier=multiplier, item=None, method="unmapped", confidence="none",
            evidence=(), reason="missing_cmc_id",
        )

    def resolve_many(
        self,
        contracts: Iterable[Mapping[str, Any]],
        marketing_rows: Iterable[Mapping[str, Any]],
    ) -> MappingSummary:
        marketing_by_symbol: dict[str, Mapping[str, Any]] = {}
        marketing_by_mapper: dict[str, list[Mapping[str, Any]]] = {}
        for item in marketing_rows:
            if not isinstance(item, Mapping) or not item.get("symbol"):
                continue
            symbol = str(item.get("symbol") or "").strip().upper()
            previous = marketing_by_symbol.get(symbol)
            if previous is not None and dict(previous) != dict(item):
                marketing_by_symbol[symbol] = {"_identity_conflict": True}
            elif previous is None:
                marketing_by_symbol[symbol] = item
            mapper_name = str(item.get("mapper_name") or "").strip().upper()
            if mapper_name:
                marketing_by_mapper.setdefault(mapper_name, []).append(item)

        def marketing_for(contract: Mapping[str, Any]) -> Mapping[str, Any] | None:
            symbol = str(contract.get("symbol") or "").strip().upper()
            exact = marketing_by_symbol.get(symbol)
            if exact is not None:
                return exact
            normalized, _multiplier = normalize_contract_asset(
                str(contract.get("baseAsset") or "")
            )
            candidates = [
                item for item in marketing_by_mapper.get(normalized, [])
                if normalize_contract_asset(str(item.get("base_asset") or ""))[0]
                == normalized
            ]
            identities = {
                _positive_id(item.get("cmc_id"))
                for item in candidates
                if _positive_id(item.get("cmc_id")) is not None
            }
            if len(identities) == 1:
                selected = next(
                    item for item in candidates
                    if _positive_id(item.get("cmc_id")) in identities
                )
                return {**dict(selected), "_matched_by_mapper": True}
            if len(identities) > 1:
                return {"_identity_conflict": True}
            return None
        records = tuple(
            self.resolve_one(contract, marketing_for(contract))
            for contract in sorted(contracts, key=lambda item: str(item.get("symbol") or ""))
        )
        reasons: dict[str, int] = {}
        for record in records:
            if record.rejection_reason:
                reasons[record.rejection_reason] = reasons.get(record.rejection_reason, 0) + 1
        return MappingSummary(
            records=records,
            trusted_count=sum(record.is_formal for record in records),
            diagnostic_count=sum(record.mapping_confidence == "diagnostic" for record in records),
            conflict_count=sum(record.mapping_method == "ambiguous" for record in records),
            unmapped_count=sum(record.mapping_method == "unmapped" for record in records),
            reason_counts=dict(sorted(reasons.items())),
        )


__all__ = [
    "CmcIdentityResolver",
    "MULTIPLIER_ALIASES",
    "MULTIPLIER_PREFIXES",
    "MappingConfigError",
    "MappingSummary",
    "normalize_contract_asset",
    "load_mapping_overrides",
]
