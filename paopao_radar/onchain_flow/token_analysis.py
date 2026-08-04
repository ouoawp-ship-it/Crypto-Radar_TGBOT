from __future__ import annotations

from typing import Any

from .behavior import BehaviorAnalyzer, SCORE_SEMANTICS
from .config import OnchainSettings
from .constants import (
    OAR_ANALYSIS_SCHEMA_VERSION,
    OAR_BEHAVIOR_ALGORITHM_VERSION,
    OAR_WALLET_GROUP_ALGORITHM_VERSION,
)
from .domain import BehaviorAnalysisEngine, ChainFactProvider
from .token_activity import (
    TokenActivityQuery,
    TokenActivityQueryError,
    TokenActivityQueryService,
)
from .wallet_groups import WalletGroupAnalyzer


class TokenAnalysisService:
    def __init__(
        self,
        settings: OnchainSettings,
        activity_service: ChainFactProvider,
        *,
        behavior_engine: BehaviorAnalysisEngine | None = None,
        wallet_group_analyzer: Any | None = None,
    ):
        self.settings = settings
        self.activity_service = activity_service
        self.behavior = behavior_engine or BehaviorAnalyzer(settings)
        self.wallet_groups = wallet_group_analyzer or WalletGroupAnalyzer(
            settings
        )

    @classmethod
    def from_settings(
        cls,
        settings: OnchainSettings,
        query: TokenActivityQuery,
    ) -> "TokenAnalysisService":
        return cls(
            settings,
            TokenActivityQueryService.from_settings(settings, query),
        )

    def execute(
        self, query: TokenActivityQuery
    ) -> dict[str, object]:
        activity = self.activity_service.execute(query)
        try:
            behavior = self.behavior.analyze(activity)
            wallet_result = self.wallet_groups.analyze(activity)
        except (KeyError, TypeError, ValueError) as exc:
            raise TokenActivityQueryError(
                "analysis_failed",
                "Token activity facts could not be analyzed safely",
            ) from exc

        analysis_complete = bool(behavior["complete"]) and bool(
            wallet_result["complete"]
        )
        analysis_status = str(behavior["status"])
        limitations = set(behavior["limitations"])
        if (
            analysis_status not in {"partial_input", "no_activity"}
            and not wallet_result["complete"]
        ):
            analysis_status = "partial_analysis"
            limitations.add("analysis_budget_exhausted")
            self._cap_behavior_confidence(behavior)

        analysis = {
            "schema_version": OAR_ANALYSIS_SCHEMA_VERSION,
            "algorithm_version": OAR_BEHAVIOR_ALGORITHM_VERSION,
            "wallet_group_algorithm_version": (
                OAR_WALLET_GROUP_ALGORITHM_VERSION
            ),
            "status": analysis_status,
            "complete": analysis_complete,
            "input_complete": bool(activity.get("complete")),
            "score_semantics": SCORE_SEMANTICS,
            "valuation_basis": behavior["valuation_basis"],
            "windows": behavior["windows"],
            "primary_behavior": behavior["primary_behavior"],
            "behavior_candidates": behavior["behavior_candidates"],
            "coexisting_behavior_types": behavior[
                "coexisting_behavior_types"
            ],
            "observed_patterns": behavior["observed_patterns"],
            "wallet_groups": wallet_result["groups"],
            "limitations": sorted(
                limitations | set(wallet_result["limitations"])
            ),
        }
        result = dict(activity)
        result["analysis"] = analysis
        return result

    @staticmethod
    def _cap_behavior_confidence(
        behavior: dict[str, object],
    ) -> None:
        primary = behavior.get("primary_behavior")
        if (
            isinstance(primary, dict)
            and primary.get("confidence_level") == "high"
        ):
            primary["confidence_level"] = "medium"
        candidates = behavior.get("behavior_candidates")
        if isinstance(candidates, list):
            for candidate in candidates:
                if (
                    isinstance(candidate, dict)
                    and candidate.get("confidence_level") == "high"
                ):
                    candidate["confidence_level"] = "medium"
