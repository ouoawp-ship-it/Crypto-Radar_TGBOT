from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.atomic_json import _file_lock
ENV_FILES = {
    "oi": "config/.env.oi",
}
ALLOWLIST = {
    "TG_BOT_TOKEN": "oi",
    "TG_CHAT_ID": "oi",
    "TG_PRIVATE_CONTROL_ENABLE": "oi",
    "TG_PRIVATE_CONTROL_ADMIN_USER_ID": "oi",
    "TG_PRIVATE_CONTROL_ALERT_ENABLE": "oi",
    "TG_PRIVATE_CONTROL_ALERT_COOLDOWN_SEC": "oi",
    "MAIN_BOT_DELIVERY_MODE": "oi",
    "MAIN_BOT_REAL_SEND": "oi",
    "MAIN_BOT_REAL_SEND_ACK": "oi",
    "PULSE_RADAR_ENABLE": "oi",
    "RADAR_SUMMARY_ENABLE": "oi",
    "FUNDING_ALERT_ENABLE": "oi",
    "FLOW_RADAR_ENABLE": "oi",
    "CONSOLIDATION_BREAKOUT_ENABLE": "oi",
    "ANNOUNCEMENT_RISK_ENABLE": "oi",
}
SECRET_KEYS = {
    "TG_BOT_TOKEN",
}
SENSITIVE_KEYS = SECRET_KEYS | {
    "TG_CHAT_ID",
    "TG_PRIVATE_CONTROL_ADMIN_USER_ID",
    "MAIN_BOT_REAL_SEND_ACK",
}
BOOLEAN_KEYS = {
    "MAIN_BOT_REAL_SEND",
    "TG_PRIVATE_CONTROL_ENABLE",
    "TG_PRIVATE_CONTROL_ALERT_ENABLE",
    "PULSE_RADAR_ENABLE",
    "RADAR_SUMMARY_ENABLE",
    "FUNDING_ALERT_ENABLE",
    "FLOW_RADAR_ENABLE",
    "CONSOLIDATION_BREAKOUT_ENABLE",
    "ANNOUNCEMENT_RISK_ENABLE",
}
INTEGER_RANGES: dict[str, tuple[int, int]] = {
    "TG_PRIVATE_CONTROL_ALERT_COOLDOWN_SEC": (300, 86400),
}
DECIMAL_RANGES: dict[str, tuple[float, float]] = {}
BACKUP_LIMIT = 30
MAIN_BOT_REAL_SEND_ACK_PHRASE = "发送真实主BOT提醒"


class ConfigManagerError(ValueError):
    pass


