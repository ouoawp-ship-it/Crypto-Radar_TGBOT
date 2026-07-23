from __future__ import annotations

import json
import hashlib
import re
import sqlite3
import sys
import time
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import BASE_DIR, Settings
from .symbol_dossier import clean_signal_text, extract_symbols_from_text, signal_event_template_label


DEFAULT_SIGNAL_DB_PATH = BASE_DIR / "data" / "signals.db"
SIGNAL_STORE_SCHEMA_VERSION = 6
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
    "symbol", "coin", "score", "total_score", "stage", "category", "kind", "state",
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
})


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _safe_json_loads(value: str, default: Any) -> Any:
    try:
        return json.loads(value)
    except Exception:
        return default


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


def signal_public_targets(dedup_key: str, text: str) -> list[dict[str, str]]:
    symbols = extract_symbols_from_text(text) or [""]
    return [
        {"public_ref": signal_public_ref(dedup_key, symbol), "symbol": str(symbol or "").upper()}
        for symbol in symbols
    ]


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
                if normalized_symbol and not re.fullmatch(r"[A-Z0-9]{2,24}USDT", normalized_symbol):
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
        return len(rows)

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

    def latest_after(
        self,
        *,
        after_id: int = 0,
        limit: int = 100,
        compact: bool = False,
    ) -> list[dict[str, Any]]:
        projection = SIGNAL_LIST_PROJECTION if compact else "*"
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT {projection} FROM signals WHERE id > ? ORDER BY id ASC LIMIT ?",
                (max(0, int(after_id or 0)), _limit(limit, 100, 300)),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

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

    def symbol_timeline(
        self,
        symbol: str,
        *,
        limit: int = 100,
        compact: bool = False,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        normalized = str(symbol or "").strip().upper()
        if normalized and not normalized.endswith("USDT"):
            normalized = f"{normalized}USDT"
        projection = SIGNAL_LIST_PROJECTION if compact else "*"
        if conn is None:
            with self.connect() as active_conn:
                rows = active_conn.execute(
                    f"SELECT {projection} FROM signals WHERE symbol = ? ORDER BY id DESC LIMIT ?",
                    (normalized, _limit(limit, 100, 300)),
                ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT {projection} FROM signals WHERE symbol = ? ORDER BY id DESC LIMIT ?",
                (normalized, _limit(limit, 100, 300)),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

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

    def intelligence_events(
        self,
        *,
        start_ts: int,
        end_ts: int,
        limit: int = 2000,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        """Return the bounded, sent-only fact set used by public signal intelligence."""
        safe_limit = max(1, min(5000, int(limit or 2000)))
        sql = (
            "SELECT * FROM signals "
            "WHERE status = 'sent' AND symbol <> '' AND ts >= ? AND ts <= ? "
            "ORDER BY ts DESC, id DESC LIMIT ?"
        )
        params = (int(start_ts), int(end_ts), safe_limit)
        if conn is None:
            with self.connect() as active_conn:
                rows = active_conn.execute(sql, params).fetchall()
        else:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_dict(row) for row in rows]

    def events_after_id(self, last_id: int = 0, *, limit: int = 50) -> list[dict[str, Any]]:
        """Return a bounded ascending stream projection without private payload fields."""
        safe_id = max(0, int(last_id or 0))
        safe_limit = max(1, min(200, int(limit or 50)))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, public_ref, ts, time, module, signal_type, symbol, status, score, stage, excerpt
                FROM signals
                WHERE id > ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (safe_id, safe_limit),
            ).fetchall()
        return [{key: row[key] for key in row.keys()} for row in rows]

    def latest_event_id(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT MAX(id) AS value FROM signals").fetchone()
        return int(row["value"] or 0) if row else 0

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
