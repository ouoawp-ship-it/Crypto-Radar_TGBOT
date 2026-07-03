from __future__ import annotations

import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from .config import Settings
from .data_sources import HTTP_HEADERS


VALID_DIRECTIONS = {"above", "below", "up", "down", "both"}
VALID_ALERT_TYPES = {"target_price", "price_change", "oi_change", "funding_change"}
VALID_REPEAT_POLICIES = {"once", "repeat", "interval"}
VALID_EXCHANGES = {"binance", "bybit", "okx", "bitget", "gate"}
VALID_MARKET_TYPES = {"spot", "futures"}
ALERT_MARKET_EXCHANGES = ("binance", "bybit", "okx", "bitget", "gate")
ALERT_MARKET_TYPES = ("spot", "futures")
ALERT_MARKET_CACHE_TTL_SEC = 30
ALERT_QUOTE_CACHE_TTL_SEC = 3
ALERT_MARKET_WORKERS = 10
FUTURES_CONTRACT_PREFIXES = ("1000", "10000", "1000000")
TIMEFRAME_LABELS = {
    300: "5分钟",
    900: "15分钟",
    3600: "60分钟",
}
REPEAT_POLICY_LABELS = {
    "once": "提醒一次",
    "repeat": "重复提醒",
    "interval": "持续提醒",
}
ALERT_TYPE_LABELS = {
    "target_price": "目标价提醒",
    "price_change": "价格急涨急跌监控",
    "oi_change": "持仓量变化监控",
    "funding_change": "资金费率变化监控",
}
EXCHANGE_LABELS = {
    "binance": "Binance",
    "bybit": "Bybit",
    "okx": "OKX",
    "bitget": "Bitget",
    "gate": "Gate",
}
MARKET_TYPE_LABELS = {
    "spot": "现货",
    "futures": "USDT 合约",
}

_ALERT_MARKET_CACHE_LOCK = threading.Lock()
_ALERT_MARKET_CACHE: dict[tuple[Any, ...], tuple[float, list["AlertMarketQuote"]]] = {}
_ALERT_QUOTE_CACHE_LOCK = threading.Lock()
_ALERT_QUOTE_CACHE: dict[tuple[Any, ...], tuple[float, AlertMarketQuote | None]] = {}


