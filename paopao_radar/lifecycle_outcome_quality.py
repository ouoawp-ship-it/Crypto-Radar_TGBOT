from __future__ import annotations

import json
import re
import sqlite3
import time
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from .atomic_json import atomic_write_text
from .config import BASE_DIR, Settings
from .lifecycle_intelligence_store import IntelligenceStore
from .lifecycle_store import normalize_lifecycle_symbol, safe_int
from .outcome_tracker import OUTCOME_WINDOWS, OutcomeStore, scan_signal_outcomes


HORIZONS = tuple(OUTCOME_WINDOWS)
ELIGIBILITY_STATUSES = {"eligible", "ineligible", "unknown"}
CANDIDATE_STATUSES = {
    "not_due", "ready", "queued", "processing", "linked", "success", "unavailable",
    "retry_wait", "terminal_ineligible", "terminal_unavailable", "terminal_error",
}
INELIGIBLE_REASONS = {
    "aggregate_summary_signal", "announcement_signal", "test_signal", "dry_run_signal",
    "failed_signal", "blocked_signal", "skipped_signal", "missing_symbol", "invalid_symbol",
    "unsupported_quote_asset", "non_binance_symbol", "missing_signal_id", "missing_signal_time",
    "invalid_signal_time", "unsupported_module", "unsupported_signal_type", "unsupported_horizon",
    "duplicate_candidate", "lifecycle_event_without_signal",
}
GAP_REASONS = {
    "not_due", "queued_for_backfill", "backfill_not_attempted", "backfill_in_progress",
    "outcome_row_missing", "historical_kline_unavailable", "symbol_delisted",
    "spot_pair_unavailable", "futures_pair_unavailable", "provider_rate_limited",
    "provider_timeout", "provider_network_error", "provider_response_invalid",
    "ambiguous_legacy_match", "signal_not_found", "time_mismatch", "module_mismatch",
    "outcome_unavailable", "retry_exhausted",
    "real_error", "outcome_success", "outcome_linked",
}
RESOLVED_STATUSES = {"success", "terminal_unavailable", "terminal_error"}
LINKED_STATUSES = {"linked", "success", "unavailable", "terminal_unavailable", "terminal_error"}
TERMINAL_STATUSES = {"success", "terminal_ineligible", "terminal_unavailable", "terminal_error"}
UNAVAILABLE_STATUSES = {"unavailable", "terminal_unavailable"}
ERROR_STATUSES = {"terminal_error"}
GENERIC_REASON = "no_outcome_row"
QUALITY_REPORT_JSON = BASE_DIR / "docs" / "generated" / "lifecycle_outcome_quality_latest.json"
QUALITY_REPORT_MD = BASE_DIR / "docs" / "generated" / "lifecycle_outcome_quality_latest.md"
READINESS_REPORT_JSON = BASE_DIR / "docs" / "generated" / "lifecycle_calibration_readiness_latest.json"


def _utc_now(value: datetime | int | float | None = None) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        parsed = datetime.fromtimestamp(float(value), timezone.utc)
    else:
        parsed = datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        try:
            parsed = datetime.fromtimestamp(float(value), timezone.utc)
        except (OSError, OverflowError, ValueError):
            return None
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | int | float | None = None) -> str:
    return _utc_now(value).isoformat()


def _ratio(numerator: int | float, denominator: int | float) -> float:
    return round(float(numerator) / float(denominator), 6) if denominator else 0.0


def _normalized_symbol_reason(value: Any, exchange: Any = "") -> tuple[str, str]:
    original = str(value or "").strip().upper()
    raw = re.sub(r"[\s/_-]+", "", original)
    exchange_name = str(exchange or "").strip().lower()
    if exchange_name and exchange_name not in {"binance", "binance_spot", "binance_futures"}:
        return "", "non_binance_symbol"
    if ":" in str(value or ""):
        prefix = str(value).split(":", 1)[0].strip().lower()
        if prefix and prefix != "binance":
            return "", "non_binance_symbol"
    if not raw:
        return "", "missing_symbol"
    if re.search(r"[^A-Z0-9\s/_:-]", original):
        return "", "invalid_symbol"
    if re.fullmatch(r"[A-Z0-9]{2,30}", raw) and raw.endswith(("USDC", "BUSD", "BTC", "ETH")):
        return "", "unsupported_quote_asset"
    normalized = normalize_lifecycle_symbol(raw)
    if normalized:
        return normalized, ""
    if re.fullmatch(r"[A-Z0-9]{2,30}", raw) and raw.endswith("USD"):
        return "", "unsupported_quote_asset"
    return "", "invalid_symbol"


def stable_candidate_key(
    *,
    lifecycle_id: Any,
    signal_id: Any,
    lifecycle_event_id: Any,
    signal_time: Any,
    horizon: Any,
) -> str:
    """Build the deterministic v1.78.2 candidate identity."""

    lifecycle = safe_int(lifecycle_id)
    signal = safe_int(signal_id)
    event = safe_int(lifecycle_event_id)
    normalized_horizon = str(horizon or "").strip().lower()
    if lifecycle <= 0 or normalized_horizon not in HORIZONS:
        raise ValueError("valid lifecycle_id and horizon are required")
    if signal > 0:
        return f"{lifecycle}:{signal}:{normalized_horizon}"
    parsed = _parse_time(signal_time)
    normalized_time = parsed.isoformat() if parsed is not None else "missing-time"
    event_part = str(event) if event > 0 else "legacy"
    return f"{lifecycle}:event:{event_part}:{normalized_time}:{normalized_horizon}"


def retry_delay_seconds(
    attempt_count: int,
    *,
    base_sec: int = 900,
    max_sec: int = 21600,
) -> int:
    exponent = max(0, safe_int(attempt_count) - 1)
    return min(max(1, safe_int(max_sec, 21600)), max(1, safe_int(base_sec, 900)) * (2**exponent))


@dataclass(frozen=True)
class CandidateClassification:
    eligible: bool
    eligibility_status: str
    eligibility_reason: str
    candidate_status: str
    is_terminal: bool
    is_retryable: bool
    due_at: str
    next_action: str
    next_retry_at: str = ""
    last_error_code: str = ""
    last_error_summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def classify_provider_failure(
    error: Any,
    *,
    attempt_count: int = 1,
    now: datetime | int | float | None = None,
    retry_max_attempts: int = 5,
    retry_base_sec: int = 900,
    retry_max_sec: int = 21600,
) -> CandidateClassification:
    message = str(error or "").strip()[:300]
    lower = message.lower()
    permanent_reason = ""
    if any(token in lower for token in ("invalid symbol", "unknown symbol", "symbol not found", "delisted")):
        permanent_reason = "symbol_delisted"
    elif any(token in lower for token in ("historical kline", "empty kline", "history unavailable", "no kline")):
        permanent_reason = "historical_kline_unavailable"
    elif "spot pair" in lower and "unavailable" in lower:
        permanent_reason = "spot_pair_unavailable"
    elif "futures pair" in lower and "unavailable" in lower:
        permanent_reason = "futures_pair_unavailable"
    if permanent_reason:
        return CandidateClassification(
            True, "eligible", permanent_reason, "terminal_unavailable", True, False, "",
            "none", last_error_code=permanent_reason, last_error_summary=message,
        )
    if any(token in lower for token in ("429", "418", "rate limit", "too many requests")):
        reason = "provider_rate_limited"
    elif any(token in lower for token in ("timeout", "timed out", "readtimeout")):
        reason = "provider_timeout"
    elif any(token in lower for token in ("connection", "network", "dns", "temporary failure")):
        reason = "provider_network_error"
    elif any(token in lower for token in ("json", "parse", "invalid response", "malformed")):
        reason = "provider_response_invalid"
    else:
        reason = "real_error"
    attempts = max(1, safe_int(attempt_count, 1))
    if attempts >= max(1, safe_int(retry_max_attempts, 5)):
        return CandidateClassification(
            True, "eligible", "retry_exhausted", "terminal_error", True, False, "", "none",
            last_error_code=reason, last_error_summary=message,
        )
    delay = retry_delay_seconds(attempts, base_sec=retry_base_sec, max_sec=retry_max_sec)
    retry_at = _utc_now(now) + timedelta(seconds=delay)
    return CandidateClassification(
        True, "eligible", reason, "retry_wait", False, True, "", "retry",
        next_retry_at=retry_at.isoformat(), last_error_code=reason, last_error_summary=message,
    )


def _ineligible(reason: str) -> CandidateClassification:
    return CandidateClassification(False, "ineligible", reason, "terminal_ineligible", True, False, "", "none")


