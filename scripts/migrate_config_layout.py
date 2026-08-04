#!/usr/bin/env python3
from __future__ import annotations

import argparse
from contextlib import ExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.atomic_json import _file_lock, _fsync_parent


class ConfigMigrationError(RuntimeError):
    pass


STATE_SCHEMA_VERSION = 1


def _chmod(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass


def _require_mode(path: Path, mode: int, error: str) -> None:
    if os.name != "posix" or not path.exists():
        return
    try:
        actual = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise ConfigMigrationError(error) from exc
    if actual != mode:
        raise ConfigMigrationError(error)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _owner_uid(user: str | None) -> int | None:
    if not user or os.name != "posix":
        return None
    try:
        import pwd

        return int(pwd.getpwnam(user).pw_uid)
    except (ImportError, KeyError) as exc:
        raise ConfigMigrationError("service_user_invalid") from exc


def _set_owner(path: Path, uid: int | None) -> None:
    if uid is None or not path.exists():
        return
    try:
        if path.stat().st_uid != uid:
            os.chown(path, uid, -1)
    except OSError as exc:
        raise ConfigMigrationError("config_owner_update_failed") from exc


def _require_regular_file(path: Path) -> None:
    if path.is_symlink():
        raise ConfigMigrationError("env_symlink_rejected")
    if path.exists() and not path.is_file():
        raise ConfigMigrationError("env_path_invalid")


def _atomic_copy(source: Path, target: Path) -> None:
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
            delete=False,
        ) as handle:
            temporary_name = handle.name
            with source.open("rb") as reader:
                shutil.copyfileobj(reader, handle)
            handle.flush()
            os.fsync(handle.fileno())
        temporary = Path(temporary_name)
        _chmod(temporary, 0o600)
        os.replace(temporary, target)
        _chmod(target, 0o600)
        _require_mode(target, 0o600, "env_permissions_update_failed")
        _fsync_parent(target)
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)


def _atomic_write_json(path: Path, value: dict[str, object]) -> None:
    content = (
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n"
    ).encode("utf-8")
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary = Path(temporary_name)
        _chmod(temporary, 0o600)
        os.replace(temporary, path)
        _chmod(path, 0o600)
        _require_mode(path, 0o600, "env_permissions_update_failed")
        _fsync_parent(path)
    finally:
        if temporary_name:
            Path(temporary_name).unlink(missing_ok=True)


