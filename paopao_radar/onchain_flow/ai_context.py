from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from .constants import OAR_AI_CONTEXT_SCHEMA_VERSION


MAX_LARGEST_TRANSFERS = 20
MAX_WALLET_GROUPS = 10
MAX_WALLETS_PER_GROUP = 20
MAX_TEXT_LENGTH = 320


def _text(value: object, *, limit: int = MAX_TEXT_LENGTH) -> str:
    return str(value or "").replace("\x00", "")[:limit]


def _integer(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _boolean(value: object) -> bool:
    return bool(value)


def _scalars(
    source: object,
    keys: Iterable[str],
) -> dict[str, object]:
    mapping = source if isinstance(source, dict) else {}
    result: dict[str, object] = {}
    for key in keys:
        value = mapping.get(key)
        if value is None or isinstance(value, (bool, int, float)):
            result[key] = value
        elif isinstance(value, str):
            result[key] = _text(value)
    return result


def _evidence(values: object) -> list[str]:
    if not isinstance(values, list):
        return []
    return sorted({_text(value, limit=240) for value in values if _text(value)})


def _identity(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    return {
        "address": _text(source.get("address"), limit=42).lower(),
        "known": _boolean(source.get("known")),
        "classification_eligible": _boolean(
            source.get("classification_eligible")
        ),
        "entity_name": _text(source.get("entity_name"), limit=120),
        "entity_type": _text(source.get("entity_type"), limit=40),
        "address_type": _text(source.get("address_type"), limit=40),
        "confidence": source.get("confidence"),
    }


def _transfer(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    return {
        "event_id": _text(source.get("event_id"), limit=180),
        "block_time": _integer(source.get("block_time")),
        "tx_hash": _text(source.get("tx_hash"), limit=66).lower(),
        "explorer_url": _text(source.get("explorer_url"), limit=180),
        "from": _identity(source.get("from")),
        "to": _identity(source.get("to")),
        "amount": _text(source.get("amount"), limit=120),
        "amount_usd": (
            None
            if source.get("amount_usd") is None
            else _text(source.get("amount_usd"), limit=120)
        ),
        "flow_type": _text(source.get("flow_type"), limit=40),
    }


def _decimal_sort_value(value: object) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("-1")
    return result if result.is_finite() else Decimal("-1")


def _transfer_sort_key(value: dict[str, object]) -> tuple[Decimal, Decimal, str]:
    usd = (
        _decimal_sort_value(value.get("amount_usd"))
        if value.get("amount_usd") is not None
        else Decimal("-1")
    )
    amount = _decimal_sort_value(value.get("amount"))
    return (-usd, -amount, _text(value.get("event_id")))


def _behavior(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    return {
        **_scalars(
            source,
            (
                "type",
                "label",
                "score",
                "confidence_level",
                "window",
                "score_semantics",
            ),
        ),
        "supporting_evidence": _evidence(source.get("supporting_evidence")),
        "counter_evidence": _evidence(source.get("counter_evidence")),
        "limitations": _evidence(source.get("limitations")),
        "source_event_ids": sorted(
            {
                _text(item, limit=180)
                for item in (source.get("source_event_ids") or [])
                if _text(item)
            }
        )[:50],
    }


def _wallet_group(value: object) -> dict[str, object]:
    source = value if isinstance(value, dict) else {}
    wallets = sorted(
        {
            _text(item, limit=42).lower()
            for item in (source.get("wallets") or [])
            if _text(item)
        }
    )[:MAX_WALLETS_PER_GROUP]
    return {
        **_scalars(
            source,
            (
                "group_id",
                "group_type",
                "window",
                "score",
                "level",
                "algorithm_version",
                "score_semantics",
            ),
        ),
        "wallets": wallets,
        "supporting_evidence": _evidence(source.get("supporting_evidence")),
        "counter_evidence": _evidence(source.get("counter_evidence")),
        "limitations": _evidence(source.get("limitations")),
        "source_event_ids": sorted(
            {
                _text(item, limit=180)
                for item in (source.get("source_event_ids") or [])
                if _text(item)
            }
        )[:50],
    }


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def build_ai_context(
    payload: dict[str, object],
    *,
    max_chars: int,
) -> dict[str, object]:
    token = payload.get("token")
    query = payload.get("query")
    summary = payload.get("summary")
    analysis = payload.get("analysis")
    token_map = token if isinstance(token, dict) else {}
    query_map = query if isinstance(query, dict) else {}
    analysis_map = analysis if isinstance(analysis, dict) else {}
    windows = analysis_map.get("windows")
    windows_map = windows if isinstance(windows, dict) else {}
    query_window = _text(query_map.get("window"), limit=8)
    window_summary = windows_map.get(query_window)
    cex_flows = _scalars(
        window_summary,
        (
            "inflow_count",
            "outflow_count",
            "internal_count",
            "cex_consolidation_count",
            "cross_cex_count",
            "gross_cex_inflow_token",
            "gross_cex_outflow_token",
            "net_cex_flow_token",
            "gross_cex_inflow_usd",
            "gross_cex_outflow_usd",
            "net_cex_flow_usd",
        ),
    )
    candidates = [
        _behavior(item)
        for item in (analysis_map.get("behavior_candidates") or [])
        if isinstance(item, dict)
    ]
    candidates.sort(
        key=lambda item: (
            -_integer(item.get("score")),
            _text(item.get("type")),
        )
    )
    groups = [
        _wallet_group(item)
        for item in (analysis_map.get("wallet_groups") or [])
        if isinstance(item, dict)
    ]
    groups.sort(
        key=lambda item: (
            -_integer(item.get("score")),
            _text(item.get("group_type")),
            _text(item.get("group_id")),
        )
    )
    groups = groups[:MAX_WALLET_GROUPS]
    primary = _behavior(analysis_map.get("primary_behavior"))
    supporting = set(primary["supporting_evidence"])
    counter = set(primary["counter_evidence"])
    limitations = set(_evidence(analysis_map.get("limitations")))
    for candidate in candidates:
        supporting.update(candidate["supporting_evidence"])
        counter.update(candidate["counter_evidence"])
        limitations.update(candidate["limitations"])
    for group in groups:
        supporting.update(group["supporting_evidence"])
        counter.update(group["counter_evidence"])
        limitations.update(group["limitations"])
    if not bool(payload.get("complete")):
        limitations.add("query_incomplete")
    if not bool(analysis_map.get("complete")):
        limitations.add("analysis_incomplete")

    context: dict[str, object] = {
        "schema_version": OAR_AI_CONTEXT_SCHEMA_VERSION,
        "token": {
            "chain": _text(query_map.get("chain"), limit=20),
            "chain_id": _integer(query_map.get("chain_id")),
            "contract": _text(
                query_map.get("contract") or token_map.get("contract"),
                limit=42,
            ).lower(),
            "symbol": _text(token_map.get("symbol"), limit=40),
            "name": _text(token_map.get("name"), limit=120),
            "decimals": _integer(token_map.get("decimals")),
        },
        "query": {
            "window": query_window,
            "from_time": _integer(query_map.get("from_time")),
            "to_time": _integer(query_map.get("to_time")),
            "complete": _boolean(payload.get("complete")),
            "truncated": _boolean(payload.get("truncated")),
            "truncation_reason": (
                None
                if payload.get("truncation_reason") is None
                else _text(payload.get("truncation_reason"), limit=80)
            ),
        },
        "transfer_summary": _scalars(
            summary,
            (
                "transfer_count",
                "mint_count",
                "burn_count",
                "non_cex_count",
                "unclassified_count",
                "inflow_count",
                "outflow_count",
                "internal_count",
                "consolidation_count",
                "cross_cex_count",
                "unique_senders",
                "unique_receivers",
                "total_token_amount",
                "total_usd",
                "unpriced_transfer_count",
            ),
        ),
        "largest_transfers": sorted([
            _transfer(item)
            for item in (payload.get("largest_transfers") or [])
            if isinstance(item, dict)
        ], key=_transfer_sort_key)[:MAX_LARGEST_TRANSFERS],
        "cex_flows": cex_flows,
        "primary_behavior": primary,
        "behavior_candidates": candidates,
        "wallet_groups": groups,
        "supporting_evidence": sorted(supporting),
        "counter_evidence": sorted(counter),
        "data_limitations": sorted(limitations),
    }
    while len(_canonical_json(context)) > max_chars:
        if context["largest_transfers"]:
            context["largest_transfers"].pop()
            limitations.add("ai_context_truncated")
            context["data_limitations"] = sorted(limitations)
            continue
        if context["wallet_groups"]:
            context["wallet_groups"].pop()
            limitations.add("ai_context_truncated")
            context["data_limitations"] = sorted(limitations)
            continue
        if context["behavior_candidates"]:
            context["behavior_candidates"].pop()
            limitations.add("ai_context_truncated")
            context["data_limitations"] = sorted(limitations)
            continue
        raise ValueError("AI context exceeds the configured safe size")
    digest = hashlib.sha256(
        _canonical_json(context).encode("utf-8")
    ).hexdigest()
    return {"context_hash": digest, **context}
