from __future__ import annotations

from typing import Any


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_timestamp_ms(value: Any) -> int:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return 0
    if timestamp <= 0:
        return 0
    if timestamp < 10_000_000_000:
        timestamp *= 1000
    return int(timestamp)
