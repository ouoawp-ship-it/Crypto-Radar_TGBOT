from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

from .config import OnchainSettings
from .constants import (
    OAR_AUTOMATION_SCHEMA_VERSION,
    OAR_WATCH_MAX_ACTIVE_TOKENS_HARD,
)
from .labels import LabelValidationError, normalize_evm_address


MARKET_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,24}USDT$")
TOKEN_KEY_RE = re.compile(r"^([1-9]\d*):(0x[0-9a-f]{40})$")
CHAIN_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
REGISTRY_STATUSES = {"pending", "verified", "disabled", "rejected"}
WATCH_STATUSES = {"active", "paused", "expired"}


class AutomationStoreError(RuntimeError):
    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code


def canonical_market_symbol(value: str) -> str:
    symbol = str(value or "").strip().upper()
    if not MARKET_SYMBOL_RE.fullmatch(symbol):
        raise AutomationStoreError(
            "invalid_symbol",
            "market symbol must be an uppercase USDT trading pair",
        )
    return symbol


def canonical_token_key(chain_id: int, contract: str) -> str:
    try:
        parsed_chain_id = int(chain_id)
    except (TypeError, ValueError) as exc:
        raise AutomationStoreError(
            "invalid_chain_id", "chain id must be a positive integer"
        ) from exc
    if isinstance(chain_id, bool) or parsed_chain_id <= 0:
        raise AutomationStoreError(
            "invalid_chain_id", "chain id must be a positive integer"
        )
    try:
        address = normalize_evm_address(contract)
    except LabelValidationError as exc:
        raise AutomationStoreError(
            "invalid_contract", "contract must be a 20-byte EVM address"
        ) from exc
    return f"{parsed_chain_id}:{address}"


def parse_token_key(token_key: str) -> tuple[int, str]:
    match = TOKEN_KEY_RE.fullmatch(str(token_key or "").strip().lower())
    if match is None:
        raise AutomationStoreError(
            "invalid_token_key",
            "token key must be <chain_id>:<lowercase EVM contract>",
        )
    return int(match.group(1)), match.group(2)


