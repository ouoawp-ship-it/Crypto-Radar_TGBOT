from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
from typing import Any, Iterable


SCHEMA_VERSION = 1
INVENTORY_NAME = "runtime-data-inventory.json"
SQLITE_SUFFIXES = {".db", ".sqlite", ".sqlite3"}
SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


class RuntimeDataError(RuntimeError):
    pass


def _resolved(path: Path, *, must_exist: bool) -> Path:
    return path.resolve(strict=must_exist)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_sqlite_sidecar(path: Path) -> bool:
    for suffix in SQLITE_SIDECAR_SUFFIXES:
        if not path.name.endswith(suffix):
            continue
        database_name = path.name[: -len(suffix)]
        return Path(database_name).suffix.lower() in SQLITE_SUFFIXES
    return False


def _sqlite_integrity(path: Path) -> str:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=30)) as connection:
        row = connection.execute("PRAGMA integrity_check").fetchone()
    return str(row[0] if row else "")


def _backup_sqlite(source: Path, destination: Path) -> None:
    source_uri = f"file:{source.resolve().as_posix()}?mode=ro"
    with (
        closing(
            sqlite3.connect(source_uri, uri=True, timeout=30)
        ) as source_connection,
        closing(sqlite3.connect(destination, timeout=30)) as destination_connection,
    ):
        source_connection.backup(destination_connection)
        destination_connection.commit()
        mode = destination_connection.execute(
            "PRAGMA journal_mode=DELETE"
        ).fetchone()
        destination_connection.commit()
        if str(mode[0] if mode else "").lower() != "delete":
            raise RuntimeDataError("runtime_sqlite_backup_journal_mode_failed")
    for suffix in SQLITE_SIDECAR_SUFFIXES:
        sidecar = Path(f"{destination}{suffix}")
        if sidecar.is_symlink():
            raise RuntimeDataError("runtime_sqlite_backup_sidecar_invalid")
        if sidecar.exists():
            sidecar.unlink()
    if _sqlite_integrity(destination).lower() != "ok":
        raise RuntimeDataError("runtime_sqlite_backup_integrity_failed")


def _excluded(path: Path, excluded_root: Path | None) -> bool:
    return excluded_root is not None and (
        path == excluded_root or _is_within(path, excluded_root)
    )


def _walk_regular_tree(
    root: Path,
    *,
    excluded_root: Path | None = None,
) -> tuple[list[Path], list[Path]]:
    directories: list[Path] = []
    files: list[Path] = []
    for current, directory_names, file_names in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        current_path = Path(current)
        retained_directories: list[str] = []
        for name in sorted(directory_names):
            candidate = current_path / name
            if candidate.is_symlink():
                raise RuntimeDataError("runtime_data_symlink_rejected")
            if not candidate.is_dir():
                raise RuntimeDataError("runtime_data_special_entry_rejected")
            resolved_candidate = candidate.resolve(strict=True)
            if _excluded(resolved_candidate, excluded_root):
                continue
            retained_directories.append(name)
            directories.append(candidate)
        directory_names[:] = retained_directories
        for name in sorted(file_names):
            candidate = current_path / name
            if candidate.is_symlink():
                raise RuntimeDataError("runtime_data_symlink_rejected")
            if not candidate.is_file():
                raise RuntimeDataError("runtime_data_special_entry_rejected")
            resolved_candidate = candidate.resolve(strict=True)
            if _excluded(resolved_candidate, excluded_root):
                continue
            files.append(candidate)
    return directories, files


def _relative(path: Path, root: Path) -> str:
    value = path.relative_to(root).as_posix()
    if not value or value.startswith("../"):
        raise RuntimeDataError("runtime_data_relative_path_invalid")
    return value


