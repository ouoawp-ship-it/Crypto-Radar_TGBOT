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
    "ONCHAIN_RPC_MAX_BLOCK_RANGE": "onchain",
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
    "OAR_AI_TIMEOUT_SEC": "onchain",
    "OAR_AI_MAX_RETRIES": "onchain",
    "OAR_AUTOMATION_ENABLE": "onchain",
    "ONCHAIN_REAL_SEND": "onchain",
    "OAR_WATCH_DELIVERY_MODE": "onchain",
    "OAR_WATCH_WITH_AI": "onchain",
    "OAR_WATCH_REAL_SEND_ACK": "onchain",
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
    "OAR_WATCH_REAL_SEND_ACK",
}
BOOLEAN_KEYS = {
    "OAR_AI_ENABLE",
    "OAR_AUTOMATION_ENABLE",
    "ONCHAIN_REAL_SEND",
    "OAR_WATCH_WITH_AI",
}
INTEGER_RANGES = {
    "ONCHAIN_RPC_MAX_BLOCK_RANGE": (1, 10000),
    "OAR_AI_TIMEOUT_SEC": (5, 180),
    "OAR_AI_MAX_RETRIES": (0, 2),
}
BACKUP_LIMIT = 30
DEEPSEEK_V4_PRO_PROFILE = {
    "OAR_AI_PROVIDER": "deepseek",
    "OAR_AI_BASE_URL": "https://api.deepseek.com",
    "OAR_AI_MODEL": "deepseek-v4-pro",
    "OAR_AI_THINKING_MODE": "enabled",
    "OAR_AI_REASONING_EFFORT": "high",
    "OAR_AI_MAX_TOKENS": "8192",
    "OAR_AI_TIMEOUT_SEC": "60",
    "OAR_AI_MAX_RETRIES": "0",
}
OAR_REAL_SEND_ACK = "发送真实链上提醒"


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
        backup, validation = self._write_values(
            path,
            {key: value},
        )
        return {
            "status": "ok",
            "key": key,
            "value": _redacted(key, value),
            "backup_created": backup is not None,
            "validation": validation,
        }

    def profile(self, name: str) -> dict[str, object]:
        if name != "deepseek-v4-pro":
            raise ConfigManagerError("unknown configuration profile")
        for key, value in DEEPSEEK_V4_PRO_PROFILE.items():
            self._validate_value(key, value)
        path = self.base_dir / ENV_FILES["onchain"]
        backup, validation = self._write_values(
            path,
            DEEPSEEK_V4_PRO_PROFILE,
        )
        current = self.status()
        return {
            "status": "ok",
            "profile": name,
            "backup_created": backup is not None,
            "configuration": {
                key: current[key]
                for key in (
                    "OAR_AI_PROVIDER",
                    "OAR_AI_BASE_URL",
                    "OAR_AI_MODEL",
                    "OAR_AI_THINKING_MODE",
                    "OAR_AI_REASONING_EFFORT",
                    "OAR_AI_MAX_TOKENS",
                    "OAR_AI_TIMEOUT_SEC",
                    "OAR_AI_MAX_RETRIES",
                    "OAR_AI_API_KEY",
                    "OAR_AI_ENABLE",
                )
            },
            "validation": validation,
        }

    def watch_delivery(self, mode: str) -> dict[str, object]:
        if mode == "observe":
            values = {
                "OAR_WATCH_DELIVERY_MODE": "observe",
                "ONCHAIN_REAL_SEND": "false",
                "OAR_WATCH_REAL_SEND_ACK": "",
                "OAR_WATCH_WITH_AI": "false",
            }
        elif mode == "dry-run":
            values = {
                "OAR_WATCH_DELIVERY_MODE": "dry_run",
                "ONCHAIN_REAL_SEND": "false",
                "OAR_WATCH_REAL_SEND_ACK": "",
            }
        elif mode == "real":
            values = {
                "OAR_WATCH_DELIVERY_MODE": "real",
                "ONCHAIN_REAL_SEND": "true",
                "OAR_WATCH_REAL_SEND_ACK": OAR_REAL_SEND_ACK,
            }
        elif mode == "enable-ai":
            values = {"OAR_WATCH_WITH_AI": "true"}
        elif mode == "disable-ai":
            values = {"OAR_WATCH_WITH_AI": "false"}
        else:
            raise ConfigManagerError("unknown OAR watch delivery action")
        for key, value in values.items():
            self._validate_value(key, value)
        path = self.base_dir / ENV_FILES["onchain"]
        backup, validation = self._write_values(path, values)
        current = self.status()
        return {
            "status": "ok",
            "action": mode,
            "backup_created": backup is not None,
            "configuration": {
                key: current[key]
                for key in (
                    "OAR_WATCH_DELIVERY_MODE",
                    "OAR_WATCH_WITH_AI",
                    "ONCHAIN_REAL_SEND",
                    "OAR_WATCH_REAL_SEND_ACK",
                )
            },
            "validation": validation,
        }

    def _write_values(
        self,
        path: Path,
        values: dict[str, str],
    ) -> tuple[Path | None, dict[str, object]]:
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
            updated = text
            for key, value in values.items():
                updated = _replace_value(updated, key, value)
            try:
                self._atomic_write(path, updated)
                validation = self.validate(path.name)
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
        return backup, validation

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
        shared = self._read_env(ENV_FILES["oi"])
        onchain = self._read_env(ENV_FILES["onchain"])
        checks = self._validate_business_values({**shared, **onchain})
        if filename in {None, ".env.onchain"}:
            try:
                OnchainSettings.load(
                    base_dir=self.base_dir,
                    environ={},
                ).validate()
            except SettingsValidationError as exc:
                raise ConfigManagerError(str(exc)) from exc
        return {
            "status": "ok",
            "validated": targets,
            "checks": checks,
        }

    def backups(self, target: str) -> list[dict[str, object]]:
        filename = ENV_FILES.get(target)
        if filename is None:
            raise ConfigManagerError("unknown environment file")
        records = []
        for path in sorted(
            self.base_dir.glob(f"{filename}.bak.*"),
            key=lambda item: item.name,
            reverse=True,
        )[:BACKUP_LIMIT]:
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
        if key in INTEGER_RANGES:
            minimum, maximum = INTEGER_RANGES[key]
            try:
                amount = int(value)
            except ValueError as exc:
                raise ConfigManagerError(
                    f"{key} must be an integer"
                ) from exc
            if not minimum <= amount <= maximum:
                raise ConfigManagerError(
                    f"{key} must be in [{minimum}, {maximum}]"
                )
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
        if key == "OAR_WATCH_DELIVERY_MODE" and value not in {
            "observe",
            "dry_run",
            "real",
        }:
            raise ConfigManagerError("invalid OAR watch delivery mode")
        if (
            key == "OAR_WATCH_REAL_SEND_ACK"
            and value not in {"", OAR_REAL_SEND_ACK}
        ):
            raise ConfigManagerError(
                "OAR_WATCH_REAL_SEND_ACK must be empty or the fixed phrase"
            )
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

    def _read_env(self, filename: str) -> dict[str, str]:
        path = self.base_dir / filename
        if not path.exists():
            return {}
        return _parse_env(path.read_text(encoding="utf-8-sig"))

    def _validate_business_values(
        self,
        values: dict[str, str],
    ) -> dict[str, object]:
        token = values.get("TG_BOT_TOKEN", "").strip()
        if token and not re.fullmatch(r"[1-9]\d*:[A-Za-z0-9_-]+", token):
            raise ConfigManagerError("TG_BOT_TOKEN has an invalid format")

        chat_id = values.get("TG_CHAT_ID", "").strip()
        if chat_id:
            if not re.fullmatch(r"[+-]?\d+", chat_id) or int(chat_id) == 0:
                raise ConfigManagerError(
                    "TG_CHAT_ID must be a non-zero signed integer"
                )

        topic_id = values.get("TG_ONCHAIN_FLOW_TOPIC_ID", "").strip()
        if topic_id and (
            not re.fullmatch(r"\d+", topic_id) or int(topic_id) <= 0
        ):
            raise ConfigManagerError(
                "TG_ONCHAIN_FLOW_TOPIC_ID must be a positive integer"
            )

        provider = values.get("OAR_AI_PROVIDER", "deepseek").strip().lower()
        model = values.get("OAR_AI_MODEL", "deepseek-v4-pro").strip()
        if provider == "deepseek" and model not in {
            "deepseek-v4-pro",
            "deepseek-v4-flash",
        }:
            raise ConfigManagerError(
                "DeepSeek model must be deepseek-v4-pro or "
                "deepseek-v4-flash"
            )

        ai_base_url = values.get(
            "OAR_AI_BASE_URL",
            "https://api.deepseek.com",
        ).strip()
        if ai_base_url:
            parsed = urlsplit(ai_base_url)
            if (
                parsed.scheme.lower() not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or bool(parsed.query)
                or bool(parsed.fragment)
            ):
                raise ConfigManagerError(
                    "OAR_AI_BASE_URL must be a credential-free HTTP(S) URL"
                )
            if (
                provider == "deepseek"
                and parsed.scheme.lower() == "http"
                and parsed.hostname.lower()
                not in {"localhost", "127.0.0.1", "::1"}
            ):
                raise ConfigManagerError(
                    "DeepSeek remote base URL must use HTTPS"
                )

        labels_status = self._validate_labels_path(
            values.get("ONCHAIN_CEX_LABELS_FILE", "").strip()
        )
        delivery_mode = values.get(
            "OAR_WATCH_DELIVERY_MODE",
            "observe",
        ).strip()
        if delivery_mode not in {"observe", "dry_run", "real"}:
            raise ConfigManagerError("invalid OAR watch delivery mode")
        watch_with_ai = values.get(
            "OAR_WATCH_WITH_AI",
            "false",
        ).strip().lower()
        if watch_with_ai not in {"true", "false"}:
            raise ConfigManagerError(
                "OAR_WATCH_WITH_AI must be true or false"
            )
        real_send = values.get(
            "ONCHAIN_REAL_SEND",
            "false",
        ).strip().lower()
        if real_send not in {"true", "false"}:
            raise ConfigManagerError(
                "ONCHAIN_REAL_SEND must be true or false"
            )
        real_send_ack = values.get(
            "OAR_WATCH_REAL_SEND_ACK",
            "",
        ).strip()
        if real_send_ack not in {"", OAR_REAL_SEND_ACK}:
            raise ConfigManagerError(
                "OAR_WATCH_REAL_SEND_ACK must be empty or the fixed phrase"
            )
        if delivery_mode == "real" and not (
            real_send == "true"
            and real_send_ack == OAR_REAL_SEND_ACK
            and token
            and chat_id
            and topic_id
        ):
            raise ConfigManagerError(
                "real_send_gate_blocked: complete Telegram configuration, "
                "ONCHAIN_REAL_SEND=true, and the fixed acknowledgement "
                "are required"
            )
        return {
            "telegram_bot_token": (
                "configured" if token else "not_configured"
            ),
            "telegram_chat_id": (
                "configured" if chat_id else "not_configured"
            ),
            "telegram_onchain_topic": (
                "configured" if topic_id else "not_configured"
            ),
            "cex_labels_file": labels_status,
            "oar_watch_delivery_mode": delivery_mode,
            "oar_watch_with_ai": watch_with_ai == "true",
            "oar_watch_real_send": real_send == "true",
            "oar_watch_real_send_ack": (
                "configured" if real_send_ack else "not_configured"
            ),
        }

    def _validate_labels_path(self, raw: str) -> str:
        if not raw:
            return "not_configured"
        if "\x00" in raw:
            raise ConfigManagerError(
                "ONCHAIN_CEX_LABELS_FILE must not contain NUL"
            )
        candidate = Path(raw)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ConfigManagerError(
                "ONCHAIN_CEX_LABELS_FILE must be a safe project-relative path"
            )
        try:
            resolved = (self.base_dir / candidate).resolve(strict=False)
            resolved.relative_to(self.base_dir)
        except (OSError, RuntimeError, ValueError) as exc:
            raise ConfigManagerError(
                "ONCHAIN_CEX_LABELS_FILE escapes the project directory"
            ) from exc
        return "present" if resolved.is_file() else "not_present"

    def _backup(self, path: Path, content: bytes) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = path.with_name(f"{path.name}.bak.{stamp}")
        backup.write_bytes(content)
        _chmod_600(backup)
        self._trim_backups(path.name)
        return backup

    def _trim_backups(self, filename: str) -> None:
        if filename not in ENV_FILES.values():
            raise ConfigManagerError("invalid environment backup target")
        backups = sorted(
            self.base_dir.glob(f"{filename}.bak.*"),
            key=lambda item: item.name,
            reverse=True,
        )
        for path in backups[BACKUP_LIMIT:]:
            if (
                path.parent == self.base_dir
                and path.name.startswith(f"{filename}.bak.")
                and path.is_file()
                and not path.is_symlink()
            ):
                path.unlink()

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
    profile = subparsers.add_parser("profile")
    profile.add_argument("name", choices=("deepseek-v4-pro",))
    watch_delivery = subparsers.add_parser("watch-delivery")
    watch_delivery.add_argument(
        "action",
        choices=("observe", "dry-run", "real", "enable-ai", "disable-ai"),
    )
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
        elif args.command == "profile":
            print(json.dumps(
                manager.profile(args.name),
                ensure_ascii=False,
                sort_keys=True,
            ))
        elif args.command == "watch-delivery":
            print(json.dumps(
                manager.watch_delivery(args.action),
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
