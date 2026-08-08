from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from math import isfinite
from pathlib import Path
import threading
import time
from typing import Any, Callable, Mapping, Sequence

from shared.storage import JsonStore

from .models import json_safe


PRODUCTION_STATE_SCHEMA_VERSION = 1
PRODUCTION_STATE_MODULE = "altcoin_contract_anomaly.production"
MAX_PROCESSED_EVENT_IDS = 10_000
MAX_SENT_BATCH_IDS = 4_000
TERMINAL_BATCH_STATUSES = frozenset({
    "sent",
    "suppressed",
    "previewed",
    "quarantined",
})
PRUNABLE_TERMINAL_BATCH_STATUSES = frozenset({
    "sent",
    "suppressed",
    "previewed",
})

CONFIRMATION_EVENT_TYPES = frozenset({
    "short_fuel_building",
    "short_squeeze_ignition",
    "high_leverage_anomaly",
    "long_crowding_risk",
})
EXPIRATION_EVENT_TYPES = frozenset({"anomaly_weakening"})
INVALIDATION_EVENT_TYPES = frozenset({"candidate_condition_invalidated"})


class ProductionStateCorruptError(RuntimeError):
    pass


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(float(timestamp), timezone.utc).isoformat()


def _default_state() -> dict[str, Any]:
    return {
        "schema_version": PRODUCTION_STATE_SCHEMA_VERSION,
        "module": PRODUCTION_STATE_MODULE,
        "processed_event_ids": [],
        "sent_batch_ids": [],
        "sent_page_timestamps": [],
        "symbols": {},
    }


def _default_outbox() -> dict[str, Any]:
    return {
        "schema_version": PRODUCTION_STATE_SCHEMA_VERSION,
        "module": PRODUCTION_STATE_MODULE,
        "batches": [],
    }


def _valid_document(payload: Any) -> bool:
    return bool(
        isinstance(payload, Mapping)
        and payload.get("schema_version") == PRODUCTION_STATE_SCHEMA_VERSION
        and payload.get("module") == PRODUCTION_STATE_MODULE
    )


def _bounded_unique(values: Sequence[Any], limit: int) -> list[str]:
    output = list(dict.fromkeys(str(value) for value in values if str(value)))
    return output[-max(1, int(limit)):]


def _retry_deadline_due(batch: Mapping[str, Any], now_ts: float) -> bool:
    raw = batch.get("next_attempt_at_ts")
    if raw in (None, ""):
        return True
    try:
        return float(raw) <= float(now_ts)
    except (TypeError, ValueError):
        return False


