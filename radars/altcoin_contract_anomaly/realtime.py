from __future__ import annotations

import math
import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from config import Settings
from shared.binance_data import BinanceDataSource
from shared.market_cockpit import MarketSnapshotStore
from shared.realtime_market import (
    BinanceRealtimeMarketService,
    MarkPriceBook,
    MarkPriceUpdate,
    RealtimeFeatureStore,
    parse_binance_mark_price_update,
    run_realtime_market_session,
)

from .models import FORMAL_MAPPING_METHODS, RULES_VERSION, finite_float, json_safe
from .radar import AltcoinAnomalyDataUnavailable
from .rules import HIGH_LEVERAGE_CANDIDATE, SHORT_SQUEEZE_CANDIDATE
from .state import CandidatePoolStore, CandidateStateError
from .realtime_state import RealtimeObservationState, deterministic_event_id


P2_SCHEMA_VERSION = 2
P2_RULES_VERSION = "altcoin_contract_anomaly.p2.v2"
P2_CLOCK_SKEW_TOLERANCE_SEC = 5.0
EVENT_NAMES_CN = {
    "short_fuel_building": "空头燃料堆积",
    "short_squeeze_ignition": "逼空启动",
    "high_leverage_anomaly": "高杠杆异动",
    "long_crowding_risk": "多头拥挤风险",
    "anomaly_weakening": "异动减弱",
    "candidate_condition_invalidated": "候选条件失效",
}
FACTOR_FAMILIES = (
    "price_momentum",
    "volume_expansion",
    "aggressive_flow",
    "open_interest",
    "funding",
    "liquidation",
)


def _number(value: Any, *, minimum: float | None = None) -> float | None:
    parsed = finite_float(value, minimum=minimum)
    return parsed


def _epoch_seconds(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        parsed = _number(value)
        if parsed is None or parsed <= 0:
            return None
        return parsed / 1000.0 if parsed > 10_000_000_000 else parsed
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed_time = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed_time.tzinfo is None:
        parsed_time = parsed_time.replace(tzinfo=timezone.utc)
    return parsed_time.astimezone(timezone.utc).timestamp()


def _iso(value: Any) -> str:
    timestamp = _epoch_seconds(value)
    if timestamp is None:
        return ""
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def _ratio(current: Any, previous: Any) -> float | None:
    current_value = _number(current, minimum=0.0)
    previous_value = _number(previous, minimum=0.0)
    if current_value is None or previous_value is None or previous_value <= 0:
        return None
    return (current_value - previous_value) / previous_value


@dataclass(frozen=True)
class ValidatedCandidateManifest:
    generated_at: str
    candidate_pool_hash: str
    candidate_snapshot_hash: str
    rules_fingerprint: str
    rules_version: str
    candidates: dict[str, dict[str, Any]]

    @property
    def symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self.candidates))

    def summary(self) -> dict[str, Any]:
        return json_safe({
            "generated_at": self.generated_at,
            "candidate_pool_hash": self.candidate_pool_hash,
            "candidate_snapshot_hash": self.candidate_snapshot_hash,
            "rules_fingerprint": self.rules_fingerprint,
            "rules_version": self.rules_version,
            "candidates": self.candidates,
        })

    @classmethod
    def from_summary(cls, payload: Mapping[str, Any] | None) -> "ValidatedCandidateManifest | None":
        if not isinstance(payload, Mapping) or not isinstance(payload.get("candidates"), Mapping):
            return None
        candidates = {
            str(symbol).upper(): dict(row)
            for symbol, row in payload["candidates"].items()
            if isinstance(row, Mapping) and str(symbol).upper().endswith("USDT")
        }
        required = (
            "generated_at",
            "candidate_pool_hash",
            "candidate_snapshot_hash",
            "rules_fingerprint",
            "rules_version",
        )
        if any(not isinstance(payload.get(key), str) or not payload.get(key) for key in required):
            return None
        return cls(
            generated_at=str(payload["generated_at"]),
            candidate_pool_hash=str(payload["candidate_pool_hash"]),
            candidate_snapshot_hash=str(payload["candidate_snapshot_hash"]),
            rules_fingerprint=str(payload["rules_fingerprint"]),
            rules_version=str(payload["rules_version"]),
            candidates=candidates,
        )


class CandidateManifestConsumer:
    """Strict P2 wrapper around the P1 atomic store and cache validation."""

    def __init__(
        self,
        settings: Settings,
        *,
        previous: Mapping[str, Any] | None = None,
        pool_store: CandidatePoolStore | None = None,
    ) -> None:
        self.settings = settings
        path = Path(settings.altcoin_contract_anomaly_candidate_snapshot_path)
        self.pool_store = pool_store or CandidatePoolStore(path, data_dir=settings.data_dir)
        self.last_valid = ValidatedCandidateManifest.from_summary(previous)
        self.event_ready = False
        self.last_error = ""
        self.last_poll_at = 0.0

    def _strict_manifest(
        self,
        pool: Mapping[str, Any],
        *,
        now_ts: float,
    ) -> ValidatedCandidateManifest:
        expected_rule_parameters = {
            "market_cap_max_usd": float(
                self.settings.altcoin_contract_anomaly_market_cap_max_usd
            ),
            "short_squeeze_min_ratio": float(
                self.settings.altcoin_contract_anomaly_short_squeeze_min_oi_market_cap_ratio
            ),
            "short_squeeze_max_funding_rate": float(
                self.settings.altcoin_contract_anomaly_short_squeeze_max_funding_rate
            ),
            "high_leverage_min_ratio": float(
                self.settings.altcoin_contract_anomaly_high_leverage_min_oi_market_cap_ratio
            ),
        }
        if pool.get("rule_parameters") != expected_rule_parameters:
            raise AltcoinAnomalyDataUnavailable("candidate_manifest_rules_mismatch")
        generated_at = str(pool.get("generated_at") or "")
        generated_ts = _epoch_seconds(generated_at)
        max_age = max(1, int(getattr(
            self.settings,
            "altcoin_contract_anomaly_manifest_max_age_sec",
            1200,
        ) or 1200))
        if (
            generated_ts is None
            or now_ts - generated_ts > max_age
            or generated_ts - now_ts > 300
        ):
            raise AltcoinAnomalyDataUnavailable("candidate_manifest_stale")

        snapshots = {
            str(row.get("symbol") or "").upper(): row
            for row in pool.get("snapshots") or []
            if isinstance(row, Mapping) and row.get("symbol")
        }
        mappings = {
            str(row.get("binance_symbol") or "").upper(): row
            for row in pool.get("mappings") or []
            if isinstance(row, Mapping) and row.get("binance_symbol")
        }
        candidates: dict[str, dict[str, Any]] = {}
        for raw_symbol in pool.get("candidate_symbols") or []:
            symbol = str(raw_symbol).upper()
            row = snapshots.get(symbol)
            mapping = mappings.get(symbol)
            if row is None or mapping is None:
                raise AltcoinAnomalyDataUnavailable("candidate_manifest_incomplete")
            tags = list(row.get("candidate_tags") or [])
            unavailable = (
                list(row.get("missing_fields") or [])
                + list(row.get("stale_fields") or [])
                + list(row.get("invalid_fields") or [])
            )
            formal = (
                row.get("mapping_method") in FORMAL_MAPPING_METHODS
                and row.get("mapping_confidence") == "high"
                and isinstance(row.get("cmc_id"), int)
                and not isinstance(row.get("cmc_id"), bool)
                and int(row.get("cmc_id")) > 0
            )
            mapping_consistent = (
                mapping.get("cmc_id") == row.get("cmc_id")
                and mapping.get("mapping_method") == row.get("mapping_method")
                and mapping.get("mapping_confidence") == row.get("mapping_confidence")
            )
            if (
                not symbol.endswith("USDT")
                or str(row.get("exchange") or "").upper() != "BINANCE"
                or str(row.get("contract_type") or "").upper() != "USDT_PERPETUAL"
                or row.get("data_quality") != "complete"
                or unavailable
                or not formal
                or not mapping_consistent
                or not tags
            ):
                raise AltcoinAnomalyDataUnavailable("candidate_manifest_untrusted")
            required_times = {
                "market_cap_updated_at": int(
                    self.settings.altcoin_contract_anomaly_cmc_max_data_age_sec
                ),
                "open_interest_updated_at": int(
                    self.settings.altcoin_contract_anomaly_binance_oi_max_age_sec
                ),
                "mark_price_updated_at": int(
                    self.settings.altcoin_contract_anomaly_binance_oi_max_age_sec
                ),
                "funding_rate_updated_at": int(
                    self.settings.altcoin_contract_anomaly_funding_max_age_sec
                ),
            }
            for field_name, field_max_age in required_times.items():
                field_time = _epoch_seconds(row.get(field_name))
                if (
                    field_time is None
                    or generated_ts - field_time > field_max_age
                    or field_time - generated_ts > 300
                ):
                    raise AltcoinAnomalyDataUnavailable(
                        f"candidate_manifest_field_stale:{field_name}"
                    )
            candidates[symbol] = dict(row)
        return ValidatedCandidateManifest(
            generated_at=generated_at,
            candidate_pool_hash=str(pool.get("candidate_pool_hash") or ""),
            candidate_snapshot_hash=str(pool.get("candidate_snapshot_hash") or ""),
            rules_fingerprint=str(pool.get("rules_fingerprint") or ""),
            rules_version=str(pool.get("rules_version") or ""),
            candidates=dict(sorted(candidates.items())),
        )

    def poll(self, *, now_ts: float | None = None) -> dict[str, Any]:
        now = float(now_ts if now_ts is not None else time.time())
        self.last_poll_at = now
        try:
            # CandidatePoolStore performs the complete P1 schema, module, rules
            # version, fingerprint, membership and snapshot hash validation.
            pool = self.pool_store.load()
            if pool is None:
                raise AltcoinAnomalyDataUnavailable("candidate_manifest_missing")
            # P2 has its own manifest age gate. Reusing P1's cache-only loader
            # would silently inherit the shorter P1 OI cache age and make a
            # 12--15 minute realtime observation session degrade mid-run.
            manifest = self._strict_manifest(pool, now_ts=now)
        except (AltcoinAnomalyDataUnavailable, CandidateStateError, OSError, ValueError) as exc:
            self.event_ready = False
            self.last_error = str(exc)[:160] or type(exc).__name__
            return {
                "status": "manifest_degraded",
                "reason": self.last_error,
                "changed": False,
                "retained_candidate_count": len(self.last_valid.symbols) if self.last_valid else 0,
            }

        previous = self.last_valid
        changed = previous is None or (
            manifest.candidate_pool_hash != previous.candidate_pool_hash
            or manifest.candidate_snapshot_hash != previous.candidate_snapshot_hash
        )
        self.last_valid = manifest
        self.event_ready = True
        self.last_error = ""
        return {
            "status": "valid_changed" if changed else "valid_unchanged",
            "reason": "",
            "changed": changed,
            "candidate_count": len(manifest.symbols),
            "candidate_symbols": list(manifest.symbols),
            "candidate_pool_hash": manifest.candidate_pool_hash,
            "candidate_snapshot_hash": manifest.candidate_snapshot_hash,
            "rules_fingerprint": manifest.rules_fingerprint,
        }


class CandidateMarkPriceBook(MarkPriceBook):
    """Compatibility parser around the shared closed-window mark book."""

    @staticmethod
    def _normalize(payload: Mapping[str, Any]) -> dict[str, Any] | None:
        source = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
        symbol = str(source.get("symbol") or source.get("s") or "").upper()
        mark = _number(source.get("mark_price", source.get("p")), minimum=0.0)
        funding = _number(source.get("funding_rate", source.get("r")))
        raw_event_ms = _number(source.get("event_time_ms", source.get("E")), minimum=0.0)
        raw_next_ms = _number(source.get("next_funding_time_ms", source.get("T")), minimum=0.0)
        event_ts = (
            raw_event_ms / 1000.0
            if raw_event_ms is not None
            else _epoch_seconds(source.get("event_time"))
        )
        next_ts = (
            raw_next_ms / 1000.0
            if raw_next_ms is not None
            else _epoch_seconds(source.get("next_funding_time"))
        )
        if (
            not symbol.endswith("USDT")
            or mark is None
            or mark <= 0
            or funding is None
            or event_ts is None
            or next_ts is None
        ):
            return None
        return {
            "symbol": symbol,
            "mark_price": mark,
            "funding_rate": funding,
            "event_time_ms": int(event_ts * 1000),
            "next_funding_time_ms": int(next_ts * 1000),
            "source": str(source.get("source") or "binance_mark_price_stream"),
        }

    def apply(
        self,
        payload: Mapping[str, Any] | MarkPriceUpdate,
        *,
        subscription_epoch: str = "",
    ) -> bool:
        if isinstance(payload, MarkPriceUpdate):
            return self.update(payload, subscription_epoch=subscription_epoch)
        row = self._normalize(payload)
        if row is None:
            return False
        update = MarkPriceUpdate(
            symbol=str(row["symbol"]),
            mark_price=float(row["mark_price"]),
            funding_rate=float(row["funding_rate"]),
            next_funding_time_ms=int(row["next_funding_time_ms"]),
            event_time_ms=int(row["event_time_ms"]),
            source=str(row.get("source") or "binance_ws_mark_price"),
        )
        return self.update(update, subscription_epoch=subscription_epoch)


