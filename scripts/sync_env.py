#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import stat
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from shared.atomic_json import _file_lock, _fsync_parent


PRESERVE_KEYS = {
    "TG_BOT_TOKEN",
    "TG_CHAT_ID",
    "TG_TOPIC_ID",
    "TG_RADAR_SUMMARY_TOPIC_ID",
    "TG_LAUNCH_ALERT_TOPIC_ID",
    "TG_ANNOUNCEMENT_ALERT_TOPIC_ID",
    "TG_FLOW_RADAR_TOPIC_ID",
    "TG_FUNDING_ALERT_TOPIC_ID",
    "TG_CONSOLIDATION_BREAKOUT_TOPIC_ID",
    "TG_TEST_TOPIC_ID",
    "SIGNAL_EVENTS_DB_FILE",
    "DATABASE_BACKUP_DIR",
    "TG_OUTBOX_FILE",
}

RETIRED_KEYS = {
    "ACCUMULATION_QUALITY_V2_ENABLE",
    "TELEGRAM_ANNOUNCEMENT_ALERT_TOPIC_ID",
    "TG_AUTO_CREATE_TOPICS",
    "TG_TOPIC_INTRO_ENABLE",
    "SIGNAL_EVENTS_FILE",
    "REALTIME_BYBIT_ENABLE",
    "REALTIME_OKX_ENABLE",
    "BYBIT_PUBLIC_REST_URL",
    "BYBIT_LINEAR_WS_URL",
    "OKX_PUBLIC_REST_URL",
    "OKX_PUBLIC_WS_URL",
    "NEWS_EVENTS_DB_FILE",
    "NEWS_EVENTS_RETENTION_DAYS",
    "NEWS_EVENTS_LIMIT",
    "COINGLASS_ENABLE",
    "COINGLASS_API_KEY",
    "COINGLASS_API_BASE_URL",
    "COINGLASS_RATE_LIMIT_PER_MINUTE",
    "COINALYZE_ENABLE",
    "COINALYZE_API_KEY",
    "COINALYZE_BASE_URL",
    "COINALYZE_RATE_LIMIT_PER_MINUTE",
    "COINALYZE_REQUEST_BUDGET",
    "DERIVATIVES_VALIDATION_SYMBOL_LIMIT",
    "HEAT_CONTEXT_ENABLE",
    "HEAT_CONTEXT_CACHE_FILE",
    "HEAT_CONTEXT_CACHE_TTL_SEC",
    "HEAT_CONTEXT_TIMEOUT_SEC",
    "HEAT_CONTEXT_CANDIDATE_LIMIT",
    "HEAT_VOLUME_RATIO_MIN",
    "COINGECKO_API_BASE_URL",
    "BINANCE_SQUARE_HEAT_ENABLE",
    "BINANCE_SQUARE_HEAT_CANDIDATE_LIMIT",
    "FLOW_MODEL_COMPARISON_ENABLE",
    "FLOW_MODEL_COMPARISON_FILE",
    "FLOW_MODEL_COMPARISON_HISTORY_LIMIT",
    "ANNOUNCEMENT_ENRICHMENT_ENABLE",
    "ANNOUNCEMENT_ENRICHMENT_CACHE_FILE",
    "ANNOUNCEMENT_ENRICHMENT_CACHE_TTL_SEC",
    "ANNOUNCEMENT_ENRICHMENT_TIMEOUT_SEC",
    "ANNOUNCEMENT_ENRICHMENT_CANDIDATE_LIMIT",
    "ANNOUNCEMENT_ENRICHMENT_MAX_CONCURRENCY",
    "FLOW_CANDIDATE_POOL",
    "FUNDING_ALERT_REPLY_CHAIN_ENABLE",
    "LAUNCH_MULTI_EXCHANGE_FUNDING_ENABLE",
    "LAUNCH_ALERT_ENABLE",
    "LAUNCH_SCAN_LIMIT",
    "LAUNCH_FUNDING_EXCHANGES",
    "LAUNCH_FUNDING_HISTORY_LIMIT",
    "LAUNCH_STATE_FILE",
    "LAUNCH_WATCHLIST_FILE",
    "LAUNCH_WATCH_HISTORY_FILE",
    "LAUNCH_WATCH_HISTORY_LIMIT",
    "LAUNCH_MIN_SCORE_PUSH",
    "LAUNCH_WATCH_SCORE",
    "LAUNCH_PRIMED_SCORE",
    "LAUNCH_BREAKOUT_SCORE",
    "LAUNCH_LAUNCHED_SCORE",
    "LAUNCH_CLOSE_DELAY_SEC",
    "LAUNCH_STAGE_COOLDOWN_SEC",
    "LAUNCH_INVALIDATION_GRACE_SEC",
    "LAUNCH_LIFECYCLE_V2_ENABLE",
    "LAUNCH_LIFECYCLE_INVALID_WINDOWS",
    "LAUNCH_MESSAGE_PACKAGE_V2_ENABLE",
    "LAUNCH_PRICE_ACTION_V3_ENABLE",
    "LAUNCH_PA_BOX_LOOKBACK",
    "LAUNCH_PA_MAX_BOX_RANGE_PCT",
    "LAUNCH_PA_MIN_BODY_RATIO",
    "LAUNCH_PA_WICK_BODY_RATIO",
    "LAUNCH_CHART_V2_ENABLE",
    "LAUNCH_OUTCOME_V2_ENABLE",
    "LAUNCH_OUTCOME_FOLLOW_THROUGH_PCT",
    "LAUNCH_OUTCOME_MIN_SAMPLES",
    "LAUNCH_FUSION_ENABLE",
    "LAUNCH_DIRECTIONAL_ENABLE",
    "LAUNCH_DIRECTIONAL_MAX_CANDIDATES",
    "LAUNCH_AI_INTERPRETER_ENABLE",
    "LAUNCH_AI_AUTO_ENABLE",
    "TG_BOT_USERNAME",
    "AI_API_KEY",
    "AI_BASE_URL",
    "AI_MODEL",
    "AI_OPERATOR_PROMPT",
    "AI_TIMEOUT_SEC",
    "AI_ON_DEMAND_DAILY_LIMIT",
    "LAUNCH_SAME_STAGE_MIN_INTERVAL_SEC",
    "LAUNCH_PACKAGE_SCORE_DELTA",
    "LAUNCH_PACKAGE_PRICE_DELTA_PCT",
    "LAUNCH_PACKAGE_OI_DELTA_PCT",
    "LAUNCH_MESSAGE_CLEANUP_ENABLE",
    "LAUNCH_MESSAGE_CLEANUP_MAX_AGE_SEC",
    "LAUNCH_MESSAGE_CLEANUP_LIMIT",
    "LAUNCH_STATE_TTL_SEC",
    "LAUNCH_FAILED_TTL_SEC",
    "LAUNCH_SMC_V4_ENABLE",
    "LAUNCH_SMC_HISTORY_BARS",
    "LAUNCH_SMC_SWING_LENGTH",
    "LAUNCH_SMC_EQUAL_TOLERANCE_ATR",
    "LAUNCH_SMC_DISPLACEMENT_BODY_ATR",
    "LAUNCH_SMC_MAX_ZONE_AGE_BARS",
    "WEB_HOST",
    "WEB_PORT",
    "WEB_ADMIN_TOKEN",
    "WEB_AUTH_MODE",
    "WEB_ADMIN_USERNAME",
    "WEB_ADMIN_PASSWORD_HASH",
    "WEB_SESSION_SECRET",
    "WEB_SESSION_TTL_SEC",
    "WEB_AUTH_COOKIE_NAME",
    "WEB_AUTH_MAX_FAILURES",
    "WEB_AUTH_LOCKOUT_SEC",
    "WEB_AUTH_FAILURE_WINDOW_SEC",
    "WEB_AUTH_AUDIT_LIMIT",
    "WEB_SESSION_REFRESH_THRESHOLD_RATIO",
    "WEB_JOBS_DB_FILE",
    "WEB_JOBS_RETENTION_DAYS",
    "WEB_JOBS_LIMIT",
    "WEB_JOBS_STDOUT_TAIL_CHARS",
    "WEB_JOBS_STDERR_TAIL_CHARS",
    "PAOXX_PUBLIC_BASE_URL",
    "PUBLIC_API_RATE_LIMIT_PER_MINUTE",
    "PUBLIC_API_HEAVY_RATE_LIMIT_PER_MINUTE",
    "PUBLIC_API_TRUSTED_PROXY_IPS",
    "PAOXX_COCKPIT_V2_MODE",
    "AGENT_INSIGHTS_DB_FILE",
    "INFO_PUBLIC_SOURCES_ENABLE",
    "INFO_KOL_HANDLES",
    "INFO_PLAZA_FEED_URI",
    "AI_ASSISTANT_ENABLE",
    "AI_BOT_TOKEN",
    "AI_BOT_USERNAME",
    "AI_ADMIN_USER_IDS",
    "AI_ALLOW_GROUP_CHAT",
    "AI_ALLOWED_CHAT_IDS",
    "AI_PRICE_ALERTS_ENABLE",
    "AI_PRICE_ALERTS_DB_FILE",
    "AI_DEFAULT_CHAT_ID",
    "AI_ALERT_CHECK_INTERVAL_SEC",
    "AI_POLL_TIMEOUT_SEC",
    "AI_PROVIDER_ENABLE",
    "AI_REQUEST_TIMEOUT_SEC",
    "AI_PROMPTS_FILE",
    "TG_STRUCTURE_TOPIC_ID",
    "STRUCTURE_TOPIC_ID",
    "STRUCTURE_REVIEW_TOPIC_ID",
    "TG_STRUCTURE_REVIEW_TOPIC_ID",
    "STRUCTURE_RUNTIME_STATUS_FILE",
    "LIQUIDITY_FALLBACK_ENABLE",
    "LIQUIDITY_SCORE_MAX_DELTA",
    "LIQUIDITY_MIN_DISTANCE_PCT",
    "LIQUIDITY_MAX_DISTANCE_PCT",
    "BINANCE_ORDERBOOK_LIQUIDITY_ENABLE",
    "BINANCE_ORDERBOOK_DEPTH_LIMIT",
    "STRUCTURE_RADAR_ENABLE",
    "STRUCTURE_INTERVAL",
    "STRUCTURE_HIGHER_INTERVAL",
    "STRUCTURE_BOX_LOOKBACK",
    "STRUCTURE_TOP_SYMBOLS",
    "STRUCTURE_NEAR_EDGE_PCT",
    "STRUCTURE_MIN_SCORE",
    "STRUCTURE_SEND_CHART_TOP_N",
    "STRUCTURE_SAVE_CHARTS",
    "STRUCTURE_DELETE_CHART_AFTER_SEND",
    "STRUCTURE_CHART_RETENTION_HOURS",
    "STRUCTURE_MAX_CHART_FILES",
    "STRUCTURE_PRE_SCAN_MINUTE",
    "STRUCTURE_CONFIRM_DELAY_SEC",
    "STRUCTURE_COOLDOWN_SEC",
    "STRUCTURE_STATE_FILE",
    "STRUCTURE_HISTORY_FILE",
    "STRUCTURE_CHART_DIR",
    "STRUCTURE_REPLY_CHAIN_ENABLE",
    "STRUCTURE_REVIEW_ENABLE",
    "STRUCTURE_REVIEW_LOOKBACK_HOURS",
    "STRUCTURE_REVIEW_FORWARD_HOURS",
    "STRUCTURE_REVIEW_MIN_AGE_MINUTES",
    "STRUCTURE_REVIEW_REPORT_TOP_N",
    "STRUCTURE_REVIEW_MIN_SAMPLE",
    "STRUCTURE_REVIEW_MAX_REPORT_INTERVAL_SEC",
    "STRUCTURE_REVIEW_FILE",
    "STRUCTURE_STATS_FILE",
    "STRUCTURE_REVIEW_REPORT_FILE",
}