def backup_runtime_data(
    source: Path,
    destination: Path,
    *,
    exclude_root: Path | None = None,
) -> dict[str, Any]:
    if source.is_symlink():
        raise RuntimeDataError("runtime_data_source_invalid")
    source_root = _resolved(source, must_exist=True)
    if not source_root.is_dir():
        raise RuntimeDataError("runtime_data_source_invalid")
    destination_root = _resolved(destination, must_exist=False)
    excluded = (
        _resolved(exclude_root, must_exist=True)
        if exclude_root is not None and exclude_root.exists()
        else None
    )
    if excluded == source_root:
        raise RuntimeDataError("release_backup_cannot_equal_runtime_data")
    if _is_within(destination_root, source_root) and not _excluded(
        destination_root,
        excluded,
    ):
        raise RuntimeDataError("runtime_backup_destination_must_be_excluded")
    if destination_root.exists() and any(destination_root.iterdir()):
        raise RuntimeDataError("runtime_backup_destination_not_empty")
    destination_root.mkdir(parents=True, exist_ok=True)
    destination_root.chmod(0o700)

    directories, files = _walk_regular_tree(
        source_root,
        excluded_root=excluded,
    )
    inventory_files: list[dict[str, Any]] = []
    sqlite_sidecars = [path for path in files if _is_sqlite_sidecar(path)]
    files = [path for path in files if not _is_sqlite_sidecar(path)]
    for directory in directories:
        target = destination_root / _relative(directory, source_root)
        target.mkdir(parents=True, exist_ok=True)
        target.chmod(0o700)
    for source_path in files:
        relative = _relative(source_path, source_root)
        target = destination_root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        is_sqlite = source_path.suffix.lower() in SQLITE_SUFFIXES
        if is_sqlite:
            _backup_sqlite(source_path, target)
        else:
            source_hash = _sha256(source_path)
            shutil.copy2(source_path, target, follow_symlinks=False)
        if target.is_symlink() or not target.is_file():
            raise RuntimeDataError("runtime_backup_copy_type_invalid")
        target.chmod(0o600)
        destination_hash = _sha256(target)
        if not is_sqlite and source_hash != destination_hash:
            raise RuntimeDataError("runtime_backup_copy_verification_failed")
        inventory_files.append(
            {
                "path": relative,
                "bytes": target.stat().st_size,
                "sha256": destination_hash,
                "kind": "sqlite" if is_sqlite else "file",
            }
        )

    inventory = {
        "schema_version": SCHEMA_VERSION,
        "directories": sorted(
            _relative(path, source_root) for path in directories
        ),
        "files": sorted(inventory_files, key=lambda item: str(item["path"])),
        "sqlite_files": sorted(
            item["path"]
            for item in inventory_files
            if Path(str(item["path"])).suffix.lower() in SQLITE_SUFFIXES
        ),
        "sqlite_sidecars_excluded": sorted(
            _relative(path, source_root) for path in sqlite_sidecars
        ),
    }
    inventory_path = destination_root / INVENTORY_NAME
    inventory_path.write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    inventory_path.chmod(0o600)
    return inventory


