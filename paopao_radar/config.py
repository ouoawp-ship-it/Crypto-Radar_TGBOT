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
        os.environ.setdefault(key, value)
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


def normalize_ai_model(value: str) -> str:
    text = str(value or "").strip().strip('"').strip("'")
    for _ in range(3):
        key, sep, rest = text.partition("=")
        normalized_key = key.strip().upper().replace("_", "")
        if sep and normalized_key in {"AIMODEL", "MODEL"}:
            text = rest.strip().strip('"').strip("'")
            continue
        break
    return text or "deepseek-v4-pro"


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
    tg_structure_topic_id: str = ""
    tg_structure_review_topic_id: str = ""
    tg_auto_create_topics: bool = True
    tg_topic_routes_path: Path = BASE_DIR / "data" / "tg_topic_routes.json"
    tg_topic_intro_enable: bool = True
    tg_topic_intro_pin: bool = True
    tg_use_topic: bool = False
    tg_push_history_path: Path = BASE_DIR / "data" / "tg_push_history.json"
    tg_push_split_limit: int = 3800
    tg_push_timeout_sec: int = 10
    tg_push_retry: int = 2
    tg_global_hourly_limit: int = 20
    tg_default_cooldown_sec: int = 6 * 3600
    tg_push_history_limit: int = 2000
    tg_push_history_retention_days: int = 30
    ai_assistant_enable: bool = False
    ai_bot_token: str = ""
    ai_admin_user_ids: tuple[str, ...] = ()
    ai_allow_group_chat: bool = False
    ai_allowed_chat_ids: tuple[str, ...] = ()
    ai_price_alerts_enable: bool = True
    ai_price_alerts_db_path: Path = BASE_DIR / "data" / "price_alerts.db"
    ai_default_chat_id: str = ""
    ai_alert_check_interval_sec: int = 30
    ai_poll_timeout_sec: int = 20
    ai_provider_enable: bool = False
    ai_api_key: str = ""
    ai_base_url: str = "https://api.deepseek.com"
    ai_model: str = "deepseek-v4-pro"
    ai_request_timeout_sec: int = 90
    ai_prompts_path: Path = BASE_DIR / "data" / "ai_prompts.json"
    runtime_status_path: Path = BASE_DIR / "data" / "runtime_status.json"
    structure_runtime_status_path: Path = BASE_DIR / "data" / "structure_runtime_status.json"
    cleanup_enable: bool = True
    cleanup_interval_sec: int = 3600
    cleanup_state_path: Path = BASE_DIR / "data" / "cleanup_state.json"
    cleanup_corrupt_retention_days: int = 7
    cleanup_log_retention_days: int = 14

    http_timeout_sec: int = 10
    http_retry: int = 2
    http_backoff_sec: float = 0.8
    http_cache_enable: bool = True
    http_cache_ttl_sec: int = 10
    binance_fapi_base_url: str = "https://fapi.binance.com"
    binance_spot_base_url: str = "https://api.binance.com"
    excluded_base_assets: tuple[str, ...] = ("XAU", "XAG")

    liquidity_fallback_enable: bool = True
    liquidity_score_max_delta: int = 15
    liquidity_min_distance_pct: float = 0.5
    liquidity_max_distance_pct: float = 8.0
    binance_orderbook_liquidity_enable: bool = True
    binance_orderbook_depth_limit: int = 100
    coinalyze_enable: bool = False
    coinalyze_api_key: str = ""
    coinalyze_base_url: str = "https://api.coinalyze.net/v1"
    coinalyze_request_budget: int = 40
    coinalyze_symbol_suffix: str = "_PERP.A"
    coinalyze_liquidation_interval: str = "1hour"
    coinalyze_liquidation_lookback_hours: int = 24

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

    structure_radar_enable: bool = True
    structure_interval: str = "15m"
    structure_higher_interval: str = "1h"
    structure_box_lookback: int = 36
    structure_top_symbols: int = 80
    structure_near_edge_pct: float = 1.5
    structure_min_score: int = 65
    structure_send_chart_top_n: int = 3
    structure_save_charts: bool = True
    structure_delete_chart_after_send: bool = True
    structure_chart_retention_hours: int = 12
    structure_max_chart_files: int = 200
    structure_pre_scan_minute: int = 55
    structure_confirm_delay_sec: int = 300
    structure_cooldown_sec: int = 3600
    structure_state_path: Path = BASE_DIR / "data" / "structure_state.json"
    structure_history_path: Path = BASE_DIR / "data" / "structure_history.json"
    structure_chart_dir: Path = BASE_DIR / "data" / "charts"
    structure_reply_chain_enable: bool = True
    structure_review_enable: bool = True
    structure_review_lookback_hours: int = 24
    structure_review_forward_hours: int = 4
    structure_review_min_age_minutes: int = 15
    structure_review_report_top_n: int = 10
    structure_review_min_sample: int = 10
    structure_review_max_report_interval_sec: int = 3600
    structure_review_path: Path = BASE_DIR / "data" / "structure_review.json"
    structure_stats_path: Path = BASE_DIR / "data" / "structure_stats.json"
    structure_review_report_path: Path = BASE_DIR / "data" / "structure_review_report.txt"

    oi_hist_budget: int = 80
    kline_budget: int = 120
    funding_history_budget: int = 25
    fuse_seconds: int = 15 * 60

    launch_scan_limit: int = 80
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
            tg_structure_topic_id=env_first("STRUCTURE_TOPIC_ID", "TG_STRUCTURE_TOPIC_ID", "TELEGRAM_STRUCTURE_TOPIC_ID"),
            tg_structure_review_topic_id=env_first("STRUCTURE_REVIEW_TOPIC_ID", "TG_STRUCTURE_REVIEW_TOPIC_ID", "TELEGRAM_STRUCTURE_REVIEW_TOPIC_ID"),
            tg_auto_create_topics=env_bool("TG_AUTO_CREATE_TOPICS", True),
            tg_topic_routes_path=data_path(data_dir, "TG_TOPIC_ROUTES_FILE", "tg_topic_routes.json"),
            tg_topic_intro_enable=env_bool("TG_TOPIC_INTRO_ENABLE", True),
            tg_topic_intro_pin=env_bool("TG_TOPIC_INTRO_PIN", True),
            tg_use_topic=env_bool("TELEGRAM_USE_TOPIC", False),
            tg_push_history_path=data_path(data_dir, "TG_PUSH_HISTORY_FILE", "tg_push_history.json"),
            tg_push_split_limit=env_int("TG_PUSH_SPLIT_LIMIT", 3800),
            tg_push_timeout_sec=env_int("TG_PUSH_TIMEOUT_SEC", 10),
            tg_push_retry=env_int("TG_PUSH_RETRY", 2),
            tg_global_hourly_limit=env_int("TG_GLOBAL_HOURLY_LIMIT", 20),
            tg_default_cooldown_sec=env_int("TG_DEFAULT_COOLDOWN_SEC", 6 * 3600),
            tg_push_history_limit=env_int("TG_PUSH_HISTORY_LIMIT", 2000),
            tg_push_history_retention_days=env_int("TG_PUSH_HISTORY_RETENTION_DAYS", 30),
            ai_assistant_enable=env_bool("AI_ASSISTANT_ENABLE", False),
            ai_bot_token=os.getenv("AI_BOT_TOKEN", "").strip(),
            ai_admin_user_ids=env_list("AI_ADMIN_USER_IDS"),
            ai_allow_group_chat=env_bool("AI_ALLOW_GROUP_CHAT", False),
            ai_allowed_chat_ids=env_list("AI_ALLOWED_CHAT_IDS"),
            ai_price_alerts_enable=env_bool("AI_PRICE_ALERTS_ENABLE", True),
            ai_price_alerts_db_path=data_path(data_dir, "AI_PRICE_ALERTS_DB_FILE", "price_alerts.db"),
            ai_default_chat_id=os.getenv("AI_DEFAULT_CHAT_ID", "").strip(),
            ai_alert_check_interval_sec=env_int("AI_ALERT_CHECK_INTERVAL_SEC", 30),
            ai_poll_timeout_sec=env_int("AI_POLL_TIMEOUT_SEC", 20),
            ai_provider_enable=env_bool("AI_PROVIDER_ENABLE", False),
            ai_api_key=os.getenv("AI_API_KEY", "").strip(),
            ai_base_url=os.getenv("AI_BASE_URL", "https://api.deepseek.com").rstrip("/"),
            ai_model=normalize_ai_model(os.getenv("AI_MODEL", "deepseek-v4-pro")),
            ai_request_timeout_sec=env_int("AI_REQUEST_TIMEOUT_SEC", 90),
            ai_prompts_path=data_path(data_dir, "AI_PROMPTS_FILE", "ai_prompts.json"),
            runtime_status_path=data_path(data_dir, "RUNTIME_STATUS_FILE", "runtime_status.json"),
            structure_runtime_status_path=data_path(data_dir, "STRUCTURE_RUNTIME_STATUS_FILE", "structure_runtime_status.json"),
            cleanup_enable=env_bool("CLEANUP_ENABLE", True),
            cleanup_interval_sec=env_int("CLEANUP_INTERVAL_SEC", 3600),
            cleanup_state_path=data_path(data_dir, "CLEANUP_STATE_FILE", "cleanup_state.json"),
            cleanup_corrupt_retention_days=env_int("CLEANUP_CORRUPT_RETENTION_DAYS", 7),
            cleanup_log_retention_days=env_int("CLEANUP_LOG_RETENTION_DAYS", 14),
            http_timeout_sec=env_int("BINANCE_API_TIMEOUT_SEC", env_int("HTTP_TIMEOUT_SEC", 10)),
            http_retry=env_int("BINANCE_API_RETRY", env_int("HTTP_RETRY", 2)),
            http_backoff_sec=env_float("BINANCE_API_BACKOFF_SEC", env_float("HTTP_BACKOFF_SEC", 0.8)),
            http_cache_enable=env_bool("DATA_SOURCE_CACHE_ENABLE", True),
            http_cache_ttl_sec=env_int("DATA_SOURCE_CACHE_TTL_SEC", 10),
            binance_fapi_base_url=os.getenv("BINANCE_FAPI_BASE_URL", "https://fapi.binance.com").rstrip("/"),
            binance_spot_base_url=os.getenv("BINANCE_SPOT_BASE_URL", "https://api.binance.com").rstrip("/"),
            excluded_base_assets=env_csv("EXCLUDED_BASE_ASSETS", ("XAU", "XAG")),
            liquidity_fallback_enable=env_bool("LIQUIDITY_FALLBACK_ENABLE", True),
            liquidity_score_max_delta=env_int("LIQUIDITY_SCORE_MAX_DELTA", 15),
            liquidity_min_distance_pct=env_float("LIQUIDITY_MIN_DISTANCE_PCT", 0.5),
            liquidity_max_distance_pct=env_float("LIQUIDITY_MAX_DISTANCE_PCT", 8.0),
            binance_orderbook_liquidity_enable=env_bool("BINANCE_ORDERBOOK_LIQUIDITY_ENABLE", True),
            binance_orderbook_depth_limit=env_int("BINANCE_ORDERBOOK_DEPTH_LIMIT", 100),
            coinalyze_enable=env_bool("COINALYZE_ENABLE", False),
            coinalyze_api_key=os.getenv("COINALYZE_API_KEY", "").strip(),
            coinalyze_base_url=os.getenv("COINALYZE_BASE_URL", "https://api.coinalyze.net/v1").rstrip("/"),
            coinalyze_request_budget=env_int("COINALYZE_REQUEST_BUDGET", 40),
            coinalyze_symbol_suffix=os.getenv("COINALYZE_SYMBOL_SUFFIX", "_PERP.A").strip() or "_PERP.A",
            coinalyze_liquidation_interval=os.getenv("COINALYZE_LIQUIDATION_INTERVAL", "1hour").strip() or "1hour",
            coinalyze_liquidation_lookback_hours=env_int("COINALYZE_LIQUIDATION_LOOKBACK_HOURS", 24),
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
            structure_radar_enable=env_bool("STRUCTURE_RADAR_ENABLE", True),
            structure_interval=os.getenv("STRUCTURE_INTERVAL", "15m").strip() or "15m",
            structure_higher_interval=os.getenv("STRUCTURE_HIGHER_INTERVAL", "1h").strip() or "1h",
            structure_box_lookback=env_int("STRUCTURE_BOX_LOOKBACK", 36),
            structure_top_symbols=env_int("STRUCTURE_TOP_SYMBOLS", 80),
            structure_near_edge_pct=env_float("STRUCTURE_NEAR_EDGE_PCT", 1.5),
            structure_min_score=env_int("STRUCTURE_MIN_SCORE", 65),
            structure_send_chart_top_n=env_int("STRUCTURE_SEND_CHART_TOP_N", 3),
            structure_save_charts=env_bool("STRUCTURE_SAVE_CHARTS", True),
            structure_delete_chart_after_send=env_bool("STRUCTURE_DELETE_CHART_AFTER_SEND", True),
            structure_chart_retention_hours=env_int("STRUCTURE_CHART_RETENTION_HOURS", 12),
            structure_max_chart_files=env_int("STRUCTURE_MAX_CHART_FILES", 200),
            structure_pre_scan_minute=env_int("STRUCTURE_PRE_SCAN_MINUTE", 55),
            structure_confirm_delay_sec=env_int("STRUCTURE_CONFIRM_DELAY_SEC", 300),
            structure_cooldown_sec=env_int("STRUCTURE_COOLDOWN_SEC", 3600),
            structure_state_path=data_path(data_dir, "STRUCTURE_STATE_FILE", "structure_state.json"),
            structure_history_path=data_path(data_dir, "STRUCTURE_HISTORY_FILE", "structure_history.json"),
            structure_chart_dir=data_path(data_dir, "STRUCTURE_CHART_DIR", "charts"),
            structure_reply_chain_enable=env_bool("STRUCTURE_REPLY_CHAIN_ENABLE", True),
            structure_review_enable=env_bool("STRUCTURE_REVIEW_ENABLE", True),
            structure_review_lookback_hours=env_int("STRUCTURE_REVIEW_LOOKBACK_HOURS", 24),
            structure_review_forward_hours=env_int("STRUCTURE_REVIEW_FORWARD_HOURS", 4),
            structure_review_min_age_minutes=env_int("STRUCTURE_REVIEW_MIN_AGE_MINUTES", 15),
            structure_review_report_top_n=env_int("STRUCTURE_REVIEW_REPORT_TOP_N", 10),
            structure_review_min_sample=env_int("STRUCTURE_REVIEW_MIN_SAMPLE", 10),
            structure_review_max_report_interval_sec=env_int("STRUCTURE_REVIEW_MAX_REPORT_INTERVAL_SEC", 3600),
            structure_review_path=data_path(data_dir, "STRUCTURE_REVIEW_FILE", "structure_review.json"),
            structure_stats_path=data_path(data_dir, "STRUCTURE_STATS_FILE", "structure_stats.json"),
            structure_review_report_path=data_path(data_dir, "STRUCTURE_REVIEW_REPORT_FILE", "structure_review_report.txt"),
            oi_hist_budget=env_int("OI_HIST_REQUEST_BUDGET", 80),
            kline_budget=env_int("KLINE_REQUEST_BUDGET", 120),
            funding_history_budget=env_int("FUNDING_HISTORY_REQUEST_BUDGET", 25),
            fuse_seconds=env_int("DATA_SOURCE_FUSE_SECONDS", 15 * 60),
            launch_scan_limit=env_int("LAUNCH_SCAN_LIMIT", 80),
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
                    "structure_radar": bool(self.tg_structure_topic_id),
                    "structure_review": bool(self.tg_structure_review_topic_id),
                },
                "auto_create_topics": self.tg_auto_create_topics,
                "topic_routes_file": str(self.tg_topic_routes_path),
                "topic_intro_enable": self.tg_topic_intro_enable,
                "topic_intro_pin": self.tg_topic_intro_pin,
                "use_topic": self.tg_use_topic,
            },
            "ai_assistant": {
                "enable": self.ai_assistant_enable,
                "bot_token_configured": bool(self.ai_bot_token),
                "admin_user_ids_configured": bool(self.ai_admin_user_ids),
                "admin_user_count": len(self.ai_admin_user_ids),
                "allow_group_chat": self.ai_allow_group_chat,
                "allowed_chat_ids_configured": bool(self.ai_allowed_chat_ids),
                "allowed_chat_count": len(self.ai_allowed_chat_ids),
                "price_alerts_enable": self.ai_price_alerts_enable,
                "price_alerts_db_file": str(self.ai_price_alerts_db_path),
                "default_chat_id_configured": bool(self.ai_default_chat_id),
                "alert_check_interval_sec": self.ai_alert_check_interval_sec,
                "poll_timeout_sec": self.ai_poll_timeout_sec,
                "provider_enable": self.ai_provider_enable,
                "api_key_configured": bool(self.ai_api_key),
                "base_url": self.ai_base_url,
                "model": self.ai_model,
                "request_timeout_sec": self.ai_request_timeout_sec,
                "prompts_file": str(self.ai_prompts_path),
            },
            "runtime": {
                "status_file": str(self.runtime_status_path),
                "structure_status_file": str(self.structure_runtime_status_path),
                "cleanup_enable": self.cleanup_enable,
                "cleanup_interval_sec": self.cleanup_interval_sec,
                "cleanup_state_file": str(self.cleanup_state_path),
            },
            "http": {
                "futures_base_url": self.binance_fapi_base_url,
                "spot_base_url": self.binance_spot_base_url,
                "timeout_sec": self.http_timeout_sec,
                "retry": self.http_retry,
                "cache_enable": self.http_cache_enable,
                "cache_ttl_sec": self.http_cache_ttl_sec,
            },
            "liquidity": {
                "fallback_enable": self.liquidity_fallback_enable,
                "score_max_delta": self.liquidity_score_max_delta,
                "min_distance_pct": self.liquidity_min_distance_pct,
                "max_distance_pct": self.liquidity_max_distance_pct,
                "binance_orderbook_enable": self.binance_orderbook_liquidity_enable,
                "binance_orderbook_depth_limit": self.binance_orderbook_depth_limit,
            },
            "coinalyze": {
                "enable": self.coinalyze_enable,
                "api_key_configured": bool(self.coinalyze_api_key),
                "base_url": self.coinalyze_base_url,
                "request_budget": self.coinalyze_request_budget,
                "symbol_suffix": self.coinalyze_symbol_suffix,
                "liquidation_interval": self.coinalyze_liquidation_interval,
                "liquidation_lookback_hours": self.coinalyze_liquidation_lookback_hours,
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
            "structure_radar": {
                "enable": self.structure_radar_enable,
                "interval": self.structure_interval,
                "higher_interval": self.structure_higher_interval,
                "box_lookback": self.structure_box_lookback,
                "top_symbols": self.structure_top_symbols,
                "near_edge_pct": self.structure_near_edge_pct,
                "min_score": self.structure_min_score,
                "send_chart_top_n": self.structure_send_chart_top_n,
                "save_charts": self.structure_save_charts,
                "delete_chart_after_send": self.structure_delete_chart_after_send,
                "chart_retention_hours": self.structure_chart_retention_hours,
                "max_chart_files": self.structure_max_chart_files,
                "pre_scan_minute": self.structure_pre_scan_minute,
                "confirm_delay_sec": self.structure_confirm_delay_sec,
                "cooldown_sec": self.structure_cooldown_sec,
                "state_file": str(self.structure_state_path),
                "history_file": str(self.structure_history_path),
                "chart_dir": str(self.structure_chart_dir),
                "reply_chain_enable": self.structure_reply_chain_enable,
                "review_enable": self.structure_review_enable,
                "review_lookback_hours": self.structure_review_lookback_hours,
                "review_forward_hours": self.structure_review_forward_hours,
                "review_min_age_minutes": self.structure_review_min_age_minutes,
                "review_report_top_n": self.structure_review_report_top_n,
                "review_min_sample": self.structure_review_min_sample,
                "review_max_report_interval_sec": self.structure_review_max_report_interval_sec,
                "review_file": str(self.structure_review_path),
                "stats_file": str(self.structure_stats_path),
                "review_report_file": str(self.structure_review_report_path),
            },
            "launch": {
                "scan_limit": self.launch_scan_limit,
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
