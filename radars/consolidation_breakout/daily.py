from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


DAILY_DETECTOR_PROFILE = "daily_adaptive.v1"


@dataclass(frozen=True, slots=True)
class DailyCandle:
    """Small daily-candle value object that also accepts existing candle ducks."""

    high: float
    low: float
    close: float
    open: float = 0.0
    volume: float = 0.0
    close_time: int = 0

    @classmethod
    def from_value(cls, value: object) -> DailyCandle:
        if isinstance(value, cls):
            return value

        def field(name: str, default: Any = None) -> Any:
            if isinstance(value, Mapping):
                return value.get(name, default)
            return getattr(value, name, default)

        try:
            high = float(field("high"))
            low = float(field("low"))
            close = float(field("close"))
            open_price = float(field("open", close))
            volume = max(0.0, float(field("volume", 0.0)))
            close_time = int(field("close_time", 0))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("invalid daily candle") from exc

        prices = (open_price, high, low, close)
        if not all(math.isfinite(number) for number in prices):
            raise ValueError("daily candle contains a non-finite price")
        if not math.isfinite(volume) or close <= 0 or high < low:
            raise ValueError("daily candle has invalid price or volume bounds")
        return cls(
            open=open_price,
            high=high,
            low=low,
            close=close,
            volume=volume,
            close_time=close_time,
        )


@dataclass(frozen=True, slots=True)
class DailyHorizonSpec:
    name: str
    label: str
    anchors: tuple[int, ...]
    max_width_atr: float
    max_width_pct: float
    max_efficiency: float
    stability: int
    rank: int
    trim_ratio: float = 0.05
    min_candle_coverage: float = 0.90
    min_close_coverage: float = 0.95
    touch_tolerance_atr: float = 0.20
    endpoint_drift_atr: float = 0.35
    minimum_touch_clusters: int = 2
    atr_period: int = 14

    def __post_init__(self) -> None:
        if not self.name or not self.anchors:
            raise ValueError("daily horizon requires a name and anchors")
        if tuple(sorted(set(self.anchors))) != self.anchors:
            raise ValueError("daily horizon anchors must be unique and ascending")
        if self.anchors[0] < 2 or self.stability < 1 or self.atr_period < 1:
            raise ValueError("daily horizon contains an invalid length")
        if not 0 <= self.trim_ratio < 0.5:
            raise ValueError("trim_ratio must be in [0, 0.5)")
        if not 0 <= self.min_candle_coverage <= 1:
            raise ValueError("min_candle_coverage must be in [0, 1]")
        if not 0 <= self.min_close_coverage <= 1:
            raise ValueError("min_close_coverage must be in [0, 1]")


DAILY_HORIZONS = (
    DailyHorizonSpec(
        "short",
        "短期",
        (20, 30, 40, 50),
        4.5,
        8.0,
        0.35,
        3,
        1,
    ),
    DailyHorizonSpec(
        "medium",
        "中期",
        (60, 90, 120, 150),
        9.0,
        18.0,
        0.30,
        5,
        2,
    ),
    DailyHorizonSpec(
        "long",
        "长期",
        (180, 240, 300, 360, 420, 500),
        18.0,
        35.0,
        0.25,
        8,
        3,
    ),
)


def _normalize_candles(values: Sequence[object]) -> list[DailyCandle]:
    return [DailyCandle.from_value(value) for value in values]


def _atr(
    candles: Sequence[DailyCandle],
    end_index: int,
    period: int,
) -> float:
    """Return simple true-range ATR ending immediately before ``end_index``."""

    if end_index <= 1 or end_index > len(candles):
        return 0.0
    start = max(1, end_index - max(1, period))
    ranges: list[float] = []
    for index in range(start, end_index):
        candle = candles[index]
        previous_close = candles[index - 1].close
        ranges.append(max(
            candle.high - candle.low,
            abs(candle.high - previous_close),
            abs(candle.low - previous_close),
        ))
    return sum(ranges) / len(ranges) if ranges else 0.0