def stable_payload_hash(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_list(value: object) -> str:
    items = value if isinstance(value, list) else []
    return json.dumps(
        items,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _json_dict(value: object) -> str:
    item = value if isinstance(value, dict) else {}
    return json.dumps(
        item,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class AutomationStore:
    def __init__(
        self,
        path: Path,
        *,
        data_dir: Path,
        clock: Any = time.time,
    ):
        self.path = Path(path)
        self.data_dir = Path(data_dir)
        self.clock = clock
        self._initialized = False
        resolved = self.path.resolve()
        if not resolved.is_relative_to(self.data_dir.resolve()):
            raise AutomationStoreError(
                "unsafe_automation_path",
                "automation database must stay inside ONCHAIN_DATA_DIR",
            )

    @classmethod
    def from_settings(cls, settings: OnchainSettings) -> "AutomationStore":
        return cls(
            settings.oar_automation_db_path,
            data_dir=settings.data_dir,
        )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            yield conn
        finally:
            conn.close()

    @contextmanager
    def connect_existing(self) -> Iterator[sqlite3.Connection | None]:
        if not self.path.exists():
            yield None
            return
        uri = f"file:{quote(self.path.resolve().as_posix(), safe='/:')}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA busy_timeout=2000")
            self._require_schema(conn)
            yield conn
        finally:
            conn.close()

    def migrate(self) -> None:
        if self._initialized:
            return
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS automation_meta (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                    """
                )
                current = conn.execute(
                    "SELECT value FROM automation_meta "
                    "WHERE key='schema_version'"
                ).fetchone()
                current_version = (
                    int(current["value"]) if current is not None else 0
                )
                if current_version not in {
                    0,
                    1,
                    2,
                    OAR_AUTOMATION_SCHEMA_VERSION,
                }:
                    raise AutomationStoreError(
                        "automation_schema_incompatible",
                        "automation database schema version is incompatible",
                    )
                if current_version == 1:
                    self._migrate_v1_to_v2(conn)
                    current_version = 2
                if current_version == 2:
                    self._create_schema(conn)
                    self._migrate_v2_to_v3(conn)
                if current_version in {0, OAR_AUTOMATION_SCHEMA_VERSION}:
                    self._create_schema(conn)
                conn.execute(
                    "INSERT OR REPLACE INTO automation_meta(key, value) "
                    "VALUES('schema_version', ?)",
                    (str(OAR_AUTOMATION_SCHEMA_VERSION),),
                )
                conn.commit()
                self._initialized = True
            except Exception:
                conn.rollback()
                raise

    @staticmethod
    def _migrate_v1_to_v2(conn: sqlite3.Connection) -> None:
        conn.execute(
            "ALTER TABLE unresolved_signals "
            "ADD COLUMN status TEXT NOT NULL DEFAULT 'open'"
        )
        conn.execute(
            "ALTER TABLE unresolved_signals "
            "ADD COLUMN resolved_at INTEGER"
        )
        conn.execute(
            "ALTER TABLE unresolved_signals "
            "ADD COLUMN resolved_token_key TEXT"
        )
        conn.execute(
            "ALTER TABLE unresolved_signals "
            "ADD COLUMN resolution_note TEXT NOT NULL DEFAULT ''"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_unresolved_open_symbol "
            "ON unresolved_signals(status, source_symbol, reason)"
        )

    @staticmethod
    def _migrate_v2_to_v3(conn: sqlite3.Connection) -> None:
        additions = (
            "query_window TEXT NOT NULL DEFAULT ''",
            "total_token_amount TEXT NOT NULL DEFAULT ''",
            "unique_senders INTEGER",
            "unique_receivers INTEGER",
            "baseline_status TEXT NOT NULL DEFAULT ''",
            "baseline_anomaly INTEGER NOT NULL DEFAULT 0",
            "baseline_json TEXT NOT NULL DEFAULT '{}'",
        )
        existing = {
            str(row["name"])
            for row in conn.execute(
                "PRAGMA table_info(watch_scan_runs)"
            ).fetchall()
        }
        for definition in additions:
            name = definition.split()[0]
            if name not in existing:
                conn.execute(
                    f"ALTER TABLE watch_scan_runs ADD COLUMN {definition}"
                )

    @staticmethod
    def _create_schema(conn: sqlite3.Connection) -> None:
        schema = """
            CREATE TABLE IF NOT EXISTS token_registry (
                token_key TEXT PRIMARY KEY,
                chain TEXT NOT NULL,
                chain_id INTEGER NOT NULL,
                contract_address TEXT NOT NULL,
                market_symbol TEXT NOT NULL,
                token_symbol TEXT NOT NULL DEFAULT '',
                token_name TEXT NOT NULL DEFAULT '',
                decimals INTEGER,
                status TEXT NOT NULL,
                source TEXT NOT NULL,
                verification_method TEXT NOT NULL DEFAULT '',
                verification_note TEXT NOT NULL DEFAULT '',
                metadata_hash TEXT NOT NULL DEFAULT '',
                is_primary INTEGER NOT NULL DEFAULT 0,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                verified_at INTEGER,
                disabled_at INTEGER,
                UNIQUE(chain_id, contract_address)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ux_registry_primary
                ON token_registry(market_symbol, chain_id)
                WHERE status='verified' AND is_primary=1;
            CREATE INDEX IF NOT EXISTS idx_registry_symbol
                ON token_registry(market_symbol, status, is_primary);

            CREATE TABLE IF NOT EXISTS watch_items (
                token_key TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                priority INTEGER NOT NULL,
                query_window TEXT NOT NULL,
                scan_interval_sec INTEGER NOT NULL,
                next_scan_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                manual_watch INTEGER NOT NULL DEFAULT 0,
                manual_expires_at INTEGER,
                manual_priority INTEGER NOT NULL DEFAULT 0,
                lease_owner TEXT NOT NULL DEFAULT '',
                lease_until INTEGER,
                last_scan_at INTEGER,
                last_scan_status TEXT NOT NULL DEFAULT '',
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                last_error_code TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                FOREIGN KEY(token_key) REFERENCES token_registry(token_key)
            );
            CREATE INDEX IF NOT EXISTS idx_watch_due
                ON watch_items(status, next_scan_at, priority DESC);

            CREATE TABLE IF NOT EXISTS watch_sources (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                token_key TEXT NOT NULL,
                source_module TEXT NOT NULL,
                source_public_ref TEXT NOT NULL,
                source_signal_id INTEGER,
                source_symbol TEXT NOT NULL,
                source_score REAL,
                source_priority INTEGER NOT NULL,
                source_stage TEXT NOT NULL DEFAULT '',
                source_severity TEXT NOT NULL DEFAULT '',
                source_summary TEXT NOT NULL DEFAULT '',
                source_ts INTEGER NOT NULL,
                source_payload_hash TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                UNIQUE(source_public_ref, token_key),
                FOREIGN KEY(token_key) REFERENCES token_registry(token_key)
            );
            CREATE INDEX IF NOT EXISTS idx_watch_sources_active
                ON watch_sources(token_key, expires_at);

            CREATE TABLE IF NOT EXISTS bridge_state (
                source_key TEXT PRIMARY KEY,
                last_signal_ts INTEGER NOT NULL,
                last_signal_id INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS unresolved_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_public_ref TEXT NOT NULL,
                source_signal_id INTEGER,
                source_module TEXT NOT NULL,
                source_symbol TEXT NOT NULL,
                source_ts INTEGER NOT NULL,
                source_payload_hash TEXT NOT NULL,
                reason TEXT NOT NULL,
                candidate_chain TEXT NOT NULL DEFAULT '',
                candidate_contract TEXT NOT NULL DEFAULT '',
                attempts INTEGER NOT NULL DEFAULT 1,
                first_seen_at INTEGER NOT NULL,
                last_seen_at INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                resolved_at INTEGER,
                resolved_token_key TEXT,
                resolution_note TEXT NOT NULL DEFAULT '',
                UNIQUE(source_public_ref, source_payload_hash, reason)
            );
            CREATE INDEX IF NOT EXISTS idx_unresolved_open_symbol
                ON unresolved_signals(status, source_symbol, reason);

            CREATE TABLE IF NOT EXISTS watch_scan_runs (
                scan_id TEXT PRIMARY KEY,
                token_key TEXT NOT NULL,
                started_at INTEGER NOT NULL,
                completed_at INTEGER,
                status TEXT NOT NULL,
                activity_complete INTEGER,
                analysis_complete INTEGER,
                analysis_status TEXT NOT NULL DEFAULT '',
                behavior_type TEXT NOT NULL DEFAULT '',
                behavior_score INTEGER,
                max_wallet_group_score INTEGER,
                transfer_count INTEGER,
                rpc_request_count INTEGER,
                query_window TEXT NOT NULL DEFAULT '',
                total_token_amount TEXT NOT NULL DEFAULT '',
                unique_senders INTEGER,
                unique_receivers INTEGER,
                baseline_status TEXT NOT NULL DEFAULT '',
                baseline_anomaly INTEGER NOT NULL DEFAULT 0,
                baseline_json TEXT NOT NULL DEFAULT '{}',
                context_hash TEXT NOT NULL DEFAULT '',
                notification_status TEXT NOT NULL DEFAULT '',
                notification_reason TEXT NOT NULL DEFAULT '',
                error_code TEXT NOT NULL DEFAULT '',
                source_refs_json TEXT NOT NULL DEFAULT '[]',
                FOREIGN KEY(token_key) REFERENCES token_registry(token_key)
            );
            CREATE INDEX IF NOT EXISTS idx_scan_token_time
                ON watch_scan_runs(token_key, started_at DESC);
            """
        for statement in schema.split(";"):
            sql = statement.strip()
            if sql:
                conn.execute(sql)

    @staticmethod
    def _require_schema(conn: sqlite3.Connection) -> None:
        try:
            row = conn.execute(
                "SELECT value FROM automation_meta WHERE key='schema_version'"
            ).fetchone()
        except sqlite3.Error as exc:
            raise AutomationStoreError(
                "automation_schema_incompatible",
                "automation database is not initialized",
            ) from exc
        if row is None or int(row["value"]) != OAR_AUTOMATION_SCHEMA_VERSION:
            raise AutomationStoreError(
                "automation_schema_incompatible",
                "automation database schema version is incompatible",
            )

    @staticmethod
    def integrity_check_existing(path: Path) -> str:
        path = Path(path)
        if not path.exists():
            return "not_initialized"
        uri = f"file:{quote(path.resolve().as_posix(), safe='/:')}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            conn.execute("PRAGMA query_only=ON")
            row = conn.execute("PRAGMA quick_check").fetchone()
            return str(row[0]) if row else "failed"
        finally:
            conn.close()

    def add_registry(
        self,
        *,
        market_symbol: str,
        contract: str,
        chain: str = "base",
        chain_id: int = 8453,
        source: str,
        note: str = "",
        now: int | None = None,
    ) -> dict[str, object]:
        timestamp = int(now if now is not None else self.clock())
        symbol = canonical_market_symbol(market_symbol)
        token_key = canonical_token_key(chain_id, contract)
        normalized_chain = str(chain or "").strip().lower()
        if CHAIN_SLUG_RE.fullmatch(normalized_chain) is None:
            raise AutomationStoreError(
                "invalid_chain", "chain must be a canonical EVM chain slug"
            )
        _, address = parse_token_key(token_key)
        self.migrate()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT market_symbol FROM token_registry WHERE token_key=?",
                (token_key,),
            ).fetchone()
            if (
                existing is not None
                and str(existing["market_symbol"]) != symbol
            ):
                conn.rollback()
                raise AutomationStoreError(
                    "contract_market_symbol_conflict",
                    "the contract is already registered to another market symbol",
                )
            conn.execute(
                """
                INSERT INTO token_registry(
                    token_key, chain, chain_id, contract_address,
                    market_symbol, status, source, verification_note,
                    created_at, updated_at
                ) VALUES(?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                ON CONFLICT(token_key) DO UPDATE SET
                    source=excluded.source,
                    verification_note=excluded.verification_note,
                    updated_at=excluded.updated_at
                """,
                (
                    token_key,
                    normalized_chain,
                    int(chain_id),
                    address,
                    symbol,
                    str(source or "manual")[:80],
                    str(note or "")[:500],
                    timestamp,
                    timestamp,
                ),
            )
            conn.commit()
        return self.get_registry(token_key) or {}

    def get_registry(self, token_key: str) -> dict[str, object] | None:
        parse_token_key(token_key)
        with self.connect_existing() as conn:
            if conn is None:
                return None
            row = conn.execute(
                "SELECT * FROM token_registry WHERE token_key=?",
                (token_key.lower(),),
            ).fetchone()
            return dict(row) if row else None

    def list_registry(
        self,
        *,
        status: str | None = None,
        market_symbol: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, object]] | None:
        if status is not None and status not in REGISTRY_STATUSES:
            raise AutomationStoreError(
                "invalid_registry_status", "unsupported registry status"
            )
        clauses: list[str] = []
        params: list[object] = []
        if status:
            clauses.append("status=?")
            params.append(status)
        if market_symbol:
            clauses.append("market_symbol=?")
            params.append(canonical_market_symbol(market_symbol))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect_existing() as conn:
            if conn is None:
                return None
            rows = conn.execute(
                "SELECT * FROM token_registry"
                + where
                + " ORDER BY market_symbol, token_key LIMIT ?",
                (*params, max(1, min(int(limit), 500))),
            ).fetchall()
            return [dict(row) for row in rows]

    def verify_registry(
        self,
        token_key: str,
        *,
        token_symbol: str,
        token_name: str,
        decimals: int,
        metadata_hash: str,
        verification_method: str,
        verification_note: str = "",
        set_primary: bool,
        now: int | None = None,
    ) -> dict[str, object]:
        self.migrate()
        chain_id, _contract = parse_token_key(token_key)
        if decimals < 0 or decimals > 36:
            raise AutomationStoreError(
                "invalid_decimals", "verified decimals must be in [0, 36]"
            )
        timestamp = int(now if now is not None else self.clock())
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT market_symbol, status, is_primary "
                "FROM token_registry WHERE token_key=?",
                (token_key.lower(),),
            ).fetchone()
            if row is None:
                conn.rollback()
                raise AutomationStoreError(
                    "registry_not_found", "registry token does not exist"
                )
            if set_primary:
                conn.execute(
                    """
                    UPDATE token_registry SET is_primary=0, updated_at=?
                    WHERE market_symbol=? AND chain_id=? AND token_key<>?
                    """,
                    (
                        timestamp,
                        str(row["market_symbol"]),
                        chain_id,
                        token_key.lower(),
                    ),
                )
            was_primary = bool(int(row["is_primary"] or 0))
            target_primary = bool(set_primary or was_primary)
            conn.execute(
                """
                UPDATE token_registry SET
                    token_symbol=?, token_name=?, decimals=?,
                    status='verified', verification_method=?,
                    verification_note=?, metadata_hash=?,
                    is_primary=?, verified_at=?, disabled_at=NULL,
                    updated_at=?
                WHERE token_key=?
                """,
                (
                    str(token_symbol or "")[:80],
                    str(token_name or "")[:200],
                    int(decimals),
                    str(verification_method or "")[:80],
                    str(verification_note or "")[:500],
                    str(metadata_hash or "")[:64],
                    int(target_primary),
                    timestamp,
                    timestamp,
                    token_key.lower(),
                ),
            )
            conn.commit()
        result = self.get_registry(token_key) or {}
        result["verification"] = {
            "was_primary": was_primary,
            "is_primary": target_primary,
            "primary_changed": was_primary != target_primary,
        }
        return result

    def disable_registry(
        self, token_key: str, *, now: int | None = None
    ) -> dict[str, object]:
        self.migrate()
        parse_token_key(token_key)
        timestamp = int(now if now is not None else self.clock())
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                """
                UPDATE token_registry
                SET status='disabled', is_primary=0,
                    disabled_at=?, updated_at=?
                WHERE token_key=?
                """,
                (timestamp, timestamp, token_key.lower()),
            ).rowcount
            if not changed:
                conn.rollback()
                raise AutomationStoreError(
                    "registry_not_found", "registry token does not exist"
                )
            conn.execute(
                """
                UPDATE watch_items
                SET status='paused', lease_owner='', lease_until=NULL,
                    last_error_code='registry_disabled', updated_at=?
                WHERE token_key=?
                """,
                (timestamp, token_key.lower()),
            )
            conn.commit()
        return self.get_registry(token_key) or {}

    def resolve_registry(self, market_symbol: str) -> dict[str, object]:
        symbol = canonical_market_symbol(market_symbol)
        with self.connect_existing() as conn:
            if conn is None:
                return {"status": "unresolved_contract", "token": None}
            rows = conn.execute(
                """
                SELECT * FROM token_registry
                WHERE market_symbol=? AND status='verified'
                ORDER BY is_primary DESC, token_key
                """,
                (symbol,),
            ).fetchall()
        if not rows:
            pending = self.list_registry(
                market_symbol=symbol, limit=20
            ) or []
            return {
                "status": (
                    "registry_not_verified" if pending else "unresolved_contract"
                ),
                "token": None,
            }
        primary = [row for row in rows if int(row["is_primary"]) == 1]
        if len(primary) != 1:
            return {"status": "ambiguous_contract", "token": None}
        return {"status": "resolved", "token": dict(primary[0])}

    def add_manual_watch(
        self,
        token_key: str,
        *,
        ttl_sec: int,
        priority: int,
        query_window: str,
        scan_interval_sec: int,
        max_active_tokens: int = OAR_WATCH_MAX_ACTIVE_TOKENS_HARD,
        now: int | None = None,
    ) -> dict[str, object]:
        self.migrate()
        parse_token_key(token_key)
        timestamp = int(now if now is not None else self.clock())
        expires_at = timestamp + int(ttl_sec)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            registry = conn.execute(
                "SELECT status FROM token_registry WHERE token_key=?",
                (token_key.lower(),),
            ).fetchone()
            if registry is None or registry["status"] != "verified":
                conn.rollback()
                raise AutomationStoreError(
                    "registry_not_verified",
                    "only verified tokens can enter the watchlist",
                )
            existing = conn.execute(
                "SELECT status, expires_at FROM watch_items WHERE token_key=?",
                (token_key.lower(),),
            ).fetchone()
            currently_active = existing is not None and (
                str(existing["status"]) == "active"
                and int(existing["expires_at"] or 0) > timestamp
            )
            active_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM watch_items "
                    "WHERE status='active' AND expires_at>?",
                    (timestamp,),
                ).fetchone()[0]
            )
            if (
                not currently_active
                and active_count >= int(max_active_tokens)
            ):
                conn.rollback()
                raise AutomationStoreError(
                    "capacity_exceeded",
                    "active watch token capacity has been reached",
                )
            conn.execute(
                """
                INSERT INTO watch_items(
                    token_key, status, priority, query_window,
                    scan_interval_sec, next_scan_at, expires_at,
                    manual_watch, manual_expires_at, manual_priority,
                    created_at, updated_at
                ) VALUES(?, 'active', ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
                ON CONFLICT(token_key) DO UPDATE SET
                    status='active',
                    priority=MAX(watch_items.priority, excluded.priority),
                    query_window=excluded.query_window,
                    scan_interval_sec=excluded.scan_interval_sec,
                    next_scan_at=MIN(watch_items.next_scan_at, excluded.next_scan_at),
                    expires_at=MAX(watch_items.expires_at, excluded.expires_at),
                    manual_watch=1,
                    manual_expires_at=MAX(
                        COALESCE(watch_items.manual_expires_at, 0),
                        excluded.manual_expires_at
                    ),
                    manual_priority=MAX(
                        watch_items.manual_priority,
                        excluded.manual_priority
                    ),
                    updated_at=excluded.updated_at
                """,
                (
                    token_key.lower(),
                    int(priority),
                    query_window,
                    int(scan_interval_sec),
                    timestamp,
                    expires_at,
                    expires_at,
                    int(priority),
                    timestamp,
                    timestamp,
                ),
            )
            conn.commit()
        return self.get_watch(token_key) or {}

    def get_watch(self, token_key: str) -> dict[str, object] | None:
        parse_token_key(token_key)
        with self.connect_existing() as conn:
            if conn is None:
                return None
            row = conn.execute(
                "SELECT * FROM watch_items WHERE token_key=?",
                (token_key.lower(),),
            ).fetchone()
            return dict(row) if row else None

    def list_watch_items(
        self,
        *,
        status: str | None = None,
        due_only: bool = False,
        limit: int = 100,
        now: int | None = None,
    ) -> list[dict[str, object]] | None:
        if status is not None and status not in WATCH_STATUSES:
            raise AutomationStoreError(
                "invalid_watch_status", "unsupported watch status"
            )
        timestamp = int(now if now is not None else self.clock())
        clauses: list[str] = []
        params: list[object] = []
        if status:
            clauses.append("w.status=?")
            params.append(status)
        if due_only:
            clauses.extend(
                [
                    "w.status='active'",
                    "w.next_scan_at<=?",
                    "w.expires_at>?",
                    "(w.lease_until IS NULL OR w.lease_until<=?)",
                ]
            )
            params.extend([timestamp, timestamp, timestamp])
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        with self.connect_existing() as conn:
            if conn is None:
                return None
            rows = conn.execute(
                """
                SELECT w.*, r.market_symbol, r.token_symbol,
                       r.chain, r.chain_id, r.contract_address,
                       r.status AS registry_status
                FROM watch_items w
                JOIN token_registry r ON r.token_key=w.token_key
                """
                + where
                + " ORDER BY w.priority DESC, w.next_scan_at, w.token_key LIMIT ?",
                (*params, max(1, min(int(limit), 500))),
            ).fetchall()
            return [dict(row) for row in rows]

    def remove_manual_watch(
        self, token_key: str, *, now: int | None = None
    ) -> dict[str, object]:
        self.migrate()
        parse_token_key(token_key)
        timestamp = int(now if now is not None else self.clock())
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                "UPDATE watch_items SET manual_watch=0, manual_expires_at=NULL, "
                "manual_priority=0, "
                "updated_at=? "
                "WHERE token_key=?",
                (timestamp, token_key.lower()),
            ).rowcount
            if not changed:
                conn.rollback()
                raise AutomationStoreError(
                    "watch_not_found", "watch item does not exist"
                )
            self._recompute_watch(
                conn,
                token_key.lower(),
                now=timestamp,
                manual_priority=0,
            )
            conn.commit()
        return self.get_watch(token_key) or {}

    @staticmethod
    def _recompute_watch(
        conn: sqlite3.Connection,
        token_key: str,
        *,
        now: int,
        manual_priority: int,
    ) -> None:
        item = conn.execute(
            "SELECT * FROM watch_items WHERE token_key=?", (token_key,)
        ).fetchone()
        if item is None:
            return
        source = conn.execute(
            """
            SELECT MAX(expires_at) AS expires_at
            FROM watch_sources WHERE token_key=? AND expires_at>?
            """,
            (token_key, now),
        ).fetchone()
        source_priority = conn.execute(
            """
            SELECT MAX(source_priority) AS priority
            FROM watch_sources WHERE token_key=? AND expires_at>?
            """,
            (token_key, now),
        ).fetchone()
        manual_active = bool(item["manual_watch"]) and int(
            item["manual_expires_at"] or 0
        ) > now
        source_expires = int(source["expires_at"] or 0)
        if not manual_active and source_expires <= now:
            conn.execute(
                """
                UPDATE watch_items
                SET status='expired', manual_watch=0, lease_owner='',
                    lease_until=NULL, updated_at=?
                WHERE token_key=?
                """,
                (now, token_key),
            )
            return
        priority = max(
            int(source_priority["priority"] or 0),
            int(item["manual_priority"] or manual_priority)
            if manual_active
            else 0,
        )
        expires_at = max(
            source_expires,
            int(item["manual_expires_at"] or 0) if manual_active else 0,
        )
        next_status = (
            "paused" if str(item["status"]) == "paused" else "active"
        )
        conn.execute(
            """
            UPDATE watch_items
            SET status=?, priority=?, expires_at=?, updated_at=?
            WHERE token_key=?
            """,
            (next_status, priority, expires_at, now, token_key),
        )

    def bridge_checkpoint(
        self, source_key: str = "main_signals"
    ) -> tuple[int, int]:
        with self.connect_existing() as conn:
            if conn is None:
                return (0, 0)
            row = conn.execute(
                "SELECT last_signal_ts, last_signal_id FROM bridge_state "
                "WHERE source_key=?",
                (source_key,),
            ).fetchone()
            return (
                (int(row["last_signal_ts"]), int(row["last_signal_id"]))
                if row
                else (0, 0)
            )

    def process_bridge_signal(
        self,
        signal: dict[str, object],
        *,
        resolution: dict[str, object],
        source_ttl_sec: int,
        source_priority: int,
        query_window: str,
        scan_interval_sec: int,
        max_active_tokens: int,
        source_key: str = "main_signals",
        now: int | None = None,
    ) -> str:
        self.migrate()
        timestamp = int(now if now is not None else self.clock())
        signal_ts = int(signal.get("ts") or 0)
        signal_id = int(signal.get("id") or 0)
        public_ref = str(signal.get("public_ref") or "")[:160]
        payload_hash = str(signal.get("payload_hash") or "")
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                outcome = str(resolution.get("status") or "unresolved_contract")
                token = resolution.get("token")
                if outcome == "resolved" and isinstance(token, dict):
                    token_key = str(token["token_key"])
                    existing = conn.execute(
                        "SELECT status, expires_at FROM watch_items "
                        "WHERE token_key=?",
                        (token_key,),
                    ).fetchone()
                    prior_source = conn.execute(
                        """
                        SELECT source_signal_id, source_score, source_stage,
                               source_severity, source_summary, source_ts,
                               source_payload_hash, expires_at
                        FROM watch_sources
                        WHERE source_public_ref=? AND token_key=?
                        """,
                        (public_ref, token_key),
                    ).fetchone()
                    source_values = (
                        signal_id,
                        signal.get("score"),
                        str(signal.get("stage") or "")[:80],
                        str(signal.get("severity") or "")[:24],
                        str(signal.get("excerpt") or "")[:300],
                        signal_ts,
                        payload_hash,
                        signal_ts + int(source_ttl_sec),
                    )
                    source_changed = prior_source is None or tuple(
                        prior_source
                    ) != source_values
                    active_count = int(
                        conn.execute(
                            "SELECT COUNT(*) FROM watch_items "
                            "WHERE status='active' AND expires_at>?",
                            (timestamp,),
                        ).fetchone()[0]
                    )
                    would_activate = existing is None or (
                        str(existing["status"]) != "paused"
                        and not (
                            str(existing["status"]) == "active"
                            and int(existing["expires_at"] or 0) > timestamp
                        )
                    )
                    if would_activate and active_count >= int(
                        max_active_tokens
                    ):
                        outcome = "capacity_exceeded"
                        self._upsert_unresolved(
                            conn,
                            signal,
                            reason=outcome,
                            now=timestamp,
                        )
                    else:
                        conn.execute(
                            """
                            INSERT INTO watch_sources(
                                token_key, source_module, source_public_ref,
                                source_signal_id, source_symbol, source_score,
                                source_priority,
                                source_stage, source_severity, source_summary,
                                source_ts, source_payload_hash, expires_at,
                                created_at, updated_at
                            ) VALUES(
                                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                            )
                            ON CONFLICT(source_public_ref, token_key) DO UPDATE SET
                                source_signal_id=excluded.source_signal_id,
                                source_score=excluded.source_score,
                                source_priority=excluded.source_priority,
                                source_stage=excluded.source_stage,
                                source_severity=excluded.source_severity,
                                source_summary=excluded.source_summary,
                                source_ts=excluded.source_ts,
                                source_payload_hash=excluded.source_payload_hash,
                                expires_at=excluded.expires_at,
                                updated_at=excluded.updated_at
                            """,
                            (
                                token_key,
                                str(signal.get("module") or ""),
                                public_ref,
                                signal_id,
                                str(signal.get("symbol") or ""),
                                signal.get("score"),
                                int(source_priority),
                                str(signal.get("stage") or "")[:80],
                                str(signal.get("severity") or "")[:24],
                                str(signal.get("excerpt") or "")[:300],
                                signal_ts,
                                payload_hash,
                                signal_ts + int(source_ttl_sec),
                                timestamp,
                                timestamp,
                            ),
                        )
                        conn.execute(
                            """
                            INSERT INTO watch_items(
                                token_key, status, priority, query_window,
                                scan_interval_sec, next_scan_at, expires_at,
                                manual_watch, created_at, updated_at
                            ) VALUES(?, 'active', ?, ?, ?, ?, ?, 0, ?, ?)
                            ON CONFLICT(token_key) DO UPDATE SET
                                status=CASE
                                    WHEN watch_items.status='paused'
                                    THEN 'paused'
                                    ELSE 'active'
                                END,
                                priority=MAX(watch_items.priority, excluded.priority),
                                query_window=excluded.query_window,
                                scan_interval_sec=excluded.scan_interval_sec,
                                expires_at=MAX(
                                    watch_items.expires_at,
                                    excluded.expires_at
                                ),
                                updated_at=excluded.updated_at
                            """,
                            (
                                token_key,
                                int(source_priority),
                                query_window,
                                int(scan_interval_sec),
                                timestamp,
                                signal_ts + int(source_ttl_sec),
                                timestamp,
                                timestamp,
                            ),
                        )
                        if existing is not None and source_changed:
                            conn.execute(
                                """
                                UPDATE watch_items
                                SET next_scan_at=MIN(next_scan_at, ?)
                                WHERE token_key=? AND status<>'paused'
                                """,
                                (timestamp, token_key),
                            )
                        outcome = (
                            "watch_refreshed"
                            if existing is not None
                            else "watch_created"
                        )
                        self._recompute_watch(
                            conn,
                            token_key,
                            now=timestamp,
                            manual_priority=0,
                        )
                else:
                    self._upsert_unresolved(
                        conn,
                        signal,
                        reason=outcome,
                        now=timestamp,
                    )
                previous = conn.execute(
                    "SELECT last_signal_ts, last_signal_id FROM bridge_state "
                    "WHERE source_key=?",
                    (source_key,),
                ).fetchone()
                checkpoint = (
                    max(
                        (int(previous["last_signal_ts"]), int(previous["last_signal_id"])),
                        (signal_ts, signal_id),
                    )
                    if previous
                    else (signal_ts, signal_id)
                )
                conn.execute(
                    """
                    INSERT INTO bridge_state(
                        source_key, last_signal_ts, last_signal_id, updated_at
                    ) VALUES(?, ?, ?, ?)
                    ON CONFLICT(source_key) DO UPDATE SET
                        last_signal_ts=excluded.last_signal_ts,
                        last_signal_id=excluded.last_signal_id,
                        updated_at=excluded.updated_at
                    """,
                    (source_key, checkpoint[0], checkpoint[1], timestamp),
                )
                conn.commit()
                return outcome
            except Exception:
                conn.rollback()
                raise

    def checkpoint_ignored_signal(
        self,
        signal: dict[str, object],
        *,
        source_key: str = "main_signals",
        now: int | None = None,
    ) -> None:
        self.migrate()
        timestamp = int(now if now is not None else self.clock())
        signal_ts = int(signal.get("ts") or 0)
        signal_id = int(signal.get("id") or 0)
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            previous = conn.execute(
                "SELECT last_signal_ts, last_signal_id FROM bridge_state "
                "WHERE source_key=?",
                (source_key,),
            ).fetchone()
            checkpoint = (
                max(
                    (
                        int(previous["last_signal_ts"]),
                        int(previous["last_signal_id"]),
                    ),
                    (signal_ts, signal_id),
                )
                if previous
                else (signal_ts, signal_id)
            )
            conn.execute(
                """
                INSERT INTO bridge_state(
                    source_key, last_signal_ts, last_signal_id, updated_at
                ) VALUES(?, ?, ?, ?)
                ON CONFLICT(source_key) DO UPDATE SET
                    last_signal_ts=excluded.last_signal_ts,
                    last_signal_id=excluded.last_signal_id,
                    updated_at=excluded.updated_at
                """,
                (source_key, checkpoint[0], checkpoint[1], timestamp),
            )
            conn.commit()

    @staticmethod
    def _upsert_unresolved(
        conn: sqlite3.Connection,
        signal: dict[str, object],
        *,
        reason: str,
        now: int,
    ) -> None:
        conn.execute(
            """
            INSERT INTO unresolved_signals(
                source_public_ref, source_signal_id, source_module,
                source_symbol, source_ts, source_payload_hash, reason,
                candidate_chain, candidate_contract, attempts,
                first_seen_at, last_seen_at, status, resolved_at,
                resolved_token_key, resolution_note
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, 'open', NULL, NULL, '')
            ON CONFLICT(source_public_ref, source_payload_hash, reason)
            DO UPDATE SET
                source_signal_id=excluded.source_signal_id,
                attempts=CASE
                    WHEN unresolved_signals.status='open'
                    THEN unresolved_signals.attempts + 1
                    ELSE unresolved_signals.attempts
                END,
                last_seen_at=CASE
                    WHEN unresolved_signals.status='open'
                    THEN excluded.last_seen_at
                    ELSE unresolved_signals.last_seen_at
                END
            """,
            (
                str(signal.get("public_ref") or "")[:160],
                int(signal.get("id") or 0),
                str(signal.get("module") or "")[:40],
                str(signal.get("symbol") or "")[:40],
                int(signal.get("ts") or 0),
                str(signal.get("payload_hash") or "")[:64],
                str(reason or "unresolved_contract")[:80],
                str(signal.get("candidate_chain") or "")[:20],
                str(signal.get("candidate_contract") or "")[:42],
                now,
                now,
            ),
        )

    def list_open_unresolved(
        self,
        *,
        market_symbol: str | None = None,
        reasons: tuple[str, ...] = (
            "unresolved_contract",
            "registry_not_verified",
            "ambiguous_contract",
        ),
        limit: int = 100,
    ) -> list[dict[str, object]]:
        if not reasons:
            return []
        clauses = ["status='open'"]
        params: list[object] = []
        if market_symbol is not None:
            clauses.append("source_symbol=?")
            params.append(canonical_market_symbol(market_symbol))
        placeholders = ",".join("?" for _ in reasons)
        clauses.append(f"reason IN ({placeholders})")
        params.extend(reasons)
        with self.connect_existing() as conn:
            if conn is None:
                return []
            rows = conn.execute(
                "SELECT * FROM unresolved_signals WHERE "
                + " AND ".join(clauses)
                + " ORDER BY source_ts, id LIMIT ?",
                (*params, max(1, min(int(limit), 100))),
            ).fetchall()
            return [dict(row) for row in rows]

    def unresolved_summary(
        self,
        *,
        limit: int = 20,
    ) -> list[dict[str, object]]:
        bounded_limit = max(1, min(int(limit), 20))
        with self.connect_existing() as conn:
            if conn is None:
                return []
            rows = conn.execute(
                """
                SELECT reason, source_symbol, candidate_chain,
                       candidate_contract, COUNT(*) AS count,
                       MAX(source_ts) AS latest_source_ts
                FROM unresolved_signals
                WHERE status='open'
                GROUP BY reason, source_symbol, candidate_chain,
                         candidate_contract
                ORDER BY count DESC, latest_source_ts DESC, source_symbol
                LIMIT ?
                """,
                (bounded_limit,),
            ).fetchall()
            return [dict(row) for row in rows]

    def resolve_unresolved(
        self,
        unresolved_id: int,
        *,
        status: str,
        token_key: str = "",
        note: str = "",
        now: int | None = None,
    ) -> bool:
        if status not in {"resolved", "expired"}:
            raise AutomationStoreError(
                "invalid_unresolved_status",
                "unresolved status must be resolved or expired",
            )
        self.migrate()
        timestamp = int(now if now is not None else self.clock())
        normalized_key: str | None = None
        if token_key:
            parse_token_key(token_key)
            normalized_key = token_key.lower()
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                """
                UPDATE unresolved_signals
                SET status=?, resolved_at=?, resolved_token_key=?,
                    resolution_note=?, last_seen_at=?
                WHERE id=? AND status='open'
                """,
                (
                    status,
                    timestamp,
                    normalized_key,
                    str(note or "")[:200],
                    timestamp,
                    int(unresolved_id),
                ),
            ).rowcount
            conn.commit()
        return bool(changed)

    def open_unresolved_count(
        self, *, market_symbol: str | None = None
    ) -> int:
        params: tuple[object, ...] = ()
        where = "status='open'"
        if market_symbol is not None:
            where += " AND source_symbol=?"
            params = (canonical_market_symbol(market_symbol),)
        with self.connect_existing() as conn:
            if conn is None:
                return 0
            return int(
                conn.execute(
                    f"SELECT COUNT(*) FROM unresolved_signals WHERE {where}",
                    params,
                ).fetchone()[0]
            )

    def expire_and_recompute(
        self,
        *,
        manual_priority: int,
        now: int | None = None,
    ) -> int:
        self.migrate()
        timestamp = int(now if now is not None else self.clock())
        changed = 0
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT token_key FROM watch_items").fetchall()
            for row in rows:
                before = conn.execute(
                    "SELECT status, priority, expires_at, manual_watch "
                    "FROM watch_items WHERE token_key=?",
                    (row["token_key"],),
                ).fetchone()
                self._recompute_watch(
                    conn,
                    str(row["token_key"]),
                    now=timestamp,
                    manual_priority=manual_priority,
                )
                after = conn.execute(
                    "SELECT status, priority, expires_at, manual_watch "
                    "FROM watch_items WHERE token_key=?",
                    (row["token_key"],),
                ).fetchone()
                changed += int(tuple(before) != tuple(after))
            conn.commit()
        return changed

    def claim_due(
        self,
        *,
        owner: str,
        limit: int,
        lease_sec: int,
        now: int | None = None,
    ) -> list[dict[str, object]]:
        self.migrate()
        timestamp = int(now if now is not None else self.clock())
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT w.*, r.market_symbol, r.token_symbol,
                       r.chain, r.chain_id, r.contract_address, r.decimals
                FROM watch_items w
                JOIN token_registry r ON r.token_key=w.token_key
                WHERE w.status='active'
                  AND w.next_scan_at<=?
                  AND w.expires_at>?
                  AND r.status='verified'
                  AND (w.lease_until IS NULL OR w.lease_until<=?)
                ORDER BY w.priority DESC, w.next_scan_at, w.token_key
                LIMIT ?
                """,
                (timestamp, timestamp, timestamp, int(limit)),
            ).fetchall()
            keys = [str(row["token_key"]) for row in rows]
            for token_key in keys:
                conn.execute(
                    """
                    UPDATE watch_items SET lease_owner=?, lease_until=?,
                        updated_at=? WHERE token_key=?
                    """,
                    (owner, timestamp + int(lease_sec), timestamp, token_key),
                )
            conn.commit()
            return [
                {
                    **dict(row),
                    "lease_owner": owner,
                    "lease_until": timestamp + int(lease_sec),
                }
                for row in rows
            ]

    def due_token_keys(
        self,
        *,
        limit: int,
        now: int | None = None,
    ) -> list[str]:
        timestamp = int(now if now is not None else self.clock())
        with self.connect_existing() as conn:
            if conn is None:
                return []
            rows = conn.execute(
                """
                SELECT w.token_key
                FROM watch_items w
                JOIN token_registry r ON r.token_key=w.token_key
                WHERE w.status='active'
                  AND w.next_scan_at<=?
                  AND w.expires_at>?
                  AND r.status='verified'
                  AND (w.lease_until IS NULL OR w.lease_until<=?)
                ORDER BY w.priority DESC, w.next_scan_at, w.token_key
                LIMIT ?
                """,
                (timestamp, timestamp, timestamp, max(1, min(int(limit), 100))),
            ).fetchall()
            return [str(row["token_key"]) for row in rows]

    def renew_lease(
        self,
        token_key: str,
        *,
        lease_owner: str,
        lease_sec: int,
        now: int | None = None,
    ) -> bool:
        self.migrate()
        parse_token_key(token_key)
        timestamp = int(now if now is not None else self.clock())
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                """
                UPDATE watch_items
                SET lease_until=?, updated_at=?
                WHERE token_key=? AND lease_owner=?
                """,
                (
                    timestamp + int(lease_sec),
                    timestamp,
                    token_key.lower(),
                    str(lease_owner),
                ),
            ).rowcount
            conn.commit()
        return bool(changed)

    def release_claim_without_failure(
        self,
        token_key: str,
        *,
        lease_owner: str,
        reason: str = "deferred_by_cycle_budget",
        now: int | None = None,
    ) -> bool:
        self.migrate()
        parse_token_key(token_key)
        timestamp = int(now if now is not None else self.clock())
        scan_id = uuid.uuid4().hex
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT lease_owner FROM watch_items WHERE token_key=?",
                (token_key.lower(),),
            ).fetchone()
            if current is None or str(current["lease_owner"] or "") != str(
                lease_owner
            ):
                conn.rollback()
                return False
            conn.execute(
                """
                INSERT INTO watch_scan_runs(
                    scan_id, token_key, started_at, completed_at, status,
                    error_code
                ) VALUES(?, ?, ?, ?, 'deferred', ?)
                """,
                (
                    scan_id,
                    token_key.lower(),
                    timestamp,
                    timestamp,
                    str(reason or "deferred_by_cycle_budget")[:80],
                ),
            )
            conn.execute(
                """
                UPDATE watch_items
                SET lease_owner='', lease_until=NULL, updated_at=?
                WHERE token_key=? AND lease_owner=?
                """,
                (timestamp, token_key.lower(), str(lease_owner)),
            )
            self._trim_scan_audit(conn, token_key.lower())
            conn.commit()
        return True

    def active_sources(
        self,
        token_key: str,
        *,
        now: int | None = None,
        limit: int = 20,
    ) -> list[dict[str, object]]:
        parse_token_key(token_key)
        timestamp = int(now if now is not None else self.clock())
        with self.connect_existing() as conn:
            if conn is None:
                return []
            rows = conn.execute(
                """
                SELECT * FROM watch_sources
                WHERE token_key=? AND expires_at>?
                ORDER BY source_priority DESC, source_ts DESC,
                         source_public_ref
                LIMIT ?
                """,
                (token_key.lower(), timestamp, max(1, min(limit, 100))),
            ).fetchall()
            return [dict(row) for row in rows]

    def record_scan(
        self,
        token_key: str,
        *,
        lease_owner: str,
        started_at: int,
        status: str,
        activity_complete: bool | None,
        analysis_complete: bool | None,
        analysis_status: str = "",
        behavior_type: str = "",
        behavior_score: int | None = None,
        max_wallet_group_score: int | None = None,
        transfer_count: int | None = None,
        rpc_request_count: int | None = None,
        query_window: str = "",
        total_token_amount: str = "",
        unique_senders: int | None = None,
        unique_receivers: int | None = None,
        historical_baseline: dict[str, object] | None = None,
        context_hash: str = "",
        notification_status: str = "",
        notification_reason: str = "",
        error_code: str = "",
        source_refs: list[str] | None = None,
        scan_interval_sec: int,
        max_consecutive_failures: int,
        now: int | None = None,
    ) -> str:
        self.migrate()
        parse_token_key(token_key)
        completed_at = int(now if now is not None else self.clock())
        scan_id = uuid.uuid4().hex
        failure = status in {"failed", "partial"}
        baseline = (
            historical_baseline
            if isinstance(historical_baseline, dict)
            else {}
        )
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT consecutive_failures, lease_owner "
                "FROM watch_items WHERE token_key=?",
                (token_key.lower(),),
            ).fetchone()
            lease_matches = current is not None and str(
                current["lease_owner"] or ""
            ) == str(lease_owner)
            audit_status = status if lease_matches else "stale"
            audit_error = error_code if lease_matches else "lease_lost"
            conn.execute(
                """
                INSERT INTO watch_scan_runs(
                    scan_id, token_key, started_at, completed_at, status,
                    activity_complete, analysis_complete, analysis_status,
                    behavior_type, behavior_score, max_wallet_group_score,
                    transfer_count, rpc_request_count, query_window,
                    total_token_amount, unique_senders, unique_receivers,
                    baseline_status, baseline_anomaly, baseline_json,
                    context_hash,
                    notification_status, notification_reason, error_code,
                    source_refs_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scan_id,
                    token_key.lower(),
                    int(started_at),
                    completed_at,
                    audit_status,
                    None if activity_complete is None else int(activity_complete),
                    None if analysis_complete is None else int(analysis_complete),
                    str(analysis_status or "")[:80],
                    str(behavior_type or "")[:80],
                    behavior_score,
                    max_wallet_group_score,
                    transfer_count,
                    rpc_request_count,
                    str(query_window or "")[:8],
                    str(total_token_amount or "")[:160],
                    unique_senders,
                    unique_receivers,
                    str(baseline.get("status") or "")[:24],
                    int(bool(baseline.get("anomaly"))),
                    _json_dict(baseline),
                    str(context_hash or "")[:64],
                    str(notification_status or "")[:40],
                    str(notification_reason or "")[:120],
                    str(audit_error or "")[:80],
                    _json_list(sorted(set(source_refs or []))[:10]),
                ),
            )
            if not lease_matches:
                self._trim_scan_audit(conn, token_key.lower())
                conn.commit()
                return "lease_lost"
            failures = (
                int(current["consecutive_failures"] or 0) + 1
                if failure
                else 0
            )
            if failure:
                delays = (300, 900, 1800, 3600)
                delay = delays[min(failures - 1, len(delays) - 1)]
                next_scan_at = completed_at + delay
                watch_status = (
                    "paused"
                    if failures >= int(max_consecutive_failures)
                    else "active"
                )
            else:
                next_scan_at = completed_at + int(scan_interval_sec)
                watch_status = "active"
            changed = conn.execute(
                """
                UPDATE watch_items SET
                    status=?, next_scan_at=?, lease_owner='',
                    lease_until=NULL, last_scan_at=?, last_scan_status=?,
                    consecutive_failures=?, last_error_code=?, updated_at=?
                WHERE token_key=? AND lease_owner=?
                """,
                (
                    watch_status,
                    next_scan_at,
                    completed_at,
                    status,
                    failures,
                    str(error_code or "")[:80],
                    completed_at,
                    token_key.lower(),
                    str(lease_owner),
                ),
            ).rowcount
            if not changed:
                conn.rollback()
                return "lease_lost"
            self._trim_scan_audit(conn, token_key.lower())
            conn.commit()
        return scan_id

    def complete_scan_history(
        self,
        token_key: str,
        *,
        query_window: str,
        limit: int = 64,
    ) -> list[dict[str, object]]:
        parse_token_key(token_key)
        bounded_limit = max(1, min(int(limit), 100))
        with self.connect_existing() as conn:
            if conn is None:
                return []
            rows = conn.execute(
                """
                SELECT transfer_count, total_token_amount,
                       unique_senders, unique_receivers,
                       behavior_score, max_wallet_group_score,
                       baseline_json
                FROM watch_scan_runs
                WHERE token_key=? AND query_window=? AND status='ok'
                  AND activity_complete=1 AND analysis_complete=1
                ORDER BY started_at DESC, scan_id DESC
                LIMIT ?
                """,
                (token_key.lower(), str(query_window), bounded_limit),
            ).fetchall()
        result: list[dict[str, object]] = []
        for row in reversed(rows):
            item = dict(row)
            try:
                baseline = json.loads(str(item.pop("baseline_json") or "{}"))
            except json.JSONDecodeError:
                baseline = {}
            windows = (
                baseline.get("windows")
                if isinstance(baseline, dict)
                else {}
            )
            window_metrics: dict[str, object] = {}
            if isinstance(windows, dict):
                for name, value in windows.items():
                    if not isinstance(value, dict):
                        continue
                    current = value.get("current")
                    if isinstance(current, dict):
                        window_metrics[str(name)] = current
            item["window_metrics"] = window_metrics
            result.append(item)
        return result

    def latest_scan_baseline(
        self, token_key: str
    ) -> dict[str, object] | None:
        parse_token_key(token_key)
        with self.connect_existing() as conn:
            if conn is None:
                return None
            row = conn.execute(
                """
                SELECT scan_id, token_key, started_at, completed_at,
                       query_window, baseline_status, baseline_anomaly,
                       baseline_json
                FROM watch_scan_runs
                WHERE token_key=? AND baseline_status<>''
                ORDER BY started_at DESC, scan_id DESC
                LIMIT 1
                """,
                (token_key.lower(),),
            ).fetchone()
        if row is None:
            return None
        try:
            baseline = json.loads(str(row["baseline_json"] or "{}"))
        except json.JSONDecodeError:
            baseline = {
                "status": "local_error",
                "error": "historical_baseline_invalid_audit",
            }
        return {
            "scan_id": str(row["scan_id"]),
            "token_key": str(row["token_key"]),
            "started_at": int(row["started_at"]),
            "completed_at": int(row["completed_at"] or 0),
            "query_window": str(row["query_window"] or ""),
            "baseline_status": str(row["baseline_status"] or ""),
            "baseline_anomaly": bool(row["baseline_anomaly"]),
            "historical_baseline": (
                baseline if isinstance(baseline, dict) else {}
            ),
        }

    @staticmethod
    def _trim_scan_audit(
        conn: sqlite3.Connection, token_key: str
    ) -> None:
        conn.execute(
            """
            DELETE FROM watch_scan_runs
            WHERE scan_id IN (
                SELECT scan_id FROM watch_scan_runs
                WHERE token_key=?
                ORDER BY started_at DESC, scan_id DESC
                LIMIT -1 OFFSET 100
            )
            """,
            (token_key,),
        )
        conn.execute(
            """
            DELETE FROM watch_scan_runs
            WHERE scan_id IN (
                SELECT scan_id FROM watch_scan_runs
                ORDER BY started_at DESC, scan_id DESC
                LIMIT -1 OFFSET 5000
            )
            """
        )

    def status_summary(self, *, now: int | None = None) -> dict[str, object]:
        timestamp = int(now if now is not None else self.clock())
        with self.connect_existing() as conn:
            if conn is None:
                return {
                    "automation_db_exists": False,
                    "status": "not_initialized",
                }
            counts = {
                str(row["status"]): int(row["count"])
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS count FROM token_registry "
                    "GROUP BY status"
                ).fetchall()
            }
            watches = {
                str(row["status"]): int(row["count"])
                for row in conn.execute(
                    "SELECT status, COUNT(*) AS count FROM watch_items "
                    "GROUP BY status"
                ).fetchall()
            }
            due = int(
                conn.execute(
                    """
                    SELECT COUNT(*) FROM watch_items
                    WHERE status='active' AND next_scan_at<=?
                      AND expires_at>?
                      AND (lease_until IS NULL OR lease_until<=?)
                    """,
                    (timestamp, timestamp, timestamp),
                ).fetchone()[0]
            )
            unresolved = int(
                conn.execute(
                    "SELECT COUNT(*) FROM unresolved_signals "
                    "WHERE status='open'"
                ).fetchone()[0]
            )
            bridge = conn.execute(
                "SELECT last_signal_ts, last_signal_id FROM bridge_state "
                "WHERE source_key='main_signals'"
            ).fetchone()
            scan = conn.execute(
                "SELECT completed_at, status FROM watch_scan_runs "
                "ORDER BY completed_at DESC LIMIT 1"
            ).fetchone()
            return {
                "automation_db_exists": True,
                "status": "ok",
                "registry_pending": counts.get("pending", 0),
                "registry_verified": counts.get("verified", 0),
                "active_watch_items": watches.get("active", 0),
                "paused_watch_items": watches.get("paused", 0),
                "expired_watch_items": watches.get("expired", 0),
                "due_watch_items": due,
                "unresolved_signals": unresolved,
                "last_bridge_ts": int(bridge["last_signal_ts"]) if bridge else 0,
                "last_bridge_signal_id": (
                    int(bridge["last_signal_id"]) if bridge else 0
                ),
                "last_scan_at": int(scan["completed_at"] or 0) if scan else 0,
                "last_scan_status": str(scan["status"] or "") if scan else "",
            }

    def doctor(self, *, now: int | None = None) -> dict[str, object]:
        timestamp = int(now if now is not None else self.clock())
        if not self.path.exists():
            return {"status": "not_initialized", "integrity": "not_initialized"}
        integrity = self.integrity_check_existing(self.path)
        issues: list[str] = []
        with self.connect_existing() as conn:
            if conn is None:
                return {
                    "status": "not_initialized",
                    "integrity": "not_initialized",
                }
            ambiguous = conn.execute(
                """
                SELECT market_symbol FROM token_registry
                WHERE status='verified'
                GROUP BY market_symbol, chain_id
                HAVING COUNT(*)>1
                   AND SUM(CASE WHEN is_primary=1 THEN 1 ELSE 0 END)<>1
                """
            ).fetchall()
            if ambiguous:
                issues.append("ambiguous_primary")
            orphan = conn.execute(
                """
                SELECT COUNT(*) FROM watch_items w
                LEFT JOIN token_registry r ON r.token_key=w.token_key
                WHERE r.token_key IS NULL
                """
            ).fetchone()[0]
            if orphan:
                issues.append("orphan_watch_item")
            stale_lease = conn.execute(
                "SELECT COUNT(*) FROM watch_items "
                "WHERE lease_until IS NOT NULL AND lease_until<?",
                (timestamp - 86400,),
            ).fetchone()[0]
            if stale_lease:
                issues.append("stale_lease")
        return {
            "status": (
                "ok" if integrity == "ok" and not issues else "failed"
            ),
            "integrity": integrity,
            "issues": issues,
        }
