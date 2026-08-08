from __future__ import annotations

import json
import hashlib
import math
import re
import secrets
import sqlite3
import sys
import time
from collections.abc import Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from config import BASE_DIR, Settings
from .signal_text import clean_signal_text, extract_symbols_from_text, signal_event_template_label


DEFAULT_SIGNAL_DB_PATH = BASE_DIR / "data" / "signals.db"
SIGNAL_STORE_SCHEMA_VERSION = 7
AI_CONTEXT_SNAPSHOT_SCHEMA_VERSION = 2
AI_CONTEXT_SNAPSHOT_MAX_BYTES = 16 * 1024
AI_RESULT_MAX_BYTES = 16 * 1024
AI_TRANSIENT_STATE_MAX_SEC = 5 * 60
ACTIVE_SIGNAL_MODULES = (
    "funding",
    "flow",
    "launch",
    "announcement",
    "summary",
    "test",
    "telegram",
)
SIGNAL_STORE_REQUIRED_OBJECTS = {
    "signals": "table",
    "signal_outcomes": "table",
    "idx_signals_ts": "index",
    "idx_signals_symbol_ts": "index",
    "idx_signals_symbol_id": "index",
    "idx_signals_module_ts": "index",
    "idx_signals_template_ts": "index",
    "idx_signals_public_ref": "index",
    "ux_signals_dedup_symbol": "index",
    "idx_signal_outcomes_due": "index",
    "idx_signal_outcomes_signal": "index",
    "signal_ai_snapshots": "table",
    "signal_ai_cache": "table",
    "signal_ai_audit": "table",
    "idx_signal_ai_snapshots_public_ref": "index",
    "idx_signal_ai_cache_signal": "index",
    "idx_signal_ai_audit_public_ref": "index",
    "idx_signal_ai_audit_ts": "index",
}
SIGNAL_DECISION_COLUMNS = (
    "id",
    "public_ref",
    "ts",
    "time",
    "module",
    "template_id",
    "signal_type",
    "symbol",
    "stage",
    "severity",
    "score",
    "title",
    "excerpt",
    "text_html",
    "status",
)
SIGNAL_COLUMN_MIGRATIONS = {
    "public_ref": "TEXT NOT NULL DEFAULT ''",
    "payload_json": "TEXT NOT NULL DEFAULT '{}'",
    "error": "TEXT NOT NULL DEFAULT ''",
    "ingest_mode": "TEXT NOT NULL DEFAULT 'legacy'",
    "quality_status": "TEXT NOT NULL DEFAULT 'degraded'",
}
SIGNAL_COLUMNS = (
    "id",
    "public_ref",
    "ts",
    "time",
    "module",
    "template_id",
    "signal_type",
    "symbol",
    "coin",
    "stage",
    "severity",
    "score",
    "title",
    "excerpt",
    "text_html",
    "dedup_key",
    "status",
    "sent",
    "topic_id",
    "message_ids_json",
    "reply_to_message_id",
    "payload_json",
    "error",
    "ingest_mode",
    "quality_status",
)
SIGNAL_LIST_PROJECTION = ", ".join(
    "substr(excerpt, 1, 260) AS excerpt"
    if column == "excerpt"
    else "'' AS text_html"
    if column == "text_html"
    else "'{}' AS payload_json"
    if column == "payload_json"
    else column
    for column in SIGNAL_COLUMNS
)
SIGNAL_COMPAT_DEFAULTS = {
    "id": "NULL",
    "public_ref": "''",
    "ts": "0",
    "time": "''",
    "module": "''",
    "template_id": "''",
    "signal_type": "''",
    "symbol": "''",
    "coin": "''",
    "stage": "''",
    "severity": "'info'",
    "score": "NULL",
    "title": "''",
    "excerpt": "''",
    "text_html": "''",
    "dedup_key": "''",
    "status": "''",
    "sent": "0",
    "topic_id": "''",
    "message_ids_json": "'[]'",
    "reply_to_message_id": "0",
    "payload_json": "'{}'",
    "error": "''",
    "ingest_mode": "'legacy'",
    "quality_status": "'degraded'",
}

STRUCTURED_SIGNAL_FIELDS = frozenset({
    "symbol", "coin", "score", "total_score", "stage", "category", "kind",
    "state", "status",
    "severity", "risk_level", "reason", "summary", "title", "price", "price_pct",
    "price_24h", "quote_volume", "market_cap", "mcap", "oi_usd", "oi_change_pct",
    "oi_24h", "funding_pct", "spot_cvd_delta", "futures_cvd_delta",
    "spot_inflow_usd", "spot_outflow_usd", "futures_inflow_usd",
    "futures_outflow_usd", "data_status", "window_sec", "observed_at", "source",
    "exchange", "grade", "scenario", "dedup_key", "code", "url",
    "data_quality_status", "data_quality_score", "quality_gate",
    "primary_data_source", "oi_source_agreement_score", "oi_binance_1h",
    "predicted_funding_pct", "funding_acceleration_pct",
    "last_price", "price_24h_pct", "primary_kind", "signal_direction",
    "evaluation_eligible", "launch_message_package_v2", "launch_cycle_id",
    "launch_cycle_no", "launch_observation_id",
    "module", "template_id",
})

AI_RESULT_FIELDS = (
    "status",
    "direction",
    "stage",
    "summary",
    "supporting_evidence",
    "counter_evidence",
    "risk_notes",
    "wait_for",
    "limitations",
)
AI_RESULT_LIST_FIELDS = AI_RESULT_FIELDS[4:]
AI_CONTEXT_FIELDS = frozenset({
    "discovery_score",
    "rule_result",
    "launch_phase",
    "multi_timeframe",
    "price_open_interest",
    "active_flow",
    "funding_basis",
    "structure",
    "plan",
    "completeness",
})
AI_CONTEXT_MAPPING_FIELDS = AI_CONTEXT_FIELDS - {"discovery_score"}
_AI_FORBIDDEN_KEY_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "bot_token",
    "credential",
    "password",
    "provider_body",
    "raw_response",
    "reasoning",
    "rpc_url",
    "secret",
)
_AI_FORBIDDEN_TEXT_MARKERS = (
    "http://",
    "https://",
    "authorization",
    "bearer ",
    "api_key",
    "bot_token",
)
_AI_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_AI_CACHE_KEY_PATTERN = re.compile(r"aic_[0-9a-f]{64}")
_AI_PUBLIC_REF_PATTERN = re.compile(r"sig_[0-9a-f]{20}")
_AI_SAFE_VERSION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}")
_AI_SAFE_MODEL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}")
_AI_ALLOWED_ERROR_CODES = frozenset({
    "ai_auth_failed",
    "ai_client_error",
    "ai_client_unavailable",
    "ai_connection_failed",
    "ai_dns_failed",
    "ai_empty_content",
    "ai_endpoint_not_found",
    "ai_http_error",
    "ai_insufficient_balance",
    "ai_invalid_parameters",
    "ai_invalid_request",
    "ai_output_truncated",
    "ai_policy_violation",
    "ai_provider_unavailable",
    "ai_rate_limited",
    "ai_redirect_rejected",
    "ai_request_failed",
    "ai_rule_conflict",
    "ai_timeout",
    "ai_tls_failed",
    "invalid_ai_output",
    "invalid_ai_result",
    "invalid_configuration",
})


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _safe_json_loads(value: str, default: Any) -> Any:
    try:
        return json.loads(value)
    except Exception:
        return default


def _ai_value_is_sensitive(value: object) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                return True
            normalized_key = key.strip().lower().replace("-", "_")
            if any(marker in normalized_key for marker in _AI_FORBIDDEN_KEY_MARKERS):
                return True
            if _ai_value_is_sensitive(item):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_ai_value_is_sensitive(item) for item in value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        return any(marker in lowered for marker in _AI_FORBIDDEN_TEXT_MARKERS)
    return False


def _normalize_ai_json_object(
    value: object,
    *,
    max_bytes: int,
) -> tuple[dict[str, Any] | None, str, str]:
    if not isinstance(value, dict) or not value or _ai_value_is_sensitive(value):
        return None, "", "snapshot_invalid"
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (OverflowError, RecursionError, TypeError, ValueError):
        return None, "", "snapshot_invalid"
    if len(encoded.encode("utf-8")) > max(1, int(max_bytes)):
        return None, "", "snapshot_too_large"
    decoded = _safe_json_loads(encoded, None)
    if not isinstance(decoded, dict) or not decoded:
        return None, "", "snapshot_invalid"
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return decoded, digest, "ready"


def _normalize_ai_success_result(
    value: object,
) -> tuple[dict[str, Any] | None, str]:
    if not isinstance(value, dict) or set(value) != set(AI_RESULT_FIELDS):
        return None, "invalid_ai_result"
    if value.get("status") != "available" or _ai_value_is_sensitive(value):
        return None, "invalid_ai_result"
    for key in ("direction", "stage"):
        item = value.get(key)
        if not isinstance(item, str) or not item.strip() or len(item) > 64:
            return None, "invalid_ai_result"
    summary = value.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 600:
        return None, "invalid_ai_result"
    normalized: dict[str, Any] = {
        "status": "available",
        "direction": str(value["direction"]).strip(),
        "stage": str(value["stage"]).strip(),
        "summary": summary.strip(),
    }
    for key in AI_RESULT_LIST_FIELDS:
        items = value.get(key)
        if (
            not isinstance(items, list)
            or len(items) > 8
            or any(
                not isinstance(item, str)
                or not item.strip()
                or len(item) > 240
                for item in items
            )
        ):
            return None, "invalid_ai_result"
        normalized[key] = [str(item).strip() for item in items]
    normalized_value, _digest, status = _normalize_ai_json_object(
        normalized,
        max_bytes=AI_RESULT_MAX_BYTES,
    )
    if status != "ready":
        return None, "invalid_ai_result"
    return normalized_value, "ready"


def _normalize_ai_context_snapshot(
    value: object,
) -> tuple[dict[str, Any] | None, str, str]:
    normalized, context_hash, status = _normalize_ai_json_object(
        value,
        max_bytes=AI_CONTEXT_SNAPSHOT_MAX_BYTES,
    )
    if status != "ready" or normalized is None:
        return None, "", status
    if set(normalized) != AI_CONTEXT_FIELDS or any(
        not isinstance(normalized.get(key), dict)
        for key in AI_CONTEXT_MAPPING_FIELDS
    ):
        return None, "", "snapshot_invalid"
    discovery_score = normalized.get("discovery_score")
    if (
        isinstance(discovery_score, bool)
        or (
            discovery_score is not None
            and not isinstance(discovery_score, (int, float))
        )
        or (
            isinstance(discovery_score, float)
            and not math.isfinite(discovery_score)
        )
    ):
        return None, "", "snapshot_invalid"
    rule = normalized.get("rule_result")
    direction = rule.get("direction") if isinstance(rule, dict) else None
    stage = (
        rule.get("stage") or rule.get("status")
        if isinstance(rule, dict)
        else None
    )
    if (
        not isinstance(direction, str)
        or not direction.strip()
        or len(direction) > 64
        or not isinstance(stage, str)
        or not stage.strip()
        or len(stage) > 64
    ):
        return None, "", "snapshot_invalid"
    return normalized, context_hash, "ready"


