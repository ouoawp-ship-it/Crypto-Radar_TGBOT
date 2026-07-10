from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, Iterator, TypeVar


T = TypeVar("T")

_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.RLock] = {}

try:  # Linux and other POSIX production hosts.
    import fcntl  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - platform specific
    fcntl = None  # type: ignore[assignment]

try:  # Windows development and test hosts.
    import msvcrt  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - platform specific
    msvcrt = None  # type: ignore[assignment]


def _path(value: str | os.PathLike[str]) -> Path:
    return Path(value)


def _lock_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.lock")


def _process_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path.resolve(strict=False)))
    with _LOCKS_GUARD:
        lock = _PROCESS_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _PROCESS_LOCKS[key] = lock
        return lock


def _acquire_windows_lock(handle: Any) -> bool:
    if msvcrt is None:
        return False
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    handle.seek(0)
    while True:
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            time.sleep(0.01)


def _release_windows_lock(handle: Any) -> None:
    if msvcrt is None:  # pragma: no cover - guarded by acquisition
        return
    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Serialize access to *path* across threads and, where supported, processes."""

    path.parent.mkdir(parents=True, exist_ok=True)
    local_lock = _process_lock(path)
    lock_path = _lock_path(path)
    with local_lock:
        with lock_path.open("a+b") as handle:
            windows_locked = False
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            elif msvcrt is not None:
                windows_locked = _acquire_windows_lock(handle)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                elif windows_locked:
                    _release_windows_lock(handle)


def _clone_default(default: T) -> T:
    try:
        return deepcopy(default)
    except Exception:
        return default


def _read_json_unlocked(path: Path, default: T, *, quarantine_corrupt: bool = False) -> Any | T:
    if not path.exists():
        return _clone_default(default)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        if quarantine_corrupt:
            corrupt = path.with_name(f"{path.name}.corrupt.{int(time.time())}.{os.getpid()}")
            try:
                os.replace(path, corrupt)
            except OSError:
                pass
        return _clone_default(default)


def locked_read_json(
    path: str | os.PathLike[str],
    default: T,
    *,
    quarantine_corrupt: bool = False,
) -> Any | T:
    target = _path(path)
    with _file_lock(target):
        return _read_json_unlocked(target, default, quarantine_corrupt=quarantine_corrupt)


def _fsync_parent(path: Path) -> None:
    if os.name != "posix" or not hasattr(os, "O_DIRECTORY"):
        return
    try:
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write_text_unlocked(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        with temporary.open("w", encoding=encoding, newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_parent(path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def atomic_write_text(
    path: str | os.PathLike[str],
    text: str,
    *,
    encoding: str = "utf-8",
) -> None:
    target = _path(path)
    with _file_lock(target):
        _atomic_write_text_unlocked(target, str(text), encoding=encoding)


def _write_json_unlocked(path: Path, data: Any) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
    _atomic_write_text_unlocked(path, text)


def locked_write_json(path: str | os.PathLike[str], data: Any) -> None:
    target = _path(path)
    with _file_lock(target):
        _write_json_unlocked(target, data)


def locked_update_json(
    path: str | os.PathLike[str],
    update_fn: Callable[[Any], T],
    default: Any,
) -> T:
    """Read, update and atomically replace a JSON document under one file lock."""

    target = _path(path)
    with _file_lock(target):
        current = _read_json_unlocked(target, default, quarantine_corrupt=True)
        updated = update_fn(current)
        _write_json_unlocked(target, updated)
        return updated


def _legacy_json_array(text: str) -> list[Any] | None:
    if not text.lstrip().startswith("["):
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else None


def append_jsonl(
    path: str | os.PathLike[str],
    record: Any,
    max_lines: int | None = None,
) -> None:
    """Append one durable JSON line, accepting a legacy JSON-array file on first use."""

    target = _path(path)
    encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    limit = None if max_lines is None else max(0, int(max_lines))
    with _file_lock(target):
        target.parent.mkdir(parents=True, exist_ok=True)
        existing = ""
        legacy: list[Any] | None = None
        if target.exists():
            with target.open("rb") as handle:
                prefix = handle.read(4096)
            if prefix.lstrip().startswith(b"["):
                existing = target.read_text(encoding="utf-8")
                legacy = _legacy_json_array(existing)
        if legacy is not None:
            lines = [json.dumps(item, ensure_ascii=False, separators=(",", ":")) for item in legacy]
            lines.append(encoded)
            if limit:
                lines = lines[-limit:]
            _atomic_write_text_unlocked(target, "\n".join(lines) + "\n")
            return

        if limit:
            if not existing and target.exists():
                existing = target.read_text(encoding="utf-8")
            lines = [line for line in existing.splitlines() if line.strip()]
            lines.append(encoded)
            lines = lines[-limit:]
            _atomic_write_text_unlocked(target, "\n".join(lines) + "\n")
            return

        needs_newline = False
        if target.exists() and target.stat().st_size:
            with target.open("rb") as handle:
                handle.seek(-1, os.SEEK_END)
                needs_newline = handle.read(1) not in {b"\n", b"\r"}
        with target.open("a", encoding="utf-8", newline="") as handle:
            if needs_newline:
                handle.write("\n")
            handle.write(encoded)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())


__all__ = [
    "append_jsonl",
    "atomic_write_text",
    "locked_read_json",
    "locked_update_json",
    "locked_write_json",
]
