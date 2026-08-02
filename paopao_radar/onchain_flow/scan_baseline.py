from __future__ import annotations

from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Mapping, Sequence


BASELINE_METRICS = (
    "transfer_count",
    "total_token_amount",
    "unique_senders",
    "unique_receivers",
    "behavior_score",
    "max_wallet_group_score",
)
WINDOW_BASELINE_METRICS = (
    "transfer_count",
    "total_token_amount",
    "unique_senders",
    "unique_receivers",
    "inflow_count",
    "outflow_count",
    "unclassified_count",
    "active_15m_buckets",
)
WINDOW_ORDER = ("15m", "1h", "4h", "24h")


def _decimal(value: object) -> Decimal:
    try:
        result = Decimal(str(value if value is not None else "0"))
    except (InvalidOperation, ValueError):
        return Decimal("0")
    return result if result.is_finite() and result >= 0 else Decimal("0")


def _decimal_text(value: Decimal) -> str:
    text = format(value.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def scan_metrics(
    summary: Mapping[str, object],
    *,
    behavior_score: int,
    max_wallet_group_score: int,
) -> dict[str, object]:
    return {
        "transfer_count": max(0, int(summary.get("transfer_count") or 0)),
        "total_token_amount": _decimal_text(
            _decimal(summary.get("total_token_amount"))
        ),
        "unique_senders": max(0, int(summary.get("unique_senders") or 0)),
        "unique_receivers": max(
            0, int(summary.get("unique_receivers") or 0)
        ),
        "behavior_score": max(0, int(behavior_score)),
        "max_wallet_group_score": max(0, int(max_wallet_group_score)),
    }


def nested_window_metrics(
    analysis: Mapping[str, object],
) -> dict[str, dict[str, object]]:
    windows = analysis.get("windows")
    windows = windows if isinstance(windows, Mapping) else {}
    result: dict[str, dict[str, object]] = {}
    for name in WINDOW_ORDER:
        source = windows.get(name)
        if not isinstance(source, Mapping):
            continue
        result[name] = {
            "transfer_count": max(
                0, int(source.get("transfer_count") or 0)
            ),
            "total_token_amount": _decimal_text(
                _decimal(source.get("total_token_amount"))
            ),
            "unique_senders": max(
                0, int(source.get("unique_senders") or 0)
            ),
            "unique_receivers": max(
                0, int(source.get("unique_receivers") or 0)
            ),
            "inflow_count": max(0, int(source.get("inflow_count") or 0)),
            "outflow_count": max(
                0, int(source.get("outflow_count") or 0)
            ),
            "unclassified_count": max(
                0, int(source.get("unclassified_count") or 0)
            ),
            "active_15m_buckets": max(
                0, int(source.get("active_15m_buckets") or 0)
            ),
        }
    return result


class HistoricalScanBaseline:
    """Compare one complete scan with prior complete scans of the same window.

    The result is diagnostic only. It never changes the notification gate.
    """

    def __init__(
        self,
        *,
        min_samples: int = 8,
        max_samples: int = 64,
        mad_multiplier: Decimal = Decimal("3.5"),
        metrics: Sequence[str] = BASELINE_METRICS,
    ):
        self.min_samples = int(min_samples)
        self.max_samples = int(max_samples)
        self.mad_multiplier = Decimal(mad_multiplier)
        self.metrics = tuple(str(name) for name in metrics)

    def analyze(
        self,
        current: Mapping[str, object],
        history: Sequence[Mapping[str, object]],
    ) -> dict[str, object]:
        bounded = list(history)[-self.max_samples :]
        sample_count = len(bounded)
        normalized_current = {
            name: _decimal_text(_decimal(current.get(name)))
            for name in self.metrics
        }
        if sample_count < self.min_samples:
            return {
                "status": "cold_start",
                "sample_count": sample_count,
                "min_samples": self.min_samples,
                "max_samples": self.max_samples,
                "mad_multiplier": _decimal_text(self.mad_multiplier),
                "anomaly": False,
                "anomalous_metrics": [],
                "current": normalized_current,
                "metrics": {},
            }

        diagnostics: dict[str, object] = {}
        anomalous_metrics: list[str] = []
        for name in self.metrics:
            values = [_decimal(row.get(name)) for row in bounded]
            center = median(values)
            deviations = [abs(value - center) for value in values]
            mad = median(deviations)
            observed = _decimal(current.get(name))
            flat_baseline = mad == 0
            threshold = (
                center + self.mad_multiplier * mad
                if mad > 0
                else (center * Decimal("2") if center > 0 else Decimal("1"))
            )
            anomalous = (
                observed > threshold if mad > 0 else observed >= threshold
            )
            robust_z: str | None = None
            if mad > 0:
                robust_z = _decimal_text((observed - center) / mad)
            if anomalous:
                anomalous_metrics.append(name)
            diagnostics[name] = {
                "current": _decimal_text(observed),
                "median": _decimal_text(center),
                "mad": _decimal_text(mad),
                "threshold": _decimal_text(threshold),
                "robust_mad_ratio": robust_z,
                "flat_baseline": flat_baseline,
                "anomalous": anomalous,
            }
        return {
            "status": "ready",
            "sample_count": sample_count,
            "min_samples": self.min_samples,
            "max_samples": self.max_samples,
            "mad_multiplier": _decimal_text(self.mad_multiplier),
            "anomaly": bool(anomalous_metrics),
            "anomalous_metrics": anomalous_metrics,
            "current": normalized_current,
            "metrics": diagnostics,
        }


def analyze_nested_windows(
    current: Mapping[str, Mapping[str, object]],
    history: Sequence[Mapping[str, object]],
    *,
    min_samples: int,
    max_samples: int,
    mad_multiplier: Decimal,
) -> dict[str, object]:
    analyzer = HistoricalScanBaseline(
        min_samples=min_samples,
        max_samples=max_samples,
        mad_multiplier=mad_multiplier,
        metrics=WINDOW_BASELINE_METRICS,
    )
    windows: dict[str, object] = {}
    anomalous_windows: list[str] = []
    ready_windows: list[str] = []
    for name in WINDOW_ORDER:
        current_metrics = current.get(name)
        if not isinstance(current_metrics, Mapping):
            continue
        prior: list[Mapping[str, object]] = []
        for row in history:
            by_window = row.get("window_metrics")
            if not isinstance(by_window, Mapping):
                continue
            item = by_window.get(name)
            if isinstance(item, Mapping):
                prior.append(item)
        result = analyzer.analyze(current_metrics, prior)
        windows[name] = result
        if result["status"] == "ready":
            ready_windows.append(name)
        if bool(result["anomaly"]):
            anomalous_windows.append(name)
    return {
        "status": "ready" if ready_windows else "cold_start",
        "windows": windows,
        "ready_windows": ready_windows,
        "anomalous_windows": anomalous_windows,
        "multi_window_anomaly": len(anomalous_windows) >= 2,
    }


def baseline_local_error(
    *, min_samples: int, max_samples: int, mad_multiplier: Decimal
) -> dict[str, object]:
    return {
        "status": "local_error",
        "sample_count": 0,
        "min_samples": int(min_samples),
        "max_samples": int(max_samples),
        "mad_multiplier": _decimal_text(Decimal(mad_multiplier)),
        "anomaly": False,
        "anomalous_metrics": [],
        "current": {},
        "metrics": {},
        "windows": {},
        "ready_windows": [],
        "anomalous_windows": [],
        "multi_window_anomaly": False,
        "error": "historical_baseline_local_error",
    }


def baseline_skipped_incomplete(
    *, min_samples: int, max_samples: int, mad_multiplier: Decimal
) -> dict[str, object]:
    return {
        "status": "skipped_incomplete",
        "sample_count": 0,
        "min_samples": int(min_samples),
        "max_samples": int(max_samples),
        "mad_multiplier": _decimal_text(Decimal(mad_multiplier)),
        "anomaly": False,
        "anomalous_metrics": [],
        "current": {},
        "metrics": {},
        "windows": {},
        "ready_windows": [],
        "anomalous_windows": [],
        "multi_window_anomaly": False,
    }
