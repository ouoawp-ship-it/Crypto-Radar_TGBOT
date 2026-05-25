from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

from config import Settings
from storage import JsonStore


LEGACY_STATE_FILES = {
    "bn_signal_history.json": "bn_signal_history.json",
    "fr_snapshot.json": "funding_snapshot.legacy.json",
    "heat_history.json": "heat_history.legacy.json",
}


def legacy_state_report(settings: Settings) -> list[dict[str, Any]]:
    report: list[dict[str, Any]] = []
    for source_name, target_name in LEGACY_STATE_FILES.items():
        source = settings.base_dir / source_name
        target = settings.data_dir / target_name
        source_exists = source.exists()
        target_exists = target.exists()
        if not source_exists:
            action = "missing"
        elif target_exists:
            action = "skip_target_exists"
        else:
            action = "copy_available"
        report.append({
            "source": str(source),
            "target": str(target),
            "source_exists": source_exists,
            "target_exists": target_exists,
            "source_size": source.stat().st_size if source_exists else 0,
            "target_size": target.stat().st_size if target_exists else 0,
            "action": action,
        })
    return report


def migrate_legacy_state(settings: Settings, apply: bool = False) -> dict[str, Any]:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    actions: list[dict[str, Any]] = []
    for item in legacy_state_report(settings):
        action = dict(item)
        if item["action"] == "copy_available" and apply:
            source = Path(item["source"])
            target = Path(item["target"])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            action["action"] = "copied"
            action["target_exists"] = True
            action["target_size"] = target.stat().st_size
        elif item["action"] == "copy_available":
            action["action"] = "dry_run_copy_available"
        actions.append(action)
    return {
        "applied": apply,
        "actions": actions,
    }


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
    if limit > 0 and len(retained) > limit:
        retained = retained[-limit:]
    changed = len(retained) != before
    if changed:
        store.save(path, retained)
    return {"path": str(path), "before": before, "after": len(retained), "changed": changed}


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

    pruned = [
        _prune_json_list_by_ts(
            store,
            settings.tg_push_history_path,
            max(100, int(settings.tg_push_history_limit)),
            max(1, int(settings.tg_push_history_retention_days)),
        ),
        _prune_json_list_by_ts(
            store,
            settings.launch_watch_history_path,
            max(1, int(settings.launch_watch_history_limit)),
            None,
        ),
    ]

    result = {
        "enabled": settings.cleanup_enable,
        "skipped": False,
        "ran_at": now,
        "removed_files": removed_files,
        "removed_dirs": removed_dirs,
        "pruned": pruned,
    }
    store.save(settings.cleanup_state_path, {
        "last_run_ts": now,
        "last_run_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "removed_file_count": len(removed_files),
        "removed_dir_count": len(removed_dirs),
    })
    return result
