"""P1 candidate discovery for 山寨合约异动雷达.

This package is intentionally one-shot and dry-run only. It is not registered
with the production radar loop or Telegram delivery gateway.
"""

from .models import CandidateSnapshot, MappingRecord
from .rules import CandidateThresholds, apply_candidate_rules

__all__ = [
    "CandidateSnapshot",
    "CandidateThresholds",
    "MappingRecord",
    "apply_candidate_rules",
]
