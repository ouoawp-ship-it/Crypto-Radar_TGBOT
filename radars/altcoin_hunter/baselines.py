"""Bounded, causal descriptive baselines, without a claim of market calibration.

Sampling policies belong to each metric/window. Overlapping observations are
correlated observations, never independent statistical samples.
"""

from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import math
from statistics import median
from typing import Any, Mapping


def _integer(value: Any, name: str, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"invalid {name}")
    return value


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError(f"invalid {name}")
    try:
        parsed = Decimal(str(value))
        result = float(parsed)
    except (ValueError, InvalidOperation, OverflowError) as exc:
        raise ValueError(f"invalid {name}") from exc
    if not parsed.is_finite() or not math.isfinite(result) or (parsed != 0 and result == 0):
        raise ValueError(f"nonfinite {name}")
    return result


@dataclass(frozen=True, slots=True)
class BaselinePolicy:
    """Explicit per-series policy; defaults are offline examples, not calibration."""

    max_samples: int = 512
    min_sample_count: int = 20
    min_span_ms: int = 600_000
    min_coverage_ratio: float = 0.95
    sampling_stride: int = 1
    sample_interval_ms: int = 60_000
    metric_floor: float = 0.01
    ewma_alpha: float | None = None
    clip_z: float = 6.0
    baseline_version: str = "1"

    def __post_init__(self) -> None:
        for key in ("max_samples", "min_sample_count", "sampling_stride", "sample_interval_ms"):
            _integer(getattr(self, key), key)
        _integer(self.min_span_ms, "min_span_ms", 0)
        if self.max_samples > 100_000 or self.min_sample_count > self.max_samples:
            raise ValueError("invalid sample capacity")
        for key in ("min_coverage_ratio", "metric_floor", "clip_z"):
            object.__setattr__(self, key, _finite(getattr(self, key), key))
        if not 0 < self.min_coverage_ratio <= 1 or self.metric_floor <= 0 or not 0 < self.clip_z <= 6:
            raise ValueError("invalid baseline limits")
        if self.ewma_alpha is not None:
            alpha = _finite(self.ewma_alpha, "ewma_alpha")
            if not 0 < alpha <= 1:
                raise ValueError("invalid ewma_alpha")
            object.__setattr__(self, "ewma_alpha", alpha)
        if not isinstance(self.baseline_version, str) or self.baseline_version != "1":
            raise ValueError("unsupported baseline version")

    @property
    def config_hash(self) -> str:
        encoded = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class BaselineKey:
    source: str
    exchange: str
    market: str
    instrument_id: str
    feature: str
    window_sec: int

    def __post_init__(self) -> None:
        for key in ("source", "exchange", "market", "instrument_id", "feature"):
            value = getattr(self, key)
            if not isinstance(value, str) or not value.strip() or len(value) > 256:
                raise ValueError(f"invalid {key}")
        if type(self.window_sec) is not int or self.window_sec not in (60, 180, 300, 900, 1800, 3600):
            raise ValueError("unsupported window_sec")