def _chmod_600(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _chmod_700(path: Path) -> None:
    try:
        path.chmod(0o700)
    except OSError:
        pass


def _require_mode(path: Path, mode: int) -> None:
    if os.name != "posix":
        return
    try:
        actual = stat.S_IMODE(path.stat().st_mode)
    except OSError as exc:
        raise ConfigManagerError(
            "configuration permissions validation failed"
        ) from exc
    if actual != mode:
        raise ConfigManagerError(
            "configuration permissions validation failed"
        )


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

    def _require_current_layout(self) -> None:
        if (self.base_dir / ".env.oi").exists():
            raise ConfigManagerError("env_layout_migration_required")

    def _effective_values(self) -> dict[str, str]:
        canonical = self.base_dir / ENV_FILES["oi"]
        source = (
            ENV_FILES["oi"]
            if canonical.exists()
            else ".env.oi"
        )
        shared = self._read_env(source)
        values = dict(shared)
        if (self.base_dir / source).exists():
            values["TG_BOT_TOKEN"] = shared.get("TG_BOT_TOKEN", "")
            values["TG_CHAT_ID"] = shared.get("TG_CHAT_ID", "")
        return values

    def status(self) -> dict[str, object]:
        values = self._effective_values()
        effective_defaults = {
            "MAIN_BOT_DELIVERY_MODE": "dry_run",
            "MAIN_BOT_REAL_SEND": "false",
            "TG_PRIVATE_CONTROL_ENABLE": "false",
            "TG_PRIVATE_CONTROL_ALERT_ENABLE": "false",
            "TG_PRIVATE_CONTROL_ALERT_COOLDOWN_SEC": "3600",
            "PULSE_RADAR_ENABLE": "true",
            "RADAR_SUMMARY_ENABLE": "true",
            "FUNDING_ALERT_ENABLE": "true",
            "FLOW_RADAR_ENABLE": "true",
            "CONSOLIDATION_BREAKOUT_ENABLE": "false",
            "ANNOUNCEMENT_RISK_ENABLE": "true",
        }
        status = {
            key: _redacted(
                key,
                values.get(key, effective_defaults.get(key, "")),
            )
            for key in sorted(ALLOWLIST)
        }
        private_admin = values.get(
            "TG_PRIVATE_CONTROL_ADMIN_USER_ID",
            "",
        ).strip()
        if private_admin:
            try:
                self._validate_private_control_admin_id(private_admin)
            except ConfigManagerError:
                status["TG_PRIVATE_CONTROL_ADMIN_USER_ID"] = "invalid"
            else:
                status["TG_PRIVATE_CONTROL_ADMIN_USER_ID"] = "configured"
        else:
            status["TG_PRIVATE_CONTROL_ADMIN_USER_ID"] = "not_configured"
        return status

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

    def main_bot_delivery(self, mode: str) -> dict[str, object]:
        if mode == "dry-run":
            values = {
                "MAIN_BOT_DELIVERY_MODE": "dry_run",
                "MAIN_BOT_REAL_SEND": "false",
                "MAIN_BOT_REAL_SEND_ACK": "",
            }
        elif mode == "real":
            values = {
                "MAIN_BOT_DELIVERY_MODE": "real",
                "MAIN_BOT_REAL_SEND": "true",
                "MAIN_BOT_REAL_SEND_ACK": MAIN_BOT_REAL_SEND_ACK_PHRASE,
            }
        else:
            raise ConfigManagerError(
                "unknown main BOT delivery action"
            )
        for key, value in values.items():
            self._validate_value(key, value)
        path = self.base_dir / ENV_FILES["oi"]
        backup, validation = self._write_values(path, values)
        current = self.status()
        return {
            "status": "ok",
            "action": mode,
            "backup_created": backup is not None,
            "configuration": {
                key: current[key]
                for key in (
                    "MAIN_BOT_DELIVERY_MODE",
                    "MAIN_BOT_REAL_SEND",
                    "MAIN_BOT_REAL_SEND_ACK",
                )
            },
            "validation": validation,
        }

    def _write_values(
        self,
        path: Path,
        values: dict[str, str],
    ) -> tuple[Path | None, dict[str, object]]:
        self._require_current_layout()
        if path.is_symlink():
            raise ConfigManagerError(
                "environment file must not be a symbolic link"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        _chmod_700(path.parent)
        _require_mode(path.parent, 0o700)
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
                validation = self.validate(
                    path.relative_to(self.base_dir).as_posix()
                )
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
            _require_mode(path, 0o600)
            _require_mode(path.with_name(f"{path.name}.lock"), 0o600)
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
        checks = self._validate_business_values(self._effective_values())
        return {
            "status": "ok",
            "validated": targets,
            "checks": checks,
        }

    def backups(self, target: str) -> list[dict[str, object]]:
        filename = ENV_FILES.get(target)
        if filename is None:
            raise ConfigManagerError("unknown environment file")
        env_path = self.base_dir / filename
        records = []
        for path in sorted(
            env_path.parent.glob(f"{env_path.name}.bak.*"),
            key=lambda item: item.name,
            reverse=True,
        )[:BACKUP_LIMIT]:
            records.append(
                {"version": path.name, "size": path.stat().st_size}
            )
        return records

    def rollback(self, target: str, version: str) -> dict[str, object]:
        self._require_current_layout()
        filename = ENV_FILES.get(target)
        if filename is None:
            raise ConfigManagerError("unknown environment file")
        path = self.base_dir / filename
        candidate = path.parent / version
        if (
            candidate.parent != path.parent
            or not candidate.name.startswith(f"{path.name}.bak.")
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
        if key in DECIMAL_RANGES:
            minimum, maximum = DECIMAL_RANGES[key]
            try:
                amount = float(value)
            except ValueError as exc:
                raise ConfigManagerError(
                    f"{key} must be a decimal"
                ) from exc
            if not minimum <= amount <= maximum:
                raise ConfigManagerError(
                    f"{key} must be in [{minimum}, {maximum}]"
                )
        if key == "MAIN_BOT_DELIVERY_MODE" and value not in {
            "dry_run",
            "real",
        }:
            raise ConfigManagerError("invalid main BOT delivery mode")
        if (
            key == "MAIN_BOT_REAL_SEND_ACK"
            and value not in {"", MAIN_BOT_REAL_SEND_ACK_PHRASE}
        ):
            raise ConfigManagerError(
                "MAIN_BOT_REAL_SEND_ACK must be empty or the fixed phrase"
            )
        if key == "TG_PRIVATE_CONTROL_ADMIN_USER_ID" and value:
            self._validate_private_control_admin_id(value)

    @staticmethod
    def _validate_private_control_admin_id(value: str) -> None:
        if not re.fullmatch(r"[1-9][0-9]{0,18}", value):
            raise ConfigManagerError(
                "TG_PRIVATE_CONTROL_ADMIN_USER_ID must be a positive integer"
            )
        if int(value) > 9_223_372_036_854_775_807:
            raise ConfigManagerError(
                "TG_PRIVATE_CONTROL_ADMIN_USER_ID is out of range"
            )

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

        private_control_admin = values.get(
            "TG_PRIVATE_CONTROL_ADMIN_USER_ID",
            "",
        ).strip()
        if private_control_admin:
            self._validate_private_control_admin_id(private_control_admin)
        private_control_enable = values.get(
            "TG_PRIVATE_CONTROL_ENABLE",
            "false",
        ).strip().lower()
        if private_control_enable not in {"true", "false"}:
            raise ConfigManagerError(
                "TG_PRIVATE_CONTROL_ENABLE must be true or false"
            )
        if private_control_enable == "true" and not (
            token and private_control_admin
        ):
            raise ConfigManagerError(
                "private_control_gate_blocked: configure Telegram Bot Token "
                "and the private administrator ID first"
            )
        private_alert_enable = values.get(
            "TG_PRIVATE_CONTROL_ALERT_ENABLE",
            "false",
        ).strip().lower()
        if private_alert_enable not in {"true", "false"}:
            raise ConfigManagerError(
                "TG_PRIVATE_CONTROL_ALERT_ENABLE must be true or false"
            )
        if private_alert_enable == "true" and private_control_enable != "true":
            raise ConfigManagerError(
                "private_control_alert_gate_blocked: enable private control first"
            )

        radar_switches: dict[str, bool] = {}
        for key, default in (
            ("PULSE_RADAR_ENABLE", "true"),
            ("RADAR_SUMMARY_ENABLE", "true"),
            ("FUNDING_ALERT_ENABLE", "true"),
            ("FLOW_RADAR_ENABLE", "true"),
            ("CONSOLIDATION_BREAKOUT_ENABLE", "false"),
            ("ANNOUNCEMENT_RISK_ENABLE", "true"),
        ):
            raw_switch = values.get(key, default).strip().lower()
            if raw_switch not in {"true", "false"}:
                raise ConfigManagerError(f"{key} must be true or false")
            radar_switches[key] = raw_switch == "true"

        main_bot_mode = values.get(
            "MAIN_BOT_DELIVERY_MODE",
            "dry_run",
        ).strip()
        if main_bot_mode not in {"dry_run", "real"}:
            raise ConfigManagerError("invalid main BOT delivery mode")
        main_bot_real_send = values.get(
            "MAIN_BOT_REAL_SEND",
            "false",
        ).strip().lower()
        if main_bot_real_send not in {"true", "false"}:
            raise ConfigManagerError(
                "MAIN_BOT_REAL_SEND must be true or false"
            )
        main_bot_ack = values.get(
            "MAIN_BOT_REAL_SEND_ACK",
            "",
        ).strip()
        if main_bot_ack not in {"", MAIN_BOT_REAL_SEND_ACK_PHRASE}:
            raise ConfigManagerError(
                "MAIN_BOT_REAL_SEND_ACK must be empty or the fixed phrase"
            )
        if main_bot_mode == "real" and not (
            main_bot_real_send == "true"
            and main_bot_ack == MAIN_BOT_REAL_SEND_ACK_PHRASE
            and token
            and chat_id
        ):
            raise ConfigManagerError(
                "main_bot_real_send_gate_blocked: complete Telegram "
                "configuration, MAIN_BOT_REAL_SEND=true, and the fixed "
                "acknowledgement are required"
            )
        if main_bot_mode == "dry_run" and (
            main_bot_real_send != "false" or main_bot_ack
        ):
            raise ConfigManagerError(
                "main_bot_dry_run_gate_inconsistent"
            )
        bounded_integers: dict[str, int] = {}
        for key, default in (
            ("TG_PRIVATE_CONTROL_ALERT_COOLDOWN_SEC", "3600"),
        ):
            raw_value = values.get(key, default).strip()
            try:
                amount = int(raw_value)
            except ValueError as exc:
                raise ConfigManagerError(f"{key} must be an integer") from exc
            minimum, maximum = INTEGER_RANGES[key]
            if not minimum <= amount <= maximum:
                raise ConfigManagerError(
                    f"{key} must be in [{minimum}, {maximum}]"
                )
            bounded_integers[key] = amount
        return {
            "telegram_bot_token": (
                "configured" if token else "not_configured"
            ),
            "telegram_chat_id": (
                "configured" if chat_id else "not_configured"
            ),
            "telegram_private_control_enable": (
                private_control_enable == "true"
            ),
            "telegram_private_control_admin": (
                "configured" if private_control_admin else "not_configured"
            ),
            "telegram_private_control_alert_enable": (
                private_alert_enable == "true"
            ),
            "telegram_private_control_alert_cooldown_sec": bounded_integers[
                "TG_PRIVATE_CONTROL_ALERT_COOLDOWN_SEC"
            ],
            "radar_switches": {
                key: radar_switches[key]
                for key in sorted(radar_switches)
            },
            "main_bot_delivery_mode": main_bot_mode,
            "main_bot_real_send": main_bot_real_send == "true",
            "main_bot_real_send_ack": (
                "configured" if main_bot_ack else "not_configured"
            ),
        }

    def _backup(self, path: Path, content: bytes) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = path.with_name(f"{path.name}.bak.{stamp}")
        backup.write_bytes(content)
        _chmod_600(backup)
        self._trim_backups(path)
        return backup

    def _trim_backups(self, path: Path) -> None:
        try:
            relative = path.relative_to(self.base_dir).as_posix()
        except ValueError as exc:
            raise ConfigManagerError(
                "invalid environment backup target"
            ) from exc
        if relative not in set(ENV_FILES.values()):
            raise ConfigManagerError("invalid environment backup target")
        backups = sorted(
            path.parent.glob(f"{path.name}.bak.*"),
            key=lambda item: item.name,
            reverse=True,
        )
        for backup_path in backups[BACKUP_LIMIT:]:
            if (
                backup_path.parent == (self.base_dir / relative).parent
                and backup_path.name.startswith(
                    f"{(self.base_dir / relative).name}.bak."
                )
                and backup_path.is_file()
                and not backup_path.is_symlink()
            ):
                backup_path.unlink()

    def _atomic_write(self, path: Path, text: str) -> None:
        temporary_name = ""
        ownership: tuple[int, int] | None = None
        if path.exists() and hasattr(os, "chown"):
            current = path.stat()
            ownership = (current.st_uid, current.st_gid)
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
            if ownership is not None:
                os.chown(path, *ownership)
            _chmod_600(path)
            _require_mode(path, 0o600)
        finally:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)

    def _atomic_write_bytes(self, path: Path, content: bytes) -> None:
        temporary_name = ""
        ownership: tuple[int, int] | None = None
        if path.exists() and hasattr(os, "chown"):
            current = path.stat()
            ownership = (current.st_uid, current.st_gid)
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
            _chmod_600(temporary)
            os.replace(temporary, path)
            if ownership is not None:
                os.chown(path, *ownership)
            _chmod_600(path)
            _require_mode(path, 0o600)
        finally:
            if temporary_name:
                Path(temporary_name).unlink(missing_ok=True)


def _read_value(key: str) -> str:
    if sys.stdin.isatty():
        prompt = f"请输入 {key}: "
        return input(prompt)
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
    main_bot_delivery = subparsers.add_parser("main-bot-delivery")
    main_bot_delivery.add_argument(
        "action",
        choices=("dry-run", "real"),
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
        elif args.command == "main-bot-delivery":
            print(json.dumps(
                manager.main_bot_delivery(args.action),
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
