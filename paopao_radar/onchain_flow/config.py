from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from math import isfinite
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from .constants import (
    OAR_BRIDGE_MAX_SIGNALS_PER_CYCLE_HARD,
    OAR_BEHAVIOR_MIN_ACTIVE_BUCKETS_HARD,
    OAR_BEHAVIOR_MIN_TX_HARD,
    OAR_MAX_ANALYZED_WALLETS_HARD,
    OAR_MAX_SOURCE_EVENT_IDS_HARD,
    OAR_MAX_WALLET_GROUPS_HARD,
    OAR_PATTERN_MIN_TX_HARD,
    OAR_PATTERN_MIN_WALLETS_HARD,
    OAR_WATCH_MAX_ACTIVE_TOKENS_HARD,
    OAR_WATCH_MAX_RPC_REQUESTS_PER_CYCLE_HARD,
    OAR_WATCH_MAX_TOKENS_PER_CYCLE_HARD,
    OAR_WALLET_SYNC_WINDOW_SEC_HARD,
    PRODUCTION_WRITE_PATHS,
    TOKEN_ACTIVITY_ADAPTIVE_MAX_REQUESTS_HARD,
    TOKEN_ACTIVITY_BLOCK_SEARCH_MAX_CALLS_HARD,
    TOKEN_ACTIVITY_MAX_EVENTS_HARD,
    TOKEN_ACTIVITY_MAX_RPC_REQUESTS_HARD,
    TOKEN_ACTIVITY_MAX_UNIQUE_BLOCK_HEADERS_HARD,
    TOKEN_ACTIVITY_MAX_WINDOW_HOURS_HARD,
    TOKEN_ACTIVITY_TOP_N_HARD,
)


BASE_DIR = Path(__file__).resolve().parents[2]
OAR_TELEGRAM_QUERY_ACK = "启用群内链上查询"


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


