#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path


PRESERVE_KEYS = {
    "TG_BOT_TOKEN",
    "TG_CHAT_ID",
    "TG_TOPIC_ID",
    "TG_RADAR_SUMMARY_TOPIC_ID",
    "TG_LAUNCH_ALERT_TOPIC_ID",
    "TG_ANNOUNCEMENT_ALERT_TOPIC_ID",
    "TG_FLOW_RADAR_TOPIC_ID",
    "TG_FUNDING_ALERT_TOPIC_ID",
    "TG_STRUCTURE_TOPIC_ID",
    "STRUCTURE_TOPIC_ID",
    "STRUCTURE_REVIEW_TOPIC_ID",
    "TG_STRUCTURE_REVIEW_TOPIC_ID",
    "TG_TEST_TOPIC_ID",
    "COINALYZE_API_KEY",
    "COINALYZE_ENABLE",
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
    "AI_ASSISTANT_ENABLE",
    "AI_BOT_TOKEN",
    "AI_ADMIN_USER_IDS",
    "AI_ALLOWED_CHAT_IDS",
    "AI_DEFAULT_CHAT_ID",
    "AI_API_KEY",
    "AI_PROVIDER_ENABLE",
    "AI_MODEL",
    "AI_PROMPTS_FILE",
    "SIGNAL_EVENTS_FILE",
    "SIGNAL_EVENTS_DB_FILE",
    "WEB_JOBS_DB_FILE",
    "WEB_JOBS_RETENTION_DAYS",
    "WEB_JOBS_LIMIT",
    "WEB_JOBS_STDOUT_TAIL_CHARS",
    "WEB_JOBS_STDERR_TAIL_CHARS",
    "SIGNAL_EVENTS_LIMIT",
    "SIGNAL_EVENTS_RETENTION_DAYS",
}

MANAGED_MIGRATIONS = {
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
    "WEB_HOST": {
        "old": {"", "127.0.0.1", "localhost"},
        "new": "0.0.0.0",
        "note": "正式入口为 https://paoxx.com/admin；8080 仅作为本机/Nginx 反代后端入口",
    },
    "WEB_PORT": {
        "old": {"", "80"},
        "new": "8080",
        "note": "Web 控制台默认使用 8080 端口",
    },
    "AI_REQUEST_TIMEOUT_SEC": {
        "old": {"", "20"},
        "new": "90",
        "note": "DeepSeek v4-pro 思考模式默认超时提高到 90 秒",
    },
}

ENV_LINE_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


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
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    index = env_index(lines)
    added: list[str] = []
    updated: list[str] = []
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

    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"added": added, "updated": updated, "preserved": preserved}


def main() -> int:
    parser = argparse.ArgumentParser(description="Safely sync .env.oi with .env.oi.example")
    parser.add_argument("--env", default=".env.oi", help="Path to real env file")
    parser.add_argument("--example", default=".env.oi.example", help="Path to env template")
    args = parser.parse_args()

    result = sync_env(Path(args.env), Path(args.example))
    print(
        "env_sync: "
        f"added={len(result['added'])} "
        f"updated={len(result['updated'])} "
        f"preserved={len(result['preserved'])}"
    )
    if result["updated"]:
        print("env_sync updated: " + ", ".join(result["updated"]))
    if result["added"]:
        print("env_sync added: " + ", ".join(result["added"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
