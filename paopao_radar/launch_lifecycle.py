from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping


ACTIVE_STATUS = "active"
FAILED_STATUS = "failed"
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


@dataclass(frozen=True)
class LaunchLifecycleStore:
    """Durable, window-idempotent lifecycle state for launch signals."""

    db_path: Path
    watch_score: int = 45
    start_score: int = 60
    invalid_windows_required: int = 2
    window_sec: int = 15 * 60

    def __post_init__(self) -> None:
        object.__setattr__(self, "db_path", Path(self.db_path))
        object.__setattr__(self, "invalid_windows_required", max(1, int(self.invalid_windows_required)))
        object.__setattr__(self, "window_sec", max(60, int(self.window_sec)))

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
                UNIQUE(cycle_id, window_end_ts),
                FOREIGN KEY(cycle_id) REFERENCES launch_lifecycle_cycles(id) ON DELETE CASCADE
            )
            """
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
                items.append(item)
            return items

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
                stage=stage,
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

    @staticmethod
    def _open_cycle(
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
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?)
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
        is_consecutive = (
            previous is None
            or window_end_ts - int(previous["window_end_ts"]) == self.window_sec
        )

        invalid_count = int(cycle["invalid_window_count"] or 0)
        if score < self.watch_score:
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

        failed_by_score = invalid_count >= self.invalid_windows_required
        failed_by_breakout = breakout_below_count >= self.invalid_windows_required
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
            lifecycle_stage = "cooling" if score < self.watch_score else str(stage or "idle")
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
            "_invalid_window_count": invalid_count,
            "_breakout_below_count": breakout_below_count,
            "_cycle_breakout_price": cycle_breakout_price if cycle_breakout_price > 0 else None,
            "_end_reason": end_reason,
        }

    @staticmethod
    def _result(
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
        return {
            "status": status,
            "cycle_id": int(cycle_id),
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
        }


__all__ = ["LaunchLifecycleStore"]