@dataclass(frozen=True, slots=True)
class BaselineResult:
    raw_value: str | None
    value: float | None
    median: float | None
    mad: float | None
    robust_z: float | None
    unclipped_z: float | None
    clipped: bool
    ewma: float | None
    ready: bool
    coverage_ratio: float
    sample_count: int
    expected_sample_count: int
    wall_clock_span_ms: int
    baseline_version: str
    config_hash: str
    reason_codes: tuple[str, ...]
    sampling_stride: int
    sample_semantics: str = "overlapping_observations_not_independent_samples"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RollingBaseline:
    """One explicitly clocked metric/window; history is strictly before t."""

    def __init__(self, policy: BaselinePolicy):
        self.policy = policy
        self._samples: deque[tuple[int, str | None]] = deque(maxlen=policy.max_samples)
        self._last_evaluated_at: int | None = None

    @property
    def retained_sample_count(self) -> int:
        return len(self._samples)

    def evaluate(self, timestamp_ms: int, value: Any | None) -> BaselineResult:
        _integer(timestamp_ms, "timestamp_ms")
        numeric = None if value is None else _finite(value, "value")
        step = self.policy.sample_interval_ms * self.policy.sampling_stride
        cutoff = timestamp_ms - step * self.policy.max_samples
        history = [(t, raw) for t, raw in self._samples if cutoff <= t < timestamp_ms]
        values = [_finite(raw, "history") for _, raw in history if raw is not None]
        valid_times = [t for t, raw in history if raw is not None]
        span = valid_times[-1] - valid_times[0] if valid_times else 0
        expected = 0 if not history else min(self.policy.max_samples, (timestamp_ms - 1 - history[0][0]) // step + 1)
        coverage = len(values) / expected if expected else 0.0
        decimal_values = [Decimal(str(item)) for item in values]
        decimal_center = median(decimal_values) if values else None
        decimal_mad = median([abs(item - decimal_center) for item in decimal_values]) if values else None
        center = float(decimal_center) if values else None
        deviation = float(decimal_mad) if values else None
        reasons = []
        if deviation is not None and not math.isfinite(deviation):
            deviation = None
            reasons.append("history_dispersion_overflow")
        if len(values) < self.policy.min_sample_count:
            reasons.append("insufficient_samples")
        if span < self.policy.min_span_ms:
            reasons.append("insufficient_span")
        if coverage < self.policy.min_coverage_ratio:
            reasons.append("insufficient_coverage")
        if numeric is None:
            reasons.append("missing_current_value")
        ewma = None
        if values and self.policy.ewma_alpha is not None:
            alpha = self.policy.ewma_alpha
            decimal_ewma = decimal_values[0]
            decimal_alpha = Decimal(str(alpha))
            for item in decimal_values[1:]:
                decimal_ewma = (1 - decimal_alpha) * decimal_ewma + decimal_alpha * item
            ewma = float(decimal_ewma)
        ready = not reasons
        raw_z = None
        if ready:
            # Decimal arithmetic prevents intermediate subtraction overflow even
            # when each finite float is individually near its representable bound.
            denominator = max(Decimal("1.4826") * Decimal(str(deviation)), Decimal(str(self.policy.metric_floor)))
            exact_z = (Decimal(str(numeric)) - Decimal(str(center))) / denominator
            raw_z = float(exact_z)
            if not math.isfinite(raw_z):
                ready = False
                reasons.append("normalization_overflow")
                raw_z = None
        z = None if raw_z is None else max(-self.policy.clip_z, min(self.policy.clip_z, raw_z))
        return BaselineResult(
            None if value is None else str(value), numeric, center, deviation, z, raw_z,
            raw_z is not None and raw_z != z, ewma, ready, coverage, len(values), expected,
            span, self.policy.baseline_version, self.policy.config_hash, tuple(reasons), self.policy.sampling_stride,
        )

    def evaluate_and_observe(self, timestamp_ms: int, value: Any | None) -> BaselineResult:
        if self._last_evaluated_at is not None and timestamp_ms <= self._last_evaluated_at:
            raise ValueError("baseline clock must increase")
        result = self.evaluate(timestamp_ms, value)
        self._last_evaluated_at = timestamp_ms
        step = self.policy.sample_interval_ms * self.policy.sampling_stride
        if timestamp_ms % step == 0:
            self._samples.append((timestamp_ms, None if value is None else str(value)))
        return result

    def export(self, key: BaselineKey) -> dict[str, Any]:
        return {
            **asdict(key), "baseline_version": self.policy.baseline_version,
            "config_hash": self.policy.config_hash,
            "updated_at_ms": self._last_evaluated_at or 0,
            "payload": {"policy": asdict(self.policy), "samples": list(self._samples),
                        "last_evaluated_at": self._last_evaluated_at},
        }

    @classmethod
    def restore(cls, record: Mapping[str, Any]) -> "RollingBaseline":
        payload = record["payload"]
        policy = BaselinePolicy(**payload["policy"])
        if record.get("config_hash") != policy.config_hash or record.get("baseline_version") != policy.baseline_version:
            raise ValueError("baseline version/config mismatch")
        result = cls(policy)
        samples = payload["samples"]
        if not isinstance(samples, (list, tuple)) or len(samples) > policy.max_samples:
            raise ValueError("invalid baseline snapshot capacity")
        previous = 0
        for timestamp, raw in samples:
            _integer(timestamp, "sample timestamp")
            if timestamp <= previous or timestamp % (policy.sampling_stride * policy.sample_interval_ms):
                raise ValueError("invalid baseline sample order/stride")
            if raw is not None:
                _finite(raw, "snapshot value")
            result._samples.append((timestamp, raw))
            previous = timestamp
        last = payload["last_evaluated_at"]
        if last is not None:
            _integer(last, "last_evaluated_at")
            if last < previous:
                raise ValueError("invalid baseline checkpoint")
        elif samples:
            raise ValueError("missing baseline clock")
        if type(record.get("updated_at_ms")) is not int or record["updated_at_ms"] != (last or 0):
            raise ValueError("baseline outer checkpoint mismatch")
        result._last_evaluated_at = last
        return result


class BaselineEngine:
    """Explicit registration prevents one hardcoded policy for all windows."""

    def __init__(self, *, max_series: int = 12_000):
        self.max_series = _integer(max_series, "max_series")
        self._series: dict[BaselineKey, RollingBaseline] = {}

    def register(self, key: BaselineKey, policy: BaselinePolicy) -> None:
        if key in self._series:
            if self._series[key].policy != policy:
                raise ValueError("baseline policy change requires a new versioned run")
            return
        if len(self._series) >= self.max_series:
            raise OverflowError("baseline series capacity")
        self._series[key] = RollingBaseline(policy)

    def evaluate_and_observe(self, key: BaselineKey, timestamp_ms: int, value: Any | None) -> BaselineResult:
        return self._series[key].evaluate_and_observe(timestamp_ms, value)

    def export(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._series[key].export(key) for key in sorted(self._series, key=lambda item: tuple(asdict(item).values())))

    def restore(self, records: list[dict[str, Any]]) -> None:
        if len(records) > self.max_series:
            raise OverflowError("baseline series capacity")
        restored = {}
        for record in records:
            key = BaselineKey(**{name: record[name] for name in BaselineKey.__dataclass_fields__})
            if key in restored:
                raise ValueError("duplicate baseline snapshot")
            restored[key] = RollingBaseline.restore(record)
        self._series = restored