def classify_outcome_candidate(
    signal: dict[str, Any] | None,
    lifecycle_event: dict[str, Any] | None,
    horizon: str,
    now: datetime | int | float | None,
    *,
    outcome: dict[str, Any] | None = None,
    current: dict[str, Any] | None = None,
    retry_max_attempts: int = 5,
    retry_base_sec: int = 900,
    retry_max_sec: int = 21600,
    processing_stale_sec: int = 1800,
) -> CandidateClassification:
    """Classify one candidate without database or network side effects."""

    source = dict(signal or {})
    event = dict(lifecycle_event or {})
    existing = dict(current or {})
    normalized_horizon = str(horizon or "").strip().lower()
    if normalized_horizon not in HORIZONS:
        return _ineligible("unsupported_horizon")
    signal_id = safe_int(source.get("id") or source.get("signal_id") or event.get("signal_id"))
    legacy_unique = bool(source.get("legacy_match_unique"))
    if bool(source.get("duplicate_candidate")):
        return _ineligible("duplicate_candidate")
    module = str(source.get("module") or source.get("source_module") or event.get("source_module") or "").strip().lower()
    template = str(source.get("template_id") or source.get("template") or source.get("source_template") or event.get("source_template") or "").strip().lower()
    signal_type = str(source.get("signal_type") or source.get("source_signal_type") or "").strip().lower()
    status = str(source.get("status") or source.get("source_status") or "sent").strip().lower()
    if module == "summary" or bool(source.get("is_aggregate")) or signal_type in {"summary", "aggregate"}:
        return _ineligible("aggregate_summary_signal")
    if module == "announcement" or signal_type == "announcement" or "announcement" in template:
        return _ineligible("announcement_signal")
    if module == "test" or signal_type == "test" or "test" in template:
        return _ineligible("test_signal")
    if status in {"dry_run", "dry-run"} or bool(source.get("dry_run")):
        return _ineligible("dry_run_signal")
    if status == "failed":
        return _ineligible("failed_signal")
    if status == "blocked":
        return _ineligible("blocked_signal")
    if status == "skipped":
        return _ineligible("skipped_signal")
    if bool(source.get("unsupported_module")):
        return _ineligible("unsupported_module")
    if bool(source.get("unsupported_signal_type")):
        return _ineligible("unsupported_signal_type")
    raw_symbol = source.get("symbol") or event.get("symbol")
    symbol, symbol_reason = _normalized_symbol_reason(raw_symbol, source.get("exchange"))
    if symbol_reason:
        return _ineligible(symbol_reason)
    legacy_no_match_reason = str(source.get("legacy_match_reason") or "")
    if signal_id <= 0 and legacy_no_match_reason:
        return CandidateClassification(
            False, "unknown", legacy_no_match_reason, "terminal_ineligible",
            True, False, "", "manual_review",
        )
    if signal_id <= 0 and bool(source.get("legacy_ambiguous")):
        return CandidateClassification(
            False, "unknown", "ambiguous_legacy_match", "terminal_ineligible",
            True, False, "", "manual_review",
        )
    if signal_id <= 0 and not legacy_unique:
        return _ineligible("lifecycle_event_without_signal" if event else "missing_signal_id")
    raw_time = source.get("time") or source.get("signal_time") or source.get("ts") or event.get("event_time")
    if raw_time in (None, ""):
        return _ineligible("missing_signal_time")
    signal_time = _parse_time(raw_time)
    if signal_time is None:
        return _ineligible("invalid_signal_time")
    current_time = _utc_now(now)
    due = signal_time + timedelta(seconds=OUTCOME_WINDOWS[normalized_horizon])
    due_text = due.isoformat()
    outcome_row = dict(outcome or {})
    outcome_status = str(outcome_row.get("data_status") or outcome_row.get("outcome_status") or "").strip().lower()
    current_status = str(existing.get("candidate_status") or "")
    if outcome_status != "success" and current_status == "processing":
        last_attempt = _parse_time(existing.get("last_attempt_at"))
        stale = last_attempt is None or (
            current_time - last_attempt
        ).total_seconds() >= max(1, safe_int(processing_stale_sec, 1800))
        if not stale:
            return CandidateClassification(
                True, "eligible", "backfill_in_progress", "processing", False, False,
                due_text, "wait_processing",
            )
        if outcome_status == "error":
            return CandidateClassification(
                True, "eligible", str(existing.get("eligibility_reason") or "real_error"),
                "ready", False, True, due_text, "retry",
                last_error_code=str(existing.get("last_error_code") or ""),
                last_error_summary=str(existing.get("last_error_summary") or "")[:300],
            )
    if outcome_status == "error" and current_status == "retry_wait":
        next_retry = _parse_time(existing.get("next_retry_at"))
        if next_retry is not None and next_retry > current_time:
            return CandidateClassification(
                True, "eligible", str(existing.get("eligibility_reason") or "real_error"),
                "retry_wait", False, True, due_text, "wait_retry",
                next_retry_at=next_retry.isoformat(),
                last_error_code=str(existing.get("last_error_code") or ""),
                last_error_summary=str(existing.get("last_error_summary") or "")[:300],
            )
        return CandidateClassification(
            True, "eligible", str(existing.get("eligibility_reason") or "real_error"),
            "ready", False, True, due_text, "retry",
            last_error_code=str(existing.get("last_error_code") or ""),
            last_error_summary=str(existing.get("last_error_summary") or "")[:300],
        )
    if outcome_status == "error" and current_status in {"ready", "queued"} and safe_int(existing.get("attempt_count")) > 0:
        return CandidateClassification(
            True, "eligible", str(existing.get("eligibility_reason") or "real_error"),
            "ready", False, True, due_text, "retry",
            last_error_code=str(existing.get("last_error_code") or ""),
            last_error_summary=str(existing.get("last_error_summary") or "")[:300],
        )
    if outcome_row:
        if outcome_status == "success":
            return CandidateClassification(True, "eligible", "outcome_success", "success", True, False, due_text, "none")
        if outcome_status == "unavailable":
            failure = classify_provider_failure(
                outcome_row.get("error") or outcome_row.get("error_summary") or "outcome unavailable",
                attempt_count=max(1, safe_int(existing.get("attempt_count"))), now=current_time,
                retry_max_attempts=retry_max_attempts, retry_base_sec=retry_base_sec,
                retry_max_sec=retry_max_sec,
            )
            if failure.candidate_status == "terminal_unavailable":
                return CandidateClassification(
                    True, "eligible", failure.eligibility_reason, "terminal_unavailable", True, False,
                    due_text, "none", last_error_code=failure.last_error_code,
                    last_error_summary=failure.last_error_summary,
                )
            return CandidateClassification(
                True, "eligible", "outcome_unavailable", "terminal_unavailable",
                True, False, due_text, "none",
            )
        if outcome_status == "error":
            failure = classify_provider_failure(
                outcome_row.get("error") or outcome_row.get("error_summary") or "real error",
                attempt_count=max(1, safe_int(existing.get("attempt_count"))), now=current_time,
                retry_max_attempts=retry_max_attempts, retry_base_sec=retry_base_sec,
                retry_max_sec=retry_max_sec,
            )
            return CandidateClassification(
                True, "eligible", failure.eligibility_reason, failure.candidate_status,
                failure.is_terminal, failure.is_retryable, due_text, failure.next_action,
                next_retry_at=failure.next_retry_at, last_error_code=failure.last_error_code,
                last_error_summary=failure.last_error_summary,
            )
        if outcome_status in {"pending", "ready"}:
            if due > current_time:
                return CandidateClassification(True, "eligible", "not_due", "not_due", False, False, due_text, "wait_due")
            return CandidateClassification(True, "eligible", "outcome_linked", "linked", False, False, due_text, "outcome_tracker")
    if due > current_time:
        return CandidateClassification(True, "eligible", "not_due", "not_due", False, False, due_text, "wait_due")
    if current_status == "processing":
        last_attempt = _parse_time(existing.get("last_attempt_at"))
        stale = last_attempt is None or (current_time - last_attempt).total_seconds() >= max(1, safe_int(processing_stale_sec, 1800))
        if not stale:
            return CandidateClassification(True, "eligible", "backfill_in_progress", "processing", False, False, due_text, "wait_processing")
    if current_status == "retry_wait":
        next_retry = _parse_time(existing.get("next_retry_at"))
        if next_retry is not None and next_retry > current_time:
            return CandidateClassification(
                True, "eligible", str(existing.get("eligibility_reason") or "real_error"),
                "retry_wait", False, True, due_text, "wait_retry", next_retry_at=next_retry.isoformat(),
                last_error_code=str(existing.get("last_error_code") or ""),
                last_error_summary=str(existing.get("last_error_summary") or "")[:300],
            )
    if current_status in {"terminal_unavailable", "terminal_error", "success"}:
        return CandidateClassification(
            True, "eligible", str(existing.get("eligibility_reason") or "real_error"), current_status,
            True, False, due_text, "none", last_error_code=str(existing.get("last_error_code") or ""),
            last_error_summary=str(existing.get("last_error_summary") or "")[:300],
        )
    attempts = safe_int(existing.get("attempt_count"))
    reason = "outcome_row_missing" if attempts > 0 else "backfill_not_attempted"
    return CandidateClassification(True, "eligible", reason, "ready", False, False, due_text, "queue_backfill")