@dataclass(frozen=True)
class PriceAlert:
    id: int
    user_id: str
    chat_id: str
    username: str
    symbol: str
    exchange: str
    market_type: str
    pair: str
    direction: str
    target_price: float
    status: str
    source: str
    note: str
    created_at: int
    updated_at: int
    triggered_at: int | None = None
    last_price: float | None = None
    alert_type: str = "target_price"
    timeframe_sec: int = 0
    threshold_pct: float = 0.0
    repeat_policy: str = "once"
    repeat_interval_sec: int = 0
    last_triggered_at: int | None = None
    trigger_count: int = 0
    last_value: float | None = None
    last_baseline: float | None = None
    metadata: str = ""

    @property
    def direction_label(self) -> str:
        return {
            "above": "高于或等于",
            "below": "低于或等于",
            "up": "上涨",
            "down": "下跌",
            "both": "双向",
        }.get(self.direction, self.direction)

    @property
    def condition_text(self) -> str:
        if self.alert_type == "price_change":
            return f"{self.venue_label} {self.pair} {self.timeframe_label}价格{self.direction_label}超过 {self.threshold_pct:g}%"
        if self.alert_type == "oi_change":
            return f"{self.exchange_label} 合约 {self.pair} {self.timeframe_label}持仓量{self.direction_label}超过 {self.threshold_pct:g}%"
        if self.alert_type == "funding_change":
            return f"{self.exchange_label} 合约 {self.pair} 监控资金费率周期缩短或极端正负费率"
        op = ">=" if self.direction == "above" else "<="
        return f"{self.venue_label} {self.pair} {op} {format_price(self.target_price)}"

    @property
    def alert_type_label(self) -> str:
        return ALERT_TYPE_LABELS.get(self.alert_type, self.alert_type)

    @property
    def timeframe_label(self) -> str:
        return TIMEFRAME_LABELS.get(self.timeframe_sec, f"{self.timeframe_sec}秒" if self.timeframe_sec else "-")

    @property
    def repeat_policy_label(self) -> str:
        if self.repeat_policy == "interval":
            minutes = max(1, int(self.repeat_interval_sec or 300) // 60)
            return f"持续提醒，每{minutes}分钟一次"
        return REPEAT_POLICY_LABELS.get(self.repeat_policy, self.repeat_policy)

    @property
    def exchange_label(self) -> str:
        return EXCHANGE_LABELS.get(self.exchange, self.exchange or "Binance")

    @property
    def market_type_label(self) -> str:
        return MARKET_TYPE_LABELS.get(self.market_type, self.market_type or "USDT 合约")

    @property
    def venue_label(self) -> str:
        return f"{self.exchange_label} {self.market_type_label}"

    @property
    def price_key(self) -> str:
        return price_key(self.exchange, self.market_type, self.pair or self.symbol)


@dataclass(frozen=True)
class AlertMarketQuote:
    exchange: str
    market_type: str
    symbol: str
    pair: str
    price: float

    @property
    def exchange_label(self) -> str:
        return EXCHANGE_LABELS.get(self.exchange, self.exchange)

    @property
    def market_type_label(self) -> str:
        return MARKET_TYPE_LABELS.get(self.market_type, self.market_type)

    @property
    def venue_label(self) -> str:
        return f"{self.exchange_label} {self.market_type_label}"

    @property
    def key(self) -> str:
        return price_key(self.exchange, self.market_type, self.pair)


def clear_alert_market_cache() -> None:
    with _ALERT_MARKET_CACHE_LOCK:
        _ALERT_MARKET_CACHE.clear()
    with _ALERT_QUOTE_CACHE_LOCK:
        _ALERT_QUOTE_CACHE.clear()


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


def base_symbol(value: str) -> str:
    symbol = normalize_symbol(value)
    return symbol[:-4] if symbol.endswith("USDT") else symbol


def contract_pair_multiplier(pair: str, symbol: str) -> int:
    """Return the contract unit multiplier for prefixed futures pairs."""

    try:
        normalized_symbol = normalize_symbol(symbol)
    except ValueError:
        return 1
    base = base_symbol(normalized_symbol)
    clean_pair = re.sub(r"[^A-Z0-9]", "", str(pair or "").upper())
    for prefix in sorted(FUTURES_CONTRACT_PREFIXES, key=len, reverse=True):
        if clean_pair == f"{prefix}{base}USDT":
            return int(prefix)
    return 1


def normalize_exchange(value: str | None) -> str:
    exchange = (value or "binance").strip().lower()
    if exchange == "bianca":
        exchange = "binance"
    if exchange not in VALID_EXCHANGES:
        raise ValueError("交易所不支持")
    return exchange


def normalize_market_type(value: str | None) -> str:
    market_type = (value or "futures").strip().lower()
    if market_type in {"future", "swap", "perp", "contract", "linear", "合约", "永续"}:
        market_type = "futures"
    if market_type in {"现货"}:
        market_type = "spot"
    if market_type not in VALID_MARKET_TYPES:
        raise ValueError("市场类型只能是 spot/futures")
    return market_type


def normalize_alert_type(value: str | None) -> str:
    alert_type = (value or "target_price").strip().lower()
    aliases = {
        "target": "target_price",
        "price": "target_price",
        "price_alert": "target_price",
        "change": "price_change",
        "volatility": "price_change",
        "price_volatility": "price_change",
        "oi": "oi_change",
        "open_interest": "oi_change",
        "funding": "funding_change",
        "funding_rate": "funding_change",
    }
    alert_type = aliases.get(alert_type, alert_type)
    if alert_type not in VALID_ALERT_TYPES:
        raise ValueError("提醒类型不正确")
    return alert_type


def normalize_timeframe_sec(value: int | str | None) -> int:
    text = str(value or "").strip().lower()
    aliases = {
        "5": 300,
        "5m": 300,
        "5min": 300,
        "5分钟": 300,
        "15": 900,
        "15m": 900,
        "15min": 900,
        "15分钟": 900,
        "60": 3600,
        "60m": 3600,
        "1h": 3600,
        "1小时": 3600,
        "小时": 3600,
    }
    if text in aliases:
        return aliases[text]
    try:
        seconds = int(float(text))
    except ValueError:
        seconds = 0
    if seconds in {0, 300, 900, 3600}:
        return seconds
    if seconds in {5, 15, 60}:
        return seconds * 60
    raise ValueError("时间窗口只支持 5分钟、15分钟、60分钟")


def normalize_repeat_policy(value: str | None) -> str:
    policy = (value or "once").strip().lower()
    aliases = {
        "one": "once",
        "single": "once",
        "一次": "once",
        "提醒一次": "once",
        "repeat": "repeat",
        "rearm": "repeat",
        "重复": "repeat",
        "重复提醒": "repeat",
        "interval": "interval",
        "continuous": "interval",
        "持续": "interval",
        "持续提醒": "interval",
        "5min": "interval",
    }
    policy = aliases.get(policy, policy)
    if policy not in VALID_REPEAT_POLICIES:
        raise ValueError("提醒方式不正确")
    return policy


def normalize_repeat_interval_sec(value: int | str | None, repeat_policy: str = "once") -> int:
    if repeat_policy != "interval":
        return 0
    text = str(value or "").strip().lower()
    aliases = {
        "": 300,
        "5": 300,
        "5m": 300,
        "5min": 300,
        "5分钟": 300,
        "15": 900,
        "15m": 900,
        "15分钟": 900,
        "30": 1800,
        "30m": 1800,
        "30分钟": 1800,
        "60": 3600,
        "60m": 3600,
        "1h": 3600,
        "1小时": 3600,
    }
    if text in aliases:
        return aliases[text]
    try:
        seconds = int(float(text))
    except ValueError:
        seconds = 300
    if seconds in {5, 15, 30, 60}:
        seconds *= 60
    return max(60, min(24 * 3600, seconds))


def default_pair_for_symbol(symbol: str, exchange: str = "binance", market_type: str = "futures") -> str:
    normalized_symbol = normalize_symbol(symbol)
    base = base_symbol(normalized_symbol)
    normalized_exchange = normalize_exchange(exchange)
    normalized_market = normalize_market_type(market_type)
    if normalized_exchange == "okx":
        return f"{base}-USDT-SWAP" if normalized_market == "futures" else f"{base}-USDT"
    if normalized_exchange == "gate":
        return f"{base}_USDT"
    return normalized_symbol


def normalize_pair(symbol: str, pair: str | None, exchange: str = "binance", market_type: str = "futures") -> str:
    clean = str(pair or "").strip().upper()
    if not clean:
        return default_pair_for_symbol(symbol, exchange, market_type)
    normalized_exchange = normalize_exchange(exchange)
    normalized_market = normalize_market_type(market_type)
    if normalized_exchange == "okx":
        clean = clean.replace("_", "-").replace("/", "-")
        if normalized_market == "futures" and not clean.endswith("-SWAP"):
            clean = f"{clean}-SWAP"
        return clean
    if normalized_exchange == "gate":
        return clean.replace("-", "_").replace("/", "_")
    return re.sub(r"[^A-Z0-9]", "", clean)


def alert_market_pair_candidates(symbol: str, exchange: str = "binance", market_type: str = "futures", pair: str | None = None) -> list[str]:
    normalized_symbol = normalize_symbol(symbol)
    normalized_exchange = normalize_exchange(exchange)
    normalized_market = normalize_market_type(market_type)
    primary = normalize_pair(normalized_symbol, pair, normalized_exchange, normalized_market)
    candidates = [primary]
    if pair:
        return candidates
    if normalized_market == "futures" and normalized_exchange in {"binance", "bybit"}:
        base = base_symbol(normalized_symbol)
        for prefix in FUTURES_CONTRACT_PREFIXES:
            prefixed = f"{prefix}{base}USDT"
            if prefixed != primary and prefixed not in candidates:
                candidates.append(prefixed)
    return candidates


def price_key(exchange: str, market_type: str, pair: str) -> str:
    return f"{normalize_exchange(exchange)}:{normalize_market_type(market_type)}:{str(pair or '').upper()}"


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


def normalize_direction(value: str, alert_type: str = "target_price") -> str:
    direction = (value or "").strip().lower()
    if normalize_alert_type(alert_type) != "target_price":
        if direction in {"up", "above", "gte", ">=", ">", "上涨", "急涨", "增加", "变大", "高于"}:
            return "up"
        if direction in {"down", "below", "lte", "<=", "<", "下跌", "急跌", "减少", "变小", "低于"}:
            return "down"
        if direction in {"", "both", "any", "双向", "涨跌都提醒", "全部"}:
            return "both"
        raise ValueError("方向只能是上涨、下跌或双向")
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
    if value < 0.000001:
        return f"${value:.12f}".rstrip("0").rstrip(".")
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
                    exchange TEXT NOT NULL DEFAULT 'binance',
                    market_type TEXT NOT NULL DEFAULT 'futures',
                    pair TEXT NOT NULL DEFAULT '',
                    direction TEXT NOT NULL,
                    target_price REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    source TEXT NOT NULL DEFAULT 'telegram',
                    note TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    triggered_at INTEGER,
                    last_price REAL,
                    alert_type TEXT NOT NULL DEFAULT 'target_price',
                    timeframe_sec INTEGER NOT NULL DEFAULT 0,
                    threshold_pct REAL NOT NULL DEFAULT 0,
                    repeat_policy TEXT NOT NULL DEFAULT 'once',
                    repeat_interval_sec INTEGER NOT NULL DEFAULT 0,
                    last_triggered_at INTEGER,
                    trigger_count INTEGER NOT NULL DEFAULT 0,
                    last_value REAL,
                    last_baseline REAL,
                    metadata TEXT NOT NULL DEFAULT ''
                )
                """
            )
            self._ensure_price_alert_columns(conn)
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
            conn.execute("CREATE INDEX IF NOT EXISTS idx_price_alerts_price_key ON price_alerts(exchange, market_type, pair)")

    def _ensure_price_alert_columns(self, conn: sqlite3.Connection) -> None:
        rows = conn.execute("PRAGMA table_info(price_alerts)").fetchall()
        columns = {str(row["name"]) for row in rows}
        if "exchange" not in columns:
            conn.execute("ALTER TABLE price_alerts ADD COLUMN exchange TEXT NOT NULL DEFAULT 'binance'")
        if "market_type" not in columns:
            conn.execute("ALTER TABLE price_alerts ADD COLUMN market_type TEXT NOT NULL DEFAULT 'futures'")
        if "pair" not in columns:
            conn.execute("ALTER TABLE price_alerts ADD COLUMN pair TEXT NOT NULL DEFAULT ''")
        if "alert_type" not in columns:
            conn.execute("ALTER TABLE price_alerts ADD COLUMN alert_type TEXT NOT NULL DEFAULT 'target_price'")
        if "timeframe_sec" not in columns:
            conn.execute("ALTER TABLE price_alerts ADD COLUMN timeframe_sec INTEGER NOT NULL DEFAULT 0")
        if "threshold_pct" not in columns:
            conn.execute("ALTER TABLE price_alerts ADD COLUMN threshold_pct REAL NOT NULL DEFAULT 0")
        if "repeat_policy" not in columns:
            conn.execute("ALTER TABLE price_alerts ADD COLUMN repeat_policy TEXT NOT NULL DEFAULT 'once'")
        if "repeat_interval_sec" not in columns:
            conn.execute("ALTER TABLE price_alerts ADD COLUMN repeat_interval_sec INTEGER NOT NULL DEFAULT 0")
        if "last_triggered_at" not in columns:
            conn.execute("ALTER TABLE price_alerts ADD COLUMN last_triggered_at INTEGER")
        if "trigger_count" not in columns:
            conn.execute("ALTER TABLE price_alerts ADD COLUMN trigger_count INTEGER NOT NULL DEFAULT 0")
        if "last_value" not in columns:
            conn.execute("ALTER TABLE price_alerts ADD COLUMN last_value REAL")
        if "last_baseline" not in columns:
            conn.execute("ALTER TABLE price_alerts ADD COLUMN last_baseline REAL")
        if "metadata" not in columns:
            conn.execute("ALTER TABLE price_alerts ADD COLUMN metadata TEXT NOT NULL DEFAULT ''")
        conn.execute("UPDATE price_alerts SET exchange = 'binance' WHERE exchange IS NULL OR exchange = ''")
        conn.execute("UPDATE price_alerts SET market_type = 'futures' WHERE market_type IS NULL OR market_type = ''")
        conn.execute("UPDATE price_alerts SET pair = symbol WHERE pair IS NULL OR pair = ''")
        conn.execute("UPDATE price_alerts SET alert_type = 'target_price' WHERE alert_type IS NULL OR alert_type = ''")
        conn.execute("UPDATE price_alerts SET repeat_policy = 'once' WHERE repeat_policy IS NULL OR repeat_policy = ''")
        conn.execute("UPDATE price_alerts SET timeframe_sec = 0 WHERE timeframe_sec IS NULL")
        conn.execute("UPDATE price_alerts SET threshold_pct = 0 WHERE threshold_pct IS NULL")
        conn.execute("UPDATE price_alerts SET repeat_interval_sec = 0 WHERE repeat_interval_sec IS NULL")
        conn.execute("UPDATE price_alerts SET trigger_count = 0 WHERE trigger_count IS NULL")
        conn.execute("UPDATE price_alerts SET metadata = '' WHERE metadata IS NULL")

    def create_alert(
        self,
        *,
        user_id: str,
        chat_id: str,
        symbol: str,
        direction: str,
        target_price: float,
        exchange: str = "binance",
        market_type: str = "futures",
        pair: str | None = None,
        username: str = "",
        source: str = "telegram",
        note: str = "",
        alert_type: str = "target_price",
        timeframe_sec: int = 0,
        threshold_pct: float = 0.0,
        repeat_policy: str = "once",
        repeat_interval_sec: int = 0,
        metadata: str = "",
    ) -> PriceAlert:
        now = int(time.time())
        normalized_symbol = normalize_symbol(symbol)
        normalized_exchange = normalize_exchange(exchange)
        normalized_market_type = normalize_market_type(market_type)
        normalized_pair = normalize_pair(normalized_symbol, pair, normalized_exchange, normalized_market_type)
        normalized_alert_type = normalize_alert_type(alert_type)
        normalized_direction = normalize_direction(direction, normalized_alert_type)
        price = parse_price(target_price) if normalized_alert_type == "target_price" else float(target_price or 0)
        normalized_timeframe = normalize_timeframe_sec(timeframe_sec)
        normalized_threshold = max(0.0, float(threshold_pct or 0))
        normalized_repeat_policy = normalize_repeat_policy(repeat_policy)
        normalized_repeat_interval = normalize_repeat_interval_sec(repeat_interval_sec, normalized_repeat_policy)
        with self.connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO price_alerts
                (
                    user_id, chat_id, username, symbol, exchange, market_type, pair,
                    direction, target_price, status, source, note, created_at, updated_at,
                    alert_type, timeframe_sec, threshold_pct, repeat_policy, repeat_interval_sec, metadata
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(user_id),
                    str(chat_id),
                    username or "",
                    normalized_symbol,
                    normalized_exchange,
                    normalized_market_type,
                    normalized_pair,
                    normalized_direction,
                    price,
                    source or "telegram",
                    note or "",
                    now,
                    now,
                    normalized_alert_type,
                    normalized_timeframe,
                    normalized_threshold,
                    normalized_repeat_policy,
                    normalized_repeat_interval,
                    metadata or "",
                ),
            )
            alert_id = int(cursor.lastrowid)
            self._record_event(conn, alert_id, "created", None, f"created {normalized_alert_type}:{normalized_exchange}:{normalized_market_type}:{normalized_pair}")
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

    def active_price_keys(self) -> list[tuple[str, str, str, str]]:
        with self.connection() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT symbol, exchange, market_type, pair
                FROM price_alerts
                WHERE status = 'active'
                ORDER BY exchange, market_type, pair
                """
            ).fetchall()
        return [
            (
                str(row["symbol"]),
                normalize_exchange(str(row["exchange"] or "binance")),
                normalize_market_type(str(row["market_type"] or "futures")),
                normalize_pair(str(row["symbol"]), str(row["pair"] or ""), str(row["exchange"] or "binance"), str(row["market_type"] or "futures")),
            )
            for row in rows
        ]

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

    def update_last_price(
        self,
        symbol: str,
        price: float,
        *,
        exchange: str = "binance",
        market_type: str = "futures",
        pair: str | None = None,
    ) -> None:
        normalized_symbol = normalize_symbol(symbol)
        normalized_exchange = normalize_exchange(exchange)
        normalized_market_type = normalize_market_type(market_type)
        normalized_pair = normalize_pair(normalized_symbol, pair, normalized_exchange, normalized_market_type)
        now = int(time.time())
        with self.connection() as conn:
            conn.execute(
                """
                UPDATE price_alerts
                SET last_price = ?, updated_at = ?
                WHERE symbol = ? AND exchange = ? AND market_type = ? AND pair = ? AND status = 'active'
                """,
                (float(price), now, normalized_symbol, normalized_exchange, normalized_market_type, normalized_pair),
            )

    def update_monitor_state(
        self,
        alert_id: int,
        *,
        last_value: float | None = None,
        last_baseline: float | None = None,
        last_price: float | None = None,
        metadata: str | None = None,
    ) -> None:
        now = int(time.time())
        assignments = ["updated_at = ?"]
        args: list[Any] = [now]
        if last_value is not None:
            assignments.append("last_value = ?")
            args.append(float(last_value))
        if last_baseline is not None:
            assignments.append("last_baseline = ?")
            args.append(float(last_baseline))
        if last_price is not None:
            assignments.append("last_price = ?")
            args.append(float(last_price))
        if metadata is not None:
            assignments.append("metadata = ?")
            args.append(str(metadata))
        args.append(int(alert_id))
        with self.connection() as conn:
            conn.execute(
                f"UPDATE price_alerts SET {', '.join(assignments)} WHERE id = ?",
                args,
            )

    def mark_triggered(self, alert_or_id: PriceAlert | int, price: float, message: str = "triggered") -> bool:
        alert = alert_or_id if isinstance(alert_or_id, PriceAlert) else self.get_alert(int(alert_or_id))
        if alert is None:
            return False
        now = int(time.time())
        next_status = "triggered" if alert.repeat_policy == "once" else "active"
        with self.connection() as conn:
            cursor = conn.execute(
                """
                UPDATE price_alerts
                SET status = ?, triggered_at = COALESCE(triggered_at, ?),
                    last_triggered_at = ?, trigger_count = trigger_count + 1,
                    last_price = ?, updated_at = ?
                WHERE id = ? AND status = 'active'
                """,
                (next_status, now, now, float(price), now, int(alert.id)),
            )
            changed = cursor.rowcount > 0
            if changed:
                self._record_event(conn, int(alert.id), "triggered", float(price), message or "triggered")
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
        exchange=normalize_exchange(str(row["exchange"] or "binance")),
        market_type=normalize_market_type(str(row["market_type"] or "futures")),
        pair=normalize_pair(
            str(row["symbol"]),
            str(row["pair"] or row["symbol"]),
            str(row["exchange"] or "binance"),
            str(row["market_type"] or "futures"),
        ),
        direction=str(row["direction"]),
        target_price=float(row["target_price"]),
        status=str(row["status"]),
        source=str(row["source"] or ""),
        note=str(row["note"] or ""),
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
        triggered_at=int(row["triggered_at"]) if row["triggered_at"] is not None else None,
        last_price=float(row["last_price"]) if row["last_price"] is not None else None,
        alert_type=normalize_alert_type(str(row["alert_type"] or "target_price")),
        timeframe_sec=normalize_timeframe_sec(row["timeframe_sec"] or 0),
        threshold_pct=float(row["threshold_pct"] or 0),
        repeat_policy=normalize_repeat_policy(str(row["repeat_policy"] or "once")),
        repeat_interval_sec=normalize_repeat_interval_sec(row["repeat_interval_sec"] or 0, str(row["repeat_policy"] or "once")),
        last_triggered_at=int(row["last_triggered_at"]) if row["last_triggered_at"] is not None else None,
        trigger_count=int(row["trigger_count"] or 0),
        last_value=float(row["last_value"]) if row["last_value"] is not None else None,
        last_baseline=float(row["last_baseline"]) if row["last_baseline"] is not None else None,
        metadata=str(row["metadata"] or ""),
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


