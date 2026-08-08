from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
import threading
import time
from typing import Any, Callable, Iterable, Mapping, Sequence

from config.settings import Settings

from .models import json_safe
from .production_state import ProductionStateStore
from .radar import scan_candidate_pool
from .state import CandidatePoolStore


Formatter = Callable[[Sequence[Mapping[str, Any]], Mapping[str, Any]], Sequence[str]]
Delivery = Callable[..., Any]
CandidateLookup = Callable[[str], Mapping[str, Any] | None]


def _safe_error_class(value: Any, fallback: str = "delivery_failed") -> str:
    normalized = str(value or "").strip()
    if (
        normalized
        and len(normalized) <= 100
        and all(character.isalnum() or character in "_.-" for character in normalized)
    ):
        return normalized
    return fallback


def _parse_iso(value: Any) -> float | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


class CandidateManifestRefreshWorker:
    """Single-flight P1 refresh scheduler kept outside all WS callbacks."""

    def __init__(
        self,
        settings: Settings,
        *,
        interval_sec: int,
        max_manifest_age_sec: int,
        retry_sec: int = 60,
        manifest_path: str | Path | None = None,
        scan_callable: Callable[..., Mapping[str, Any]] = scan_candidate_pool,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if int(interval_sec) <= 0:
            raise ValueError("candidate refresh interval must be positive")
        if int(max_manifest_age_sec) <= 0:
            raise ValueError("candidate manifest max age must be positive")
        if int(retry_sec) <= 0:
            raise ValueError("candidate refresh retry must be positive")
        self.settings = settings
        self.interval_sec = int(interval_sec)
        self.max_manifest_age_sec = int(max_manifest_age_sec)
        self.retry_sec = int(retry_sec)
        self.manifest_path = Path(
            manifest_path
            or settings.altcoin_contract_anomaly_candidate_snapshot_path
        )
        self._scan = scan_callable
        self._clock = clock
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._condition = threading.Condition()
        self._thread: threading.Thread | None = None
        self._generation = 0
        self._last_attempt_succeeded = False
        self._stats: dict[str, Any] = {
            "refresh_attempts": 0,
            "refresh_successes": 0,
            "refresh_failures": 0,
            "refresh_in_progress": False,
            "last_attempt_at": "",
            "last_success_at": "",
            "last_failure_at": "",
            "last_error_class": "",
            "last_duration_sec": 0.0,
            "candidate_count": 0,
            "manifest_generated_at": "",
            "candidate_pool_hash": "",
            "candidate_snapshot_hash": "",
            "binance_oi_requests": 0,
            "binance_oi_request_budget": 0,
            "cmc_map_requests": 0,
            "cmc_quote_requests": 0,
            "cmc_cache_used": False,
            "stop_timed_out": False,
        }
        self._load_existing_summary()

    def _load_existing_summary(self) -> None:
        try:
            payload = CandidatePoolStore(
                self.manifest_path,
                data_dir=self.settings.data_dir,
            ).load()
        except Exception:
            payload = None
        if isinstance(payload, Mapping):
            self._update_manifest_summary(payload)

    def _update_manifest_summary(self, payload: Mapping[str, Any]) -> None:
        symbols = payload.get("candidate_symbols")
        diagnostics = payload.get("diagnostics")
        metrics = dict(diagnostics) if isinstance(diagnostics, Mapping) else {}
        self._stats["candidate_count"] = len(symbols) if isinstance(symbols, list) else 0
        self._stats["manifest_generated_at"] = str(payload.get("generated_at") or "")
        self._stats["candidate_pool_hash"] = str(
            payload.get("candidate_pool_hash") or ""
        )
        self._stats["candidate_snapshot_hash"] = str(
            payload.get("candidate_snapshot_hash") or ""
        )
        self._stats["binance_oi_requests"] = int(
            metrics.get("binance_oi_request_count") or 0
        )
        self._stats["binance_oi_request_budget"] = int(
            metrics.get("binance_oi_request_budget") or 0
        )
        self._stats["cmc_map_requests"] = int(
            metrics.get("cmc_map_request_count") or 0
        )
        self._stats["cmc_quote_requests"] = int(
            metrics.get("cmc_quote_request_count") or 0
        )
        self._stats["cmc_cache_used"] = bool(
            metrics.get("cmc_map_cache_hit")
            or metrics.get("cmc_quotes_cache_hit")
        )

    def start(self) -> None:
        with self._condition:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._wake.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="altcoin-candidate-refresh",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 10.0) -> bool:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(max(0.0, float(timeout)))
        stopped = not bool(thread is not None and thread.is_alive())
        with self._condition:
            self._stats["stop_timed_out"] = not stopped
        return stopped

    def request_refresh(self) -> int:
        """Schedule a refresh and return the current completion generation."""

        with self._condition:
            generation = self._generation
        self._wake.set()
        return generation

    def wait_for_generation(self, generation: int, timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._condition:
            while self._generation <= int(generation):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def refresh_now(self, timeout: float = 120.0) -> bool:
        """Run one refresh on the worker and wait for its bounded result."""

        with self._condition:
            generation = self._generation
            in_progress = bool(self._stats.get("refresh_in_progress"))
            running = bool(self._thread is not None and self._thread.is_alive())
        target = generation + 1 if in_progress else generation
        if not running:
            self.start()
        else:
            self.request_refresh()
        if not self.wait_for_generation(target, timeout=timeout):
            return False
        with self._condition:
            return bool(self._last_attempt_succeeded)

    def _run(self) -> None:
        while not self._stop.is_set():
            succeeded = self._refresh_once()
            if self._stop.is_set():
                break
            self._wake.wait(self.interval_sec if succeeded else self.retry_sec)
            self._wake.clear()

    def _refresh_once(self) -> bool:
        started_wall = float(self._clock())
        started_mono = time.monotonic()
        succeeded = False
        with self._condition:
            self._stats["refresh_in_progress"] = True
            self._stats["refresh_attempts"] += 1
            self._stats["last_attempt_at"] = datetime.fromtimestamp(
                started_wall, timezone.utc
            ).isoformat()
        try:
            payload = self._scan(self.settings)
            if not isinstance(payload, Mapping):
                raise ValueError("candidate scan returned a non-object manifest")
            generated_ts = _parse_iso(payload.get("generated_at"))
            if generated_ts is None:
                raise ValueError("candidate scan returned an undated manifest")
            age = started_wall - generated_ts
            if age < -300 or age > self.max_manifest_age_sec:
                raise ValueError("candidate scan returned a stale manifest")
        except Exception as exc:
            succeeded = False
            with self._condition:
                self._stats["refresh_failures"] += 1
                self._stats["last_failure_at"] = datetime.fromtimestamp(
                    float(self._clock()), timezone.utc
                ).isoformat()
                self._stats["last_error_class"] = type(exc).__name__
        else:
            succeeded = True
            with self._condition:
                self._stats["refresh_successes"] += 1
                self._stats["last_success_at"] = datetime.fromtimestamp(
                    float(self._clock()), timezone.utc
                ).isoformat()
                self._stats["last_error_class"] = ""
                self._update_manifest_summary(payload)
        finally:
            with self._condition:
                self._stats["last_duration_sec"] = max(
                    0.0,
                    time.monotonic() - started_mono,
                )
                self._stats["refresh_in_progress"] = False
                self._last_attempt_succeeded = succeeded
                self._generation += 1
                self._condition.notify_all()
        return succeeded

    def stats(self) -> dict[str, Any]:
        with self._condition:
            output = dict(self._stats)
            output["running"] = bool(self._thread is not None and self._thread.is_alive())
            generated_ts = _parse_iso(output.get("manifest_generated_at"))
            age = None if generated_ts is None else max(0.0, float(self._clock()) - generated_ts)
            output["manifest_age_sec"] = age
            output["manifest_stale"] = age is None or age > self.max_manifest_age_sec
            output["generation"] = self._generation
            output["retry_sec"] = self.retry_sec
            return json_safe(output)


class AltcoinProductionEventProcessor:
    """Bounded, non-blocking P2-event receiver with a durable delivery WAL."""

    def __init__(
        self,
        *,
        state_path: str | Path,
        outbox_path: str | Path,
        formatter: Formatter | None = None,
        delivery: Delivery | None = None,
        candidate_lookup: CandidateLookup | None = None,
        cooldown_sec: int = 1800,
        max_messages_per_hour: int = 20,
        queue_size: int = 256,
        poll_interval_sec: float = 0.25,
        retry_base_sec: float = 5.0,
        retry_max_sec: float = 900.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if int(cooldown_sec) < 0:
            raise ValueError("production cooldown cannot be negative")
        if int(max_messages_per_hour) < 0:
            raise ValueError("production hourly limit cannot be negative")
        if int(queue_size) <= 0:
            raise ValueError("production event queue size must be positive")
        if float(poll_interval_sec) <= 0:
            raise ValueError("production event poll interval must be positive")
        if not isfinite(float(retry_base_sec)) or float(retry_base_sec) <= 0:
            raise ValueError("production retry base must be positive")
        if (
            not isfinite(float(retry_max_sec))
            or float(retry_max_sec) < float(retry_base_sec)
        ):
            raise ValueError("production retry maximum must cover the base")
        self._clock = clock
        self._formatter = formatter
        self._delivery = delivery
        self._candidate_lookup = candidate_lookup
        self.cooldown_sec = int(cooldown_sec)
        self.max_messages_per_hour = int(max_messages_per_hour)
        # submit() writes to the durable WAL before acknowledging the caller.
        # This lock serializes that short local-storage transaction; formatting
        # and all network work remain on the delivery worker.
        self._ingest_lock = threading.RLock()
        self._poll_interval_sec = float(poll_interval_sec)
        self._retry_base_sec = float(retry_base_sec)
        self._retry_max_sec = float(retry_max_sec)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._stats_lock = threading.RLock()
        self._stats: dict[str, Any] = {
            "submitted_batches": 0,
            "submitted_events": 0,
            "queue_rejections": 0,
            "invalid_events": 0,
            "duplicate_events": 0,
            "merged_batches": 0,
            "sent_batches": 0,
            "sent_pages": 0,
            "recovered_pages": 0,
            "previewed_batches": 0,
            "previewed_pages": 0,
            "quarantined_batches": 0,
            "retry_scheduled": 0,
            "worker_failures": 0,
            "delivery_failures": 0,
            "formatter_failures": 0,
            "rate_limited": 0,
            "cooldown_suppressed": 0,
            "policy_suppressed": 0,
            "last_delivery_at": "",
            "last_error_class": "",
            "stop_timed_out": False,
        }
        self.state = ProductionStateStore(
            state_path,
            outbox_path,
            clock=clock,
            max_batches=max(1, int(queue_size)),
        )

    def start(self) -> None:
        with self._stats_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._wake.clear()
            self._thread = threading.Thread(
                target=self._run,
                name="altcoin-production-events",
                daemon=True,
            )
            self._thread.start()

    def stop(self, timeout: float = 10.0) -> bool:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(max(0.0, float(timeout)))
        stopped = not bool(thread is not None and thread.is_alive())
        with self._stats_lock:
            self._stats["stop_timed_out"] = not stopped
        return stopped

    def submit(self, events: Iterable[Mapping[str, Any]]) -> bool:
        normalized = [dict(event) for event in events if isinstance(event, Mapping)]
        if not normalized:
            return True
        with self._stats_lock:
            self._stats["submitted_batches"] += 1
            self._stats["submitted_events"] += len(normalized)
        try:
            # A successful return is the durable acceptance acknowledgement.
            # The P2 controller advances its own event cursor before invoking
            # the sink, so an in-memory-only queue would create a loss window.
            with self._ingest_lock:
                self._ingest_submissions(normalized)
        except Exception as exc:
            with self._stats_lock:
                self._stats["queue_rejections"] += 1
                self._stats["last_error_class"] = type(exc).__name__
            raise
        self._wake.set()
        return True

    @staticmethod
    def _is_valid_event(event: Mapping[str, Any]) -> bool:
        return bool(
            event.get("event_id")
            and event.get("event_type")
            and event.get("symbol")
            and event.get("window_end")
            and event.get("data_quality") == "complete"
        )

    def _ingest_submissions(self, submitted: Sequence[Mapping[str, Any]]) -> None:
        if not submitted:
            return
        known = self.state.known_event_ids()
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        seen: set[str] = set()
        for raw in submitted:
            event = json_safe(dict(raw))
            if not self._is_valid_event(event):
                with self._stats_lock:
                    self._stats["invalid_events"] += 1
                continue
            event_id = str(event["event_id"])
            if event_id in known or event_id in seen:
                with self._stats_lock:
                    self._stats["duplicate_events"] += 1
                continue
            seen.add(event_id)
            key = (
                str(event["symbol"]).upper(),
                str(event.get("window_start") or ""),
                str(event["window_end"]),
            )
            groups.setdefault(key, []).append(event)
        entries: list[dict[str, Any]] = []
        entry_suppressions: list[str] = []
        reserved_confirmation_symbols: set[str] = set()
        submission_now = float(self._clock())
        for key in sorted(groups):
            events = sorted(
                groups[key],
                key=lambda item: (str(item.get("event_type") or ""), str(item["event_id"])),
            )
            symbol = key[0]
            event_types = {str(event.get("event_type") or "") for event in events}
            if symbol in reserved_confirmation_symbols:
                if "candidate_condition_invalidated" in event_types:
                    notification_kind, suppression = "candidate_invalidated", ""
                elif "anomaly_weakening" in event_types:
                    notification_kind, suppression = "signal_expired", ""
                else:
                    notification_kind, suppression = None, "pending_confirmation"
            else:
                notification_kind, suppression = self.state.classify(
                    symbol=symbol,
                    event_types=event_types,
                    now_ts=submission_now,
                    cooldown_sec=self.cooldown_sec,
                )
            if notification_kind in {"first_confirmation", "new_round"}:
                reserved_confirmation_symbols.add(symbol)
            candidate_snapshot: Mapping[str, Any] | None = None
            if self._candidate_lookup is not None:
                try:
                    candidate_snapshot = self._candidate_lookup(symbol)
                except Exception as exc:
                    with self._stats_lock:
                        self._stats["last_error_class"] = type(exc).__name__
            for event in events:
                event["notification_kind"] = notification_kind or "suppressed"
                event["candidate_snapshot"] = (
                    json_safe(dict(candidate_snapshot))
                    if isinstance(candidate_snapshot, Mapping)
                    else None
                )
            entries.append({
                "events": events,
                "notification_kind": notification_kind or "suppressed",
                "suppression_reason": suppression if notification_kind is None else "",
            })
            entry_suppressions.append(suppression if notification_kind is None else "")
        if not entries:
            return
        results = self.state.enqueue_many(entries, now_ts=submission_now)
        for (_batch, created), suppression in zip(results, entry_suppressions):
            if not created:
                continue
            with self._stats_lock:
                self._stats["merged_batches"] += 1
            if suppression:
                with self._stats_lock:
                    key_name = (
                        "cooldown_suppressed"
                        if suppression == "symbol_cooldown"
                        else "policy_suppressed"
                    )
                    self._stats[key_name] += 1

    @staticmethod
    def _delivery_outcome(
        result: Any,
    ) -> tuple[bool, bool, bool, bool, float | None]:
        if result is True:
            return True, False, False, False, None
        if isinstance(result, Mapping):
            reason = str(result.get("reason") or "")
            sent = bool(result.get("sent")) or str(result.get("status") or "") == "sent"
            status = str(result.get("status") or "")
            retry_after = result.get("retry_after_sec", result.get("retry_after"))
        else:
            reason = str(getattr(result, "reason", "") or "")
            sent = bool(getattr(result, "sent", False)) or str(
                getattr(result, "status", "") or ""
            ) == "sent"
            status = str(getattr(result, "status", "") or "")
            diagnostics = getattr(result, "diagnostics", None)
            retry_after = getattr(
                diagnostics,
                "retry_after_sec",
                getattr(result, "retry_after_sec", None),
            )
        try:
            parsed_retry_after = (
                None if retry_after is None else max(0.0, float(retry_after))
            )
        except (TypeError, ValueError):
            parsed_retry_after = None
        if parsed_retry_after is not None and not isfinite(parsed_retry_after):
            parsed_retry_after = None
        if reason == "dedup_cooldown":
            return True, True, False, False, parsed_retry_after
        previewed = status == "previewed" or reason == "production_preview_recorded"
        quarantined = status == "quarantined" or reason == "delivery_quarantine"
        return sent or previewed, False, previewed, quarantined, parsed_retry_after

    def _schedule_retry(
        self,
        batch_id: str,
        error_class: str,
        *,
        retry_after_sec: float | None = None,
    ) -> None:
        self.state.mark_failure(
            batch_id,
            error_class,
            now_ts=float(self._clock()),
            retry_after_sec=retry_after_sec,
            base_delay_sec=self._retry_base_sec,
            max_delay_sec=self._retry_max_sec,
        )
        with self._stats_lock:
            self._stats["retry_scheduled"] += 1

    def _format_batch(self, batch: Mapping[str, Any]) -> list[str]:
        if self._formatter is None:
            raise RuntimeError("production formatter is not configured")
        events = [
            dict(event)
            for event in batch.get("events") or []
            if isinstance(event, Mapping)
        ]
        context = {
            "batch_id": batch.get("batch_id"),
            "symbol": batch.get("symbol"),
            "window_start": batch.get("window_start"),
            "window_end": batch.get("window_end"),
            "notification_kind": batch.get("notification_kind"),
            "production_state": self.state.state_snapshot(),
        }
        pages = self._formatter(events, context)
        if isinstance(pages, str):
            normalized = [pages]
        else:
            normalized = [str(page) for page in pages if str(page)]
        if not normalized:
            raise ValueError("production formatter returned no pages")
        return normalized

    def _deliver_pending(self) -> None:
        now_ts = float(self._clock())
        for initial in self.state.pending_batches(now_ts=now_ts, due_only=True):
            batch_id = str(initial["batch_id"])
            batch = initial
            if batch.get("pages") is None:
                try:
                    pages = self._format_batch(batch)
                    batch = self.state.set_pages(batch_id, pages)
                except Exception as exc:
                    self._schedule_retry(batch_id, type(exc).__name__)
                    with self._stats_lock:
                        self._stats["formatter_failures"] += 1
                        self._stats["last_error_class"] = type(exc).__name__
                    return
            if self._delivery is None:
                self._schedule_retry(batch_id, "delivery_not_configured")
                with self._stats_lock:
                    self._stats["delivery_failures"] += 1
                    self._stats["last_error_class"] = "delivery_not_configured"
                return
            pages = list(batch.get("pages") or [])
            next_page = int(batch.get("next_page_index") or 0)
            existing_outcomes = [
                outcome
                for outcome in batch.get("page_outcomes") or []
                if isinstance(outcome, Mapping)
            ]
            batch_previewed = bool(existing_outcomes) and all(
                bool(outcome.get("previewed")) for outcome in existing_outcomes
            )
            batch_quarantined = False
            for index in range(next_page, len(pages)):
                now_ts = float(self._clock())
                if not self.state.can_send_page(
                    now_ts=now_ts,
                    max_per_hour=self.max_messages_per_hour,
                ):
                    self._schedule_retry(
                        batch_id,
                        "production_hourly_limit",
                        retry_after_sec=60.0,
                    )
                    with self._stats_lock:
                        self._stats["rate_limited"] += 1
                    return
                dedup_key = f"altcoin_contract_anomaly:{batch_id}:page:{index + 1}"
                context = {
                    "batch_id": batch_id,
                    "page_index": index,
                    "page_count": len(pages),
                    "symbol": batch.get("symbol"),
                    "notification_kind": batch.get("notification_kind"),
                    "events": deepcopy(batch.get("events") or []),
                }
                try:
                    result = self._delivery(
                        pages[index],
                        dedup_key=dedup_key,
                        context=context,
                    )
                    (
                        delivered,
                        recovered,
                        previewed,
                        quarantined,
                        retry_after_sec,
                    ) = self._delivery_outcome(result)
                except Exception as exc:
                    self._schedule_retry(
                        batch_id,
                        type(exc).__name__,
                        retry_after_sec=getattr(exc, "retry_after_sec", None),
                    )
                    with self._stats_lock:
                        self._stats["delivery_failures"] += 1
                        self._stats["last_error_class"] = type(exc).__name__
                    return
                if quarantined:
                    self.state.mark_quarantined(
                        batch_id,
                        "delivery_quarantine",
                        now_ts=float(self._clock()),
                    )
                    with self._stats_lock:
                        self._stats["quarantined_batches"] += 1
                        self._stats["last_error_class"] = "delivery_quarantine"
                    batch_quarantined = True
                    break
                if not delivered:
                    reason = str(getattr(result, "reason", "") or "delivery_failed")
                    if isinstance(result, Mapping):
                        reason = str(result.get("reason") or "delivery_failed")
                    reason = _safe_error_class(reason)
                    self._schedule_retry(
                        batch_id,
                        reason,
                        retry_after_sec=retry_after_sec,
                    )
                    with self._stats_lock:
                        self._stats["delivery_failures"] += 1
                        self._stats["last_error_class"] = reason[:100]
                    return
                batch_previewed = batch_previewed or previewed
                batch = self.state.mark_page_delivered(
                    batch_id,
                    index,
                    recovered_by_dedup=recovered,
                    previewed=previewed,
                )
                with self._stats_lock:
                    if previewed:
                        self._stats["previewed_pages"] += 1
                    else:
                        self._stats["sent_pages"] += 1
                    if recovered:
                        self._stats["recovered_pages"] += 1
            if batch_quarantined:
                continue
            committed_at = float(self._clock())
            if batch_previewed:
                self.state.mark_previewed(batch_id, now_ts=committed_at)
            else:
                self.state.mark_sent(batch_id, now_ts=committed_at)
            with self._stats_lock:
                if batch_previewed:
                    self._stats["previewed_batches"] += 1
                else:
                    self._stats["sent_batches"] += 1
                    self._stats["last_delivery_at"] = datetime.fromtimestamp(
                        committed_at, timezone.utc
                    ).isoformat()
                self._stats["last_error_class"] = ""

    def drain_once(self) -> None:
        self._deliver_pending()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.drain_once()
            except Exception as exc:
                # A provider success followed by a local WAL write failure is
                # recoverable through the shared Telegram dedup ledger.  Keep
                # the long-running worker alive, but avoid hot-looping on a
                # failing disk or local state store.
                with self._stats_lock:
                    self._stats["worker_failures"] += 1
                    self._stats["last_error_class"] = type(exc).__name__
                if self._stop.wait(self._retry_base_sec):
                    break
                continue
            self._wake.wait(self._poll_interval_sec)
            self._wake.clear()

    def stats(self) -> dict[str, Any]:
        with self._stats_lock:
            output = dict(self._stats)
            output.update(self.state.stats())
            output["queue_depth"] = 0
            output["durable_pending_depth"] = output.get("pending_batches", 0)
            output["running"] = bool(self._thread is not None and self._thread.is_alive())
            output["delivery_configured"] = self._delivery is not None
            output["retry_base_sec"] = self._retry_base_sec
            output["retry_max_sec"] = self._retry_max_sec
            return json_safe(output)


__all__ = [
    "AltcoinProductionEventProcessor",
    "CandidateManifestRefreshWorker",
]
