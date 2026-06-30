from __future__ import annotations

import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from .config import Settings
from .data_sources import HTTP_HEADERS


VALID_DIRECTIONS = {"above", "below"}


@dataclass(frozen=True)
class PriceAlert:
    id: int
    user_id: str
    chat_id: str
    username: str
    symbol: str
    direction: str
    target_price: float
    status: str
    source: str
    note: str
    created_at: int
    updated_at: int
    triggered_at: int | None = None
    last_price: float | None = None

    @property
    def direction_label(self) -> str:
        return "高于或等于" if self.direction == "above" else "低于或等于"

    @property
    def condition_text(self) -> str:
        op = ">=" if self.direction == "above" else "<="
        return f"{self.symbol} {op} {format_price(self.target_price)}"


def normalize_symbol(value: str) -> str:
    symbol = re.sub(r"[^A-Za-z0-9]", "", value or "").upper()
    if not symbol:
        raise ValueError("币种不能为空")
    if symbol.endswith("USD") and not symbol.endswith("USDT"):
        symbol = f"{symbol}T"
    if not symbol.endswith("USDT"):
        symbol = f"{symbol}USDT"
    if not re.fullmatch(r"[A-Z0-9]{3,30}", symbol):
        raise ValueError("币种格式不正确")
    return symbol


def parse_price(value: str | float | int) -> float:
    if isinstance(value, (int, float)):
        price = float(value)
    else:
        text = str(value).strip().replace(",", "")
        match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([kKmM]?)", text)
        if not match:
            raise ValueError("价格格式不正确")
        price = float(match.group(1))
        suffix = match.group(2).lower()
        if suffix == "k":
            price *= 1_000
        elif suffix == "m":
            price *= 1_000_000
    if price <= 0:
        raise ValueError("价格必须大于 0")
    return price


def normalize_direction(value: str) -> str:
    direction = (value or "").strip().lower()
    if direction in {"above", "up", "gte", ">=", ">", "高于", "突破", "涨到", "大于"}:
        return "above"
    if direction in {"below", "down", "lte", "<=", "<", "低于", "跌破", "小于"}:
        return "below"
    raise ValueError("方向只能是 above/below 或 高于/低于")


def format_price(value: float | None) -> str:
    if value is None:
        return "暂无"
    if value >= 1000:
        return f"${value:,.2f}"
    if value >= 1:
        return f"${value:.4f}".rstrip("0").rstrip(".")
    return f"${value:.8f}".rstrip("0").rstrip(".")


def format_ts(epoch: int | None) -> str:
    if not epoch:
        return ""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(epoch))


class PriceAlertStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    @contextmanager
    def connection(self) -> Any:
        conn = self.connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self.connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS price_alerts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    username TEXT NOT NULL DEFAULT '',
                    symbol TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    target_price REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    source TEXT NOT NULL DEFAULT 'telegram',
                    note TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    triggered_at INTEGER,
                    last_price REAL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS price_alert_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    price REAL,
                    message TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    FOREIGN KEY(alert_id) REFERENCES price_alerts(id)
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_price_alerts_status ON price_alerts(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_price_alerts_user ON price_alerts(user_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_price_alerts_symbol ON price_alerts(symbol)")

    def create_alert(
        self,
        *,
        user_id: str,
        chat_id: str,
        symbol: str,
        direction: str,
        target_price: float,
        username: str = "",
        source: str = "telegram",
        note: str = "",
    ) -> PriceAlert:
        now = int(time.time())
        normalized_symbol = normalize_symbol(symbol)
        normalized_direction = normalize_direction(direction)
        price = parse_price(target_price)
        with self.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO price_alerts
                (user_id, chat_id, username, symbol, direction, target_price, status, source, note, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?)
                """,
                (
                    str(user_id),
                    str(chat_id),
                    username or "",
                    normalized_symbol,
                    normalized_direction,
                    price,
                    source or "telegram",
                    note or "",
                    now,
                    now,
                ),
            )
            alert_id = int(cursor.lastrowid)
            self._record_event(conn, alert_id, "created", None, f"created {normalized_symbol}")
        alert = self.get_alert(alert_id)
        if alert is None:
            raise RuntimeError("提醒创建后无法读取")
        return alert

    def get_alert(self, alert_id: int) -> PriceAlert | None:
        with self.connection() as conn:
            row = conn.execute("SELECT * FROM price_alerts WHERE id = ?", (int(alert_id),)).fetchone()
        return row_to_alert(row) if row else None

    def list_alerts(
        self,
        *,
        user_id: str | None = None,
        status: str | None = None,
        limit: int = 200,
    ) -> list[PriceAlert]:
        clauses: list[str] = []
        args: list[Any] = []
        if user_id:
            clauses.append("user_id = ?")
            args.append(str(user_id))
        if status:
            clauses.append("status = ?")
            args.append(str(status))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        args.append(max(1, min(1000, int(limit))))
        with self.connection() as conn:
            rows = conn.execute(
                f"SELECT * FROM price_alerts {where} ORDER BY id DESC LIMIT ?",
                args,
            ).fetchall()
        return [row_to_alert(row) for row in rows]

    def active_symbols(self) -> list[str]:
        with self.connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT symbol FROM price_alerts WHERE status = 'active' ORDER BY symbol"
            ).fetchall()
        return [str(row["symbol"]) for row in rows]

    def set_status(self, alert_id: int, status: str, *, user_id: str | None = None) -> bool:
        if status not in {"active", "paused", "triggered"}:
            raise ValueError("状态不正确")
        now = int(time.time())
        clauses = ["id = ?"]
        args: list[Any] = [int(alert_id)]
        if user_id:
            clauses.append("user_id = ?")
            args.append(str(user_id))
        with self.connection() as conn:
            cursor = conn.execute(
                f"UPDATE price_alerts SET status = ?, updated_at = ? WHERE {' AND '.join(clauses)}",
                [status, now, *args],
            )
            changed = cursor.rowcount > 0
            if changed:
                self._record_event(conn, int(alert_id), status, None, f"status={status}")
        return changed

    def delete_alert(self, alert_id: int, *, user_id: str | None = None) -> bool:
        clauses = ["id = ?"]
        args: list[Any] = [int(alert_id)]
        if user_id:
            clauses.append("user_id = ?")
            args.append(str(user_id))
        with self.connection() as conn:
            cursor = conn.execute(f"DELETE FROM price_alerts WHERE {' AND '.join(clauses)}", args)
            changed = cursor.rowcount > 0
            if changed:
                self._record_event(conn, int(alert_id), "deleted", None, "deleted")
        return changed

    def update_last_price(self, symbol: str, price: float) -> None:
        now = int(time.time())
        with self.connection() as conn:
            conn.execute(
                "UPDATE price_alerts SET last_price = ?, updated_at = ? WHERE symbol = ? AND status = 'active'",
                (float(price), now, normalize_symbol(symbol)),
            )

    def mark_triggered(self, alert_id: int, price: float) -> bool:
        now = int(time.time())
        with self.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE price_alerts
                SET status = 'triggered', triggered_at = ?, last_price = ?, updated_at = ?
                WHERE id = ? AND status = 'active'
                """,
                (now, float(price), now, int(alert_id)),
            )
            changed = cursor.rowcount > 0
            if changed:
                self._record_event(conn, int(alert_id), "triggered", float(price), "triggered")
        return changed

    def stats(self) -> dict[str, int]:
        with self.connection() as conn:
            rows = conn.execute("SELECT status, COUNT(*) AS count FROM price_alerts GROUP BY status").fetchall()
        data = {"active": 0, "paused": 0, "triggered": 0, "total": 0}
        for row in rows:
            count = int(row["count"])
            data[str(row["status"])] = count
            data["total"] += count
        return data

    def _record_event(
        self,
        conn: sqlite3.Connection,
        alert_id: int,
        event_type: str,
        price: float | None,
        message: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO price_alert_events (alert_id, event_type, price, message, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (int(alert_id), event_type, price, message, int(time.time())),
        )


def row_to_alert(row: sqlite3.Row) -> PriceAlert:
    return PriceAlert(
        id=int(row["id"]),
        user_id=str(row["user_id"]),
        chat_id=str(row["chat_id"]),
        username=str(row["username"] or ""),
        symbol=str(row["symbol"]),
        direction=str(row["direction"]),
        target_price=float(row["target_price"]),
        status=str(row["status"]),
        source=str(row["source"] or ""),
        note=str(row["note"] or ""),
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
        triggered_at=int(row["triggered_at"]) if row["triggered_at"] is not None else None,
        last_price=float(row["last_price"]) if row["last_price"] is not None else None,
    )


def fetch_binance_prices(settings: Settings, symbols: list[str] | None = None) -> dict[str, float]:
    wanted = {normalize_symbol(symbol) for symbol in symbols} if symbols else set()
    timeout = max(3, int(settings.http_timeout_sec))
    response = requests.get(
        f"{settings.binance_fapi_base_url}/fapi/v1/ticker/24hr",
        headers=HTTP_HEADERS,
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list):
        return {}
    prices: dict[str, float] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").upper()
        if wanted and symbol not in wanted:
            continue
        try:
            prices[symbol] = float(item.get("lastPrice"))
        except (TypeError, ValueError):
            continue
    return prices


def triggered_alerts(alerts: list[PriceAlert], prices: dict[str, float]) -> list[tuple[PriceAlert, float]]:
    triggered: list[tuple[PriceAlert, float]] = []
    for alert in alerts:
        price = prices.get(alert.symbol)
        if price is None:
            continue
        if alert.direction == "above" and price >= alert.target_price:
            triggered.append((alert, price))
        elif alert.direction == "below" and price <= alert.target_price:
            triggered.append((alert, price))
    return triggered


def alert_to_dict(alert: PriceAlert) -> dict[str, Any]:
    return {
        "id": alert.id,
        "user_id": alert.user_id,
        "chat_id": alert.chat_id,
        "username": alert.username,
        "symbol": alert.symbol,
        "direction": alert.direction,
        "direction_label": alert.direction_label,
        "target_price": alert.target_price,
        "target_price_text": format_price(alert.target_price),
        "condition_text": alert.condition_text,
        "status": alert.status,
        "source": alert.source,
        "note": alert.note,
        "created_at": alert.created_at,
        "created_at_text": format_ts(alert.created_at),
        "updated_at": alert.updated_at,
        "updated_at_text": format_ts(alert.updated_at),
        "triggered_at": alert.triggered_at,
        "triggered_at_text": format_ts(alert.triggered_at),
        "last_price": alert.last_price,
        "last_price_text": format_price(alert.last_price),
    }
