from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Iterable

from .config import OnchainSettings
from .constants import ZERO_ADDRESS
from .token_activity import WINDOW_SECONDS


WINDOW_ORDER = ("15m", "1h", "4h", "24h")
BEHAVIOR_ORDER = (
    "distribution_candidate",
    "accumulation_candidate",
    "wallet_consolidation_candidate",
    "fanout_candidate",
    "isolated",
)
BEHAVIOR_LABELS = {
    "no_activity": "未发现近期活动",
    "isolated": "偶发行为",
    "accumulation_candidate": "持续吸筹候选",
    "distribution_candidate": "持续派发候选",
    "wallet_consolidation_candidate": "多钱包归集候选",
    "fanout_candidate": "批量分发候选",
    "insufficient_data": "数据不足",
}
SCORE_SEMANTICS = "rule_score_not_probability"
BASE_LIMITATIONS = (
    "gas_funding_not_analyzed",
    "contract_role_unknown",
    "dex_path_not_analyzed",
    "bridge_path_not_analyzed",
    "ownership_not_confirmed",
)


def decimal_value(value: object) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("analysis amount must be a finite decimal") from exc
    if not result.is_finite():
        raise ValueError("analysis amount must be a finite decimal")
    return result


def decimal_string(value: Decimal | None) -> str | None:
    if value is None:
        return None
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def address_of(record: dict[str, object], side: str) -> str:
    payload = record.get(side)
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("address") or "").lower()


def identity_of(
    record: dict[str, object], side: str
) -> dict[str, object]:
    payload = record.get(side)
    return payload if isinstance(payload, dict) else {}


def is_classification_cex(
    record: dict[str, object], side: str
) -> bool:
    payload = identity_of(record, side)
    return bool(
        payload.get("classification_eligible")
        and str(payload.get("entity_type") or "") == "cex"
    )


def event_ids(
    records: Iterable[dict[str, object]], limit: int
) -> tuple[list[str], bool]:
    values = sorted(
        {
            str(record.get("event_id") or "")
            for record in records
            if str(record.get("event_id") or "")
        }
    )
    return values[:limit], len(values) > limit


@dataclass(frozen=True)
class WindowFacts:
    name: str
    seconds: int
    window_start: int
    window_end: int
    records: tuple[dict[str, object], ...]
    relevant: tuple[dict[str, object], ...]

    def records_for(self, flow_type: str) -> tuple[dict[str, object], ...]:
        return tuple(
            record
            for record in self.relevant
            if str(record.get("flow_type") or "") == flow_type
        )

    def amount_for(self, flow_type: str) -> Decimal:
        return sum(
            (
                decimal_value(record.get("amount") or "0")
                for record in self.records_for(flow_type)
            ),
            Decimal("0"),
        )

    def active_buckets(
        self, records: Iterable[dict[str, object]] | None = None
    ) -> int:
        source = self.relevant if records is None else tuple(records)
        return len(
            {
                int(record.get("block_time") or 0) // WINDOW_SECONDS["15m"]
                for record in source
            }
        )

    def public(self) -> dict[str, object]:
        flow_names = (
            "mint",
            "burn",
            "inflow",
            "outflow",
            "internal",
            "consolidation",
            "cross_cex",
            "non_cex",
            "unclassified",
        )
        counts = Counter(
            str(record.get("flow_type") or "")
            for record in self.relevant
        )
        inflow = self.amount_for("inflow")
        outflow = self.amount_for("outflow")
        directional_count = counts["inflow"] + counts["outflow"]
        dominance = (
            Decimal(max(counts["inflow"], counts["outflow"]))
            / Decimal(directional_count)
            if directional_count
            else Decimal("0")
        )
        total_amount = sum(
            (
                decimal_value(record.get("amount") or "0")
                for record in self.relevant
            ),
            Decimal("0"),
        )
        priced_records = tuple(
            record
            for record in self.relevant
            if record.get("amount_usd") is not None
        )
        gross_inflow_usd = sum(
            (
                decimal_value(record["amount_usd"])
                for record in priced_records
                if record.get("flow_type") == "inflow"
            ),
            Decimal("0"),
        )
        gross_outflow_usd = sum(
            (
                decimal_value(record["amount_usd"])
                for record in priced_records
                if record.get("flow_type") == "outflow"
            ),
            Decimal("0"),
        )
        return {
            "window_start": self.window_start,
            "window_end": self.window_end,
            "transfer_count": len(self.records),
            "relevant_transfer_count": len(self.relevant),
            **{
                (
                    "cex_consolidation_count"
                    if name == "consolidation"
                    else f"{name}_count"
                ): counts[name]
                for name in flow_names
            },
            "unique_senders": len(
                {
                    address_of(record, "from")
                    for record in self.relevant
                    if address_of(record, "from")
                }
            ),
            "unique_receivers": len(
                {
                    address_of(record, "to")
                    for record in self.relevant
                    if address_of(record, "to")
                }
            ),
            "active_15m_buckets": self.active_buckets(),
            "total_token_amount": decimal_string(total_amount),
            "gross_cex_inflow_token": decimal_string(inflow),
            "gross_cex_outflow_token": decimal_string(outflow),
            "net_cex_flow_token": decimal_string(inflow - outflow),
            "directional_dominance": decimal_string(dominance),
            "priced_transfer_count": len(priced_records),
            "unpriced_transfer_count": len(self.relevant)
            - len(priced_records),
            "gross_cex_inflow_usd": (
                decimal_string(gross_inflow_usd)
                if priced_records
                else None
            ),
            "gross_cex_outflow_usd": (
                decimal_string(gross_outflow_usd)
                if priced_records
                else None
            ),
            "net_cex_flow_usd": (
                decimal_string(gross_inflow_usd - gross_outflow_usd)
                if priced_records
                else None
            ),
        }


