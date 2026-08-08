"""山寨合约异动雷达的候选、实时确认与显式生产运行能力。

P1 remains one-shot and P2 remains a bounded Dry-run.  Final production is a
separate, default-off injection into the existing market-stream process.
"""

from .models import CandidateSnapshot, MappingRecord
from .rules import CandidateThresholds, apply_candidate_rules

__all__ = [
    "CandidateSnapshot",
    "CandidateThresholds",
    "MappingRecord",
    "apply_candidate_rules",
]
