from __future__ import annotations

import json
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from .config import BASE_DIR, Settings
from .signal_store import SignalEventStore
from .web_services.api_core import normalize_symbol_filter, redact_api_payload


DEFAULT_OUTCOME_DB_PATH = BASE_DIR / "data" / "outcomes.db"
OUTCOME_WINDOWS: dict[str, int] = {
    "1h": 3600,
    "4h": 14400,
    "24h": 86400,
    "72h": 259200,
}
VALID_DATA_STATUSES = {"pending", "ready", "success", "unavailable", "error"}
VALID_RESULTS = {"表现较强", "小幅走强", "震荡", "小幅走弱", "明显回撤", "数据不足"}
OUTCOME_COLUMNS = (
    "id",
    "signal_id",
    "symbol",
    "coin",
    "signal_time",
    "horizon",
    "horizon_sec",
    "due_time",
    "direction",
    "entry_price",
    "future_price",
    "max_high_price",
    "min_low_price",
    "final_return_pct",
    "max_gain_pct",
    "max_drawdown_pct",
    "result_label",
    "result_tone",
    "decision_code",
    "decision_label",
    "decision_confidence",
    "risk_level",
    "module",
    "signal_type",
    "signal_score",
    "signal_stage",
    "data_status",
    "data_source",
    "error",
    "created_at",
    "updated_at",
)
SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,24}USDT$")
INVALID_PRICE_ERROR_PATTERNS = (
    "HTTP Error 400",
    "Bad Request",
    "invalid symbol",
    "symbol not found",
    "empty kline data",
)
PRICE_UNAVAILABLE_REASON = "价格源不支持该交易对或暂无 K 线数据"
PREFIX_1000_UNAVAILABLE_REASON = "当前价格源不支持 1000 前缀交易对，后续可接入公开合约 K 线补齐"

KlineFetcher = Callable[[str, int, int, str, int], list[dict[str, float]]]


def _now_ts() -> int:
    return int(time.time())


def _iso(ts: int | float | None = None) -> str:
    value = _now_ts() if ts is None else int(ts)
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return int(default)


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _coin(symbol: str) -> str:
    value = str(symbol or "").upper()
    return value[:-4] if value.endswith("USDT") else value


def normalize_outcome_symbol(value: Any) -> str:
    symbol = normalize_symbol_filter(value).get("symbol", "")
    return symbol if SYMBOL_RE.fullmatch(symbol) else ""


def _is_1000_prefix_symbol(symbol: str) -> bool:
    return bool(re.fullmatch(r"1000[A-Z0-9]{2,20}USDT", str(symbol or "").upper()))


def price_unavailable_reason(symbol: str) -> str:
    if _is_1000_prefix_symbol(symbol):
        # v1.72.2 keeps spot Binance as the active source. A future version can add public futures K lines here.
        return PREFIX_1000_UNAVAILABLE_REASON
    return PRICE_UNAVAILABLE_REASON


def is_price_unavailable_error(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError) and int(getattr(exc, "code", 0) or 0) == 400:
        return True
    message = f"{type(exc).__name__}: {exc}".lower()
    return any(pattern.lower() in message for pattern in INVALID_PRICE_ERROR_PATTERNS)


def _unavailable_summary(symbol: str, horizon: str, source: str, reason: str) -> str:
    prefix = f"{str(symbol or '').upper()} {str(horizon or '')}".strip()
    source_text = str(source or "price-source").strip()
    return f"{prefix}: {source_text} {reason}".strip()


def safe_outcome_windows(values: tuple[str, ...] | list[str] | str | None = None) -> dict[str, int]:
    if values is None:
        return dict(OUTCOME_WINDOWS)
    if isinstance(values, str):
        raw_values = [part.strip() for part in values.split(",") if part.strip()]
    else:
        raw_values = [str(part or "").strip() for part in values if str(part or "").strip()]
    result: dict[str, int] = {}
    for raw in raw_values:
        key = raw.lower()
        if key in OUTCOME_WINDOWS:
            result[key] = OUTCOME_WINDOWS[key]
    return result or dict(OUTCOME_WINDOWS)


def interval_for_horizon(horizon_sec: int) -> str:
    return "1m" if int(horizon_sec or 0) <= OUTCOME_WINDOWS["4h"] else "5m"


def outcome_result_label(
    *,
    final_return_pct: float | None,
    max_gain_pct: float | None,
    max_drawdown_pct: float | None,
) -> dict[str, str]:
    if final_return_pct is None or max_gain_pct is None or max_drawdown_pct is None:
        return {"result_label": "数据不足", "result_tone": "muted"}
    if final_return_pct >= 3 or max_gain_pct >= 5:
        return {"result_label": "表现较强", "result_tone": "good"}
    if final_return_pct >= 1:
        return {"result_label": "小幅走强", "result_tone": "info"}
    if final_return_pct <= -3 or max_drawdown_pct <= -5:
        return {"result_label": "明显回撤", "result_tone": "bad"}
    if final_return_pct <= -1:
        return {"result_label": "小幅走弱", "result_tone": "warn"}
    return {"result_label": "震荡", "result_tone": "neutral"}


