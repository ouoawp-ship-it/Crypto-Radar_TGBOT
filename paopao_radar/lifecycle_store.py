from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import BASE_DIR
from .web_services.api_core import normalize_symbol_filter, redact_api_payload


DEFAULT_LIFECYCLE_DB_PATH = BASE_DIR / "data" / "lifecycle.db"
SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,24}USDT$")
PUBLIC_SENSITIVE_TEXT_RE = re.compile(
    r"(?i)(WEB_ADMIN_TOKEN|WEB_SESSION_SECRET|WEB_ADMIN_PASSWORD_HASH|BOT_TOKEN|TELEGRAM|"
    r"Authorization|Cookie|chat_id|topic_id|message_id|dedup_key|api_key|payload_json|"
    r"text_html|database|/home/ubuntu)"
)
JSON_FIELDS = {"exchange_context_json", "metrics_json", "reasons_json"}
EVENT_JSON_FIELDS = {"metrics_json", "reasons_json", "exchange_context_json"}
SNAPSHOT_JSON_FIELDS = {"metrics_json"}


def utc_iso(ts: int | float | None = None) -> str:
    value = int(time.time() if ts is None else ts)
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def normalize_lifecycle_symbol(value: Any) -> str:
    symbol = normalize_symbol_filter(value).get("symbol", "")
    return symbol if SYMBOL_RE.fullmatch(symbol) else ""


def coin_from_symbol(symbol: str) -> str:
    text = str(symbol or "").upper()
    return text[:-4] if text.endswith("USDT") else text


def json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, separators=(",", ":"))


def json_loads(value: Any, default: Any) -> Any:
    try:
        if value is None or value == "":
            return default
        return json.loads(str(value))
    except Exception:
        return default


def safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None or value == "":
            return int(default)
        return int(float(str(value)))
    except (TypeError, ValueError):
        return int(default)


def pct_change(current: Any, first: Any) -> float | None:
    current_value = safe_float(current)
    first_value = safe_float(first)
    if current_value is None or first_value is None or first_value == 0:
        return None
    return round((current_value - first_value) / first_value * 100.0, 4)


def _row_to_dict(row: sqlite3.Row | None, *, json_fields: set[str]) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    for key in list(json_fields):
        if key in item:
            public_key = key.removesuffix("_json")
            item[public_key] = json_loads(item.pop(key), [] if key == "reasons_json" else {})
    return item


