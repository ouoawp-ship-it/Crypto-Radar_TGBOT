from __future__ import annotations

import json
import hashlib
import re
import sqlite3
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import BASE_DIR, Settings
from .symbol_dossier import clean_signal_text, extract_symbols_from_text, signal_event_template_label


DEFAULT_SIGNAL_DB_PATH = BASE_DIR / "data" / "signals.db"
SIGNAL_STORE_SCHEMA_VERSION = 1
SIGNAL_COLUMNS = (
    "id",
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
)
SIGNAL_COMPAT_DEFAULTS = {
    "id": "NULL",
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
}


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
    match = re.search(r"(?:分数|评分|score)\s*[:：]\s*(-?\d+(?:\.\d+)?)", str(text or ""), flags=re.IGNORECASE)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _module_for_template(template_id: str) -> str:
    value = str(template_id or "").upper()
    if "FUNDING" in value:
        return "funding"
    if "FLOW" in value:
        return "flow"
    if "STRUCTURE_REVIEW" in value:
        return "structure_review"
    if "STRUCTURE" in value:
        return "structure"
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


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    item = {key: row[key] for key in SIGNAL_COLUMNS}
    item["sent"] = bool(item.get("sent"))
    item["message_ids"] = _safe_json_loads(str(item.pop("message_ids_json") or "[]"), [])
    item["payload"] = _safe_json_loads(str(item.pop("payload_json") or "{}"), {})
    return item


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
            conn.execute("PRAGMA busy_timeout=15000")
            self._ensure_schema(conn)
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
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
                error TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_symbol_ts ON signals(symbol, ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_module_ts ON signals(module, ts DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_template_ts ON signals(template_id, ts DESC)")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_signals_dedup_symbol ON signals(dedup_key, symbol)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS signal_store_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self._ensure_compat_views(conn)
        conn.execute(
            "INSERT OR REPLACE INTO signal_store_meta(key, value) VALUES('schema_version', ?)",
            (str(SIGNAL_STORE_SCHEMA_VERSION),),
        )

    def _ensure_compat_views(self, conn: sqlite3.Connection) -> None:
        existing = conn.execute(
            "SELECT type FROM sqlite_master WHERE name = 'signal_events' LIMIT 1"
        ).fetchone()
        if existing:
            conn.execute(
                "INSERT OR REPLACE INTO signal_store_meta(key, value) VALUES('signal_events_object_type', ?)",
                (str(existing["type"]),),
            )
            return
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
    ) -> int:
        now = int(ts or time.time())
        symbols = extract_symbols_from_text(text)
        if not symbols:
            symbols = [""]
        clean_excerpt = clean_signal_text(text)[:1200]
        title = _clean_title(text)
        safe_dedup_key = str(dedup_key or "").strip()
        if not safe_dedup_key:
            digest = hashlib.sha1(str(text or "").encode("utf-8", errors="ignore")).hexdigest()[:16]
            safe_dedup_key = f"{template_id or 'telegram'}:{now}:{digest}"
        module = _module_for_template(template_id)
        signal_type = signal_event_template_label(template_id)
        stage = _extract_stage(text)
        score = _extract_score(text)
        severity = _severity_for_status(status, text)
        message_ids_json = _json_dumps([int(item) for item in (message_ids or []) if isinstance(item, int)])
        payload = {
            "source": "telegram_push",
            "schema_version": SIGNAL_STORE_SCHEMA_VERSION,
            "reason": str(status or ""),
        }
        payload_json = _json_dumps(payload)
        rows = []
        for symbol in symbols:
            normalized_symbol = str(symbol or "").upper()
            rows.append(
                {
                    "ts": now,
                    "time": _utc_time_text(now),
                    "module": module,
                    "template_id": str(template_id or ""),
                    "signal_type": str(signal_type or template_id or ""),
                    "symbol": normalized_symbol,
                    "coin": _coin_from_symbol(normalized_symbol),
                    "stage": stage,
                    "severity": severity,
                    "score": score,
                    "title": title,
                    "excerpt": clean_excerpt,
                    "text_html": str(text or "")[:20000],
                    "dedup_key": safe_dedup_key,
                    "status": str(status or ""),
                    "sent": 1 if sent else 0,
                    "topic_id": str(topic_id or ""),
                    "message_ids_json": message_ids_json,
                    "reply_to_message_id": int(reply_to_message_id or 0),
                    "payload_json": payload_json,
                    "error": "" if str(status or "").lower() not in {"failed", "blocked"} else clean_excerpt[:300],
                }
            )
        with self.connect() as conn:
            for row in rows:
                conn.execute(
                    """
                    INSERT INTO signals (
                        ts, time, module, template_id, signal_type, symbol, coin, stage, severity, score,
                        title, excerpt, text_html, dedup_key, status, sent, topic_id, message_ids_json,
                        reply_to_message_id, payload_json, error
                    ) VALUES (
                        :ts, :time, :module, :template_id, :signal_type, :symbol, :coin, :stage, :severity, :score,
                        :title, :excerpt, :text_html, :dedup_key, :status, :sent, :topic_id, :message_ids_json,
                        :reply_to_message_id, :payload_json, :error
                    )
                    ON CONFLICT(dedup_key, symbol) DO UPDATE SET
                        ts=excluded.ts,
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
                        error=excluded.error
                    """,
                    row,
                )
        return len(rows)

    def list_signals(
        self,
        *,
        limit: int = 50,
        cursor: int | None = None,
        module: str = "",
        symbol: str = "",
        status: str = "",
        severity: str = "",
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: dict[str, Any] = {"limit": _limit(limit, 50, 200)}
        if cursor:
            clauses.append("id < :cursor")
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
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM signals {where} ORDER BY id DESC LIMIT :limit",
                params,
            ).fetchall()
        items = [_row_to_dict(row) for row in rows]
        return {
            "items": items,
            "next_cursor": items[-1]["id"] if items else None,
            "count": len(items),
        }

    def latest_after(self, *, after_id: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM signals WHERE id > ? ORDER BY id ASC LIMIT ?",
                (max(0, int(after_id or 0)), _limit(limit, 100, 300)),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def stats(self, *, window_sec: int = 86400) -> dict[str, Any]:
        now = int(time.time())
        cutoff = now - max(1, int(window_sec or 86400))
        with self.connect() as conn:
            total = int(conn.execute("SELECT COUNT(*) FROM signals WHERE ts >= ?", (cutoff,)).fetchone()[0])
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
        }

    def symbol_timeline(self, symbol: str, *, limit: int = 100) -> list[dict[str, Any]]:
        normalized = str(symbol or "").strip().upper()
        if normalized and not normalized.endswith("USDT"):
            normalized = f"{normalized}USDT"
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM signals WHERE symbol = ? ORDER BY id DESC LIMIT ?",
                (normalized, _limit(limit, 100, 300)),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def signal_detail(self, signal_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM signals WHERE id = ?", (int(signal_id),)).fetchone()
        return _row_to_dict(row) if row else None


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
        )
    except Exception as exc:
        print(f"[signal_store] append failed {type(exc).__name__}: {exc}", file=sys.stderr)
        return 0
