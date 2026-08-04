from __future__ import annotations

import shutil
import sqlite3
import time
from pathlib import Path
from typing import Any

from config import Settings
from shared.storage import JsonStore


GENERATED_ROOT_ARTIFACT_PATTERNS = (
    "PROJECT_CURRENT_SUMMARY.md",
    "UPGRADE_*.md",
    "*_REPORT.md",
    "*_SUMMARY.md",
    "*_REPORT.txt",
    "*_SUMMARY.txt",
)


def _inside_skipped_dir(path: Path) -> bool:
    skipped = {".git", ".venv", "data"}
    return any(part in skipped for part in path.parts)


def _remove_file(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def _remove_tree(path: Path) -> bool:
    try:
        shutil.rmtree(path)
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def _int_value(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _prune_json_list_by_ts(
    store: JsonStore,
    path: Path,
    limit: int,
    retention_days: int | None = None,
    allowed_template_ids: frozenset[str] | None = None,
) -> dict[str, Any]:
    records = store.load(path, [])
    if not isinstance(records, list):
        return {"path": str(path), "before": 0, "after": 0, "changed": False, "reason": "not_list"}
    before = len(records)
    retained = records
    if retention_days is not None and retention_days > 0:
        cutoff = int(time.time()) - retention_days * 86400
        retained = [
            record for record in retained
            if not isinstance(record, dict) or _int_value(record.get("ts"), int(time.time())) >= cutoff
        ]
    if allowed_template_ids is not None:
        retained = [
            record for record in retained
            if not isinstance(record, dict)
            or not str(record.get("template_id") or "")
            or str(record.get("template_id") or "") in allowed_template_ids
        ]
    if limit > 0 and len(retained) > limit:
        retained = retained[-limit:]
    changed = len(retained) != before
    if changed:
        store.save(path, retained)
    return {"path": str(path), "before": before, "after": len(retained), "changed": changed}


def _compact_launch_history(
    store: JsonStore,
    path: Path,
    limit: int,
) -> dict[str, Any]:
    records = store.load(path, [])
    if not isinstance(records, list):
        return {
            "path": str(path),
            "before": 0,
            "after": 0,
            "changed": False,
            "reason": "not_list",
        }
    before_count = len(records)
    before_bytes = path.stat().st_size if path.exists() else 0
    compacted: list[Any] = []
    compacted_items = 0
    transient = {
        "chart_png_bytes",
        "launch_lifecycle",
        "launch_package",
        "price_action_analysis",
    }
    for record in records:
        if not isinstance(record, dict):
            compacted.append(record)
            continue
        next_record = dict(record)
        items = record.get("items")
        if isinstance(items, list):
            next_items: list[Any] = []
            for item in items:
                if not isinstance(item, dict):
                    next_items.append(item)
                    continue
                next_item = {
                    key: value
                    for key, value in item.items()
                    if key not in transient
                }
                if len(next_item) != len(item):
                    compacted_items += 1
                next_items.append(next_item)
            next_record["items"] = next_items
        compacted.append(next_record)
    if limit > 0 and len(compacted) > limit:
        compacted = compacted[-limit:]
    changed = compacted_items > 0 or len(compacted) != before_count
    if changed:
        store.save(path, compacted)
    after_bytes = path.stat().st_size if path.exists() else 0
    return {
        "path": str(path),
        "before": before_count,
        "after": len(compacted),
        "changed": changed,
        "compacted_items": compacted_items,
        "before_bytes": before_bytes,
        "after_bytes": after_bytes,
        "freed_bytes": max(0, before_bytes - after_bytes),
    }


def _compact_launch_state(
    settings: Settings,
    store: JsonStore,
) -> dict[str, Any]:
    from radars.common import compact_launch_state_records

    state = store.load(settings.launch_state_path, {})
    if not isinstance(state, dict):
        return {
            "path": str(settings.launch_state_path),
            "records": 0,
            "changed": False,
            "reason": "not_dict",
        }
    before_bytes = (
        settings.launch_state_path.stat().st_size
        if settings.launch_state_path.exists()
        else 0
    )
    compacted, changed_records = compact_launch_state_records(state)
    if changed_records:
        store.save(settings.launch_state_path, compacted)
    after_bytes = (
        settings.launch_state_path.stat().st_size
        if settings.launch_state_path.exists()
        else 0
    )
    return {
        "path": str(settings.launch_state_path),
        "records": len(compacted),
        "changed": changed_records > 0,
        "compacted_records": changed_records,
        "before_bytes": before_bytes,
        "after_bytes": after_bytes,
        "freed_bytes": max(0, before_bytes - after_bytes),
    }


def cleanup_generated_root_artifacts(base_dir: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "base_dir": str(base_dir),
        "scanned": 0,
        "deleted": 0,
        "kept": 0,
        "errors": [],
    }
    seen: set[Path] = set()
    for pattern in GENERATED_ROOT_ARTIFACT_PATTERNS:
        for path in base_dir.glob(pattern):
            if path in seen or not path.is_file() or path.parent != base_dir:
                continue
            seen.add(path)
            result["scanned"] += 1
            try:
                if _remove_file(path):
                    result["deleted"] += 1
                else:
                    result["kept"] += 1
            except OSError as exc:
                result["errors"].append(f"{path.name}:{type(exc).__name__}")
    return result


def cleanup_runtime_artifacts(
    settings: Settings,
    store: JsonStore,
    *,
    force: bool = False,
) -> dict[str, Any]:
    now = int(time.time())
    state = store.load(settings.cleanup_state_path, {})
    last_run = int(state.get("last_run_ts", 0)) if isinstance(state, dict) else 0
    interval = max(60, int(settings.cleanup_interval_sec))
    if not force and (not settings.cleanup_enable or now - last_run < interval):
        return {
            "enabled": settings.cleanup_enable,
            "skipped": True,
            "reason": "disabled_or_not_due",
            "next_run_ts": last_run + interval if last_run else now,
        }

    removed_files: list[str] = []
    removed_dirs: list[str] = []
    cutoff_corrupt = now - max(1, int(settings.cleanup_corrupt_retention_days)) * 86400
    cutoff_logs = now - max(1, int(settings.cleanup_log_retention_days)) * 86400

    for pattern in ("*.pyc", "*.pyo"):
        for path in settings.base_dir.rglob(pattern):
            if _inside_skipped_dir(path.relative_to(settings.base_dir)):
                continue
            if _remove_file(path):
                removed_files.append(str(path))

    pycache_dirs = [
        path for path in settings.base_dir.rglob("__pycache__")
        if path.is_dir() and not _inside_skipped_dir(path.relative_to(settings.base_dir))
    ]
    for path in sorted(pycache_dirs, key=lambda item: len(item.parts), reverse=True):
        if _remove_tree(path):
            removed_dirs.append(str(path))

    for path in settings.data_dir.glob("*.tmp"):
        if path.stat().st_mtime <= now - 3600 and _remove_file(path):
            removed_files.append(str(path))
    for path in settings.data_dir.glob(".*.tmp"):
        if path.stat().st_mtime <= now - 3600 and _remove_file(path):
            removed_files.append(str(path))
    for path in settings.data_dir.glob("*.corrupt.*"):
        if path.stat().st_mtime <= cutoff_corrupt and _remove_file(path):
            removed_files.append(str(path))
    for directory in (settings.base_dir, settings.data_dir):
        for path in directory.glob("*.log"):
            if path.stat().st_mtime <= cutoff_logs and _remove_file(path):
                removed_files.append(str(path))

    from shared.signal_text import ACTIVE_SIGNAL_TEMPLATE_IDS

    pruned = [
        _prune_json_list_by_ts(
            store,
            settings.tg_push_history_path,
            max(100, int(settings.tg_push_history_limit)),
            max(1, int(settings.tg_push_history_retention_days)),
            ACTIVE_SIGNAL_TEMPLATE_IDS,
        ),
        _compact_launch_history(
            store,
            settings.launch_watch_history_path,
            max(1, int(settings.launch_watch_history_limit)),
        ),
        _prune_json_list_by_ts(
            store,
            settings.tg_outbox_path,
            1000,
            max(1, int(settings.tg_outbox_retention_days)),
        ),
    ]
    from shared.signal_store import SignalEventStore

    try:
        signal_database = SignalEventStore(settings.signal_events_db_path).prune(
            before_ts=now - max(1, int(settings.signal_events_retention_days)) * 86400,
            max_rows=max(100, int(settings.signal_events_limit)),
        )
        signal_database["status"] = "ok"
    except (OSError, ValueError, sqlite3.Error) as exc:
        signal_database = {"status": "failed", "error": type(exc).__name__}
    generated_root_artifacts = cleanup_generated_root_artifacts(settings.base_dir)
    launch_state_compaction = _compact_launch_state(settings, store)

    result = {
        "enabled": settings.cleanup_enable,
        "skipped": False,
        "ran_at": now,
        "removed_files": removed_files,
        "removed_dirs": removed_dirs,
        "pruned": pruned,
        "signal_database": signal_database,
        "generated_root_artifacts": generated_root_artifacts,
        "launch_state_compaction": launch_state_compaction,
    }
    store.save(settings.cleanup_state_path, {
        "last_run_ts": now,
        "last_run_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "removed_file_count": len(removed_files),
        "removed_dir_count": len(removed_dirs),
    })
    return result