@dataclass
class LifecycleStore:
    db_path: Path = DEFAULT_LIFECYCLE_DB_PATH

    def __post_init__(self) -> None:
        self.db_path = Path(self.db_path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=15000")
            self.ensure_schema(conn)
            yield conn
            conn.commit()
        finally:
            conn.close()

    def ensure_schema(self, conn: sqlite3.Connection | None = None) -> None:
        own_conn = conn is None
        if own_conn:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path), timeout=15)
            conn.row_factory = sqlite3.Row
        assert conn is not None
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS signal_lifecycles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL UNIQUE,
                    first_signal_id INTEGER,
                    first_signal_at TEXT NOT NULL,
                    first_signal_module TEXT,
                    first_signal_template TEXT,
                    first_signal_type TEXT,
                    first_signal_level TEXT,
                    first_signal_level_rank INTEGER,
                    first_signal_score REAL,
                    first_signal_excerpt TEXT,
                    first_price REAL,
                    first_market_cap_usd REAL,
                    first_volume_15m REAL,
                    first_quote_volume_15m REAL,
                    first_oi REAL,
                    first_oi_value_usdt REAL,
                    first_futures_cvd_15m REAL,
                    first_spot_cvd_15m REAL,
                    first_funding_rate REAL,
                    current_state TEXT NOT NULL,
                    highest_level TEXT,
                    highest_level_rank INTEGER,
                    lifecycle_score REAL DEFAULT 0,
                    risk_score REAL DEFAULT 0,
                    latest_signal_id INTEGER,
                    latest_signal_at TEXT,
                    latest_price REAL,
                    latest_market_cap_usd REAL,
                    latest_oi REAL,
                    latest_oi_value_usdt REAL,
                    latest_futures_cvd_15m REAL,
                    latest_spot_cvd_15m REAL,
                    latest_funding_rate REAL,
                    price_change_from_first_pct REAL,
                    market_cap_change_from_first_pct REAL,
                    oi_change_from_first_pct REAL,
                    oi_value_change_from_first_pct REAL,
                    futures_cvd_change_from_first REAL,
                    spot_cvd_change_from_first REAL,
                    exchange_context_json TEXT,
                    metrics_json TEXT,
                    reasons_json TEXT,
                    is_active INTEGER DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    closed_at TEXT,
                    close_reason TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS lifecycle_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lifecycle_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    event_time TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_level TEXT,
                    event_level_rank INTEGER,
                    signal_id INTEGER,
                    source_module TEXT,
                    source_template TEXT,
                    source_excerpt TEXT,
                    previous_state TEXT,
                    new_state TEXT,
                    price REAL,
                    price_change_from_first_pct REAL,
                    volume_change_pct REAL,
                    quote_volume_change_pct REAL,
                    oi_change_pct REAL,
                    oi_value_change_pct REAL,
                    futures_cvd_delta REAL,
                    spot_cvd_delta REAL,
                    funding_rate REAL,
                    event_score REAL,
                    risk_score REAL,
                    metrics_json TEXT,
                    reasons_json TEXT,
                    exchange_context_json TEXT,
                    dedup_key TEXT NOT NULL UNIQUE,
                    pushed_to_telegram INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS lifecycle_metric_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    snapshot_time TEXT NOT NULL,
                    price REAL,
                    volume REAL,
                    quote_volume REAL,
                    oi REAL,
                    oi_value_usdt REAL,
                    futures_cvd_delta REAL,
                    spot_cvd_delta REAL,
                    funding_rate REAL,
                    market_cap_usd REAL,
                    metrics_json TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(symbol, timeframe, snapshot_time)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_signal_lifecycles_state ON signal_lifecycles(current_state)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_signal_lifecycles_updated ON signal_lifecycles(updated_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_lifecycle_events_symbol ON lifecycle_events(symbol, event_time DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_lifecycle_events_type ON lifecycle_events(event_type)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_lifecycle_snapshots_symbol ON lifecycle_metric_snapshots(symbol, snapshot_time DESC)")
            if own_conn:
                conn.commit()
        finally:
            if own_conn:
                conn.close()

    def get_lifecycle(self, symbol: str) -> dict[str, Any] | None:
        normalized = normalize_lifecycle_symbol(symbol)
        if not normalized:
            return None
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM signal_lifecycles WHERE symbol = ?", (normalized,)).fetchone()
        return _row_to_dict(row, json_fields=JSON_FIELDS)

    def create_lifecycle(self, values: dict[str, Any], *, dry_run: bool = False) -> tuple[dict[str, Any], bool]:
        symbol = normalize_lifecycle_symbol(values.get("symbol"))
        if not symbol:
            raise ValueError("invalid lifecycle symbol")
        existing = self.get_lifecycle(symbol)
        if existing:
            return existing, False
        now = utc_iso()
        row = dict(values)
        row.update({
            "symbol": symbol,
            "exchange_context_json": json_dumps(row.pop("exchange_context", row.get("exchange_context_json", {}))),
            "metrics_json": json_dumps(row.pop("metrics", row.get("metrics_json", {}))),
            "reasons_json": json_dumps(row.pop("reasons", row.get("reasons_json", []))),
            "created_at": row.get("created_at") or now,
            "updated_at": row.get("updated_at") or now,
        })
        if dry_run:
            fake = dict(row)
            fake["id"] = 0
            return {
                **fake,
                "exchange_context": json_loads(fake.get("exchange_context_json"), {}),
                "metrics": json_loads(fake.get("metrics_json"), {}),
                "reasons": json_loads(fake.get("reasons_json"), []),
            }, True
        columns = [
            "symbol", "first_signal_id", "first_signal_at", "first_signal_module", "first_signal_template",
            "first_signal_type", "first_signal_level", "first_signal_level_rank", "first_signal_score",
            "first_signal_excerpt", "first_price", "first_market_cap_usd", "first_volume_15m",
            "first_quote_volume_15m", "first_oi", "first_oi_value_usdt", "first_futures_cvd_15m",
            "first_spot_cvd_15m", "first_funding_rate", "current_state", "highest_level",
            "highest_level_rank", "lifecycle_score", "risk_score", "latest_signal_id", "latest_signal_at",
            "latest_price", "latest_market_cap_usd", "latest_oi", "latest_oi_value_usdt",
            "latest_futures_cvd_15m", "latest_spot_cvd_15m", "latest_funding_rate",
            "price_change_from_first_pct", "market_cap_change_from_first_pct", "oi_change_from_first_pct",
            "oi_value_change_from_first_pct", "futures_cvd_change_from_first", "spot_cvd_change_from_first",
            "exchange_context_json", "metrics_json", "reasons_json", "is_active", "created_at", "updated_at",
            "closed_at", "close_reason",
        ]
        params = {key: row.get(key) for key in columns}
        with self.connect() as conn:
            placeholders = ", ".join(f":{key}" for key in columns)
            conn.execute(
                f"INSERT OR IGNORE INTO signal_lifecycles ({', '.join(columns)}) VALUES ({placeholders})",
                params,
            )
        created = self.get_lifecycle(symbol)
        return created or {}, True

    def update_lifecycle(self, symbol: str, values: dict[str, Any], *, dry_run: bool = False) -> dict[str, Any] | None:
        normalized = normalize_lifecycle_symbol(symbol)
        if not normalized:
            return None
        updates = dict(values)
        if "exchange_context" in updates:
            updates["exchange_context_json"] = json_dumps(updates.pop("exchange_context"))
        if "metrics" in updates:
            updates["metrics_json"] = json_dumps(updates.pop("metrics"))
        if "reasons" in updates:
            updates["reasons_json"] = json_dumps(updates.pop("reasons"))
        updates["updated_at"] = utc_iso()
        allowed = {
            "current_state", "highest_level", "highest_level_rank", "lifecycle_score", "risk_score",
            "latest_signal_id", "latest_signal_at", "latest_price", "latest_market_cap_usd", "latest_oi",
            "latest_oi_value_usdt", "latest_futures_cvd_15m", "latest_spot_cvd_15m", "latest_funding_rate",
            "price_change_from_first_pct", "market_cap_change_from_first_pct", "oi_change_from_first_pct",
            "oi_value_change_from_first_pct", "futures_cvd_change_from_first", "spot_cvd_change_from_first",
            "exchange_context_json", "metrics_json", "reasons_json", "is_active", "updated_at", "closed_at",
            "close_reason",
        }
        updates = {key: value for key, value in updates.items() if key in allowed}
        if dry_run:
            current = self.get_lifecycle(normalized) or {}
            current.update(updates)
            return current
        if not updates:
            return self.get_lifecycle(normalized)
        assignments = ", ".join(f"{key} = :{key}" for key in updates)
        params = {**updates, "symbol": normalized}
        with self.connect() as conn:
            conn.execute(f"UPDATE signal_lifecycles SET {assignments} WHERE symbol = :symbol", params)
        return self.get_lifecycle(normalized)

    def insert_event(self, values: dict[str, Any], *, dry_run: bool = False) -> tuple[dict[str, Any], bool]:
        row = dict(values)
        row["symbol"] = normalize_lifecycle_symbol(row.get("symbol"))
        row["metrics_json"] = json_dumps(row.pop("metrics", row.get("metrics_json", {})))
        row["reasons_json"] = json_dumps(row.pop("reasons", row.get("reasons_json", [])))
        row["exchange_context_json"] = json_dumps(row.pop("exchange_context", row.get("exchange_context_json", {})))
        row["created_at"] = row.get("created_at") or utc_iso()
        columns = [
            "lifecycle_id", "symbol", "event_time", "event_type", "event_level", "event_level_rank",
            "signal_id", "source_module", "source_template", "source_excerpt", "previous_state",
            "new_state", "price", "price_change_from_first_pct", "volume_change_pct",
            "quote_volume_change_pct", "oi_change_pct", "oi_value_change_pct", "futures_cvd_delta",
            "spot_cvd_delta", "funding_rate", "event_score", "risk_score", "metrics_json",
            "reasons_json", "exchange_context_json", "dedup_key", "pushed_to_telegram", "created_at",
        ]
        if dry_run:
            fake = {key: row.get(key) for key in columns}
            fake["id"] = 0
            fake["metrics"] = json_loads(fake.pop("metrics_json"), {})
            fake["reasons"] = json_loads(fake.pop("reasons_json"), [])
            fake["exchange_context"] = json_loads(fake.pop("exchange_context_json"), {})
            return fake, True
        params = {key: row.get(key) for key in columns}
        with self.connect() as conn:
            before = conn.total_changes
            conn.execute(
                f"INSERT OR IGNORE INTO lifecycle_events ({', '.join(columns)}) VALUES ({', '.join(f':{key}' for key in columns)})",
                params,
            )
            inserted = conn.total_changes > before
            event = conn.execute("SELECT * FROM lifecycle_events WHERE dedup_key = ?", (row.get("dedup_key"),)).fetchone()
        return _row_to_dict(event, json_fields=EVENT_JSON_FIELDS) or {}, inserted

    def mark_event_pushed(self, event_id: int) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE lifecycle_events SET pushed_to_telegram = 1 WHERE id = ?", (int(event_id),))

    def insert_snapshot(self, values: dict[str, Any], *, dry_run: bool = False) -> bool:
        row = dict(values)
        row["symbol"] = normalize_lifecycle_symbol(row.get("symbol"))
        row["metrics_json"] = json_dumps(row.pop("metrics", row.get("metrics_json", {})))
        row["created_at"] = row.get("created_at") or utc_iso()
        columns = [
            "symbol", "timeframe", "snapshot_time", "price", "volume", "quote_volume", "oi",
            "oi_value_usdt", "futures_cvd_delta", "spot_cvd_delta", "funding_rate", "market_cap_usd",
            "metrics_json", "created_at",
        ]
        if dry_run:
            return True
        params = {key: row.get(key) for key in columns}
        with self.connect() as conn:
            before = conn.total_changes
            conn.execute(
                f"INSERT OR IGNORE INTO lifecycle_metric_snapshots ({', '.join(columns)}) VALUES ({', '.join(f':{key}' for key in columns)})",
                params,
            )
            return conn.total_changes > before

    def list_lifecycles(
        self,
        *,
        limit: int = 50,
        cursor: int | None = None,
        symbol: str = "",
        state: str = "",
        level: str = "",
        risk: str = "",
        active_only: bool = True,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: dict[str, Any] = {"limit": max(1, min(int(limit or 50), 300))}
        normalized = normalize_lifecycle_symbol(symbol)
        if normalized:
            clauses.append("symbol = :symbol")
            params["symbol"] = normalized
        if state:
            clauses.append("current_state = :state")
            params["state"] = str(state)
        if level:
            clauses.append("highest_level = :level")
            params["level"] = str(level)
        if risk:
            if str(risk).lower() in {"high", "高"}:
                clauses.append("risk_score >= 70")
            elif str(risk).lower() in {"mid", "medium", "中"}:
                clauses.append("risk_score >= 40 AND risk_score < 70")
            elif str(risk).lower() in {"low", "低"}:
                clauses.append("risk_score < 40")
        if active_only:
            clauses.append("is_active = 1")
        if cursor:
            clauses.append("id < :cursor")
            params["cursor"] = int(cursor)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM signal_lifecycles {where} ORDER BY updated_at DESC, id DESC LIMIT :limit",
                params,
            ).fetchall()
        items = [_row_to_dict(row, json_fields=JSON_FIELDS) or {} for row in rows]
        return {"items": items, "count": len(items), "next_cursor": items[-1]["id"] if items else None}

    def summary(self) -> dict[str, Any]:
        with self.connect() as conn:
            total = int(conn.execute("SELECT COUNT(*) FROM signal_lifecycles").fetchone()[0])
            active = int(conn.execute("SELECT COUNT(*) FROM signal_lifecycles WHERE is_active = 1").fetchone()[0])
            state_counts = {
                str(row["current_state"] or "unknown"): int(row["count"])
                for row in conn.execute(
                    "SELECT current_state, COUNT(*) AS count FROM signal_lifecycles GROUP BY current_state"
                ).fetchall()
            }
            level_counts = {
                str(row["highest_level"] or "unknown"): int(row["count"])
                for row in conn.execute(
                    "SELECT highest_level, COUNT(*) AS count FROM signal_lifecycles GROUP BY highest_level"
                ).fetchall()
            }
            top = [
                _row_to_dict(row, json_fields=JSON_FIELDS) or {}
                for row in conn.execute(
                    """
                    SELECT * FROM signal_lifecycles
                    WHERE is_active = 1
                    ORDER BY lifecycle_score DESC, risk_score DESC, updated_at DESC
                    LIMIT 10
                    """
                ).fetchall()
            ]
        return {
            "total_count": total,
            "active_count": active,
            "warming_count": state_counts.get("warming", 0),
            "launching_count": state_counts.get("launching", 0),
            "upgraded_1h_count": state_counts.get("upgraded_1h", 0),
            "upgraded_4h_count": state_counts.get("upgraded_4h", 0),
            "trend_confirmed_count": state_counts.get("trend_confirmed", 0),
            "risk_warning_count": state_counts.get("risk_warning", 0),
            "cooling_count": state_counts.get("cooling", 0),
            "failed_count": state_counts.get("failed", 0),
            "by_state": state_counts,
            "by_level": level_counts,
            "top_items": top,
        }

    def list_events(self, *, symbol: str = "", limit: int = 100) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: dict[str, Any] = {"limit": max(1, min(int(limit or 100), 300))}
        normalized = normalize_lifecycle_symbol(symbol)
        if normalized:
            clauses.append("symbol = :symbol")
            params["symbol"] = normalized
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM lifecycle_events {where} ORDER BY event_time DESC, id DESC LIMIT :limit",
                params,
            ).fetchall()
        return [_row_to_dict(row, json_fields=EVENT_JSON_FIELDS) or {} for row in rows]

    def list_snapshots(self, *, symbol: str = "", timeframe: str = "", limit: int = 100) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: dict[str, Any] = {"limit": max(1, min(int(limit or 100), 500))}
        normalized = normalize_lifecycle_symbol(symbol)
        if normalized:
            clauses.append("symbol = :symbol")
            params["symbol"] = normalized
        if timeframe:
            clauses.append("timeframe = :timeframe")
            params["timeframe"] = str(timeframe)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM lifecycle_metric_snapshots {where} ORDER BY snapshot_time DESC, id DESC LIMIT :limit",
                params,
            ).fetchall()
        return [_row_to_dict(row, json_fields=SNAPSHOT_JSON_FIELDS) or {} for row in rows]


