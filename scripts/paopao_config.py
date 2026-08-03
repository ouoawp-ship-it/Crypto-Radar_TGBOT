from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
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
    OAR_TELEGRAM_QUERY_ACK,
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
    "MAIN_BOT_DELIVERY_MODE": "oi",
    "MAIN_BOT_REAL_SEND": "oi",
    "MAIN_BOT_REAL_SEND_ACK": "oi",
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
    "ARKHAM_API_BASE_URL": "onchain",
    "ARKHAM_API_KEY": "onchain",
    "ARKHAM_API_TIMEOUT_SEC": "onchain",
    "ARKHAM_API_MAX_RETRIES": "onchain",
    "OAR_LABEL_CANDIDATE_MAX_ADDRESSES": "onchain",
    "DUNE_API_BASE_URL": "onchain",
    "DUNE_API_KEY": "onchain",
    "DUNE_API_TIMEOUT_SEC": "onchain",
    "DUNE_API_MAX_RETRIES": "onchain",
    "DUNE_API_MAX_REQUESTS": "onchain",
    "DUNE_API_POLL_INTERVAL_SEC": "onchain",
    "DUNE_API_EXECUTION_TIMEOUT_SEC": "onchain",
    "DUNE_API_MAX_ROWS": "onchain",
    "OAR_TELEGRAM_QUERY_ENABLE": "onchain",
    "OAR_TELEGRAM_QUERY_ACK": "onchain",
    "OAR_TELEGRAM_QUERY_POLL_TIMEOUT_SEC": "onchain",
    "OAR_TELEGRAM_QUERY_COOLDOWN_SEC": "onchain",
    "OAR_TELEGRAM_QUERY_MAX_PER_HOUR": "onchain",
    "OAR_TELEGRAM_QUERY_MAX_EVENTS": "onchain",
    "OAR_TELEGRAM_QUERY_MAX_RPC_REQUESTS": "onchain",
    "OAR_TELEGRAM_QUERY_TOP_N": "onchain",
    "OAR_WATCH_BASELINE_MIN_SAMPLES": "onchain",
    "OAR_WATCH_BASELINE_MAX_SAMPLES": "onchain",
    "OAR_WATCH_BASELINE_MAD_MULTIPLIER": "onchain",
    "OAR_WATCH_CONTROLLED_ALERT_ENABLE": "onchain",
    "OAR_WATCH_SCAN_INTERVAL_SEC": "onchain",
    "OAR_WATCH_QUERY_WINDOW": "onchain",
}
SECRET_KEYS = {
    "TG_BOT_TOKEN",
    "COINGLASS_API_KEY",
    "COINALYZE_API_KEY",
    "OAR_AI_API_KEY",
    "ARKHAM_API_KEY",
    "DUNE_API_KEY",
}
SENSITIVE_KEYS = SECRET_KEYS | {
    "TG_CHAT_ID",
    "TG_ONCHAIN_FLOW_TOPIC_ID",
    "ONCHAIN_BASE_HTTP_RPC_URL",
    "ONCHAIN_CEX_LABELS_FILE",
    "OAR_AI_BASE_URL",
    "OAR_WATCH_REAL_SEND_ACK",
    "MAIN_BOT_REAL_SEND_ACK",
    "ARKHAM_API_BASE_URL",
    "DUNE_API_BASE_URL",
    "OAR_TELEGRAM_QUERY_ACK",
}
BOOLEAN_KEYS = {
    "OAR_AI_ENABLE",
    "OAR_AUTOMATION_ENABLE",
    "ONCHAIN_REAL_SEND",
    "OAR_WATCH_WITH_AI",
    "MAIN_BOT_REAL_SEND",
    "OAR_TELEGRAM_QUERY_ENABLE",
    "OAR_WATCH_CONTROLLED_ALERT_ENABLE",
}
INTEGER_RANGES = {
    "ONCHAIN_RPC_MAX_BLOCK_RANGE": (1, 10000),
    "OAR_AI_TIMEOUT_SEC": (5, 180),
    "OAR_AI_MAX_RETRIES": (0, 2),
    "ARKHAM_API_TIMEOUT_SEC": (1, 60),
    "ARKHAM_API_MAX_RETRIES": (0, 2),
    "OAR_LABEL_CANDIDATE_MAX_ADDRESSES": (1, 100),
    "DUNE_API_TIMEOUT_SEC": (1, 60),
    "DUNE_API_MAX_RETRIES": (0, 2),
    "DUNE_API_MAX_REQUESTS": (4, 40),
    "DUNE_API_EXECUTION_TIMEOUT_SEC": (5, 120),
    "DUNE_API_MAX_ROWS": (1, 500),
    "OAR_TELEGRAM_QUERY_POLL_TIMEOUT_SEC": (1, 50),
    "OAR_TELEGRAM_QUERY_COOLDOWN_SEC": (10, 3600),
    "OAR_TELEGRAM_QUERY_MAX_PER_HOUR": (1, 60),
    "OAR_TELEGRAM_QUERY_MAX_EVENTS": (1, 5000),
    "OAR_TELEGRAM_QUERY_MAX_RPC_REQUESTS": (1, 256),
    "OAR_TELEGRAM_QUERY_TOP_N": (1, 50),
    "OAR_WATCH_BASELINE_MIN_SAMPLES": (4, 100),
    "OAR_WATCH_BASELINE_MAX_SAMPLES": (8, 100),
    "OAR_WATCH_SCAN_INTERVAL_SEC": (60, 86400),
}
DECIMAL_RANGES = {
    "DUNE_API_POLL_INTERVAL_SEC": (0.2, 10.0),
    "OAR_WATCH_BASELINE_MAD_MULTIPLIER": (1.0, 10.0),
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
MAIN_BOT_REAL_SEND_ACK_PHRASE = "发送真实主BOT提醒"


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

    def _effective_values(self) -> dict[str, str]:
        shared = self._read_env(ENV_FILES["oi"])
        onchain = self._read_env(ENV_FILES["onchain"])
        values = {**shared, **onchain}
        if (self.base_dir / ENV_FILES["oi"]).exists():
            values["TG_BOT_TOKEN"] = shared.get("TG_BOT_TOKEN", "")
            values["TG_CHAT_ID"] = shared.get("TG_CHAT_ID", "")
        return values

    def status(self) -> dict[str, object]:
        values = self._effective_values()
        effective_defaults = {
            "MAIN_BOT_DELIVERY_MODE": "dry_run",
            "MAIN_BOT_REAL_SEND": "false",
            "OAR_WATCH_CONTROLLED_ALERT_ENABLE": "false",
            "OAR_WATCH_SCAN_INTERVAL_SEC": "900",
            "OAR_WATCH_QUERY_WINDOW": "4h",
        }
        return {
            key: _redacted(
                key,
                values.get(key, effective_defaults.get(key, "")),
            )
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

    def telegram_query(self, action: str) -> dict[str, object]:
        if action == "enable":
            values = {
                "OAR_TELEGRAM_QUERY_ENABLE": "true",
                "OAR_TELEGRAM_QUERY_ACK": OAR_TELEGRAM_QUERY_ACK,
            }
        elif action == "disable":
            values = {
                "OAR_TELEGRAM_QUERY_ENABLE": "false",
                "OAR_TELEGRAM_QUERY_ACK": "",
            }
        else:
            raise ConfigManagerError("unknown Telegram query action")
        for key, value in values.items():
            self._validate_value(key, value)
        path = self.base_dir / ENV_FILES["onchain"]
        backup, validation = self._write_values(path, values)
        current = self.status()
        return {
            "status": "ok",
            "action": action,
            "backup_created": backup is not None,
            "configuration": {
                "OAR_TELEGRAM_QUERY_ENABLE": current[
                    "OAR_TELEGRAM_QUERY_ENABLE"
                ],
                "OAR_TELEGRAM_QUERY_ACK": current[
                    "OAR_TELEGRAM_QUERY_ACK"
                ],
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
        checks = self._validate_business_values(self._effective_values())
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
        if key == "OAR_WATCH_QUERY_WINDOW" and value not in {
            "15m",
            "1h",
            "4h",
            "24h",
        }:
            raise ConfigManagerError("invalid OAR watch query window")
        if key == "MAIN_BOT_DELIVERY_MODE" and value not in {
            "dry_run",
            "real",
        }:
            raise ConfigManagerError("invalid main BOT delivery mode")
        if (
            key == "OAR_WATCH_REAL_SEND_ACK"
            and value not in {"", OAR_REAL_SEND_ACK}
        ):
            raise ConfigManagerError(
                "OAR_WATCH_REAL_SEND_ACK must be empty or the fixed phrase"
            )
        if (
            key == "MAIN_BOT_REAL_SEND_ACK"
            and value not in {"", MAIN_BOT_REAL_SEND_ACK_PHRASE}
        ):
            raise ConfigManagerError(
                "MAIN_BOT_REAL_SEND_ACK must be empty or the fixed phrase"
            )
        if (
            key == "OAR_TELEGRAM_QUERY_ACK"
            and value not in {"", OAR_TELEGRAM_QUERY_ACK}
        ):
            raise ConfigManagerError(
                "OAR_TELEGRAM_QUERY_ACK must be empty or the fixed phrase"
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
        if key in {
            "ONCHAIN_BASE_HTTP_RPC_URL",
            "OAR_AI_BASE_URL",
            "ARKHAM_API_BASE_URL",
            "DUNE_API_BASE_URL",
        } and value:
            parsed = urlsplit(value)
            if (
                parsed.scheme.lower() not in (
                    {"https"}
                    if key in {
                        "ARKHAM_API_BASE_URL",
                        "DUNE_API_BASE_URL",
                    }
                    else {"http", "https"}
                )
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
        arkham_base_url = values.get(
            "ARKHAM_API_BASE_URL",
            "https://api.arkm.com",
        ).strip()
        parsed_arkham = urlsplit(arkham_base_url)
        arkham_key = values.get("ARKHAM_API_KEY", "").strip()
        if arkham_key and not arkham_base_url:
            raise ConfigManagerError(
                "ARKHAM_API_BASE_URL is required when Arkham is configured"
            )
        if arkham_base_url and (
            parsed_arkham.scheme.lower() != "https"
            or not parsed_arkham.hostname
            or parsed_arkham.username is not None
            or parsed_arkham.password is not None
            or bool(parsed_arkham.query)
            or bool(parsed_arkham.fragment)
        ):
            raise ConfigManagerError(
                "ARKHAM_API_BASE_URL must be a credential-free HTTPS URL"
            )
        dune_base_url = values.get(
            "DUNE_API_BASE_URL",
            "https://api.dune.com/api",
        ).strip()
        parsed_dune = urlsplit(dune_base_url)
        dune_key = values.get("DUNE_API_KEY", "").strip()
        if dune_key and not dune_base_url:
            raise ConfigManagerError(
                "DUNE_API_BASE_URL is required when Dune is configured"
            )
        if dune_base_url and (
            parsed_dune.scheme.lower() != "https"
            or not parsed_dune.hostname
            or parsed_dune.username is not None
            or parsed_dune.password is not None
            or bool(parsed_dune.query)
            or bool(parsed_dune.fragment)
        ):
            raise ConfigManagerError(
                "DUNE_API_BASE_URL must be a credential-free HTTPS URL"
            )
        try:
            dune_max_requests = int(
                values.get("DUNE_API_MAX_REQUESTS", "10")
            )
            dune_poll_interval = Decimal(
                values.get("DUNE_API_POLL_INTERVAL_SEC", "4")
            )
            dune_execution_timeout = int(
                values.get("DUNE_API_EXECUTION_TIMEOUT_SEC", "30")
            )
        except (InvalidOperation, ValueError) as exc:
            raise ConfigManagerError(
                "invalid Dune polling configuration"
            ) from exc
        if not dune_poll_interval.is_finite():
            raise ConfigManagerError(
                "invalid Dune polling configuration"
            )
        if (
            Decimal(dune_max_requests - 2) * dune_poll_interval
            < Decimal(dune_execution_timeout)
        ):
            raise ConfigManagerError("dune_poll_budget_inconsistent")
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
        controlled_alert_enable = values.get(
            "OAR_WATCH_CONTROLLED_ALERT_ENABLE",
            "false",
        ).strip().lower()
        if controlled_alert_enable not in {"true", "false"}:
            raise ConfigManagerError(
                "OAR_WATCH_CONTROLLED_ALERT_ENABLE must be true or false"
            )
        try:
            watch_scan_interval = int(
                values.get("OAR_WATCH_SCAN_INTERVAL_SEC", "900")
            )
            watch_live_poll = int(
                values.get("OAR_WATCH_LIVE_POLL_SEC", "60")
            )
        except ValueError as exc:
            raise ConfigManagerError(
                "invalid OAR watch rolling coverage configuration"
            ) from exc
        watch_query_window = values.get(
            "OAR_WATCH_QUERY_WINDOW", "4h"
        ).strip()
        watch_window_seconds = {
            "15m": 15 * 60,
            "1h": 60 * 60,
            "4h": 4 * 60 * 60,
            "24h": 24 * 60 * 60,
        }
        if watch_query_window not in watch_window_seconds:
            raise ConfigManagerError("invalid OAR watch query window")
        if (
            watch_scan_interval + watch_live_poll
            > watch_window_seconds[watch_query_window]
        ):
            raise ConfigManagerError(
                "oar_watch_rolling_coverage_inconsistent"
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
        telegram_query_enable = values.get(
            "OAR_TELEGRAM_QUERY_ENABLE", "false"
        ).strip().lower()
        if telegram_query_enable not in {"true", "false"}:
            raise ConfigManagerError(
                "OAR_TELEGRAM_QUERY_ENABLE must be true or false"
            )
        telegram_query_ack = values.get(
            "OAR_TELEGRAM_QUERY_ACK", ""
        ).strip()
        if telegram_query_ack not in {"", OAR_TELEGRAM_QUERY_ACK}:
            raise ConfigManagerError(
                "OAR_TELEGRAM_QUERY_ACK must be empty or the fixed phrase"
            )
        if telegram_query_enable == "true" and not (
            telegram_query_ack == OAR_TELEGRAM_QUERY_ACK
            and token
            and chat_id
            and topic_id
        ):
            raise ConfigManagerError("telegram_query_gate_blocked")
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
            "arkham_api_key": (
                "configured"
                if arkham_key
                else "optional_disabled"
            ),
            "arkham_api_base_url": (
                "configured" if arkham_base_url else "not_configured"
            ),
            "dune_api_key": (
                "configured"
                if dune_key
                else "optional_disabled"
            ),
            "dune_api_base_url": (
                "configured" if dune_base_url else "not_configured"
            ),
            "oar_watch_delivery_mode": delivery_mode,
            "oar_watch_with_ai": watch_with_ai == "true",
            "oar_watch_controlled_alert_enable": (
                controlled_alert_enable == "true"
            ),
            "oar_watch_scan_interval_sec": watch_scan_interval,
            "oar_watch_query_window": watch_query_window,
            "oar_watch_real_send": real_send == "true",
            "oar_watch_real_send_ack": (
                "configured" if real_send_ack else "not_configured"
            ),
            "main_bot_delivery_mode": main_bot_mode,
            "main_bot_real_send": main_bot_real_send == "true",
            "main_bot_real_send_ack": (
                "configured" if main_bot_ack else "not_configured"
            ),
            "telegram_query_enabled": telegram_query_enable == "true",
            "telegram_query_ack": (
                "configured" if telegram_query_ack else "not_configured"
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
    profile = subparsers.add_parser("profile")
    profile.add_argument("name", choices=("deepseek-v4-pro",))
    watch_delivery = subparsers.add_parser("watch-delivery")
    watch_delivery.add_argument(
        "action",
        choices=("observe", "dry-run", "real", "enable-ai", "disable-ai"),
    )
    main_bot_delivery = subparsers.add_parser("main-bot-delivery")
    main_bot_delivery.add_argument(
        "action",
        choices=("dry-run", "real"),
    )
    telegram_query = subparsers.add_parser("telegram-query")
    telegram_query.add_argument(
        "action",
        choices=("enable", "disable"),
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
        elif args.command == "telegram-query":
            print(json.dumps(
                manager.telegram_query(args.action),
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