def deterministic_production_batch_id(events: Sequence[Mapping[str, Any]]) -> str:
    event_ids = sorted(str(event.get("event_id") or "") for event in events)
    if not event_ids or any(not value for value in event_ids):
        raise ValueError("production events require event_id")
    canonical = json.dumps(
        {
            "event_ids": event_ids,
            "symbol": str(events[0].get("symbol") or "").upper(),
            "window_end": str(events[0].get("window_end") or ""),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ProductionStateStore:
    """Module-owned state and recoverable delivery WAL.

    A terminal outbox record is written before the compact state cursor.  On
    restart, terminal outbox records are replayed into state, closing the crash
    window without treating a failed Telegram attempt as sent.
    """

    def __init__(
        self,
        state_path: str | Path,
        outbox_path: str | Path,
        *,
        clock: Callable[[], float] = time.time,
        max_batches: int = 2_000,
    ) -> None:
        self.state_path = Path(state_path)
        self.outbox_path = Path(outbox_path)
        if self.state_path.resolve(strict=False) == self.outbox_path.resolve(strict=False):
            raise ValueError("production state and outbox paths must differ")
        self._clock = clock
        self._max_batches = max(1, int(max_batches))
        self._lock = threading.RLock()
        self._state_store = JsonStore(self.state_path.parent)
        self._outbox_store = JsonStore(self.outbox_path.parent)
        self._state = self._load_state()
        self._outbox = self._load_outbox()
        self._reconcile_terminal_batches()

    def _load_state(self) -> dict[str, Any]:
        existed = self.state_path.exists()
        quarantined = any(
            self.state_path.parent.glob(f"{self.state_path.name}.corrupt.*")
        )
        payload = self._state_store.load(self.state_path, None)
        if not _valid_document(payload):
            if existed or quarantined:
                raise ProductionStateCorruptError(
                    "altcoin anomaly production state is corrupt or incompatible"
                )
            return _default_state()
        output = _default_state()
        output["processed_event_ids"] = _bounded_unique(
            list(payload.get("processed_event_ids") or []),
            MAX_PROCESSED_EVENT_IDS,
        )
        output["sent_batch_ids"] = _bounded_unique(
            list(payload.get("sent_batch_ids") or []),
            MAX_SENT_BATCH_IDS,
        )
        timestamps = []
        for value in payload.get("sent_page_timestamps") or []:
            try:
                parsed = float(value)
            except (TypeError, ValueError):
                continue
            if parsed >= 0:
                timestamps.append(parsed)
        output["sent_page_timestamps"] = timestamps[-10_000:]
        symbols = payload.get("symbols")
        if isinstance(symbols, Mapping):
            output["symbols"] = {
                str(symbol).upper(): json_safe(dict(value))
                for symbol, value in symbols.items()
                if isinstance(value, Mapping)
            }
        return output

    def _load_outbox(self) -> dict[str, Any]:
        existed = self.outbox_path.exists()
        quarantined = any(
            self.outbox_path.parent.glob(f"{self.outbox_path.name}.corrupt.*")
        )
        payload = self._outbox_store.load(self.outbox_path, None)
        if not _valid_document(payload):
            if existed or quarantined:
                raise ProductionStateCorruptError(
                    "altcoin anomaly production outbox is corrupt or incompatible"
                )
            return _default_outbox()
        batches = payload.get("batches")
        output = _default_outbox()
        if isinstance(batches, list):
            normalized = [
                json_safe(dict(batch))
                for batch in batches
                if isinstance(batch, Mapping) and batch.get("batch_id")
            ]
            pending = [
                batch for batch in normalized
                if batch.get("status") not in TERMINAL_BATCH_STATUSES
            ]
            terminal = [
                batch for batch in normalized
                if batch.get("status") in PRUNABLE_TERMINAL_BATCH_STATUSES
            ]
            quarantined = [
                batch for batch in normalized
                if batch.get("status") == "quarantined"
            ]
            # Never truncate undelivered WAL entries.  The retention bound is
            # applied only to terminal audit history.  Provider-side effects
            # in quarantine remain until explicit operator recovery.
            output["batches"] = [
                *terminal[-self._max_batches:],
                *quarantined,
                *pending,
            ]
        return output

    def _save_state(self) -> None:
        self._state_store.save(self.state_path, json_safe(self._state))

    def _save_outbox(self) -> None:
        self._outbox_store.save(self.outbox_path, json_safe(self._outbox))

    def _batch(self, batch_id: str) -> dict[str, Any] | None:
        for batch in self._outbox.get("batches") or []:
            if isinstance(batch, dict) and batch.get("batch_id") == batch_id:
                return batch
        return None

    def _apply_terminal_batch(self, batch: Mapping[str, Any]) -> bool:
        batch_id = str(batch.get("batch_id") or "")
        status = str(batch.get("status") or "")
        if status not in {"sent", "suppressed"} or not batch_id:
            return False
        event_ids = [
            str(event.get("event_id") or "")
            for event in batch.get("events") or []
            if isinstance(event, Mapping)
        ]
        existing_events = list(self._state.get("processed_event_ids") or [])
        changed = any(event_id and event_id not in existing_events for event_id in event_ids)
        self._state["processed_event_ids"] = _bounded_unique(
            [*existing_events, *event_ids],
            MAX_PROCESSED_EVENT_IDS,
        )
        if status == "suppressed":
            return changed
        sent_ids = list(self._state.get("sent_batch_ids") or [])
        if batch_id in sent_ids:
            return changed
        self._state["sent_batch_ids"] = _bounded_unique(
            [*sent_ids, batch_id],
            MAX_SENT_BATCH_IDS,
        )
        sent_at = float(batch.get("sent_at_ts") or self._clock())
        page_count = max(1, len(batch.get("pages") or []))
        page_times = list(self._state.get("sent_page_timestamps") or [])
        page_times.extend([sent_at] * page_count)
        self._state["sent_page_timestamps"] = page_times[-10_000:]
        symbol = str(batch.get("symbol") or "").upper()
        symbol_state = dict((self._state.get("symbols") or {}).get(symbol) or {})
        lifecycle = str(batch.get("notification_kind") or "")
        if lifecycle in {"first_confirmation", "new_round"}:
            symbol_state.update({
                "active": True,
                "ever_confirmed": True,
                "last_confirmation_sent_at": sent_at,
                "last_confirmation_batch_id": batch_id,
                "last_window_end": str(batch.get("window_end") or ""),
            })
        elif lifecycle in {"signal_expired", "candidate_invalidated"}:
            symbol_state.update({
                "active": False,
                "last_terminal_sent_at": sent_at,
                "last_terminal_kind": lifecycle,
                "last_window_end": str(batch.get("window_end") or ""),
            })
        if symbol:
            self._state.setdefault("symbols", {})[symbol] = symbol_state
        return True

    def _reconcile_terminal_batches(self) -> None:
        changed = False
        with self._lock:
            for batch in self._outbox.get("batches") or []:
                if isinstance(batch, Mapping):
                    changed = self._apply_terminal_batch(batch) or changed
            if changed:
                self._save_state()

    def state_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._state)

    def known_event_ids(self) -> set[str]:
        with self._lock:
            output = set(str(value) for value in self._state.get("processed_event_ids") or [])
            for batch in self._outbox.get("batches") or []:
                if not isinstance(batch, Mapping):
                    continue
                for event in batch.get("events") or []:
                    if isinstance(event, Mapping) and event.get("event_id"):
                        output.add(str(event["event_id"]))
            return output

    def classify(
        self,
        *,
        symbol: str,
        event_types: set[str],
        now_ts: float,
        cooldown_sec: int,
    ) -> tuple[str | None, str]:
        with self._lock:
            symbol_state = dict(
                (self._state.get("symbols") or {}).get(str(symbol).upper()) or {}
            )
            pending_for_symbol = [
                batch
                for batch in self._outbox.get("batches") or []
                if isinstance(batch, Mapping)
                and batch.get("status") == "pending"
                and str(batch.get("symbol") or "").upper() == str(symbol).upper()
            ]
            quarantined_for_symbol = any(
                isinstance(batch, Mapping)
                and batch.get("status") == "quarantined"
                and str(batch.get("symbol") or "").upper() == str(symbol).upper()
                for batch in self._outbox.get("batches") or []
            )
        # A provider quarantine means Telegram may already have produced an
        # externally visible effect that cannot be proven locally.  No later
        # notification for that symbol is safe until an operator explicitly
        # resolves the quarantined WAL record.
        if quarantined_for_symbol:
            return None, "symbol_quarantined"
        active = bool(symbol_state.get("active"))
        pending_confirmation = any(
            str(batch.get("notification_kind") or "")
            in {"first_confirmation", "new_round"}
            for batch in pending_for_symbol
        )
        if event_types & INVALIDATION_EVENT_TYPES:
            return (
                ("candidate_invalidated", "")
                if active or pending_confirmation
                else (None, "no_active_signal")
            )
        if event_types & EXPIRATION_EVENT_TYPES:
            return (
                ("signal_expired", "")
                if active or pending_confirmation
                else (None, "no_active_signal")
            )
        if not event_types & CONFIRMATION_EVENT_TYPES:
            return None, "unsupported_event_type"
        if pending_confirmation:
            return None, "pending_confirmation"
        last_sent = float(symbol_state.get("last_confirmation_sent_at") or 0.0)
        if last_sent and float(now_ts) - last_sent < max(0, int(cooldown_sec)):
            return None, "symbol_cooldown"
        if not bool(symbol_state.get("ever_confirmed")):
            return "first_confirmation", ""
        return "new_round", ""

    def enqueue(
        self,
        events: Sequence[Mapping[str, Any]],
        *,
        notification_kind: str,
        now_ts: float,
    ) -> tuple[dict[str, Any], bool]:
        results = self.enqueue_many([{
            "events": list(events),
            "notification_kind": notification_kind,
            "suppression_reason": "",
        }], now_ts=now_ts)
        return results[0]

    @staticmethod
    def _new_batch(
        events: Sequence[Mapping[str, Any]],
        *,
        notification_kind: str,
        suppression_reason: str,
        now_ts: float,
    ) -> dict[str, Any]:
        if not events:
            raise ValueError("production event batch cannot be empty")
        batch_id = deterministic_production_batch_id(events)
        symbol = str(events[0].get("symbol") or "").upper()
        window_start = str(events[0].get("window_start") or "")
        window_end = str(events[0].get("window_end") or "")
        if not symbol or not window_end:
            raise ValueError("production event batch requires symbol and window_end")
        suppressed = bool(suppression_reason)
        return json_safe({
                "batch_id": batch_id,
                "symbol": symbol,
                "window_start": window_start,
                "window_end": window_end,
                "notification_kind": str(notification_kind),
                "events": [dict(event) for event in events],
                "event_ids": sorted(str(event.get("event_id") or "") for event in events),
                "event_types": sorted(str(event.get("event_type") or "") for event in events),
                "created_at": _iso(now_ts),
                "created_at_ts": float(now_ts),
                "status": "suppressed" if suppressed else "pending",
                "suppressed_reason": str(suppression_reason)[:100],
                "pages": None,
                "next_page_index": 0,
                "attempts": 0,
                "last_error_class": "",
                "next_attempt_at": _iso(now_ts),
                "next_attempt_at_ts": float(now_ts),
            })

    def enqueue_many(
        self,
        entries: Sequence[Mapping[str, Any]],
        *,
        now_ts: float,
    ) -> list[tuple[dict[str, Any], bool]]:
        """Atomically accept every group from one sink submission into WAL."""

        prepared = [
            self._new_batch(
                list(entry.get("events") or []),
                notification_kind=str(entry.get("notification_kind") or ""),
                suppression_reason=str(entry.get("suppression_reason") or ""),
                now_ts=now_ts,
            )
            for entry in entries
        ]
        if not prepared:
            return []
        with self._lock:
            existing_by_id = {
                str(batch.get("batch_id") or ""): batch
                for batch in self._outbox.get("batches") or []
                if isinstance(batch, Mapping)
            }
            results: list[tuple[dict[str, Any], bool]] = []
            added: list[dict[str, Any]] = []
            for batch in prepared:
                batch_id = str(batch["batch_id"])
                existing = existing_by_id.get(batch_id)
                if existing is not None:
                    results.append((deepcopy(existing), False))
                    continue
                existing_by_id[batch_id] = batch
                added.append(batch)
                results.append((deepcopy(batch), True))
            if not added:
                return results
            current = list(self._outbox.get("batches") or [])
            pending = [
                batch for batch in current
                if isinstance(batch, Mapping)
                and batch.get("status") not in TERMINAL_BATCH_STATUSES
            ]
            terminal = [
                batch for batch in current
                if isinstance(batch, Mapping)
                and batch.get("status") in PRUNABLE_TERMINAL_BATCH_STATUSES
            ]
            quarantined = [
                batch for batch in current
                if isinstance(batch, Mapping)
                and batch.get("status") == "quarantined"
            ]
            added_terminal = [
                batch for batch in added if batch.get("status") == "suppressed"
            ]
            added_pending = [
                batch for batch in added if batch.get("status") == "pending"
            ]
            if len(pending) + len(added_pending) > self._max_batches:
                raise OverflowError("production outbox pending capacity exceeded")
            retained_terminal = [*terminal, *added_terminal][-self._max_batches:]
            previous_outbox = self._outbox
            self._outbox = {
                "schema_version": PRODUCTION_STATE_SCHEMA_VERSION,
                "module": PRODUCTION_STATE_MODULE,
                "batches": [
                    *retained_terminal,
                    *quarantined,
                    *pending,
                    *added_pending,
                ],
            }
            try:
                self._save_outbox()
            except Exception:
                self._outbox = previous_outbox
                raise
            state_changed = False
            for batch in added_terminal:
                state_changed = self._apply_terminal_batch(batch) or state_changed
            if state_changed:
                self._save_state()
            return results

    def set_pages(self, batch_id: str, pages: Sequence[str]) -> dict[str, Any]:
        normalized = [str(page) for page in pages if str(page)]
        if not normalized:
            raise ValueError("production formatter returned no pages")
        with self._lock:
            batch = self._batch(batch_id)
            if batch is None:
                raise KeyError(batch_id)
            if batch.get("pages") is None:
                previous = deepcopy(batch)
                batch["pages"] = normalized
                batch["next_page_index"] = 0
                try:
                    self._save_outbox()
                except Exception:
                    batch.clear()
                    batch.update(previous)
                    raise
            elif list(batch.get("pages") or []) != normalized:
                raise ValueError("production pages changed for an existing WAL batch")
            return deepcopy(batch)

    def mark_failure(
        self,
        batch_id: str,
        error_class: str,
        *,
        now_ts: float | None = None,
        retry_after_sec: float | None = None,
        base_delay_sec: float = 5.0,
        max_delay_sec: float = 900.0,
    ) -> dict[str, Any] | None:
        """Persist a bounded retry deadline before returning to the worker.

        The exponential component is capped.  A provider supplied
        ``Retry-After`` may extend that deadline beyond the local cap and is
        never shortened.
        """

        effective_now = float(self._clock() if now_ts is None else now_ts)
        with self._lock:
            batch = self._batch(batch_id)
            if batch is None:
                return None
            previous = deepcopy(batch)
            attempts = int(batch.get("attempts") or 0) + 1
            base_delay = max(0.001, float(base_delay_sec))
            max_delay = max(base_delay, float(max_delay_sec))
            exponent = min(30, max(0, attempts - 1))
            exponential_delay = min(max_delay, base_delay * (2 ** exponent))
            provider_delay = 0.0
            if retry_after_sec is not None:
                try:
                    provider_delay = max(0.0, float(retry_after_sec))
                except (TypeError, ValueError):
                    provider_delay = 0.0
                if not isfinite(provider_delay):
                    provider_delay = 0.0
            delay = max(exponential_delay, provider_delay)
            batch["attempts"] = attempts
            batch["last_error_class"] = str(error_class or "delivery_failed")[:100]
            batch["last_failure_at"] = _iso(effective_now)
            batch["last_failure_at_ts"] = effective_now
            batch["retry_delay_sec"] = delay
            batch["retry_after_sec"] = provider_delay or None
            batch["next_attempt_at_ts"] = effective_now + delay
            batch["next_attempt_at"] = _iso(effective_now + delay)
            try:
                self._save_outbox()
            except Exception:
                batch.clear()
                batch.update(previous)
                raise
            return deepcopy(batch)

    def mark_page_delivered(
        self,
        batch_id: str,
        page_index: int,
        *,
        recovered_by_dedup: bool,
        previewed: bool = False,
    ) -> dict[str, Any]:
        with self._lock:
            batch = self._batch(batch_id)
            if batch is None:
                raise KeyError(batch_id)
            previous = deepcopy(batch)
            expected = int(batch.get("next_page_index") or 0)
            if page_index < expected:
                return deepcopy(batch)
            if page_index != expected:
                raise ValueError("production page acknowledgement is out of order")
            outcomes = list(batch.get("page_outcomes") or [])
            outcomes.append({
                "page_index": page_index,
                "delivered_at": _iso(self._clock()),
                "recovered_by_dedup": bool(recovered_by_dedup),
                "previewed": bool(previewed),
            })
            batch["page_outcomes"] = outcomes
            batch["next_page_index"] = expected + 1
            batch["last_error_class"] = ""
            batch["next_attempt_at_ts"] = float(self._clock())
            batch["next_attempt_at"] = _iso(self._clock())
            try:
                self._save_outbox()
            except Exception:
                batch.clear()
                batch.update(previous)
                raise
            return deepcopy(batch)

    def mark_previewed(self, batch_id: str, *, now_ts: float) -> None:
        """Finish a dry-run batch without advancing production lifecycle state."""

        with self._lock:
            batch = self._batch(batch_id)
            if batch is None:
                raise KeyError(batch_id)
            if int(batch.get("next_page_index") or 0) < len(batch.get("pages") or []):
                raise ValueError("cannot commit a partially previewed production batch")
            previous = deepcopy(batch)
            batch["status"] = "previewed"
            batch["previewed_at"] = _iso(now_ts)
            batch["previewed_at_ts"] = float(now_ts)
            batch["next_attempt_at"] = ""
            batch["next_attempt_at_ts"] = None
            try:
                self._save_outbox()
            except Exception:
                batch.clear()
                batch.update(previous)
                raise

    def mark_quarantined(self, batch_id: str, reason: str, *, now_ts: float) -> None:
        """Permanently stop an unknowable provider-side effect for manual review."""

        with self._lock:
            batch = self._batch(batch_id)
            if batch is None:
                raise KeyError(batch_id)
            previous = deepcopy(batch)
            batch["status"] = "quarantined"
            batch["quarantine_reason"] = str(reason or "delivery_quarantine")[:100]
            batch["quarantined_at"] = _iso(now_ts)
            batch["quarantined_at_ts"] = float(now_ts)
            batch["next_attempt_at"] = ""
            batch["next_attempt_at_ts"] = None
            try:
                self._save_outbox()
            except Exception:
                batch.clear()
                batch.update(previous)
                raise

    def mark_sent(self, batch_id: str, *, now_ts: float) -> None:
        with self._lock:
            batch = self._batch(batch_id)
            if batch is None:
                raise KeyError(batch_id)
            if int(batch.get("next_page_index") or 0) < len(batch.get("pages") or []):
                raise ValueError("cannot commit a partially delivered production batch")
            previous = deepcopy(batch)
            batch["status"] = "sent"
            batch["sent_at"] = _iso(now_ts)
            batch["sent_at_ts"] = float(now_ts)
            batch["next_attempt_at"] = ""
            batch["next_attempt_at_ts"] = None
            try:
                self._save_outbox()
            except Exception:
                batch.clear()
                batch.update(previous)
                raise
            if self._apply_terminal_batch(batch):
                self._save_state()

    def mark_suppressed(self, batch_id: str, reason: str) -> None:
        with self._lock:
            batch = self._batch(batch_id)
            if batch is None:
                raise KeyError(batch_id)
            previous = deepcopy(batch)
            batch["status"] = "suppressed"
            batch["suppressed_reason"] = str(reason or "policy_suppressed")[:100]
            try:
                self._save_outbox()
            except Exception:
                batch.clear()
                batch.update(previous)
                raise
            if self._apply_terminal_batch(batch):
                self._save_state()

    def pending_batches(
        self,
        *,
        now_ts: float | None = None,
        due_only: bool = False,
    ) -> list[dict[str, Any]]:
        effective_now = float(self._clock() if now_ts is None else now_ts)
        with self._lock:
            return [
                deepcopy(batch)
                for batch in self._outbox.get("batches") or []
                if (
                    isinstance(batch, Mapping)
                    and batch.get("status") == "pending"
                    and (
                        not due_only
                        or _retry_deadline_due(batch, effective_now)
                    )
                )
            ]

    def can_send_page(self, *, now_ts: float, max_per_hour: int) -> bool:
        if max_per_hour <= 0:
            return False
        cutoff = float(now_ts) - 3600.0
        with self._lock:
            recent = [
                float(value)
                for value in self._state.get("sent_page_timestamps") or []
                if float(value) >= cutoff
            ]
            # Pages durably acknowledged in a still-pending batch have already
            # consumed Telegram capacity, even though the batch cursor is not
            # committed until every page succeeds.
            for batch in self._outbox.get("batches") or []:
                if not isinstance(batch, Mapping) or batch.get("status") != "pending":
                    continue
                for outcome in batch.get("page_outcomes") or []:
                    if not isinstance(outcome, Mapping):
                        continue
                    if bool(outcome.get("previewed")):
                        continue
                    delivered_at = outcome.get("delivered_at")
                    try:
                        parsed = datetime.fromisoformat(
                            str(delivered_at).replace("Z", "+00:00")
                        ).timestamp()
                    except (TypeError, ValueError):
                        continue
                    if parsed >= cutoff:
                        recent.append(parsed)
        return len(recent) < int(max_per_hour)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            batches = list(self._outbox.get("batches") or [])
            quarantined_symbols = {
                str(batch.get("symbol") or "").upper()
                for batch in batches
                if isinstance(batch, Mapping)
                and batch.get("status") == "quarantined"
                and str(batch.get("symbol") or "").strip()
            }
            pending = [
                batch for batch in batches
                if isinstance(batch, Mapping) and batch.get("status") == "pending"
            ]
            retry_deadlines = []
            pending_retry_batches = 0
            for batch in pending:
                try:
                    if int(batch.get("attempts") or 0) > 0:
                        pending_retry_batches += 1
                except (TypeError, ValueError):
                    pass
                try:
                    deadline = float(batch.get("next_attempt_at_ts") or 0.0)
                except (TypeError, ValueError):
                    continue
                if deadline > 0 and isfinite(deadline):
                    retry_deadlines.append(deadline)
            next_deadline = min(retry_deadlines) if retry_deadlines else None
            return {
                "pending_batches": len(pending),
                "pending_capacity": self._max_batches,
                "pending_capacity_remaining": max(
                    0,
                    self._max_batches - len(pending),
                ),
                "pending_retry_batches": pending_retry_batches,
                "next_attempt_at_ts": next_deadline,
                "next_attempt_at": _iso(next_deadline) if next_deadline is not None else "",
                "sent_batches": len(self._state.get("sent_batch_ids") or []),
                "previewed_batches": sum(
                    1 for batch in batches
                    if isinstance(batch, Mapping) and batch.get("status") == "previewed"
                ),
                "quarantined_batches": sum(
                    1 for batch in batches
                    if isinstance(batch, Mapping) and batch.get("status") == "quarantined"
                ),
                "quarantined_symbols": len(quarantined_symbols),
                "processed_events": len(self._state.get("processed_event_ids") or []),
                "tracked_symbols": len(self._state.get("symbols") or {}),
            }


__all__ = [
    "CONFIRMATION_EVENT_TYPES",
    "EXPIRATION_EVENT_TYPES",
    "INVALIDATION_EVENT_TYPES",
    "PRODUCTION_STATE_MODULE",
    "PRODUCTION_STATE_SCHEMA_VERSION",
    "ProductionStateCorruptError",
    "ProductionStateStore",
    "deterministic_production_batch_id",
]
