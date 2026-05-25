from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


class JsonStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def load(self, path: Path, default: Any) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            corrupt = path.with_suffix(path.suffix + f".corrupt.{int(time.time())}")
            try:
                path.replace(corrupt)
            except Exception:
                pass
            return default

    def save(self, path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)

    def append_record(self, path: Path, record: dict[str, Any], limit: int = 2000) -> None:
        records = self.load(path, [])
        if not isinstance(records, list):
            records = []
        records.append(record)
        if limit > 0 and len(records) > limit:
            records = records[-limit:]
        self.save(path, records)

    def exists_summary(self, paths: list[Path]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for path in paths:
            result.append({
                "path": str(path),
                "exists": path.exists(),
                "size": path.stat().st_size if path.exists() else 0,
            })
        return result
