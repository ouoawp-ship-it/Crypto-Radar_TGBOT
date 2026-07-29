from __future__ import annotations

import hashlib
from collections import Counter, defaultdict, deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from .behavior import (
    WindowFacts,
    address_of,
    build_nested_windows,
    decimal_value,
    event_ids,
    identity_of,
    is_classification_cex,
)
from .config import OnchainSettings
from .constants import (
    OAR_WALLET_GROUP_ALGORITHM_VERSION,
    ZERO_ADDRESS,
)


SCORE_SEMANTICS = "rule_score_not_probability"
GROUP_LEVELS = (
    (80, "强关联候选"),
    (60, "高概率关联"),
    (40, "中等概率关联"),
    (20, "弱关联"),
    (0, "证据不足"),
)


@dataclass(frozen=True)
class GroupSeed:
    group_type: str
    window: str
    wallets: tuple[str, ...]
    records: tuple[dict[str, object], ...]
    anchor: str = ""
    exchange: str = ""
    synchronized: bool = False
    anchor_known: bool = False

    @property
    def signature(self) -> tuple[str, tuple[str, ...], str, str]:
        return (
            self.group_type,
            self.wallets,
            self.anchor,
            self.exchange,
        )


class WalletGroupAnalyzer:
    def __init__(self, settings: OnchainSettings):
        self.settings = settings

    def analyze(
        self,
        activity: dict[str, object],
        *,
        windows: tuple[WindowFacts, ...] | None = None,
    ) -> dict[str, object]:
        windows = windows or build_nested_windows(activity)
        input_complete = bool(activity.get("complete"))
        allowed_wallets, wallet_budget_exhausted = self._wallet_budget(
            windows[-1].relevant
        )
        seeds, seed_budget_exhausted = self._seeds(
            windows, allowed_wallets
        )
        occurrences = Counter(seed.signature for seed in seeds)
        all_records = windows[-1].relevant
        groups = [
            self._score(
                activity,
                seed,
                occurrences=occurrences[seed.signature],
                all_records=all_records,
                input_complete=input_complete,
                analysis_truncated=(
                    wallet_budget_exhausted or seed_budget_exhausted
                ),
            )
            for seed in seeds
        ]
        deduplicated: dict[str, dict[str, object]] = {}
        for group in groups:
            group_id = str(group["group_id"])
            existing = deduplicated.get(group_id)
            if existing is None or self._sort_key(group) < self._sort_key(
                existing
            ):
                deduplicated[group_id] = group
        ordered = sorted(deduplicated.values(), key=self._sort_key)
        groups_truncated = len(ordered) > self.settings.oar_max_wallet_groups
        ordered = ordered[: self.settings.oar_max_wallet_groups]
        truncated = (
            wallet_budget_exhausted
            or seed_budget_exhausted
            or groups_truncated
        )
        if truncated:
            ordered = [self._cap_for_partial_analysis(group) for group in ordered]
        limitations: list[str] = []
        if truncated:
            limitations.append("analysis_budget_exhausted")
        return {
            "complete": not truncated,
            "truncated": truncated,
            "groups": ordered,
            "limitations": limitations,
            "analyzed_wallet_count": len(allowed_wallets),
        }

    @classmethod
    def _cap_for_partial_analysis(
        cls, group: dict[str, object]
    ) -> dict[str, object]:
        capped = dict(group)
        capped["score"] = min(int(capped.get("score") or 0), 59)
        capped["level"] = cls._level(int(capped["score"]))
        limitations = set(capped.get("limitations") or [])
        limitations.add("analysis_budget_exhausted")
        capped["limitations"] = sorted(limitations)
        return capped

    def _wallet_budget(
        self,
        records: Iterable[dict[str, object]],
    ) -> tuple[set[str], bool]:
        wallets: set[str] = set()
        for record in records:
            if record.get("flow_type") in {"mint", "burn"}:
                continue
            for side in ("from", "to"):
                address = address_of(record, side)
                if (
                    address
                    and address != ZERO_ADDRESS
                    and not is_classification_cex(record, side)
                ):
                    wallets.add(address)
        ordered = sorted(wallets)
        limit = self.settings.oar_max_analyzed_wallets
        return set(ordered[:limit]), len(ordered) > limit

    def _seeds(
        self,
        windows: tuple[WindowFacts, ...],
        allowed_wallets: set[str],
    ) -> tuple[list[GroupSeed], bool]:
        seeds: list[GroupSeed] = []
        seed_limit = self.settings.oar_max_wallet_groups * 8
        for window in windows:
            seeds.extend(
                self._shared_seeds(
                    window,
                    allowed_wallets=allowed_wallets,
                    incoming=True,
                )
            )
            seeds.extend(
                self._shared_seeds(
                    window,
                    allowed_wallets=allowed_wallets,
                    incoming=False,
                )
            )
            cex_seeds, exhausted = self._cex_seeds(
                window,
                allowed_wallets=allowed_wallets,
                seed_limit=max(0, seed_limit - len(seeds)),
            )
            seeds.extend(cex_seeds)
            if exhausted or len(seeds) >= seed_limit:
                return seeds[:seed_limit], True
        return seeds, False

    def _shared_seeds(
        self,
        window: WindowFacts,
        *,
        allowed_wallets: set[str],
        incoming: bool,
    ) -> list[GroupSeed]:
        anchor_side = "to" if incoming else "from"
        member_side = "from" if incoming else "to"
        group_type = "shared_target" if incoming else "shared_source"
        grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
        for record in window.relevant:
            if record.get("flow_type") in {"mint", "burn"}:
                continue
            anchor = address_of(record, anchor_side)
            member = address_of(record, member_side)
            if (
                not anchor
                or not member
                or anchor == ZERO_ADDRESS
                or anchor not in allowed_wallets
                or member not in allowed_wallets
                or is_classification_cex(record, anchor_side)
            ):
                continue
            grouped[anchor].append(record)
        total_amount = sum(
            (
                decimal_value(record.get("amount") or "0")
                for record in window.relevant
                if record.get("flow_type") not in {"mint", "burn"}
            ),
            Decimal("0"),
        )
        result: list[GroupSeed] = []
        for anchor, records in grouped.items():
            wallets = tuple(
                sorted(
                    {
                        address_of(record, member_side)
                        for record in records
                        if address_of(record, member_side)
                    }
                )
            )
            group_amount = sum(
                (
                    decimal_value(record.get("amount") or "0")
                    for record in records
                ),
                Decimal("0"),
            )
            amount_share = (
                group_amount / total_amount
                if total_amount > 0
                else Decimal("0")
            )
            if (
                len(wallets) < self.settings.oar_pattern_min_wallets
                or len(records) < self.settings.oar_pattern_min_tx
                or amount_share
                < self.settings.oar_pattern_min_amount_share
            ):
                continue
            result.append(
                GroupSeed(
                    group_type=group_type,
                    window=window.name,
                    wallets=wallets,
                    records=tuple(records),
                    anchor=anchor,
                    synchronized=self._is_synchronized(records),
                    anchor_known=bool(
                        identity_of(records[0], anchor_side).get("known")
                    ),
                )
            )
        return result

    def _cex_seeds(
        self,
        window: WindowFacts,
        *,
        allowed_wallets: set[str],
        seed_limit: int,
    ) -> tuple[list[GroupSeed], bool]:
        result: list[GroupSeed] = []
        seen: set[tuple[str, str, tuple[str, ...]]] = set()
        for flow_type in ("inflow", "outflow"):
            cex_side = "to" if flow_type == "inflow" else "from"
            wallet_side = "from" if flow_type == "inflow" else "to"
            grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
            for record in window.records_for(flow_type):
                wallet = address_of(record, wallet_side)
                cex = identity_of(record, cex_side)
                exchange = str(cex.get("entity_name") or "")
                if wallet in allowed_wallets and exchange:
                    grouped[exchange].append(record)
            for exchange, records in grouped.items():
                ordered = sorted(
                    records,
                    key=lambda record: (
                        int(record.get("block_time") or 0),
                        str(record.get("event_id") or ""),
                    ),
                )
                active: deque[dict[str, object]] = deque()
                counts: Counter[str] = Counter()
                for record in ordered:
                    timestamp = int(record.get("block_time") or 0)
                    active.append(record)
                    counts[address_of(record, wallet_side)] += 1
                    while (
                        active
                        and timestamp
                        - int(active[0].get("block_time") or 0)
                        > self.settings.oar_wallet_sync_window_sec
                    ):
                        removed = active.popleft()
                        removed_wallet = address_of(removed, wallet_side)
                        counts[removed_wallet] -= 1
                        if counts[removed_wallet] <= 0:
                            del counts[removed_wallet]
                    wallets = tuple(sorted(counts))
                    if (
                        len(wallets) < self.settings.oar_pattern_min_wallets
                        or len(active) < self.settings.oar_pattern_min_tx
                    ):
                        continue
                    group_type = f"synchronized_cex_{flow_type}"
                    signature = (group_type, exchange, wallets)
                    if signature in seen:
                        continue
                    seen.add(signature)
                    if len(result) >= seed_limit:
                        return result, True
                    result.append(
                        GroupSeed(
                            group_type=group_type,
                            window=window.name,
                            wallets=wallets,
                            records=tuple(active),
                            exchange=exchange,
                            synchronized=True,
                            anchor_known=True,
                        )
                    )
        return result, False

    def _score(
        self,
        activity: dict[str, object],
        seed: GroupSeed,
        *,
        occurrences: int,
        all_records: tuple[dict[str, object], ...],
        input_complete: bool,
        analysis_truncated: bool,
    ) -> dict[str, object]:
        score = 0
        supporting: list[str] = []
        counter: list[str] = []
        limitations: list[str] = []
        evidence_types: set[str] = set()
        if seed.group_type in {"shared_target", "shared_source"}:
            score += 30
            evidence = (
                "repeated_shared_target"
                if seed.group_type == "shared_target"
                else "repeated_shared_source"
            )
            supporting.append(evidence)
            evidence_types.add("shared_counterparty")
            if not seed.anchor_known:
                limitations.append(
                    "target_role_unknown"
                    if seed.group_type == "shared_target"
                    else "sender_role_unknown"
                )
        if occurrences >= 2 and seed.synchronized:
            score += 20
            supporting.append("repeated_across_nested_windows")
            evidence_types.add("nested_windows")
        if seed.synchronized:
            score += 15
            supporting.append("time_synchronized")
            evidence_types.add("time")
        if self._amounts_similar(seed.records):
            score += 15
            supporting.append("amounts_similar")
            evidence_types.add("amount")
        if self._has_direct_transfer(seed.wallets, all_records):
            score += 10
            supporting.append("direct_token_transfer_between_members")
            evidence_types.add("direct_transfer")
        if seed.group_type.startswith("synchronized_cex_"):
            score += 10
            supporting.append("same_exchange_synchronized_flow")
            evidence_types.add("cex_sync")
            if seed.group_type == "synchronized_cex_inflow":
                limitations.append(
                    "coordinated_deposit_not_control_proof"
                )
            else:
                limitations.append("cex_batch_withdrawal_possible")
            if "direct_transfer" not in evidence_types:
                counter.append("same_cex_may_be_only_common_factor")
                score = min(score, 39)

        if len(seed.records) < 3:
            score = min(score, 19)
            limitations.append("supporting_events_below_three")
        if len(evidence_types) <= 1:
            score = min(score, 39)
            limitations.append("single_evidence_type")
        if not input_complete:
            score = min(score, 39)
            limitations.append("query_incomplete")
        if len(seed.wallets) > 20:
            score = min(score, 39)
            limitations.append("batch_or_airdrop_possible")
        if analysis_truncated:
            score = min(score, 59)
            limitations.append("analysis_budget_exhausted")
        score = max(0, min(100, score))
        identifiers, identifiers_truncated = event_ids(
            seed.records,
            self.settings.oar_max_source_event_ids,
        )
        if identifiers_truncated:
            limitations.append("source_events_truncated")
        group_id = self._group_id(activity, seed)
        return {
            "group_id": group_id,
            "group_type": seed.group_type,
            "token_contract": str(
                activity.get("query", {}).get("contract") or ""
            ),
            "window": seed.window,
            "wallets": list(seed.wallets),
            "score": score,
            "level": self._level(score),
            "supporting_evidence": supporting,
            "counter_evidence": counter,
            "limitations": sorted(set(limitations)),
            "source_event_ids": identifiers,
            "source_events_truncated": identifiers_truncated,
            "algorithm_version": OAR_WALLET_GROUP_ALGORITHM_VERSION,
            "score_semantics": SCORE_SEMANTICS,
        }

    def _amounts_similar(
        self, records: Iterable[dict[str, object]]
    ) -> bool:
        amounts = sorted(
            decimal_value(record.get("amount") or "0")
            for record in records
        )
        if len(amounts) < 2 or amounts[-1] <= 0:
            return False
        return (
            amounts[-1] - amounts[0]
        ) / amounts[-1] <= self.settings.oar_wallet_amount_similarity_tolerance

    @staticmethod
    def _has_direct_transfer(
        wallets: tuple[str, ...],
        records: Iterable[dict[str, object]],
    ) -> bool:
        members = set(wallets)
        return any(
            record.get("flow_type") not in {"mint", "burn"}
            and address_of(record, "from") in members
            and address_of(record, "to") in members
            for record in records
        )

    def _is_synchronized(
        self, records: Iterable[dict[str, object]]
    ) -> bool:
        timestamps = [
            int(record.get("block_time") or 0) for record in records
        ]
        return bool(
            len(timestamps) >= 2
            and max(timestamps) - min(timestamps)
            <= self.settings.oar_wallet_sync_window_sec
        )

    @staticmethod
    def _level(score: int) -> str:
        for minimum, label in GROUP_LEVELS:
            if score >= minimum:
                return label
        return "证据不足"

    @staticmethod
    def _sort_key(
        group: dict[str, object],
    ) -> tuple[int, str, str]:
        return (
            -int(group.get("score") or 0),
            str(group.get("group_type") or ""),
            str(group.get("group_id") or ""),
        )

    @staticmethod
    def _group_id(
        activity: dict[str, object], seed: GroupSeed
    ) -> str:
        query = activity.get("query")
        query = query if isinstance(query, dict) else {}
        material = "|".join(
            (
                str(query.get("chain_id") or ""),
                str(query.get("contract") or "").lower(),
                seed.window,
                seed.group_type,
                ",".join(sorted(seed.wallets)),
                seed.anchor,
                seed.exchange,
                OAR_WALLET_GROUP_ALGORITHM_VERSION,
            )
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()