def calculate_outcome_metrics(klines: list[dict[str, float]]) -> dict[str, Any]:
    if not klines:
        return {
            "entry_price": None,
            "future_price": None,
            "max_high_price": None,
            "min_low_price": None,
            "final_return_pct": None,
            "max_gain_pct": None,
            "max_drawdown_pct": None,
            **outcome_result_label(final_return_pct=None, max_gain_pct=None, max_drawdown_pct=None),
        }
    entry = _safe_float(klines[0].get("close"))
    future = _safe_float(klines[-1].get("close"))
    highs = [_safe_float(item.get("high")) for item in klines]
    lows = [_safe_float(item.get("low")) for item in klines]
    highs = [value for value in highs if value is not None]
    lows = [value for value in lows if value is not None]
    if entry is None or entry <= 0 or future is None or not highs or not lows:
        return {
            "entry_price": entry,
            "future_price": future,
            "max_high_price": max(highs) if highs else None,
            "min_low_price": min(lows) if lows else None,
            "final_return_pct": None,
            "max_gain_pct": None,
            "max_drawdown_pct": None,
            **outcome_result_label(final_return_pct=None, max_gain_pct=None, max_drawdown_pct=None),
        }
    max_high = max(highs)
    min_low = min(lows)
    final_return = (future - entry) / entry * 100.0
    max_gain = (max_high - entry) / entry * 100.0
    max_drawdown = (min_low - entry) / entry * 100.0
    result = outcome_result_label(
        final_return_pct=final_return,
        max_gain_pct=max_gain,
        max_drawdown_pct=max_drawdown,
    )
    return {
        "entry_price": round(entry, 10),
        "future_price": round(future, 10),
        "max_high_price": round(max_high, 10),
        "min_low_price": round(min_low, 10),
        "final_return_pct": round(final_return, 4),
        "max_gain_pct": round(max_gain, 4),
        "max_drawdown_pct": round(max_drawdown, 4),
        **result,
    }


def fetch_binance_klines(symbol: str, start_ts: int, end_ts: int, interval: str, timeout_sec: int) -> list[dict[str, float]]:
    params = urllib.parse.urlencode({
        "symbol": str(symbol or "").upper(),
        "interval": interval,
        "startTime": int(start_ts) * 1000,
        "endTime": int(end_ts) * 1000,
        "limit": 1000,
    })
    url = f"https://api.binance.com/api/v3/klines?{params}"
    with urllib.request.urlopen(url, timeout=max(1, int(timeout_sec or 10))) as response:
        raw = response.read()
    rows = json.loads(raw.decode("utf-8"))
    if not isinstance(rows, list):
        return []
    result: list[dict[str, float]] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 5:
            continue
        result.append({
            "open_time": float(row[0]) / 1000.0,
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
        })
    return result