def build_nested_windows(
    activity: dict[str, object],
) -> tuple[WindowFacts, ...]:
    query = activity.get("query")
    if not isinstance(query, dict):
        raise ValueError("activity query is missing")
    query_window = str(query.get("window") or "")
    if query_window not in WINDOW_ORDER:
        raise ValueError("activity query window is invalid")
    to_time = int(query.get("to_time") or 0)
    if to_time <= 0:
        raise ValueError("activity query to_time is invalid")
    max_index = WINDOW_ORDER.index(query_window)
    transfers = activity.get("transfers")
    if not isinstance(transfers, list):
        raise ValueError("activity transfers are missing")
    ordered_records = tuple(
        sorted(
            (
                record
                for record in transfers
                if isinstance(record, dict)
            ),
            key=lambda record: (
                int(record.get("block_time") or 0),
                str(record.get("event_id") or ""),
            ),
        )
    )
    windows: list[WindowFacts] = []
    for name in WINDOW_ORDER[: max_index + 1]:
        seconds = WINDOW_SECONDS[name]
        start = to_time - seconds
        records = tuple(
            record
            for record in ordered_records
            if start <= int(record.get("block_time") or 0) <= to_time
        )
        relevant = tuple(
            record
            for record in records
            if decimal_value(record.get("amount") or "0") > 0
        )
        windows.append(
            WindowFacts(
                name=name,
                seconds=seconds,
                window_start=start,
                window_end=to_time,
                records=records,
                relevant=relevant,
            )
        )
    return tuple(windows)


