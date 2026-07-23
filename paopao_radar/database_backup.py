from __future__ import annotations

import json
import re
import shutil
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings


BACKUP_SET_RE = re.compile(r"^\d{8}T\d{6}Z$")


def _integrity(conn: sqlite3.Connection) -> str:
    row = conn.execute("PRAGMA integrity_check").fetchone()
    return str(row[0] if row else "")


def _backup_one(source_path: Path, destination_path: Path) -> dict[str, Any]:
    source_uri = f"file:{source_path.resolve().as_posix()}?mode=ro"
    with (
        closing(sqlite3.connect(source_uri, uri=True, timeout=30)) as source,
        closing(sqlite3.connect(destination_path, timeout=30)) as destination,
    ):
        source.backup(destination)
        destination.commit()
        backup_integrity = _integrity(destination)
    if backup_integrity.lower() != "ok":
        raise sqlite3.DatabaseError(f"backup integrity failed: {source_path.name}")

    backup_uri = f"file:{destination_path.resolve().as_posix()}?mode=ro"
    with (
        closing(sqlite3.connect(backup_uri, uri=True, timeout=30)) as backup,
        closing(sqlite3.connect(":memory:")) as restored,
    ):
        backup.backup(restored)
        restore_integrity = _integrity(restored)
    if restore_integrity.lower() != "ok":
        raise sqlite3.DatabaseError(f"restore verification failed: {source_path.name}")

    return {
        "source": source_path.name,
        "backup": destination_path.name,
        "bytes": destination_path.stat().st_size,
        "integrity": backup_integrity,
        "restore_verification": restore_integrity,
    }


def _prune_backup_sets(backup_root: Path, *, cutoff_ts: int, keep: Path) -> list[str]:
    root = backup_root.resolve()
    removed: list[str] = []
    for candidate in backup_root.iterdir():
        if (
            candidate == keep
            or not candidate.is_dir()
            or candidate.is_symlink()
            or not BACKUP_SET_RE.fullmatch(candidate.name)
            or candidate.stat().st_mtime >= cutoff_ts
        ):
            continue
        resolved = candidate.resolve()
        if resolved.parent != root:
            continue
        shutil.rmtree(resolved)
        removed.append(candidate.name)
    return removed


def backup_databases(
    settings: Settings,
    *,
    now_ts: int | None = None,
) -> dict[str, Any]:
    """Create consistent SQLite backups and prove they can be restored in memory."""

    now = int(now_ts or time.time())
    stamp = datetime.fromtimestamp(now, timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_root = settings.database_backup_dir
    backup_root.mkdir(parents=True, exist_ok=True)
    final_dir = backup_root / stamp
    incomplete_dir = backup_root / f".incomplete-{stamp}"
    if final_dir.exists() or incomplete_dir.exists():
        raise FileExistsError(f"backup set already exists: {stamp}")
    incomplete_dir.mkdir()

    sources = (
        settings.signal_events_db_path,
        settings.market_snapshots_db_path,
        settings.realtime_features_db_path,
        settings.news_events_db_path,
    )
    databases: list[dict[str, Any]] = []
    skipped: list[str] = []
    try:
        for source in sources:
            if not source.exists():
                skipped.append(source.name)
                continue
            databases.append(_backup_one(source, incomplete_dir / source.name))
        manifest = {
            "status": "ok" if databases else "empty",
            "created_at": now,
            "created_at_iso": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "databases": databases,
            "skipped": skipped,
        }
        (incomplete_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        incomplete_dir.rename(final_dir)
    except Exception:
        shutil.rmtree(incomplete_dir, ignore_errors=True)
        raise

    retention_days = max(1, int(settings.database_backup_retention_days))
    removed = _prune_backup_sets(
        backup_root,
        cutoff_ts=now - retention_days * 86_400,
        keep=final_dir,
    )
    return {
        **manifest,
        "backup_set": str(final_dir),
        "retention_days": retention_days,
        "pruned_sets": removed,
    }


__all__ = ["backup_databases"]
