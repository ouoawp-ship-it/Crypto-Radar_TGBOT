from __future__ import annotations

import argparse
from datetime import datetime, timezone
import getpass
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from paopao_radar.atomic_json import _file_lock
from paopao_radar.onchain_flow.config import (
    OnchainSettings,
    SettingsValidationError,
)


ENV_FILES = {
    "oi": ".env.oi",
    "onchain": ".env.onchain",
}
ALLOWLIST = {
    "TG_BOT_TOKEN": "oi",
    "TG_CHAT_ID": "oi",
    "COINGLASS_API_KEY": "oi",
    "COINALYZE_API_KEY": "oi",
    "ONCHAIN_BASE_HTTP_RPC_URL": "onchain",
    "ONCHAIN_CEX_LABELS_FILE": "onchain",
    "TG_ONCHAIN_FLOW_TOPIC_ID": "onchain",
    "OAR_AI_ENABLE": "onchain",
    "OAR_AI_PROVIDER": "onchain",
    "OAR_AI_BASE_URL": "onchain",
    "OAR_AI_API_KEY": "onchain",
    "OAR_AI_MODEL": "onchain",
    "OAR_AI_THINKING_MODE": "onchain",
    "OAR_AI_REASONING_EFFORT": "onchain",
    "OAR_AI_MAX_TOKENS": "onchain",
    "OAR_AUTOMATION_ENABLE": "onchain",
}
SECRET_KEYS = {
    "TG_BOT_TOKEN",
    "COINGLASS_API_KEY",
    "COINALYZE_API_KEY",
    "OAR_AI_API_KEY",
}
SENSITIVE_KEYS = SECRET_KEYS | {
    "TG_CHAT_ID",
    "TG_ONCHAIN_FLOW_TOPIC_ID",
    "ONCHAIN_BASE_HTTP_RPC_URL",
    "ONCHAIN_CEX_LABELS_FILE",
    "OAR_AI_BASE_URL",
}
BOOLEAN_KEYS = {
    "OAR_AI_ENABLE",
    "OAR_AUTOMATION_ENABLE",
}


class ConfigManagerError(ValueError):
    pass


