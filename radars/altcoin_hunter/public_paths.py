"""Read-only selection of existing local temporary storage for public smoke.

Unlike tempfile.gettempdir(), selection never creates a probe file. Callers may
allocate fresh children only after this function has validated the chosen root.
No environment lookup, filesystem lookup or directory creation occurs at import.
"""
from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath
import stat


def _configured_temporary_path() -> str:
    """A configured but unsafe root fails closed instead of silently falling back."""
    if os.name == "nt":
        for name in ("TEMP", "TMP"):
            value = os.environ.get(name)
            if value:
                return value
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return str(PureWindowsPath(local) / "Temp")
        raise ValueError("temporary_root_unavailable")
    return os.environ.get("TMPDIR") or "/tmp"


def safe_temporary_root() -> Path:
    """Return an existing local directory without a write/probe or link traversal.

    Windows TEMP, TMP, then LOCALAPPDATA/Temp are considered in that order;
    POSIX uses TMPDIR, falling back to /tmp only when it is not configured.
    The selected root must not be the checkout itself or beneath the checkout.
    """
    raw = _configured_temporary_path()
    if (type(raw) is not str or not raw or len(raw) > 4096 or raw != raw.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in raw)):
        raise ValueError("invalid_temporary_root")
    windows = PureWindowsPath(raw)
    if (raw.startswith(("\\\\", "//")) or windows.drive.startswith("\\\\")
            or "://" in raw or ":" in raw[len(windows.drive):]):
        raise ValueError("nonlocal_temporary_root_forbidden")
    root = Path(raw)
    if not root.is_absolute() or ".." in root.parts:
        raise ValueError("absolute_temporary_root_required")
    repository = Path(__file__).absolute().parents[2]
    if root == repository or repository in root.parents:
        raise ValueError("repository_temporary_root_forbidden")
    if os.name == "nt":
        import ctypes
        if ctypes.windll.kernel32.GetDriveTypeW(str(root.anchor)) not in (2, 3, 5, 6):
            raise ValueError("nonlocal_temporary_root_forbidden")
    for component in (*reversed(root.parents), root):
        metadata = component.lstat()
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise ValueError("linked_temporary_root_forbidden")
        if not stat.S_ISDIR(metadata.st_mode):
            raise ValueError("temporary_root_must_be_directory")
    return root
