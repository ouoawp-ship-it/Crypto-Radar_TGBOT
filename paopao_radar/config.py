from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env.oi"


def load_env_file(path: Path = ENV_FILE) -> dict[str, str]:
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


def env_list(name: str, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return tuple(part.strip() for part in value.split(",") if part.strip())


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
    tg_auto_create_topics: bool = True
    tg_topic_routes_path: Path = BASE_DIR / "data" / "tg_topic_routes.json"
    tg_topic_intro_enable: bool = True
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
    signal_events_path: Path = BASE_DIR / "data" / "signal_events.json"
    signal_events_db_path: Path = BASE_DIR / "data" / "signals.db"
    market_snapshots_db_path: Path = BASE_DIR / "data" / "market_snapshots.db"
    realtime_features_db_path: Path = BASE_DIR / "data" / "realtime_features.db"
    news_events_db_path: Path = BASE_DIR / "data" / "news_events.db"
    news_events_retention_days: int = 90
    news_events_limit: int = 5000
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
    realtime_bybit_enable: bool = True
    realtime_okx_enable: bool = True
    coinglass_enable: bool = False
    coinglass_api_key: str = ""
    coinglass_api_base_url: str = "https://open-api-v4.coinglass.com"
    coinglass_rate_limit_per_minute: int = 80
    coinalyze_enable: bool = False
    coinalyze_api_key: str = ""
    coinalyze_base_url: str = "https://api.coinalyze.net/v1"
    coinalyze_rate_limit_per_minute: int = 40
    derivatives_validation_symbol_limit: int = 8
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
    bybit_public_rest_url: str = "https://api.bybit.com"
    bybit_linear_ws_url: str = "wss://stream.bybit.com/v5/public/linear"
    okx_public_rest_url: str = "https://www.okx.com"
    okx_public_ws_url: str = "wss://ws.okx.com:8443/ws/v5/public"
    excluded_base_assets: tuple[str, ...] = ("XAU", "XAG")

    radar_scan_limit: int = 120
    radar_min_quote_volume: float = 5_000_000
    radar_top_n: int = 8
    radar_summary_min_interval_sec: int = 6 * 3600
    radar_summary_close_delay_sec: int = 300
    radar_summary_max_daily_push: int = 4
    radar_state_path: Path = BASE_DIR / "data" / "radar_state.json"
    funding_snapshot_path: Path = BASE_DIR / "data" / "funding_snapshot.json"

    flow_scan_limit: int = 12
    flow_candidate_pool: int = 50
    flow_top_n: int = 8
    flow_min_score: int = 50
    flow_interval_sec: int = 3600
    flow_close_delay_sec: int = 300

    funding_alert_enable: bool = True
    funding_alert_interval_sec: int = 180
    funding_alert_scan_limit: int = 120
    funding_scan_concurrency: int = 8
    funding_request_timeout_sec: int = 8
    funding_max_symbols_per_batch: int = 120
    funding_alert_min_quote_volume: float = 5_000_000
    funding_alert_exchanges: tuple[str, ...] = ("BINANCE", "OKX", "BYBIT", "BITGET", "GATE")
    funding_alert_history_limit: int = 4
    funding_alert_cooldown_sec: int = 3600
    funding_alert_extreme_negative_pct: float = -0.5
    funding_alert_super_negative_pct: float = -1.0
    funding_alert_extreme_positive_pct: float = 0.5
    funding_alert_min_exchange_count: int = 2
    funding_alert_divergence_pct: float = 0.75
    funding_alert_reply_chain_enable: bool = True
    funding_alert_decay_quiet_scans: int = 2
    funding_alert_end_quiet_scans: int = 5
    funding_alert_state_path: Path = BASE_DIR / "data" / "funding_alert_state.json"

    oi_hist_budget: int = 80
    kline_budget: int = 120
    funding_history_budget: int = 25
    fuse_seconds: int = 15 * 60

    launch_scan_limit: int = 80
    launch_multi_exchange_funding_enable: bool = True
    launch_funding_exchanges: tuple[str, ...] = ("BINANCE", "OKX", "BYBIT", "BITGET", "GATE")
    launch_funding_history_limit: int = 4
    launch_state_path: Path = BASE_DIR / "data" / "launch_state.json"
    launch_watchlist_path: Path = BASE_DIR / "data" / "launch_watchlist.json"
    launch_watch_history_path: Path = BASE_DIR / "data" / "launch_watch_history.json"
    launch_watch_history_limit: int = 500
    launch_min_score_push: int = 60
    launch_watch_score: int = 45
    launch_primed_score: int = 60
    launch_breakout_score: int = 75
    launch_launched_score: int = 90
    launch_close_delay_sec: int = 60
    launch_stage_cooldown_sec: int = 6 * 3600
    launch_state_ttl_sec: int = 48 * 3600
    launch_failed_ttl_sec: int = 24 * 3600

    announcement_state_path: Path = BASE_DIR / "data" / "announcement_state.json"
    announcement_page_size: int = 50
    announcement_only_today: bool = True
    announcement_default_ttl_days: int = 3

    divergence_state_path: Path = BASE_DIR / "data" / "oi_divergence_state.json"
    divergence_cooldown_path: Path = BASE_DIR / "data" / "oi_divergence_cooldown.json"

    def __post_init__(self) -> None:
        default_signal_path = BASE_DIR / "data" / "signal_events.json"
        default_outbox_path = BASE_DIR / "data" / "tg_outbox.json"
        default_signal_db_path = BASE_DIR / "data" / "signals.db"
        default_market_snapshots_db_path = BASE_DIR / "data" / "market_snapshots.db"
        default_realtime_features_db_path = BASE_DIR / "data" / "realtime_features.db"
        default_news_events_db_path = BASE_DIR / "data" / "news_events.db"
        default_database_backup_dir = BASE_DIR / "data" / "backups"
        if self.data_dir != BASE_DIR / "data" and self.signal_events_path == default_signal_path:
            object.__setattr__(self, "signal_events_path", self.data_dir / "signal_events.json")
        if self.data_dir != BASE_DIR / "data" and self.tg_outbox_path == default_outbox_path:
            object.__setattr__(self, "tg_outbox_path", self.data_dir / "tg_outbox.json")
        if self.data_dir != BASE_DIR / "data" and self.signal_events_db_path == default_signal_db_path:
            object.__setattr__(self, "signal_events_db_path", self.data_dir / "signals.db")
        if self.data_dir != BASE_DIR / "data" and self.market_snapshots_db_path == default_market_snapshots_db_path:
            object.__setattr__(self, "market_snapshots_db_path", self.data_dir / "market_snapshots.db")
        if self.data_dir != BASE_DIR / "data" and self.realtime_features_db_path == default_realtime_features_db_path:
            object.__setattr__(self, "realtime_features_db_path", self.data_dir / "realtime_features.db")
        if self.data_dir != BASE_DIR / "data" and self.news_events_db_path == default_news_events_db_path:
            object.__setattr__(self, "news_events_db_path", self.data_dir / "news_events.db")
        if self.data_dir != BASE_DIR / "data" and self.database_backup_dir == default_database_backup_dir:
            object.__setattr__(self, "database_backup_dir", self.data_dir / "backups")

    @classmethod
    def load(cls) -> "Settings":
        load_env_file()
        data_dir = BASE_DIR / "data"
        return cls(
            data_dir=data_dir,
            tg_bot_token=os.getenv("TG_BOT_TOKEN", ""),
            tg_chat_id=os.getenv("TG_CHAT_ID", ""),
            tg_topic_id=env_first("TG_TOPIC_ID", "TELEGRAM_MESSAGE_THREAD_ID"),
            tg_radar_summary_topic_id=env_first("TG_RADAR_SUMMARY_TOPIC_ID", "TELEGRAM_RADAR_SUMMARY_TOPIC_ID"),
            tg_launch_alert_topic_id=env_first("TG_LAUNCH_ALERT_TOPIC_ID", "TELEGRAM_LAUNCH_ALERT_TOPIC_ID"),
            tg_announcement_alert_topic_id=env_first("TG_ANNOUNCEMENT_ALERT_TOPIC_ID", "TELEGRAM_ANNOUNCEMENT_ALERT_TOPIC_ID"),
            tg_test_topic_id=env_first("TG_TEST_TOPIC_ID", "TELEGRAM_TEST_TOPIC_ID"),
            tg_flow_radar_topic_id=env_first("TG_FLOW_RADAR_TOPIC_ID", "TELEGRAM_FLOW_RADAR_TOPIC_ID"),
            tg_funding_alert_topic_id=env_first("TG_FUNDING_ALERT_TOPIC_ID", "TELEGRAM_FUNDING_ALERT_TOPIC_ID"),
            tg_auto_create_topics=env_bool("TG_AUTO_CREATE_TOPICS", True),
            tg_topic_routes_path=data_path(data_dir, "TG_TOPIC_ROUTES_FILE", "tg_topic_routes.json"),
            tg_topic_intro_enable=env_bool("TG_TOPIC_INTRO_ENABLE", True),
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
            signal_events_path=data_path(data_dir, "SIGNAL_EVENTS_FILE", "signal_events.json"),
            signal_events_db_path=data_path(data_dir, "SIGNAL_EVENTS_DB_FILE", "signals.db"),
            market_snapshots_db_path=data_path(data_dir, "MARKET_SNAPSHOTS_DB_FILE", "market_snapshots.db"),
            realtime_features_db_path=data_path(data_dir, "REALTIME_FEATURES_DB_FILE", "realtime_features.db"),
            news_events_db_path=data_path(data_dir, "NEWS_EVENTS_DB_FILE", "news_events.db"),
            news_events_retention_days=env_int("NEWS_EVENTS_RETENTION_DAYS", 90),
            news_events_limit=env_int("NEWS_EVENTS_LIMIT", 5000),
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
            realtime_bybit_enable=env_bool("REALTIME_BYBIT_ENABLE", True),
            realtime_okx_enable=env_bool("REALTIME_OKX_ENABLE", True),
            coinglass_enable=env_bool("COINGLASS_ENABLE", False),
            coinglass_api_key=os.getenv("COINGLASS_API_KEY", "").strip(),
            coinglass_api_base_url=os.getenv(
                "COINGLASS_API_BASE_URL", "https://open-api-v4.coinglass.com"
            ).rstrip("/"),
            coinglass_rate_limit_per_minute=env_int("COINGLASS_RATE_LIMIT_PER_MINUTE", 80),
            coinalyze_enable=env_bool("COINALYZE_ENABLE", False),
            coinalyze_api_key=os.getenv("COINALYZE_API_KEY", "").strip(),
            coinalyze_base_url=os.getenv(
                "COINALYZE_BASE_URL", "https://api.coinalyze.net/v1"
            ).rstrip("/"),
            coinalyze_rate_limit_per_minute=env_int(
                "COINALYZE_RATE_LIMIT_PER_MINUTE",
                env_int("COINALYZE_REQUEST_BUDGET", 40),
            ),
            derivatives_validation_symbol_limit=env_int("DERIVATIVES_VALIDATION_SYMBOL_LIMIT", 8),
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
            bybit_public_rest_url=os.getenv("BYBIT_PUBLIC_REST_URL", "https://api.bybit.com").rstrip("/"),
            bybit_linear_ws_url=os.getenv("BYBIT_LINEAR_WS_URL", "wss://stream.bybit.com/v5/public/linear").rstrip("/"),
            okx_public_rest_url=os.getenv("OKX_PUBLIC_REST_URL", "https://www.okx.com").rstrip("/"),
            okx_public_ws_url=os.getenv("OKX_PUBLIC_WS_URL", "wss://ws.okx.com:8443/ws/v5/public").rstrip("/"),
            excluded_base_assets=env_csv("EXCLUDED_BASE_ASSETS", ("XAU", "XAG")),
            radar_scan_limit=env_int("RADAR_SCAN_LIMIT", env_int("BN_SCAN_LIMIT", 120)),
            radar_min_quote_volume=env_float("RADAR_MIN_QUOTE_VOLUME", env_float("BN_MIN_QUOTE_VOLUME", 5_000_000)),
            radar_top_n=env_int("RADAR_TOP_N", 8),
            radar_summary_min_interval_sec=env_int("RADAR_SUMMARY_MIN_INTERVAL_SEC", 6 * 3600),
            radar_summary_close_delay_sec=env_int("RADAR_SUMMARY_CLOSE_DELAY_SEC", 300),
            radar_summary_max_daily_push=env_int("RADAR_SUMMARY_MAX_DAILY_PUSH", 4),
            radar_state_path=data_path(data_dir, "RADAR_STATE_FILE", "radar_state.json"),
            funding_snapshot_path=data_path(data_dir, "FUNDING_SNAPSHOT_FILE", "funding_snapshot.json"),
            flow_scan_limit=env_int("FLOW_SCAN_LIMIT", 12),
            flow_candidate_pool=env_int("FLOW_CANDIDATE_POOL", 50),
            flow_top_n=env_int("FLOW_TOP_N", 8),
            flow_min_score=env_int("FLOW_MIN_SCORE", 50),
            flow_interval_sec=env_int("FLOW_INTERVAL_SEC", 3600),
            flow_close_delay_sec=env_int("FLOW_CLOSE_DELAY_SEC", 300),
            funding_alert_enable=env_bool("FUNDING_ALERT_ENABLE", True),
            funding_alert_interval_sec=env_int("FUNDING_ALERT_INTERVAL_SEC", 180),
            funding_alert_scan_limit=env_int("FUNDING_ALERT_SCAN_LIMIT", 120),
            funding_scan_concurrency=env_int("FUNDING_SCAN_CONCURRENCY", 8),
            funding_request_timeout_sec=env_int("FUNDING_REQUEST_TIMEOUT_SEC", 8),
            funding_max_symbols_per_batch=env_int("FUNDING_MAX_SYMBOLS_PER_BATCH", 120),
            funding_alert_min_quote_volume=env_float("FUNDING_ALERT_MIN_QUOTE_VOLUME", 5_000_000),
            funding_alert_exchanges=env_csv("FUNDING_ALERT_EXCHANGES", ("BINANCE", "OKX", "BYBIT", "BITGET", "GATE")),
            funding_alert_history_limit=env_int("FUNDING_ALERT_HISTORY_LIMIT", 4),
            funding_alert_cooldown_sec=env_int("FUNDING_ALERT_COOLDOWN_SEC", 3600),
            funding_alert_extreme_negative_pct=env_float("FUNDING_ALERT_EXTREME_NEGATIVE_PCT", -0.5),
            funding_alert_super_negative_pct=env_float("FUNDING_ALERT_SUPER_NEGATIVE_PCT", -1.0),
            funding_alert_extreme_positive_pct=env_float("FUNDING_ALERT_EXTREME_POSITIVE_PCT", 0.5),
            funding_alert_min_exchange_count=env_int("FUNDING_ALERT_MIN_EXCHANGE_COUNT", 2),
            funding_alert_divergence_pct=env_float("FUNDING_ALERT_DIVERGENCE_PCT", 0.75),
            funding_alert_reply_chain_enable=env_bool("FUNDING_ALERT_REPLY_CHAIN_ENABLE", True),
            funding_alert_decay_quiet_scans=env_int("FUNDING_ALERT_DECAY_QUIET_SCANS", 2),
            funding_alert_end_quiet_scans=env_int("FUNDING_ALERT_END_QUIET_SCANS", 5),
            funding_alert_state_path=data_path(data_dir, "FUNDING_ALERT_STATE_FILE", "funding_alert_state.json"),
            oi_hist_budget=env_int("OI_HIST_REQUEST_BUDGET", 80),
            kline_budget=env_int("KLINE_REQUEST_BUDGET", 120),
            funding_history_budget=env_int("FUNDING_HISTORY_REQUEST_BUDGET", 25),
            fuse_seconds=env_int("DATA_SOURCE_FUSE_SECONDS", 15 * 60),
            launch_scan_limit=env_int("LAUNCH_SCAN_LIMIT", 80),
            launch_multi_exchange_funding_enable=env_bool("LAUNCH_MULTI_EXCHANGE_FUNDING_ENABLE", True),
            launch_funding_exchanges=env_csv("LAUNCH_FUNDING_EXCHANGES", ("BINANCE", "OKX", "BYBIT", "BITGET", "GATE")),
            launch_funding_history_limit=env_int("LAUNCH_FUNDING_HISTORY_LIMIT", 4),
            launch_state_path=data_path(data_dir, "LAUNCH_STATE_FILE", "launch_state.json"),
            launch_watchlist_path=data_path(data_dir, "LAUNCH_WATCHLIST_FILE", "launch_watchlist.json"),
            launch_watch_history_path=data_path(data_dir, "LAUNCH_WATCH_HISTORY_FILE", "launch_watch_history.json"),
            launch_watch_history_limit=env_int("LAUNCH_WATCH_HISTORY_LIMIT", 500),
            launch_min_score_push=env_int("LAUNCH_MIN_SCORE_PUSH", 60),
            launch_watch_score=env_int("LAUNCH_WATCH_SCORE", 45),
            launch_primed_score=env_int("LAUNCH_PRIMED_SCORE", 60),
            launch_breakout_score=env_int("LAUNCH_BREAKOUT_SCORE", 75),
            launch_launched_score=env_int("LAUNCH_LAUNCHED_SCORE", 90),
            launch_close_delay_sec=env_int("LAUNCH_CLOSE_DELAY_SEC", 60),
            launch_stage_cooldown_sec=env_int("LAUNCH_STAGE_COOLDOWN_SEC", 6 * 3600),
            launch_state_ttl_sec=env_int("LAUNCH_STATE_TTL_SEC", 48 * 3600),
            launch_failed_ttl_sec=env_int("LAUNCH_FAILED_TTL_SEC", 24 * 3600),
            announcement_state_path=data_path(data_dir, "ANNOUNCEMENT_STATE_FILE", "announcement_state.json"),
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
            "env_file_exists": ENV_FILE.exists(),
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
                },
                "auto_create_topics": self.tg_auto_create_topics,
                "topic_routes_file": str(self.tg_topic_routes_path),
                "topic_intro_enable": self.tg_topic_intro_enable,
                "topic_intro_pin": self.tg_topic_intro_pin,
                "use_topic": self.tg_use_topic,
                "outbox_file": str(self.tg_outbox_path),
                "outbox_quarantine_sec": self.tg_outbox_quarantine_sec,
            },
            "bot_data": {
                "signal_events_file": str(self.signal_events_path),
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
                "realtime_exchanges": {
                    "binance": True,
                    "bybit": self.realtime_bybit_enable,
                    "okx": self.realtime_okx_enable,
                },
                "market_snapshot_interval_sec": self.market_snapshot_interval_sec,
                "market_snapshot_retention_days": self.market_snapshot_retention_days,
                "market_snapshot_limit": self.market_snapshot_limit,
                "market_snapshot_oi_limit": self.market_snapshot_oi_limit,
                "market_snapshot_workers": self.market_snapshot_workers,
                "market_flow_fact_interval_sec": self.market_flow_fact_interval_sec,
                "market_flow_fact_limit": self.market_flow_fact_limit,
                "market_readiness_target_days": self.market_readiness_target_days,
                "news_events_db_file": str(self.news_events_db_path),
                "news_events_db_exists": self.news_events_db_path.exists(),
                "news_events_retention_days": self.news_events_retention_days,
                "news_events_limit": self.news_events_limit,
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
                "scan_limit": self.radar_scan_limit,
                "min_quote_volume": self.radar_min_quote_volume,
                "top_n": self.radar_top_n,
                "summary_min_interval_sec": self.radar_summary_min_interval_sec,
                "summary_max_daily_push": self.radar_summary_max_daily_push,
            },
            "flow_radar": {
                "scan_limit": self.flow_scan_limit,
                "candidate_pool": self.flow_candidate_pool,
                "top_n": self.flow_top_n,
                "min_score": self.flow_min_score,
                "interval_sec": self.flow_interval_sec,
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
                "reply_chain_enable": self.funding_alert_reply_chain_enable,
                "decay_quiet_scans": self.funding_alert_decay_quiet_scans,
                "end_quiet_scans": self.funding_alert_end_quiet_scans,
                "state_file": str(self.funding_alert_state_path),
            },
            "derivatives_validation": {
                "coinglass_enabled": self.coinglass_enable,
                "coinglass_key_configured": bool(self.coinglass_api_key),
                "coinalyze_enabled": self.coinalyze_enable,
                "coinalyze_key_configured": bool(self.coinalyze_api_key),
                "symbol_limit": self.derivatives_validation_symbol_limit,
            },
            "launch": {
                "scan_limit": self.launch_scan_limit,
                "multi_exchange_funding_enable": self.launch_multi_exchange_funding_enable,
                "funding_exchanges": list(self.launch_funding_exchanges),
                "funding_history_limit": self.launch_funding_history_limit,
                "min_score_push": self.launch_min_score_push,
                "thresholds": {
                    "watching": self.launch_watch_score,
                    "primed": self.launch_primed_score,
                    "breakout": self.launch_breakout_score,
                    "launched": self.launch_launched_score,
                },
                "stage_cooldown_sec": self.launch_stage_cooldown_sec,
                "state_ttl_sec": self.launch_state_ttl_sec,
                "failed_ttl_sec": self.launch_failed_ttl_sec,
                "watch_history_limit": self.launch_watch_history_limit,
            },
            "announcements": {
                "page_size": self.announcement_page_size,
            },
        }