def _float_from(data: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = data.get(key)
        try:
            if value is not None and str(value) != "":
                return float(value)
        except (TypeError, ValueError):
            continue
    return None


def fetch_alert_market_quote(
    settings: Settings,
    symbol: str,
    exchange: str = "binance",
    market_type: str = "futures",
    pair: str | None = None,
    cache_ttl_sec: int = ALERT_QUOTE_CACHE_TTL_SEC,
) -> AlertMarketQuote | None:
    normalized_symbol = normalize_symbol(symbol)
    normalized_exchange = normalize_exchange(exchange)
    normalized_market = normalize_market_type(market_type)
    cache_key = _alert_quote_cache_key(settings, normalized_symbol, normalized_exchange, normalized_market, pair)
    now = time.time()
    if cache_ttl_sec > 0:
        with _ALERT_QUOTE_CACHE_LOCK:
            cached = _ALERT_QUOTE_CACHE.get(cache_key)
            if cached and now - cached[0] <= cache_ttl_sec:
                return cached[1]
    pair_candidates = alert_market_pair_candidates(normalized_symbol, normalized_exchange, normalized_market, pair)
    timeout = max(3, int(settings.http_timeout_sec))
    matched_pair = ""
    price: float | None = None
    for candidate_pair in pair_candidates:
        try:
            candidate_price = _fetch_alert_market_price(settings, normalized_exchange, normalized_market, candidate_pair, timeout)
        except requests.RequestException:
            continue
        except (TypeError, ValueError, KeyError):
            continue
        if candidate_price is not None and candidate_price > 0:
            matched_pair = candidate_pair
            price = candidate_price
            break
    if not matched_pair or price is None or price <= 0:
        if cache_ttl_sec > 0:
            with _ALERT_QUOTE_CACHE_LOCK:
                _ALERT_QUOTE_CACHE[cache_key] = (now, None)
        return None
    quote = AlertMarketQuote(
        exchange=normalized_exchange,
        market_type=normalized_market,
        symbol=normalized_symbol,
        pair=matched_pair,
        price=price,
    )
    if cache_ttl_sec > 0:
        with _ALERT_QUOTE_CACHE_LOCK:
            _ALERT_QUOTE_CACHE[cache_key] = (now, quote)
    return quote


def _fetch_alert_market_price(
    settings: Settings,
    exchange: str,
    market_type: str,
    pair: str,
    timeout: int,
) -> float | None:
    headers = HTTP_HEADERS
    if exchange == "binance":
        base = settings.binance_spot_base_url.rstrip("/") if market_type == "spot" else settings.binance_fapi_base_url.rstrip("/")
        path = "/api/v3/ticker/price" if market_type == "spot" else "/fapi/v1/ticker/price"
        response = requests.get(f"{base}{path}", params={"symbol": pair}, headers=headers, timeout=timeout)
        response.raise_for_status()
        data = response.json()
        return _float_from(data if isinstance(data, dict) else {}, ("price", "lastPrice"))

    if exchange == "bybit":
        category = "spot" if market_type == "spot" else "linear"
        response = requests.get(
            "https://api.bybit.com/v5/market/tickers",
            params={"category": category, "symbol": pair},
            headers=headers,
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        items = (((data or {}).get("result") or {}).get("list") or []) if isinstance(data, dict) else []
        item = items[0] if isinstance(items, list) and items else {}
        return _float_from(item if isinstance(item, dict) else {}, ("lastPrice",))

    if exchange == "okx":
        response = requests.get(
            "https://www.okx.com/api/v5/market/ticker",
            params={"instId": pair},
            headers=headers,
            timeout=timeout,
        )
        response.raise_for_status()
        data = response.json()
        items = data.get("data") if isinstance(data, dict) else []
        item = items[0] if isinstance(items, list) and items else {}
        return _float_from(item if isinstance(item, dict) else {}, ("last",))

    if exchange == "bitget":
        if market_type == "spot":
            response = requests.get(
                "https://api.bitget.com/api/v2/spot/market/tickers",
                params={"symbol": pair},
                headers=headers,
                timeout=timeout,
            )
        else:
            response = requests.get(
                "https://api.bitget.com/api/v2/mix/market/ticker",
                params={"symbol": pair, "productType": "USDT-FUTURES"},
                headers=headers,
                timeout=timeout,
            )
        response.raise_for_status()
        data = response.json()
        payload = data.get("data") if isinstance(data, dict) else {}
        item = payload[0] if isinstance(payload, list) and payload else payload
        return _float_from(item if isinstance(item, dict) else {}, ("lastPr", "last", "close", "lastPrice"))

    if exchange == "gate":
        gate_headers = {"Accept": "application/json"}
        if market_type == "spot":
            response = requests.get(
                "https://api.gateio.ws/api/v4/spot/tickers",
                params={"currency_pair": pair},
                headers=gate_headers,
                timeout=timeout,
            )
        else:
            response = requests.get(
                "https://fx-api.gateio.ws/api/v4/futures/usdt/tickers",
                params={"contract": pair},
                headers=gate_headers,
                timeout=timeout,
            )
        response.raise_for_status()
        data = response.json()
        item = data[0] if isinstance(data, list) and data else {}
        return _float_from(item if isinstance(item, dict) else {}, ("last",))

    return None


def _alert_market_cache_key(settings: Settings, symbol: str) -> tuple[Any, ...]:
    return (
        normalize_symbol(symbol),
        settings.binance_spot_base_url.rstrip("/"),
        settings.binance_fapi_base_url.rstrip("/"),
        int(settings.http_timeout_sec),
    )


def _alert_quote_cache_key(
    settings: Settings,
    symbol: str,
    exchange: str,
    market_type: str,
    pair: str | None,
) -> tuple[Any, ...]:
    normalized_symbol = normalize_symbol(symbol)
    normalized_exchange = normalize_exchange(exchange)
    normalized_market = normalize_market_type(market_type)
    normalized_pair = normalize_pair(normalized_symbol, pair, normalized_exchange, normalized_market) if pair else ""
    return (
        normalized_symbol,
        normalized_exchange,
        normalized_market,
        normalized_pair,
        settings.binance_spot_base_url.rstrip("/"),
        settings.binance_fapi_base_url.rstrip("/"),
        int(settings.http_timeout_sec),
    )


def _sort_alert_quotes(quotes: list[AlertMarketQuote]) -> list[AlertMarketQuote]:
    exchange_order = {exchange: index for index, exchange in enumerate(ALERT_MARKET_EXCHANGES)}
    market_order = {market_type: index for index, market_type in enumerate(ALERT_MARKET_TYPES)}
    return sorted(
        quotes,
        key=lambda quote: (
            market_order.get(quote.market_type, 99),
            exchange_order.get(quote.exchange, 99),
            quote.pair,
        ),
    )


def discover_alert_markets(settings: Settings, symbol: str, cache_ttl_sec: int = ALERT_MARKET_CACHE_TTL_SEC) -> list[AlertMarketQuote]:
    normalized_symbol = normalize_symbol(symbol)
    cache_key = _alert_market_cache_key(settings, normalized_symbol)
    now = time.time()
    if cache_ttl_sec > 0:
        with _ALERT_MARKET_CACHE_LOCK:
            cached = _ALERT_MARKET_CACHE.get(cache_key)
            if cached and now - cached[0] <= cache_ttl_sec:
                return list(cached[1])

    quotes: list[AlertMarketQuote] = []
    seen: set[str] = set()
    jobs = [(exchange, market_type) for market_type in ALERT_MARKET_TYPES for exchange in ALERT_MARKET_EXCHANGES]
    with ThreadPoolExecutor(max_workers=min(ALERT_MARKET_WORKERS, len(jobs))) as executor:
        future_map = {
            executor.submit(fetch_alert_market_quote, settings, normalized_symbol, exchange, market_type): (exchange, market_type)
            for exchange, market_type in jobs
        }
        for future in as_completed(future_map):
            try:
                quote = future.result()
            except Exception:
                quote = None
            if quote and quote.key not in seen:
                quotes.append(quote)
                seen.add(quote.key)
    quotes = _sort_alert_quotes(quotes)
    if cache_ttl_sec > 0:
        with _ALERT_MARKET_CACHE_LOCK:
            _ALERT_MARKET_CACHE[cache_key] = (now, list(quotes))
    return quotes


def fetch_price_alert_prices(settings: Settings, alerts: list[PriceAlert]) -> dict[str, float]:
    prices: dict[str, float] = {}
    unique_alerts: dict[str, PriceAlert] = {}
    for alert in alerts:
        unique_alerts.setdefault(alert.price_key, alert)
    if not unique_alerts:
        return prices
    with ThreadPoolExecutor(max_workers=min(ALERT_MARKET_WORKERS, len(unique_alerts))) as executor:
        future_map = {
            executor.submit(fetch_alert_market_quote, settings, alert.symbol, alert.exchange, alert.market_type, alert.pair): price_key_value
            for price_key_value, alert in unique_alerts.items()
        }
        for future in as_completed(future_map):
            quote = None
            try:
                quote = future.result()
            except Exception:
                quote = None
            if quote:
                prices[future_map[future]] = quote.price
    return prices


def _condition_met(alert: PriceAlert, value: float, change_pct: float | None = None) -> bool:
    if alert.alert_type == "target_price":
        if alert.direction == "above":
            return value >= alert.target_price
        if alert.direction == "below":
            return value <= alert.target_price
        return False
    pct = float(change_pct or 0)
    threshold = float(alert.threshold_pct or 0)
    if threshold <= 0:
        return False
    if alert.direction == "up":
        return pct >= threshold
    if alert.direction == "down":
        return pct <= -threshold
    return abs(pct) >= threshold


def _was_condition_met(alert: PriceAlert) -> bool:
    if alert.last_price is None:
        return False
    return _condition_met(alert, float(alert.last_price))


def alert_can_send(alert: PriceAlert, now: int | None = None) -> bool:
    if alert.repeat_policy == "interval":
        interval = max(60, int(alert.repeat_interval_sec or 300))
        last = int(alert.last_triggered_at or 0)
        return not last or int(now or time.time()) - last >= interval
    if alert.repeat_policy == "repeat":
        return True
    return alert.trigger_count <= 0


def triggered_alerts(alerts: list[PriceAlert], prices: dict[str, float]) -> list[tuple[PriceAlert, float]]:
    triggered: list[tuple[PriceAlert, float]] = []
    now = int(time.time())
    for alert in alerts:
        if alert.alert_type != "target_price":
            continue
        price = prices.get(alert.price_key)
        if price is None:
            price = prices.get(alert.symbol)
        if price is None:
            continue
        met = _condition_met(alert, price)
        crossed = met and not _was_condition_met(alert)
        if alert.repeat_policy == "interval":
            should_send = met and alert_can_send(alert, now)
        elif alert.repeat_policy == "repeat":
            should_send = crossed
        else:
            should_send = met and alert.trigger_count <= 0
        if should_send:
            triggered.append((alert, price))
    return triggered


def timeframe_to_interval(timeframe_sec: int) -> str:
    normalized = normalize_timeframe_sec(timeframe_sec)
    return {300: "5m", 900: "15m", 3600: "1h"}.get(normalized, "5m")


def _pct_change(current: float, baseline: float) -> float | None:
    if baseline <= 0:
        return None
    return (current - baseline) / baseline * 100


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or str(value) == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _kline_change_from_item(item: Any, open_index: int, close_index: int) -> tuple[float, float, float] | None:
    if not isinstance(item, (list, tuple)) or len(item) <= max(open_index, close_index):
        return None
    opened = _safe_float(item[open_index])
    closed = _safe_float(item[close_index])
    if opened is None or closed is None:
        return None
    change = _pct_change(closed, opened)
    if change is None:
        return None
    return opened, closed, change


def fetch_price_change_snapshot(settings: Settings, alert: PriceAlert) -> dict[str, float] | None:
    interval = timeframe_to_interval(alert.timeframe_sec)
    pair = alert.pair
    timeout = max(3, int(settings.http_timeout_sec))
    try:
        if alert.exchange == "binance":
            base = settings.binance_spot_base_url.rstrip("/") if alert.market_type == "spot" else settings.binance_fapi_base_url.rstrip("/")
            path = "/api/v3/klines" if alert.market_type == "spot" else "/fapi/v1/klines"
            data = requests.get(f"{base}{path}", params={"symbol": pair, "interval": interval, "limit": 1}, headers=HTTP_HEADERS, timeout=timeout).json()
            parsed = _kline_change_from_item(data[-1] if isinstance(data, list) and data else None, 1, 4)
        elif alert.exchange == "bybit":
            category = "spot" if alert.market_type == "spot" else "linear"
            bybit_interval = {"5m": "5", "15m": "15", "1h": "60"}[interval]
            data = requests.get("https://api.bybit.com/v5/market/kline", params={"category": category, "symbol": pair, "interval": bybit_interval, "limit": 1}, headers=HTTP_HEADERS, timeout=timeout).json()
            items = (((data or {}).get("result") or {}).get("list") or []) if isinstance(data, dict) else []
            parsed = _kline_change_from_item(items[0] if items else None, 1, 4)
        elif alert.exchange == "okx":
            okx_interval = {"5m": "5m", "15m": "15m", "1h": "1H"}[interval]
            data = requests.get("https://www.okx.com/api/v5/market/candles", params={"instId": pair, "bar": okx_interval, "limit": 1}, headers=HTTP_HEADERS, timeout=timeout).json()
            items = data.get("data", []) if isinstance(data, dict) else []
            parsed = _kline_change_from_item(items[0] if items else None, 1, 4)
        elif alert.exchange == "bitget":
            if alert.market_type == "spot":
                data = requests.get("https://api.bitget.com/api/v2/spot/market/candles", params={"symbol": pair, "granularity": {"5m": "5min", "15m": "15min", "1h": "1h"}[interval], "limit": 1}, headers=HTTP_HEADERS, timeout=timeout).json()
            else:
                data = requests.get("https://api.bitget.com/api/v2/mix/market/candles", params={"symbol": pair, "productType": "USDT-FUTURES", "granularity": interval, "limit": 1}, headers=HTTP_HEADERS, timeout=timeout).json()
            items = data.get("data", []) if isinstance(data, dict) else []
            parsed = _kline_change_from_item(items[0] if items else None, 1, 4)
        elif alert.exchange == "gate":
            if alert.market_type == "spot":
                data = requests.get("https://api.gateio.ws/api/v4/spot/candlesticks", params={"currency_pair": pair, "interval": interval, "limit": 1}, headers={"Accept": "application/json"}, timeout=timeout).json()
            else:
                data = requests.get("https://fx-api.gateio.ws/api/v4/futures/usdt/candlesticks", params={"contract": pair, "interval": interval, "limit": 1}, headers={"Accept": "application/json"}, timeout=timeout).json()
            item = data[0] if isinstance(data, list) and data else None
            if isinstance(item, dict):
                opened = _safe_float(item.get("o") or item.get("open"))
                closed = _safe_float(item.get("c") or item.get("close"))
                change = _pct_change(float(closed or 0), float(opened or 0)) if opened and closed else None
                parsed = (opened, closed, change) if opened is not None and closed is not None and change is not None else None
            else:
                parsed = _kline_change_from_item(item, 5, 2)
        else:
            parsed = None
    except Exception:
        return None
    if not parsed:
        return None
    baseline, current, change = parsed
    return {"baseline": baseline, "current": current, "change_pct": change}


def fetch_open_interest_value(settings: Settings, alert: PriceAlert) -> float | None:
    timeout = max(3, int(settings.http_timeout_sec))
    pair = alert.pair
    try:
        if alert.exchange == "binance":
            data = requests.get(f"{settings.binance_fapi_base_url.rstrip('/')}/fapi/v1/openInterest", params={"symbol": pair}, headers=HTTP_HEADERS, timeout=timeout).json()
            return _safe_float(data.get("openInterest")) if isinstance(data, dict) else None
        if alert.exchange == "bybit":
            data = requests.get("https://api.bybit.com/v5/market/open-interest", params={"category": "linear", "symbol": pair, "intervalTime": "5min", "limit": 1}, headers=HTTP_HEADERS, timeout=timeout).json()
            items = (((data or {}).get("result") or {}).get("list") or []) if isinstance(data, dict) else []
            item = items[0] if items else {}
            return _safe_float(item.get("openInterest")) if isinstance(item, dict) else None
        if alert.exchange == "okx":
            data = requests.get("https://www.okx.com/api/v5/public/open-interest", params={"instType": "SWAP", "instId": pair}, headers=HTTP_HEADERS, timeout=timeout).json()
            items = data.get("data", []) if isinstance(data, dict) else []
            item = items[0] if items else {}
            return _safe_float(item.get("oi")) if isinstance(item, dict) else None
        if alert.exchange == "bitget":
            data = requests.get("https://api.bitget.com/api/v2/mix/market/open-interest", params={"symbol": pair, "productType": "USDT-FUTURES"}, headers=HTTP_HEADERS, timeout=timeout).json()
            payload = data.get("data") if isinstance(data, dict) else {}
            item = payload[0] if isinstance(payload, list) and payload else payload
            return _safe_float(item.get("openInterest")) if isinstance(item, dict) else None
        if alert.exchange == "gate":
            data = requests.get(f"https://fx-api.gateio.ws/api/v4/futures/usdt/contracts/{pair}", headers={"Accept": "application/json"}, timeout=timeout).json()
            return _safe_float(data.get("open_interest")) if isinstance(data, dict) else None
    except Exception:
        return None
    return None


def alert_to_dict(alert: PriceAlert) -> dict[str, Any]:
    return {
        "id": alert.id,
        "user_id": alert.user_id,
        "chat_id": alert.chat_id,
        "username": alert.username,
        "symbol": alert.symbol,
        "exchange": alert.exchange,
        "exchange_label": alert.exchange_label,
        "market_type": alert.market_type,
        "market_type_label": alert.market_type_label,
        "pair": alert.pair,
        "venue_label": alert.venue_label,
        "price_key": alert.price_key,
        "direction": alert.direction,
        "direction_label": alert.direction_label,
        "target_price": alert.target_price,
        "target_price_text": format_price(alert.target_price),
        "alert_type": alert.alert_type,
        "alert_type_label": alert.alert_type_label,
        "timeframe_sec": alert.timeframe_sec,
        "timeframe_label": alert.timeframe_label,
        "threshold_pct": alert.threshold_pct,
        "repeat_policy": alert.repeat_policy,
        "repeat_policy_label": alert.repeat_policy_label,
        "repeat_interval_sec": alert.repeat_interval_sec,
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
        "last_triggered_at": alert.last_triggered_at,
        "last_triggered_at_text": format_ts(alert.last_triggered_at),
        "trigger_count": alert.trigger_count,
        "last_value": alert.last_value,
        "last_baseline": alert.last_baseline,
        "metadata": alert.metadata,
    }
