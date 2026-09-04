"""Zero-write queries over a closed, checkpointed offline Hunter database.

This P1A reader deliberately does not support a live WAL database. It rejects
even empty WAL/SHM sidecars, takes a shared advisory lock on the existing DB,
and uses immutable=1 + mode=ro + query_only. HunterWriter honors the advisory
lock; external writers must be stopped by the caller. Stat/sidecar checks add
change detection, not a guarantee against an uncooperative external writer.
No lock file, directory, database, migration, or repair is ever created here.
"""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Any

from .storage import (
    SCHEMA_VERSION, TABLES, StorageError, _DatabaseFileLock, _safe_path,
    _validate_schema,
)


class ReadOnlyUnavailable(StorageError):
    pass


def _signature(path: Path) -> tuple[int, int, int, int]:
    info = path.stat()
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _require_offline(path: Path) -> None:
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise ReadOnlyUnavailable("offline_checkpointed_database_required")


class HunterReadModel:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    @contextmanager
    def _connect(self):
        path = _safe_path(self.db_path)
        if not path.is_file():
            raise ReadOnlyUnavailable("database_missing")
        lock = _DatabaseFileLock(path, shared=True)
        connection = None
        try:
            try:
                lock.acquire()
            except OSError as exc:
                raise ReadOnlyUnavailable("database_writer_active") from exc
            _require_offline(path)
            before = _signature(path)
            uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
            connection = sqlite3.connect(uri, uri=True, timeout=0, isolation_level=None)
            connection.execute("PRAGMA query_only=ON")
            _validate_schema(connection)
            yield connection
            _require_offline(path)
            if _signature(path) != before:
                raise ReadOnlyUnavailable("database_changed_during_offline_read")
        finally:
            if connection is not None:
                connection.close()
            lock.release()

    def status(self) -> dict[str, Any]:
        """Report unavailable/missing without initializing any runtime state."""
        try:
            with self._connect() as connection:
                counts = {name: connection.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0] for name in TABLES}
                last_bucket = connection.execute("SELECT MAX(end_ms) FROM market_buckets_1m").fetchone()[0]
                last_checkpoint = connection.execute("SELECT MAX(committed_through_ms) FROM ingest_checkpoints WHERE checkpoint_kind='source'").fetchone()[0]
            return {
                "status": "ok", "read_mode": "offline_immutable", "schema_version": SCHEMA_VERSION,
                "counts": counts, "latest_bucket_end_ms": last_bucket,
                "latest_checkpoint_ms": last_checkpoint,
            }
        except (StorageError, OSError, sqlite3.Error) as exc:
            # Reasons are fixed local codes; never echo SQL, paths or payloads.
            known = str(exc) if isinstance(exc, StorageError) else "database_read_failed"
            return {"status": "missing" if known == "database_missing" else "unavailable", "reason": known, "schema_version": None, "counts": {}}

    def list_buckets(self, *, limit: int = 1000, source: str | None = None,
                     exchange: str | None = None, market: str | None = None,
                     instrument_id: str | None = None, start_ms: int | None = None,
                     end_ms: int | None = None) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10_000:
            raise ValueError("limit_must_be_between_1_and_10000")
        predicates = []
        parameters: list[Any] = []
        for key, value in (("source", source), ("exchange", exchange), ("market", market), ("instrument_id", instrument_id)):
            if value is not None:
                predicates.append(f"{key}=?")
                parameters.append(value)
        for field, comparison, value in (("start_ms", ">=", start_ms), ("end_ms", "<=", end_ms)):
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ValueError("invalid_bucket_time_bound")
                predicates.append(f"{field}{comparison}?")
                parameters.append(value)
        where = " WHERE " + " AND ".join(predicates) if predicates else ""
        with self._connect() as connection:
            rows = connection.execute("SELECT record_json FROM market_buckets_1m" + where + " ORDER BY start_ms,source,exchange,market,instrument_id LIMIT ?", (*parameters, limit)).fetchall()
            result = [json.loads(row[0]) for row in rows]
        return result

    def load_baselines(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            result = [json.loads(row[0]) for row in connection.execute("SELECT record_json FROM baseline_state ORDER BY source,exchange,market,instrument_id,feature,window_sec,baseline_version")]
        return result

    def checkpoints(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            result = [json.loads(row[0]) for row in connection.execute("SELECT record_json FROM ingest_checkpoints WHERE checkpoint_kind='source' ORDER BY source,exchange,market,instrument_id")]
        return result

    def instruments(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            result = [json.loads(row[0]) for row in connection.execute("SELECT record_json FROM instruments ORDER BY exchange,market,instrument_id")]
        return result