LEGACY_ALIASES = {
    "TELEGRAM_ANNOUNCEMENT_ALERT_TOPIC_ID": (
        "TG_ANNOUNCEMENT_ALERT_TOPIC_ID"
    ),
    "LAUNCH_ALERT_ENABLE": "PULSE_RADAR_ENABLE",
    "LAUNCH_SCAN_LIMIT": "SIMPLE_ALERT_SCAN_LIMIT",
    "LAUNCH_MESSAGE_CLEANUP_MAX_AGE_SEC": (
        "TOPIC_MESSAGE_CLEANUP_MAX_AGE_SEC"
    ),
}

MANAGED_MIGRATIONS = {
    "CONSOLIDATION_BREAKOUT_INTERVAL_SEC": {
        "old": {"", "900"},
        "new": "300",
        "note": "盘整突破雷达改为每 5 分钟轮转一个安全批次",
    },
    "CONSOLIDATION_BREAKOUT_SCAN_LIMIT": {
        "old": {"", "24"},
        "new": "40",
        "note": "每批 40 个合约，在默认 120 次 K 线预算内覆盖三个周期",
    },
    "CONSOLIDATION_BREAKOUT_MIN_QUOTE_VOLUME": {
        "old": {"", "5000000", "5000000.0"},
        "new": "0",
        "note": "0 表示不按 24 小时成交额排除活跃 USDT 永续合约",
    },
    "SIMPLE_ALERT_MIN_QUOTE_VOLUME": {
        "old": {"", "5000000", "5000000.0"},
        "new": "1000000",
        "note": "脉冲雷达旧 500 万美元默认值迁移为 100 万美元低流动性覆盖下限",
    },
    "SIMPLE_ALERT_SCAN_LIMIT": {
        "old": {"", "80", "120"},
        "new": "0",
        "note": "0 表示每 15 分钟覆盖全部合格加密合约，图表预算动态预留",
    },
    "SIGNAL_EVENTS_LIMIT": {
        "old": {"", "5000"},
        "new": "20000",
        "note": "P2 calibration retains enough structured signals for regime analysis",
    },
    "SIGNAL_EVENTS_RETENTION_DAYS": {
        "old": {"", "60"},
        "new": "365",
        "note": "P2 outcomes retain one year of calibration history",
    },
    "MARKET_SNAPSHOT_RETENTION_DAYS": {
        "old": {"", "30"},
        "new": "7",
        "note": "BOT-only snapshots retain seven days",
    },
    "MARKET_READINESS_TARGET_DAYS": {
        "old": {"", "30"},
        "new": "7",
        "note": "BOT-only readiness matches the seven-day snapshot horizon",
    },
    "REALTIME_MARKET_RETENTION_DAYS": {
        "old": {"", "7"},
        "new": "3",
        "note": "Realtime BOT intelligence uses a 24-hour window with a three-day safety buffer",
    },
    "BINANCE_FUTURES_WS_URL": {
        "old": {"", "wss://fstream.binance.com/ws", "wss://fstream.binance.com/stream"},
        "new": "wss://fstream.binance.com/market/ws",
        "note": "Binance USD-M market streams moved to the routed /market endpoint",
    },
    "RADAR_SUMMARY_MIN_INTERVAL_SEC": {
        "old": {"", "1800"},
        "new": "21600",
        "note": "资金摘要默认改为 6 小时一次",
    },
    "RADAR_SUMMARY_MAX_DAILY_PUSH": {
        "old": {"", "6"},
        "new": "4",
        "note": "资金摘要默认改为每天最多 4 次",
    },
    "FLOW_INTERVAL_SEC": {
        "old": {"", "900"},
        "new": "3600",
        "note": "资金流雷达默认改为每小时整点推送",
    },
    "ANNOUNCEMENT_PAGE_SIZE": {
        "old": {"", "20"},
        "new": "50",
        "note": "公告抓取默认扩大到 50 条",
    },
}