def _load_inventory(source_root: Path) -> dict[str, Any]:
    inventory_path = source_root / INVENTORY_NAME
    if not inventory_path.is_file() or inventory_path.is_symlink():
        raise RuntimeDataError("runtime_backup_inventory_missing")
    try:
        payload = json.loads(inventory_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeDataError("runtime_backup_inventory_invalid") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeDataError("runtime_backup_inventory_schema_invalid")
    if not isinstance(payload.get("directories"), list) or not isinstance(
        payload.get("files"),
        list,
    ):
        raise RuntimeDataError("runtime_backup_inventory_invalid")
    return payload


def _safe_relative_path(value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise RuntimeDataError("runtime_backup_inventory_path_invalid")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise RuntimeDataError("runtime_backup_inventory_path_invalid")
    return path


def _ensure_destination_parent_safe(root: Path, relative: Path) -> None:
    current = root
    for part in relative.parts[:-1]:
        current /= part
        if current.is_symlink():
            raise RuntimeDataError("runtime_restore_symlink_rejected")
        if current.exists() and not current.is_dir():
            raise RuntimeDataError("runtime_restore_parent_invalid")


def restore_runtime_data(source: Path, destination: Path) -> dict[str, int]:
    if source.is_symlink():
        raise RuntimeDataError("runtime_backup_source_invalid")
    source_root = _resolved(source, must_exist=True)
    if not source_root.is_dir():
        raise RuntimeDataError("runtime_backup_source_invalid")
    if destination.is_symlink():
        raise RuntimeDataError("runtime_restore_destination_invalid")
    destination_root = _resolved(destination, must_exist=True)
    if not destination_root.is_dir():
        raise RuntimeDataError("runtime_restore_destination_invalid")
    inventory = _load_inventory(source_root)

    directory_paths = [
        _safe_relative_path(value) for value in inventory["directories"]
    ]
    file_entries: list[tuple[Path, dict[str, Any]]] = []
    for raw in inventory["files"]:
        if not isinstance(raw, dict):
            raise RuntimeDataError("runtime_backup_inventory_invalid")
        relative = _safe_relative_path(raw.get("path"))
        source_path = source_root / relative
        if not source_path.is_file() or source_path.is_symlink():
            raise RuntimeDataError("runtime_backup_file_missing")
        if _sha256(source_path) != str(raw.get("sha256") or ""):
            raise RuntimeDataError("runtime_backup_file_checksum_failed")
        expected_bytes = raw.get("bytes")
        if (
            not isinstance(expected_bytes, int)
            or isinstance(expected_bytes, bool)
            or expected_bytes < 0
            or source_path.stat().st_size != expected_bytes
        ):
            raise RuntimeDataError("runtime_backup_file_size_failed")
        file_entries.append((relative, raw))

    actual_directories, actual_files = _walk_regular_tree(source_root)
    expected_directory_names = {path.as_posix() for path in directory_paths}
    expected_file_names = {relative.as_posix() for relative, _raw in file_entries}
    if len(expected_directory_names) != len(directory_paths) or len(
        expected_file_names
    ) != len(file_entries):
        raise RuntimeDataError("runtime_backup_inventory_duplicate_path")
    actual_directory_names = {
        _relative(path, source_root) for path in actual_directories
    }
    actual_file_names = {
        _relative(path, source_root)
        for path in actual_files
        if path.name != INVENTORY_NAME or path.parent != source_root
    }
    if (
        actual_directory_names != expected_directory_names
        or actual_file_names != expected_file_names
    ):
        raise RuntimeDataError("runtime_backup_inventory_tree_mismatch")

    for relative in directory_paths:
        _ensure_destination_parent_safe(destination_root, relative / ".keep")
        target = destination_root / relative
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            raise RuntimeDataError("runtime_restore_directory_invalid")
        target.mkdir(parents=True, exist_ok=True)
        target.chmod(0o700)

    for relative, _raw in file_entries:
        if relative.suffix.lower() not in SQLITE_SUFFIXES:
            continue
        target = destination_root / relative
        _ensure_destination_parent_safe(destination_root, relative)
        for suffix in SQLITE_SIDECAR_SUFFIXES:
            sidecar = Path(f"{target}{suffix}")
            if sidecar.is_symlink():
                raise RuntimeDataError("runtime_restore_symlink_rejected")
            if sidecar.exists():
                if not sidecar.is_file():
                    raise RuntimeDataError("runtime_restore_sidecar_invalid")
                sidecar.unlink()

    for relative, raw in file_entries:
        target = destination_root / relative
        _ensure_destination_parent_safe(destination_root, relative)
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise RuntimeDataError("runtime_restore_file_invalid")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_root / relative, target, follow_symlinks=False)
        target.chmod(0o600)
        if _sha256(target) != str(raw["sha256"]):
            raise RuntimeDataError("runtime_restore_copy_verification_failed")
        if relative.suffix.lower() in SQLITE_SUFFIXES:
            if _sqlite_integrity(target).lower() != "ok":
                raise RuntimeDataError("runtime_sqlite_restore_integrity_failed")

    return {
        "directories": len(directory_paths),
        "files": len(file_entries),
        "sqlite_files": len(inventory.get("sqlite_files") or []),
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup = subparsers.add_parser("backup")
    backup.add_argument("--source", type=Path, required=True)
    backup.add_argument("--destination", type=Path, required=True)
    backup.add_argument("--exclude-root", type=Path)
    restore = subparsers.add_parser("restore")
    restore.add_argument("--source", type=Path, required=True)
    restore.add_argument("--destination", type=Path, required=True)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        if args.command == "backup":
            result = backup_runtime_data(
                args.source,
                args.destination,
                exclude_root=args.exclude_root,
            )
        else:
            result = restore_runtime_data(args.source, args.destination)
    except (OSError, RuntimeDataError, sqlite3.Error, ValueError) as exc:
        reason = (
            str(exc)
            if isinstance(exc, RuntimeDataError)
            else "runtime_data_operation_failed"
        )
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_class": type(exc).__name__,
                    "reason": reason,
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    print(json.dumps({"status": "ok", **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
