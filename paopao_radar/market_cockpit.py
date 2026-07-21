from __future__ import annotations

import json
import math
import sqlite3
import time
from bisect import bisect_right
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import Settings
from .data_sources import BinanceDataSource
from .flow_radar import kline_cvd_flow_info
from .time_windows import closed_window


MARKET_COCKPIT_SCHEMA_VERSION = "2026-07-19.4"
SUPPORTED_WINDOWS = (900, 1800, 3600, 14400, 86400)
RADAR_ASSET_TYPES = {
    "AAPL": "美股", "AMD": "美股", "AMZN": "美股", "BABA": "美股",
    "COIN": "美股", "META": "美股", "MSTR": "美股", "MSFT": "美股",
    "MU": "美股", "NVDA": "美股", "SNDK": "美股", "SKHY": "美股",
    "SKHYNIX": "美股", "SPCX": "美股", "TSLA": "美股",
    "PAXG": "黄金", "XAU": "黄金", "XAUT": "黄金", "XAG": "白银",
}
SNAPSHOT_COLUMNS = (
    "price",
    "quote_volume",
    "market_cap",
    "oi_usd",
    "spot_inflow_usd",
    "spot_outflow_usd",
    "spot_flow_usd",
    "futures_inflow_usd",
    "futures_outflow_usd",
    "futures_flow_usd",
    "funding_pct",
)
FLOW_COLUMNS = (
    "spot_inflow_usd",
    "spot_outflow_usd",
    "spot_flow_usd",
    "futures_inflow_usd",
    "futures_outflow_usd",
    "futures_flow_usd",
)


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and number > 0 else None


