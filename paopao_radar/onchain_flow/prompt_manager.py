from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
import tempfile
import time
from typing import Callable

from paopao_radar.atomic_json import _file_lock

from .constants import (
    OAR_AI_OPERATOR_PROMPT_HISTORY_LIMIT,
    OAR_AI_OPERATOR_PROMPT_MAX_CHARS,
)


class OperatorPromptError(ValueError):
    pass


@dataclass(frozen=True)
class OperatorPrompt:
    content: str
    prompt_hash: str
    present: bool


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _chmod(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass


class OperatorPromptManager:
    def __init__(
        self,
        *,
        data_dir: Path,
        prompt_path: Path,
        default_path: Path,
        clock_ns: Callable[[], int] = time.time_ns,
    ):
        self.data_dir = data_dir.resolve()
        self.prompt_path = prompt_path.resolve(strict=False)
        self.default_path = default_path.resolve(strict=False)
        self.history_dir = self.prompt_path.parent / (
            "oar_ai_operator_prompt.history"
        )
        self.clock_ns = clock_ns
        self._validate_runtime_path()

    @classmethod
    def from_settings(cls, settings: object) -> "OperatorPromptManager":
        repository_default = (
            Path(getattr(settings, "base_dir"))
            / "config"
            / "onchain"
            / "oar_ai_operator_prompt.default.txt"
        )
        if not repository_default.exists():
            repository_default = (
                Path(__file__).resolve().parents[2]
                / "config"
                / "onchain"
                / "oar_ai_operator_prompt.default.txt"
            )
        return cls(
            data_dir=Path(getattr(settings, "data_dir")),
            prompt_path=Path(
                getattr(settings, "oar_ai_operator_prompt_path")
            ),
            default_path=repository_default,
        )

    @staticmethod
    def hash_text(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    @staticmethod
    def validate_text(text: str) -> str:
        if not isinstance(text, str):
            raise OperatorPromptError("operator prompt must be UTF-8 text")
        if "\x00" in text:
            raise OperatorPromptError("operator prompt must not contain NUL")
        if len(text) > OAR_AI_OPERATOR_PROMPT_MAX_CHARS:
            raise OperatorPromptError(
                "operator prompt exceeds 12000 characters"
            )
        return text

    def _validate_runtime_path(self) -> None:
        if not _is_relative_to(self.prompt_path, self.data_dir):
            raise OperatorPromptError(
                "operator prompt path must stay under data/onchain"
            )
        if self.prompt_path == self.data_dir:
            raise OperatorPromptError("operator prompt path must be a file")
        if self.prompt_path.is_symlink():
            raise OperatorPromptError(
                "operator prompt path must not be a symbolic link"
            )

    def _prepare_parent(self) -> None:
        self.prompt_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _chmod(self.prompt_path.parent, 0o700)

    def _read_text(self, path: Path) -> str:
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OperatorPromptError(
                "operator prompt must use valid UTF-8"
            ) from exc
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        return self.validate_text(text)

    def _atomic_write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _chmod(path.parent, 0o700)
        temporary_name = ""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                prefix=f".{path.name}.",
                suffix=".tmp",
                dir=path.parent,
                delete=False,
            ) as handle:
                temporary_name = handle.name
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            temporary = Path(temporary_name)
            _chmod(temporary, 0o600)
            os.replace(temporary, path)
            _chmod(path, 0o600)
        finally:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)

    def _snapshot(self, text: str, *, present: bool) -> OperatorPrompt:
        return OperatorPrompt(
            content=text,
            prompt_hash=self.hash_text(text),
            present=present,
        )

    def status(self) -> dict[str, object]:
        result: dict[str, object] = {
            "status": "ok",
            "private": self._file_status(self.prompt_path),
            "default": self._file_status(self.default_path),
            "history_count": len(self.history()),
        }
        if result["private"]["status"] == "invalid":
            result["status"] = "invalid"
        elif result["private"]["status"] == "missing":
            result["status"] = "not_installed"
        return result

    def _file_status(self, path: Path) -> dict[str, object]:
        if not path.exists():
            return {
                "status": "missing",
                "present": False,
                "length": 0,
                "prompt_hash": "",
            }
        try:
            text = self._read_text(path)
        except (OSError, OperatorPromptError):
            return {
                "status": "invalid",
                "present": True,
                "length": 0,
                "prompt_hash": "",
            }
        return {
            "status": "ok",
            "present": True,
            "length": len(text),
            "prompt_hash": self.hash_text(text),
        }

    def show(self) -> str:
        if not self.prompt_path.exists():
            raise OperatorPromptError("operator prompt is not installed")
        return self._read_text(self.prompt_path)

    def validate(self) -> dict[str, object]:
        text = self.show()
        return {
            "status": "ok",
            "length": len(text),
            "prompt_hash": self.hash_text(text),
        }

    def install_default(self) -> OperatorPrompt:
        self._prepare_parent()
        with _file_lock(self.prompt_path):
            if self.prompt_path.exists():
                text = self._read_text(self.prompt_path)
            else:
                if not self.default_path.exists():
                    raise OperatorPromptError(
                        "default operator prompt is missing"
                    )
                text = self._read_text(self.default_path)
                self._atomic_write(self.prompt_path, text)
            _chmod(self.prompt_path.with_name(
                f"{self.prompt_path.name}.lock"
            ), 0o600)
        return self._snapshot(text, present=True)

    def load_for_request(self) -> OperatorPrompt:
        return self.install_default()

    def save(self, text: str) -> OperatorPrompt:
        normalized = self.validate_text(text)
        self._prepare_parent()
        with _file_lock(self.prompt_path):
            if self.prompt_path.exists():
                self._write_history(self._read_text(self.prompt_path))
            self._atomic_write(self.prompt_path, normalized)
            self._trim_history()
            _chmod(self.prompt_path.with_name(
                f"{self.prompt_path.name}.lock"
            ), 0o600)
        return self._snapshot(normalized, present=True)

    def restore_default(self) -> OperatorPrompt:
        if not self.default_path.exists():
            raise OperatorPromptError("default operator prompt is missing")
        return self.save(self._read_text(self.default_path))

    def history(self) -> list[dict[str, object]]:
        if not self.history_dir.exists():
            return []
        records: list[dict[str, object]] = []
        for path in sorted(
            self.history_dir.glob("*.txt"),
            key=lambda item: item.name,
            reverse=True,
        )[:OAR_AI_OPERATOR_PROMPT_HISTORY_LIMIT]:
            try:
                text = self._read_text(path)
            except (OSError, OperatorPromptError):
                continue
            records.append(
                {
                    "version": path.name,
                    "prompt_hash": self.hash_text(text),
                    "length": len(text),
                }
            )
        return records

    def rollback(self, version: str) -> OperatorPrompt:
        candidates = [
            item
            for item in self.history_dir.glob("*.txt")
            if item.name == version
            or item.name.startswith(version)
            or self.hash_text(self._read_text(item)).startswith(version)
        ]
        if len(candidates) != 1:
            raise OperatorPromptError(
                "operator prompt history version is missing or ambiguous"
            )
        replacement = self._read_text(candidates[0])
        return self.save(replacement)

    def hash(self) -> str:
        return self.validate()["prompt_hash"]

    def _write_history(self, text: str) -> None:
        self.history_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        _chmod(self.history_dir, 0o700)
        stamp = int(self.clock_ns())
        prompt_hash = self.hash_text(text)
        path = self.history_dir / f"{stamp}-{prompt_hash[:16]}.txt"
        if not path.exists():
            self._atomic_write(path, text)

    def _trim_history(self) -> None:
        paths = sorted(
            self.history_dir.glob("*.txt"),
            key=lambda item: item.name,
            reverse=True,
        )
        for path in paths[OAR_AI_OPERATOR_PROMPT_HISTORY_LIMIT:]:
            path.unlink(missing_ok=True)