class ClosedRealtimeFeatureBuilder:
    def __init__(self, settings: Settings, store: RealtimeFeatureStore) -> None:
        self.settings = settings
        self.store = store
        self.last_stats: dict[str, Any] = {
            "candidate_count": 0,
            "closed_1m_ready": 0,
            "closed_5m_ready": 0,
            "volume_baseline_ready": 0,
            "complete": 0,
            "closed_1m_coverage_ratio": 1.0,
            "closed_5m_coverage_ratio": 1.0,
            "volume_baseline_coverage_ratio": 1.0,
            "complete_coverage_ratio": 1.0,
        }

    @staticmethod
    def _valid_trade_row(row: Mapping[str, Any]) -> bool:
        return (
            int(row.get("trade_count") or 0) > 0
            and all(
                (_number(row.get(field), minimum=0.0) or 0) > 0
                for field in ("price_open", "price_high", "price_low", "price_close")
            )
            and all(
                _number(row.get(field), minimum=0.0) is not None
                for field in (
                    "trade_buy_usd",
                    "trade_sell_usd",
                    "long_liquidation_usd",
                    "short_liquidation_usd",
                )
            )
            and _number(row.get("cvd_usd")) is not None
        )

    def build_many(
        self,
        symbols: Iterable[str],
        *,
        now_ts: float,
        candidate_epochs: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        one_sec = max(1, int(getattr(
            self.settings,
            "altcoin_contract_anomaly_feature_1m_window_sec",
            60,
        ) or 60))
        five_sec = max(one_sec, int(getattr(
            self.settings,
            "altcoin_contract_anomaly_feature_5m_window_sec",
            300,
        ) or 300))
        expected_five = five_sec // one_sec if five_sec % one_sec == 0 else 0
        baseline_count = max(1, int(getattr(
            self.settings,
            "altcoin_contract_anomaly_volume_baseline_buckets",
            5,
        ) or 5))
        min_samples = max(1, int(getattr(
            self.settings,
            "altcoin_contract_anomaly_volume_min_samples",
            4,
        ) or 4))
        min_coverage = float(getattr(
            self.settings,
            "altcoin_contract_anomaly_volume_min_coverage",
            0.8,
        ) or 0.8)
        max_age = max(1, int(getattr(
            self.settings,
            "altcoin_contract_anomaly_realtime_data_max_age_sec",
            120,
        ) or 120))
        history_sec = (baseline_count + max(5, expected_five) + 3) * one_sec
        rows = self.store.recent_rows(now_ts=int(now_ts), window_sec=history_sec)
        wanted = {str(symbol).upper() for symbol in symbols}
        grouped: dict[str, dict[int, dict[str, Any]]] = {symbol: {} for symbol in wanted}
        for raw in rows:
            if not isinstance(raw, Mapping):
                continue
            symbol = str(raw.get("symbol") or "").upper()
            if (
                symbol not in wanted
                or str(raw.get("exchange") or "").lower() != "binance"
                or str(raw.get("market") or "").lower() != "futures"
                or int(raw.get("bucket_sec") or 0) != one_sec
            ):
                continue
            start = int(raw.get("bucket_start") or 0)
            if start > 0 and start + one_sec <= int(now_ts):
                grouped[symbol][start] = dict(raw)

        output: dict[str, dict[str, Any]] = {}
        for symbol in sorted(wanted):
            by_start = grouped.get(symbol) or {}
            epoch = dict((candidate_epochs or {}).get(symbol) or {})
            cutoff_ms = int(epoch.get("eligible_1m_bucket_start_ms") or 0)
            starts = sorted(
                start for start in by_start
                if not cutoff_ms or start * 1_000 >= cutoff_ms
            )
            missing: list[str] = []
            stale: list[str] = []
            if not starts:
                output[symbol] = {
                    "symbol": symbol,
                    "subscription_epoch": str(epoch.get("epoch_id") or ""),
                    "candidate_epoch_activated_at_ms": epoch.get("activated_at_ms"),
                    "data_quality": "insufficient_history",
                    "missing_fields": ["closed_1m", "closed_5m", "volume_baseline"],
                    "stale_fields": [],
                }
                continue
            latest_start = starts[-1]
            latest = by_start[latest_start]
            age_sec = max(0.0, now_ts - (latest_start + one_sec))
            if age_sec > max_age:
                stale.append("closed_1m")
            if not self._valid_trade_row(latest):
                missing.append("closed_1m")

            five_starts = [latest_start - one_sec * index for index in reversed(range(expected_five))]
            five_rows = [
                by_start.get(start)
                if not cutoff_ms or start * 1_000 >= cutoff_ms
                else None
                for start in five_starts
            ]
            if (
                expected_five != 5
                or any(row is None or not self._valid_trade_row(row) for row in five_rows)
            ):
                missing.append("closed_5m")
                valid_five: list[dict[str, Any]] = []
            else:
                valid_five = [dict(row) for row in five_rows if row is not None]

            baseline_starts = [latest_start - one_sec * index for index in range(baseline_count, 0, -1)]
            baseline_volumes = []
            for start in baseline_starts:
                if cutoff_ms and start * 1_000 < cutoff_ms:
                    continue
                row = by_start.get(start)
                if row is None or not self._valid_trade_row(row):
                    continue
                buy = _number(row.get("trade_buy_usd"), minimum=0.0)
                sell = _number(row.get("trade_sell_usd"), minimum=0.0)
                if buy is not None and sell is not None:
                    baseline_volumes.append(buy + sell)
            baseline_coverage = len(baseline_volumes) / baseline_count
            if len(baseline_volumes) < min_samples or baseline_coverage < min_coverage:
                missing.append("volume_baseline")

            buy_1m = _number(latest.get("trade_buy_usd"), minimum=0.0)
            sell_1m = _number(latest.get("trade_sell_usd"), minimum=0.0)
            volume_1m = buy_1m + sell_1m if buy_1m is not None and sell_1m is not None else None
            price_1m = _ratio(latest.get("price_close"), latest.get("price_open"))
            total_1m = volume_1m or 0.0
            buy_ratio_1m = buy_1m / total_1m if buy_1m is not None and total_1m > 0 else None
            sell_ratio_1m = sell_1m / total_1m if sell_1m is not None and total_1m > 0 else None

            if valid_five:
                buy_5m = sum(float(row.get("trade_buy_usd") or 0) for row in valid_five)
                sell_5m = sum(float(row.get("trade_sell_usd") or 0) for row in valid_five)
                volume_5m = buy_5m + sell_5m
                price_5m = _ratio(valid_five[-1].get("price_close"), valid_five[0].get("price_open"))
                buy_ratio_5m = buy_5m / volume_5m if volume_5m > 0 else None
                sell_ratio_5m = sell_5m / volume_5m if volume_5m > 0 else None
                cvd_5m = sum(float(row.get("cvd_usd") or 0) for row in valid_five)
                long_liq_5m = sum(float(row.get("long_liquidation_usd") or 0) for row in valid_five)
                short_liq_5m = sum(float(row.get("short_liquidation_usd") or 0) for row in valid_five)
            else:
                buy_5m = sell_5m = volume_5m = None
                price_5m = buy_ratio_5m = sell_ratio_5m = cvd_5m = None
                long_liq_5m = short_liq_5m = None

            sorted_baseline = sorted(baseline_volumes)
            if sorted_baseline:
                midpoint = len(sorted_baseline) // 2
                baseline = (
                    sorted_baseline[midpoint]
                    if len(sorted_baseline) % 2
                    else (sorted_baseline[midpoint - 1] + sorted_baseline[midpoint]) / 2
                )
            else:
                baseline = None
            volume_multiple = volume_1m / baseline if volume_1m is not None and baseline and baseline > 0 else None
            if baseline == 0:
                missing.append("volume_baseline")
            output[symbol] = json_safe({
                "symbol": symbol,
                "subscription_epoch": str(epoch.get("epoch_id") or ""),
                "candidate_epoch_activated_at_ms": epoch.get("activated_at_ms"),
                "window_start": _iso(latest_start),
                "window_end": _iso(latest_start + one_sec),
                "price_change_1m": price_1m,
                "price_change_5m": price_5m,
                "quote_volume_1m_usd": volume_1m,
                "quote_volume_5m_usd": volume_5m,
                "volume_anomaly_multiple": volume_multiple,
                "aggressive_buy_ratio_1m": buy_ratio_1m,
                "aggressive_sell_ratio_1m": sell_ratio_1m,
                "aggressive_buy_ratio_5m": buy_ratio_5m,
                "aggressive_sell_ratio_5m": sell_ratio_5m,
                "cvd_1m_usd": _number(latest.get("cvd_usd")),
                "cvd_5m_usd": cvd_5m,
                "long_liquidation_1m_usd": _number(latest.get("long_liquidation_usd"), minimum=0.0),
                "short_liquidation_1m_usd": _number(latest.get("short_liquidation_usd"), minimum=0.0),
                "long_liquidation_5m_usd": long_liq_5m,
                "short_liquidation_5m_usd": short_liq_5m,
                "volume_baseline_usd": baseline,
                "volume_baseline_samples": len(baseline_volumes),
                "volume_baseline_coverage": baseline_coverage,
                "window_coverage_5m": len(valid_five) / 5,
                "data_age_sec": age_sec,
                "source": "binance_websocket_closed_1m_buckets",
                "data_quality": (
                    "stale" if stale else "insufficient_history" if missing else "complete"
                ),
                "missing_fields": sorted(set(missing)),
                "stale_fields": sorted(set(stale)),
                "source_timestamps": {
                    "closed_1m_end": _iso(latest_start + one_sec),
                    "closed_5m_start": _iso(five_starts[0]) if five_starts else "",
                    "closed_5m_end": _iso(latest_start + one_sec),
                },
            })
        candidate_count = len(wanted)
        divisor = candidate_count or 1
        closed_1m_ready = sum(
            "closed_1m" not in set(row.get("missing_fields") or [])
            for row in output.values()
        )
        closed_5m_ready = sum(
            "closed_5m" not in set(row.get("missing_fields") or [])
            for row in output.values()
        )
        volume_baseline_ready = sum(
            "volume_baseline" not in set(row.get("missing_fields") or [])
            for row in output.values()
        )
        complete = sum(row.get("data_quality") == "complete" for row in output.values())
        self.last_stats = json_safe({
            "candidate_count": candidate_count,
            "closed_1m_ready": closed_1m_ready,
            "closed_5m_ready": closed_5m_ready,
            "volume_baseline_ready": volume_baseline_ready,
            "complete": complete,
            "closed_1m_coverage_ratio": closed_1m_ready / divisor if candidate_count else 1.0,
            "closed_5m_coverage_ratio": closed_5m_ready / divisor if candidate_count else 1.0,
            "volume_baseline_coverage_ratio": volume_baseline_ready / divisor if candidate_count else 1.0,
            "complete_coverage_ratio": complete / divisor if candidate_count else 1.0,
        })
        return output


class CandidateOiSampler:
    def __init__(
        self,
        settings: Settings,
        *,
        market_store: MarketSnapshotStore,
        samples: Mapping[str, list[Mapping[str, Any]]] | None = None,
        source_factory: Callable[..., Any] | None = None,
        budget_window_sec: int = 0,
    ) -> None:
        self.settings = settings
        self.market_store = market_store
        self.source_factory = source_factory
        self.samples: dict[str, list[dict[str, Any]]] = {}
        for symbol, rows in (samples or {}).items():
            normalized_symbol = str(symbol).upper()
            for item in rows:
                if isinstance(item, Mapping):
                    self._add_sample(normalized_symbol, item)
        self.last_refresh_at = 0.0
        self._last_requested_boundary: dict[str, tuple[str, int]] = {}
        self._rate_limit_latched = False
        self._rate_limit_latched_until = 0.0
        self._rate_limit_latch_resets = 0
        self._budget_window_sec = max(0, int(budget_window_sec))
        self._budget_window_started_at = 0
        self._budget_window_used = 0
        self._budget_window_resets = 0
        budget_limit = max(1, int(getattr(
            settings,
            "altcoin_contract_anomaly_realtime_oi_request_budget",
            50,
        ) or 50))
        self.last_stats: dict[str, Any] = {
            "candidate_count": 0,
            "requests": 0,
            "cache_hits": 0,
            "successes": 0,
            "failures": 0,
            "budget_used": 0,
            "budget_limit": budget_limit,
            "budget_mode": (
                "window" if self._budget_window_sec else "bounded_session"
            ),
            "budget_window_sec": self._budget_window_sec,
            "budget_window_started_at": None,
            "budget_window_used": 0,
            "budget_window_resets": 0,
            "budget_exhausted": 0,
            "rate_limit_blocked": 0,
            "rate_limit_latched": False,
            "rate_limit_latched_until": None,
            "rate_limit_latch_resets": 0,
            "http_429": 0,
            "http_418": 0,
            "refresh_rounds": 0,
            "last_round": {},
        }

    def _advance_budget_window(self, now_ts: float) -> None:
        if self._budget_window_sec <= 0:
            self.last_stats["budget_window_used"] = int(
                self.last_stats.get("budget_used") or 0
            )
            return
        window_start = int(now_ts // self._budget_window_sec) * self._budget_window_sec
        if self._budget_window_started_at <= 0:
            self._budget_window_started_at = window_start
        elif window_start > self._budget_window_started_at:
            self._budget_window_started_at = window_start
            self._budget_window_used = 0
            self._budget_window_resets += 1
        self.last_stats.update({
            "budget_window_started_at": _iso(self._budget_window_started_at),
            "budget_window_used": self._budget_window_used,
            "budget_window_resets": self._budget_window_resets,
        })

    def _advance_rate_limit_fuse(self, now_ts: float) -> None:
        if (
            self._rate_limit_latched
            and self._rate_limit_latched_until > 0
            and now_ts >= self._rate_limit_latched_until
        ):
            self._rate_limit_latched = False
            self._rate_limit_latched_until = 0.0
            self._rate_limit_latch_resets += 1
        self.last_stats.update({
            "rate_limit_latched": self._rate_limit_latched,
            "rate_limit_latched_until": (
                _iso(self._rate_limit_latched_until)
                if self._rate_limit_latched_until > 0
                else None
            ),
            "rate_limit_latch_resets": self._rate_limit_latch_resets,
        })

    def _add_sample(self, symbol: str, sample: Mapping[str, Any]) -> None:
        raw_value = sample.get("oi_value_usd")
        raw_observed_at = sample.get("observed_at")
        if (
            isinstance(raw_value, bool)
            or not isinstance(raw_value, (int, float))
            or isinstance(raw_observed_at, bool)
            or not isinstance(raw_observed_at, int)
        ):
            return
        value = _number(raw_value, minimum=0.0)
        observed_at = _epoch_seconds(raw_observed_at)
        exact_5m = sample.get("exact_5m")
        source = str(sample.get("source") or "")
        expected_source = (
            "binance_open_interest_hist.sumOpenInterestValue"
            if exact_5m is True
            else "binance_futures_batch"
        )
        if (
            value is None
            or observed_at is None
            or not isinstance(exact_5m, bool)
            or source != expected_source
            or (exact_5m and int(observed_at) % 300 != 0)
        ):
            return
        normalized = {
            "observed_at": int(observed_at),
            "oi_value_usd": value,
            "source": source,
            "exact_5m": exact_5m,
            "subscription_epoch": str(sample.get("subscription_epoch") or ""),
        }
        rows = self.samples.setdefault(symbol, [])
        rows = [row for row in rows if int(row.get("observed_at") or 0) != int(observed_at)]
        rows.append(normalized)
        rows.sort(key=lambda row: int(row.get("observed_at") or 0))
        self.samples[symbol] = rows[-12:]

    def _cached_from_market_store(
        self,
        symbol: str,
        *,
        now_ts: float,
        max_age: int,
        subscription_epoch: str = "",
    ) -> bool:
        try:
            points = self.market_store.symbol_series(
                symbol,
                start_ts=max(0, int(now_ts) - max_age),
                end_ts=int(now_ts),
                limit=8,
            )
        except Exception:
            return False
        hit = False
        for point in points:
            if not isinstance(point, Mapping) or _number(point.get("oi_usd"), minimum=0.0) is None:
                continue
            sources = tuple(str(value) for value in point.get("sources") or ())
            if not sources or any(source != "binance_futures_batch" for source in sources):
                continue
            self._add_sample(symbol, {
                "observed_at": point.get("observed_at"),
                "oi_value_usd": point.get("oi_usd"),
                "source": "+".join(sources),
                "exact_5m": False,
                "subscription_epoch": subscription_epoch,
            })
            hit = True
        return hit

    def _exact_pair(
        self,
        symbol: str,
        *,
        target_boundary: int,
        subscription_epoch: str = "",
        eligible_boundary: int = 0,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        rows = [
            row for row in self.samples.get(symbol, [])
            if row.get("exact_5m")
            and int(row.get("observed_at") or 0) in {
                int(target_boundary) - 300,
                int(target_boundary),
            }
            and (
                not subscription_epoch
                or str(row.get("subscription_epoch") or "") == subscription_epoch
            )
            and int(row.get("observed_at") or 0) >= int(eligible_boundary or 0)
        ]
        rows.sort(key=lambda row: int(row.get("observed_at") or 0))
        if len(rows) == 2 and (
            int(rows[0]["observed_at"]) == int(target_boundary) - 300
            and int(rows[1]["observed_at"]) == int(target_boundary)
        ):
            return rows[0], rows[1]
        return None

    def _make_source(self, budget: int) -> Any:
        if self.source_factory is None:
            return BinanceDataSource(self.settings, oi_hist_budget=budget)
        try:
            return self.source_factory(self.settings, budget)
        except TypeError:
            try:
                return self.source_factory(budget)
            except TypeError:
                return self.source_factory()

    def _request_priority(
        self,
        symbol: str,
        *,
        subscription_epoch: str,
    ) -> tuple[int, int, str]:
        successful_boundaries = [
            int(row.get("observed_at") or 0)
            for row in self.samples.get(symbol, [])
            if row.get("exact_5m")
            and (
                not subscription_epoch
                or str(row.get("subscription_epoch") or "")
                == subscription_epoch
            )
        ]
        if not successful_boundaries:
            return 0, 0, symbol
        return 1, max(successful_boundaries), symbol

    @staticmethod
    def _close_source(source: Any) -> None:
        if hasattr(source, "close"):
            source.close()
        elif hasattr(source, "http") and hasattr(source.http, "close"):
            source.http.close()

    def _value(
        self,
        symbol: str,
        *,
        now_ts: float,
        max_age: int,
        target_boundary: int | None = None,
        subscription_epoch: str = "",
        eligible_boundary: int = 0,
    ) -> dict[str, Any]:
        target_boundary = int(
            target_boundary
            if target_boundary is not None
            else int(now_ts // 300) * 300
        )
        pair = self._exact_pair(
            symbol,
            target_boundary=target_boundary,
            subscription_epoch=subscription_epoch,
            eligible_boundary=eligible_boundary,
        )
        if pair is not None:
            pair_age = now_ts - int(pair[1].get("observed_at") or 0)
            if not -P2_CLOCK_SKEW_TOLERANCE_SEC <= pair_age <= max_age:
                return json_safe({
                    "symbol": symbol,
                    "subscription_epoch": subscription_epoch,
                    "target_boundary": _iso(target_boundary),
                    "oi_value_usd": pair[1].get("oi_value_usd"),
                    "oi_change_5m": None,
                    "updated_at": _iso(pair[1].get("observed_at")),
                    "data_age_sec": max(0.0, pair_age),
                    "source": pair[1].get("source"),
                    "change_source": None,
                    "change_start_at": _iso(pair[0].get("observed_at")),
                    "change_end_at": _iso(pair[1].get("observed_at")),
                    "data_quality": "stale",
                    "missing_fields": [],
                    "stale_fields": ["oi_value_usd", "oi_change_5m"],
                })
        rows = [
            row for row in self.samples.get(symbol, [])
            if 0 <= now_ts - int(row.get("observed_at") or 0) <= max_age
            and int(row.get("observed_at") or 0) <= target_boundary
            and (
                not subscription_epoch
                or str(row.get("subscription_epoch") or "") == subscription_epoch
            )
        ]
        current = pair[1] if pair else (
            max(rows, key=lambda row: int(row.get("observed_at") or 0)) if rows else None
        )
        if current is None:
            return {
                "symbol": symbol,
                "subscription_epoch": subscription_epoch,
                "target_boundary": _iso(target_boundary),
                "data_quality": "partial",
                "missing_fields": [
                    "oi_value_usd",
                    "oi_change_5m",
                    "oi_window_mismatch",
                ],
            }
        change = _ratio(pair[1]["oi_value_usd"], pair[0]["oi_value_usd"]) if pair else None
        return json_safe({
            "symbol": symbol,
            "subscription_epoch": subscription_epoch,
            "target_boundary": _iso(target_boundary),
            "oi_value_usd": current["oi_value_usd"],
            "oi_change_5m": change,
            "updated_at": _iso(current["observed_at"]),
            "data_age_sec": max(0.0, now_ts - int(current["observed_at"])),
            "source": current.get("source"),
            "change_source": "binance_open_interest_hist.sumOpenInterestValue" if pair else None,
            "change_start_at": _iso(pair[0]["observed_at"]) if pair else None,
            "change_end_at": _iso(pair[1]["observed_at"]) if pair else None,
            "data_quality": "complete" if pair and change is not None else "partial",
            "missing_fields": [] if pair and change is not None else ["oi_change_5m", "oi_window_mismatch"],
        })

    def refresh(
        self,
        symbols: Iterable[str],
        *,
        now_ts: float,
        target_boundaries: Mapping[str, int] | None = None,
        candidate_epochs: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        candidates = tuple(sorted({
            str(symbol).upper()
            for symbol in symbols
            if str(symbol).upper().endswith("USDT")
        }))
        self.samples = {symbol: self.samples.get(symbol, []) for symbol in candidates}
        self._last_requested_boundary = {
            symbol: value
            for symbol, value in self._last_requested_boundary.items()
            if symbol in candidates
        }
        max_age = max(300, int(getattr(
            self.settings,
            "altcoin_contract_anomaly_realtime_oi_max_age_sec",
            600,
        ) or 600))
        default_boundary = int(now_ts // 300) * 300
        boundaries = {
            symbol: int((target_boundaries or {}).get(symbol, default_boundary))
            for symbol in candidates
        }
        epochs = {
            symbol: dict((candidate_epochs or {}).get(symbol) or {})
            for symbol in candidates
        }
        eligible_boundaries = {
            symbol: int(
                int(epochs[symbol].get("eligible_5m_boundary_ms") or 0) / 1_000
            )
            for symbol in candidates
        }
        self._advance_budget_window(now_ts)
        self._advance_rate_limit_fuse(now_ts)
        self.last_stats["candidate_count"] = len(candidates)
        due: list[str] = []
        for symbol in candidates:
            if boundaries[symbol] < eligible_boundaries[symbol]:
                continue
            epoch_id = str(epochs[symbol].get("epoch_id") or "")
            request_key = (epoch_id, boundaries[symbol])
            if self._last_requested_boundary.get(symbol) != request_key:
                due.append(symbol)
        if not due:
            return {
                symbol: self._value(
                    symbol,
                    now_ts=now_ts,
                    max_age=max_age,
                    target_boundary=boundaries[symbol],
                    subscription_epoch=str(epochs[symbol].get("epoch_id") or ""),
                    eligible_boundary=int(
                        int(epochs[symbol].get("eligible_5m_boundary_ms") or 0)
                        / 1_000
                    ),
                )
                for symbol in candidates
            }

        cache_hits = sum(
            self._cached_from_market_store(
                symbol,
                now_ts=now_ts,
                max_age=max_age,
                subscription_epoch=str(epochs[symbol].get("epoch_id") or ""),
            )
            for symbol in due
        )
        budget_limit = int(self.last_stats.get("budget_limit") or 0)
        total_used_before = int(self.last_stats.get("budget_used") or 0)
        budget_used_before = (
            self._budget_window_used
            if self._budget_window_sec > 0
            else total_used_before
        )
        remaining = max(0, budget_limit - budget_used_before)
        budget_exhausted = max(0, len(due) - remaining)
        rate_limit_blocked = min(len(due), remaining) if self._rate_limit_latched else 0
        due.sort(key=lambda symbol: self._request_priority(
            symbol,
            subscription_epoch=str(epochs[symbol].get("epoch_id") or ""),
        ))
        requestable = [] if self._rate_limit_latched else due[:remaining]
        workers = max(1, min(16, int(getattr(
            self.settings,
            "altcoin_contract_anomaly_realtime_oi_workers",
            4,
        ) or 4)))
        source = self._make_source(len(requestable)) if requestable else None
        successes = 0
        failures = 0
        try:
            if source is not None:
                def fetch(symbol: str) -> tuple[str, list[dict[str, Any]]]:
                    target = boundaries[symbol]
                    try:
                        rows = source.open_interest_hist(
                            symbol,
                            period="5m",
                            limit=2,
                            end_time=target * 1_000,
                        )
                    except TypeError:
                        rows = source.open_interest_hist(symbol, period="5m", limit=2)
                    return symbol, rows if isinstance(rows, list) else []

                with ThreadPoolExecutor(
                    max_workers=workers,
                    thread_name_prefix="altcoin-p2-oi",
                ) as executor:
                    futures = {
                        executor.submit(fetch, symbol): symbol
                        for symbol in requestable
                    }
                    for future in as_completed(futures):
                        symbol = futures[future]
                        target = boundaries[symbol]
                        epoch_id = str(epochs[symbol].get("epoch_id") or "")
                        eligible_boundary = int(
                            int(epochs[symbol].get("eligible_5m_boundary_ms") or 0)
                            / 1_000
                        )
                        try:
                            _returned, rows = future.result()
                        except Exception:
                            rows = []
                        accepted_boundaries: set[int] = set()
                        for row in rows:
                            if not isinstance(row, Mapping):
                                continue
                            timestamp = _epoch_seconds(row.get("timestamp"))
                            value = _number(row.get("sumOpenInterestValue"), minimum=0.0)
                            if timestamp is None or value is None:
                                continue
                            bucket = int(timestamp // 300) * 300
                            if (
                                bucket not in {target - 300, target}
                                or bucket < eligible_boundary
                            ):
                                continue
                            self._add_sample(symbol, {
                                "observed_at": bucket,
                                "oi_value_usd": value,
                                "source": "binance_open_interest_hist.sumOpenInterestValue",
                                "exact_5m": True,
                                "subscription_epoch": epoch_id,
                            })
                            accepted_boundaries.add(bucket)
                        if target in accepted_boundaries:
                            successes += 1
                        else:
                            failures += 1
        finally:
            if source is not None:
                self._close_source(source)

        warnings: list[str] = []
        failure_reasons: dict[str, dict[str, int]] = {}
        if source is not None and hasattr(source, "quality"):
            quality = source.quality.snapshot()
            warnings = [str(value) for value in quality.get("warnings") or []]
            raw_reasons = quality.get("failure_reasons")
            if isinstance(raw_reasons, Mapping):
                failure_reasons = {
                    str(key): {
                        str(reason): int(count)
                        for reason, count in dict(value).items()
                        if isinstance(count, int) and not isinstance(count, bool)
                    }
                    for key, value in raw_reasons.items()
                    if isinstance(value, Mapping)
                }
        if failure_reasons:
            http_429 = sum(
                count
                for reasons in failure_reasons.values()
                for reason, count in reasons.items()
                if reason == "status=429"
            )
            http_418 = sum(
                count
                for reasons in failure_reasons.values()
                for reason, count in reasons.items()
                if reason == "status=418"
            )
        else:
            http_429 = sum("429" in warning for warning in warnings)
            http_418 = sum("418" in warning for warning in warnings)
        if http_429 or http_418:
            self._rate_limit_latched = True
            fuse_seconds = max(1, int(getattr(
                self.settings,
                "fuse_seconds",
                15 * 60,
            ) or 15 * 60))
            self._rate_limit_latched_until = max(
                self._rate_limit_latched_until,
                now_ts + fuse_seconds,
            )
        for symbol in requestable:
            self._last_requested_boundary[symbol] = (
                str(epochs[symbol].get("epoch_id") or ""),
                boundaries[symbol],
            )
        requested_set = set(requestable)
        round_stats = {
            "target_boundaries": {
                symbol: boundaries[symbol] for symbol in due
            },
            "request_order": list(requestable),
            "deferred_symbols": [
                symbol for symbol in due if symbol not in requested_set
            ],
            "requests": len(requestable),
            "cache_hits": cache_hits,
            "successes": successes,
            "failures": failures + budget_exhausted + rate_limit_blocked,
            "budget_exhausted": budget_exhausted,
            "rate_limit_blocked": rate_limit_blocked,
            "http_429": http_429,
            "http_418": http_418,
            "rate_limit_latched": self._rate_limit_latched,
            "rate_limit_latched_until": (
                _iso(self._rate_limit_latched_until)
                if self._rate_limit_latched_until > 0
                else None
            ),
            "budget_window_started_at": (
                _iso(self._budget_window_started_at)
                if self._budget_window_sec > 0
                else None
            ),
            "budget_window_used_before": budget_used_before,
            "budget_window_used_after": budget_used_before + len(requestable),
        }
        if self._budget_window_sec > 0:
            self._budget_window_used += len(requestable)
        self.last_stats = {
            "candidate_count": len(candidates),
            "requests": int(self.last_stats.get("requests") or 0) + len(requestable),
            "cache_hits": int(self.last_stats.get("cache_hits") or 0) + cache_hits,
            "successes": int(self.last_stats.get("successes") or 0) + successes,
            "failures": (
                int(self.last_stats.get("failures") or 0)
                + failures
                + budget_exhausted
                + rate_limit_blocked
            ),
            "budget_used": total_used_before + len(requestable),
            "budget_limit": budget_limit,
            "budget_mode": (
                "window" if self._budget_window_sec else "bounded_session"
            ),
            "budget_window_sec": self._budget_window_sec,
            "budget_window_started_at": (
                _iso(self._budget_window_started_at)
                if self._budget_window_sec > 0
                else None
            ),
            "budget_window_used": (
                self._budget_window_used
                if self._budget_window_sec > 0
                else total_used_before + len(requestable)
            ),
            "budget_window_resets": self._budget_window_resets,
            "budget_exhausted": (
                int(self.last_stats.get("budget_exhausted") or 0)
                + budget_exhausted
            ),
            "rate_limit_blocked": (
                int(self.last_stats.get("rate_limit_blocked") or 0)
                + rate_limit_blocked
            ),
            "rate_limit_latched": self._rate_limit_latched,
            "rate_limit_latched_until": (
                _iso(self._rate_limit_latched_until)
                if self._rate_limit_latched_until > 0
                else None
            ),
            "rate_limit_latch_resets": self._rate_limit_latch_resets,
            "http_429": int(self.last_stats.get("http_429") or 0) + http_429,
            "http_418": int(self.last_stats.get("http_418") or 0) + http_418,
            "refresh_rounds": int(self.last_stats.get("refresh_rounds") or 0) + 1,
            "last_round": round_stats,
        }
        self.last_refresh_at = now_ts
        return {
            symbol: self._value(
                symbol,
                now_ts=now_ts,
                max_age=max_age,
                target_boundary=boundaries[symbol],
                subscription_epoch=str(epochs[symbol].get("epoch_id") or ""),
                eligible_boundary=int(
                    int(epochs[symbol].get("eligible_5m_boundary_ms") or 0)
                    / 1_000
                ),
            )
            for symbol in candidates
        }


class AltcoinRealtimeController:
    """P2 dry-run domain controller. It owns no websocket or Telegram gateway."""

    def __init__(
        self,
        settings: Settings,
        *,
        feature_store: RealtimeFeatureStore | None = None,
        market_store: MarketSnapshotStore | None = None,
        mark_price_book: Any | None = None,
        source_factory: Callable[..., Any] | None = None,
        observation_state: RealtimeObservationState | None = None,
        manifest_consumer: CandidateManifestConsumer | None = None,
        oi_budget_window_sec: int = 0,
    ) -> None:
        self.settings = settings
        state_path = Path(getattr(
            settings,
            "altcoin_contract_anomaly_realtime_state_path",
            settings.data_dir / "altcoin_contract_anomaly_p2_state.json",
        ))
        event_path = Path(getattr(
            settings,
            "altcoin_contract_anomaly_realtime_event_path",
            settings.data_dir / "altcoin_contract_anomaly_p2_events.jsonl",
        ))
        self.state_store = observation_state or RealtimeObservationState(state_path, event_path)
        self.manifest_consumer = manifest_consumer or CandidateManifestConsumer(
            settings,
            previous=self.state_store.last_valid_manifest,
        )
        self.feature_builder = ClosedRealtimeFeatureBuilder(
            settings,
            feature_store or RealtimeFeatureStore(settings.realtime_features_db_path),
        )
        self.oi_sampler = CandidateOiSampler(
            settings,
            market_store=market_store or MarketSnapshotStore(settings.market_snapshots_db_path),
            samples=self.state_store.oi_samples,
            source_factory=source_factory,
            budget_window_sec=oi_budget_window_sec,
        )
        self.mark_price_book = mark_price_book or MarkPriceBook()
        self._symbol_states = self.state_store.symbol_states
        self._recent_events: list[dict[str, Any]] = []
        self._manifest_event_ready = False
        self._last_now_ts = 0.0
        self._stats: dict[str, Any] = {
            "manifest_polls": 0,
            "manifest_failures": 0,
            "feature_evaluations": 0,
            "last_evaluation_candidate_count": 0,
            "last_evaluation_complete_count": 0,
            "last_evaluation_complete_ratio": 1.0,
            "last_evaluation_epoch_complete_count": 0,
            "last_evaluation_funding_complete_count": 0,
            "aligned_evaluation_rounds": 0,
            "non_aligned_evaluation_skips": 0,
            "last_aligned_evaluation_at": "",
            "data_quality_skips": 0,
            "data_quality_skip_reasons": {},
            "mark_price_messages": 0,
            "mark_price_rejected": 0,
            "events": {key: 0 for key in EVENT_NAMES_CN},
            "last_event_at": "",
            "last_error": "",
        }

    def _record_skip(self, reason: str, *, count: int = 1) -> None:
        safe_reason = reason if reason in {
            "disabled",
            "manifest_degraded",
            "capacity_degraded",
            "subscription_degraded",
            "websocket_stale",
            "feature_stale",
            "insufficient_history",
            "feature_invalid",
            "mark_missing",
            "mark_stale",
            "funding_missing",
            "funding_change_missing",
            "oi_missing",
            "oi_stale",
            "oi_insufficient_history",
        } else "feature_invalid"
        increment = max(0, int(count))
        reasons = self._stats["data_quality_skip_reasons"]
        reasons[safe_reason] = int(reasons.get(safe_reason, 0)) + increment

    @property
    def enabled(self) -> bool:
        return bool(getattr(self.settings, "altcoin_contract_anomaly_realtime_enable", False))

    @property
    def candidate_symbols(self) -> tuple[str, ...]:
        manifest = self.manifest_consumer.last_valid
        return manifest.symbols if manifest is not None else ()

    @property
    def manifest_event_ready(self) -> bool:
        return self._manifest_event_ready

    @property
    def recent_events(self) -> list[dict[str, Any]]:
        return [dict(event) for event in self._recent_events]

    def _record_event(self, event: dict[str, Any]) -> bool:
        if not self.state_store.record_event(
            event,
            symbol_states=self._symbol_states,
            oi_samples=self.oi_sampler.samples,
        ):
            return False
        self._recent_events.append(event)
        self._recent_events = self._recent_events[-200:]
        event_type = str(event["event_type"])
        self._stats["events"][event_type] = int(self._stats["events"].get(event_type, 0)) + 1
        self._stats["last_event_at"] = event["observed_at"]
        return True

    def _manifest_invalidation_event(
        self,
        *,
        old: ValidatedCandidateManifest,
        new: ValidatedCandidateManifest,
        symbol: str,
        now_ts: float,
    ) -> dict[str, Any]:
        old_row = old.candidates[symbol]
        new_row = new.candidates.get(symbol, {})
        old_tags = sorted(str(value) for value in old_row.get("candidate_tags") or [])
        new_tags = sorted(str(value) for value in new_row.get("candidate_tags") or [])
        event_id = deterministic_event_id(
            rules_version=P2_RULES_VERSION,
            event_type="candidate_condition_invalidated",
            symbol=symbol,
            direction="mixed",
            window_end=new.generated_at,
            candidate_pool_hash=new.candidate_pool_hash,
            candidate_snapshot_hash=new.candidate_snapshot_hash,
        )
        return {
            "schema_version": P2_SCHEMA_VERSION,
            "rules_version": P2_RULES_VERSION,
            "event_id": event_id,
            "event_type": "candidate_condition_invalidated",
            "event_name_cn": EVENT_NAMES_CN["candidate_condition_invalidated"],
            "symbol": symbol,
            "direction": "mixed",
            "observed_at": _iso(now_ts),
            "window_start": old.generated_at,
            "window_end": new.generated_at,
            "candidate_pool_hash": new.candidate_pool_hash,
            "candidate_snapshot_hash": new.candidate_snapshot_hash,
            "candidate_tags": old_tags,
            "matched_candidate_rules": list(old_row.get("matched_rules") or []),
            # This is intentionally a manifest-transition exception to the realtime
            # two-factor gate; inventing two market factors would be misleading.
            "confirmed_factor_families": [],
            "factor_values": {
                "previous_candidate_tags": old_tags,
                "current_candidate_tags": new_tags,
                "removed_from_pool": symbol not in new.candidates,
            },
            "factor_thresholds": {},
            "source_timestamps": {
                "previous_manifest": old.generated_at,
                "current_manifest": new.generated_at,
            },
            "data_quality": "complete",
            "missing_factors": [],
            "stale_factors": [],
            "subscription_generation": 0,
            "candidate_subscription_epoch": "",
            "candidate_epoch_activated_at": "",
            "dry_run": True,
        }

    def poll_manifest(self, *, now_ts: float | None = None) -> dict[str, Any]:
        now = float(now_ts if now_ts is not None else time.time())
        self._last_now_ts = now
        if not self.enabled:
            self._manifest_event_ready = False
            return {"status": "disabled", "changed": False}
        previous = self.manifest_consumer.last_valid
        result = self.manifest_consumer.poll(now_ts=now)
        self._stats["manifest_polls"] += 1
        self._manifest_event_ready = result.get("status") in {"valid_changed", "valid_unchanged"}
        if not self._manifest_event_ready:
            self._stats["manifest_failures"] += 1
            self._stats["last_error"] = str(result.get("reason") or "manifest_degraded")
            return result
        current = self.manifest_consumer.last_valid
        invalidation_events: list[dict[str, Any]] = []
        pending_symbol_states = copy.deepcopy(self._symbol_states)
        if previous is not None and current is not None and result.get("changed"):
            for symbol, old_row in previous.candidates.items():
                current_tags = set((current.candidates.get(symbol) or {}).get("candidate_tags") or [])
                old_tags = set(old_row.get("candidate_tags") or [])
                if symbol not in current.candidates or not old_tags.issubset(current_tags):
                    invalidation_events.append(self._manifest_invalidation_event(
                        old=previous,
                        new=current,
                        symbol=symbol,
                        now_ts=now,
                    ))
                    pending_symbol_states.pop(symbol, None)
        if current is None:
            return {**result, "events": []}

        # The manifest transition and all of its invalidation events are one
        # recoverable unit. Production observation state admits the complete
        # batch to its WAL before this call advances last_valid_manifest.
        try:
            newly_appended = set(self.state_store.record_event_batch(
                invalidation_events,
                last_valid_manifest=current.summary(),
                symbol_states=pending_symbol_states,
                oi_samples=self.oi_sampler.samples,
            ))
        except Exception as exc:
            # CandidateManifestConsumer.poll() has already moved last_valid in
            # memory. Roll it back so the same deterministic transition is
            # retried on the next poll after a WAL/disk failure.
            self.manifest_consumer.last_valid = previous
            self.manifest_consumer.event_ready = False
            self.manifest_consumer.last_error = (
                f"manifest_transition_persist_failed:{type(exc).__name__}"
            )
            self._manifest_event_ready = False
            self._stats["manifest_failures"] += 1
            self._stats["last_error"] = self.manifest_consumer.last_error
            return {
                "status": "manifest_degraded",
                "reason": self.manifest_consumer.last_error,
                "changed": False,
                "retained_candidate_count": len(previous.symbols) if previous else 0,
                "events": [],
            }

        self._symbol_states = pending_symbol_states
        self._stats["last_error"] = ""
        emitted = [
            event for event in invalidation_events
            if str(event.get("event_id") or "") in newly_appended
        ]
        for event in emitted:
            self._recent_events.append(event)
            event_type = str(event["event_type"])
            self._stats["events"][event_type] = int(
                self._stats["events"].get(event_type, 0)
            ) + 1
            self._stats["last_event_at"] = event["observed_at"]
        self._recent_events = self._recent_events[-200:]
        return {**result, "events": emitted}

    @staticmethod
    def _payload_symbol(payload: Mapping[str, Any] | MarkPriceUpdate) -> str:
        if isinstance(payload, MarkPriceUpdate):
            return str(payload.symbol).upper()
        source = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
        return str(source.get("symbol") or source.get("s") or "").upper()

    @staticmethod
    def _shared_mark_update(
        payload: Mapping[str, Any] | MarkPriceUpdate,
    ) -> MarkPriceUpdate | None:
        if isinstance(payload, MarkPriceUpdate):
            return payload
        parsed = parse_binance_mark_price_update(payload)
        if parsed is not None:
            return parsed
        source = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
        symbol = str(source.get("symbol") or source.get("s") or "").upper()
        mark_price = _number(source.get("mark_price"), minimum=0.0)
        funding_rate = _number(source.get("funding_rate"))
        event_time_ms = _number(source.get("event_time_ms"), minimum=0.0)
        next_funding_time_ms = _number(
            source.get("next_funding_time_ms"),
            minimum=0.0,
        )
        if (
            not symbol.endswith("USDT")
            or mark_price is None
            or mark_price <= 0
            or funding_rate is None
            or event_time_ms is None
            or event_time_ms <= 0
            or next_funding_time_ms is None
            or next_funding_time_ms <= 0
        ):
            return None
        return MarkPriceUpdate(
            symbol=symbol,
            mark_price=mark_price,
            funding_rate=funding_rate,
            next_funding_time_ms=int(next_funding_time_ms),
            event_time_ms=int(event_time_ms),
            exchange=str(source.get("exchange") or "binance").lower(),
            market=str(source.get("market") or "futures").lower(),
            source=str(source.get("source") or "binance_ws_mark_price"),
        )

    def handle_mark_price(
        self,
        update: Mapping[str, Any] | MarkPriceUpdate,
        *,
        subscription_epoch: str = "",
    ) -> bool:
        symbol = self._payload_symbol(update)
        if symbol not in set(self.candidate_symbols):
            self._stats["mark_price_rejected"] += 1
            return False
        try:
            apply_method = getattr(self.mark_price_book, "apply", None)
            if callable(apply_method):
                accepted = bool(apply_method(
                    update,
                    subscription_epoch=subscription_epoch,
                ))
            else:
                parsed = self._shared_mark_update(update)
                accepted = bool(self.mark_price_book.update(
                    parsed,
                    subscription_epoch=subscription_epoch,
                ))
        except (AttributeError, TypeError, ValueError):
            accepted = False
        if accepted:
            self._stats["mark_price_messages"] += 1
        else:
            self._stats["mark_price_rejected"] += 1
        return accepted

    def _mark_snapshot(
        self,
        symbol: str,
        *,
        feature: Mapping[str, Any],
        epoch: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        timestamps = dict(feature.get("source_timestamps") or {})
        window_start = _epoch_seconds(timestamps.get("closed_5m_start"))
        window_end = _epoch_seconds(timestamps.get("closed_5m_end"))
        snapshot_window = getattr(self.mark_price_book, "snapshot_window", None)
        if callable(snapshot_window) and window_start is not None and window_end is not None:
            max_gap_sec = max(1, int(getattr(
                self.settings,
                "altcoin_contract_anomaly_funding_max_gap_sec",
                15,
            ) or 15))
            try:
                value = snapshot_window(
                    symbol,
                    window_end_ms=int(window_end * 1_000),
                    window_sec=int(round(window_end - window_start)),
                    subscription_epoch=str(epoch.get("epoch_id") or ""),
                    epoch_started_ms=int(epoch.get("activated_at_ms") or 0),
                    max_gap_ms=max_gap_sec * 1_000,
                )
            except (KeyError, TypeError, ValueError):
                value = None
            if isinstance(value, Mapping):
                return dict(value)
        for name in ("snapshot", "get", "latest"):
            method = getattr(self.mark_price_book, name, None)
            if not callable(method):
                continue
            try:
                value = method(symbol)
            except (KeyError, TypeError, ValueError):
                continue
            if isinstance(value, Mapping):
                return dict(value)
        return None

    def _subscription_gate(
        self,
        status: Mapping[str, Any],
        *,
        now_ts: float,
    ) -> tuple[bool, str, int, dict[str, dict[str, Any]]]:
        generation = int(status.get("subscription_generation") or 0)
        if status.get("candidate_capacity_degraded"):
            return False, "capacity_degraded", generation, {}
        if status.get("manifest_degraded"):
            return False, "manifest_degraded", generation, {}
        connected = status.get("connected")
        if connected is None:
            connected = str(status.get("connection_state") or "").lower() in {"connected", "ready"}
        if not connected:
            return False, "subscription_degraded", generation, {}
        last_receive = _epoch_seconds(status.get("last_receive_ms", status.get("last_receive_at")))
        max_age = max(1, int(getattr(
            self.settings,
            "altcoin_contract_anomaly_realtime_data_max_age_sec",
            120,
        ) or 120))
        if (
            last_receive is None
            or now_ts - last_receive > max_age
            or last_receive - now_ts > P2_CLOCK_SKEW_TOLERANCE_SEC
        ):
            return False, "stale", generation, {}
        active = status.get("active_candidate_symbols", status.get("candidate_symbols_active"))
        active_symbols = {
            str(value).upper() for value in active or []
            if str(value).upper().endswith("USDT")
        }
        streams = {str(value).lower() for value in status.get("active_subscriptions") or []}
        if not active_symbols and streams:
            active_symbols = {
                stream.split("@", 1)[0].upper()
                for stream in streams if stream.endswith("@aggtrade")
            }
        coverage_explicit = status.get("candidate_coverage_complete")
        covered = (
            bool(coverage_explicit)
            if coverage_explicit is not None
            else set(self.candidate_symbols).issubset(active_symbols)
        )
        force_order = status.get("force_order_active")
        if force_order is None:
            force_order = "!forceorder@arr" in streams
        if not covered or not force_order:
            return False, "subscription_degraded", generation, {}
        raw_epochs = status.get("candidate_epochs")
        if not isinstance(raw_epochs, Mapping):
            return False, "subscription_degraded", generation, {}
        epochs: dict[str, dict[str, Any]] = {}
        for symbol in self.candidate_symbols:
            row = raw_epochs.get(symbol)
            if not isinstance(row, Mapping):
                return False, "subscription_degraded", generation, {}
            epoch_id = str(row.get("epoch_id") or "")
            activated_at_ms = row.get("activated_at_ms")
            eligible_1m = row.get("eligible_1m_bucket_start_ms")
            eligible_5m = row.get("eligible_5m_boundary_ms")
            if (
                not epoch_id
                or isinstance(activated_at_ms, bool)
                or not isinstance(activated_at_ms, int)
                or activated_at_ms <= 0
                or isinstance(eligible_1m, bool)
                or not isinstance(eligible_1m, int)
                or isinstance(eligible_5m, bool)
                or not isinstance(eligible_5m, int)
            ):
                return False, "subscription_degraded", generation, {}
            epochs[symbol] = dict(row)
        return True, "complete", generation, epochs

    def _thresholds(self) -> dict[str, float]:
        def value(name: str, default: float) -> float:
            parsed = _number(getattr(self.settings, name, default))
            return parsed if parsed is not None else default
        return {
            "price_1m_move_ratio": value("altcoin_contract_anomaly_price_1m_move_ratio", 0.01),
            "price_5m_move_ratio": value("altcoin_contract_anomaly_price_5m_move_ratio", 0.02),
            "volume_expansion_ratio": value("altcoin_contract_anomaly_volume_expansion_ratio", 2.0),
            "aggressive_buy_ratio": value("altcoin_contract_anomaly_aggressive_buy_ratio", 0.60),
            "aggressive_sell_ratio": value("altcoin_contract_anomaly_aggressive_sell_ratio", 0.60),
            "open_interest_move_ratio": value("altcoin_contract_anomaly_open_interest_move_ratio", 0.03),
            "funding_positive_rate": value("altcoin_contract_anomaly_funding_positive_rate", 0.0001),
            "funding_change_ratio": value("altcoin_contract_anomaly_funding_change_ratio", 0.00005),
            "liquidation_min_usd": value("altcoin_contract_anomaly_liquidation_min_usd", 100_000.0),
            "price_stall_ratio": value("altcoin_contract_anomaly_price_stall_ratio", 0.003),
            "weakening_volume_ratio": value("altcoin_contract_anomaly_weakening_volume_ratio", 1.1),
        }

    @staticmethod
    def _factor_values(
        candidate: Mapping[str, Any],
        features: Mapping[str, Any],
        mark: Mapping[str, Any],
        oi: Mapping[str, Any],
    ) -> dict[str, Any]:
        market_cap_usd = _number(candidate.get("market_cap_usd"), minimum=0.0)
        oi_value_usd = _number(oi.get("oi_value_usd"), minimum=0.0)
        oi_market_cap_ratio = (
            oi_value_usd / market_cap_usd
            if oi_value_usd is not None
            and market_cap_usd is not None
            and market_cap_usd > 0
            else None
        )
        return json_safe({
            **{key: value for key, value in features.items() if key not in {"missing_fields", "stale_fields"}},
            # Keep the displayed trio internally coherent: the current closed
            # OI point divided by the trusted market cap carried by this exact
            # manifest, never the older P1 ratio snapshot.
            "market_cap_usd": market_cap_usd,
            "oi_value_usd": oi_value_usd,
            "oi_market_cap_ratio": oi_market_cap_ratio,
            "mark_price": mark.get("mark_price"),
            "funding_rate": mark.get("funding_rate"),
            "funding_rate_start_5m": mark.get("funding_rate_start_5m"),
            "funding_rate_end_5m": mark.get("funding_rate_end_5m"),
            "funding_rate_change_5m": mark.get("funding_rate_change_5m"),
            "oi_change_5m": oi.get("oi_change_5m"),
        })

    def _factors(
        self,
        features: Mapping[str, Any],
        mark: Mapping[str, Any],
        oi: Mapping[str, Any],
        thresholds: Mapping[str, float],
    ) -> dict[str, dict[str, Any]]:
        p1 = float(features["price_change_1m"])
        p5 = float(features["price_change_5m"])
        volume = float(features["volume_anomaly_multiple"])
        buy = float(features["aggressive_buy_ratio_5m"])
        sell = float(features["aggressive_sell_ratio_5m"])
        cvd = float(features["cvd_5m_usd"])
        oi_change = float(oi["oi_change_5m"])
        funding = float(mark["funding_rate"])
        funding_change = float(mark["funding_rate_change_5m"])
        short_liq = float(features["short_liquidation_5m_usd"])
        long_liq = float(features["long_liquidation_5m_usd"])
        price_up = p1 >= thresholds["price_1m_move_ratio"] or p5 >= thresholds["price_5m_move_ratio"]
        price_down = p1 <= -thresholds["price_1m_move_ratio"] or p5 <= -thresholds["price_5m_move_ratio"]
        return {
            "price_momentum": {
                "confirmed": price_up or price_down,
                "direction": "up" if price_up and not price_down else "down" if price_down and not price_up else "mixed" if price_up else "flat",
                "stall": abs(p1) <= thresholds["price_stall_ratio"] and abs(p5) <= thresholds["price_stall_ratio"],
            },
            "volume_expansion": {
                "confirmed": volume >= thresholds["volume_expansion_ratio"],
                "direction": "mixed",
            },
            "aggressive_flow": {
                # The configured sell threshold is the upper bound for the
                # aggressive-buy share (default 0.40), while the structured
                # feature still exposes both complementary ratios.
                "confirmed": (buy >= thresholds["aggressive_buy_ratio"] and cvd > 0) or (buy <= thresholds["aggressive_sell_ratio"] and sell > buy and cvd < 0),
                "direction": "up" if buy >= thresholds["aggressive_buy_ratio"] and cvd > 0 else "down" if buy <= thresholds["aggressive_sell_ratio"] and sell > buy and cvd < 0 else "mixed",
            },
            "open_interest": {
                "confirmed": abs(oi_change) >= thresholds["open_interest_move_ratio"],
                "direction": "up" if oi_change >= thresholds["open_interest_move_ratio"] else "down" if oi_change <= -thresholds["open_interest_move_ratio"] else "mixed",
            },
            "funding": {
                "confirmed": funding >= thresholds["funding_positive_rate"] or abs(funding_change) >= thresholds["funding_change_ratio"],
                "direction": "up" if funding >= thresholds["funding_positive_rate"] or funding_change >= thresholds["funding_change_ratio"] else "down" if funding_change <= -thresholds["funding_change_ratio"] else "mixed",
            },
            "liquidation": {
                "confirmed": short_liq >= thresholds["liquidation_min_usd"] or long_liq >= thresholds["liquidation_min_usd"],
                "direction": "up" if short_liq >= thresholds["liquidation_min_usd"] and short_liq > long_liq else "down" if long_liq >= thresholds["liquidation_min_usd"] and long_liq > short_liq else "mixed",
            },
        }

    def _event(
        self,
        *,
        event_type: str,
        symbol: str,
        direction: str,
        candidate: Mapping[str, Any],
        features: Mapping[str, Any],
        mark: Mapping[str, Any],
        oi: Mapping[str, Any],
        factors: Iterable[str],
        thresholds: Mapping[str, float],
        now_ts: float,
        subscription_generation: int,
        candidate_epoch: Mapping[str, Any],
    ) -> dict[str, Any]:
        manifest = self.manifest_consumer.last_valid
        if manifest is None:
            raise RuntimeError("valid manifest is required")
        families = sorted(set(factors), key=FACTOR_FAMILIES.index)
        window_end = str(features.get("window_end") or _iso(now_ts))
        event_id = deterministic_event_id(
            rules_version=P2_RULES_VERSION,
            event_type=event_type,
            symbol=symbol,
            direction=direction,
            window_end=window_end,
            candidate_pool_hash=manifest.candidate_pool_hash,
            candidate_snapshot_hash=manifest.candidate_snapshot_hash,
        )
        return json_safe({
            "schema_version": P2_SCHEMA_VERSION,
            "rules_version": P2_RULES_VERSION,
            "event_id": event_id,
            "event_type": event_type,
            "event_name_cn": EVENT_NAMES_CN[event_type],
            "symbol": symbol,
            "direction": direction,
            "observed_at": _iso(now_ts),
            "window_start": features.get("window_start"),
            "window_end": window_end,
            "candidate_pool_hash": manifest.candidate_pool_hash,
            "candidate_snapshot_hash": manifest.candidate_snapshot_hash,
            "candidate_tags": list(candidate.get("candidate_tags") or []),
            "matched_candidate_rules": list(candidate.get("matched_rules") or []),
            "confirmed_factor_families": families,
            "factor_values": self._factor_values(candidate, features, mark, oi),
            "factor_thresholds": dict(thresholds),
            "source_timestamps": {
                **dict(features.get("source_timestamps") or {}),
                "mark_price": _iso(mark.get("event_time_ms")),
                "funding_change_start_5m": _iso(
                    mark.get("funding_window_start_event_time_ms")
                ),
                "funding_change_end_5m": _iso(
                    mark.get("funding_window_end_event_time_ms")
                ),
                "oi_change_start": oi.get("change_start_at"),
                "oi_change_end": oi.get("change_end_at"),
            },
            "data_quality": "complete",
            "missing_factors": [],
            "stale_factors": [],
            "subscription_generation": subscription_generation,
            "candidate_subscription_epoch": str(candidate_epoch.get("epoch_id") or ""),
            "candidate_epoch_activated_at": _iso(candidate_epoch.get("activated_at_ms")),
            "dry_run": True,
        })

    def _candidate_events(
        self,
        *,
        symbol: str,
        candidate: Mapping[str, Any],
        features: Mapping[str, Any],
        mark: Mapping[str, Any],
        oi: Mapping[str, Any],
        now_ts: float,
        generation: int,
        candidate_epoch: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        manifest = self.manifest_consumer.last_valid
        evaluation_key = (
            f"{manifest.candidate_snapshot_hash if manifest else ''}:"
            f"{features.get('window_end') or ''}:"
            f"{candidate_epoch.get('epoch_id') or ''}"
        )
        symbol_state = self._symbol_states.setdefault(symbol, {})
        if symbol_state.get("last_evaluation_key") == evaluation_key:
            return []
        symbol_state["last_evaluation_key"] = evaluation_key
        thresholds = self._thresholds()
        factor = self._factors(features, mark, oi, thresholds)
        tags = set(candidate.get("candidate_tags") or [])
        current_funding = float(mark["funding_rate"])
        short_candidate_funding_limit = float(getattr(
            self.settings,
            "altcoin_contract_anomaly_short_squeeze_max_funding_rate",
            0.0,
        ))
        short_candidate_basis_valid = (
            current_funding < short_candidate_funding_limit
        )
        matched: list[tuple[str, str, list[str]]] = []

        fuel_families = [
            family for family, direction in (
                ("open_interest", "up"),
                ("funding", "down"),
                ("volume_expansion", "mixed"),
                ("aggressive_flow", "down"),
            )
            if factor[family]["confirmed"]
            and (direction == "mixed" or factor[family]["direction"] == direction)
        ]
        rapid_up = factor["price_momentum"]["confirmed"] and factor["price_momentum"]["direction"] == "up"
        if (
            SHORT_SQUEEZE_CANDIDATE in tags
            and short_candidate_basis_valid
            and not rapid_up
            and len(fuel_families) >= 2
        ):
            matched.append(("short_fuel_building", "mixed", fuel_families))

        ignition_extra = [
            family for family, direction in (
                ("volume_expansion", "mixed"),
                ("aggressive_flow", "up"),
                ("liquidation", "up"),
                ("open_interest", "down"),
            )
            if factor[family]["confirmed"]
            and (direction == "mixed" or factor[family]["direction"] == direction)
        ]
        if (
            SHORT_SQUEEZE_CANDIDATE in tags
            and short_candidate_basis_valid
            and rapid_up
            and ignition_extra
        ):
            matched.append(("short_squeeze_ignition", "up", ["price_momentum", *ignition_extra]))

        price_or_volume = factor["price_momentum"]["confirmed"] or factor["volume_expansion"]["confirmed"]
        leverage_extra = [
            family for family in ("aggressive_flow", "open_interest", "liquidation")
            if factor[family]["confirmed"]
        ]
        if HIGH_LEVERAGE_CANDIDATE in tags and price_or_volume and leverage_extra:
            families = [
                family for family in ("price_momentum", "volume_expansion")
                if factor[family]["confirmed"]
            ] + leverage_extra
            directions = {
                factor[family]["direction"] for family in families
                if factor[family]["direction"] in {"up", "down"}
            }
            direction = next(iter(directions)) if len(directions) == 1 else "mixed"
            matched.append(("high_leverage_anomaly", direction, families))

        crowding_extra = [
            family for family, eligible in (
                ("open_interest", factor["open_interest"]["confirmed"] and factor["open_interest"]["direction"] == "up"),
                ("price_momentum", factor["price_momentum"]["direction"] == "down" or factor["price_momentum"]["stall"]),
                ("aggressive_flow", factor["aggressive_flow"]["confirmed"] and factor["aggressive_flow"]["direction"] == "down"),
                ("liquidation", factor["liquidation"]["confirmed"] and factor["liquidation"]["direction"] == "down"),
            )
            if eligible
        ]
        if (
            HIGH_LEVERAGE_CANDIDATE in tags
            and factor["funding"]["confirmed"]
            and factor["funding"]["direction"] == "up"
            and len(crowding_extra) >= 2
        ):
            matched.append(("long_crowding_risk", "down", ["funding", *crowding_extra]))

        events = [
            self._event(
                event_type=event_type,
                symbol=symbol,
                direction=direction,
                candidate=candidate,
                features=features,
                mark=mark,
                oi=oi,
                factors=families,
                thresholds=thresholds,
                now_ts=now_ts,
                subscription_generation=generation,
                candidate_epoch=candidate_epoch,
            )
            for event_type, direction, families in matched
        ]

        if events:
            last = events[-1]
            symbol_state.update({
                "last_confirmed_event_type": last["event_type"],
                "last_confirmed_families": list(last["confirmed_factor_families"]),
                "last_confirmed_direction": last["direction"],
                "last_confirmed_window_end": last["window_end"],
                "weakening_count": 0,
            })
        elif symbol_state.get("last_confirmed_event_type"):
            previous_families = [str(value) for value in symbol_state.get("last_confirmed_families") or []]
            previous_direction = str(symbol_state.get("last_confirmed_direction") or "mixed")
            weakened = [
                family for family in previous_families
                if family in factor and (
                    not factor[family]["confirmed"]
                    or (
                        previous_direction in {"up", "down"}
                        and factor[family]["direction"] not in {previous_direction, "mixed"}
                    )
                )
            ]
            if float(features["volume_anomaly_multiple"]) <= thresholds["weakening_volume_ratio"]:
                if "volume_expansion" in previous_families and "volume_expansion" not in weakened:
                    weakened.append("volume_expansion")
            if len(set(weakened)) >= 2:
                symbol_state["weakening_count"] = int(symbol_state.get("weakening_count") or 0) + 1
            else:
                symbol_state["weakening_count"] = 0
            required_windows = max(1, int(getattr(
                self.settings,
                "altcoin_contract_anomaly_weakening_windows",
                2,
            ) or 2))
            if int(symbol_state["weakening_count"]) >= required_windows:
                event = self._event(
                    event_type="anomaly_weakening",
                    symbol=symbol,
                    direction="mixed",
                    candidate=candidate,
                    features=features,
                    mark=mark,
                    oi=oi,
                    factors=sorted(set(weakened), key=FACTOR_FAMILIES.index),
                    thresholds=thresholds,
                    now_ts=now_ts,
                    subscription_generation=generation,
                    candidate_epoch=candidate_epoch,
                )
                event["factor_values"]["weakened_factor_families"] = sorted(
                    set(weakened), key=FACTOR_FAMILIES.index
                )
                events.append(event)
                symbol_state["weakening_count"] = 0
        return events

    def evaluate(
        self,
        subscription_status: Mapping[str, Any],
        *,
        now_ts: float | None = None,
    ) -> list[dict[str, Any]]:
        now = float(now_ts if now_ts is not None else time.time())
        self._last_now_ts = now
        candidate_count = len(self.candidate_symbols)
        evaluation_boundary = int(now // 60) * 60
        if candidate_count and evaluation_boundary % 300 != 0:
            self._stats["non_aligned_evaluation_skips"] += candidate_count
            return []
        self._stats["aligned_evaluation_rounds"] += 1
        self._stats["last_aligned_evaluation_at"] = _iso(evaluation_boundary)
        self._stats["last_evaluation_candidate_count"] = candidate_count
        self._stats["last_evaluation_complete_count"] = 0
        self._stats["last_evaluation_epoch_complete_count"] = 0
        self._stats["last_evaluation_funding_complete_count"] = 0
        self._stats["last_evaluation_complete_ratio"] = (
            1.0 if candidate_count == 0 else 0.0
        )
        if not self.enabled:
            self._stats["data_quality_skips"] += candidate_count
            self._record_skip("disabled", count=candidate_count)
            return []
        if not self._manifest_event_ready:
            self._stats["data_quality_skips"] += candidate_count
            self._record_skip("manifest_degraded", count=candidate_count)
            return []
        ready, quality, generation, candidate_epochs = self._subscription_gate(
            subscription_status,
            now_ts=now,
        )
        if not ready:
            self._stats["data_quality_skips"] += candidate_count
            subscription_reason = {
                "stale": "websocket_stale",
                "capacity_degraded": "capacity_degraded",
                "manifest_degraded": "manifest_degraded",
            }.get(quality, "subscription_degraded")
            self._record_skip(subscription_reason, count=candidate_count)
            self._stats["last_error"] = quality
            return []
        manifest = self.manifest_consumer.last_valid
        if manifest is None:
            return []
        features = self.feature_builder.build_many(
            manifest.symbols,
            now_ts=now,
            candidate_epochs=candidate_epochs,
        )
        target_boundaries = {}
        for symbol, feature in features.items():
            end_ts = _epoch_seconds(
                dict(feature.get("source_timestamps") or {}).get("closed_5m_end")
            )
            if end_ts is not None:
                target_boundaries[symbol] = int(end_ts // 300) * 300
        oi_values = self.oi_sampler.refresh(
            manifest.symbols,
            now_ts=now,
            target_boundaries=target_boundaries,
            candidate_epochs=candidate_epochs,
        )
        pending_events: list[dict[str, Any]] = []
        pending_symbol_states = copy.deepcopy(self._symbol_states)
        max_age = max(1, int(getattr(
            self.settings,
            "altcoin_contract_anomaly_realtime_data_max_age_sec",
            120,
        ) or 120))
        for symbol in manifest.symbols:
            self._stats["feature_evaluations"] += 1
            feature = features.get(symbol) or {}
            oi = oi_values.get(symbol) or {}
            epoch = candidate_epochs.get(symbol) or {}
            epoch_id = str(epoch.get("epoch_id") or "")
            mark = self._mark_snapshot(
                symbol,
                feature=feature,
                epoch=epoch,
            ) or {}
            mark_ts = _epoch_seconds(mark.get("event_time_ms", mark.get("event_time")))
            mark_age = now - mark_ts if mark_ts is not None else None
            mark_fresh = (
                mark_age is not None
                and -P2_CLOCK_SKEW_TOLERANCE_SEC <= mark_age <= max_age
            )
            activated_ms = int(epoch.get("activated_at_ms") or 0)
            last_trade_ms = int(epoch.get("last_agg_trade_event_ms") or 0)
            last_mark_ms = int(epoch.get("last_mark_price_event_ms") or 0)
            per_symbol_market_fresh = bool(
                epoch_id
                and last_trade_ms >= activated_ms > 0
                and last_mark_ms >= activated_ms
                and -P2_CLOCK_SKEW_TOLERANCE_SEC
                <= now - last_trade_ms / 1_000.0
                <= max_age
                and -P2_CLOCK_SKEW_TOLERANCE_SEC
                <= now - last_mark_ms / 1_000.0
                <= max_age
            )
            if per_symbol_market_fresh:
                self._stats["last_evaluation_epoch_complete_count"] += 1
            if (
                mark.get("subscription_epoch") == epoch_id
                and mark.get("funding_window_quality") == "complete"
                and _number(mark.get("funding_rate_change_5m")) is not None
            ):
                self._stats["last_evaluation_funding_complete_count"] += 1
            feature_5m_end = _epoch_seconds(
                dict(feature.get("source_timestamps") or {}).get("closed_5m_end")
            )
            oi_change_end = _epoch_seconds(oi.get("change_end_at"))
            oi_window_matches = bool(
                feature_5m_end is not None
                and int(feature_5m_end) % 300 == 0
                and oi_change_end is not None
                and int(oi_change_end) == int(feature_5m_end)
            )
            required_feature_values = (
                "price_change_1m", "price_change_5m", "volume_anomaly_multiple",
                "aggressive_buy_ratio_5m", "aggressive_sell_ratio_5m", "cvd_5m_usd",
                "long_liquidation_5m_usd", "short_liquidation_5m_usd",
            )
            complete = (
                feature.get("data_quality") == "complete"
                and feature.get("subscription_epoch") == epoch_id
                and oi.get("data_quality") == "complete"
                and oi.get("subscription_epoch") == epoch_id
                and oi_window_matches
                and mark_fresh
                and per_symbol_market_fresh
                and mark.get("subscription_epoch") == epoch_id
                and mark.get("funding_window_quality") == "complete"
                and _number(mark.get("funding_rate")) is not None
                and _number(mark.get("funding_rate_change_5m")) is not None
                and all(_number(feature.get(key)) is not None for key in required_feature_values)
                and _number(oi.get("oi_change_5m")) is not None
            )
            if not complete:
                self._stats["data_quality_skips"] += 1
                reasons: set[str] = set()
                feature_quality = str(feature.get("data_quality") or "")
                if feature_quality == "stale":
                    reasons.add("feature_stale")
                elif feature_quality == "insufficient_history":
                    reasons.add("insufficient_history")
                elif feature_quality != "complete":
                    reasons.add("feature_invalid")
                if not mark:
                    reasons.add("mark_missing")
                elif not mark_fresh or not per_symbol_market_fresh:
                    reasons.add("mark_stale")
                if _number(mark.get("funding_rate")) is None:
                    reasons.add("funding_missing")
                if (
                    _number(mark.get("funding_rate_change_5m")) is None
                    or mark.get("funding_window_quality") != "complete"
                ):
                    reasons.add("funding_change_missing")
                oi_quality = str(oi.get("data_quality") or "")
                if not oi:
                    reasons.add("oi_missing")
                elif oi_quality == "stale":
                    reasons.add("oi_stale")
                elif oi_quality != "complete" or _number(oi.get("oi_change_5m")) is None:
                    reasons.add("oi_insufficient_history")
                elif not oi_window_matches:
                    reasons.add("oi_insufficient_history")
                if any(_number(feature.get(key)) is None for key in required_feature_values):
                    reasons.add("feature_invalid")
                for reason in sorted(reasons or {"feature_invalid"}):
                    self._record_skip(reason)
                continue
            self._stats["last_evaluation_complete_count"] += 1
            old_state = copy.deepcopy(self._symbol_states.get(symbol) or {})
            if str(old_state.get("subscription_epoch") or "") != epoch_id:
                working_state = {
                    "subscription_epoch": epoch_id,
                    "subscription_epoch_activated_at": _iso(activated_ms),
                }
            else:
                working_state = copy.deepcopy(old_state)
            self._symbol_states[symbol] = working_state
            events = self._candidate_events(
                symbol=symbol,
                candidate=manifest.candidates[symbol],
                features=feature,
                mark=mark,
                oi=oi,
                now_ts=now,
                generation=generation,
                candidate_epoch=epoch,
            )
            pending_symbol_states[symbol] = copy.deepcopy(
                self._symbol_states.get(symbol) or {}
            )
            if old_state:
                self._symbol_states[symbol] = old_state
            else:
                self._symbol_states.pop(symbol, None)
            pending_events.extend(events)
        self._stats["last_evaluation_complete_ratio"] = (
            self._stats["last_evaluation_complete_count"] / candidate_count
            if candidate_count
            else 1.0
        )
        newly_appended = set(self.state_store.record_event_batch(
            pending_events,
            last_valid_manifest=manifest.summary(),
            symbol_states=pending_symbol_states,
            oi_samples=self.oi_sampler.samples,
        ))
        self._symbol_states = pending_symbol_states
        emitted = [
            event for event in pending_events
            if str(event.get("event_id") or "") in newly_appended
        ]
        for event in emitted:
            self._recent_events.append(event)
            event_type = str(event["event_type"])
            self._stats["events"][event_type] = int(
                self._stats["events"].get(event_type, 0)
            ) + 1
            self._stats["last_event_at"] = event["observed_at"]
        self._recent_events = self._recent_events[-200:]
        return emitted

    def stats(self) -> dict[str, Any]:
        manifest = self.manifest_consumer.last_valid
        manifest_ts = _epoch_seconds(manifest.generated_at) if manifest else None
        reference_ts = self._last_now_ts or time.time()
        manifest_age_sec = (
            max(0.0, reference_ts - manifest_ts)
            if manifest_ts is not None
            else None
        )
        return json_safe({
            **self._stats,
            "enabled": self.enabled,
            "manifest_event_ready": self._manifest_event_ready,
            "manifest_hash": manifest.candidate_pool_hash if manifest else "",
            "manifest_snapshot_hash": manifest.candidate_snapshot_hash if manifest else "",
            "manifest_age_sec": manifest_age_sec,
            "manifest_last_error": self.manifest_consumer.last_error,
            "candidate_count": len(self.candidate_symbols),
            "features": dict(getattr(self.feature_builder, "last_stats", {}) or {}),
            "oi": dict(self.oi_sampler.last_stats),
        })


def run_realtime_confirmation_session(
    settings: Settings,
    *,
    duration_sec: float,
) -> dict[str, Any]:
    """Run the one existing Binance market connection as a bounded P2 dry-run."""

    from .configuration import AltcoinAnomalyConfig

    AltcoinAnomalyConfig.from_settings(settings, realtime=True)
    preflight_now = time.time()
    manifest_consumer = CandidateManifestConsumer(settings)
    preflight = manifest_consumer.poll(now_ts=preflight_now)
    manifest = manifest_consumer.last_valid
    generated_ts = _epoch_seconds(manifest.generated_at) if manifest else None
    manifest_max_age = max(
        1,
        int(
            getattr(
                settings,
                "altcoin_contract_anomaly_manifest_max_age_sec",
                1200,
            )
            or 1200
        ),
    )
    remaining_manifest_lifetime = (
        manifest_max_age - max(0.0, preflight_now - generated_ts)
        if generated_ts is not None
        else 0.0
    )
    preflight_failure = ""
    if not str(preflight.get("status") or "").startswith("valid_"):
        preflight_failure = str(preflight.get("reason") or "candidate_manifest_unavailable")
    elif remaining_manifest_lifetime < float(duration_sec):
        preflight_failure = "candidate_manifest_lifetime_insufficient"

    if preflight_failure:
        timestamp = datetime.fromtimestamp(preflight_now, timezone.utc).isoformat()
        raw = {
            "started_at": timestamp,
            "ended_at": timestamp,
            "duration_sec_requested": float(duration_sec),
            "duration_sec_actual": 0.0,
            "interrupted": False,
            "failures": [],
            "events": [],
            "stats": {
                "manifest_event_ready": False,
                "manifest_hash": manifest.candidate_pool_hash if manifest else "",
                "manifest_snapshot_hash": (
                    manifest.candidate_snapshot_hash if manifest else ""
                ),
                "manifest_age_sec": (
                    max(0.0, preflight_now - generated_ts)
                    if generated_ts is not None
                    else None
                ),
                "candidate_count": len(manifest.symbols) if manifest else 0,
                "manifest_preflight_failure": preflight_failure,
            },
        }
    else:
        # P2 is injected only after every bounded-session preflight succeeds.
        # The ordinary market-stream constructor therefore remains permanently
        # legacy, regardless of environment switches.
        feature_store = RealtimeFeatureStore(settings.realtime_features_db_path)
        controller = AltcoinRealtimeController(
            settings,
            feature_store=feature_store,
            manifest_consumer=manifest_consumer,
        )
        service = BinanceRealtimeMarketService(
            settings,
            store=feature_store,
            realtime_controller=controller,
        )
        from shared.process_lock import ProcessFileLock

        raw = run_realtime_market_session(
            settings,
            duration_sec=duration_sec,
            service=service,
            process_lock=ProcessFileLock(
                getattr(
                    settings,
                    "altcoin_contract_anomaly_realtime_lock_path",
                    settings.data_dir / "altcoin_contract_anomaly_realtime.lock",
                )
            ),
        )
    stats = dict(raw.get("stats") or {})
    failures = [str(value) for value in raw.get("failures") or []]
    evaluation_errors = int(stats.get("evaluation_errors") or 0)
    if evaluation_errors > 0:
        failures.append("realtime_evaluation_internal_error")
    runner_shutdown_timeouts = int(stats.get("runner_shutdown_timeouts") or 0)
    if runner_shutdown_timeouts > 0:
        failures.append("websocket_runner_shutdown_timeout")
    candidate_count = int(stats.get("candidate_count") or 0)
    manifest_ready = bool(stats.get("manifest_event_ready"))
    accepted_events = int(stats.get("accepted_events") or 0)
    candidate_coverage_complete = bool(stats.get("candidate_coverage_complete"))
    mark_price_data_coverage = _number(
        stats.get("mark_price_data_coverage_ratio"),
        minimum=0.0,
    )
    feature_coverage = dict(stats.get("feature_coverage") or {})
    closed_feature_coverage = _number(
        feature_coverage.get("complete_coverage_ratio"),
        minimum=0.0,
    )
    feature_candidate_count = int(feature_coverage.get("candidate_count") or 0)
    last_evaluation_candidate_count = int(
        stats.get("last_evaluation_candidate_count") or 0
    )
    last_evaluation_complete_count = int(
        stats.get("last_evaluation_complete_count") or 0
    )
    last_evaluation_epoch_complete_count = int(
        stats.get("last_evaluation_epoch_complete_count") or 0
    )
    last_evaluation_funding_complete_count = int(
        stats.get("last_evaluation_funding_complete_count") or 0
    )
    evaluation_complete_coverage = _number(
        stats.get("last_evaluation_complete_ratio"),
        minimum=0.0,
    )
    interrupted = bool(raw.get("interrupted"))

    exit_code = 0
    if failures:
        status = "internal_error"
        exit_code = 1
    elif interrupted:
        status = "interrupted"
        exit_code = 130
    elif not manifest_ready:
        status = "data_unavailable"
        exit_code = 3
        failures.append("candidate_manifest_unavailable")
        preflight_failure = str(stats.get("manifest_preflight_failure") or "")
        if preflight_failure and preflight_failure not in failures:
            failures.append(preflight_failure)
    elif candidate_count == 0:
        status = "completed_no_candidates"
    elif (
        accepted_events <= 0
        or not candidate_coverage_complete
        or mark_price_data_coverage is None
        or mark_price_data_coverage <= 0
        or closed_feature_coverage is None
        or closed_feature_coverage < 1.0
        or feature_candidate_count != candidate_count
        or evaluation_complete_coverage is None
        or evaluation_complete_coverage < 1.0
        or last_evaluation_candidate_count != candidate_count
        or last_evaluation_complete_count != candidate_count
        or last_evaluation_epoch_complete_count != candidate_count
        or last_evaluation_funding_complete_count != candidate_count
    ):
        status = "data_unavailable"
        exit_code = 3
        if accepted_events <= 0:
            failures.append("no_market_events_received")
        if not candidate_coverage_complete:
            failures.append("candidate_subscription_incomplete")
        if mark_price_data_coverage is None or mark_price_data_coverage <= 0:
            failures.append("candidate_mark_price_unavailable")
        if (
            closed_feature_coverage is None
            or closed_feature_coverage < 1.0
            or feature_candidate_count != candidate_count
        ):
            failures.append("candidate_closed_features_incomplete")
        if (
            evaluation_complete_coverage is None
            or evaluation_complete_coverage < 1.0
            or last_evaluation_candidate_count != candidate_count
            or last_evaluation_complete_count != candidate_count
        ):
            failures.append("candidate_confirmation_data_incomplete")
        if last_evaluation_epoch_complete_count != candidate_count:
            failures.append("candidate_subscription_epoch_incomplete")
        if last_evaluation_funding_complete_count != candidate_count:
            failures.append("candidate_funding_window_incomplete")
    else:
        status = "completed"

    active_stream_count = int(stats.get("active_stream_count") or 0)
    desired_stream_count = int(stats.get("desired_stream_count") or 0)
    event_counts = dict(stats.get("event_counts") or {})
    events = [
        dict(event)
        for event in raw.get("events") or []
        if isinstance(event, Mapping)
    ]
    return json_safe({
        "schema_version": P2_SCHEMA_VERSION,
        "rules_version": P2_RULES_VERSION,
        "module": "altcoin_contract_anomaly",
        "mode": "realtime_confirmation_dry_run",
        "status": status,
        "exit_code": exit_code,
        "dry_run": True,
        "started_at": raw.get("started_at"),
        "ended_at": raw.get("ended_at"),
        "requested_duration_sec": raw.get("duration_sec_requested"),
        "elapsed_duration_sec": raw.get("duration_sec_actual"),
        "candidate_pool_hash": stats.get("manifest_hash"),
        "candidate_snapshot_hash": stats.get("manifest_snapshot_hash"),
        "candidate_count": candidate_count,
        "subscriptions": {
            "base_symbol_count": int(stats.get("base_symbol_count") or 0),
            "union_symbol_count": int(stats.get("union_symbol_count") or 0),
            "expected_stream_count": int(stats.get("expected_stream_count") or 0),
            "desired_stream_count": desired_stream_count,
            "active_stream_count": active_stream_count,
            "pending_subscribe_count": int(stats.get("pending_subscribe_count") or 0),
            "pending_unsubscribe_count": int(stats.get("pending_unsubscribe_count") or 0),
            "candidate_coverage_complete": candidate_coverage_complete,
            "candidate_epoch_count": int(stats.get("candidate_epoch_count") or 0),
            "candidate_epoch_coverage_ratio": stats.get(
                "candidate_epoch_coverage_ratio"
            ),
            "mark_price_subscription_coverage_ratio": stats.get("mark_price_coverage_ratio"),
            "mark_price_data_coverage_ratio": mark_price_data_coverage,
            "force_order_subscription_count": int(
                stats.get("force_order_subscription_count") or 0
            ),
            "subscription_generation": int(stats.get("subscription_generation") or 0),
            "capacity_degraded": bool(stats.get("capacity_degraded")),
            "candidate_capacity_degraded": bool(
                stats.get("candidate_capacity_degraded")
            ),
        },
        "events": {
            "counts": event_counts,
            "total": sum(int(value or 0) for value in event_counts.values()),
            "items": events,
        },
        "data_quality": {
            "manifest_ready": manifest_ready,
            "manifest_age_sec": stats.get("manifest_age_sec"),
            "last_market_receive_ms": stats.get("last_market_receive_ms"),
            "accepted_market_events": accepted_events,
            "features": feature_coverage,
            "last_evaluation_candidate_count": last_evaluation_candidate_count,
            "last_evaluation_complete_count": last_evaluation_complete_count,
            "last_evaluation_complete_ratio": evaluation_complete_coverage,
            "last_evaluation_epoch_complete_count": (
                last_evaluation_epoch_complete_count
            ),
            "last_evaluation_funding_complete_count": (
                last_evaluation_funding_complete_count
            ),
            "skip_count": int(stats.get("data_quality_skips") or 0),
            "skip_reasons": dict(stats.get("data_quality_skip_reasons") or {}),
            "evaluation_errors": evaluation_errors,
            "runner_shutdown_timeouts": runner_shutdown_timeouts,
            "oi": {
                "candidate_count": int(stats.get("oi_candidate_count") or 0),
                "requests": int(stats.get("oi_requests") or 0),
                "cache_hits": int(stats.get("oi_cache_hits") or 0),
                "successes": int(stats.get("oi_successes") or 0),
                "failures": int(stats.get("oi_failures") or 0),
                "budget_used": int(stats.get("oi_budget_used") or 0),
                "budget_limit": int(stats.get("oi_budget_limit") or 0),
                "budget_exhausted": int(stats.get("oi_budget_exhausted") or 0),
                "rate_limit_blocked": int(
                    stats.get("oi_rate_limit_blocked") or 0
                ),
                "http_429": int(stats.get("oi_http_429") or 0),
                "http_418": int(stats.get("oi_http_418") or 0),
                "refresh_rounds": int(stats.get("oi_refresh_rounds") or 0),
                "last_round": dict(stats.get("oi_last_round") or {}),
            },
        },
        "failures": failures,
        "service_stats": stats,
        "telegram": {"enabled": False, "sent": 0},
    })


__all__ = [
    "AltcoinRealtimeController",
    "CandidateManifestConsumer",
    "CandidateMarkPriceBook",
    "CandidateOiSampler",
    "ClosedRealtimeFeatureBuilder",
    "EVENT_NAMES_CN",
    "FACTOR_FAMILIES",
    "P2_RULES_VERSION",
    "P2_SCHEMA_VERSION",
    "ValidatedCandidateManifest",
    "run_realtime_confirmation_session",
]
