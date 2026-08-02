from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Mapping, Sequence


CLASSIFIED_FLOW_TYPES = frozenset(
    {
        "non_cex",
        "inflow",
        "outflow",
        "internal",
        "consolidation",
        "cross_cex",
    }
)
CEX_CLASSIFIED_FLOW_TYPES = frozenset(
    {
        "inflow",
        "outflow",
        "internal",
        "consolidation",
        "cross_cex",
    }
)
CEX_DIRECTION_FLOW_TYPES = frozenset({"inflow", "outflow"})
OUT_OF_SCOPE_FLOW_TYPES = frozenset({"mint", "burn"})
OBSERVED_COVERAGE_STATUSES = frozenset(
    {"not_applicable", "none", "partial", "complete", "unknown"}
)


def _decimal(value: object) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")
    if not result.is_finite() or result < 0:
        return Decimal("0")
    return result


def _decimal_string(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _nonnegative_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _bounded_bps(value: object) -> int | None:
    if value is None:
        return None
    try:
        return max(0, min(10_000, int(value)))
    except (TypeError, ValueError):
        return None


def _coverage_bps(numerator: Decimal, denominator: Decimal) -> int | None:
    if denominator <= 0:
        return None
    return min(10_000, max(0, int(numerator * 10_000 / denominator)))


def summarize_classification_coverage(
    records: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    scope_count = 0
    classified_count = 0
    unclassified_count = 0
    cex_classified_count = 0
    cex_direction_count = 0
    scope_amount = Decimal("0")
    classified_amount = Decimal("0")
    unclassified_amount = Decimal("0")
    cex_classified_amount = Decimal("0")
    cex_direction_amount = Decimal("0")

    for record in records:
        flow_type = str(record.get("flow_type") or "")
        if flow_type in OUT_OF_SCOPE_FLOW_TYPES:
            continue
        amount = _decimal(record.get("amount"))
        scope_count += 1
        scope_amount += amount
        if flow_type in CLASSIFIED_FLOW_TYPES:
            classified_count += 1
            classified_amount += amount
        else:
            unclassified_count += 1
            unclassified_amount += amount
        if flow_type in CEX_CLASSIFIED_FLOW_TYPES:
            cex_classified_count += 1
            cex_classified_amount += amount
        if flow_type in CEX_DIRECTION_FLOW_TYPES:
            cex_direction_count += 1
            cex_direction_amount += amount

    if scope_count == 0:
        status = "not_applicable"
    elif classified_count == 0:
        status = "none"
    elif classified_count == scope_count:
        status = "complete"
    else:
        status = "partial"

    return {
        "classification_scope_transfer_count": scope_count,
        "classified_transfer_count": classified_count,
        "unclassified_transfer_count": unclassified_count,
        "cex_classified_transfer_count": cex_classified_count,
        "cex_direction_transfer_count": cex_direction_count,
        "classification_scope_token_amount": _decimal_string(scope_amount),
        "classified_token_amount": _decimal_string(classified_amount),
        "unclassified_token_amount": _decimal_string(unclassified_amount),
        "cex_classified_token_amount": _decimal_string(
            cex_classified_amount
        ),
        "cex_direction_token_amount": _decimal_string(cex_direction_amount),
        "classification_transfer_coverage_bps": _coverage_bps(
            Decimal(classified_count), Decimal(scope_count)
        ),
        "classification_amount_coverage_bps": _coverage_bps(
            classified_amount, scope_amount
        ),
        "classification_coverage_status": status,
    }


def label_coverage_snapshot(
    summary: Mapping[str, object],
    labels: Mapping[str, object],
) -> dict[str, object]:
    observed_status = str(
        summary.get("classification_coverage_status") or "unknown"
    )
    if observed_status not in OBSERVED_COVERAGE_STATUSES:
        observed_status = "unknown"
    scope_count = _nonnegative_int(
        summary.get("classification_scope_transfer_count")
    )
    classified_count = _nonnegative_int(
        summary.get("classified_transfer_count")
    )
    unclassified_count = _nonnegative_int(
        summary.get("unclassified_transfer_count")
        if summary.get("unclassified_transfer_count") is not None
        else summary.get("unclassified_count")
    )
    cex_direction_count = _nonnegative_int(
        summary.get("cex_direction_transfer_count")
    )
    cex_classified_count = _nonnegative_int(
        summary.get("cex_classified_transfer_count")
    )
    transfer_bps = summary.get("classification_transfer_coverage_bps")
    amount_bps = summary.get("classification_amount_coverage_bps")
    return {
        # Keep status as the registry status for report-schema compatibility.
        "status": str(labels.get("status") or "missing"),
        "registry_status": str(labels.get("status") or "missing"),
        "identity_label_count": _nonnegative_int(
            labels.get("identity_label_count")
        ),
        "classification_eligible_cex_count": _nonnegative_int(
            labels.get("classification_eligible_cex_count")
        ),
        "observed_status": observed_status,
        "classification_scope_transfer_count": scope_count,
        "classified_transfer_count": classified_count,
        "unclassified_transfer_count": unclassified_count,
        "cex_classified_transfer_count": cex_classified_count,
        "cex_direction_transfer_count": cex_direction_count,
        "classification_transfer_coverage_bps": _bounded_bps(transfer_bps),
        "classification_amount_coverage_bps": _bounded_bps(amount_bps),
        "classification_scope_token_amount": str(
            summary.get("classification_scope_token_amount") or "0"
        ),
        "classified_token_amount": str(
            summary.get("classified_token_amount") or "0"
        ),
        "unclassified_token_amount": str(
            summary.get("unclassified_token_amount") or "0"
        ),
        "cex_classified_token_amount": str(
            summary.get("cex_classified_token_amount") or "0"
        ),
        "cex_direction_token_amount": str(
            summary.get("cex_direction_token_amount") or "0"
        ),
        "cex_direction_observed": cex_direction_count > 0,
    }
