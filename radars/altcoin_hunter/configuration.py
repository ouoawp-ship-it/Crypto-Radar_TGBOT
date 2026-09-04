"""Immutable, explicit configuration for offline P1A only."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from .models import strict_int

_LEGACY_DATABASES = frozenset({
    "signals.db", "jobs.db", "onchain_signals.db", "market_snapshots.db",
    "realtime_features.db", "binance_coordination.db",
})


def validate_database_path(value: Any) -> Path | None:
    """Validate a path without creating a directory or opening a database."""
    if value is None or value == "":
        return None
    if not isinstance(value, (str, Path)):
        raise ValueError("db_file must be an explicit absolute path")
    raw = str(value)
    if not raw or raw != raw.strip() or any(ord(char) < 32 for char in raw):
        raise ValueError("invalid db_file")
    path = Path(value)
    if raw.startswith(("\\\\", "//")):
        raise ValueError("db_file must be a local filesystem path")
    if not path.is_absolute() or ".." in path.parts or path.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
        raise ValueError("db_file must be an absolute SQLite path without parent traversal")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    for part in path.parts:
        if part == path.anchor:
            continue
        if (any(char in '<>:"|?*' for char in part) or part.rstrip(" .") != part
                or part.split(".", 1)[0].upper() in reserved):
            raise ValueError("db_file contains an invalid portable path component")
    if path.name.lower() in _LEGACY_DATABASES:
        raise ValueError("db_file must not target a legacy database")
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError("db_file must not use symbolic links")
        if component.exists() and component != path and not component.is_dir():
            raise ValueError("db_file parent is not a directory")
    if path.exists() and not path.is_file():
        raise ValueError("db_file must not be a directory")
    return path


def _boolean(value: Any, name: str) -> bool:
    if type(value) is bool:
        return value
    if isinstance(value, str) and value in {"true", "false", "1", "0"}:
        return value in {"true", "1"}
    raise ValueError(f"{name} must be true/false or 1/0")


def _integer(value: Any, name: str) -> int:
    if isinstance(value, str):
        if not value or not value.isascii() or not value.isdigit():
            raise ValueError(f"{name} must be an integer")
        return int(value)
    return strict_int(value, name)


@dataclass(frozen=True)
class AltcoinHunterConfig:
    enable: bool = False
    live_data_enable: bool = False
    send_enable: bool = False
    db_file: Path | None = None
    bucket_sec: int = 60
    allowed_lateness_ms: int = 2000
    raw_capture_enable: bool = False
    retention_1m_days: int = 3
    config_version: int = 1

    def __post_init__(self) -> None:
        for name in ("enable", "live_data_enable", "send_enable", "raw_capture_enable"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be a boolean")
        if self.live_data_enable or self.send_enable or self.raw_capture_enable:
            raise ValueError("live data, Telegram sending and raw capture are unsupported in P1A")
        strict_int(self.bucket_sec, "bucket_sec", minimum=1, maximum=3600)
        if self.bucket_sec != 60:
            raise ValueError("P1A supports only 60-second buckets")
        strict_int(self.allowed_lateness_ms, "allowed_lateness_ms", maximum=60000)
        strict_int(self.retention_1m_days, "retention_1m_days", minimum=1, maximum=365)
        if type(self.config_version) is not int or self.config_version != 1:
            raise ValueError("unsupported config_version")
        object.__setattr__(self, "db_file", validate_database_path(self.db_file))

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> "AltcoinHunterConfig":
        if not isinstance(mapping, Mapping):
            raise ValueError("configuration must be a mapping")
        names = {field.name for field in fields(cls)}
        values: dict[str, Any] = {}
        for key, value in mapping.items():
            if not isinstance(key, str):
                raise ValueError("configuration key must be a string")
            name = key.removeprefix("ALTCOIN_HUNTER_").lower() if key.startswith("ALTCOIN_HUNTER_") else key
            if name not in names or name in values:
                raise ValueError("unknown or duplicate configuration key")
            if name in {"enable", "live_data_enable", "send_enable", "raw_capture_enable"}:
                value = _boolean(value, name)
            elif name != "db_file":
                value = _integer(value, name)
            values[name] = value
        return cls(**values)

    @property
    def config_hash(self) -> str:
        payload = asdict(self)
        payload["db_file"] = str(self.db_file) if self.db_file is not None else None
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def redacted_status(self) -> dict[str, Any]:
        return {
            "enable": self.enable, "live_data_enable": self.live_data_enable,
            "send_enable": self.send_enable, "raw_capture_enable": self.raw_capture_enable,
            "db_configured": self.db_file is not None, "bucket_sec": self.bucket_sec,
            "allowed_lateness_ms": self.allowed_lateness_ms,
            "retention_1m_days": self.retention_1m_days,
            "config_version": self.config_version, "config_hash": self.config_hash,
        }
