from __future__ import annotations

import os
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
ENV_FILE = CONFIG_DIR / ".env.oi"
LEGACY_ENV_FILE = BASE_DIR / ".env.oi"


def active_env_file() -> Path:
    if ENV_FILE.exists():
        return ENV_FILE
    if LEGACY_ENV_FILE.exists():
        return LEGACY_ENV_FILE
    return ENV_FILE


def load_env_file(path: Path | None = None) -> dict[str, str]:
    path = path or active_env_file()
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw_line in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        env[key] = value
        current = os.environ.get(key)
        if current is None or (current.strip() == "" and value.strip()):
            os.environ[key] = value
    return env


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def env_bounded_int(
    name: str,
    default: int,
    minimum: int,
    maximum: int,
) -> int:
    value = env_int(name, default)
    return value if minimum <= value <= maximum else default


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    try:
        return float(value)
    except ValueError:
        return default


def env_csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return tuple(part.strip().upper() for part in value.split(",") if part.strip())


def env_first(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "")
        if value and value.strip():
            return value.strip()
    return ""


def data_path(data_dir: Path, env_name: str, default_name: str) -> Path:
    value = os.getenv(env_name, default_name)
    path = Path(value)
    if path.is_absolute():
        return path
    if path.parts and path.parts[0].lower() == "data":
        return BASE_DIR / path
    return data_dir / path


def project_path(env_name: str, default_name: str) -> Path:
    value = os.getenv(env_name, default_name)
    path = Path(value)
    return path if path.is_absolute() else BASE_DIR / path