class BehaviorAnalyzer:
    def __init__(self, settings: OnchainSettings):
        self.settings = settings

    def analyze(self, activity: dict[str, object]) -> dict[str, object]:
        windows = build_nested_windows(activity)
        input_complete = bool(activity.get("complete"))
        labels = activity.get("labels")
        labels_status = (
            str(labels.get("status") or "")
            if isinstance(labels, dict)
            else ""
        )
        limitations = list(BASE_LIMITATIONS)
        price = activity.get("price")
        price_status = (
            str(price.get("status") or "")
            if isinstance(price, dict)
            else ""
        )
        valuation_basis = (
            "current_usd_estimate"
            if price_status == "available"
            else "token_amount"
        )
        if price_status in {"missing", "failed"}:
            limitations.append("price_unavailable")

        observed = self._observed_patterns(windows)
        if not input_complete:
            limitations.append("query_incomplete")
            return {
                "status": "partial_input",
                "complete": False,
                "input_complete": False,
                "valuation_basis": valuation_basis,
                "windows": {
                    window.name: window.public() for window in windows
                },
                "primary_behavior": self._primary(
                    "insufficient_data", window=str(
                        activity.get("query", {}).get("window", "")
                    )
                ),
                "behavior_candidates": [],
                "coexisting_behavior_types": [],
                "observed_patterns": observed,
                "limitations": sorted(set(limitations)),
            }

        if not any(window.relevant for window in windows):
            return {
                "status": "no_activity",
                "complete": True,
                "input_complete": True,
                "valuation_basis": valuation_basis,
                "windows": {
                    window.name: window.public() for window in windows
                },
                "primary_behavior": self._primary(
                    "no_activity", window=windows[-1].name
                ),
                "behavior_candidates": [],
                "coexisting_behavior_types": [],
                "observed_patterns": observed,
                "limitations": sorted(set(limitations)),
            }

        candidates: list[dict[str, object]] = []
        for direction in ("inflow", "outflow"):
            candidate = self._direction_candidate(
                windows,
                direction=direction,
                labels_status=labels_status,
            )
            if candidate is not None:
                candidates.append(candidate)
        candidates.extend(
            self._structure_candidates(
                windows,
                behavior_type="wallet_consolidation_candidate",
            )
        )
        candidates.extend(
            self._structure_candidates(
                windows,
                behavior_type="fanout_candidate",
            )
        )
        candidates = self._sort_candidates(candidates)
        if candidates:
            primary = dict(candidates[0])
        else:
            isolated = self._primary(
                "isolated",
                window=windows[-1].name,
                evidence=["no_repeated_behavior_gate_met"],
                source_records=windows[-1].relevant,
            )
            candidates = [isolated]
            primary = dict(isolated)
        return {
            "status": "ok",
            "complete": True,
            "input_complete": True,
            "valuation_basis": valuation_basis,
            "windows": {
                window.name: window.public() for window in windows
            },
            "primary_behavior": primary,
            "behavior_candidates": candidates,
            "coexisting_behavior_types": list(
                dict.fromkeys(
                    str(candidate["type"]) for candidate in candidates
                )
            ),
            "observed_patterns": observed,
            "limitations": sorted(set(limitations)),
        }

    def _direction_candidate(
        self,
        windows: tuple[WindowFacts, ...],
        *,
        direction: str,
        labels_status: str,
    ) -> dict[str, object] | None:
        if labels_status != "ok":
            return None
        opposite = "outflow" if direction == "inflow" else "inflow"
        behavior_type = (
            "distribution_candidate"
            if direction == "inflow"
            else "accumulation_candidate"
        )
        qualified_windows: list[str] = []
        for window in windows:
            if window.seconds < WINDOW_SECONDS["1h"]:
                continue
            direction_records = window.records_for(direction)
            directional_count = len(direction_records) + len(
                window.records_for(opposite)
            )
            dominance = (
                Decimal(len(direction_records)) / Decimal(directional_count)
                if directional_count
                else Decimal("0")
            )
            if (
                len(direction_records) >= self.settings.oar_behavior_min_tx
                and dominance
                >= self.settings.oar_behavior_dominance_min
            ):
                qualified_windows.append(window.name)

        candidates: list[dict[str, object]] = []
        for window in windows:
            if window.seconds < WINDOW_SECONDS["1h"]:
                continue
            direction_records = window.records_for(direction)
            opposite_records = window.records_for(opposite)
            directional_count = len(direction_records) + len(
                opposite_records
            )
            dominance = (
                Decimal(len(direction_records)) / Decimal(directional_count)
                if directional_count
                else Decimal("0")
            )
            counterpart_side = "from" if direction == "inflow" else "to"
            counterparties = [
                address_of(record, counterpart_side)
                for record in direction_records
                if address_of(record, counterpart_side)
            ]
            counterparty_counts = Counter(counterparties)
            counterparty_gate = (
                len(counterparty_counts) >= 2
                or max(counterparty_counts.values(), default=0) >= 3
            )
            minimum_buckets = (
                self.settings.oar_behavior_min_active_buckets_1h
                if window.name == "1h"
                else self.settings.oar_behavior_min_active_buckets_long
            )
            active_buckets = window.active_buckets(direction_records)
            if not (
                len(direction_records) >= self.settings.oar_behavior_min_tx
                and dominance
                >= self.settings.oar_behavior_dominance_min
                and counterparty_gate
                and active_buckets >= minimum_buckets
            ):
                continue

            supporting = [
                f"{direction}_dominance_met",
                f"{direction}_transaction_count_met",
                "multiple_15m_buckets",
                "multiple_or_repeated_counterparties",
            ]
            score = 25 + 20 + 20 + 15
            nested_direction = [
                name
                for name in qualified_windows
                if WINDOW_SECONDS[name] <= window.seconds
            ]
            if len(nested_direction) >= 2:
                score += 10
                supporting.append("direction_repeated_across_nested_windows")
            non_mint_burn_amount = sum(
                (
                    decimal_value(record.get("amount") or "0")
                    for record in window.relevant
                    if record.get("flow_type") not in {"mint", "burn"}
                ),
                Decimal("0"),
            )
            direction_amount = sum(
                (
                    decimal_value(record.get("amount") or "0")
                    for record in direction_records
                ),
                Decimal("0"),
            )
            amount_share = (
                direction_amount / non_mint_burn_amount
                if non_mint_burn_amount > 0
                else Decimal("0")
            )
            if amount_share >= Decimal("0.20"):
                score += 10
                supporting.append("direction_token_amount_share_met")

            counter_evidence: list[str] = []
            opposite_amount = sum(
                (
                    decimal_value(record.get("amount") or "0")
                    for record in opposite_records
                ),
                Decimal("0"),
            )
            directional_gross = direction_amount + opposite_amount
            if (
                directional_gross > 0
                and opposite_amount / directional_gross >= Decimal("0.40")
            ):
                score -= 25
                counter_evidence.append("opposite_cex_flow_material")
            internal_count = sum(
                len(window.records_for(flow_type))
                for flow_type in (
                    "internal",
                    "cross_cex",
                    "consolidation",
                )
            )
            cex_touching = directional_count + internal_count
            if (
                cex_touching > 0
                and Decimal(internal_count) / Decimal(cex_touching)
                >= Decimal("0.50")
            ):
                score -= 20
                counter_evidence.append("cex_internal_activity_dominant")
            score = max(0, min(100, score))
            if score < 55:
                continue
            identifiers, identifiers_truncated = event_ids(
                direction_records,
                self.settings.oar_max_source_event_ids,
            )
            limitations: list[str] = []
            if identifiers_truncated:
                limitations.append("source_events_truncated")
            candidates.append(
                {
                    "type": behavior_type,
                    "label": BEHAVIOR_LABELS[behavior_type],
                    "score": score,
                    "confidence_level": self._confidence(score),
                    "window": window.name,
                    "persistence": "multi_bucket",
                    "supporting_evidence": supporting,
                    "counter_evidence": counter_evidence,
                    "limitations": limitations,
                    "source_event_ids": identifiers,
                    "source_events_truncated": identifiers_truncated,
                    "score_semantics": SCORE_SEMANTICS,
                }
            )
        return self._sort_candidates(candidates)[0] if candidates else None

    def _structure_candidates(
        self,
        windows: tuple[WindowFacts, ...],
        *,
        behavior_type: str,
    ) -> list[dict[str, object]]:
        incoming = behavior_type == "wallet_consolidation_candidate"
        anchor_side = "to" if incoming else "from"
        member_side = "from" if incoming else "to"
        qualifying: list[
            tuple[WindowFacts, str, tuple[dict[str, object], ...], set[str]]
        ] = []
        for window in windows:
            groups: dict[str, list[dict[str, object]]] = defaultdict(list)
            for record in window.relevant:
                if record.get("flow_type") in {"mint", "burn"}:
                    continue
                anchor = address_of(record, anchor_side)
                member = address_of(record, member_side)
                if (
                    not anchor
                    or not member
                    or anchor == ZERO_ADDRESS
                    or is_classification_cex(record, anchor_side)
                ):
                    continue
                groups[anchor].append(record)
            total_amount = sum(
                (
                    decimal_value(record.get("amount") or "0")
                    for record in window.relevant
                    if record.get("flow_type") not in {"mint", "burn"}
                ),
                Decimal("0"),
            )
            for anchor, records in groups.items():
                members = {
                    address_of(record, member_side)
                    for record in records
                    if address_of(record, member_side)
                }
                group_amount = sum(
                    (
                        decimal_value(record.get("amount") or "0")
                        for record in records
                    ),
                    Decimal("0"),
                )
                share = (
                    group_amount / total_amount
                    if total_amount > 0
                    else Decimal("0")
                )
                if (
                    len(members) >= self.settings.oar_pattern_min_wallets
                    and len(records) >= self.settings.oar_pattern_min_tx
                    and share >= self.settings.oar_pattern_min_amount_share
                ):
                    qualifying.append(
                        (window, anchor, tuple(records), members)
                    )

        signatures = Counter(
            (
                behavior_type,
                anchor,
                tuple(sorted(members)),
            )
            for _, anchor, _, members in qualifying
        )
        best: dict[tuple[str, str], dict[str, object]] = {}
        for window, anchor, records, members in qualifying:
            supporting = [
                "wallet_count_met",
                "transaction_count_met",
                "token_amount_share_met",
            ]
            score = 30 + 20 + 20
            if window.active_buckets(records) >= 2:
                score += 15
                supporting.append("multiple_15m_buckets")
            signature = (
                behavior_type,
                anchor,
                tuple(sorted(members)),
            )
            if signatures[signature] >= 2:
                score += 15
                supporting.append("repeated_across_nested_windows")
            anchor_identity = identity_of(records[0], anchor_side)
            limitations: list[str] = []
            if not anchor_identity.get("known"):
                limitations.append(
                    "target_role_unknown"
                    if incoming
                    else "sender_role_unknown"
                )
            if len(members) > 20:
                limitations.append("batch_or_airdrop_possible")
            identifiers, identifiers_truncated = event_ids(
                records,
                self.settings.oar_max_source_event_ids,
            )
            if identifiers_truncated:
                limitations.append("source_events_truncated")
            candidate = {
                "type": behavior_type,
                "label": BEHAVIOR_LABELS[behavior_type],
                "score": min(100, score),
                "confidence_level": self._confidence(min(100, score)),
                "window": window.name,
                "persistence": (
                    "multi_bucket"
                    if window.active_buckets(records) >= 2
                    else "single_window"
                ),
                "anchor_address": anchor,
                "wallets": sorted(members),
                "supporting_evidence": supporting,
                "counter_evidence": [],
                "limitations": sorted(set(limitations)),
                "source_event_ids": identifiers,
                "source_events_truncated": identifiers_truncated,
                "score_semantics": SCORE_SEMANTICS,
            }
            key = (behavior_type, anchor)
            existing = best.get(key)
            if existing is None or self._candidate_key(
                candidate
            ) < self._candidate_key(existing):
                best[key] = candidate
        return self._sort_candidates(list(best.values()))

    @staticmethod
    def _observed_patterns(
        windows: tuple[WindowFacts, ...],
    ) -> list[dict[str, object]]:
        observed: list[dict[str, object]] = []
        for window in windows:
            counts = Counter(
                str(record.get("flow_type") or "")
                for record in window.relevant
            )
            for flow_type in ("inflow", "outflow"):
                if counts[flow_type]:
                    observed.append(
                        {
                            "type": f"cex_{flow_type}_observed",
                            "window": window.name,
                            "event_count": counts[flow_type],
                        }
                    )
        return observed

    def _primary(
        self,
        behavior_type: str,
        *,
        window: str,
        evidence: list[str] | None = None,
        source_records: Iterable[dict[str, object]] = (),
    ) -> dict[str, object]:
        identifiers, truncated = event_ids(
            source_records, self.settings.oar_max_source_event_ids
        )
        return {
            "type": behavior_type,
            "label": BEHAVIOR_LABELS[behavior_type],
            "score": 0,
            "confidence_level": "low",
            "window": window,
            "persistence": "single_window",
            "supporting_evidence": list(evidence or []),
            "counter_evidence": [],
            "limitations": (
                ["source_events_truncated"] if truncated else []
            ),
            "source_event_ids": identifiers,
            "source_events_truncated": truncated,
            "score_semantics": SCORE_SEMANTICS,
        }

    @staticmethod
    def _confidence(score: int) -> str:
        if score >= 85:
            return "high"
        if score >= 70:
            return "medium"
        return "low"

    @staticmethod
    def _candidate_key(
        candidate: dict[str, object],
    ) -> tuple[int, int, int, str, str]:
        behavior_type = str(candidate.get("type") or "")
        order = (
            BEHAVIOR_ORDER.index(behavior_type)
            if behavior_type in BEHAVIOR_ORDER
            else len(BEHAVIOR_ORDER)
        )
        return (
            -int(candidate.get("score") or 0),
            -len(candidate.get("supporting_evidence") or []),
            order,
            str(candidate.get("window") or ""),
            str(candidate.get("anchor_address") or ""),
        )

    @classmethod
    def _sort_candidates(
        cls, candidates: list[dict[str, object]]
    ) -> list[dict[str, object]]:
        return sorted(candidates, key=cls._candidate_key)
