from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any

from .signal_store import SignalEventStore


OUTCOME_HORIZONS = (
    ("15m", 15 * 60),
    ("1h", 60 * 60),
    ("4h", 4 * 60 * 60),
    ("24h", 24 * 60 * 60),
)
OUTCOME_PRICE_TOLERANCE_SEC = 15 * 60
OUTCOME_MISSING_GRACE_SEC = 30 * 60
OUTCOME_TRACKED_MODULES = ("flow", "launch", "funding")
TRUSTED_QUALITY_GATES = (
    "allow",
    "native_multi_exchange_only",
    "native_multi_exchange_override",
)

FLOW_LONG_CATEGORIES = frozenset({"真启动候选", "吸筹观察", "空头燃料"})
FLOW_SHORT_CATEGORIES = frozenset({"诱多/派发", "恐慌下跌"})
FUNDING_LONG_KINDS = frozenset({"multi_negative", "extreme_negative"})
FUNDING_SHORT_KINDS = frozenset({"multi_positive", "extreme_positive"})


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or abs(number) == float("inf"):
        return None
    return number


def _positive(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and number > 0 else None


def _payload(value: Any) -> dict[str, Any]:
    try:
        data = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def infer_signal_direction(module: str, facts: dict[str, Any]) -> str:
    """Return a trade hypothesis only when the stored signal semantics are explicit."""

    explicit = str(facts.get("signal_direction") or "").strip().lower()
    if explicit in {"long", "short"}:
        return explicit

    source = str(module or "").strip().lower()
    if source == "launch":
        return "long"
    if source == "flow":
        category = str(facts.get("category") or "").strip()
        if category in FLOW_LONG_CATEGORIES:
            return "long"
        if category in FLOW_SHORT_CATEGORIES:
            return "short"
        return ""
    if source == "funding":
        kind = str(facts.get("primary_kind") or "").strip().lower()
        if kind in FUNDING_LONG_KINDS:
            return "long"
        if kind in FUNDING_SHORT_KINDS:
            return "short"
    return ""


class SignalOutcomeTracker:
    """Evaluate stored, sent signal hypotheses from persisted market prices."""

    def __init__(self, signal_db_path: str | Path, market_db_path: str | Path):
        self.signal_db_path = Path(signal_db_path)
        self.market_db_path = Path(market_db_path)

    def _market_connection(self) -> sqlite3.Connection | None:
        if not self.market_db_path.exists():
            return None
        uri = f"file:{self.market_db_path.resolve().as_posix()}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("SELECT 1 FROM market_snapshots LIMIT 1")
            return conn
        except sqlite3.Error:
            try:
                conn.close()
            except (UnboundLocalError, sqlite3.Error):
                pass
            return None

    @staticmethod
    def _market_price(
        conn: sqlite3.Connection | None,
        symbol: str,
        target_ts: int,
        *,
        now_ts: int,
    ) -> tuple[float | None, int, str]:
        if conn is None:
            return None, 0, ""
        row = conn.execute(
            """
            SELECT price, observed_at, source
            FROM market_snapshots
            WHERE symbol = ?
              AND price IS NOT NULL
              AND price > 0
              AND observed_at BETWEEN ? AND ?
              AND observed_at <= ?
            ORDER BY
              CASE WHEN observed_at >= ? THEN 0 ELSE 1 END,
              ABS(observed_at - ?),
              CASE WHEN source = 'binance_futures_batch' THEN 0 ELSE 1 END,
              id
            LIMIT 1
            """,
            (
                symbol,
                int(target_ts) - OUTCOME_PRICE_TOLERANCE_SEC,
                int(target_ts) + OUTCOME_PRICE_TOLERANCE_SEC,
                int(now_ts),
                int(target_ts),
                int(target_ts),
            ),
        ).fetchone()
        if row is None:
            return None, 0, ""
        return _positive(row["price"]), int(row["observed_at"] or 0), str(row["source"] or "")

    @staticmethod
    def _facts(row: sqlite3.Row) -> dict[str, Any]:
        payload = _payload(row["payload_json"])
        facts = payload.get("facts")
        return facts if isinstance(facts, dict) else {}

    def refresh(self, *, now_ts: int | None = None, signal_limit: int = 5_000) -> dict[str, Any]:
        now = int(now_ts or time.time())
        created = 0
        matured = 0
        unavailable = 0
        lifecycle_package_outcomes_removed = 0
        tracked_signal_ids: set[int] = set()
        market_conn = self._market_connection()
        market_ready = market_conn is not None
        price_cache: dict[tuple[str, int], tuple[float | None, int, str]] = {}

        def market_price(symbol: str, target_ts: int) -> tuple[float | None, int, str]:
            key = (symbol, int(target_ts))
            if key not in price_cache:
                price_cache[key] = self._market_price(
                    market_conn,
                    symbol,
                    target_ts,
                    now_ts=now,
                )
            return price_cache[key]

        try:
            store = SignalEventStore(self.signal_db_path)
            with store.connect() as conn:
                removed = conn.execute(
                    """
                    DELETE FROM signal_outcomes
                    WHERE signal_id IN (
                        SELECT id FROM signals
                        WHERE module = 'launch'
                          AND dedup_key LIKE 'launch-package:%'
                    )
                    """
                )
                lifecycle_package_outcomes_removed = max(
                    0,
                    int(removed.rowcount),
                )
                signals = conn.execute(
                    """
                    SELECT id, ts, module, symbol, score, stage, payload_json
                    FROM signals
                    WHERE sent = 1
                      AND status = 'sent'
                      AND ingest_mode = 'structured'
                      AND symbol <> ''
                      AND module IN (?, ?, ?)
                      AND NOT (
                        module = 'launch'
                        AND dedup_key LIKE 'launch-package:%'
                      )
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (*OUTCOME_TRACKED_MODULES, max(1, int(signal_limit))),
                ).fetchall()
                for signal in signals:
                    facts = self._facts(signal)
                    if facts.get("evaluation_eligible") is False:
                        continue
                    direction = infer_signal_direction(str(signal["module"] or ""), facts)
                    quality_gate = str(facts.get("quality_gate") or "unknown").strip() or "unknown"
                    if not direction or quality_gate == "block":
                        continue
                    signal_id = int(signal["id"])
                    signal_ts = int(signal["ts"])
                    tracked_signal_ids.add(signal_id)
                    entry_price = _positive(facts.get("price"))
                    entry_source = "signal_fact" if entry_price is not None else ""
                    entry_observed_at = signal_ts if entry_price is not None else 0
                    category = str(
                        facts.get("category")
                        or facts.get("primary_kind")
                        or signal["stage"]
                        or ""
                    )[:80]
                    data_quality_score = _number(facts.get("data_quality_score"))
                    existing_count = int(
                        conn.execute(
                            "SELECT COUNT(*) FROM signal_outcomes WHERE signal_id = ?",
                            (signal_id,),
                        ).fetchone()[0]
                    )
                    if existing_count >= len(OUTCOME_HORIZONS):
                        continue
                    for horizon, horizon_sec in OUTCOME_HORIZONS:
                        cursor = conn.execute(
                            """
                            INSERT OR IGNORE INTO signal_outcomes (
                                signal_id, horizon, horizon_sec, due_at, status, direction,
                                signal_score, signal_stage, signal_category, quality_gate,
                                data_quality_score, entry_price, entry_observed_at, entry_source
                            ) VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                signal_id,
                                horizon,
                                horizon_sec,
                                signal_ts + horizon_sec,
                                direction,
                                _number(signal["score"]),
                                str(signal["stage"] or "")[:80],
                                category,
                                quality_gate,
                                data_quality_score,
                                entry_price,
                                entry_observed_at or None,
                                entry_source,
                            ),
                        )
                        created += max(0, int(cursor.rowcount))

                pending = conn.execute(
                    """
                    SELECT outcome.id, outcome.signal_id, outcome.due_at, outcome.status,
                           outcome.direction, outcome.entry_price, signal.symbol, signal.ts
                    FROM signal_outcomes AS outcome
                    JOIN signals AS signal ON signal.id = outcome.signal_id
                    WHERE outcome.status = 'pending'
                    ORDER BY outcome.due_at, outcome.id
                    """
                ).fetchall()
                for outcome in pending:
                    outcome_id = int(outcome["id"])
                    entry_price = _positive(outcome["entry_price"])
                    if entry_price is None:
                        entry_price, entry_at, entry_source = market_price(
                            str(outcome["symbol"]),
                            int(outcome["ts"]),
                        )
                        if entry_price is not None:
                            conn.execute(
                                """
                                UPDATE signal_outcomes
                                SET entry_price = ?, entry_observed_at = ?, entry_source = ?,
                                    status = 'pending', error = ''
                                WHERE id = ?
                                """,
                                (entry_price, entry_at, entry_source, outcome_id),
                            )
                    due_at = int(outcome["due_at"])
                    if entry_price is None:
                        if now > int(outcome["ts"]) + OUTCOME_MISSING_GRACE_SEC:
                            cursor = conn.execute(
                                """
                                UPDATE signal_outcomes
                                SET status = 'unavailable', evaluated_at = ?, error = 'missing_entry_price'
                                WHERE id = ? AND status <> 'unavailable'
                                """,
                                (now, outcome_id),
                            )
                            unavailable += max(0, int(cursor.rowcount))
                        continue
                    if now < due_at:
                        continue
                    exit_price, exit_at, exit_source = market_price(
                        str(outcome["symbol"]),
                        due_at,
                    )
                    if exit_price is not None:
                        raw_return = (exit_price - entry_price) / entry_price * 100
                        directional_return = raw_return if outcome["direction"] == "long" else -raw_return
                        cursor = conn.execute(
                            """
                            UPDATE signal_outcomes
                            SET status = 'matured', exit_price = ?, exit_observed_at = ?,
                                exit_source = ?, raw_return_pct = ?, directional_return_pct = ?,
                                is_hit = ?, evaluated_at = ?, error = ''
                            WHERE id = ? AND status <> 'matured'
                            """,
                            (
                                exit_price,
                                exit_at,
                                exit_source,
                                raw_return,
                                directional_return,
                                1 if directional_return > 0 else 0,
                                now,
                                outcome_id,
                            ),
                        )
                        matured += max(0, int(cursor.rowcount))
                    elif now > due_at + OUTCOME_MISSING_GRACE_SEC:
                        cursor = conn.execute(
                            """
                            UPDATE signal_outcomes
                            SET status = 'unavailable', evaluated_at = ?, error = 'missing_exit_price'
                            WHERE id = ? AND status <> 'unavailable'
                            """,
                            (now, outcome_id),
                        )
                        unavailable += max(0, int(cursor.rowcount))
        finally:
            if market_conn is not None:
                market_conn.close()

        result = {
            "status": "ok",
            "signals_tracked": len(tracked_signal_ids),
            "outcomes_created": created,
            "outcomes_matured": matured,
            "outcomes_unavailable": unavailable,
            "lifecycle_package_outcomes_removed": lifecycle_package_outcomes_removed,
            "market_database_ready": market_ready,
        }
        result["summary"] = self.summary()
        return result

    def summary(self) -> dict[str, Any]:
        store = SignalEventStore(self.signal_db_path)
        with store.connect() as conn:
            status_counts = {
                str(row["status"]): int(row["count"])
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS count FROM signal_outcomes GROUP BY status"
                ).fetchall()
            }
            tracked_signals = int(
                conn.execute("SELECT COUNT(DISTINCT signal_id) FROM signal_outcomes").fetchone()[0]
            )
            by_horizon = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT horizon, horizon_sec,
                           COUNT(*) AS samples,
                           ROUND(AVG(directional_return_pct), 6) AS avg_directional_return_pct,
                           ROUND(100.0 * AVG(is_hit), 2) AS hit_rate_pct
                    FROM signal_outcomes
                    WHERE status = 'matured'
                    GROUP BY horizon, horizon_sec
                    ORDER BY horizon_sec
                    """
                ).fetchall()
            ]
            by_module = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT signal.module, outcome.horizon, outcome.horizon_sec,
                           COUNT(*) AS samples,
                           ROUND(AVG(outcome.directional_return_pct), 6) AS avg_directional_return_pct,
                           ROUND(100.0 * AVG(outcome.is_hit), 2) AS hit_rate_pct
                    FROM signal_outcomes AS outcome
                    JOIN signals AS signal ON signal.id = outcome.signal_id
                    WHERE outcome.status = 'matured'
                    GROUP BY signal.module, outcome.horizon, outcome.horizon_sec
                    ORDER BY signal.module, outcome.horizon_sec
                    """
                ).fetchall()
            ]
            by_category = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT signal_category AS category, direction, horizon, horizon_sec,
                           COUNT(*) AS samples,
                           ROUND(AVG(directional_return_pct), 6) AS avg_directional_return_pct,
                           ROUND(100.0 * AVG(is_hit), 2) AS hit_rate_pct
                    FROM signal_outcomes
                    WHERE status = 'matured' AND signal_category <> ''
                    GROUP BY signal_category, direction, horizon, horizon_sec
                    ORDER BY signal_category, direction, horizon_sec
                    """
                ).fetchall()
            ]
            by_score_bucket = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT
                        CASE
                            WHEN signal_score IS NULL THEN 'unknown'
                            WHEN signal_score < 60 THEN '<60'
                            WHEN signal_score < 75 THEN '60-74'
                            WHEN signal_score < 90 THEN '75-89'
                            ELSE '90+'
                        END AS score_bucket,
                        horizon,
                        horizon_sec,
                        COUNT(*) AS samples,
                        ROUND(AVG(directional_return_pct), 6) AS avg_directional_return_pct,
                        ROUND(100.0 * AVG(is_hit), 2) AS hit_rate_pct
                    FROM signal_outcomes
                    WHERE status = 'matured'
                    GROUP BY score_bucket, horizon, horizon_sec
                    ORDER BY
                        CASE score_bucket
                            WHEN '<60' THEN 1
                            WHEN '60-74' THEN 2
                            WHEN '75-89' THEN 3
                            WHEN '90+' THEN 4
                            ELSE 5
                        END,
                        horizon_sec
                    """
                ).fetchall()
            ]
            by_quality_gate = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT quality_gate, horizon, horizon_sec,
                           COUNT(*) AS samples,
                           ROUND(AVG(directional_return_pct), 6) AS avg_directional_return_pct,
                           ROUND(100.0 * AVG(is_hit), 2) AS hit_rate_pct
                    FROM signal_outcomes
                    WHERE status = 'matured'
                    GROUP BY quality_gate, horizon, horizon_sec
                    ORDER BY quality_gate, horizon_sec
                    """
                ).fetchall()
            ]
            trusted_matured = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM signal_outcomes
                    WHERE status = 'matured' AND quality_gate IN (?, ?, ?)
                    """,
                    TRUSTED_QUALITY_GATES,
                ).fetchone()[0]
            )
            trusted_signals = int(
                conn.execute(
                    """
                    SELECT COUNT(DISTINCT signal_id) FROM signal_outcomes
                    WHERE status = 'matured' AND quality_gate IN (?, ?, ?)
                    """,
                    TRUSTED_QUALITY_GATES,
                ).fetchone()[0]
            )
            trusted_by_horizon = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT horizon, horizon_sec, COUNT(*) AS samples
                    FROM signal_outcomes
                    WHERE status = 'matured' AND quality_gate IN (?, ?, ?)
                    GROUP BY horizon, horizon_sec
                    ORDER BY horizon_sec
                    """,
                    TRUSTED_QUALITY_GATES,
                ).fetchall()
            ]
        normalized_counts = {
            status: int(status_counts.get(status, 0))
            for status in ("pending", "matured", "unavailable")
        }
        review_ready_horizons = [
            str(row["horizon"])
            for row in trusted_by_horizon
            if int(row["samples"] or 0) >= 50
        ]
        return {
            "tracked_signals": tracked_signals,
            "status_counts": normalized_counts,
            "trusted_matured_samples": trusted_matured,
            "trusted_matured_signals": trusted_signals,
            "trusted_by_horizon": trusted_by_horizon,
            "minimum_trusted_samples_for_review": 50,
            "review_ready_horizons": review_ready_horizons,
            "calibration_status": "ready_for_manual_review" if review_ready_horizons else "accumulating",
            "automatic_parameter_changes": False,
            "by_horizon": by_horizon,
            "by_module": by_module,
            "by_category": by_category,
            "by_score_bucket": by_score_bucket,
            "by_quality_gate": by_quality_gate,
        }


__all__ = [
    "OUTCOME_HORIZONS",
    "SignalOutcomeTracker",
    "infer_signal_direction",
]