ENV_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
BACKUP_LIMIT = 30


def _chmod_600(path: Path) -> None:
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _require_600(path: Path) -> None:
    if os.name != "posix":
        return
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise PermissionError("environment_permissions_update_failed")


def _atomic_replace_bytes(path: Path, content: bytes) -> None:
    temporary_name = ""
    ownership: tuple[int, int] | None = None
    if path.exists() and hasattr(os, "chown"):
        stat = path.stat()
        ownership = (stat.st_uid, stat.st_gid)
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
        _require_600(path)
        try:
            _fsync_parent(path)
        except OSError:
            pass
    finally:
        if temporary_name:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


def _backup(path: Path, content: bytes) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = path.with_name(f"{path.name}.bak.{stamp}")
    _atomic_replace_bytes(backup, content)
    if hasattr(os, "chown"):
        current = path.stat()
        os.chown(backup, current.st_uid, current.st_gid)
    backups = sorted(
        path.parent.glob(f"{path.name}.bak.*"),
        key=lambda item: item.name,
        reverse=True,
    )
    for expired in backups[BACKUP_LIMIT:]:
        if expired.parent == path.parent and expired.is_file() and not expired.is_symlink():
            expired.unlink()
    return backup


def split_env_line(line: str) -> tuple[str, str] | None:
    match = ENV_LINE_RE.match(line)
    if not match:
        return None
    return match.group(1), match.group(2).strip()