def _csv(values: Mapping[str, str], name: str, default: str) -> tuple[str, ...]:
    raw = values.get(name, default)
    return tuple(
        item.strip().lower() for item in raw.split(",") if item.strip()
    )


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
    oar_ai_cache_path: Path = BASE_DIR / "data" / "onchain" / "oar_ai_cache.json"
    oar_ai_operator_prompt_path: Path = (
        BASE_DIR / "data" / "onchain" / "config" / "oar_ai_operator_prompt.txt"
    )
    oar_automation_db_path: Path = (
        BASE_DIR / "data" / "onchain" / "oar_automation.db"
    )
    label_candidates_path: Path = (
        BASE_DIR / "data" / "onchain" / "label_candidates.json"
    )
    address_intelligence_path: Path = (
        BASE_DIR / "data" / "onchain" / "address_intelligence.json"
    )
    oar_telegram_query_state_path: Path = (
        BASE_DIR / "data" / "onchain" / "telegram_query_state.json"
    )
    main_signal_db_path: Path = BASE_DIR / "data" / "signals.db"
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
    token_activity_max_window_hours: int = 24
    token_activity_max_events: int = 5000
    token_activity_max_rpc_requests: int = 256
    token_activity_adaptive_max_requests: int = 128
    token_activity_max_unique_block_headers: int = 2000
    token_activity_top_n: int = 50
    token_activity_block_search_max_calls: int = 32
    arkham_api_base_url: str = "https://api.arkm.com"
    arkham_api_key: str = ""
    arkham_api_timeout_sec: int = 15
    arkham_api_max_retries: int = 1
    oar_label_candidate_max_addresses: int = 50
    dune_api_base_url: str = "https://api.dune.com/api"
    dune_api_key: str = ""
    dune_api_timeout_sec: int = 15
    dune_api_max_retries: int = 1
    dune_api_max_requests: int = 10
    dune_api_poll_interval_sec: Decimal = Decimal("4")
    dune_api_execution_timeout_sec: int = 30
    dune_api_max_rows: int = 100
    oar_behavior_min_tx: int = 3
    oar_behavior_dominance_min: Decimal = Decimal("0.67")
    oar_behavior_min_active_buckets_1h: int = 2
    oar_behavior_min_active_buckets_long: int = 3
    oar_pattern_min_wallets: int = 3
    oar_pattern_min_tx: int = 3
    oar_pattern_min_amount_share: Decimal = Decimal("0.10")
    oar_wallet_sync_window_sec: int = 300
    oar_wallet_amount_similarity_tolerance: Decimal = Decimal("0.10")
    oar_max_analyzed_wallets: int = 100
    oar_max_wallet_groups: int = 20
    oar_max_source_event_ids: int = 50
    oar_ai_enable: bool = False
    oar_ai_provider: str = "deepseek"
    oar_ai_base_url: str = "https://api.deepseek.com"
    oar_ai_api_key: str = ""
    oar_ai_model: str = "deepseek-v4-pro"
    oar_ai_thinking_mode: str = "enabled"
    oar_ai_reasoning_effort: str = "high"
    oar_ai_max_tokens: int = 8192
    oar_ai_timeout_sec: int = 60
    oar_ai_max_retries: int = 0
    oar_ai_max_calls_per_hour: int = 10
    oar_ai_cache_ttl_sec: int = 3600
    oar_ai_max_context_chars: int = 30000
    oar_ai_max_output_chars: int = 8000
    oar_replace_complete_card_with_partial: bool = False
    oar_replace_rich_ai_card_with_rule_only: bool = False
    oar_automation_enable: bool = False
    oar_bridge_allowed_modules: tuple[str, ...] = (
        "launch",
        "flow",
        "funding",
        "announcement",
    )
    oar_bridge_overlap_sec: int = 300
    oar_bridge_bootstrap_lookback_sec: int = 3600
    oar_bridge_max_signals_per_cycle: int = 100
    oar_watch_max_active_tokens: int = 50
    oar_watch_max_tokens_per_cycle: int = 5
    oar_watch_scan_interval_sec: int = 900
    oar_watch_live_poll_sec: int = 60
    oar_watch_query_window: str = "4h"
    oar_watch_lease_sec: int = 600
    oar_watch_max_consecutive_failures: int = 10
    oar_watch_manual_ttl_sec: int = 2592000
    oar_watch_launch_ttl_sec: int = 86400
    oar_watch_flow_ttl_sec: int = 86400
    oar_watch_funding_ttl_sec: int = 43200
    oar_watch_announcement_ttl_sec: int = 259200
    oar_watch_manual_priority: int = 100
    oar_watch_launch_priority: int = 90
    oar_watch_flow_priority: int = 80
    oar_watch_funding_priority: int = 70
    oar_watch_announcement_priority: int = 75
    oar_watch_notify_min_behavior_score: int = 55
    oar_watch_notify_min_wallet_score: int = 60
    oar_watch_notify_partial: bool = False
    oar_watch_max_events_per_token: int = 1000
    oar_watch_max_rpc_requests_per_token: int = 100
    oar_watch_top_transfers: int = 20
    oar_watch_max_rpc_requests_per_cycle: int = 400
    oar_telegram_query_enable: bool = False
    oar_telegram_query_ack: str = ""
    oar_telegram_query_poll_timeout_sec: int = 20
    oar_telegram_query_cooldown_sec: int = 60
    oar_telegram_query_max_per_hour: int = 12
    oar_telegram_query_max_events: int = 500
    oar_telegram_query_max_rpc_requests: int = 128
    oar_telegram_query_top_n: int = 20

    def __post_init__(self) -> None:
        default_automation = (
            BASE_DIR / "data" / "onchain" / "oar_automation.db"
        )
        if self.oar_automation_db_path == default_automation:
            object.__setattr__(
                self,
                "oar_automation_db_path",
                self.data_dir / "oar_automation.db",
            )
        default_label_candidates = (
            BASE_DIR / "data" / "onchain" / "label_candidates.json"
        )
        if self.label_candidates_path == default_label_candidates:
            object.__setattr__(
                self,
                "label_candidates_path",
                self.data_dir / "label_candidates.json",
            )
        default_address_intelligence = (
            BASE_DIR / "data" / "onchain" / "address_intelligence.json"
        )
        if self.address_intelligence_path == default_address_intelligence:
            object.__setattr__(
                self,
                "address_intelligence_path",
                self.data_dir / "address_intelligence.json",
            )
        default_telegram_query_state = (
            BASE_DIR / "data" / "onchain" / "telegram_query_state.json"
        )
        if self.oar_telegram_query_state_path == default_telegram_query_state:
            object.__setattr__(
                self,
                "oar_telegram_query_state_path",
                self.data_dir / "telegram_query_state.json",
            )
        default_operator_prompt = (
            BASE_DIR
            / "data"
            / "onchain"
            / "config"
            / "oar_ai_operator_prompt.txt"
        )
        if self.oar_ai_operator_prompt_path == default_operator_prompt:
            object.__setattr__(
                self,
                "oar_ai_operator_prompt_path",
                self.data_dir / "config" / "oar_ai_operator_prompt.txt",
            )
        default_main_signals = BASE_DIR / "data" / "signals.db"
        if self.main_signal_db_path == default_main_signals:
            object.__setattr__(
                self,
                "main_signal_db_path",
                self.base_dir / "data" / "signals.db",
            )

    @classmethod
    def load(
        cls,
        *,
        base_dir: Path = BASE_DIR,
        environ: Mapping[str, str] | None = None,
    ) -> "OnchainSettings":
        base_dir = base_dir.resolve()
        shared_path = base_dir / ".env.oi"
        shared = parse_env_file(shared_path)
        onchain = parse_env_file(base_dir / ".env.onchain")
        runtime = dict(os.environ if environ is None else environ)
        values = {**shared, **onchain, **runtime}
        if shared_path.exists():
            # The main BOT owns the shared Telegram identity.  OAR may keep
            # its own topic, but a stale duplicate in .env.onchain or the
            # service environment must never select a different BOT/group.
            values["TG_BOT_TOKEN"] = shared.get("TG_BOT_TOKEN", "")
            values["TG_CHAT_ID"] = shared.get("TG_CHAT_ID", "")
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
            oar_ai_cache_path=_resolve_data_file(
                base_dir,
                data_dir,
                values.get("OAR_AI_CACHE_FILE", "oar_ai_cache.json"),
            ),
            oar_ai_operator_prompt_path=_resolve_data_file(
                base_dir,
                data_dir,
                values.get(
                    "OAR_AI_OPERATOR_PROMPT_FILE",
                    "config/oar_ai_operator_prompt.txt",
                ),
            ),
            oar_automation_db_path=_resolve_data_file(
                base_dir,
                data_dir,
                values.get("OAR_AUTOMATION_DB_FILE", "oar_automation.db"),
            ),
            label_candidates_path=_resolve_data_file(
                base_dir,
                data_dir,
                values.get(
                    "OAR_LABEL_CANDIDATES_FILE",
                    "label_candidates.json",
                ),
            ),
            address_intelligence_path=_resolve_data_file(
                base_dir,
                data_dir,
                values.get(
                    "OAR_ADDRESS_INTELLIGENCE_FILE",
                    "address_intelligence.json",
                ),
            ),
            oar_telegram_query_state_path=_resolve_data_file(
                base_dir,
                data_dir,
                values.get(
                    "OAR_TELEGRAM_QUERY_STATE_FILE",
                    "telegram_query_state.json",
                ),
            ),
            main_signal_db_path=_resolve_repo_file(
                base_dir,
                values.get("OAR_MAIN_SIGNAL_DB_FILE", "data/signals.db"),
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
            token_activity_max_window_hours=_int(
                values, "TOKEN_ACTIVITY_MAX_WINDOW_HOURS", 24
            ),
            token_activity_max_events=_int(
                values, "TOKEN_ACTIVITY_MAX_EVENTS", 5000
            ),
            token_activity_max_rpc_requests=_int(
                values, "TOKEN_ACTIVITY_MAX_RPC_REQUESTS", 256
            ),
            token_activity_adaptive_max_requests=_int(
                values, "TOKEN_ACTIVITY_ADAPTIVE_MAX_REQUESTS", 128
            ),
            token_activity_max_unique_block_headers=_int(
                values, "TOKEN_ACTIVITY_MAX_UNIQUE_BLOCK_HEADERS", 2000
            ),
            token_activity_top_n=_int(
                values, "TOKEN_ACTIVITY_TOP_N", 50
            ),
            token_activity_block_search_max_calls=_int(
                values, "TOKEN_ACTIVITY_BLOCK_SEARCH_MAX_CALLS", 32
            ),
            arkham_api_base_url=values.get(
                "ARKHAM_API_BASE_URL",
                "https://api.arkm.com",
            ).strip().rstrip("/"),
            arkham_api_key=values.get("ARKHAM_API_KEY", "").strip(),
            arkham_api_timeout_sec=_int(
                values, "ARKHAM_API_TIMEOUT_SEC", 15
            ),
            arkham_api_max_retries=_int(
                values, "ARKHAM_API_MAX_RETRIES", 1
            ),
            oar_label_candidate_max_addresses=_int(
                values, "OAR_LABEL_CANDIDATE_MAX_ADDRESSES", 50
            ),
            dune_api_base_url=values.get(
                "DUNE_API_BASE_URL",
                "https://api.dune.com/api",
            ).strip().rstrip("/"),
            dune_api_key=values.get("DUNE_API_KEY", "").strip(),
            dune_api_timeout_sec=_int(
                values, "DUNE_API_TIMEOUT_SEC", 15
            ),
            dune_api_max_retries=_int(
                values, "DUNE_API_MAX_RETRIES", 1
            ),
            dune_api_max_requests=_int(
                values, "DUNE_API_MAX_REQUESTS", 10
            ),
            dune_api_poll_interval_sec=_decimal(
                values, "DUNE_API_POLL_INTERVAL_SEC", "4"
            ),
            dune_api_execution_timeout_sec=_int(
                values, "DUNE_API_EXECUTION_TIMEOUT_SEC", 30
            ),
            dune_api_max_rows=_int(
                values, "DUNE_API_MAX_ROWS", 100
            ),
            oar_behavior_min_tx=_int(
                values, "OAR_BEHAVIOR_MIN_TX", 3
            ),
            oar_behavior_dominance_min=_decimal(
                values, "OAR_BEHAVIOR_DOMINANCE_MIN", "0.67"
            ),
            oar_behavior_min_active_buckets_1h=_int(
                values, "OAR_BEHAVIOR_MIN_ACTIVE_BUCKETS_1H", 2
            ),
            oar_behavior_min_active_buckets_long=_int(
                values, "OAR_BEHAVIOR_MIN_ACTIVE_BUCKETS_LONG", 3
            ),
            oar_pattern_min_wallets=_int(
                values, "OAR_PATTERN_MIN_WALLETS", 3
            ),
            oar_pattern_min_tx=_int(
                values, "OAR_PATTERN_MIN_TX", 3
            ),
            oar_pattern_min_amount_share=_decimal(
                values, "OAR_PATTERN_MIN_AMOUNT_SHARE", "0.10"
            ),
            oar_wallet_sync_window_sec=_int(
                values, "OAR_WALLET_SYNC_WINDOW_SEC", 300
            ),
            oar_wallet_amount_similarity_tolerance=_decimal(
                values,
                "OAR_WALLET_AMOUNT_SIMILARITY_TOLERANCE",
                "0.10",
            ),
            oar_max_analyzed_wallets=_int(
                values, "OAR_MAX_ANALYZED_WALLETS", 100
            ),
            oar_max_wallet_groups=_int(
                values, "OAR_MAX_WALLET_GROUPS", 20
            ),
            oar_max_source_event_ids=_int(
                values, "OAR_MAX_SOURCE_EVENT_IDS", 50
            ),
            oar_ai_enable=_bool(values, "OAR_AI_ENABLE", False),
            oar_ai_provider=values.get(
                "OAR_AI_PROVIDER", "deepseek"
            ).strip().lower(),
            oar_ai_base_url=(
                values.get(
                    "OAR_AI_BASE_URL", "https://api.deepseek.com"
                ).strip().rstrip("/")
            ),
            oar_ai_api_key=values.get("OAR_AI_API_KEY", "").strip(),
            oar_ai_model=values.get(
                "OAR_AI_MODEL", "deepseek-v4-pro"
            ).strip(),
            oar_ai_thinking_mode=values.get(
                "OAR_AI_THINKING_MODE", "enabled"
            ).strip().lower(),
            oar_ai_reasoning_effort=values.get(
                "OAR_AI_REASONING_EFFORT", "high"
            ).strip().lower(),
            oar_ai_max_tokens=_int(values, "OAR_AI_MAX_TOKENS", 8192),
            oar_ai_timeout_sec=_int(values, "OAR_AI_TIMEOUT_SEC", 60),
            oar_ai_max_retries=_int(values, "OAR_AI_MAX_RETRIES", 0),
            oar_ai_max_calls_per_hour=_int(
                values, "OAR_AI_MAX_CALLS_PER_HOUR", 10
            ),
            oar_ai_cache_ttl_sec=_int(
                values, "OAR_AI_CACHE_TTL_SEC", 3600
            ),
            oar_ai_max_context_chars=_int(
                values, "OAR_AI_MAX_CONTEXT_CHARS", 30000
            ),
            oar_ai_max_output_chars=_int(
                values, "OAR_AI_MAX_OUTPUT_CHARS", 8000
            ),
            oar_replace_complete_card_with_partial=_bool(
                values,
                "OAR_REPLACE_COMPLETE_CARD_WITH_PARTIAL",
                False,
            ),
            oar_replace_rich_ai_card_with_rule_only=_bool(
                values,
                "OAR_REPLACE_RICH_AI_CARD_WITH_RULE_ONLY",
                False,
            ),
            oar_automation_enable=_bool(
                values, "OAR_AUTOMATION_ENABLE", False
            ),
            oar_bridge_allowed_modules=_csv(
                values,
                "OAR_BRIDGE_ALLOWED_MODULES",
                "launch,flow,funding,announcement",
            ),
            oar_bridge_overlap_sec=_int(
                values, "OAR_BRIDGE_OVERLAP_SEC", 300
            ),
            oar_bridge_bootstrap_lookback_sec=_int(
                values, "OAR_BRIDGE_BOOTSTRAP_LOOKBACK_SEC", 3600
            ),
            oar_bridge_max_signals_per_cycle=_int(
                values, "OAR_BRIDGE_MAX_SIGNALS_PER_CYCLE", 100
            ),
            oar_watch_max_active_tokens=_int(
                values, "OAR_WATCH_MAX_ACTIVE_TOKENS", 50
            ),
            oar_watch_max_tokens_per_cycle=_int(
                values, "OAR_WATCH_MAX_TOKENS_PER_CYCLE", 5
            ),
            oar_watch_scan_interval_sec=_int(
                values, "OAR_WATCH_SCAN_INTERVAL_SEC", 900
            ),
            oar_watch_live_poll_sec=_int(
                values, "OAR_WATCH_LIVE_POLL_SEC", 60
            ),
            oar_watch_query_window=values.get(
                "OAR_WATCH_QUERY_WINDOW", "4h"
            ).strip(),
            oar_watch_lease_sec=_int(
                values, "OAR_WATCH_LEASE_SEC", 600
            ),
            oar_watch_max_consecutive_failures=_int(
                values, "OAR_WATCH_MAX_CONSECUTIVE_FAILURES", 10
            ),
            oar_watch_manual_ttl_sec=_int(
                values, "OAR_WATCH_MANUAL_TTL_SEC", 2592000
            ),
            oar_watch_launch_ttl_sec=_int(
                values, "OAR_WATCH_LAUNCH_TTL_SEC", 86400
            ),
            oar_watch_flow_ttl_sec=_int(
                values, "OAR_WATCH_FLOW_TTL_SEC", 86400
            ),
            oar_watch_funding_ttl_sec=_int(
                values, "OAR_WATCH_FUNDING_TTL_SEC", 43200
            ),
            oar_watch_announcement_ttl_sec=_int(
                values, "OAR_WATCH_ANNOUNCEMENT_TTL_SEC", 259200
            ),
            oar_watch_manual_priority=_int(
                values, "OAR_WATCH_MANUAL_PRIORITY", 100
            ),
            oar_watch_launch_priority=_int(
                values, "OAR_WATCH_LAUNCH_PRIORITY", 90
            ),
            oar_watch_flow_priority=_int(
                values, "OAR_WATCH_FLOW_PRIORITY", 80
            ),
            oar_watch_funding_priority=_int(
                values, "OAR_WATCH_FUNDING_PRIORITY", 70
            ),
            oar_watch_announcement_priority=_int(
                values, "OAR_WATCH_ANNOUNCEMENT_PRIORITY", 75
            ),
            oar_watch_notify_min_behavior_score=_int(
                values, "OAR_WATCH_NOTIFY_MIN_BEHAVIOR_SCORE", 55
            ),
            oar_watch_notify_min_wallet_score=_int(
                values, "OAR_WATCH_NOTIFY_MIN_WALLET_SCORE", 60
            ),
            oar_watch_notify_partial=_bool(
                values, "OAR_WATCH_NOTIFY_PARTIAL", False
            ),
            oar_watch_max_events_per_token=_int(
                values, "OAR_WATCH_MAX_EVENTS_PER_TOKEN", 1000
            ),
            oar_watch_max_rpc_requests_per_token=_int(
                values, "OAR_WATCH_MAX_RPC_REQUESTS_PER_TOKEN", 100
            ),
            oar_watch_top_transfers=_int(
                values, "OAR_WATCH_TOP_TRANSFERS", 20
            ),
            oar_watch_max_rpc_requests_per_cycle=_int(
                values, "OAR_WATCH_MAX_RPC_REQUESTS_PER_CYCLE", 400
            ),
            oar_telegram_query_enable=_bool(
                values, "OAR_TELEGRAM_QUERY_ENABLE", False
            ),
            oar_telegram_query_ack=values.get(
                "OAR_TELEGRAM_QUERY_ACK", ""
            ).strip(),
            oar_telegram_query_poll_timeout_sec=_int(
                values, "OAR_TELEGRAM_QUERY_POLL_TIMEOUT_SEC", 20
            ),
            oar_telegram_query_cooldown_sec=_int(
                values, "OAR_TELEGRAM_QUERY_COOLDOWN_SEC", 60
            ),
            oar_telegram_query_max_per_hour=_int(
                values, "OAR_TELEGRAM_QUERY_MAX_PER_HOUR", 12
            ),
            oar_telegram_query_max_events=_int(
                values, "OAR_TELEGRAM_QUERY_MAX_EVENTS", 500
            ),
            oar_telegram_query_max_rpc_requests=_int(
                values, "OAR_TELEGRAM_QUERY_MAX_RPC_REQUESTS", 128
            ),
            oar_telegram_query_top_n=_int(
                values, "OAR_TELEGRAM_QUERY_TOP_N", 20
            ),
        )

    @property
    def writable_paths(self) -> tuple[Path, ...]:
        paths = (
            self.db_path,
            self.runtime_status_path,
            self.tg_push_history_path,
            self.tg_outbox_path,
            self.tg_topic_routes_path,
            self.signal_events_path,
            self.signal_events_db_path,
            self.oar_automation_db_path,
            self.label_candidates_path,
            self.address_intelligence_path,
            self.oar_telegram_query_state_path,
            self.oar_ai_operator_prompt_path,
        )
        if self.oar_ai_enable:
            return (*paths, self.oar_ai_cache_path)
        return paths

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
            "dune_api_poll_interval_sec",
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
            "token_activity_max_window_hours",
            "token_activity_max_events",
            "token_activity_max_rpc_requests",
            "token_activity_adaptive_max_requests",
            "token_activity_max_unique_block_headers",
            "token_activity_top_n",
            "token_activity_block_search_max_calls",
            "arkham_api_timeout_sec",
            "oar_label_candidate_max_addresses",
            "dune_api_timeout_sec",
            "dune_api_max_requests",
            "dune_api_execution_timeout_sec",
            "dune_api_max_rows",
            "oar_behavior_min_tx",
            "oar_behavior_min_active_buckets_1h",
            "oar_behavior_min_active_buckets_long",
            "oar_pattern_min_wallets",
            "oar_pattern_min_tx",
            "oar_wallet_sync_window_sec",
            "oar_max_analyzed_wallets",
            "oar_max_wallet_groups",
            "oar_max_source_event_ids",
            "oar_ai_timeout_sec",
            "oar_ai_max_calls_per_hour",
            "oar_ai_cache_ttl_sec",
            "oar_ai_max_context_chars",
            "oar_ai_max_output_chars",
            "oar_bridge_bootstrap_lookback_sec",
            "oar_bridge_max_signals_per_cycle",
            "oar_watch_max_active_tokens",
            "oar_watch_max_tokens_per_cycle",
            "oar_watch_scan_interval_sec",
            "oar_watch_live_poll_sec",
            "oar_watch_lease_sec",
            "oar_watch_max_consecutive_failures",
            "oar_watch_manual_ttl_sec",
            "oar_watch_launch_ttl_sec",
            "oar_watch_flow_ttl_sec",
            "oar_watch_funding_ttl_sec",
            "oar_watch_announcement_ttl_sec",
            "oar_watch_max_events_per_token",
            "oar_watch_max_rpc_requests_per_token",
            "oar_watch_top_transfers",
            "oar_watch_max_rpc_requests_per_cycle",
            "oar_telegram_query_poll_timeout_sec",
            "oar_telegram_query_cooldown_sec",
            "oar_telegram_query_max_per_hour",
            "oar_telegram_query_max_events",
            "oar_telegram_query_max_rpc_requests",
            "oar_telegram_query_top_n",
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
            "oar_ai_max_retries",
            "arkham_api_max_retries",
            "dune_api_max_retries",
            "oar_bridge_overlap_sec",
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
        query_caps = (
            (
                "TOKEN_ACTIVITY_MAX_WINDOW_HOURS",
                self.token_activity_max_window_hours,
                TOKEN_ACTIVITY_MAX_WINDOW_HOURS_HARD,
            ),
            (
                "TOKEN_ACTIVITY_MAX_EVENTS",
                self.token_activity_max_events,
                TOKEN_ACTIVITY_MAX_EVENTS_HARD,
            ),
            (
                "TOKEN_ACTIVITY_MAX_RPC_REQUESTS",
                self.token_activity_max_rpc_requests,
                TOKEN_ACTIVITY_MAX_RPC_REQUESTS_HARD,
            ),
            (
                "TOKEN_ACTIVITY_ADAPTIVE_MAX_REQUESTS",
                self.token_activity_adaptive_max_requests,
                TOKEN_ACTIVITY_ADAPTIVE_MAX_REQUESTS_HARD,
            ),
            (
                "TOKEN_ACTIVITY_MAX_UNIQUE_BLOCK_HEADERS",
                self.token_activity_max_unique_block_headers,
                TOKEN_ACTIVITY_MAX_UNIQUE_BLOCK_HEADERS_HARD,
            ),
            (
                "TOKEN_ACTIVITY_TOP_N",
                self.token_activity_top_n,
                TOKEN_ACTIVITY_TOP_N_HARD,
            ),
            (
                "TOKEN_ACTIVITY_BLOCK_SEARCH_MAX_CALLS",
                self.token_activity_block_search_max_calls,
                TOKEN_ACTIVITY_BLOCK_SEARCH_MAX_CALLS_HARD,
            ),
        )
        for name, value, hard_cap in query_caps:
            if value > hard_cap:
                raise SettingsValidationError(
                    f"{name} cannot exceed the hard cap {hard_cap}"
                )
        analysis_caps = (
            (
                "OAR_BEHAVIOR_MIN_TX",
                self.oar_behavior_min_tx,
                OAR_BEHAVIOR_MIN_TX_HARD,
            ),
            (
                "OAR_BEHAVIOR_MIN_ACTIVE_BUCKETS_1H",
                self.oar_behavior_min_active_buckets_1h,
                OAR_BEHAVIOR_MIN_ACTIVE_BUCKETS_HARD,
            ),
            (
                "OAR_BEHAVIOR_MIN_ACTIVE_BUCKETS_LONG",
                self.oar_behavior_min_active_buckets_long,
                OAR_BEHAVIOR_MIN_ACTIVE_BUCKETS_HARD,
            ),
            (
                "OAR_PATTERN_MIN_WALLETS",
                self.oar_pattern_min_wallets,
                OAR_PATTERN_MIN_WALLETS_HARD,
            ),
            (
                "OAR_PATTERN_MIN_TX",
                self.oar_pattern_min_tx,
                OAR_PATTERN_MIN_TX_HARD,
            ),
            (
                "OAR_WALLET_SYNC_WINDOW_SEC",
                self.oar_wallet_sync_window_sec,
                OAR_WALLET_SYNC_WINDOW_SEC_HARD,
            ),
            (
                "OAR_MAX_ANALYZED_WALLETS",
                self.oar_max_analyzed_wallets,
                OAR_MAX_ANALYZED_WALLETS_HARD,
            ),
            (
                "OAR_MAX_WALLET_GROUPS",
                self.oar_max_wallet_groups,
                OAR_MAX_WALLET_GROUPS_HARD,
            ),
            (
                "OAR_MAX_SOURCE_EVENT_IDS",
                self.oar_max_source_event_ids,
                OAR_MAX_SOURCE_EVENT_IDS_HARD,
            ),
        )
        for name, value, hard_cap in analysis_caps:
            if value > hard_cap:
                raise SettingsValidationError(
                    f"{name} cannot exceed the hard cap {hard_cap}"
                )
        for name, value, minimum, maximum in (
            (
                "OAR_BEHAVIOR_DOMINANCE_MIN",
                self.oar_behavior_dominance_min,
                Decimal("0.5"),
                Decimal("1"),
            ),
            (
                "OAR_PATTERN_MIN_AMOUNT_SHARE",
                self.oar_pattern_min_amount_share,
                Decimal("0"),
                Decimal("1"),
            ),
            (
                "OAR_WALLET_AMOUNT_SIMILARITY_TOLERANCE",
                self.oar_wallet_amount_similarity_tolerance,
                Decimal("0"),
                Decimal("0.50"),
            ),
        ):
            try:
                decimal_value = Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise SettingsValidationError(
                    f"{name} must be a finite decimal"
                ) from exc
            if (
                not decimal_value.is_finite()
                or decimal_value < minimum
                or decimal_value > maximum
            ):
                raise SettingsValidationError(
                    f"{name} must be in [{minimum}, {maximum}]"
                )
        allowed_bridge_modules = {
            "launch",
            "flow",
            "funding",
            "announcement",
        }
        if (
            not self.oar_bridge_allowed_modules
            or len(set(self.oar_bridge_allowed_modules))
            != len(self.oar_bridge_allowed_modules)
            or not set(self.oar_bridge_allowed_modules).issubset(
                allowed_bridge_modules
            )
        ):
            raise SettingsValidationError(
                "OAR_BRIDGE_ALLOWED_MODULES must be a unique non-empty subset "
                "of launch,flow,funding,announcement"
            )
        for name, value, hard_cap in (
            (
                "OAR_BRIDGE_MAX_SIGNALS_PER_CYCLE",
                self.oar_bridge_max_signals_per_cycle,
                OAR_BRIDGE_MAX_SIGNALS_PER_CYCLE_HARD,
            ),
            (
                "OAR_WATCH_MAX_ACTIVE_TOKENS",
                self.oar_watch_max_active_tokens,
                OAR_WATCH_MAX_ACTIVE_TOKENS_HARD,
            ),
            (
                "OAR_WATCH_MAX_TOKENS_PER_CYCLE",
                self.oar_watch_max_tokens_per_cycle,
                OAR_WATCH_MAX_TOKENS_PER_CYCLE_HARD,
            ),
            (
                "OAR_WATCH_MAX_RPC_REQUESTS_PER_CYCLE",
                self.oar_watch_max_rpc_requests_per_cycle,
                OAR_WATCH_MAX_RPC_REQUESTS_PER_CYCLE_HARD,
            ),
        ):
            if value > hard_cap:
                raise SettingsValidationError(
                    f"{name} cannot exceed the hard cap {hard_cap}"
                )
        for name, value, minimum, maximum in (
            ("OAR_BRIDGE_OVERLAP_SEC", self.oar_bridge_overlap_sec, 0, 3600),
            (
                "OAR_BRIDGE_BOOTSTRAP_LOOKBACK_SEC",
                self.oar_bridge_bootstrap_lookback_sec,
                60,
                86400,
            ),
            (
                "OAR_WATCH_SCAN_INTERVAL_SEC",
                self.oar_watch_scan_interval_sec,
                60,
                86400,
            ),
            (
                "OAR_WATCH_LIVE_POLL_SEC",
                self.oar_watch_live_poll_sec,
                10,
                3600,
            ),
            ("OAR_WATCH_LEASE_SEC", self.oar_watch_lease_sec, 60, 3600),
            (
                "OAR_WATCH_MAX_CONSECUTIVE_FAILURES",
                self.oar_watch_max_consecutive_failures,
                1,
                100,
            ),
            (
                "OAR_WATCH_MANUAL_TTL_SEC",
                self.oar_watch_manual_ttl_sec,
                60,
                365 * 86400,
            ),
            (
                "OAR_WATCH_LAUNCH_TTL_SEC",
                self.oar_watch_launch_ttl_sec,
                60,
                30 * 86400,
            ),
            (
                "OAR_WATCH_FLOW_TTL_SEC",
                self.oar_watch_flow_ttl_sec,
                60,
                30 * 86400,
            ),
            (
                "OAR_WATCH_FUNDING_TTL_SEC",
                self.oar_watch_funding_ttl_sec,
                60,
                30 * 86400,
            ),
            (
                "OAR_WATCH_ANNOUNCEMENT_TTL_SEC",
                self.oar_watch_announcement_ttl_sec,
                60,
                30 * 86400,
            ),
            (
                "OAR_WATCH_MAX_EVENTS_PER_TOKEN",
                self.oar_watch_max_events_per_token,
                1,
                TOKEN_ACTIVITY_MAX_EVENTS_HARD,
            ),
            (
                "OAR_WATCH_MAX_RPC_REQUESTS_PER_TOKEN",
                self.oar_watch_max_rpc_requests_per_token,
                1,
                TOKEN_ACTIVITY_MAX_RPC_REQUESTS_HARD,
            ),
            (
                "OAR_WATCH_TOP_TRANSFERS",
                self.oar_watch_top_transfers,
                1,
                TOKEN_ACTIVITY_TOP_N_HARD,
            ),
        ):
            if value < minimum or value > maximum:
                raise SettingsValidationError(
                    f"{name} must be in [{minimum}, {maximum}]"
                )
        for name, value in (
            ("OAR_WATCH_MANUAL_PRIORITY", self.oar_watch_manual_priority),
            ("OAR_WATCH_LAUNCH_PRIORITY", self.oar_watch_launch_priority),
            ("OAR_WATCH_FLOW_PRIORITY", self.oar_watch_flow_priority),
            ("OAR_WATCH_FUNDING_PRIORITY", self.oar_watch_funding_priority),
            (
                "OAR_WATCH_ANNOUNCEMENT_PRIORITY",
                self.oar_watch_announcement_priority,
            ),
            (
                "OAR_WATCH_NOTIFY_MIN_BEHAVIOR_SCORE",
                self.oar_watch_notify_min_behavior_score,
            ),
            (
                "OAR_WATCH_NOTIFY_MIN_WALLET_SCORE",
                self.oar_watch_notify_min_wallet_score,
            ),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                or value > 100
            ):
                raise SettingsValidationError(f"{name} must be in [0, 100]")
        if self.oar_watch_query_window not in {"15m", "1h", "4h", "24h"}:
            raise SettingsValidationError(
                "OAR_WATCH_QUERY_WINDOW must be 15m, 1h, 4h, or 24h"
            )
        if self.oar_watch_max_rpc_requests_per_cycle < (
            self.oar_watch_max_rpc_requests_per_token
        ):
            raise SettingsValidationError(
                "OAR_WATCH_MAX_RPC_REQUESTS_PER_CYCLE cannot be lower than "
                "OAR_WATCH_MAX_RPC_REQUESTS_PER_TOKEN"
            )
        if self.oar_automation_enable and (
            self.oar_watch_max_events_per_token
            > self.token_activity_max_events
            or self.oar_watch_max_rpc_requests_per_token
            > self.token_activity_max_rpc_requests
            or self.oar_watch_top_transfers > self.token_activity_top_n
        ):
            raise SettingsValidationError(
                "enabled OAR watch query budgets cannot exceed the configured "
                "Token Activity limits"
            )
        for name, value, minimum, maximum in (
            (
                "OAR_TELEGRAM_QUERY_POLL_TIMEOUT_SEC",
                self.oar_telegram_query_poll_timeout_sec,
                1,
                50,
            ),
            (
                "OAR_TELEGRAM_QUERY_COOLDOWN_SEC",
                self.oar_telegram_query_cooldown_sec,
                10,
                3600,
            ),
            (
                "OAR_TELEGRAM_QUERY_MAX_PER_HOUR",
                self.oar_telegram_query_max_per_hour,
                1,
                60,
            ),
            (
                "OAR_TELEGRAM_QUERY_MAX_EVENTS",
                self.oar_telegram_query_max_events,
                1,
                TOKEN_ACTIVITY_MAX_EVENTS_HARD,
            ),
            (
                "OAR_TELEGRAM_QUERY_MAX_RPC_REQUESTS",
                self.oar_telegram_query_max_rpc_requests,
                1,
                TOKEN_ACTIVITY_MAX_RPC_REQUESTS_HARD,
            ),
            (
                "OAR_TELEGRAM_QUERY_TOP_N",
                self.oar_telegram_query_top_n,
                1,
                TOKEN_ACTIVITY_TOP_N_HARD,
            ),
        ):
            if value < minimum or value > maximum:
                raise SettingsValidationError(
                    f"{name} must be in [{minimum}, {maximum}]"
                )
        if self.oar_telegram_query_enable and (
            self.oar_telegram_query_max_events
            > self.token_activity_max_events
            or self.oar_telegram_query_max_rpc_requests
            > self.token_activity_max_rpc_requests
            or self.oar_telegram_query_top_n > self.token_activity_top_n
        ):
            raise SettingsValidationError(
                "Telegram query budgets cannot exceed the configured "
                "Token Activity limits"
            )
        if self.oar_telegram_query_ack not in {
            "",
            OAR_TELEGRAM_QUERY_ACK,
        }:
            raise SettingsValidationError(
                "OAR_TELEGRAM_QUERY_ACK must be empty or the fixed phrase"
            )
        if self.oar_telegram_query_enable and not (
            self.oar_telegram_query_ack == OAR_TELEGRAM_QUERY_ACK
            and self.tg_bot_token
            and self.tg_chat_id
            and self.tg_onchain_flow_topic_id
        ):
            raise SettingsValidationError(
                "telegram_query_gate_blocked"
            )
        for name, value, minimum, maximum in (
            ("OAR_AI_TIMEOUT_SEC", self.oar_ai_timeout_sec, 5, 180),
            ("OAR_AI_MAX_RETRIES", self.oar_ai_max_retries, 0, 2),
            ("OAR_AI_MAX_TOKENS", self.oar_ai_max_tokens, 512, 32768),
            (
                "OAR_AI_MAX_CALLS_PER_HOUR",
                self.oar_ai_max_calls_per_hour,
                1,
                100,
            ),
            (
                "OAR_AI_CACHE_TTL_SEC",
                self.oar_ai_cache_ttl_sec,
                60,
                86400,
            ),
            (
                "OAR_AI_MAX_CONTEXT_CHARS",
                self.oar_ai_max_context_chars,
                5000,
                100000,
            ),
            (
                "OAR_AI_MAX_OUTPUT_CHARS",
                self.oar_ai_max_output_chars,
                1000,
                20000,
            ),
        ):
            if value < minimum or value > maximum:
                raise SettingsValidationError(
                    f"{name} must be in [{minimum}, {maximum}]"
                )
        if self.oar_ai_provider not in {"deepseek", "openai_compatible"}:
            raise SettingsValidationError(
                "OAR_AI_PROVIDER must be deepseek or openai_compatible"
            )
        if self.oar_ai_thinking_mode not in {"enabled", "disabled"}:
            raise SettingsValidationError(
                "OAR_AI_THINKING_MODE must be enabled or disabled"
            )
        if self.oar_ai_reasoning_effort not in {"high", "max"}:
            raise SettingsValidationError(
                "OAR_AI_REASONING_EFFORT must be high or max"
            )
        if (
            self.oar_ai_provider == "deepseek"
            and self.oar_ai_model
            and self.oar_ai_model not in {
                "deepseek-v4-pro",
                "deepseek-v4-flash",
            }
        ):
            raise SettingsValidationError(
                "DeepSeek OAR model must be deepseek-v4-pro or "
                "deepseek-v4-flash"
            )
        if self.oar_ai_base_url:
            ai_url = urlsplit(self.oar_ai_base_url)
            if (
                ai_url.scheme.lower() not in {"http", "https"}
                or not ai_url.hostname
                or ai_url.username is not None
                or ai_url.password is not None
                or bool(ai_url.query)
                or bool(ai_url.fragment)
            ):
                raise SettingsValidationError(
                    "OAR_AI_BASE_URL must be a credential-free HTTP(S) URL "
                    "without query or fragment"
                )
            if (
                ai_url.scheme.lower() == "http"
                and ai_url.hostname.lower()
                not in {"localhost", "127.0.0.1", "::1"}
            ):
                raise SettingsValidationError(
                    "OAR_AI_BASE_URL must use HTTPS unless it targets loopback"
                )
        if self.oar_ai_enable and not (
            self.oar_ai_base_url
            and self.oar_ai_api_key
            and self.oar_ai_model
        ):
            raise SettingsValidationError(
                "enabled OAR AI requires base URL, API key, and model"
            )
        if not 1 <= self.arkham_api_timeout_sec <= 60:
            raise SettingsValidationError(
                "ARKHAM_API_TIMEOUT_SEC must be in [1, 60]"
            )
        if not 0 <= self.arkham_api_max_retries <= 2:
            raise SettingsValidationError(
                "ARKHAM_API_MAX_RETRIES must be in [0, 2]"
            )
        if not 1 <= self.oar_label_candidate_max_addresses <= 100:
            raise SettingsValidationError(
                "OAR_LABEL_CANDIDATE_MAX_ADDRESSES must be in [1, 100]"
            )
        if not 1 <= self.dune_api_timeout_sec <= 60:
            raise SettingsValidationError(
                "DUNE_API_TIMEOUT_SEC must be in [1, 60]"
            )
        if not 0 <= self.dune_api_max_retries <= 2:
            raise SettingsValidationError(
                "DUNE_API_MAX_RETRIES must be in [0, 2]"
            )
        if not 4 <= self.dune_api_max_requests <= 40:
            raise SettingsValidationError(
                "DUNE_API_MAX_REQUESTS must be in [4, 40]"
            )
        if not Decimal("0.2") <= self.dune_api_poll_interval_sec <= Decimal("10"):
            raise SettingsValidationError(
                "DUNE_API_POLL_INTERVAL_SEC must be in [0.2, 10]"
            )
        if not 5 <= self.dune_api_execution_timeout_sec <= 120:
            raise SettingsValidationError(
                "DUNE_API_EXECUTION_TIMEOUT_SEC must be in [5, 120]"
            )
        if (
            Decimal(self.dune_api_max_requests - 2)
            * self.dune_api_poll_interval_sec
            < Decimal(self.dune_api_execution_timeout_sec)
        ):
            raise SettingsValidationError(
                "dune_poll_budget_inconsistent"
            )
        if not 1 <= self.dune_api_max_rows <= 500:
            raise SettingsValidationError(
                "DUNE_API_MAX_ROWS must be in [1, 500]"
            )
        arkham_url = urlsplit(self.arkham_api_base_url)
        if self.arkham_api_key and not self.arkham_api_base_url:
            raise SettingsValidationError(
                "ARKHAM_API_BASE_URL is required when Arkham is configured"
            )
        if self.arkham_api_base_url and (
            arkham_url.scheme.lower() != "https"
            or not arkham_url.hostname
            or arkham_url.username is not None
            or arkham_url.password is not None
            or bool(arkham_url.query)
            or bool(arkham_url.fragment)
        ):
            raise SettingsValidationError(
                "ARKHAM_API_BASE_URL must be a credential-free HTTPS URL "
                "without query or fragment"
            )
        dune_url = urlsplit(self.dune_api_base_url)
        if self.dune_api_key and not self.dune_api_base_url:
            raise SettingsValidationError(
                "DUNE_API_BASE_URL is required when Dune is configured"
            )
        if self.dune_api_base_url and (
            dune_url.scheme.lower() != "https"
            or not dune_url.hostname
            or dune_url.username is not None
            or dune_url.password is not None
            or bool(dune_url.query)
            or bool(dune_url.fragment)
        ):
            raise SettingsValidationError(
                "DUNE_API_BASE_URL must be a credential-free HTTPS URL "
                "without query or fragment"
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
        main_signal_path = self.main_signal_db_path.resolve()
        if main_signal_path == self.oar_automation_db_path.resolve() or (
            _is_relative_to(main_signal_path, self.data_dir.resolve())
        ):
            raise SettingsValidationError(
                "OAR_MAIN_SIGNAL_DB_FILE must remain outside the on-chain "
                "writable data directory"
            )

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
            "telegram_query": {
                "enabled": self.oar_telegram_query_enable,
                "ack_configured": bool(self.oar_telegram_query_ack),
                "poll_timeout_sec": self.oar_telegram_query_poll_timeout_sec,
                "cooldown_sec": self.oar_telegram_query_cooldown_sec,
                "max_per_hour": self.oar_telegram_query_max_per_hour,
                "state_file_exists": self.oar_telegram_query_state_path.exists(),
                "network_calls_without_explicit_command": 0,
            },
            "token_activity": {
                "max_window_hours": self.token_activity_max_window_hours,
                "max_events": self.token_activity_max_events,
                "max_rpc_requests": self.token_activity_max_rpc_requests,
                "adaptive_max_requests": (
                    self.token_activity_adaptive_max_requests
                ),
                "max_unique_block_headers": (
                    self.token_activity_max_unique_block_headers
                ),
                "top_n": self.token_activity_top_n,
                "block_search_max_calls": (
                    self.token_activity_block_search_max_calls
                ),
            },
            "arkham_intelligence": {
                "status": (
                    "configured"
                    if self.arkham_api_key
                    else "optional_disabled"
                ),
                "base_url_configured": bool(self.arkham_api_base_url),
                "api_key_configured": bool(self.arkham_api_key),
                "timeout_sec": self.arkham_api_timeout_sec,
                "max_retries": self.arkham_api_max_retries,
                "candidate_max_addresses": (
                    self.oar_label_candidate_max_addresses
                ),
                "candidate_file_exists": self.label_candidates_path.exists(),
                "automatic_calls": False,
            },
            "address_intelligence": {
                "status": "available",
                "store_exists": self.address_intelligence_path.exists(),
                "dune_status": (
                    "configured"
                    if self.dune_api_key
                    else "optional_disabled"
                ),
                "arkham_status": (
                    "configured"
                    if self.arkham_api_key
                    else "optional_disabled"
                ),
                "watch_external_provider_calls": False,
                "providers_optional": True,
            },
            "token_analysis": {
                "behavior_min_tx": self.oar_behavior_min_tx,
                "behavior_dominance_min": str(
                    self.oar_behavior_dominance_min
                ),
                "behavior_min_active_buckets_1h": (
                    self.oar_behavior_min_active_buckets_1h
                ),
                "behavior_min_active_buckets_long": (
                    self.oar_behavior_min_active_buckets_long
                ),
                "pattern_min_wallets": self.oar_pattern_min_wallets,
                "pattern_min_tx": self.oar_pattern_min_tx,
                "pattern_min_amount_share": str(
                    self.oar_pattern_min_amount_share
                ),
                "wallet_sync_window_sec": self.oar_wallet_sync_window_sec,
                "wallet_amount_similarity_tolerance": str(
                    self.oar_wallet_amount_similarity_tolerance
                ),
                "max_analyzed_wallets": self.oar_max_analyzed_wallets,
                "max_wallet_groups": self.oar_max_wallet_groups,
                "max_source_event_ids": self.oar_max_source_event_ids,
            },
            "oar_reporting": {
                "ai_enabled": self.oar_ai_enable,
                "ai_provider": self.oar_ai_provider,
                "ai_base_url_configured": bool(self.oar_ai_base_url),
                "ai_api_key_configured": bool(self.oar_ai_api_key),
                "ai_model_configured": bool(self.oar_ai_model),
                "ai_model": self.oar_ai_model,
                "ai_thinking_mode": self.oar_ai_thinking_mode,
                "ai_reasoning_effort": self.oar_ai_reasoning_effort,
                "ai_max_tokens": self.oar_ai_max_tokens,
                "ai_operator_prompt_configured": (
                    self.oar_ai_operator_prompt_path.exists()
                ),
                "ai_timeout_sec": self.oar_ai_timeout_sec,
                "ai_max_retries": self.oar_ai_max_retries,
                "ai_max_calls_per_hour": self.oar_ai_max_calls_per_hour,
                "ai_cache_ttl_sec": self.oar_ai_cache_ttl_sec,
                "replace_complete_card_with_partial": (
                    self.oar_replace_complete_card_with_partial
                ),
                "replace_rich_ai_card_with_rule_only": (
                    self.oar_replace_rich_ai_card_with_rule_only
                ),
            },
            "oar_automation": {
                "enabled": self.oar_automation_enable,
                "automation_db_exists": self.oar_automation_db_path.exists(),
                "main_signal_db_exists": self.main_signal_db_path.exists(),
                "allowed_modules": list(self.oar_bridge_allowed_modules),
                "bridge_overlap_sec": self.oar_bridge_overlap_sec,
                "bridge_bootstrap_lookback_sec": (
                    self.oar_bridge_bootstrap_lookback_sec
                ),
                "bridge_max_signals_per_cycle": (
                    self.oar_bridge_max_signals_per_cycle
                ),
                "watch_max_active_tokens": self.oar_watch_max_active_tokens,
                "watch_max_tokens_per_cycle": (
                    self.oar_watch_max_tokens_per_cycle
                ),
                "watch_scan_interval_sec": self.oar_watch_scan_interval_sec,
                "watch_live_poll_sec": self.oar_watch_live_poll_sec,
                "watch_query_window": self.oar_watch_query_window,
                "watch_max_events_per_token": (
                    self.oar_watch_max_events_per_token
                ),
                "watch_max_rpc_requests_per_token": (
                    self.oar_watch_max_rpc_requests_per_token
                ),
                "watch_max_rpc_requests_per_cycle": (
                    self.oar_watch_max_rpc_requests_per_cycle
                ),
                "notify_min_behavior_score": (
                    self.oar_watch_notify_min_behavior_score
                ),
                "notify_min_wallet_score": (
                    self.oar_watch_notify_min_wallet_score
                ),
                "notify_partial": self.oar_watch_notify_partial,
            },
        }
