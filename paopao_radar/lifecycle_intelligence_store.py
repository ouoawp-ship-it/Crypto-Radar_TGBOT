from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import BASE_DIR
from .lifecycle_store import normalize_lifecycle_symbol, safe_float, safe_int


DEFAULT_LIFECYCLE_DB_PATH = BASE_DIR / "data" / "lifecycle.db"
LIFECYCLE_SCHEMA_VERSION = 1780

INTELLIGENCE_JSON_FIELDS = {
    "strengths_json": "strengths",
    "risks_json": "risks",
    "watch_points_json": "watch_points",
    "factors_json": "factors",
}
REPLAY_JSON_FIELDS = {"summary_json": "summary"}
FRAME_JSON_FIELDS = {"metrics_json": "metrics"}

INTELLIGENCE_COMPACT_COLUMNS = (
    "lifecycle_id",
    "symbol",
    "intelligence_score",
    "quality_label",
    "stage",
    "stage_label",
    "momentum_label",
    "capital_confirmation_label",
    "risk_label",
    "maturity_label",
    "confidence_label",
    "model_version",
    "calculated_at",
    "updated_at",
)
REPLAY_COMPACT_COLUMNS = (
    "lifecycle_id",
    "symbol",
    "replay_version",
    "frame_count",
    "duration_sec",
    "upgrade_path",
    "highest_level",
    "time_to_1h_sec",
    "time_to_4h_sec",
    "time_to_24h_sec",
    "max_price_gain_pct",
    "max_drawdown_pct",
    "final_return_pct",
    "final_state",
    "result_label",
    "outcome_status",
    "outcome_count",
    "calculated_at",
    "updated_at",
)
FRAME_PUBLIC_COLUMNS = (
    "id",
    "lifecycle_id",
    "symbol",
    "frame_index",
    "event_id",
    "event_time",
    "event_type",
    "event_label",
    "state_before",
    "state_after",
    "signal_level",
    "price",
    "price_change_from_first_pct",
    "oi_change_from_first_pct",
    "spot_cvd_delta",
    "futures_cvd_delta",
    "funding_rate",
    "lifecycle_score",
    "risk_score",
    "intelligence_score",
    "summary",
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return (value or _utc_now()).astimezone(timezone.utc).isoformat()


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, separators=(",", ":"))