def build_ai_cache_key(
    *,
    context_hash: str,
    model: str,
    endpoint_hash: str,
    prompt_hash: str,
    policy_version: str,
) -> str:
    """Build a cache key from reviewed hashes without accepting endpoints or secrets."""

    normalized_context_hash = str(context_hash or "").strip().lower()
    normalized_endpoint_hash = str(endpoint_hash or "").strip().lower()
    normalized_prompt_hash = str(prompt_hash or "").strip().lower()
    normalized_model = str(model or "").strip()
    normalized_policy = str(policy_version or "").strip()
    if not all(
        _AI_HASH_PATTERN.fullmatch(value)
        for value in (
            normalized_context_hash,
            normalized_endpoint_hash,
            normalized_prompt_hash,
        )
    ):
        raise ValueError("ai_cache_hash_invalid")
    if not _AI_SAFE_MODEL_PATTERN.fullmatch(normalized_model):
        raise ValueError("ai_cache_model_invalid")
    if not _AI_SAFE_VERSION_PATTERN.fullmatch(normalized_policy):
        raise ValueError("ai_cache_policy_version_invalid")
    material = _json_dumps(
        {
            "context_hash": normalized_context_hash,
            "endpoint_hash": normalized_endpoint_hash,
            "model": normalized_model,
            "policy_version": normalized_policy,
            "prompt_hash": normalized_prompt_hash,
        }
    )
    return f"aic_{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def _safe_ai_error_code(value: object) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in _AI_ALLOWED_ERROR_CODES else "ai_request_failed"


def _ai_result_matches_context(
    result: Mapping[str, Any],
    context: Mapping[str, Any],
) -> bool:
    rule = context.get("rule_result")
    if not isinstance(rule, Mapping):
        return False
    expected_direction = str(rule.get("direction") or "none")[:64]
    expected_stage = str(rule.get("stage") or rule.get("status") or "unknown")[:64]
    return (
        result.get("direction") == expected_direction
        and result.get("stage") == expected_stage
    )


def _utc_time_text(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).isoformat()


def _limit(value: int | str | None, default: int, maximum: int) -> int:
    try:
        number = int(value if value is not None else default)
    except (TypeError, ValueError):
        number = default
    return max(1, min(maximum, number))


def _like_pattern(value: str) -> str:
    escaped = str(value or "").strip()[:80]
    escaped = escaped.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _clean_title(text: str) -> str:
    for line in str(text or "").splitlines():
        cleaned = clean_signal_text(line)
        if cleaned:
            return cleaned[:160]
    return ""


