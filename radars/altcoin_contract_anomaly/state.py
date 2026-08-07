from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from math import isfinite
from pathlib import Path
from typing import Any, Iterable, Mapping

from shared.storage import JsonStore

from .models import (
    FORMAL_MAPPING_METHODS,
    CandidateSnapshot,
    RULES_VERSION,
    SCHEMA_VERSION,
    calculate_oi_market_cap_ratio,
    json_safe,
)
from .rules import (
    HIGH_LEVERAGE_CANDIDATE,
    SHORT_SQUEEZE_CANDIDATE,
    CandidateThresholds,
    apply_candidate_rules,
)


MODULE_ID = "altcoin_contract_anomaly"
RULE_PARAMETER_KEYS = (
    "market_cap_max_usd",
    "short_squeeze_min_ratio",
    "short_squeeze_max_funding_rate",
    "high_leverage_min_ratio",
)


class CandidateStateError(RuntimeError):
    pass


class CandidateStateSchemaError(CandidateStateError):
    pass


class CandidateStatePartialUpdateError(CandidateStateError):
    pass


def candidate_sort_key(snapshot: CandidateSnapshot) -> tuple[float, str]:
    ratio = snapshot.binance_oi_market_cap_ratio
    return (-(ratio if ratio is not None else -1.0), snapshot.symbol)


def _membership_content_for_hash(
    snapshots: Iterable[CandidateSnapshot],
) -> list[dict[str, Any]]:
    fields = (
        "symbol",
        "cmc_id",
        "mapping_method",
        "candidate_tags",
        "matched_rules",
    )
    return [
        {field: json_safe(getattr(snapshot, field)) for field in fields}
        for snapshot in sorted(snapshots, key=lambda item: item.symbol)
        if snapshot.candidate_tags
    ]


def _snapshot_content_for_hash(snapshots: Iterable[CandidateSnapshot]) -> list[dict[str, Any]]:
    fields = (
        "symbol",
        "cmc_id",
        "mapping_method",
        "market_cap_usd",
        "oi_value_usd",
        "binance_oi_usd",
        "mark_price",
        "funding_rate",
        "oi_market_cap_ratio",
        "binance_oi_market_cap_ratio",
        "binance_oi_source",
        "global_oi_usd",
        "global_oi_market_cap_ratio",
        "global_oi_source",
        "candidate_tags",
        "matched_rules",
        "data_quality",
    )
    return [
        {field: json_safe(getattr(snapshot, field)) for field in fields}
        for snapshot in sorted(snapshots, key=lambda item: item.symbol)
        if snapshot.candidate_tags
    ]


def _canonical_rule_parameters(
    parameters: Mapping[str, Any] | None,
) -> dict[str, float]:
    if parameters is None:
        return {}
    if set(parameters) != set(RULE_PARAMETER_KEYS):
        raise CandidateStateSchemaError("candidate rule parameters are incomplete")
    output: dict[str, float] = {}
    for key in RULE_PARAMETER_KEYS:
        value = parameters.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CandidateStateSchemaError("candidate rule parameter is invalid")
        parsed = float(value)
        if not isfinite(parsed):
            raise CandidateStateSchemaError("candidate rule parameter is invalid")
        output[key] = parsed
    return output