def _nonnegative(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and number >= 0 else None


def _radar_asset_type(row: dict[str, Any], symbol: str) -> str | None:
    explicit = str(row.get("asset_type") or "").strip()
    if explicit:
        return explicit
    coin = symbol[:-4] if symbol.endswith("USDT") else symbol
    return RADAR_ASSET_TYPES.get(coin)


def _gross_flow(inflow: Any, outflow: Any) -> float | None:
    buy = _nonnegative(inflow)
    sell = _nonnegative(outflow)
    return buy + sell if buy is not None and sell is not None else None


def _positive_ratio(values: list[float]) -> float | None:
    positive = sum(value for value in values if value > 0)
    negative = sum(abs(value) for value in values if value < 0)
    total = positive + negative
    return round(positive / total, 6) if total > 0 else None


def _pct(current: Any, previous: Any) -> float | None:
    current_number = _number(current)
    previous_number = _number(previous)
    if current_number is None or previous_number is None or previous_number <= 0:
        return None
    return (current_number - previous_number) / previous_number * 100


def _signed_pct(current: Any, previous: Any) -> float | None:
    current_number = _number(current)
    previous_number = _number(previous)
    if current_number is None or previous_number in (None, 0):
        return None
    return (current_number - previous_number) / abs(previous_number) * 100


def _change_amount(current: Any, change_pct: Any) -> float | None:
    current_number = _positive(current)
    percent_number = _number(change_pct)
    if current_number is None or percent_number is None or percent_number <= -100:
        return None
    previous = current_number / (1 + percent_number / 100)
    return current_number - previous


def _iso(ts: int | float) -> str:
    if int(ts or 0) <= 0:
        return ""
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_window(value: Any, default: int = 3600) -> int:
    try:
        requested = int(value)
    except (TypeError, ValueError):
        requested = default
    return requested if requested in SUPPORTED_WINDOWS else default


def _coverage(value: Any) -> dict[str, bool]:
    source = value if isinstance(value, dict) else {}
    return {str(key): bool(item) for key, item in source.items()}


class MarketSnapshotStore:
    """SQLite history for the public market cockpit.

    Rows are append-only facts from a named collector. Public payloads merge the
    newest valid field per symbol while retaining each field's observed time.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialized = False

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            if not self._initialized:
                self._ensure_schema(conn)
                self._initialized = True
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS market_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                observed_at INTEGER NOT NULL,
                source TEXT NOT NULL,
                window_sec INTEGER NOT NULL DEFAULT 0,
                price REAL,
                price_change_pct REAL,
                change_window_sec INTEGER NOT NULL DEFAULT 0,
                quote_volume REAL,
                market_cap REAL,
                oi_usd REAL,
                oi_change_pct REAL,
                spot_inflow_usd REAL,
                spot_outflow_usd REAL,
                spot_flow_usd REAL,
                futures_inflow_usd REAL,
                futures_outflow_usd REAL,
                futures_flow_usd REAL,
                funding_pct REAL,
                coverage_json TEXT NOT NULL DEFAULT '{}',
                data_status TEXT NOT NULL DEFAULT 'fresh',
                created_at INTEGER NOT NULL,
                UNIQUE(symbol, observed_at, source)
            )
            """
        )
        existing_columns = {
            str(row[1])
            for row in conn.execute("PRAGMA table_info(market_snapshots)").fetchall()
        }
        migrations = {
            "market_cap": "REAL",
            "spot_inflow_usd": "REAL",
            "spot_outflow_usd": "REAL",
            "futures_inflow_usd": "REAL",
            "futures_outflow_usd": "REAL",
            "data_status": "TEXT NOT NULL DEFAULT 'fresh'",
        }
        for column, definition in migrations.items():
            if column not in existing_columns:
                conn.execute(f"ALTER TABLE market_snapshots ADD COLUMN {column} {definition}")
        conn.execute(
            """
            DELETE FROM market_snapshots
            WHERE id NOT IN (
                SELECT MAX(id) FROM market_snapshots GROUP BY symbol, observed_at, source
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_market_snapshots_fact ON market_snapshots(symbol, observed_at, source)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_market_snapshots_time ON market_snapshots(observed_at DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_market_snapshots_symbol_time ON market_snapshots(symbol, observed_at DESC)"
        )

    @staticmethod
    def _row(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        value = dict(row)
        try:
            value["coverage"] = json.loads(str(value.pop("coverage_json", "{}") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            value["coverage"] = {}
        return value

    def append_many(self, rows: list[dict[str, Any]]) -> int:
        prepared: list[dict[str, Any]] = []
        created_at = int(time.time())
        for raw in rows:
            if not isinstance(raw, dict):
                continue
            symbol = str(raw.get("symbol") or "").strip().upper()
            observed_at = int(_number(raw.get("observed_at")) or 0)
            source = str(raw.get("source") or "").strip()[:80]
            if not symbol.endswith("USDT") or len(symbol) > 24 or observed_at <= 0 or not source:
                continue
            prepared.append({
                "symbol": symbol,
                "observed_at": observed_at,
                "source": source,
                "window_sec": max(0, int(_number(raw.get("window_sec")) or 0)),
                "price": _positive(raw.get("price")),
                "price_change_pct": _number(raw.get("price_change_pct")),
                "change_window_sec": max(0, int(_number(raw.get("change_window_sec")) or 0)),
                "quote_volume": _positive(raw.get("quote_volume")),
                "market_cap": _positive(raw.get("market_cap")),
                "oi_usd": _positive(raw.get("oi_usd")),
                "oi_change_pct": _number(raw.get("oi_change_pct")),
                "spot_inflow_usd": _nonnegative(raw.get("spot_inflow_usd")),
                "spot_outflow_usd": _nonnegative(raw.get("spot_outflow_usd")),
                "spot_flow_usd": _number(raw.get("spot_flow_usd")),
                "futures_inflow_usd": _nonnegative(raw.get("futures_inflow_usd")),
                "futures_outflow_usd": _nonnegative(raw.get("futures_outflow_usd")),
                "futures_flow_usd": _number(raw.get("futures_flow_usd")),
                "funding_pct": _number(raw.get("funding_pct")),
                "coverage_json": json.dumps(_coverage(raw.get("coverage")), ensure_ascii=False, sort_keys=True),
                "data_status": str(raw.get("data_status") or "fresh")[:24],
                "created_at": created_at,
            })
        if not prepared:
            return 0
        with self.connect() as conn:
            conn.executemany(
                """
                INSERT INTO market_snapshots (
                    symbol, observed_at, source, window_sec, price, price_change_pct,
                    change_window_sec, quote_volume, market_cap, oi_usd, oi_change_pct,
                    spot_inflow_usd, spot_outflow_usd, spot_flow_usd,
                    futures_inflow_usd, futures_outflow_usd, futures_flow_usd, funding_pct, coverage_json,
                    data_status, created_at
                ) VALUES (
                    :symbol, :observed_at, :source, :window_sec, :price, :price_change_pct,
                    :change_window_sec, :quote_volume, :market_cap, :oi_usd, :oi_change_pct,
                    :spot_inflow_usd, :spot_outflow_usd, :spot_flow_usd,
                    :futures_inflow_usd, :futures_outflow_usd, :futures_flow_usd, :funding_pct, :coverage_json,
                    :data_status, :created_at
                )
                ON CONFLICT(symbol, observed_at, source) DO UPDATE SET
                    window_sec=excluded.window_sec,
                    price=excluded.price,
                    price_change_pct=excluded.price_change_pct,
                    change_window_sec=excluded.change_window_sec,
                    quote_volume=excluded.quote_volume,
                    market_cap=excluded.market_cap,
                    oi_usd=excluded.oi_usd,
                    oi_change_pct=excluded.oi_change_pct,
                    spot_inflow_usd=excluded.spot_inflow_usd,
                    spot_outflow_usd=excluded.spot_outflow_usd,
                    spot_flow_usd=excluded.spot_flow_usd,
                    futures_inflow_usd=excluded.futures_inflow_usd,
                    futures_outflow_usd=excluded.futures_outflow_usd,
                    futures_flow_usd=excluded.futures_flow_usd,
                    funding_pct=excluded.funding_pct,
                    coverage_json=excluded.coverage_json,
                    data_status=excluded.data_status
                """,
                prepared,
            )
        return len(prepared)

    def latest_timestamp(self, source: str = "") -> int:
        with self.connect() as conn:
            if source:
                row = conn.execute(
                    "SELECT MAX(observed_at) AS value FROM market_snapshots WHERE source = ?",
                    (source,),
                ).fetchone()
            else:
                row = conn.execute("SELECT MAX(observed_at) AS value FROM market_snapshots").fetchone()
        return int(row["value"] or 0) if row else 0

    @staticmethod
    def _window_flow(
        rows: list[dict[str, Any]],
        *,
        end_ts: int,
        window_sec: int,
    ) -> tuple[dict[str, float | None], str]:
        """Return one non-overlapping flow fact for the requested closed window."""

        start_ts = int(end_ts) - int(window_sec)
        expected = max(1, int(window_sec) // 900)
        canonical = [
            row for row in rows
            if str(row.get("source") or "") == "market_flow_15m"
            and int(row.get("window_sec") or 0) == 900
            and start_ts < int(row.get("observed_at") or 0) <= int(end_ts)
        ]
        canonical.sort(key=lambda row: int(row.get("observed_at") or 0))
        if len(canonical) >= expected:
            selected = canonical[-expected:]
            timestamps = [int(row.get("observed_at") or 0) for row in selected]
            if all(current - previous == 900 for previous, current in zip(timestamps, timestamps[1:])):
                values: dict[str, float | None] = {}
                for key in FLOW_COLUMNS:
                    samples = [_number(row.get(key)) for row in selected]
                    values[key] = sum(float(value) for value in samples if value is not None) if all(value is not None for value in samples) else None
                return values, "aggregated_15m"

        freshness_sec = max(int(window_sec), 1_800)
        exact = [
            row for row in rows
            if 0 <= int(end_ts) - int(row.get("observed_at") or 0) <= freshness_sec
            and int(row.get("window_sec") or 0) == int(window_sec)
            and any(_number(row.get(key)) is not None for key in FLOW_COLUMNS)
        ]
        if exact:
            row = max(exact, key=lambda item: int(item.get("observed_at") or 0))
            return {key: _number(row.get(key)) for key in FLOW_COLUMNS}, "exact_window"

        legacy = [
            row for row in rows
            if 0 <= int(end_ts) - int(row.get("observed_at") or 0) <= freshness_sec
            and int(row.get("window_sec") or 0) == 0
            and any(_number(row.get(key)) is not None for key in FLOW_COLUMNS)
        ]
        if legacy:
            row = max(legacy, key=lambda item: int(item.get("observed_at") or 0))
            return {key: _number(row.get(key)) for key in FLOW_COLUMNS}, "legacy_unscoped"
        return {key: None for key in FLOW_COLUMNS}, "insufficient"

    @staticmethod
    def _flow_history_samples(
        rows: list[dict[str, Any]],
        *,
        window_sec: int,
        quality: str,
    ) -> dict[str, list[float]]:
        """Return historical flow magnitudes using the current window's exact semantics."""

        samples: dict[str, list[float]] = defaultdict(list)
        safe_window = int(window_sec)
        if quality == "aggregated_15m":
            expected = max(1, safe_window // 900)
            canonical = sorted(
                (
                    row for row in rows
                    if str(row.get("source") or "") == "market_flow_15m"
                    and int(row.get("window_sec") or 0) == 900
                ),
                key=lambda row: int(row.get("observed_at") or 0),
            )
            if len(canonical) < expected:
                return samples
            timestamps = [int(row.get("observed_at") or 0) for row in canonical]
            gap_prefix = [0]
            for index in range(1, len(timestamps)):
                gap_prefix.append(gap_prefix[-1] + int(timestamps[index] - timestamps[index - 1] != 900))
            value_prefix: dict[str, list[float]] = {}
            missing_prefix: dict[str, list[int]] = {}
            for key in ("spot_flow_usd", "futures_flow_usd"):
                totals = [0.0]
                missing = [0]
                for row in canonical:
                    value = _number(row.get(key))
                    totals.append(totals[-1] + (float(value) if value is not None else 0.0))
                    missing.append(missing[-1] + int(value is None))
                value_prefix[key] = totals
                missing_prefix[key] = missing
            for end_index in range(expected - 1, len(canonical)):
                start_index = end_index - expected + 1
                if gap_prefix[end_index] - gap_prefix[start_index] != 0:
                    continue
                for key in ("spot_flow_usd", "futures_flow_usd"):
                    if missing_prefix[key][end_index + 1] - missing_prefix[key][start_index] == 0:
                        total = value_prefix[key][end_index + 1] - value_prefix[key][start_index]
                        samples[key].append(abs(total))
            return samples

        requested_window = safe_window if quality == "exact_window" else 0 if quality == "legacy_unscoped" else None
        if requested_window is None:
            return samples
        for row in rows:
            if int(row.get("window_sec") or 0) != requested_window:
                continue
            for key in ("spot_flow_usd", "futures_flow_usd"):
                value = _number(row.get(key))
                if value is not None:
                    samples[key].append(abs(value))
        return samples

    def recent_metric_rows(
        self,
        metric: str,
        *,
        now_ts: int,
        window_sec: int = 90_000,
        limit: int = 120_000,
    ) -> list[dict[str, Any]]:
        """Read one numeric snapshot metric across symbols for rolling anomaly ranks."""
        key = str(metric or "").strip()
        if key not in SNAPSHOT_COLUMNS:
            raise ValueError("unsupported snapshot metric")
        safe_limit = max(100, min(200_000, int(limit or 120_000)))
        start_ts = int(now_ts) - max(3_600, int(window_sec or 90_000))
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT symbol, observed_at, {key} AS metric_value
                FROM market_snapshots
                WHERE observed_at >= ? AND observed_at <= ? AND {key} IS NOT NULL
                ORDER BY symbol ASC, observed_at ASC, id ASC
                LIMIT ?
                """,
                (start_ts, int(now_ts), safe_limit),
            ).fetchall()
        merged: dict[tuple[str, int], dict[str, Any]] = {}
        for row in rows:
            symbol = str(row["symbol"] or "").upper()
            observed_at = int(row["observed_at"] or 0)
            value = _number(row["metric_value"])
            if not symbol.endswith("USDT") or observed_at <= 0 or value is None:
                continue
            merged[(symbol, observed_at)] = {
                "symbol": symbol,
                "observed_at": observed_at,
                key: value,
            }
        return [merged[index] for index in sorted(merged)]

    def readiness_summaries(
        self,
        settings: Settings,
        *,
        now_ts: int | None = None,
        requested_window_secs: tuple[int, ...] | list[int] = (3600,),
    ) -> dict[int, dict[str, Any]]:
        """Return window-specific readiness from one database snapshot."""

        now = int(now_ts or time.time())
        windows = tuple(dict.fromkeys(normalize_window(value) for value in requested_window_secs)) or (3600,)
        snapshot_interval = max(60, int(settings.market_snapshot_interval_sec))
        flow_interval = max(300, int(getattr(settings, "market_flow_fact_interval_sec", 900) or 900))
        target_sec = max(86400, int(getattr(settings, "market_readiness_target_days", 30) or 30) * 86400)
        freshness_budget = max(900, snapshot_interval * 3)
        recent_cutoff = now - max(freshness_budget, flow_interval * 2)
        with self.connect() as conn:
            bounds = conn.execute(
                "SELECT COUNT(*) AS total, MIN(observed_at) AS oldest, MAX(observed_at) AS latest, "
                "COUNT(DISTINCT symbol) AS symbols FROM market_snapshots"
            ).fetchone()
            metric = conn.execute(
                """
                SELECT
                    COUNT(DISTINCT CASE WHEN price IS NOT NULL THEN symbol END) AS price,
                    COUNT(DISTINCT CASE WHEN funding_pct IS NOT NULL THEN symbol END) AS funding,
                    COUNT(DISTINCT CASE WHEN oi_usd IS NOT NULL THEN symbol END) AS oi,
                    COUNT(DISTINCT CASE WHEN spot_flow_usd IS NOT NULL THEN symbol END) AS spot_flow,
                    COUNT(DISTINCT CASE WHEN futures_flow_usd IS NOT NULL THEN symbol END) AS futures_flow,
                    COUNT(DISTINCT CASE WHEN price IS NOT NULL THEN symbol END) AS assets
                FROM market_snapshots WHERE observed_at >= ? AND observed_at <= ?
                """,
                (recent_cutoff, now),
            ).fetchone()
            source_rows = conn.execute(
                "SELECT source, COUNT(*) AS count, MAX(observed_at) AS latest "
                "FROM market_snapshots WHERE observed_at >= ? GROUP BY source ORDER BY source",
                (now - target_sec,),
            ).fetchall()
        total = int(bounds["total"] or 0) if bounds else 0
        oldest = int(bounds["oldest"] or 0) if bounds else 0
        latest = int(bounds["latest"] or 0) if bounds else 0
        span = max(0, latest - oldest) if oldest and latest else 0
        age = max(0, now - latest) if latest else None
        assets = int(metric["assets"] or 0) if metric else 0
        price = int(metric["price"] or 0) if metric else 0
        funding = int(metric["funding"] or 0) if metric else 0
        oi = int(metric["oi"] or 0) if metric else 0
        spot_flow = int(metric["spot_flow"] or 0) if metric else 0
        futures_flow = int(metric["futures_flow"] or 0) if metric else 0
        oi_target = min(assets, max(1, int(settings.market_snapshot_limit))) if assets else 0
        flow_target = min(assets, max(1, int(getattr(settings, "market_flow_fact_limit", 40) or 40))) if assets else 0

        def ratio(value: int, denominator: int) -> float:
            return round(value / denominator, 4) if denominator else 0.0

        remaining = max(0, target_sec - span)
        summaries: dict[int, dict[str, Any]] = {}
        for requested_window_sec in windows:
            if total <= 0:
                status = "empty"
            elif age is None or age > freshness_budget:
                status = "stale"
            elif assets <= 0 or ratio(price, assets) < 0.8:
                status = "partial"
            elif span < max(300, int(requested_window_sec)):
                status = "warming_up"
            elif ratio(oi, oi_target) < 0.5 or min(ratio(spot_flow, flow_target), ratio(futures_flow, flow_target)) < 0.5:
                status = "partial"
            else:
                status = "ready"
            summaries[requested_window_sec] = {
                "status": status,
                "rows": total,
                "symbols_seen": int(bounds["symbols"] or 0) if bounds else 0,
                "oldest_at": _iso(oldest),
                "latest_at": _iso(latest),
                "history_span_sec": span,
                "history_target_sec": target_sec,
                "requested_window_sec": int(requested_window_sec),
                "warmup_progress_pct": round(min(100.0, span / target_sec * 100), 2),
                "warmup_remaining_sec": remaining,
                "estimated_full_history_at": _iso(now + remaining) if remaining else _iso(now),
                "freshness": {
                    "status": "fresh" if age is not None and age <= freshness_budget else "stale" if age is not None else "empty",
                    "age_sec": age,
                    "budget_sec": freshness_budget,
                },
                "coverage": {
                    "assets": assets,
                    "price": price, "price_ratio": ratio(price, assets),
                    "funding": funding, "funding_ratio": ratio(funding, assets),
                    "oi": oi, "oi_target": oi_target, "oi_ratio": ratio(oi, oi_target),
                    "spot_flow": spot_flow, "futures_flow": futures_flow, "flow_target": flow_target,
                    "spot_flow_ratio": ratio(spot_flow, flow_target),
                    "futures_flow_ratio": ratio(futures_flow, flow_target),
                },
                "source_status": [
                    {"source": str(row["source"]), "rows": int(row["count"] or 0), "latest_at": _iso(int(row["latest"] or 0))}
                    for row in source_rows
                ],
            }
        return summaries

    def readiness_summary(
        self,
        settings: Settings,
        *,
        now_ts: int | None = None,
        requested_window_sec: int = 3600,
    ) -> dict[str, Any]:
        safe_window = normalize_window(requested_window_sec)
        return self.readiness_summaries(
            settings,
            now_ts=now_ts,
            requested_window_secs=(safe_window,),
        )[safe_window]

    def comparisons(
        self,
        *,
        now_ts: int,
        window_secs: tuple[int, ...] | list[int],
        max_symbols: int = 240,
        symbols: tuple[str, ...] | list[str] | None = None,
    ) -> dict[int, tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]]:
        """Build several closed-window comparisons from one history read."""
        windows = tuple(dict.fromkeys(normalize_window(value) for value in window_secs)) or (3600,)
        start_ts = int(now_ts) - max(2 * max(windows), 2 * 86400)
        safe_symbols = max(1, min(500, int(max_symbols or 240)))
        with self.connect() as conn:
            requested_symbols = list(dict.fromkeys(
                str(symbol or "").strip().upper()
                for symbol in (symbols or [])
                if str(symbol or "").strip().upper().endswith("USDT")
            ))[:safe_symbols]
            if requested_symbols:
                selected_symbols = requested_symbols
            else:
                symbol_rows = conn.execute(
                    """
                    SELECT symbol, MAX(COALESCE(quote_volume, 0)) AS volume
                    FROM market_snapshots
                    WHERE observed_at >= ? AND observed_at <= ?
                    GROUP BY symbol
                    ORDER BY volume DESC, symbol ASC
                    LIMIT ?
                    """,
                    (int(now_ts) - max(7_200, max(windows)), int(now_ts), safe_symbols),
                ).fetchall()
                selected_symbols = [str(row["symbol"] or "") for row in symbol_rows if str(row["symbol"] or "")]
            if not selected_symbols:
                return {window: ([], {}) for window in windows}
            placeholders = ",".join("?" for _ in selected_symbols)
            row_limit = max(120_000, min(600_000, safe_symbols * 1_200))
            rows = conn.execute(
                f"""
                SELECT * FROM market_snapshots
                WHERE observed_at >= ? AND observed_at <= ?
                  AND symbol IN ({placeholders})
                ORDER BY symbol ASC, observed_at ASC, id ASC
                LIMIT ?
                """,
                (start_ts, int(now_ts), *selected_symbols, row_limit),
            ).fetchall()
        grouped_by_time: dict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
        source_rows_by_symbol: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            source = self._row(row)
            symbol = str(source.get("symbol") or "")
            observed_at = int(source.get("observed_at") or 0)
            if not symbol or observed_at <= 0:
                continue
            source_rows_by_symbol[symbol].append(source)
            point = grouped_by_time[symbol].setdefault(
                observed_at,
                {"symbol": symbol, "observed_at": observed_at, "coverage": {}},
            )
            point["coverage"].update(_coverage(source.get("coverage")))
            for key, value in source.items():
                if key not in {"id", "coverage", "created_at"} and value is not None:
                    point[key] = value
        grouped = {
            symbol: [points[index] for index in sorted(points)]
            for symbol, points in grouped_by_time.items()
        }

        results: dict[int, tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]] = {}
        for safe_window in windows:
            latest_rows: list[dict[str, Any]] = []
            baselines: dict[str, dict[str, Any]] = {}
            for symbol, history in grouped.items():
                latest = self._merge_latest(history, now_ts=int(now_ts), max_age_sec=max(2 * safe_window, 7200))
                if not latest:
                    continue
                current_flow, current_flow_quality = self._window_flow(
                    source_rows_by_symbol.get(symbol, []),
                    end_ts=int(latest["observed_at"]),
                    window_sec=safe_window,
                )
                latest.update(current_flow)
                latest["_flow_window_quality"] = current_flow_quality
                target = int(latest["observed_at"]) - safe_window
                candidates = [
                    row
                    for row in history
                    if int(row.get("observed_at") or 0) <= target and _positive(row.get("price"))
                ]
                if candidates:
                    baselines[symbol] = candidates[-1]
                    previous_flow, previous_flow_quality = self._window_flow(
                        source_rows_by_symbol.get(symbol, []),
                        end_ts=target,
                        window_sec=safe_window,
                    )
                    baselines[symbol].update(previous_flow)
                    baselines[symbol]["_flow_window_quality"] = previous_flow_quality
                    latest["_historical_strength"] = self._historical_strength(
                        history,
                        flow_rows=source_rows_by_symbol.get(symbol, []),
                        latest=latest,
                        baseline=candidates[-1],
                        window_sec=safe_window,
                    )
                latest_rows.append(latest)

            latest_rows.sort(key=lambda row: float(row.get("quote_volume") or 0), reverse=True)
            selected = latest_rows[:safe_symbols]
            allowed = {str(row.get("symbol") or "") for row in selected}
            results[safe_window] = (
                selected,
                {key: value for key, value in baselines.items() if key in allowed},
            )
        return results

    @staticmethod
    def _historical_strength(
        history: list[dict[str, Any]],
        *,
        flow_rows: list[dict[str, Any]],
        latest: dict[str, Any],
        baseline: dict[str, Any],
        window_sec: int,
    ) -> dict[str, float]:
        """Rank current movement against the same symbol's trailing history."""

        if len(history) < 6:
            return {}
        times = [int(point.get("observed_at") or 0) for point in history]
        samples: dict[str, list[float]] = defaultdict(list)
        tolerance = max(300, min(900, int(window_sec) // 3))
        for index, point in enumerate(history):
            point_at = times[index]
            previous_index = bisect_right(times, point_at - int(window_sec), 0, index) - 1
            if previous_index >= 0 and times[previous_index] >= point_at - int(window_sec) - tolerance:
                previous = history[previous_index]
                for key, source_key in (("price_change_pct", "price"), ("oi_change_pct", "oi_usd")):
                    change = _pct(point.get(source_key), previous.get(source_key))
                    if change is not None:
                        samples[key].append(abs(change))
            funding = _number(point.get("funding_pct"))
            if funding is not None:
                samples["funding_pct"].append(abs(funding))

        flow_samples = MarketSnapshotStore._flow_history_samples(
            flow_rows,
            window_sec=window_sec,
            quality=str(latest.get("_flow_window_quality") or "insufficient"),
        )
        samples.update(flow_samples)

        current_values = {
            "price_change_pct": _pct(latest.get("price"), baseline.get("price")),
            "oi_change_pct": _pct(latest.get("oi_usd"), baseline.get("oi_usd")),
            "spot_flow_usd": _number(latest.get("spot_flow_usd")),
            "futures_flow_usd": _number(latest.get("futures_flow_usd")),
            "funding_pct": _number(latest.get("funding_pct")),
        }
        result: dict[str, float] = {}
        for key, current in current_values.items():
            history_values = samples.get(key) or []
            if current is None or len(history_values) < 5:
                continue
            magnitude = abs(current)
            result[key] = round(
                sum(1 for value in history_values if value <= magnitude) / len(history_values) * 100,
                1,
            )
        return result

    def comparison(
        self,
        *,
        now_ts: int,
        window_sec: int,
        max_symbols: int = 240,
    ) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
        safe_window = normalize_window(window_sec)
        return self.comparisons(
            now_ts=now_ts,
            window_secs=(safe_window,),
            max_symbols=max_symbols,
        )[safe_window]

    def symbol_series(
        self,
        symbol: str,
        *,
        start_ts: int,
        end_ts: int,
        limit: int = 600,
    ) -> list[dict[str, Any]]:
        target = str(symbol or "").strip().upper()
        safe_limit = max(2, min(25_000, int(limit or 600)))
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM market_snapshots
                WHERE symbol = ? AND observed_at >= ? AND observed_at <= ?
                ORDER BY observed_at DESC, id DESC
                LIMIT ?
                """,
                (target, int(start_ts), int(end_ts), safe_limit * 4),
            ).fetchall()
        grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
        for row in reversed(rows):
            value = self._row(row)
            grouped[int(value.get("observed_at") or 0)].append(value)
        points: list[dict[str, Any]] = []
        for observed_at in sorted(grouped):
            if observed_at <= 0:
                continue
            point: dict[str, Any] = {
                "observed_at": observed_at,
                "updated_at": _iso(observed_at),
                "sources": [],
            }
            for row in grouped[observed_at]:
                source = str(row.get("source") or "")
                if source and source not in point["sources"]:
                    point["sources"].append(source)
                for key in (*SNAPSHOT_COLUMNS, "price_change_pct", "oi_change_pct"):
                    if row.get(key) is not None:
                        point[key] = row.get(key)
            points.append(point)
        return points[-safe_limit:]

    @staticmethod
    def _merge_latest(history: list[dict[str, Any]], *, now_ts: int, max_age_sec: int) -> dict[str, Any]:
        if not history:
            return {}
        newest = dict(history[-1])
        observed: dict[str, int] = {}
        coverage: dict[str, bool] = {}
        for row in reversed(history):
            row_ts = int(row.get("observed_at") or 0)
            if now_ts - row_ts > max_age_sec:
                break
            coverage.update(_coverage(row.get("coverage")))
            for key in (*SNAPSHOT_COLUMNS, "price_change_pct", "oi_change_pct"):
                if newest.get(key) is None and row.get(key) is not None:
                    newest[key] = row.get(key)
                if key not in observed and row.get(key) is not None:
                    observed[key] = row_ts
        newest["coverage"] = coverage
        newest["metric_observed_at"] = observed
        return newest

    def prune(self, *, before_ts: int) -> int:
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM market_snapshots WHERE observed_at < ?", (int(before_ts),))
            return int(cursor.rowcount or 0)


def _collect_binance_market_rows(
    settings: Settings,
    *,
    source: BinanceDataSource | None = None,
    now_ts: int | None = None,
    limit: int | None = None,
    oi_source: BinanceDataSource | None = None,
) -> list[dict[str, Any]]:
    loaded = source or BinanceDataSource(settings)
    observed_at = int(now_ts or time.time())
    valid_symbols = {
        str(item.get("symbol") or "")
        for item in loaded.usdt_perp_symbols()
        if isinstance(item, dict)
    }
    premium_map = {
        str(item.get("symbol") or ""): (_number(item.get("lastFundingRate")) or 0.0) * 100
        for item in loaded.premium_index()
        if isinstance(item, dict)
    }
    market_cap_map: dict[str, float] = {}
    if hasattr(loaded, "market_caps"):
        try:
            raw_caps = loaded.market_caps()
            market_cap_map = {
                str(key).upper(): float(value)
                for key, value in raw_caps.items()
                if _positive(value) is not None
            } if isinstance(raw_caps, dict) else {}
        except Exception:
            market_cap_map = {}
    excluded = set(settings.excluded_base_assets)
    rows: list[dict[str, Any]] = []
    for item in loaded.ticker_24h():
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").upper()
        if not symbol.endswith("USDT") or (valid_symbols and symbol not in valid_symbols):
            continue
        coin = symbol[:-4]
        if coin in excluded:
            continue
        quote_volume = _positive(item.get("quoteVolume"))
        price = _positive(item.get("lastPrice"))
        if quote_volume is None or price is None or quote_volume < settings.radar_min_quote_volume:
            continue
        rows.append({
            "symbol": symbol,
            "observed_at": observed_at,
            "source": "binance_futures_batch",
            "window_sec": 0,
            "price": price,
            "price_change_pct": _number(item.get("priceChangePercent")),
            "change_window_sec": 86400,
            "quote_volume": quote_volume,
            "market_cap": market_cap_map.get(coin),
            "funding_pct": premium_map.get(symbol),
            "coverage": {
                "price": True,
                "volume": True,
                "market_cap": coin in market_cap_map,
                "funding": symbol in premium_map,
            },
            "data_status": "fresh",
        })
    rows.sort(key=lambda row: float(row.get("quote_volume") or 0), reverse=True)
    safe_limit = max(20, min(500, int(limit or getattr(settings, "market_snapshot_limit", 160))))
    selected = rows[:safe_limit]
    oi_limit = max(0, min(len(selected), int(getattr(settings, "market_snapshot_oi_limit", 80) or 0)))
    if oi_limit and hasattr(oi_source or loaded, "open_interest_hist"):
        interval = max(60, int(getattr(settings, "market_snapshot_interval_sec", 300) or 300))
        offset = ((observed_at // interval) * oi_limit) % max(1, len(selected))
        targets = [selected[(offset + index) % len(selected)] for index in range(oi_limit)]
        oi_loaded = oi_source or loaded

        def fetch_oi(row: dict[str, Any]) -> tuple[str, float | None, float | None]:
            history = oi_loaded.open_interest_hist(str(row["symbol"]), period="5m", limit=2)
            values = [
                _positive(item.get("sumOpenInterestValue"))
                for item in history
                if isinstance(item, dict)
            ]
            clean_values = [value for value in values if value is not None]
            current = clean_values[-1] if clean_values else None
            previous = clean_values[-2] if len(clean_values) >= 2 else None
            return str(row["symbol"]), current, _pct(current, previous)

        workers = max(1, min(16, int(getattr(settings, "market_snapshot_workers", 8) or 8)))
        results: dict[str, tuple[float | None, float | None]] = {}
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="market-oi") as executor:
            futures = {executor.submit(fetch_oi, row): str(row["symbol"]) for row in targets}
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    _symbol, oi_usd, oi_change_pct = future.result()
                except Exception:
                    oi_usd, oi_change_pct = None, None
                results[symbol] = (oi_usd, oi_change_pct)
        for row in selected:
            if str(row["symbol"]) not in results:
                continue
            oi_usd, oi_change_pct = results[str(row["symbol"])]
            row["oi_usd"] = oi_usd
            row["oi_change_pct"] = oi_change_pct
            row["coverage"]["oi"] = oi_usd is not None
    return selected


def collect_binance_market_rows(
    settings: Settings,
    *,
    source: BinanceDataSource | None = None,
    now_ts: int | None = None,
    limit: int | None = None,
    oi_source: BinanceDataSource | None = None,
) -> list[dict[str, Any]]:
    owns_source = source is None
    loaded = source or BinanceDataSource(settings)
    try:
        return _collect_binance_market_rows(
            settings,
            source=loaded,
            now_ts=now_ts,
            limit=limit,
            oi_source=oi_source,
        )
    finally:
        if owns_source:
            loaded.http.close()


def collect_market_flow_facts(
    settings: Settings,
    *,
    source: BinanceDataSource,
    symbols: list[str],
    now_ts: int | None = None,
) -> tuple[int, list[dict[str, Any]]]:
    """Collect closed 15-minute spot/futures taker-flow facts independent of Telegram pushes."""

    now = int(now_ts or time.time())
    window_sec = max(300, int(getattr(settings, "market_flow_fact_interval_sec", 900) or 900))
    window = closed_window(
        now=datetime.fromtimestamp(now, timezone.utc),
        interval_sec=window_sec,
        delay_sec=max(0, min(window_sec // 2, int(getattr(settings, "flow_close_delay_sec", 300) or 300))),
    )
    observed_at = int(window.end.timestamp())
    safe_symbols = [str(symbol).upper() for symbol in symbols if str(symbol).upper().endswith("USDT")]
    safe_symbols = safe_symbols[: max(0, min(120, int(getattr(settings, "market_flow_fact_limit", 40) or 40)))]

    def fetch(symbol: str) -> dict[str, Any]:
        start_ms = window.start_ms
        end_ms = window.end_ms - 1
        spot = source.spot_klines(symbol, interval="5m", limit=4, start_time=start_ms, end_time=end_ms)
        futures = source.klines(symbol, interval="5m", limit=4, start_time=start_ms, end_time=end_ms)
        spot_delta, spot_in, spot_out, spot_ready, _ = kline_cvd_flow_info(spot, window)
        futures_delta, futures_in, futures_out, futures_ready, _ = kline_cvd_flow_info(futures, window)
        return {
            "symbol": symbol,
            "observed_at": observed_at,
            "source": "market_flow_15m",
            "window_sec": window_sec,
            "spot_inflow_usd": spot_in if spot_ready else None,
            "spot_outflow_usd": spot_out if spot_ready else None,
            "spot_flow_usd": spot_delta if spot_ready else None,
            "futures_inflow_usd": futures_in if futures_ready else None,
            "futures_outflow_usd": futures_out if futures_ready else None,
            "futures_flow_usd": futures_delta if futures_ready else None,
            "coverage": {"spot_flow": spot_ready, "futures_flow": futures_ready},
            "data_status": "fresh" if spot_ready and futures_ready else "degraded",
        }

    workers = max(1, min(16, int(getattr(settings, "market_snapshot_workers", 8) or 8)))
    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="market-flow") as executor:
        futures = {executor.submit(fetch, symbol): symbol for symbol in safe_symbols}
        for future in as_completed(futures):
            try:
                rows.append(future.result())
            except Exception:
                continue
    rows.sort(key=lambda row: safe_symbols.index(str(row["symbol"])))
    return observed_at, rows


def persist_market_flow_facts(
    settings: Settings,
    *,
    symbols: list[str],
    store: MarketSnapshotStore,
    now_ts: int | None = None,
) -> dict[str, Any]:
    now = int(now_ts or time.time())
    window_sec = max(300, int(getattr(settings, "market_flow_fact_interval_sec", 900) or 900))
    expected_window = closed_window(
        now=datetime.fromtimestamp(now, timezone.utc),
        interval_sec=window_sec,
        delay_sec=max(0, min(window_sec // 2, int(getattr(settings, "flow_close_delay_sec", 300) or 300))),
    )
    expected_at = int(expected_window.end.timestamp())
    previous = store.latest_timestamp("market_flow_15m")
    if previous >= expected_at:
        return {"status": "skipped", "count": 0, "observed_at": previous}
    dedicated = BinanceDataSource(settings)
    try:
        observed_at, rows = collect_market_flow_facts(settings, source=dedicated, symbols=symbols, now_ts=now)
        count = store.append_many(rows)
        return {"status": "saved" if count else "empty", "count": count, "observed_at": observed_at}
    finally:
        dedicated.http.close()


def persist_market_batch(
    settings: Settings,
    *,
    source: BinanceDataSource | None = None,
    store: MarketSnapshotStore | None = None,
    now_ts: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    now = int(now_ts or time.time())
    target = store or MarketSnapshotStore(settings.market_snapshots_db_path)
    interval = max(60, int(settings.market_snapshot_interval_sec))
    previous = target.latest_timestamp("binance_futures_batch")
    if not force and previous and now - previous < interval:
        return {"status": "skipped", "count": 0, "observed_at": previous}
    dedicated_oi = BinanceDataSource(settings) if isinstance(source, BinanceDataSource) else None
    try:
        rows = collect_binance_market_rows(settings, source=source, oi_source=dedicated_oi, now_ts=now)
    finally:
        if dedicated_oi is not None:
            dedicated_oi.http.close()
    count = target.append_many(rows)
    flow_result = (
        persist_market_flow_facts(
            settings,
            symbols=[str(row.get("symbol") or "") for row in rows],
            store=target,
            now_ts=now,
        )
        if rows and (source is None or isinstance(source, BinanceDataSource))
        else {"status": "not_scheduled", "count": 0, "observed_at": 0}
    )
    retention = max(1, int(settings.market_snapshot_retention_days)) * 86400
    pruned = target.prune(before_ts=now - retention)
    return {
        "status": "saved" if count else "empty",
        "count": count,
        "pruned": pruned,
        "observed_at": now,
        "flow_facts": flow_result,
    }


def persist_flow_market_rows(
    settings: Settings,
    flow: dict[str, Any],
    *,
    store: MarketSnapshotStore | None = None,
) -> int:
    observed_at = int(_number(flow.get("observed_at")) or time.time())
    window_sec = max(60, int(_number(flow.get("window_sec")) or settings.flow_interval_sec))
    raw_rows = flow.get("snapshots") if isinstance(flow.get("snapshots"), list) else []
    rows: list[dict[str, Any]] = []
    for item in raw_rows:
        if not isinstance(item, dict):
            continue
        rows.append({
            "symbol": item.get("symbol"),
            "observed_at": observed_at,
            "source": "flow_radar",
            "window_sec": window_sec,
            "price": item.get("price"),
            "quote_volume": item.get("quote_volume"),
            "market_cap": item.get("market_cap"),
            "oi_usd": item.get("oi_usd"),
            "oi_change_pct": item.get("oi_change_pct", item.get("oi_24h")),
            "spot_inflow_usd": item.get("spot_inflow_usd") if item.get("spot_cvd_ready") else None,
            "spot_outflow_usd": item.get("spot_outflow_usd") if item.get("spot_cvd_ready") else None,
            "spot_flow_usd": item.get("spot_cvd_delta") if item.get("spot_cvd_ready") else None,
            "futures_inflow_usd": item.get("futures_inflow_usd") if item.get("futures_cvd_ready") else None,
            "futures_outflow_usd": item.get("futures_outflow_usd") if item.get("futures_cvd_ready") else None,
            "futures_flow_usd": item.get("futures_cvd_delta") if item.get("futures_cvd_ready") else None,
            "funding_pct": item.get("funding_pct"),
            "coverage": {
                "price": bool(item.get("price_ready")),
                "volume": bool(_positive(item.get("quote_volume"))),
                "market_cap": bool(_positive(item.get("market_cap"))),
                "oi": bool(item.get("oi_ready")),
                "spot_flow": bool(item.get("spot_cvd_ready")),
                "futures_flow": bool(item.get("futures_cvd_ready")),
                "funding": item.get("funding_pct") is not None,
            },
            "data_status": "fresh" if item.get("price_ready") and item.get("oi_ready") else "degraded",
        })
    target = store or MarketSnapshotStore(settings.market_snapshots_db_path)
    return target.append_many(rows)


def _rank_percentiles(items: list[dict[str, Any]], key: str) -> None:
    available = sorted(
        (abs(float(item[key])), index)
        for index, item in enumerate(items)
        if _number(item.get(key)) is not None
    )
    total = len(available)
    if not total:
        return
    for position, (_value, index) in enumerate(available, start=1):
        items[index].setdefault("strength", {})[key] = round(position / total * 100, 1)


def _board_item(
    item: dict[str, Any],
    key: str,
    *,
    unit: str,
    magnitude_key: str | None = None,
) -> dict[str, Any]:
    value = _number(item.get(key))
    magnitude = _number(item.get(magnitude_key)) if magnitude_key else (value if unit == "usd" else None)
    return {
        "symbol": item.get("symbol"),
        "coin": item.get("coin"),
        "asset_type": item.get("asset_type"),
        "price": item.get("price"),
        "value": value,
        "unit": unit,
        "magnitude_usd": abs(magnitude) if magnitude is not None else None,
        "strength_percentile": (item.get("strength") or {}).get(key),
        "updated_at": item.get("updated_at"),
        "status": item.get("status"),
        "quality": (item.get("quality") or {}).get(key, "direct"),
    }


def _two_sided_board(
    assets: list[dict[str, Any]],
    *,
    key: str,
    board_key: str,
    title: str,
    positive_title: str,
    negative_title: str,
    unit: str,
    limit: int,
    amount_key: str | None = None,
    amount_unit: str | None = None,
) -> dict[str, Any]:
    available = [item for item in assets if _number(item.get(key)) is not None]
    positive = sorted((item for item in available if float(item[key]) > 0), key=lambda row: float(row[key]), reverse=True)
    negative = sorted((item for item in available if float(item[key]) < 0), key=lambda row: float(row[key]))
    amount_metric = amount_key or key
    amount_available = [item for item in available if _number(item.get(amount_metric)) is not None]
    amount_positive = sorted(
        (item for item in amount_available if float(item[key]) > 0),
        key=lambda row: abs(float(row[amount_metric])),
        reverse=True,
    )
    amount_negative = sorted(
        (item for item in amount_available if float(item[key]) < 0),
        key=lambda row: abs(float(row[amount_metric])),
        reverse=True,
    )
    strength_positive = sorted(
        positive,
        key=lambda row: float((row.get("strength") or {}).get(key) or 0),
        reverse=True,
    )
    strength_negative = sorted(
        negative,
        key=lambda row: float((row.get("strength") or {}).get(key) or 0),
        reverse=True,
    )
    item_kwargs = {"unit": unit, "magnitude_key": amount_key}
    return {
        "key": board_key,
        "title": title,
        "metric": key,
        "unit": unit,
        "amount_metric": amount_metric,
        "amount_unit": amount_unit or unit,
        "available": bool(available),
        "coverage": len(available),
        "positive": {"title": positive_title, "items": [_board_item(item, key, **item_kwargs) for item in positive[:limit]]},
        "negative": {"title": negative_title, "items": [_board_item(item, key, **item_kwargs) for item in negative[:limit]]},
        "amount_positive": {"title": positive_title, "items": [_board_item(item, key, **item_kwargs) for item in amount_positive[:limit]]},
        "amount_negative": {"title": negative_title, "items": [_board_item(item, key, **item_kwargs) for item in amount_negative[:limit]]},
        "strength_positive": {"title": positive_title, "items": [_board_item(item, key, **item_kwargs) for item in strength_positive[:limit]]},
        "strength_negative": {"title": negative_title, "items": [_board_item(item, key, **item_kwargs) for item in strength_negative[:limit]]},
        "reason": "" if available else "当前窗口尚未积累可验证数据",
    }


def build_market_cockpit(
    latest_rows: list[dict[str, Any]],
    baselines: dict[str, dict[str, Any]] | None = None,
    *,
    now_ts: int | None = None,
    window_sec: int = 3600,
    board_limit: int = 8,
) -> dict[str, Any]:
    now = int(now_ts or time.time())
    safe_window = normalize_window(window_sec)
    safe_limit = max(3, min(20, int(board_limit or 8)))
    baseline_map = baselines or {}
    assets: list[dict[str, Any]] = []
    for row in latest_rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper()
        price = _positive(row.get("price"))
        if not symbol.endswith("USDT") or price is None:
            continue
        baseline = baseline_map.get(symbol) if isinstance(baseline_map.get(symbol), dict) else {}
        price_change = _pct(price, baseline.get("price"))
        price_quality = "derived_window"
        price_window = safe_window
        if price_change is None:
            price_change = _number(row.get("price_change_pct"))
            price_window = int(_number(row.get("change_window_sec")) or 0)
            price_quality = "ticker_fallback" if price_change is not None else "missing"
        oi_value = _positive(row.get("oi_usd"))
        baseline_oi = _positive(baseline.get("oi_usd"))
        oi_change = _pct(oi_value, baseline_oi)
        oi_quality = "derived_window"
        if oi_change is None:
            oi_change = _number(row.get("oi_change_pct"))
            oi_quality = "collector_window" if oi_change is not None else "missing"
        oi_change_usd: float | None = None
        oi_amount_quality = "missing"
        if oi_value is not None and baseline_oi is not None:
            oi_change_usd = oi_value - baseline_oi
            oi_amount_quality = "derived_window"
        elif oi_value is not None and oi_change is not None and oi_change > -100:
            previous_oi = oi_value / (1 + oi_change / 100)
            oi_change_usd = oi_value - previous_oi
            oi_amount_quality = "derived_from_pct"
        spot_inflow = _nonnegative(row.get("spot_inflow_usd"))
        spot_outflow = _nonnegative(row.get("spot_outflow_usd"))
        spot_volume = _gross_flow(spot_inflow, spot_outflow)
        baseline_spot_volume = _gross_flow(baseline.get("spot_inflow_usd"), baseline.get("spot_outflow_usd"))
        futures_inflow = _nonnegative(row.get("futures_inflow_usd"))
        futures_outflow = _nonnegative(row.get("futures_outflow_usd"))
        futures_volume = _gross_flow(futures_inflow, futures_outflow)
        baseline_futures_volume = _gross_flow(baseline.get("futures_inflow_usd"), baseline.get("futures_outflow_usd"))
        observed_at = int(_number(row.get("observed_at")) or 0)
        age_sec = max(0, now - observed_at) if observed_at else 10**9
        status = str(row.get("data_status") or "fresh")
        if age_sec > max(2 * safe_window, 900):
            status = "stale"
        assets.append({
            "symbol": symbol,
            "coin": symbol[:-4],
            "asset_type": _radar_asset_type(row, symbol),
            "price": price,
            "price_change_pct": price_change,
            "price_change_window_sec": price_window,
            "quote_volume": _positive(row.get("quote_volume")),
            "volume_change_pct": _pct(row.get("quote_volume"), baseline.get("quote_volume")),
            "market_cap": _positive(row.get("market_cap")),
            "oi_usd": oi_value,
            "oi_change_pct": oi_change,
            "oi_change_usd": round(oi_change_usd, 2) if oi_change_usd is not None else None,
            "spot_inflow_usd": spot_inflow,
            "spot_outflow_usd": spot_outflow,
            "spot_flow_usd": _number(row.get("spot_flow_usd")),
            "spot_flow_change_pct": _signed_pct(row.get("spot_flow_usd"), baseline.get("spot_flow_usd")),
            "spot_volume_usd": spot_volume,
            "spot_volume_change_pct": _pct(spot_volume, baseline_spot_volume),
            "futures_inflow_usd": futures_inflow,
            "futures_outflow_usd": futures_outflow,
            "futures_flow_usd": _number(row.get("futures_flow_usd")),
            "futures_flow_change_pct": _signed_pct(row.get("futures_flow_usd"), baseline.get("futures_flow_usd")),
            "futures_volume_usd": futures_volume,
            "futures_volume_change_pct": _pct(futures_volume, baseline_futures_volume),
            "funding_pct": _number(row.get("funding_pct")),
            "coverage": _coverage(row.get("coverage")),
            "quality": {
                "price_change_pct": price_quality,
                "oi_change_pct": oi_quality,
                "oi_change_usd": oi_amount_quality,
                "flow_window": str(row.get("_flow_window_quality") or "direct"),
            },
            "_historical_strength": dict(row.get("_historical_strength") or {}),
            "status": status,
            "updated_at": _iso(observed_at),
            "age_sec": age_sec,
        })
    for key in ("price_change_pct", "volume_change_pct", "oi_change_pct", "spot_flow_usd", "futures_flow_usd", "funding_pct"):
        _rank_percentiles(assets, key)
    for item in assets:
        historical = item.pop("_historical_strength", {})
        for key, value in historical.items():
            if _number(value) is not None:
                item.setdefault("strength", {})[key] = float(value)

    boards = [
        _two_sided_board(assets, key="price_change_pct", board_key="price", title="价格动量", positive_title="涨幅榜", negative_title="跌幅榜", unit="percent", limit=safe_limit),
        _two_sided_board(assets, key="oi_change_pct", board_key="oi", title="持仓变化", positive_title="OI 增长", negative_title="OI 下降", unit="percent", amount_key="oi_change_usd", amount_unit="usd", limit=safe_limit),
        _two_sided_board(assets, key="futures_flow_usd", board_key="futures_flow", title="合约主动资金", positive_title="合约流入", negative_title="合约流出", unit="usd", limit=safe_limit),
        _two_sided_board(assets, key="spot_flow_usd", board_key="spot_flow", title="现货主动资金", positive_title="现货流入", negative_title="现货流出", unit="usd", limit=safe_limit),
        _two_sided_board(assets, key="funding_pct", board_key="funding", title="资金费率", positive_title="正费率", negative_title="负费率", unit="percent_per_cycle", limit=safe_limit),
    ]
    price_assets = [item for item in assets if _number(item.get("price_change_pct")) is not None]
    advancing = sum(1 for item in price_assets if float(item["price_change_pct"]) > 0)
    declining = sum(1 for item in price_assets if float(item["price_change_pct"]) < 0)
    spot_assets = [item for item in assets if _number(item.get("spot_flow_usd")) is not None]
    futures_assets = [item for item in assets if _number(item.get("futures_flow_usd")) is not None]
    oi_assets = [item for item in assets if _number(item.get("oi_change_pct")) is not None]
    oi_amount_assets = [item for item in assets if _number(item.get("oi_change_usd")) is not None]
    spot_net = sum(float(item["spot_flow_usd"]) for item in spot_assets)
    futures_net = sum(float(item["futures_flow_usd"]) for item in futures_assets)
    oi_net_change = sum(float(item["oi_change_usd"]) for item in oi_amount_assets)
    breadth = ((advancing - declining) / len(price_assets) * 100) if price_assets else 0.0
    selected_symbols = {str(item.get("symbol") or "") for item in assets}
    previous_rows = [
        row for symbol, row in baseline_map.items()
        if symbol in selected_symbols and isinstance(row, dict)
    ]
    previous_spot = [_number(row.get("spot_flow_usd")) for row in previous_rows]
    previous_futures = [_number(row.get("futures_flow_usd")) for row in previous_rows]
    previous_oi = [_change_amount(row.get("oi_usd"), row.get("oi_change_pct")) for row in previous_rows]
    previous_prices = [_number(row.get("price_change_pct")) for row in previous_rows]
    previous_spot_values = [value for value in previous_spot if value is not None]
    previous_futures_values = [value for value in previous_futures if value is not None]
    previous_oi_values = [value for value in previous_oi if value is not None]
    previous_price_values = [value for value in previous_prices if value is not None]
    previous_advancing = sum(1 for value in previous_price_values if value > 0)
    previous_declining = sum(1 for value in previous_price_values if value < 0)
    previous_breadth = (
        (previous_advancing - previous_declining) / len(previous_price_values) * 100
        if previous_price_values else None
    )
    previous_overview = {
        "advancing": previous_advancing if previous_price_values else None,
        "declining": previous_declining if previous_price_values else None,
        "breadth_pct": round(previous_breadth, 2) if previous_breadth is not None else None,
        "spot_net_flow_usd": round(sum(previous_spot_values), 2) if previous_spot_values else None,
        "futures_net_flow_usd": round(sum(previous_futures_values), 2) if previous_futures_values else None,
        "oi_net_change_usd": round(sum(previous_oi_values), 2) if previous_oi_values else None,
    }
    current_overview = {
        "breadth_pct": round(breadth, 2),
        "spot_net_flow_usd": round(spot_net, 2) if spot_assets else None,
        "futures_net_flow_usd": round(futures_net, 2) if futures_assets else None,
        "oi_net_change_usd": round(oi_net_change, 2) if oi_amount_assets else None,
    }
    directional_ratios = {
        "spot_positive_ratio": _positive_ratio([float(item["spot_flow_usd"]) for item in spot_assets]),
        "futures_positive_ratio": _positive_ratio([float(item["futures_flow_usd"]) for item in futures_assets]),
        "oi_positive_ratio": _positive_ratio([float(item["oi_change_usd"]) for item in oi_amount_assets]),
    }
    comparison_delta = {
        key: round(float(current_overview[key]) - float(previous_overview[key]), 2)
        if current_overview.get(key) is not None and previous_overview.get(key) is not None else None
        for key in current_overview
    }
    if spot_assets or futures_assets:
        net_flow = spot_net + futures_net
        bias = "inflow" if net_flow > 0 and breadth >= 0 else "outflow" if net_flow < 0 and breadth <= 0 else "mixed"
    else:
        bias = "broad_up" if breadth >= 20 else "broad_down" if breadth <= -20 else "mixed"
    warnings: list[str] = []
    if any(item.get("quality", {}).get("price_change_pct") == "ticker_fallback" for item in assets):
        warnings.append("所选周期历史快照尚未完整时，价格榜使用 24h 行情回退并明确标记。")
    if len(oi_assets) < max(1, len(assets) // 5):
        warnings.append("OI 覆盖不足，OI 榜单可能为空或仅覆盖资金流扫描候选。")
    if len(spot_assets) < max(1, len(assets) // 5) or len(futures_assets) < max(1, len(assets) // 5):
        warnings.append("现货/合约主动资金来自 CVD 估算，仅覆盖完成资金流扫描的资产。")
    ready_boards = sum(1 for board in boards[:4] if board["available"])
    data_status = "ready" if assets and ready_boards == 4 else "degraded" if assets else "empty"
    assets.sort(key=lambda item: float(item.get("quote_volume") or 0), reverse=True)
    return {
        "schema_version": MARKET_COCKPIT_SCHEMA_VERSION,
        "generated_at": _iso(now),
        "window_sec": safe_window,
        "data_status": data_status,
        "warnings": warnings,
        "coverage": {
            "assets": len(assets),
            "price": len(price_assets),
            "oi": len(oi_assets),
            "spot_flow": len(spot_assets),
            "futures_flow": len(futures_assets),
            "funding": sum(1 for item in assets if _number(item.get("funding_pct")) is not None),
        },
        "overview": {
            "bias": bias,
            "advancing": advancing,
            "declining": declining,
            "flat": max(0, len(price_assets) - advancing - declining),
            "breadth_pct": round(breadth, 2),
            "total_quote_volume": round(sum(float(item.get("quote_volume") or 0) for item in assets), 2),
            "spot_net_flow_usd": round(spot_net, 2) if spot_assets else None,
            "futures_net_flow_usd": round(futures_net, 2) if futures_assets else None,
            "oi_net_change_usd": round(oi_net_change, 2) if oi_amount_assets else None,
            **directional_ratios,
            "comparison": {
                "previous": previous_overview,
                "delta": comparison_delta,
            },
        },
        "boards": boards,
        "assets": assets,
        "methodology": {
            "price": "优先使用同币窗口首尾快照计算；历史不足时回退交易所 24h 涨跌并标记质量。",
            "oi": "优先使用同币窗口首尾 OI 金额计算；否则使用资金流采集器的封闭窗口变化率反推金额变化，并标记质量。",
            "flow": "现货与合约资金优先由封闭 15m Binance K 线主动买卖事实按所选窗口求和；历史库只有同窗口事实时使用同窗口值，不拿短窗口冒充长窗口。CVD 不代表交易所充提净流入。",
            "directional_balance": "全场态势红绿比例为各指标正向贡献金额 / 正负绝对贡献金额之和。",
            "strength": "优先按同币近 48h 同窗口历史样本计算异常强度分位；历史不足时回退当前横截面分位。",
        },
    }


def load_market_cockpit_windows(
    settings: Settings,
    *,
    window_secs: tuple[int, ...] | list[int] = (3600,),
    board_limit: int = 8,
    now_ts: int | None = None,
    store: MarketSnapshotStore | None = None,
    live_rows: list[dict[str, Any]] | None = None,
) -> dict[int, dict[str, Any]]:
    now = int(now_ts or time.time())
    windows = tuple(dict.fromkeys(normalize_window(value) for value in window_secs)) or (3600,)
    target = store or MarketSnapshotStore(settings.market_snapshots_db_path)
    comparisons = target.comparisons(
        now_ts=now,
        window_secs=windows,
        max_symbols=max(20, int(settings.market_snapshot_limit)),
    )
    readiness_by_window = target.readiness_summaries(
        settings,
        now_ts=now,
        requested_window_secs=windows,
    )
    fallback_rows: list[dict[str, Any]] | None = None
    payloads: dict[int, dict[str, Any]] = {}
    for window_sec in windows:
        latest, baselines = comparisons[window_sec]
        if not latest:
            if fallback_rows is None:
                fallback_rows = live_rows if live_rows is not None else collect_binance_market_rows(settings, now_ts=now)
            latest = fallback_rows
        payload = build_market_cockpit(
            latest,
            baselines,
            now_ts=now,
            window_sec=window_sec,
            board_limit=board_limit,
        )
        readiness = readiness_by_window[window_sec]
        payload["readiness"] = readiness
        readiness_status = str(readiness.get("status") or "empty")
        if readiness_status == "empty" and payload.get("assets"):
            readiness_status = "warming_up"
        if readiness_status in {"warming_up", "partial", "stale"}:
            payload["data_status"] = readiness_status
        elif readiness_status == "empty" and not payload.get("assets"):
            payload["data_status"] = "empty"
        payloads[window_sec] = payload
    return payloads


def load_market_cockpit(
    settings: Settings,
    *,
    window_sec: int = 3600,
    board_limit: int = 8,
    now_ts: int | None = None,
    store: MarketSnapshotStore | None = None,
    live_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    safe_window = normalize_window(window_sec)
    return load_market_cockpit_windows(
        settings,
        window_secs=(safe_window,),
        board_limit=board_limit,
        now_ts=now_ts,
        store=store,
        live_rows=live_rows,
    )[safe_window]


__all__ = [
    "MARKET_COCKPIT_SCHEMA_VERSION",
    "SUPPORTED_WINDOWS",
    "MarketSnapshotStore",
    "build_market_cockpit",
    "collect_binance_market_rows",
    "collect_market_flow_facts",
    "load_market_cockpit",
    "load_market_cockpit_windows",
    "normalize_window",
    "persist_flow_market_rows",
    "persist_market_flow_facts",
    "persist_market_batch",
]
