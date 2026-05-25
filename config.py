from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
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
    tg_use_topic: bool = False
    tg_push_history_path: Path = BASE_DIR / "data" / "tg_push_history.json"
    tg_push_split_limit: int = 3800
    tg_push_timeout_sec: int = 10
    tg_push_retry: int = 2
    tg_global_hourly_limit: int = 20
    tg_default_cooldown_sec: int = 6 * 3600
    runtime_status_path: Path = BASE_DIR / "data" / "runtime_status.json"

    http_timeout_sec: int = 10
    http_retry: int = 2
    http_backoff_sec: float = 0.8
    http_cache_enable: bool = True
    http_cache_ttl_sec: int = 10
    binance_fapi_base_url: str = "https://fapi.binance.com"
    excluded_base_assets: tuple[str, ...] = ("XAU", "XAG")

    radar_scan_limit: int = 120
    radar_min_quote_volume: float = 5_000_000
    radar_top_n: int = 8
    radar_summary_min_interval_sec: int = 30 * 60
    radar_summary_max_daily_push: int = 6
    radar_state_path: Path = BASE_DIR / "data" / "radar_state.json"
    funding_snapshot_path: Path = BASE_DIR / "data" / "funding_snapshot.json"

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
    launch_stage_cooldown_sec: int = 6 * 3600
    launch_state_ttl_sec: int = 48 * 3600
    launch_failed_ttl_sec: int = 24 * 3600

    announcement_state_path: Path = BASE_DIR / "data" / "announcement_state.json"
    announcement_page_size: int = 20

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
            tg_topic_id=(os.getenv("TG_TOPIC_ID", "") or os.getenv("TELEGRAM_MESSAGE_THREAD_ID", "")).strip(),
            tg_use_topic=env_bool("TELEGRAM_USE_TOPIC", False),
            tg_push_history_path=data_path(data_dir, "TG_PUSH_HISTORY_FILE", "tg_push_history.json"),
            tg_push_split_limit=env_int("TG_PUSH_SPLIT_LIMIT", 3800),
            tg_push_timeout_sec=env_int("TG_PUSH_TIMEOUT_SEC", 10),
            tg_push_retry=env_int("TG_PUSH_RETRY", 2),
            tg_global_hourly_limit=env_int("TG_GLOBAL_HOURLY_LIMIT", 20),
            tg_default_cooldown_sec=env_int("TG_DEFAULT_COOLDOWN_SEC", 6 * 3600),
            runtime_status_path=data_path(data_dir, "RUNTIME_STATUS_FILE", "runtime_status.json"),
            http_timeout_sec=env_int("BINANCE_API_TIMEOUT_SEC", env_int("HTTP_TIMEOUT_SEC", 10)),
            http_retry=env_int("BINANCE_API_RETRY", env_int("HTTP_RETRY", 2)),
            http_backoff_sec=env_float("BINANCE_API_BACKOFF_SEC", env_float("HTTP_BACKOFF_SEC", 0.8)),
            http_cache_enable=env_bool("DATA_SOURCE_CACHE_ENABLE", True),
            http_cache_ttl_sec=env_int("DATA_SOURCE_CACHE_TTL_SEC", 10),
            binance_fapi_base_url=os.getenv("BINANCE_FAPI_BASE_URL", "https://fapi.binance.com").rstrip("/"),
            excluded_base_assets=env_csv("EXCLUDED_BASE_ASSETS", ("XAU", "XAG")),
            radar_scan_limit=env_int("RADAR_SCAN_LIMIT", env_int("BN_SCAN_LIMIT", 120)),
            radar_min_quote_volume=env_float("RADAR_MIN_QUOTE_VOLUME", env_float("BN_MIN_QUOTE_VOLUME", 5_000_000)),
            radar_top_n=env_int("RADAR_TOP_N", 8),
            radar_summary_min_interval_sec=env_int("RADAR_SUMMARY_MIN_INTERVAL_SEC", 30 * 60),
            radar_summary_max_daily_push=env_int("RADAR_SUMMARY_MAX_DAILY_PUSH", 6),
            radar_state_path=data_path(data_dir, "RADAR_STATE_FILE", "radar_state.json"),
            funding_snapshot_path=data_path(data_dir, "FUNDING_SNAPSHOT_FILE", "funding_snapshot.json"),
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
            launch_stage_cooldown_sec=env_int("LAUNCH_STAGE_COOLDOWN_SEC", 6 * 3600),
            launch_state_ttl_sec=env_int("LAUNCH_STATE_TTL_SEC", 48 * 3600),
            launch_failed_ttl_sec=env_int("LAUNCH_FAILED_TTL_SEC", 24 * 3600),
            announcement_state_path=data_path(data_dir, "ANNOUNCEMENT_STATE_FILE", "announcement_state.json"),
            announcement_page_size=env_int("ANNOUNCEMENT_PAGE_SIZE", 20),
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
                "use_topic": self.tg_use_topic,
            },
            "runtime": {
                "status_file": str(self.runtime_status_path),
            },
            "http": {
                "base_url": self.binance_fapi_base_url,
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
                "funding_history": self.funding_history_budget,
            },
            "radar": {
                "scan_limit": self.radar_scan_limit,
                "min_quote_volume": self.radar_min_quote_volume,
                "top_n": self.radar_top_n,
                "summary_min_interval_sec": self.radar_summary_min_interval_sec,
                "summary_max_daily_push": self.radar_summary_max_daily_push,
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
