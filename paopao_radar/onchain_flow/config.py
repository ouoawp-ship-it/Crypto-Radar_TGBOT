from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from math import isfinite
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

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
    value = values.get(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise SettingsValidationError(f"{name} must be an integer") from exc


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


def _endpoint_diagnostic(value: str) -> dict[str, object]:
    if not value:
        return {"configured": False, "scheme": "", "host": ""}
    parsed = urlsplit(value)
    return {
        "configured": True,
        "scheme": parsed.scheme.lower(),
        "host": parsed.hostname or "",
    }


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
    base_enable: bool = False
    base_chain_id: int = 8453
    base_http_rpc_url: str = ""
    base_wss_rpc_url: str = ""
    base_confirmation_depth: int = 20
    base_bootstrap_lookback_blocks: int = 300
    base_reorg_lookback_blocks: int = 64
    rpc_timeout_sec: Decimal = Decimal("10")
    rpc_retry: int = 3
    rpc_backoff_sec: Decimal = Decimal("1")
    rpc_max_block_range: int = 1000
    rpc_min_block_range: int = 1
    rpc_topic_address_batch: int = 50
    rpc_poll_sec: Decimal = Decimal("5")
    rpc_rate_limit_per_second: int = 20
    rpc_adaptive_max_requests: int = 64
    rpc_adaptive_max_depth: int = 12
    wss_reconnect_sec: Decimal = Decimal("5")
    wss_idle_timeout_sec: Decimal = Decimal("30")
    wss_queue_max: int = 100
    price_enable: bool = False
    price_provider: str = "none"
    price_max_age_sec: int = 300
    price_batch_size: int = 50
    price_rate_limit_per_minute: int = 30
    coingecko_api_key: str = ""
    coingecko_api_base_url: str = "https://pro-api.coingecko.com/api/v3"
    net_dominance_min: Decimal = Decimal("0.60")
    rolling_evaluation_bucket_sec: int = 300
    alert_max_event_age_sec: int = 1800

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
            base_enable=_bool(values, "ONCHAIN_BASE_ENABLE", False),
            base_chain_id=_int(values, "ONCHAIN_BASE_CHAIN_ID", 8453),
            base_http_rpc_url=values.get(
                "ONCHAIN_BASE_HTTP_RPC_URL", ""
            ).strip(),
            base_wss_rpc_url=values.get(
                "ONCHAIN_BASE_WSS_RPC_URL", ""
            ).strip(),
            base_confirmation_depth=_int(
                values, "ONCHAIN_BASE_CONFIRMATION_DEPTH", 20
            ),
            base_bootstrap_lookback_blocks=_int(
                values, "ONCHAIN_BASE_BOOTSTRAP_LOOKBACK_BLOCKS", 300
            ),
            base_reorg_lookback_blocks=_int(
                values, "ONCHAIN_BASE_REORG_LOOKBACK_BLOCKS", 64
            ),
            rpc_timeout_sec=_decimal(
                values, "ONCHAIN_RPC_TIMEOUT_SEC", "10"
            ),
            rpc_retry=_int(values, "ONCHAIN_RPC_RETRY", 3),
            rpc_backoff_sec=_decimal(
                values, "ONCHAIN_RPC_BACKOFF_SEC", "1"
            ),
            rpc_max_block_range=_int(
                values, "ONCHAIN_RPC_MAX_BLOCK_RANGE", 1000
            ),
            rpc_min_block_range=_int(
                values, "ONCHAIN_RPC_MIN_BLOCK_RANGE", 1
            ),
            rpc_topic_address_batch=_int(
                values, "ONCHAIN_RPC_TOPIC_ADDRESS_BATCH", 50
            ),
            rpc_poll_sec=_decimal(
                values, "ONCHAIN_RPC_POLL_SEC", "5"
            ),
            rpc_rate_limit_per_second=_int(
                values, "ONCHAIN_RPC_RATE_LIMIT_PER_SECOND", 20
            ),
            rpc_adaptive_max_requests=_int(
                values, "ONCHAIN_RPC_ADAPTIVE_MAX_REQUESTS", 64
            ),
            rpc_adaptive_max_depth=_int(
                values, "ONCHAIN_RPC_ADAPTIVE_MAX_DEPTH", 12
            ),
            wss_reconnect_sec=_decimal(
                values, "ONCHAIN_WSS_RECONNECT_SEC", "5"
            ),
            wss_idle_timeout_sec=_decimal(
                values, "ONCHAIN_WSS_IDLE_TIMEOUT_SEC", "30"
            ),
            wss_queue_max=_int(values, "ONCHAIN_WSS_QUEUE_MAX", 100),
            price_enable=_bool(values, "ONCHAIN_PRICE_ENABLE", False),
            price_provider=values.get(
                "ONCHAIN_PRICE_PROVIDER", "none"
            ).strip().lower(),
            price_max_age_sec=_int(
                values, "ONCHAIN_PRICE_MAX_AGE_SEC", 300
            ),
            price_batch_size=_int(
                values, "ONCHAIN_PRICE_BATCH_SIZE", 50
            ),
            price_rate_limit_per_minute=_int(
                values, "ONCHAIN_PRICE_RATE_LIMIT_PER_MINUTE", 30
            ),
            coingecko_api_key=values.get(
                "ONCHAIN_COINGECKO_API_KEY", ""
            ).strip(),
            coingecko_api_base_url=values.get(
                "ONCHAIN_COINGECKO_API_BASE_URL",
                "https://pro-api.coingecko.com/api/v3",
            ).strip().rstrip("/"),
            net_dominance_min=_decimal(
                values, "ONCHAIN_NET_DOMINANCE_MIN", "0.60"
            ),
            rolling_evaluation_bucket_sec=_int(
                values, "ONCHAIN_ROLLING_EVALUATION_BUCKET_SEC", 300
            ),
            alert_max_event_age_sec=_int(
                values, "ONCHAIN_ALERT_MAX_EVENT_AGE_SEC", 1800
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
            "rpc_timeout_sec",
            "rpc_backoff_sec",
            "rpc_poll_sec",
            "wss_reconnect_sec",
            "wss_idle_timeout_sec",
            "net_dominance_min",
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
        if self.base_chain_id != 8453:
            raise SettingsValidationError("ONCHAIN_BASE_CHAIN_ID must be 8453")
        positive_ints = (
            "rpc_max_block_range",
            "rpc_min_block_range",
            "rpc_topic_address_batch",
            "rpc_rate_limit_per_second",
            "wss_queue_max",
            "price_max_age_sec",
            "price_batch_size",
            "price_rate_limit_per_minute",
            "rolling_evaluation_bucket_sec",
            "rpc_adaptive_max_requests",
            "rpc_adaptive_max_depth",
        )
        for field_name in positive_ints:
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise SettingsValidationError(f"{field_name} must be positive")
        non_negative_ints = (
            "base_confirmation_depth",
            "base_bootstrap_lookback_blocks",
            "base_reorg_lookback_blocks",
            "rpc_retry",
            "alert_max_event_age_sec",
        )
        for field_name in non_negative_ints:
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise SettingsValidationError(
                    f"{field_name} must be non-negative"
                )
        if self.rpc_min_block_range > self.rpc_max_block_range:
            raise SettingsValidationError(
                "rpc_min_block_range cannot exceed rpc_max_block_range"
            )
        if self.rpc_timeout_sec <= 0 or self.rpc_poll_sec <= 0:
            raise SettingsValidationError(
                "RPC timeout and poll interval must be positive"
            )
        if self.wss_reconnect_sec <= 0 or self.wss_idle_timeout_sec <= 0:
            raise SettingsValidationError(
                "WSS reconnect and idle timeout must be positive"
            )
        if self.net_dominance_min > 1:
            raise SettingsValidationError(
                "net_dominance_min must be in [0, 1]"
            )
        if self.price_provider not in {"none", "static", "coingecko_onchain"}:
            raise SettingsValidationError(
                "ONCHAIN_PRICE_PROVIDER must be none, static, or coingecko_onchain"
            )
        price_api = urlsplit(self.coingecko_api_base_url)
        if (
            price_api.scheme.lower() != "https"
            or not price_api.hostname
            or price_api.username is not None
            or price_api.password is not None
        ):
            raise SettingsValidationError(
                "ONCHAIN_COINGECKO_API_BASE_URL must be a credential-free HTTPS URL"
            )
        if price_api.hostname.lower() == "api.coingecko.com":
            raise SettingsValidationError(
                "CoinGecko Pro credentials cannot use api.coingecko.com"
            )
        for name, value, schemes in (
            (
                "ONCHAIN_BASE_HTTP_RPC_URL",
                self.base_http_rpc_url,
                {"http", "https"},
            ),
            (
                "ONCHAIN_BASE_WSS_RPC_URL",
                self.base_wss_rpc_url,
                {"ws", "wss"},
            ),
        ):
            if value and urlsplit(value).scheme.lower() not in schemes:
                raise SettingsValidationError(f"{name} has an invalid scheme")
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
            "base": {
                "enabled": self.base_enable,
                "chain_id": self.base_chain_id,
                "confirmation_depth": self.base_confirmation_depth,
                "http_provider": _endpoint_diagnostic(
                    self.base_http_rpc_url
                ),
                "wss_provider": _endpoint_diagnostic(
                    self.base_wss_rpc_url
                ),
            },
            "price": {
                "enabled": self.price_enable,
                "provider": self.price_provider,
                "api_key_configured": bool(self.coingecko_api_key),
                "api": _endpoint_diagnostic(self.coingecko_api_base_url),
                "max_age_sec": self.price_max_age_sec,
            },
            "telegram": {
                "bot_token_configured": bool(self.tg_bot_token),
                "chat_id_configured": bool(self.tg_chat_id),
                "topic_id_configured": bool(self.tg_onchain_flow_topic_id),
                "hourly_limit": self.tg_hourly_limit,
                "cooldown_sec": self.alert_cooldown_sec,
            },
        }
