from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any, Iterator, Mapping

from .launch_price_action import advance_price_action_state


ACTIVE_STATUS = "active"
FAILED_STATUS = "failed"
OUTCOME_EVALUATION_VERSION = 1
STAGE_RANK = {
    "idle": 0,
    "watching": 1,
    "primed": 2,
    "breakout": 3,
    "launched": 4,
}


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _percent_change(current: float, base: float) -> float | None:
    if base <= 0:
        return None
    return (current / base - 1.0) * 100.0


def _funding_8h(funding_pct: float, interval_hours: int) -> float | None:
    if interval_hours <= 0:
        return None
    return funding_pct * 8.0 / interval_hours


def _round_optional(value: float | None, digits: int = 8) -> float | None:
    return round(value, digits) if value is not None else None


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


@dataclass(frozen=True)
class LaunchLifecycleStore:
    """Durable, window-idempotent lifecycle state for launch signals."""

    db_path: Path
    watch_score: int = 45
    start_score: int = 60
    invalid_windows_required: int = 2
    window_sec: int = 15 * 60
    package_enabled: bool = False
    package_score_delta: int = 15
    package_price_delta_pct: float = 3.0
    package_oi_delta_pct: float = 5.0
    outcome_enabled: bool = False
    outcome_follow_through_pct: float = 3.0
    outcome_min_samples: int = 20
    breakout_score: int = 75
    launched_score: int = 90
    price_action_enabled: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "db_path", Path(self.db_path))
        object.__setattr__(self, "invalid_windows_required", max(1, int(self.invalid_windows_required)))
        object.__setattr__(self, "window_sec", max(60, int(self.window_sec)))
        object.__setattr__(self, "package_score_delta", max(1, int(self.package_score_delta)))
        object.__setattr__(self, "package_price_delta_pct", max(0.0, float(self.package_price_delta_pct)))
        object.__setattr__(self, "package_oi_delta_pct", max(0.0, float(self.package_oi_delta_pct)))
        object.__setattr__(
            self,
            "outcome_follow_through_pct",
            max(0.0, float(self.outcome_follow_through_pct)),
        )
        object.__setattr__(self, "outcome_min_samples", max(1, int(self.outcome_min_samples)))

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

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS launch_lifecycle_cycles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                cycle_no INTEGER NOT NULL,
                status TEXT NOT NULL,
                current_stage TEXT NOT NULL,
                peak_stage TEXT NOT NULL,
                started_at INTEGER NOT NULL,
                first_window_end INTEGER NOT NULL,
                last_window_end INTEGER NOT NULL,
                ended_at INTEGER,
                end_reason TEXT NOT NULL DEFAULT '',
                invalid_window_count INTEGER NOT NULL DEFAULT 0,
                breakout_below_count INTEGER NOT NULL DEFAULT 0,
                breakout_price REAL,
                last_published_observation_id INTEGER,
                latest_message_ids_json TEXT NOT NULL DEFAULT '[]',
                cleanup_pending_message_ids_json TEXT NOT NULL DEFAULT '[]',
                package_version INTEGER NOT NULL DEFAULT 0,
                package_updated_at INTEGER,
                outcome_rule_key TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                UNIQUE(symbol, cycle_no)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS launch_lifecycle_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cycle_id INTEGER NOT NULL,
                observation_no INTEGER NOT NULL,
                symbol TEXT NOT NULL,
                window_end_ts INTEGER NOT NULL,
                observed_at INTEGER NOT NULL,
                observed_stage TEXT NOT NULL,
                lifecycle_stage TEXT NOT NULL,
                lifecycle_status TEXT NOT NULL,
                score INTEGER NOT NULL,
                closed_price REAL NOT NULL,
                closed_oi_usd REAL NOT NULL,
                closed_quote_volume REAL NOT NULL,
                price_15m REAL NOT NULL,
                price_1h REAL NOT NULL,
                oi_15m REAL NOT NULL,
                oi_1h REAL NOT NULL,
                volume_ratio REAL NOT NULL,
                funding_available INTEGER NOT NULL DEFAULT 0,
                funding_pct REAL NOT NULL,
                funding_interval_hours INTEGER NOT NULL,
                funding_8h_pct REAL,
                breakout INTEGER NOT NULL DEFAULT 0,
                breakout_price REAL,
                data_quality_status TEXT NOT NULL,
                data_quality_score REAL NOT NULL,
                quality_gate TEXT NOT NULL,
                primary_data_source TEXT NOT NULL,
                data_confirmation_json TEXT NOT NULL DEFAULT '{}',
                reasons_json TEXT NOT NULL DEFAULT '[]',
                spot_active_net_usd REAL,
                futures_active_net_usd REAL,
                funds_direction TEXT NOT NULL DEFAULT 'unknown',
                price_action_json TEXT NOT NULL DEFAULT '{}',
                price_vs_first_pct REAL,
                oi_vs_first_pct REAL,
                funding_vs_first_pct_point REAL,
                funding_8h_vs_first_pct_point REAL,
                funding_interval_vs_first_hours INTEGER NOT NULL DEFAULT 0,
                score_vs_first INTEGER NOT NULL DEFAULT 0,
                price_vs_previous_pct REAL,
                oi_vs_previous_pct REAL,
                funding_vs_previous_pct_point REAL,
                funding_8h_vs_previous_pct_point REAL,
                funding_interval_vs_previous_hours INTEGER NOT NULL DEFAULT 0,
                score_vs_previous INTEGER NOT NULL DEFAULT 0,
                checkpoint_no INTEGER,
                checkpoint_reasons_json TEXT NOT NULL DEFAULT '[]',
                published_at INTEGER,
                UNIQUE(cycle_id, window_end_ts),
                FOREIGN KEY(cycle_id) REFERENCES launch_lifecycle_cycles(id) ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS launch_lifecycle_outcomes (
                cycle_id INTEGER PRIMARY KEY,
                evaluation_version INTEGER NOT NULL,
                rule_key TEXT NOT NULL,
                symbol TEXT NOT NULL,
                cycle_no INTEGER NOT NULL,
                status TEXT NOT NULL,
                started_at INTEGER NOT NULL,
                ended_at INTEGER NOT NULL,
                duration_sec INTEGER NOT NULL,
                observation_count INTEGER NOT NULL,
                first_stage TEXT NOT NULL,
                peak_stage TEXT NOT NULL,
                failure_reason TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                max_close_price REAL NOT NULL,
                min_close_price REAL NOT NULL,
                max_favorable_return_pct REAL NOT NULL,
                max_adverse_return_pct REAL NOT NULL,
                end_return_pct REAL NOT NULL,
                max_oi_increase_pct REAL NOT NULL,
                max_oi_decrease_pct REAL NOT NULL,
                peak_score INTEGER NOT NULL,
                confirmed INTEGER NOT NULL,
                launched INTEGER NOT NULL,
                followed_through INTEGER NOT NULL,
                confirmed_at INTEGER,
                launched_at INTEGER,
                time_to_confirm_sec INTEGER,
                time_to_launch_sec INTEGER,
                evaluated_at INTEGER NOT NULL,
                FOREIGN KEY(cycle_id) REFERENCES launch_lifecycle_cycles(id) ON DELETE CASCADE
            )
            """
        )
        cycle_columns = {
            "last_published_observation_id": "INTEGER",
            "latest_message_ids_json": "TEXT NOT NULL DEFAULT '[]'",
            "cleanup_pending_message_ids_json": "TEXT NOT NULL DEFAULT '[]'",
            "package_version": "INTEGER NOT NULL DEFAULT 0",
            "package_updated_at": "INTEGER",
            "outcome_rule_key": "TEXT NOT NULL DEFAULT ''",
        }
        observation_columns = {
            "spot_active_net_usd": "REAL",
            "futures_active_net_usd": "REAL",
            "funds_direction": "TEXT NOT NULL DEFAULT 'unknown'",
            "price_action_json": "TEXT NOT NULL DEFAULT '{}'",
            "checkpoint_no": "INTEGER",
            "checkpoint_reasons_json": "TEXT NOT NULL DEFAULT '[]'",
            "published_at": "INTEGER",
        }
        LaunchLifecycleStore._ensure_columns(
            conn,
            "launch_lifecycle_cycles",
            cycle_columns,
        )
        LaunchLifecycleStore._ensure_columns(
            conn,
            "launch_lifecycle_observations",
            observation_columns,
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_launch_lifecycle_active_symbol
            ON launch_lifecycle_cycles(symbol)
            WHERE status = 'active'
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_launch_lifecycle_cycles_status_window
            ON launch_lifecycle_cycles(status, last_window_end)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_launch_lifecycle_observations_cycle_window
            ON launch_lifecycle_observations(cycle_id, window_end_ts)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_launch_lifecycle_observations_checkpoint
            ON launch_lifecycle_observations(cycle_id, checkpoint_no)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_launch_lifecycle_outcomes_rule
            ON launch_lifecycle_outcomes(rule_key, evaluated_at)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_launch_lifecycle_outcomes_symbol
            ON launch_lifecycle_outcomes(symbol, evaluated_at)
            """
        )

    @staticmethod
    def _ensure_columns(
        conn: sqlite3.Connection,
        table: str,
        columns: Mapping[str, str],
    ) -> None:
        existing = {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, definition in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def list_active_symbols(self, *, limit: int | None = None) -> list[str]:
        sql = (
            "SELECT symbol FROM launch_lifecycle_cycles "
            "WHERE status = ? ORDER BY last_window_end ASC, id ASC"
        )
        params: list[Any] = [ACTIVE_STATUS]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(0, int(limit)))
        with self.connect() as conn:
            return [
                str(row["symbol"])
                for row in conn.execute(sql, params).fetchall()
            ]

    def get_latest_cycle(self, symbol: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM launch_lifecycle_cycles
                WHERE symbol = ?
                ORDER BY cycle_no DESC
                LIMIT 1
                """,
                (str(symbol).upper(),),
            ).fetchone()
            return dict(row) if row is not None else None

    def list_observations(self, cycle_id: int) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM launch_lifecycle_observations
                WHERE cycle_id = ?
                ORDER BY observation_no ASC
                """,
                (int(cycle_id),),
            ).fetchall()
            items: list[dict[str, Any]] = []
            for row in rows:
                item = dict(row)
                item["data_confirmation"] = json.loads(
                    str(item.pop("data_confirmation_json") or "{}")
                )
                item["reasons"] = json.loads(str(item.pop("reasons_json") or "[]"))
                item["checkpoint_reasons"] = json.loads(
                    str(item.pop("checkpoint_reasons_json") or "[]")
                )
                items.append(item)
            return items

    @property
    def outcome_rule_key(self) -> str:
        follow_through = f"{self.outcome_follow_through_pct:.6f}".rstrip("0").rstrip(".")
        return (
            f"launch-v{OUTCOME_EVALUATION_VERSION}"
            f":start={self.start_score}"
            f":watch={self.watch_score}"
            f":breakout={self.breakout_score}"
            f":launched={self.launched_score}"
            f":invalid={self.invalid_windows_required}"
            f":follow={follow_through}"
        )

    @staticmethod
    def _rule_number(rule_key: str, name: str, fallback: float) -> float:
        for part in str(rule_key).split(":"):
            if not part.startswith(f"{name}="):
                continue
            try:
                return max(0.0, float(part.split("=", 1)[1]))
            except (TypeError, ValueError):
                break
        return max(0.0, float(fallback))

    def _stage_for_rule(self, score: int, rule_key: str) -> str:
        watch = int(self._rule_number(rule_key, "watch", self.watch_score))
        start = int(self._rule_number(rule_key, "start", self.start_score))
        breakout = int(self._rule_number(rule_key, "breakout", self.breakout_score))
        launched = int(self._rule_number(rule_key, "launched", self.launched_score))
        if score < watch:
            return "idle"
        if score < start:
            return "watching"
        if score < breakout:
            return "primed"
        if score < launched:
            return "breakout"
        return "launched"

    def refresh_outcomes(self, *, evaluated_at: int) -> dict[str, Any]:
        """Backfill completed lifecycle outcomes without counting message replacements."""

        if not self.outcome_enabled:
            return {
                "enabled": False,
                "status": "disabled",
                "evaluated": 0,
            }
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE launch_lifecycle_cycles
                SET outcome_rule_key = ?
                WHERE outcome_rule_key = ''
                """,
                (self.outcome_rule_key,),
            )
            rows = conn.execute(
                """
                SELECT cycle.id
                FROM launch_lifecycle_cycles AS cycle
                LEFT JOIN launch_lifecycle_outcomes AS outcome
                  ON outcome.cycle_id = cycle.id
                WHERE cycle.status = ?
                  AND (
                    outcome.cycle_id IS NULL
                    OR outcome.evaluation_version != ?
                    OR outcome.rule_key != cycle.outcome_rule_key
                  )
                ORDER BY cycle.id
                """,
                (
                    FAILED_STATUS,
                    OUTCOME_EVALUATION_VERSION,
                ),
            ).fetchall()
            evaluated = 0
            for row in rows:
                if self._evaluate_cycle(
                    conn,
                    cycle_id=int(row["id"]),
                    evaluated_at=int(evaluated_at),
                ) is not None:
                    evaluated += 1
            counts = conn.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM launch_lifecycle_cycles WHERE status = ?) AS completed,
                    (SELECT COUNT(*) FROM launch_lifecycle_cycles WHERE status = ?) AS active,
                    (SELECT COUNT(*) FROM launch_lifecycle_outcomes WHERE rule_key = ?) AS same_rule
                """,
                (FAILED_STATUS, ACTIVE_STATUS, self.outcome_rule_key),
            ).fetchone()
            return {
                "enabled": True,
                "status": "active",
                "evaluated": evaluated,
                "completed_cycles": int(counts["completed"] or 0),
                "active_cycles": int(counts["active"] or 0),
                "same_rule_samples": int(counts["same_rule"] or 0),
                "minimum_samples": self.outcome_min_samples,
                "rates_available": int(counts["same_rule"] or 0) >= self.outcome_min_samples,
                "rule_key": self.outcome_rule_key,
            }

    def _cycle_metrics(
        self,
        conn: sqlite3.Connection,
        *,
        cycle_id: int,
    ) -> dict[str, Any] | None:
        cycle = conn.execute(
            "SELECT * FROM launch_lifecycle_cycles WHERE id = ?",
            (int(cycle_id),),
        ).fetchone()
        observations = conn.execute(
            """
            SELECT * FROM launch_lifecycle_observations
            WHERE cycle_id = ?
            ORDER BY observation_no
            """,
            (int(cycle_id),),
        ).fetchall()
        if cycle is None or not observations:
            return None

        first = observations[0]
        last = observations[-1]
        entry_price = _number(first["closed_price"])
        entry_oi = _number(first["closed_oi_usd"])
        if entry_price <= 0 or entry_oi <= 0:
            return None
        prices = [_number(row["closed_price"]) for row in observations]
        oi_values = [_number(row["closed_oi_usd"]) for row in observations]
        max_price = max(prices)
        min_price = min(prices)
        max_oi = max(oi_values)
        min_oi = min(oi_values)
        first_window_end = int(first["window_end_ts"])
        rule_key = str(cycle["outcome_rule_key"] or self.outcome_rule_key)
        follow_through_pct = self._rule_number(
            rule_key,
            "follow",
            self.outcome_follow_through_pct,
        )

        confirmed_at = next(
            (
                int(row["window_end_ts"])
                for row in observations
                if STAGE_RANK.get(str(row["observed_stage"]), 0)
                >= STAGE_RANK["breakout"]
            ),
            None,
        )
        launched_at = next(
            (
                int(row["window_end_ts"])
                for row in observations
                if STAGE_RANK.get(str(row["observed_stage"]), 0)
                >= STAGE_RANK["launched"]
            ),
            None,
        )
        max_favorable = _percent_change(max_price, entry_price) or 0.0
        max_adverse = _percent_change(min_price, entry_price) or 0.0
        end_return = _percent_change(_number(last["closed_price"]), entry_price) or 0.0
        max_oi_increase = _percent_change(max_oi, entry_oi) or 0.0
        max_oi_decrease = _percent_change(min_oi, entry_oi) or 0.0
        return {
            "cycle_id": int(cycle["id"]),
            "symbol": str(cycle["symbol"]),
            "cycle_no": int(cycle["cycle_no"]),
            "rule_key": rule_key,
            "cycle_status": str(cycle["status"]),
            "started_at": int(cycle["first_window_end"]),
            "ended_at": int(cycle["ended_at"] or cycle["last_window_end"]),
            "duration_sec": max(
                0,
                int(cycle["last_window_end"]) - int(cycle["first_window_end"]),
            ),
            "observation_count": len(observations),
            "first_stage": str(first["observed_stage"]),
            "peak_stage": str(cycle["peak_stage"]),
            "failure_reason": str(cycle["end_reason"] or ""),
            "entry_price": entry_price,
            "exit_price": _number(last["closed_price"]),
            "max_close_price": max_price,
            "min_close_price": min_price,
            "max_favorable_return_pct": round(max_favorable, 8),
            "max_adverse_return_pct": round(max_adverse, 8),
            "end_return_pct": round(end_return, 8),
            "max_oi_increase_pct": round(max_oi_increase, 8),
            "max_oi_decrease_pct": round(max_oi_decrease, 8),
            "peak_score": max(int(row["score"]) for row in observations),
            "confirmed": confirmed_at is not None,
            "launched": launched_at is not None,
            "followed_through": max_favorable >= follow_through_pct,
            "confirmed_at": confirmed_at,
            "launched_at": launched_at,
            "time_to_confirm_sec": (
                max(0, confirmed_at - first_window_end)
                if confirmed_at is not None
                else None
            ),
            "time_to_launch_sec": (
                max(0, launched_at - first_window_end)
                if launched_at is not None
                else None
            ),
        }

    def _evaluate_cycle(
        self,
        conn: sqlite3.Connection,
        *,
        cycle_id: int,
        evaluated_at: int,
    ) -> dict[str, Any] | None:
        metrics = self._cycle_metrics(conn, cycle_id=int(cycle_id))
        if metrics is None or metrics["cycle_status"] != FAILED_STATUS:
            return None
        conn.execute(
            """
            INSERT INTO launch_lifecycle_outcomes (
                cycle_id, evaluation_version, rule_key, symbol, cycle_no, status,
                started_at, ended_at, duration_sec, observation_count,
                first_stage, peak_stage, failure_reason,
                entry_price, exit_price, max_close_price, min_close_price,
                max_favorable_return_pct, max_adverse_return_pct, end_return_pct,
                max_oi_increase_pct, max_oi_decrease_pct, peak_score,
                confirmed, launched, followed_through,
                confirmed_at, launched_at, time_to_confirm_sec, time_to_launch_sec,
                evaluated_at
            ) VALUES (
                ?, ?, ?, ?, ?, 'evaluated',
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?,
                ?, ?, ?, ?,
                ?
            )
            ON CONFLICT(cycle_id) DO UPDATE SET
                evaluation_version = excluded.evaluation_version,
                rule_key = excluded.rule_key,
                symbol = excluded.symbol,
                cycle_no = excluded.cycle_no,
                status = excluded.status,
                started_at = excluded.started_at,
                ended_at = excluded.ended_at,
                duration_sec = excluded.duration_sec,
                observation_count = excluded.observation_count,
                first_stage = excluded.first_stage,
                peak_stage = excluded.peak_stage,
                failure_reason = excluded.failure_reason,
                entry_price = excluded.entry_price,
                exit_price = excluded.exit_price,
                max_close_price = excluded.max_close_price,
                min_close_price = excluded.min_close_price,
                max_favorable_return_pct = excluded.max_favorable_return_pct,
                max_adverse_return_pct = excluded.max_adverse_return_pct,
                end_return_pct = excluded.end_return_pct,
                max_oi_increase_pct = excluded.max_oi_increase_pct,
                max_oi_decrease_pct = excluded.max_oi_decrease_pct,
                peak_score = excluded.peak_score,
                confirmed = excluded.confirmed,
                launched = excluded.launched,
                followed_through = excluded.followed_through,
                confirmed_at = excluded.confirmed_at,
                launched_at = excluded.launched_at,
                time_to_confirm_sec = excluded.time_to_confirm_sec,
                time_to_launch_sec = excluded.time_to_launch_sec,
                evaluated_at = excluded.evaluated_at
            """,
            (
                metrics["cycle_id"],
                OUTCOME_EVALUATION_VERSION,
                metrics["rule_key"],
                metrics["symbol"],
                metrics["cycle_no"],
                metrics["started_at"],
                metrics["ended_at"],
                metrics["duration_sec"],
                metrics["observation_count"],
                metrics["first_stage"],
                metrics["peak_stage"],
                metrics["failure_reason"],
                metrics["entry_price"],
                metrics["exit_price"],
                metrics["max_close_price"],
                metrics["min_close_price"],
                metrics["max_favorable_return_pct"],
                metrics["max_adverse_return_pct"],
                metrics["end_return_pct"],
                metrics["max_oi_increase_pct"],
                metrics["max_oi_decrease_pct"],
                metrics["peak_score"],
                int(metrics["confirmed"]),
                int(metrics["launched"]),
                int(metrics["followed_through"]),
                metrics["confirmed_at"],
                metrics["launched_at"],
                metrics["time_to_confirm_sec"],
                metrics["time_to_launch_sec"],
                int(evaluated_at),
            ),
        )
        row = conn.execute(
            "SELECT * FROM launch_lifecycle_outcomes WHERE cycle_id = ?",
            (int(cycle_id),),
        ).fetchone()
        return self._outcome_summary(row)

    @staticmethod
    def _outcome_summary(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        followed = bool(row["followed_through"])
        confirmed = bool(row["confirmed"])
        launched = bool(row["launched"])
        if followed and launched:
            label = "launched_follow_through"
        elif followed and confirmed:
            label = "confirmed_follow_through"
        elif followed:
            label = "price_follow_through_only"
        elif launched or confirmed:
            label = "confirmed_no_follow_through"
        else:
            label = "false_start"
        return {
            "status": str(row["status"]),
            "label": label,
            "evaluation_version": int(row["evaluation_version"]),
            "rule_key": str(row["rule_key"]),
            "observation_count": int(row["observation_count"]),
            "duration_sec": int(row["duration_sec"]),
            "first_stage": str(row["first_stage"]),
            "peak_stage": str(row["peak_stage"]),
            "failure_reason": str(row["failure_reason"]),
            "entry_price": _number(row["entry_price"]),
            "exit_price": _number(row["exit_price"]),
            "max_favorable_return_pct": _number(row["max_favorable_return_pct"]),
            "max_adverse_return_pct": _number(row["max_adverse_return_pct"]),
            "end_return_pct": _number(row["end_return_pct"]),
            "max_oi_increase_pct": _number(row["max_oi_increase_pct"]),
            "max_oi_decrease_pct": _number(row["max_oi_decrease_pct"]),
            "peak_score": int(row["peak_score"]),
            "confirmed": confirmed,
            "launched": launched,
            "followed_through": followed,
            "confirmed_at": (
                int(row["confirmed_at"])
                if row["confirmed_at"] is not None
                else None
            ),
            "launched_at": (
                int(row["launched_at"])
                if row["launched_at"] is not None
                else None
            ),
            "time_to_confirm_sec": (
                int(row["time_to_confirm_sec"])
                if row["time_to_confirm_sec"] is not None
                else None
            ),
            "time_to_launch_sec": (
                int(row["time_to_launch_sec"])
                if row["time_to_launch_sec"] is not None
                else None
            ),
        }

    def _reliability_summary(
        self,
        conn: sqlite3.Connection,
        *,
        cycle_id: int,
        symbol: str,
        rule_key: str,
    ) -> dict[str, Any]:
        rows = conn.execute(
            """
            SELECT confirmed, launched, followed_through,
                   max_favorable_return_pct, max_adverse_return_pct
            FROM launch_lifecycle_outcomes
            WHERE rule_key = ? AND cycle_id != ?
            ORDER BY evaluated_at, cycle_id
            """,
            (str(rule_key), int(cycle_id)),
        ).fetchall()
        samples = len(rows)
        confirmed_count = sum(int(row["confirmed"]) for row in rows)
        launched_count = sum(int(row["launched"]) for row in rows)
        followed_count = sum(int(row["followed_through"]) for row in rows)
        rates_available = samples >= self.outcome_min_samples
        symbol_samples = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM launch_lifecycle_outcomes
                WHERE rule_key = ? AND cycle_id != ? AND symbol = ?
                """,
                (str(rule_key), int(cycle_id), str(symbol)),
            ).fetchone()[0]
        )
        result: dict[str, Any] = {
            "status": "review_ready" if rates_available else "accumulating",
            "completed_samples": samples,
            "minimum_samples": self.outcome_min_samples,
            "rates_available": rates_available,
            "confirmed_count": confirmed_count,
            "launched_count": launched_count,
            "followed_through_count": followed_count,
            "symbol_completed_samples": symbol_samples,
            "follow_through_threshold_pct": self._rule_number(
                rule_key,
                "follow",
                self.outcome_follow_through_pct,
            ),
            "rule_key": str(rule_key),
        }
        if rates_available:
            result.update({
                "confirmed_rate_pct": round(confirmed_count / samples * 100.0, 2),
                "launched_rate_pct": round(launched_count / samples * 100.0, 2),
                "followed_through_rate_pct": round(followed_count / samples * 100.0, 2),
                "median_max_favorable_return_pct": round(
                    median(float(row["max_favorable_return_pct"]) for row in rows),
                    4,
                ),
                "median_max_adverse_return_pct": round(
                    median(float(row["max_adverse_return_pct"]) for row in rows),
                    4,
                ),
            })
        return result

    def _outcome_context(
        self,
        conn: sqlite3.Connection,
        *,
        cycle: sqlite3.Row,
        evaluated_at: int,
    ) -> dict[str, Any]:
        if not self.outcome_enabled:
            return {"enabled": False, "status": "disabled"}
        metrics = self._cycle_metrics(conn, cycle_id=int(cycle["id"]))
        outcome = None
        if str(cycle["status"]) == FAILED_STATUS:
            outcome = self._evaluate_cycle(
                conn,
                cycle_id=int(cycle["id"]),
                evaluated_at=int(evaluated_at),
            )
        progress = None
        if metrics is not None:
            progress = {
                key: metrics[key]
                for key in (
                    "cycle_status",
                    "observation_count",
                    "duration_sec",
                    "max_favorable_return_pct",
                    "max_adverse_return_pct",
                    "end_return_pct",
                    "max_oi_increase_pct",
                    "max_oi_decrease_pct",
                    "peak_score",
                    "confirmed",
                    "launched",
                    "followed_through",
                    "confirmed_at",
                    "launched_at",
                    "time_to_confirm_sec",
                    "time_to_launch_sec",
                )
            }
        return {
            "enabled": True,
            "status": "evaluated" if outcome is not None else "tracking",
            "progress": progress,
            "outcome": outcome,
            "reliability": self._reliability_summary(
                conn,
                cycle_id=int(cycle["id"]),
                symbol=str(cycle["symbol"]),
                rule_key=str(cycle["outcome_rule_key"] or self.outcome_rule_key),
            ),
        }

    def list_pending_cleanups(
        self,
        *,
        limit: int = 20,
        now_ts: int | None = None,
        max_age_sec: int | None = None,
    ) -> list[dict[str, Any]]:
        cutoff = (
            max(0, int(now_ts or 0) - max(1, int(max_age_sec)))
            if max_age_sec is not None
            else 0
        )
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT id, symbol, cycle_no, status, package_updated_at,
                       latest_message_ids_json, cleanup_pending_message_ids_json
                FROM launch_lifecycle_cycles
                WHERE cleanup_pending_message_ids_json != '[]'
                   OR (
                       status = ?
                       AND latest_message_ids_json != '[]'
                       AND COALESCE(package_updated_at, 0) >= ?
                   )
                ORDER BY package_updated_at ASC, id ASC
                LIMIT ?
                """,
                (FAILED_STATUS, cutoff, max(0, int(limit))),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in rows:
                expire_latest = (
                    str(row["status"]) == FAILED_STATUS
                    and bool(self._message_ids(row["latest_message_ids_json"]))
                    and int(row["package_updated_at"] or 0) >= cutoff
                )
                pending = self._message_ids(
                    row["cleanup_pending_message_ids_json"]
                )
                latest = (
                    self._message_ids(row["latest_message_ids_json"])
                    if expire_latest
                    else []
                )
                result.append({
                    "cycle_id": int(row["id"]),
                    "symbol": str(row["symbol"]),
                    "cycle_no": int(row["cycle_no"]),
                    "message_ids": list(dict.fromkeys([*pending, *latest])),
                    "expire_latest": expire_latest,
                })
            return result

    def commit_package(
        self,
        *,
        cycle_id: int,
        observation_id: int,
        message_ids: list[int],
        checkpoint_reasons: list[str],
        published_at: int,
    ) -> dict[str, Any]:
        normalized = self._message_ids(message_ids)
        if not normalized:
            return {"status": "rejected", "reason": "missing_message_ids"}
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cycle = conn.execute(
                "SELECT * FROM launch_lifecycle_cycles WHERE id = ?",
                (int(cycle_id),),
            ).fetchone()
            observation = conn.execute(
                """
                SELECT * FROM launch_lifecycle_observations
                WHERE id = ? AND cycle_id = ?
                """,
                (int(observation_id), int(cycle_id)),
            ).fetchone()
            if cycle is None or observation is None:
                return {"status": "rejected", "reason": "lifecycle_record_not_found"}
            if observation["checkpoint_no"] is not None:
                return {
                    "status": "idempotent",
                    "cycle_id": int(cycle_id),
                    "checkpoint_no": int(observation["checkpoint_no"]),
                    "delete_message_ids": self._message_ids(
                        cycle["cleanup_pending_message_ids_json"]
                    ),
                }

            checkpoint_no = int(
                conn.execute(
                    """
                    SELECT COALESCE(MAX(checkpoint_no), 0) + 1
                    FROM launch_lifecycle_observations
                    WHERE cycle_id = ?
                    """,
                    (int(cycle_id),),
                ).fetchone()[0]
            )
            previous_ids = self._message_ids(cycle["latest_message_ids_json"])
            pending_ids = self._message_ids(
                cycle["cleanup_pending_message_ids_json"]
            )
            delete_ids = [
                message_id
                for message_id in dict.fromkeys([*pending_ids, *previous_ids])
                if message_id not in normalized
            ]
            reasons = [
                str(reason)
                for reason in checkpoint_reasons
                if str(reason).strip()
            ]
            conn.execute(
                """
                UPDATE launch_lifecycle_observations
                SET checkpoint_no = ?,
                    checkpoint_reasons_json = ?,
                    published_at = ?
                WHERE id = ?
                """,
                (
                    checkpoint_no,
                    json.dumps(reasons, ensure_ascii=False),
                    int(published_at),
                    int(observation_id),
                ),
            )
            conn.execute(
                """
                UPDATE launch_lifecycle_cycles
                SET last_published_observation_id = ?,
                    latest_message_ids_json = ?,
                    cleanup_pending_message_ids_json = ?,
                    package_version = package_version + 1,
                    package_updated_at = ?,
                    updated_at = MAX(updated_at, ?)
                WHERE id = ?
                """,
                (
                    int(observation_id),
                    json.dumps(normalized),
                    json.dumps(delete_ids),
                    int(published_at),
                    int(published_at),
                    int(cycle_id),
                ),
            )
            return {
                "status": "committed",
                "cycle_id": int(cycle_id),
                "checkpoint_no": checkpoint_no,
                "message_ids": normalized,
                "delete_message_ids": delete_ids,
            }

    def complete_package_cleanup(
        self,
        *,
        cycle_id: int,
        deleted_ids: list[int],
        failed_ids: list[int],
        updated_at: int,
        expire_latest: bool = False,
    ) -> dict[str, Any]:
        deleted = set(self._message_ids(deleted_ids))
        failed = self._message_ids(failed_ids)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cycle = conn.execute(
                "SELECT * FROM launch_lifecycle_cycles WHERE id = ?",
                (int(cycle_id),),
            ).fetchone()
            if cycle is None:
                return {"status": "not_found", "remaining_ids": failed}
            pending = self._message_ids(
                cycle["cleanup_pending_message_ids_json"]
            )
            latest = (
                self._message_ids(cycle["latest_message_ids_json"])
                if expire_latest and str(cycle["status"]) == FAILED_STATUS
                else []
            )
            remaining = [
                message_id
                for message_id in dict.fromkeys([*failed, *pending, *latest])
                if message_id not in deleted
            ]
            conn.execute(
                """
                UPDATE launch_lifecycle_cycles
                SET cleanup_pending_message_ids_json = ?,
                    latest_message_ids_json = CASE
                        WHEN ? THEN '[]'
                        ELSE latest_message_ids_json
                    END,
                    package_updated_at = ?,
                    updated_at = MAX(updated_at, ?)
                WHERE id = ?
                """,
                (
                    json.dumps(remaining),
                    int(bool(expire_latest)),
                    int(updated_at),
                    int(updated_at),
                    int(cycle_id),
                ),
            )
            return {
                "status": "complete" if not remaining else "pending",
                "remaining_ids": remaining,
            }

    def reconcile_topic_message_cleanup(
        self,
        *,
        deleted_ids: list[int],
        updated_at: int,
    ) -> dict[str, int]:
        deleted = set(self._message_ids(deleted_ids))
        if not deleted:
            return {"cycles_updated": 0, "message_ids_removed": 0}
        cycles_updated = 0
        message_ids_removed = 0
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT id, latest_message_ids_json,
                       cleanup_pending_message_ids_json
                FROM launch_lifecycle_cycles
                """
            ).fetchall()
            for row in rows:
                latest = self._message_ids(row["latest_message_ids_json"])
                pending = self._message_ids(
                    row["cleanup_pending_message_ids_json"]
                )
                filtered_latest = [
                    message_id
                    for message_id in latest
                    if message_id not in deleted
                ]
                filtered_pending = [
                    message_id
                    for message_id in pending
                    if message_id not in deleted
                ]
                removed = (len(latest) - len(filtered_latest)) + (
                    len(pending) - len(filtered_pending)
                )
                if removed <= 0:
                    continue
                conn.execute(
                    """
                    UPDATE launch_lifecycle_cycles
                    SET latest_message_ids_json = ?,
                        cleanup_pending_message_ids_json = ?,
                        package_updated_at = ?,
                        updated_at = MAX(updated_at, ?)
                    WHERE id = ?
                    """,
                    (
                        json.dumps(filtered_latest),
                        json.dumps(filtered_pending),
                        int(updated_at),
                        int(updated_at),
                        int(row["id"]),
                    ),
                )
                cycles_updated += 1
                message_ids_removed += removed
        return {
            "cycles_updated": cycles_updated,
            "message_ids_removed": message_ids_removed,
        }

    @staticmethod
    def _message_ids(value: Any) -> list[int]:
        raw = value
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                raw = []
        if not isinstance(raw, (list, tuple, set)):
            raw = []
        return list(dict.fromkeys(
            int(message_id)
            for message_id in raw
            if isinstance(message_id, int) or str(message_id).isdigit()
        ))

    def record_observation(
        self,
        snapshot: Mapping[str, Any],
        *,
        stage: str,
        observed_at: int,
    ) -> dict[str, Any]:
        return self.record_observations(
            [(snapshot, stage, int(observed_at))]
        )[0]

    def record_observations(
        self,
        observations: list[tuple[Mapping[str, Any], str, int]],
    ) -> list[dict[str, Any]]:
        if not observations:
            return []
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            return [
                self._record_observation(
                    conn,
                    snapshot=snapshot,
                    stage=stage,
                    observed_at=int(observed_at),
                )
                for snapshot, stage, observed_at in observations
            ]

    def _record_observation(
        self,
        conn: sqlite3.Connection,
        *,
        snapshot: Mapping[str, Any],
        stage: str,
        observed_at: int,
    ) -> dict[str, Any]:
        symbol = str(snapshot.get("symbol") or "").upper()
        window_end_ts = int(_number(snapshot.get("window_end_ts")))
        score = int(_number(snapshot.get("score")))
        closed_price = _number(snapshot.get("closed_price"))
        closed_oi_usd = _number(snapshot.get("closed_oi_usd"))
        quality_gate = str(snapshot.get("quality_gate") or "block")
        if not symbol or window_end_ts <= 0 or closed_price <= 0 or closed_oi_usd <= 0:
            return {"status": "frozen", "reason": "invalid_snapshot", "symbol": symbol}
        if quality_gate != "allow":
            return {"status": "frozen", "reason": "quality_gate_blocked", "symbol": symbol}

        cycle = conn.execute(
            """
            SELECT * FROM launch_lifecycle_cycles
            WHERE symbol = ? AND status = ?
            LIMIT 1
            """,
            (symbol, ACTIVE_STATUS),
        ).fetchone()
        if cycle is None:
            latest_cycle = conn.execute(
                """
                SELECT * FROM launch_lifecycle_cycles
                WHERE symbol = ?
                ORDER BY cycle_no DESC
                LIMIT 1
                """,
                (symbol,),
            ).fetchone()
            if latest_cycle is not None and window_end_ts <= int(latest_cycle["last_window_end"]):
                latest_observation = conn.execute(
                    """
                    SELECT * FROM launch_lifecycle_observations
                    WHERE cycle_id = ? AND window_end_ts = ?
                    LIMIT 1
                    """,
                    (int(latest_cycle["id"]), window_end_ts),
                ).fetchone()
                if latest_observation is not None:
                    return self._result(
                        conn,
                        cycle_id=int(latest_cycle["id"]),
                        observation=dict(latest_observation),
                        status="duplicate",
                    )
                return {
                    "status": "ignored",
                    "reason": "stale_window",
                    "symbol": symbol,
                }
            if score < self.start_score:
                return {
                    "status": "ignored",
                    "reason": "below_start_score",
                    "symbol": symbol,
                }
            cycle = self._open_cycle(
                conn,
                symbol=symbol,
                stage=self._stage_for_rule(
                    score,
                    self.outcome_rule_key,
                ),
                window_end_ts=window_end_ts,
                observed_at=int(observed_at),
                breakout_price=(
                    _number(snapshot.get("breakout_price"))
                    if bool(snapshot.get("breakout"))
                    else None
                ),
            )
            opened = True
        else:
            opened = False
        stage = self._stage_for_rule(
            score,
            str(cycle["outcome_rule_key"] or self.outcome_rule_key),
        )

        duplicate = conn.execute(
            """
            SELECT * FROM launch_lifecycle_observations
            WHERE cycle_id = ? AND window_end_ts = ?
            LIMIT 1
            """,
            (int(cycle["id"]), window_end_ts),
        ).fetchone()
        if duplicate is not None:
            return self._result(
                conn,
                cycle_id=int(cycle["id"]),
                observation=dict(duplicate),
                status="duplicate",
            )
        if window_end_ts < int(cycle["last_window_end"]):
            return {
                "status": "ignored",
                "reason": "stale_window",
                "symbol": symbol,
                "cycle_id": int(cycle["id"]),
                "cycle_no": int(cycle["cycle_no"]),
            }

        first = conn.execute(
            """
            SELECT * FROM launch_lifecycle_observations
            WHERE cycle_id = ?
            ORDER BY observation_no ASC
            LIMIT 1
            """,
            (int(cycle["id"]),),
        ).fetchone()
        previous = conn.execute(
            """
            SELECT * FROM launch_lifecycle_observations
            WHERE cycle_id = ?
            ORDER BY observation_no DESC
            LIMIT 1
            """,
            (int(cycle["id"]),),
        ).fetchone()
        observation = self._build_observation(
            cycle=cycle,
            snapshot=snapshot,
            stage=stage,
            observed_at=int(observed_at),
            first=first,
            previous=previous,
        )
        invalid_window_count = int(observation.pop("_invalid_window_count"))
        breakout_below_count = int(observation.pop("_breakout_below_count"))
        cycle_breakout_price = observation.pop("_cycle_breakout_price")
        end_reason = str(observation.pop("_end_reason") or "")
        columns = ", ".join(observation)
        placeholders = ", ".join("?" for _ in observation)
        cursor = conn.execute(
            f"INSERT INTO launch_lifecycle_observations ({columns}) VALUES ({placeholders})",
            tuple(observation.values()),
        )
        observation["id"] = int(cursor.lastrowid)

        observed_stage = str(stage or "idle")
        previous_peak = str(cycle["peak_stage"] or "idle")
        peak_stage = (
            observed_stage
            if STAGE_RANK.get(observed_stage, 0) > STAGE_RANK.get(previous_peak, 0)
            else previous_peak
        )
        lifecycle_status = str(observation["lifecycle_status"])
        lifecycle_stage = str(observation["lifecycle_stage"])
        ended_at = window_end_ts if lifecycle_status == FAILED_STATUS else None
        conn.execute(
            """
            UPDATE launch_lifecycle_cycles
            SET status = ?,
                current_stage = ?,
                peak_stage = ?,
                last_window_end = ?,
                ended_at = ?,
                end_reason = ?,
                invalid_window_count = ?,
                breakout_below_count = ?,
                breakout_price = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                lifecycle_status,
                lifecycle_stage,
                peak_stage,
                window_end_ts,
                ended_at,
                end_reason,
                invalid_window_count,
                breakout_below_count,
                cycle_breakout_price,
                int(observed_at),
                int(cycle["id"]),
            )
        )
        return self._result(
            conn,
            cycle_id=int(cycle["id"]),
            observation=observation,
            status="opened" if opened else lifecycle_status,
        )

    def _open_cycle(
        self,
        conn: sqlite3.Connection,
        *,
        symbol: str,
        stage: str,
        window_end_ts: int,
        observed_at: int,
        breakout_price: float | None,
    ) -> sqlite3.Row:
        next_cycle_no = int(
            conn.execute(
                "SELECT COALESCE(MAX(cycle_no), 0) + 1 FROM launch_lifecycle_cycles WHERE symbol = ?",
                (symbol,),
            ).fetchone()[0]
        )
        cursor = conn.execute(
            """
            INSERT INTO launch_lifecycle_cycles (
                symbol, cycle_no, status, current_stage, peak_stage,
                started_at, first_window_end, last_window_end,
                invalid_window_count, breakout_below_count, breakout_price,
                outcome_rule_key, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?, ?)
            """,
            (
                symbol,
                next_cycle_no,
                ACTIVE_STATUS,
                stage,
                stage,
                observed_at,
                window_end_ts,
                window_end_ts,
                breakout_price if breakout_price and breakout_price > 0 else None,
                self.outcome_rule_key,
                observed_at,
                observed_at,
            ),
        )
        return conn.execute(
            "SELECT * FROM launch_lifecycle_cycles WHERE id = ?",
            (int(cursor.lastrowid),),
        ).fetchone()

    def _build_observation(
        self,
        *,
        cycle: sqlite3.Row,
        snapshot: Mapping[str, Any],
        stage: str,
        observed_at: int,
        first: sqlite3.Row | None,
        previous: sqlite3.Row | None,
    ) -> dict[str, Any]:
        window_end_ts = int(_number(snapshot.get("window_end_ts")))
        score = int(_number(snapshot.get("score")))
        closed_price = _number(snapshot.get("closed_price"))
        closed_oi_usd = _number(snapshot.get("closed_oi_usd"))
        funding_pct = _number(snapshot.get("funding_pct"))
        funding_interval_hours = int(_number(snapshot.get("funding_interval_hours")))
        funding_8h_pct = _funding_8h(funding_pct, funding_interval_hours)
        rule_key = str(cycle["outcome_rule_key"] or self.outcome_rule_key)
        watch_score = int(
            self._rule_number(rule_key, "watch", self.watch_score)
        )
        invalid_windows_required = int(
            self._rule_number(
                rule_key,
                "invalid",
                self.invalid_windows_required,
            )
        )
        is_consecutive = (
            previous is None
            or window_end_ts - int(previous["window_end_ts"]) == self.window_sec
        )

        invalid_count = int(cycle["invalid_window_count"] or 0)
        if score < watch_score:
            invalid_count = invalid_count + 1 if is_consecutive else 1
        else:
            invalid_count = 0

        cycle_breakout_price = _number(cycle["breakout_price"])
        snapshot_breakout_price = _number(snapshot.get("breakout_price"))
        if cycle_breakout_price <= 0 and bool(snapshot.get("breakout")) and snapshot_breakout_price > 0:
            cycle_breakout_price = snapshot_breakout_price

        breakout_below_count = int(cycle["breakout_below_count"] or 0)
        if cycle_breakout_price > 0 and closed_price < cycle_breakout_price:
            breakout_below_count = breakout_below_count + 1 if is_consecutive else 1
        else:
            breakout_below_count = 0

        failed_by_score = invalid_count >= invalid_windows_required
        failed_by_breakout = breakout_below_count >= invalid_windows_required
        if failed_by_breakout:
            lifecycle_status = FAILED_STATUS
            lifecycle_stage = FAILED_STATUS
            end_reason = "two_closes_below_breakout"
        elif failed_by_score:
            lifecycle_status = FAILED_STATUS
            lifecycle_stage = FAILED_STATUS
            end_reason = "two_windows_below_watch_score"
        else:
            lifecycle_status = ACTIVE_STATUS
            lifecycle_stage = "cooling" if score < watch_score else str(stage or "idle")
            end_reason = ""

        first_row: Mapping[str, Any] = first if first is not None else {
            "closed_price": closed_price,
            "closed_oi_usd": closed_oi_usd,
            "funding_pct": funding_pct,
            "funding_8h_pct": funding_8h_pct,
            "funding_interval_hours": funding_interval_hours,
            "score": score,
        }
        previous_row: Mapping[str, Any] = previous if previous is not None else first_row
        first_funding_8h = first_row["funding_8h_pct"]
        previous_funding_8h = previous_row["funding_8h_pct"]
        observation_no = int(previous["observation_no"]) + 1 if previous is not None else 1
        previous_price_action = (
            _json_object(previous["price_action_json"])
            if previous is not None
            else {}
        )
        price_action = (
            advance_price_action_state(
                previous_price_action,
                snapshot.get("price_action_analysis"),
            )
            if self.price_action_enabled
            else {}
        )

        return {
            "cycle_id": int(cycle["id"]),
            "observation_no": observation_no,
            "symbol": str(snapshot.get("symbol") or "").upper(),
            "window_end_ts": window_end_ts,
            "observed_at": int(observed_at),
            "observed_stage": str(stage or "idle"),
            "lifecycle_stage": lifecycle_stage,
            "lifecycle_status": lifecycle_status,
            "score": score,
            "closed_price": closed_price,
            "closed_oi_usd": closed_oi_usd,
            "closed_quote_volume": _number(snapshot.get("closed_quote_volume")),
            "price_15m": _number(snapshot.get("price_15m")),
            "price_1h": _number(snapshot.get("price_1h")),
            "oi_15m": _number(snapshot.get("oi_15m")),
            "oi_1h": _number(snapshot.get("oi_1h")),
            "volume_ratio": _number(snapshot.get("volume_ratio")),
            "funding_available": int(bool(snapshot.get("funding_available"))),
            "funding_pct": funding_pct,
            "funding_interval_hours": funding_interval_hours,
            "funding_8h_pct": _round_optional(funding_8h_pct),
            "breakout": int(bool(snapshot.get("breakout"))),
            "breakout_price": snapshot_breakout_price if snapshot_breakout_price > 0 else None,
            "data_quality_status": str(snapshot.get("data_quality_status") or "unknown"),
            "data_quality_score": _number(snapshot.get("data_quality_score")),
            "quality_gate": str(snapshot.get("quality_gate") or "block"),
            "primary_data_source": str(snapshot.get("primary_data_source") or "binance_native"),
            "data_confirmation_json": json.dumps(
                dict(snapshot.get("data_confirmation") or {}),
                ensure_ascii=False,
            ),
            "reasons_json": json.dumps(list(snapshot.get("reasons") or []), ensure_ascii=False),
            "spot_active_net_usd": (
                _number(snapshot.get("spot_active_net_usd"))
                if snapshot.get("spot_active_net_usd") is not None
                else None
            ),
            "futures_active_net_usd": (
                _number(snapshot.get("futures_active_net_usd"))
                if snapshot.get("futures_active_net_usd") is not None
                else None
            ),
            "funds_direction": str(snapshot.get("funds_direction") or "unknown"),
            "price_action_json": json.dumps(price_action, ensure_ascii=False),
            "price_vs_first_pct": _round_optional(
                _percent_change(closed_price, _number(first_row["closed_price"]))
            ),
            "oi_vs_first_pct": _round_optional(
                _percent_change(closed_oi_usd, _number(first_row["closed_oi_usd"]))
            ),
            "funding_vs_first_pct_point": round(funding_pct - _number(first_row["funding_pct"]), 8),
            "funding_8h_vs_first_pct_point": _round_optional(
                funding_8h_pct - _number(first_funding_8h)
                if funding_8h_pct is not None and first_funding_8h is not None
                else None
            ),
            "funding_interval_vs_first_hours": (
                funding_interval_hours - int(first_row["funding_interval_hours"])
            ),
            "score_vs_first": score - int(first_row["score"]),
            "price_vs_previous_pct": _round_optional(
                _percent_change(closed_price, _number(previous_row["closed_price"]))
            ),
            "oi_vs_previous_pct": _round_optional(
                _percent_change(closed_oi_usd, _number(previous_row["closed_oi_usd"]))
            ),
            "funding_vs_previous_pct_point": round(
                funding_pct - _number(previous_row["funding_pct"]),
                8,
            ),
            "funding_8h_vs_previous_pct_point": _round_optional(
                funding_8h_pct - _number(previous_funding_8h)
                if funding_8h_pct is not None and previous_funding_8h is not None
                else None
            ),
            "funding_interval_vs_previous_hours": (
                funding_interval_hours - int(previous_row["funding_interval_hours"])
            ),
            "score_vs_previous": score - int(previous_row["score"]),
            "checkpoint_no": None,
            "checkpoint_reasons_json": "[]",
            "published_at": None,
            "_invalid_window_count": invalid_count,
            "_breakout_below_count": breakout_below_count,
            "_cycle_breakout_price": cycle_breakout_price if cycle_breakout_price > 0 else None,
            "_end_reason": end_reason,
        }

    def _result(
        self,
        conn: sqlite3.Connection,
        *,
        cycle_id: int,
        observation: Mapping[str, Any],
        status: str,
    ) -> dict[str, Any]:
        cycle = conn.execute(
            "SELECT * FROM launch_lifecycle_cycles WHERE id = ?",
            (int(cycle_id),),
        ).fetchone()
        publication = self._publication_context(
            conn,
            cycle=cycle,
            observation=observation,
        )
        outcome_evaluation = self._outcome_context(
            conn,
            cycle=cycle,
            evaluated_at=int(observation["observed_at"]),
        )
        return {
            "status": status,
            "cycle_id": int(cycle_id),
            "observation_id": int(observation["id"]),
            "cycle_no": int(cycle["cycle_no"]),
            "symbol": str(cycle["symbol"]),
            "cycle_status": str(cycle["status"]),
            "current_stage": str(cycle["current_stage"]),
            "peak_stage": str(cycle["peak_stage"]),
            "observation_no": int(observation["observation_no"]),
            "first_window_end": int(cycle["first_window_end"]),
            "window_end_ts": int(observation["window_end_ts"]),
            "duration_sec": max(
                0,
                int(observation["window_end_ts"]) - int(cycle["first_window_end"]),
            ),
            "invalid_window_count": int(cycle["invalid_window_count"]),
            "breakout_below_count": int(cycle["breakout_below_count"]),
            "end_reason": str(cycle["end_reason"] or ""),
            "delta_from_first": {
                "price_pct": observation["price_vs_first_pct"],
                "oi_pct": observation["oi_vs_first_pct"],
                "funding_pct_point": observation["funding_vs_first_pct_point"],
                "funding_8h_pct_point": observation["funding_8h_vs_first_pct_point"],
                "funding_interval_hours": int(observation["funding_interval_vs_first_hours"]),
                "score": int(observation["score_vs_first"]),
            },
            "delta_from_previous": {
                "price_pct": observation["price_vs_previous_pct"],
                "oi_pct": observation["oi_vs_previous_pct"],
                "funding_pct_point": observation["funding_vs_previous_pct_point"],
                "funding_8h_pct_point": observation["funding_8h_vs_previous_pct_point"],
                "funding_interval_hours": int(observation["funding_interval_vs_previous_hours"]),
                "score": int(observation["score_vs_previous"]),
            },
            "price_action": _json_object(observation.get("price_action_json")),
            "publication": publication,
            "outcome_evaluation": outcome_evaluation,
        }

    def _publication_context(
        self,
        conn: sqlite3.Connection,
        *,
        cycle: sqlite3.Row,
        observation: Mapping[str, Any],
    ) -> dict[str, Any]:
        current = dict(observation)
        first_row = conn.execute(
            """
            SELECT * FROM launch_lifecycle_observations
            WHERE cycle_id = ?
            ORDER BY observation_no ASC
            LIMIT 1
            """,
            (int(cycle["id"]),),
        ).fetchone()
        last_published = None
        if cycle["last_published_observation_id"] is not None:
            last_published = conn.execute(
                """
                SELECT * FROM launch_lifecycle_observations
                WHERE id = ? AND cycle_id = ?
                """,
                (
                    int(cycle["last_published_observation_id"]),
                    int(cycle["id"]),
                ),
            ).fetchone()
        latest_message_ids = self._message_ids(
            cycle["latest_message_ids_json"]
        )
        reasons = self._publication_reasons(current, last_published)
        if (
            self.package_enabled
            and str(cycle["status"]) == ACTIVE_STATUS
            and last_published is not None
            and not latest_message_ids
            and "active_message_missing" not in reasons
        ):
            reasons.append("active_message_missing")
        checkpoints = conn.execute(
            """
            SELECT * FROM launch_lifecycle_observations
            WHERE cycle_id = ? AND checkpoint_no IS NOT NULL
            ORDER BY checkpoint_no ASC
            """,
            (int(cycle["id"]),),
        ).fetchall()
        checkpoint_items = [
            self._observation_summary(row)
            for row in checkpoints
        ]
        current_checkpoint_no = (
            int(current["checkpoint_no"])
            if current.get("checkpoint_no") is not None
            else int(cycle["package_version"] or 0) + 1
        )
        return {
            "enabled": bool(self.package_enabled),
            "publish_required": bool(self.package_enabled and reasons),
            "checkpoint_no": current_checkpoint_no,
            "checkpoint_reasons": reasons,
            "first": self._observation_summary(first_row),
            "previous_published": self._observation_summary(last_published),
            "current": self._observation_summary(current),
            "checkpoints": checkpoint_items,
            "latest_message_ids": latest_message_ids,
            "cleanup_pending_message_ids": self._message_ids(
                cycle["cleanup_pending_message_ids_json"]
            ),
        }

    def _publication_reasons(
        self,
        current: Mapping[str, Any],
        previous: sqlite3.Row | None,
    ) -> list[str]:
        if not self.package_enabled:
            return []
        if current.get("checkpoint_no") is not None:
            return []
        if previous is None:
            return ["cycle_opened"]

        reasons: list[str] = []
        current_stage = str(current.get("lifecycle_stage") or "idle")
        previous_stage = str(previous["lifecycle_stage"] or "idle")
        if current_stage != previous_stage:
            reasons.append("stage_changed")
        if abs(int(current.get("score") or 0) - int(previous["score"] or 0)) >= self.package_score_delta:
            reasons.append("score_delta")
        price_delta = _percent_change(
            _number(current.get("closed_price")),
            _number(previous["closed_price"]),
        )
        if price_delta is not None and abs(price_delta) >= self.package_price_delta_pct:
            reasons.append("price_delta")
        oi_delta = _percent_change(
            _number(current.get("closed_oi_usd")),
            _number(previous["closed_oi_usd"]),
        )
        if oi_delta is not None and abs(oi_delta) >= self.package_oi_delta_pct:
            reasons.append("oi_delta")
        current_interval = int(_number(current.get("funding_interval_hours")))
        previous_interval = int(_number(previous["funding_interval_hours"]))
        if (
            current_interval > 0
            and previous_interval > 0
            and current_interval != previous_interval
        ):
            reasons.append("funding_interval_changed")
        current_funds = str(current.get("funds_direction") or "unknown")
        previous_funds = str(previous["funds_direction"] or "unknown")
        if current_funds.startswith("divergence_") and current_funds != previous_funds:
            reasons.append("funds_divergence")
        current_price_action = _json_object(current.get("price_action_json"))
        previous_price_action = _json_object(previous["price_action_json"])
        if (
            current_price_action.get("enabled")
            and str(current_price_action.get("event_key") or "")
            and current_price_action.get("event_key")
            != previous_price_action.get("event_key")
        ):
            reasons.append("price_action_changed")
        if (
            current_price_action.get("enabled")
            and str(current_price_action.get("smc_event_key") or "")
            and current_price_action.get("smc_event_key")
            != previous_price_action.get("smc_event_key")
        ):
            reasons.append("smc_changed")
        return reasons

    @staticmethod
    def _observation_summary(row: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if row is None:
            return None
        return {
            "observation_id": int(row["id"]),
            "observation_no": int(row["observation_no"]),
            "checkpoint_no": (
                int(row["checkpoint_no"])
                if row["checkpoint_no"] is not None
                else None
            ),
            "window_end_ts": int(row["window_end_ts"]),
            "stage": str(row["lifecycle_stage"]),
            "status": str(row["lifecycle_status"]),
            "score": int(row["score"]),
            "price": _number(row["closed_price"]),
            "oi_usd": _number(row["closed_oi_usd"]),
            "funding_pct": _number(row["funding_pct"]),
            "funding_interval_hours": int(_number(row["funding_interval_hours"])),
            "spot_active_net_usd": (
                _number(row["spot_active_net_usd"])
                if row["spot_active_net_usd"] is not None
                else None
            ),
            "futures_active_net_usd": (
                _number(row["futures_active_net_usd"])
                if row["futures_active_net_usd"] is not None
                else None
            ),
            "funds_direction": str(row["funds_direction"] or "unknown"),
            "price_action": _json_object(row["price_action_json"]),
            "checkpoint_reasons": (
                json.loads(str(row["checkpoint_reasons_json"] or "[]"))
                if row["checkpoint_reasons_json"] is not None
                else []
            ),
        }


__all__ = ["LaunchLifecycleStore", "OUTCOME_EVALUATION_VERSION"]