def rules_fingerprint(parameters: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        _canonical_rule_parameters(parameters),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def candidate_pool_hash(
    snapshots: Iterable[CandidateSnapshot],
    *,
    rule_parameters: Mapping[str, Any] | None = None,
) -> str:
    canonical = json.dumps(
        {
            "candidates": _membership_content_for_hash(snapshots),
            "rule_parameters": _canonical_rule_parameters(rule_parameters),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def candidate_snapshot_hash(
    snapshots: Iterable[CandidateSnapshot],
    *,
    rule_parameters: Mapping[str, Any] | None = None,
) -> str:
    canonical = json.dumps(
        {
            "candidates": _snapshot_content_for_hash(snapshots),
            "rule_parameters": _canonical_rule_parameters(rule_parameters),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_pool_document(
    snapshots: Iterable[CandidateSnapshot],
    *,
    generated_at: str,
    universe: Mapping[str, Any],
    mapping_stats: Mapping[str, Any],
    rule_parameters: Mapping[str, Any],
    mapping_records: Iterable[Any],
    previous: Mapping[str, Any] | None = None,
    data_sources: Mapping[str, Any] | None = None,
    diagnostics: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    ordered_all = sorted(snapshots, key=lambda item: item.symbol)
    short = sorted(
        (item for item in ordered_all if SHORT_SQUEEZE_CANDIDATE in item.candidate_tags),
        key=candidate_sort_key,
    )
    leverage = sorted(
        (item for item in ordered_all if HIGH_LEVERAGE_CANDIDATE in item.candidate_tags),
        key=candidate_sort_key,
    )
    short_symbols = [item.symbol for item in short]
    leverage_symbols = [item.symbol for item in leverage]
    merged = sorted(set(short_symbols) | set(leverage_symbols))
    dual = sorted(
        (
            item
            for item in ordered_all
            if SHORT_SQUEEZE_CANDIDATE in item.candidate_tags
            and HIGH_LEVERAGE_CANDIDATE in item.candidate_tags
        ),
        key=candidate_sort_key,
    )
    dual_symbols = [item.symbol for item in dual]
    previous_symbols = {
        str(value)
        for value in ((previous or {}).get("candidate_symbols") or [])
        if str(value)
    }
    current_symbols = set(merged)
    delta = {
        "added": sorted(current_symbols - previous_symbols),
        "retained": sorted(current_symbols & previous_symbols),
        "removed": sorted(previous_symbols - current_symbols),
    }
    normalized_rule_parameters = _canonical_rule_parameters(rule_parameters)
    content_hash = candidate_pool_hash(
        ordered_all,
        rule_parameters=normalized_rule_parameters,
    )
    snapshot_hash = candidate_snapshot_hash(
        ordered_all,
        rule_parameters=normalized_rule_parameters,
    )
    serialized_mappings = []
    for record in mapping_records:
        if hasattr(record, "to_dict"):
            serialized_mappings.append(record.to_dict())
        elif isinstance(record, Mapping):
            serialized_mappings.append(dict(record))
    serialized_mappings.sort(key=lambda item: str(item.get("binance_symbol") or ""))
    return json_safe({
        "schema_version": SCHEMA_VERSION,
        "module": MODULE_ID,
        "rules_version": RULES_VERSION,
        "rule_parameters": normalized_rule_parameters,
        "rules_fingerprint": rules_fingerprint(normalized_rule_parameters),
        "generated_at": generated_at,
        "candidate_pool_hash": content_hash,
        "candidate_snapshot_hash": snapshot_hash,
        "previous_candidate_pool_hash": (previous or {}).get("candidate_pool_hash"),
        "previous_candidate_snapshot_hash": (previous or {}).get("candidate_snapshot_hash"),
        "changed": content_hash != (previous or {}).get("candidate_pool_hash"),
        "snapshot_changed": snapshot_hash != (previous or {}).get("candidate_snapshot_hash"),
        "universe": dict(universe),
        "mapping_stats": dict(mapping_stats),
        "stats": {
            "snapshot_count": len(ordered_all),
            "short_squeeze_count": len(short_symbols),
            "high_leverage_count": len(leverage_symbols),
            "dual_match_count": len(dual_symbols),
            "merged_candidate_count": len(merged),
        },
        "short_squeeze_symbols": short_symbols,
        "high_leverage_symbols": leverage_symbols,
        "dual_match_symbols": dual_symbols,
        "candidate_symbols": merged,
        "delta": delta,
        "mappings": serialized_mappings,
        "snapshots": [item.to_dict() for item in ordered_all],
        "data_sources": dict(data_sources or {}),
        "diagnostics": dict(diagnostics or {}),
    })


class CandidatePoolStore:
    def __init__(self, path: Path, *, data_dir: Path | None = None) -> None:
        self.path = Path(path)
        self.store = JsonStore(data_dir or self.path.parent)

    def load(self) -> dict[str, Any] | None:
        payload = self.store.load(self.path, None)
        if payload is None:
            return None
        if not isinstance(payload, dict):
            raise CandidateStateSchemaError("candidate snapshot is not an object")
        _validate_pool_document(payload)
        return payload

    def save(self, payload: Mapping[str, Any]) -> None:
        _validate_pool_document(payload)
        existing = self.load()
        if existing is not None:
            _validate_candidate_transition(existing, payload)
        self.store.save(self.path, json_safe(dict(payload)))


def _validate_candidate_transition(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
) -> None:
    previous_rows = {
        str(row.get("symbol")): row
        for row in previous.get("snapshots") or []
        if isinstance(row, Mapping) and row.get("symbol")
    }
    current_rows = {
        str(row.get("symbol")): row
        for row in current.get("snapshots") or []
        if isinstance(row, Mapping) and row.get("symbol")
    }
    degraded_symbols: list[str] = []
    for symbol in previous.get("candidate_symbols") or []:
        old_row = previous_rows.get(str(symbol))
        new_row = current_rows.get(str(symbol))
        if old_row is None or new_row is None:
            # A symbol absent from the current active/eligible universe is a
            # genuine scope removal, not a partial market-data downgrade.
            continue
        old_tags = set(old_row.get("candidate_tags") or [])
        required = {
            "market_cap_usd",
            "oi_value_usd",
            "mark_price",
        }
        if SHORT_SQUEEZE_CANDIDATE in old_tags:
            required.add("funding_rate")
        unavailable = (
            set(new_row.get("missing_fields") or [])
            | set(new_row.get("stale_fields") or [])
            | set(new_row.get("invalid_fields") or [])
        )
        mapping_ready = (
            new_row.get("mapping_method") in FORMAL_MAPPING_METHODS
            and new_row.get("mapping_confidence") == "high"
            and isinstance(new_row.get("cmc_id"), int)
            and not isinstance(new_row.get("cmc_id"), bool)
            and int(new_row.get("cmc_id")) > 0
        )
        market_cap = new_row.get("market_cap_usd")
        oi_value = new_row.get("binance_oi_usd")
        mark_price = new_row.get("mark_price")
        funding_rate = new_row.get("funding_rate")
        values_ready = (
            isinstance(market_cap, (int, float))
            and not isinstance(market_cap, bool)
            and market_cap > 0
            and isinstance(oi_value, (int, float))
            and not isinstance(oi_value, bool)
            and oi_value >= 0
            and isinstance(mark_price, (int, float))
            and not isinstance(mark_price, bool)
            and mark_price > 0
            and (
                "funding_rate" not in required
                or (
                    isinstance(funding_rate, (int, float))
                    and not isinstance(funding_rate, bool)
                )
            )
        )
        if not mapping_ready or not values_ready or unavailable & required:
            degraded_symbols.append(str(symbol))
    if degraded_symbols:
        raise CandidateStatePartialUpdateError(
            "partial candidate refresh would replace complete state "
            f"(count={len(degraded_symbols)})"
        )


def _validate_pool_document(payload: Mapping[str, Any]) -> None:
    if payload.get("schema_version") != SCHEMA_VERSION or payload.get("module") != MODULE_ID:
        raise CandidateStateSchemaError("unsupported candidate snapshot schema")
    if payload.get("rules_version") != RULES_VERSION:
        raise CandidateStateSchemaError("unsupported candidate rules version")
    required_mappings = (
        "universe",
        "mapping_stats",
        "stats",
        "delta",
        "data_sources",
        "diagnostics",
        "rule_parameters",
    )
    required_lists = (
        "short_squeeze_symbols",
        "high_leverage_symbols",
        "dual_match_symbols",
        "candidate_symbols",
        "mappings",
        "snapshots",
    )
    if not isinstance(payload.get("generated_at"), str) or not payload.get("generated_at"):
        raise CandidateStateSchemaError("candidate snapshot timestamp is invalid")
    for key in ("candidate_pool_hash", "candidate_snapshot_hash"):
        value = payload.get(key)
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise CandidateStateSchemaError(f"{key} is invalid")
    if any(not isinstance(payload.get(key), Mapping) for key in required_mappings):
        raise CandidateStateSchemaError("candidate snapshot mappings are incomplete")
    if any(not isinstance(payload.get(key), list) for key in required_lists):
        raise CandidateStateSchemaError("candidate snapshot rows are invalid")
    try:
        json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise CandidateStateSchemaError("candidate snapshot is not JSON-safe") from exc
    rule_parameters = _canonical_rule_parameters(payload.get("rule_parameters"))
    if (
        rule_parameters["market_cap_max_usd"] <= 0
        or not 0 <= rule_parameters["short_squeeze_min_ratio"] <= 100
        or not -1 <= rule_parameters["short_squeeze_max_funding_rate"] <= 1
        or not 0 <= rule_parameters["high_leverage_min_ratio"] <= 100
    ):
        raise CandidateStateSchemaError("candidate rule parameter is out of range")
    if payload.get("rules_fingerprint") != rules_fingerprint(rule_parameters):
        raise CandidateStateSchemaError("candidate rule fingerprint is inconsistent")
    symbols = payload.get("candidate_symbols") or []
    if any(not isinstance(value, str) or not value for value in symbols) or len(symbols) != len(set(symbols)):
        raise CandidateStateSchemaError("candidate symbols are invalid")
    parsed_snapshots: list[CandidateSnapshot] = []
    snapshot_symbols: set[str] = set()
    for snapshot in payload.get("snapshots") or []:
        if (
            not isinstance(snapshot, Mapping)
            or snapshot.get("schema_version") != SCHEMA_VERSION
            or not isinstance(snapshot.get("symbol"), str)
            or not snapshot.get("symbol")
        ):
            raise CandidateStateSchemaError("candidate snapshot row is invalid")
        if (
            not isinstance(snapshot.get("candidate_tags"), list)
            or not isinstance(snapshot.get("matched_rules"), list)
            or any(not isinstance(value, str) for value in snapshot.get("candidate_tags") or [])
            or any(not isinstance(value, str) for value in snapshot.get("matched_rules") or [])
        ):
            raise CandidateStateSchemaError("candidate snapshot tags are invalid")
        for field_name in ("missing_fields", "stale_fields", "invalid_fields"):
            values = snapshot.get(field_name)
            if (
                not isinstance(values, list)
                or any(not isinstance(value, str) for value in values)
                or len(values) != len(set(values))
            ):
                raise CandidateStateSchemaError("candidate quality fields are invalid")
        for field_name in (
            "market_cap_usd",
            "open_interest_raw",
            "oi_value_usd",
            "mark_price",
            "funding_rate",
            "oi_market_cap_ratio",
            "binance_oi_usd",
            "binance_oi_market_cap_ratio",
        ):
            value = snapshot.get(field_name)
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not isfinite(float(value))
            ):
                raise CandidateStateSchemaError("candidate numeric field is invalid")
        tags = snapshot.get("candidate_tags") or []
        if (
            len(tags) != len(set(tags))
            or not set(tags).issubset({
                SHORT_SQUEEZE_CANDIDATE,
                HIGH_LEVERAGE_CANDIDATE,
            })
            or snapshot.get("symbol") in snapshot_symbols
        ):
            raise CandidateStateSchemaError("candidate snapshot identity is invalid")
        snapshot_symbols.add(str(snapshot.get("symbol")))
        if (
            snapshot.get("oi_value_usd") != snapshot.get("binance_oi_usd")
            or snapshot.get("oi_market_cap_ratio")
            != snapshot.get("binance_oi_market_cap_ratio")
            or snapshot.get("global_oi_usd") is not None
            or snapshot.get("global_oi_market_cap_ratio") is not None
            or snapshot.get("global_oi_source") is not None
        ):
            raise CandidateStateSchemaError("P1 open-interest scope is inconsistent")
        try:
            parsed_snapshot = CandidateSnapshot(**dict(snapshot))
        except (TypeError, ValueError) as exc:
            raise CandidateStateSchemaError("candidate snapshot row is incomplete") from exc
        expected_ratio = calculate_oi_market_cap_ratio(
            parsed_snapshot.binance_oi_usd,
            parsed_snapshot.market_cap_usd,
        )
        if parsed_snapshot.binance_oi_market_cap_ratio != expected_ratio:
            raise CandidateStateSchemaError("candidate OI ratio is inconsistent")
        if parsed_snapshot.mapping_method == "ambiguous":
            expected_quality = "mapping_conflict"
        elif not (
            parsed_snapshot.mapping_method in FORMAL_MAPPING_METHODS
            and parsed_snapshot.mapping_confidence == "high"
            and isinstance(parsed_snapshot.cmc_id, int)
            and not isinstance(parsed_snapshot.cmc_id, bool)
            and parsed_snapshot.cmc_id > 0
        ):
            expected_quality = "unmapped"
        elif parsed_snapshot.invalid_fields:
            expected_quality = "invalid"
        elif parsed_snapshot.stale_fields:
            expected_quality = "stale"
        elif parsed_snapshot.missing_fields:
            expected_quality = "partial"
        else:
            expected_quality = "complete"
        if parsed_snapshot.data_quality != expected_quality:
            raise CandidateStateSchemaError("candidate data quality is inconsistent")
        try:
            expected_rules = apply_candidate_rules(
                deepcopy(parsed_snapshot),
                CandidateThresholds(
                    market_cap_max_usd=rule_parameters["market_cap_max_usd"],
                    short_squeeze_min_ratio=rule_parameters[
                        "short_squeeze_min_ratio"
                    ],
                    short_squeeze_max_funding_rate=rule_parameters[
                        "short_squeeze_max_funding_rate"
                    ],
                    high_leverage_min_ratio=rule_parameters[
                        "high_leverage_min_ratio"
                    ],
                ),
            )
        except (TypeError, ValueError) as exc:
            raise CandidateStateSchemaError("candidate rule inputs are invalid") from exc
        if (
            parsed_snapshot.candidate_tags != expected_rules.candidate_tags
            or parsed_snapshot.matched_rules != expected_rules.matched_rules
        ):
            raise CandidateStateSchemaError("candidate rule result is inconsistent")
        parsed_snapshots.append(parsed_snapshot)

    mapping_symbols: list[str] = []
    for mapping in payload.get("mappings") or []:
        if not isinstance(mapping, Mapping):
            raise CandidateStateSchemaError("candidate mapping row is invalid")
        symbol = mapping.get("binance_symbol")
        if not isinstance(symbol, str) or not symbol:
            raise CandidateStateSchemaError("candidate mapping identity is invalid")
        mapping_symbols.append(symbol)
    if (
        len(mapping_symbols) != len(set(mapping_symbols))
        or set(mapping_symbols) != snapshot_symbols
    ):
        raise CandidateStateSchemaError("candidate mappings are incomplete")
    universe = payload.get("universe") or {}
    raw_counts = tuple(
        universe.get(key) for key in (
            "loaded_usdt_perpetuals",
            "eligible_altcoin_contracts",
            "excluded_contracts",
        )
    )
    if any(isinstance(value, bool) or not isinstance(value, int) for value in raw_counts):
        raise CandidateStateSchemaError("candidate universe counts are invalid")
    loaded_count, eligible_count, excluded_count = raw_counts
    if (
        eligible_count <= 0
        or eligible_count != len(parsed_snapshots)
        or loaded_count != eligible_count + excluded_count
        or excluded_count < 0
    ):
        raise CandidateStateSchemaError("candidate universe is inconsistent")

    try:
        short = sorted(
            (
                item for item in parsed_snapshots
                if SHORT_SQUEEZE_CANDIDATE in item.candidate_tags
            ),
            key=candidate_sort_key,
        )
        leverage = sorted(
            (
                item for item in parsed_snapshots
                if HIGH_LEVERAGE_CANDIDATE in item.candidate_tags
            ),
            key=candidate_sort_key,
        )
    except (TypeError, ValueError) as exc:
        raise CandidateStateSchemaError("candidate snapshot sort fields are invalid") from exc
    short_symbols = [item.symbol for item in short]
    leverage_symbols = [item.symbol for item in leverage]
    dual_symbols = [
        item.symbol
        for item in sorted(
            (
                item
                for item in parsed_snapshots
                if SHORT_SQUEEZE_CANDIDATE in item.candidate_tags
                and HIGH_LEVERAGE_CANDIDATE in item.candidate_tags
            ),
            key=candidate_sort_key,
        )
    ]
    candidate_symbols = sorted(set(short_symbols) | set(leverage_symbols))
    expected_stats = {
        "snapshot_count": len(parsed_snapshots),
        "short_squeeze_count": len(short_symbols),
        "high_leverage_count": len(leverage_symbols),
        "dual_match_count": len(dual_symbols),
        "merged_candidate_count": len(candidate_symbols),
    }
    expected_lists = {
        "short_squeeze_symbols": short_symbols,
        "high_leverage_symbols": leverage_symbols,
        "dual_match_symbols": dual_symbols,
        "candidate_symbols": candidate_symbols,
    }
    if payload.get("stats") != expected_stats or any(
        payload.get(key) != value for key, value in expected_lists.items()
    ):
        raise CandidateStateSchemaError("candidate snapshot indexes are inconsistent")
    if payload.get("candidate_pool_hash") != candidate_pool_hash(
        parsed_snapshots,
        rule_parameters=rule_parameters,
    ):
        raise CandidateStateSchemaError("candidate pool hash is inconsistent")
    if payload.get("candidate_snapshot_hash") != candidate_snapshot_hash(
        parsed_snapshots,
        rule_parameters=rule_parameters,
    ):
        raise CandidateStateSchemaError("candidate snapshot hash is inconsistent")


__all__ = [
    "CandidatePoolStore",
    "CandidateStateError",
    "CandidateStatePartialUpdateError",
    "CandidateStateSchemaError",
    "MODULE_ID",
    "RULE_PARAMETER_KEYS",
    "build_pool_document",
    "candidate_pool_hash",
    "candidate_snapshot_hash",
    "candidate_sort_key",
    "rules_fingerprint",
]
