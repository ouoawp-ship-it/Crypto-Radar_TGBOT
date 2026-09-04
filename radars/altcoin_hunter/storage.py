"""Explicit single-writer persistence for the isolated Hunter foundation.

Constructors never create directories, databases, migrations, or lock files.
All records use caller-supplied event times; decimal market values stay strings
inside canonical JSON. No production store or Telegram module is imported.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any, Iterable, Mapping

from .migrations import SCHEMA_VERSION, scripts


TABLES = (
    "schema_migrations", "instruments", "universe_history", "market_buckets_1m",
    "baseline_state", "ingest_checkpoints", "health_rollups_1m",
)
_LEGACY_NAMES = frozenset({
    "signals.db", "market_snapshots.db", "realtime_features.db",
    "binance_coordination.db", "binance_coordination.test.db",
    "jobs.db", "onchain_signals.db",
})
_IDENTITY = ("source", "exchange", "market", "instrument_id")
_ADVISORY_LOCK_OFFSET = 0x7FFF_FFFF_0000


class StorageError(RuntimeError):
    pass


class MigrationError(StorageError):
    pass


@dataclass(frozen=True)
class CommitReceipt:
    batch_id: str
    schema_version: int
    bucket_count: int
    checkpoint_count: int
    health_count: int
    baseline_count: int
    already_committed: bool = False


@dataclass(frozen=True)
class CommittedBatch:
    receipt: CommitReceipt
    buckets: tuple[Any, ...]

    @property
    def batch_id(self) -> str:
        return self.receipt.batch_id


@dataclass(frozen=True)
class RegistryResult:
    inserted: int
    updated: int
    unchanged: int
    history_count: int


def _record(value: Any) -> dict[str, Any]:
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    if not isinstance(value, Mapping):
        raise StorageError("record_must_be_mapping")
    # This validates JSON-safe and finite values, and detaches mutable input.
    return json.loads(_json(dict(value)))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _text(record: Mapping[str, Any], key: str) -> str:
    value = record.get(key)
    if not isinstance(value, str) or not value.strip():
        raise StorageError(f"missing_or_invalid_{key}")
    return value


def _integer(value: Any, key: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise StorageError(f"invalid_{key}")
    return value


def _safe_path(value: str | Path) -> Path:
    from .configuration import validate_database_path

    try:
        path = validate_database_path(value)
    except ValueError as exc:
        raise StorageError("invalid_database_path") from exc
    if path is None:
        raise StorageError("explicit_database_path_required")
    if path.name.lower() in _LEGACY_NAMES:
        raise StorageError("legacy_database_path_forbidden")
    if path.is_symlink():
        raise StorageError("database_symlink_forbidden")
    return path


class _DatabaseFileLock:
    """Non-blocking advisory lock on the existing DB, never a sidecar file.

    This coordinates this package's writer and offline reader. External SQLite
    writers do not honor it and are outside the offline reader contract.
    """

    def __init__(self, path: Path, *, shared: bool = False) -> None:
        self.path = path
        self.shared = shared
        self.handle = None

    def acquire(self) -> None:
        handle = self.path.open("rb")
        try:
            if os.name == "posix":
                import fcntl

                fcntl.flock(handle.fileno(), (fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
            elif os.name == "nt":
                import msvcrt

                # Windows byte locks are mandatory: avoid SQLite's header,
                # data pages, and its own reserved lock-byte range.
                handle.seek(_ADVISORY_LOCK_OFFSET)
                mode = msvcrt.LK_NBRLCK if self.shared else msvcrt.LK_NBLCK
                msvcrt.locking(handle.fileno(), mode, 1)
            else:
                raise StorageError("database_file_lock_unsupported")
        except BaseException:
            handle.close()
            raise
        self.handle = handle

    def release(self) -> None:
        handle, self.handle = self.handle, None
        if handle is None:
            return
        try:
            if os.name == "posix":
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            elif os.name == "nt":
                import msvcrt

                handle.seek(_ADVISORY_LOCK_OFFSET)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            handle.close()


def _migration_definitions() -> tuple[tuple[int, str, str], ...]:
    return tuple((version, sql, hashlib.sha256(sql.encode("utf-8")).hexdigest()) for version, sql in scripts())


def _validate_schema(connection: sqlite3.Connection, definitions=None) -> None:
    definitions = definitions or _migration_definitions()
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "schema_migrations" not in tables:
        raise MigrationError("explicit_migration_required")
    versions = dict(connection.execute("SELECT version, checksum FROM schema_migrations"))
    expected = {version: checksum for version, _sql, checksum in definitions}
    if any(version > SCHEMA_VERSION for version in versions):
        raise MigrationError("database_schema_newer_than_supported")
    if set(versions) != set(expected):
        raise MigrationError("schema_migration_history_incomplete")
    if any(versions[version] != expected[version] for version in versions):
        raise MigrationError("schema_migration_checksum_mismatch")
    if tables != set(TABLES):
        raise MigrationError("database_schema_tables_mismatch")


def _statements(sql: str):
    pending = ""
    for line in sql.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            yield pending
            pending = ""
    if pending.strip():
        raise MigrationError("migration_sql_incomplete")


def migrate(db_path: str | Path, *, busy_timeout_ms: int = 1000, applied_at_ms: int = 0) -> dict[str, Any]:
    """Explicitly initialize only the requested new database; never mkdir.

    A repeated call validates checksums and has no schema/data mutation. The
    caller owns creation of any parent directory and any deployment approval.
    """
    path = _safe_path(db_path)
    timeout = _integer(busy_timeout_ms, "busy_timeout_ms", minimum=1)
    if timeout > 30_000:
        raise StorageError("busy_timeout_exceeds_limit")
    _integer(applied_at_ms, "applied_at_ms")
    if not path.parent.is_dir():
        raise StorageError("database_parent_missing")
    definitions = _migration_definitions()
    # Explicit migration alone may create the DB. The file is not a lock file.
    if not path.exists():
        with path.open("xb"):
            pass
    lock = _DatabaseFileLock(path)
    connection = None
    try:
        lock.acquire()
        connection = sqlite3.connect(str(path), timeout=timeout / 1000, isolation_level=None)
        connection.execute(f"PRAGMA busy_timeout={timeout}")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables:
            _validate_schema(connection, definitions)
            return {"schema_version": SCHEMA_VERSION, "applied": [], "status": "current"}
        connection.execute("BEGIN IMMEDIATE")
        for version, sql, checksum in definitions:
            for statement in _statements(sql):
                connection.execute(statement)
            connection.execute("INSERT INTO schema_migrations VALUES (?, ?, ?)", (version, checksum, applied_at_ms))
        connection.commit()
        _validate_schema(connection, definitions)
        return {"schema_version": SCHEMA_VERSION, "applied": [item[0] for item in definitions], "status": "migrated"}
    except BaseException:
        if connection is not None and connection.in_transaction:
            connection.rollback()
        raise
    finally:
        if connection is not None:
            connection.close()
        lock.release()


class HunterWriter:
    """An explicitly opened, owner-thread-only, single transaction writer."""

    def __init__(self, db_path: str | Path, *, busy_timeout_ms: int = 1000) -> None:
        self.db_path = Path(db_path)
        self.busy_timeout_ms = busy_timeout_ms
        self._connection: sqlite3.Connection | None = None
        self._owner: int | None = None
        self._lock: _DatabaseFileLock | None = None

    def open(self) -> HunterWriter:
        if self._connection is not None:
            self._check_owner()
            return self
        path = _safe_path(self.db_path)
        timeout = _integer(self.busy_timeout_ms, "busy_timeout_ms", minimum=1)
        if timeout > 30_000:
            raise StorageError("busy_timeout_exceeds_limit")
        if not path.is_file():
            raise StorageError("explicit_migration_required")
        lock = _DatabaseFileLock(path)
        connection = None
        try:
            lock.acquire()
            uri = path.resolve().as_uri() + "?mode=rw"
            connection = sqlite3.connect(uri, uri=True, timeout=timeout / 1000, isolation_level=None)
            _validate_schema(connection)
            connection.execute(f"PRAGMA busy_timeout={timeout}")
            if connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() != "wal":
                raise StorageError("wal_mode_unavailable")
            connection.execute("PRAGMA synchronous=FULL")
        except BaseException:
            if connection is not None:
                connection.close()
            lock.release()
            raise
        self._connection = connection
        self._owner = threading.get_ident()
        self._lock = lock
        return self

    def _check_owner(self) -> sqlite3.Connection:
        if self._connection is None:
            raise StorageError("writer_not_open")
        if self._owner != threading.get_ident():
            raise StorageError("writer_thread_mismatch")
        return self._connection

    def _commit_transaction(self, connection: sqlite3.Connection) -> None:
        """Dedicated commit boundary also allows deterministic fault injection."""
        connection.commit()

    @contextmanager
    def _transaction(self):
        connection = self._check_owner()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            self._commit_transaction(connection)
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise

    def close(self) -> None:
        if self._connection is None:
            return
        connection = self._check_owner()
        try:
            if connection.in_transaction:
                connection.rollback()
            row = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if row and row[0]:
                raise StorageError("checkpoint_busy_offline_read_unavailable")
        finally:
            connection.close()
            self._connection = None
            self._owner = None
            if self._lock is not None:
                self._lock.release()
                self._lock = None

    def __enter__(self) -> HunterWriter:
        return self.open()

    def __exit__(self, *_exc) -> None:
        self.close()

    def commit_batch(self, pending: Any, *, baseline_states: Iterable[Any] = ()) -> CommittedBatch:
        from .aggregation import MinuteBucket

        batch_id = _text({"batch_id": pending.batch_id}, "batch_id")
        bucket_records = tuple(_record(item) for item in pending.buckets)
        try:
            buckets = tuple(MinuteBucket.from_dict(item) for item in bucket_records)
        except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
            raise StorageError("invalid_minute_bucket") from exc
        checkpoint_input = pending.checkpoints
        if isinstance(checkpoint_input, Mapping):
            checkpoint_input = checkpoint_input.values()
        checkpoints = tuple(_record(item) for item in checkpoint_input)
        health = tuple(_record(item) for item in getattr(pending, "health_rollups", getattr(pending, "health", ())))
        baselines = tuple(_record(item) for item in baseline_states)
        latest_buckets: dict[tuple[str, ...], Mapping[str, Any]] = {}
        seen_buckets: set[tuple[Any, ...]] = set()
        for record in bucket_records:
            identity = tuple(_text(record, key) for key in _IDENTITY)
            bucket_key = (*identity, record["start_ms"])
            if bucket_key in seen_buckets:
                raise StorageError("duplicate_bucket_in_batch")
            seen_buckets.add(bucket_key)
            if identity not in latest_buckets or record["end_ms"] > latest_buckets[identity]["end_ms"]:
                latest_buckets[identity] = record
        seen_checkpoints: set[tuple[str, ...]] = set()
        for record in checkpoints:
            identity = tuple(_text(record, key) for key in _IDENTITY)
            bucket = latest_buckets.get(identity)
            through = _integer(record.get("committed_through_ms"), "committed_through_ms")
            if identity in seen_checkpoints or bucket is None or through != bucket["end_ms"]:
                raise StorageError("checkpoint_does_not_match_batch")
            if record.get("connection_epoch") != bucket["connection_epoch"]:
                raise StorageError("checkpoint_epoch_mismatch")
            seen_checkpoints.add(identity)
        if seen_checkpoints != set(latest_buckets):
            raise StorageError("bucket_checkpoint_missing")
        fingerprint = _digest({"buckets": bucket_records, "checkpoints": checkpoints, "health": health, "baselines": baselines})
        receipt = CommitReceipt(batch_id, SCHEMA_VERSION, len(buckets), len(checkpoints), len(health), len(baselines))
        marker_key = "batch:" + batch_id
        with self._transaction() as connection:
            previous = connection.execute("SELECT content_checksum, record_json FROM ingest_checkpoints WHERE checkpoint_key=?", (marker_key,)).fetchone()
            if previous is not None:
                if previous[0] != fingerprint:
                    raise StorageError("batch_id_payload_mismatch")
                return CommittedBatch(CommitReceipt(**{**json.loads(previous[1]), "already_committed": True}), buckets)
            for record in bucket_records:
                identity = tuple(_text(record, key) for key in _IDENTITY)
                start = _integer(record.get("start_ms"), "start_ms")
                end = _integer(record.get("end_ms"), "end_ms")
                if start % 60_000 or end != start + 60_000:
                    raise StorageError("invalid_minute_bucket_boundaries")
                checksum = _digest(record)
                old = connection.execute("SELECT content_checksum FROM market_buckets_1m WHERE source=? AND exchange=? AND market=? AND instrument_id=? AND start_ms=?", (*identity, start)).fetchone()
                if old is not None:
                    if old[0] != checksum:
                        raise StorageError("committed_bucket_conflict")
                    continue
                connection.execute("INSERT INTO market_buckets_1m VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (*identity, _text(record, "symbol"), start, end, _integer(record.get("connection_epoch"), "connection_epoch", minimum=-1), _text(record, "quality_status"), checksum, _json(record)))
            for record in checkpoints:
                identity = tuple(_text(record, key) for key in _IDENTITY)
                through = _integer(record.get("committed_through_ms"), "committed_through_ms")
                key = "source:" + _digest(identity)
                old = connection.execute("SELECT committed_through_ms FROM ingest_checkpoints WHERE checkpoint_key=?", (key,)).fetchone()
                if old is not None and through < old[0]:
                    raise StorageError("checkpoint_regression")
                connection.execute("INSERT INTO ingest_checkpoints VALUES (?, 'source', ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(checkpoint_key) DO UPDATE SET committed_through_ms=excluded.committed_through_ms,batch_id=excluded.batch_id,content_checksum=excluded.content_checksum,record_json=excluded.record_json", (key, *identity, through, batch_id, _digest(record), _json(record)))
            for record in health:
                record = self._health_record(record)
                identity = tuple(record[key] for key in _IDENTITY)
                minute = _integer(record.get("minute_ms"), "minute_ms")
                if minute % 60_000:
                    raise StorageError("invalid_health_minute")
                old = connection.execute("SELECT record_json FROM health_rollups_1m WHERE source=? AND exchange=? AND market=? AND instrument_id=? AND minute_ms=?", (*identity, minute)).fetchone()
                if old is not None:
                    previous_health = json.loads(old[0])
                    counters = dict(previous_health["counters"])
                    for key, value in record["counters"].items():
                        counters[key] = counters.get(key, 0) + value
                    changes = {_json(item): item for item in (*previous_health["status_changes"], *record["status_changes"])}
                    record = {
                        **record, "counters": counters,
                        "max_processing_latency_ms": max(previous_health["max_processing_latency_ms"], record["max_processing_latency_ms"]),
                        "max_event_latency_ms": max(previous_health.get("max_event_latency_ms", 0), record["max_event_latency_ms"]),
                        "max_queue_depth": max(previous_health.get("max_queue_depth", 0), record["max_queue_depth"]),
                        "max_checkpoint_lag_ms": max(previous_health.get("max_checkpoint_lag_ms", 0), record["max_checkpoint_lag_ms"]),
                        "status_changes": sorted(changes.values(), key=lambda item: (item["at_ms"], item["status"], item["reason"])),
                    }
                connection.execute("INSERT INTO health_rollups_1m VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(source,exchange,market,instrument_id,minute_ms) DO UPDATE SET content_checksum=excluded.content_checksum,record_json=excluded.record_json", (*identity, minute, _digest(record), _json(record)))
            self._save_baselines(connection, baselines)
            committed_through = max((record["end_ms"] for record in bucket_records), default=0)
            connection.execute("INSERT INTO ingest_checkpoints VALUES (?, 'batch', '', '', '', '', ?, ?, ?, ?)", (marker_key, committed_through, batch_id, fingerprint, _json(asdict(receipt))))
        return CommittedBatch(receipt, buckets)

    @staticmethod
    def _health_record(value: Mapping[str, Any]) -> dict[str, Any]:
        record = dict(value)
        for key in _IDENTITY:
            field = record.get(key, "")
            if not isinstance(field, str):
                raise StorageError("invalid_health_identity")
            record[key] = field or "*"
        counters = record.get("counters", {})
        if not isinstance(counters, Mapping):
            raise StorageError("invalid_health_counters")
        record["counters"] = {str(key): _integer(value, "health_counter") for key, value in counters.items()}
        record["max_processing_latency_ms"] = _integer(record.get("max_processing_latency_ms", 0), "processing_latency_ms")
        record["max_event_latency_ms"] = _integer(record.get("max_event_latency_ms", 0), "event_latency_ms")
        record["max_queue_depth"] = _integer(record.get("max_queue_depth", 0), "queue_depth")
        record["max_checkpoint_lag_ms"] = _integer(record.get("max_checkpoint_lag_ms", 0), "checkpoint_lag_ms")
        changes = record.get("status_changes", [])
        if not isinstance(changes, list):
            raise StorageError("invalid_health_status_changes")
        for item in changes:
            if not isinstance(item, Mapping):
                raise StorageError("invalid_health_status_change")
            _integer(item.get("at_ms"), "status_change_at_ms")
            _text(item, "status")
            if not isinstance(item.get("reason"), str):
                raise StorageError("invalid_health_status_reason")
        record["status_changes"] = changes
        return record

    @staticmethod
    def _save_baselines(connection: sqlite3.Connection, records: Iterable[Mapping[str, Any]]) -> None:
        from .baselines import BaselineKey, RollingBaseline

        for record in records:
            try:
                BaselineKey(**{name: record[name] for name in BaselineKey.__dataclass_fields__})
                RollingBaseline.restore(record)
            except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
                raise StorageError("invalid_baseline_state") from exc
            identity = tuple(_text(record, key) for key in (*_IDENTITY, "feature"))
            window = _integer(record.get("window_sec"), "window_sec", minimum=1)
            version = _text(record, "baseline_version")
            updated = _integer(record.get("updated_at_ms", 0), "updated_at_ms")
            serialized = _json(record)
            old = connection.execute("SELECT updated_at_ms,record_json FROM baseline_state WHERE source=? AND exchange=? AND market=? AND instrument_id=? AND feature=? AND window_sec=? AND baseline_version=?", (*identity, window, version)).fetchone()
            if old is not None:
                if updated < old[0]:
                    raise StorageError("baseline_checkpoint_regression")
                if updated == old[0]:
                    if serialized != old[1]:
                        raise StorageError("baseline_checkpoint_payload_conflict")
                    continue
                if json.loads(old[1]).get("config_hash") != record.get("config_hash"):
                    raise StorageError("baseline_policy_change_requires_new_versioned_run")
            connection.execute("INSERT INTO baseline_state VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(source,exchange,market,instrument_id,feature,window_sec,baseline_version) DO UPDATE SET updated_at_ms=excluded.updated_at_ms,record_json=excluded.record_json", (*identity, window, version, updated, serialized))

    def save_baselines(self, records: Iterable[Any]) -> None:
        records = tuple(_record(item) for item in records)
        with self._transaction() as connection:
            self._save_baselines(connection, records)

    def load_baselines(self) -> list[dict[str, Any]]:
        connection = self._check_owner()
        return [json.loads(row[0]) for row in connection.execute("SELECT record_json FROM baseline_state ORDER BY source,exchange,market,instrument_id,feature,window_sec,baseline_version")]

    def checkpoints(self) -> list[dict[str, Any]]:
        connection = self._check_owner()
        return [json.loads(row[0]) for row in connection.execute("SELECT record_json FROM ingest_checkpoints WHERE checkpoint_kind='source' ORDER BY source,exchange,market,instrument_id")]

    def read_counts(self) -> dict[str, int]:
        connection = self._check_owner()
        return {table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in TABLES}

    def upsert_instruments(self, records: Iterable[Any], *, observed_at_ms: int | None = None) -> RegistryResult:
        from .universe import instrument_from_dict

        detached = tuple(_record(item) for item in records)
        inserted = updated = unchanged = 0
        with self._transaction() as connection:
            for record in detached:
                if "effective_at_ms" not in record and observed_at_ms is not None:
                    record["effective_at_ms"] = observed_at_ms
                try:
                    record = instrument_from_dict(record).to_dict()
                except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
                    raise StorageError("invalid_instrument_record") from exc
                identity = tuple(_text(record, key) for key in ("exchange", "market", "instrument_id"))
                effective = _integer(record.get("effective_at_ms", observed_at_ms), "effective_at_ms")
                record["effective_at_ms"] = effective
                checksum = _digest({key: value for key, value in record.items() if key != "effective_at_ms"})
                old = connection.execute("SELECT content_checksum,effective_at_ms FROM instruments WHERE exchange=? AND market=? AND instrument_id=?", identity).fetchone()
                if old is not None and old[0] == checksum:
                    unchanged += 1
                    continue
                if old is not None and effective < old[1]:
                    raise StorageError("instrument_effective_time_regression")
                values = (*identity, _text(record, "symbol"), record.get("canonical_asset_id"), _text(record, "eligibility_status"), _text(record, "listing_stage"), _text(record, "activity_tier"), _text(record, "sampling_priority"), effective, _integer(record.get("metadata_version"), "metadata_version", minimum=1), checksum, _json(record))
                connection.execute("INSERT INTO instruments VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(exchange,market,instrument_id) DO UPDATE SET symbol=excluded.symbol,canonical_asset_id=excluded.canonical_asset_id,eligibility_status=excluded.eligibility_status,listing_stage=excluded.listing_stage,activity_tier=excluded.activity_tier,sampling_priority=excluded.sampling_priority,effective_at_ms=excluded.effective_at_ms,metadata_version=excluded.metadata_version,content_checksum=excluded.content_checksum,record_json=excluded.record_json", values)
                previous = old[0] if old is not None else None
                change_id = _digest([*identity, effective, previous, checksum])
                connection.execute("INSERT INTO universe_history VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (change_id, *identity, effective, previous, checksum, _json(record)))
                if old is None:
                    inserted += 1
                else:
                    updated += 1
        return RegistryResult(inserted, updated, unchanged, inserted + updated)