@dataclass(frozen=True)
class Settings:
    base_dir: Path = BASE_DIR
    data_dir: Path = BASE_DIR / "data"

    tg_bot_token: str = ""
    tg_chat_id: str = ""
    tg_topic_id: str = ""
    tg_radar_summary_topic_id: str = ""
    tg_launch_alert_topic_id: str = ""
    tg_announcement_alert_topic_id: str = ""
    tg_test_topic_id: str = ""
    tg_flow_radar_topic_id: str = ""
    tg_funding_alert_topic_id: str = ""
    tg_altcoin_contract_anomaly_topic_id: str = ""
    tg_private_control_enable: bool = False
    tg_private_control_admin_user_id: str = ""
    tg_private_control_state_path: Path = (
        BASE_DIR / "data" / "telegram_private_control_state.json"
    )
    tg_private_control_alert_enable: bool = False
    tg_private_control_alert_cooldown_sec: int = 3600
    tg_private_control_alert_state_path: Path = (
        BASE_DIR / "data" / "telegram_private_alert_state.json"
    )
    tg_topic_routes_path: Path = BASE_DIR / "data" / "tg_topic_routes.json"
    tg_topic_intro_pin: bool = True
    tg_use_topic: bool = False
    tg_push_history_path: Path = BASE_DIR / "data" / "tg_push_history.json"
    tg_outbox_path: Path = BASE_DIR / "data" / "tg_outbox.json"
    tg_outbox_quarantine_sec: int = 15 * 60
    tg_outbox_retention_days: int = 7
    tg_push_split_limit: int = 3800
    tg_push_timeout_sec: int = 10
    tg_push_retry: int = 2
    tg_global_hourly_limit: int = 20
    tg_default_cooldown_sec: int = 6 * 3600
    tg_push_history_limit: int = 2000
    tg_push_history_retention_days: int = 30
    signal_events_db_path: Path = BASE_DIR / "data" / "signals.db"
    market_snapshots_db_path: Path = BASE_DIR / "data" / "market_snapshots.db"
    realtime_features_db_path: Path = BASE_DIR / "data" / "realtime_features.db"
    market_snapshot_interval_sec: int = 300
    market_snapshot_retention_days: int = 7
    market_snapshot_limit: int = 500
    market_snapshot_oi_limit: int = 80
    market_snapshot_workers: int = 8
    market_flow_fact_interval_sec: int = 900
    market_flow_fact_limit: int = 40
    market_readiness_target_days: int = 7
    realtime_market_bucket_sec: int = 60
    realtime_market_grace_ms: int = 2000
    realtime_market_flush_interval_sec: int = 1
    realtime_market_reconnect_sec: int = 5
    realtime_market_connect_timeout_sec: int = 15
    realtime_market_idle_timeout_sec: int = 30
    realtime_market_retention_days: int = 3
    realtime_market_symbol_limit: int = 80
    realtime_market_min_quote_volume: float = 5_000_000
    realtime_market_symbol_refresh_sec: int = 300
    signal_events_limit: int = 20_000
    signal_events_retention_days: int = 365
    database_backup_dir: Path = BASE_DIR / "data" / "backups"
    database_backup_retention_days: int = 7
    runtime_status_path: Path = BASE_DIR / "data" / "runtime_status.json"
    cleanup_enable: bool = True
    cleanup_interval_sec: int = 3600
    cleanup_state_path: Path = BASE_DIR / "data" / "cleanup_state.json"
    cleanup_corrupt_retention_days: int = 7
    cleanup_log_retention_days: int = 14
    health_runtime_max_age_sec: int = 10 * 60
    health_realtime_fresh_sec: int = 3 * 60
    health_database_backup_max_age_sec: int = 36 * 60 * 60
    health_disk_warn_mb: int = 1024
    health_disk_fail_mb: int = 256

    http_timeout_sec: int = 10
    http_retry: int = 2
    http_backoff_sec: float = 0.8
    http_cache_enable: bool = True
    http_cache_ttl_sec: int = 10
    http_cache_max_entries: int = 128
    binance_fapi_base_url: str = "https://fapi.binance.com"
    binance_spot_base_url: str = "https://api.binance.com"
    binance_futures_ws_url: str = "wss://fstream.binance.com/market/ws"
    excluded_base_assets: tuple[str, ...] = ("XAU", "XAG")

    altcoin_contract_anomaly_enable: bool = False
    altcoin_contract_anomaly_cmc_api_key: str = ""
    altcoin_contract_anomaly_cmc_connect_timeout_sec: int = 5
    altcoin_contract_anomaly_cmc_read_timeout_sec: int = 15
    altcoin_contract_anomaly_cmc_retry: int = 2
    altcoin_contract_anomaly_cmc_backoff_base_sec: float = 0.5
    altcoin_contract_anomaly_cmc_min_request_interval_sec: float = 2.0
    altcoin_contract_anomaly_cmc_batch_size: int = 100
    altcoin_contract_anomaly_cmc_cache_ttl_sec: int = 300
    altcoin_contract_anomaly_cmc_max_data_age_sec: int = 900
    altcoin_contract_anomaly_cmc_cache_path: Path = (
        BASE_DIR / "data" / "altcoin_contract_anomaly_cmc_cache.json"
    )
    altcoin_contract_anomaly_candidate_snapshot_path: Path = (
        BASE_DIR / "data" / "altcoin_contract_anomaly_candidate_pool.json"
    )
    altcoin_contract_anomaly_mapping_overrides_path: Path = (
        BASE_DIR / "config" / "altcoin_contract_anomaly_overrides.json"
    )
    altcoin_contract_anomaly_market_cap_max_usd: float = 30_000_000
    altcoin_contract_anomaly_short_squeeze_min_oi_market_cap_ratio: float = 0.20
    altcoin_contract_anomaly_short_squeeze_max_funding_rate: float = 0.0
    altcoin_contract_anomaly_high_leverage_min_oi_market_cap_ratio: float = 0.50
    altcoin_contract_anomaly_candidate_refresh_sec: int = 300
    altcoin_contract_anomaly_binance_oi_max_age_sec: int = 600
    altcoin_contract_anomaly_funding_max_age_sec: int = 600
    altcoin_contract_anomaly_telegram_preview_page_chars: int = 3800
    altcoin_contract_anomaly_oi_workers: int = 8
    altcoin_contract_anomaly_oi_request_budget: int = 600
    altcoin_contract_anomaly_realtime_enable: bool = False
    altcoin_contract_anomaly_manifest_poll_sec: int = 5
    altcoin_contract_anomaly_manifest_max_age_sec: int = 1200
    altcoin_contract_anomaly_subscription_batch_size: int = 50
    altcoin_contract_anomaly_subscription_min_interval_sec: float = 1.0
    altcoin_contract_anomaly_subscription_ack_timeout_sec: int = 10
    altcoin_contract_anomaly_max_streams: int = 300
    altcoin_contract_anomaly_realtime_data_max_age_sec: int = 120
    altcoin_contract_anomaly_funding_max_gap_sec: int = 15
    altcoin_contract_anomaly_oi_refresh_sec: int = 300
    altcoin_contract_anomaly_realtime_oi_max_age_sec: int = 600
    altcoin_contract_anomaly_realtime_oi_workers: int = 4
    altcoin_contract_anomaly_realtime_oi_request_budget: int = 50
    altcoin_contract_anomaly_feature_1m_window_sec: int = 60
    altcoin_contract_anomaly_feature_5m_window_sec: int = 300
    altcoin_contract_anomaly_volume_baseline_buckets: int = 5
    altcoin_contract_anomaly_volume_min_samples: int = 4
    altcoin_contract_anomaly_volume_min_coverage: float = 0.8
    altcoin_contract_anomaly_price_1m_move_ratio: float = 0.01
    altcoin_contract_anomaly_price_5m_move_ratio: float = 0.02
    altcoin_contract_anomaly_volume_expansion_ratio: float = 2.0
    altcoin_contract_anomaly_aggressive_buy_ratio: float = 0.60
    altcoin_contract_anomaly_aggressive_sell_ratio: float = 0.40
    altcoin_contract_anomaly_open_interest_move_ratio: float = 0.03
    altcoin_contract_anomaly_funding_positive_rate: float = 0.0005
    altcoin_contract_anomaly_funding_change_ratio: float = 0.0001
    altcoin_contract_anomaly_liquidation_min_usd: float = 100_000
    altcoin_contract_anomaly_price_stall_ratio: float = 0.003
    altcoin_contract_anomaly_weakening_volume_ratio: float = 1.2
    altcoin_contract_anomaly_weakening_windows: int = 2
    altcoin_contract_anomaly_realtime_state_path: Path = (
        BASE_DIR / "data" / "altcoin_contract_anomaly_p2_state.json"
    )
    altcoin_contract_anomaly_realtime_event_path: Path = (
        BASE_DIR / "data" / "altcoin_contract_anomaly_p2_events.jsonl"
    )
    altcoin_contract_anomaly_smoke_duration_sec: int = 900
    altcoin_contract_anomaly_production_enable: bool = False
    altcoin_contract_anomaly_production_send_enable: bool = False
    altcoin_contract_anomaly_production_send_confirm: str = ""
    altcoin_contract_anomaly_production_manifest_refresh_sec: int = 1800
    altcoin_contract_anomaly_production_manifest_retry_sec: int = 60
    altcoin_contract_anomaly_production_manifest_max_age_sec: int = 2400
    altcoin_contract_anomaly_production_cooldown_sec: int = 3600
    altcoin_contract_anomaly_production_hourly_limit: int = 20
    altcoin_contract_anomaly_production_daily_limit: int = 50
    altcoin_contract_anomaly_production_queue_size: int = 256
    altcoin_contract_anomaly_production_status_interval_sec: int = 30
    altcoin_contract_anomaly_production_oi_budget_window_sec: int = 300
    altcoin_contract_anomaly_production_observation_state_path: Path = (
        BASE_DIR / "data" / "altcoin_contract_anomaly_production_observation_state.json"
    )
    altcoin_contract_anomaly_production_observation_event_path: Path = (
        BASE_DIR / "data" / "altcoin_contract_anomaly_production_observation_events.jsonl"
    )
    altcoin_contract_anomaly_production_state_path: Path = (
        BASE_DIR / "data" / "altcoin_contract_anomaly_production_state.json"
    )
    altcoin_contract_anomaly_production_outbox_path: Path = (
        BASE_DIR / "data" / "altcoin_contract_anomaly_production_outbox.json"
    )
    altcoin_contract_anomaly_production_status_path: Path = (
        BASE_DIR / "data" / "altcoin_contract_anomaly_production_status.json"
    )
    altcoin_contract_anomaly_realtime_lock_path: Path = (
        BASE_DIR / "data" / "altcoin_contract_anomaly_realtime.lock"
    )

    radar_scan_limit: int = 120
    radar_summary_enable: bool = True
    radar_min_quote_volume: float = 5_000_000
    radar_top_n: int = 8
    radar_summary_min_interval_sec: int = 6 * 3600
    radar_summary_close_delay_sec: int = 300
    radar_summary_max_daily_push: int = 4
    radar_state_path: Path = BASE_DIR / "data" / "radar_state.json"
    funding_snapshot_path: Path = BASE_DIR / "data" / "funding_snapshot.json"
    accumulation_quality_diagnostics_path: Path = (
        BASE_DIR / "data" / "accumulation_quality_diagnostics.json"
    )
    accumulation_min_history_days: int = 45
    accumulation_max_range_pct: float = 80.0
    accumulation_max_abs_slope_pct: float = 20.0
    accumulation_max_avg_daily_quote_volume: float = 20_000_000
    accumulation_recent_days: int = 7
    accumulation_max_recent_price_gain_pct: float = 300.0

    flow_scan_limit: int = 24
    flow_radar_enable: bool = True
    flow_candidate_state_path: Path = BASE_DIR / "data" / "flow_candidate_state.json"
    flow_top_n: int = 5
    flow_min_score: int = 60
    flow_interval_sec: int = 3600
    flow_close_delay_sec: int = 300
    flow_spot_net_ratio_min_pct: float = 3.0
    flow_futures_net_ratio_min_pct: float = 2.0
    flow_spot_net_min_usd: float = 10_000
    flow_futures_net_min_usd: float = 25_000
    flow_price_move_min_pct: float = 1.0
    flow_price_flat_max_pct: float = 1.5
    flow_oi_build_min_pct: float = 2.0
    flow_oi_unwind_max_pct: float = -1.5

    funding_alert_enable: bool = True
    funding_alert_interval_sec: int = 180
    funding_alert_scan_limit: int = 120
    funding_scan_concurrency: int = 8
    funding_request_timeout_sec: int = 8
    funding_max_symbols_per_batch: int = 120
    funding_alert_min_quote_volume: float = 5_000_000
    funding_alert_exchanges: tuple[str, ...] = ("BINANCE",)
    funding_alert_history_limit: int = 4
    funding_alert_cooldown_sec: int = 3600
    funding_alert_extreme_negative_pct: float = -0.5
    funding_alert_super_negative_pct: float = -1.0
    funding_alert_extreme_positive_pct: float = 0.5
    funding_alert_min_exchange_count: int = 1
    funding_alert_divergence_pct: float = 0.75
    funding_alert_decay_quiet_scans: int = 2
    funding_alert_end_quiet_scans: int = 5
    funding_alert_state_path: Path = BASE_DIR / "data" / "funding_alert_state.json"
    funding_flip_oi_enable: bool = False
    funding_flip_oi_state_path: Path = BASE_DIR / "data" / "funding_flip_oi_state.json"
    funding_flip_oi_window_points: int = 48
    funding_flip_oi_min_coverage: float = 0.90
    funding_flip_oi_max_age_sec: int = 3 * 3600
    funding_flip_oi_min_growth_pct: float = 8.0
    funding_flip_oi_segment_tolerance_pct: float = 0.5
    funding_flip_oi_rate_max_age_sec: int = 15 * 60
    funding_flip_oi_cooldown_sec: int = 24 * 3600

    oi_hist_budget: int = 80
    kline_budget: int = 120
    funding_history_budget: int = 25
    fuse_seconds: int = 15 * 60

    pulse_simple_scan_limit: int = 120
    pulse_divergence_scan_limit: int = 200
    pulse_radar_enable: bool = True
    topic_message_cleanup_max_age_sec: int = 47 * 3600

    announcement_state_path: Path = BASE_DIR / "data" / "announcement_state.json"
    announcement_risk_enable: bool = True
    announcement_page_size: int = 50
    announcement_only_today: bool = True
    announcement_default_ttl_days: int = 3

    divergence_state_path: Path = BASE_DIR / "data" / "oi_divergence_state.json"
    divergence_cooldown_path: Path = BASE_DIR / "data" / "oi_divergence_cooldown.json"

    def __post_init__(self) -> None:
        default_data_dir = BASE_DIR / "data"
        if self.data_dir == default_data_dir:
            return
        for field in fields(self):
            if field.name in {"base_dir", "data_dir"}:
                continue
            value = getattr(self, field.name)
            if not isinstance(value, Path):
                continue
            try:
                relative = value.relative_to(default_data_dir)
            except ValueError:
                continue
            object.__setattr__(self, field.name, self.data_dir / relative)

    @classmethod
    def load(cls) -> "Settings":
        file_env = load_env_file()

        def reloadable_bool(name: str, default: bool) -> bool:
            value = file_env.get(name)
            if value is None:
                return env_bool(name, default)
            normalized = value.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
            raise ValueError("invalid reloadable boolean configuration")

        def reloadable_bool_alias(
            name: str,
            legacy_name: str,
            default: bool,
        ) -> bool:
            raw = file_env.get(name)
            if raw is None:
                raw = file_env.get(legacy_name)
            if raw is None:
                raw = os.getenv(name)
            if raw is None:
                raw = os.getenv(legacy_name)
            if raw is None or not raw.strip():
                return default
            normalized = raw.strip().lower()
            if normalized in {"1", "true", "yes", "on"}:
                return True
            if normalized in {"0", "false", "no", "off"}:
                return False
            raise ValueError("invalid reloadable boolean configuration")

        def reloadable_bounded_int(
            name: str,
            default: int,
            minimum: int,
            maximum: int,
        ) -> int:
            raw = file_env.get(name)
            if raw is None:
                return env_bounded_int(name, default, minimum, maximum)
            try:
                value = int(raw)
            except ValueError as exc:
                raise ValueError(
                    "invalid reloadable integer configuration"
                ) from exc
            if not minimum <= value <= maximum:
                raise ValueError("reloadable integer configuration out of range")
            return value

        def reloadable_bounded_int_alias(
            name: str,
            legacy_name: str,
            default: int,
            minimum: int,
            maximum: int,
        ) -> int:
            raw = file_env.get(name)
            if raw is None:
                raw = file_env.get(legacy_name)
            if raw is None:
                raw = os.getenv(name)
            if raw is None:
                raw = os.getenv(legacy_name)
            if raw is None or not raw.strip():
                return default
            try:
                value = int(raw)
            except ValueError as exc:
                raise ValueError(
                    "invalid reloadable integer configuration"
                ) from exc
            if not minimum <= value <= maximum:
                raise ValueError("reloadable integer configuration out of range")
            return value

        data_dir = BASE_DIR / "data"
        return cls(
            data_dir=data_dir,
            tg_bot_token=os.getenv("TG_BOT_TOKEN", ""),
            tg_chat_id=os.getenv("TG_CHAT_ID", ""),
            tg_topic_id=env_first("TG_TOPIC_ID", "TELEGRAM_MESSAGE_THREAD_ID"),
            tg_radar_summary_topic_id=env_first("TG_RADAR_SUMMARY_TOPIC_ID", "TELEGRAM_RADAR_SUMMARY_TOPIC_ID"),
            tg_launch_alert_topic_id=env_first("TG_LAUNCH_ALERT_TOPIC_ID", "TELEGRAM_LAUNCH_ALERT_TOPIC_ID"),
            tg_announcement_alert_topic_id=env_first(
                "TG_ANNOUNCEMENT_ALERT_TOPIC_ID",
                "TELEGRAM_ANNOUNCEMENT_ALERT_TOPIC_ID",
            ),
            tg_test_topic_id=env_first("TG_TEST_TOPIC_ID", "TELEGRAM_TEST_TOPIC_ID"),
            tg_flow_radar_topic_id=env_first("TG_FLOW_RADAR_TOPIC_ID", "TELEGRAM_FLOW_RADAR_TOPIC_ID"),
            tg_funding_alert_topic_id=env_first("TG_FUNDING_ALERT_TOPIC_ID", "TELEGRAM_FUNDING_ALERT_TOPIC_ID"),
            tg_altcoin_contract_anomaly_topic_id=env_first(
                "TG_ALTCOIN_CONTRACT_ANOMALY_TOPIC_ID",
                "TELEGRAM_ALTCOIN_CONTRACT_ANOMALY_TOPIC_ID",
            ),
            tg_private_control_enable=env_bool(
                "TG_PRIVATE_CONTROL_ENABLE",
                False,
            ),
            tg_private_control_admin_user_id=os.getenv(
                "TG_PRIVATE_CONTROL_ADMIN_USER_ID",
                "",
            ).strip(),
            tg_private_control_alert_enable=reloadable_bool(
                "TG_PRIVATE_CONTROL_ALERT_ENABLE",
                False,
            ),
            tg_private_control_alert_cooldown_sec=reloadable_bounded_int(
                "TG_PRIVATE_CONTROL_ALERT_COOLDOWN_SEC",
                3600,
                300,
                86400,
            ),
            tg_topic_routes_path=data_path(data_dir, "TG_TOPIC_ROUTES_FILE", "tg_topic_routes.json"),
            tg_topic_intro_pin=env_bool("TG_TOPIC_INTRO_PIN", True),
            tg_use_topic=env_bool("TELEGRAM_USE_TOPIC", False),
            tg_push_history_path=data_path(data_dir, "TG_PUSH_HISTORY_FILE", "tg_push_history.json"),
            tg_outbox_path=data_path(data_dir, "TG_OUTBOX_FILE", "tg_outbox.json"),
            tg_outbox_quarantine_sec=env_int("TG_OUTBOX_QUARANTINE_SEC", 15 * 60),
            tg_outbox_retention_days=env_int("TG_OUTBOX_RETENTION_DAYS", 7),
            tg_push_split_limit=env_int("TG_PUSH_SPLIT_LIMIT", 3800),
            tg_push_timeout_sec=env_int("TG_PUSH_TIMEOUT_SEC", 10),
            tg_push_retry=env_int("TG_PUSH_RETRY", 2),
            tg_global_hourly_limit=env_int("TG_GLOBAL_HOURLY_LIMIT", 20),
            tg_default_cooldown_sec=env_int("TG_DEFAULT_COOLDOWN_SEC", 6 * 3600),
            tg_push_history_limit=env_int("TG_PUSH_HISTORY_LIMIT", 2000),
            tg_push_history_retention_days=env_int("TG_PUSH_HISTORY_RETENTION_DAYS", 30),
            signal_events_db_path=data_path(data_dir, "SIGNAL_EVENTS_DB_FILE", "signals.db"),
            market_snapshots_db_path=data_path(data_dir, "MARKET_SNAPSHOTS_DB_FILE", "market_snapshots.db"),
            realtime_features_db_path=data_path(data_dir, "REALTIME_FEATURES_DB_FILE", "realtime_features.db"),
            market_snapshot_interval_sec=env_int("MARKET_SNAPSHOT_INTERVAL_SEC", 300),
            market_snapshot_retention_days=env_int("MARKET_SNAPSHOT_RETENTION_DAYS", 7),
            market_snapshot_limit=env_int("MARKET_SNAPSHOT_LIMIT", 500),
            market_snapshot_oi_limit=env_int("MARKET_SNAPSHOT_OI_LIMIT", 80),
            market_snapshot_workers=env_int("MARKET_SNAPSHOT_WORKERS", 8),
            market_flow_fact_interval_sec=env_int("MARKET_FLOW_FACT_INTERVAL_SEC", 900),
            market_flow_fact_limit=env_int("MARKET_FLOW_FACT_LIMIT", 40),
            market_readiness_target_days=env_int("MARKET_READINESS_TARGET_DAYS", 7),
            realtime_market_bucket_sec=env_int("REALTIME_MARKET_BUCKET_SEC", 60),
            realtime_market_grace_ms=env_int("REALTIME_MARKET_GRACE_MS", 2000),
            realtime_market_flush_interval_sec=env_int("REALTIME_MARKET_FLUSH_INTERVAL_SEC", 1),
            realtime_market_reconnect_sec=env_int("REALTIME_MARKET_RECONNECT_SEC", 5),
            realtime_market_connect_timeout_sec=env_int("REALTIME_MARKET_CONNECT_TIMEOUT_SEC", 15),
            realtime_market_idle_timeout_sec=env_int("REALTIME_MARKET_IDLE_TIMEOUT_SEC", 30),
            realtime_market_retention_days=env_int("REALTIME_MARKET_RETENTION_DAYS", 3),
            realtime_market_symbol_limit=env_int("REALTIME_MARKET_SYMBOL_LIMIT", 80),
            realtime_market_min_quote_volume=env_float("REALTIME_MARKET_MIN_QUOTE_VOLUME", 5_000_000),
            realtime_market_symbol_refresh_sec=env_int("REALTIME_MARKET_SYMBOL_REFRESH_SEC", 300),
            signal_events_limit=env_int("SIGNAL_EVENTS_LIMIT", 20_000),
            signal_events_retention_days=env_int("SIGNAL_EVENTS_RETENTION_DAYS", 365),
            database_backup_dir=data_path(data_dir, "DATABASE_BACKUP_DIR", "backups"),
            database_backup_retention_days=env_int("DATABASE_BACKUP_RETENTION_DAYS", 7),
            runtime_status_path=data_path(data_dir, "RUNTIME_STATUS_FILE", "runtime_status.json"),
            cleanup_enable=env_bool("CLEANUP_ENABLE", True),
            cleanup_interval_sec=env_int("CLEANUP_INTERVAL_SEC", 3600),
            cleanup_state_path=data_path(data_dir, "CLEANUP_STATE_FILE", "cleanup_state.json"),
            cleanup_corrupt_retention_days=env_int("CLEANUP_CORRUPT_RETENTION_DAYS", 7),
            cleanup_log_retention_days=env_int("CLEANUP_LOG_RETENTION_DAYS", 14),
            health_runtime_max_age_sec=env_int("HEALTH_RUNTIME_MAX_AGE_SEC", 10 * 60),
            health_realtime_fresh_sec=env_int("HEALTH_REALTIME_FRESH_SEC", 3 * 60),
            health_database_backup_max_age_sec=env_int(
                "HEALTH_DATABASE_BACKUP_MAX_AGE_SEC",
                36 * 60 * 60,
            ),
            health_disk_warn_mb=env_int("HEALTH_DISK_WARN_MB", 1024),
            health_disk_fail_mb=env_int("HEALTH_DISK_FAIL_MB", 256),
            http_timeout_sec=env_int("BINANCE_API_TIMEOUT_SEC", env_int("HTTP_TIMEOUT_SEC", 10)),
            http_retry=env_int("BINANCE_API_RETRY", env_int("HTTP_RETRY", 2)),
            http_backoff_sec=env_float("BINANCE_API_BACKOFF_SEC", env_float("HTTP_BACKOFF_SEC", 0.8)),
            http_cache_enable=env_bool("DATA_SOURCE_CACHE_ENABLE", True),
            http_cache_ttl_sec=env_int("DATA_SOURCE_CACHE_TTL_SEC", 10),
            http_cache_max_entries=env_int("DATA_SOURCE_CACHE_MAX_ENTRIES", 128),
            binance_fapi_base_url=os.getenv("BINANCE_FAPI_BASE_URL", "https://fapi.binance.com").rstrip("/"),
            binance_spot_base_url=os.getenv("BINANCE_SPOT_BASE_URL", "https://api.binance.com").rstrip("/"),
            binance_futures_ws_url=os.getenv(
                "BINANCE_FUTURES_WS_URL", "wss://fstream.binance.com/market/ws"
            ).rstrip("/"),
            excluded_base_assets=env_csv("EXCLUDED_BASE_ASSETS", ("XAU", "XAG")),
            altcoin_contract_anomaly_enable=env_bool(
                "ALTCOIN_CONTRACT_ANOMALY_ENABLE",
                False,
            ),
            altcoin_contract_anomaly_cmc_api_key=os.getenv(
                "ALTCOIN_CONTRACT_ANOMALY_CMC_API_KEY",
                "",
            ).strip(),
            altcoin_contract_anomaly_cmc_connect_timeout_sec=env_bounded_int(
                "ALTCOIN_CONTRACT_ANOMALY_CMC_CONNECT_TIMEOUT_SEC",
                5,
                1,
                120,
            ),
            altcoin_contract_anomaly_cmc_read_timeout_sec=env_bounded_int(
                "ALTCOIN_CONTRACT_ANOMALY_CMC_READ_TIMEOUT_SEC",
                15,
                1,
                180,
            ),
            altcoin_contract_anomaly_cmc_retry=env_bounded_int(
                "ALTCOIN_CONTRACT_ANOMALY_CMC_RETRY",
                2,
                0,
                5,
            ),
            altcoin_contract_anomaly_cmc_backoff_base_sec=env_float(
                "ALTCOIN_CONTRACT_ANOMALY_CMC_BACKOFF_BASE_SEC",
                0.5,
            ),
            altcoin_contract_anomaly_cmc_min_request_interval_sec=env_float(
                "ALTCOIN_CONTRACT_ANOMALY_CMC_MIN_REQUEST_INTERVAL_SEC",
                2.0,
            ),
            altcoin_contract_anomaly_cmc_batch_size=env_bounded_int(
                "ALTCOIN_CONTRACT_ANOMALY_CMC_BATCH_SIZE",
                100,
                1,
                100,
            ),
            altcoin_contract_anomaly_cmc_cache_ttl_sec=env_bounded_int(
                "ALTCOIN_CONTRACT_ANOMALY_CMC_CACHE_TTL_SEC",
                300,
                1,
                86_400,
            ),
            altcoin_contract_anomaly_cmc_max_data_age_sec=env_bounded_int(
                "ALTCOIN_CONTRACT_ANOMALY_CMC_MAX_DATA_AGE_SEC",
                900,
                1,
                604_800,
            ),
            altcoin_contract_anomaly_cmc_cache_path=data_path(
                data_dir,
                "ALTCOIN_CONTRACT_ANOMALY_CMC_CACHE_FILE",
                "altcoin_contract_anomaly_cmc_cache.json",
            ),
            altcoin_contract_anomaly_candidate_snapshot_path=data_path(
                data_dir,
                "ALTCOIN_CONTRACT_ANOMALY_CANDIDATE_SNAPSHOT_FILE",
                "altcoin_contract_anomaly_candidate_pool.json",
            ),
            altcoin_contract_anomaly_mapping_overrides_path=project_path(
                "ALTCOIN_CONTRACT_ANOMALY_MAPPING_OVERRIDES_FILE",
                "config/altcoin_contract_anomaly_overrides.json",
            ),
            altcoin_contract_anomaly_market_cap_max_usd=env_float(
                "ALTCOIN_CONTRACT_ANOMALY_MARKET_CAP_MAX_USD",
                30_000_000,
            ),
            altcoin_contract_anomaly_short_squeeze_min_oi_market_cap_ratio=env_float(
                "ALTCOIN_CONTRACT_ANOMALY_SHORT_SQUEEZE_MIN_OI_MARKET_CAP_RATIO",
                0.20,
            ),
            altcoin_contract_anomaly_short_squeeze_max_funding_rate=env_float(
                "ALTCOIN_CONTRACT_ANOMALY_SHORT_SQUEEZE_MAX_FUNDING_RATE",
                0.0,
            ),
            altcoin_contract_anomaly_high_leverage_min_oi_market_cap_ratio=env_float(
                "ALTCOIN_CONTRACT_ANOMALY_HIGH_LEVERAGE_MIN_OI_MARKET_CAP_RATIO",
                0.50,
            ),
            altcoin_contract_anomaly_candidate_refresh_sec=env_bounded_int(
                "ALTCOIN_CONTRACT_ANOMALY_CANDIDATE_REFRESH_SEC",
                300,
                1,
                86_400,
            ),
            altcoin_contract_anomaly_binance_oi_max_age_sec=env_bounded_int(
                "ALTCOIN_CONTRACT_ANOMALY_BINANCE_OI_MAX_AGE_SEC",
                600,
                1,
                86_400,
            ),
            altcoin_contract_anomaly_funding_max_age_sec=env_bounded_int(
                "ALTCOIN_CONTRACT_ANOMALY_FUNDING_MAX_AGE_SEC",
                600,
                1,
                86_400,
            ),
            altcoin_contract_anomaly_telegram_preview_page_chars=env_bounded_int(
                "ALTCOIN_CONTRACT_ANOMALY_TELEGRAM_PREVIEW_PAGE_CHARS",
                3800,
                512,
                4096,
            ),
            altcoin_contract_anomaly_oi_workers=env_bounded_int(
                "ALTCOIN_CONTRACT_ANOMALY_OI_WORKERS",
                8,
                1,
                16,
            ),
            altcoin_contract_anomaly_oi_request_budget=env_bounded_int(
                "ALTCOIN_CONTRACT_ANOMALY_OI_REQUEST_BUDGET",
                600,
                1,
                5_000,
            ),
            altcoin_contract_anomaly_realtime_enable=env_bool(
                "ALTCOIN_CONTRACT_ANOMALY_REALTIME_ENABLE",
                False,
            ),
            altcoin_contract_anomaly_manifest_poll_sec=env_bounded_int(
                "ALTCOIN_CONTRACT_ANOMALY_MANIFEST_POLL_SEC",
                5,
                1,
                3_600,
            ),
            altcoin_contract_anomaly_manifest_max_age_sec=env_bounded_int(
                "ALTCOIN_CONTRACT_ANOMALY_MANIFEST_MAX_AGE_SEC",
                1200,
                1,
                86_400,
            ),
            altcoin_contract_anomaly_subscription_batch_size=env_bounded_int(
                "ALTCOIN_CONTRACT_ANOMALY_SUBSCRIPTION_BATCH_SIZE",
                50,
                1,
                200,
            ),
            altcoin_contract_anomaly_subscription_min_interval_sec=env_float(
                "ALTCOIN_CONTRACT_ANOMALY_SUBSCRIPTION_MIN_INTERVAL_SEC",
                1.0,
            ),
            altcoin_contract_anomaly_subscription_ack_timeout_sec=env_bounded_int(
                "ALTCOIN_CONTRACT_ANOMALY_SUBSCRIPTION_ACK_TIMEOUT_SEC",
                10,
                1,
                120,
            ),
            altcoin_contract_anomaly_max_streams=env_bounded_int(
                "ALTCOIN_CONTRACT_ANOMALY_MAX_STREAMS",
                300,
                1,
                1_024,
            ),
            altcoin_contract_anomaly_realtime_data_max_age_sec=env_bounded_int(
                "ALTCOIN_CONTRACT_ANOMALY_REALTIME_DATA_MAX_AGE_SEC",
                120,
                1,
                3_600,
            ),
            altcoin_contract_anomaly_funding_max_gap_sec=env_bounded_int(
                "ALTCOIN_CONTRACT_ANOMALY_FUNDING_MAX_GAP_SEC",
                15,
                1,
                30,
            ),
            altcoin_contract_anomaly_oi_refresh_sec=env_bounded_int(
                "ALTCOIN_CONTRACT_ANOMALY_OI_REFRESH_SEC",
                300,
                300,
                300,
            ),
            altcoin_contract_anomaly_realtime_oi_max_age_sec=env_bounded_int(
                "ALTCOIN_CONTRACT_ANOMALY_REALTIME_OI_MAX_AGE_SEC",
                600,
                1,
                86_400,
            ),
            altcoin_contract_anomaly_realtime_oi_workers=env_bounded_int(
                "ALTCOIN_CONTRACT_ANOMALY_REALTIME_OI_WORKERS",
                4,
                1,
                16,
            ),
            altcoin_contract_anomaly_realtime_oi_request_budget=env_bounded_int(
                "ALTCOIN_CONTRACT_ANOMALY_REALTIME_OI_REQUEST_BUDGET",
                50,
                1,
                5_000,
            ),
            altcoin_contract_anomaly_feature_1m_window_sec=env_bounded_int(
                "ALTCOIN_CONTRACT_ANOMALY_FEATURE_1M_WINDOW_SEC",
                60,
                1,
                3_600,
            ),
            altcoin_contract_anomaly_feature_5m_window_sec=env_bounded_int(
                "ALTCOIN_CONTRACT_ANOMALY_FEATURE_5M_WINDOW_SEC",
                300,
                1,
                3_600,
            ),
            altcoin_contract_anomaly_volume_baseline_buckets=env_bounded_int(
                "ALTCOIN_CONTRACT_ANOMALY_VOLUME_BASELINE_BUCKETS",
                5,
                2,
                1_000,
            ),
            altcoin_contract_anomaly_volume_min_samples=env_bounded_int(
                "ALTCOIN_CONTRACT_ANOMALY_VOLUME_MIN_SAMPLES",
                4,
                1,
                1_000,
            ),
            altcoin_contract_anomaly_volume_min_coverage=env_float(
                "ALTCOIN_CONTRACT_ANOMALY_VOLUME_MIN_COVERAGE",
                0.8,
            ),
            altcoin_contract_anomaly_price_1m_move_ratio=env_float(
                "ALTCOIN_CONTRACT_ANOMALY_PRICE_1M_MOVE_RATIO",
                0.01,
            ),
            altcoin_contract_anomaly_price_5m_move_ratio=env_float(
                "ALTCOIN_CONTRACT_ANOMALY_PRICE_5M_MOVE_RATIO",
                0.02,
            ),
            altcoin_contract_anomaly_volume_expansion_ratio=env_float(
                "ALTCOIN_CONTRACT_ANOMALY_VOLUME_EXPANSION_RATIO",
                2.0,
            ),
            altcoin_contract_anomaly_aggressive_buy_ratio=env_float(
                "ALTCOIN_CONTRACT_ANOMALY_AGGRESSIVE_BUY_RATIO",
                0.60,
            ),
            altcoin_contract_anomaly_aggressive_sell_ratio=env_float(
                "ALTCOIN_CONTRACT_ANOMALY_AGGRESSIVE_SELL_RATIO",
                0.40,
            ),
            altcoin_contract_anomaly_open_interest_move_ratio=env_float(
                "ALTCOIN_CONTRACT_ANOMALY_OPEN_INTEREST_MOVE_RATIO",
                0.03,
            ),
            altcoin_contract_anomaly_funding_positive_rate=env_float(
                "ALTCOIN_CONTRACT_ANOMALY_FUNDING_POSITIVE_RATE",
                0.0005,
            ),
            altcoin_contract_anomaly_funding_change_ratio=env_float(
                "ALTCOIN_CONTRACT_ANOMALY_FUNDING_CHANGE_RATIO",
                0.0001,
            ),
            altcoin_contract_anomaly_liquidation_min_usd=env_float(
                "ALTCOIN_CONTRACT_ANOMALY_LIQUIDATION_MIN_USD",
                100_000,
            ),
            altcoin_contract_anomaly_price_stall_ratio=env_float(
                "ALTCOIN_CONTRACT_ANOMALY_PRICE_STALL_RATIO",
                0.003,
            ),
            altcoin_contract_anomaly_weakening_volume_ratio=env_float(
                "ALTCOIN_CONTRACT_ANOMALY_WEAKENING_VOLUME_RATIO",
                1.2,
            ),
            altcoin_contract_anomaly_weakening_windows=env_bounded_int(
                "ALTCOIN_CONTRACT_ANOMALY_WEAKENING_WINDOWS",
                2,
                1,
                100,
            ),
            altcoin_contract_anomaly_realtime_state_path=data_path(
                data_dir,
                "ALTCOIN_CONTRACT_ANOMALY_REALTIME_STATE_FILE",
                "altcoin_contract_anomaly_p2_state.json",
            ),
            altcoin_contract_anomaly_realtime_event_path=data_path(
                data_dir,
                "ALTCOIN_CONTRACT_ANOMALY_REALTIME_EVENT_FILE",
                "altcoin_contract_anomaly_p2_events.jsonl",
            ),
            altcoin_contract_anomaly_smoke_duration_sec=env_bounded_int(
                "ALTCOIN_CONTRACT_ANOMALY_SMOKE_DURATION_SEC",
                900,
                30,
                3_600,
            ),
            altcoin_contract_anomaly_production_enable=env_bool(
                "ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_ENABLE",
                False,
            ),
            altcoin_contract_anomaly_production_send_enable=env_bool(
                "ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_SEND_ENABLE",
                False,
            ),
            altcoin_contract_anomaly_production_send_confirm=os.getenv(
                "ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_SEND_CONFIRM",
                "",
            ).strip(),
            altcoin_contract_anomaly_production_manifest_refresh_sec=env_bounded_int(
                "ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_MANIFEST_REFRESH_SEC",
                1800,
                300,
                86_400,
            ),
            altcoin_contract_anomaly_production_manifest_retry_sec=env_bounded_int(
                "ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_MANIFEST_RETRY_SEC",
                60,
                10,
                3_600,
            ),
            altcoin_contract_anomaly_production_manifest_max_age_sec=env_bounded_int(
                "ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_MANIFEST_MAX_AGE_SEC",
                2400,
                300,
                86_400,
            ),
            altcoin_contract_anomaly_production_cooldown_sec=env_bounded_int(
                "ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_COOLDOWN_SEC",
                3600,
                60,
                604_800,
            ),
            altcoin_contract_anomaly_production_hourly_limit=env_bounded_int(
                "ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_HOURLY_LIMIT",
                20,
                1,
                1_000,
            ),
            altcoin_contract_anomaly_production_daily_limit=env_bounded_int(
                "ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_DAILY_LIMIT",
                50,
                1,
                10_000,
            ),
            altcoin_contract_anomaly_production_queue_size=env_bounded_int(
                "ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_QUEUE_SIZE",
                256,
                1,
                10_000,
            ),
            altcoin_contract_anomaly_production_status_interval_sec=env_bounded_int(
                "ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_STATUS_INTERVAL_SEC",
                30,
                5,
                3_600,
            ),
            altcoin_contract_anomaly_production_oi_budget_window_sec=env_bounded_int(
                "ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_OI_BUDGET_WINDOW_SEC",
                300,
                300,
                86_400,
            ),
            altcoin_contract_anomaly_production_observation_state_path=data_path(
                data_dir,
                "ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_OBSERVATION_STATE_FILE",
                "altcoin_contract_anomaly_production_observation_state.json",
            ),
            altcoin_contract_anomaly_production_observation_event_path=data_path(
                data_dir,
                "ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_OBSERVATION_EVENT_FILE",
                "altcoin_contract_anomaly_production_observation_events.jsonl",
            ),
            altcoin_contract_anomaly_production_state_path=data_path(
                data_dir,
                "ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_STATE_FILE",
                "altcoin_contract_anomaly_production_state.json",
            ),
            altcoin_contract_anomaly_production_outbox_path=data_path(
                data_dir,
                "ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_OUTBOX_FILE",
                "altcoin_contract_anomaly_production_outbox.json",
            ),
            altcoin_contract_anomaly_production_status_path=data_path(
                data_dir,
                "ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_STATUS_FILE",
                "altcoin_contract_anomaly_production_status.json",
            ),
            altcoin_contract_anomaly_realtime_lock_path=data_path(
                data_dir,
                "ALTCOIN_CONTRACT_ANOMALY_REALTIME_LOCK_FILE",
                "altcoin_contract_anomaly_realtime.lock",
            ),
            radar_scan_limit=env_int("RADAR_SCAN_LIMIT", env_int("BN_SCAN_LIMIT", 120)),
            radar_summary_enable=reloadable_bool("RADAR_SUMMARY_ENABLE", True),
            radar_min_quote_volume=env_float("RADAR_MIN_QUOTE_VOLUME", env_float("BN_MIN_QUOTE_VOLUME", 5_000_000)),
            radar_top_n=env_int("RADAR_TOP_N", 8),
            radar_summary_min_interval_sec=env_int("RADAR_SUMMARY_MIN_INTERVAL_SEC", 6 * 3600),
            radar_summary_close_delay_sec=env_int("RADAR_SUMMARY_CLOSE_DELAY_SEC", 300),
            radar_summary_max_daily_push=env_int("RADAR_SUMMARY_MAX_DAILY_PUSH", 4),
            radar_state_path=data_path(data_dir, "RADAR_STATE_FILE", "radar_state.json"),
            funding_snapshot_path=data_path(data_dir, "FUNDING_SNAPSHOT_FILE", "funding_snapshot.json"),
            accumulation_min_history_days=env_int("ACCUMULATION_MIN_HISTORY_DAYS", 45),
            accumulation_max_range_pct=env_float("ACCUMULATION_MAX_RANGE_PCT", 80.0),
            accumulation_max_abs_slope_pct=env_float("ACCUMULATION_MAX_ABS_SLOPE_PCT", 20.0),
            accumulation_max_avg_daily_quote_volume=env_float(
                "ACCUMULATION_MAX_AVG_DAILY_QUOTE_VOLUME",
                20_000_000,
            ),
            accumulation_recent_days=env_int("ACCUMULATION_RECENT_DAYS", 7),
            accumulation_max_recent_price_gain_pct=env_float(
                "ACCUMULATION_MAX_RECENT_PRICE_GAIN_PCT",
                300.0,
            ),
            flow_scan_limit=env_int("FLOW_SCAN_LIMIT", 24),
            flow_radar_enable=reloadable_bool("FLOW_RADAR_ENABLE", True),
            flow_candidate_state_path=data_path(
                data_dir,
                "FLOW_CANDIDATE_STATE_FILE",
                "flow_candidate_state.json",
            ),
            flow_top_n=env_int("FLOW_TOP_N", 5),
            flow_min_score=env_int("FLOW_MIN_SCORE", 60),
            flow_interval_sec=env_int("FLOW_INTERVAL_SEC", 3600),
            flow_close_delay_sec=env_int("FLOW_CLOSE_DELAY_SEC", 300),
            flow_spot_net_ratio_min_pct=env_float("FLOW_SPOT_NET_RATIO_MIN_PCT", 3.0),
            flow_futures_net_ratio_min_pct=env_float("FLOW_FUTURES_NET_RATIO_MIN_PCT", 2.0),
            flow_spot_net_min_usd=env_float("FLOW_SPOT_NET_MIN_USD", 10_000),
            flow_futures_net_min_usd=env_float("FLOW_FUTURES_NET_MIN_USD", 25_000),
            flow_price_move_min_pct=env_float("FLOW_PRICE_MOVE_MIN_PCT", 1.0),
            flow_price_flat_max_pct=env_float("FLOW_PRICE_FLAT_MAX_PCT", 1.5),
            flow_oi_build_min_pct=env_float("FLOW_OI_BUILD_MIN_PCT", 2.0),
            flow_oi_unwind_max_pct=env_float("FLOW_OI_UNWIND_MAX_PCT", -1.5),
            funding_alert_enable=reloadable_bool("FUNDING_ALERT_ENABLE", True),
            funding_alert_interval_sec=env_int("FUNDING_ALERT_INTERVAL_SEC", 180),
            funding_alert_scan_limit=env_int("FUNDING_ALERT_SCAN_LIMIT", 120),
            funding_scan_concurrency=env_int("FUNDING_SCAN_CONCURRENCY", 8),
            funding_request_timeout_sec=env_int("FUNDING_REQUEST_TIMEOUT_SEC", 8),
            funding_max_symbols_per_batch=env_int("FUNDING_MAX_SYMBOLS_PER_BATCH", 120),
            funding_alert_min_quote_volume=env_float("FUNDING_ALERT_MIN_QUOTE_VOLUME", 5_000_000),
            funding_alert_exchanges=env_csv("FUNDING_ALERT_EXCHANGES", ("BINANCE",)),
            funding_alert_history_limit=env_int("FUNDING_ALERT_HISTORY_LIMIT", 4),
            funding_alert_cooldown_sec=env_int("FUNDING_ALERT_COOLDOWN_SEC", 3600),
            funding_alert_extreme_negative_pct=env_float("FUNDING_ALERT_EXTREME_NEGATIVE_PCT", -0.5),
            funding_alert_super_negative_pct=env_float("FUNDING_ALERT_SUPER_NEGATIVE_PCT", -1.0),
            funding_alert_extreme_positive_pct=env_float("FUNDING_ALERT_EXTREME_POSITIVE_PCT", 0.5),
            funding_alert_min_exchange_count=env_int("FUNDING_ALERT_MIN_EXCHANGE_COUNT", 1),
            funding_alert_divergence_pct=env_float("FUNDING_ALERT_DIVERGENCE_PCT", 0.75),
            funding_alert_decay_quiet_scans=env_int("FUNDING_ALERT_DECAY_QUIET_SCANS", 2),
            funding_alert_end_quiet_scans=env_int("FUNDING_ALERT_END_QUIET_SCANS", 5),
            funding_alert_state_path=data_path(data_dir, "FUNDING_ALERT_STATE_FILE", "funding_alert_state.json"),
            funding_flip_oi_enable=env_bool("FUNDING_FLIP_OI_ENABLE", False),
            funding_flip_oi_state_path=data_path(
                data_dir,
                "FUNDING_FLIP_OI_STATE_FILE",
                "funding_flip_oi_state.json",
            ),
            funding_flip_oi_window_points=env_int("FUNDING_FLIP_OI_WINDOW_POINTS", 48),
            funding_flip_oi_min_coverage=env_float("FUNDING_FLIP_OI_MIN_COVERAGE", 0.90),
            funding_flip_oi_max_age_sec=env_int("FUNDING_FLIP_OI_MAX_AGE_SEC", 3 * 3600),
            funding_flip_oi_min_growth_pct=env_float("FUNDING_FLIP_OI_MIN_GROWTH_PCT", 8.0),
            funding_flip_oi_segment_tolerance_pct=env_float(
                "FUNDING_FLIP_OI_SEGMENT_TOLERANCE_PCT",
                0.5,
            ),
            funding_flip_oi_rate_max_age_sec=env_int(
                "FUNDING_FLIP_OI_RATE_MAX_AGE_SEC",
                15 * 60,
            ),
            funding_flip_oi_cooldown_sec=env_int(
                "FUNDING_FLIP_OI_COOLDOWN_SEC",
                24 * 3600,
            ),
            oi_hist_budget=env_int("OI_HIST_REQUEST_BUDGET", 80),
            kline_budget=env_int("KLINE_REQUEST_BUDGET", 120),
            funding_history_budget=env_int("FUNDING_HISTORY_REQUEST_BUDGET", 25),
            fuse_seconds=env_int("DATA_SOURCE_FUSE_SECONDS", 15 * 60),
            pulse_simple_scan_limit=reloadable_bounded_int_alias(
                "SIMPLE_ALERT_SCAN_LIMIT",
                "LAUNCH_SCAN_LIMIT",
                120,
                1,
                1000,
            ),
            pulse_divergence_scan_limit=reloadable_bounded_int_alias(
                "DIVERGENCE_SCAN_LIMIT",
                "LAUNCH_SCAN_LIMIT",
                200,
                1,
                1000,
            ),
            pulse_radar_enable=reloadable_bool_alias(
                "PULSE_RADAR_ENABLE",
                "LAUNCH_ALERT_ENABLE",
                True,
            ),
            topic_message_cleanup_max_age_sec=env_int(
                "TOPIC_MESSAGE_CLEANUP_MAX_AGE_SEC",
                env_int("LAUNCH_MESSAGE_CLEANUP_MAX_AGE_SEC", 47 * 3600),
            ),
            announcement_state_path=data_path(data_dir, "ANNOUNCEMENT_STATE_FILE", "announcement_state.json"),
            announcement_risk_enable=reloadable_bool(
                "ANNOUNCEMENT_RISK_ENABLE",
                True,
            ),
            announcement_page_size=env_int("ANNOUNCEMENT_PAGE_SIZE", 50),
            announcement_only_today=env_bool("ANNOUNCEMENT_ONLY_TODAY", True),
            announcement_default_ttl_days=env_int("ANNOUNCEMENT_DEFAULT_TTL_DAYS", 3),
            divergence_state_path=data_path(data_dir, "OI_DIVERGENCE_STATE_FILE", "oi_divergence_state.json"),
            divergence_cooldown_path=data_path(data_dir, "OI_DIVERGENCE_COOLDOWN_FILE", "oi_divergence_cooldown.json"),
        )

    def redacted_status(self) -> dict[str, Any]:
        return {
            "scope": "telegram-bot-only",
            "base_dir": str(self.base_dir),
            "data_dir": str(self.data_dir),
            "env_file_exists": active_env_file().exists(),
            "telegram": {
                "bot_token_configured": bool(self.tg_bot_token),
                "chat_id_configured": bool(self.tg_chat_id),
                "topic_id_configured": bool(self.tg_topic_id),
                "topic_routes_configured": {
                    "radar_summary": bool(self.tg_radar_summary_topic_id),
                    "launch_alert": bool(self.tg_launch_alert_topic_id),
                    "announcement_alert": bool(self.tg_announcement_alert_topic_id),
                    "test": bool(self.tg_test_topic_id),
                    "flow_radar": bool(self.tg_flow_radar_topic_id),
                    "funding_alert": bool(self.tg_funding_alert_topic_id),
                    "altcoin_contract_anomaly": bool(
                        self.tg_altcoin_contract_anomaly_topic_id
                    ),
                },
                "topic_routes_file": str(self.tg_topic_routes_path),
                "private_control": {
                    "enabled": self.tg_private_control_enable,
                    "admin_configured": bool(
                        self.tg_private_control_admin_user_id
                    ),
                    "fault_alerts_enabled": self.tg_private_control_alert_enable,
                    "fault_alert_cooldown_sec": (
                        self.tg_private_control_alert_cooldown_sec
                    ),
                },
                "topic_management": "manual_only",
                "topic_intro_pin": self.tg_topic_intro_pin,
                "use_topic": self.tg_use_topic,
                "outbox_file": str(self.tg_outbox_path),
                "outbox_quarantine_sec": self.tg_outbox_quarantine_sec,
            },
            "bot_data": {
                "signal_events_db_file": str(self.signal_events_db_path),
                "signal_events_db_exists": self.signal_events_db_path.exists(),
                "market_snapshots_db_file": str(self.market_snapshots_db_path),
                "market_snapshots_db_exists": self.market_snapshots_db_path.exists(),
                "realtime_features_db_file": str(self.realtime_features_db_path),
                "realtime_features_db_exists": self.realtime_features_db_path.exists(),
                "realtime_market_bucket_sec": self.realtime_market_bucket_sec,
                "realtime_market_symbol_limit": self.realtime_market_symbol_limit,
                "realtime_market_retention_days": self.realtime_market_retention_days,
                "realtime_market_symbol_refresh_sec": self.realtime_market_symbol_refresh_sec,
                "realtime_exchanges": {"binance": True},
                "market_snapshot_interval_sec": self.market_snapshot_interval_sec,
                "market_snapshot_retention_days": self.market_snapshot_retention_days,
                "market_snapshot_limit": self.market_snapshot_limit,
                "market_snapshot_oi_limit": self.market_snapshot_oi_limit,
                "market_snapshot_workers": self.market_snapshot_workers,
                "market_flow_fact_interval_sec": self.market_flow_fact_interval_sec,
                "market_flow_fact_limit": self.market_flow_fact_limit,
                "market_readiness_target_days": self.market_readiness_target_days,
                "signal_events_limit": self.signal_events_limit,
                "signal_events_retention_days": self.signal_events_retention_days,
                "database_backup_dir": str(self.database_backup_dir),
                "database_backup_retention_days": self.database_backup_retention_days,
            },
            "runtime": {
                "status_file": str(self.runtime_status_path),
                "cleanup_enable": self.cleanup_enable,
                "cleanup_interval_sec": self.cleanup_interval_sec,
                "cleanup_state_file": str(self.cleanup_state_path),
                "health_database_backup_max_age_sec": self.health_database_backup_max_age_sec,
            },
            "http": {
                "futures_base_url": self.binance_fapi_base_url,
                "spot_base_url": self.binance_spot_base_url,
                "timeout_sec": self.http_timeout_sec,
                "retry": self.http_retry,
                "cache_enable": self.http_cache_enable,
                "cache_ttl_sec": self.http_cache_ttl_sec,
            },
            "altcoin_contract_anomaly": {
                "enabled": self.altcoin_contract_anomaly_enable,
                "cmc_api_key_configured": bool(
                    self.altcoin_contract_anomaly_cmc_api_key
                ),
                "cmc_connect_timeout_sec": (
                    self.altcoin_contract_anomaly_cmc_connect_timeout_sec
                ),
                "cmc_read_timeout_sec": (
                    self.altcoin_contract_anomaly_cmc_read_timeout_sec
                ),
                "cmc_retry": self.altcoin_contract_anomaly_cmc_retry,
                "cmc_backoff_base_sec": (
                    self.altcoin_contract_anomaly_cmc_backoff_base_sec
                ),
                "cmc_min_request_interval_sec": (
                    self.altcoin_contract_anomaly_cmc_min_request_interval_sec
                ),
                "cmc_batch_size": self.altcoin_contract_anomaly_cmc_batch_size,
                "cmc_cache_ttl_sec": (
                    self.altcoin_contract_anomaly_cmc_cache_ttl_sec
                ),
                "cmc_max_data_age_sec": (
                    self.altcoin_contract_anomaly_cmc_max_data_age_sec
                ),
                "cmc_cache_file": str(
                    self.altcoin_contract_anomaly_cmc_cache_path
                ),
                "candidate_snapshot_file": str(
                    self.altcoin_contract_anomaly_candidate_snapshot_path
                ),
                "mapping_overrides_file": str(
                    self.altcoin_contract_anomaly_mapping_overrides_path
                ),
                "candidate_refresh_sec": (
                    self.altcoin_contract_anomaly_candidate_refresh_sec
                ),
                "market_cap_max_usd": (
                    self.altcoin_contract_anomaly_market_cap_max_usd
                ),
                "short_squeeze_min_oi_market_cap_ratio": (
                    self.altcoin_contract_anomaly_short_squeeze_min_oi_market_cap_ratio
                ),
                "short_squeeze_max_funding_rate": (
                    self.altcoin_contract_anomaly_short_squeeze_max_funding_rate
                ),
                "high_leverage_min_oi_market_cap_ratio": (
                    self.altcoin_contract_anomaly_high_leverage_min_oi_market_cap_ratio
                ),
                "binance_oi_max_age_sec": (
                    self.altcoin_contract_anomaly_binance_oi_max_age_sec
                ),
                "funding_max_age_sec": (
                    self.altcoin_contract_anomaly_funding_max_age_sec
                ),
                "oi_workers": self.altcoin_contract_anomaly_oi_workers,
                "oi_request_budget": (
                    self.altcoin_contract_anomaly_oi_request_budget
                ),
                "realtime": {
                    "enabled": self.altcoin_contract_anomaly_realtime_enable,
                    "manifest_poll_sec": self.altcoin_contract_anomaly_manifest_poll_sec,
                    "manifest_max_age_sec": self.altcoin_contract_anomaly_manifest_max_age_sec,
                    "subscription_batch_size": (
                        self.altcoin_contract_anomaly_subscription_batch_size
                    ),
                    "subscription_min_interval_sec": (
                        self.altcoin_contract_anomaly_subscription_min_interval_sec
                    ),
                    "subscription_ack_timeout_sec": (
                        self.altcoin_contract_anomaly_subscription_ack_timeout_sec
                    ),
                    "max_streams": self.altcoin_contract_anomaly_max_streams,
                    "data_max_age_sec": (
                        self.altcoin_contract_anomaly_realtime_data_max_age_sec
                    ),
                    "funding_max_gap_sec": (
                        self.altcoin_contract_anomaly_funding_max_gap_sec
                    ),
                    "oi_refresh_sec": self.altcoin_contract_anomaly_oi_refresh_sec,
                    "oi_max_age_sec": (
                        self.altcoin_contract_anomaly_realtime_oi_max_age_sec
                    ),
                    "oi_workers": self.altcoin_contract_anomaly_realtime_oi_workers,
                    "oi_request_budget": (
                        self.altcoin_contract_anomaly_realtime_oi_request_budget
                    ),
                    "feature_1m_window_sec": (
                        self.altcoin_contract_anomaly_feature_1m_window_sec
                    ),
                    "feature_5m_window_sec": (
                        self.altcoin_contract_anomaly_feature_5m_window_sec
                    ),
                    "volume_baseline_buckets": (
                        self.altcoin_contract_anomaly_volume_baseline_buckets
                    ),
                    "volume_min_samples": (
                        self.altcoin_contract_anomaly_volume_min_samples
                    ),
                    "volume_min_coverage": (
                        self.altcoin_contract_anomaly_volume_min_coverage
                    ),
                    "price_1m_move_ratio": (
                        self.altcoin_contract_anomaly_price_1m_move_ratio
                    ),
                    "price_5m_move_ratio": (
                        self.altcoin_contract_anomaly_price_5m_move_ratio
                    ),
                    "volume_expansion_ratio": (
                        self.altcoin_contract_anomaly_volume_expansion_ratio
                    ),
                    "aggressive_buy_ratio": (
                        self.altcoin_contract_anomaly_aggressive_buy_ratio
                    ),
                    "aggressive_sell_ratio": (
                        self.altcoin_contract_anomaly_aggressive_sell_ratio
                    ),
                    "open_interest_move_ratio": (
                        self.altcoin_contract_anomaly_open_interest_move_ratio
                    ),
                    "funding_positive_rate": (
                        self.altcoin_contract_anomaly_funding_positive_rate
                    ),
                    "funding_change_ratio": (
                        self.altcoin_contract_anomaly_funding_change_ratio
                    ),
                    "liquidation_min_usd": (
                        self.altcoin_contract_anomaly_liquidation_min_usd
                    ),
                    "price_stall_ratio": (
                        self.altcoin_contract_anomaly_price_stall_ratio
                    ),
                    "weakening_volume_ratio": (
                        self.altcoin_contract_anomaly_weakening_volume_ratio
                    ),
                    "weakening_windows": (
                        self.altcoin_contract_anomaly_weakening_windows
                    ),
                    "state_file": str(
                        self.altcoin_contract_anomaly_realtime_state_path
                    ),
                    "event_file": str(
                        self.altcoin_contract_anomaly_realtime_event_path
                    ),
                    "smoke_duration_sec": (
                        self.altcoin_contract_anomaly_smoke_duration_sec
                    ),
                },
                "production": {
                    "enabled": self.altcoin_contract_anomaly_production_enable,
                    "send_enabled": (
                        self.altcoin_contract_anomaly_production_send_enable
                    ),
                    "send_confirm_configured": bool(
                        self.altcoin_contract_anomaly_production_send_confirm
                    ),
                    "topic_configured": bool(
                        self.tg_altcoin_contract_anomaly_topic_id
                    ),
                    "manifest_refresh_sec": (
                        self.altcoin_contract_anomaly_production_manifest_refresh_sec
                    ),
                    "manifest_retry_sec": (
                        self.altcoin_contract_anomaly_production_manifest_retry_sec
                    ),
                    "manifest_max_age_sec": (
                        self.altcoin_contract_anomaly_production_manifest_max_age_sec
                    ),
                    "cooldown_sec": (
                        self.altcoin_contract_anomaly_production_cooldown_sec
                    ),
                    "hourly_limit": (
                        self.altcoin_contract_anomaly_production_hourly_limit
                    ),
                    "daily_limit": (
                        self.altcoin_contract_anomaly_production_daily_limit
                    ),
                    "queue_size": (
                        self.altcoin_contract_anomaly_production_queue_size
                    ),
                    "status_interval_sec": (
                        self.altcoin_contract_anomaly_production_status_interval_sec
                    ),
                    "oi_budget_window_sec": (
                        self.altcoin_contract_anomaly_production_oi_budget_window_sec
                    ),
                    "observation_state_file": str(
                        self.altcoin_contract_anomaly_production_observation_state_path
                    ),
                    "observation_event_file": str(
                        self.altcoin_contract_anomaly_production_observation_event_path
                    ),
                    "state_file": str(
                        self.altcoin_contract_anomaly_production_state_path
                    ),
                    "outbox_file": str(
                        self.altcoin_contract_anomaly_production_outbox_path
                    ),
                    "status_file": str(
                        self.altcoin_contract_anomaly_production_status_path
                    ),
                    "realtime_lock_file": str(
                        self.altcoin_contract_anomaly_realtime_lock_path
                    ),
                },
            },
            "filters": {
                "excluded_base_assets": list(self.excluded_base_assets),
            },
            "budgets": {
                "oi_hist": self.oi_hist_budget,
                "klines": self.kline_budget,
                "spot_klines": self.kline_budget,
                "funding_history": self.funding_history_budget,
            },
            "radar": {
                "enabled": self.radar_summary_enable,
                "scan_limit": self.radar_scan_limit,
                "min_quote_volume": self.radar_min_quote_volume,
                "top_n": self.radar_top_n,
                "summary_min_interval_sec": self.radar_summary_min_interval_sec,
                "summary_max_daily_push": self.radar_summary_max_daily_push,
                "accumulation_quality_evidence": "summary_diagnostic_only",
            },
            "flow_radar": {
                "enabled": self.flow_radar_enable,
                "scan_limit": self.flow_scan_limit,
                "candidate_pool": "unlimited",
                "candidate_state_file": str(self.flow_candidate_state_path),
                "top_n": self.flow_top_n,
                "min_score": self.flow_min_score,
                "interval_sec": self.flow_interval_sec,
                "spot_net_ratio_min_pct": self.flow_spot_net_ratio_min_pct,
                "futures_net_ratio_min_pct": self.flow_futures_net_ratio_min_pct,
                "spot_net_min_usd": self.flow_spot_net_min_usd,
                "futures_net_min_usd": self.flow_futures_net_min_usd,
                "price_move_min_pct": self.flow_price_move_min_pct,
                "price_flat_max_pct": self.flow_price_flat_max_pct,
                "oi_build_min_pct": self.flow_oi_build_min_pct,
                "oi_unwind_max_pct": self.flow_oi_unwind_max_pct,
            },
            "funding_alert": {
                "enable": self.funding_alert_enable,
                "interval_sec": self.funding_alert_interval_sec,
                "scan_limit": self.funding_alert_scan_limit,
                "scan_concurrency": self.funding_scan_concurrency,
                "request_timeout_sec": self.funding_request_timeout_sec,
                "max_symbols_per_batch": self.funding_max_symbols_per_batch,
                "min_quote_volume": self.funding_alert_min_quote_volume,
                "exchanges": list(self.funding_alert_exchanges),
                "history_limit": self.funding_alert_history_limit,
                "cooldown_sec": self.funding_alert_cooldown_sec,
                "extreme_negative_pct": self.funding_alert_extreme_negative_pct,
                "super_negative_pct": self.funding_alert_super_negative_pct,
                "extreme_positive_pct": self.funding_alert_extreme_positive_pct,
                "min_exchange_count": self.funding_alert_min_exchange_count,
                "divergence_pct": self.funding_alert_divergence_pct,
                "decay_quiet_scans": self.funding_alert_decay_quiet_scans,
                "end_quiet_scans": self.funding_alert_end_quiet_scans,
                "state_file": str(self.funding_alert_state_path),
                "flip_oi_enable": self.funding_flip_oi_enable,
                "flip_oi_min_growth_pct": self.funding_flip_oi_min_growth_pct,
                "flip_oi_state_file": str(self.funding_flip_oi_state_path),
            },
            "pulse": {
                "enabled": self.pulse_radar_enable,
                "simple_scan_limit": self.pulse_simple_scan_limit,
                "divergence_scan_limit": self.pulse_divergence_scan_limit,
                "simple_interval_sec": 15 * 60,
                "divergence_interval_sec": 2 * 3600,
                "simple_state_file": str(
                    self.data_dir / "simple_alert_state.json"
                ),
                "review_file": str(self.data_dir / "review_signals.json"),
                "telegram_template": "TG_LAUNCH_ALERT",
                "legacy_launch_warning_available": False,
            },
            "announcement_risk": {
                "enabled": self.announcement_risk_enable,
                "page_size": self.announcement_page_size,
                "standalone_push": True,
                "pulse_classification_input": False,
            },
        }
