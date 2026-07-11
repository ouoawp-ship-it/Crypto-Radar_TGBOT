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
LIFECYCLE_SCHEMA_VERSION = 1800

INTELLIGENCE_JSON_FIELDS = {
    "strengths_json": "strengths",
    "risks_json": "risks",
    "watch_points_json": "watch_points",
    "factors_json": "factors",
}
REPLAY_JSON_FIELDS = {"summary_json": "summary"}
FRAME_JSON_FIELDS = {"metrics_json": "metrics"}
OUTCOME_COVERAGE_JSON_FIELDS = {"reasons_json": "reasons"}

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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS lifecycle_outcome_links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lifecycle_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    signal_id INTEGER,
                    lifecycle_event_id INTEGER,
                    outcome_id INTEGER NOT NULL,
                    horizon TEXT NOT NULL,
                    outcome_status TEXT NOT NULL,
                    link_role TEXT NOT NULL,
                    link_method TEXT NOT NULL,
                    link_confidence REAL NOT NULL DEFAULT 1.0,
                    signal_time TEXT,
                    outcome_time TEXT,
                    is_primary INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(lifecycle_id, outcome_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS lifecycle_outcome_coverage (
                    lifecycle_id INTEGER PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    candidate_signal_count INTEGER NOT NULL DEFAULT 0,
                    linked_signal_count INTEGER NOT NULL DEFAULT 0,
                    linked_outcome_count INTEGER NOT NULL DEFAULT 0,
                    primary_outcome_id INTEGER,
                    horizon_1h_status TEXT,
                    horizon_4h_status TEXT,
                    horizon_24h_status TEXT,
                    horizon_72h_status TEXT,
                    linked_horizon_count INTEGER NOT NULL DEFAULT 0,
                    mature_horizon_count INTEGER NOT NULL DEFAULT 0,
                    link_coverage_ratio REAL NOT NULL DEFAULT 0,
                    maturity_ratio REAL NOT NULL DEFAULT 0,
                    coverage_label TEXT,
                    maturity_label TEXT,
                    unlinked_reason TEXT,
                    reasons_json TEXT,
                    calculated_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS lifecycle_outcome_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_key TEXT NOT NULL UNIQUE,
                    lifecycle_id INTEGER NOT NULL,
                    lifecycle_event_id INTEGER,
                    signal_id INTEGER,
                    symbol TEXT NOT NULL,
                    signal_time TEXT,
                    source_module TEXT,
                    source_template TEXT,
                    source_signal_type TEXT,
                    horizon TEXT NOT NULL,
                    due_at TEXT,
                    eligibility_status TEXT NOT NULL,
                    eligibility_reason TEXT,
                    candidate_status TEXT NOT NULL,
                    outcome_id INTEGER,
                    is_terminal INTEGER NOT NULL DEFAULT 0,
                    is_retryable INTEGER NOT NULL DEFAULT 0,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    last_attempt_at TEXT,
                    next_retry_at TEXT,
                    source_status TEXT,
                    last_error_code TEXT,
                    last_error_summary TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS calibration_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_version TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    sample_count INTEGER NOT NULL DEFAULT 0,
                    mature_sample_count INTEGER NOT NULL DEFAULT 0,
                    summary_json TEXT NOT NULL,
                    recommendations_json TEXT NOT NULL,
                    source_signature TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS calibration_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_id INTEGER NOT NULL,
                    metric_type TEXT NOT NULL,
                    metric_key TEXT NOT NULL,
                    sample_count INTEGER NOT NULL DEFAULT 0,
                    success_ratio REAL,
                    avg_return REAL,
                    avg_drawdown REAL,
                    metrics_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(report_id, metric_type, metric_key)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS optimization_scenarios (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scenario_key TEXT NOT NULL UNIQUE,
                    scenario_name TEXT NOT NULL,
                    scenario_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL,
                    base_model_version TEXT NOT NULL,
                    scenario_version TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,
                    candidate_params_json TEXT NOT NULL,
                    built_in INTEGER NOT NULL DEFAULT 1,
                    optimization_version TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS optimization_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scenario_id INTEGER NOT NULL,
                    run_key TEXT NOT NULL UNIQUE,
                    optimization_version TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    source_signature TEXT NOT NULL,
                    scope_json TEXT NOT NULL,
                    sample_count INTEGER NOT NULL DEFAULT 0,
                    mature_sample_count INTEGER NOT NULL DEFAULT 0,
                    selected_sample_count INTEGER NOT NULL DEFAULT 0,
                    confidence REAL NOT NULL DEFAULT 0,
                    confidence_label TEXT NOT NULL,
                    status TEXT NOT NULL,
                    production_json TEXT NOT NULL,
                    candidate_json TEXT NOT NULL,
                    delta_json TEXT NOT NULL,
                    recommendations_json TEXT NOT NULL,
                    readiness_json TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    generated_at TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS optimization_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    metric_scope TEXT NOT NULL,
                    metric_type TEXT NOT NULL,
                    metric_key TEXT NOT NULL,
                    sample_count INTEGER NOT NULL DEFAULT 0,
                    production_value REAL,
                    candidate_value REAL,
                    delta_value REAL,
                    metric_value REAL,
                    metrics_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(run_id, metric_scope, metric_key)
                )
                """
            )
            # Forward-compatible migration for databases created by early v1.78 builds.
            self._add_column(conn, "lifecycle_intelligence", "stage TEXT")
            self._add_column(conn, "lifecycle_intelligence", "source_signature TEXT")
            self._add_column(conn, "lifecycle_replays", "outcome_count INTEGER DEFAULT 0")
            self._add_column(conn, "lifecycle_replays", "source_signature TEXT")
            # Forward-compatible migration for pre-release v1.80 databases.
            self._add_column(conn, "optimization_scenarios", "scenario_key TEXT NOT NULL DEFAULT ''")
            self._add_column(conn, "optimization_scenarios", "scenario_name TEXT NOT NULL DEFAULT ''")
            self._add_column(conn, "optimization_scenarios", "name TEXT NOT NULL DEFAULT ''")
            self._add_column(conn, "optimization_scenarios", "base_model_version TEXT NOT NULL DEFAULT ''")
            self._add_column(conn, "optimization_scenarios", "scenario_version TEXT NOT NULL DEFAULT ''")
            self._add_column(conn, "optimization_scenarios", "parameters_json TEXT NOT NULL DEFAULT '{}'")
            self._add_column(conn, "optimization_scenarios", "candidate_params_json TEXT NOT NULL DEFAULT '{}'")
            self._add_column(conn, "optimization_scenarios", "built_in INTEGER NOT NULL DEFAULT 0")
            self._add_column(conn, "optimization_scenarios", "optimization_version TEXT NOT NULL DEFAULT ''")
            self._add_column(conn, "optimization_scenarios", "status TEXT NOT NULL DEFAULT 'draft'")
            self._add_column(conn, "optimization_scenarios", "created_at TEXT NOT NULL DEFAULT ''")
            self._add_column(conn, "optimization_scenarios", "updated_at TEXT NOT NULL DEFAULT ''")
            self._add_column(conn, "optimization_runs", "run_key TEXT NOT NULL DEFAULT ''")
            self._add_column(conn, "optimization_runs", "optimization_version TEXT NOT NULL DEFAULT ''")
            self._add_column(conn, "optimization_runs", "model_version TEXT NOT NULL DEFAULT ''")
            self._add_column(conn, "optimization_runs", "source_signature TEXT NOT NULL DEFAULT ''")
            self._add_column(conn, "optimization_runs", "scope_json TEXT NOT NULL DEFAULT '{}'")
            self._add_column(conn, "optimization_runs", "selected_sample_count INTEGER NOT NULL DEFAULT 0")
            self._add_column(conn, "optimization_runs", "confidence REAL NOT NULL DEFAULT 0")
            self._add_column(conn, "optimization_runs", "confidence_label TEXT NOT NULL DEFAULT 'low_confidence'")
            self._add_column(conn, "optimization_runs", "production_json TEXT NOT NULL DEFAULT '{}'")
            self._add_column(conn, "optimization_runs", "candidate_json TEXT NOT NULL DEFAULT '{}'")
            self._add_column(conn, "optimization_runs", "delta_json TEXT NOT NULL DEFAULT '{}'")
            self._add_column(conn, "optimization_runs", "recommendations_json TEXT NOT NULL DEFAULT '[]'")
            self._add_column(conn, "optimization_runs", "readiness_json TEXT NOT NULL DEFAULT '{}'")
            self._add_column(conn, "optimization_runs", "report_json TEXT NOT NULL DEFAULT '{}'")
            self._add_column(conn, "optimization_runs", "result_json TEXT NOT NULL DEFAULT '{}'")
            self._add_column(conn, "optimization_runs", "started_at TEXT NOT NULL DEFAULT ''")
            self._add_column(conn, "optimization_runs", "finished_at TEXT NOT NULL DEFAULT ''")
            self._add_column(conn, "optimization_runs", "generated_at TEXT NOT NULL DEFAULT ''")
            self._add_column(conn, "optimization_runs", "created_at TEXT NOT NULL DEFAULT ''")
            self._add_column(conn, "optimization_metrics", "metric_scope TEXT NOT NULL DEFAULT 'comparison'")
            self._add_column(conn, "optimization_metrics", "metric_type TEXT NOT NULL DEFAULT 'summary'")
            self._add_column(conn, "optimization_metrics", "sample_count INTEGER NOT NULL DEFAULT 0")
            self._add_column(conn, "optimization_metrics", "production_value REAL")
            self._add_column(conn, "optimization_metrics", "candidate_value REAL")
            self._add_column(conn, "optimization_metrics", "delta_value REAL")
            self._add_column(conn, "optimization_metrics", "metric_value REAL")
            self._add_column(conn, "optimization_metrics", "created_at TEXT NOT NULL DEFAULT ''")
            conn.execute(
                "UPDATE optimization_scenarios SET scenario_key='legacy-' || id "
                "WHERE COALESCE(scenario_key,'')=''"
            )
            conn.execute(
                "UPDATE optimization_scenarios SET name=COALESCE(NULLIF(scenario_name,''),scenario_key) "
                "WHERE COALESCE(name,'')=''"
            )
            conn.execute(
                "UPDATE optimization_runs SET run_key='legacy-run-' || id "
                "WHERE COALESCE(run_key,'')=''"
            )
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
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_lifecycle_outcome_links_lifecycle "
                "ON lifecycle_outcome_links(lifecycle_id, horizon)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_lifecycle_outcome_links_signal "
                "ON lifecycle_outcome_links(signal_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_lifecycle_outcome_links_outcome "
                "ON lifecycle_outcome_links(outcome_id)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_lifecycle_outcome_links_primary "
                "ON lifecycle_outcome_links(lifecycle_id, is_primary)"
            )
            # SQLite partial uniqueness gives the persisted primary selection a
            # database-level invariant without changing either source database.
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_lifecycle_outcome_links_one_primary "
                "ON lifecycle_outcome_links(lifecycle_id) WHERE is_primary = 1"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_lifecycle_outcome_coverage_label "
                "ON lifecycle_outcome_coverage(coverage_label, maturity_label)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_lifecycle_outcome_candidates_due "
                "ON lifecycle_outcome_candidates(eligibility_status, candidate_status, due_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_lifecycle_outcome_candidates_retry "
                "ON lifecycle_outcome_candidates(is_retryable, next_retry_at)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_lifecycle_outcome_candidates_lifecycle "
                "ON lifecycle_outcome_candidates(lifecycle_id, horizon)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_lifecycle_outcome_candidates_signal "
                "ON lifecycle_outcome_candidates(signal_id, horizon)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_lifecycle_outcome_candidates_reason "
                "ON lifecycle_outcome_candidates(eligibility_reason, candidate_status)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_calibration_reports_version_time "
                "ON calibration_reports(report_version, model_version, generated_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_calibration_reports_signature "
                "ON calibration_reports(report_version, model_version, source_signature)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_calibration_metrics_report_type "
                "ON calibration_metrics(report_id, metric_type, metric_key)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_optimization_scenarios_key "
                "ON optimization_scenarios(scenario_key)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_optimization_runs_key "
                "ON optimization_runs(run_key)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_optimization_scenarios_type "
                "ON optimization_scenarios(scenario_type, scenario_key)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_optimization_runs_scenario_time "
                "ON optimization_runs(scenario_id, generated_at DESC)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_optimization_runs_signature "
                "ON optimization_runs(optimization_version, model_version, source_signature)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_optimization_metrics_run_scope "
                "ON optimization_metrics(run_id, metric_scope, metric_key)"
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

    def replace_outcome_links(
        self,
        lifecycle_id: int,
        links: list[dict[str, Any]],
        *,
        primary_outcome_id: int | None = None,
        replace: bool = False,
        prune: bool = False,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        """Upsert one lifecycle's links inside the caller's batch transaction."""

        if conn is None:
            with self.transaction() as owned:
                return self.replace_outcome_links(
                    lifecycle_id,
                    links,
                    primary_outcome_id=primary_outcome_id,
                    replace=replace,
                    prune=prune,
                    conn=owned,
                )
        normalized_id = safe_int(lifecycle_id)
        if normalized_id <= 0:
            raise ValueError("lifecycle_id is required")
        existing_primary = conn.execute(
            "SELECT outcome_id, signal_id FROM lifecycle_outcome_links WHERE lifecycle_id = ? AND is_primary = 1",
            (normalized_id,),
        ).fetchone()
        planned_outcome_ids = {
            safe_int(item.get("outcome_id")) for item in links if safe_int(item.get("outcome_id")) > 0
        }
        desired_primary_signal_id = next(
            (
                safe_int(item.get("signal_id"))
                for item in links
                if safe_int(item.get("outcome_id")) == safe_int(primary_outcome_id)
            ),
            0,
        )
        if (
            not replace
            and existing_primary is not None
            and safe_int(existing_primary[0]) == safe_int(primary_outcome_id)
            and safe_int(existing_primary[1]) == desired_primary_signal_id
        ):
            primary_outcome_id = safe_int(existing_primary[0])
        if replace:
            conn.execute("DELETE FROM lifecycle_outcome_links WHERE lifecycle_id = ?", (normalized_id,))
        else:
            if prune:
                if planned_outcome_ids:
                    placeholders = ",".join("?" for _ in planned_outcome_ids)
                    conn.execute(
                        f"DELETE FROM lifecycle_outcome_links WHERE lifecycle_id = ? "
                        f"AND outcome_id NOT IN ({placeholders})",
                        [normalized_id, *sorted(planned_outcome_ids)],
                    )
                else:
                    conn.execute("DELETE FROM lifecycle_outcome_links WHERE lifecycle_id = ?", (normalized_id,))
            conn.execute(
                "UPDATE lifecycle_outcome_links SET is_primary = 0 WHERE lifecycle_id = ? AND is_primary = 1",
                (normalized_id,),
            )
        now = _iso()
        columns = (
            "lifecycle_id", "symbol", "signal_id", "lifecycle_event_id", "outcome_id",
            "horizon", "outcome_status", "link_role", "link_method", "link_confidence",
            "signal_time", "outcome_time", "is_primary", "created_at", "updated_at",
        )
        rows: list[dict[str, Any]] = []
        for raw in links:
            row = dict(raw)
            row.update({
                "lifecycle_id": normalized_id,
                "symbol": normalize_lifecycle_symbol(row.get("symbol")),
                "signal_id": safe_int(row.get("signal_id")) or None,
                "lifecycle_event_id": safe_int(row.get("lifecycle_event_id")) or None,
                "outcome_id": safe_int(row.get("outcome_id")),
                "horizon": str(row.get("horizon") or ""),
                "outcome_status": str(row.get("outcome_status") or "missing"),
                "link_role": str(row.get("link_role") or "fallback"),
                "link_method": str(row.get("link_method") or "symbol_time_module"),
                "link_confidence": max(0.0, min(1.0, safe_float(row.get("link_confidence")) or 0.0)),
                "is_primary": 1 if safe_int(row.get("outcome_id")) == safe_int(primary_outcome_id) else 0,
                "created_at": str(row.get("created_at") or now),
                "updated_at": now,
            })
            if row["symbol"] and row["outcome_id"] > 0 and row["horizon"]:
                rows.append({key: row.get(key) for key in columns})
        if rows:
            assignments = ", ".join(
                f"{key}=excluded.{key}"
                for key in columns
                if key not in {"lifecycle_id", "outcome_id", "created_at"}
            )
            conn.executemany(
                f"INSERT INTO lifecycle_outcome_links ({', '.join(columns)}) "
                f"VALUES ({', '.join(':'+key for key in columns)}) "
                f"ON CONFLICT(lifecycle_id, outcome_id) DO UPDATE SET {assignments}",
                rows,
            )
        return len(rows)

    def upsert_outcome_coverage(
        self,
        record: dict[str, Any],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        if conn is None:
            with self.transaction() as owned:
                return self.upsert_outcome_coverage(record, conn=owned)
        row = dict(record)
        lifecycle_id = safe_int(row.get("lifecycle_id"))
        symbol = normalize_lifecycle_symbol(row.get("symbol"))
        if lifecycle_id <= 0 or not symbol:
            raise ValueError("lifecycle_id and symbol are required")
        now = _iso()
        row.update({
            "lifecycle_id": lifecycle_id,
            "symbol": symbol,
            "reasons_json": _json_dumps(row.pop("reasons", row.get("reasons_json", {}))),
            "calculated_at": str(row.get("calculated_at") or now),
            "updated_at": now,
        })
        columns = (
            "lifecycle_id", "symbol", "candidate_signal_count", "linked_signal_count",
            "linked_outcome_count", "primary_outcome_id", "horizon_1h_status",
            "horizon_4h_status", "horizon_24h_status", "horizon_72h_status",
            "linked_horizon_count", "mature_horizon_count", "link_coverage_ratio",
            "maturity_ratio", "coverage_label", "maturity_label", "unlinked_reason",
            "reasons_json", "calculated_at", "updated_at",
        )
        params = {key: row.get(key) for key in columns}
        assignments = ", ".join(f"{key}=excluded.{key}" for key in columns if key != "lifecycle_id")
        conn.execute(
            f"INSERT INTO lifecycle_outcome_coverage ({', '.join(columns)}) "
            f"VALUES ({', '.join(':'+key for key in columns)}) "
            f"ON CONFLICT(lifecycle_id) DO UPDATE SET {assignments}",
            params,
        )
        stored = conn.execute(
            "SELECT * FROM lifecycle_outcome_coverage WHERE lifecycle_id = ?", (lifecycle_id,)
        ).fetchone()
        return _deserialize(stored, OUTCOME_COVERAGE_JSON_FIELDS) or {}

    def write_outcome_plan_batch(
        self,
        plans: list[dict[str, Any]],
        *,
        preserve_primary: bool = True,
        replace_links: bool = True,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, int]:
        """Persist deterministic link/coverage plans without per-lifecycle queries."""

        if conn is None:
            with self.transaction() as owned:
                return self.write_outcome_plan_batch(
                    plans,
                    preserve_primary=preserve_primary,
                    replace_links=replace_links,
                    conn=owned,
                )
        normalized = [
            dict(plan)
            for plan in plans
            if safe_int((plan.get("coverage") or {}).get("lifecycle_id")) > 0
        ]
        lifecycle_ids = sorted({
            safe_int((plan.get("coverage") or {}).get("lifecycle_id")) for plan in normalized
        })
        if not lifecycle_ids:
            return {"lifecycles": 0, "links": 0, "coverages": 0}
        placeholders = ",".join("?" for _ in lifecycle_ids)
        existing_rows = conn.execute(
            f"SELECT lifecycle_id, outcome_id, signal_id, is_primary, created_at "
            f"FROM lifecycle_outcome_links WHERE lifecycle_id IN ({placeholders})",
            lifecycle_ids,
        ).fetchall()
        existing_created = {
            (safe_int(row["lifecycle_id"]), safe_int(row["outcome_id"])): str(row["created_at"] or "")
            for row in existing_rows
        }
        now = _iso()
        link_rows: list[dict[str, Any]] = []
        coverage_rows: list[dict[str, Any]] = []
        for plan in normalized:
            coverage = dict(plan.get("coverage") or {})
            lifecycle_id = safe_int(coverage.get("lifecycle_id"))
            planned_links = [dict(item) for item in list(plan.get("links") or [])]
            primary_id = safe_int(coverage.get("primary_outcome_id"))
            coverage["primary_outcome_id"] = primary_id or None
            if isinstance(plan.get("coverage"), dict):
                plan["coverage"]["primary_outcome_id"] = primary_id or None
            for raw in planned_links:
                outcome_id = safe_int(raw.get("outcome_id"))
                symbol = normalize_lifecycle_symbol(raw.get("symbol"))
                horizon = str(raw.get("horizon") or "")
                if outcome_id <= 0 or not symbol or not horizon:
                    continue
                link_rows.append({
                    "lifecycle_id": lifecycle_id,
                    "symbol": symbol,
                    "signal_id": safe_int(raw.get("signal_id")) or None,
                    "lifecycle_event_id": safe_int(raw.get("lifecycle_event_id")) or None,
                    "outcome_id": outcome_id,
                    "horizon": horizon,
                    "outcome_status": str(raw.get("outcome_status") or "missing"),
                    "link_role": str(raw.get("link_role") or "fallback"),
                    "link_method": str(raw.get("link_method") or "symbol_time_module"),
                    "link_confidence": max(0.0, min(1.0, safe_float(raw.get("link_confidence")) or 0.0)),
                    "signal_time": raw.get("signal_time"),
                    "outcome_time": raw.get("outcome_time"),
                    "is_primary": int(outcome_id == primary_id),
                    "created_at": existing_created.get((lifecycle_id, outcome_id)) or now,
                    "updated_at": now,
                })
            coverage_rows.append({
                **coverage,
                "symbol": normalize_lifecycle_symbol(coverage.get("symbol")),
                "reasons_json": _json_dumps(coverage.get("reasons") or {}),
                "calculated_at": str(coverage.get("calculated_at") or now),
                "updated_at": now,
            })
        if replace_links:
            conn.execute(
                "CREATE TEMP TABLE IF NOT EXISTS _lifecycle_outcome_plan_links ("
                "lifecycle_id INTEGER NOT NULL, outcome_id INTEGER NOT NULL, "
                "PRIMARY KEY(lifecycle_id, outcome_id)) WITHOUT ROWID"
            )
            conn.execute("DELETE FROM _lifecycle_outcome_plan_links")
            if link_rows:
                conn.executemany(
                    "INSERT OR IGNORE INTO _lifecycle_outcome_plan_links(lifecycle_id, outcome_id) VALUES (?, ?)",
                    [(safe_int(row["lifecycle_id"]), safe_int(row["outcome_id"])) for row in link_rows],
                )
            conn.execute(
                f"DELETE FROM lifecycle_outcome_links "
                f"WHERE lifecycle_id IN ({placeholders}) "
                "AND NOT EXISTS (SELECT 1 FROM _lifecycle_outcome_plan_links planned "
                "WHERE planned.lifecycle_id=lifecycle_outcome_links.lifecycle_id "
                "AND planned.outcome_id=lifecycle_outcome_links.outcome_id)",
                lifecycle_ids,
            )
        if link_rows:
            linked_ids = sorted({safe_int(item["lifecycle_id"]) for item in link_rows})
            linked_placeholders = ",".join("?" for _ in linked_ids)
            conn.execute(
                f"UPDATE lifecycle_outcome_links SET is_primary=0 "
                f"WHERE lifecycle_id IN ({linked_placeholders})",
                linked_ids,
            )
        link_columns = (
            "lifecycle_id", "symbol", "signal_id", "lifecycle_event_id", "outcome_id",
            "horizon", "outcome_status", "link_role", "link_method", "link_confidence",
            "signal_time", "outcome_time", "is_primary", "created_at", "updated_at",
        )
        if link_rows:
            assignments = ", ".join(
                f"{key}=excluded.{key}" for key in link_columns
                if key not in {"lifecycle_id", "outcome_id", "created_at"}
            )
            conn.executemany(
                f"INSERT INTO lifecycle_outcome_links ({', '.join(link_columns)}) "
                f"VALUES ({', '.join(':'+key for key in link_columns)}) "
                f"ON CONFLICT(lifecycle_id, outcome_id) DO UPDATE SET {assignments}",
                [{key: row.get(key) for key in link_columns} for row in link_rows],
            )
        coverage_columns = (
            "lifecycle_id", "symbol", "candidate_signal_count", "linked_signal_count",
            "linked_outcome_count", "primary_outcome_id", "horizon_1h_status",
            "horizon_4h_status", "horizon_24h_status", "horizon_72h_status",
            "linked_horizon_count", "mature_horizon_count", "link_coverage_ratio",
            "maturity_ratio", "coverage_label", "maturity_label", "unlinked_reason",
            "reasons_json", "calculated_at", "updated_at",
        )
        assignments = ", ".join(
            f"{key}=excluded.{key}" for key in coverage_columns if key != "lifecycle_id"
        )
        conn.executemany(
            f"INSERT INTO lifecycle_outcome_coverage ({', '.join(coverage_columns)}) "
            f"VALUES ({', '.join(':'+key for key in coverage_columns)}) "
            f"ON CONFLICT(lifecycle_id) DO UPDATE SET {assignments}",
            [{key: row.get(key) for key in coverage_columns} for row in coverage_rows],
        )
        return {"lifecycles": len(normalized), "links": len(link_rows), "coverages": len(coverage_rows)}

    def get_outcome_coverage(
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
                return self.get_outcome_coverage(lifecycle_id, normalized, conn=owned)
        if lifecycle_id:
            row = conn.execute(
                "SELECT * FROM lifecycle_outcome_coverage WHERE lifecycle_id = ?", (safe_int(lifecycle_id),)
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM lifecycle_outcome_coverage WHERE symbol = ?", (normalized,)
            ).fetchone()
        return _deserialize(row, OUTCOME_COVERAGE_JSON_FIELDS)

    def list_outcome_links(
        self,
        lifecycle_id: int,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        if conn is None:
            with self.connect() as owned:
                return self.list_outcome_links(lifecycle_id, conn=owned)
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM lifecycle_outcome_links WHERE lifecycle_id = ? "
                "ORDER BY is_primary DESC, signal_time, signal_id, horizon, outcome_id",
                (safe_int(lifecycle_id),),
            ).fetchall()
        ]

    def upsert_outcome_candidates(
        self,
        records: list[dict[str, Any]],
        *,
        preserve_progress: bool = True,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, int]:
        """Batch-upsert candidate classifications in one transaction.

        Refresh jobs may rediscover a candidate while another worker owns it.
        ``preserve_progress`` keeps terminal/success and live processing states
        unless the caller supplies a newer resolved Outcome state.
        """

        if conn is None:
            with self.transaction() as owned:
                return self.upsert_outcome_candidates(
                    records,
                    preserve_progress=preserve_progress,
                    conn=owned,
                )
        incoming = [dict(item) for item in records if str(item.get("candidate_key") or "").strip()]
        if not incoming:
            return {"processed": 0, "inserted": 0, "updated": 0}
        keys = sorted({str(item.get("candidate_key") or "").strip() for item in incoming})
        existing: dict[str, dict[str, Any]] = {}
        for offset in range(0, len(keys), 800):
            chunk = keys[offset : offset + 800]
            placeholders = ",".join("?" for _ in chunk)
            for row in conn.execute(
                f"SELECT * FROM lifecycle_outcome_candidates WHERE candidate_key IN ({placeholders})",
                chunk,
            ).fetchall():
                existing[str(row["candidate_key"])] = dict(row)
        now = _iso()
        terminal_statuses = {"success", "terminal_ineligible", "terminal_unavailable", "terminal_error"}
        rows: list[dict[str, Any]] = []
        for raw in incoming:
            key = str(raw.get("candidate_key") or "").strip()
            previous = existing.get(key, {})
            row = dict(raw)
            incoming_status = str(row.get("candidate_status") or "ready")
            previous_status = str(previous.get("candidate_status") or "")
            incoming_outcome_id = safe_int(row.get("outcome_id")) or None
            if preserve_progress and previous:
                preserve_terminal = (
                    previous_status in terminal_statuses
                    and incoming_outcome_id is None
                    and not (
                        previous_status == "terminal_ineligible"
                        and str(row.get("eligibility_status") or "") == "eligible"
                    )
                )
                if preserve_terminal:
                    for name in (
                        "candidate_status", "outcome_id", "is_terminal", "is_retryable",
                        "last_attempt_at", "next_retry_at", "source_status",
                        "last_error_code", "last_error_summary",
                    ):
                        row[name] = previous.get(name)
                elif previous_status == "processing" and incoming_status in {
                    "not_due", "ready", "queued", "linked", "retry_wait", "processing",
                }:
                    for name in (
                        "candidate_status", "outcome_id", "is_terminal", "is_retryable",
                        "last_attempt_at", "next_retry_at", "source_status",
                        "last_error_code", "last_error_summary",
                    ):
                        row[name] = previous.get(name)
            lifecycle_id = safe_int(row.get("lifecycle_id"))
            symbol = normalize_lifecycle_symbol(row.get("symbol"))
            horizon = str(row.get("horizon") or "").lower()
            if lifecycle_id <= 0 or not symbol or not horizon:
                raise ValueError("candidate lifecycle_id, symbol, and horizon are required")
            attempt_count = max(safe_int(previous.get("attempt_count")), safe_int(row.get("attempt_count")))
            rows.append({
                "candidate_key": key,
                "lifecycle_id": lifecycle_id,
                "lifecycle_event_id": safe_int(row.get("lifecycle_event_id")) or None,
                "signal_id": safe_int(row.get("signal_id")) or None,
                "symbol": symbol,
                "signal_time": row.get("signal_time"),
                "source_module": str(row.get("source_module") or ""),
                "source_template": str(row.get("source_template") or ""),
                "source_signal_type": str(row.get("source_signal_type") or ""),
                "horizon": horizon,
                "due_at": row.get("due_at"),
                "eligibility_status": str(row.get("eligibility_status") or "unknown"),
                "eligibility_reason": str(row.get("eligibility_reason") or ""),
                "candidate_status": str(row.get("candidate_status") or "ready"),
                "outcome_id": safe_int(row.get("outcome_id")) or None,
                "is_terminal": int(bool(row.get("is_terminal"))),
                "is_retryable": int(bool(row.get("is_retryable"))),
                "attempt_count": attempt_count,
                "last_attempt_at": row.get("last_attempt_at"),
                "next_retry_at": row.get("next_retry_at"),
                "source_status": str(row.get("source_status") or ""),
                "last_error_code": str(row.get("last_error_code") or "")[:80],
                "last_error_summary": str(row.get("last_error_summary") or "")[:300],
                "created_at": str(previous.get("created_at") or row.get("created_at") or now),
                "updated_at": now,
            })
        columns = (
            "candidate_key", "lifecycle_id", "lifecycle_event_id", "signal_id", "symbol",
            "signal_time", "source_module", "source_template", "source_signal_type", "horizon",
            "due_at", "eligibility_status", "eligibility_reason", "candidate_status", "outcome_id",
            "is_terminal", "is_retryable", "attempt_count", "last_attempt_at", "next_retry_at",
            "source_status", "last_error_code", "last_error_summary", "created_at", "updated_at",
        )
        assignments = ", ".join(
            f"{name}=excluded.{name}" for name in columns if name not in {"candidate_key", "created_at"}
        )
        conn.executemany(
            f"INSERT INTO lifecycle_outcome_candidates ({', '.join(columns)}) "
            f"VALUES ({', '.join(':'+name for name in columns)}) "
            f"ON CONFLICT(candidate_key) DO UPDATE SET {assignments}",
            [{name: row.get(name) for name in columns} for row in rows],
        )
        inserted = sum(1 for row in rows if row["candidate_key"] not in existing)
        return {"processed": len(rows), "inserted": inserted, "updated": len(rows) - inserted}

    def list_outcome_candidates(
        self,
        *,
        lifecycle_id: int | None = None,
        symbol: str = "",
        horizon: str = "",
        eligibility_status: str = "",
        candidate_status: str = "",
        candidate_statuses: list[str] | tuple[str, ...] | None = None,
        module: str = "",
        due_before: datetime | str | None = None,
        retry_due_before: datetime | str | None = None,
        exclude_eligibility_reasons: list[str] | tuple[str, ...] | None = None,
        limit: int = 1000,
        offset: int = 0,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        if conn is None:
            with self.connect() as owned:
                return self.list_outcome_candidates(
                    lifecycle_id=lifecycle_id, symbol=symbol, horizon=horizon,
                    eligibility_status=eligibility_status, candidate_status=candidate_status,
                    candidate_statuses=candidate_statuses, module=module,
                    due_before=due_before, retry_due_before=retry_due_before,
                    exclude_eligibility_reasons=exclude_eligibility_reasons,
                    limit=limit, offset=offset, conn=owned,
                )
        clauses: list[str] = []
        params: list[Any] = []
        if lifecycle_id is not None:
            clauses.append("lifecycle_id=?")
            params.append(safe_int(lifecycle_id))
        if symbol:
            normalized = normalize_lifecycle_symbol(symbol)
            if not normalized:
                return []
            clauses.append("symbol=?")
            params.append(normalized)
        for column, value in (
            ("horizon", horizon),
            ("eligibility_status", eligibility_status),
            ("candidate_status", candidate_status),
            ("source_module", module),
        ):
            if str(value or "").strip():
                clauses.append(f"{column}=?")
                params.append(str(value).strip().lower())
        normalized_statuses = sorted({
            str(value or "").strip().lower()
            for value in (candidate_statuses or ())
            if str(value or "").strip()
        })
        if normalized_statuses:
            clauses.append(f"candidate_status IN ({','.join('?' for _ in normalized_statuses)})")
            params.extend(normalized_statuses)
        if due_before is not None:
            parsed_due = _parse_time(due_before)
            if parsed_due is not None:
                clauses.append("due_at IS NOT NULL AND due_at<=?")
                params.append(_iso(parsed_due))
        if retry_due_before is not None:
            parsed_retry = _parse_time(retry_due_before)
            if parsed_retry is not None:
                clauses.append("(candidate_status!='retry_wait' OR next_retry_at IS NULL OR next_retry_at<=?)")
                params.append(_iso(parsed_retry))
        excluded_reasons = sorted({
            str(value or "").strip()
            for value in (exclude_eligibility_reasons or ())
            if str(value or "").strip()
        })
        if excluded_reasons:
            clauses.append(
                f"COALESCE(eligibility_reason,'') NOT IN ({','.join('?' for _ in excluded_reasons)})"
            )
            params.extend(excluded_reasons)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([max(1, min(safe_int(limit, 1000), 5000)), max(0, safe_int(offset))])
        return [
            dict(row)
            for row in conn.execute(
                f"SELECT * FROM lifecycle_outcome_candidates {where} "
                "ORDER BY due_at, lifecycle_id, signal_id, horizon LIMIT ? OFFSET ?",
                params,
            ).fetchall()
        ]

    def get_outcome_candidate(
        self,
        candidate_key: str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        if conn is None:
            with self.connect() as owned:
                return self.get_outcome_candidate(candidate_key, conn=owned)
        row = conn.execute(
            "SELECT * FROM lifecycle_outcome_candidates WHERE candidate_key=?",
            (str(candidate_key or ""),),
        ).fetchone()
        return dict(row) if row is not None else None

    def claim_outcome_candidates(
        self,
        candidate_keys: list[str],
        *,
        now: datetime | None = None,
        return_keys: bool = False,
        conn: sqlite3.Connection | None = None,
    ) -> int | list[str]:
        """Atomically claim ready/retry candidates and increment attempts."""

        if conn is None:
            with self.transaction() as owned:
                return self.claim_outcome_candidates(
                    candidate_keys, now=now, return_keys=return_keys, conn=owned,
                )
        keys = sorted({str(key or "").strip() for key in candidate_keys if str(key or "").strip()})
        if not keys:
            return [] if return_keys else 0
        claimed_at = _iso(now)
        claimed_keys: list[str] = []
        for offset in range(0, len(keys), 800):
            chunk = keys[offset : offset + 800]
            placeholders = ",".join("?" for _ in chunk)
            claimable = [
                str(row[0])
                for row in conn.execute(
                    f"SELECT candidate_key FROM lifecycle_outcome_candidates "
                    f"WHERE candidate_key IN ({placeholders}) AND eligibility_status='eligible' "
                    "AND candidate_status IN ('ready','queued','linked','retry_wait') "
                    "AND (candidate_status!='retry_wait' OR next_retry_at IS NULL OR next_retry_at<=?)",
                    [*chunk, claimed_at],
                ).fetchall()
            ]
            if not claimable:
                continue
            claim_placeholders = ",".join("?" for _ in claimable)
            conn.execute(
                f"UPDATE lifecycle_outcome_candidates SET candidate_status='processing', "
                "attempt_count=attempt_count+1, last_attempt_at=?, next_retry_at=NULL, updated_at=? "
                f"WHERE candidate_key IN ({claim_placeholders})",
                [claimed_at, claimed_at, *claimable],
            )
            claimed_keys.extend(claimable)
        return claimed_keys if return_keys else len(claimed_keys)

    def recover_stale_outcome_candidates(
        self,
        stale_before: datetime | str,
        *,
        conn: sqlite3.Connection | None = None,
    ) -> int:
        if conn is None:
            with self.transaction() as owned:
                return self.recover_stale_outcome_candidates(stale_before, conn=owned)
        threshold = _iso(_parse_time(stale_before))
        now = _iso()
        before = conn.total_changes
        conn.execute(
            "UPDATE lifecycle_outcome_candidates SET candidate_status='ready', is_retryable=1, "
            "last_error_code='processing_stale', "
            "last_error_summary='Interrupted processing was recovered for retry.', updated_at=? "
            "WHERE candidate_status='processing' AND (last_attempt_at IS NULL OR last_attempt_at < ?)",
            (now, threshold),
        )
        return max(0, conn.total_changes - before)

    def write_calibration_report(
        self,
        record: dict[str, Any],
        metrics: list[dict[str, Any]],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Persist one immutable calibration report and its metrics atomically."""

        if conn is None:
            with self.transaction() as owned:
                return self.write_calibration_report(record, metrics, conn=owned)
        report_version = str(record.get("report_version") or "").strip()
        model_version = str(record.get("model_version") or "").strip()
        source = str(record.get("source_signature") or "").strip()
        if not report_version or not model_version or not source:
            raise ValueError("report_version, model_version, and source_signature are required")
        generated_at = str(record.get("generated_at") or _iso())
        summary = record.get("summary") if isinstance(record.get("summary"), dict) else {}
        recommendations = record.get("recommendations")
        if not isinstance(recommendations, list):
            recommendations = []
        cursor = conn.execute(
            "INSERT INTO calibration_reports ("
            "report_version,model_version,generated_at,sample_count,mature_sample_count,"
            "summary_json,recommendations_json,source_signature"
            ") VALUES (?,?,?,?,?,?,?,?)",
            (
                report_version,
                model_version,
                generated_at,
                max(0, safe_int(record.get("sample_count"))),
                max(0, safe_int(record.get("mature_sample_count"))),
                _json_dumps(summary),
                _json_dumps(recommendations),
                source,
            ),
        )
        report_id = safe_int(cursor.lastrowid)
        rows: list[tuple[Any, ...]] = []
        for raw in metrics:
            item = dict(raw)
            metric_type = str(item.get("metric_type") or "").strip()
            metric_key = str(item.get("metric_key") or item.get("key") or "").strip()
            if not metric_type or not metric_key:
                continue
            rows.append((
                report_id,
                metric_type,
                metric_key,
                max(0, safe_int(item.get("sample_count"))),
                safe_float(item.get("success_ratio")),
                safe_float(item.get("avg_return_pct", item.get("avg_return"))),
                safe_float(item.get("avg_max_drawdown_pct", item.get("avg_drawdown"))),
                _json_dumps(item),
                generated_at,
            ))
        if rows:
            conn.executemany(
                "INSERT INTO calibration_metrics ("
                "report_id,metric_type,metric_key,sample_count,success_ratio,avg_return,"
                "avg_drawdown,metrics_json,created_at"
                ") VALUES (?,?,?,?,?,?,?,?,?)",
                rows,
            )
        return {
            "id": report_id,
            "report_version": report_version,
            "model_version": model_version,
            "generated_at": generated_at,
            "sample_count": max(0, safe_int(record.get("sample_count"))),
            "mature_sample_count": max(0, safe_int(record.get("mature_sample_count"))),
            "summary": dict(summary),
            "recommendations": list(recommendations),
            "source_signature": source,
            "metrics": [dict(item) for item in metrics if item.get("metric_type")],
        }

    def latest_calibration_report(
        self,
        *,
        report_version: str = "",
        model_version: str = "",
        source_signature: str = "",
        conn: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        if conn is None:
            with self.connect() as owned:
                return self.latest_calibration_report(
                    report_version=report_version,
                    model_version=model_version,
                    source_signature=source_signature,
                    conn=owned,
                )
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("report_version", report_version),
            ("model_version", model_version),
            ("source_signature", source_signature),
        ):
            if str(value or "").strip():
                clauses.append(f"{column}=?")
                params.append(str(value).strip())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        row = conn.execute(
            f"SELECT * FROM calibration_reports {where} ORDER BY id DESC LIMIT 1",
            params,
        ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["summary"] = _json_loads(result.pop("summary_json", None), {})
        result["recommendations"] = _json_loads(result.pop("recommendations_json", None), [])
        result["metrics"] = [
            _json_loads(item[0], {})
            for item in conn.execute(
                "SELECT metrics_json FROM calibration_metrics WHERE report_id=? "
                "ORDER BY metric_type,metric_key,id",
                (safe_int(result.get("id")),),
            ).fetchall()
        ]
        return result

    def write_optimization_bundle(
        self,
        scenarios: list[dict[str, Any]],
        runs: list[dict[str, Any]],
        *,
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        """Persist scenario metadata, runs, and metrics in one transaction."""

        if conn is None:
            with self.transaction() as owned:
                return self.write_optimization_bundle(scenarios, runs, conn=owned)
        now = _iso()
        for raw in scenarios:
            scenario = dict(raw)
            key = str(scenario.get("scenario_key") or "").strip()
            scenario_type = str(scenario.get("scenario_type") or "").strip()
            version = str(scenario.get("optimization_version") or "").strip()
            if not key or not scenario_type or not version:
                raise ValueError("scenario_key, scenario_type, and optimization_version are required")
            conn.execute(
                "INSERT INTO optimization_scenarios ("
                "scenario_key,scenario_name,scenario_type,name,description,base_model_version,"
                "scenario_version,parameters_json,candidate_params_json,built_in,"
                "optimization_version,status,created_at,updated_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(scenario_key) DO UPDATE SET "
                "scenario_name=excluded.scenario_name,scenario_type=excluded.scenario_type,"
                "name=excluded.name,description=excluded.description,"
                "base_model_version=excluded.base_model_version,scenario_version=excluded.scenario_version,"
                "parameters_json=excluded.parameters_json,candidate_params_json=excluded.candidate_params_json,"
                "built_in=excluded.built_in,optimization_version=excluded.optimization_version,"
                "status=excluded.status,updated_at=excluded.updated_at",
                (
                    key,
                    str(scenario.get("scenario_name") or scenario.get("name") or key),
                    scenario_type,
                    str(scenario.get("name") or key),
                    str(scenario.get("description") or ""),
                    str(scenario.get("base_model_version") or scenario.get("model_version") or ""),
                    str(scenario.get("scenario_version") or version),
                    _json_dumps(scenario.get("parameters") or scenario.get("candidate_params") or {}),
                    _json_dumps(scenario.get("candidate_params") or {}),
                    1 if scenario.get("built_in", True) else 0,
                    version,
                    str(scenario.get("status") or "draft"),
                    str(scenario.get("created_at") or now),
                    str(scenario.get("updated_at") or now),
                ),
            )
        scenario_ids = {
            str(row["scenario_key"]): safe_int(row["id"])
            for row in conn.execute(
                "SELECT id,scenario_key FROM optimization_scenarios"
            ).fetchall()
        }
        stored: list[dict[str, Any]] = []
        for raw in runs:
            run = dict(raw)
            scenario_key = str(run.get("scenario_key") or "").strip()
            run_key = str(run.get("run_key") or "").strip()
            scenario_id = scenario_ids.get(scenario_key, 0)
            if not scenario_id or not run_key:
                raise ValueError("known scenario_key and run_key are required")
            existing = conn.execute(
                "SELECT id FROM optimization_runs WHERE run_key=?",
                (run_key,),
            ).fetchone()
            if existing is not None:
                stored.append({**run, "id": safe_int(existing[0]), "cached": True})
                continue
            generated_at = str(run.get("generated_at") or now)
            cursor = conn.execute(
                "INSERT INTO optimization_runs ("
                "scenario_id,run_key,optimization_version,model_version,source_signature,scope_json,"
                "sample_count,mature_sample_count,selected_sample_count,confidence,confidence_label,"
                "status,production_json,candidate_json,delta_json,recommendations_json,readiness_json,"
                "report_json,result_json,started_at,finished_at,generated_at,created_at"
                ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    scenario_id,
                    run_key,
                    str(run.get("optimization_version") or ""),
                    str(run.get("model_version") or ""),
                    str(run.get("source_signature") or ""),
                    _json_dumps(run.get("scope") or {}),
                    max(0, safe_int(run.get("sample_count"))),
                    max(0, safe_int(run.get("mature_sample_count"))),
                    max(0, safe_int(run.get("selected_sample_count"))),
                    safe_float(run.get("confidence")) or 0.0,
                    str(run.get("confidence_label") or "low_confidence"),
                    str(run.get("status") or "insufficient_samples"),
                    _json_dumps(run.get("production") or {}),
                    _json_dumps(run.get("candidate") or {}),
                    _json_dumps(run.get("delta") or {}),
                    _json_dumps(run.get("recommendations") or []),
                    _json_dumps(run.get("readiness") or {}),
                    _json_dumps(run.get("report") or {}),
                    _json_dumps(run.get("result") or run.get("report") or {}),
                    str(run.get("started_at") or generated_at),
                    str(run.get("finished_at") or generated_at),
                    generated_at,
                    str(run.get("created_at") or generated_at),
                ),
            )
            run_id = safe_int(cursor.lastrowid)
            metric_rows: list[tuple[Any, ...]] = []
            for metric in run.get("metrics") or []:
                if not isinstance(metric, dict):
                    continue
                scope = str(metric.get("metric_scope") or "").strip()
                key = str(metric.get("metric_key") or "").strip()
                if not scope or not key:
                    continue
                metric_rows.append((
                    run_id,
                    scope,
                    str(metric.get("metric_type") or scope),
                    key,
                    max(0, safe_int(metric.get("sample_count"))),
                    safe_float(metric.get("production_value")),
                    safe_float(metric.get("candidate_value")),
                    safe_float(metric.get("delta_value")),
                    safe_float(metric.get("metric_value")),
                    _json_dumps(metric.get("metrics") or metric),
                    generated_at,
                ))
            if metric_rows:
                conn.executemany(
                    "INSERT INTO optimization_metrics ("
                    "run_id,metric_scope,metric_type,metric_key,sample_count,production_value,"
                    "candidate_value,delta_value,metric_value,metrics_json,created_at"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    metric_rows,
                )
            stored.append({**run, "id": run_id, "cached": False})
        return stored

    def latest_optimization_runs(
        self,
        *,
        scenario_keys: list[str] | tuple[str, ...] = (),
        optimization_version: str = "",
        model_version: str = "",
        source_signature: str = "",
        conn: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        if conn is None:
            with self.connect() as owned:
                return self.latest_optimization_runs(
                    scenario_keys=scenario_keys,
                    optimization_version=optimization_version,
                    model_version=model_version,
                    source_signature=source_signature,
                    conn=owned,
                )
        clauses: list[str] = []
        params: list[Any] = []
        normalized_keys = [str(key).strip() for key in scenario_keys if str(key).strip()]
        if normalized_keys:
            placeholders = ",".join("?" for _ in normalized_keys)
            clauses.append(f"s.scenario_key IN ({placeholders})")
            params.extend(normalized_keys)
        for column, value in (
            ("r.optimization_version", optimization_version),
            ("r.model_version", model_version),
            ("r.source_signature", source_signature),
        ):
            if str(value or "").strip():
                clauses.append(f"{column}=?")
                params.append(str(value).strip())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = conn.execute(
            "SELECT r.*,s.scenario_key,s.scenario_type,s.name AS scenario_name,"
            "s.description AS scenario_description,s.candidate_params_json "
            "FROM optimization_runs r JOIN optimization_scenarios s ON s.id=r.scenario_id "
            f"{where} ORDER BY r.id DESC",
            params,
        ).fetchall()
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            result = dict(row)
            key = str(result.get("scenario_key") or "")
            if key in latest:
                continue
            for stored_name, public_name, default in (
                ("scope_json", "scope", {}),
                ("production_json", "production", {}),
                ("candidate_json", "candidate", {}),
                ("delta_json", "delta", {}),
                ("recommendations_json", "recommendations", []),
                ("readiness_json", "readiness", {}),
                ("report_json", "report", {}),
                ("result_json", "result", {}),
                ("parameters_json", "parameters", {}),
                ("candidate_params_json", "candidate_params", {}),
            ):
                result[public_name] = _json_loads(result.pop(stored_name, None), default)
            result["metrics"] = []
            latest[key] = result
        metrics_by_run: dict[int, list[dict[str, Any]]] = {}
        run_ids = sorted({safe_int(item.get("id")) for item in latest.values() if safe_int(item.get("id")) > 0})
        if run_ids:
            placeholders = ",".join("?" for _ in run_ids)
            for metric in conn.execute(
                "SELECT run_id,metrics_json FROM optimization_metrics "
                f"WHERE run_id IN ({placeholders}) ORDER BY run_id,metric_scope,metric_key,id",
                run_ids,
            ).fetchall():
                metrics_by_run.setdefault(safe_int(metric[0]), []).append(
                    _json_loads(metric[1], {})
                )
        for item in latest.values():
            item["metrics"] = metrics_by_run.get(safe_int(item.get("id")), [])
        return [latest[key] for key in normalized_keys if key in latest] if normalized_keys else list(latest.values())

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