def _json_loads(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


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


def _deserialize(row: sqlite3.Row | None, fields: dict[str, str]) -> dict[str, Any] | None:
    if row is None:
        return None
    result = dict(row)
    for stored_name, public_name in fields.items():
        if stored_name in result:
            default: Any = [] if public_name in {"strengths", "risks", "watch_points"} else {}
            result[public_name] = _json_loads(result.pop(stored_name), default)
    return result


def source_signature(value: Any) -> str:
    """Return a stable, non-secret fingerprint for replay/intelligence source data."""
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class IntelligenceStore:
    """Compatibility storage for lifecycle intelligence, replay, and analytics.

    The constructor accepts either a Settings-like object or a lifecycle DB path.
    It intentionally does not touch the filesystem; schema creation happens on the
    first non-dry-run operation through :meth:`ensure_schema` or :meth:`connect`.
    """

    settings_or_path: Any = DEFAULT_LIFECYCLE_DB_PATH
    db_path: Path = field(init=False)
    _schema_ready: bool = field(default=False, init=False, repr=False)
    _schema_lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        candidate = getattr(self.settings_or_path, "lifecycle_db_path", self.settings_or_path)
        self.db_path = Path(candidate or DEFAULT_LIFECYCLE_DB_PATH)

    @contextmanager
    def connect(self, *, ensure_schema: bool = True) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=15000")
            if ensure_schema and not self._schema_ready:
                with self._schema_lock:
                    if not self._schema_ready:
                        conn.execute("PRAGMA journal_mode=WAL")
                        self.ensure_schema(conn)
                        conn.commit()
            yield conn
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as conn:
            self._begin_immediate_with_retry(conn)
            yield conn

    @staticmethod
    def _begin_immediate_with_retry(
        conn: sqlite3.Connection,
        *,
        attempts: int = 5,
        base_delay_sec: float = 0.05,
    ) -> None:
        conn.commit()
        for attempt in range(max(1, int(attempts))):
            try:
                conn.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as exc:
                if not any(token in str(exc).lower() for token in ("locked", "busy")):
                    raise
                if attempt + 1 >= max(1, int(attempts)):
                    raise
                time.sleep(max(0.0, float(base_delay_sec)) * (2**attempt))

    @staticmethod
    def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}

    @classmethod
    def _add_column(cls, conn: sqlite3.Connection, table: str, declaration: str) -> None:
        name = declaration.split()[0].strip('"')
        if name not in cls._columns(conn, table):
            conn.execute(f'ALTER TABLE "{table}" ADD COLUMN {declaration}')

    def ensure_schema(self, conn: sqlite3.Connection | None = None) -> None:
        own_conn = conn is None
        if own_conn:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.db_path), timeout=15)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=15000")
        assert conn is not None
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS lifecycle_intelligence (
                    lifecycle_id INTEGER PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    intelligence_score REAL NOT NULL DEFAULT 0,
                    quality_label TEXT,
                    stage TEXT,
                    stage_label TEXT,
                    momentum_label TEXT,
                    capital_confirmation_label TEXT,
                    risk_label TEXT,
                    maturity_label TEXT,
                    confidence_label TEXT,
                    summary TEXT,
                    strengths_json TEXT,
                    risks_json TEXT,
                    watch_points_json TEXT,
                    factors_json TEXT,
                    model_version TEXT NOT NULL,
                    source_signature TEXT,
                    calculated_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS lifecycle_replays (
                    lifecycle_id INTEGER PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    replay_version TEXT NOT NULL,
                    frame_count INTEGER DEFAULT 0,
                    duration_sec INTEGER,
                    upgrade_path TEXT,
                    highest_level TEXT,
                    time_to_1h_sec INTEGER,
                    time_to_4h_sec INTEGER,
                    time_to_24h_sec INTEGER,
                    max_price_gain_pct REAL,
                    max_drawdown_pct REAL,
                    final_return_pct REAL,
                    final_state TEXT,
                    result_label TEXT,
                    outcome_status TEXT,
                    outcome_count INTEGER DEFAULT 0,
                    source_signature TEXT,
                    summary_json TEXT,
                    calculated_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS lifecycle_replay_frames (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lifecycle_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    frame_index INTEGER NOT NULL,
                    event_id INTEGER,
                    event_time TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_label TEXT,
                    state_before TEXT,
                    state_after TEXT,
                    signal_level TEXT,
                    price REAL,
                    price_change_from_first_pct REAL,
                    oi_change_from_first_pct REAL,
                    spot_cvd_delta REAL,
                    futures_cvd_delta REAL,
                    funding_rate REAL,
                    lifecycle_score REAL,
                    risk_score REAL,
                    intelligence_score REAL,
                    summary TEXT,
                    metrics_json TEXT,
                    UNIQUE(lifecycle_id, frame_index)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS lifecycle_analytics_cache (
                    cache_key TEXT PRIMARY KEY,
                    data_json TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )
                """
            )
            # Forward-compatible migration for databases created by early v1.78 builds.
            self._add_column(conn, "lifecycle_intelligence", "stage TEXT")
            self._add_column(conn, "lifecycle_intelligence", "source_signature TEXT")
            self._add_column(conn, "lifecycle_replays", "outcome_count INTEGER DEFAULT 0")
            self._add_column(conn, "lifecycle_replays", "source_signature TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_lifecycle_intelligence_score "
                "ON lifecycle_intelligence(intelligence_score DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_lifecycle_intelligence_quality "
                "ON lifecycle_intelligence(quality_label)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_lifecycle_replays_result "
                "ON lifecycle_replays(result_label)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_lifecycle_replays_path "
                "ON lifecycle_replays(upgrade_path)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_lifecycle_replay_frames_time "
                "ON lifecycle_replay_frames(lifecycle_id, event_time)"
            )
            current_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if current_version < LIFECYCLE_SCHEMA_VERSION:
                conn.execute(f"PRAGMA user_version={LIFECYCLE_SCHEMA_VERSION}")
            if own_conn:
                conn.commit()
            self._schema_ready = True
        finally:
            if own_conn:
                conn.close()

    def get_intelligence(
        self,
        lifecycle_id: int | None = None,
        symbol: str = "",
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        normalized = normalize_lifecycle_symbol(symbol)
        if not lifecycle_id and not normalized:
            return None
        if conn is None:
            with self.connect() as owned:
                return self.get_intelligence(lifecycle_id, normalized, conn=owned)
        if lifecycle_id:
            row = conn.execute(
                "SELECT * FROM lifecycle_intelligence WHERE lifecycle_id = ?", (safe_int(lifecycle_id),)
            ).fetchone()
        else:
            row = conn.execute("SELECT * FROM lifecycle_intelligence WHERE symbol = ?", (normalized,)).fetchone()
        return _deserialize(row, INTELLIGENCE_JSON_FIELDS)

    def upsert_intelligence(
        self,
        record: dict[str, Any],
        *,
        conn: sqlite3.Connection | None = None,
        fetch: bool = True,
    ) -> dict[str, Any]:
        if conn is None:
            with self.transaction() as owned:
                return self.upsert_intelligence(record, conn=owned, fetch=fetch)
        lifecycle_id = safe_int(record.get("lifecycle_id"))
        symbol = normalize_lifecycle_symbol(record.get("symbol"))
        if lifecycle_id <= 0 or not symbol:
            raise ValueError("lifecycle_id and symbol are required")
        now = _iso()
        values = dict(record)
        values.update(
            {
                "lifecycle_id": lifecycle_id,
                "symbol": symbol,
                "intelligence_score": max(0.0, min(100.0, safe_float(values.get("intelligence_score")) or 0.0)),
                "strengths_json": _json_dumps(values.pop("strengths", values.get("strengths_json", []))),
                "risks_json": _json_dumps(values.pop("risks", values.get("risks_json", []))),
                "watch_points_json": _json_dumps(values.pop("watch_points", values.get("watch_points_json", []))),
                "factors_json": _json_dumps(values.pop("factors", values.get("factors_json", {}))),
                "calculated_at": str(values.get("calculated_at") or now),
                "updated_at": str(values.get("updated_at") or now),
            }
        )
        columns = (
            "lifecycle_id", "symbol", "intelligence_score", "quality_label", "stage", "stage_label",
            "momentum_label", "capital_confirmation_label", "risk_label", "maturity_label",
            "confidence_label", "summary", "strengths_json", "risks_json", "watch_points_json",
            "factors_json", "model_version", "source_signature", "calculated_at", "updated_at",
        )
        params = {key: values.get(key) for key in columns}
        params["model_version"] = str(params.get("model_version") or "lifecycle-intelligence-v1")
        assignments = ", ".join(f"{key}=excluded.{key}" for key in columns if key != "lifecycle_id")
        conn.execute(
            f"INSERT INTO lifecycle_intelligence ({', '.join(columns)}) "
            f"VALUES ({', '.join(':'+key for key in columns)}) "
            f"ON CONFLICT(lifecycle_id) DO UPDATE SET {assignments}",
            params,
        )
        if not fetch:
            return {key: values.get(key) for key in columns}
        return self.get_intelligence(lifecycle_id, conn=conn) or {}

    def list_intelligence(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        active: bool | None = None,
        compact: bool = True,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        if conn is None:
            with self.connect() as owned:
                return self.list_intelligence(limit=limit, offset=offset, active=active, compact=compact, conn=owned)
        projection = ", ".join(f"i.{column}" for column in INTELLIGENCE_COMPACT_COLUMNS) if compact else "i.*"
        join = ""
        where = ""
        params: dict[str, Any] = {
            "limit": max(1, min(safe_int(limit, 50), 500)),
            "offset": max(0, safe_int(offset)),
        }
        if active is not None and self._table_exists(conn, "signal_lifecycles"):
            join = " JOIN signal_lifecycles l ON l.id = i.lifecycle_id"
            where = " WHERE l.is_active = :active"
            params["active"] = 1 if active else 0
        rows = conn.execute(
            f"SELECT {projection} FROM lifecycle_intelligence i{join}{where} "
            "ORDER BY i.intelligence_score DESC, i.updated_at DESC, i.lifecycle_id DESC "
            "LIMIT :limit OFFSET :offset",
            params,
        ).fetchall()
        return [_deserialize(row, {} if compact else INTELLIGENCE_JSON_FIELDS) or {} for row in rows]

    def get_replay(
        self,
        lifecycle_id: int | None = None,
        symbol: str = "",
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        normalized = normalize_lifecycle_symbol(symbol)
        if not lifecycle_id and not normalized:
            return None
        if conn is None:
            with self.connect() as owned:
                return self.get_replay(lifecycle_id, normalized, conn=owned)
        if lifecycle_id:
            row = conn.execute("SELECT * FROM lifecycle_replays WHERE lifecycle_id = ?", (safe_int(lifecycle_id),)).fetchone()
        else:
            row = conn.execute("SELECT * FROM lifecycle_replays WHERE symbol = ?", (normalized,)).fetchone()
        return _deserialize(row, REPLAY_JSON_FIELDS)

    def upsert_replay(
        self,
        record: dict[str, Any],
        frames: list[dict[str, Any]] | None = None,
        *,
        conn: sqlite3.Connection | None = None,
        fetch: bool = True,
    ) -> dict[str, Any]:
        if conn is None:
            with self.transaction() as owned:
                return self.upsert_replay(record, frames, conn=owned, fetch=fetch)
        lifecycle_id = safe_int(record.get("lifecycle_id"))
        symbol = normalize_lifecycle_symbol(record.get("symbol"))
        if lifecycle_id <= 0 or not symbol:
            raise ValueError("lifecycle_id and symbol are required")
        now = _iso()
        values = dict(record)
        summary_value = values.pop("summary", values.get("summary_json", {}))
        values.update(
            {
                "lifecycle_id": lifecycle_id,
                "symbol": symbol,
                "replay_version": str(values.get("replay_version") or "lifecycle-replay-v1"),
                "frame_count": safe_int(values.get("frame_count"), len(frames or [])),
                "outcome_count": safe_int(values.get("outcome_count")),
                "summary_json": _json_dumps(summary_value),
                "calculated_at": str(values.get("calculated_at") or now),
                "updated_at": str(values.get("updated_at") or now),
            }
        )
        columns = (
            "lifecycle_id", "symbol", "replay_version", "frame_count", "duration_sec", "upgrade_path",
            "highest_level", "time_to_1h_sec", "time_to_4h_sec", "time_to_24h_sec",
            "max_price_gain_pct", "max_drawdown_pct", "final_return_pct", "final_state", "result_label",
            "outcome_status", "outcome_count", "source_signature", "summary_json", "calculated_at", "updated_at",
        )
        params = {key: values.get(key) for key in columns}
        assignments = ", ".join(f"{key}=excluded.{key}" for key in columns if key != "lifecycle_id")
        conn.execute(
            f"INSERT INTO lifecycle_replays ({', '.join(columns)}) "
            f"VALUES ({', '.join(':'+key for key in columns)}) "
            f"ON CONFLICT(lifecycle_id) DO UPDATE SET {assignments}",
            params,
        )
        if frames is not None:
            self.replace_replay_frames(lifecycle_id, symbol, frames, conn=conn)
        if not fetch:
            return {key: values.get(key) for key in columns}
        return self.get_replay(lifecycle_id, conn=conn) or {}

    def list_replays(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        completed_only: bool = False,
        compact: bool = True,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        if conn is None:
            with self.connect() as owned:
                return self.list_replays(
                    limit=limit,
                    offset=offset,
                    completed_only=completed_only,
                    compact=compact,
                    conn=owned,
                )
        projection = ", ".join(REPLAY_COMPACT_COLUMNS) if compact else "*"
        where = "WHERE result_label IS NOT NULL AND result_label != 'insufficient_data'" if completed_only else ""
        rows = conn.execute(
            f"SELECT {projection} FROM lifecycle_replays {where} "
            "ORDER BY updated_at DESC, lifecycle_id DESC LIMIT ? OFFSET ?",
            (max(1, min(safe_int(limit, 50), 500)), max(0, safe_int(offset))),
        ).fetchall()
        return [_deserialize(row, {} if compact else REPLAY_JSON_FIELDS) or {} for row in rows]

    def replace_replay_frames(
        self,
        lifecycle_id: int,
        symbol: str,
        frames: list[dict[str, Any]],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        if conn is None:
            with self.transaction() as owned:
                return self.replace_replay_frames(lifecycle_id, symbol, frames, conn=owned)
        normalized_id = safe_int(lifecycle_id)
        normalized_symbol = normalize_lifecycle_symbol(symbol)
        if normalized_id <= 0 or not normalized_symbol:
            raise ValueError("lifecycle_id and symbol are required")
        conn.execute("DELETE FROM lifecycle_replay_frames WHERE lifecycle_id = ?", (normalized_id,))
        rows: list[tuple[Any, ...]] = []
        for index, item in enumerate(frames, 1):
            frame = dict(item)
            rows.append(
                (
                    normalized_id,
                    normalized_symbol,
                    index,
                    safe_int(frame.get("event_id")) or None,
                    str(frame.get("event_time") or _iso()),
                    str(frame.get("event_type") or "unknown"),
                    str(frame.get("event_label") or ""),
                    str(frame.get("state_before") or ""),
                    str(frame.get("state_after") or ""),
                    str(frame.get("signal_level") or ""),
                    safe_float(frame.get("price")),
                    safe_float(frame.get("price_change_from_first_pct")),
                    safe_float(frame.get("oi_change_from_first_pct")),
                    safe_float(frame.get("spot_cvd_delta")),
                    safe_float(frame.get("futures_cvd_delta")),
                    safe_float(frame.get("funding_rate")),
                    safe_float(frame.get("lifecycle_score")),
                    safe_float(frame.get("risk_score")),
                    safe_float(frame.get("intelligence_score")),
                    str(frame.get("summary") or "")[:1200],
                    _json_dumps(frame.get("metrics") or {}),
                )
            )
        if rows:
            conn.executemany(
                """
                INSERT INTO lifecycle_replay_frames (
                    lifecycle_id, symbol, frame_index, event_id, event_time, event_type, event_label,
                    state_before, state_after, signal_level, price, price_change_from_first_pct,
                    oi_change_from_first_pct, spot_cvd_delta, futures_cvd_delta, funding_rate,
                    lifecycle_score, risk_score, intelligence_score, summary, metrics_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        return len(rows)

    def list_replay_frames(
        self,
        lifecycle_id: int | None = None,
        symbol: str = "",
        *,
        limit: int = 100,
        offset: int = 0,
        include_metrics: bool = False,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        normalized = normalize_lifecycle_symbol(symbol)
        if not lifecycle_id and not normalized:
            return []
        if conn is None:
            with self.connect() as owned:
                return self.list_replay_frames(
                    lifecycle_id,
                    normalized,
                    limit=limit,
                    offset=offset,
                    include_metrics=include_metrics,
                    conn=owned,
                )
        where = "lifecycle_id = :lifecycle_id" if lifecycle_id else "symbol = :symbol"
        params = {
            "lifecycle_id": safe_int(lifecycle_id),
            "symbol": normalized,
            "limit": max(1, min(safe_int(limit, 100), 500)),
            "offset": max(0, safe_int(offset)),
        }
        projection = "*" if include_metrics else ", ".join(FRAME_PUBLIC_COLUMNS)
        rows = conn.execute(
            f"SELECT {projection} FROM lifecycle_replay_frames WHERE {where} "
            "ORDER BY frame_index ASC LIMIT :limit OFFSET :offset",
            params,
        ).fetchall()
        return [_deserialize(row, FRAME_JSON_FIELDS if include_metrics else {}) or {} for row in rows]

    def count_replay_frames(
        self,
        lifecycle_id: int,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        if conn is None:
            with self.connect() as owned:
                return self.count_replay_frames(lifecycle_id, conn=owned)
        row = conn.execute(
            "SELECT COUNT(*) FROM lifecycle_replay_frames WHERE lifecycle_id = ?", (safe_int(lifecycle_id),)
        ).fetchone()
        return int(row[0] if row else 0)

    def get_analytics_cache(
        self,
        cache_key: str,
        *,
        now: datetime | None = None,
        conn: sqlite3.Connection | None = None,
    ) -> Any | None:
        key = str(cache_key or "").strip()
        if not key:
            return None
        if conn is None:
            with self.connect() as owned:
                return self.get_analytics_cache(key, now=now, conn=owned)
        row = conn.execute(
            "SELECT data_json, expires_at FROM lifecycle_analytics_cache WHERE cache_key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        expires = _parse_time(row["expires_at"])
        if expires is None or expires <= (now or _utc_now()):
            return None
        return _json_loads(row["data_json"], None)

    def put_analytics_cache(
        self,
        cache_key: str,
        data: Any,
        *,
        ttl_sec: int = 21600,
        conn: sqlite3.Connection | None = None,
    ) -> None:
        key = str(cache_key or "").strip()
        if not key:
            raise ValueError("cache_key is required")
        if conn is None:
            with self.transaction() as owned:
                self.put_analytics_cache(key, data, ttl_sec=ttl_sec, conn=owned)
                return
        now = _utc_now()
        expires = now + timedelta(seconds=max(1, safe_int(ttl_sec, 21600)))
        conn.execute(
            """
            INSERT INTO lifecycle_analytics_cache (cache_key, data_json, generated_at, expires_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                data_json=excluded.data_json,
                generated_at=excluded.generated_at,
                expires_at=excluded.expires_at
            """,
            (key, _json_dumps(data), _iso(now), _iso(expires)),
        )

    def invalidate_analytics_cache(
        self,
        prefix: str = "",
        *,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        if conn is None:
            with self.transaction() as owned:
                return self.invalidate_analytics_cache(prefix, conn=owned)
        before = conn.total_changes
        if prefix:
            conn.execute("DELETE FROM lifecycle_analytics_cache WHERE cache_key LIKE ?", (f"{prefix}%",))
        else:
            conn.execute("DELETE FROM lifecycle_analytics_cache")
        return max(0, conn.total_changes - before)

    # Concise aliases used by analytics adapters.
    cache_get = get_analytics_cache
    cache_set = put_analytics_cache

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (str(name),)
        ).fetchone() is not None


# Explicit alias keeps the module self-describing for callers preferring a
# longer class name without duplicating storage behavior.
LifecycleIntelligenceStore = IntelligenceStore
