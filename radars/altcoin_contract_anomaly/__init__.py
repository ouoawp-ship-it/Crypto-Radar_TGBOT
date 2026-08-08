"""P1 candidate discovery and bounded P2 confirmation for 山寨合约异动雷达.

P1 remains one-shot; P2 is an explicitly bounded realtime Dry-run. Neither is
registered with the production radar loop or Telegram delivery gateway.
"""

from .models import CandidateSnapshot, MappingRecord
from .rules import CandidateThresholds, apply_candidate_rules

__all__ = [
    "CandidateSnapshot",
    "CandidateThresholds",
    "MappingRecord",
    "apply_candidate_rules",
]
