"""Explicit, checksum-verified Hunter migrations. Importing performs no I/O."""

from pathlib import Path


SCHEMA_VERSION = 1


def scripts() -> tuple[tuple[int, str], ...]:
    """Read packaged SQL only when a caller explicitly requests migration."""
    path = Path(__file__).with_name("001_foundation.sql")
    return ((1, path.read_text(encoding="utf-8")),)


def migrate(db_path, **kwargs):
    from ..storage import migrate as migrate_database

    return migrate_database(db_path, **kwargs)