def clean_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value


def env_index(lines: list[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for idx, line in enumerate(lines):
        parsed = split_env_line(line)
        if parsed:
            key, _value = parsed
            result.setdefault(key, idx)
    return result


def migrate_legacy_aliases(
    lines: list[str],
) -> tuple[list[str], list[str]]:
    index = env_index(lines)
    updated: list[str] = []
    for legacy_key, canonical_key in LEGACY_ALIASES.items():
        legacy_position = index.get(legacy_key)
        if legacy_position is None:
            continue
        parsed = split_env_line(lines[legacy_position])
        legacy_value = clean_value(parsed[1] if parsed else "")
        if not legacy_value:
            continue
        canonical_position = index.get(canonical_key)
        if canonical_position is None:
            lines.append(f"{canonical_key}={legacy_value}")
            index[canonical_key] = len(lines) - 1
            updated.append(canonical_key)
            continue
        canonical = split_env_line(lines[canonical_position])
        if not clean_value(canonical[1] if canonical else ""):
            lines[canonical_position] = f"{canonical_key}={legacy_value}"
            updated.append(canonical_key)
    return lines, updated


def example_values(path: Path) -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        parsed = split_env_line(line)
        if parsed:
            values.append(parsed)
    return values


def sync_env(env_path: Path, example_path: Path) -> dict[str, list[str]]:
    env_path.parent.mkdir(parents=True, exist_ok=True)
    with _file_lock(env_path):
        existed = env_path.exists()
        original = env_path.read_bytes() if existed else b""
        lines = original.decode("utf-8").splitlines() if existed else []
        lines, alias_updates = migrate_legacy_aliases(lines)
        removed = [key for line in lines if (parsed := split_env_line(line)) and (key := parsed[0]) in RETIRED_KEYS]
        lines = [line for line in lines if not ((parsed := split_env_line(line)) and parsed[0] in RETIRED_KEYS)]
        index = env_index(lines)
        added: list[str] = []
        updated: list[str] = list(alias_updates)
        preserved: list[str] = []

        for key, value in example_values(example_path):
            if key not in index:
                lines.append(f"{key}={value}")
                index[key] = len(lines) - 1
                added.append(key)

        for key, rule in MANAGED_MIGRATIONS.items():
            if key not in index:
                continue
            if key in PRESERVE_KEYS:
                preserved.append(key)
                continue
            parsed = split_env_line(lines[index[key]])
            current = clean_value(parsed[1] if parsed else "")
            if current in rule["old"] and current != rule["new"]:
                lines[index[key]] = f"{key}={rule['new']}"
                updated.append(key)

        content = ("\n".join(lines) + "\n").encode("utf-8")
        if content != original:
            if existed:
                _backup(env_path, original)
            _atomic_replace_bytes(env_path, content)
        _chmod_600(env_path)
        _chmod_600(env_path.with_name(f"{env_path.name}.lock"))
        _require_600(env_path)
        _require_600(env_path.with_name(f"{env_path.name}.lock"))
    return {"added": added, "updated": updated, "preserved": preserved, "removed": removed}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Safely sync config/.env.oi with its example"
    )
    parser.add_argument(
        "--env",
        default="config/.env.oi",
        help="Path to real env file",
    )
    parser.add_argument(
        "--example",
        default="config/.env.oi.example",
        help="Path to env template",
    )
    args = parser.parse_args()

    result = sync_env(Path(args.env), Path(args.example))
    print(
        "env_sync: "
        f"added={len(result['added'])} "
        f"updated={len(result['updated'])} "
        f"preserved={len(result['preserved'])} "
        f"removed={len(result['removed'])}"
    )
    if result["updated"]:
        print("env_sync updated: " + ", ".join(result["updated"]))
    if result["added"]:
        print("env_sync added: " + ", ".join(result["added"]))
    if result["removed"]:
        print("env_sync removed retired keys: " + ", ".join(result["removed"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