def _path_efficiency(candles: Sequence[DailyCandle]) -> float:
    if len(candles) < 2:
        return 1.0
    travelled = sum(
        abs(candles[index].close - candles[index - 1].close)
        for index in range(1, len(candles))
    )
    if travelled <= 0:
        return 0.0
    return abs(candles[-1].close - candles[0].close) / travelled


def _trimmed_boundaries(
    candles: Sequence[DailyCandle],
    trim_ratio: float,
) -> tuple[float, float, int]:
    if not candles:
        return 0.0, 0.0, 0
    trim_count = int(len(candles) * max(0.0, trim_ratio))
    trim_count = min(trim_count, max(0, (len(candles) - 1) // 2))
    highs = sorted(candle.high for candle in candles)
    lows = sorted(candle.low for candle in candles)
    upper = highs[len(highs) - trim_count - 1]
    lower = lows[trim_count]
    return upper, lower, trim_count


def _coverage_metrics(
    candles: Sequence[DailyCandle],
    *,
    upper: float,
    lower: float,
) -> tuple[float, float]:
    if not candles:
        return 0.0, 0.0
    candle_coverage = sum(
        candle.high <= upper and candle.low >= lower
        for candle in candles
    ) / len(candles)
    close_coverage = sum(
        lower <= candle.close <= upper
        for candle in candles
    ) / len(candles)
    return candle_coverage, close_coverage


def _cluster_count(flags: Sequence[bool]) -> int:
    count = 0
    touching = False
    for flag in flags:
        active = bool(flag)
        if active and not touching:
            count += 1
        touching = active
    return count


def _touch_clusters(
    candles: Sequence[DailyCandle],
    *,
    upper: float,
    lower: float,
    tolerance: float,
) -> tuple[int, int]:
    tolerance = max(0.0, tolerance)
    upper_touches = _cluster_count([
        abs(candle.high - upper) <= tolerance for candle in candles
    ])
    lower_touches = _cluster_count([
        abs(candle.low - lower) <= tolerance for candle in candles
    ])
    return upper_touches, lower_touches


def _quality(
    *,
    spec: DailyHorizonSpec,
    candle_coverage: float,
    close_coverage: float,
    efficiency: float,
    width_atr: float,
    width_pct: float,
    upper_touches: int,
    lower_touches: int,
) -> dict[str, Any]:
    if (
        candle_coverage >= 0.96
        and close_coverage >= 0.99
        and upper_touches >= 3
        and lower_touches >= 3
        and efficiency <= spec.max_efficiency * 0.65
        and width_atr <= spec.max_width_atr * 0.75
    ):
        label = "strong"
        label_zh = "强"
    elif (
        candle_coverage >= 0.93
        and close_coverage >= 0.97
        and efficiency <= spec.max_efficiency * 0.85
    ):
        label = "standard"
        label_zh = "标准"
    else:
        label = "watch"
        label_zh = "观察"
    return {
        "label": label,
        "label_zh": label_zh,
        "reasons": [
            {
                "factor": "candle_coverage",
                "value": candle_coverage,
                "minimum": spec.min_candle_coverage,
            },
            {
                "factor": "close_coverage",
                "value": close_coverage,
                "minimum": spec.min_close_coverage,
            },
            {
                "factor": "touch_clusters",
                "upper": upper_touches,
                "lower": lower_touches,
                "minimum_each": spec.minimum_touch_clusters,
            },
            {
                "factor": "path_efficiency",
                "value": efficiency,
                "maximum": spec.max_efficiency,
            },
            {
                "factor": "box_width",
                "atr": width_atr,
                "pct": width_pct,
                "maximum_atr": spec.max_width_atr,
                "maximum_pct": spec.max_width_pct,
            },
        ],
    }


def _evaluate_length(
    candles: Sequence[DailyCandle],
    *,
    end_index: int,
    spec: DailyHorizonSpec,
    length: int,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    required_bars = length + spec.stability - 1
    diagnostics: dict[str, Any] = {
        "length": length,
        "accepted": False,
        "reasons": [],
        "metrics": {
            "available_bars": end_index,
            "required_bars": required_bars,
        },
    }
    reasons: list[str] = diagnostics["reasons"]
    metrics: dict[str, Any] = diagnostics["metrics"]
    if end_index < required_bars:
        reasons.append("insufficient_history")
        return None, diagnostics

    window = candles[end_index - length:end_index]
    atr = _atr(candles, end_index, spec.atr_period)
    metrics["atr"] = atr
    if atr <= 0:
        reasons.append("invalid_atr")
        return None, diagnostics

    upper, lower, trim_count = _trimmed_boundaries(window, spec.trim_ratio)
    width = upper - lower
    midpoint = (upper + lower) / 2.0
    metrics.update({
        "upper": upper,
        "lower": lower,
        "trimmed_each_side": trim_count,
    })
    if width <= 0 or midpoint <= 0:
        reasons.append("invalid_boundaries")
        return None, diagnostics

    width_atr = width / atr
    width_pct = width / midpoint * 100.0
    efficiency = _path_efficiency(window)
    candle_coverage, close_coverage = _coverage_metrics(
        window,
        upper=upper,
        lower=lower,
    )
    upper_touches, lower_touches = _touch_clusters(
        window,
        upper=upper,
        lower=lower,
        tolerance=spec.touch_tolerance_atr * atr,
    )
    metrics.update({
        "width_atr": width_atr,
        "width_pct": width_pct,
        "efficiency": efficiency,
        "candle_coverage": candle_coverage,
        "close_coverage": close_coverage,
        "upper_touches": upper_touches,
        "lower_touches": lower_touches,
    })

    if width_atr > spec.max_width_atr:
        reasons.append("width_atr")
    if width_pct > spec.max_width_pct:
        reasons.append("width_pct")
    if efficiency > spec.max_efficiency:
        reasons.append("path_efficiency")
    if candle_coverage < spec.min_candle_coverage:
        reasons.append("candle_coverage")
    if close_coverage < spec.min_close_coverage:
        reasons.append("close_coverage")

    max_drift = spec.endpoint_drift_atr * atr
    unstable_upper = False
    unstable_lower = False
    for shift in range(1, spec.stability):
        shifted_end = end_index - shift
        shifted = candles[shifted_end - length:shifted_end]
        shifted_upper, shifted_lower, _trimmed = _trimmed_boundaries(
            shifted,
            spec.trim_ratio,
        )
        unstable_upper = unstable_upper or abs(shifted_upper - upper) > max_drift
        unstable_lower = unstable_lower or abs(shifted_lower - lower) > max_drift
    metrics["unstable_upper"] = unstable_upper
    metrics["unstable_lower"] = unstable_lower
    if unstable_upper:
        reasons.append("unstable_upper")
    if unstable_lower:
        reasons.append("unstable_lower")
    if upper_touches < spec.minimum_touch_clusters:
        reasons.append("upper_touches")
    if lower_touches < spec.minimum_touch_clusters:
        reasons.append("lower_touches")

    if reasons:
        return None, diagnostics

    quality = _quality(
        spec=spec,
        candle_coverage=candle_coverage,
        close_coverage=close_coverage,
        efficiency=efficiency,
        width_atr=width_atr,
        width_pct=width_pct,
        upper_touches=upper_touches,
        lower_touches=lower_touches,
    )
    candidate = {
        "detector_profile": DAILY_DETECTOR_PROFILE,
        "horizon": spec.name,
        "horizon_label": spec.label,
        "base_bars": length,
        "active_bars": 0,
        "upper": upper,
        "lower": lower,
        "atr": atr,
        "width_atr": width_atr,
        "width_pct": width_pct,
        "efficiency": efficiency,
        "candle_coverage": candle_coverage,
        "close_coverage": close_coverage,
        "upper_touches": upper_touches,
        "lower_touches": lower_touches,
        "boundary_method": "trimmed_wick_5pct_v1",
        "trimmed_each_side": trim_count,
        "window_start_close_time": window[0].close_time,
        "formed_close_time": window[-1].close_time,
        "quality_label": quality["label"],
        "quality_label_zh": quality["label_zh"],
        "quality_reasons": quality["reasons"],
    }
    diagnostics["accepted"] = True
    diagnostics["quality_label"] = quality["label"]
    return candidate, diagnostics


def _select_normalized(
    candles: Sequence[DailyCandle],
    spec: DailyHorizonSpec,
    *,
    end_index: int,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    evaluations: list[dict[str, Any]] = []
    accepted: list[dict[str, Any]] = []
    for length in sorted(spec.anchors, reverse=True):
        candidate, diagnostic = _evaluate_length(
            candles,
            end_index=end_index,
            spec=spec,
            length=length,
        )
        evaluations.append(diagnostic)
        if candidate is not None:
            accepted.append(candidate)

    selected = accepted[0] if accepted else None
    reason_counts = Counter(
        reason
        for evaluation in evaluations
        for reason in evaluation["reasons"]
    )
    diagnostics = {
        "detector_profile": DAILY_DETECTOR_PROFILE,
        "horizon": spec.name,
        "available_bars": end_index,
        "selected_length": selected["base_bars"] if selected else 0,
        "accepted_lengths": [candidate["base_bars"] for candidate in accepted],
        "reason_counts": dict(sorted(reason_counts.items())),
        "evaluations": evaluations,
        "status": (
            "accepted"
            if selected is not None
            else "insufficient_history"
            if reason_counts and set(reason_counts) == {"insufficient_history"}
            else "rejected"
        ),
    }
    return selected, diagnostics


def select_daily_candidate(
    candles: Sequence[object],
    spec: DailyHorizonSpec,
    *,
    end_index: int | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Select the longest qualifying anchor ending before ``end_index``.

    ``end_index`` is exclusive. Omitting it evaluates all supplied closed bars.
    Existing ``radar.Candle`` instances are accepted through attribute access.
    """

    normalized = _normalize_candles(candles)
    resolved_end = len(normalized) if end_index is None else int(end_index)
    if resolved_end < 0 or resolved_end > len(normalized):
        raise ValueError("end_index is outside the supplied candle history")
    return _select_normalized(normalized, spec, end_index=resolved_end)


def detect_daily_boxes(
    candles: Sequence[object],
    *,
    end_index: int | None = None,
    specs: Sequence[DailyHorizonSpec] = DAILY_HORIZONS,
) -> dict[str, Any]:
    """Evaluate every daily horizon and return boxes plus explainable diagnostics."""

    normalized = _normalize_candles(candles)
    resolved_end = len(normalized) if end_index is None else int(end_index)
    if resolved_end < 0 or resolved_end > len(normalized):
        raise ValueError("end_index is outside the supplied candle history")

    boxes: dict[str, dict[str, Any] | None] = {}
    horizon_diagnostics: dict[str, dict[str, Any]] = {}
    all_reasons: Counter[str] = Counter()
    for spec in specs:
        candidate, diagnostics = _select_normalized(
            normalized,
            spec,
            end_index=resolved_end,
        )
        boxes[spec.name] = candidate
        horizon_diagnostics[spec.name] = diagnostics
        all_reasons.update(diagnostics["reason_counts"])
    return {
        "detector_profile": DAILY_DETECTOR_PROFILE,
        "available_bars": resolved_end,
        "boxes": boxes,
        "diagnostics": {
            "reason_counts": dict(sorted(all_reasons.items())),
            "horizons": horizon_diagnostics,
        },
    }


__all__ = [
    "DAILY_DETECTOR_PROFILE",
    "DAILY_HORIZONS",
    "DailyCandle",
    "DailyHorizonSpec",
    "detect_daily_boxes",
    "select_daily_candidate",
]