def _extract_stage(text: str) -> str:
    patterns = (
        r"(?:阶段|狀態|状态)\s*[:：]\s*([^\n|]+)",
        r"(?:分类|類型|类型)\s*[:：]\s*([^\n|]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, str(text or ""), flags=re.IGNORECASE)
        if match:
            return clean_signal_text(match.group(1))[:80]
    return ""


def _extract_score(text: str) -> float | None:
    match = re.search(
        r"(?:分数|评分|score)\s*[:：]\s*(-?\d+(?:\.\d+)?)|(-?\d+(?:\.\d+)?)\s*分(?:\b|\s|\|)",
        clean_signal_text(text),
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    try:
        return float(match.group(1) or match.group(2))
    except ValueError:
        return None


def _extract_symbol_score(text: str, symbol: str, *, symbol_count: int) -> float | None:
    """Recover a score near one explicit symbol without sharing it across a batch."""

    target = str(symbol or "").strip().upper()
    if not target:
        return _extract_score(text) if symbol_count <= 1 else None
    visible = clean_signal_text(text)
    match = re.search(rf"\b{re.escape(target)}\b", visible, flags=re.IGNORECASE)
    if match:
        segment = visible[match.end():match.end() + 360]
        local = re.search(
            r"(?:分数|评分|score)\s*[:：]?\s*(-?\d+(?:\.\d+)?)|(-?\d+(?:\.\d+)?)\s*分(?:\b|\s|\|)",
            segment,
            flags=re.IGNORECASE,
        )
        if local:
            try:
                return float(local.group(1) or local.group(2))
            except ValueError:
                return None
    return _extract_score(text) if symbol_count <= 1 else None


def _structured_number(record: dict[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = record.get(key)
        if value in (None, ""):
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number == number and abs(number) != float("inf"):
            return number
    return None


def _structured_payload(record: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key in STRUCTURED_SIGNAL_FIELDS:
        value = record.get(key)
        if value is None or isinstance(value, (str, int, float, bool)):
            if value is not None:
                payload[key] = value
        elif (
            key in {"linked_source_refs", "linked_source_modules"}
            and isinstance(value, list)
        ):
            payload[key] = [
                str(item)[:160]
                for item in value[:10]
                if isinstance(item, str) and item
            ]
    return payload


def _module_for_template(template_id: str) -> str:
    value = str(template_id or "").upper()
    if "FUNDING" in value:
        return "funding"
    if "FLOW" in value:
        return "flow"
    if "LAUNCH" in value:
        return "launch"
    if "ANNOUNCEMENT" in value:
        return "announcement"
    if "SUMMARY" in value or "RADAR" in value:
        return "summary"
    if "TEST" in value:
        return "test"
    return "telegram"


def _severity_for_status(status: str, text: str) -> str:
    status_key = str(status or "").lower()
    if status_key == "failed":
        return "error"
    if status_key == "blocked":
        return "warning"
    if status_key in {"dry_run", "skipped"}:
        return "info"
    clean = clean_signal_text(text).lower()
    if any(token in clean for token in ("极度危险", "高风险", "danger", "critical")):
        return "critical"
    if any(token in clean for token in ("警告", "预警", "风险", "warning", "warn")):
        return "warning"
    return "info"


def _coin_from_symbol(symbol: str) -> str:
    value = str(symbol or "").upper()
    return value[:-4] if value.endswith("USDT") else value


def signal_public_ref(dedup_key: str, symbol: str) -> str:
    """Build a stable, non-sequential public reference for a pushed signal."""
    source = f"{str(dedup_key or '').strip()}\x1f{str(symbol or '').strip().upper()}"
    return f"sig_{hashlib.sha256(source.encode('utf-8', errors='ignore')).hexdigest()[:20]}"


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = {key: row[key] for key in SIGNAL_COLUMNS}
    item["sent"] = bool(item.get("sent"))
    item["message_ids"] = _safe_json_loads(str(item.pop("message_ids_json") or "[]"), [])
    item["payload"] = _safe_json_loads(str(item.pop("payload_json") or "{}"), {})
    return item


def _row_to_decision_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in SIGNAL_DECISION_COLUMNS}


@dataclass(frozen=True)
class SignalEventStore:
    db_path: Path = DEFAULT_SIGNAL_DB_PATH

    def __post_init__(self) -> None:
        object.__setattr__(self, "db_path", Path(self.db_path))

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=15000")
            self._ensure_schema(conn)
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        if self._schema_is_current(conn):
            return
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                public_ref TEXT NOT NULL DEFAULT '',
                ts INTEGER NOT NULL,
                time TEXT NOT NULL,
                module TEXT NOT NULL,
                template_id TEXT NOT NULL,
                signal_type TEXT NOT NULL,
                symbol TEXT NOT NULL DEFAULT '',
                coin TEXT NOT NULL DEFAULT '',
                stage TEXT NOT NULL DEFAULT '',
                severity TEXT NOT NULL DEFAULT 'info',
                score REAL,
                title TEXT NOT NULL DEFAULT '',
                excerpt TEXT NOT NULL DEFAULT '',
                text_html TEXT NOT NULL DEFAULT '',
                dedup_key TEXT NOT NULL,
                status TEXT NOT NULL,
                sent INTEGER NOT NULL DEFAULT 0,
                topic_id TEXT NOT NULL DEFAULT '',
                message_ids_json TEXT NOT NULL DEFAULT '[]',
                reply_to_message_id INTEGER NOT NULL DEFAULT 0,
                payload_json TEXT NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                ingest_mode TEXT NOT NULL DEFAULT 'legacy',
                quality_status TEXT NOT NULL DEFAULT 'degraded'
            )
            """
        )
        self._ensure_signal_columns(conn)
        placeholders = ", ".join("?" for _ in ACTIVE_SIGNAL_MODULES)
        conn.execute(
            f"DELETE FROM signals WHERE module NOT IN ({placeholders})",
            ACTIVE_SIGNAL_MODULES,
        )
        missing_refs = conn.execute(
            "SELECT id, dedup_key, symbol FROM signals WHERE public_ref = ''"
        ).fetchall()
        for row in missing_refs:
            conn.execute(
                "UPDATE signals SET public_ref = ? WHERE id = ?",
                (signal_public_ref(str(row["dedup_key"] or ""), str(row["symbol"] or "")), int(row["id"])),
            )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_symbol_ts ON signals(symbol, ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_symbol_id ON signals(symbol, id DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_module_ts ON signals(module, ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_template_ts ON signals(template_id, ts DESC)")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_signals_public_ref ON signals(public_ref)")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_signals_dedup_symbol ON signals(dedup_key, symbol)"
        )
        self._ensure_outcome_schema(conn)
        self._ensure_ai_schema(conn)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS signal_store_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self._ensure_compat_views(conn)
        conn.execute(
            "INSERT OR REPLACE INTO signal_store_meta(key, value) VALUES('schema_version', ?)",
            (str(SIGNAL_STORE_SCHEMA_VERSION),),
        )

    @staticmethod
    def _ensure_outcome_schema(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS signal_outcomes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER NOT NULL,
                horizon TEXT NOT NULL,
                horizon_sec INTEGER NOT NULL,
                due_at INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                direction TEXT NOT NULL,
                signal_score REAL,
                signal_stage TEXT NOT NULL DEFAULT '',
                signal_category TEXT NOT NULL DEFAULT '',
                quality_gate TEXT NOT NULL DEFAULT 'unknown',
                data_quality_score REAL,
                entry_price REAL,
                entry_observed_at INTEGER,
                entry_source TEXT NOT NULL DEFAULT '',
                exit_price REAL,
                exit_observed_at INTEGER,
                exit_source TEXT NOT NULL DEFAULT '',
                raw_return_pct REAL,
                directional_return_pct REAL,
                is_hit INTEGER,
                evaluated_at INTEGER,
                error TEXT NOT NULL DEFAULT '',
                UNIQUE(signal_id, horizon),
                FOREIGN KEY(signal_id) REFERENCES signals(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_signal_outcomes_due ON signal_outcomes(status, due_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_signal_outcomes_signal ON signal_outcomes(signal_id)"
        )

    @staticmethod
    def _ensure_ai_schema(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS signal_ai_snapshots (
                signal_id INTEGER PRIMARY KEY,
                public_ref TEXT NOT NULL UNIQUE,
                schema_version INTEGER NOT NULL,
                context_hash TEXT NOT NULL,
                context_json TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                FOREIGN KEY(signal_id) REFERENCES signals(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS signal_ai_cache (
                cache_key TEXT PRIMARY KEY,
                signal_id INTEGER NOT NULL,
                context_hash TEXT NOT NULL,
                model TEXT NOT NULL,
                endpoint_hash TEXT NOT NULL,
                prompt_hash TEXT NOT NULL,
                policy_version TEXT NOT NULL,
                state TEXT NOT NULL,
                lease_id TEXT NOT NULL DEFAULT '',
                result_json TEXT NOT NULL DEFAULT '{}',
                error_code TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 0,
                started_at INTEGER NOT NULL DEFAULT 0,
                in_flight_until INTEGER NOT NULL DEFAULT 0,
                cooldown_until INTEGER NOT NULL DEFAULT 0,
                completed_at INTEGER NOT NULL DEFAULT 0,
                updated_at INTEGER NOT NULL,
                FOREIGN KEY(signal_id) REFERENCES signals(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS signal_ai_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id INTEGER NOT NULL,
                public_ref TEXT NOT NULL,
                cache_key TEXT NOT NULL,
                lease_id TEXT NOT NULL DEFAULT '',
                event TEXT NOT NULL,
                status TEXT NOT NULL,
                error_code TEXT NOT NULL DEFAULT '',
                ts INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_signal_ai_snapshots_public_ref "
            "ON signal_ai_snapshots(public_ref)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_signal_ai_cache_signal "
            "ON signal_ai_cache(signal_id, updated_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_signal_ai_audit_public_ref "
            "ON signal_ai_audit(public_ref, id DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_signal_ai_audit_ts "
            "ON signal_ai_audit(ts DESC)"
        )

    @staticmethod
    def _schema_is_current(conn: sqlite3.Connection) -> bool:
        try:
            version = conn.execute(
                "SELECT value FROM signal_store_meta WHERE key = 'schema_version'"
            ).fetchone()
        except sqlite3.OperationalError:
            return False
        if version is None or str(version["value"]) != str(SIGNAL_STORE_SCHEMA_VERSION):
            return False

        names = tuple((*SIGNAL_STORE_REQUIRED_OBJECTS, "signal_events"))
        placeholders = ", ".join("?" for _ in names)
        objects = {
            str(row["name"]): str(row["type"])
            for row in conn.execute(
                f"SELECT name, type FROM sqlite_master WHERE name IN ({placeholders})",
                names,
            ).fetchall()
        }
        if any(
            objects.get(name) != object_type
            for name, object_type in SIGNAL_STORE_REQUIRED_OBJECTS.items()
        ):
            return False
        if objects.get("signal_events") not in {"table", "view"}:
            return False
        columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(signals)").fetchall()
        }
        return set(SIGNAL_COLUMNS).issubset(columns)

    @staticmethod
    def _ensure_signal_columns(conn: sqlite3.Connection) -> None:
        available = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(signals)").fetchall()
        }
        for column, definition in SIGNAL_COLUMN_MIGRATIONS.items():
            if column not in available:
                conn.execute(f"ALTER TABLE signals ADD COLUMN {column} {definition}")

    def _ensure_compat_views(self, conn: sqlite3.Connection) -> None:
        existing = conn.execute(
            "SELECT type FROM sqlite_master WHERE name = 'signal_events' LIMIT 1"
        ).fetchone()
        if existing and str(existing["type"]) != "view":
            conn.execute(
                "INSERT OR REPLACE INTO signal_store_meta(key, value) VALUES('signal_events_object_type', ?)",
                (str(existing["type"]),),
            )
            return
        if existing:
            conn.execute("DROP VIEW IF EXISTS signal_events")
        available_columns = {
            str(row["name"])
            for row in conn.execute("PRAGMA table_info(signals)").fetchall()
        }
        select_columns = []
        for column in SIGNAL_COLUMNS:
            if column in available_columns:
                select_columns.append(column)
            else:
                select_columns.append(f"{SIGNAL_COMPAT_DEFAULTS[column]} AS {column}")
        conn.execute(
            f"""
            CREATE VIEW IF NOT EXISTS signal_events AS
            SELECT {", ".join(select_columns)}
            FROM signals
            """
        )
        conn.execute(
            "INSERT OR REPLACE INTO signal_store_meta(key, value) VALUES('signal_events_object_type', 'view')"
        )

    def append_from_push(
        self,
        *,
        template_id: str,
        dedup_key: str,
        status: str,
        sent: bool,
        text: str,
        ts: int | None = None,
        topic_id: str = "",
        message_ids: list[int] | None = None,
        reply_to_message_id: int | None = None,
        structured_records: list[dict[str, Any]] | None = None,
    ) -> int:
        now = int(ts or time.time())
        clean_excerpt = clean_signal_text(text)[:1200]
        title = _clean_title(text)
        safe_dedup_key = str(dedup_key or "").strip()
        if not safe_dedup_key:
            digest = hashlib.sha1(str(text or "").encode("utf-8", errors="ignore")).hexdigest()[:16]
            safe_dedup_key = f"{template_id or 'telegram'}:{now}:{digest}"
        module = _module_for_template(template_id)
        signal_type = signal_event_template_label(template_id)
        message_ids_json = _json_dumps([int(item) for item in (message_ids or []) if isinstance(item, int)])
        raw_records = [item for item in (structured_records or []) if isinstance(item, dict)]
        prepared_records: list[dict[str, Any]] = []
        for record in raw_records:
            candidates = [record.get("symbol")]
            if not candidates[0] and isinstance(record.get("symbols"), list):
                candidates = list(record.get("symbols") or [])
            for candidate in candidates:
                normalized_symbol = str(candidate or "").strip().upper()
                if normalized_symbol and not normalized_symbol.endswith("USDT"):
                    normalized_symbol = f"{normalized_symbol}USDT"
                symbol_pattern = r"[A-Z0-9]{2,24}USDT"
                if normalized_symbol and not re.fullmatch(
                    symbol_pattern, normalized_symbol
                ):
                    continue
                prepared_records.append({**record, "symbol": normalized_symbol})
        structured_mode = bool(prepared_records)
        if not prepared_records:
            symbols = extract_symbols_from_text(text) or [""]
            prepared_records = [{"symbol": symbol} for symbol in symbols]

        rows = []
        symbol_count = len(prepared_records)
        for record in prepared_records:
            normalized_symbol = str(record.get("symbol") or "").upper()
            score = (
                _structured_number(record, "score", "total_score")
                if structured_mode
                else _extract_symbol_score(text, normalized_symbol, symbol_count=symbol_count)
            )
            stage = str(
                record.get("stage")
                or record.get("category")
                or record.get("kind")
                or record.get("state")
                or _extract_stage(text)
                or ""
            )[:80]
            severity = str(record.get("severity") or record.get("risk_level") or "")[:24]
            if not severity:
                severity = _severity_for_status(status, text)
            record_summary = str(record.get("summary") or record.get("reason") or "").strip()
            record_title = str(record.get("title") or "").strip()
            payload = {
                "source": "telegram_push",
                "ingest_source": "engine_structured" if structured_mode else "telegram_text",
                "schema_version": SIGNAL_STORE_SCHEMA_VERSION,
                "reason": str(status or ""),
            }
            if structured_mode:
                facts = _structured_payload(record)
                if module in {"flow", "launch", "funding"}:
                    facts.setdefault("evaluation_eligible", True)
                payload["facts"] = facts
            ai_snapshot: dict[str, Any] | None = None
            ai_context_hash = ""
            ai_snapshot_status = "snapshot_missing"
            if "ai_context_snapshot" in record:
                if module != "launch":
                    ai_snapshot_status = "snapshot_ignored_module"
                else:
                    (
                        ai_snapshot,
                        ai_context_hash,
                        ai_snapshot_status,
                    ) = _normalize_ai_context_snapshot(
                        record.get("ai_context_snapshot")
                    )
                payload["ai_context_snapshot_status"] = ai_snapshot_status
                if ai_context_hash:
                    payload["ai_context_hash"] = ai_context_hash
                    payload["ai_context_snapshot_schema_version"] = (
                        AI_CONTEXT_SNAPSHOT_SCHEMA_VERSION
                    )
            quality_status = "ready" if structured_mode and normalized_symbol else "degraded"
            rows.append(
                {
                    "ts": now,
                    "public_ref": signal_public_ref(safe_dedup_key, normalized_symbol),
                    "time": _utc_time_text(now),
                    "module": module,
                    "template_id": str(template_id or ""),
                    "signal_type": str(signal_type or template_id or ""),
                    "symbol": normalized_symbol,
                    "coin": _coin_from_symbol(normalized_symbol),
                    "stage": stage,
                    "severity": severity,
                    "score": score,
                    "title": (record_title or title)[:160],
                    "excerpt": (record_summary or clean_excerpt)[:1200],
                    "text_html": str(text or "")[:20000],
                    "dedup_key": safe_dedup_key,
                    "status": str(status or ""),
                    "sent": 1 if sent else 0,
                    "topic_id": str(topic_id or ""),
                    "message_ids_json": message_ids_json,
                    "reply_to_message_id": int(reply_to_message_id or 0),
                    "payload_json": _json_dumps(payload),
                    "error": "" if str(status or "").lower() not in {"failed", "blocked"} else clean_excerpt[:300],
                    "ingest_mode": "structured" if structured_mode else "text_fallback",
                    "quality_status": quality_status,
                    "_ai_context_snapshot": ai_snapshot,
                    "_ai_context_hash": ai_context_hash,
                    "_ai_snapshot_supplied": "ai_context_snapshot" in record,
                }
            )
        with self.connect() as conn:
            for row in rows:
                conn.execute(
                    """
                    INSERT INTO signals (
                        public_ref, ts, time, module, template_id, signal_type, symbol, coin, stage, severity, score,
                        title, excerpt, text_html, dedup_key, status, sent, topic_id, message_ids_json,
                        reply_to_message_id, payload_json, error, ingest_mode, quality_status
                    ) VALUES (
                        :public_ref, :ts, :time, :module, :template_id, :signal_type, :symbol, :coin, :stage, :severity, :score,
                        :title, :excerpt, :text_html, :dedup_key, :status, :sent, :topic_id, :message_ids_json,
                        :reply_to_message_id, :payload_json, :error, :ingest_mode, :quality_status
                    )
                    ON CONFLICT(dedup_key, symbol) DO UPDATE SET
                        ts=excluded.ts,
                        public_ref=excluded.public_ref,
                        time=excluded.time,
                        module=excluded.module,
                        template_id=excluded.template_id,
                        signal_type=excluded.signal_type,
                        coin=excluded.coin,
                        stage=excluded.stage,
                        severity=excluded.severity,
                        score=excluded.score,
                        title=excluded.title,
                        excerpt=excluded.excerpt,
                        text_html=excluded.text_html,
                        status=excluded.status,
                        sent=excluded.sent,
                        topic_id=excluded.topic_id,
                        message_ids_json=excluded.message_ids_json,
                        reply_to_message_id=excluded.reply_to_message_id,
                        payload_json=excluded.payload_json,
                        error=excluded.error,
                        ingest_mode=excluded.ingest_mode,
                        quality_status=excluded.quality_status
                    """,
                    row,
                )
                signal = conn.execute(
                    "SELECT id, public_ref FROM signals WHERE dedup_key = ? AND symbol = ?",
                    (row["dedup_key"], row["symbol"]),
                ).fetchone()
                if signal is None or row["module"] != "launch":
                    continue
                signal_id = int(signal["id"])
                snapshot = row.get("_ai_context_snapshot")
                context_hash = str(row.get("_ai_context_hash") or "")
                if isinstance(snapshot, dict) and context_hash:
                    conn.execute(
                        """
                        INSERT INTO signal_ai_snapshots (
                            signal_id, public_ref, schema_version, context_hash,
                            context_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(signal_id) DO UPDATE SET
                            public_ref=excluded.public_ref,
                            schema_version=excluded.schema_version,
                            context_hash=excluded.context_hash,
                            context_json=excluded.context_json,
                            updated_at=excluded.updated_at
                        """,
                        (
                            signal_id,
                            str(signal["public_ref"]),
                            AI_CONTEXT_SNAPSHOT_SCHEMA_VERSION,
                            context_hash,
                            _json_dumps(snapshot),
                            now,
                            now,
                        ),
                    )
                    conn.execute(
                        "DELETE FROM signal_ai_cache WHERE signal_id = ? AND context_hash != ?",
                        (signal_id, context_hash),
                    )
                elif bool(row.get("_ai_snapshot_supplied")):
                    conn.execute(
                        "DELETE FROM signal_ai_snapshots WHERE signal_id = ?",
                        (signal_id,),
                    )
                    conn.execute(
                        "DELETE FROM signal_ai_cache WHERE signal_id = ?",
                        (signal_id,),
                    )
        return len(rows)

    @staticmethod
    def _load_ai_context_snapshot_conn(
        conn: sqlite3.Connection,
        public_ref: str,
    ) -> dict[str, Any]:
        normalized_ref = str(public_ref or "").strip()
        if not _AI_PUBLIC_REF_PATTERN.fullmatch(normalized_ref):
            return {"status": "invalid_public_ref"}
        row = conn.execute(
            """
            SELECT
                signals.id AS signal_id,
                signals.public_ref,
                signals.symbol,
                signals.ts AS signal_ts,
                signals.stage,
                signal_ai_snapshots.schema_version,
                signal_ai_snapshots.context_hash,
                signal_ai_snapshots.context_json,
                signal_ai_snapshots.created_at,
                signal_ai_snapshots.updated_at
            FROM signals
            LEFT JOIN signal_ai_snapshots
              ON signal_ai_snapshots.signal_id = signals.id
            WHERE signals.public_ref = ?
              AND signals.module = 'launch'
              AND signals.sent = 1
              AND signals.status = 'sent'
              AND signals.quality_status = 'ready'
            LIMIT 1
            """,
            (normalized_ref,),
        ).fetchone()
        if row is None:
            return {"status": "signal_unavailable", "public_ref": normalized_ref}
        raw_symbol = str(row["symbol"] or "").strip().upper()
        symbol = (
            raw_symbol
            if re.fullmatch(r"[A-Z0-9]{2,24}USDT", raw_symbol)
            else ""
        )
        stage_value = clean_signal_text(str(row["stage"] or "")).replace(
            "\x00",
            "",
        )[:80]
        signal_meta = {
            "public_ref": normalized_ref,
            "symbol": symbol,
            "signal_ts": max(0, int(row["signal_ts"] or 0)),
            "stage": "" if _ai_value_is_sensitive(stage_value) else stage_value,
        }
        if row["context_json"] is None:
            return {"status": "snapshot_missing", **signal_meta}
        if int(row["schema_version"] or 0) != AI_CONTEXT_SNAPSHOT_SCHEMA_VERSION:
            return {"status": "snapshot_invalid", **signal_meta}
        raw_context = _safe_json_loads(str(row["context_json"] or ""), None)
        context, context_hash, status = _normalize_ai_context_snapshot(raw_context)
        stored_hash = str(row["context_hash"] or "").strip().lower()
        if (
            status != "ready"
            or context is None
            or not _AI_HASH_PATTERN.fullmatch(stored_hash)
            or context_hash != stored_hash
        ):
            return {"status": "snapshot_invalid", **signal_meta}
        return {
            "status": "ready",
            **signal_meta,
            "context_hash": context_hash,
            "snapshot": context,
            "captured_at": int(row["created_at"] or 0),
            "updated_at": int(row["updated_at"] or 0),
            "_signal_id": int(row["signal_id"]),
        }

    def load_ai_context_snapshot(self, public_ref: str) -> dict[str, Any]:
        """Load one immutable prompt-ready snapshot by its opaque public reference."""

        with self.connect() as conn:
            result = self._load_ai_context_snapshot_conn(conn, public_ref)
        result.pop("_signal_id", None)
        return result

    @staticmethod
    def _append_ai_audit(
        conn: sqlite3.Connection,
        *,
        signal_id: int,
        public_ref: str,
        cache_key: str,
        lease_id: str,
        event: str,
        status: str,
        error_code: str = "",
        ts: int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO signal_ai_audit (
                signal_id, public_ref, cache_key, lease_id,
                event, status, error_code, ts
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(signal_id),
                str(public_ref),
                str(cache_key),
                str(lease_id),
                str(event)[:40],
                str(status)[:40],
                _safe_ai_error_code(error_code) if error_code else "",
                int(ts),
            ),
        )

    @staticmethod
    def _ai_daily_quota_conn(
        conn: sqlite3.Connection,
        *,
        now_ts: int,
        daily_limit: int | None,
    ) -> dict[str, Any]:
        day_start = max(0, int(now_ts) - (int(now_ts) % 86400))
        day_end = day_start + 86400
        used = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM signal_ai_audit
                WHERE event = 'reserved' AND ts >= ? AND ts < ?
                """,
                (day_start, day_end),
            ).fetchone()[0]
        )
        payload: dict[str, Any] = {
            "status": "ok",
            "utc_day_start": day_start,
            "utc_day_end": day_end,
            "provider_reserved": used,
        }
        if daily_limit is not None:
            limit = int(daily_limit)
            payload.update(
                daily_limit=limit,
                remaining=max(0, limit - used),
                exhausted=used >= limit,
            )
        return payload

    def ai_daily_quota(
        self,
        *,
        now_ts: int | None = None,
        daily_limit: int | None = None,
    ) -> dict[str, Any]:
        """Report provider reservations for the current UTC day."""

        now = int(time.time() if now_ts is None else now_ts)
        normalized_limit: int | None = None
        if daily_limit is not None:
            normalized_limit = int(daily_limit)
            if not 0 <= normalized_limit <= 100_000:
                raise ValueError("ai_daily_limit_invalid")
        with self.connect() as conn:
            return self._ai_daily_quota_conn(
                conn,
                now_ts=now,
                daily_limit=normalized_limit,
            )

    def reserve_ai_interpretation(
        self,
        public_ref: str,
        *,
        model: str,
        endpoint_hash: str,
        prompt_hash: str,
        policy_version: str,
        now_ts: int | None = None,
        in_flight_ttl_sec: int = 120,
        daily_limit: int | None = None,
    ) -> dict[str, Any]:
        """Return cached success or atomically reserve one provider request."""

        now = int(time.time() if now_ts is None else now_ts)
        lease_ttl = max(
            1,
            min(AI_TRANSIENT_STATE_MAX_SEC, int(in_flight_ttl_sec)),
        )
        normalized_daily_limit: int | None = None
        if daily_limit is not None:
            normalized_daily_limit = int(daily_limit)
            if not 0 <= normalized_daily_limit <= 100_000:
                raise ValueError("ai_daily_limit_invalid")
        with self.connect() as conn:
            if normalized_daily_limit is not None:
                if conn.in_transaction:
                    conn.commit()
                conn.execute("BEGIN IMMEDIATE")
            snapshot = self._load_ai_context_snapshot_conn(conn, public_ref)
            if snapshot.get("status") != "ready":
                snapshot.pop("_signal_id", None)
                return snapshot
            signal_id = int(snapshot["_signal_id"])
            normalized_ref = str(snapshot["public_ref"])
            context_hash = str(snapshot["context_hash"])
            cache_key = build_ai_cache_key(
                context_hash=context_hash,
                model=model,
                endpoint_hash=endpoint_hash,
                prompt_hash=prompt_hash,
                policy_version=policy_version,
            )
            normalized_model = str(model).strip()
            normalized_endpoint_hash = str(endpoint_hash).strip().lower()
            normalized_prompt_hash = str(prompt_hash).strip().lower()
            normalized_policy = str(policy_version).strip()
            signal_meta = {
                "public_ref": normalized_ref,
                "symbol": str(snapshot.get("symbol") or ""),
                "signal_ts": int(snapshot.get("signal_ts") or 0),
                "stage": str(snapshot.get("stage") or ""),
            }

            def quota_rejection() -> dict[str, Any] | None:
                if normalized_daily_limit is None:
                    return None
                quota = self._ai_daily_quota_conn(
                    conn,
                    now_ts=now,
                    daily_limit=normalized_daily_limit,
                )
                if not quota.get("exhausted"):
                    return None
                self._append_ai_audit(
                    conn,
                    signal_id=signal_id,
                    public_ref=normalized_ref,
                    cache_key=cache_key,
                    lease_id="",
                    event="quota_rejected",
                    status="quota_exhausted",
                    ts=now,
                )
                return {
                    "status": "quota_exhausted",
                    "source": "quota",
                    **signal_meta,
                    "cache_key": cache_key,
                    "daily_limit": normalized_daily_limit,
                    "provider_reserved": int(quota["provider_reserved"]),
                    "remaining": 0,
                }

            for _attempt in range(3):
                row = conn.execute(
                    "SELECT * FROM signal_ai_cache WHERE cache_key = ?",
                    (cache_key,),
                ).fetchone()
                if row is None:
                    rejected = quota_rejection()
                    if rejected is not None:
                        return rejected
                    lease_id = secrets.token_hex(16)
                    inserted = conn.execute(
                        """
                        INSERT OR IGNORE INTO signal_ai_cache (
                            cache_key, signal_id, context_hash, model,
                            endpoint_hash, prompt_hash, policy_version,
                            state, lease_id, result_json, error_code,
                            attempts, started_at, in_flight_until,
                            cooldown_until, completed_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'in_flight', ?, '{}', '', 1, ?, ?, 0, 0, ?)
                        """,
                        (
                            cache_key,
                            signal_id,
                            context_hash,
                            normalized_model,
                            normalized_endpoint_hash,
                            normalized_prompt_hash,
                            normalized_policy,
                            lease_id,
                            now,
                            now + lease_ttl,
                            now,
                        ),
                    ).rowcount
                    if inserted:
                        self._append_ai_audit(
                            conn,
                            signal_id=signal_id,
                            public_ref=normalized_ref,
                            cache_key=cache_key,
                            lease_id=lease_id,
                            event="reserved",
                            status="in_flight",
                            ts=now,
                        )
                        return {
                            "status": "reserved",
                            "source": "provider",
                            **signal_meta,
                            "cache_key": cache_key,
                            "lease_id": lease_id,
                            "context_hash": context_hash,
                            "snapshot": snapshot["snapshot"],
                            "in_flight_until": now + lease_ttl,
                        }
                    continue
                state = str(row["state"] or "")
                if state == "available":
                    cached_result, result_status = _normalize_ai_success_result(
                        _safe_json_loads(str(row["result_json"] or "{}"), None)
                    )
                    if (
                        result_status == "ready"
                        and cached_result is not None
                        and _ai_result_matches_context(
                            cached_result,
                            snapshot["snapshot"],
                        )
                    ):
                        self._append_ai_audit(
                            conn,
                            signal_id=signal_id,
                            public_ref=normalized_ref,
                            cache_key=cache_key,
                            lease_id="",
                            event="cache_hit",
                            status="available",
                            ts=now,
                        )
                        return {
                            "status": "available",
                            "source": "cache",
                            **signal_meta,
                            "cache_key": cache_key,
                            "context_hash": context_hash,
                            "result": cached_result,
                        }
                elif state == "in_flight" and int(row["in_flight_until"] or 0) > now:
                    retry_after = int(row["in_flight_until"]) - now
                    self._append_ai_audit(
                        conn,
                        signal_id=signal_id,
                        public_ref=normalized_ref,
                        cache_key=cache_key,
                        lease_id="",
                        event="deduplicated",
                        status="in_flight",
                        ts=now,
                    )
                    return {
                        "status": "in_flight",
                        "source": "singleflight",
                        **signal_meta,
                        "cache_key": cache_key,
                        "retry_after": retry_after,
                    }
                elif state == "cooldown" and int(row["cooldown_until"] or 0) > now:
                    retry_after = int(row["cooldown_until"]) - now
                    error_code = _safe_ai_error_code(row["error_code"])
                    self._append_ai_audit(
                        conn,
                        signal_id=signal_id,
                        public_ref=normalized_ref,
                        cache_key=cache_key,
                        lease_id="",
                        event="cooldown_hit",
                        status="cooldown",
                        error_code=error_code,
                        ts=now,
                    )
                    return {
                        "status": "cooldown",
                        "source": "cooldown",
                        **signal_meta,
                        "cache_key": cache_key,
                        "error_code": error_code,
                        "retry_after": retry_after,
                    }

                rejected = quota_rejection()
                if rejected is not None:
                    return rejected
                lease_id = secrets.token_hex(16)
                previous_state = state
                previous_updated_at = int(row["updated_at"] or 0)
                previous_lease_id = str(row["lease_id"] or "")
                updated = conn.execute(
                    """
                    UPDATE signal_ai_cache
                    SET signal_id = ?, state = 'in_flight', lease_id = ?,
                        result_json = '{}', error_code = '',
                        attempts = attempts + 1, started_at = ?,
                        in_flight_until = ?, cooldown_until = 0,
                        completed_at = 0, updated_at = ?
                    WHERE cache_key = ?
                      AND state = ?
                      AND updated_at = ?
                      AND lease_id = ?
                    """,
                    (
                        signal_id,
                        lease_id,
                        now,
                        now + lease_ttl,
                        now,
                        cache_key,
                        previous_state,
                        previous_updated_at,
                        previous_lease_id,
                    ),
                ).rowcount
                if updated:
                    self._append_ai_audit(
                        conn,
                        signal_id=signal_id,
                        public_ref=normalized_ref,
                        cache_key=cache_key,
                        lease_id=lease_id,
                        event="reserved",
                        status="in_flight",
                        ts=now,
                    )
                    return {
                        "status": "reserved",
                        "source": "provider",
                        **signal_meta,
                        "cache_key": cache_key,
                        "lease_id": lease_id,
                        "context_hash": context_hash,
                        "snapshot": snapshot["snapshot"],
                        "in_flight_until": now + lease_ttl,
                    }
            return {
                "status": "in_flight",
                "source": "singleflight",
                **signal_meta,
                "cache_key": cache_key,
                "retry_after": 1,
            }

    def cache_ai_success(
        self,
        cache_key: str,
        lease_id: str,
        result: Mapping[str, Any],
        *,
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        """Persist only a validated, policy-safe successful interpretation."""

        normalized_cache_key = str(cache_key or "").strip()
        normalized_lease_id = str(lease_id or "").strip().lower()
        if not _AI_CACHE_KEY_PATTERN.fullmatch(normalized_cache_key):
            return {"status": "invalid_cache_key"}
        if not re.fullmatch(r"[0-9a-f]{32}", normalized_lease_id):
            return {"status": "invalid_lease_id"}
        normalized_result, result_status = _normalize_ai_success_result(result)
        if result_status != "ready" or normalized_result is None:
            failed = self.cache_ai_failure(
                normalized_cache_key,
                normalized_lease_id,
                "invalid_ai_result",
                cooldown_sec=60,
                now_ts=now_ts,
            )
            return {
                "status": "invalid_ai_result",
                "stored": False,
                "cooldown": failed.get("status") == "cooldown",
            }
        now = int(time.time() if now_ts is None else now_ts)
        with self.connect() as conn:
            context_row = conn.execute(
                """
                SELECT
                    signal_ai_cache.context_hash AS cache_context_hash,
                    signal_ai_snapshots.context_hash AS snapshot_context_hash,
                    signal_ai_snapshots.context_json
                FROM signal_ai_cache
                JOIN signal_ai_snapshots
                  ON signal_ai_snapshots.signal_id = signal_ai_cache.signal_id
                WHERE signal_ai_cache.cache_key = ?
                  AND signal_ai_cache.state = 'in_flight'
                  AND signal_ai_cache.lease_id = ?
                """,
                (normalized_cache_key, normalized_lease_id),
            ).fetchone()
        if context_row is None:
            return {"status": "stale_request", "stored": False}
        raw_context = _safe_json_loads(str(context_row["context_json"] or ""), None)
        context, context_hash, context_status = _normalize_ai_context_snapshot(
            raw_context
        )
        if (
            context_status != "ready"
            or context is None
            or context_hash != str(context_row["cache_context_hash"] or "")
            or context_hash != str(context_row["snapshot_context_hash"] or "")
            or not _ai_result_matches_context(normalized_result, context)
        ):
            failed = self.cache_ai_failure(
                normalized_cache_key,
                normalized_lease_id,
                "ai_rule_conflict",
                cooldown_sec=60,
                now_ts=now,
            )
            return {
                "status": "ai_rule_conflict",
                "stored": False,
                "cooldown": failed.get("status") == "cooldown",
            }
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT signal_ai_cache.signal_id, signals.public_ref
                FROM signal_ai_cache
                JOIN signals ON signals.id = signal_ai_cache.signal_id
                WHERE signal_ai_cache.cache_key = ?
                """,
                (normalized_cache_key,),
            ).fetchone()
            if row is None:
                return {"status": "stale_request", "stored": False}
            updated = conn.execute(
                """
                UPDATE signal_ai_cache
                SET state = 'available', lease_id = '', result_json = ?,
                    error_code = '', in_flight_until = 0,
                    cooldown_until = 0, completed_at = ?, updated_at = ?
                WHERE cache_key = ? AND state = 'in_flight' AND lease_id = ?
                """,
                (
                    _json_dumps(normalized_result),
                    now,
                    now,
                    normalized_cache_key,
                    normalized_lease_id,
                ),
            ).rowcount
            if not updated:
                return {"status": "stale_request", "stored": False}
            self._append_ai_audit(
                conn,
                signal_id=int(row["signal_id"]),
                public_ref=str(row["public_ref"]),
                cache_key=normalized_cache_key,
                lease_id=normalized_lease_id,
                event="completed",
                status="available",
                ts=now,
            )
        return {"status": "available", "stored": True}

    def cache_ai_failure(
        self,
        cache_key: str,
        lease_id: str,
        error_code: str,
        *,
        cooldown_sec: int = 60,
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        """Store only a bounded error code and a short retry cooldown."""

        normalized_cache_key = str(cache_key or "").strip()
        normalized_lease_id = str(lease_id or "").strip().lower()
        if not _AI_CACHE_KEY_PATTERN.fullmatch(normalized_cache_key):
            return {"status": "invalid_cache_key", "stored": False}
        if not re.fullmatch(r"[0-9a-f]{32}", normalized_lease_id):
            return {"status": "invalid_lease_id", "stored": False}
        safe_error = _safe_ai_error_code(error_code)
        cooldown = max(
            1,
            min(AI_TRANSIENT_STATE_MAX_SEC, int(cooldown_sec)),
        )
        now = int(time.time() if now_ts is None else now_ts)
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT signal_ai_cache.signal_id, signals.public_ref
                FROM signal_ai_cache
                JOIN signals ON signals.id = signal_ai_cache.signal_id
                WHERE signal_ai_cache.cache_key = ?
                """,
                (normalized_cache_key,),
            ).fetchone()
            if row is None:
                return {"status": "stale_request", "stored": False}
            updated = conn.execute(
                """
                UPDATE signal_ai_cache
                SET state = 'cooldown', lease_id = '', result_json = '{}',
                    error_code = ?, in_flight_until = 0,
                    cooldown_until = ?, completed_at = ?, updated_at = ?
                WHERE cache_key = ? AND state = 'in_flight' AND lease_id = ?
                """,
                (
                    safe_error,
                    now + cooldown,
                    now,
                    now,
                    normalized_cache_key,
                    normalized_lease_id,
                ),
            ).rowcount
            if not updated:
                return {"status": "stale_request", "stored": False}
            self._append_ai_audit(
                conn,
                signal_id=int(row["signal_id"]),
                public_ref=str(row["public_ref"]),
                cache_key=normalized_cache_key,
                lease_id=normalized_lease_id,
                event="failed",
                status="cooldown",
                error_code=safe_error,
                ts=now,
            )
        return {
            "status": "cooldown",
            "stored": True,
            "error_code": safe_error,
            "retry_after": cooldown,
        }

    def list_ai_interpretation_audit(
        self,
        public_ref: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        normalized_ref = str(public_ref or "").strip()
        if not _AI_PUBLIC_REF_PATTERN.fullmatch(normalized_ref):
            return []
        row_limit = _limit(limit, 50, 200)
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT public_ref, event, status, error_code, ts
                FROM signal_ai_audit
                WHERE public_ref = ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (normalized_ref, row_limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def launch_message_cleanup_candidates(
        self,
        *,
        symbol: str,
        cycle_started_at: int,
        now_ts: int,
        max_age_sec: int,
    ) -> dict[str, Any]:
        """Return still-actionable Telegram message IDs for one launch cycle."""

        normalized_symbol = str(symbol or "").strip().upper()
        cutoff = int(now_ts) - max(1, int(max_age_sec))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, ts, message_ids_json, payload_json
                FROM signals
                WHERE module = 'launch'
                  AND symbol = ?
                  AND sent = 1
                  AND status = 'sent'
                  AND ts >= ?
                ORDER BY ts ASC, id ASC
                """,
                (normalized_symbol, max(0, int(cycle_started_at))),
            ).fetchall()

        candidates: dict[int, int] = {}
        for row in rows:
            message_ids = _safe_json_loads(str(row["message_ids_json"] or "[]"), [])
            payload = _safe_json_loads(str(row["payload_json"] or "{}"), {})
            cleanup = payload.get("telegram_cleanup", {}) if isinstance(payload, dict) else {}
            completed = {
                int(message_id)
                for key in ("deleted_message_ids", "undeletable_message_ids")
                for message_id in ((cleanup.get(key) or []) if isinstance(cleanup, dict) else [])
                if isinstance(message_id, int) or str(message_id).isdigit()
            }
            for message_id in message_ids if isinstance(message_ids, list) else []:
                if not (isinstance(message_id, int) or str(message_id).isdigit()):
                    continue
                normalized_id = int(message_id)
                if normalized_id not in completed:
                    candidates[normalized_id] = int(row["ts"])

        deletable_ids = sorted(
            message_id for message_id, sent_at in candidates.items() if sent_at >= cutoff
        )
        undeletable_ids = sorted(
            message_id for message_id, sent_at in candidates.items() if sent_at < cutoff
        )
        return {
            "row_count": len(rows),
            "deletable_ids": deletable_ids,
            "undeletable_ids": undeletable_ids,
        }

    def mark_launch_message_cleanup(
        self,
        *,
        symbol: str,
        cycle_started_at: int,
        message_ids: list[int],
        outcome: str,
        now_ts: int,
    ) -> int:
        """Audit Telegram cleanup without changing signal delivery/evaluation status."""

        if outcome not in {"deleted", "undeletable"}:
            raise ValueError("outcome must be deleted or undeletable")
        normalized_ids = {
            int(message_id)
            for message_id in message_ids
            if isinstance(message_id, int) or str(message_id).isdigit()
        }
        if not normalized_ids:
            return 0

        normalized_symbol = str(symbol or "").strip().upper()
        target_key = "deleted_message_ids" if outcome == "deleted" else "undeletable_message_ids"
        updated = 0
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, message_ids_json, payload_json
                FROM signals
                WHERE module = 'launch'
                  AND symbol = ?
                  AND sent = 1
                  AND status = 'sent'
                  AND ts >= ?
                """,
                (normalized_symbol, max(0, int(cycle_started_at))),
            ).fetchall()
            for row in rows:
                row_message_ids = {
                    int(message_id)
                    for message_id in _safe_json_loads(str(row["message_ids_json"] or "[]"), [])
                    if isinstance(message_id, int) or str(message_id).isdigit()
                }
                matched = sorted(row_message_ids & normalized_ids)
                if not matched:
                    continue
                payload = _safe_json_loads(str(row["payload_json"] or "{}"), {})
                if not isinstance(payload, dict):
                    payload = {}
                cleanup = payload.get("telegram_cleanup", {})
                if not isinstance(cleanup, dict):
                    cleanup = {}
                existing = {
                    int(message_id)
                    for message_id in (cleanup.get(target_key) or [])
                    if isinstance(message_id, int) or str(message_id).isdigit()
                }
                cleanup[target_key] = sorted(existing | set(matched))
                cleanup["reason"] = "launch_signal_expired"
                cleanup["updated_at"] = int(now_ts)
                payload["telegram_cleanup"] = cleanup
                conn.execute(
                    "UPDATE signals SET payload_json = ? WHERE id = ?",
                    (_json_dumps(payload), int(row["id"])),
                )
                updated += 1
        return updated

    def prune(self, *, before_ts: int, max_rows: int) -> dict[str, int]:
        """Bound persistent signal history without blocking the live writer for long."""

        cutoff = max(0, int(before_ts))
        row_limit = max(1, int(max_rows))
        launch_cycles_expired = 0
        with self.connect() as conn:
            audit_expired_cursor = conn.execute(
                "DELETE FROM signal_ai_audit WHERE ts < ?",
                (cutoff,),
            )
            audit_expired = max(0, int(audit_expired_cursor.rowcount))
            before = int(conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0])
            expired_cursor = conn.execute("DELETE FROM signals WHERE ts < ?", (cutoff,))
            expired = max(0, int(expired_cursor.rowcount))
            overflow_cursor = conn.execute(
                """
                DELETE FROM signals
                WHERE id NOT IN (
                    SELECT id FROM signals ORDER BY ts DESC, id DESC LIMIT ?
                )
                """,
                (row_limit,),
            )
            overflow = max(0, int(overflow_cursor.rowcount))
            after = int(conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0])
            lifecycle_table = conn.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'launch_lifecycle_cycles'
                """
            ).fetchone()
            if lifecycle_table is not None:
                lifecycle_cursor = conn.execute(
                    """
                    DELETE FROM launch_lifecycle_cycles
                    WHERE status != 'active'
                      AND COALESCE(ended_at, last_window_end) < ?
                    """,
                    (cutoff,),
                )
                launch_cycles_expired = max(0, int(lifecycle_cursor.rowcount))
            conn.execute("PRAGMA optimize")

        checkpointed = 0
        try:
            with closing(sqlite3.connect(str(self.db_path), timeout=15)) as conn:
                checkpoint = conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
                checkpointed = int(checkpoint[1] or 0) if checkpoint else 0
        except sqlite3.Error:
            # Retention succeeded; a busy checkpoint can safely wait for the next run.
            checkpointed = 0
        return {
            "before": before,
            "after": after,
            "expired": expired,
            "overflow": overflow,
            "launch_cycles_expired": launch_cycles_expired,
            "ai_audit_expired": audit_expired,
            "checkpoint_pages": checkpointed,
        }

    def list_signals(
        self,
        *,
        limit: int = 50,
        cursor: int | None = None,
        module: str = "",
        symbol: str = "",
        status: str = "",
        severity: str = "",
        sort_field: str = "id",
        sort_direction: str = "desc",
        start_ts: int | None = None,
        end_ts: int | None = None,
        q: str = "",
        compact: bool = False,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: dict[str, Any] = {"limit": _limit(limit, 50, 200)}
        if cursor:
            clauses.append("id > :cursor" if str(sort_direction).lower() == "asc" and str(sort_field) == "id" else "id < :cursor")
            params["cursor"] = int(cursor)
        if module:
            clauses.append("module = :module")
            params["module"] = str(module).strip().lower()
        if symbol:
            clauses.append("symbol = :symbol")
            params["symbol"] = str(symbol).strip().upper()
        if status:
            clauses.append("status = :status")
            params["status"] = str(status).strip().lower()
        if severity:
            clauses.append("severity = :severity")
            params["severity"] = str(severity).strip().lower()
        if start_ts is not None:
            clauses.append("ts >= :start_ts")
            params["start_ts"] = int(start_ts)
        if end_ts is not None:
            clauses.append("ts <= :end_ts")
            params["end_ts"] = int(end_ts)
        q_text = str(q or "").strip()[:80]
        if q_text:
            clauses.append(
                """
                (
                    symbol LIKE :q_like ESCAPE '\\' COLLATE NOCASE
                    OR coin LIKE :q_like ESCAPE '\\' COLLATE NOCASE
                    OR module LIKE :q_like ESCAPE '\\' COLLATE NOCASE
                    OR template_id LIKE :q_like ESCAPE '\\' COLLATE NOCASE
                    OR signal_type LIKE :q_like ESCAPE '\\' COLLATE NOCASE
                    OR status LIKE :q_like ESCAPE '\\' COLLATE NOCASE
                    OR excerpt LIKE :q_like ESCAPE '\\' COLLATE NOCASE
                    OR title LIKE :q_like ESCAPE '\\' COLLATE NOCASE
                )
                """
            )
            params["q_like"] = _like_pattern(q_text)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        allowed_sort_fields = {"id", "ts", "module", "symbol", "status", "severity", "score"}
        safe_sort_field = str(sort_field or "id")
        if safe_sort_field not in allowed_sort_fields:
            safe_sort_field = "id"
        safe_sort_direction = "ASC" if str(sort_direction or "").lower() == "asc" else "DESC"
        tie_direction = "ASC" if safe_sort_direction == "ASC" else "DESC"
        projection = SIGNAL_LIST_PROJECTION if compact else "*"
        if conn is None:
            with self.connect() as active_conn:
                rows = active_conn.execute(
                    f"SELECT {projection} FROM signals {where} ORDER BY {safe_sort_field} {safe_sort_direction}, id {tie_direction} LIMIT :limit",
                    params,
                ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {projection} FROM signals {where} ORDER BY {safe_sort_field} {safe_sort_direction}, id {tie_direction} LIMIT :limit",
                params,
            ).fetchall()
        items = [_row_to_dict(row) for row in rows]
        return {
            "items": items,
            "next_cursor": items[-1]["id"] if items else None,
            "count": len(items),
        }

    def stats(self, *, window_sec: int = 86400) -> dict[str, Any]:
        with self.connect() as conn:
            return self._stats_from_conn(conn, window_sec=window_sec)

    @staticmethod
    def _stats_from_conn(conn: sqlite3.Connection, *, window_sec: int) -> dict[str, Any]:
        cutoff = int(time.time()) - max(1, int(window_sec or 86400))
        summary = conn.execute(
            "SELECT COUNT(*) AS total, MAX(time) AS latest_at, MAX(ts) AS latest_ts FROM signals WHERE ts >= ?",
            (cutoff,),
        ).fetchone()
        total = int(summary["total"] or 0) if summary else 0
        by_status = {
            str(row["status"]): int(row["count"])
            for row in conn.execute(
                "SELECT status, COUNT(*) AS count FROM signals WHERE ts >= ? GROUP BY status ORDER BY count DESC",
                (cutoff,),
            ).fetchall()
        }
        by_module = {
            str(row["module"]): int(row["count"])
            for row in conn.execute(
                "SELECT module, COUNT(*) AS count FROM signals WHERE ts >= ? GROUP BY module ORDER BY count DESC",
                (cutoff,),
            ).fetchall()
        }
        by_template = {
            str(row["template_id"]): int(row["count"])
            for row in conn.execute(
                "SELECT template_id, COUNT(*) AS count FROM signals WHERE ts >= ? GROUP BY template_id ORDER BY count DESC",
                (cutoff,),
            ).fetchall()
        }
        top_symbols = [
            {"symbol": str(row["symbol"]), "count": int(row["count"])}
            for row in conn.execute(
                """
                SELECT symbol, COUNT(*) AS count
                FROM signals
                WHERE ts >= ? AND symbol != ''
                GROUP BY symbol
                ORDER BY count DESC, symbol ASC
                LIMIT 12
                """,
                (cutoff,),
            ).fetchall()
        ]
        return {
            "total": total,
            "sent": by_status.get("sent", 0),
            "dry_run": by_status.get("dry_run", 0),
            "skipped": by_status.get("skipped", 0),
            "blocked": by_status.get("blocked", 0),
            "failed": by_status.get("failed", 0),
            "by_module": by_module,
            "by_template": by_template,
            "by_status": by_status,
            "top_symbols": top_symbols,
            "window_sec": int(window_sec or 86400),
            "latest_at": str(summary["latest_at"] or "") if summary else "",
            "latest_ts": int(summary["latest_ts"] or 0) if summary else 0,
        }

    def health_summary(self, *, window_sec: int = 86400) -> dict[str, Any]:
        cutoff = int(time.time()) - max(1, int(window_sec or 86400))
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total, MAX(time) AS latest_at, MAX(ts) AS latest_ts FROM signals WHERE ts >= ?",
                (cutoff,),
            ).fetchone()
        return {
            "total": int(row["total"] or 0) if row else 0,
            "latest_at": str(row["latest_at"] or "") if row else "",
            "latest_ts": int(row["latest_ts"] or 0) if row else 0,
            "window_sec": int(window_sec or 86400),
        }

    def stats_with_recent(self, *, window_sec: int = 86400, latest_limit: int = 8) -> dict[str, Any]:
        safe_latest_limit = _limit(latest_limit, 8, 100)
        with self.connect() as conn:
            result = self._stats_from_conn(conn, window_sec=window_sec)
            latest_rows = conn.execute(
                f"SELECT {SIGNAL_LIST_PROJECTION} FROM signals ORDER BY id DESC LIMIT ?",
                (safe_latest_limit,),
            ).fetchall()
        result["latest"] = [_row_to_dict(row) for row in latest_rows]
        return result

    def stats_with_latest(
        self,
        *,
        window_sec: int = 86400,
        status_limit: int = 5,
        latest_limit: int = 8,
        module_limit: int = 8,
    ) -> dict[str, Any]:
        safe_status_limit = _limit(status_limit, 5, 50)
        safe_latest_limit = _limit(latest_limit, 8, 100)
        safe_module_limit = _limit(module_limit, 8, 20)
        with self.connect() as conn:
            result = self._stats_from_conn(conn, window_sec=window_sec)

            def latest_for_status(status: str) -> list[dict[str, Any]]:
                rows = conn.execute(
                    f"SELECT {SIGNAL_LIST_PROJECTION} FROM signals WHERE status = ? ORDER BY id DESC LIMIT ?",
                    (status, safe_status_limit),
                ).fetchall()
                return [_row_to_dict(row) for row in rows]

            latest_rows = conn.execute(
                f"SELECT {SIGNAL_LIST_PROJECTION} FROM signals ORDER BY id DESC LIMIT ?",
                (safe_latest_limit,),
            ).fetchall()
            modules = list(result.get("by_module", {}))[:safe_module_limit]
            latest_by_module = {str(module): [] for module in modules}
            if modules:
                placeholders = ", ".join("?" for _ in modules)
                module_rows = conn.execute(
                    f"""
                    SELECT {SIGNAL_LIST_PROJECTION}
                    FROM signals
                    WHERE id IN (
                        SELECT MAX(id)
                        FROM signals
                        WHERE module IN ({placeholders})
                        GROUP BY module
                    )
                    ORDER BY id DESC
                    """,
                    modules,
                ).fetchall()
                for row in module_rows:
                    item = _row_to_dict(row)
                    latest_by_module[str(item.get("module") or "")] = [item]

            result.update({
                "latest": [_row_to_dict(row) for row in latest_rows],
                "latest_sent": latest_for_status("sent"),
                "latest_failed": latest_for_status("failed"),
                "latest_by_module": latest_by_module,
            })
            return result

    def list_by_symbol(
        self,
        symbol: str,
        *,
        limit: int = 100,
        cursor: int | None = None,
        start_ts: int | None = None,
        end_ts: int | None = None,
    ) -> dict[str, Any]:
        normalized = str(symbol or "").strip().upper()
        if normalized and not normalized.endswith("USDT"):
            normalized = f"{normalized}USDT"
        clauses = ["symbol = :symbol"]
        params: dict[str, Any] = {"symbol": normalized, "limit": _limit(limit, 100, 300)}
        if cursor:
            clauses.append("id < :cursor")
            params["cursor"] = int(cursor)
        if start_ts is not None:
            clauses.append("ts >= :start_ts")
            params["start_ts"] = int(start_ts)
        if end_ts is not None:
            clauses.append("ts <= :end_ts")
            params["end_ts"] = int(end_ts)
        where = " AND ".join(clauses)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM signals WHERE {where} ORDER BY id DESC LIMIT :limit",
                params,
            ).fetchall()
        items = [_row_to_dict(row) for row in rows]
        return {
            "items": items,
            "next_cursor": items[-1]["id"] if items else None,
            "count": len(items),
            "symbol": normalized,
        }

    def list_by_symbols(
        self,
        symbols: list[str],
        *,
        limit_per_symbol: int = 50,
        start_ts: int | None = None,
        end_ts: int | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        normalized_symbols = self._normalize_symbol_list(symbols)
        grouped = {symbol: [] for symbol in normalized_symbols}
        for symbol, items in self.iter_by_symbols(
            normalized_symbols,
            limit_per_symbol=limit_per_symbol,
            start_ts=start_ts,
            end_ts=end_ts,
        ):
            grouped[symbol] = items
        return grouped

    @staticmethod
    def _normalize_symbol_list(symbols: list[str]) -> list[str]:
        normalized_symbols: list[str] = []
        seen: set[str] = set()
        for value in symbols:
            normalized = str(value or "").strip().upper()
            if normalized and not normalized.endswith("USDT"):
                normalized = f"{normalized}USDT"
            if normalized and normalized not in seen:
                seen.add(normalized)
                normalized_symbols.append(normalized)
        return normalized_symbols[:200]

    def iter_by_symbols(
        self,
        symbols: list[str],
        *,
        limit_per_symbol: int = 50,
        start_ts: int | None = None,
        end_ts: int | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> Iterator[tuple[str, list[dict[str, Any]]]]:
        normalized_symbols = self._normalize_symbol_list(symbols)
        if not normalized_symbols:
            return

        clauses = ["symbol = ?"]
        bounds: list[Any] = []
        if start_ts is not None:
            clauses.append("ts >= ?")
            bounds.append(int(start_ts))
        if end_ts is not None:
            clauses.append("ts <= ?")
            bounds.append(int(end_ts))
        safe_limit = _limit(limit_per_symbol, 50, 200)
        where = " AND ".join(clauses)
        projection = ", ".join(SIGNAL_DECISION_COLUMNS)

        def load(active_conn: sqlite3.Connection) -> Iterator[tuple[str, list[dict[str, Any]]]]:
            for symbol in normalized_symbols:
                rows = active_conn.execute(
                    f"""
                    SELECT {projection}
                    FROM signals INDEXED BY idx_signals_symbol_id
                    WHERE {where}
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    [symbol, *bounds, safe_limit],
                )
                items = [_row_to_decision_dict(row) for row in rows]
                if items:
                    yield symbol, items

        if conn is None:
            with self.connect() as active_conn:
                yield from load(active_conn)
        else:
            yield from load(conn)

    def stats_by_symbol(
        self,
        symbol: str,
        *,
        start_ts: int | None = None,
        end_ts: int | None = None,
    ) -> dict[str, Any]:
        normalized = str(symbol or "").strip().upper()
        if normalized and not normalized.endswith("USDT"):
            normalized = f"{normalized}USDT"
        clauses = ["symbol = :symbol"]
        params: dict[str, Any] = {"symbol": normalized}
        if start_ts is not None:
            clauses.append("ts >= :start_ts")
            params["start_ts"] = int(start_ts)
        if end_ts is not None:
            clauses.append("ts <= :end_ts")
            params["end_ts"] = int(end_ts)
        where = " AND ".join(clauses)
        with self.connect() as conn:
            total = int(conn.execute(f"SELECT COUNT(*) FROM signals WHERE {where}", params).fetchone()[0])
            by_status = {
                str(row["status"]): int(row["count"])
                for row in conn.execute(
                    f"SELECT status, COUNT(*) AS count FROM signals WHERE {where} GROUP BY status ORDER BY count DESC",
                    params,
                ).fetchall()
            }
            by_module = {
                str(row["module"]): int(row["count"])
                for row in conn.execute(
                    f"SELECT module, COUNT(*) AS count FROM signals WHERE {where} GROUP BY module ORDER BY count DESC",
                    params,
                ).fetchall()
            }
            bounds = conn.execute(
                f"SELECT MIN(time) AS first_at, MAX(time) AS latest_at, MIN(ts) AS first_ts, MAX(ts) AS latest_ts FROM signals WHERE {where}",
                params,
            ).fetchone()
        return {
            "symbol": normalized,
            "coin": _coin_from_symbol(normalized),
            "total": total,
            "sent": by_status.get("sent", 0),
            "dry_run": by_status.get("dry_run", 0),
            "skipped": by_status.get("skipped", 0),
            "blocked": by_status.get("blocked", 0),
            "failed": by_status.get("failed", 0),
            "by_module": by_module,
            "by_status": by_status,
            "first_at": str(bounds["first_at"] or "") if bounds else "",
            "latest_at": str(bounds["latest_at"] or "") if bounds else "",
            "first_ts": int(bounds["first_ts"] or 0) if bounds else 0,
            "latest_ts": int(bounds["latest_ts"] or 0) if bounds else 0,
        }

    def search_symbols(
        self,
        q: str = "",
        *,
        limit: int = 20,
        start_ts: int | None = None,
        end_ts: int | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        clauses = ["symbol != ''"]
        params: dict[str, Any] = {"limit": _limit(limit, 20, 100)}
        if start_ts is not None:
            clauses.append("ts >= :start_ts")
            params["start_ts"] = int(start_ts)
        if end_ts is not None:
            clauses.append("ts <= :end_ts")
            params["end_ts"] = int(end_ts)
        q_text = str(q or "").strip()[:40]
        if q_text:
            clauses.append("(symbol LIKE :q_like ESCAPE '\\' COLLATE NOCASE OR coin LIKE :q_like ESCAPE '\\' COLLATE NOCASE)")
            params["q_like"] = _like_pattern(q_text)
        where = " AND ".join(clauses)
        if conn is None:
            with self.connect() as active_conn:
                rows = active_conn.execute(
                    f"""
                    SELECT
                        symbol,
                        coin,
                        COUNT(*) AS count,
                        MAX(time) AS latest_at,
                        COUNT(DISTINCT module) AS module_count,
                        SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count
                    FROM signals
                    WHERE {where}
                    GROUP BY symbol, coin
                    ORDER BY count DESC, latest_at DESC, symbol ASC
                    LIMIT :limit
                    """,
                    params,
                ).fetchall()
        else:
            rows = conn.execute(
                f"""
                SELECT
                    symbol,
                    coin,
                    COUNT(*) AS count,
                    MAX(time) AS latest_at,
                    COUNT(DISTINCT module) AS module_count,
                    SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed_count
                FROM signals
                WHERE {where}
                GROUP BY symbol, coin
                ORDER BY count DESC, latest_at DESC, symbol ASC
                LIMIT :limit
                """,
                params,
            ).fetchall()
        return [
            {
                "symbol": str(row["symbol"] or ""),
                "coin": str(row["coin"] or _coin_from_symbol(str(row["symbol"] or ""))),
                "count": int(row["count"] or 0),
                "latest_at": str(row["latest_at"] or ""),
                "module_count": int(row["module_count"] or 0),
                "failed_count": int(row["failed_count"] or 0),
            }
            for row in rows
        ]

    def list_timeline(
        self,
        *,
        symbol: str = "",
        limit: int = 100,
        cursor: int | None = None,
        start_ts: int | None = None,
        end_ts: int | None = None,
        module: str = "",
        status: str = "",
        q: str = "",
        sort_direction: str = "desc",
        compact: bool = False,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        normalized = str(symbol or "").strip().upper()
        if normalized and not normalized.endswith("USDT"):
            normalized = f"{normalized}USDT"
        direction = "ASC" if str(sort_direction or "").lower() == "asc" else "DESC"
        cursor_op = ">" if direction == "ASC" else "<"
        clauses: list[str] = []
        params: dict[str, Any] = {"limit": _limit(limit, 100, 300)}
        if normalized:
            clauses.append("symbol = :symbol")
            params["symbol"] = normalized
        if cursor:
            clauses.append(f"id {cursor_op} :cursor")
            params["cursor"] = int(cursor)
        if start_ts is not None:
            clauses.append("ts >= :start_ts")
            params["start_ts"] = int(start_ts)
        if end_ts is not None:
            clauses.append("ts <= :end_ts")
            params["end_ts"] = int(end_ts)
        if module:
            clauses.append("module = :module")
            params["module"] = str(module).strip().lower()
        if status:
            clauses.append("status = :status")
            params["status"] = str(status).strip().lower()
        q_text = str(q or "").strip()[:80]
        if q_text:
            clauses.append(
                """
                (
                    symbol LIKE :q_like ESCAPE '\\' COLLATE NOCASE
                    OR coin LIKE :q_like ESCAPE '\\' COLLATE NOCASE
                    OR module LIKE :q_like ESCAPE '\\' COLLATE NOCASE
                    OR template_id LIKE :q_like ESCAPE '\\' COLLATE NOCASE
                    OR signal_type LIKE :q_like ESCAPE '\\' COLLATE NOCASE
                    OR status LIKE :q_like ESCAPE '\\' COLLATE NOCASE
                    OR excerpt LIKE :q_like ESCAPE '\\' COLLATE NOCASE
                    OR title LIKE :q_like ESCAPE '\\' COLLATE NOCASE
                )
                """
            )
            params["q_like"] = _like_pattern(q_text)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        projection = SIGNAL_LIST_PROJECTION if compact else "*"
        if conn is None:
            with self.connect() as active_conn:
                rows = active_conn.execute(
                    f"SELECT {projection} FROM signals {where} ORDER BY ts {direction}, id {direction} LIMIT :limit",
                    params,
                ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {projection} FROM signals {where} ORDER BY ts {direction}, id {direction} LIMIT :limit",
                params,
            ).fetchall()
        items = [_row_to_dict(row) for row in rows]
        return {
            "items": items,
            "next_cursor": items[-1]["id"] if items else None,
            "count": len(items),
            "symbol": normalized,
        }

    def timeline_stats(
        self,
        *,
        symbol: str = "",
        start_ts: int | None = None,
        end_ts: int | None = None,
        module: str = "",
        status: str = "",
        q: str = "",
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        normalized = str(symbol or "").strip().upper()
        if normalized and not normalized.endswith("USDT"):
            normalized = f"{normalized}USDT"
        clauses: list[str] = []
        params: dict[str, Any] = {}
        if normalized:
            clauses.append("symbol = :symbol")
            params["symbol"] = normalized
        if start_ts is not None:
            clauses.append("ts >= :start_ts")
            params["start_ts"] = int(start_ts)
        if end_ts is not None:
            clauses.append("ts <= :end_ts")
            params["end_ts"] = int(end_ts)
        if module:
            clauses.append("module = :module")
            params["module"] = str(module).strip().lower()
        if status:
            clauses.append("status = :status")
            params["status"] = str(status).strip().lower()
        q_text = str(q or "").strip()[:80]
        if q_text:
            clauses.append(
                """
                (
                    symbol LIKE :q_like ESCAPE '\\' COLLATE NOCASE
                    OR coin LIKE :q_like ESCAPE '\\' COLLATE NOCASE
                    OR module LIKE :q_like ESCAPE '\\' COLLATE NOCASE
                    OR template_id LIKE :q_like ESCAPE '\\' COLLATE NOCASE
                    OR signal_type LIKE :q_like ESCAPE '\\' COLLATE NOCASE
                    OR status LIKE :q_like ESCAPE '\\' COLLATE NOCASE
                    OR excerpt LIKE :q_like ESCAPE '\\' COLLATE NOCASE
                    OR title LIKE :q_like ESCAPE '\\' COLLATE NOCASE
                )
                """
            )
            params["q_like"] = _like_pattern(q_text)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        def load(active_conn: sqlite3.Connection) -> tuple[int, dict[str, int], dict[str, int], sqlite3.Row | None]:
            total = int(active_conn.execute(f"SELECT COUNT(*) FROM signals {where}", params).fetchone()[0])
            by_status = {
                str(row["status"]): int(row["count"])
                for row in active_conn.execute(
                    f"SELECT status, COUNT(*) AS count FROM signals {where} GROUP BY status ORDER BY count DESC",
                    params,
                ).fetchall()
            }
            by_module = {
                str(row["module"]): int(row["count"])
                for row in active_conn.execute(
                    f"SELECT module, COUNT(*) AS count FROM signals {where} GROUP BY module ORDER BY count DESC",
                    params,
                ).fetchall()
            }
            bounds = active_conn.execute(
                f"SELECT MIN(time) AS first_at, MAX(time) AS latest_at, MIN(ts) AS first_ts, MAX(ts) AS latest_ts FROM signals {where}",
                params,
            ).fetchone()
            return total, by_status, by_module, bounds

        if conn is None:
            with self.connect() as active_conn:
                total, by_status, by_module, bounds = load(active_conn)
        else:
            total, by_status, by_module, bounds = load(conn)
        return {
            "symbol": normalized,
            "coin": _coin_from_symbol(normalized),
            "total": total,
            "sent": by_status.get("sent", 0),
            "dry_run": by_status.get("dry_run", 0),
            "skipped": by_status.get("skipped", 0),
            "blocked": by_status.get("blocked", 0),
            "failed": by_status.get("failed", 0),
            "by_module": by_module,
            "by_status": by_status,
            "first_at": str(bounds["first_at"] or "") if bounds else "",
            "latest_at": str(bounds["latest_at"] or "") if bounds else "",
            "first_ts": int(bounds["first_ts"] or 0) if bounds else 0,
            "latest_ts": int(bounds["latest_ts"] or 0) if bounds else 0,
        }

    def signal_detail(
        self,
        signal_id: int | str,
        *,
        compact: bool = False,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        reference = str(signal_id or "").strip()
        is_numeric = reference.isdigit()
        where = "id = ?" if is_numeric else "public_ref = ?"
        value: int | str = int(reference) if is_numeric else reference
        projection = SIGNAL_LIST_PROJECTION if compact else "*"
        if conn is None:
            with self.connect() as active_conn:
                row = active_conn.execute(f"SELECT {projection} FROM signals WHERE {where}", (value,)).fetchone()
        else:
            row = conn.execute(f"SELECT {projection} FROM signals WHERE {where}", (value,)).fetchone()
        return _row_to_dict(row) if row else None

    def data_quality_report(self) -> dict[str, Any]:
        """Audit legacy signal quality without mutating the database."""

        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, symbol, text_html, score, ingest_mode, quality_status FROM signals ORDER BY id"
            ).fetchall()
        artifact_ids: list[int] = []
        recoverable_scores = 0
        structured = 0
        ready = 0
        for row in rows:
            symbol = str(row["symbol"] or "").upper()
            text = str(row["text_html"] or "")
            extracted = extract_symbols_from_text(text)
            if symbol.startswith("3A") and "%3A" in text.upper() and symbol not in extracted:
                artifact_ids.append(int(row["id"]))
                continue
            if row["score"] is None and symbol:
                if _extract_symbol_score(text, symbol, symbol_count=max(1, len(extracted))) is not None:
                    recoverable_scores += 1
            structured += int(str(row["ingest_mode"] or "") == "structured")
            ready += int(str(row["quality_status"] or "") == "ready")
        total = len(rows)
        return {
            "status": "attention" if artifact_ids or recoverable_scores else "ok",
            "total": total,
            "artifact_rows": len(artifact_ids),
            "artifact_ids": artifact_ids[:100],
            "recoverable_scores": recoverable_scores,
            "structured_rows": structured,
            "ready_rows": ready,
            "ready_ratio": round(ready / total, 4) if total else 0.0,
        }

    def repair_legacy_signals(self, *, apply: bool = False) -> dict[str, Any]:
        """Remove URL-derived symbols and recover legacy scores with an online backup."""

        report = self.data_quality_report()
        report.update({"applied": False, "deleted": 0, "scores_recovered": 0, "backup_path": ""})
        if not apply:
            return report

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup_path = self.db_path.with_name(f"{self.db_path.name}.pre-signal-repair-{stamp}.bak")
        with self.connect() as source, closing(sqlite3.connect(backup_path)) as backup:
            source.backup(backup)
            rows = source.execute(
                "SELECT id, symbol, text_html, score FROM signals ORDER BY id"
            ).fetchall()
            artifact_ids: list[int] = []
            recovered = 0
            for row in rows:
                row_id = int(row["id"])
                symbol = str(row["symbol"] or "").upper()
                text = str(row["text_html"] or "")
                extracted = extract_symbols_from_text(text)
                if symbol.startswith("3A") and "%3A" in text.upper() and symbol not in extracted:
                    artifact_ids.append(row_id)
                    continue
                if row["score"] is None and symbol:
                    recovered_score = _extract_symbol_score(text, symbol, symbol_count=max(1, len(extracted)))
                    if recovered_score is not None:
                        source.execute(
                            "UPDATE signals SET score = ?, ingest_mode = 'legacy_repaired' WHERE id = ?",
                            (recovered_score, row_id),
                        )
                        recovered += 1
            if artifact_ids:
                placeholders = ",".join("?" for _ in artifact_ids)
                source.execute(f"DELETE FROM signals WHERE id IN ({placeholders})", artifact_ids)
            source.commit()
        report.update({
            "applied": True,
            "deleted": len(artifact_ids),
            "scores_recovered": recovered,
            "backup_path": str(backup_path),
            "after": self.data_quality_report(),
        })
        return report


def append_from_push(
    settings: Settings,
    *,
    template_id: str,
    dedup_key: str,
    status: str,
    sent: bool,
    text: str,
    ts: int | None = None,
    topic_id: str = "",
    message_ids: list[int] | None = None,
    reply_to_message_id: int | None = None,
    structured_records: list[dict[str, Any]] | None = None,
) -> int:
    store = SignalEventStore(getattr(settings, "signal_events_db_path", DEFAULT_SIGNAL_DB_PATH))
    try:
        return store.append_from_push(
            template_id=template_id,
            dedup_key=dedup_key,
            status=status,
            sent=sent,
            text=text,
            ts=ts,
            topic_id=topic_id,
            message_ids=message_ids,
            reply_to_message_id=reply_to_message_id,
            structured_records=structured_records,
        )
    except Exception as exc:
        print(f"[signal_store] append failed {type(exc).__name__}: {exc}", file=sys.stderr)
        return 0