def build_candidate_record(
    lifecycle: dict[str, Any],
    extracted: dict[str, Any],
    signal: dict[str, Any] | None,
    horizon: str,
    *,
    outcome: dict[str, Any] | None = None,
    current: dict[str, Any] | None = None,
    now: datetime | int | float | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    source = dict(extracted)
    source.update({key: value for key, value in dict(signal or {}).items() if value not in (None, "")})
    source["signal_lookup_missing"] = signal is None and safe_int(extracted.get("signal_id")) > 0
    source.setdefault("id", extracted.get("signal_id"))
    source.setdefault("symbol", lifecycle.get("symbol") or extracted.get("symbol"))
    source.setdefault("time", extracted.get("signal_time"))
    source.setdefault("module", extracted.get("module"))
    source.setdefault("template_id", extracted.get("template"))
    source.setdefault("signal_type", extracted.get("signal_type"))
    loaded = settings
    classification = classify_outcome_candidate(
        source,
        {
            "id": extracted.get("lifecycle_event_id"),
            "signal_id": extracted.get("signal_id"),
            "event_time": extracted.get("signal_time"),
            "symbol": extracted.get("symbol"),
            "source_module": extracted.get("module"),
            "source_template": extracted.get("template"),
        },
        horizon,
        now,
        outcome=outcome,
        current=current,
        retry_max_attempts=safe_int(getattr(loaded, "lifecycle_outcome_retry_max_attempts", 5), 5),
        retry_base_sec=safe_int(getattr(loaded, "lifecycle_outcome_retry_base_sec", 900), 900),
        retry_max_sec=safe_int(getattr(loaded, "lifecycle_outcome_retry_max_sec", 21600), 21600),
        processing_stale_sec=safe_int(getattr(loaded, "lifecycle_outcome_processing_stale_sec", 1800), 1800),
    )
    if (
        source.get("signal_lookup_missing")
        and outcome is None
        and classification.eligibility_status == "eligible"
        and classification.candidate_status == "ready"
    ):
        classification = CandidateClassification(
            True, "eligible", "signal_not_found", "ready", False, False,
            classification.due_at, "restore_signal",
        )
    lifecycle_id = safe_int(lifecycle.get("id") or lifecycle.get("lifecycle_id"))
    signal_id = safe_int(extracted.get("signal_id")) or safe_int(source.get("id"))
    legacy_identity = bool(extracted.get("legacy_identity"))
    identity_signal_id = 0 if legacy_identity else signal_id
    key = stable_candidate_key(
        lifecycle_id=lifecycle_id, signal_id=identity_signal_id,
        lifecycle_event_id=extracted.get("lifecycle_event_id"),
        signal_time=(
            extracted.get("signal_time")
            if legacy_identity
            else source.get("time") or extracted.get("signal_time")
        ),
        horizon=horizon,
    )
    previous = dict(current or {})
    return {
        "candidate_key": key,
        "lifecycle_id": lifecycle_id,
        "lifecycle_event_id": safe_int(extracted.get("lifecycle_event_id")) or None,
        "signal_id": signal_id or None,
        "symbol": normalize_lifecycle_symbol(source.get("symbol") or lifecycle.get("symbol")),
        "signal_time": (_parse_time(source.get("time") or extracted.get("signal_time")) or _parse_time(previous.get("signal_time"))).isoformat()
        if (_parse_time(source.get("time") or extracted.get("signal_time")) or _parse_time(previous.get("signal_time"))) else None,
        "source_module": str(source.get("module") or extracted.get("module") or "").lower(),
        "source_template": str(source.get("template_id") or extracted.get("template") or ""),
        "source_signal_type": str(source.get("signal_type") or extracted.get("signal_type") or ""),
        "horizon": str(horizon).lower(),
        "due_at": classification.due_at or None,
        "eligibility_status": classification.eligibility_status,
        "eligibility_reason": classification.eligibility_reason,
        "candidate_status": classification.candidate_status,
        "outcome_id": safe_int((outcome or {}).get("id")) or safe_int(previous.get("outcome_id")) or None,
        "is_terminal": int(classification.is_terminal),
        "is_retryable": int(classification.is_retryable),
        "attempt_count": safe_int(previous.get("attempt_count")),
        "last_attempt_at": previous.get("last_attempt_at"),
        "next_retry_at": classification.next_retry_at or None,
        "source_status": str(source.get("status") or previous.get("source_status") or "sent").lower(),
        "last_error_code": classification.last_error_code,
        "last_error_summary": classification.last_error_summary,
    }


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _read_existing_candidates(
    path: Path,
    candidate_keys: Iterable[str],
) -> dict[str, dict[str, Any]]:
    keys = sorted({str(value or "").strip() for value in candidate_keys if str(value or "").strip()})
    if not path.exists() or not keys:
        return {}
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if not _table_exists(conn, "lifecycle_outcome_candidates"):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for offset in range(0, len(keys), 800):
            chunk = keys[offset : offset + 800]
            placeholders = ",".join("?" for _ in chunk)
            for row in conn.execute(
                f"SELECT * FROM lifecycle_outcome_candidates WHERE candidate_key IN ({placeholders})",
                chunk,
            ):
                result[str(row["candidate_key"])] = dict(row)
        return result
    finally:
        conn.close()


def _count_generic_candidates(path: Path, candidate_keys: Iterable[str]) -> int:
    keys = sorted({str(value or "").strip() for value in candidate_keys if str(value or "").strip()})
    if not path.exists() or not keys:
        return 0
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        if not _table_exists(conn, "lifecycle_outcome_candidates"):
            return 0
        total = 0
        for offset in range(0, len(keys), 800):
            chunk = keys[offset : offset + 800]
            placeholders = ",".join("?" for _ in chunk)
            total += safe_int(conn.execute(
                "SELECT COUNT(*) FROM lifecycle_outcome_candidates "
                f"WHERE candidate_key IN ({placeholders}) "
                "AND (eligibility_reason=? OR candidate_status=?)",
                [*chunk, GENERIC_REASON, GENERIC_REASON],
            ).fetchone()[0])
        return total
    finally:
        conn.close()


def _legacy_generic_coverage_rows(
    path: Path,
    *,
    lifecycle_ids: Iterable[int] | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    if not path.exists():
        return 0, []
    selected = sorted({safe_int(value) for value in (lifecycle_ids or ()) if safe_int(value) > 0})
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if not _table_exists(conn, "lifecycle_outcome_coverage"):
            return 0, []
        where = ""
        params: list[Any] = []
        if selected:
            where = f"WHERE lifecycle_id IN ({','.join('?' for _ in selected)})"
            params.extend(selected)
        result: list[dict[str, Any]] = []
        total = 0
        for row in conn.execute(
            f"SELECT lifecycle_id, unlinked_reason, reasons_json FROM lifecycle_outcome_coverage {where}",
            params,
        ):
            try:
                reasons = json.loads(str(row["reasons_json"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                reasons = {}
            reason_counts = reasons.get("reason_counts") if isinstance(reasons, dict) else {}
            generic_count = safe_int((reason_counts or {}).get(GENERIC_REASON))
            if generic_count <= 0 and str(row["unlinked_reason"] or "") == GENERIC_REASON:
                generic_count = 1
            if generic_count > 0:
                total += generic_count
                result.append({
                    "lifecycle_id": safe_int(row["lifecycle_id"]),
                    "unlinked_reason": str(row["unlinked_reason"] or ""),
                    "reasons": reasons if isinstance(reasons, dict) else {},
                    "generic_count": generic_count,
                })
        return total, result
    finally:
        conn.close()


def _candidate_gap_reason_counts(records: list[dict[str, Any]]) -> dict[int, Counter[str]]:
    grouped: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    for row in records:
        lifecycle_id = safe_int(row.get("lifecycle_id"))
        signal_id = safe_int(row.get("signal_id"))
        identity = (
            f"signal:{signal_id}"
            if signal_id > 0
            else f"event:{safe_int(row.get('lifecycle_event_id'))}:{str(row.get('signal_time') or '')}"
        )
        grouped[(lifecycle_id, identity)].append(row)
    priority = {
        "terminal_error": 0, "retry_wait": 1, "terminal_unavailable": 2, "unavailable": 3,
        "terminal_ineligible": 4, "processing": 5, "queued": 6, "ready": 7, "not_due": 8,
    }
    result: dict[int, Counter[str]] = defaultdict(Counter)
    for (lifecycle_id, _identity), rows in sorted(grouped.items()):
        # v1.78.1 considered a signal linked as soon as any horizon row existed.
        # Only wholly unlinked signal candidates replace its legacy gap reason.
        if any(safe_int(row.get("outcome_id")) > 0 for row in rows):
            continue
        selected = min(
            rows,
            key=lambda row: (
                priority.get(str(row.get("candidate_status") or ""), 99),
                OUTCOME_WINDOWS.get(str(row.get("horizon") or ""), 10**9),
            ),
        )
        reason = _gap_reason(selected)
        if reason in {"", GENERIC_REASON, "outcome_success", "outcome_linked"}:
            reason = "backfill_not_attempted"
        result[lifecycle_id][reason] += 1
    return dict(result)


def _migrate_legacy_generic_coverage(
    settings: Settings,
    records: list[dict[str, Any]],
    *,
    lifecycle_ids: Iterable[int],
    dry_run: bool,
) -> dict[str, int]:
    selected_ids = sorted({safe_int(value) for value in lifecycle_ids if safe_int(value) > 0})
    if not selected_ids:
        return {"legacy_generic_before": 0, "legacy_generic_after": 0, "legacy_generic_migrated": 0}
    before, legacy_rows = _legacy_generic_coverage_rows(
        Path(settings.lifecycle_db_path), lifecycle_ids=selected_ids,
    )
    new_counts = _candidate_gap_reason_counts(records)
    updates: list[tuple[str, str, int]] = []
    migrated = 0
    for row in legacy_rows:
        lifecycle_id = safe_int(row.get("lifecycle_id"))
        generic_count = safe_int(row.get("generic_count"))
        replacements = Counter(new_counts.get(lifecycle_id) or {})
        if generic_count <= 0:
            continue
        if not replacements:
            # A coverage row with no reconstructable candidate is itself an
            # actionable legacy-data classification, never another generic.
            replacements["lifecycle_event_without_signal"] = generic_count
        # Preserve the legacy gap total so operators can audit all historical
        # 609 items while replacing each generic bucket deterministically.
        expanded = [reason for reason, count in sorted(replacements.items()) for _ in range(count)]
        fallback = replacements.most_common(1)[0][0] if replacements else "backfill_not_attempted"
        normalized = Counter((expanded[index] if index < len(expanded) else fallback) for index in range(generic_count))
        reasons = dict(row.get("reasons") or {})
        reason_counts = Counter({
            str(key): safe_int(value)
            for key, value in dict(reasons.get("reason_counts") or {}).items()
            if str(key) != GENERIC_REASON and safe_int(value) > 0
        })
        reason_counts.update(normalized)
        reasons["reason_counts"] = dict(sorted(reason_counts.items()))
        reasons["candidate_quality_reason_counts"] = dict(sorted(normalized.items()))
        new_unlinked = str(row.get("unlinked_reason") or "")
        if new_unlinked == GENERIC_REASON:
            new_unlinked = normalized.most_common(1)[0][0]
        updates.append((json.dumps(reasons, ensure_ascii=False, separators=(",", ":")), new_unlinked, lifecycle_id))
        migrated += generic_count
    if updates and not dry_run:
        with IntelligenceStore(settings).transaction() as conn:
            conn.executemany(
                "UPDATE lifecycle_outcome_coverage SET reasons_json=?, unlinked_reason=?, updated_at=? "
                "WHERE lifecycle_id=?",
                [(reasons_json, reason, _iso(), lifecycle_id) for reasons_json, reason, lifecycle_id in updates],
            )
    remaining = max(0, before - migrated)
    return {"legacy_generic_before": before, "legacy_generic_after": remaining, "legacy_generic_migrated": migrated}


def _read_signal_rows(path: Path, signal_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    ids = sorted({safe_int(value) for value in signal_ids if safe_int(value) > 0})
    if not ids or not path.exists():
        return {}
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if not _table_exists(conn, "signals"):
            return {}
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(signals)")}
        allowed = (
            "id", "ts", "time", "module", "template_id", "signal_type", "symbol", "status",
            "stage", "sent",
        )
        projection = ",".join(name for name in allowed if name in columns)
        result: dict[int, dict[str, Any]] = {}
        for offset in range(0, len(ids), 800):
            chunk = ids[offset : offset + 800]
            placeholders = ",".join("?" for _ in chunk)
            for row in conn.execute(f"SELECT {projection} FROM signals WHERE id IN ({placeholders})", chunk):
                result[safe_int(row["id"])] = dict(row)
        return result
    finally:
        conn.close()


def _legacy_lookup_key(candidate: dict[str, Any]) -> str:
    parsed = _parse_time(candidate.get("signal_time"))
    return "{lifecycle}:{event}:{time}:{module}:{template}".format(
        lifecycle=safe_int(candidate.get("lifecycle_id")),
        event=safe_int(candidate.get("lifecycle_event_id")),
        time=parsed.isoformat() if parsed is not None else "",
        module=str(candidate.get("module") or "").strip().casefold(),
        template=str(candidate.get("template") or "").strip().casefold(),
    )


def _read_legacy_signal_matches(
    path: Path,
    candidates: Iterable[dict[str, Any]],
    *,
    tolerance_sec: int,
) -> dict[str, dict[str, Any]]:
    """Resolve legacy candidates with one bounded signals.db read.

    A match always requires normalized symbol, time tolerance and at least one
    exact module/template match.  It never falls back to symbol-only matching.
    """

    legacy = [
        dict(item)
        for item in candidates
        if safe_int(item.get("signal_id")) <= 0
        and normalize_lifecycle_symbol(item.get("symbol"))
        and _parse_time(item.get("signal_time")) is not None
    ]
    if not legacy or not path.exists():
        return {}
    symbols = sorted({normalize_lifecycle_symbol(item.get("symbol")) for item in legacy})
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if not _table_exists(conn, "signals"):
            return {
                _legacy_lookup_key(item): {"match_status": "none", "reason": "signal_not_found"}
                for item in legacy
            }
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(signals)")}
        allowed = (
            "id", "ts", "time", "module", "template_id", "signal_type", "symbol", "status",
            "stage", "sent",
        )
        projection = ",".join(name for name in allowed if name in columns)
        placeholders = ",".join("?" for _ in symbols)
        rows = [
            dict(row)
            for row in conn.execute(
                f"SELECT {projection} FROM signals WHERE UPPER(symbol) IN ({placeholders})",
                symbols,
            )
        ]
    finally:
        conn.close()
    by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        normalized = normalize_lifecycle_symbol(row.get("symbol"))
        if normalized:
            by_symbol[normalized].append(row)
    tolerance = max(0, safe_int(tolerance_sec, 300))
    result: dict[str, dict[str, Any]] = {}
    for candidate in legacy:
        key = _legacy_lookup_key(candidate)
        symbol_rows = by_symbol.get(normalize_lifecycle_symbol(candidate.get("symbol")), [])
        if not symbol_rows:
            result[key] = {"match_status": "none", "reason": "signal_not_found"}
            continue
        candidate_time = _parse_time(candidate.get("signal_time"))
        time_rows = [
            row for row in symbol_rows
            if _parse_time(row.get("time") or row.get("ts")) is not None
            and abs((_parse_time(row.get("time") or row.get("ts")) - candidate_time).total_seconds()) <= tolerance
        ]
        if not time_rows:
            result[key] = {"match_status": "none", "reason": "time_mismatch"}
            continue
        module = str(candidate.get("module") or "").strip().casefold()
        template = str(candidate.get("template") or "").strip().casefold()
        matched = [
            row for row in time_rows
            if (
                bool(module and str(row.get("module") or "").strip().casefold() == module)
                or bool(template and str(row.get("template_id") or "").strip().casefold() == template)
            )
        ]
        unique_ids = {safe_int(row.get("id")) for row in matched if safe_int(row.get("id")) > 0}
        if len(unique_ids) == 1:
            selected_id = next(iter(unique_ids))
            result[key] = {
                "match_status": "unique",
                "signal": next(row for row in matched if safe_int(row.get("id")) == selected_id),
            }
        elif len(unique_ids) > 1:
            result[key] = {"match_status": "ambiguous", "reason": "ambiguous_legacy_match"}
        else:
            result[key] = {"match_status": "none", "reason": "module_mismatch"}
    return result


def _read_outcomes(settings: Settings, signal_ids: Iterable[int]) -> dict[tuple[int, str], dict[str, Any]]:
    ids = sorted({safe_int(value) for value in signal_ids if safe_int(value) > 0})
    if not ids or not Path(settings.outcome_db_path).exists():
        return {}
    store = OutcomeStore(settings.outcome_db_path)
    rows = store.list_by_signal_ids(
        ids,
        columns=("id", "signal_id", "symbol", "signal_time", "horizon", "due_time", "data_status", "error", "updated_at"),
    )
    return {(safe_int(row.get("signal_id")), str(row.get("horizon") or "")): row for row in rows}


def _read_persisted_links(path: Path, lifecycle_ids: Iterable[int]) -> dict[int, list[dict[str, Any]]]:
    ids = sorted({safe_int(value) for value in lifecycle_ids if safe_int(value) > 0})
    if not ids or not path.exists():
        return {}
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    result: dict[int, list[dict[str, Any]]] = defaultdict(list)
    try:
        if not _table_exists(conn, "lifecycle_outcome_links"):
            return {}
        for offset in range(0, len(ids), 800):
            chunk = ids[offset : offset + 800]
            placeholders = ",".join("?" for _ in chunk)
            for row in conn.execute(
                "SELECT lifecycle_id,lifecycle_event_id,signal_id,outcome_id,horizon,outcome_status,"
                "signal_time,link_method FROM lifecycle_outcome_links "
                f"WHERE lifecycle_id IN ({placeholders})",
                chunk,
            ):
                result[safe_int(row["lifecycle_id"])].append(dict(row))
        return dict(result)
    finally:
        conn.close()


def _legacy_candidate_link(
    candidate: dict[str, Any],
    links: list[dict[str, Any]],
    horizon: str,
) -> dict[str, Any] | None:
    selected = [row for row in links if str(row.get("horizon") or "") == horizon]
    event_id = safe_int(candidate.get("lifecycle_event_id"))
    if event_id > 0:
        event_matches = [row for row in selected if safe_int(row.get("lifecycle_event_id")) == event_id]
        return event_matches[0] if len(event_matches) == 1 else None
    signal_time = _parse_time(candidate.get("signal_time"))
    if signal_time is None:
        return None
    time_matches = [
        row for row in selected
        if _parse_time(row.get("signal_time")) == signal_time
    ]
    return time_matches[0] if len(time_matches) == 1 else None


def _collect_candidate_records(
    settings: Settings,
    *,
    symbol: str = "",
    lifecycle_id: int | None = None,
    lifecycle_ids: Iterable[int] | None = None,
    horizon: str = "",
    module: str = "",
    limit: int = 200,
    now: datetime | int | float | None = None,
    _authoritative_keys: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    from .lifecycle_outcomes import _read_lifecycle_sources, extract_lifecycle_signal_candidates

    lifecycles, events = _read_lifecycle_sources(
        settings, symbol=symbol, lifecycle_id=lifecycle_id, lifecycle_ids=lifecycle_ids,
        limit=max(1, min(safe_int(limit, 200), 1000)), rotate=False,
    )
    extracted_by_lifecycle = {
        safe_int(item.get("id")): extract_lifecycle_signal_candidates(item, events.get(safe_int(item.get("id")), []))
        for item in lifecycles
    }
    extracted = [candidate for rows in extracted_by_lifecycle.values() for candidate in rows]
    legacy_matches = _read_legacy_signal_matches(
        Path(settings.signal_events_db_path),
        extracted,
        tolerance_sec=safe_int(
            getattr(settings, "lifecycle_outcome_link_time_tolerance_sec", 300), 300,
        ),
    )
    persisted_links = _read_persisted_links(
        Path(settings.lifecycle_db_path), (safe_int(item.get("id")) for item in lifecycles),
    )
    linked_signal_ids = [
        safe_int(link.get("signal_id"))
        for rows in persisted_links.values()
        for link in rows
    ]
    legacy_signal_ids = [
        safe_int((match.get("signal") or {}).get("id"))
        for match in legacy_matches.values()
        if str(match.get("match_status") or "") == "unique"
    ]
    signal_rows = _read_signal_rows(
        Path(settings.signal_events_db_path),
        [
            *(safe_int(candidate.get("signal_id")) for candidate in extracted),
            *linked_signal_ids,
            *legacy_signal_ids,
        ],
    )
    outcomes = _read_outcomes(
        settings,
        [
            *(safe_int(candidate.get("signal_id")) for candidate in extracted),
            *linked_signal_ids,
            *legacy_signal_ids,
        ],
    )
    selected_horizons = (str(horizon).lower(),) if str(horizon or "").lower() in HORIZONS else HORIZONS
    planned_keys = [
        stable_candidate_key(
            lifecycle_id=lifecycle.get("id"),
            signal_id=candidate.get("signal_id"),
            lifecycle_event_id=candidate.get("lifecycle_event_id"),
            signal_time=candidate.get("signal_time"),
            horizon=current_horizon,
        )
        for lifecycle in lifecycles
        for candidate in extracted_by_lifecycle.get(safe_int(lifecycle.get("id")), [])
        for current_horizon in selected_horizons
    ]
    existing = _read_existing_candidates(Path(settings.lifecycle_db_path), planned_keys)
    normalized_module = str(module or "").strip().lower()
    records: list[dict[str, Any]] = []
    for lifecycle in lifecycles:
        for candidate in extracted_by_lifecycle.get(safe_int(lifecycle.get("id")), []):
            source = signal_rows.get(safe_int(candidate.get("signal_id")))
            candidate_module = str((source or {}).get("module") or candidate.get("module") or "").lower()
            if normalized_module and candidate_module != normalized_module:
                continue
            for current_horizon in selected_horizons:
                effective_candidate = dict(candidate)
                effective_source = dict(source or {})
                outcome = outcomes.get((safe_int(candidate.get("signal_id")), current_horizon))
                if safe_int(candidate.get("signal_id")) <= 0:
                    effective_candidate["legacy_identity"] = True
                    link = _legacy_candidate_link(
                        candidate,
                        persisted_links.get(safe_int(lifecycle.get("id")), []),
                        current_horizon,
                    )
                    if link is not None:
                        linked_signal_id = safe_int(link.get("signal_id"))
                        effective_source.update(signal_rows.get(linked_signal_id) or {})
                        effective_source["legacy_match_unique"] = True
                        outcome = outcomes.get((linked_signal_id, current_horizon)) or {
                            "id": safe_int(link.get("outcome_id")),
                            "signal_id": linked_signal_id or None,
                            "horizon": current_horizon,
                            "data_status": str(link.get("outcome_status") or "pending"),
                            "signal_time": link.get("signal_time"),
                        }
                    else:
                        match = legacy_matches.get(_legacy_lookup_key(candidate)) or {}
                        match_status = str(match.get("match_status") or "")
                        if match_status == "unique":
                            matched_source = dict(match.get("signal") or {})
                            matched_signal_id = safe_int(matched_source.get("id"))
                            effective_source.update(matched_source)
                            effective_source["legacy_match_unique"] = True
                            outcome = outcomes.get((matched_signal_id, current_horizon))
                        elif match_status == "ambiguous":
                            effective_source["legacy_ambiguous"] = True
                        else:
                            effective_source["legacy_match_reason"] = str(
                                match.get("reason") or "signal_not_found"
                            )
                key = stable_candidate_key(
                    lifecycle_id=lifecycle.get("id"), signal_id=candidate.get("signal_id"),
                    lifecycle_event_id=candidate.get("lifecycle_event_id"),
                    signal_time=(source or {}).get("time") or candidate.get("signal_time"),
                    horizon=current_horizon,
                )
                current_record = existing.get(key)
                if key in (_authoritative_keys or set()) and current_record:
                    current_record = dict(current_record)
                    if str(current_record.get("candidate_status") or "") == "processing":
                        current_record["candidate_status"] = ""
                records.append(build_candidate_record(
                    lifecycle, effective_candidate, effective_source or None, current_horizon,
                    outcome=outcome,
                    current=current_record, now=now, settings=settings,
                ))
    return lifecycles, records


def refresh_outcome_candidates(
    settings: Settings | None = None,
    *,
    symbol: str = "",
    lifecycle_id: int | None = None,
    horizon: str = "",
    module: str = "",
    limit: int = 200,
    dry_run: bool = False,
    force: bool = False,
    now: datetime | int | float | None = None,
    _lifecycle_ids: Iterable[int] | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    loaded = settings or Settings.load()
    normalized_horizon = str(horizon or "").lower()
    if normalized_horizon and normalized_horizon not in HORIZONS:
        return {"ok": False, "error": "invalid_horizon", "processed": 0, "failed": 1, "dry_run": bool(dry_run)}
    if symbol and not normalize_lifecycle_symbol(symbol):
        return {"ok": False, "error": "invalid_symbol", "processed": 0, "failed": 1, "dry_run": bool(dry_run)}
    if lifecycle_id is not None and safe_int(lifecycle_id) <= 0:
        return {"ok": False, "error": "invalid_lifecycle_id", "processed": 0, "failed": 1, "dry_run": bool(dry_run)}
    lifecycles, records = _collect_candidate_records(
        loaded, symbol=symbol, lifecycle_id=lifecycle_id, lifecycle_ids=_lifecycle_ids,
        horizon=normalized_horizon, module=module, limit=limit, now=now,
    )
    selected_keys = {str(item.get("candidate_key") or "") for item in records}
    generic_before = _count_generic_candidates(Path(loaded.lifecycle_db_path), selected_keys)
    write_result = {"processed": len(records), "inserted": 0, "updated": 0}
    if not dry_run and records:
        write_result = IntelligenceStore(loaded).upsert_outcome_candidates(records, preserve_progress=not force)
    counts = Counter(str(item.get("eligibility_status") or "unknown") for item in records)
    statuses = Counter(str(item.get("candidate_status") or "") for item in records)
    generic_after = (
        _count_generic_candidates(Path(loaded.lifecycle_db_path), selected_keys)
        if not dry_run
        else sum(
            str(item.get("eligibility_reason") or "") == GENERIC_REASON
            or str(item.get("candidate_status") or "") == GENERIC_REASON
            for item in records
        )
    )
    return {
        "ok": True,
        "dry_run": bool(dry_run),
        "processed": len(records),
        "classified": len(records),
        "lifecycles": len(lifecycles),
        "inserted": safe_int(write_result.get("inserted")),
        "updated": safe_int(write_result.get("updated")),
        "eligible": counts.get("eligible", 0),
        "ineligible": counts.get("ineligible", 0),
        "unknown": counts.get("unknown", 0),
        "statuses": dict(sorted(statuses.items())),
        "generic_no_outcome_row_before": generic_before,
        "generic_no_outcome_row_after": generic_after,
        "failed": 0,
        "duration_sec": round(time.perf_counter() - started, 4),
    }


def classify_outcome_gaps(settings: Settings | None = None, **kwargs: Any) -> dict[str, Any]:
    result = refresh_outcome_candidates(settings, **kwargs)
    if result.get("ok") is False:
        return result
    loaded = settings or Settings.load()
    dry_run = bool(kwargs.get("dry_run"))
    migration_lifecycles, migration_records = _collect_candidate_records(
        loaded,
        symbol=str(kwargs.get("symbol") or ""), lifecycle_id=kwargs.get("lifecycle_id"),
        horizon="", module="",
        limit=safe_int(kwargs.get("limit"), 200),
    )
    migration = _migrate_legacy_generic_coverage(
        loaded,
        migration_records,
        lifecycle_ids=(safe_int(item.get("id")) for item in migration_lifecycles),
        dry_run=dry_run,
    )
    if dry_run:
        _, scoped_records = _collect_candidate_records(
            loaded,
            symbol=str(kwargs.get("symbol") or ""), lifecycle_id=kwargs.get("lifecycle_id"),
            horizon=str(kwargs.get("horizon") or ""), module=str(kwargs.get("module") or ""),
            limit=safe_int(kwargs.get("limit"), 200),
        )
    else:
        scoped_records = None
    quality = lifecycle_outcome_quality(loaded, write_reports=False, _records=scoped_records)
    summary = quality.get("summary") or {}
    statuses = quality.get("status_counts") or {}
    reasons = quality.get("reasons") or {}
    result.update({
        "generic_no_outcome_row_before": (
            safe_int(result.get("generic_no_outcome_row_before"))
            + safe_int(migration.get("legacy_generic_before"))
        ),
        "generic_no_outcome_row_after": (
            safe_int(result.get("generic_no_outcome_row_after"))
            + safe_int(migration.get("legacy_generic_after"))
        ),
        "legacy_generic_migrated": safe_int(migration.get("legacy_generic_migrated")),
        "eligible_due": safe_int(summary.get("due_candidate_count")),
        "eligible_not_due": safe_int(statuses.get("not_due")),
        "ineligible": safe_int(summary.get("ineligible_candidate_count")),
        "retryable": safe_int(statuses.get("retry_wait")),
        "terminal_unavailable": safe_int(statuses.get("terminal_unavailable")),
        "ambiguous": safe_int(reasons.get("ambiguous_legacy_match")),
        "real_error": safe_int(summary.get("real_error_count")),
    })
    return result


def _candidate_rows_with_levels(path: Path) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if not path.exists():
        return [], {"lifecycle_count": 0, "linked_lifecycle_count": 0}
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if not _table_exists(conn, "lifecycle_outcome_candidates"):
            lifecycle_count = conn.execute("SELECT COUNT(*) FROM signal_lifecycles").fetchone()[0] if _table_exists(conn, "signal_lifecycles") else 0
            return [], {"lifecycle_count": safe_int(lifecycle_count), "linked_lifecycle_count": 0}
        join = "LEFT JOIN signal_lifecycles l ON l.id=c.lifecycle_id" if _table_exists(conn, "signal_lifecycles") else ""
        level = "COALESCE(l.first_signal_level,'unknown') AS first_signal_level" if join else "'unknown' AS first_signal_level"
        rows = [dict(row) for row in conn.execute(f"SELECT c.*, {level} FROM lifecycle_outcome_candidates c {join}")]
        lifecycle_count = conn.execute("SELECT COUNT(*) FROM signal_lifecycles").fetchone()[0] if _table_exists(conn, "signal_lifecycles") else len({row["lifecycle_id"] for row in rows})
        linked_lifecycle_count = conn.execute("SELECT COUNT(DISTINCT lifecycle_id) FROM lifecycle_outcome_links").fetchone()[0] if _table_exists(conn, "lifecycle_outcome_links") else 0
        duplicate_links = conn.execute(
            "SELECT COUNT(*) FROM (SELECT lifecycle_id,outcome_id FROM lifecycle_outcome_links GROUP BY lifecycle_id,outcome_id HAVING COUNT(*)>1)"
        ).fetchone()[0] if _table_exists(conn, "lifecycle_outcome_links") else 0
        multiple_primary = conn.execute(
            "SELECT COUNT(*) FROM (SELECT lifecycle_id FROM lifecycle_outcome_links WHERE is_primary=1 GROUP BY lifecycle_id HAVING COUNT(*)>1)"
        ).fetchone()[0] if _table_exists(conn, "lifecycle_outcome_links") else 0
        orphan_links = conn.execute(
            "SELECT COUNT(*) FROM lifecycle_outcome_links x LEFT JOIN signal_lifecycles l ON l.id=x.lifecycle_id WHERE l.id IS NULL"
        ).fetchone()[0] if _table_exists(conn, "lifecycle_outcome_links") and _table_exists(conn, "signal_lifecycles") else 0
        return rows, {
            "lifecycle_count": safe_int(lifecycle_count),
            "linked_lifecycle_count": safe_int(linked_lifecycle_count),
            "duplicate_links": safe_int(duplicate_links),
            "multiple_primary": safe_int(multiple_primary),
            "orphan_links": safe_int(orphan_links),
        }
    finally:
        conn.close()


def _link_consistency(lifecycle_path: Path, outcome_path: Path) -> dict[str, int]:
    result = {
        "duplicate_candidates": 0,
        "duplicate_links": 0,
        "multiple_primary": 0,
        "orphan_links": 0,
        "processing_candidates": 0,
    }
    if not lifecycle_path.exists():
        return result
    conn = sqlite3.connect(f"file:{lifecycle_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        if _table_exists(conn, "lifecycle_outcome_candidates"):
            result["duplicate_candidates"] = safe_int(conn.execute(
                "SELECT COUNT(*) FROM (SELECT candidate_key FROM lifecycle_outcome_candidates "
                "GROUP BY candidate_key HAVING COUNT(*)>1)"
            ).fetchone()[0])
            result["processing_candidates"] = safe_int(conn.execute(
                "SELECT COUNT(*) FROM lifecycle_outcome_candidates WHERE candidate_status='processing'"
            ).fetchone()[0])
        if not _table_exists(conn, "lifecycle_outcome_links"):
            return result
        result["duplicate_links"] = safe_int(conn.execute(
            "SELECT COUNT(*) FROM (SELECT lifecycle_id,outcome_id FROM lifecycle_outcome_links "
            "GROUP BY lifecycle_id,outcome_id HAVING COUNT(*)>1)"
        ).fetchone()[0])
        result["multiple_primary"] = safe_int(conn.execute(
            "SELECT COUNT(*) FROM (SELECT lifecycle_id FROM lifecycle_outcome_links WHERE is_primary=1 "
            "GROUP BY lifecycle_id HAVING COUNT(*)>1)"
        ).fetchone()[0])
        lifecycle_ids = {
            safe_int(row[0]) for row in conn.execute("SELECT id FROM signal_lifecycles")
        } if _table_exists(conn, "signal_lifecycles") else set()
        links = [
            (safe_int(row[0]), safe_int(row[1]))
            for row in conn.execute("SELECT lifecycle_id,outcome_id FROM lifecycle_outcome_links")
        ]
    finally:
        conn.close()
    outcome_ids = {outcome_id for _lifecycle_id, outcome_id in links if outcome_id > 0}
    existing_outcomes: set[int] = set()
    if outcome_ids and outcome_path.exists():
        outcome_conn = sqlite3.connect(f"file:{outcome_path.as_posix()}?mode=ro", uri=True)
        try:
            if _table_exists(outcome_conn, "signal_outcomes"):
                ordered = sorted(outcome_ids)
                for offset in range(0, len(ordered), 800):
                    chunk = ordered[offset : offset + 800]
                    placeholders = ",".join("?" for _ in chunk)
                    existing_outcomes.update(
                        safe_int(row[0])
                        for row in outcome_conn.execute(
                            f"SELECT id FROM signal_outcomes WHERE id IN ({placeholders})", chunk,
                        )
                    )
        finally:
            outcome_conn.close()
    result["orphan_links"] = sum(
        lifecycle_id not in lifecycle_ids or outcome_id not in existing_outcomes
        for lifecycle_id, outcome_id in links
    )
    return result


def _is_due(row: dict[str, Any], now: datetime) -> bool:
    if str(row.get("eligibility_status")) != "eligible":
        return False
    due = _parse_time(row.get("due_at"))
    return bool(due is not None and due <= now)


def _gap_reason(row: dict[str, Any]) -> str:
    reason = str(row.get("eligibility_reason") or "")
    if reason:
        return reason
    return str(row.get("candidate_status") or "unknown")


def _summary_for_rows(rows: list[dict[str, Any]], now: datetime, lifecycle_count: int, linked_lifecycle_count: int) -> dict[str, Any]:
    eligible = [row for row in rows if str(row.get("eligibility_status")) == "eligible"]
    ineligible = [row for row in rows if str(row.get("eligibility_status")) == "ineligible"]
    linked = [row for row in eligible if safe_int(row.get("outcome_id")) > 0]
    due = [row for row in eligible if _is_due(row, now)]
    resolved = [row for row in due if str(row.get("candidate_status")) in RESOLVED_STATUSES]
    success = [row for row in due if str(row.get("candidate_status")) == "success"]
    due_lifecycles = {safe_int(row.get("lifecycle_id")) for row in due}
    mature_lifecycles = {safe_int(row.get("lifecycle_id")) for row in success}
    real_errors = [row for row in rows if str(row.get("candidate_status")) == "terminal_error"]
    generic = [row for row in rows if _gap_reason(row) == GENERIC_REASON or str(row.get("candidate_status")) == GENERIC_REASON]
    retry_times = sorted(
        parsed
        for row in rows
        if str(row.get("candidate_status")) == "retry_wait"
        for parsed in [_parse_time(row.get("next_retry_at"))]
        if parsed is not None
    )
    return {
        "lifecycle_count": lifecycle_count,
        "linked_lifecycle_count": linked_lifecycle_count,
        "lifecycle_link_coverage_ratio": _ratio(linked_lifecycle_count, lifecycle_count),
        "candidate_count": len(rows),
        "outcome_candidate_count": len(rows),
        "eligible_candidate_count": len(eligible),
        "eligible": len(eligible),
        "ineligible_candidate_count": len(ineligible),
        "ineligible": len(ineligible),
        "unknown_candidate_count": len(rows) - len(eligible) - len(ineligible),
        "linked_candidate_count": len(linked),
        "linked": len(linked),
        "candidate_link_coverage_ratio": _ratio(len(linked), len(eligible)),
        "due_candidate_count": len(due),
        "resolved_due_candidate_count": len(resolved),
        "due_resolution_ratio": _ratio(len(resolved), len(due)),
        "successful_due_candidate_count": len(success),
        "usable_outcome_maturity_ratio": _ratio(len(success), len(due)),
        "due_lifecycle_count": len(due_lifecycles),
        "mature_lifecycle_count": len(mature_lifecycles),
        "lifecycle_maturity_ratio": _ratio(len(mature_lifecycles), len(due_lifecycles)),
        "generic_unclassified_count": len(generic),
        "generic_no_outcome_row": len(generic),
        "real_error_count": len(real_errors),
        "real_error_ratio": _ratio(len(real_errors), len(due)),
        "not_due": sum(str(row.get("candidate_status")) == "not_due" for row in rows),
        "ready": sum(str(row.get("candidate_status")) in {"ready", "queued"} for row in rows),
        "queued": sum(str(row.get("candidate_status")) == "queued" for row in rows),
        "processing": sum(str(row.get("candidate_status")) == "processing" for row in rows),
        "success": sum(str(row.get("candidate_status")) == "success" for row in rows),
        "unavailable": sum(str(row.get("candidate_status")) in UNAVAILABLE_STATUSES for row in rows),
        "terminal_unavailable": sum(str(row.get("candidate_status")) == "terminal_unavailable" for row in rows),
        "retry_wait": sum(str(row.get("candidate_status")) == "retry_wait" for row in rows),
        "retryable_count": sum(bool(row.get("is_retryable")) for row in rows),
        "terminal_error": len(real_errors),
        "next_retry_at": retry_times[0].isoformat() if retry_times else None,
    }


def _group_quality(rows: list[dict[str, Any]], key_name: str, now: datetime) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get(key_name) or "unknown")].append(row)
    result: list[dict[str, Any]] = []
    output_key = {
        "source_module": "module",
        "source_signal_type": "signal_type",
    }.get(key_name, key_name)
    for key, group in sorted(grouped.items()):
        summary = _summary_for_rows(group, now, len({safe_int(row.get("lifecycle_id")) for row in group}), len({safe_int(row.get("lifecycle_id")) for row in group if safe_int(row.get("outcome_id")) > 0}))
        gaps = Counter(_gap_reason(row) for row in group if str(row.get("candidate_status")) not in {"success", "linked"})
        result.append({
            "key": key,
            output_key: key,
            "candidate_count": summary["candidate_count"],
            "eligible_count": summary["eligible_candidate_count"],
            "ineligible_count": summary["ineligible_candidate_count"],
            "linked_count": summary["linked_candidate_count"],
            "success_count": summary["successful_due_candidate_count"],
            "unavailable_count": sum(str(row.get("candidate_status")) in UNAVAILABLE_STATUSES for row in group),
            "error_count": sum(str(row.get("candidate_status")) in ERROR_STATUSES for row in group),
            "link_coverage_ratio": summary["candidate_link_coverage_ratio"],
            "maturity_ratio": summary["usable_outcome_maturity_ratio"],
            "resolution_ratio": summary["due_resolution_ratio"],
            "top_gap_reasons": [{"reason": name, "count": count} for name, count in gaps.most_common(3)],
        })
    return result


def calculate_quality_metrics(
    rows: list[dict[str, Any]],
    *,
    lifecycle_count: int | None = None,
    linked_lifecycle_count: int | None = None,
    now: datetime | int | float | None = None,
    consistency: dict[str, int] | None = None,
) -> dict[str, Any]:
    current = _utc_now(now)
    total_lifecycles = safe_int(lifecycle_count, len({safe_int(row.get("lifecycle_id")) for row in rows}))
    linked_lifecycles = safe_int(linked_lifecycle_count, len({safe_int(row.get("lifecycle_id")) for row in rows if safe_int(row.get("outcome_id")) > 0}))
    summary = _summary_for_rows(rows, current, total_lifecycles, linked_lifecycles)
    statuses = Counter(str(row.get("candidate_status") or "unknown") for row in rows)
    reasons = Counter(
        _gap_reason(row)
        for row in rows
        if str(row.get("candidate_status")) not in {"success", "linked"}
        and _gap_reason(row) not in {"outcome_success", "outcome_linked"}
    )
    horizons = _group_quality(rows, "horizon", current)
    for item in horizons:
        group = [row for row in rows if str(row.get("horizon") or "") == item["horizon"]]
        item.update({
            "not_due": sum(str(row.get("candidate_status")) == "not_due" for row in group),
            "ready": sum(str(row.get("candidate_status")) in {"ready", "queued"} for row in group),
            "processing": sum(str(row.get("candidate_status")) == "processing" for row in group),
            "success": sum(str(row.get("candidate_status")) == "success" for row in group),
            "unavailable": sum(str(row.get("candidate_status")) in UNAVAILABLE_STATUSES for row in group),
            "retry_wait": sum(str(row.get("candidate_status")) == "retry_wait" for row in group),
            "error": sum(str(row.get("candidate_status")) == "terminal_error" for row in group),
        })
    timeline: list[dict[str, Any]] = []
    for label, seconds in (("24h", 86400), ("7d", 604800), ("30d", 2592000), ("all", None)):
        scoped = rows if seconds is None else [
            row for row in rows
            if (_parse_time(row.get("signal_time")) is not None and (current - _parse_time(row.get("signal_time"))).total_seconds() <= seconds)
        ]
        timeline.append({"time_range": label, **_summary_for_rows(scoped, current, len({safe_int(row.get("lifecycle_id")) for row in scoped}), len({safe_int(row.get("lifecycle_id")) for row in scoped if safe_int(row.get("outcome_id")) > 0}))})
    return {
        "ok": True,
        "generated_at": current.isoformat(),
        "summary": summary,
        "status_counts": dict(sorted(statuses.items())),
        "reasons": dict(sorted(reasons.items())),
        "modules": _group_quality(rows, "source_module", current),
        "levels": _group_quality(rows, "first_signal_level", current),
        "signal_types": _group_quality(rows, "source_signal_type", current),
        "horizons": horizons,
        "timeline": timeline,
        "consistency": {"duplicate_links": 0, "multiple_primary": 0, "orphan_links": 0, **(consistency or {})},
    }


def lifecycle_outcome_quality(
    settings: Settings | None = None,
    *,
    symbol: str = "",
    lifecycle_id: int | None = None,
    horizon: str = "",
    module: str = "",
    time_range: str = "all",
    write_reports: bool = False,
    _records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    loaded = settings or Settings.load()
    rows, meta = _candidate_rows_with_levels(Path(loaded.lifecycle_db_path)) if _records is None else (_records, {})
    normalized_symbol = normalize_lifecycle_symbol(symbol)
    current = _utc_now()
    if symbol and not normalized_symbol:
        return {"ok": False, "error": "invalid_symbol"}
    if normalized_symbol:
        rows = [row for row in rows if normalize_lifecycle_symbol(row.get("symbol")) == normalized_symbol]
    if lifecycle_id is not None:
        rows = [row for row in rows if safe_int(row.get("lifecycle_id")) == safe_int(lifecycle_id)]
    if horizon:
        rows = [row for row in rows if str(row.get("horizon")) == str(horizon).lower()]
    if module:
        rows = [row for row in rows if str(row.get("source_module") or "").lower() == str(module).lower()]
    ranges = {"24h": 86400, "7d": 604800, "30d": 2592000}
    if time_range in ranges:
        rows = [row for row in rows if _parse_time(row.get("signal_time")) and (current - _parse_time(row.get("signal_time"))).total_seconds() <= ranges[time_range]]
    scoped = bool(normalized_symbol or lifecycle_id is not None or horizon or module or time_range != "all")
    consistency = _link_consistency(Path(loaded.lifecycle_db_path), Path(loaded.outcome_db_path))
    report = calculate_quality_metrics(
        rows,
        lifecycle_count=len({safe_int(row.get("lifecycle_id")) for row in rows}) if scoped else safe_int(meta.get("lifecycle_count"), len({safe_int(row.get("lifecycle_id")) for row in rows})),
        linked_lifecycle_count=len({safe_int(row.get("lifecycle_id")) for row in rows if safe_int(row.get("outcome_id")) > 0}) if scoped else safe_int(meta.get("linked_lifecycle_count")),
        now=current,
        consistency=consistency,
    )
    if write_reports:
        report["report"] = write_lifecycle_outcome_quality_report(report)
    return report


def evaluate_calibration_readiness(
    quality: dict[str, Any],
    *,
    settings: Settings | None = None,
) -> dict[str, Any]:
    loaded = settings or Settings.load()
    summary = dict(quality.get("summary") or {})
    horizons = {str(item.get("horizon")): item for item in list(quality.get("horizons") or [])}
    consistency = dict(quality.get("consistency") or {})
    current = {
        "success_24h": safe_int((horizons.get("24h") or {}).get("success")),
        "success_72h": safe_int((horizons.get("72h") or {}).get("success")),
        "due_resolution_ratio": float(summary.get("due_resolution_ratio") or 0),
        "lifecycle_maturity_ratio": float(summary.get("lifecycle_maturity_ratio") or 0),
        "real_error_ratio": float(summary.get("real_error_ratio") or 0),
        "duplicate_links": safe_int(consistency.get("duplicate_links")),
        "multiple_primary": safe_int(consistency.get("multiple_primary")),
        "orphan_links": safe_int(consistency.get("orphan_links")),
        "generic_no_outcome_row": safe_int(summary.get("generic_unclassified_count")),
    }
    required = {
        "success_24h": safe_int(getattr(loaded, "lifecycle_calibration_min_24h_success", 50), 50),
        "success_72h": safe_int(getattr(loaded, "lifecycle_calibration_min_72h_success", 30), 30),
        "due_resolution_ratio": float(getattr(loaded, "lifecycle_calibration_min_due_resolution_ratio", 0.90)),
        "lifecycle_maturity_ratio": float(getattr(loaded, "lifecycle_calibration_min_lifecycle_maturity_ratio", 0.60)),
        "max_error_ratio": float(getattr(loaded, "lifecycle_calibration_max_error_ratio", 0.01)),
        "duplicate_links": 0,
        "multiple_primary": 0,
        "orphan_links": 0,
        "generic_no_outcome_row": 0,
    }
    checks = {
        "24h_success": current["success_24h"] >= required["success_24h"],
        "72h_success": current["success_72h"] >= required["success_72h"],
        "due_resolution_ratio": current["due_resolution_ratio"] >= required["due_resolution_ratio"],
        "lifecycle_maturity_ratio": current["lifecycle_maturity_ratio"] >= required["lifecycle_maturity_ratio"],
        "real_error_ratio": current["real_error_ratio"] <= required["max_error_ratio"],
        "duplicate_links": current["duplicate_links"] == 0,
        "multiple_primary": current["multiple_primary"] == 0,
        "orphan_links": current["orphan_links"] == 0,
        "generic_no_outcome_row": current["generic_no_outcome_row"] == 0,
    }
    passed = [key for key, value in checks.items() if value]
    blocked = [key for key, value in checks.items() if not value]
    return {
        "ok": True,
        "ready": not blocked,
        "label": "已达到模型校准条件" if not blocked else "暂未达到模型校准条件",
        "passed": passed,
        "blocked": blocked,
        "warnings": [],
        "current": current,
        "required": required,
        "note": "此处仅判断数据是否足够，不会自动修改模型。",
        "calculated_at": _iso(),
    }


def lifecycle_calibration_readiness(
    settings: Settings | None = None,
    *,
    write_reports: bool = False,
) -> dict[str, Any]:
    loaded = settings or Settings.load()
    result = evaluate_calibration_readiness(lifecycle_outcome_quality(loaded, write_reports=False), settings=loaded)
    if write_reports:
        result["report"] = write_lifecycle_calibration_readiness_report(result)
    return result


def incremental_outcome_backfill(
    settings: Settings | None = None,
    *,
    symbol: str = "",
    lifecycle_id: int | None = None,
    horizon: str = "",
    module: str = "",
    limit: int = 200,
    dry_run: bool = False,
    force: bool = False,
    now: datetime | int | float | None = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    loaded = settings or Settings.load()
    current = _utc_now(now)
    refreshed = refresh_outcome_candidates(
        loaded, symbol=symbol, lifecycle_id=lifecycle_id, horizon=horizon, module=module,
        limit=max(limit, 200), dry_run=dry_run, force=force, now=current,
    )
    if not refreshed.get("ok"):
        return refreshed
    max_items = max(
        1,
        min(
            safe_int(limit, 200),
            safe_int(getattr(loaded, "lifecycle_outcome_incremental_max_items", 1000), 1000),
        ),
    )
    if dry_run:
        _, records = _collect_candidate_records(
            loaded, symbol=symbol, lifecycle_id=lifecycle_id, horizon=horizon, module=module,
            limit=max(limit, 200), now=current,
        )
    else:
        store = IntelligenceStore(loaded)
        stale_before = current - timedelta(seconds=safe_int(getattr(loaded, "lifecycle_outcome_processing_stale_sec", 1800), 1800))
        store.recover_stale_outcome_candidates(stale_before)
        actionable_statuses = ["ready", "queued", "linked", "retry_wait"]
        if force:
            actionable_statuses.extend(["unavailable", "terminal_unavailable", "terminal_error"])
        records = store.list_outcome_candidates(
            lifecycle_id=lifecycle_id, symbol=symbol, horizon=horizon,
            eligibility_status="eligible", candidate_statuses=actionable_statuses,
            module=module, due_before=current, retry_due_before=current,
            exclude_eligibility_reasons=["signal_not_found"], limit=max_items,
        )
    eligible = [
        row for row in records
        if str(row.get("eligibility_status")) == "eligible"
        and _parse_time(row.get("due_at")) is not None
        and _parse_time(row.get("due_at")) <= current
        and (
            str(row.get("candidate_status")) in {"ready", "queued", "linked"}
            or (
                str(row.get("candidate_status")) == "retry_wait"
                and (_parse_time(row.get("next_retry_at")) is None or _parse_time(row.get("next_retry_at")) <= current)
            )
            or (
                force
                and str(row.get("candidate_status")) in {
                    "unavailable", "terminal_error", "terminal_unavailable",
                }
            )
        )
        and str(row.get("eligibility_reason") or "") != "signal_not_found"
    ]
    max_symbols = max(1, safe_int(getattr(loaded, "lifecycle_outcome_incremental_max_symbols", 100), 100))
    selected: list[dict[str, Any]] = []
    symbols: set[str] = set()
    for row in eligible:
        current_symbol = str(row.get("symbol") or "")
        if current_symbol not in symbols and len(symbols) >= max_symbols:
            continue
        symbols.add(current_symbol)
        selected.append(row)
        if len(selected) >= max_items:
            break
    if dry_run:
        return {
            "ok": True, "dry_run": True, "processed": 0, "planned": len(selected),
            "linked": 0, "backfilled": 0, "retry": 0, "terminal": 0, "failed": 0,
            "duration_sec": round(time.perf_counter() - started, 4),
        }
    store = IntelligenceStore(loaded)
    if force:
        forced_rows: list[dict[str, Any]] = []
        for row in selected:
            if str(row.get("candidate_status") or "") not in {
                "unavailable", "terminal_error", "terminal_unavailable",
            }:
                continue
            forced = dict(row)
            forced.update({
                "candidate_status": "ready", "is_terminal": 0, "is_retryable": 0,
                "next_retry_at": None,
            })
            forced_rows.append(forced)
        if forced_rows:
            store.upsert_outcome_candidates(forced_rows, preserve_progress=False)
    claimed_keys = store.claim_outcome_candidates(
        [str(row.get("candidate_key")) for row in selected], now=current, return_keys=True,
    )
    claimed_key_set = set(claimed_keys if isinstance(claimed_keys, list) else [])
    selected = [row for row in selected if str(row.get("candidate_key") or "") in claimed_key_set]
    expected_attempts = {
        str(row.get("candidate_key") or ""): safe_int(row.get("attempt_count")) + 1
        for row in selected
    }
    claimed = len(selected)
    signal_rows = _read_signal_rows(Path(loaded.signal_events_db_path), (safe_int(row.get("signal_id")) for row in selected))
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    retry_horizons: set[str] = set()
    for row in selected:
        source = signal_rows.get(safe_int(row.get("signal_id")))
        if source:
            grouped[str(row.get("horizon"))].append(source)
            if str(row.get("candidate_status")) == "retry_wait":
                retry_horizons.add(str(row.get("horizon")))
    scan_results: list[dict[str, Any]] = []
    failed_horizons: dict[str, str] = {}
    for current_horizon, signals in grouped.items():
        try:
            scan_result = scan_signal_outcomes(
                signals, settings=loaded, limit=len(signals), horizon=current_horizon,
                dry_run=False, now_ts=int(current.timestamp()),
                force_rebuild=force or current_horizon in retry_horizons,
            )
            scan_results.append(scan_result)
            if scan_result.get("ok") is False:
                failed_horizons[current_horizon] = str(scan_result.get("error") or "outcome scan failed")
        except Exception as exc:
            error_text = f"{type(exc).__name__}: {exc}"[:240]
            scan_results.append({"ok": False, "error": error_text})
            failed_horizons[current_horizon] = error_text
    lifecycle_ids = sorted({safe_int(row.get("lifecycle_id")) for row in selected})
    selected_keys = {str(row.get("candidate_key") or "") for row in selected}
    from .lifecycle_outcomes import link_lifecycle_outcomes

    link_result = link_lifecycle_outcomes(
        loaded, limit=max(1, len(lifecycle_ids)), force_relink=force, now=current,
        _lifecycle_ids=lifecycle_ids, _write_report=False,
    ) if lifecycle_ids else {"ok": True, "linked": 0}
    # Reclassify authoritatively so no processing marker survives task completion.
    _, final_records = _collect_candidate_records(
        loaded, lifecycle_ids=lifecycle_ids, horizon=horizon, module=module,
        limit=max(1, len(lifecycle_ids)), now=current,
        _authoritative_keys=selected_keys,
    ) if lifecycle_ids else ([], [])
    owned_records = [
        record
        for record in final_records
        if str(record.get("candidate_key") or "") in selected_keys
        and safe_int(record.get("attempt_count")) == expected_attempts.get(str(record.get("candidate_key") or ""), -1)
    ]
    for record in owned_records:
        failed_error = failed_horizons.get(str(record.get("horizon") or ""))
        record_key = str(record.get("candidate_key") or "")
        if record_key in selected_keys and str(record.get("candidate_status") or "") == "processing" and not failed_error:
            record.update({
                "eligibility_reason": "outcome_row_missing",
                "candidate_status": "ready",
                "is_terminal": 0,
                "is_retryable": 0,
                "next_retry_at": None,
                "last_error_code": "",
                "last_error_summary": "",
            })
        if not failed_error or record_key not in selected_keys:
            continue
        failure = classify_provider_failure(
            failed_error,
            attempt_count=max(1, safe_int(record.get("attempt_count"))),
            now=current,
            retry_max_attempts=safe_int(getattr(loaded, "lifecycle_outcome_retry_max_attempts", 5), 5),
            retry_base_sec=safe_int(getattr(loaded, "lifecycle_outcome_retry_base_sec", 900), 900),
            retry_max_sec=safe_int(getattr(loaded, "lifecycle_outcome_retry_max_sec", 21600), 21600),
        )
        record.update({
            "eligibility_status": failure.eligibility_status,
            "eligibility_reason": failure.eligibility_reason,
            "candidate_status": failure.candidate_status,
            "is_terminal": int(failure.is_terminal),
            "is_retryable": int(failure.is_retryable),
            "next_retry_at": failure.next_retry_at or None,
            "last_error_code": failure.last_error_code,
            "last_error_summary": failure.last_error_summary,
        })
    if owned_records:
        store.upsert_outcome_candidates(owned_records, preserve_progress=False)
    if lifecycle_ids and final_records:
        _migrate_legacy_generic_coverage(
            loaded,
            final_records,
            lifecycle_ids=lifecycle_ids,
            dry_run=False,
        )
    changed_outcomes = sum(
        safe_int((result.get("counts") or result).get(status))
        for result in scan_results
        for status in ("success", "unavailable", "error")
        if isinstance(result, dict)
    )
    refresh: dict[str, Any] = {}
    if changed_outcomes > 0 and lifecycle_ids:
        try:
            from .lifecycle_replay import rebuild_replays

            refresh["replay"] = rebuild_replays(
                loaded,
                lifecycle_ids=lifecycle_ids,
                limit=min(max(1, len(lifecycle_ids)), 500),
                force=True,
            )
        except Exception as exc:
            refresh["replay"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:240]}
        try:
            from .lifecycle_intelligence import generate_intelligence

            refresh["intelligence"] = generate_intelligence(
                settings=loaded,
                lifecycle_ids=lifecycle_ids,
                all_active=False,
                limit=min(max(1, len(lifecycle_ids)), 500),
                force=True,
            )
        except Exception as exc:
            refresh["intelligence"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:240]}
        try:
            from .lifecycle_analytics import generate_lifecycle_analytics

            refresh["analytics"] = generate_lifecycle_analytics(settings=loaded, force=True)
        except Exception as exc:
            refresh["analytics"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:240]}
    refresh_failed = sum(
        1
        for value in refresh.values()
        if not isinstance(value, dict)
        or value.get("ok") is False
        or safe_int(value.get("failed")) > 0
    )
    final_quality = lifecycle_outcome_quality(loaded, write_reports=True)
    final_selected = owned_records
    statuses = Counter(str(row.get("candidate_status")) for row in final_selected)
    failed = sum(1 for result in scan_results if result.get("ok") is False)
    total_failed = failed + refresh_failed
    return {
        "ok": total_failed == 0 and bool(link_result.get("ok", True)),
        "dry_run": False,
        "processed": claimed,
        "planned": len(selected),
        "linked": sum(safe_int(row.get("outcome_id")) > 0 for row in final_selected),
        "backfilled": statuses.get("success", 0),
        "retry": statuses.get("retry_wait", 0),
        "terminal": statuses.get("terminal_unavailable", 0) + statuses.get("terminal_error", 0),
        "failed": total_failed,
        "refresh_failed": refresh_failed,
        "changed_outcomes": changed_outcomes,
        "refresh": refresh,
        "scan_results": scan_results,
        "quality_summary": final_quality.get("summary"),
        "duration_sec": round(time.perf_counter() - started, 4),
    }


def write_lifecycle_outcome_quality_report(
    report: dict[str, Any] | None = None,
    *,
    settings: Settings | None = None,
    json_path: Path = QUALITY_REPORT_JSON,
    markdown_path: Path = QUALITY_REPORT_MD,
) -> dict[str, str]:
    payload = report or lifecycle_outcome_quality(settings, write_reports=False)
    safe_payload = {
        key: payload.get(key)
        for key in ("ok", "generated_at", "summary", "status_counts", "reasons", "modules", "levels", "signal_types", "horizons", "timeline", "consistency")
    }
    atomic_write_text(json_path, json.dumps(safe_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    summary = safe_payload.get("summary") or {}
    lines = [
        "# Lifecycle Outcome Data Quality",
        "",
        f"Generated: {safe_payload.get('generated_at', '')}",
        "",
        f"- 生命周期关联覆盖率: {summary.get('lifecycle_link_coverage_ratio', 0):.2%}",
        f"- 候选信号关联覆盖率: {summary.get('candidate_link_coverage_ratio', 0):.2%}",
        f"- 到期候选解决率: {summary.get('due_resolution_ratio', 0):.2%}",
        f"- 有效 Outcome 成熟率: {summary.get('usable_outcome_maturity_ratio', 0):.2%}",
        f"- 生命周期成熟率: {summary.get('lifecycle_maturity_ratio', 0):.2%}",
        f"- 未分类缺口: {summary.get('generic_unclassified_count', 0)}",
        "",
        "尚未到期不是错误；unavailable 不等于亏损；ineligible 不进入 Outcome 分母。",
    ]
    atomic_write_text(markdown_path, "\n".join(lines) + "\n")
    return {"json": str(json_path), "markdown": str(markdown_path)}


def write_lifecycle_calibration_readiness_report(
    report: dict[str, Any] | None = None,
    *,
    settings: Settings | None = None,
    json_path: Path = READINESS_REPORT_JSON,
) -> dict[str, str]:
    payload = report or lifecycle_calibration_readiness(settings, write_reports=False)
    safe_payload = {
        key: payload.get(key)
        for key in ("ok", "ready", "label", "passed", "blocked", "warnings", "current", "required", "note", "calculated_at")
    }
    atomic_write_text(json_path, json.dumps(safe_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return {"json": str(json_path)}


__all__ = [
    "CANDIDATE_STATUSES", "CandidateClassification", "ELIGIBILITY_STATUSES", "GAP_REASONS",
    "INELIGIBLE_REASONS", "build_candidate_record", "calculate_quality_metrics",
    "classify_outcome_candidate", "classify_outcome_gaps", "classify_provider_failure",
    "evaluate_calibration_readiness", "incremental_outcome_backfill",
    "lifecycle_calibration_readiness", "lifecycle_outcome_quality", "refresh_outcome_candidates",
    "retry_delay_seconds", "stable_candidate_key", "write_lifecycle_calibration_readiness_report",
    "write_lifecycle_outcome_quality_report",
]