def _chmod_600(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in seen:
            raise ConfigManagerError(f"duplicate environment key: {key}")
        seen.add(key)
        values[key] = value.strip().strip('"').strip("'")
    return values


def _replace_value(text: str, key: str, value: str) -> str:
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ConfigManagerError("configuration value must be one line")
    pattern = re.compile(rf"^(\s*{re.escape(key)}\s*=).*$")
    lines = text.splitlines()
    matches = [
        index for index, line in enumerate(lines) if pattern.match(line)
    ]
    if len(matches) > 1:
        raise ConfigManagerError(f"duplicate environment key: {key}")
    replacement = f"{key}={value}"
    if matches:
        lines[matches[0]] = replacement
    else:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(replacement)
    return "\n".join(lines) + "\n"


def _redacted(key: str, value: str) -> object:
    if key in BOOLEAN_KEYS:
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if key in SENSITIVE_KEYS:
        return "configured" if value else "not_configured"
    return value if value else "not_configured"


class ConfigManager:
    def __init__(self, base_dir: Path):
        self.base_dir = base_dir.resolve()

    def path_for_key(self, key: str) -> Path:
        target = ALLOWLIST.get(key)
        if target is None:
            raise ConfigManagerError("configuration key is not allowlisted")
        return self.base_dir / ENV_FILES[target]

    def status(self) -> dict[str, object]:
        values: dict[str, str] = {}
        for filename in ENV_FILES.values():
            path = self.base_dir / filename
            if path.exists():
                values.update(_parse_env(path.read_text(
                    encoding="utf-8-sig"
                )))
        return {
            key: _redacted(key, values.get(key, ""))
            for key in sorted(ALLOWLIST)
        }

    def set(self, key: str, value: str) -> dict[str, object]:
        self._validate_value(key, value)
        path = self.path_for_key(key)
        if path.is_symlink():
            raise ConfigManagerError(
                "environment file must not be a symbolic link"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        with _file_lock(path):
            existed = path.exists()
            original = path.read_bytes() if existed else b""
            text = (
                original.decode("utf-8-sig")
                if existed
                else "# Managed by paopao_config.py; unknown fields are preserved.\n"
            )
            _parse_env(text)
            backup = self._backup(path, original) if existed else None
            updated = _replace_value(text, key, value)
            try:
                self._atomic_write(path, updated)
                self.validate(ENV_FILES[ALLOWLIST[key]])
            except Exception:
                if existed:
                    self._atomic_write(
                        path,
                        original.decode("utf-8-sig"),
                    )
                else:
                    path.unlink(missing_ok=True)
                raise
            _chmod_600(path)
            _chmod_600(path.with_name(f"{path.name}.lock"))
        return {
            "status": "ok",
            "key": key,
            "value": _redacted(key, value),
            "backup_created": backup is not None,
        }

    def validate(self, filename: str | None = None) -> dict[str, object]:
        targets = (
            [filename]
            if filename
            else list(ENV_FILES.values())
        )
        for target in targets:
            path = self.base_dir / target
            if path.exists():
                _parse_env(path.read_text(encoding="utf-8-sig"))
        if filename in {None, ".env.onchain"}:
            try:
                OnchainSettings.load(
                    base_dir=self.base_dir,
                    environ={},
                ).validate()
            except SettingsValidationError as exc:
                raise ConfigManagerError(str(exc)) from exc
        return {"status": "ok", "validated": targets}

    def backups(self, target: str) -> list[dict[str, object]]:
        filename = ENV_FILES.get(target)
        if filename is None:
            raise ConfigManagerError("unknown environment file")
        records = []
        for path in sorted(
            self.base_dir.glob(f"{filename}.bak.*"),
            key=lambda item: item.name,
            reverse=True,
        )[:50]:
            records.append(
                {"version": path.name, "size": path.stat().st_size}
            )
        return records

    def rollback(self, target: str, version: str) -> dict[str, object]:
        filename = ENV_FILES.get(target)
        if filename is None:
            raise ConfigManagerError("unknown environment file")
        path = self.base_dir / filename
        candidate = self.base_dir / version
        if (
            candidate.parent != self.base_dir
            or not candidate.name.startswith(f"{filename}.bak.")
            or not candidate.is_file()
            or candidate.is_symlink()
        ):
            raise ConfigManagerError("invalid configuration backup version")
        replacement = candidate.read_bytes().decode("utf-8-sig")
        _parse_env(replacement)
        with _file_lock(path):
            existed = path.exists()
            original = path.read_bytes() if existed else b""
            self._backup(path, original) if existed else None
            try:
                self._atomic_write(path, replacement)
                self.validate(filename)
            except Exception:
                if existed:
                    self._atomic_write(
                        path,
                        original.decode("utf-8-sig"),
                    )
                else:
                    path.unlink(missing_ok=True)
                raise
            _chmod_600(path)
        return {
            "status": "ok",
            "file": target,
            "restored_version": candidate.name,
        }

    def _validate_value(self, key: str, value: str) -> None:
        self.path_for_key(key)
        if "\n" in value or "\r" in value or "\x00" in value:
            raise ConfigManagerError("configuration value must be one line")
        if key in BOOLEAN_KEYS and value.lower() not in {
            "true",
            "false",
        }:
            raise ConfigManagerError(f"{key} must be true or false")
        if key == "OAR_AI_PROVIDER" and value not in {
            "deepseek",
            "openai_compatible",
        }:
            raise ConfigManagerError("invalid OAR AI provider")
        if key == "OAR_AI_THINKING_MODE" and value not in {
            "enabled",
            "disabled",
        }:
            raise ConfigManagerError("invalid OAR AI thinking mode")
        if key == "OAR_AI_REASONING_EFFORT" and value not in {
            "high",
            "max",
        }:
            raise ConfigManagerError("invalid OAR AI reasoning effort")
        if key == "OAR_AI_MAX_TOKENS":
            try:
                amount = int(value)
            except ValueError as exc:
                raise ConfigManagerError(
                    "OAR_AI_MAX_TOKENS must be an integer"
                ) from exc
            if not 512 <= amount <= 32768:
                raise ConfigManagerError(
                    "OAR_AI_MAX_TOKENS must be in [512, 32768]"
                )
        if key in {"ONCHAIN_BASE_HTTP_RPC_URL", "OAR_AI_BASE_URL"} and value:
            parsed = urlsplit(value)
            if (
                parsed.scheme.lower() not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or bool(parsed.query)
                or bool(parsed.fragment)
            ):
                raise ConfigManagerError("invalid credential-free endpoint URL")

    def _backup(self, path: Path, content: bytes) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = path.with_name(f"{path.name}.bak.{stamp}")
        backup.write_bytes(content)
        _chmod_600(backup)
        return backup

    def _atomic_write(self, path: Path, text: str) -> None:
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
            _chmod_600(temporary)
            os.replace(temporary, path)
            _chmod_600(path)
        finally:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)


def _read_value(key: str) -> str:
    if sys.stdin.isatty():
        prompt = f"请输入 {key}: "
        return (
            getpass.getpass(prompt)
            if key in SENSITIVE_KEYS
            else input(prompt)
        )
    return sys.stdin.read().rstrip("\r\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Paopao allowlisted atomic environment manager"
    )
    parser.add_argument(
        "--base-dir",
        default=os.environ.get(
            "PAOPAO_APP_DIR",
            str(ROOT),
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    status = subparsers.add_parser("status")
    status.add_argument("--json", action="store_true")
    set_value = subparsers.add_parser("set")
    set_value.add_argument("key")
    enable = subparsers.add_parser("enable")
    enable.add_argument("key")
    disable = subparsers.add_parser("disable")
    disable.add_argument("key")
    validate = subparsers.add_parser("validate")
    validate.add_argument(
        "--file",
        choices=tuple(ENV_FILES.values()),
        default=None,
    )
    backups = subparsers.add_parser("backups")
    backups.add_argument("file", choices=tuple(ENV_FILES))
    rollback = subparsers.add_parser("rollback")
    rollback.add_argument("file", choices=tuple(ENV_FILES))
    rollback.add_argument("--version", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    manager = ConfigManager(Path(args.base_dir))
    try:
        if args.command == "status":
            payload = manager.status()
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            else:
                for key, value in payload.items():
                    print(f"{key}={value}")
        elif args.command == "set":
            print(json.dumps(
                manager.set(args.key, _read_value(args.key)),
                ensure_ascii=False,
                sort_keys=True,
            ))
        elif args.command in {"enable", "disable"}:
            if args.key not in BOOLEAN_KEYS:
                raise ConfigManagerError(
                    "enable/disable only supports allowlisted boolean keys"
                )
            print(json.dumps(
                manager.set(
                    args.key,
                    "true" if args.command == "enable" else "false",
                ),
                ensure_ascii=False,
                sort_keys=True,
            ))
        elif args.command == "validate":
            print(json.dumps(
                manager.validate(args.file),
                ensure_ascii=False,
                sort_keys=True,
            ))
        elif args.command == "backups":
            print(json.dumps(
                {
                    "status": "ok",
                    "file": args.file,
                    "backups": manager.backups(args.file),
                },
                ensure_ascii=False,
                sort_keys=True,
            ))
        else:
            print(json.dumps(
                manager.rollback(args.file, args.version),
                ensure_ascii=False,
                sort_keys=True,
            ))
        return 0
    except (ConfigManagerError, OSError, UnicodeError) as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": type(exc).__name__,
                    "reason": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