def _load_state(path: Path) -> dict[str, object] | None:
    _require_regular_file(path)
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigMigrationError("env_migration_state_invalid") from exc
    digest = value.get("legacy_sha256") if isinstance(value, dict) else None
    if (
        value.get("schema_version") != STATE_SCHEMA_VERSION
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        raise ConfigMigrationError("env_migration_state_invalid")
    return value


def _write_state(path: Path, legacy_digest: str) -> None:
    _atomic_write_json(path, {
        "schema_version": STATE_SCHEMA_VERSION,
        "legacy_sha256": legacy_digest,
    })


def _backup_files(
    base_dir: Path,
    paths: list[Path],
    owner_uid: int | None = None,
) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir = base_dir / "backups" / f"config-migration-{stamp}"
    counter = 1
    while backup_dir.exists():
        backup_dir = base_dir / "backups" / (
            f"config-migration-{stamp}-{counter}"
        )
        counter += 1
    backup_dir.mkdir(parents=True, mode=0o700)
    _chmod(backup_dir.parent, 0o700)
    _chmod(backup_dir, 0o700)
    _set_owner(backup_dir.parent, owner_uid)
    _set_owner(backup_dir, owner_uid)
    _require_mode(
        backup_dir,
        0o700,
        "env_backup_permissions_update_failed",
    )
    for path in paths:
        target = backup_dir / (
            "legacy.env.oi" if path.parent == base_dir else "canonical.env.oi"
        )
        _atomic_copy(path, target)
        _set_owner(target, owner_uid)
        if _digest(path) != _digest(target):
            raise ConfigMigrationError("env_backup_verification_failed")
    return backup_dir


def migrate(
    base_dir: Path,
    *,
    finalize: bool = False,
    owner_user: str | None = None,
) -> dict[str, object]:
    base_dir = base_dir.resolve()
    owner_uid = _owner_uid(owner_user)
    config_dir = base_dir / "config"
    legacy = base_dir / ".env.oi"
    canonical = config_dir / ".env.oi"
    example = config_dir / ".env.oi.example"
    state_path = config_dir / ".env.oi.migration-state.json"

    if config_dir.is_symlink():
        raise ConfigMigrationError("config_directory_symlink_rejected")
    config_dir.mkdir(parents=True, exist_ok=True)
    _chmod(config_dir, 0o700)
    _set_owner(config_dir, owner_uid)
    _require_regular_file(legacy)
    _require_regular_file(canonical)
    _require_regular_file(example)
    _require_regular_file(state_path)

    lock_stack = ExitStack()
    try:
        lock_stack.enter_context(_file_lock(legacy))
        lock_stack.enter_context(_file_lock(canonical))
        if canonical.exists() and legacy.exists():
            legacy_digest = _digest(legacy)
            canonical_digest = _digest(canonical)
            state = _load_state(state_path)
            if canonical_digest != legacy_digest and (
                state is None
                or state.get("legacy_sha256") != legacy_digest
            ):
                raise ConfigMigrationError("env_path_conflict")
            if finalize:
                if (
                    canonical_digest != legacy_digest
                    and state is None
                ):
                    raise ConfigMigrationError("env_migration_state_missing")
                _backup_files(
                    base_dir,
                    [legacy, canonical],
                    owner_uid,
                )
                legacy.unlink()
                state_path.unlink(missing_ok=True)
            else:
                _write_state(state_path, legacy_digest)
            _chmod(canonical, 0o600)
            if legacy.exists():
                _chmod(legacy, 0o600)
            return {
                "status": "ok",
                "migration": (
                    "duplicate_legacy_removed"
                    if finalize
                    else "ready_to_finalize"
                ),
                "backup_created": finalize,
                "configuration": "configured",
            }

        if canonical.exists():
            state_path.unlink(missing_ok=True)
            _chmod(canonical, 0o600)
            return {
                "status": "ok",
                "migration": "already_current",
                "backup_created": False,
                "configuration": "configured",
            }

        if legacy.exists():
            original_digest = _digest(legacy)
            _backup_files(base_dir, [legacy], owner_uid)
            _atomic_copy(legacy, canonical)
            if _digest(canonical) != original_digest:
                canonical.unlink(missing_ok=True)
                raise ConfigMigrationError("env_migration_verification_failed")
            if finalize:
                legacy.unlink()
                state_path.unlink(missing_ok=True)
            else:
                _chmod(legacy, 0o600)
                _write_state(state_path, original_digest)
            return {
                "status": "ok",
                "migration": (
                    "legacy_moved" if finalize else "legacy_copied"
                ),
                "backup_created": True,
                "configuration": "configured",
            }

        if not example.exists():
            raise ConfigMigrationError("env_example_missing")
        _atomic_copy(example, canonical)
        state_path.unlink(missing_ok=True)
        return {
            "status": "needs_configuration",
            "migration": "empty_config_created",
            "backup_created": False,
            "configuration": "not_configured",
        }
    finally:
        lock_stack.close()
        legacy_lock = legacy.with_name(f"{legacy.name}.lock")
        if finalize and not legacy.exists():
            legacy_lock.unlink(missing_ok=True)
        _chmod(config_dir, 0o700)
        for path in (
            config_dir,
            canonical,
            canonical.with_name(f"{canonical.name}.lock"),
            state_path,
            legacy_lock,
        ):
            _set_owner(path, owner_uid)
        _chmod(config_dir, 0o700)
        _require_mode(
            config_dir,
            0o700,
            "config_directory_permissions_update_failed",
        )
        for path in (
            canonical,
            canonical.with_name(f"{canonical.name}.lock"),
            state_path,
            legacy,
            legacy_lock,
        ):
            if path.exists():
                _chmod(path, 0o600)
                _require_mode(
                    path,
                    0o600,
                    "env_permissions_update_failed",
                )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Safely move the private environment file into config/"
    )
    parser.add_argument("--base-dir", default=str(ROOT))
    parser.add_argument(
        "--finalize",
        action="store_true",
        help="remove the verified legacy copy after services use config/",
    )
    parser.add_argument(
        "--owner-user",
        default=None,
        help="ensure the service account owns the private config files",
    )
    args = parser.parse_args(argv)
    try:
        result = migrate(
            Path(args.base_dir),
            finalize=args.finalize,
            owner_user=args.owner_user,
        )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result["status"] == "ok" else 2
    except ConfigMigrationError as exc:
        print(json.dumps(
            {"status": "failed", "error": str(exc)},
            ensure_ascii=False,
            sort_keys=True,
        ))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