def public_lifecycle_item(item: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "id", "symbol", "first_signal_id", "first_signal_at", "first_signal_module", "first_signal_type",
        "first_signal_level", "first_signal_level_rank", "first_signal_score", "first_signal_excerpt",
        "first_price", "first_market_cap_usd", "first_volume_15m", "first_quote_volume_15m", "first_oi",
        "first_oi_value_usdt", "first_futures_cvd_15m", "first_spot_cvd_15m", "first_funding_rate",
        "current_state", "highest_level", "highest_level_rank", "lifecycle_score", "risk_score",
        "latest_signal_id", "latest_signal_at", "latest_price", "latest_market_cap_usd", "latest_oi",
        "latest_oi_value_usdt", "latest_futures_cvd_15m", "latest_spot_cvd_15m", "latest_funding_rate",
        "price_change_from_first_pct", "market_cap_change_from_first_pct", "oi_change_from_first_pct",
        "oi_value_change_from_first_pct", "futures_cvd_change_from_first", "spot_cvd_change_from_first",
        "exchange_context", "metrics", "reasons", "is_active", "created_at", "updated_at", "closed_at",
        "close_reason",
    }
    return public_lifecycle_redact({key: value for key, value in item.items() if key in allowed})


def public_lifecycle_event(item: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "id", "lifecycle_id", "symbol", "event_time", "event_type", "event_level", "event_level_rank",
        "signal_id", "source_module", "source_template", "source_excerpt", "previous_state", "new_state",
        "price", "price_change_from_first_pct", "volume_change_pct", "quote_volume_change_pct",
        "oi_change_pct", "oi_value_change_pct", "futures_cvd_delta", "spot_cvd_delta", "funding_rate",
        "event_score", "risk_score", "metrics", "reasons", "exchange_context", "created_at",
    }
    return public_lifecycle_redact({key: value for key, value in item.items() if key in allowed})


def public_lifecycle_redact(value: Any) -> Any:
    redacted = redact_api_payload(value)
    if isinstance(redacted, dict):
        return {str(key): public_lifecycle_redact(item) for key, item in redacted.items()}
    if isinstance(redacted, list):
        return [public_lifecycle_redact(item) for item in redacted]
    if isinstance(redacted, str) and PUBLIC_SENSITIVE_TEXT_RE.search(redacted):
        return "<redacted:sensitive-line>"
    return redacted
