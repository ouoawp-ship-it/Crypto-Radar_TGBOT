from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, TypeVar

from .atomic_json import locked_read_json, locked_update_json, locked_write_json


T = TypeVar("T")


class JsonStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def load(self, path: Path, default: Any) -> Any:
        return locked_read_json(path, default, quarantine_corrupt=True)

    def save(self, path: Path, data: Any) -> None:
        locked_write_json(path, data)

    def update(self, path: Path, update_fn: Callable[[Any], T], default: Any) -> T:
        return locked_update_json(path, update_fn, default)

    def append_record(self, path: Path, record: dict[str, Any], limit: int = 2000) -> None:
        def append(records: Any) -> list[Any]:
            history = list(records) if isinstance(records, list) else []
            history.append(record)
            if limit > 0 and len(history) > limit:
                history = history[-limit:]
            return history

        self.update(path, append, [])

    def exists_summary(self, paths: list[Path]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for path in paths:
            result.append({
                "path": str(path),
                "exists": path.exists(),
                "size": path.stat().st_size if path.exists() else 0,
            })
        return result