def _row_to_outcome(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {key: row[key] for key in row.keys()}


@dataclass
class OutcomeStore:
    db_path: Path = DEFAULT_OUTCOME_DB_PATH

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
        except Exception:
            conn.rollback()
            raise
        else:
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
                CREATE TABLE IF NOT EXISTS signal_outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    coin TEXT,
                    signal_time TEXT NOT NULL,
                    horizon TEXT NOT NULL,
                    horizon_sec INTEGER NOT NULL,
                    due_time TEXT NOT NULL,
                    direction TEXT NOT NULL DEFAULT 'long',
                    entry_price REAL,
                    future_price REAL,
                    max_high_price REAL,
                    min_low_price REAL,
                    final_return_pct REAL,
                    max_gain_pct REAL,
                    max_drawdown_pct REAL,
                    result_label TEXT,
                    result_tone TEXT,
                    decision_code TEXT,
                    decision_label TEXT,
                    decision_confidence INTEGER,
                    risk_level TEXT,
                    module TEXT,
                    signal_type TEXT,
                    signal_score REAL,
                    signal_stage TEXT,
                    data_status TEXT NOT NULL DEFAULT 'pending',
                    data_source TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(signal_id, horizon)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_signal_outcomes_symbol ON signal_outcomes(symbol)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_signal_outcomes_horizon ON signal_outcomes(horizon)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_signal_outcomes_due_time ON signal_outcomes(due_time)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_signal_outcomes_status ON signal_outcomes(data_status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_signal_outcomes_decision ON signal_outcomes(decision_code)")
            if own_conn:
                conn.commit()
        finally:
            if own_conn:
                conn.close()

    def create_pending(
        self,
        signals: list[dict[str, Any]],
        windows: dict[str, int],
        *,
        dry_run: bool = False,
    ) -> int:
        rows: list[tuple[Any, ...]] = []
        now_text = _iso()
        for item in signals:
            signal_id = _safe_int(item.get("id"), 0)
            ts = _safe_int(item.get("ts"), 0)
            symbol = normalize_outcome_symbol(item.get("symbol"))
            if not signal_id or not ts or not symbol:
                continue
            for horizon, horizon_sec in windows.items():
                rows.append((
                    signal_id,
                    symbol,
                    _coin(symbol),
                    str(item.get("time") or _iso(ts)),
                    horizon,
                    int(horizon_sec),
                    _iso(ts + int(horizon_sec)),
                    "long",
                    str(item.get("module") or ""),
                    str(item.get("signal_type") or ""),
                    _safe_float(item.get("score")),
                    str(item.get("stage") or ""),
                    "pending",
                    now_text,
                    now_text,
                ))
        if dry_run or not rows:
            return len(rows)
        with self.connect() as conn:
            before = conn.total_changes
            conn.executemany(
                """
                INSERT OR IGNORE INTO signal_outcomes (
                    signal_id, symbol, coin, signal_time, horizon, horizon_sec, due_time, direction,
                    module, signal_type, signal_score, signal_stage, data_status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            return max(0, conn.total_changes - before)

    def repair_unavailable_errors(self, *, data_source: str = "binance", dry_run: bool = False) -> int:
        like_clauses = []
        params: dict[str, Any] = {
            "data_source": str(data_source or "binance").lower(),
            "result_label": "数据不足",
            "result_tone": "muted",
            "reason": PRICE_UNAVAILABLE_REASON,
            "updated_at": _iso(),
        }
        for index, pattern in enumerate(INVALID_PRICE_ERROR_PATTERNS):
            key = f"pattern_{index}"
            like_clauses.append(f"LOWER(COALESCE(error, '')) LIKE :{key}")
            params[key] = f"%{pattern.lower()}%"
        where = (
            "data_status = 'error' "
            "AND LOWER(COALESCE(data_source, '')) = :data_source "
            f"AND ({' OR '.join(like_clauses)})"
        )
        with self.connect() as conn:
            count = int(conn.execute(f"SELECT COUNT(*) FROM signal_outcomes WHERE {where}", params).fetchone()[0])
            if dry_run or count <= 0:
                return count
            conn.execute(
                f"""
                UPDATE signal_outcomes
                SET data_status = 'unavailable',
                    result_label = :result_label,
                    result_tone = :result_tone,
                    error = :reason,
                    updated_at = :updated_at
                WHERE {where}
                """,
                params,
            )
            return count

    def due_outcomes(
        self,
        *,
        now_ts: int | None = None,
        limit: int = 100,
        horizon: str = "",
        symbol: str = "",
        signal_ids: Iterable[int] | None = None,
    ) -> list[dict[str, Any]]:
        now_text = _iso(now_ts)
        clauses = ["data_status IN ('pending', 'ready')", "due_time <= :now"]
        params: dict[str, Any] = {"now": now_text, "limit": max(1, min(int(limit or 100), 1000))}
        if horizon:
            clauses.append("horizon = :horizon")
            params["horizon"] = str(horizon)
        normalized = normalize_outcome_symbol(symbol)
        if normalized:
            clauses.append("symbol = :symbol")
            params["symbol"] = normalized
        normalized_ids = sorted({_safe_int(value) for value in (signal_ids or ()) if _safe_int(value) > 0})
        if normalized_ids:
            placeholders = ", ".join(f":signal_id_{index}" for index in range(len(normalized_ids)))
            clauses.append(f"signal_id IN ({placeholders})")
            params.update({f"signal_id_{index}": value for index, value in enumerate(normalized_ids)})
        where = " AND ".join(clauses)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM signal_outcomes WHERE {where} ORDER BY due_time ASC, id ASC LIMIT :limit",
                params,
            ).fetchall()
        return [dict(row) for row in rows]

    def list_by_signal_ids(
        self,
        signal_ids: Iterable[int],
        *,
        horizons: Iterable[str] | None = None,
        columns: tuple[str, ...] | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> list[dict[str, Any]]:
        """Read Outcome rows for explicit signals with one request-scoped connection."""

        normalized_ids = sorted({_safe_int(value) for value in signal_ids if _safe_int(value) > 0})
        if not normalized_ids:
            return []
        selected_columns = tuple(column for column in (columns or OUTCOME_COLUMNS) if column in OUTCOME_COLUMNS)
        if "id" not in selected_columns:
            selected_columns = ("id", *selected_columns)
        projection = ", ".join(selected_columns)
        normalized_horizons = tuple(
            value for value in dict.fromkeys(str(item or "").strip().lower() for item in (horizons or ()))
            if value in OUTCOME_WINDOWS
        )
        context = self.connect() if connection is None else nullcontext(connection)
        result: list[dict[str, Any]] = []
        with context as conn:
            for offset in range(0, len(normalized_ids), 800):
                chunk = normalized_ids[offset : offset + 800]
                placeholders = ", ".join("?" for _ in chunk)
                params: list[Any] = list(chunk)
                horizon_clause = ""
                if normalized_horizons:
                    horizon_placeholders = ", ".join("?" for _ in normalized_horizons)
                    horizon_clause = f" AND horizon IN ({horizon_placeholders})"
                    params.extend(normalized_horizons)
                rows = conn.execute(
                    f"SELECT {projection} FROM signal_outcomes "
                    f"WHERE signal_id IN ({placeholders}){horizon_clause} "
                    "ORDER BY signal_id, horizon_sec, id",
                    params,
                ).fetchall()
                result.extend(dict(row) for row in rows)
        return result

    def reset_for_rebuild(
        self,
        signal_ids: Iterable[int],
        *,
        horizons: Iterable[str] | None = None,
        dry_run: bool = False,
    ) -> int:
        """Explicitly mark selected rows ready; never used by normal scans."""

        normalized_ids = sorted({_safe_int(value) for value in signal_ids if _safe_int(value) > 0})
        if not normalized_ids:
            return 0
        normalized_horizons = tuple(
            value for value in dict.fromkeys(str(item or "").strip().lower() for item in (horizons or ()))
            if value in OUTCOME_WINDOWS
        )
        with self.connect() as conn:
            total = 0
            for offset in range(0, len(normalized_ids), 800):
                chunk = normalized_ids[offset : offset + 800]
                placeholders = ", ".join("?" for _ in chunk)
                params: list[Any] = list(chunk)
                horizon_clause = ""
                if normalized_horizons:
                    horizon_placeholders = ", ".join("?" for _ in normalized_horizons)
                    horizon_clause = f" AND horizon IN ({horizon_placeholders})"
                    params.extend(normalized_horizons)
                count = int(conn.execute(
                    f"SELECT COUNT(*) FROM signal_outcomes WHERE signal_id IN ({placeholders}){horizon_clause}",
                    params,
                ).fetchone()[0])
                total += count
                if dry_run or count <= 0:
                    continue
                conn.execute(
                    f"""
                    UPDATE signal_outcomes
                    SET entry_price=NULL, future_price=NULL, max_high_price=NULL, min_low_price=NULL,
                        final_return_pct=NULL, max_gain_pct=NULL, max_drawdown_pct=NULL,
                        result_label=NULL, result_tone=NULL, data_status='ready',
                        data_source=NULL, error='', updated_at=?
                    WHERE signal_id IN ({placeholders}){horizon_clause}
                    """,
                    [_iso(), *params],
                )
            return total

    def update_outcome(self, outcome_id: int, values: dict[str, Any]) -> None:
        self.update_outcomes([(outcome_id, values)])

    def update_outcomes(self, outcomes: list[tuple[int, dict[str, Any]]]) -> None:
        """Update a scan batch atomically, rolling the whole batch back on failure."""
        if not outcomes:
            return
        allowed = {
            "entry_price",
            "future_price",
            "max_high_price",
            "min_low_price",
            "final_return_pct",
            "max_gain_pct",
            "max_drawdown_pct",
            "result_label",
            "result_tone",
            "decision_code",
            "decision_label",
            "decision_confidence",
            "risk_level",
            "data_status",
            "data_source",
            "error",
        }
        updated_at = _iso()
        with self.connect() as conn:
            for outcome_id, values in outcomes:
                updates = {key: value for key, value in values.items() if key in allowed}
                updates["updated_at"] = updated_at
                assignments = ", ".join(f"{key} = :{key}" for key in updates)
                params = {**updates, "id": int(outcome_id)}
                conn.execute(f"UPDATE signal_outcomes SET {assignments} WHERE id = :id", params)

    def list_outcomes(
        self,
        *,
        limit: int = 50,
        cursor: int | None = None,
        symbol: str = "",
        horizon: str = "",
        decision: str = "",
        result: str = "",
        module: str = "",
        data_status: str = "",
        start_time: str = "",
        end_time: str = "",
        sort: str = "-id",
        columns: tuple[str, ...] | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: dict[str, Any] = {"limit": max(1, min(int(limit or 50), 300))}
        normalized = normalize_outcome_symbol(symbol)
        if normalized:
            clauses.append("symbol = :symbol")
            params["symbol"] = normalized
        if horizon:
            clauses.append("horizon = :horizon")
            params["horizon"] = str(horizon)
        if decision:
            clauses.append("decision_code = :decision")
            params["decision"] = str(decision)
        if result:
            clauses.append("result_label = :result")
            params["result"] = str(result)
        if module:
            clauses.append("module = :module")
            params["module"] = str(module).strip().lower()
        if data_status:
            clauses.append("data_status = :data_status")
            params["data_status"] = str(data_status)
        if start_time:
            clauses.append("signal_time >= :start_time")
            params["start_time"] = start_time
        if end_time:
            clauses.append("signal_time <= :end_time")
            params["end_time"] = end_time
        direction = "ASC" if str(sort or "").lower() in {"id", "signal_time", "due_time"} and not str(sort).startswith("-") else "DESC"
        sort_field = str(sort or "-id").lstrip("-")
        if sort_field not in {"id", "signal_time", "due_time", "horizon", "symbol", "result_label", "final_return_pct"}:
            sort_field = "id"
        if cursor and sort_field == "id":
            clauses.append("id > :cursor" if direction == "ASC" else "id < :cursor")
            params["cursor"] = int(cursor)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        selected_columns = tuple(column for column in (columns or OUTCOME_COLUMNS) if column in OUTCOME_COLUMNS)
        if "id" not in selected_columns:
            selected_columns = ("id", *selected_columns)
        projection = ", ".join(selected_columns)
        context = self.connect() if connection is None else nullcontext(connection)
        with context as conn:
            rows = conn.execute(
                f"SELECT {projection} FROM signal_outcomes {where} ORDER BY {sort_field} {direction}, id {direction} LIMIT :limit",
                params,
            ).fetchall()
        items = [dict(row) for row in rows]
        return {"items": items, "count": len(items), "next_cursor": items[-1]["id"] if items else None}

    def stats(
        self,
        *,
        horizon: str = "",
        symbol: str = "",
        decision: str = "",
        module: str = "",
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: dict[str, Any] = {}
        normalized = normalize_outcome_symbol(symbol)
        if normalized:
            clauses.append("symbol = :symbol")
            params["symbol"] = normalized
        if horizon:
            clauses.append("horizon = :horizon")
            params["horizon"] = str(horizon)
        if decision:
            clauses.append("decision_code = :decision")
            params["decision"] = str(decision)
        if module:
            clauses.append("module = :module")
            params["module"] = str(module).strip().lower()
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        context = self.connect() if connection is None else nullcontext(connection)
        with context as conn:
            rows = conn.execute(
                f"""
                WITH filtered AS (
                    SELECT data_status, result_label, decision_code, module,
                           final_return_pct, max_gain_pct, max_drawdown_pct
                    FROM signal_outcomes {where}
                )
                SELECT 'aggregate' AS category, '' AS group_key, COUNT(*) AS item_count,
                       AVG(final_return_pct) AS avg_final_return_pct,
                       AVG(max_gain_pct) AS avg_max_gain_pct,
                       AVG(max_drawdown_pct) AS avg_max_drawdown_pct,
                       SUM(CASE WHEN final_return_pct > 0 THEN 1 ELSE 0 END) AS positive,
                       SUM(CASE WHEN result_label = '表现较强' THEN 1 ELSE 0 END) AS strong,
                       SUM(CASE WHEN result_label = '明显回撤' THEN 1 ELSE 0 END) AS drawdown,
                       SUM(CASE WHEN data_status = 'success' THEN 1 ELSE 0 END) AS success
                FROM filtered
                UNION ALL
                SELECT 'status', COALESCE(data_status, ''), COUNT(*),
                       NULL, NULL, NULL, NULL, NULL, NULL, NULL
                FROM filtered GROUP BY data_status
                UNION ALL
                SELECT 'result', COALESCE(NULLIF(result_label, ''), '数据不足'), COUNT(*),
                       NULL, NULL, NULL, NULL, NULL, NULL, NULL
                FROM filtered GROUP BY COALESCE(NULLIF(result_label, ''), '数据不足')
                UNION ALL
                SELECT 'decision', COALESCE(NULLIF(decision_code, ''), 'unknown'), COUNT(*),
                       NULL, NULL, NULL, NULL, NULL, NULL, NULL
                FROM filtered GROUP BY COALESCE(NULLIF(decision_code, ''), 'unknown')
                UNION ALL
                SELECT 'module', COALESCE(NULLIF(module, ''), 'unknown'), COUNT(*),
                       NULL, NULL, NULL, NULL, NULL, NULL, NULL
                FROM filtered GROUP BY COALESCE(NULLIF(module, ''), 'unknown')
                ORDER BY category, item_count DESC
                """,
                params,
            ).fetchall()
        aggregates = next((row for row in rows if row["category"] == "aggregate"), None)
        total = int(aggregates["item_count"] or 0) if aggregates else 0
        status_counts = {str(row["group_key"]): int(row["item_count"]) for row in rows if row["category"] == "status"}
        result_counts = {str(row["group_key"]): int(row["item_count"]) for row in rows if row["category"] == "result"}
        decision_counts = {str(row["group_key"]): int(row["item_count"]) for row in rows if row["category"] == "decision"}
        module_counts = {str(row["group_key"]): int(row["item_count"]) for row in rows if row["category"] == "module"}
        success_count = int(aggregates["success"] or 0) if aggregates else 0
        denominator = max(1, success_count)
        return {
            "total": total,
            "success_count": status_counts.get("success", 0),
            "pending_count": status_counts.get("pending", 0),
            "ready_count": status_counts.get("ready", 0),
            "unavailable_count": status_counts.get("unavailable", 0),
            "error_count": status_counts.get("error", 0),
            "avg_final_return_pct": round(float((aggregates or {})["avg_final_return_pct"] or 0.0), 4) if aggregates else 0.0,
            "avg_max_gain_pct": round(float((aggregates or {})["avg_max_gain_pct"] or 0.0), 4) if aggregates else 0.0,
            "avg_max_drawdown_pct": round(float((aggregates or {})["avg_max_drawdown_pct"] or 0.0), 4) if aggregates else 0.0,
            "positive_ratio": round(float((aggregates or {})["positive"] or 0) / denominator, 4) if aggregates else 0.0,
            "strong_ratio": round(float((aggregates or {})["strong"] or 0) / denominator, 4) if aggregates else 0.0,
            "drawdown_ratio": round(float((aggregates or {})["drawdown"] or 0) / denominator, 4) if aggregates else 0.0,
            "by_status": status_counts,
            "by_result": result_counts,
            "by_decision": decision_counts,
            "by_module": module_counts,
            "horizon": horizon,
            "symbol": normalized,
        }


def _candidate_signals(
    *,
    settings: Settings,
    limit: int,
    symbol: str = "",
    backfill_days: int = 7,
) -> list[dict[str, Any]]:
    end_ts = _now_ts()
    start_ts = end_ts - max(1, int(backfill_days or 7)) * 86400
    normalized = normalize_outcome_symbol(symbol)
    store = SignalEventStore(settings.signal_events_db_path)
    result = store.list_signals(
        limit=max(1, min(int(limit or 100), 500)),
        symbol=normalized,
        status="sent",
        start_ts=start_ts,
        end_ts=end_ts,
        sort_field="id",
        sort_direction="desc",
    )
    candidates: list[dict[str, Any]] = []
    for item in result.get("items", []):
        symbol_value = normalize_outcome_symbol(item.get("symbol"))
        if not symbol_value:
            continue
        if str(item.get("status") or "").lower() != "sent":
            continue
        if not _safe_int(item.get("ts"), 0):
            continue
        candidates.append(item)
    return candidates


def _decision_snapshot(symbol: str, settings: Settings) -> dict[str, Any]:
    try:
        from .web_services.decision import decision_for_symbol_payload

        payload = decision_for_symbol_payload(symbol, settings=settings, window_sec=86400, limit=50)
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        decision = (data or {}).get("decision") or {}
        return {
            "decision_code": str(decision.get("code") or ""),
            "decision_label": str(decision.get("label") or ""),
            "decision_confidence": _safe_int(decision.get("confidence"), 0),
            "risk_level": str(decision.get("risk_level") or ""),
        }
    except Exception:
        return {"decision_code": "", "decision_label": "", "decision_confidence": 0, "risk_level": ""}


@dataclass
class _OutcomePriceRequest:
    row: dict[str, Any]
    symbol: str
    interval: str
    start_ts: int
    end_ts: int


@dataclass
class _OutcomePriceRequestGroup:
    symbol: str
    interval: str
    start_ts: int
    end_ts: int
    requests: list[_OutcomePriceRequest]


def _price_request_for_outcome(row: dict[str, Any]) -> _OutcomePriceRequest:
    signal_time_text = str(row.get("signal_time") or "")
    horizon_sec = int(row.get("horizon_sec") or 0)
    try:
        start_ts = int(datetime.fromisoformat(signal_time_text.replace("Z", "+00:00")).timestamp())
    except Exception:
        # Signals also store ts in the source table, but outcome rows persist the ISO text only.
        start_ts = int(datetime.fromisoformat(str(row.get("due_time")).replace("Z", "+00:00")).timestamp()) - horizon_sec
    return _OutcomePriceRequest(
        row=row,
        symbol=str(row.get("symbol") or "").upper(),
        interval=interval_for_horizon(horizon_sec),
        start_ts=start_ts,
        end_ts=start_ts + horizon_sec + 60,
    )


def _interval_seconds(interval: str) -> int:
    value = str(interval or "").strip().lower()
    if value.endswith("m"):
        return max(1, _safe_int(value[:-1], 1)) * 60
    if value.endswith("h"):
        return max(1, _safe_int(value[:-1], 1)) * 3600
    return 60


def _group_price_requests(rows: list[dict[str, Any]]) -> list[_OutcomePriceRequestGroup]:
    """Merge overlapping horizon windows without exceeding Binance's 1,000-bar cap."""
    buckets: dict[tuple[str, str], list[_OutcomePriceRequest]] = {}
    for row in rows:
        request = _price_request_for_outcome(row)
        buckets.setdefault((request.symbol, request.interval), []).append(request)

    groups: list[_OutcomePriceRequestGroup] = []
    for (symbol, interval), requests in buckets.items():
        max_span_sec = _interval_seconds(interval) * 999
        current: _OutcomePriceRequestGroup | None = None
        for request in sorted(requests, key=lambda item: (item.start_ts, item.end_ts, int(item.row.get("id") or 0))):
            merged_end = max(current.end_ts, request.end_ts) if current is not None else request.end_ts
            if (
                current is not None
                and request.start_ts <= current.end_ts
                and merged_end - current.start_ts <= max_span_sec
            ):
                current.end_ts = merged_end
                current.requests.append(request)
                continue
            current = _OutcomePriceRequestGroup(
                symbol=symbol,
                interval=interval,
                start_ts=request.start_ts,
                end_ts=request.end_ts,
                requests=[request],
            )
            groups.append(current)
    return groups


def _klines_for_request(
    klines: list[dict[str, float]],
    request: _OutcomePriceRequest,
) -> list[dict[str, float]]:
    timestamps: list[float | None] = []
    for item in klines:
        timestamp = _safe_float(item.get("open_time"))
        if timestamp is not None and timestamp > 10_000_000_000:
            timestamp /= 1000.0
        timestamps.append(timestamp)
    if not any(timestamp is not None for timestamp in timestamps):
        # Custom/test fetchers predating open_time returned only OHLC values. Preserve
        # their behavior; the built-in Binance fetcher always supplies timestamps.
        return list(klines)
    return [
        item
        for item, timestamp in zip(klines, timestamps)
        if timestamp is not None and request.start_ts <= timestamp <= request.end_ts
    ]


def scan_outcomes(
    *,
    settings: Settings | None = None,
    limit: int | None = None,
    horizon: str = "",
    symbol: str = "",
    dry_run: bool = False,
    backfill_days: int | None = None,
    price_fetcher: KlineFetcher | None = None,
    now_ts: int | None = None,
    candidate_signals: list[dict[str, Any]] | None = None,
    signal_ids: Iterable[int] | None = None,
    force_rebuild: bool = False,
) -> dict[str, Any]:
    loaded = settings or Settings.load()
    explicit_candidates = candidate_signals is not None
    safe_limit = max(
        1,
        min(int(limit or loaded.outcome_scan_limit or 100), 1000 if explicit_candidates else 500),
    )
    windows = safe_outcome_windows(loaded.outcome_windows)
    if horizon:
        key = str(horizon).lower()
        windows = {key: windows[key]} if key in windows else {}
    store = OutcomeStore(loaded.outcome_db_path)
    store.ensure_schema()
    candidates = list(candidate_signals or []) if explicit_candidates else _candidate_signals(
        settings=loaded,
        limit=safe_limit,
        symbol=symbol,
        backfill_days=int(backfill_days or loaded.outcome_backfill_days or 7),
    )
    requested_ids = sorted({
        _safe_int(value)
        for value in (signal_ids or (_safe_int(item.get("id")) for item in candidates))
        if _safe_int(value) > 0
    })
    pending_planned = store.create_pending(candidates, windows, dry_run=dry_run)
    rebuilt = 0
    if force_rebuild and requested_ids:
        rebuilt = store.reset_for_rebuild(
            requested_ids,
            horizons=windows.keys(),
            dry_run=dry_run,
        )
    due = [] if dry_run else store.due_outcomes(
        now_ts=now_ts,
        limit=safe_limit,
        horizon=horizon,
        symbol=symbol,
        signal_ids=requested_ids if explicit_candidates else None,
    )
    fetcher = price_fetcher or fetch_binance_klines
    # The historical repair sweep is intentionally global for the legacy
    # scanner, but an explicit lifecycle batch must never mutate unrelated
    # Outcome rows outside its requested signal IDs.
    repaired_unavailable = (
        0
        if explicit_candidates
        else store.repair_unavailable_errors(data_source=loaded.outcome_price_source, dry_run=dry_run)
    )
    counts = {
        "candidate_signals": len(candidates),
        "new_pending": pending_planned,
        "due": len(due),
        "success": 0,
        "unavailable": 0,
        "error": 0,
        "repaired_unavailable": repaired_unavailable,
        "force_rebuilt": rebuilt,
        "dry_run": bool(dry_run),
    }
    errors: list[str] = []
    unavailable_summaries: list[str] = []
    invalid_symbol_cache: dict[str, str] = {}
    decision_cache: dict[str, dict[str, Any]] = {}
    updates: list[tuple[int, dict[str, Any]]] = []

    def decision_for_symbol(row_symbol: str) -> dict[str, Any]:
        if row_symbol not in decision_cache:
            decision_cache[row_symbol] = _decision_snapshot(row_symbol, loaded)
        return decision_cache[row_symbol]

    def mark_unavailable(row_data: dict[str, Any], reason: str) -> None:
        row_symbol = str(row_data.get("symbol") or "").upper()
        row_horizon = str(row_data.get("horizon") or "")
        updates.append((int(row_data["id"]), {
            "data_status": "unavailable",
            "data_source": loaded.outcome_price_source,
            "result_label": "数据不足",
            "result_tone": "muted",
            "error": reason,
            **decision_for_symbol(row_symbol),
        }))
        counts["unavailable"] += 1
        if len(unavailable_summaries) < 10:
            unavailable_summaries.append(_unavailable_summary(row_symbol, row_horizon, loaded.outcome_price_source, reason))

    def mark_error(row_data: dict[str, Any], exc: BaseException) -> None:
        row_symbol = str(row_data.get("symbol") or "").upper()
        row_horizon = str(row_data.get("horizon") or "")
        message = str(redact_api_payload(f"{type(exc).__name__}: {exc}"))[:300]
        updates.append((int(row_data["id"]), {
            "data_status": "error",
            "data_source": loaded.outcome_price_source,
            "result_label": "数据不足",
            "result_tone": "muted",
            "error": message,
            **decision_for_symbol(row_symbol),
        }))
        counts["error"] += 1
        errors.append(f"{row_symbol} {row_horizon}: {message}")

    for group in _group_price_requests(due):
        row_symbol = group.symbol
        if row_symbol in invalid_symbol_cache:
            for request in group.requests:
                mark_unavailable(request.row, invalid_symbol_cache[row_symbol])
            continue
        if _is_1000_prefix_symbol(row_symbol) and str(loaded.outcome_price_source or "").lower() == "binance":
            reason = price_unavailable_reason(row_symbol)
            invalid_symbol_cache[row_symbol] = reason
            for request in group.requests:
                mark_unavailable(request.row, reason)
            continue
        try:
            klines = fetcher(
                row_symbol,
                group.start_ts,
                group.end_ts,
                group.interval,
                int(loaded.outcome_http_timeout_sec or 10),
            )
            if loaded.outcome_request_sleep_sec:
                time.sleep(max(0.0, float(loaded.outcome_request_sleep_sec)))
            if not klines:
                reason = price_unavailable_reason(row_symbol)
                invalid_symbol_cache[row_symbol] = reason
                for request in group.requests:
                    mark_unavailable(request.row, reason)
                continue

            for request in group.requests:
                row = request.row
                row_horizon = str(row.get("horizon") or "")
                metrics = calculate_outcome_metrics(_klines_for_request(klines, request))
                status = "success" if metrics.get("final_return_pct") is not None else "unavailable"
                updates.append((int(row["id"]), {
                    **metrics,
                    **decision_for_symbol(row_symbol),
                    "data_status": status,
                    "data_source": loaded.outcome_price_source,
                    "error": "" if status == "success" else PRICE_UNAVAILABLE_REASON,
                }))
                counts["success" if status == "success" else "unavailable"] += 1
                if status == "unavailable" and len(unavailable_summaries) < 10:
                    unavailable_summaries.append(_unavailable_summary(row_symbol, row_horizon, loaded.outcome_price_source, PRICE_UNAVAILABLE_REASON))
        except Exception as exc:
            if is_price_unavailable_error(exc):
                reason = price_unavailable_reason(row_symbol)
                invalid_symbol_cache[row_symbol] = reason
                for request in group.requests:
                    mark_unavailable(request.row, reason)
                continue
            for request in group.requests:
                mark_error(request.row, exc)

    store.update_outcomes(updates)
    return {
        "ok": True,
        "counts": counts,
        "settings": {
            "db_path": str(loaded.outcome_db_path),
            "windows": list(windows.keys()),
            "limit": safe_limit,
            "symbol": normalize_outcome_symbol(symbol),
            "horizon": str(horizon or ""),
            "backfill_days": int(backfill_days or loaded.outcome_backfill_days or 7),
        },
        "unavailable": unavailable_summaries[:10],
        "errors": errors[:5],
        "message": "信号结果追踪扫描完成" if not dry_run else "信号结果追踪 dry-run 完成",
    }


def scan_signal_outcomes(
    signals: list[dict[str, Any]],
    *,
    settings: Settings | None = None,
    limit: int = 1000,
    horizon: str = "",
    dry_run: bool = False,
    price_fetcher: KlineFetcher | None = None,
    now_ts: int | None = None,
    force_rebuild: bool = False,
) -> dict[str, Any]:
    """Create and calculate Outcome rows for an explicit, bounded signal batch.

    Lifecycle backfill uses this entry point so historical signal IDs are never
    replaced by an unrelated "latest signal for the same symbol" lookup.
    """

    bounded = [dict(item) for item in list(signals or [])[: max(1, min(int(limit or 1000), 1000))]]
    signal_ids = [_safe_int(item.get("id")) for item in bounded if _safe_int(item.get("id")) > 0]
    return scan_outcomes(
        settings=settings,
        limit=max(1, min(int(limit or 1000), 1000)),
        horizon=horizon,
        dry_run=dry_run,
        price_fetcher=price_fetcher,
        now_ts=now_ts,
        candidate_signals=bounded,
        signal_ids=signal_ids,
        force_rebuild=force_rebuild,
    )


def scan_report_text(result: dict[str, Any]) -> str:
    counts = result.get("counts") or {}
    lines = [
        "信号结果追踪扫描",
        f"新增待追踪: {counts.get('new_pending', 0)}",
        f"到期待计算: {counts.get('due', 0)}",
        f"成功计算: {counts.get('success', 0)}",
        f"数据不足: {counts.get('unavailable', 0)}",
        f"历史误分类修复: {counts.get('repaired_unavailable', 0)}",
        f"失败: {counts.get('error', 0)}",
    ]
    if counts.get("dry_run"):
        lines.append("模式: dry-run，未写入新结果。")
    unavailable = result.get("unavailable") or []
    if unavailable:
        lines.append("数据不足 / 价格源不可用摘要:")
        lines.extend(f"- {item}" for item in unavailable[:10])
    errors = result.get("errors") or []
    if errors:
        lines.append("错误摘要:")
        lines.extend(f"- {error}" for error in errors[:5])
    return "\n".join(lines)
