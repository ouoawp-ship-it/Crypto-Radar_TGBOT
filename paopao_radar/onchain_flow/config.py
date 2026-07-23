from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from math import isfinite
from pathlib import Path
from typing import Mapping

from .constants import PRODUCTION_WRITE_PATHS


BASE_DIR = Path(__file__).resolve().parents[2]


class UnsafeOnchainPath(ValueError):
    pass


class SettingsValidationError(ValueError):
    pass


def parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _bool(values: Mapping[str, str], name: str, default: bool) -> bool:
    value = values.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(values: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(values.get(name, str(default)))
    except (TypeError, ValueError):
        return default


def _decimal(values: Mapping[str, str], name: str, default: str) -> Decimal:
    value = values.get(name)
    if value is None or value.strip() == "":
        return Decimal(default)
    try:
        return Decimal(value)
    except (InvalidOperation, TypeError) as exc:
        raise SettingsValidationError(f"{name} must be a decimal") from exc


def _resolve_data_dir(base_dir: Path, raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else base_dir / path


def _resolve_data_file(base_dir: Path, data_dir: Path, raw: str) -> Path:
    path = Path(raw)
    if path.is_absolute():
        return path
    if path.parts and path.parts[0].lower() == "data":
        return base_dir / path
    return data_dir / path


def _resolve_repo_file(base_dir: Path, raw: str) -> Path:
    path = Path(raw)
    return path if path.is_absolute() else base_dir / path


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class OnchainSettings:
    base_dir: Path = BASE_DIR
    enable: bool = False
    real_send: bool = False
    data_dir: Path = BASE_DIR / "data" / "onchain"
    db_path: Path = BASE_DIR / "data" / "onchain" / "onchain_flow.db"
    runtime_status_path: Path = BASE_DIR / "data" / "onchain" / "runtime_status.json"
    tg_push_history_path: Path = BASE_DIR / "data" / "onchain" / "tg_push_history.json"
    tg_outbox_path: Path = BASE_DIR / "data" / "onchain" / "tg_outbox.json"
    tg_topic_routes_path: Path = BASE_DIR / "data" / "onchain" / "tg_topic_routes.json"
    signal_events_path: Path = BASE_DIR / "data" / "onchain" / "signal_events.json"
    signal_events_db_path: Path = BASE_DIR / "data" / "onchain" / "onchain_signals.db"
    labels_path: Path = BASE_DIR / "config" / "onchain" / "cex_addresses.example.csv"
    chains_path: Path = BASE_DIR / "config" / "onchain" / "chains.example.json"
    tg_bot_token: str = ""
    tg_chat_id: str = ""
    tg_onchain_flow_topic_id: str = ""
    tg_use_topic: bool = False
    tg_hourly_limit: int = 6
    alert_cooldown_sec: int = 3600
    min_label_confidence: float = 0.80
    single_large_floor_usd: Decimal = Decimal("1000000")
    batch_15m_floor_usd: Decimal = Decimal("2000000")
    continuous_60m_floor_usd: Decimal = Decimal("4000000")
    single_volume_ratio: Decimal = Decimal("0.001")
    batch_volume_ratio: Decimal = Decimal("0.002")
    continuous_volume_ratio: Decimal = Decimal("0.004")
    baseline_mad_multiplier: Decimal = Decimal("3")

    @classmethod
    def load(
        cls,
        *,
        base_dir: Path = BASE_DIR,
        environ: Mapping[str, str] | None = None,
    ) -> "OnchainSettings":
        base_dir = base_dir.resolve()
        shared = parse_env_file(base_dir / ".env.oi")
        onchain = parse_env_file(base_dir / ".env.onchain")
        values = {**shared, **onchain, **dict(os.environ if environ is None else environ)}
        data_dir = _resolve_data_dir(
            base_dir,
            values.get("ONCHAIN_DATA_DIR", "data/onchain"),
        )
        return cls(
            base_dir=base_dir,
            enable=_bool(values, "ONCHAIN_ENABLE", False),
            real_send=_bool(values, "ONCHAIN_REAL_SEND", False),
            data_dir=data_dir,
            db_path=_resolve_data_file(
                base_dir,
                data_dir,
                values.get("ONCHAIN_DB_FILE", "onchain_flow.db"),
            ),
            runtime_status_path=_resolve_data_file(
                base_dir,
                data_dir,
                values.get("ONCHAIN_RUNTIME_STATUS_FILE", "runtime_status.json"),
            ),
            tg_push_history_path=_resolve_data_file(
                base_dir,
                data_dir,
                values.get("ONCHAIN_TG_PUSH_HISTORY_FILE", "tg_push_history.json"),
            ),
            tg_outbox_path=_resolve_data_file(
                base_dir,
                data_dir,
                values.get("ONCHAIN_TG_OUTBOX_FILE", "tg_outbox.json"),
            ),
            tg_topic_routes_path=_resolve_data_file(
                base_dir,
                data_dir,
                values.get("ONCHAIN_TG_TOPIC_ROUTES_FILE", "tg_topic_routes.json"),
            ),
            signal_events_path=_resolve_data_file(
                base_dir,
                data_dir,
                values.get("ONCHAIN_SIGNAL_EVENTS_FILE", "signal_events.json"),
            ),
            signal_events_db_path=_resolve_data_file(
                base_dir,
                data_dir,
                values.get("ONCHAIN_SIGNAL_EVENTS_DB_FILE", "onchain_signals.db"),
            ),
            labels_path=_resolve_repo_file(
                base_dir,
                values.get(
                    "ONCHAIN_CEX_LABELS_FILE",
                    "config/onchain/cex_addresses.example.csv",
                ),
            ),
            chains_path=_resolve_repo_file(
                base_dir,
                values.get(
                    "ONCHAIN_CHAINS_FILE",
                    "config/onchain/chains.example.json",
                ),
            ),
            tg_bot_token=values.get("TG_BOT_TOKEN", "").strip(),
            tg_chat_id=values.get("TG_CHAT_ID", "").strip(),
            tg_onchain_flow_topic_id=values.get(
                "TG_ONCHAIN_FLOW_TOPIC_ID", ""
            ).strip(),
            tg_use_topic=_bool(values, "TELEGRAM_USE_TOPIC", False),
            tg_hourly_limit=max(1, _int(values, "ONCHAIN_TG_HOURLY_LIMIT", 6)),
            alert_cooldown_sec=max(
                0, _int(values, "ONCHAIN_ALERT_COOLDOWN_SEC", 3600)
            ),
            min_label_confidence=float(
                _decimal(values, "ONCHAIN_MIN_LABEL_CONFIDENCE", "0.80")
            ),
            single_large_floor_usd=_decimal(
                values, "ONCHAIN_SINGLE_LARGE_FLOOR_USD", "1000000"
            ),
            batch_15m_floor_usd=_decimal(
                values, "ONCHAIN_BATCH_15M_FLOOR_USD", "2000000"
            ),
            continuous_60m_floor_usd=_decimal(
                values, "ONCHAIN_CONTINUOUS_60M_FLOOR_USD", "4000000"
            ),
            single_volume_ratio=_decimal(
                values, "ONCHAIN_SINGLE_VOLUME_RATIO", "0.001"
            ),
            batch_volume_ratio=_decimal(
                values, "ONCHAIN_BATCH_VOLUME_RATIO", "0.002"
            ),
            continuous_volume_ratio=_decimal(
                values, "ONCHAIN_CONTINUOUS_VOLUME_RATIO", "0.004"
            ),
            baseline_mad_multiplier=_decimal(
                values, "ONCHAIN_BASELINE_MAD_MULTIPLIER", "3"
            ),
        )

    @property
    def writable_paths(self) -> tuple[Path, ...]:
        return (
            self.db_path,
            self.runtime_status_path,
            self.tg_push_history_path,
            self.tg_outbox_path,
            self.tg_topic_routes_path,
            self.signal_events_path,
            self.signal_events_db_path,
        )

    def assert_safe_paths(self) -> None:
        root = self.data_dir.resolve()
        production_paths = {
            (self.base_dir / relative).resolve() for relative in PRODUCTION_WRITE_PATHS
        }
        if root == (self.base_dir / "data").resolve():
            raise UnsafeOnchainPath("ONCHAIN_DATA_DIR cannot be the production data root")
        for path in self.writable_paths:
            resolved = path.resolve()
            if resolved in production_paths:
                raise UnsafeOnchainPath(
                    f"on-chain write path collides with production path: {resolved}"
                )
            if not _is_relative_to(resolved, root):
                raise UnsafeOnchainPath(
                    f"on-chain write path escapes ONCHAIN_DATA_DIR: {resolved}"
                )

    def validate(self) -> None:
        try:
            confidence = float(self.min_label_confidence)
        except (TypeError, ValueError, OverflowError) as exc:
            raise SettingsValidationError(
                "min_label_confidence must be finite and in [0, 1]"
            ) from exc
        if not isfinite(confidence) or not 0 <= confidence <= 1:
            raise SettingsValidationError(
                "min_label_confidence must be finite and in [0, 1]"
            )
        non_negative_decimals = (
            "single_large_floor_usd",
            "batch_15m_floor_usd",
            "continuous_60m_floor_usd",
            "single_volume_ratio",
            "batch_volume_ratio",
            "continuous_volume_ratio",
            "baseline_mad_multiplier",
        )
        for field_name in non_negative_decimals:
            value = getattr(self, field_name)
            try:
                numeric_value = Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise SettingsValidationError(
                    f"{field_name} must be finite and non-negative"
                ) from exc
            if not numeric_value.is_finite() or numeric_value < 0:
                raise SettingsValidationError(
                    f"{field_name} must be finite and non-negative"
                )
        self.assert_safe_paths()

    def diagnostic(self) -> dict[str, object]:
        self.validate()
        return {
            "enabled": self.enable,
            "real_send_enabled": self.real_send,
            "data_dir": str(self.data_dir),
            "db_file": str(self.db_path),
            "labels_file": str(self.labels_path),
            "chains_file": str(self.chains_path),
            "telegram": {
                "bot_token_configured": bool(self.tg_bot_token),
                "chat_id_configured": bool(self.tg_chat_id),
                "topic_id_configured": bool(self.tg_onchain_flow_topic_id),
                "hourly_limit": self.tg_hourly_limit,
                "cooldown_sec": self.alert_cooldown_sec,
            },
        }
